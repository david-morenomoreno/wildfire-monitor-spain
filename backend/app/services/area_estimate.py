"""
Server-side equivalent of the map's client-side concave-hull area estimate
(see estimateIncidentGrowth/concaveHull/ringAreaHectares in
frontend/public/app.js).

FireIncident.area_ha is only ever populated when an EFFIS burnt-area
detection lands on that incident (see rebuild_incidents in
services/incidents.py - it's `max(d.area_ha for d in group if d.area_ha is
not None)`, sourced only from EFFIS ingestion). Most incidents never get an
EFFIS overpass, so area_ha is null for most of them. The map already works
around this: it traces a concave hull over an incident's own raw detection
points and reports THAT shape's real-world area whenever the official figure
is missing. Pages that talk to the backend API directly (the ranking/report
views) have no access to that client-side JS state, so this gives them an
equivalent estimate computed in Python.

This is NOT the same algorithm as the frontend's (different concave-hull
library/parameterization, no morphological smoothing pass) - treat it as "a
good-faith estimate of similar quality", not "the literal number the map
would show for the same incident". Callers MUST label any figure this
returns as an estimate, never present it with the same authority as an
official EFFIS area_ha (see the "(oficial)"/"(estimado)" convention already
used in app.js's incident sidebar).
"""

from __future__ import annotations

import math
from functools import reduce

import shapely
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry

# Below this point count a "hull" is either impossible (need >= 3 points to
# enclose any area at all) or so thin on data that any polygon traced over it
# would be fabricating a shape the detections don't actually support - same
# don't-invent-data principle as MIN_HULL_AREA_HA in app.js.
MIN_POINTS_FOR_HULL = 3

# shapely.concave_hull's `ratio` (0 = tightest/most concave hull possible,
# 1 = convex hull) is a unitless fraction of the convex hull's area - unlike
# the frontend's hull.js `concavity`, which is a distance in degrees, so the
# two aren't directly comparable. 0.3 keeps real concave notches (a fire's
# burned extent is rarely a clean convex blob) without collapsing into a
# spiky shape driven by GPS/pixel jitter in individual hotspot detections.
CONCAVE_HULL_RATIO = 0.3

# A handful of detections a couple hundred meters apart can form a
# numerically "valid" but practically meaningless sliver polygon. Same floor
# as the frontend's MIN_HULL_AREA_HA - below this, report "not enough data"
# (None) rather than a technically-real but noise-level number.
MIN_MEANINGFUL_AREA_HA = 3.0

# Meters per degree of latitude - effectively constant across the latitudes
# this app actually operates at (mainland Spain + islands, roughly 27-44N),
# so a single constant is fine here; longitude scales it by cos(latitude)
# below.
_METERS_PER_DEG_LAT = 110_574


def _project_to_local_meters(points: list[tuple[float, float]]) -> list[Point]:
    """
    Converts (lat, lon) pairs to a local flat-earth meters projection
    centered on the cluster's own mean position - the same "treat the small
    area as locally flat" approximation the frontend's convexHull() comment
    documents. Good enough at the few-km scale a single incident spans; not
    intended for anything larger.
    """
    lat0 = sum(lat for lat, _lon in points) / len(points)
    lon0 = sum(lon for _lat, lon in points) / len(points)
    meters_per_deg_lon = _METERS_PER_DEG_LAT * math.cos(math.radians(lat0))
    return [
        Point((lon - lon0) * meters_per_deg_lon, (lat - lat0) * _METERS_PER_DEG_LAT)
        for lat, lon in points
    ]


def _multipoint_from(points: list[Point]) -> BaseGeometry:
    # Deliberately NOT shapely.geometry.MultiPoint(points) / shapely.
    # multipoints() - this container's shapely/GEOS build throws a TypeError
    # from that vectorized construction path on perfectly valid input
    # (confirmed via direct reproduction - same issue already documented in
    # geo_filter.py's _load_spain_shape and fire_spread.py's _water_shape).
    # A pairwise `.union()` reduce of individual Points works fine and
    # produces an equivalent MultiPoint.
    return reduce(lambda a, b: a.union(b), points)


def estimate_area_ha(points: list[tuple[float, float]]) -> float | None:
    """
    Estimates a real-world hectare figure for a fire's extent from its raw
    (lat, lon) detection points, tracing a concave hull the same way the
    map's estimateIncidentGrowth() does client-side - for use anywhere
    (report/rankings endpoints) that can't reach into that client-side JS
    state.

    Returns None - never a fabricated number - when there aren't enough
    distinct points to trace a shape, or the shape traced is a
    near-collinear sliver too small to trust as a real extent (see
    MIN_MEANINGFUL_AREA_HA).
    """
    unique_points = list({(lat, lon) for lat, lon in points})
    if len(unique_points) < MIN_POINTS_FOR_HULL:
        return None

    projected = _project_to_local_meters(unique_points)
    multipoint = _multipoint_from(projected)

    try:
        hull: BaseGeometry = shapely.concave_hull(multipoint, ratio=CONCAVE_HULL_RATIO)
        if not isinstance(hull, Polygon) or hull.is_empty:
            hull = multipoint.convex_hull
    except Exception:
        # Best-effort estimate, not a critical path - any GEOS/shapely
        # hiccup on unusual input falls back to the simpler, more robust
        # convex hull rather than failing the whole request.
        hull = multipoint.convex_hull

    if not isinstance(hull, Polygon) or hull.is_empty:
        return None  # degenerate/collinear input - no meaningful shape to report

    area_ha = hull.area / 10_000  # m^2 -> ha
    if area_ha < MIN_MEANINGFUL_AREA_HA:
        return None
    return area_ha
