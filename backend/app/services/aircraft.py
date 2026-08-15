"""
adsb.lol - free, keyless, community-run ADS-B aggregation API. Confirmed live
(2026-08-15) via GET https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm},
no auth needed, returns a JSON object with an "ac" array of aircraft.

Aircraft positions are inherently live/ephemeral (every few seconds) - this
is deliberately NOT synced into a DB table on a schedule, same reasoning as
the Windy webcams integration (see services/webcams/windy.py): fetched fresh
per map viewport instead, and never persisted.
"""

import math

import httpx

ADSB_LOL_URL = "https://api.adsb.lol/v2/point"
KM_PER_DEGREE = 111.32
NM_PER_KM = 1 / 1.852
# adsb.lol has no documented hard radius cap, but a very zoomed-out viewport
# (e.g. all of Spain) would otherwise ask for a huge, slow response full of
# aircraft nowhere near the visible map - 250nm keeps this a "nearby traffic"
# layer, not a full-country flight tracker.
MAX_RADIUS_NM = 250


def fetch_nearby_aircraft(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[dict]:
    """
    Live fetch for the given map viewport bbox, converted to adsb.lol's own
    circular point+radius query - same bbox-to-center/radius flat-earth
    approximation fetch_windy_webcams already uses, just converted to
    nautical miles (adsb.lol's unit) instead of km.
    """
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    lat_km = (max_lat - center_lat) * KM_PER_DEGREE
    lon_km = (max_lon - center_lon) * KM_PER_DEGREE * math.cos(math.radians(center_lat))
    radius_nm = min(max(1, round(math.hypot(lat_km, lon_km) * NM_PER_KM)), MAX_RADIUS_NM)

    try:
        response = httpx.get(
            f"{ADSB_LOL_URL}/{center_lat}/{center_lon}/{radius_nm}",
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        # Supplementary live layer, not core functionality - a flaky/rate-
        # limited upstream should just mean "no planes shown", not an error
        # surfaced to the whole map.
        return []

    aircraft = []
    for ac in payload.get("ac", []):
        latitude = ac.get("lat")
        longitude = ac.get("lon")
        if latitude is None or longitude is None:
            continue
        aircraft.append(
            {
                "hex": ac.get("hex"),
                "flight": (ac.get("flight") or "").strip() or None,
                "aircraft_type": ac.get("t"),
                "altitude_ft": ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else ac.get("alt_geom"),
                "ground_speed_kt": ac.get("gs"),
                "track_deg": ac.get("track"),
                "latitude": latitude,
                "longitude": longitude,
                "category": ac.get("category"),
            }
        )
    return aircraft
