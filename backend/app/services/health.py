from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import SourceCheck

# Worse status wins when multiple checks land on the same day - matches the
# convention status pages use (a single interruption marks the whole day,
# even if things recovered later that day).
_SEVERITY = {"ok": 0, "skipped": 1, "degraded": 2, "disrupted": 3}


def record_check(
    db: Session, source_key: str, status: str, message: str | None = None, rows_written: int | None = None
) -> None:
    """
    rows_written is the count of GENUINELY NEW rows this check wrote - not
    just rows re-fetched/re-seen (most ingestion sources upsert with
    ON CONFLICT DO NOTHING, so a re-polled, already-known row still gets
    "processed" without being new). Leave it None for skipped/disrupted
    checks where nothing was attempted, or 0 when the fetch succeeded but
    found nothing new. Powers GET /api/sources' last_data_at, distinct from
    "last successfully checked at all" - see routers/sources.py.
    """
    db.add(
        SourceCheck(
            source_key=source_key,
            status=status,
            message=message[:1000] if message else None,
            checked_at=datetime.utcnow(),
            rows_written=rows_written,
        )
    )
    db.commit()


def get_health_grid(db: Session, days: int = 14) -> list[dict]:
    """
    Returns one entry per source_key ever checked, each with a `days` list
    (oldest first) of {date, status, message} for the last `days` days - the
    message shown is from the worst check that day.
    """
    since = datetime.utcnow() - timedelta(days=days)
    checks = (
        db.query(SourceCheck)
        .filter(SourceCheck.checked_at >= since)
        .order_by(SourceCheck.checked_at.asc())
        .all()
    )

    # source_key -> date -> {"status": ..., "message": ...}
    by_source: dict[str, dict[str, dict]] = defaultdict(dict)
    for check in checks:
        day = check.checked_at.date().isoformat()
        current = by_source[check.source_key].get(day)
        if current is None or _SEVERITY[check.status] >= _SEVERITY[current["status"]]:
            by_source[check.source_key][day] = {"status": check.status, "message": check.message}

    today = datetime.utcnow().date()
    date_range = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]

    result = []
    for source_key, day_map in by_source.items():
        result.append(
            {
                "source_key": source_key,
                "days": [
                    {"date": day, **day_map.get(day, {"status": None, "message": None})}
                    for day in date_range
                ],
            }
        )
    return sorted(result, key=lambda entry: entry["source_key"])
