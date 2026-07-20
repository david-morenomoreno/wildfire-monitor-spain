from fastapi import APIRouter, Query

from app.services.geo_filter import segment_crosses_water

router = APIRouter(prefix="/api/geo", tags=["geo"])


@router.get("/segment-crosses-water")
def segment_crosses_water_endpoint(
    lat1: float = Query(..., ge=-90, le=90),
    lon1: float = Query(..., ge=-180, le=180),
    lat2: float = Query(..., ge=-90, le=90),
    lon2: float = Query(..., ge=-180, le=180),
):
    """
    See services/geo_filter.py's segment_crosses_water docstring - not
    currently called by the frontend, kept for a likely follow-up (cutting
    real water bodies out of an incident's hull polygon as a hole).
    """
    return {"crosses_water": segment_crosses_water(lat1, lon1, lat2, lon2)}
