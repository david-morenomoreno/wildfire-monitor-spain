from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import state
from app.database import get_db
from app.config import settings
from app.models import (
    AdminBulletin,
    AdminSource,
    FireDetection,
    RegionalIncident,
    RegionalIncidentSource,
    SatelliteScene,
    SourceCheck,
    TelegramChannel,
    TelegramMessage,
    Webcam,
)
from app.services.copernicus import is_configured as copernicus_is_configured
from app.services.eumetsat import is_configured as eumetsat_is_configured
from app.services.telegram import is_configured as telegram_is_configured
from app.services.webcams.registry import WEBCAM_SOURCES

router = APIRouter(prefix="/api/sources", tags=["sources"])


def _last_success_map(db: Session) -> dict[str, str]:
    rows = (
        db.query(SourceCheck.source_key, func.max(SourceCheck.checked_at))
        .filter(SourceCheck.status == "ok")
        .group_by(SourceCheck.source_key)
        .all()
    )
    return {key: checked_at.isoformat() for key, checked_at in rows}


def _satellite_entry(
    key: str, name: str, url: str, source_code: str, refresh_url: str, db: Session, last_success: dict
) -> dict:
    count = db.query(FireDetection).filter(FireDetection.source == source_code).count()
    # seconds_since_last_attempt is in-memory and resets on backend restart -
    # fall back to "has stored data" so status doesn't wrongly say "not yet
    # polled" right after a restart when the DB clearly has prior detections.
    seconds = state.seconds_since_last_attempt(key)
    status = "active" if (seconds is not None or count > 0) else "not yet polled"
    return {
        "key": key,
        "category": "satellite",
        "name": name,
        "url": url,
        "status": status,
        "detail": f"{count} detections stored",
        "refresh_url": refresh_url,
        "last_success_at": last_success.get(key),
    }


@router.get("")
def list_sources(db: Session = Depends(get_db)):
    """
    Self-authored directory of every source this app actually ingests -
    not scraped from any third-party site's own sources page. Mirrors the
    kind of overview mapasdeincendios.es/fuentes gives, scoped to what we
    ourselves pull data from. Each entry's refresh_url is what the status
    page's per-source "Refresh now" button POSTs to.
    """
    last_success = _last_success_map(db)

    sources: list[dict] = [
        _satellite_entry(
            "firms",
            "NASA FIRMS",
            "https://firms.modaps.eosdis.nasa.gov/api/",
            "FIRMS",
            "/api/fires/refresh/firms?force=true",
            db,
            last_success,
        ),
        _satellite_entry(
            "effis",
            "Copernicus EFFIS",
            "https://effis.jrc.ec.europa.eu/",
            "EFFIS",
            "/api/fires/refresh/effis?force=true",
            db,
            last_success,
        ),
    ]

    scene_count = db.query(SatelliteScene).count()
    copernicus_active = copernicus_is_configured()
    sources.append(
        {
            "key": "copernicus",
            "category": "satellite",
            "name": "Copernicus Data Space (Sentinel Hub Catalog)",
            "url": "https://dataspace.copernicus.eu/",
            "status": "active" if copernicus_active else "needs setup",
            "detail": f"{scene_count} scenes discovered across all incidents",
            "refresh_url": "/api/copernicus/discover-all",
            "last_success_at": last_success.get("copernicus"),
        }
    )

    eumetsat_count = db.query(FireDetection).filter(FireDetection.source == "EUMETSAT").count()
    eumetsat_configured = eumetsat_is_configured()
    eumetsat_seconds = state.seconds_since_last_attempt("eumetsat")
    eumetsat_status = (
        "needs setup"
        if not eumetsat_configured
        else "active" if (eumetsat_seconds is not None or eumetsat_count > 0) else "not yet polled"
    )
    sources.append(
        {
            "key": "eumetsat",
            "category": "satellite",
            "name": "EUMETSAT MTG Active Fire Monitoring",
            "url": "https://user.eumetsat.int/catalogue/EO:EUM:DAT:0682",
            "status": eumetsat_status,
            "detail": f"{eumetsat_count} detections stored",
            "refresh_url": "/api/fires/refresh/eumetsat?force=true",
            "last_success_at": last_success.get("eumetsat"),
        }
    )

    admin_sources = db.query(AdminSource).all()
    bulletin_counts = dict(
        db.query(AdminBulletin.source_id, func.count(AdminBulletin.id))
        .group_by(AdminBulletin.source_id)
        .all()
    )
    for admin_source in admin_sources:
        key = f"admin:{admin_source.region_code}"
        sources.append(
            {
                "key": key,
                "category": "administration",
                "name": admin_source.name,
                "url": admin_source.portal_url,
                "status": "active",
                "detail": f"{bulletin_counts.get(admin_source.id, 0)} bulletins discovered",
                "refresh_url": f"/api/admin-sources/{admin_source.region_code}/refresh",
                "last_success_at": last_success.get(key),
            }
        )

    telegram_channels = db.query(TelegramChannel).all()
    message_counts = dict(
        db.query(TelegramMessage.channel_id, func.count(TelegramMessage.id))
        .group_by(TelegramMessage.channel_id)
        .all()
    )
    telegram_active = telegram_is_configured()
    for channel in telegram_channels:
        key = f"telegram:{channel.username}"
        sources.append(
            {
                "key": key,
                "category": "telegram",
                "name": channel.display_name or f"@{channel.username}",
                "url": f"https://t.me/{channel.username}",
                "status": "active" if telegram_active and channel.is_active else "needs setup",
                "detail": f"{message_counts.get(channel.id, 0)} messages ingested",
                "refresh_url": f"/api/telegram/channels/{channel.id}/refresh",
                "last_success_at": last_success.get(key),
            }
        )

    regional_sources = db.query(RegionalIncidentSource).all()
    regional_counts = dict(
        db.query(RegionalIncident.source_id, func.count(RegionalIncident.id))
        .group_by(RegionalIncident.source_id)
        .all()
    )
    for regional_source in regional_sources:
        key = f"regional:{regional_source.region_code}"
        sources.append(
            {
                "key": key,
                "category": "regional-incidents",
                "name": regional_source.name,
                "url": regional_source.portal_url,
                "status": "active",
                "detail": f"{regional_counts.get(regional_source.id, 0)} fires tracked",
                "refresh_url": f"/api/regional-incidents/{regional_source.region_code}/refresh",
                "last_success_at": last_success.get(key),
            }
        )

    webcam_counts = dict(
        db.query(Webcam.source, func.count(Webcam.id)).group_by(Webcam.source).all()
    )
    for source_key, source in WEBCAM_SOURCES.items():
        key = f"webcams:{source_key}"
        sources.append(
            {
                "key": key,
                "category": "webcams",
                "name": source.name,
                "url": source.portal_url,
                "status": "active" if webcam_counts.get(source_key) else "not yet polled",
                "detail": f"{webcam_counts.get(source_key, 0)} cameras",
                "refresh_url": f"/api/webcams/{source_key}/refresh",
                "last_success_at": last_success.get(key),
            }
        )

    # UME: reference-only, no public API exists (confirmed 2026-07-15) - just
    # a news RSS feed with no structured per-fire fields, so it's listed as a
    # link-out rather than something we parse into incident events.
    sources.append(
        {
            "key": "ume",
            "category": "reference",
            "name": "UME (Unidad Militar de Emergencias)",
            "url": settings.ume_rss_url,
            "status": "reference only",
            "detail": "News RSS feed - no structured per-fire data available",
            "refresh_url": None,
            "last_success_at": last_success.get("ume"),
        }
    )

    return sources
