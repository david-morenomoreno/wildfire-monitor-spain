"""
Windy Webcams API (v3) - confirmed live (2026-07-16) via
GET https://api.windy.com/webcams/api/v3/webcams with header
"x-windy-api-key: <key>" and params nearby=lat,lon,radius_km / limit / include.

Unlike the DGT source (services/webcams/dgt.py), this is NOT synced into the
Webcam table on a schedule - Windy's own docs say its image URLs carry a
short-lived signed token (10 minutes on the free tier, 24 hours on
Professional) and recommend calling the endpoint fresh every time the page
loads. Storing a snapshot's URL in our DB and serving it hours later would
just return an expired-token 401, so this fetches live per map viewport
instead (see routers/webcams.py's /windy endpoint) and is never persisted.
"""

import math

import httpx

from app.config import settings

WINDY_API_URL = "https://api.windy.com/webcams/api/v3/webcams"
KM_PER_DEGREE = 111.32


def fetch_windy_webcams(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, limit: int = 50
) -> list[dict]:
    """
    Live fetch for the given map viewport bbox, converted to Windy's own
    circular `nearby=lat,lon,radius_km` query (center of the bbox, radius =
    distance to the farthest corner - the same flat-earth degree-to-km
    approximation used elsewhere in this codebase, e.g. fire_spread.py).
    Returns plain dicts shaped like the Webcam model/WebcamOut schema so the
    router can return them alongside DGT cameras without a second schema.
    """
    if not settings.windy_api_key:
        return []

    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    lat_km = (max_lat - center_lat) * KM_PER_DEGREE
    lon_km = (max_lon - center_lon) * KM_PER_DEGREE * math.cos(math.radians(center_lat))
    # Windy's `nearby` radius must be a plain integer - "33.8" 400s with
    # "Invalid nearby string" while "33" and "50" both work (confirmed live).
    radius_km = max(1, round(math.hypot(lat_km, lon_km)))

    response = httpx.get(
        WINDY_API_URL,
        params={
            "nearby": f"{center_lat},{center_lon},{radius_km}",
            "limit": limit,
            "include": "images,location",
        },
        headers={"x-windy-api-key": settings.windy_api_key},
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()

    webcams = []
    for cam in payload.get("webcams", []):
        if cam.get("status") != "active":
            continue
        location = cam.get("location") or {}
        preview_url = ((cam.get("images") or {}).get("current") or {}).get("preview")
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if not preview_url or latitude is None or longitude is None:
            continue

        webcam_id = cam["webcamId"]
        place = ", ".join(filter(None, [location.get("city"), location.get("region")]))
        webcams.append(
            {
                # Negative id so it can never collide with a real DB
                # (DGT-sourced) primary key - this row is never persisted.
                "id": -webcam_id,
                "source": "windy",
                "external_id": str(webcam_id),
                "name": cam.get("title"),
                "road": None,
                "province": place or None,
                "latitude": latitude,
                "longitude": longitude,
                "image_url": preview_url,
                "updated_at": cam.get("lastUpdatedOn"),
            }
        )
    return webcams
