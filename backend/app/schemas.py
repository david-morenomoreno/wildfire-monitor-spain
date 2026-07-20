from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class FireDetectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    latitude: float
    longitude: float
    confidence: Optional[str] = None
    brightness: Optional[float] = None
    acquired_at: datetime
    geometry_geojson: Optional[str] = None
    area_ha: Optional[float] = None


class FireIncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    centroid_lat: float
    centroid_lon: float
    province: Optional[str] = None
    locality: Optional[str] = None
    country_code: Optional[str] = None
    status: str
    severity_score: float
    risk_level: str
    detection_count: int
    area_ha: Optional[float] = None
    first_detected_at: datetime
    last_detected_at: datetime
    updated_at: datetime
    # Computed in the router (not real columns) from IncidentEvent event
    # types present for this incident, so the frontend can filter "satellite
    # only" vs "has official status" vs "has Telegram mentions" without a
    # timeline fetch per incident.
    has_regional_status: bool = False
    has_telegram_mentions: bool = False
    has_satellite_imagery: bool = False


class RankedIncidentOut(FireIncidentOut):
    # Added on top of FireIncidentOut purely for the rankings view - position
    # within the requested sort/window, and duration as a ready-to-render
    # number (first/last_detected_at are already on the base model, but the
    # frontend shouldn't have to redo this arithmetic for every row).
    rank: int
    duration_hours: float


class IncidentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    occurred_at: datetime
    event_type: str
    source: Optional[str] = None
    title: str
    description: Optional[str] = None
    raw_data: Optional[str] = None


class AdminSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region_code: str
    name: str
    portal_url: str


class AdminBulletinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    title: str
    file_url: str
    file_type: str
    fetched_at: datetime
    parsed_at: Optional[datetime] = None
    row_count: Optional[int] = None


class TelegramChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: Optional[str] = None
    last_message_id: int
    is_active: bool
    added_at: datetime


class TelegramChannelCreate(BaseModel):
    username: str  # bare username, "@name", or a t.me link
    display_name: Optional[str] = None


class TelegramMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    message_id: int
    posted_at: datetime
    text: Optional[str] = None
    media_path: Optional[str] = None
    matched_incident_id: Optional[int] = None


class SatelliteSceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    collection: str
    scene_id: str
    captured_at: datetime
    cloud_cover: Optional[float] = None
    thumbnail_url: Optional[str] = None
    item_url: Optional[str] = None
    discovered_at: datetime


class RegionalIncidentSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region_code: str
    name: str
    portal_url: str


class RegionalIncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    external_id: str
    status: str
    municipality: Optional[str] = None
    province: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    started_at: Optional[datetime] = None
    controlled_at: Optional[datetime] = None
    extinguished_at: Optional[datetime] = None
    area_ha: Optional[float] = None
    cause: Optional[str] = None
    personnel_summary: Optional[str] = None
    matched_incident_id: Optional[int] = None
    updated_at: datetime


class IncidentDetectionSourceCount(BaseModel):
    source: str
    count: int


class IncidentReportOut(BaseModel):
    """
    Everything this app tracks about one FireIncident, assembled server-side
    so the frontend's per-incident report page can render a full dossier from
    a single request instead of the 5-6 separate calls the map sidebar makes
    lazily (timeline, regional status, satellite scenes, Telegram mentions).
    """

    incident: FireIncidentOut
    duration_hours: float
    timeline: list[IncidentEventOut] = []
    regional_status: list[RegionalIncidentOut] = []
    satellite_scenes: list[SatelliteSceneOut] = []
    telegram_messages: list[TelegramMessageOut] = []
    # Best-effort - see _detection_source_breakdown in routers/incidents.py
    # for why this is a proximity re-query rather than a stored FK.
    detection_sources: list[IncidentDetectionSourceCount] = []


class WebcamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    external_id: str
    name: Optional[str] = None
    road: Optional[str] = None
    province: Optional[str] = None
    latitude: float
    longitude: float
    image_url: str
    updated_at: datetime


class UserReportCreate(BaseModel):
    source: str = "manual"
    external_ref: Optional[str] = None
    hashtag_location: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = None


class UserReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    external_ref: Optional[str] = None
    hashtag_location: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image_path: Optional[str] = None
    reported_at: datetime
    notes: Optional[str] = None
