"""
EUMETSAT MTG/FCI Active Fire Monitoring - geostationary fire-pixel ingestion.

Complements FIRMS/EFFIS (polar-orbiting VIIRS/MODIS, a handful of passes/day)
with continuous ~10-min full-disk coverage from Meteosat Third Generation's
Flexible Combined Imager (FCI). Confirmed LIVE against the real API
(2026-07-18):
  - Auth: POST https://api.eumetsat.int/token, HTTP Basic
    base64(consumer_key:consumer_secret), body grant_type=client_credentials
    -> {"access_token": ..., "expires_in": ...}. Search does NOT need a token
    (browsing/searching the Data Store is open); only downloading a product
    does.
  - Collection EO:EUM:DAT:0682 ("Active Fire Monitoring (netCDF) - MTG - 0
    degree") returns real per-cycle products via the search API - MSG's
    older equivalent (EO:EUM:DAT:MSG:FIRC / FRP-SEVIRI, the ID shown on
    EUMETSAT's own product pages) returns "Collection not found" against the
    Data Store's actual collections list, i.e. it's no longer distributed
    there - MTG has operationally superseded it.
  - Each product is a small (~1KB) zip containing one netCDF-4 file with
    that cycle's detected fire pixels (there are usually few or none in any
    single 10-min slice over Spain).

CONFIRMED against a real downloaded product (2026-07-19): there is NO flat
lat/lon pixel list. The product is a geostationary fixed-grid raster -
variables found: ['mtg_geos_projection', 'x', 'y', 'fire_result',
'fire_probability', 'product_quality', 'product_completeness',
'product_timeliness']. 'x'/'y' are the 1-D fixed-grid scan-angle coordinate
vectors (CF "geostationary" convention), 'fire_result'/'fire_probability'
are 2-D (y, x) rasters, and 'mtg_geos_projection' is a CF grid_mapping
variable carrying the projection parameters (semi_major_axis,
perspective_point_height, longitude_of_projection_origin, etc.) needed to
turn a grid cell's (x, y) into real lat/lon - see _pixel_latlon.

fire_result has no flag_meanings/flag_values CF attributes in this product
(_fire_flag_values still checks first, in case a future revision adds them),
so it falls back to settings.eumetsat_fire_result_fallback_values. That
fallback's meaning (0/1/2/3 = no fire/low/mid/high confidence fire) is
CONFIRMED against EUMETSAT's own "MTG-FCI: ATBD for Active Fire Monitoring
Product" (EUM/MTG/DOC/10/0613 v2A, Table 4) - NOT a guess. The live value
histogram also showed a 5th code, 4, which isn't in that table at all - it's
overwhelmingly the most common value (~91% of a full-disk grid) and almost
certainly covers pixels the FIR algorithm doesn't process at all (sea/mixed
water, bare soil, sun-glint, or beyond the ~70 deg satellite-zenith
processing radius - see the ATBD's section 3.5 prerequisites), not a fire
category - hence it's deliberately excluded from the fallback set.

KNOWN LIMITATION even with correct parsing (confirmed 2026-07-19, checking 4
hours of real cycles against the active Guadalajara/Tamajón fire that FIRMS
was already reporting at ~8,000+ ha): this product did not flag it in any
cycle checked. Per the same ATBD, plausible reasons are NOT ingestion bugs:
smoke/cloud over the fire failing the VIS-reflectance/sun-glint prerequisite
checks (section 3.5), this collection being the coarser 2km FDHSI variant
rather than the 1km one the ATBD names as future work for smaller fires, and
the ATBD's own admission that its threshold coefficients (Table 3) were
tuned for MSG-SEVIRI and still needed "fine tuning during the commissioning
phase of MTG-FCI" - i.e. this specific instrument's real-world sensitivity
may still not match FIRMS/EFFIS for a while. Treat EUMETSAT as a
supplementary near-real-time layer, not a replacement for FIRMS/EFFIS.
"""

import io
import logging
import zipfile
from datetime import datetime, timedelta

import numpy as np
import pyproj
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import FireDetection
from app.services.eumetsat_client import download_product, is_configured, search_products
from app.services.geo_filter import is_in_spain, is_over_water
from app.services.health import record_check

logger = logging.getLogger(__name__)

_FIRE_RESULT_CANDIDATES = ["fire_result"]
_FIRE_PROBABILITY_CANDIDATES = ["fire_probability"]
# Sanity ceiling - see the guard in _parse_netcdf_bytes.
_MAX_FIRE_PIXELS_PER_PRODUCT = 5000

_logged_variable_names = False  # module-level: only log the diagnostic dump once per process
_logged_fire_result_values = False  # ditto, for the fire_result value-histogram fallback path


def _find_var(variable_names: list[str], candidates: list[str]) -> str | None:
    lowered = {name.lower(): name for name in variable_names}
    for candidate in candidates:
        for lower_name, real_name in lowered.items():
            if candidate in lower_name:
                return real_name
    return None


def _fire_flag_values(fire_result_var) -> set[int] | None:
    """
    The CF-correct way to know which fire_result codes mean "fire": read its
    own flag_meanings/flag_values attributes (space-separated names / matching
    array of codes) and keep whichever names look fire-related. Returns None
    (not "no fires found") when the variable doesn't carry these attributes,
    so the caller can fall back to the configured guess instead of silently
    treating "no metadata" as "no fires".
    """
    meanings = getattr(fire_result_var, "flag_meanings", None)
    values = getattr(fire_result_var, "flag_values", None)
    if not meanings or values is None:
        return None
    names = str(meanings).split()
    codes = [int(v) for v in np.atleast_1d(values)]
    if len(names) != len(codes):
        return None
    return {
        code
        for name, code in zip(names, codes)
        if "fire" in name.lower() and "no_fire" not in name.lower() and "not_fire" not in name.lower()
    }


def _pixel_latlon(ds, x_idx: np.ndarray, y_idx: np.ndarray, grid_mapping_var_name: str) -> tuple[np.ndarray, np.ndarray]:
    """
    Converts fixed-grid (x, y) index pairs into (lat, lon) using the CF
    grid_mapping variable's projection parameters - see the module docstring.
    """
    grid_var = ds.variables[grid_mapping_var_name]
    grid_attrs = {attr: grid_var.getncattr(attr) for attr in grid_var.ncattrs()}
    crs = pyproj.CRS.from_cf(grid_attrs)

    x = np.asarray(ds.variables["x"][:], dtype="float64")
    y = np.asarray(ds.variables["y"][:], dtype="float64")
    x_units = str(getattr(ds.variables["x"], "units", ""))
    # CONFIRMED live (2026-07-19): this product ships x/y with NO units
    # attribute at all (empty string) even though the values (~0.15) are
    # plainly scan-angle radians, not meters - trusting a missing units
    # string made every point collapse to ~(0, 0) (the projection origin).
    # Real planar geostationary coordinates are on the order of the satellite
    # height/Earth radius (millions of meters); radians for a full Earth disk
    # view are always well under 1 - so detect by magnitude instead of
    # relying on a units string that may not be there.
    looks_like_radians = "rad" in x_units.lower() or (np.abs(x).max() < 10 and np.abs(y).max() < 10)
    if looks_like_radians:
        # CF fixed-grid convention: x/y are scan angles in radians: multiply
        # by the satellite's distance from the projection's focal point to
        # get the projection's native planar coordinates (meters).
        height = float(grid_attrs["perspective_point_height"]) + float(grid_attrs.get("semi_major_axis", 6378137.0))
        x = x * height
        y = y * height

    transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = transformer.transform(x[x_idx], y[y_idx])
    return np.asarray(lats), np.asarray(lons)


def _parse_netcdf_bytes(nc_bytes: bytes) -> list[dict]:
    global _logged_variable_names, _logged_fire_result_values
    # Imported lazily: this is a heavy optional dependency only needed when
    # EUMETSAT ingestion is actually configured and running.
    import netCDF4

    pixels: list[dict] = []
    with netCDF4.Dataset("inmemory.nc", memory=nc_bytes) as ds:
        variable_names = list(ds.variables.keys())
        if not _logged_variable_names:
            logger.info("EUMETSAT netCDF variables found in a real product: %s", variable_names)
            _logged_variable_names = True

        fire_result_name = _find_var(variable_names, _FIRE_RESULT_CANDIDATES)
        if not fire_result_name or "x" not in variable_names or "y" not in variable_names:
            raise RuntimeError(
                f"Could not find the expected fire_result/x/y fixed-grid variables in EUMETSAT "
                f"product (available: {variable_names}) - update _FIRE_RESULT_CANDIDATES in "
                f"eumetsat.py to match"
            )
        fire_result_var = ds.variables[fire_result_name]
        # netCDF4 returns a MaskedArray when the variable has a _FillValue -
        # fill masked (no-data) cells with a code no real product uses so they
        # can never accidentally match a real fire flag value below.
        fire_result = np.ma.filled(fire_result_var[:], -9999)

        fire_codes = _fire_flag_values(fire_result_var)
        if fire_codes is not None:
            fire_mask = np.isin(fire_result, list(fire_codes))
        else:
            if not _logged_fire_result_values:
                unique, counts = np.unique(fire_result, return_counts=True)
                logger.info(
                    "EUMETSAT fire_result has no flag_meanings/flag_values metadata - falling back to "
                    "settings.eumetsat_fire_result_fallback_values. Unique values seen in a real product: %s",
                    dict(zip(unique.tolist(), counts.tolist())),
                )
                _logged_fire_result_values = True
            fallback_codes = {int(v) for v in settings.eumetsat_fire_result_fallback_values.split(",") if v.strip()}
            fire_mask = np.isin(fire_result, list(fallback_codes))

        y_idx, x_idx = np.nonzero(fire_mask)
        if len(y_idx) == 0:
            return pixels
        if len(y_idx) > _MAX_FIRE_PIXELS_PER_PRODUCT:
            # A real fire-detection product covering Spain is "usually few or
            # none" (see module docstring) - tens of thousands of matches
            # means the fire_result code set above is wrong (matching a
            # background/no-data category, e.g. the ~28M "outside disk"
            # pixels seen live under the wrong fallback guess), not a real
            # continent-sized fire. Bail instead of geolocating+inserting
            # millions of bogus rows / hanging the request for minutes.
            logger.error(
                "EUMETSAT fire_result mask matched %d pixels (> %d) - treating as a wrong fire-code "
                "guess, not real fires. Check the fire_result value histogram logged above and fix "
                "eumetsat_fire_result_fallback_values.",
                len(y_idx),
                _MAX_FIRE_PIXELS_PER_PRODUCT,
            )
            return pixels

        grid_mapping_name = getattr(fire_result_var, "grid_mapping", None) or "mtg_geos_projection"
        if grid_mapping_name not in variable_names:
            raise RuntimeError(
                f"Could not find the '{grid_mapping_name}' grid_mapping variable in EUMETSAT product "
                f"(available: {variable_names})"
            )
        lats, lons = _pixel_latlon(ds, x_idx, y_idx, grid_mapping_name)

        probability_name = _find_var(variable_names, _FIRE_PROBABILITY_CANDIDATES)
        probabilities = (
            np.ma.filled(ds.variables[probability_name][:], np.nan)[y_idx, x_idx] if probability_name else None
        )
        fire_codes_at_pixels = fire_result[y_idx, x_idx]

        for i in range(len(y_idx)):
            pixel = {"latitude": float(lats[i]), "longitude": float(lons[i]), "confidence": str(int(fire_codes_at_pixels[i]))}
            if probabilities is not None:
                pixel["fire_probability"] = float(probabilities[i])
            pixels.append(pixel)
    return pixels


def _parse_fire_pixels(zip_bytes: bytes) -> list[dict]:
    pixels: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        nc_names = [name for name in zf.namelist() if name.lower().endswith(".nc")]
        for name in nc_names:
            with zf.open(name) as f:
                pixels.extend(_parse_netcdf_bytes(f.read()))
    return pixels


def ingest_eumetsat(db: Session) -> int:
    """
    Searches for new MTG Active Fire Monitoring products since the last
    lookback window, downloads+parses each, and upserts fire pixels over
    Spain. Skipped (not an error) when credentials aren't configured yet.
    """
    state.mark_attempt("eumetsat")
    try:
        count = _ingest_eumetsat(db)
    except Exception as exc:
        record_check(db, "eumetsat", "disrupted", str(exc))
        raise
    record_check(db, "eumetsat", "ok", f"{count} fire pixels processed")
    return count


def _ingest_eumetsat(db: Session) -> int:
    if not is_configured():
        record_check(db, "eumetsat", "skipped", "consumer_key/consumer_secret not configured")
        return 0

    end = datetime.utcnow()
    start = end - timedelta(minutes=settings.eumetsat_lookback_minutes)
    features = search_products(settings.eumetsat_collection_id, start, end)

    count = 0
    skipped_outside_spain = 0
    skipped_over_water = 0
    for feature in features:
        product_id = feature.get("id") or feature.get("properties", {}).get("identifier") or "unknown"
        date_range = (feature.get("properties", {}).get("date") or "").split("/")
        try:
            captured_at = datetime.strptime(date_range[0], "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, IndexError):
            captured_at = end

        try:
            zip_bytes = download_product(feature)
            pixels = _parse_fire_pixels(zip_bytes)
        except Exception:
            logger.exception("Failed to download/parse EUMETSAT product %s", product_id)
            continue

        for pixel in pixels:
            latitude = pixel["latitude"]
            longitude = pixel["longitude"]
            if not is_in_spain(latitude, longitude):
                skipped_outside_spain += 1
                continue

            # Geostationary fire products are especially prone to over-water
            # false positives (sun glint, ships, offshore platforms) given
            # their coarser pixel size vs. VIIRS/MODIS - see geo_filter.py's
            # is_over_water docstring.
            if is_over_water(latitude, longitude):
                skipped_over_water += 1
                continue

            external_id = f"{product_id}-{latitude:.4f}-{longitude:.4f}"
            stmt = (
                insert(FireDetection)
                .values(
                    source="EUMETSAT",
                    external_id=external_id,
                    latitude=latitude,
                    longitude=longitude,
                    confidence=pixel.get("confidence"),
                    brightness=None,  # fire_probability (0-1) isn't a brightness temperature - see pixel["fire_probability"] in raw_properties instead
                    acquired_at=captured_at,
                    raw_properties=str(pixel),
                )
                .on_conflict_do_nothing(constraint="uq_source_external_id")
            )
            db.execute(stmt)
            count += 1

    if skipped_outside_spain:
        logger.info(
            "Skipped %d EUMETSAT fire pixel(s) outside Spain (full-disk product covers Europe/Africa/Atlantic)",
            skipped_outside_spain,
        )
    if skipped_over_water:
        logger.info("Skipped %d EUMETSAT fire pixel(s) landing inside a real water body (likely sensor false positive)", skipped_over_water)
    db.commit()
    return count
