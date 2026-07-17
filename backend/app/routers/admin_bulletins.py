from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminBulletin, AdminSource
from app.schemas import AdminBulletinOut, AdminSourceOut
from app.services.admin_bulletins.registry import REGION_SOURCES
from app.services.admin_bulletins.sync import sync_region

router = APIRouter(prefix="/api/admin-sources", tags=["admin-bulletins"])


@router.get("", response_model=list[AdminSourceOut])
def list_admin_sources(db: Session = Depends(get_db)):
    return db.query(AdminSource).all()


@router.get("/{region_code}/bulletins", response_model=list[AdminBulletinOut])
def list_bulletins(region_code: str, db: Session = Depends(get_db)):
    source = db.query(AdminSource).filter_by(region_code=region_code).first()
    if source is None:
        raise HTTPException(status_code=404, detail="Unknown region")
    return (
        db.query(AdminBulletin)
        .filter_by(source_id=source.id)
        .order_by(AdminBulletin.fetched_at.desc())
        .all()
    )


@router.post("/{region_code}/refresh")
def refresh_region(region_code: str, db: Session = Depends(get_db)):
    if region_code not in REGION_SOURCES:
        raise HTTPException(status_code=404, detail="Unknown region")
    try:
        new_count = sync_region(db, region_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Admin bulletin sync failed: {exc}") from exc
    return {"region_code": region_code, "new_bulletins": new_count}
