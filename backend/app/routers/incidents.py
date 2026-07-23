from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    FireDetection,
    FireIncident,
    IncidentEvent,
    RegionalIncident,
    SatelliteScene,
    TelegramMessage,
)
from app.schemas import (
    FireIncidentOut,
    IncidentDetectionSourceCount,
    IncidentEventOut,
    IncidentMergeRequest,
    IncidentRenameRequest,
    IncidentReportOut,
    RankedIncidentOut,
)
from app.services.area_estimate import estimate_area_ha
from app.services.incidents import INCIDENT_REASSOCIATION_DEG
from app.services.incidents import merge_incidents as _merge_incidents_core
from app.services.incidents import merge_reassociable_incidents as _merge_reassociable_incidents

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

# Earliest data this monitor has ever recorded (first FIRMS ingestion run) -
# rebuild_incidents only re-clusters detections within a rolling 30-day
# window (INCIDENTS_WINDOW_HOURS), but FireIncident rows themselves are never
# deleted once created, only re-labeled active/cooling/archived - so ranking
# across "all incidents ever stored" is honest and doesn't fabricate a
# multi-year "season" this app hasn't actually been running for.
_SORT_METRICS = {
    "severity": lambda inc: inc.severity_score,
    "area": lambda inc: inc.area_ha or 0.0,
    "detections": lambda inc: inc.detection_count,
    "duration": lambda inc: (inc.last_detected_at - inc.first_detected_at).total_seconds(),
}

_SOURCE_FLAG_EVENT_TYPES = {
    "regional_status": "has_regional_status",
    "telegram_message": "has_telegram_mentions",
    "satellite_imagery": "has_satellite_imagery",
    "ems_activation": "has_ems_activation",
}


def _with_source_flags(db: Session, incidents: list[FireIncident]) -> list[FireIncidentOut]:
    """Enriches each incident with which non-satellite sources have contributed
    to it, in one bulk query rather than a per-incident timeline fetch."""
    ids = [incident.id for incident in incidents]
    flags_by_incident: dict[int, set[str]] = defaultdict(set)
    if ids:
        rows = (
            db.query(IncidentEvent.incident_id, IncidentEvent.event_type)
            .filter(
                IncidentEvent.incident_id.in_(ids),
                IncidentEvent.event_type.in_(_SOURCE_FLAG_EVENT_TYPES.keys()),
            )
            .distinct()
            .all()
        )
        for incident_id, event_type in rows:
            flags_by_incident[incident_id].add(event_type)

    result = []
    for incident in incidents:
        present = flags_by_incident.get(incident.id, set())
        out = FireIncidentOut.model_validate(incident)
        result.append(
            out.model_copy(
                update={
                    field: (event_type in present)
                    for event_type, field in _SOURCE_FLAG_EVENT_TYPES.items()
                }
            )
        )
    return result


def _dedupe_by_place(incidents: list[FireIncident]) -> list[FireIncident]:
    """
    rebuild_incidents' reassociation logic can occasionally leave more than
    one FireIncident row describing what is really the same real-world fire
    (confirmed live: several archived rows sharing identical locality,
    severity_score, and detection_count - a reassociation-window edge case,
    not intentional re-detection). A rankings page reads as broken if the
    same fire occupies 5+ rows, so this collapses rows sharing a
    (locality, province, first-detection day) key down to the richest one
    (highest detection_count, tie-broken by newest id) - one row per real
    fire rather than one row per database record.
    """
    best: dict[tuple, FireIncident] = {}
    for inc in incidents:
        key = (inc.locality or "", inc.province or "", inc.first_detected_at.date())
        current = best.get(key)
        if current is None or (inc.detection_count, inc.id) > (current.detection_count, current.id):
            best[key] = inc
    return list(best.values())


def _detections_near_incident(db: Session, incident: FireIncident) -> list[tuple[str, float, float]]:
    """
    FireDetection rows aren't foreign-keyed to a FireIncident - incidents are
    built by re-clustering raw detections by proximity on every scheduler
    pass (see rebuild_incidents), so there's no stored "detections for
    incident X" set to just query. This re-runs that same proximity test
    (using the wider INCIDENT_REASSOCIATION_DEG radius rebuild_incidents
    itself uses for cluster membership) against the incident's own centroid
    and active window. It's best-effort, not an authoritative stored set -
    two incidents whose windows/areas overlap could double-count a
    detection. Shared by both _detection_source_breakdown (which source) and
    _estimate_incident_area_ha (spatial extent) so they don't each re-run the
    query independently.
    """
    window_start = incident.first_detected_at - timedelta(hours=1)
    window_end = incident.last_detected_at + timedelta(hours=1)
    candidates = (
        db.query(FireDetection.source, FireDetection.latitude, FireDetection.longitude)
        .filter(FireDetection.acquired_at >= window_start, FireDetection.acquired_at <= window_end)
        .all()
    )
    return [
        (source, lat, lon)
        for source, lat, lon in candidates
        if ((lat - incident.centroid_lat) ** 2 + (lon - incident.centroid_lon) ** 2) ** 0.5
        <= INCIDENT_REASSOCIATION_DEG
    ]


def _detection_source_breakdown(db: Session, incident: FireIncident) -> list[IncidentDetectionSourceCount]:
    """Which satellites/sources picked up this fire - see _detections_near_incident."""
    counts: dict[str, int] = defaultdict(int)
    for source, _lat, _lon in _detections_near_incident(db, incident):
        counts[source] += 1
    return [
        IncidentDetectionSourceCount(source=source, count=count)
        for source, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _estimate_incident_area_ha(db: Session, incident: FireIncident) -> float | None:
    """
    Best-effort hectare estimate for incidents with no official EFFIS
    area_ha (see area_estimate.estimate_area_ha's module docstring for why
    this exists and how it differs from the map's client-side estimate).
    Only worth computing when the official figure is actually missing -
    EFFIS's own reported area always takes precedence over an estimate.
    """
    if incident.area_ha is not None:
        return None
    points = [(lat, lon) for _source, lat, lon in _detections_near_incident(db, incident)]
    return estimate_area_ha(points)


@router.get("/rankings", response_model=list[RankedIncidentOut])
def get_incident_rankings(
    sort: str = Query(
        "severity",
        description="'severity' (composite score), 'area' (hectares burned - EFFIS-reported incidents only), "
        "'detections' (satellite hotspot count), or 'duration' (longest active)",
    ),
    days: Optional[int] = Query(
        None,
        ge=1,
        le=3650,
        description="Restrict to incidents last detected within N days; omit to rank every incident ever stored by this monitor",
    ),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if sort not in _SORT_METRICS:
        raise HTTPException(status_code=400, detail=f"Unknown sort '{sort}' - use severity, area, detections, or duration")

    query = db.query(FireIncident)
    if days is not None:
        since = datetime.utcnow() - timedelta(days=days)
        query = query.filter(FireIncident.last_detected_at >= since)
    if sort == "area":
        query = query.filter(FireIncident.area_ha.isnot(None))

    deduped = _dedupe_by_place(query.all())
    ranked = sorted(deduped, key=_SORT_METRICS[sort], reverse=True)[:limit]
    enriched = _with_source_flags(db, ranked)

    result = []
    for position, (incident, out) in enumerate(zip(ranked, enriched), start=1):
        duration_hours = (incident.last_detected_at - incident.first_detected_at).total_seconds() / 3600
        area_ha_estimated = _estimate_incident_area_ha(db, incident)
        result.append(
            RankedIncidentOut(
                **out.model_dump(exclude={"area_ha_estimated"}),
                area_ha_estimated=area_ha_estimated,
                rank=position,
                duration_hours=round(duration_hours, 1),
            )
        )
    return result


@router.get("", response_model=list[FireIncidentOut])
def list_incidents(
    status: Optional[str] = Query(None, description="Filter by 'active', 'cooling', or 'archived'"),
    hours: int = Query(24 * 30, ge=1, le=24 * 30, description="Only incidents last detected in the last N hours"),
    sort: str = Query("severity", description="'severity' (default) or 'recent'"),
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(hours=hours)
    query = db.query(FireIncident).filter(FireIncident.last_detected_at >= since)
    if status:
        query = query.filter(FireIncident.status == status.lower())
    if sort == "recent":
        query = query.order_by(FireIncident.last_detected_at.desc())
    else:
        query = query.order_by(FireIncident.severity_score.desc())
    return _with_source_flags(db, query.all())


@router.get("/{incident_id}", response_model=FireIncidentOut)
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _with_source_flags(db, [incident])[0]


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventOut])
def get_incident_timeline(
    incident_id: int,
    hours: Optional[int] = Query(
        None, ge=1, le=24 * 30, description="Only events in the last N hours - matches the map's date-range filter"
    ),
    db: Session = Depends(get_db),
):
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    query = db.query(IncidentEvent).filter(IncidentEvent.incident_id == incident_id)
    if hours is not None:
        since = datetime.utcnow() - timedelta(hours=hours)
        query = query.filter(IncidentEvent.occurred_at >= since)
    return query.order_by(IncidentEvent.occurred_at.asc()).all()


@router.get("/{incident_id}/report", response_model=IncidentReportOut)
def get_incident_report(incident_id: int, db: Session = Depends(get_db)):
    """
    Assembles the full per-incident dossier server-side (core stats, full
    timeline, matched regional operational status/personnel, satellite
    scenes, Telegram mentions, best-effort detection-source breakdown) in one
    request, rather than making the frontend's report page repeat the map
    sidebar's 5-6 separate lazy fetches.
    """
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident_out = _with_source_flags(db, [incident])[0]
    incident_out = incident_out.model_copy(update={"area_ha_estimated": _estimate_incident_area_ha(db, incident)})
    timeline = (
        db.query(IncidentEvent)
        .filter(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.occurred_at.asc())
        .all()
    )
    regional_status = (
        db.query(RegionalIncident)
        .filter(RegionalIncident.matched_incident_id == incident_id)
        .order_by(RegionalIncident.updated_at.desc())
        .all()
    )
    satellite_scenes = (
        db.query(SatelliteScene)
        .filter(SatelliteScene.incident_id == incident_id)
        .order_by(SatelliteScene.captured_at.desc())
        .all()
    )
    telegram_messages = (
        db.query(TelegramMessage)
        .filter(TelegramMessage.matched_incident_id == incident_id)
        .order_by(TelegramMessage.posted_at.desc())
        .all()
    )
    duration_hours = (incident.last_detected_at - incident.first_detected_at).total_seconds() / 3600

    return IncidentReportOut(
        incident=incident_out,
        duration_hours=round(duration_hours, 1),
        timeline=timeline,
        regional_status=regional_status,
        satellite_scenes=satellite_scenes,
        telegram_messages=telegram_messages,
        detection_sources=_detection_source_breakdown(db, incident),
    )


@router.patch("/{incident_id}", response_model=FireIncidentOut)
def rename_incident(incident_id: int, body: IncidentRenameRequest, db: Session = Depends(get_db)):
    """
    Manual admin override for an incident's display name - independent of
    whatever Nominatim's reverse geocode resolved (see models.FireIncident.
    official_name). Pass null to clear the override and fall back to
    `locality` again.
    """
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    name = body.official_name.strip() if body.official_name else None
    incident.official_name = name or None
    incident.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(incident)
    return _with_source_flags(db, [incident])[0]


@router.post("/merge", response_model=FireIncidentOut)
def merge_incidents(body: IncidentMergeRequest, db: Session = Depends(get_db)):
    """
    Manual admin intervention for when rebuild_incidents' automated
    proximity matching either created duplicate rows or (before its
    archived-reassociation fix) split one real fire into several rows -
    e.g. the "IF Los Gallardos" fire that landed across 3 FireIncident rows.
    Validates the request, then delegates the actual reassignment/stat
    combination/deletion to services.incidents.merge_incidents - see that
    function's docstring for the stat-combination judgment calls.
    """
    unique_ids = list(dict.fromkeys(body.incident_ids))
    if len(unique_ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 distinct incident_ids to merge")

    incidents = db.query(FireIncident).filter(FireIncident.id.in_(unique_ids)).all()
    if len(incidents) != len(unique_ids):
        found_ids = {inc.id for inc in incidents}
        missing = [i for i in unique_ids if i not in found_ids]
        raise HTTPException(status_code=404, detail=f"Incident(s) not found: {missing}")

    survivor_id = body.survivor_id if body.survivor_id is not None else max(incidents, key=lambda inc: inc.detection_count).id
    if survivor_id not in unique_ids:
        raise HTTPException(status_code=400, detail="survivor_id must be one of incident_ids")

    absorbed_ids = [i for i in unique_ids if i != survivor_id]
    survivor = _merge_incidents_core(db, survivor_id, absorbed_ids, official_name=body.official_name)
    return _with_source_flags(db, [survivor])[0]


@router.post("/reassociate")
def reassociate_incidents(db: Session = Depends(get_db)):
    """
    Manual trigger for the retroactive auto-merge pass (see
    services.incidents.merge_reassociable_incidents) - lets an admin run it
    on demand instead of waiting for the next incident_rebuild scheduler
    tick, e.g. right after confirming a specific pair (like #53/#1439)
    should already qualify.
    """
    absorbed = _merge_reassociable_incidents(db)
    return {"incidents_absorbed": absorbed}
