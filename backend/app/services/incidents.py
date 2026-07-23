import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models import (
    CopernicusEmsActivation,
    FireDetection,
    FireIncident,
    IncidentEvent,
    RegionalIncident,
    SatelliteScene,
    TelegramMessage,
)
from app.services.geocode import reverse_geocode

logger = logging.getLogger(__name__)

# Same threshold as the frontend's REGION_LINK_DEG (app.js) so server-side
# incidents line up with the polygons already rendered on the map. This stays
# tight - it's what decides which raw detections chain together into one
# cluster's shape, and a wider value here would blur genuinely separate
# nearby fires into one blob.
REGION_LINK_DEG = 0.03

# Wider, separate threshold used ONLY when deciding whether a NEW cluster of
# detections belongs to an EXISTING incident (not for clustering raw points
# together - see REGION_LINK_DEG above). A real fire can spot/jump several km
# ahead of its main front - confirmed live: La Mierla (Guadalajara) had a
# satellite pass detect new hotspots ~6.3km from the original cluster's
# centroid, which fell outside REGION_LINK_DEG (~3.3km) and so created a
# separate, oddly-named "Arbancón" incident instead of being recognized as
# the same fire continuing to spread. ~16.7km comfortably covers a real
# multi-hour spread/jump without being so wide it'd start merging distinct
# fires in the same general area.
INCIDENT_REASSOCIATION_DEG = 0.15

INCIDENTS_WINDOW_HOURS = 24 * 30  # matches the UI's longest "date range" option

ACTIVE_AFTER_HOURS = 24
COOLING_AFTER_HOURS = 24 * 7

# Archived incidents ARE eligible for reassociation (a real fire can flare up
# again well after it's gone quiet and auto-archived - confirmed live: the
# "IF Los Gallardos" fire near Lubrín/Bédar (Almería) went quiet for ~2 days,
# auto-archived, then a genuine new detection cluster landed a few hundred
# metres away days later and - because archived incidents were excluded from
# matching entirely - created a brand-new incident row instead of
# reactivating the archived one). But an incident archived a long time ago
# (e.g. last season) shouldn't be reactivated just because a new, unrelated
# fire starts nearby - so only incidents archived "recently" are considered.
# 45 days comfortably covers a lull-then-reflare within the same fire season
# without reaching back into an entirely separate season/ignition.
ARCHIVED_REASSOCIATION_MAX_AGE_DAYS = 45

# Serializes concurrent rebuild_incidents() calls across processes (e.g. the
# scheduler's own interval job overlapping with the on-startup rebuild that
# runs every time the backend container restarts/redeploys). Without this,
# two concurrent runs can both SELECT the same "no existing incident matches
# this cluster yet" result (neither has committed its INSERT yet) and both
# create a FireIncident row for the same cluster - confirmed live: incidents
# 99 and 7741 are byte-for-byte identical (same centroid, same
# first/last_detected_at, same detection_count) but resolved to two
# different localities (Lubrín vs Bédar), consistent with two independent,
# concurrent reverse_geocode calls for the same near-boundary point rather
# than a matching-logic bug. Any fixed constant works as the lock key; this
# one has no other meaning.
_REBUILD_LOCK_KEY = 918_273_645

# Guard for merge_reassociable_incidents (see below): auto-merging is
# destructive (deletes a FireIncident row, unlike a bad automatic
# *association* of new detections onto the wrong incident, which the next
# rebuild_incidents pass can't itself undo but at least doesn't destroy
# history for). Two candidate incidents both at/above this many detections
# AND both currently active/cooling (i.e. neither has gone quiet and
# archived) are plausibly two genuinely distinct, simultaneous fires - e.g.
# two separate ignitions in the same mountain range on the same dry, windy
# day - rather than one fire's quiet-then-reflare pattern this pass exists
# to catch. Require at least one side to be small or archived before
# auto-merging; anything else is left for a human to merge manually via
# POST /api/incidents/merge.
AUTO_MERGE_SMALL_DETECTION_COUNT = 15


def group_by_proximity(points: list[tuple[float, float]], threshold_deg: float) -> list[list[int]]:
    """
    Union-find clustering of (lat, lon) points, direct port of app.js's
    groupFiresByProximity so server-side incidents match what the map already
    renders. Returns groups as lists of indices into `points`.
    """
    n = len(points)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        lat_i, lon_i = points[i]
        for j in range(i + 1, n):
            lat_j, lon_j = points[j]
            if ((lat_i - lat_j) ** 2 + (lon_i - lon_j) ** 2) ** 0.5 <= threshold_deg:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


def _resolve_locality(db: Session, lat: float, lon: float) -> tuple[str | None, str | None, str | None]:
    """
    Resolves locality/province/country for an incident centroid, actively
    calling Nominatim (via reverse_geocode's own cache-or-fetch logic) when
    this ~1km grid cell hasn't been looked up yet. The number of distinct
    incidents at any time is small (tens, not thousands) and rebuild_incidents
    only runs on the scheduler's own interval, so eagerly resolving every
    unnamed incident here - instead of waiting for a user to click "Get
    location" on its map polygon - stays comfortably within Nominatim's
    1 req/sec usage policy (enforced by state.wait_for_nominatim_slot).
    """
    try:
        result = reverse_geocode(db, lat, lon)
        return result["locality"], result["province"], result.get("country_code")
    except Exception:
        logger.warning("Reverse geocode failed for incident centroid (%s, %s)", lat, lon, exc_info=True)
        return None, None, None


def _severity(detection_count: int, area_ha: float | None, duration_hours: float) -> float:
    return detection_count * 1.0 + (area_ha or 0.0) * 0.05 + duration_hours * 0.1


def _risk_level(score: float) -> str:
    if score >= 50:
        return "critical"
    if score >= 20:
        return "high"
    if score >= 5:
        return "moderate"
    return "low"


# Spanish label for the (English, API-facing) status enum value - used only
# in human-readable IncidentEvent titles; the `status` field itself stays
# English since the frontend's filters/CSS classes key off it directly.
STATUS_LABELS_ES = {"active": "activo", "cooling": "en enfriamiento", "archived": "archivado"}


def _status(last_detected_at: datetime, now: datetime) -> str:
    age_hours = (now - last_detected_at).total_seconds() / 3600
    if age_hours <= ACTIVE_AFTER_HOURS:
        return "active"
    if age_hours <= COOLING_AFTER_HOURS:
        return "cooling"
    return "archived"


def rebuild_incidents(db: Session) -> int:
    """
    Re-clusters recent FireDetection rows into FireIncident records, matching
    each cluster to an existing incident (active/cooling, or archived
    recently enough - see ARCHIVED_REASSOCIATION_MAX_AGE_DAYS) by centroid
    proximity so ids/slugs stay stable across runs, or creating a new one.
    Appends IncidentEvent rows when an incident is created, grows, or is
    reactivated from archived. Returns the number of incidents touched (NOT
    counting any auto-merges performed afterwards - see below).

    Takes a Postgres advisory lock for its entire duration so two overlapping
    calls (e.g. scheduler tick vs. on-startup rebuild during a redeploy)
    never run their match-then-insert logic concurrently - see
    _REBUILD_LOCK_KEY above for why that matters.

    After the main clustering pass commits, also runs
    merge_reassociable_incidents - the retroactive counterpart to the
    matching above, catching cases where TWO ALREADY-EXISTING incidents
    (not a new cluster vs. an existing one) turn out to be the same real
    fire. Run here, at the tail of the same job that already fires on every
    fetch_interval_minutes tick, rather than as its own scheduler job -
    there's no reason for retroactive reassociation to run on a different
    cadence than the clustering pass whose gap it's covering for.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REBUILD_LOCK_KEY})

    now = datetime.utcnow()
    since = now - timedelta(hours=INCIDENTS_WINDOW_HOURS)
    detections = (
        db.query(FireDetection)
        .filter(FireDetection.acquired_at >= since)
        .order_by(FireDetection.acquired_at.asc())
        .all()
    )
    if not detections:
        return 0

    points = [(d.latitude, d.longitude) for d in detections]
    groups = group_by_proximity(points, REGION_LINK_DEG)

    archived_cutoff = now - timedelta(days=ARCHIVED_REASSOCIATION_MAX_AGE_DAYS)
    existing_incidents = (
        db.query(FireIncident)
        .filter(
            or_(
                FireIncident.status != "archived",
                FireIncident.last_detected_at >= archived_cutoff,
            )
        )
        .all()
    )
    existing_incidents_by_id = {inc.id: inc for inc in existing_incidents}

    # REGION_LINK_DEG clusters raw points tightly (correct for polygon shape),
    # but a real fire can spot/jump further than that between rebuild passes -
    # so a SINGLE incident can now be made of multiple raw spatial groups.
    # Resolve each raw group to the closest existing incident within the
    # wider INCIDENT_REASSOCIATION_DEG first...
    group_centroids = []
    for group_indices in groups:
        group = [detections[i] for i in group_indices]
        lat_sum = sum(d.latitude for d in group)
        lon_sum = sum(d.longitude for d in group)
        group_centroids.append((lat_sum / len(group), lon_sum / len(group)))

    raw_group_to_incident_id: dict[int, int] = {}
    for gi, (g_lat, g_lon) in enumerate(group_centroids):
        candidates = [
            (inc, ((inc.centroid_lat - g_lat) ** 2 + (inc.centroid_lon - g_lon) ** 2) ** 0.5)
            for inc in existing_incidents
        ]
        in_range = [(inc, dist) for inc, dist in candidates if dist <= INCIDENT_REASSOCIATION_DEG]
        if in_range:
            raw_group_to_incident_id[gi] = min(in_range, key=lambda pair: pair[1])[0].id

    # ...then merge any groups that matched the SAME existing incident (this
    # is the fix: without merging, two raw groups matching one incident in
    # the same pass would have the second silently overwrite the first's
    # detection_count/centroid/etc instead of combining them - confirmed live
    # via the La Mierla/"Arbancón" split described above). Raw groups that
    # matched NO existing incident might still belong together as one
    # brand-new incident (e.g. a fire's very first rebuild pass already spans
    # two spatial clusters a few km apart) - merge those via the same wider
    # radius, reusing group_by_proximity on their own centroids.
    unmatched_indices = [gi for gi in range(len(groups)) if gi not in raw_group_to_incident_id]
    unmatched_merged = group_by_proximity([group_centroids[gi] for gi in unmatched_indices], INCIDENT_REASSOCIATION_DEG)

    merged_populations: dict[str, list] = {}
    for gi, incident_id in raw_group_to_incident_id.items():
        merged_populations.setdefault(f"existing-{incident_id}", []).extend(
            detections[i] for i in groups[gi]
        )
    for local_indices in unmatched_merged:
        key = f"new-{min(local_indices)}"
        for local_idx in local_indices:
            gi = unmatched_indices[local_idx]
            merged_populations.setdefault(key, []).extend(detections[i] for i in groups[gi])

    touched = 0
    for key, group in merged_populations.items():
        match = existing_incidents_by_id[int(key.split("-", 1)[1])] if key.startswith("existing-") else None

        lat_sum = sum(d.latitude for d in group)
        lon_sum = sum(d.longitude for d in group)
        centroid_lat = lat_sum / len(group)
        centroid_lon = lon_sum / len(group)
        # This pass's `group` only ever contains detections within the last
        # INCIDENTS_WINDOW_HOURS (30 days) - see the `detections` query at the
        # top of this function. Without anchoring to the existing incident's
        # own first_detected_at, a fire whose earliest detections eventually
        # age out of that window would have its true ignition date silently
        # overwritten FORWARD in time on the next rebuild pass (confirmed
        # live: an incident's own "Primera detección" timeline event stayed
        # at its real July 4 origin while first_detected_at itself drifted to
        # July 22 once the window moved past the 4th - same root cause behind
        # a fire's displayed name/origin town appearing to silently change).
        # last_detected_at is taken the same way for symmetry/safety, though
        # in practice the group's own max should already be >= it.
        first_detected_at = min(d.acquired_at for d in group)
        last_detected_at = max(d.acquired_at for d in group)
        if match is not None:
            first_detected_at = min(first_detected_at, match.first_detected_at)
            last_detected_at = max(last_detected_at, match.last_detected_at)
        area_ha = max((d.area_ha for d in group if d.area_ha is not None), default=None)
        detection_count = len(group)
        duration_hours = (last_detected_at - first_detected_at).total_seconds() / 3600
        severity_score = _severity(detection_count, area_ha, duration_hours)
        risk_level = _risk_level(severity_score)
        status = _status(last_detected_at, now)

        # Only hit Nominatim when this incident doesn't already have a name -
        # matched incidents keep their previously-resolved locality/province
        # forever (sticky, like the rest of this function's update logic),
        # so a fire that's been named once never re-triggers a network call.
        if match is None or not match.locality or not match.country_code:
            locality, province, country_code = _resolve_locality(db, centroid_lat, centroid_lon)
        else:
            locality, province, country_code = match.locality, match.province, match.country_code

        if match is None:
            incident = FireIncident(
                slug=f"incident-{uuid.uuid4().hex[:10]}",
                centroid_lat=centroid_lat,
                centroid_lon=centroid_lon,
                province=province,
                locality=locality,
                country_code=country_code,
                status=status,
                severity_score=severity_score,
                risk_level=risk_level,
                detection_count=detection_count,
                area_ha=area_ha,
                first_detected_at=first_detected_at,
                last_detected_at=last_detected_at,
                updated_at=now,
            )
            db.add(incident)
            db.flush()  # need incident.id for the event FK below
            db.add(
                IncidentEvent(
                    incident_id=incident.id,
                    occurred_at=first_detected_at,
                    event_type="detection",
                    source="system",
                    title="Primera detección",
                    description=f"{detection_count} detección(es) en el cluster inicial.",
                )
            )
            touched += 1
            continue

        new_detections = detection_count - match.detection_count
        if new_detections > 0:
            db.add(
                IncidentEvent(
                    incident_id=match.id,
                    occurred_at=last_detected_at,
                    event_type="detection",
                    source="system",
                    title=f"{new_detections} detección(es) nueva(s)",
                )
            )
        if match.status != status:
            db.add(
                IncidentEvent(
                    incident_id=match.id,
                    occurred_at=now,
                    event_type="status_change",
                    source="system",
                    title=f"Estado cambiado a {STATUS_LABELS_ES.get(status, status)}",
                )
            )

        match.centroid_lat = centroid_lat
        match.centroid_lon = centroid_lon
        match.locality = locality or match.locality
        match.province = province or match.province
        match.country_code = country_code or match.country_code
        match.status = status
        match.severity_score = severity_score
        match.risk_level = risk_level
        match.detection_count = detection_count
        match.area_ha = area_ha
        match.first_detected_at = first_detected_at
        match.last_detected_at = last_detected_at
        match.updated_at = now
        touched += 1

    db.commit()

    absorbed = merge_reassociable_incidents(db)
    if absorbed:
        logger.info("Incident rebuild: %d incident(s) auto-merged via retroactive reassociation", absorbed)

    return touched


def merge_incidents(
    db: Session,
    survivor_id: int,
    absorbed_ids: list[int],
    official_name: str | None = None,
    event_source: str = "admin",
    event_title: str = "Incidentes fusionados manualmente",
    event_description: str | None = None,
) -> FireIncident:
    """
    Core merge logic shared by the manual POST /api/incidents/merge endpoint
    (routers/incidents.py) and the automatic merge_reassociable_incidents
    pass below. Reassigns every child row (IncidentEvent timeline,
    RegionalIncident, SatelliteScene, TelegramMessage) from absorbed_ids onto
    survivor_id, combines the merged incidents' stats, deletes the absorbed
    rows, and commits.

    Callers are responsible for validating survivor_id/absorbed_ids
    (existence, distinctness, survivor not itself in absorbed_ids) before
    calling this - see routers/incidents.py's merge_incidents handler for
    that validation.

    Stat-combination judgment calls:
    - first/last_detected_at: min/max across all merged incidents.
    - detection_count: incidents whose [first, last] windows overlap are
      treated as likely re-detections of the same underlying cluster (take
      the max, don't double count - this is exactly what happened with the
      byte-for-byte-identical id=99/7741 pair); incidents whose windows
      don't overlap are treated as genuinely separate detection batches of
      the same physical fire (sum them - this is what should happen for a
      later, real re-flareup like id=47).
    - area_ha: max of whatever official (EFFIS) figures are present.
    - centroid: detection-count-weighted average, so the combined incident's
      polygon/marker sits closer to whichever sub-cluster had more detections.
    """
    all_ids = [survivor_id, *absorbed_ids]
    incidents = db.query(FireIncident).filter(FireIncident.id.in_(all_ids)).all()
    survivor = next(inc for inc in incidents if inc.id == survivor_id)
    absorbed = [inc for inc in incidents if inc.id != survivor_id]

    ordered = sorted(incidents, key=lambda inc: inc.first_detected_at)
    combined_detection_count = ordered[0].detection_count
    running_window_end = ordered[0].last_detected_at
    for inc in ordered[1:]:
        if inc.first_detected_at <= running_window_end:
            combined_detection_count = max(combined_detection_count, inc.detection_count)
        else:
            combined_detection_count += inc.detection_count
        running_window_end = max(running_window_end, inc.last_detected_at)

    first_detected_at = min(inc.first_detected_at for inc in incidents)
    last_detected_at = max(inc.last_detected_at for inc in incidents)
    area_ha_values = [inc.area_ha for inc in incidents if inc.area_ha is not None]
    area_ha = max(area_ha_values) if area_ha_values else None

    total_weight = sum(inc.detection_count for inc in incidents) or len(incidents)
    centroid_lat = sum(inc.centroid_lat * (inc.detection_count or 1) for inc in incidents) / total_weight
    centroid_lon = sum(inc.centroid_lon * (inc.detection_count or 1) for inc in incidents) / total_weight

    duration_hours = (last_detected_at - first_detected_at).total_seconds() / 3600
    severity_score = _severity(combined_detection_count, area_ha, duration_hours)
    risk_level = _risk_level(severity_score)
    status = _status(last_detected_at, datetime.utcnow())

    db.query(IncidentEvent).filter(IncidentEvent.incident_id.in_(absorbed_ids)).update(
        {IncidentEvent.incident_id: survivor_id}, synchronize_session=False
    )
    db.query(RegionalIncident).filter(RegionalIncident.matched_incident_id.in_(absorbed_ids)).update(
        {RegionalIncident.matched_incident_id: survivor_id}, synchronize_session=False
    )
    db.query(TelegramMessage).filter(TelegramMessage.matched_incident_id.in_(absorbed_ids)).update(
        {TelegramMessage.matched_incident_id: survivor_id}, synchronize_session=False
    )
    # CopernicusEmsActivation.matched_incident_id is a real FK to
    # fire_incidents - missing this reassignment made ANY merge (manual or
    # automatic) of an incident with a matched EMS activation fail with a
    # ForeignKeyViolation on delete (confirmed live: incident 53's own
    # "El Barraco" EMS activation blocked its merge into 1439).
    db.query(CopernicusEmsActivation).filter(CopernicusEmsActivation.matched_incident_id.in_(absorbed_ids)).update(
        {CopernicusEmsActivation.matched_incident_id: survivor_id}, synchronize_session=False
    )

    # SatelliteScene has a UNIQUE(incident_id, collection, scene_id)
    # constraint - a plain bulk reassign could violate it if the survivor and
    # an absorbed row both discovered the exact same scene (plausible for
    # exact-duplicate incidents). Drop those collisions rather than fail the
    # whole merge - the survivor already has an equivalent row.
    survivor_scene_keys = {
        (s.collection, s.scene_id)
        for s in db.query(SatelliteScene).filter(SatelliteScene.incident_id == survivor_id).all()
    }
    for scene in db.query(SatelliteScene).filter(SatelliteScene.incident_id.in_(absorbed_ids)).all():
        key = (scene.collection, scene.scene_id)
        if key in survivor_scene_keys:
            db.delete(scene)
        else:
            scene.incident_id = survivor_id
            survivor_scene_keys.add(key)

    db.add(
        IncidentEvent(
            incident_id=survivor_id,
            occurred_at=datetime.utcnow(),
            event_type="merge",
            source=event_source,
            title=event_title,
            description=event_description
            or f"Fusionado con incidente(s) #{', #'.join(str(i) for i in absorbed_ids)}.",
        )
    )

    survivor.centroid_lat = centroid_lat
    survivor.centroid_lon = centroid_lon
    survivor.detection_count = combined_detection_count
    survivor.first_detected_at = first_detected_at
    survivor.last_detected_at = last_detected_at
    survivor.area_ha = area_ha
    survivor.severity_score = severity_score
    survivor.risk_level = risk_level
    survivor.status = status
    survivor.updated_at = datetime.utcnow()
    if official_name is not None:
        survivor.official_name = official_name.strip() or None
    if not survivor.locality:
        for inc in absorbed:
            if inc.locality:
                survivor.locality = inc.locality
                survivor.province = survivor.province or inc.province
                survivor.country_code = survivor.country_code or inc.country_code
                break

    # Force the SatelliteScene/RegionalIncident/TelegramMessage/IncidentEvent
    # reassignments above to hit the database before deleting the absorbed
    # FireIncident rows - without this, SQLAlchemy's unit-of-work can order
    # the DELETE FROM fire_incidents before the dependent-row updates it
    # can't see a plain ForeignKey (no ORM relationship() is declared on
    # these models) as an ordering dependency, which fails with a
    # ForeignKeyViolation instead of quietly reassigning first.
    db.flush()

    for inc in absorbed:
        db.delete(inc)

    db.commit()
    db.refresh(survivor)
    return survivor


def _auto_merge_candidate(a: FireIncident, b: FireIncident) -> bool:
    """
    Whether two EXISTING incidents are plausible auto-merge candidates for
    merge_reassociable_incidents - same spatial/temporal reasoning
    rebuild_incidents already applies to a NEW cluster vs. an existing
    incident (INCIDENT_REASSOCIATION_DEG proximity,
    ARCHIVED_REASSOCIATION_MAX_AGE_DAYS gap), plus the extra
    both-still-large guard documented above AUTO_MERGE_SMALL_DETECTION_COUNT.
    """
    dist_deg = ((a.centroid_lat - b.centroid_lat) ** 2 + (a.centroid_lon - b.centroid_lon) ** 2) ** 0.5
    if dist_deg > INCIDENT_REASSOCIATION_DEG:
        return False

    earlier, later = (a, b) if a.first_detected_at <= b.first_detected_at else (b, a)
    gap_hours = (later.first_detected_at - earlier.last_detected_at).total_seconds() / 3600
    if gap_hours > ARCHIVED_REASSOCIATION_MAX_AGE_DAYS * 24:
        return False

    def is_small_or_archived(inc: FireIncident) -> bool:
        return inc.status == "archived" or inc.detection_count < AUTO_MERGE_SMALL_DETECTION_COUNT

    return is_small_or_archived(a) or is_small_or_archived(b)


def _merge_one_reassociable_pair(db: Session) -> bool:
    """
    Finds and merges (at most) one auto-mergeable pair of existing incidents.
    Re-queries incidents fresh rather than trying to reuse an in-memory list
    across multiple merges in the same pass - merge_incidents commits (and
    SQLAlchemy's default expire-on-commit then invalidates deleted rows'
    attributes), so scanning further pairs from a stale pre-merge list risks
    touching an already-deleted incident. Simpler and safer to do one merge
    per query given the expected incident count is small (tens, not
    thousands - see _resolve_locality above).
    """
    now = datetime.utcnow()
    archived_cutoff = now - timedelta(days=ARCHIVED_REASSOCIATION_MAX_AGE_DAYS)
    candidates = (
        db.query(FireIncident)
        .filter(
            or_(
                FireIncident.status != "archived",
                FireIncident.last_detected_at >= archived_cutoff,
            )
        )
        .order_by(FireIncident.id.asc())
        .all()
    )

    for i in range(len(candidates)):
        a = candidates[i]
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            if not _auto_merge_candidate(a, b):
                continue

            # Merge the smaller (fewer-detections) incident into the larger
            # one - the larger one is more likely to already carry the
            # richer name/timeline/child-record history worth keeping as the
            # surviving identity. Ties (e.g. both freshly created) fall back
            # to keeping the OLDER row (lower id) as survivor, since it's
            # more likely to be the originally-named incident and the newer
            # row the just-created duplicate.
            if a.detection_count != b.detection_count:
                survivor, absorbed = (a, b) if a.detection_count > b.detection_count else (b, a)
            else:
                survivor, absorbed = (a, b) if a.id < b.id else (b, a)

            dist_deg = ((a.centroid_lat - b.centroid_lat) ** 2 + (a.centroid_lon - b.centroid_lon) ** 2) ** 0.5
            logger.info(
                "Auto-merging incident #%d (%s, %d detections) into #%d (%s, %d detections) - "
                "retroactive reassociation, centroids %.4f deg apart",
                absorbed.id,
                absorbed.locality or "?",
                absorbed.detection_count,
                survivor.id,
                survivor.locality or "?",
                survivor.detection_count,
                dist_deg,
            )
            try:
                merge_incidents(
                    db,
                    survivor.id,
                    [absorbed.id],
                    event_source="system",
                    event_title="Fusión automática (reasociación retroactiva)",
                    event_description=(
                        f"Incidente #{absorbed.id} fusionado automáticamente: centroides a "
                        f"{dist_deg:.4f} grados, dentro de la ventana de reasociación de "
                        f"{ARCHIVED_REASSOCIATION_MAX_AGE_DAYS} días."
                    ),
                )
            except Exception:
                # Roll back so the session is usable again (an uncaught DB
                # error here otherwise leaves it in "pending rollback" state
                # for whatever runs next - same class of bug fixed in the
                # ingest_*() functions). Log and keep scanning rather than
                # aborting the whole pass - one bad pair (e.g. an FK this
                # function doesn't yet know how to reassign) shouldn't block
                # every other legitimate merge in the same pass.
                db.rollback()
                logger.exception(
                    "Auto-merge of incident #%d into #%d failed - skipping this pair", absorbed.id, survivor.id
                )
                continue
            return True

    return False


def merge_reassociable_incidents(db: Session) -> int:
    """
    Retroactively catches what rebuild_incidents' own reassociation never
    can: two ALREADY-EXISTING FireIncident rows that are actually the same
    real fire. rebuild_incidents only ever tests a NEW detection cluster
    against existing incidents at the moment that cluster is processed - once
    an incident exists, it just keeps matching new detections to ITSELF going
    forward, and is never re-examined against other existing incidents.
    Confirmed live: incident 53 ("El Barraco", archived, last_detected_at
    2026-07-16) and incident 1439 ("Casavieja", active, first_detected_at
    2026-07-22) sit ~13-14km apart in the same Ávila province stretch - well
    under INCIDENT_REASSOCIATION_DEG (~16.7km) and well within
    ARCHIVED_REASSOCIATION_MAX_AGE_DAYS (45 days) - consistent with the same
    fire going quiet, auto-archiving, then reflaring under what looked like a
    brand-new detection cluster days later, but 1439 was created without ever
    being checked against 53.

    Deliberately pairwise, not N-way: each call to _merge_one_reassociable_pair
    finds and performs AT MOST ONE merge from a fresh query, so this loops
    until a pass finds nothing left to merge. Slower than solving transitive
    N-way clustering (e.g. A-B-C all the same fire) in one shot, but far
    simpler to reason about, and self-correcting since this runs on every
    rebuild_incidents pass (see call site) - a 3-way chain just takes two
    scheduler ticks to fully collapse instead of one.

    Reuses rebuild_incidents' own _REBUILD_LOCK_KEY advisory lock (rather
    than inventing a second lock) so a concurrent rebuild_incidents call
    never interleaves with this pass. The lock is re-acquired once per
    iteration (not held once for the whole function) because merge_incidents
    itself commits - which ends the current Postgres transaction and, with
    it, releases any pg_advisory_xact_lock taken inside it.

    Returns the number of incidents absorbed (deleted) across all passes.
    """
    total_absorbed = 0
    while True:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REBUILD_LOCK_KEY})
        if not _merge_one_reassociable_pair(db):
            break
        total_absorbed += 1
    return total_absorbed
