from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from shapely.geometry import Polygon, mapping

from app.services.geo_filter import segment_crosses_water, water_geometry_near

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


class SubtractWaterRequest(BaseModel):
    # [lat, lon] pairs tracing the hull ring, same order app.js's
    # concaveHull()/smoothRing() already produce - NOT [lon, lat], to avoid
    # the frontend having to flip its own point arrays just for this call.
    points: list[list[float]] = Field(..., min_length=3)


@router.post("/subtract-water")
def subtract_water_endpoint(body: SubtractWaterRequest):
    """
    Cuts real water body geometry (lakes, reservoirs, rivers - see
    geo_filter.WATER_CLC_CODES) out of a hull polygon, addressing the KNOWN
    CAVEAT documented in app.js's renderMap(): a single continuous hull over
    a chain-linked incident can span a real reservoir sitting inside the
    fire's spread corridor, filling it in as if it burned. Returns the
    polygon unchanged (as a GeoJSON Polygon) if no water tile has any data
    for this area - only when real water geometry is actually found nearby
    does the result become a Polygon-with-holes or a MultiPolygon.

    Input `points` are [lat, lon] pairs; output GeoJSON coordinates are the
    standard [lon, lat] order so the frontend can feed the response straight
    into L.geoJSON().
    """
    ring = [(lon, lat) for lat, lon in body.points]
    hull = Polygon(ring)
    if not hull.is_valid:
        hull = hull.buffer(0)

    lats = [lat for lat, _lon in body.points]
    lons = [lon for _lat, lon in body.points]
    water = water_geometry_near(min(lats), min(lons), max(lats), max(lons))

    result = hull if water is None else hull.difference(water)
    if result.is_empty:
        # Water fully swallowed the hull (shouldn't happen in practice for a
        # real fire perimeter, but fail safe by falling back to the
        # untouched hull rather than returning nothing to render).
        result = hull

    return {"subtracted": water is not None, "geometry": mapping(result)}
