"""
Backfill historical satellite fire data - the last N days (default 30), or
an explicit --start-date for a long historical run (e.g. back to the start
of the year).

Run from backend/ so `app` is importable, with the same env vars the API
container uses (DATABASE_URL, FIRMS_MAP_KEY, EUMETSAT_CONSUMER_KEY/SECRET, ...):

    cd backend && python -m scripts.backfill_history
    cd backend && python -m scripts.backfill_history --days 14
    cd backend && python -m scripts.backfill_history --start-date 2026-01-01
    cd backend && python -m scripts.backfill_history --start-date 2026-01-01 --sources eumetsat,sentinel3

What it does, in order:
  1. FIRMS      - this project's map key caps area/csv's day_range at 5
                  (confirmed live 2026-07-18 - NASA's docs say 10, but a
                  day_range=10 request against this key returns "Invalid day
                  range. Expects [1..5]."). Walks backwards in 5-day windows
                  (using the end_date param) to cover the full requested
                  window.
  2. EFFIS      - the WFS feed has no date filter; it always returns whatever
                  JRC currently publishes, so this just runs once.
  3. EUMETSAT   - MTG/FCI Active Fire Monitoring fires a new full-disk
                  product roughly every 10-15 min (see services/eumetsat.py),
                  so a long historical range is walked in small
                  (EUMETSAT_WINDOW_HOURS) windows to stay well under
                  search_products' page-size cap, downloading+parsing every
                  product in range - this is the slowest step by far for a
                  multi-month backfill (thousands of small downloads).
  4. Sentinel-3 - SLSTR FRP is already bbox-filtered to Spain and has far
                  fewer overpasses/day than EUMETSAT, so it's walked in
                  larger (SENTINEL3_WINDOW_HOURS) windows.
  5. Incidents rebuild - clusters the newly backfilled detections. Only
                  matters if the backfilled range exceeds
                  INCIDENTS_WINDOW_HOURS (30 days) in
                  app/services/incidents.py, otherwise the normal scheduler
                  job already does this.
  6. Copernicus discovery - searches for Sentinel-2 scenes over each
                  incident's own detected date range (which now reaches back
                  into the backfilled window), skipped if OAuth creds aren't
                  set.

RESUMING: EUMETSAT and Sentinel-3 progress is checkpointed to
scripts/.backfill_checkpoint.json after every window (both ingests upsert
with on_conflict_do_nothing, so re-running an already-done window is safe
too - the checkpoint just avoids redoing the (slow) API calls). Delete that
file to force a from-scratch backfill.
"""

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.database import SessionLocal
from app.services.copernicus import discover_for_active_incidents
from app.services.effis import ingest_effis
from app.services.eumetsat import ingest_eumetsat
from app.services.firms import ingest_firms
from app.services.incidents import rebuild_incidents
from app.services.sentinel3 import ingest_sentinel3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

# NASA's docs say area/csv accepts day_range up to 10, but this project's
# FIRMS_MAP_KEY was confirmed live (2026-07-18) to cap out at 5 - a
# day_range=10 request returns "Invalid day range. Expects [1..5]." Some
# keys/plans apparently get a lower cap, so this walks back in 5-day windows.
FIRMS_MAX_DAY_RANGE = 5

# ~10-15 min product cadence, full-disk (no bbox filter) - see
# services/eumetsat.py's module docstring. 12h keeps each window's product
# count (~48-72) comfortably under search_products' page_size=100.
EUMETSAT_WINDOW_HOURS = 12

# Already bbox-filtered to Spain server-side and only ~2 polar overpasses/day
# - far sparser than EUMETSAT's full-disk cadence, so a wider window is safe.
SENTINEL3_WINDOW_HOURS = 24 * 7

CHECKPOINT_PATH = Path(__file__).parent / ".backfill_checkpoint.json"


def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text())
    return {}


def _save_checkpoint(checkpoint: dict) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(checkpoint))


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


def _backfill_windowed(db, name: str, ingest_fn, start: datetime, end: datetime, window_hours: int, checkpoint: dict) -> int:
    """
    Shared walk-forward-in-fixed-windows loop for EUMETSAT/Sentinel-3, with
    checkpointing so an interrupted multi-hour run can resume without redoing
    already-fetched windows. A window that raises is logged and skipped
    (not fatal) so one bad product/API hiccup doesn't abort the whole
    backfill - see the ingest functions' own per-product try/except for
    finer-grained handling of that.
    """
    checkpoint_key = f"{name}_last_end"
    cursor = start
    saved = checkpoint.get(checkpoint_key)
    if saved:
        saved_dt = datetime.fromisoformat(saved)
        if saved_dt > cursor:
            logger.info("Resuming %s backfill from checkpoint %s", name, saved_dt)
            cursor = saved_dt

    window = timedelta(hours=window_hours)
    total = 0
    while cursor < end:
        window_end = min(cursor + window, end)
        try:
            count = ingest_fn(db, start=cursor, end=window_end)
            logger.info("%s window %s -> %s: %d fire pixels", name, cursor, window_end, count)
            total += count
        except Exception:
            logger.exception("%s window %s -> %s failed - continuing", name, cursor, window_end)
        cursor = window_end
        checkpoint[checkpoint_key] = cursor.isoformat()
        _save_checkpoint(checkpoint)
    return total


def backfill_eumetsat(db, start: datetime, end: datetime, checkpoint: dict) -> int:
    return _backfill_windowed(db, "eumetsat", ingest_eumetsat, start, end, EUMETSAT_WINDOW_HOURS, checkpoint)


def backfill_sentinel3(db, start: datetime, end: datetime, checkpoint: dict) -> int:
    return _backfill_windowed(db, "sentinel3", ingest_sentinel3, start, end, SENTINEL3_WINDOW_HOURS, checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="How many days back to fetch (default: 30)")
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="YYYY-MM-DD to backfill from (overrides --days) - used for FIRMS/EUMETSAT/Sentinel-3",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="firms,effis,eumetsat,sentinel3,incidents,copernicus",
        help="Comma-separated subset to run (default: all)",
    )
    args = parser.parse_args()
    sources = {s.strip() for s in args.sources.split(",") if s.strip()}

    now = datetime.utcnow()
    if args.start_date:
        start = datetime.strptime(args.start_date, "%Y-%m-%d")
        total_days = max(1, (now.date() - start.date()).days)
    else:
        start = now - timedelta(days=args.days)
        total_days = args.days

    checkpoint = _load_checkpoint()

    db = SessionLocal()
    try:
        if "firms" in sources:
            try:
                firms_count = backfill_firms(db, total_days)
                logger.info("FIRMS total: %d rows processed", firms_count)
            except Exception:
                logger.exception("FIRMS backfill failed - continuing with remaining steps")

        if "effis" in sources:
            try:
                effis_count = ingest_effis(db)
                logger.info("EFFIS: %d features processed (no date filter available)", effis_count)
            except Exception:
                # JRC's WFS backend has been observed failing server-side
                # (Oracle Spatial connection errors) independent of anything
                # this project controls - see effis.py's module docstring.
                logger.exception("EFFIS ingest failed - continuing with remaining steps")

        if "eumetsat" in sources:
            try:
                eumetsat_count = backfill_eumetsat(db, start, now, checkpoint)
                logger.info("EUMETSAT total: %d fire pixels processed", eumetsat_count)
            except Exception:
                logger.exception("EUMETSAT backfill failed - continuing with remaining steps")

        if "sentinel3" in sources:
            try:
                sentinel3_count = backfill_sentinel3(db, start, now, checkpoint)
                logger.info("Sentinel-3 total: %d fire pixels processed", sentinel3_count)
            except Exception:
                logger.exception("Sentinel-3 backfill failed - continuing with remaining steps")

        if "incidents" in sources:
            try:
                touched = rebuild_incidents(db)
                logger.info("Incident rebuild: %d incidents touched", touched)
            except Exception:
                logger.exception("Incident rebuild failed - continuing with remaining steps")

        if "copernicus" in sources:
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
