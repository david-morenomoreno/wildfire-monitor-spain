import asyncio
import json
import logging
import os
import re
from datetime import datetime

from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings
from app.models import FireIncident, IncidentEvent, TelegramChannel, TelegramMessage
from app.services.health import record_check

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session_string
    )


def normalize_channel_username(raw: str) -> str:
    """
    Accepts a bare username, an "@name", or a t.me link (with or without a
    trailing message id, e.g. "https://t.me/bomberosforestales/23831") and
    returns just the channel username.
    """
    raw = raw.strip()
    match = re.search(r"t\.me/([A-Za-z0-9_]+)", raw)
    if match:
        return match.group(1)
    return raw.lstrip("@")


def get_or_create_channel(db: Session, username: str, display_name: str | None = None) -> TelegramChannel:
    username = normalize_channel_username(username)
    channel = db.query(TelegramChannel).filter_by(username=username).first()
    if channel is None:
        channel = TelegramChannel(username=username, display_name=display_name)
        db.add(channel)
        db.commit()
        db.refresh(channel)
    return channel


def seed_default_channels(db: Session) -> None:
    """Registers configured channels (no polling) so they're visible/ready even before credentials exist."""
    for username in settings.telegram_seed_channels:
        get_or_create_channel(db, username)


# Locality-name matching alone is too broad on a channel that also covers
# labor/union news - "Jaén" or "Madrid" show up constantly with zero
# connection to a fire. Tested live against @bomberosforestales: locality-only
# matching flagged messages about union grievances and job postings just
# because they named a province. Requiring a fire-related keyword too cut
# that down to actual fire mentions.
FIRE_KEYWORDS = (
    "incendio",
    "incendios",
    "fuego",
    "llamas",
    "quema",
    "quemado",
    "conato",
    "wildfire",
)


def _match_incident(db: Session, text: str) -> int | None:
    """
    Best-effort link from a message to a FireIncident: requires BOTH a
    fire-related keyword AND a substring match on the incident's already
    -resolved locality name. Not exhaustive - unmatched messages are still
    stored and listable on their own.
    """
    if not text:
        return None
    lowered = text.lower()
    if not any(keyword in lowered for keyword in FIRE_KEYWORDS):
        return None
    incidents = db.query(FireIncident).filter(FireIncident.status != "archived").all()
    for incident in incidents:
        if incident.locality and incident.locality.lower() in lowered:
            return incident.id
    return None


async def _download_photo(client: TelegramClient, message, channel_username: str) -> str | None:
    """Downloads a message's photo (if any) to upload_dir. Returns just the filename, not a full path."""
    if not message.photo:
        return None
    try:
        os.makedirs(settings.upload_dir, exist_ok=True)
        filename = f"tg-{channel_username}-{message.id}.jpg"
        dest = os.path.join(settings.upload_dir, filename)
        saved = await client.download_media(message, file=dest)
        return filename if saved else None
    except Exception:
        logger.warning("Failed to download Telegram photo for message %s", message.id, exc_info=True)
        return None


async def _poll_channel_async(db: Session, client: TelegramClient, channel: TelegramChannel) -> int:
    messages = await client.get_messages(channel.username, min_id=channel.last_message_id, limit=200)
    count = 0
    # Telethon returns newest-first; process oldest-first so last_message_id
    # only advances after every older message in the batch is stored.
    for message in sorted(messages, key=lambda m: m.id):
        if not message.id or message.id <= channel.last_message_id:
            continue
        text = message.message or ""
        posted_at = message.date.replace(tzinfo=None) if message.date else datetime.utcnow()
        incident_id = _match_incident(db, text)
        media_path = await _download_photo(client, message, channel.username)
        db.add(
            TelegramMessage(
                channel_id=channel.id,
                message_id=message.id,
                posted_at=posted_at,
                text=text,
                media_path=media_path,
                raw_json=json.dumps({"id": message.id, "date": str(message.date)}),
                matched_incident_id=incident_id,
            )
        )
        if incident_id is not None and (text or media_path):
            db.add(
                IncidentEvent(
                    incident_id=incident_id,
                    occurred_at=posted_at,
                    event_type="telegram_message",
                    source=channel.username,
                    title=f"Mencionado en @{channel.username}",
                    description=text[:500] if text else None,
                    raw_data=json.dumps({"media_path": media_path}) if media_path else None,
                )
            )
        channel.last_message_id = message.id
        count += 1
    return count


async def _poll_channels_async(db: Session, channels: list[TelegramChannel]) -> dict[str, int]:
    results: dict[str, int] = {}
    async with TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    ) as client:
        for channel in channels:
            source_key = f"telegram:{channel.username}"
            try:
                count = await _poll_channel_async(db, client, channel)
                results[channel.username] = count
                record_check(db, source_key, "ok", f"{count} new messages")
            except Exception as exc:
                logger.exception("Telegram poll failed for channel '%s'", channel.username)
                results[channel.username] = 0
                record_check(db, source_key, "disrupted", str(exc))
    return results


def poll_channel_now(db: Session, channel: TelegramChannel) -> int:
    """
    Single-channel poll for the manual "refresh" endpoint. Uses asyncio.run()
    rather than Telethon's `sync` wrapper - that wrapper relies on an
    implicit per-thread event loop that doesn't exist in FastAPI's worker
    threads (or APScheduler's), which surfaces as "no current event loop in
    thread ...". asyncio.run() creates and tears down its own loop, so it's
    safe to call from any thread regardless of prior loop state.
    """
    results = asyncio.run(_poll_channels_async(db, [channel]))
    db.commit()
    return results.get(channel.username, 0)


def poll_all_channels(db: Session) -> dict[str, int]:
    channels = db.query(TelegramChannel).filter_by(is_active=True).all()
    if not is_configured():
        logger.info("Telegram not configured (missing api_id/api_hash/session) - skipping poll")
        for channel in channels:
            record_check(db, f"telegram:{channel.username}", "skipped", "Telegram not configured")
        return {}
    if not channels:
        return {}
    results = asyncio.run(_poll_channels_async(db, channels))
    db.commit()
    return results
