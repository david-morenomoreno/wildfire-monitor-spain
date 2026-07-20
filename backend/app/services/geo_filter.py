"""
Real Spain-boundary check, used to filter FireDetection rows at ingestion time.

WHY THIS EXISTS (root cause of the Algeria/France/Portugal spillover):
FIRMS' area/csv endpoint only accepts a rectangular bounding box
(see config.py's `firms_bbox`, "-9.5,35.9,4.4,43.9"). Spain's real border is
an irregular polygon, not a rectangle, so ANY bbox drawn tightly enough to
cover mainland Spain's full extent (Galicia's NW corner, the Pyrenees'
northern edge, Andalucía's southern coast) necessarily also captures slivers
of neighboring countries that happen to fall inside that same rectangle:
  - Northern Algeria's Mediterranean coast (e.g. Aïn Defla, Blida, Bouira -
    confirmed live 2026-07-17: these sit at ~35.9-36.7N, ~2-3.6E, well inside
    the bbox but ~250-300km south of any Spanish territory)
  - Southern France / the Pyrenees (e.g. Bordes-Uchentein, Laruns - ~42.8-43N,
    inside the bbox's northern edge but on the French side of the border)
  - Portugal (sharing a long land border that no rectangle can trace)
This is NOT a bug in the FIRMS query - it's an inherent limitation of
bbox-based querying against an irregularly-shaped country. The fix is to
additionally check each detection against Spain's real boundary polygon
before it's stored, rather than trying to fix it with a "smarter" bbox (no
rectangle can both cover all of mainland Spain and exclude all of Algeria,
since parts of both sit at the same latitude/longitude ranges).

Boundary source: extracted from the "datasets/geo-countries" GeoJSON
(https://github.com/datasets/geo-countries, derived from Natural Earth
admin-0 country boundaries, public domain / ODC-PDDL) - NOT hand-drawn, to
avoid accidentally cutting off real Spanish territory or leaving gaps. The
extracted feature (app/data/spain_boundary.geojson) is a MultiPolygon with
23 parts: mainland Spain, Ceuta, Melilla, the small North-African "plazas de
soberanía" islets, the Balearic Islands, AND the Canary Islands. Including
the Canary Islands' rings in the boundary is harmless here even though this
project doesn't currently ingest data there - `firms_bbox` never reaches
that far west/south in the first place, so this check simply never gets
asked about a Canary Islands point today; it costs nothing to leave them in
the shape rather than hand-editing them out.

A small buffer is added around the polygon to absorb two independent,
unavoidable sources of imprecision: (1) VIIRS/MODIS pixel geolocation error
(hundreds of meters), and (2) the boundary polygon itself being a simplified
vector trace of the real coastline/border, not infinitely precise. Without
some tolerance, a genuine Spanish coastal detection could fall a few dozen
meters "outside" the simplified polygon and get wrongly dropped.

This is the actual root-cause fix. The frontend's "España / Otros países"
grouping (see app.js's isSpainIncident, keyed off FireIncident.country_code
from Nominatim reverse-geocoding) remains in place as a defense-in-depth
backstop - e.g. for detections sitting exactly on a border, or for any
older incidents already in the database from before this filter existed -
not as the primary mechanism.
"""

import json
import logging
import math
from functools import reduce
from pathlib import Path

import httpx
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

BOUNDARY_PATH = Path(__file__).resolve().parent.parent / "data" / "spain_boundary.geojson"

# ~0.02 deg =~ 2.2km at Spain's latitudes - generous enough to cover pixel
# geolocation error and boundary-simplification error, tight enough that it
# never reaches the ~7km+ margins by which the confirmed Algeria/France
# offenders sit outside the real border (see module docstring).
BOUNDARY_BUFFER_DEG = 0.02

_spain_shape: BaseGeometry | None = None


def _load_spain_shape() -> BaseGeometry:
    global _spain_shape
    if _spain_shape is not None:
        return _spain_shape

    with open(BOUNDARY_PATH, encoding="utf-8") as fh:
        payload = json.load(fh)
    geometry_json = payload["features"][0]["geometry"]

    # Built as individual per-part Polygons combined via a pairwise
    # `.union()` reduce, NOT shapely.geometry.shape()/unary_union() - this
    # container's shapely/GEOS build throws a TypeError from both of those
    # vectorized paths on perfectly valid MultiPolygon input (confirmed via
    # direct reproduction), while constructing each Polygon directly and
    # combining with the classic pairwise `.union()` binary op works fine.
    # Same pattern already used by fire_spread.py's _water_shape.
    polygons = []
    for part in geometry_json["coordinates"]:
        exterior, *holes = part
        polygon = Polygon(exterior, holes)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            polygons.append(polygon)

    geometry = reduce(lambda a, b: a.union(b), polygons)
    _spain_shape = geometry.buffer(BOUNDARY_BUFFER_DEG)
    return _spain_shape


def is_in_spain(latitude: float, longitude: float) -> bool:
    """
    True if (latitude, longitude) falls within Spain's real border (mainland,
    Balearic Islands, Ceuta/Melilla and the small North-African islets),
    buffered by ~2km for pixel/geolocation tolerance. See module docstring
    for why this check exists alongside (not instead of) `firms_bbox`.
    """
    spain_shape = _load_spain_shape()
    return spain_shape.contains(Point(longitude, latitude))


# --- Water-body false-positive filtering -----------------------------------
#
# WHY THIS EXISTS: VIIRS/MODIS thermal-anomaly detections can trigger over
# open water (sun glint off waves, offshore platforms/ships, sensor noise) -
# these are real satellite outputs, not ingestion bugs, but they're physically
# impossible as wildfires and show up as isolated stray points inside lakes,
# reservoirs, or the sea. They also cause the frontend's dashed
# incident-connector lines (drawIncidentConnectors in app.js) to draw a
# nonsensical "fire jumped into the middle of the ocean" link, since that
# feature assumes an isolated 1-2 point group is a real spotted ember, not a
# sensor artifact.
#
# Reuses the same EEA Corine Land Cover ArcGIS `query` service already proven
# live in fire_spread.py's fetch_water_rings (water body codes 511/512/521/
# 522/523 = rivers/lakes/lagoons/estuaries/sea). That function fetches a small
# radius around ONE point per fire-spread prediction; filtering every ingested
# detection needs a different shape - confirmed live (2026-07-19) that
# querying geometry for the FULL firms_bbox in one request 500s (the service
# can't return that much water geometry - e.g. the entire Mediterranean coast
# - in a single response), while a 1x1 degree tile reliably returns in under a
# second. So water geometry is fetched and cached per 1-degree tile, lazily,
# the first time a detection lands in that tile - cheap since Spain only
# spans roughly 8x9 tiles and water geography never changes within a process
# lifetime.
CORINE_QUERY_URL = "https://image.discomap.eea.europa.eu/arcgis/rest/services/Corine/CLC2018_WM/MapServer/0/query"
WATER_CLC_CODES = "'511','512','521','522','523'"
WATER_TILE_DEG = 1.0

_water_tile_cache: dict[tuple[int, int], BaseGeometry | None] = {}


def _water_tile_key(latitude: float, longitude: float) -> tuple[int, int]:
    return (math.floor(latitude / WATER_TILE_DEG), math.floor(longitude / WATER_TILE_DEG))


def _fetch_water_tile(tile_lat: int, tile_lon: int) -> BaseGeometry | None:
    lat0, lon0 = tile_lat * WATER_TILE_DEG, tile_lon * WATER_TILE_DEG
    try:
        response = httpx.get(
            CORINE_QUERY_URL,
            params={
                "geometry": f"{lon0},{lat0},{lon0 + WATER_TILE_DEG},{lat0 + WATER_TILE_DEG}",
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
                "where": f"Code_18 IN ({WATER_CLC_CODES})",
                "outFields": "Code_18",
                "returnGeometry": "true",
                "inSR": 4326,
                "outSR": 4326,
                "f": "json",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        features = response.json().get("features", [])
    except Exception:
        logger.warning(
            "Water tile fetch failed for tile (%s, %s) - treating as no water data for this tile",
            tile_lat, tile_lon, exc_info=True,
        )
        return None

    polys = []
    for feature in features:
        for ring in feature.get("geometry", {}).get("rings", []):
            if len(ring) < 3:
                continue
            # ArcGIS gives [lon, lat] pairs, already matching shapely's (x, y).
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            if poly.geom_type == "GeometryCollection":
                polys.extend(g for g in poly.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty)
            elif poly.geom_type in ("Polygon", "MultiPolygon"):
                polys.append(poly)
    if not polys:
        return None
    return reduce(lambda a, b: a.union(b), polys)


def is_over_water(latitude: float, longitude: float) -> bool:
    """
    True if (latitude, longitude) falls inside a real water body (river,
    lake, reservoir, lagoon, or sea) per Corine Land Cover - i.e. this
    detection is very likely a satellite false positive, not a real fire.
    Fails open (returns False - keep the point) if the tile fetch itself
    fails, since silently dropping real detections on a flaky network call
    would be worse than occasionally letting one bad point through.
    """
    key = _water_tile_key(latitude, longitude)
    if key not in _water_tile_cache:
        _water_tile_cache[key] = _fetch_water_tile(*key)
    shape = _water_tile_cache[key]
    if shape is None:
        return False
    return shape.contains(Point(longitude, latitude))


def segment_crosses_water(lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
    """
    True if the straight line between two points passes through a real water
    body. NOT currently called by the frontend (app.js now draws one
    continuous hull per incident directly - see its renderMap() comments -
    rather than stitching separate hull fragments together, which is what
    this was originally built for). Kept as a real, working, tested
    endpoint (see routers/geo.py) for a likely follow-up: that single hull
    can span a real lake/reservoir sitting inside a fire's spread corridor,
    filling it in as if it burned - this is the piece needed to instead cut
    real water geometry out of the hull as a hole. Reuses the same per-tile
    Corine cache as is_over_water, so it's cheap after the first lookup in a
    given area. Fails open (False) if a tile fetch fails, same reasoning as
    is_over_water.
    """
    line = LineString([(lon1, lat1), (lon2, lat2)])
    min_lat, max_lat = sorted((lat1, lat2))
    min_lon, max_lon = sorted((lon1, lon2))
    tile_lat = math.floor(min_lat / WATER_TILE_DEG)
    while tile_lat * WATER_TILE_DEG <= max_lat:
        tile_lon = math.floor(min_lon / WATER_TILE_DEG)
        while tile_lon * WATER_TILE_DEG <= max_lon:
            key = (tile_lat, tile_lon)
            if key not in _water_tile_cache:
                _water_tile_cache[key] = _fetch_water_tile(*key)
            shape = _water_tile_cache[key]
            if shape is not None and shape.intersects(line):
                return True
            tile_lon += 1
        tile_lat += 1
    return False


def water_geometry_near(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> BaseGeometry | None:
    """
    Union of whatever real water body geometry (Corine 511/512/521/522/523 -
    rivers, lakes, reservoirs, lagoons, sea) intersects the given bounding
    box, or None if no tile in that box has any water data at all (either
    genuinely no water there, or every relevant tile fetch failed - both
    treated the same: "nothing to subtract", never crash the caller).

    This is the missing piece the module-level KNOWN CAVEAT comment in
    app.js's renderMap() points at: segment_crosses_water can tell you a
    single line crosses water, but cutting a whole hull polygon needs the
    actual water geometry so shapely can compute polygon.difference(water).
    Reuses the same per-tile Corine cache as is_over_water/
    segment_crosses_water, so repeated calls over the same area (e.g. one
    per incident hull on every map render) are cheap after the first.
    """
    tile_lat = math.floor(min_lat / WATER_TILE_DEG)
    shapes: list[BaseGeometry] = []
    while tile_lat * WATER_TILE_DEG <= max_lat:
        tile_lon = math.floor(min_lon / WATER_TILE_DEG)
        while tile_lon * WATER_TILE_DEG <= max_lon:
            key = (tile_lat, tile_lon)
            if key not in _water_tile_cache:
                _water_tile_cache[key] = _fetch_water_tile(*key)
            shape = _water_tile_cache[key]
            if shape is not None:
                shapes.append(shape)
            tile_lon += 1
        tile_lat += 1
    if not shapes:
        return None
    return reduce(lambda a, b: a.union(b), shapes)
