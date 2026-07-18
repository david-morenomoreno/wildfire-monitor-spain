import csv
import io
import logging
from datetime import datetime

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import FireDetection
from app.services.geo_filter import is_in_spain
from app.services.health import record_check

logger = logging.getLogger(__name__)

FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


def build_firms_url(source: str, day_range: int, end_date: str | None = None) -> str:
    if not settings.firms_map_key:
        raise RuntimeError(
            "FIRMS_MAP_KEY is not set. Request a free key at "
            "https://firms.modaps.eosdis.nasa.gov/api/"
        )
    url = (
        f"{FIRMS_BASE_URL}/{settings.firms_map_key}/{source}"
        f"/{settings.firms_bbox}/{day_range}"
    )
    # FIRMS' area/csv endpoint caps day_range at 10; passing an end_date
    # (YYYY-MM-DD) lets a caller walk further back in 10-day windows to
    # backfill a longer history - see backend/scripts/backfill_history.py.
    if end_date:
        url = f"{url}/{end_date}"
    return url


def fetch_firms_rows(source: str, day_range: int, end_date: str | None = None) -> list[dict]:
    url = build_firms_url(source, day_range, end_date)
    response = httpx.get(url, timeout=30.0)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


def _parse_acquired_at(row: dict) -> datetime:
    date_str = row["acq_date"]
    time_str = row.get("acq_time", "0000").zfill(4)
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M")


def ingest_firms(db: Session, day_range: int | None = None, end_date: str | None = None) -> int:
    """
    Fetch current FIRMS detections for Spain across all configured satellite
    sources and upsert them. Returns row count processed (across all sources).
    """
    state.mark_attempt("firms")
    try:
        count = _ingest_firms(db, day_range, end_date)
    except Exception as exc:
        record_check(db, "firms", "disrupted", str(exc))
        raise
    record_check(db, "firms", "ok", f"{count} rows processed")
    return count


def _ingest_firms(db: Session, day_range: int | None, end_date: str | None = None) -> int:
    day_range = day_range or settings.firms_day_range
    count = 0
    skipped_outside_spain = 0
    for source in settings.firms_sources:
        rows = fetch_firms_rows(source, day_range, end_date)
        for row in rows:
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                acquired_at = _parse_acquired_at(row)
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed FIRMS row %s: %s", row, exc)
                continue

            # `firms_bbox` is a rectangle around Spain's irregular real
            # border, so it necessarily also covers slivers of Algeria,
            # France and Portugal that share the same lat/lon range (see
            # geo_filter.py's module docstring for the confirmed real-world
            # examples). This is the root-cause fix for those detections
            # showing up as incidents - drop them here, before they're ever
            # stored, rather than only hiding them in the UI afterwards.
            if not is_in_spain(latitude, longitude):
                skipped_outside_spain += 1
                continue

            # MODIS CSVs use "brightness"/"bright_t31"; VIIRS uses "bright_ti4"/"bright_ti5".
            brightness_raw = row.get("bright_ti4") or row.get("brightness")

            external_id = (
                f"{source}-{row.get('satellite', '')}-{row['acq_date']}"
                f"-{row.get('acq_time', '')}-{latitude}-{longitude}"
            )

            stmt = (
                insert(FireDetection)
                .values(
                    source="FIRMS",
                    external_id=external_id,
                    latitude=latitude,
                    longitude=longitude,
                    confidence=row.get("confidence"),
                    brightness=float(brightness_raw) if brightness_raw else None,
                    acquired_at=acquired_at,
                    raw_properties=str(row),
                )
                .on_conflict_do_nothing(constraint="uq_source_external_id")
            )
            db.execute(stmt)
            count += 1

    if skipped_outside_spain:
        logger.info(
            "Skipped %d FIRMS row(s) inside firms_bbox but outside Spain's real border",
            skipped_outside_spain,
        )
    db.commit()
    return count
