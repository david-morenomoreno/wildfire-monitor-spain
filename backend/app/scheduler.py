import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import SessionLocal
from app.services.admin_bulletins.sync import sync_all_regions
from app.services.copernicus import discover_for_active_incidents
from app.services.copernicus_ems import ingest_copernicus_ems
from app.services.effis import ingest_effis
from app.services.eumetsat import ingest_eumetsat
from app.services.firms import ingest_firms
from app.services.incidents import rebuild_incidents
from app.services.regional_incidents.sync import sync_all_regions as sync_all_regional_incidents
from app.services.sentinel3 import ingest_sentinel3
from app.services.telegram import poll_all_channels
from app.services.webcams.sync import sync_all_sources as sync_all_webcams

logger = logging.getLogger(__name__)


def _run_firms_job():
    db = SessionLocal()
    try:
        count = ingest_firms(db)
        logger.info("FIRMS ingest: %d rows", count)
    except Exception:
        logger.exception("FIRMS ingest failed")
    finally:
        db.close()


def _run_effis_job():
    db = SessionLocal()
    try:
        count = ingest_effis(db)
        logger.info("EFFIS ingest: %d rows", count)
    except Exception:
        logger.exception("EFFIS ingest failed")
    finally:
        db.close()


def _run_eumetsat_job():
    db = SessionLocal()
    try:
        count = ingest_eumetsat(db)
        logger.info("EUMETSAT ingest: %d fire pixels", count)
    except Exception:
        logger.exception("EUMETSAT ingest failed")
    finally:
        db.close()


def _run_sentinel3_job():
    db = SessionLocal()
    try:
        count = ingest_sentinel3(db)
        logger.info("Sentinel-3 SLSTR FRP ingest: %d fire pixels", count)
    except Exception:
        logger.exception("Sentinel-3 SLSTR FRP ingest failed")
    finally:
        db.close()


def _run_incident_rebuild_job():
    db = SessionLocal()
    try:
        count = rebuild_incidents(db)
        logger.info("Incident rebuild: %d incidents touched", count)
    except Exception:
        logger.exception("Incident rebuild failed")
    finally:
        db.close()


def _run_admin_bulletins_job():
    db = SessionLocal()
    try:
        results = sync_all_regions(db)
        logger.info("Admin bulletin sync: %s", results)
    except Exception:
        logger.exception("Admin bulletin sync failed")
    finally:
        db.close()


def _run_telegram_poll_job():
    db = SessionLocal()
    try:
        results = poll_all_channels(db)
        logger.info("Telegram poll: %s", results)
    except Exception:
        logger.exception("Telegram poll failed")
    finally:
        db.close()


def _run_copernicus_discovery_job():
    db = SessionLocal()
    try:
        results = discover_for_active_incidents(db)
        logger.info("Copernicus discovery: %s", results)
    except Exception:
        logger.exception("Copernicus discovery failed")
    finally:
        db.close()


def _run_copernicus_ems_job():
    db = SessionLocal()
    try:
        count = ingest_copernicus_ems(db)
        logger.info("Copernicus EMS: %d activations newly matched", count)
    except Exception:
        logger.exception("Copernicus EMS ingest failed")
    finally:
        db.close()


def _run_regional_incidents_job():
    db = SessionLocal()
    try:
        results = sync_all_regional_incidents(db)
        logger.info("Regional incident sync: %s", results)
    except Exception:
        logger.exception("Regional incident sync failed")
    finally:
        db.close()


def _run_webcams_job():
    db = SessionLocal()
    try:
        results = sync_all_webcams(db)
        logger.info("Webcam sync: %s", results)
    except Exception:
        logger.exception("Webcam sync failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_firms_job,
        "interval",
        minutes=settings.fetch_interval_minutes,
        id="firms_ingest",
    )
    scheduler.add_job(
        _run_effis_job,
        "interval",
        minutes=settings.fetch_interval_minutes,
        id="effis_ingest",
    )
    scheduler.add_job(
        _run_eumetsat_job,
        "interval",
        minutes=settings.eumetsat_poll_interval_minutes,
        id="eumetsat_ingest",
    )
    scheduler.add_job(
        _run_sentinel3_job,
        "interval",
        minutes=settings.sentinel3_poll_interval_minutes,
        id="sentinel3_ingest",
    )
    scheduler.add_job(
        _run_incident_rebuild_job,
        "interval",
        minutes=settings.fetch_interval_minutes,
        id="incident_rebuild",
    )
    scheduler.add_job(
        _run_admin_bulletins_job,
        "interval",
        minutes=settings.admin_bulletins_interval_minutes,
        id="admin_bulletins_sync",
    )
    scheduler.add_job(
        _run_telegram_poll_job,
        "interval",
        minutes=settings.telegram_poll_interval_minutes,
        id="telegram_poll",
    )
    scheduler.add_job(
        _run_copernicus_discovery_job,
        "interval",
        minutes=settings.copernicus_discovery_interval_minutes,
        id="copernicus_discovery",
    )
    scheduler.add_job(
        _run_copernicus_ems_job,
        "interval",
        minutes=settings.copernicus_ems_interval_minutes,
        id="copernicus_ems_ingest",
    )
    scheduler.add_job(
        _run_regional_incidents_job,
        "interval",
        minutes=settings.regional_incidents_interval_minutes,
        id="regional_incidents_sync",
    )
    scheduler.add_job(
        _run_webcams_job,
        "interval",
        minutes=settings.webcams_interval_minutes,
        id="webcams_sync",
    )
    scheduler.start()
    return scheduler
