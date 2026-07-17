"""
Proximity alerts (experimental POC, same disclaimers as fire_spread.py):
given a user's current location, checks whether any nearby ACTIVE incident's
predicted 24h growth footprint (reusing fire_spread.predict_spread - the
same elliptical growth model used for the manual "place origin" tool) would
reach that point, and if so, roughly how many hours until it does.

This is deliberately request-time, not a background job (Phase 1 per the
user's own choice of "in-app alert while open" over a full push-notification
backend) - the frontend polls this periodically while location alerts are
enabled and shows a browser Notification if something comes back.
"""

import logging
import math

from shapely.geometry import Point, Polygon
from sqlalchemy.orm import Session

from app.models import FireIncident
from app.services.fire_spread import predict_spread

logger = logging.getLogger(__name__)

METERS_PER_DEGREE_LAT = 111_320.0
# Only bother running the (expensive - several external API calls) growth
# prediction for incidents that could plausibly reach the user at all. The
# model's own sanity cap works out to ~144km/24h in the theoretical extreme,
# but that's never actually hit in practice (see fire_spread.py's own
# comments) - 30km comfortably covers any realistic scenario without wasting
# API calls on incidents nowhere near the user.
MAX_CANDIDATE_DISTANCE_KM = 30.0


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat_km = (lat2 - lat1) * METERS_PER_DEGREE_LAT / 1000
    lon_km = (lon2 - lon1) * METERS_PER_DEGREE_LAT / 1000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(lat_km, lon_km)


def check_proximity(db: Session, lat: float, lon: float) -> list[dict]:
    candidates = (
        db.query(FireIncident)
        .filter(FireIncident.status == "active")
        .all()
    )
    nearby = [
        inc
        for inc in candidates
        if _distance_km(lat, lon, inc.centroid_lat, inc.centroid_lon) <= MAX_CANDIDATE_DISTANCE_KM
    ]

    alerts = []
    for incident in nearby:
        try:
            prediction = predict_spread(incident.centroid_lat, incident.centroid_lon, max_hours=24)
        except Exception:
            logger.warning(
                "Proximity check: spread prediction failed for incident %s", incident.id, exc_info=True
            )
            continue

        point = Point(lon, lat)
        reached_at_hour = None
        for hour_entry in prediction["hourly"]:
            polygon_latlon = hour_entry["polygon"]
            polygon = Polygon([(p[1], p[0]) for p in polygon_latlon])  # [lat,lon] -> (lon,lat)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.contains(point):
                reached_at_hour = hour_entry["hour"]
                break

        if reached_at_hour is not None:
            alerts.append(
                {
                    "incident_id": incident.id,
                    "locality": incident.locality,
                    "province": incident.province,
                    "risk_level": incident.risk_level,
                    "hours_until_reach": reached_at_hour,
                    "distance_km": round(_distance_km(lat, lon, incident.centroid_lat, incident.centroid_lon), 1),
                }
            )

    return alerts
