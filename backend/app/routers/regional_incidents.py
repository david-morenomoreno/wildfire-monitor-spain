from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RegionalIncident, RegionalIncidentSource
from app.schemas import RegionalIncidentOut, RegionalIncidentSourceOut
from app.services.regional_incidents.registry import REGION_SOURCES
from app.services.regional_incidents.sync import sync_region

router = APIRouter(prefix="/api/regional-incidents", tags=["regional-incidents"])


@router.get("/sources", response_model=list[RegionalIncidentSourceOut])
def list_sources(db: Session = Depends(get_db)):
    return db.query(RegionalIncidentSource).all()


@router.get("", response_model=list[RegionalIncidentOut])
def list_incidents(
    region: Optional[str] = Query(None, description="Filter by region_code, e.g. 'jcyl'"),
    status: Optional[str] = Query(None, description="Filter by the region's own status label, e.g. 'Activo'"),
    incident_id: Optional[int] = Query(None, description="Filter by matched FireIncident id"),
    db: Session = Depends(get_db),
):
    query = db.query(RegionalIncident)
    if region:
        source = db.query(RegionalIncidentSource).filter_by(region_code=region).first()
        if source is None:
            return []
        query = query.filter(RegionalIncident.source_id == source.id)
    if status:
        query = query.filter(RegionalIncident.status == status)
    if incident_id is not None:
        query = query.filter(RegionalIncident.matched_incident_id == incident_id)
    return query.order_by(RegionalIncident.updated_at.desc()).all()


@router.post("/{region_code}/refresh")
def refresh_region(region_code: str, db: Session = Depends(get_db)):
    if region_code not in REGION_SOURCES:
        raise HTTPException(status_code=404, detail="Unknown region")
    try:
        count = sync_region(db, region_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Regional incident sync failed: {exc}") from exc
    return {"region_code": region_code, "changed": count}
