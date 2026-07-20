"""
Shared EUMETSAT Data Store API client - OAuth2 token exchange, OpenSearch
product discovery, and authenticated product download. Used by both
services/eumetsat.py (MTG geostationary Active Fire Monitoring) and
services/sentinel3.py (Sentinel-3 SLSTR Fire Radiative Power) - both live
under the same EUMETSAT account/credentials, confirmed LIVE (2026-07-19)
against collections EO:EUM:DAT:0682 and EO:EUM:DAT:0417 respectively.

PAGINATION: the search API returns only 10 features per page by default
(confirmed live) regardless of how wide [dtstart, dtend] is - a caller that
needs more than 10 products in one window (e.g. an ad-hoc historical check
spanning hours) will silently get only the newest/first 10 unless `c` (page
size) is passed. search_products() below always passes a page size, but it
is still capped - callers checking a genuinely long window must paginate via
the response's own `next` link themselves (not needed for normal polling,
where the lookback window is short enough that a handful of products is
already everything).
"""

import base64
import time

import httpx

from app.config import settings

_token_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}


def is_configured() -> bool:
    return bool(settings.eumetsat_consumer_key and settings.eumetsat_consumer_secret)


def get_access_token() -> str:
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


def search_products(collection_id: str, start, end, bbox: str | None = None, page_size: int = 100) -> list[dict]:
    """
    Raw OpenSearch (GeoJSON) results for the given collection in [start, end].
    No auth needed (confirmed live - Data Store search/browse is open) -
    only downloading a product requires a token. `bbox` (if given) must be
    "minLon,minLat,maxLon,maxLat" (same convention as settings.firms_bbox).
    """
    params = {
        "pi": collection_id,
        "dtstart": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "dtend": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "format": "json",
        "c": page_size,
    }
    if bbox:
        params["bbox"] = bbox
    response = httpx.get(settings.eumetsat_search_url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json().get("features", [])


def download_url(feature: dict) -> str | None:
    links = (feature.get("properties", {}).get("links", {}) or {}).get("data", [])
    return links[0]["href"] if links else None


def download_product(feature: dict) -> bytes:
    href = download_url(feature)
    if not href:
        raise RuntimeError(f"Product {feature.get('id')} has no download link")
    token = get_access_token()
    response = httpx.get(href, headers={"Authorization": f"Bearer {token}"}, timeout=60.0)
    response.raise_for_status()
    return response.content
