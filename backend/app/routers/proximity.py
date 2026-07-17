from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.proximity import check_proximity

router = APIRouter(prefix="/api/proximity", tags=["proximity"])


@router.get("/check")
def check(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    db: Session = Depends(get_db),
):
    """
    Experimental: does any nearby ACTIVE incident's predicted 24h growth
    footprint (see services/fire_spread.py) reach this point? Used by the
    frontend's opt-in location-alert feature, polled periodically while
    enabled. See services/proximity.py for the model and its POC-level
    caveats - same disclaimers as the manual fire-spread tool.
    """
    try:
        return check_proximity(db, lat, lon)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proximity check failed: {exc}") from exc
