import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.routers import (
    admin_bulletins,
    copernicus,
    fire_spread,
    fires,
    geo,
    geocode,
    health,
    incidents,
    proximity,
    regional_incidents,
    reports,
    sources,
    telegram,
    webcams,
)
from app.scheduler import start_scheduler
from app.services.incidents import rebuild_incidents
from app.services.telegram import seed_default_channels

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Wildfire Monitor Spain API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fires.router)
app.include_router(reports.router)
app.include_router(geocode.router)
app.include_router(geo.router)
app.include_router(incidents.router)
app.include_router(admin_bulletins.router)
app.include_router(telegram.router)
app.include_router(sources.router)
app.include_router(health.router)
app.include_router(copernicus.router)
app.include_router(regional_incidents.router)
app.include_router(webcams.router)
app.include_router(fire_spread.router)
app.include_router(proximity.router)

# Serves both Telegram-downloaded photos and (once wired up) user-report
# uploads - previously stored under upload_dir but with no way to view them.
os.makedirs(settings.upload_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.upload_dir), name="media")


@app.on_event("startup")
def on_startup():
    # Legacy safety net, kept for now: this only ever CREATES missing tables,
    # it never adds a missing column to a table that already exists (bitten
    # by this twice - see git history for `official_name` and
    # CopernicusEmsActivation). Alembic (backend/alembic/) is now the source
    # of truth for schema changes going forward - see README.md's "Database
    # migrations" section. This call should be removed once every
    # environment, including prod, has been `alembic stamp head`-ed so
    # Alembic's own version table reflects reality everywhere.
    Base.metadata.create_all(bind=engine)
    # Populate incidents from whatever detections already exist immediately,
    # rather than waiting up to fetch_interval_minutes for the first scheduled run.
    db = SessionLocal()
    try:
        rebuild_incidents(db)
    except Exception:
        logging.exception("Initial incident rebuild failed")
    try:
        seed_default_channels(db)
    except Exception:
        logging.exception("Seeding default Telegram channels failed")
    finally:
        db.close()
    start_scheduler()


@app.get("/health")
def liveness_check():
    return {"status": "ok"}
