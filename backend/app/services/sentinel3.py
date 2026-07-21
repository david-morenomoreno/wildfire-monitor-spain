"""
Sentinel-3 SLSTR Fire Radiative Power (FRP) ingestion via the EUMETSAT Data
Store (collection EO:EUM:DAT:0417).

Complements FIRMS (VIIRS/MODIS) and EUMETSAT MTG (geostationary) with a
THIRD, independent satellite pair: Sentinel-3A/-3B, polar-orbiting like
FIRMS' own satellites but on their own separate overpass schedule, so a pass
here can catch a fire between FIRMS' own satellites' passes rather than
duplicating them.

Confirmed LIVE (2026-07-19): a single S3B pass at ~21:28-21:31 UTC on
2026-07-18 detected the Guadalajara/La Mierla megafire with ~130-310 fire
pixels (depending on channel), at almost exactly the time a third-party tool
(Pyrofire) showed a dense hotspot cluster there on X/Twitter - at a moment
when EUMETSAT's own MTG product (services/eumetsat.py) found nothing nearby.
Genuinely complementary, not a duplicate of either existing source.

Each product is a zip containing, among other things, plain CSV files (no
netCDF grid/projection math needed, unlike EUMETSAT's own MTG product) -
confirmed live column layout for the two "_standard" scheme files used here:

    lat(deg),lon(deg),day,time,D/N,FRP(MW),FRPerr(MW),used_channel,
    confidence(%),confidence_class(lower=0;nominal=1;higher=2),MWIR_BT(K),
    IFOV_area(m2),SZA(deg),VZA(deg),actrack(km),altrack(km),satellite

Two detection schemes are shipped per product - MWIR at 1km resolution and
SWIR at 500m resolution (MWIR also has an "_alternative" scheme variant
using a different day/night threshold methodology; only "_standard" is used
here, EUMETSAT's own default/recommended scheme per their file naming).
"""

import csv
import hashlib
import io
import logging
import zipfile
from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import FireDetection
from app.services.eumetsat_client import download_product, is_configured, search_products
from app.services.geo_filter import is_in_spain, is_over_water
from app.services.health import record_check

logger = logging.getLogger(__name__)

# (member filename suffix, short scheme tag for external_id/raw_properties)
_CSV_SCHEMES = (
    ("FRP_MWIR1km_standard.csv", "mwir1km"),
    ("FRP_SWIR500m.csv", "swir500m"),
)

# CONFIRMED live (2026-07-19): the confidence column's name differs by
# scheme - MWIR1km uses "confidence(%)", SWIR500m uses
# "SWIR_SAA_confidence(%)" instead. Checked in order, first match wins.
_CONFIDENCE_COLUMN_CANDIDATES = ("confidence(%)", "SWIR_SAA_confidence(%)")


def _confidence_pct(pixel: dict) -> float:
    for column in _CONFIDENCE_COLUMN_CANDIDATES:
        if column in pixel:
            return float(pixel[column])
    raise KeyError(f"none of {_CONFIDENCE_COLUMN_CANDIDATES} found in row")


def _parse_csv_rows(content: str, scheme: str) -> list[dict]:
    # Files start with several "#key = value" metadata lines before the real
    # CSV header - confirmed live (2026-07-19) - DictReader needs those
    # stripped first or it treats a comment line as the header.
    lines = [line for line in content.splitlines() if line and not line.startswith("#")]
    if not lines:
        return []
    rows = list(csv.DictReader(lines))
    for row in rows:
        row["_scheme"] = scheme
    return rows


def _parse_fire_pixels(zip_bytes: bytes) -> list[dict]:
    pixels: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        for suffix, scheme in _CSV_SCHEMES:
            matches = [name for name in names if name.endswith(suffix)]
            for name in matches:
                pixels.extend(_parse_csv_rows(zf.read(name).decode(), scheme))
    return pixels


def ingest_sentinel3(db: Session, start: datetime | None = None, end: datetime | None = None) -> int:
    """
    start/end default to the last lookback window (normal scheduler polling)
    when omitted - pass them explicitly for a historical backfill (see
    scripts/backfill_history.py).
    """
    state.mark_attempt("sentinel3")
    try:
        count = _ingest_sentinel3(db, start=start, end=end)
    except Exception as exc:
        # See the matching comment in eumetsat.py - roll back before
        # record_check reuses this session so its own db.commit() doesn't
        # raise a second, unrelated PendingRollbackError and mask the cause.
        db.rollback()
        record_check(db, "sentinel3", "disrupted", str(exc))
        raise
    record_check(db, "sentinel3", "ok", f"{count} fire pixels processed")
    return count


def _ingest_sentinel3(db: Session, start: datetime | None = None, end: datetime | None = None) -> int:
    if not is_configured():
        record_check(db, "sentinel3", "skipped", "consumer_key/consumer_secret not configured")
        return 0

    if end is None:
        end = datetime.utcnow()
    if start is None:
        start = end - timedelta(minutes=settings.sentinel3_lookback_minutes)
    features = search_products(settings.sentinel3_collection_id, start, end, bbox=settings.firms_bbox)
    if len(features) >= 100:
        # See the matching warning in eumetsat.py - search_products has no
        # pagination past page_size (default 100).
        logger.warning(
            "Sentinel-3 search for [%s, %s] returned %d products (>= page size) - "
            "results may be truncated; use a narrower window if backfilling",
            start,
            end,
            len(features),
        )

    count = 0
    skipped_low_confidence = 0
    skipped_outside_spain = 0
    skipped_over_water = 0
    for feature in features:
        product_id = feature.get("id") or "unknown"
        try:
            zip_bytes = download_product(feature)
            pixels = _parse_fire_pixels(zip_bytes)
        except Exception:
            logger.exception("Failed to download/parse Sentinel-3 SLSTR FRP product %s", product_id)
            continue

        # Short and collision-safe regardless of how long product_id is -
        # confirmed live these ids run ~100 chars, which alone would blow
        # past FireDetection.external_id's 120-char column once the per-pixel
        # suffix (scheme/lat/lon/time) is appended.
        product_hash = hashlib.md5(product_id.encode()).hexdigest()[:12]

        for pixel in pixels:
            try:
                latitude = float(pixel["lat(deg)"])
                longitude = float(pixel["lon(deg)"])
                confidence_pct = _confidence_pct(pixel)
                acquired_at = datetime.strptime(f"{pixel['day']} {pixel['time']}", "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed Sentinel-3 SLSTR row %s: %s", pixel, exc)
                continue

            if confidence_pct < settings.sentinel3_min_confidence_pct:
                skipped_low_confidence += 1
                continue

            if not is_in_spain(latitude, longitude):
                skipped_outside_spain += 1
                continue

            # Same rationale as FIRMS/EUMETSAT - see geo_filter.py's
            # is_over_water docstring.
            if is_over_water(latitude, longitude):
                skipped_over_water += 1
                continue

            external_id = f"{product_hash}-{pixel['_scheme']}-{latitude:.5f}-{longitude:.5f}-{pixel['time']}"

            stmt = (
                insert(FireDetection)
                .values(
                    source="SENTINEL3",
                    external_id=external_id,
                    latitude=latitude,
                    longitude=longitude,
                    confidence=str(confidence_pct),
                    brightness=None,  # FRP(MW) isn't a brightness temperature - see pixel["FRP(MW)"] in raw_properties instead
                    acquired_at=acquired_at,
                    raw_properties=str(pixel),
                )
                .on_conflict_do_nothing(constraint="uq_source_external_id")
            )
            db.execute(stmt)
            count += 1

    if skipped_low_confidence:
        logger.info("Skipped %d Sentinel-3 SLSTR pixel(s) below the confidence threshold", skipped_low_confidence)
    if skipped_outside_spain:
        logger.info("Skipped %d Sentinel-3 SLSTR pixel(s) outside Spain's real border", skipped_outside_spain)
    if skipped_over_water:
        logger.info("Skipped %d Sentinel-3 SLSTR pixel(s) landing inside a real water body", skipped_over_water)
    db.commit()
    return count
