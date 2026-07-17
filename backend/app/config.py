from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://wildfire:wildfire@db:5432/wildfire"
    firms_map_key: str = ""
    # FIRMS' country-code endpoint (/api/country/csv/...) returns "Invalid API call"
    # even for well-known codes as of 2026-07 - use the bbox-based area endpoint instead.
    # Covers mainland Spain + Balearics; Canary Islands (~-18,27 to -13,29) are outside
    # this box and would need a second query if you need that coverage.
    firms_bbox: str = "-9.5,35.9,4.4,43.9"
    # NASA's own FIRMS map (firms.modaps.eosdis.nasa.gov) overlays ALL of these
    # satellites. Querying only VIIRS_SNPP_NRT misses real fires - confirmed by
    # comparing row counts on 2026-07-13: SNPP=0, NOAA20=111, NOAA21=68, MODIS=11
    # for the same day/bbox, because each satellite's orbit passes over Spain at
    # different times and SNPP's pass can simply miss a detection NOAA-20/21 caught.
    firms_sources: list[str] = [
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
        "MODIS_NRT",
    ]
    firms_day_range: int = 1
    effis_wfs_url: str = (
        "https://ies-ows.jrc.ec.europa.eu/effis/ows"
        "?service=WFS&version=2.0.0&request=GetFeature"
        "&typeName=ms:ercc.ba&outputFormat=application/json"
    )
    # 3h matches the real revisit cadence of combined VIIRS/MODIS NRT passes.
    fetch_interval_minutes: int = 180
    # Admin bulletins are published at most daily (often less) - polling their
    # index pages every 3h like the satellite sources would just be wasted load.
    admin_bulletins_interval_minutes: int = 720
    upload_dir: str = "/data/uploads"

    # Telegram (Telethon) - all three required to actually poll; polling is
    # skipped (not an error) when any is blank, since a session string can't
    # be obtained non-interactively (needs a phone/2FA login) - see README.
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_string: str = ""
    telegram_poll_interval_minutes: int = 15
    # Channels to auto-register (without polling) even before credentials are
    # set, so they're ready to go the moment a session string is added.
    telegram_seed_channels: list[str] = ["bomberosforestales"]

    # Copernicus Data Space Ecosystem (Sentinel Hub Catalog API) - OAuth2
    # client_credentials, created at https://shapps.dataspace.copernicus.eu/dashboard/
    # under "User Settings" -> OAuth clients. Discovery is skipped (not an
    # error) when either is blank - see README.
    copernicus_client_id: str = ""
    copernicus_client_secret: str = ""
    copernicus_token_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    copernicus_catalog_url: str = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
    copernicus_collections: list[str] = ["sentinel-2-l2a"]
    # Sentinel-2 revisits every ~5 days; polling more often than that just
    # re-finds the same scenes.
    copernicus_discovery_interval_minutes: int = 720
    # Degrees around an incident's centroid to search (~0.1 deg =~ 11km) -
    # incidents only store a centroid, not a full extent, so this is a fixed
    # padding rather than a computed bounding box.
    copernicus_bbox_padding_deg: float = 0.1
    # Confirmed live (2026-07-14) against a real render - CDSE's Process API
    # lives on the same sh.dataspace.copernicus.eu host as Catalog, NOT
    # services.sentinel-hub.com (the generic Sentinel Hub product's host that
    # most example docs show).
    copernicus_process_url: str = "https://sh.dataspace.copernicus.eu/api/v1/process"
    # 720px renders noticeably sharper in the lightbox (which stretches the
    # image up to ~90vw/90vh) - 300px was visibly blurry once enlarged.
    copernicus_thumbnail_size: int = 720
    # True-color (B04/B03/B02) evalscript, 2.5x gain to roughly match how
    # Sentinel-2 imagery looks on Copernicus Browser - verified against a real
    # render, not just copied from docs unverified.
    copernicus_evalscript: str = (
        'function setup() { return { input: ["B02","B03","B04"], output: { bands: 3 } }; }\n'
        "function evaluatePixel(s) { return [2.5*s.B04, 2.5*s.B03, 2.5*s.B02]; }"
    )

    # Regional live-incident feeds (e.g. Castilla y León's INCYL) - unlike
    # admin_bulletins (periodic PDF/CSV documents), these are near-real-time
    # per-fire operational status, so poll closer to the satellite cadence.
    regional_incidents_interval_minutes: int = 60

    # UME (Unidad Militar de Emergencias) - reference-only: no public API
    # exists (confirmed 2026-07-15), just an RSS feed of press-style news
    # headlines with no structured per-fire fields, so this is surfaced in
    # GET /api/sources as a link-out, not parsed into incident events.
    ume_rss_url: str = "https://ume.defensa.gob.es/comun/RSSUME.xml"
    ume_portal_url: str = "https://ume.defensa.gob.es/"

    # DGT traffic cameras (webcams) - the camera *list* barely changes day to
    # day, only the live snapshot images do (fetched directly by the browser,
    # not polled by us) - so this just needs to be occasional, not frequent.
    webcams_interval_minutes: int = 720
    windy_api_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
