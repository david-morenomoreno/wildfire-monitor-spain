from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Webcam
from app.schemas import WebcamOut
from app.services.webcams.registry import WEBCAM_SOURCES
from app.services.webcams.sync import sync_source
from app.services.webcams.windy import fetch_windy_webcams

router = APIRouter(prefix="/api/webcams", tags=["webcams"])


@router.get("", response_model=list[WebcamOut])
def list_webcams(
    bbox: Optional[str] = Query(
        None, description="minLon,minLat,maxLon,maxLat - only cameras within these map bounds"
    ),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    query = db.query(Webcam)
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'")
        query = query.filter(
            Webcam.latitude >= min_lat,
            Webcam.latitude <= max_lat,
            Webcam.longitude >= min_lon,
            Webcam.longitude <= max_lon,
        )
    return query.limit(limit).all()


@router.get("/windy", response_model=list[WebcamOut])
def list_windy_webcams(
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat - fetched live, not from our DB"),
    limit: int = Query(50, ge=1, le=50),  # Windy's own API rejects limit > 50
):
    """
    Live per-viewport Windy webcams - deliberately NOT backed by the
    `webcams` table like /api/webcams (DGT) is. See services/webcams/windy.py
    for why: Windy's own image URLs expire (10min free tier / 24h Pro), so
    caching them would just serve broken images later. The frontend calls
    this alongside /api/webcams on every pan/zoom while the cameras toggle is
    on, same as it already does for DGT.
    """
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'")
    try:
        return fetch_windy_webcams(min_lon, min_lat, max_lon, max_lat, limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Windy webcams fetch failed: {exc}") from exc


@router.get("/nearby", response_model=list[WebcamOut])
def nearby_webcams(
    lat: float,
    lon: float,
    limit: int = Query(6, ge=1, le=50),
    exclude_id: Optional[int] = Query(None, description="Omit this camera (e.g. the one already being viewed)"),
    db: Session = Depends(get_db),
):
    """
    Windy-style "nearby webcams" strip: nearest N cameras to a point,
    sorted by plain degree-distance (fine at this scale - a few km - same
    approximation the rest of this app's proximity logic already uses).
    """
    candidates = db.query(Webcam).all()
    if exclude_id is not None:
        candidates = [c for c in candidates if c.id != exclude_id]
    candidates.sort(key=lambda w: (w.latitude - lat) ** 2 + (w.longitude - lon) ** 2)
    return candidates[:limit]


@router.post("/{source_key}/refresh")
def refresh_source(source_key: str, db: Session = Depends(get_db)):
    if source_key not in WEBCAM_SOURCES:
        raise HTTPException(status_code=404, detail="Unknown webcam source")
    try:
        count = sync_source(db, source_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Webcam sync failed: {exc}") from exc
    return {"source": source_key, "count": count}
