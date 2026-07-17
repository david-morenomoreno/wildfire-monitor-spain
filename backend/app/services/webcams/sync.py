import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Webcam
from app.services.health import record_check
from app.services.webcams.registry import WEBCAM_SOURCES

logger = logging.getLogger(__name__)


def sync_source(db: Session, source_key: str) -> int:
    """
    Fetches a provider's full camera list and upserts it. Returns the count
    of cameras seen (not just new ones - the list is mostly stable, but
    coordinates/name/image_url are refreshed each run in case a camera moved
    or was renamed).
    """
    source = WEBCAM_SOURCES.get(source_key)
    if source is None:
        raise KeyError(f"No webcam source registered for '{source_key}'")

    try:
        records = source.fetch()
    except Exception as exc:
        record_check(db, f"webcams:{source_key}", "disrupted", str(exc))
        raise

    existing = {
        row.external_id: row
        for row in db.query(Webcam).filter_by(source=source_key).all()
    }

    now = datetime.utcnow()
    for record in records:
        row = existing.get(record.external_id)
        if row is None:
            db.add(
                Webcam(
                    source=source_key,
                    external_id=record.external_id,
                    name=record.name,
                    road=record.road,
                    province=record.province,
                    latitude=record.latitude,
                    longitude=record.longitude,
                    image_url=record.image_url,
                    updated_at=now,
                )
            )
        else:
            row.name = record.name
            row.road = record.road
            row.province = record.province
            row.latitude = record.latitude
            row.longitude = record.longitude
            row.image_url = record.image_url
            row.updated_at = now

    db.commit()
    record_check(db, f"webcams:{source_key}", "ok", f"{len(records)} cameras")
    return len(records)


def sync_all_sources(db: Session) -> dict[str, int]:
    results = {}
    for source_key in WEBCAM_SOURCES:
        try:
            results[source_key] = sync_source(db, source_key)
        except Exception:
            logger.exception("Webcam sync failed for '%s'", source_key)
            results[source_key] = 0
    return results
