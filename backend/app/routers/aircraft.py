from fastapi import APIRouter, HTTPException, Query

from app.schemas import AircraftOut
from app.services.aircraft import fetch_nearby_aircraft

router = APIRouter(prefix="/api/aircraft", tags=["aircraft"])


@router.get("", response_model=list[AircraftOut])
def list_aircraft(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat - fetched live, not from our DB"),
):
    """
    Live nearby-aircraft layer (adsb.lol) for the current map viewport -
    useful for spotting aerial firefighting assets near a fire. Deliberately
    NOT backed by a DB table, same reasoning as GET /api/webcams/windy:
    aircraft positions are stale within seconds, so this is fetched fresh on
    every call rather than synced on a schedule.
    """
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'")
    try:
        return fetch_nearby_aircraft(min_lon, min_lat, max_lon, max_lat)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Aircraft fetch failed: {exc}") from exc
