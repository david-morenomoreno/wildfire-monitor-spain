import logging
import re
import time
import unicodedata

import httpx
from sqlalchemy.orm import Session

from app import state
from app.models import LocalityCache, PlaceGeocodeCache

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim requires an identifying User-Agent for their free usage-policy tier.
USER_AGENT = "WildfireMonitorSpain/1.0 (dev/test - contact via repo)"

# Cache key granularity: ~1km. Coarser than typical cluster spacing so nearby
# hotspots in the same cluster mostly share one cached lookup instead of each
# triggering a fresh Nominatim call.
CACHE_PRECISION = 2

# Nominatim's usage policy permits them to 429 even a compliant ~1 req/sec
# client during load spikes (confirmed live: a heavy historical backfill
# created hundreds of fresh incidents needing lookups in a short window and
# triggered 429s despite wait_for_nominatim_slot throttling every request).
# Retry a handful of times with growing backoff before giving up - this is
# deliberately scoped to 429 only, not a blanket retry-on-any-error.
NOMINATIM_429_MAX_RETRIES = 3
NOMINATIM_429_BASE_DELAY_SECONDS = 2.0


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    header = response.headers.get("Retry-After")
    if header is None:
        return fallback
    try:
        return max(float(header), 0.0)
    except ValueError:
        # Retry-After may also be an HTTP-date; Nominatim doesn't document
        # sending that form, so fall back to our own backoff rather than
        # parsing dates for a case we've never actually observed.
        return fallback


def _get_with_429_retry(url: str, params: dict) -> httpx.Response:
    delay = NOMINATIM_429_BASE_DELAY_SECONDS
    response: httpx.Response | None = None
    for attempt in range(NOMINATIM_429_MAX_RETRIES + 1):
        state.wait_for_nominatim_slot()
        response = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15.0)
        if response.status_code != 429:
            return response
        if attempt == NOMINATIM_429_MAX_RETRIES:
            break
        wait_seconds = _retry_after_seconds(response, delay)
        logger.warning(
            "Nominatim 429 (attempt %d/%d), retrying in %.1fs: %s",
            attempt + 1,
            NOMINATIM_429_MAX_RETRIES,
            wait_seconds,
            url,
        )
        time.sleep(wait_seconds)
        delay *= 2
    return response


def _hashtag_from_locality(name: str) -> str:
    # Border villages often return "Name A / Name B" (bilingual regions) - use the first.
    primary = name.split("/")[0].strip()
    normalized = unicodedata.normalize("NFKD", primary)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", ascii_only)
    return "#IF" + "".join(word.capitalize() for word in words) if words else "#IF"


def reverse_geocode(db: Session, latitude: float, longitude: float) -> dict:
    lat_rounded = round(latitude, CACHE_PRECISION)
    lon_rounded = round(longitude, CACHE_PRECISION)

    cached = (
        db.query(LocalityCache)
        .filter_by(lat_rounded=lat_rounded, lon_rounded=lon_rounded)
        .first()
    )
    if cached:
        return {
            "locality": cached.locality_name,
            "province": cached.province,
            "country_code": cached.country_code,
            "hashtag": cached.hashtag,
            "cached": True,
        }

    response = _get_with_429_retry(
        NOMINATIM_URL,
        params={
            "format": "jsonv2",
            "lat": latitude,
            "lon": longitude,
            "zoom": 12,
            "addressdetails": 1,
        },
    )
    response.raise_for_status()
    payload = response.json()
    address = payload.get("address", {})
    locality = (
        address.get("village")
        or address.get("town")
        or address.get("city")
        or address.get("municipality")
        or payload.get("name")
        or "Unknown"
    )
    province = address.get("province") or address.get("state")
    country_code = (address.get("country_code") or "").upper() or None
    hashtag = _hashtag_from_locality(locality)

    entry = LocalityCache(
        lat_rounded=lat_rounded,
        lon_rounded=lon_rounded,
        locality_name=locality,
        province=province,
        country_code=country_code,
        hashtag=hashtag,
    )
    db.add(entry)
    db.commit()

    return {
        "locality": locality,
        "province": province,
        "country_code": country_code,
        "hashtag": hashtag,
        "cached": False,
    }


def _normalize_query(query: str) -> str:
    return " ".join(query.split()).strip().lower()


def forward_geocode(db: Session, query: str) -> tuple[float, float] | None:
    """
    Best-effort place-name -> (lat, lon) lookup via Nominatim's /search
    endpoint, for sources (e.g. INFOCAM) that publish a municipality/province
    name but no coordinates. Results are cached by the normalized query
    string so a repeated sync for the same place never re-hits Nominatim -
    this can genuinely fail to resolve (ambiguous/unknown place name), in
    which case None is returned rather than fabricating a location.
    """
    normalized = _normalize_query(query)
    if not normalized:
        return None

    cached = db.query(PlaceGeocodeCache).filter_by(query_normalized=normalized).first()
    if cached:
        if cached.latitude is None or cached.longitude is None:
            return None
        return cached.latitude, cached.longitude

    try:
        response = _get_with_429_retry(
            NOMINATIM_SEARCH_URL,
            params={"format": "jsonv2", "q": query, "limit": 1, "countrycodes": "es"},
        )
        response.raise_for_status()
        results = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Forward geocode failed for %r: %s", query, exc)
        return None

    result = None
    if results:
        try:
            result = (float(results[0]["lat"]), float(results[0]["lon"]))
        except (KeyError, TypeError, ValueError):
            result = None

    entry = PlaceGeocodeCache(
        query_normalized=normalized,
        latitude=result[0] if result else None,
        longitude=result[1] if result else None,
    )
    db.add(entry)
    db.commit()

    return result
