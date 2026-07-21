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

    # EUMETSAT MTG/FCI Active Fire Monitoring (geostationary, ~10-min full-disk
    # cadence) - complements FIRMS/EFFIS's polar-orbiting few-passes-a-day
    # coverage. OAuth2 client_credentials via HTTP Basic, free account at
    # https://api.eumetsat.int/api-key/. Ingestion is skipped (not an error)
    # when either credential is blank - see services/eumetsat.py.
    eumetsat_consumer_key: str = ""
    eumetsat_consumer_secret: str = ""
    eumetsat_token_url: str = "https://api.eumetsat.int/token"
    eumetsat_search_url: str = "https://api.eumetsat.int/data/search-products/1.0.0/os"
    # "Active Fire Monitoring (netCDF) - MTG - 0 degree" - confirmed LIVE
    # (2026-07-18) to return real per-cycle products via the search API.
    # MSG's older equivalent (EO:EUM:DAT:MSG:FIRC / FRP-SEVIRI, the ID shown
    # on EUMETSAT's own product page) returns "Collection not found" against
    # the Data Store - MTG has operationally superseded it.
    eumetsat_collection_id: str = "EO:EUM:DAT:0682"
    # Wider than the poll interval itself so a slow/failed previous poll
    # doesn't leave a gap of missed products uncovered.
    eumetsat_lookback_minutes: int = 30
    eumetsat_poll_interval_minutes: int = 15
    # Fallback only: used when fire_result has no flag_meanings/flag_values CF
    # attributes to read the "fire" category names from directly (see
    # _fire_flag_values in eumetsat.py) - comma-separated raw integer codes to
    # treat as a fire detection. 0/1/2/3 = no fire/low/mid/high confidence per
    # EUMETSAT's own "MTG-FCI: ATBD for Active Fire Monitoring Product"
    # (EUM/MTG/DOC/10/0613 v2A, Table 4) - so 1,2,3 here is CONFIRMED, not a
    # guess. A real full-disk product also showed a 5th code, 4, covering
    # ~91% of the grid (vs. a few hundred pixels each for 1/2/3) - that's not
    # in the ATBD's fire-type table at all and is excluded here on purpose;
    # it almost certainly covers pixels the algorithm doesn't process (sea,
    # bare soil, sun-glint, beyond the ~70deg satellite-zenith radius - see
    # the ATBD's section 3.5 prerequisites), not a fire class. Including it
    # previously (old default "3,4") matched ~28M pixels and made a single
    # product take effectively forever to geolocate+insert.
    eumetsat_fire_result_fallback_values: str = "1,2,3"

    # Sentinel-3 SLSTR Fire Radiative Power (FRP) - polar-orbiting (Sentinel-3A
    # + -3B, a DIFFERENT satellite pair from FIRMS' own VIIRS/MODIS), served
    # through the same EUMETSAT Data Store account as eumetsat_consumer_key/
    # secret above - no separate registration needed. Confirmed LIVE
    # (2026-07-19): a single pass detected the Guadalajara/La Mierla megafire
    # with 100s of fire pixels at the same time a third-party tool (Pyrofire)
    # showed a dense hotspot cluster there, at a moment when EUMETSAT's own
    # MTG product (eumetsat_collection_id above) found nothing nearby - a
    # genuinely complementary source, not a duplicate.
    sentinel3_collection_id: str = "EO:EUM:DAT:0417"
    # Sentinel-3's per-pass revisit over any one spot is only ~1-4x/day (2
    # satellites, not continuous like MTG) - wider lookback than the poll
    # interval so a slow/failed previous poll doesn't leave a product
    # uncovered, same reasoning as eumetsat_lookback_minutes.
    sentinel3_lookback_minutes: int = 240
    sentinel3_poll_interval_minutes: int = 60
    # EUMETSAT's own confidence_class flag (0=lower,1=nominal,2=higher) is
    # coarser than this - filtering on the underlying confidence(%) directly
    # gives finer control. 70% chosen to match the ballpark of FIRMS' own
    # "nominal" VIIRS confidence tier; UNVERIFIED against a false-positive
    # rate study, adjust if Sentinel-3 detections look noisier than FIRMS'.
    sentinel3_min_confidence_pct: float = 70.0

    # Copernicus EMS Rapid Mapping - official, analyst-produced fire-extent
    # delineation maps. No auth needed - confirmed LIVE (2026-07-21): plain
    # unauthenticated JSON, standard DRF pagination (count/next/previous/
    # results), and `category`/`country` (singular) query params both work
    # and AND together server-side (e.g. ?category=Wildfire&country=Spain
    # narrowed 233 total activations to 19). `centroid` is returned as a WKT
    # string "POINT (lon lat)", not a coordinate array or GeoJSON.
    copernicus_ems_api_url: str = (
        "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/"
    )
    # Per-activation detail endpoint (?code=EMSRxxx) - confirmed LIVE
    # (2026-07-21): includes `reason` (analyst's own incident description),
    # `activator`, `reportLink` (a public ArcGIS StoryMap - the closest thing
    # to a rendered map this API offers), and a top-level `stats` object
    # (population/roads/built-up area affected). Per-product `images`/
    # `layers` files are raw full-resolution GeoTIFFs (confirmed via a real
    # byte fetch - TIFF, ~40MB), NOT browser-displayable thumbnails, so
    # those are deliberately not fetched/rendered here.
    copernicus_ems_detail_url: str = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/"
    # Activations are analyst-produced over hours-to-days, not minutes, and
    # Spain gets maybe 0-15 wildfire activations/year even in a severe season
    # - daily polling is plenty, no need for satellite-cadence checking.
    copernicus_ems_interval_minutes: int = 1440
    # Degrees around an activation's centroid to match against an existing
    # FireIncident - matches INCIDENT_REASSOCIATION_DEG's ballpark (~16.7km)
    # since an EMS analyst's centroid and this app's own FIRMS-derived
    # centroid for the same real fire won't be pixel-identical.
    copernicus_ems_match_deg: float = 0.2

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
