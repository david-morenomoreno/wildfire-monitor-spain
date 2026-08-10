"""
Exports fire_detections + the fire_incidents lineage (incident_events,
satellite_scenes) from THIS database to JSON files, for import into another
environment via scripts/import_transfer.py - built for transferring local
dev's historical backfill onto prod without re-running the (slow, heavy)
backfill there.

Deliberately does NOT export copernicus_ems_activations/copernicus_ems_products:
those are re-derivable for free once the incidents exist (the target's own
scheduled EMS poll already fetches ALL public Wildfire/Spain activations and
matches them by centroid on its own daily cadence, no quota/cost to redo) -
transferring them would need the same ID-remapping complexity for something
that shows up on its own within a day anyway. Same reasoning excludes
"ems_activation"-type incident_events (the target would create a fresh,
duplicate announcement for the same activation since it has no record of
this transfer's copy already existing).

Run from backend/ against the SOURCE database (DATABASE_URL pointed at it):
    cd backend && python -m scripts.export_transfer
"""

import json
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.models import FireDetection, FireIncident, IncidentEvent, SatelliteScene

OUT_DIR = Path("/tmp/prod_transfer")

# event_type values NOT to export - see module docstring for why.
EXCLUDED_EVENT_TYPES = {"ems_activation"}


def _serialize(obj) -> dict:
    row = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        row[col.name] = value
    return row


def export_all() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        detections = db.query(FireDetection).all()
        _write("fire_detections", detections)

        incidents = db.query(FireIncident).all()
        _write("fire_incidents", incidents)

        events = db.query(IncidentEvent).filter(~IncidentEvent.event_type.in_(EXCLUDED_EVENT_TYPES)).all()
        _write("incident_events", events)

        scenes = db.query(SatelliteScene).all()
        _write("satellite_scenes", scenes)
    finally:
        db.close()


def _write(name: str, rows: list) -> None:
    with open(OUT_DIR / f"{name}.json", "w") as f:
        json.dump([_serialize(r) for r in rows], f)
    print(f"{name}: {len(rows)} rows exported")


if __name__ == "__main__":
    export_all()
