from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FireIncident, IncidentEvent
from app.schemas import FireIncidentOut, IncidentEventOut

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

_SOURCE_FLAG_EVENT_TYPES = {
    "regional_status": "has_regional_status",
    "telegram_message": "has_telegram_mentions",
    "satellite_imagery": "has_satellite_imagery",
}


def _with_source_flags(db: Session, incidents: list[FireIncident]) -> list[FireIncidentOut]:
    """Enriches each incident with which non-satellite sources have contributed
    to it, in one bulk query rather than a per-incident timeline fetch."""
    ids = [incident.id for incident in incidents]
    flags_by_incident: dict[int, set[str]] = defaultdict(set)
    if ids:
        rows = (
            db.query(IncidentEvent.incident_id, IncidentEvent.event_type)
            .filter(
                IncidentEvent.incident_id.in_(ids),
                IncidentEvent.event_type.in_(_SOURCE_FLAG_EVENT_TYPES.keys()),
            )
            .distinct()
            .all()
        )
        for incident_id, event_type in rows:
            flags_by_incident[incident_id].add(event_type)

    result = []
    for incident in incidents:
        present = flags_by_incident.get(incident.id, set())
        out = FireIncidentOut.model_validate(incident)
        result.append(
            out.model_copy(
                update={
                    field: (event_type in present)
                    for event_type, field in _SOURCE_FLAG_EVENT_TYPES.items()
                }
            )
        )
    return result


@router.get("", response_model=list[FireIncidentOut])
def list_incidents(
    status: Optional[str] = Query(None, description="Filter by 'active', 'cooling', or 'archived'"),
    hours: int = Query(24 * 30, ge=1, le=24 * 30, description="Only incidents last detected in the last N hours"),
    sort: str = Query("severity", description="'severity' (default) or 'recent'"),
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(hours=hours)
    query = db.query(FireIncident).filter(FireIncident.last_detected_at >= since)
    if status:
        query = query.filter(FireIncident.status == status.lower())
    if sort == "recent":
        query = query.order_by(FireIncident.last_detected_at.desc())
    else:
        query = query.order_by(FireIncident.severity_score.desc())
    return _with_source_flags(db, query.all())


@router.get("/{incident_id}", response_model=FireIncidentOut)
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _with_source_flags(db, [incident])[0]


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventOut])
def get_incident_timeline(
    incident_id: int,
    hours: Optional[int] = Query(
        None, ge=1, le=24 * 30, description="Only events in the last N hours - matches the map's date-range filter"
    ),
    db: Session = Depends(get_db),
):
    incident = db.query(FireIncident).filter(FireIncident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    query = db.query(IncidentEvent).filter(IncidentEvent.incident_id == incident_id)
    if hours is not None:
        since = datetime.utcnow() - timedelta(hours=hours)
        query = query.filter(IncidentEvent.occurred_at >= since)
    return query.order_by(IncidentEvent.occurred_at.asc()).all()
