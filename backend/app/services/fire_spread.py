"""
Experimental wildfire spread POC - NOT an operational fire behavior model.

Given a clicked origin point (or, for a tracked incident, its own recent
active front(s) - see predict_incident_spread), estimates how far a fire
might spread over the next few hours (MAX_PREDICTION_HOURS) using the
elliptical growth model (Huygens' wavelet
principle) that Canada's FBP (Fire Behaviour Prediction) System and FARSITE
both use: wind speed sets the ellipse's length-to-breadth ratio, which then
splits one base rate of spread into head/flank/back rates.

Wind is NOT held constant across the whole window - each hour's own
forecasted wind (Open-Meteo) drives that hour's head/flank/back increment,
and increments are summed cumulatively (a simplified stand-in for proper
multi-point Huygens' perimeter propagation, which would need polygon-union
geometry across many perimeter points - out of scope for a POC). The
resulting shape can bend over the prediction window as wind direction shifts,
via a ROS-weighted vector-average bearing, rather than assuming one fixed
direction for the whole forecast.

Formulas and constants below are cited inline - this is deliberately the
simplified end of that family of models (no calibrated Rothermel fuel model,
no fuel moisture), appropriate for a rough POC, not a real evacuation/
suppression planning tool.
"""

import logging
import math
from datetime import datetime, timezone
from functools import reduce

import httpx
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry

from app.services.area_estimate import estimate_area_and_hull

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
# EEA's own Corine Land Cover ArcGIS service - confirmed live (2026-07-16),
# returns a real CLC 2018 land-cover code for a point via `identify`.
CORINE_IDENTIFY_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/Corine/CLC2018_WM/MapServer/identify"
)
# Same service's `query` operation on the land-cover layer (id 0) - unlike
# `identify` (one point in, one code out), this returns actual polygon
# geometry for every water feature intersecting a bounding box, fetched once
# per prediction so every hour's ellipse can be clipped locally afterwards
# (no per-vertex network round-trip). Confirmed live (2026-07-16) against the
# real Embalse de El Burguillo reservoir.
CORINE_QUERY_URL = "https://image.discomap.eea.europa.eu/arcgis/rest/services/Corine/CLC2018_WM/MapServer/0/query"

METERS_PER_DEGREE_LAT = 111_320.0
# How far out to fetch water geometry, once, at the start of a prediction.
# Generous relative to realistic 24h spread (the model's own 100 m/min head
# ROS sanity cap works out to ~144km in the theoretical worst case, but that
# cap is almost never actually reached - 20km comfortably covers any
# plausible real scenario without fetching/holding an unreasonably large
# polygon set for the common case).
WATER_FETCH_RADIUS_M = 20_000.0

# The elliptical Van Wagner model only holds up for the first few hours,
# where wind is genuinely the dominant driver - past that, real fires bend
# around terrain/fuel discontinuities (valleys, fields, firebreaks) this POC
# has no way to represent, and the compounding wind-forecast uncertainty
# itself grows fast enough by hour ~12-24 to make the ellipse more
# misleading than informative. 8h keeps predictions inside the window where
# "wind-driven ellipse" is still a defensible approximation - was 24h.
MAX_PREDICTION_HOURS = 8

# How far back to look for "currently active" detections when picking a
# fire's leading edge(s) to predict FROM (see predict_incident_spread) -
# recent enough that a quiet flank isn't mistaken for still-advancing, wide
# enough that a single satellite pass gap doesn't wrongly read as "no longer
# active" here.
RECENT_HOTSPOT_WINDOW_HOURS = 6
# Detections within this many degrees (~3km) of each other are treated as
# the same active front rather than two separate ones - tuned to separate
# genuinely distinct flanks (e.g. two sides of a ridge) without fragmenting
# one contiguous edge's own GPS/pixel jitter into spurious extra fronts.
FRONT_CLUSTER_DEG = 0.03
# Hard cap on how many simultaneous fronts get their own prediction - a
# heavily fragmented detection set (many small satellite-noise clusters)
# would otherwise explode into dozens of near-duplicate, hard-to-read
# ellipses instead of the fire's few real leading edges.
MAX_ACTIVE_FRONTS = 3
# How close (degrees, ~1.1km) a recent detection needs to be to the burnt
# extent's own boundary to still count as a leading-edge candidate rather
# than an interior re-detection (see _is_exterior_point) - loose enough that
# a real edge point just inside the hull (concave hulls are an
# approximation, not a perfect boundary) isn't wrongly excluded, tight
# enough that it doesn't just let the whole interior back in.
FRONT_BOUNDARY_BUFFER_DEG = 0.01

# CLC (Corine Land Cover) 3-digit codes -> (label, base ROS in m/min at a
# reference ~10 km/h wind on flat ground). These are rough order-of-magnitude
# figures for a POC, not calibrated fuel-model outputs - grass/light fuels
# spread fastest, dense broadleaf forest slowest, non-flammable land doesn't
# spread at all.
FUEL_TABLE: dict[str, tuple[str, float]] = {
    "211": ("tierras de labor", 8.0),
    "212": ("tierras de labor (regadío)", 8.0),
    "213": ("arrozales", 8.0),
    "221": ("viñedos", 5.0),
    "222": ("frutales", 5.0),
    "223": ("olivares", 5.0),
    "231": ("pastizales", 9.0),
    "241": ("cultivos anuales y permanentes", 7.0),
    "242": ("mosaico de cultivos", 7.0),
    "243": ("agricultura con vegetación natural", 6.5),
    "311": ("bosque de frondosas", 2.5),
    "312": ("bosque de coníferas", 4.5),
    "313": ("bosque mixto", 3.5),
    "321": ("pastizal natural", 9.5),
    "322": ("landas y matorrales", 7.0),
    "323": ("vegetación esclerófila (matorral)", 7.5),
    "324": ("matorral boscoso de transición", 8.5),
    "331": ("playas, dunas, arena", 0.0),
    "332": ("roca desnuda", 0.0),
    "333": ("áreas con vegetación escasa", 3.0),
    "334": ("zonas quemadas", 1.0),
    "335": ("glaciares y nieves permanentes", 0.0),
}
DEFAULT_FUEL = ("desconocido/sin clasificar", 4.0)
NON_FLAMMABLE_PREFIXES = ("1", "4", "5")  # urban fabric (1xx), wetlands (4xx), water bodies (5xx)


def fetch_wind_series(lat: float, lon: float, max_hours: int = 24) -> list[dict]:
    """
    Hourly wind speed (km/h) + direction (degrees, meteorological 'from'
    convention), temperature (°C) and relative humidity (%) for the next
    `max_hours` hours starting at the current hour, via Open-Meteo - free, no
    API key, confirmed live. forecast_days=3 keeps a safety margin so
    "current hour + 24" never runs past the end of the returned series
    regardless of what time of day "now" is.

    Temperature/humidity were added alongside the wind fields this function
    already fetched for the spread model - same request, same free API, no
    extra round-trip - so the map sidebar's "Previsión" section (index[0] of
    this series, i.e. the current hour) can show more than wind without a
    second weather source. Neither factors into the spread model itself
    (still wind/fuel/slope only, see predict_spread) - they're carried
    through purely for display.
    """
    response = httpx.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "windspeed_10m,winddirection_10m,temperature_2m,relativehumidity_2m",
            "forecast_days": 3,
            "timezone": "UTC",
        },
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    hourly = payload["hourly"]
    times = hourly["time"]
    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    try:
        start_idx = times.index(now_hour)
    except ValueError:
        start_idx = 0
    end_idx = min(start_idx + max_hours, len(times))
    return [
        {
            "time": times[i],
            "speed_kmh": hourly["windspeed_10m"][i],
            "direction_from_deg": hourly["winddirection_10m"][i],
            "temperature_c": hourly["temperature_2m"][i],
            "humidity_pct": hourly["relativehumidity_2m"][i],
        }
        for i in range(start_idx, end_idx)
    ]


# How many grid points to sample per axis at most - a Windy-style arrow
# field only needs to look dense at typical zoom levels, not literally
# sample every 0.1 degree; capping this keeps both the Open-Meteo request
# (one HTTP call, comma-joined lat/lon lists - see fetch_wind_field) and the
# number of arrows Leaflet has to paint bounded regardless of how far
# zoomed-out the current viewport is (a whole-Spain view spans ~14 x 8
# degrees).
WIND_FIELD_MAX_POINTS_PER_AXIS = 9
# Floor on grid spacing (degrees) - prevents an absurdly dense request if a
# caller passes a tiny bbox (deep zoom-in), where "one arrow every few
# hundred meters" would be meaningless at this forecast's resolution anyway.
WIND_FIELD_MIN_STEP_DEG = 0.25


def _wind_field_grid(west: float, south: float, east: float, north: float) -> list[tuple[float, float]]:
    lon_step = max((east - west) / WIND_FIELD_MAX_POINTS_PER_AXIS, WIND_FIELD_MIN_STEP_DEG)
    lat_step = max((north - south) / WIND_FIELD_MAX_POINTS_PER_AXIS, WIND_FIELD_MIN_STEP_DEG)

    lons = []
    lon = west + lon_step / 2  # offset half a step so points sit inside the bbox, not straddling its edge
    while lon < east:
        lons.append(round(lon, 3))
        lon += lon_step

    lats = []
    lat = south + lat_step / 2
    while lat < north:
        lats.append(round(lat, 3))
        lat += lat_step

    return [(lat, lon) for lat in lats for lon in lons]


def fetch_wind_field(west: float, south: float, east: float, north: float, hours: int = 24) -> list[dict]:
    """
    Windy-style wind field: an hourly wind speed/direction series for a grid
    of points across the given viewport bbox, in ONE Open-Meteo request -
    it natively supports batching many locations by comma-joining their
    latitude/longitude lists and returns one result object per location, in
    the same order (confirmed live) - so this costs the same single round
    trip as fetch_wind_series' one-point version, not one call per grid
    point. Each point's own `hours` array is later indexed client-side as
    the map's timeline scrubber moves, exactly like fetch_wind_series'
    single-point series already is for the fire-spread ellipse - no
    refetching per hour dragged.
    """
    points = _wind_field_grid(west, south, east, north)
    if not points:
        return []

    response = httpx.get(
        OPEN_METEO_URL,
        params={
            "latitude": ",".join(str(lat) for lat, _lon in points),
            "longitude": ",".join(str(lon) for _lat, lon in points),
            "hourly": "windspeed_10m,winddirection_10m",
            "forecast_days": 3,
            "timezone": "UTC",
        },
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    # Open-Meteo returns a bare object (not a list) when only one location
    # ends up being requested - normalize so the zip below always works.
    results = payload if isinstance(payload, list) else [payload]

    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    field = []
    for (lat, lon), location in zip(points, results):
        hourly = location.get("hourly")
        if not hourly:
            continue
        times = hourly["time"]
        try:
            start_idx = times.index(now_hour)
        except ValueError:
            start_idx = 0
        end_idx = min(start_idx + hours, len(times))
        series = [
            {
                "speed_kmh": hourly["windspeed_10m"][i],
                "direction_from_deg": hourly["winddirection_10m"][i],
            }
            for i in range(start_idx, end_idx)
        ]
        if series:
            field.append({"lat": lat, "lon": lon, "hours": series})
    return field


def fetch_fuel_type(lat: float, lon: float) -> dict:
    """Corine Land Cover class at the point, mapped to a rough fuel category."""
    pad = 0.05
    response = httpx.get(
        CORINE_IDENTIFY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": f"{lon - pad},{lat - pad},{lon + pad},{lat + pad}",
            "imageDisplay": "400,400,96",
            "f": "json",
        },
        timeout=15.0,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return {"clc_code": None, "label": DEFAULT_FUEL[0], "base_ros_m_per_min": DEFAULT_FUEL[1]}

    code = results[0].get("attributes", {}).get("Code_18")
    if code in FUEL_TABLE:
        label, base_ros = FUEL_TABLE[code]
    elif code and code.startswith(NON_FLAMMABLE_PREFIXES):
        label, base_ros = "non-flammable (urban/water/wetland)", 0.0
    else:
        label, base_ros = DEFAULT_FUEL
    return {"clc_code": code, "label": label, "base_ros_m_per_min": base_ros}


def fetch_water_rings(lat: float, lon: float, radius_m: float = WATER_FETCH_RADIUS_M) -> list[list[tuple[float, float]]]:
    """
    Fetches actual water-body polygon geometry (rivers, lakes, reservoirs,
    coastal water) within `radius_m` of the origin, ONCE per prediction, via
    the Corine Land Cover layer's `query` operation (confirmed live - returns
    real ring geometry, e.g. a ~2900-point ring for the Embalse de El
    Burguillo reservoir). Every hour's ellipse is then clipped against this
    locally (see _clip_point_to_water) instead of making a network call per
    polygon vertex per hour, which would be far too slow (72 vertices x up to
    24 hours). Returns a list of rings, each a list of (lat, lon) points -
    empty list (not an exception) if the fetch fails or no water is nearby,
    so a flaky response just means "no water clipping this time", not a
    failed prediction.
    """
    lat_pad = radius_m / METERS_PER_DEGREE_LAT
    lon_pad = radius_m / (METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    try:
        response = httpx.get(
            CORINE_QUERY_URL,
            params={
                "geometry": f"{lon - lon_pad},{lat - lat_pad},{lon + lon_pad},{lat + lat_pad}",
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
                "where": "Code_18 IN ('511','512','521','522','523')",
                "outFields": "Code_18",
                "returnGeometry": "true",
                "inSR": 4326,
                "outSR": 4326,
                "f": "json",
            },
            timeout=15.0,
        )
        response.raise_for_status()
        features = response.json().get("features", [])
    except Exception:
        logger.warning("Water polygon fetch failed for (%s, %s), assuming no nearby water", lat, lon, exc_info=True)
        return []

    rings = []
    for feature in features:
        for ring in feature.get("geometry", {}).get("rings", []):
            rings.append([(point[1], point[0]) for point in ring])  # ArcGIS gives [lon, lat]
    return rings


def _water_shape(water_rings: list[list[tuple[float, float]]]) -> BaseGeometry | None:
    """
    Unions all water rings into one shapely geometry, in (lon, lat) order to
    match shapely's (x, y) convention. Each ring is treated as an independent
    simple polygon (ignoring ArcGIS's exterior/hole winding-order semantics) -
    a small island inside a lake would be mis-treated as water too, an
    accepted simplification for this POC. `.buffer(0)` is the standard
    shapely idiom for fixing a self-intersecting ring before using it in a
    boolean operation. Returns None if there's no usable water geometry.

    Combines polygons via a pairwise `.union()` reduce, not
    shapely.ops.unary_union() - this container's shapely/GEOS build throws a
    TypeError from unary_union's vectorized path on perfectly valid Polygon
    inputs (confirmed via direct reproduction), while the classic pairwise
    `.union()` binary op works fine.
    """
    polys = []
    for ring in water_rings:
        if len(ring) < 3:
            continue
        poly = Polygon([(lon, lat) for lat, lon in ring])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        # buffer(0) on a badly self-intersecting ring can degrade into a
        # GeometryCollection mixing polygons with stray lines/points, which
        # union() can't combine with plain Polygons - keep only the
        # polygonal parts.
        if poly.geom_type == "GeometryCollection":
            polys.extend(g for g in poly.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty)
        elif poly.geom_type in ("Polygon", "MultiPolygon"):
            polys.append(poly)
    if not polys:
        return None
    return reduce(lambda a, b: a.union(b), polys)


def _offset_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Moves a point `distance_m` meters along compass `bearing_deg` (0=N, 90=E)."""
    bearing_rad = math.radians(bearing_deg)
    north_m = distance_m * math.cos(bearing_rad)
    east_m = distance_m * math.sin(bearing_rad)
    lat2 = lat + north_m / METERS_PER_DEGREE_LAT
    lon2 = lon + east_m / (METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return lat2, lon2


def fetch_slope(lat: float, lon: float, spread_bearing_deg: float) -> dict:
    """
    Rough slope in the direction the fire would spread (downwind), sampled
    via two elevation points ~300m apart - Open-Elevation, free, no key.
    Positive = upslope in the spread direction (fire runs faster uphill).
    """
    sample_distance_m = 300.0
    lat2, lon2 = _offset_point(lat, lon, spread_bearing_deg, sample_distance_m)
    response = httpx.get(
        OPEN_ELEVATION_URL,
        params={"locations": f"{lat},{lon}|{lat2},{lon2}"},
        timeout=8.0,
    )
    response.raise_for_status()
    results = response.json()["results"]
    elevation_origin = results[0]["elevation"]
    elevation_downwind = results[1]["elevation"]
    rise = elevation_downwind - elevation_origin
    slope_degrees = math.degrees(math.atan2(rise, sample_distance_m))
    return {
        "slope_degrees": slope_degrees,
        "elevation_origin_m": elevation_origin,
        "elevation_downwind_m": elevation_downwind,
    }


def _wind_speed_factor(wind_kmh: float) -> float:
    """
    Canadian Forest Fire Weather Index (FWI) System's own wind function for
    ISI (Initial Spread Index) - Van Wagner 1987: fW = exp(0.05039 * W).
    Reused here as a spread-rate multiplier, not to compute an actual ISI.
    """
    return math.exp(0.05039 * wind_kmh)


def _slope_factor(slope_degrees: float) -> float:
    """
    Simplified upslope acceleration - literature shows roughly +15-20% ROS
    per 10 degrees of upslope terrain. Downslope (negative) is left
    unmultiplied (assumed roughly neutral for this POC) rather than modeled
    as a slowdown, since that effect is less consistently documented.
    """
    if slope_degrees <= 0:
        return 1.0
    return 1.0 + 0.035 * min(slope_degrees, 40.0)


def _length_to_breadth_ratio(wind_kmh: float) -> float:
    """Van Wagner (1969) / Alexander (1985): LB = 1.0 + 0.0012 * W^2.155."""
    return 1.0 + 0.0012 * (wind_kmh**2.155)


def _build_ellipse_from_distances(
    origin_lat: float,
    origin_lon: float,
    bearing_deg: float,
    head_dist_m: float,
    back_dist_m: float,
    flank_dist_m: float,
    water_shape: BaseGeometry | None = None,
    burnt_shape: BaseGeometry | None = None,
    num_points: int = 72,
) -> list[list[float]]:
    """
    Builds the fire perimeter ellipse from already-cumulative head/back/flank
    distances (meters) - the ignition point sits at a FOCUS of the ellipse
    (not the center), because the backing fire spreads slower than the
    heading fire. Points returned as [lat, lon].

    When `water_shape` and/or `burnt_shape` are given (precomputed shapely
    geometries - see _water_shape/predict_incident_spread, each built ONCE
    per prediction, not rebuilt per hour), the raw ellipse is clipped
    against them via proper polygon difference, not per-vertex radial
    clipping back toward the origin - the earlier per-vertex approach
    created spiky sliver artifacts wherever a ray from the origin grazed a
    thin arm of water (e.g. a river mouth) close to the origin before
    returning to land further out, since each vertex was pulled back
    independently with no awareness of its neighbors or the clipping
    shape's actual boundary. A real polygon difference follows the true
    shoreline/burnt-perimeter contour instead - and, importantly, still
    lets head_dist_m/back_dist_m/flank_dist_m themselves keep accumulating
    normally hour over hour (representing the fire's real cumulative push),
    with only the DRAWN shape trimmed back to whatever's actually new
    ground. An earlier version tried to freeze the distance accumulators
    themselves whenever an edge's offset point landed in burnt_shape - that
    breaks the moment an origin sits anywhere close enough to the burnt
    extent to be picked as a front at all: the same hourly increment gets
    added then immediately subtracted back off every single hour, pinning
    that edge at 0m forever instead of ever propagating outward.
    """
    major_semi_m = (head_dist_m + back_dist_m) / 2
    minor_semi_m = flank_dist_m
    center_offset_m = (head_dist_m - back_dist_m) / 2
    bearing_rad = math.radians(bearing_deg)

    raw_points_lonlat = []  # shapely uses (x, y) = (lon, lat)
    for i in range(num_points):
        angle = 2 * math.pi * i / num_points
        # Local frame: x = downwind axis (spread direction), y = perpendicular flank axis.
        x = center_offset_m + major_semi_m * math.cos(angle)
        y = minor_semi_m * math.sin(angle)

        east_m = x * math.sin(bearing_rad) + y * math.cos(bearing_rad)
        north_m = x * math.cos(bearing_rad) - y * math.sin(bearing_rad)

        lat = origin_lat + north_m / METERS_PER_DEGREE_LAT
        lon = origin_lon + east_m / (METERS_PER_DEGREE_LAT * math.cos(math.radians(origin_lat)))
        raw_points_lonlat.append((lon, lat))

    if water_shape is None and burnt_shape is None:
        points = [[lat, lon] for lon, lat in raw_points_lonlat]
        points.append(points[0])  # close the ring
        return points

    ellipse_shape = Polygon(raw_points_lonlat)
    if not ellipse_shape.is_valid:
        ellipse_shape = ellipse_shape.buffer(0)
    clipped = ellipse_shape
    if water_shape is not None:
        clipped = clipped.difference(water_shape)
    if burnt_shape is not None:
        clipped = clipped.difference(burnt_shape)

    if clipped.is_empty:
        # Degenerate edge case (e.g. origin itself sits in water/burnt
        # ground) - fall back to the unclipped shape rather than returning
        # nothing to draw.
        points = [[lat, lon] for lon, lat in raw_points_lonlat]
        points.append(points[0])
        return points

    if clipped.geom_type == "MultiPolygon":
        # Water split the ellipse into separate pieces - keep the one still
        # containing the origin (the actual fire perimeter), or the largest
        # piece as a fallback if none technically contains it (edge rounding).
        origin_point = Point(origin_lon, origin_lat)
        containing = [g for g in clipped.geoms if g.contains(origin_point)]
        chosen = containing[0] if containing else max(clipped.geoms, key=lambda g: g.area)
    else:
        chosen = clipped

    # Holes (a water body fully enclosed within the ellipse, not touching its
    # edge) are dropped - only the exterior boundary is returned. Handling
    # holes would mean passing multi-ring polygons through to the frontend's
    # rendering, which only draws a single ring today; an acceptable
    # simplification since the reported issue is edge-crossing water, not
    # enclosed lakes.
    return [[lat, lon] for lon, lat in chosen.exterior.coords]


def _hourly_ros(wind_kmh: float, base_ros: float, slope_factor: float) -> dict:
    """One hour's head/flank/back rate of spread (m/min) from that hour's own wind."""
    ros_head = base_ros * _wind_speed_factor(wind_kmh) * slope_factor
    ros_head = min(ros_head, 100.0)  # sanity cap (~6 km/h - already extreme crown-fire territory)

    lb_ratio = _length_to_breadth_ratio(wind_kmh)
    eccentricity = math.sqrt(max(lb_ratio**2 - 1, 0)) / lb_ratio if lb_ratio > 0 else 0
    ros_back = ros_head * (1 - eccentricity) / (1 + eccentricity) if (1 + eccentricity) else 0
    ros_flank = (ros_head + ros_back) / (2 * lb_ratio) if lb_ratio else 0
    return {"head": ros_head, "flank": ros_flank, "back": ros_back, "lb_ratio": lb_ratio}


def predict_spread(lat: float, lon: float, max_hours: int = MAX_PREDICTION_HOURS, burnt_shape: BaseGeometry | None = None) -> dict:
    wind_series = fetch_wind_series(lat, lon, max_hours)
    if not wind_series:
        raise RuntimeError("No wind forecast available for this location")

    fuel = fetch_fuel_type(lat, lon)
    base_ros = fuel["base_ros_m_per_min"]

    # Slope is sampled once, in the first forecast hour's downwind direction -
    # resampling it fresh each hour (as the fire front actually advances)
    # would need many more elevation calls for a POC-level improvement.
    first_bearing = (wind_series[0]["direction_from_deg"] + 180) % 360
    try:
        slope = fetch_slope(lat, lon, first_bearing)
    except Exception:
        # Open-Elevation's free public instance is a known-flaky dependency
        # (frequent 504s / hangs) - slope is a secondary multiplier here, not
        # core to the prediction, so fall back to flat terrain rather than
        # failing the whole request over a single struggling third-party API.
        logger.warning("Slope lookup failed for (%s, %s), assuming flat terrain", lat, lon, exc_info=True)
        slope = {"slope_degrees": 0.0, "elevation_origin_m": None, "elevation_downwind_m": None, "unavailable": True}
    slope_factor = _slope_factor(slope["slope_degrees"])

    # Fetched/built once (not per hour/vertex) - see fetch_water_rings and
    # _build_ellipse_from_distances for how every hour's ellipse gets clipped
    # against this locally, with no further network calls.
    water_rings = fetch_water_rings(lat, lon)
    water_shape = _water_shape(water_rings) if water_rings else None

    cum_head_m = 0.0
    cum_back_m = 0.0
    cum_flank_m = 0.0
    # ROS-weighted vector sum of each hour's spread bearing, so the overall
    # direction can drift over the 24h window if the forecast wind shifts,
    # instead of being frozen at hour 1's direction for the whole period.
    bearing_east = 0.0
    bearing_north = 0.0

    hourly = []
    for i, entry in enumerate(wind_series):
        wind_kmh = entry["speed_kmh"]
        ros = _hourly_ros(wind_kmh, base_ros, slope_factor)

        # Distances accumulate unconditionally here - neither water nor this
        # incident's own already-burnt extent stops the fire's "would-be"
        # progress, they just clip where the drawn perimeter ends up
        # (_build_ellipse_from_distances below, via burnt_shape). That keeps
        # the clipping logic in one place instead of special-casing any one
        # direction's accumulator - an earlier version tried freezing
        # cum_back_m/cum_head_m/cum_flank_m directly whenever their offset
        # point landed in burnt_shape, which breaks the moment an origin
        # sits anywhere near the burnt extent (as a front origin always
        # does, by construction - see predict_incident_spread): the same
        # hourly increment gets added then immediately subtracted back off
        # every hour, pinning that edge at 0m forever.
        cum_head_m += ros["head"] * 60
        cum_back_m += ros["back"] * 60
        cum_flank_m += ros["flank"] * 60

        bearing_deg = (entry["direction_from_deg"] + 180) % 360
        bearing_rad = math.radians(bearing_deg)
        bearing_east += math.sin(bearing_rad) * ros["head"]
        bearing_north += math.cos(bearing_rad) * ros["head"]
        avg_bearing_deg = math.degrees(math.atan2(bearing_east, bearing_north)) % 360

        polygon = _build_ellipse_from_distances(
            lat, lon, avg_bearing_deg, cum_head_m, cum_back_m, cum_flank_m, water_shape, burnt_shape
        )
        leading_lat, leading_lon = _offset_point(lat, lon, avg_bearing_deg, cum_head_m)
        head_blocked_by_water = water_shape is not None and water_shape.contains(Point(leading_lon, leading_lat))
        hourly.append(
            {
                "hour": i + 1,
                "time": entry["time"],
                "wind_speed_kmh": wind_kmh,
                "wind_direction_from_deg": entry["direction_from_deg"],
                "temperature_c": entry["temperature_c"],
                "humidity_pct": entry["humidity_pct"],
                "rate_of_spread_m_per_min": {
                    "head": round(ros["head"], 2),
                    "flank": round(ros["flank"], 2),
                    "back": round(ros["back"], 2),
                },
                "cumulative_head_m": round(cum_head_m, 1),
                "bearing_deg": round(avg_bearing_deg, 1),
                "head_blocked_by_water": head_blocked_by_water,
                "polygon": polygon,
            }
        )

    return {
        "origin": {"latitude": lat, "longitude": lon},
        "fuel": fuel,
        "slope": slope,
        "hourly": hourly,
        "disclaimer": (
            "Prueba de concepto experimental - un modelo elíptico de crecimiento simplificado "
            "(Van Wagner 1969) impulsado por la previsión horaria real de viento (Open-Meteo), "
            "no un modelo de combustible Rothermel calibrado ni una herramienta operativa de "
            "comportamiento del fuego. No usar para planificar evacuaciones o extinción."
        ),
    }


def _cluster_recent_points(
    points: list[tuple[float, float]], max_clusters: int = MAX_ACTIVE_FRONTS
) -> list[tuple[float, float]]:
    """
    Greedy proximity clustering of a fire's most recent detections into
    distinct active fronts - a fire spreading in more than one direction at
    once (e.g. around an obstacle, or two separate flanks) has more than one
    leading edge, and a single incident-wide centroid averages them into an
    origin that may sit on ground that was never actually burning. Returns
    each cluster's own centroid, largest cluster first, capped at
    max_clusters so a noisy/fragmented detection set can't explode into
    dozens of near-duplicate predictions.
    """
    remaining = list(points)
    clusters: list[list[tuple[float, float]]] = []
    while remaining:
        seed = remaining.pop()
        cluster = [seed]
        still_remaining = []
        for point in remaining:
            if math.hypot(point[0] - seed[0], point[1] - seed[1]) <= FRONT_CLUSTER_DEG:
                cluster.append(point)
            else:
                still_remaining.append(point)
        clusters.append(cluster)
        remaining = still_remaining

    clusters.sort(key=len, reverse=True)
    centroids = []
    for cluster in clusters[:max_clusters]:
        lat = sum(p[0] for p in cluster) / len(cluster)
        lon = sum(p[1] for p in cluster) / len(cluster)
        centroids.append((lat, lon))
    return centroids


def _is_exterior_point(lat: float, lon: float, burnt_shape: BaseGeometry | None) -> bool:
    """
    True if (lat, lon) plausibly sits on the fire's actual leading edge -
    either already outside its known burnt extent entirely, or close enough
    to the boundary to effectively be part of it - as opposed to a
    re-detection deep in the fire's already-consumed interior. A large,
    long-running fire's "most recent" detections are NOT the same thing as
    its "leading edge" ones: smoldering/re-igniting spots well inside an
    already-burnt core keep generating fresh satellite hits for weeks
    (confirmed live: a 6h recent-detection window for one such incident
    covered ~its entire multi-week footprint, interior included) - picking a
    front origin from those would start the ellipse deep inside burnt
    ground instead of at the fire's real edge.
    """
    if burnt_shape is None:
        return True
    point = Point(lon, lat)
    if not burnt_shape.contains(point):
        return True  # already beyond the known burnt extent - unambiguously a leading-edge point
    return burnt_shape.boundary.distance(point) <= FRONT_BOUNDARY_BUFFER_DEG


def predict_incident_spread(
    burnt_extent_points: list[tuple[float, float]],
    recent_front_points: list[tuple[float, float]],
    max_hours: int = MAX_PREDICTION_HOURS,
) -> dict:
    """
    Multi-front, burnt-aware counterpart to predict_spread, built for an
    actual tracked incident rather than an arbitrary clicked point:

    - Traces the incident's already-burnt extent from ALL of its detections
      (same concave-hull algorithm as area_estimate.estimate_area_and_hull)
      and hands it to predict_spread as burnt_shape, so spread into ground
      with no fuel left gets suppressed on every edge (see predict_spread).
    - Narrows its MOST RECENT detections (see RECENT_HOTSPOT_WINDOW_HOURS)
      down to just the ones near/beyond that burnt extent's own boundary
      (see _is_exterior_point) before clustering into up to
      MAX_ACTIVE_FRONTS distinct active fronts - a fire's overall centroid,
      OR a centroid of merely-recent-but-still-interior detections, may be
      nowhere near where it's actually still advancing.
    """
    _area_ha, hull_latlon = estimate_area_and_hull(burnt_extent_points)
    burnt_shape = None
    if hull_latlon:
        burnt_shape = Polygon([(lon, lat) for lat, lon in hull_latlon]).buffer(0)

    exterior_recent_points = [
        (lat, lon) for lat, lon in recent_front_points if _is_exterior_point(lat, lon, burnt_shape)
    ]
    # Falls back to every recent point (interior included) if literally none
    # of them are near the edge - a worse-than-ideal origin still beats
    # refusing to predict at all for a fire that's nominally still active.
    front_origins = _cluster_recent_points(exterior_recent_points or recent_front_points)
    if not front_origins:
        raise RuntimeError("No recent detections to predict from")

    return {
        "fronts": [
            predict_spread(front_lat, front_lon, max_hours=max_hours, burnt_shape=burnt_shape)
            for front_lat, front_lon in front_origins
        ],
        "burnt_area_hull": hull_latlon,
    }
