from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.health import get_health_grid

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
def health_grid(
    days: int = Query(14, ge=1, le=90, description="How many days of history to include"),
    db: Session = Depends(get_db),
):
    return get_health_grid(db, days=days)
