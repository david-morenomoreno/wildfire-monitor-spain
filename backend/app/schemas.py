from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _utc_iso(dt: datetime) -> str:
    """
    Every datetime this app stores is a naive UTC instant (datetime.utcnow()
    throughout the ingestion code), but naive datetime.isoformat() has no
    timezone marker - the frontend's `new Date(...)` then parses it as LOCAL
    time, not UTC, silently shifting every displayed timestamp by the
    browser's UTC offset (confirmed live: ~2h off in Madrid's CEST). This
    marks the value as UTC explicitly at the serialization boundary, with no
    DB/column changes needed since the underlying instant was always UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


UTCDatetime = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str)]


class FireDetectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    latitude: float
    longitude: float
    confidence: Optional[str] = None
    brightness: Optional[float] = None
    acquired_at: UTCDatetime
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
    # Manual override set via PATCH /api/incidents/{id} - see models.FireIncident.
    # Takes priority over `locality` wherever this incident's name is displayed.
    official_name: Optional[str] = None
    country_code: Optional[str] = None
    status: str
    severity_score: float
    risk_level: str
    detection_count: int
    area_ha: Optional[float] = None
    # Best-effort concave-hull estimate over this incident's own detection
    # points (see services/area_estimate.py), computed only when area_ha
    # itself is null - i.e. no EFFIS burnt-area detection ever matched this
    # incident, which is most of them. Always None when area_ha is set:
    # EFFIS's own reported figure is authoritative and never gets an
    # estimate layered alongside it. Populated by the rankings/report
    # endpoints only (see routers/incidents.py); other endpoints leave it
    # None rather than pay for the extra query on every list/detail call.
    area_ha_estimated: Optional[float] = None
    first_detected_at: UTCDatetime
    last_detected_at: UTCDatetime
    updated_at: UTCDatetime
    # Computed in the router (not real columns) from IncidentEvent event
    # types present for this incident, so the frontend can filter "satellite
    # only" vs "has official status" vs "has Telegram mentions" without a
    # timeline fetch per incident.
    has_regional_status: bool = False
    has_telegram_mentions: bool = False
    has_satellite_imagery: bool = False
    has_ems_activation: bool = False


class IncidentRenameRequest(BaseModel):
    # Nullable so the same endpoint can also be used to CLEAR an override
    # (fall back to the reverse-geocoded locality again) by passing null.
    official_name: Optional[str] = None


class IncidentMergeRequest(BaseModel):
    incident_ids: list[int]
    # Which of incident_ids survives and absorbs the others - defaults to
    # the one with the most detections (see routers/incidents.py) when omitted.
    survivor_id: Optional[int] = None
    # Optional - set the survivor's official_name as part of the same request
    # (e.g. merge the trio AND name the result "IF Los Gallardos" in one step).
    official_name: Optional[str] = None


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
    occurred_at: UTCDatetime
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
    fetched_at: UTCDatetime
    parsed_at: Optional[UTCDatetime] = None
    row_count: Optional[int] = None


class TelegramChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: Optional[str] = None
    last_message_id: int
    is_active: bool
    added_at: UTCDatetime


class TelegramChannelCreate(BaseModel):
    username: str  # bare username, "@name", or a t.me link
    display_name: Optional[str] = None


class TelegramMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    message_id: int
    posted_at: UTCDatetime
    text: Optional[str] = None
    media_path: Optional[str] = None
    matched_incident_id: Optional[int] = None


class SatelliteSceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    collection: str
    scene_id: str
    captured_at: UTCDatetime
    cloud_cover: Optional[float] = None
    thumbnail_url: Optional[str] = None
    item_url: Optional[str] = None
    discovered_at: UTCDatetime


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
    started_at: Optional[UTCDatetime] = None
    controlled_at: Optional[UTCDatetime] = None
    extinguished_at: Optional[UTCDatetime] = None
    area_ha: Optional[float] = None
    cause: Optional[str] = None
    personnel_summary: Optional[str] = None
    matched_incident_id: Optional[int] = None
    updated_at: UTCDatetime


class IncidentDetectionSourceCount(BaseModel):
    source: str
    count: int


class LandUseHectares(BaseModel):
    label: str
    hectares: float


class IncidentVegetationOut(BaseModel):
    """
    Structured form of get_incident_vegetation_stats (services/copernicus_ems.py) -
    only ever populated from an official Copernicus EMS Rapid Mapping
    activation, so this is None for the vast majority of incidents (see that
    function's docstring for why 0-15 activations/year, not a routine feed).
    """

    top_land_use: list[LandUseHectares] = []
    burnt_area_ha: Optional[float] = None
    active_flames: Optional[int] = None
    population_affected: Optional[int] = None

    @classmethod
    def from_stats(cls, stats: dict) -> "IncidentVegetationOut":
        """stats is the raw dict get_incident_vegetation_stats returns -
        top_land_use there is a list of (label, hectares) tuples, not
        already-labeled objects, since that function is shared with the
        plain-prose timeline description (_format_product_stats_summary)."""
        return cls(
            top_land_use=[LandUseHectares(label=label, hectares=ha) for label, ha in stats["top_land_use"]],
            burnt_area_ha=stats["burnt_area_ha"],
            active_flames=stats["active_flames"],
            population_affected=stats["population_affected"],
        )


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
    # Closed ring of (lat, lon) vertices tracing this incident's estimated
    # extent (see area_estimate.estimate_area_and_hull) - only populated when
    # there's no official EFFIS area_ha AND enough detections to trace a
    # meaningful shape from. None means "draw a centroid marker instead",
    # never a fabricated outline.
    estimated_hull: list[tuple[float, float]] | None = None
    vegetation: Optional[IncidentVegetationOut] = None


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
    updated_at: UTCDatetime


class AircraftOut(BaseModel):
    # Not `from_attributes` - services/aircraft.py returns plain dicts (live,
    # never persisted), same as fetch_windy_webcams' WebcamOut-shaped dicts.
    hex: Optional[str] = None
    flight: Optional[str] = None
    aircraft_type: Optional[str] = None
    altitude_ft: Optional[float] = None
    ground_speed_kt: Optional[float] = None
    track_deg: Optional[float] = None
    latitude: float
    longitude: float
    category: Optional[str] = None


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
    reported_at: UTCDatetime
    notes: Optional[str] = None
