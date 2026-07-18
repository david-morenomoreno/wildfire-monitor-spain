"""
EUMETSAT MTG/FCI Active Fire Monitoring - geostationary fire-pixel ingestion.

Complements FIRMS/EFFIS (polar-orbiting VIIRS/MODIS, a handful of passes/day)
with continuous ~10-min full-disk coverage from Meteosat Third Generation's
Flexible Combined Imager (FCI). Confirmed LIVE against the real API
(2026-07-18):
  - Auth: POST https://api.eumetsat.int/token, HTTP Basic
    base64(consumer_key:consumer_secret), body grant_type=client_credentials
    -> {"access_token": ..., "expires_in": ...}. Search does NOT need a token
    (browsing/searching the Data Store is open); only downloading a product
    does.
  - Collection EO:EUM:DAT:0682 ("Active Fire Monitoring (netCDF) - MTG - 0
    degree") returns real per-cycle products via the search API - MSG's
    older equivalent (EO:EUM:DAT:MSG:FIRC / FRP-SEVIRI, the ID shown on
    EUMETSAT's own product pages) returns "Collection not found" against the
    Data Store's actual collections list, i.e. it's no longer distributed
    there - MTG has operationally superseded it.
  - Each product is a small (~1KB) zip containing one netCDF-4 file with
    that cycle's detected fire pixels (there are usually few or none in any
    single 10-min slice over Spain).

UNVERIFIED: the netCDF variable names below (_LAT_CANDIDATES etc.) are best
guesses from EUMETSAT's public product description ("each pixel has a
latitude and longitude... a fire result of low, medium or high confidence"),
NOT confirmed against a real downloaded file - this account has no
credentials yet (see config.py's eumetsat_consumer_key/secret, both blank
until a real key is added). _parse_fire_pixels logs every variable name it
actually finds in the file on first real ingest, specifically so a wrong
guess here is diagnosable from the logs rather than silently returning 0
rows forever - check that log line once credentials are added and adjust
the candidate lists below if needed.
"""

import base64
import io
import logging
import time
import zipfile
from datetime import datetime, timedelta

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import FireDetection
from app.services.geo_filter import is_in_spain, is_over_water
from app.services.health import record_check

logger = logging.getLogger(__name__)

_token_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}

# Best-guess candidate variable names (checked case-insensitively, first
# match wins) - see the UNVERIFIED note above.
_LAT_CANDIDATES = ["latitude", "lat"]
_LON_CANDIDATES = ["longitude", "lon"]
_CONFIDENCE_CANDIDATES = ["fire_confidence", "fire_result", "confidence", "fire_probability"]
_FRP_CANDIDATES = ["frp", "radiative_power", "fire_radiative_power"]

_logged_variable_names = False  # module-level: only log the diagnostic dump once per process


def is_configured() -> bool:
    return bool(settings.eumetsat_consumer_key and settings.eumetsat_consumer_secret)


def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expires_at"]):
        return str(_token_cache["token"])

    credentials = f"{settings.eumetsat_consumer_key}:{settings.eumetsat_consumer_secret}"
    basic = base64.b64encode(credentials.encode()).decode()
    response = httpx.post(
        settings.eumetsat_token_url,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    expires_in = payload.get("expires_in", 3600)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(30, expires_in - 60)
    return token


def search_products(start: datetime, end: datetime) -> list[dict]:
    """
    Raw OpenSearch (GeoJSON) results for the configured collection - one
    feature per ~10-min MTG full-disk repeat cycle in [start, end]. No auth
    needed (confirmed live - Data Store search/browse is open).
    """
    response = httpx.get(
        settings.eumetsat_search_url,
        params={
            "pi": settings.eumetsat_collection_id,
            "dtstart": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dtend": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "format": "json",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json().get("features", [])


def _download_url(feature: dict) -> str | None:
    links = (feature.get("properties", {}).get("links", {}) or {}).get("data", [])
    return links[0]["href"] if links else None


def _download_product(feature: dict) -> bytes:
    href = _download_url(feature)
    if not href:
        raise RuntimeError(f"Product {feature.get('id')} has no download link")
    token = _get_access_token()
    response = httpx.get(href, headers={"Authorization": f"Bearer {token}"}, timeout=60.0)
    response.raise_for_status()
    return response.content


def _find_var(variable_names: list[str], candidates: list[str]) -> str | None:
    lowered = {name.lower(): name for name in variable_names}
    for candidate in candidates:
        for lower_name, real_name in lowered.items():
            if candidate in lower_name:
                return real_name
    return None


def _parse_netcdf_bytes(nc_bytes: bytes) -> list[dict]:
    global _logged_variable_names
    # Imported lazily: this is a heavy optional dependency only needed when
    # EUMETSAT ingestion is actually configured and running.
    import netCDF4

    pixels: list[dict] = []
    with netCDF4.Dataset("inmemory.nc", memory=nc_bytes) as ds:
        variable_names = list(ds.variables.keys())
        if not _logged_variable_names:
            logger.info("EUMETSAT netCDF variables found in a real product: %s", variable_names)
            _logged_variable_names = True

        lat_var = _find_var(variable_names, _LAT_CANDIDATES)
        lon_var = _find_var(variable_names, _LON_CANDIDATES)
        if not lat_var or not lon_var:
            raise RuntimeError(
                f"Could not find latitude/longitude variables in EUMETSAT product "
                f"(available: {variable_names}) - update _LAT_CANDIDATES/_LON_CANDIDATES "
                f"in eumetsat.py to match"
            )
        confidence_var = _find_var(variable_names, _CONFIDENCE_CANDIDATES)
        frp_var = _find_var(variable_names, _FRP_CANDIDATES)

        lats = ds.variables[lat_var][:]
        lons = ds.variables[lon_var][:]
        confidences = ds.variables[confidence_var][:] if confidence_var else None
        frps = ds.variables[frp_var][:] if frp_var else None

        for i in range(len(lats)):
            pixel = {"latitude": float(lats[i]), "longitude": float(lons[i])}
            if confidences is not None:
                pixel["confidence"] = str(confidences[i])
            if frps is not None:
                pixel["frp"] = float(frps[i])
            pixels.append(pixel)
    return pixels


def _parse_fire_pixels(zip_bytes: bytes) -> list[dict]:
    pixels: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        nc_names = [name for name in zf.namelist() if name.lower().endswith(".nc")]
        for name in nc_names:
            with zf.open(name) as f:
                pixels.extend(_parse_netcdf_bytes(f.read()))
    return pixels


def ingest_eumetsat(db: Session) -> int:
    """
    Searches for new MTG Active Fire Monitoring products since the last
    lookback window, downloads+parses each, and upserts fire pixels over
    Spain. Skipped (not an error) when credentials aren't configured yet.
    """
    state.mark_attempt("eumetsat")
    try:
        count = _ingest_eumetsat(db)
    except Exception as exc:
        record_check(db, "eumetsat", "disrupted", str(exc))
        raise
    record_check(db, "eumetsat", "ok", f"{count} fire pixels processed")
    return count


def _ingest_eumetsat(db: Session) -> int:
    if not is_configured():
        record_check(db, "eumetsat", "skipped", "consumer_key/consumer_secret not configured")
        return 0

    end = datetime.utcnow()
    start = end - timedelta(minutes=settings.eumetsat_lookback_minutes)
    features = search_products(start, end)

    count = 0
    skipped_outside_spain = 0
    skipped_over_water = 0
    for feature in features:
        product_id = feature.get("id") or feature.get("properties", {}).get("identifier") or "unknown"
        date_range = (feature.get("properties", {}).get("date") or "").split("/")
        try:
            captured_at = datetime.strptime(date_range[0], "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, IndexError):
            captured_at = end

        try:
            zip_bytes = _download_product(feature)
            pixels = _parse_fire_pixels(zip_bytes)
        except Exception:
            logger.exception("Failed to download/parse EUMETSAT product %s", product_id)
            continue

        for pixel in pixels:
            latitude = pixel["latitude"]
            longitude = pixel["longitude"]
            if not is_in_spain(latitude, longitude):
                skipped_outside_spain += 1
                continue

            # Geostationary fire products are especially prone to over-water
            # false positives (sun glint, ships, offshore platforms) given
            # their coarser pixel size vs. VIIRS/MODIS - see geo_filter.py's
            # is_over_water docstring.
            if is_over_water(latitude, longitude):
                skipped_over_water += 1
                continue

            external_id = f"{product_id}-{latitude:.4f}-{longitude:.4f}"
            stmt = (
                insert(FireDetection)
                .values(
                    source="EUMETSAT",
                    external_id=external_id,
                    latitude=latitude,
                    longitude=longitude,
                    confidence=pixel.get("confidence"),
                    brightness=None,  # FRP (megawatts) isn't the same unit as brightness temperature - see pixel["frp"] in raw_properties instead
                    acquired_at=captured_at,
                    raw_properties=str(pixel),
                )
                .on_conflict_do_nothing(constraint="uq_source_external_id")
            )
            db.execute(stmt)
            count += 1

    if skipped_outside_spain:
        logger.info(
            "Skipped %d EUMETSAT fire pixel(s) outside Spain (full-disk product covers Europe/Africa/Atlantic)",
            skipped_outside_spain,
        )
    if skipped_over_water:
        logger.info("Skipped %d EUMETSAT fire pixel(s) landing inside a real water body (likely sensor false positive)", skipped_over_water)
    db.commit()
    return count
