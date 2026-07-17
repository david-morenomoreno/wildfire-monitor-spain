from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.geocode import reverse_geocode

router = APIRouter(prefix="/api/geocode", tags=["geocode"])


@router.get("")
def geocode(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    db: Session = Depends(get_db),
):
    try:
        return reverse_geocode(db, lat, lon)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Reverse geocoding failed: {exc}") from exc
