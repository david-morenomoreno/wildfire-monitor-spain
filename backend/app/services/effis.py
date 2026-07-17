import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import FireDetection
from app.services.geo_filter import is_in_spain
from app.services.health import record_check

logger = logging.getLogger(__name__)


def _centroid(coordinates, geometry_type: str) -> tuple[float, float] | None:
    """Best-effort centroid for Point/Polygon/MultiPolygon EFFIS geometries."""
    try:
        if geometry_type == "Point":
            lon, lat = coordinates[0], coordinates[1]
            return lat, lon
        if geometry_type == "Polygon":
            ring = coordinates[0]
        elif geometry_type == "MultiPolygon":
            ring = coordinates[0][0]
        else:
            return None
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    except (IndexError, TypeError, ZeroDivisionError) as exc:
        logger.warning("Could not compute centroid for geometry %s: %s", geometry_type, exc)
        return None


def fetch_effis_features() -> list[dict]:
    """
    Fetch burnt-area features from the EFFIS WFS endpoint.

    NOTE: EFFIS is a Copernicus geoserver-backed service with no stable public
    REST API or API key. The layer name and query params in EFFIS_WFS_URL may
    need adjusting if JRC changes their WFS schema - treat this integration as
    experimental and verify against https://effis.jrc.ec.europa.eu/ if it stops
    returning data.
    """
    response = httpx.get(settings.effis_wfs_url, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    return payload.get("features", [])


def ingest_effis(db: Session) -> int:
    state.mark_attempt("effis")
    try:
        count = _ingest_effis(db)
    except Exception as exc:
        record_check(db, "effis", "disrupted", str(exc))
        raise
    record_check(db, "effis", "ok", f"{count} features processed")
    return count


def _ingest_effis(db: Session) -> int:
    features = fetch_effis_features()
    count = 0
    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        centroid = _centroid(geometry.get("coordinates"), geometry.get("type", ""))
        if centroid is None:
            continue
        latitude, longitude = centroid

        # Fast-path: the feature's own "country"/"iso2" property, when present,
        # already rules out most non-Spain features cheaply. This alone is
        # NOT sufficient, though: EFFIS's WFS query (effis_wfs_url) has no
        # bbox or country filter at all - it's a flat "all burnt-area
        # polygons" feed - so a feature with a missing/blank country property
        # would previously fall through this check unfiltered. The geometric
        # is_in_spain() check below (same Spain-boundary polygon used for
        # FIRMS - see geo_filter.py) is the authoritative filter and always
        # runs, regardless of whether this property was present.
        country = str(properties.get("country") or properties.get("iso2") or "").upper()
        if country and country not in ("ES", "ESP", "SPAIN"):
            continue
        if not is_in_spain(latitude, longitude):
            continue

        external_id = str(
            feature.get("id")
            or properties.get("id")
            or f"{latitude}-{longitude}-{properties.get('lastupdate', '')}"
        )

        acquired_raw = properties.get("firedate") or properties.get("lastupdate")
        try:
            acquired_at = datetime.fromisoformat(acquired_raw) if acquired_raw else datetime.utcnow()
        except ValueError:
            acquired_at = datetime.utcnow()

        area_ha_raw = properties.get("area_ha")
        try:
            area_ha = float(area_ha_raw) if area_ha_raw is not None else None
        except (TypeError, ValueError):
            area_ha = None

        stmt = (
            insert(FireDetection)
            .values(
                source="EFFIS",
                external_id=external_id,
                latitude=latitude,
                longitude=longitude,
                confidence=None,
                brightness=None,
                acquired_at=acquired_at,
                raw_properties=str(properties),
                geometry_geojson=json.dumps(geometry) if geometry else None,
                area_ha=area_ha,
            )
            .on_conflict_do_nothing(constraint="uq_source_external_id")
        )
        db.execute(stmt)
        count += 1

    db.commit()
    return count
