"""
Lazy, disk-cached low-res preview rendering for a CopernicusEmsProduct's
Cloud-Optimized GeoTIFF - the same pattern as
services/copernicus.py's get_or_render_thumbnail, but reading straight from
the product's own `cog_url` (no OAuth token, no Process API quota) instead
of calling out to the Sentinel Hub Process API.

Confirmed live (2026-07-23) against a real EMSR898 product COG:
  - It genuinely is a Cloud-Optimized GeoTIFF with internal overviews
    (confirmed via `rasterio`'s `.overviews()`), not just a plain GeoTIFF
    given a ".tif" name - so GDAL's /vsicurl/ virtual filesystem can read a
    512x512 preview via HTTP range requests in ~1-2s, transferring only a
    few hundred KB, WITHOUT downloading the full ~40MB file.
  - `rasterio`'s manylinux wheel bundles its own GDAL build, so this doesn't
    need a system libgdal install - just `pip install rasterio`.
"""

import logging
import os
from io import BytesIO

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CopernicusEmsProduct

logger = logging.getLogger(__name__)


def render_product_preview(cog_url: str, size: int) -> bytes:
    """Reads a downsampled (size x size) true-color JPEG preview straight
    off a COG's own internal overviews via GDAL's /vsicurl/, without
    downloading the full-resolution file."""
    vsicurl_url = f"/vsicurl/{cog_url}"
    with rasterio.open(vsicurl_url) as src:
        band_count = min(src.count, 3)  # RGB (or single-band) only - drop any extra bands (e.g. alpha)
        data = src.read(
            indexes=list(range(1, band_count + 1)),
            out_shape=(band_count, size, size),
            resampling=Resampling.average,
        )
    array = np.transpose(data, (1, 2, 0))
    if band_count == 1:
        array = np.repeat(array, 3, axis=2)
    image = Image.fromarray(array)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def get_or_render_product_thumbnail(db: Session, product: CopernicusEmsProduct) -> bytes:
    """Serves a cached preview from disk if already rendered; otherwise
    renders it now (a real, if slow, HTTP fetch against the COG) and caches
    it to disk before returning."""
    if product.thumbnail_path:
        path = os.path.join(settings.upload_dir, product.thumbnail_path)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()

    if not product.cog_url:
        raise ValueError("Product has no cog_url to render a preview from")

    image_bytes = render_product_preview(product.cog_url, settings.copernicus_ems_thumbnail_size)

    os.makedirs(settings.upload_dir, exist_ok=True)
    filename = f"ems-product-{product.id}.jpg"
    with open(os.path.join(settings.upload_dir, filename), "wb") as f:
        f.write(image_bytes)
    product.thumbnail_path = filename
    db.commit()
    return image_bytes
