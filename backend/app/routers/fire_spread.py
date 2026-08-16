from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FireDetection, FireIncident
from app.services.fire_spread import (
    MAX_PREDICTION_HOURS,
    RECENT_HOTSPOT_WINDOW_HOURS,
    WEATHER_TIMELINE_FORECAST_HOURS,
    WEATHER_TIMELINE_PAST_HOURS,
    fetch_weather_timeline,
    fetch_wind_field,
    predict_incident_spread,
    predict_spread,
)
from app.services.incidents import INCIDENT_REASSOCIATION_DEG

router = APIRouter(prefix="/api/fire-spread", tags=["fire-spread"])


@router.get("/predict")
def predict(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    max_hours: int = Query(MAX_PREDICTION_HOURS, ge=1, le=MAX_PREDICTION_HOURS),
):
    """
    Experimental: given an arbitrary point (the standalone "place origin"
    tool), estimates the affected-area ellipse for each hour of the
    Open-Meteo wind forecast, using a Corine-derived fuel guess and local
    slope. For an actual tracked incident, prefer /predict-incident below -
    it starts from the fire's own recent leading edge(s) instead of a single
    clicked point, and knows about its already-burnt extent. See
    app/services/fire_spread.py's module docstring for the model and its
    limitations - this is a POC, not an operational tool.
    """
    try:
        return predict_spread(lat, lon, max_hours=max_hours)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire spread prediction failed: {exc}") from exc


def _incident_detection_points(db: Session, incident: FireIncident) -> list[tuple]:
    """Raw (lat, lon, acquired_at) detections plausibly belonging to this
    incident - same proximity re-query as routers/incidents.py's
    _detections_near_incident, just also carrying acquired_at (needed here
    to tell "recent" apart from "long since settled"), which that function's
    other callers don't need."""
    window_start = incident.first_detected_at - timedelta(hours=1)
    window_end = incident.last_detected_at + timedelta(hours=1)
    candidates = (
        db.query(FireDetection.latitude, FireDetection.longitude, FireDetection.acquired_at)
        .filter(
            FireDetection.acquired_at >= window_start,
            FireDetection.acquired_at <= window_end,
            FireDetection.latitude >= incident.centroid_lat - INCIDENT_REASSOCIATION_DEG,
            FireDetection.latitude <= incident.centroid_lat + INCIDENT_REASSOCIATION_DEG,
            FireDetection.longitude >= incident.centroid_lon - INCIDENT_REASSOCIATION_DEG,
            FireDetection.longitude <= incident.centroid_lon + INCIDENT_REASSOCIATION_DEG,
        )
        .all()
    )
    return [
        (lat, lon, acquired_at)
        for lat, lon, acquired_at in candidates
        if ((lat - incident.centroid_lat) ** 2 + (lon - incident.centroid_lon) ** 2) ** 0.5
        <= INCIDENT_REASSOCIATION_DEG
    ]


@router.get("/predict-incident/{incident_id}")
def predict_for_incident(
    incident_id: int,
    max_hours: int = Query(MAX_PREDICTION_HOURS, ge=1, le=MAX_PREDICTION_HOURS),
    db: Session = Depends(get_db),
):
    """
    Multi-front prediction for a tracked incident (see
    services.fire_spread.predict_incident_spread): predicts FROM the fire's
    own most-recently-active leading edge(s), not its overall centroid, and
    suppresses backward spread into ground the fire has already burnt.
    """
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    points = _incident_detection_points(db, incident)
    if not points:
        raise HTTPException(status_code=404, detail="No detections to predict from")

    recent_cutoff = incident.last_detected_at - timedelta(hours=RECENT_HOTSPOT_WINDOW_HOURS)
    recent_points = [(lat, lon) for lat, lon, acquired_at in points if acquired_at >= recent_cutoff]
    if not recent_points:
        # This incident's satellite passes have all gone quiet recently
        # (cooling/archived, or just an unlucky coverage gap) - fall back to
        # everything it has rather than a hard 404 for a fire that's still
        # nominally active.
        recent_points = [(lat, lon) for lat, lon, _at in points]

    try:
        return predict_incident_spread(
            burnt_extent_points=[(lat, lon) for lat, lon, _at in points],
            recent_front_points=recent_points,
            max_hours=max_hours,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire spread prediction failed: {exc}") from exc


@router.get("/wind-field")
def wind_field(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    hours: int = Query(24, ge=1, le=48),
):
    """
    Windy-style wind arrow field for the map's current viewport - a grid of
    points across the given bbox, each with an hourly wind speed/direction
    series (see fetch_wind_field). Scrubbing the map's timeline forward just
    indexes further into each point's already-fetched series client-side,
    same pattern as the single-point /predict forecast above.
    """
    if east <= west or north <= south:
        raise HTTPException(status_code=400, detail="Invalid bbox: east must be > west and north must be > south")
    try:
        return fetch_wind_field(west, south, east, north, hours=hours)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Wind field fetch failed: {exc}") from exc


@router.get("/weather-timeline")
def weather_timeline(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    past_hours: int = Query(WEATHER_TIMELINE_PAST_HOURS, ge=1, le=48),
    forecast_hours: int = Query(WEATHER_TIMELINE_FORECAST_HOURS, ge=1, le=72),
):
    """
    Continuous past-through-forecast hourly weather for one point (see
    fetch_weather_timeline) - the data behind the map's weather-at-the-fire
    popup and the incident sidebar's hourly strip. Standalone lat/lon
    endpoint (not incident-scoped) since a fire's location doesn't need any
    of predict-incident's fronts/burnt-extent machinery to answer "what's the
    weather here".
    """
    timeline = fetch_weather_timeline(lat, lon, past_hours=past_hours, forecast_hours=forecast_hours)
    if timeline is None:
        raise HTTPException(status_code=502, detail="Weather timeline fetch failed")
    return timeline
