import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import FireDetection, FireIncident, IncidentEvent
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
    each cluster to an existing (non-archived) incident by centroid proximity
    so ids/slugs stay stable across runs, or creating a new one. Appends
    IncidentEvent rows when an incident is created or grows. Returns the
    number of incidents touched.
    """
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

    existing_incidents = (
        db.query(FireIncident).filter(FireIncident.status != "archived").all()
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
        lat_sum = sum(d.latitude for d in group)
        lon_sum = sum(d.longitude for d in group)
        centroid_lat = lat_sum / len(group)
        centroid_lon = lon_sum / len(group)
        first_detected_at = min(d.acquired_at for d in group)
        last_detected_at = max(d.acquired_at for d in group)
        area_ha = max((d.area_ha for d in group if d.area_ha is not None), default=None)
        detection_count = len(group)
        duration_hours = (last_detected_at - first_detected_at).total_seconds() / 3600
        severity_score = _severity(detection_count, area_ha, duration_hours)
        risk_level = _risk_level(severity_score)
        status = _status(last_detected_at, now)

        match = existing_incidents_by_id[int(key.split("-", 1)[1])] if key.startswith("existing-") else None

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
    return touched
