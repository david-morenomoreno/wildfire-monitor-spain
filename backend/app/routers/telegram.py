from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import TelegramChannel, TelegramMessage
from app.schemas import TelegramChannelCreate, TelegramChannelOut, TelegramMessageOut
from app.services.telegram import get_or_create_channel, is_configured, poll_channel_now

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.get("/channels", response_model=list[TelegramChannelOut])
def list_channels(db: Session = Depends(get_db)):
    return db.query(TelegramChannel).all()


@router.post("/channels", response_model=TelegramChannelOut)
def add_channel(payload: TelegramChannelCreate, db: Session = Depends(get_db)):
    return get_or_create_channel(db, payload.username, payload.display_name)


@router.post("/channels/{channel_id}/refresh")
def refresh_channel(channel_id: int, db: Session = Depends(get_db)):
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Telegram is not configured (missing TELEGRAM_API_ID/API_HASH/SESSION_STRING)",
        )
    channel = db.query(TelegramChannel).filter_by(id=channel_id).first()
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    try:
        count = poll_channel_now(db, channel)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram poll failed: {exc}") from exc
    return {"channel": channel.username, "new_messages": count}


@router.get("/messages", response_model=list[TelegramMessageOut])
def list_messages(
    channel: Optional[str] = Query(None, description="Filter by channel username"),
    incident_id: Optional[int] = Query(None, description="Filter by matched incident id"),
    db: Session = Depends(get_db),
):
    query = db.query(TelegramMessage)
    if channel:
        channel_row = db.query(TelegramChannel).filter_by(username=channel).first()
        if channel_row is None:
            return []
        query = query.filter(TelegramMessage.channel_id == channel_row.id)
    if incident_id is not None:
        query = query.filter(TelegramMessage.matched_incident_id == incident_id)
    return query.order_by(TelegramMessage.posted_at.desc()).all()
