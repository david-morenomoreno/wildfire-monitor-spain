import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models import AdminBulletin, AdminSource
from app.services.admin_bulletins.base import AdminBulletinSource
from app.services.admin_bulletins.registry import REGION_SOURCES
from app.services.health import record_check

logger = logging.getLogger(__name__)


def _get_or_create_source(db: Session, source: AdminBulletinSource) -> AdminSource:
    row = db.query(AdminSource).filter_by(region_code=source.region_code).first()
    if row is None:
        row = AdminSource(
            region_code=source.region_code,
            name=source.name,
            portal_url=source.portal_url,
        )
        db.add(row)
        db.flush()
    return row


def sync_region(db: Session, region_code: str) -> int:
    """
    Discovers bulletins for one region, stores any not already known, and
    attempts best-effort table parsing on new ones. Returns the count of new
    bulletins stored (not the number of rows extracted from them).
    """
    source = REGION_SOURCES.get(region_code)
    if source is None:
        raise KeyError(f"No admin bulletin source registered for '{region_code}'")

    source_row = _get_or_create_source(db, source)
    try:
        refs = source.discover()
    except Exception as exc:
        record_check(db, f"admin:{region_code}", "disrupted", str(exc))
        raise

    existing_urls = {
        url
        for (url,) in db.query(AdminBulletin.file_url).filter_by(source_id=source_row.id).all()
    }

    new_count = 0
    fetch_failures = 0
    for ref in refs:
        if ref.file_url in existing_urls:
            continue

        row_count = None
        parsed_at = None
        try:
            response = httpx.get(ref.file_url, timeout=30.0, follow_redirects=True, verify=False)
            response.raise_for_status()
            rows = source.parse(response.content)
            if rows is not None:
                row_count = len(rows)
                parsed_at = datetime.utcnow()
        except Exception:
            logger.warning("Failed to fetch/parse bulletin %s", ref.file_url, exc_info=True)
            fetch_failures += 1

        db.add(
            AdminBulletin(
                source_id=source_row.id,
                title=ref.title,
                file_url=ref.file_url,
                file_type=ref.file_type,
                row_count=row_count,
                parsed_at=parsed_at,
            )
        )
        new_count += 1

    db.commit()

    if fetch_failures:
        record_check(
            db,
            f"admin:{region_code}",
            "degraded",
            f"{new_count} new bulletins, {fetch_failures} failed to fetch/parse",
        )
    else:
        record_check(db, f"admin:{region_code}", "ok", f"{new_count} new bulletins")
    return new_count


def sync_all_regions(db: Session) -> dict[str, int]:
    results = {}
    for region_code in REGION_SOURCES:
        try:
            results[region_code] = sync_region(db, region_code)
        except Exception:
            logger.exception("Admin bulletin sync failed for region '%s'", region_code)
            results[region_code] = 0
    return results
