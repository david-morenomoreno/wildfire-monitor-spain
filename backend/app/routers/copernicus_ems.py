from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CopernicusEmsProduct
from app.services.copernicus_ems_imagery import get_or_render_product_thumbnail

router = APIRouter(prefix="/api/copernicus-ems", tags=["copernicus-ems"])


@router.get("/products/{product_id}/thumbnail")
def product_thumbnail(product_id: int, db: Session = Depends(get_db)):
    """
    Serves a low-res true-color preview of one CopernicusEmsProduct's AOI,
    rendered on first request straight from the product's own COG (via GDAL
    /vsicurl/ - no OAuth, no external processing quota) and cached to disk
    after that - see services/copernicus_ems_imagery.py.
    """
    product = db.query(CopernicusEmsProduct).filter_by(id=product_id).first()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if not product.cog_url:
        raise HTTPException(status_code=404, detail="Product has no renderable imagery")
    try:
        image_bytes = get_or_render_product_thumbnail(db, product)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thumbnail render failed: {exc}") from exc
    return Response(content=image_bytes, media_type="image/jpeg")
