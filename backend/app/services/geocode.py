import re
import unicodedata

import httpx
from sqlalchemy.orm import Session

from app import state
from app.models import LocalityCache

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim requires an identifying User-Agent for their free usage-policy tier.
USER_AGENT = "WildfireMonitorSpain/1.0 (dev/test - contact via repo)"

# Cache key granularity: ~1km. Coarser than typical cluster spacing so nearby
# hotspots in the same cluster mostly share one cached lookup instead of each
# triggering a fresh Nominatim call.
CACHE_PRECISION = 2


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

    state.wait_for_nominatim_slot()
    response = httpx.get(
        NOMINATIM_URL,
        params={
            "format": "jsonv2",
            "lat": latitude,
            "lon": longitude,
            "zoom": 12,
            "addressdetails": 1,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
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
