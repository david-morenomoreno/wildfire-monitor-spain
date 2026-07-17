import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import FireIncident, IncidentEvent, RegionalIncident, RegionalIncidentSource as RegionalIncidentSourceModel
from app.services.health import record_check
from app.services.incidents import REGION_LINK_DEG
from app.services.regional_incidents.base import RegionalFireRecord, RegionalIncidentSource
from app.services.regional_incidents.registry import REGION_SOURCES

logger = logging.getLogger(__name__)


def _get_or_create_source(db: Session, source: RegionalIncidentSource) -> RegionalIncidentSourceModel:
    row = db.query(RegionalIncidentSourceModel).filter_by(region_code=source.region_code).first()
    if row is None:
        row = RegionalIncidentSourceModel(
            region_code=source.region_code,
            name=source.name,
            portal_url=source.portal_url,
        )
        db.add(row)
        db.flush()
    return row


def _find_matching_incident(db: Session, lat: float | None, lon: float | None) -> int | None:
    """Best-effort match to a FireIncident by centroid proximity - the region's own
    coordinates are authoritative, so this is more reliable than Telegram's
    text-based matching, but still not guaranteed (satellite detections and
    an official report can legitimately disagree on exact location)."""
    if lat is None or lon is None:
        return None
    incidents = db.query(FireIncident).filter(FireIncident.status != "archived").all()
    best_id, best_dist = None, REGION_LINK_DEG
    for incident in incidents:
        dist = ((incident.centroid_lat - lat) ** 2 + (incident.centroid_lon - lon) ** 2) ** 0.5
        if dist <= best_dist:
            best_dist, best_id = dist, incident.id
    return best_id


def _personnel_description(record: RegionalFireRecord) -> str | None:
    if not record.personnel_summary:
        return None
    total = record.personnel_summary.get("total_actuando", 0)
    breakdown = ", ".join(
        f"{count} {category}"
        for category, count in record.personnel_summary.items()
        if category != "total_actuando" and count
    )
    if not total:
        return None
    return f"{total} medio(s) desplegado(s)" + (f" ({breakdown})" if breakdown else "")


def sync_region(db: Session, region_code: str) -> int:
    """
    Fetches live per-fire status for one region, upserts RegionalIncident
    rows, and appends an IncidentEvent to a matched FireIncident's timeline
    whenever a fire is first seen or its status changes. Returns the count
    of new-or-changed records.
    """
    source = REGION_SOURCES.get(region_code)
    if source is None:
        raise KeyError(f"No regional incident source registered for '{region_code}'")

    source_row = _get_or_create_source(db, source)
    try:
        fetched = source.fetch()
    except Exception as exc:
        record_check(db, f"regional:{region_code}", "disrupted", str(exc))
        raise

    # Some regions' underlying views return the same external_id more than
    # once in a single query (confirmed live for Catalunya's Bombers view,
    # which joins/duplicates rows) - dedupe before upserting, or a batch with
    # two "new" rows sharing an external_id violates the unique constraint.
    records = list({record.external_id: record for record in fetched}.values())

    existing = {
        row.external_id: row
        for row in db.query(RegionalIncident).filter_by(source_id=source_row.id).all()
    }

    changed_count = 0
    now = datetime.utcnow()
    for record in records:
        existing_row = existing.get(record.external_id)
        personnel_json = json.dumps(record.personnel_summary)

        if existing_row is None:
            matched_id = _find_matching_incident(db, record.latitude, record.longitude)
            row = RegionalIncident(
                source_id=source_row.id,
                external_id=record.external_id,
                status=record.status,
                municipality=record.municipality,
                province=record.province,
                latitude=record.latitude,
                longitude=record.longitude,
                started_at=record.started_at,
                controlled_at=record.controlled_at,
                extinguished_at=record.extinguished_at,
                area_ha=record.area_ha,
                cause=record.cause,
                personnel_summary=personnel_json,
                matched_incident_id=matched_id,
                raw_json=json.dumps(record.raw, default=str),
                fetched_at=now,
                updated_at=now,
            )
            db.add(row)
            if matched_id is not None:
                db.add(
                    IncidentEvent(
                        incident_id=matched_id,
                        occurred_at=record.started_at or now,
                        event_type="regional_status",
                        source=region_code,
                        title=f"Estado oficial: {record.status}"
                        + (f" ({record.municipality})" if record.municipality else ""),
                        description=_personnel_description(record),
                    )
                )
            changed_count += 1
            continue

        status_changed = existing_row.status != record.status
        existing_row.status = record.status
        existing_row.municipality = record.municipality or existing_row.municipality
        existing_row.province = record.province or existing_row.province
        if record.latitude is not None:
            existing_row.latitude = record.latitude
            existing_row.longitude = record.longitude
        existing_row.controlled_at = record.controlled_at
        existing_row.extinguished_at = record.extinguished_at
        existing_row.area_ha = record.area_ha or existing_row.area_ha
        existing_row.cause = record.cause or existing_row.cause
        existing_row.personnel_summary = personnel_json
        existing_row.raw_json = json.dumps(record.raw, default=str)
        existing_row.updated_at = now
        if existing_row.matched_incident_id is None:
            existing_row.matched_incident_id = _find_matching_incident(db, record.latitude, record.longitude)

        if status_changed and existing_row.matched_incident_id is not None:
            db.add(
                IncidentEvent(
                    incident_id=existing_row.matched_incident_id,
                    occurred_at=now,
                    event_type="regional_status",
                    source=region_code,
                    title=f"Estado oficial actualizado: {record.status}",
                    description=_personnel_description(record),
                )
            )
            changed_count += 1

    db.commit()
    record_check(db, f"regional:{region_code}", "ok", f"{changed_count} new/changed fires")
    return changed_count


def sync_all_regions(db: Session) -> dict[str, int]:
    # sync_region already records its own health check (ok on success,
    # disrupted if source.fetch() itself fails) - this just logs so we don't
    # double-record the same failure.
    results = {}
    for region_code in REGION_SOURCES:
        try:
            results[region_code] = sync_region(db, region_code)
        except Exception:
            logger.exception("Regional incident sync failed for '%s'", region_code)
            results[region_code] = 0
    return results
