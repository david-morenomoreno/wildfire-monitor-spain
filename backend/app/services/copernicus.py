import json
import logging
import os
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import FireIncident, IncidentEvent, SatelliteScene
from app.services.health import record_check

logger = logging.getLogger(__name__)

# Cached in-process; a client_credentials token is valid for several minutes,
# so re-fetching one per scene search would be wasteful and slow.
_token_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}


def is_configured() -> bool:
    return bool(settings.copernicus_client_id and settings.copernicus_client_secret)


def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expires_at"]):
        return str(_token_cache["token"])

    response = httpx.post(
        settings.copernicus_token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.copernicus_client_id,
            "client_secret": settings.copernicus_client_secret,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    # Refresh a bit early (60s margin) rather than cutting it exactly at expiry.
    expires_in = payload.get("expires_in", 300)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(30, expires_in - 60)
    return token


def search_scenes(bbox: list[float], start: datetime, end: datetime, limit: int = 5) -> list[dict]:
    """Raw Catalog API search - returns the STAC 'features' list as-is."""
    token = _get_access_token()
    datetime_range = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    response = httpx.post(
        settings.copernicus_catalog_url,
        json={
            "bbox": bbox,
            "datetime": datetime_range,
            "collections": settings.copernicus_collections,
            "limit": limit,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json().get("features", [])


def render_scene_image(bbox: list[float], captured_at: datetime) -> bytes:
    """
    Renders a true-color JPEG via the Process API for a narrow (+/-12h)
    window around one scene's capture time - Sentinel-2's ~5-day revisit
    means that window will almost always contain just that one acquisition,
    even though the Process API selects a mosaic over the time range rather
    than accepting an exact scene_id directly.
    """
    token = _get_access_token()
    start = captured_at - timedelta(hours=12)
    end = captured_at + timedelta(hours=12)
    body = {
        "input": {
            "bounds": {"bbox": bbox},
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    },
                }
            ],
        },
        "output": {
            "width": settings.copernicus_thumbnail_size,
            "height": settings.copernicus_thumbnail_size,
            "responses": [{"identifier": "default", "format": {"type": "image/jpeg"}}],
        },
        "evalscript": settings.copernicus_evalscript,
    }
    response = httpx.post(
        settings.copernicus_process_url,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def get_or_render_thumbnail(db: Session, scene: SatelliteScene) -> bytes:
    """
    Serves a cached thumbnail from disk if this scene was already rendered;
    otherwise renders it now via the Process API (costs processing quota,
    which is why this isn't done automatically for all discovered scenes)
    and caches it to disk before returning.
    """
    if scene.thumbnail_path:
        path = os.path.join(settings.upload_dir, scene.thumbnail_path)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()

    incident = db.query(FireIncident).filter_by(id=scene.incident_id).first()
    bbox = _bbox_for_incident(incident)
    image_bytes = render_scene_image(bbox, scene.captured_at)

    os.makedirs(settings.upload_dir, exist_ok=True)
    filename = f"copernicus-{scene.id}.jpg"
    with open(os.path.join(settings.upload_dir, filename), "wb") as f:
        f.write(image_bytes)
    scene.thumbnail_path = filename
    db.commit()
    return image_bytes


def _bbox_for_incident(incident: FireIncident) -> list[float]:
    pad = settings.copernicus_bbox_padding_deg
    return [
        incident.centroid_lon - pad,
        incident.centroid_lat - pad,
        incident.centroid_lon + pad,
        incident.centroid_lat + pad,
    ]


def discover_for_incident(db: Session, incident: FireIncident) -> int:
    """
    Searches for Sentinel scenes over one incident's area/date range, stores
    any not already known, and appends a timeline event per new scene.
    Returns the count of new scenes stored.
    """
    bbox = _bbox_for_incident(incident)
    # +1 day past last detection: imagery captured just after the last
    # thermal detection can still usefully show the burn scar/smoke plume.
    end = incident.last_detected_at + timedelta(days=1)
    features = search_scenes(bbox, incident.first_detected_at, end)

    existing_ids = {
        scene_id
        for (scene_id,) in db.query(SatelliteScene.scene_id)
        .filter_by(incident_id=incident.id)
        .all()
    }

    new_count = 0
    for feature in features:
        scene_id = feature.get("id")
        if not scene_id or scene_id in existing_ids:
            continue

        properties = feature.get("properties") or {}
        captured_raw = properties.get("datetime") or feature.get("datetime")
        try:
            captured_at = datetime.fromisoformat(captured_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except (TypeError, ValueError, AttributeError):
            captured_at = datetime.utcnow()

        cloud_cover = properties.get("eo:cloud_cover")
        # "thumbnail" asset: confirmed absent from live sentinel-2-l2a
        # responses (only a non-browsable "data" s3:// asset is present) -
        # kept in case that changes for this or another collection later.
        thumbnail_url = ((feature.get("assets") or {}).get("thumbnail") or {}).get("href")
        # "collection" is a top-level field on the feature, not nested under
        # properties - confirmed against the live response (2026-07-14).
        collection = feature.get("collection") or (
            settings.copernicus_collections[0] if settings.copernicus_collections else "unknown"
        )
        item_url = next(
            (link.get("href") for link in feature.get("links", []) if link.get("rel") == "self"),
            None,
        )

        scene = SatelliteScene(
            incident_id=incident.id,
            collection=collection,
            scene_id=scene_id,
            captured_at=captured_at,
            cloud_cover=cloud_cover,
            thumbnail_url=thumbnail_url,
            item_url=item_url,
        )
        db.add(scene)
        db.flush()  # need scene.id for the timeline event's raw_data below

        # item_url is the raw Catalog API endpoint, which requires a Bearer
        # token - it 401s on a plain browser click, so it's stored (useful
        # for anyone calling the API with a token) but NOT rendered as a
        # clickable link here. No confirmed public deep-link format exists
        # for the Copernicus Browser UI (checked their docs - not documented),
        # so this shows the scene id for manual lookup instead of a broken link.
        # scene_db_id lets the frontend request a lazily-rendered thumbnail at
        # GET /api/copernicus/scenes/{scene_db_id}/thumbnail.
        description = f"Escena: {scene_id}" if scene_id else None
        db.add(
            IncidentEvent(
                incident_id=incident.id,
                occurred_at=captured_at,
                event_type="satellite_imagery",
                source=collection,
                title=f"Escena {collection} capturada"
                + (f" ({cloud_cover:.0f}% nubes)" if cloud_cover is not None else ""),
                description=description,
                raw_data=json.dumps({"thumbnail_url": thumbnail_url, "scene_db_id": scene.id}),
            )
        )
        new_count += 1

    db.commit()
    return new_count


def discover_for_active_incidents(db: Session) -> dict[str, int]:
    """
    Runs discovery for every non-archived incident. Skips (records
    "skipped", not an error) when OAuth credentials aren't configured yet -
    Copernicus login can't be automated, see README.
    """
    if not is_configured():
        logger.info("Copernicus not configured (missing client_id/client_secret) - skipping discovery")
        record_check(db, "copernicus", "skipped", "OAuth client_id/client_secret not configured")
        return {}

    incidents = db.query(FireIncident).filter(FireIncident.status != "archived").all()
    results: dict[str, int] = {}
    failures = 0
    for incident in incidents:
        try:
            results[incident.slug] = discover_for_incident(db, incident)
        except Exception:
            logger.exception("Copernicus discovery failed for incident %s", incident.slug)
            results[incident.slug] = 0
            failures += 1

    if failures and failures == len(incidents) and incidents:
        record_check(db, "copernicus", "disrupted", f"All {failures} incident searches failed")
    elif failures:
        record_check(db, "copernicus", "degraded", f"{failures}/{len(incidents)} incident searches failed")
    else:
        total_new = sum(results.values())
        record_check(db, "copernicus", "ok", f"{total_new} new scenes across {len(incidents)} incidents")
    return results
