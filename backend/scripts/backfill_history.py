"""
Backfill the last N days (default 30) of satellite fire data.

Run from backend/ so `app` is importable, with the same env vars the API
container uses (DATABASE_URL, FIRMS_MAP_KEY, ...):

    cd backend && python -m scripts.backfill_history
    cd backend && python -m scripts.backfill_history --days 14

What it does, in order:
  1. FIRMS  - this project's map key caps area/csv's day_range at 5 (confirmed
              live 2026-07-18 - NASA's docs say 10, but a day_range=10 request
              against this key returns "Invalid day range. Expects [1..5].").
              Walks backwards in 5-day windows (using the end_date param) to
              cover the full requested window.
  2. EFFIS  - the WFS feed has no date filter; it always returns whatever
              JRC currently publishes, so this just runs once.
  3. Incidents rebuild - clusters the newly backfilled detections. Only
              matters if --days exceeds INCIDENTS_WINDOW_HOURS (30 days) in
              app/services/incidents.py, otherwise the normal scheduler job
              already does this.
  4. Copernicus discovery - searches for Sentinel-2 scenes over each
              incident's own detected date range (which now reaches back
              into the backfilled window), skipped if OAuth creds aren't set.
"""

import argparse
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.services.copernicus import discover_for_active_incidents
from app.services.effis import ingest_effis
from app.services.firms import ingest_firms
from app.services.incidents import rebuild_incidents

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

# NASA's docs say area/csv accepts day_range up to 10, but this project's
# FIRMS_MAP_KEY was confirmed live (2026-07-18) to cap out at 5 - a
# day_range=10 request returns "Invalid day range. Expects [1..5]." Some
# keys/plans apparently get a lower cap, so this walks back in 5-day windows.
FIRMS_MAX_DAY_RANGE = 5


def backfill_firms(db, total_days: int) -> int:
    total = 0
    remaining = total_days
    end_date = datetime.utcnow().date()
    while remaining > 0:
        window = min(FIRMS_MAX_DAY_RANGE, remaining)
        count = ingest_firms(db, day_range=window, end_date=end_date.isoformat())
        logger.info("FIRMS window ending %s (%d days): %d rows", end_date, window, count)
        total += count
        end_date -= timedelta(days=window)
        remaining -= window
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="How many days back to fetch (default: 30)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        try:
            firms_count = backfill_firms(db, args.days)
            logger.info("FIRMS total: %d rows processed", firms_count)
        except Exception:
            logger.exception("FIRMS backfill failed - continuing with remaining steps")

        try:
            effis_count = ingest_effis(db)
            logger.info("EFFIS: %d features processed (no date filter available)", effis_count)
        except Exception:
            # JRC's WFS backend has been observed failing server-side
            # (Oracle Spatial connection errors) independent of anything
            # this project controls - see effis.py's module docstring.
            logger.exception("EFFIS ingest failed - continuing with remaining steps")

        try:
            touched = rebuild_incidents(db)
            logger.info("Incident rebuild: %d incidents touched", touched)
        except Exception:
            logger.exception("Incident rebuild failed - continuing with remaining steps")

        try:
            copernicus_results = discover_for_active_incidents(db)
            if copernicus_results:
                logger.info("Copernicus discovery: %s", copernicus_results)
            else:
                logger.info("Copernicus discovery: skipped (not configured or no active incidents)")
        except Exception:
            logger.exception("Copernicus discovery failed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
