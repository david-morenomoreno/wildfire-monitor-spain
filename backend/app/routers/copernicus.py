from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FireIncident, SatelliteScene
from app.schemas import SatelliteSceneOut
from app.services.copernicus import (
    discover_for_active_incidents,
    discover_for_incident,
    get_or_render_thumbnail,
    is_configured,
)

router = APIRouter(prefix="/api/copernicus", tags=["copernicus"])


@router.get("/scenes", response_model=list[SatelliteSceneOut])
def list_scenes(
    incident_id: int = Query(..., description="FireIncident id"),
    db: Session = Depends(get_db),
):
    return (
        db.query(SatelliteScene)
        .filter_by(incident_id=incident_id)
        .order_by(SatelliteScene.captured_at.desc())
        .all()
    )


@router.post("/discover/{incident_id}")
def discover_one(incident_id: int, db: Session = Depends(get_db)):
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Copernicus is not configured (missing COPERNICUS_CLIENT_ID/CLIENT_SECRET)",
        )
    incident = db.query(FireIncident).filter_by(id=incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        count = discover_for_incident(db, incident)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Copernicus discovery failed: {exc}") from exc
    return {"incident_id": incident_id, "new_scenes": count}


@router.post("/discover-all")
def discover_all(db: Session = Depends(get_db)):
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Copernicus is not configured (missing COPERNICUS_CLIENT_ID/CLIENT_SECRET)",
        )
    results = discover_for_active_incidents(db)
    return {"incidents_searched": len(results), "new_scenes": sum(results.values())}


@router.get("/scenes/{scene_id}/thumbnail")
def scene_thumbnail(scene_id: int, db: Session = Depends(get_db)):
    """
    Serves a true-color thumbnail for one scene, rendering it via the
    Process API on first request and caching to disk after that - not
    rendered automatically for every discovered scene since it costs
    processing quota.
    """
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Copernicus is not configured (missing COPERNICUS_CLIENT_ID/CLIENT_SECRET)",
        )
    scene = db.query(SatelliteScene).filter_by(id=scene_id).first()
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    try:
        image_bytes = get_or_render_thumbnail(db, scene)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thumbnail render failed: {exc}") from exc
    return Response(content=image_bytes, media_type="image/jpeg")
