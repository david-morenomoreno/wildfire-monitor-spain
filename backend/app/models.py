from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


class FireDetection(Base):
    """A single satellite fire hotspot detection from FIRMS or EFFIS."""

    __tablename__ = "fire_detections"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_source_external_id"),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False)  # "FIRMS", "EFFIS", "EUMETSAT", or "SENTINEL3"
    external_id = Column(String(120), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    confidence = Column(String(20), nullable=True)
    brightness = Column(Float, nullable=True)
    acquired_at = Column(DateTime, nullable=False)
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    raw_properties = Column(Text, nullable=True)

    # Populated for burnt-area (EFFIS) features: the full perimeter polygon
    # as GeoJSON geometry, and the affected surface area. FIRMS point
    # detections leave these null - lat/lon is the actual location for those.
    geometry_geojson = Column(Text, nullable=True)
    area_ha = Column(Float, nullable=True)


class LocalityCache(Base):
    """
    Reverse-geocoding results (Nominatim), cached by rounded lat/lon so
    repeated lookups near the same spot don't re-hit Nominatim's rate-limited
    free API (max ~1 req/sec, no bulk use per their usage policy).
    """

    __tablename__ = "locality_cache"
    __table_args__ = (
        UniqueConstraint("lat_rounded", "lon_rounded", name="uq_lat_lon_rounded"),
    )

    id = Column(Integer, primary_key=True)
    lat_rounded = Column(Float, nullable=False)
    lon_rounded = Column(Float, nullable=False)
    locality_name = Column(String(255), nullable=False)
    province = Column(String(255), nullable=True)
    country_code = Column(String(2), nullable=True)  # ISO 3166-1 alpha-2, e.g. "ES" - from Nominatim
    hashtag = Column(String(255), nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FireIncident(Base):
    """
    A stable, server-side identity for a real-world fire event - built by
    clustering nearby FireDetection rows (same proximity logic the frontend
    used to do transiently in the browser). Exists so a fire can be sorted,
    ranked, and have a timeline, and so later sources (admin bulletins,
    Telegram) have something to attach to besides raw lat/lon points.
    """

    __tablename__ = "fire_incidents"

    id = Column(Integer, primary_key=True)
    slug = Column(String(64), nullable=False, unique=True)
    centroid_lat = Column(Float, nullable=False)
    centroid_lon = Column(Float, nullable=False)
    province = Column(String(255), nullable=True)
    locality = Column(String(255), nullable=True)
    country_code = Column(String(2), nullable=True)  # ISO 3166-1 alpha-2, e.g. "ES" - from Nominatim
    status = Column(String(20), nullable=False, default="active")  # active/cooling/archived
    severity_score = Column(Float, nullable=False, default=0.0)
    risk_level = Column(String(20), nullable=False, default="low")  # low/moderate/high/critical
    detection_count = Column(Integer, nullable=False, default=0)
    area_ha = Column(Float, nullable=True)
    first_detected_at = Column(DateTime, nullable=False)
    last_detected_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class IncidentEvent(Base):
    """
    One entry in a FireIncident's timeline. event_type is deliberately open
    ended - Phase 1 only produces "detection"/"status_change" rows, later
    ingestors (admin bulletins, Telegram) append "admin_bulletin"/
    "telegram_message" rows to the same table so the timeline UI doesn't
    need per-source plumbing.
    """

    __tablename__ = "incident_events"

    id = Column(Integer, primary_key=True)
    incident_id = Column(Integer, ForeignKey("fire_incidents.id"), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    event_type = Column(String(30), nullable=False)
    source = Column(String(30), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    raw_data = Column(Text, nullable=True)


class AdminSource(Base):
    """A regional public-administration portal that publishes fire bulletins (PDF/CSV)."""

    __tablename__ = "admin_sources"

    id = Column(Integer, primary_key=True)
    region_code = Column(String(30), nullable=False, unique=True)  # e.g. "jcyl"
    name = Column(String(255), nullable=False)
    portal_url = Column(String(500), nullable=False)
    is_active = Column(String(10), nullable=False, default="true")


class AdminBulletin(Base):
    """
    A single discovered document (PDF/CSV) from an AdminSource. Many regional
    portals only publish periodic aggregate statistics rather than a clean
    per-fire table, so `row_count`/`parsed_at` stay null when the document is
    only useful as a linked reference - structured extraction is best-effort,
    not guaranteed for every bulletin.
    """

    __tablename__ = "admin_bulletins"
    __table_args__ = (
        UniqueConstraint("source_id", "file_url", name="uq_source_file_url"),
    )

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("admin_sources.id"), nullable=False)
    title = Column(String(500), nullable=False)
    file_url = Column(String(1000), nullable=False)
    file_type = Column(String(10), nullable=False)  # "pdf" or "csv"
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    parsed_at = Column(DateTime, nullable=True)
    row_count = Column(Integer, nullable=True)


class TelegramChannel(Base):
    """A public Telegram channel/group polled for fire-related messages."""

    __tablename__ = "telegram_channels"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, unique=True)  # without the leading @ or t.me/
    display_name = Column(String(255), nullable=True)
    last_message_id = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TelegramMessage(Base):
    """
    A single message pulled from a TelegramChannel. matched_incident_id is a
    best-effort link (text mentions a FireIncident's known locality) - left
    null when nothing matches, the message is still stored and listable.
    """

    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", name="uq_channel_message_id"),
    )

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("telegram_channels.id"), nullable=False)
    message_id = Column(Integer, nullable=False)
    posted_at = Column(DateTime, nullable=False)
    text = Column(Text, nullable=True)
    media_path = Column(String(500), nullable=True)  # filename under settings.upload_dir, served at /media/<name>
    raw_json = Column(Text, nullable=True)
    matched_incident_id = Column(Integer, ForeignKey("fire_incidents.id"), nullable=True)
    ingested_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SourceCheck(Base):
    """
    One ingestion attempt's outcome for a given source - "ok" (succeeded),
    "degraded" (succeeded with issues), "disrupted" (failed), or "skipped"
    (intentionally not run, e.g. Telegram not configured yet). Powers the
    AWS-style health grid at GET /api/health; source_key matches the same
    keys used in GET /api/sources (e.g. "firms", "admin:jcyl",
    "telegram:bomberosforestales").
    """

    __tablename__ = "source_checks"

    id = Column(Integer, primary_key=True)
    source_key = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)  # ok/degraded/disrupted/skipped
    message = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SatelliteScene(Base):
    """
    A Sentinel-1/2 scene discovered (via the Copernicus Data Space Sentinel
    Hub Catalog API) covering a FireIncident's area during its active date
    range. The Catalog API itself is discovery-only (finds what imagery
    exists, doesn't render pixels), so this stores metadata immediately; a
    true-color thumbnail is rendered lazily via the Process API (a separate
    call, costs processing quota) the first time it's actually requested -
    see thumbnail_path and GET /api/copernicus/scenes/{id}/thumbnail.
    """

    __tablename__ = "satellite_scenes"
    __table_args__ = (
        UniqueConstraint("incident_id", "collection", "scene_id", name="uq_incident_collection_scene"),
    )

    id = Column(Integer, primary_key=True)
    incident_id = Column(Integer, ForeignKey("fire_incidents.id"), nullable=False)
    collection = Column(String(50), nullable=False)  # e.g. "sentinel-2-l2a"
    scene_id = Column(String(255), nullable=False)
    captured_at = Column(DateTime, nullable=False)
    cloud_cover = Column(Float, nullable=True)
    # Confirmed against the live Catalog API (2026-07-14): responses only
    # include an "data" asset (an s3://eodata/... path, not browser-viewable),
    # no "thumbnail" asset - so this stays null for sentinel-2-l2a today, kept
    # for forward-compat if that changes. item_url (the STAC "self" link) is
    # the actually-useful reference this API does provide.
    thumbnail_url = Column(String(1000), nullable=True)
    item_url = Column(String(1000), nullable=True)
    # Filename under settings.upload_dir once rendered via the Process API -
    # null until first requested (rendering costs processing quota, so it's
    # lazy rather than automatic for every discovered scene).
    thumbnail_path = Column(String(500), nullable=True)
    discovered_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class RegionalIncidentSource(Base):
    """
    A regional government's live per-fire operational status feed (as opposed
    to AdminSource, which is periodic PDF/CSV bulletins) - e.g. Castilla y
    León's INCYL system. One row per region_code, same convention as
    AdminSource.
    """

    __tablename__ = "regional_incident_sources"

    id = Column(Integer, primary_key=True)
    region_code = Column(String(30), nullable=False, unique=True)  # e.g. "jcyl"
    name = Column(String(255), nullable=False)
    portal_url = Column(String(500), nullable=False)


class RegionalIncident(Base):
    """
    A single fire's live official status from a RegionalIncidentSource -
    status (active/controlled/extinguished), personnel/resources deployed,
    and (where the source provides real coordinates, unlike satellite
    hotspots) an authoritative location. Best-effort linked to a FireIncident
    by proximity, same pattern as Telegram's locality-name matching.
    """

    __tablename__ = "regional_incidents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_regional_source_external_id"),
    )

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("regional_incident_sources.id"), nullable=False)
    external_id = Column(String(100), nullable=False)  # e.g. "BU-9-254-26"
    status = Column(String(50), nullable=False)  # region's own label, e.g. "Activo"/"Controlado"/"Extinguido"
    municipality = Column(String(255), nullable=True)
    province = Column(String(255), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    started_at = Column(DateTime, nullable=True)
    controlled_at = Column(DateTime, nullable=True)
    extinguished_at = Column(DateTime, nullable=True)
    area_ha = Column(Float, nullable=True)
    cause = Column(String(255), nullable=True)
    personnel_summary = Column(Text, nullable=True)  # JSON: counts of deployed resources by category
    matched_incident_id = Column(Integer, ForeignKey("fire_incidents.id"), nullable=True)
    raw_json = Column(Text, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Webcam(Base):
    """
    A publicly-viewable camera (currently DGT traffic cameras) with a known
    location, so nearby ones can be shown on the map like Windy's webcam
    layer - "what does the area near this fire actually look like right now."
    image_url is a direct link to the provider's own live snapshot (no proxy
    needed - <img> tags aren't subject to CORS the way fetch/canvas reads
    are), so this table only stores metadata, never the image itself.
    """

    __tablename__ = "webcams"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_webcam_source_external_id"),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False)  # "dgt" (more providers later, e.g. "windy")
    external_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=True)
    road = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    image_url = Column(String(1000), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class UserReport(Base):
    """A crowd-sourced fire report, e.g. from an X/Twitter #IF-location post."""

    __tablename__ = "user_reports"

    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False, default="manual")  # "manual" or "twitter"
    external_ref = Column(String(255), nullable=True)  # tweet URL/ID once wired up
    hashtag_location = Column(String(255), nullable=True)  # raw #IF-location text
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    image_path = Column(String(500), nullable=True)
    reported_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    notes = Column(Text, nullable=True)
