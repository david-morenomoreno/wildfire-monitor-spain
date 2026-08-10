"""
Imports a JSON export produced by scripts/export_transfer.py into THIS
database (DATABASE_URL). Idempotent for fire_detections/satellite_scenes
(existing unique constraints, ON CONFLICT DO NOTHING); fire_incidents and
incident_events are always inserted as brand-new rows since this database's
own incident identities are independent of the source's - any resulting
duplicate of a real fire already tracked here is expected to be caught by
the next rebuild_incidents() pass's automatic retroactive merge (see
services/incidents.py's merge_reassociable_incidents), not by this script.

Run from backend/ against the TARGET database (DATABASE_URL pointed at it):
    cd backend && python -m scripts.import_transfer

Safe to re-run: fire_detections/satellite_scenes skip anything already
present; fire_incidents/incident_events would duplicate on a second run
(they have no natural unique key to conflict on) - only run this once per
export, and let merge_reassociable_incidents clean up if you don't.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

from app.database import SessionLocal
from app.models import FireDetection, FireIncident, IncidentEvent, SatelliteScene

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_transfer")

IN_DIR = Path("/tmp/prod_transfer")

DATETIME_COLUMNS = {
    "fire_detections": ["acquired_at", "ingested_at"],
    "fire_incidents": ["first_detected_at", "last_detected_at", "updated_at"],
    "incident_events": ["occurred_at"],
    "satellite_scenes": ["captured_at", "discovered_at"],
}


def _load(name: str) -> list[dict]:
    with open(IN_DIR / f"{name}.json") as f:
        rows = json.load(f)
    for column in DATETIME_COLUMNS.get(name, []):
        for row in rows:
            if row.get(column):
                row[column] = datetime.fromisoformat(row[column])
    return rows


def import_all() -> None:
    db = SessionLocal()
    try:
        _import_detections(db)
        incident_id_map = _import_incidents(db)
        _import_events(db, incident_id_map)
        _import_scenes(db, incident_id_map)
    finally:
        db.close()


def _import_detections(db) -> None:
    rows = _load("fire_detections")
    inserted = 0
    for row in rows:
        row = {k: v for k, v in row.items() if k != "id"}
        stmt = insert(FireDetection).values(**row).on_conflict_do_nothing(constraint="uq_source_external_id")
        inserted += db.execute(stmt).rowcount
    db.commit()
    logger.info("fire_detections: %d new rows (%d in export)", inserted, len(rows))


def _import_incidents(db) -> dict[int, int]:
    id_map: dict[int, int] = {}
    for row in _load("fire_incidents"):
        old_id = row.pop("id")
        # slug is unique - a fresh one avoids colliding with an existing
        # target row that happens to share the same generated hex suffix.
        row["slug"] = f"incident-{row['slug'].rsplit('-', 1)[-1]}-t"
        incident = FireIncident(**row)
        db.add(incident)
        db.flush()
        id_map[old_id] = incident.id
    db.commit()
    logger.info("fire_incidents: %d new rows", len(id_map))
    return id_map


def _import_events(db, incident_id_map: dict[int, int]) -> None:
    rows = _load("incident_events")
    imported = 0
    skipped = 0
    for row in rows:
        row.pop("id")
        new_incident_id = incident_id_map.get(row["incident_id"])
        if new_incident_id is None:
            skipped += 1
            continue
        row["incident_id"] = new_incident_id
        db.add(IncidentEvent(**row))
        imported += 1
    db.commit()
    logger.info("incident_events: %d new rows (%d skipped - no matching incident)", imported, skipped)


def _import_scenes(db, incident_id_map: dict[int, int]) -> None:
    rows = _load("satellite_scenes")
    inserted = 0
    skipped = 0
    for row in rows:
        row.pop("id")
        new_incident_id = incident_id_map.get(row["incident_id"])
        if new_incident_id is None:
            skipped += 1
            continue
        row["incident_id"] = new_incident_id
        # A cached thumbnail file path from the SOURCE environment's own
        # upload_dir means nothing here - null it out so it re-renders
        # lazily (same lazy-render-on-first-view behavior as a never-viewed
        # scene) instead of a GET 404ing against a file that doesn't exist.
        row["thumbnail_path"] = None
        stmt = (
            insert(SatelliteScene)
            .values(**row)
            .on_conflict_do_nothing(constraint="uq_incident_collection_scene")
        )
        inserted += db.execute(stmt).rowcount
    db.commit()
    logger.info("satellite_scenes: %d new rows (%d in export, %d skipped - no matching incident)", inserted, len(rows), skipped)


if __name__ == "__main__":
    import_all()
