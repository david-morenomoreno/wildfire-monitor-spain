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
from functools import reduce
from pathlib import Path

from shapely.geometry import Point, Polygon
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
