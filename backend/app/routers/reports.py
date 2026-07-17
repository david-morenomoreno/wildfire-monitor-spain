import os
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import UserReport
from app.schemas import UserReportOut

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[UserReportOut])
def list_reports(db: Session = Depends(get_db)):
    return db.query(UserReport).order_by(UserReport.reported_at.desc()).all()


@router.post("", response_model=UserReportOut)
def create_report(
    hashtag_location: str | None = Form(None),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    notes: str | None = Form(None),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """
    Manual/simulated stand-in for real-time Twitter ingestion.

    Live X/Twitter hashtag scanning (e.g. #IF-location) requires a paid X API
    tier to search recent posts, so this endpoint lets you submit a report the
    same way an automated Twitter listener eventually would - once that's
    wired up, it can call this same endpoint instead of a human.
    """
    image_path = None
    if image is not None:
        os.makedirs(settings.upload_dir, exist_ok=True)
        extension = os.path.splitext(image.filename or "")[1] or ".jpg"
        filename = f"{uuid.uuid4().hex}{extension}"
        image_path = os.path.join(settings.upload_dir, filename)
        with open(image_path, "wb") as f:
            f.write(image.file.read())

    report = UserReport(
        source="manual",
        hashtag_location=hashtag_location,
        latitude=latitude,
        longitude=longitude,
        notes=notes,
        image_path=image_path,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
