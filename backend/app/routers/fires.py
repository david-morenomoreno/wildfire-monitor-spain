from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.database import get_db
from app.models import FireDetection
from app.schemas import FireDetectionOut
from app.services.effis import ingest_effis
from app.services.firms import ingest_firms

router = APIRouter(prefix="/api/fires", tags=["fires"])


def _cooldown_response(source: str, key: str) -> Optional[dict]:
    """
    Manual refreshes share the same cooldown as the scheduler so clicking
    "Refresh" repeatedly (or the scheduler firing right before it) doesn't
    burn through FIRMS'/EFFIS' request quota for no new data.
    """
    elapsed = state.seconds_since_last_attempt(key)
    cooldown = settings.fetch_interval_minutes * 60
    if elapsed is not None and elapsed < cooldown:
        return {
            "source": source,
            "skipped": True,
            "ingested": 0,
            "seconds_since_last_attempt": round(elapsed),
            "cooldown_seconds": cooldown,
            "message": f"Skipped - {source} was already fetched {round(elapsed)}s ago "
            f"(cooldown is {cooldown}s). Use force=true to override.",
        }
    return None


@router.get("", response_model=list[FireDetectionOut])
def list_fires(
    source: Optional[str] = Query(None, description="Filter by 'FIRMS' or 'EFFIS'"),
    hours: int = Query(72, ge=1, le=24 * 30, description="Only detections from the last N hours"),
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(hours=hours)
    query = db.query(FireDetection).filter(FireDetection.acquired_at >= since)
    if source:
        query = query.filter(FireDetection.source == source.upper())
    return query.order_by(FireDetection.acquired_at.desc()).all()


@router.post("/refresh/firms")
def refresh_firms(
    days: int = Query(1, ge=1, le=5, description="How many days of FIRMS history to pull per request (FIRMS caps this at 5 - poll regularly to accumulate more history over time)"),
    force: bool = Query(False, description="Bypass the refresh cooldown and hit FIRMS regardless"),
    db: Session = Depends(get_db),
):
    if not force:
        skipped = _cooldown_response("FIRMS", "firms")
        if skipped:
            return skipped
    try:
        count = ingest_firms(db, day_range=days)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FIRMS fetch failed: {exc}") from exc
    return {"source": "FIRMS", "skipped": False, "ingested": count}


@router.post("/refresh/effis")
def refresh_effis(
    force: bool = Query(False, description="Bypass the refresh cooldown and hit EFFIS regardless"),
    db: Session = Depends(get_db),
):
    if not force:
        skipped = _cooldown_response("EFFIS", "effis")
        if skipped:
            return skipped
    try:
        count = ingest_effis(db)
    except Exception as exc:  # EFFIS endpoint is best-effort/experimental
        raise HTTPException(status_code=502, detail=f"EFFIS fetch failed: {exc}") from exc
    return {"source": "EFFIS", "skipped": False, "ingested": count}
