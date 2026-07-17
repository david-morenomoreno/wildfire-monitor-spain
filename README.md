# Wildfire Monitor Spain

Real-time wildfire monitoring for Spain: satellite fire detections plotted on a map,
plus a crowd-sourced report channel for on-the-ground photos (stubbed in for now, in
place of a future Twitter/X `#IF-location` listener).

## Stack

- **backend/** — FastAPI + SQLAlchemy + Postgres. Polls NASA FIRMS and EFFIS on a
  schedule, exposes a REST API.
- **frontend/** — Node/Express serving a Leaflet map that consumes the backend API.
- **db** — Postgres 16 (plain, no PostGIS — lat/lon stored as floats; migrate to
  PostGIS later if you need real spatial queries). `fire_detections` also has
  `geometry_geojson`/`area_ha` columns for EFFIS burnt-area polygons (null for FIRMS points).

## Data sources

| Source | Status | Notes |
|---|---|---|
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/) | Live | Free, needs a `FIRMS_MAP_KEY` (request one at the link above, takes seconds). Queries **all 4** satellite sources (`app/config.py: firms_sources` - VIIRS S-NPP/NOAA-20/NOAA-21 + MODIS), matching what NASA's own FIRMS map overlays - querying only one satellite misses real fires (confirmed 2026-07-13: SNPP alone returned 0 rows for Spain while NOAA-20 had 111). Uses the bbox/area endpoint (`/api/area/csv/...`), not the country-code endpoint - FIRMS' country endpoint returns "Invalid API call" for every country as of 2026-07. FIRMS also caps `day_range` at 5 per request; the map's "last N days" filter can show more by relying on data already accumulated in the DB from earlier scheduled polls. |
| [EFFIS](https://effis.jrc.ec.europa.eu/) (Copernicus) | Live, experimental | No API key required, but it's a raw WFS/geoserver endpoint with no stability guarantees. As of 2026-07-13, JRC's own fire layers (`ercc.*`, including `ercc.ba` used here) are down server-side with an Oracle connection error — confirmed by testing an unrelated layer (`admin.countries`), which fails identically. This is an outage on their infrastructure, not a bug in this integration; retry later or watch https://effis.jrc.ec.europa.eu/ for status. |
| Twitter/X `#IF-location` photos | **Stubbed** | X's free API tier can't search recent tweets by hashtag anymore - confirmed 2026-07-13 that X also removed the free embedded search/hashtag *widget* (`publish.x.com/oembed` 404s for both `/search` and `/hashtag` URLs; only single-tweet and profile-timeline embeds still work). The map's "Search on X" link just opens a real search in a new tab. `POST /api/reports` accepts the same shape of data (location, image, notes) a Twitter listener - or a human pasting a real tweet URL they found - would submit; wire it to X's paid API v2 later if you get access. |
| Regional admin bulletins (e.g. JCyL) | Live | Generic framework (`app/services/admin_bulletins/`) - one `AdminBulletinSource` subclass per region, registered in `registry.py`. Discovers linked PDF/CSV documents on a region's index page and best-effort parses tables out of them with `pdfplumber`. Most regional portals (confirmed for JCyL) publish periodic *aggregate* statistics per province, not a live per-fire feed - bulletins that don't contain an extractable table are still stored as linked reference documents (`row_count`/`parsed_at` stay null). |
| Telegram channels (e.g. `t.me/bomberosforestales`) | Live | Polls public channels via Telethon and best-effort links messages to a `FireIncident` when the text has BOTH a fire-related keyword (`incendio`, `fuego`, ...) and a known locality name - locality name alone was too broad (confirmed live: `@bomberosforestales` is mostly forestry-firefighter labor/union news, so "Jaén" or "Madrid" show up constantly with no fire connection). Requires a one-time interactive login to generate a session string - see "Telegram setup" below. Without credentials, channels can still be registered (`POST /api/telegram/channels`) but polling is skipped, not an error. |
| [Copernicus Data Space](https://dataspace.copernicus.eu/) (Sentinel Hub Catalog + Process APIs) | Live | For each active incident, the **Catalog API** (`app/services/copernicus.py`) searches for Sentinel-2 scenes over a small bbox around its centroid during its detection date range, and stores matches (scene id, capture time, cloud cover) - this part is discovery only, it doesn't render pixels; confirmed live that a scene's `assets` only include a non-browsable `s3://eodata/...` path, no thumbnail. A real true-color thumbnail is rendered **lazily** the first time it's viewed, via the separate **Process API** (`GET /api/copernicus/scenes/{id}/thumbnail`) - not automatically for all discovered scenes, since rendering costs processing quota; once rendered it's cached to disk and served instantly after that. The STAC item's own `self` link (`item_url` in `GET /api/copernicus/scenes`) requires a Bearer token and 401s on a plain click, so it's stored for API consumers but **not** rendered as a clickable link in the UI. Needs an OAuth client - see "Copernicus setup" below. Without credentials, discovery is skipped, not an error. |
| Regional live incident feeds (Castilla y León/INCYL, Andalucía/INFOCA, Catalunya/Bombers) | Live | Three regions confirmed with a genuinely live, public, unauthenticated per-fire operational feed - status, location, and deployed-resource counts, not a periodic bulletin. **JCyL** (`https://servicios.jcyl.es/incyl/json/emergencias`): status (`Activo`/`Controlado`/`Extinguido`/`Falsa Alarma`), every deployed resource individually (brigades, aircraft, bulldozers), coordinates as **UTM** (misleadingly named `latitud`/`longitud` fields, zone varies per record via a `huso` field). **INFOCA/Andalucía** (an ArcGIS hosted feature service, found via the official dashboard's own item config, not guessed): status (`ACTIVO`/`CONTROLADO`/`EXTINGUIDO`), pre-aggregated resource counts per category, coordinates in **Web Mercator**. **Bombers/Catalunya** (another ArcGIS feature service, embedded in their Experience Builder app config): fire phase (`Controlat`/`Extingit`/...), a vehicle count, coordinates in **ETRS89/UTM zone 31N** - this is an all-hazards layer filtered to `TAL_COD_ALARMA1='IV'` (wildfire/vegetation-fire code), and its underlying view returns duplicate rows for the same incident within a single query (deduped before upserting). All three reproject via `pyproj` (each verified against real known towns, not just structurally-plausible numbers) and best-effort match to a `FireIncident` by centroid proximity; status changes append to that incident's timeline. Surveyed ~10 other regions (Galicia, Comunidad Valenciana, Extremadura, Aragón, Murcia, Castilla-La Mancha, País Vasco, Asturias, Cantabria) - none have a public live incident feed (see `app/services/regional_incidents/registry.py` to add one once/if a real endpoint is confirmed - don't guess one in). |
| [UME](https://ume.defensa.gob.es/) (Unidad Militar de Emergencias) | Reference only | No public API exists (confirmed 2026-07-15) - just an RSS feed of press-style news headlines with no structured per-fire fields (personnel/aircraft counts only ever appear as prose, e.g. in Twitter/X posts or per-year PDF reports). Listed in `GET /api/sources` as a link-out only; not parsed into incident events. |
| [DGT](https://infocar.dgt.es/etraffic/) traffic cameras (webcams) | Live | A real, public, unauthenticated DATEX II v3.7 feed (`app/services/webcams/dgt.py`) of ~1,900 traffic cameras nationwide, each with exact coordinates and a direct live JPEG snapshot URL (`fse:deviceUrl`) - confirmed by downloading and viewing a real, current, timestamped road image. Toggle "DGT traffic cameras nearby" on the map to see pins in the current viewport (loaded per-bbox, not all ~1,900 at once); click one for a Windy-style popup with the live snapshot plus a scrollable strip of nearby cameras you can click through without losing your place. Camera list itself barely changes day to day (`webcams_interval_minutes`, default 12h) - the images are fetched live and directly by the browser, never polled/stored by us. Investigated Pyronear (self-host-only, Apache-2.0, France-centric with one Catalonia pilot - no public feed) and Pyro.es/BseedWATCH (commercial, dashboard requires login, no public API) as alternative camera networks - neither has a legitimately consumable public feed today. |

## Getting started

1. Get a free FIRMS API key: https://firms.modaps.eosdis.nasa.gov/api/
2. `cp .env.example .env` and fill in `FIRMS_MAP_KEY`
3. `docker compose up --build`
4. Backend API docs: http://localhost:8000/docs
5. Map UI: http://localhost:3000

The backend polls FIRMS/EFFIS automatically every `FETCH_INTERVAL_MINUTES` (default 180 = 3h,
matching real VIIRS/MODIS NRT revisit cadence), or trigger it manually from the map UI's
"Refresh hotspots" / "Refresh burnt areas" buttons - these share the same cooldown as the
scheduler (see Refresh cooldown below), so clicking them repeatedly doesn't burn through
FIRMS'/EFFIS' request quota for no new data.

## Map features

- **Date-range filter**: show detections from the last 1/3/7/14/30 days. This filters
  what's already in the DB - it's independent from how far back a single FIRMS/EFFIS
  refresh pulls.
- **Recency color scale**: hotspots are shaded red (most recent in the selected window)
  to yellow (oldest in the window).
- **No source/model shown on the map**: the map deliberately doesn't distinguish FIRMS
  vs EFFIS vs satellite instrument visually - by design, so the map reads as "here's
  what's burning," not "here's which sensor said so." Internally the backend still needs
  the distinction (point vs polygon rendering), it's just not exposed as a legend/color.
- **Hotspot clustering by density**: nearby point detections are grouped into one circle
  whose radius scales with the cluster's point count - a dense cluster (bigger/more active
  fire) reads as a visibly bigger circle than an isolated single detection. Grid size shrinks
  as you zoom in, so a close-up view separates out individual hotspots.
- **Estimated fire-area polygons**: hotspots are also grouped by real-world proximity
  (chain-linkage, ~3km, independent of zoom/grid) into one region per contiguous fire event.
  Each region with 3+ detections gets ONE dashed polygon - not one per grid cell, which used
  to fragment into overlapping shapes that ate clicks meant for markers. The outline is a
  **concave hull** (`hull.js`, `CONCAVE_HULL_CONCAVITY_DEG` in `app.js`), not a convex one -
  a convex hull always bulges out to the extreme points and overstates area for
  irregularly-shaped spreads; the concave version hugs the actual hotspot shape, with a
  convex-hull fallback if hull.js ever degenerates on a particular point set.
  Clearly labeled as an estimate from hotspot spread, not an official EFFIS perimeter.
- **Location + hashtag lookup**: click a region polygon and hit "Get location & hashtag" to
  reverse-geocode its centroid (via OpenStreetMap Nominatim, cached in the `locality_cache`
  table so repeat lookups don't hit their ~1 req/sec rate limit) and get a `#IF<Locality>`
  hashtag - matching the convention Spanish/Catalan forestry agencies (e.g. Agents Rurals)
  actually use on X, like `#IFAiguamúrcia`. Includes a "Search on X" link for that hashtag.
- **Burnt-area perimeters**: when EFFIS returns polygon geometry (`ercc.ba` burnt-area
  features), the map draws the actual perimeter shape instead of a single point, with
  affected area (ha) in the popup - closer to how Copernicus/EFFIS present past fires.
  FIRMS only ever gives point hotspots (active detections), so this only applies to EFFIS.
- **Satellite imagery basemap**: toggle "Satellite imagery (NASA GIBS)" and pick a date
  to overlay real MODIS true-color imagery under the markers, similar to the
  Copernicus/USGS reference imagery. Free, no API key. Defaults to 2 days ago since
  GIBS "best available" imagery often lags by a day or two; tiles cap out natively at
  zoom 9 and are upsampled beyond that.

## Refresh cooldown

Manual refreshes (from `/sources.html`'s "Refresh now" buttons, or directly via
`POST /api/fires/refresh/...`) are throttled server-side (in `app/state.py`) to the same
interval as the scheduler (`FETCH_INTERVAL_MINUTES`) for FIRMS/EFFIS specifically. If already
fetched within that window - whether by the scheduler or a previous manual click - a refresh
request returns immediately with `skipped: true` instead of hitting the external API again.
The status page's buttons always pass `?force=true` since a deliberate manual click on an ops
page should just run now. Cooldown state is in-memory and resets on backend restart.

Manual data refresh used to live as buttons on the map itself - it's moved to `/sources.html`
now, next to each source's health history and last-success time, so triggering a refresh and
seeing whether it actually worked are in the same place. The map's own "Reload map" button
only re-reads what's already in our DB (`/api/fires`, `/api/incidents`) - it doesn't hit any
external API.

## API

- `GET /api/fires?source=FIRMS&hours=72` — recent fire detections (also drives the map's date-range filter, 1/3/7/14/30 days)
- `POST /api/fires/refresh/firms?days=5&force=false` — poll FIRMS (`days` capped at 5; skipped if within cooldown unless `force=true`)
- `POST /api/fires/refresh/effis?force=false` — poll EFFIS (skipped if within cooldown unless `force=true`)
- `GET /api/reports` — list user-submitted reports
- `POST /api/reports` (multipart form: `hashtag_location`, `latitude`, `longitude`, `notes`, `image`) — submit a report
- `GET /api/geocode?lat=&lon=` — reverse-geocode a point to a locality name + `#IF<Locality>` hashtag (cached in `locality_cache`)
- `GET /api/incidents?status=&hours=&sort=severity` — fires clustered server-side, ranked by severity (drives the sidebar); `hours` matches the map's date-range filter
- `GET /api/incidents/{id}` / `GET /api/incidents/{id}/timeline?hours=` — incident detail and its event timeline; `hours` filters events the same way (omit for full history)
- `GET /api/admin-sources` / `GET /api/admin-sources/{region}/bulletins` — regional bulletin directory
- `POST /api/admin-sources/{region}/refresh` — re-scrape one region's bulletins now
- `GET /api/telegram/channels` / `POST /api/telegram/channels` (`{"username": "..."}`, accepts a bare name, `@name`, or a `t.me/...` link) — manage polled channels
- `POST /api/telegram/channels/{id}/refresh` — poll one channel now (400s if Telegram isn't configured yet)
- `GET /api/telegram/messages?channel=&incident_id=` — list ingested messages, optionally filtered
  (each includes `media_path` when the message had a photo - fetch the image at `GET /media/<media_path>`)
- `GET /api/sources` — directory of every source currently ingested (satellite/administration/telegram), with live status, `last_success_at`, and the `refresh_url` the status page's "Refresh now" buttons use
- `GET /api/health?days=14` — per-source day-by-day ingestion history (`ok`/`degraded`/`disrupted`/`skipped`), worst status of the day wins
- `GET /api/copernicus/scenes?incident_id=` — Sentinel scenes discovered for one incident
- `POST /api/copernicus/discover/{incident_id}` / `POST /api/copernicus/discover-all` — run discovery now (400s if Copernicus isn't configured yet)
- `GET /api/copernicus/scenes/{id}/thumbnail` — true-color JPEG, rendered via the Process API on first request and cached to disk after that
- `GET /api/regional-incidents/sources` / `GET /api/regional-incidents?region=&status=` — regional live-incident feeds and the fires they report
- `POST /api/regional-incidents/{region}/refresh` — re-fetch one region's live status now
- `GET /api/webcams?bbox=minLon,minLat,maxLon,maxLat` — cameras within map bounds (bbox required to avoid dumping ~1,900 rows at once)
- `GET /api/webcams/nearby?lat=&lon=&exclude_id=&limit=6` — nearest cameras to a point (the popup's "nearby" thumbnail strip)
- `POST /api/webcams/{source}/refresh` — re-fetch a provider's camera list now
- `GET /api/fire-spread/predict?lat=&lon=&max_hours=` — experimental POC: hourly spread ellipses (up to 24h) from an origin point, driven by the hour-by-hour wind forecast (see "Fire spread prediction" below)

## Data sources & status page

`/sources.html` (linked from the map's "Data sources" panel, and the "Sources" tab) is a merged
catalog + health page: a searchable, category-grouped directory of every source (was a separate
`/status.html` originally - merged since both pages were really about the same sources, just
different views of them). Each card shows its own AWS-style 14-day health strip inline - one dot
per day, colored by the worst outcome that day. Every ingestion
path - FIRMS, EFFIS, each admin-bulletin region, each Telegram channel - records a
`SourceCheck` row on every attempt (scheduled *or* manual refresh, since the check is recorded
inside the shared ingest function itself, not duplicated per call site):

- **ok** — succeeded (0 new rows is still ok; that's normal, not every poll finds something new)
- **degraded** — succeeded but with partial issues (e.g. some admin bulletins failed to fetch/parse)
- **disrupted** — the whole attempt failed (exception raised)
- **skipped** — intentionally not run (currently only Telegram when credentials aren't configured yet)

This already caught a real live issue while building it: Copernicus EFFIS's WFS endpoint was
returning HTTP 400 at the time, which showed up immediately as a red cell for today.

## Telegram media on the map

Map polygons are matched to their backend `FireIncident` (by centroid proximity) so a
polygon's popup can show Telegram mentions/photos for that fire and a "View fire timeline →"
button into the same event timeline the sidebar uses - the polygon doubles as a thread of
everything known about that fire (detections, admin bulletins, Telegram posts), not just
satellite hotspots. Downloaded Telegram photos and (once wired up) `/api/reports` image
uploads are both served from `settings.upload_dir` via the `/media/<filename>` static mount.

## Telegram setup

Telegram's login needs a real phone number + a code sent to your app, so this can't be
automated non-interactively. One-time setup:

1. Get an `api_id`/`api_hash` from https://my.telegram.org (API development tools) - free, instant.
2. Run the helper script and follow the prompts (phone number, login code, 2FA password if enabled):
   ```
   docker compose run --rm backend python scripts/generate_telegram_session.py
   ```
3. Add the three values it prints to `.env`: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
   `TELEGRAM_SESSION_STRING`. Treat the session string like a password - anyone with it
   can act as your Telegram account.
4. `docker compose up -d --build backend` - polling starts automatically
   (`TELEGRAM_POLL_INTERVAL_MINUTES`, default 15) for every registered, active channel.

Channels can be registered any time via `POST /api/telegram/channels` even before credentials
are set - `bomberosforestales` is seeded by default (`TELEGRAM_SEED_CHANNELS` in
`app/config.py`). Messages are best-effort linked to a `FireIncident` when their text mentions
a locality name the incident already resolved to; unmatched messages are still stored and
listable via `GET /api/telegram/messages`.

## Copernicus setup

Copernicus Data Space uses OAuth2 `client_credentials` - unlike Telegram, there's no
interactive login step once you have a client, just a one-time dashboard action:

1. Log in at https://shapps.dataspace.copernicus.eu/dashboard/, go to **User Settings** ->
   **OAuth clients** -> **Create**. Save the `client_id`/`client_secret` shown - the secret
   can't be retrieved again afterward.
2. Add both to `.env`: `COPERNICUS_CLIENT_ID`, `COPERNICUS_CLIENT_SECRET`.
3. `docker compose up -d --build backend` - discovery starts automatically
   (`COPERNICUS_DISCOVERY_INTERVAL_MINUTES`, default 720 = 12h, since Sentinel-2 only
   revisits every ~5 days) for every non-archived incident, or trigger it now from
   `/sources.html`'s "Refresh now" button on the Copernicus row.

Without credentials, discovery is skipped (recorded as `skipped` on the status page, not an
error) rather than failing. Discovery searches a fixed ~0.1° box (`COPERNICUS_BBOX_PADDING_DEG`)
around each incident's centroid - incidents only store a centroid, not a full extent, so this
is a padding radius rather than a computed bounding box from the actual hotspot spread. Found
scenes appear as `event_type="satellite_imagery"` entries in the incident timeline with a
real true-color thumbnail - the `<img>` tag points at
`GET /api/copernicus/scenes/{id}/thumbnail`, which renders via the Process API on first
request (costs processing quota) and caches the result to `settings.upload_dir`, so every
request after the first is served straight from disk. Scenes themselves (metadata, without
triggering a render) are listable via `GET /api/copernicus/scenes?incident_id=`.

## Regional live-incident feeds

`app/services/regional_incidents/` mirrors `admin_bulletins/`'s plugin pattern (one
`RegionalIncidentSource` subclass per region, registered in `registry.py`) but for genuinely
live per-fire operational data instead of periodic documents. Currently only
`jcyl.py` (Castilla y León's INCYL) is implemented - it's the only region where a real public
endpoint was confirmed after surveying ~10 others (see the data sources table above). Adding
another region means: confirm a real working endpoint first (don't guess one, verify like this
one was - check the page's JS bundle for embedded API calls, or an ArcGIS
`arcgis/rest/services` URL, or a downloadable open-data layer), then add a subclass that
returns `RegionalFireRecord`s and register it.

Two things worth knowing if you touch this:
- **Coordinates aren't always lat/lon.** JCyL's `latitud`/`longitud` fields are actually UTM
  easting/northing (ETRS89, zone given by a `huso` field) - reprojected via `pyproj`
  (`EPSG:2582<huso>` → `EPSG:4326`), verified against a real known town's coordinates, not just
  "the numbers look like coordinates." Don't assume another region's feed uses the same
  convention without checking.
- **Personnel/resource counts aren't pre-aggregated** - JCyL gives a raw list of every deployed
  unit (`medios[]`, each with a category and whether it's currently active); the count shown in
  the timeline (`_personnel_description` in `sync.py`) is derived by filtering/counting that
  list, not a field the API hands you directly.

## Fire spread prediction (experimental POC)

`app/services/fire_spread.py` + `GET /api/fire-spread/predict?lat=&lon=&max_hours=`: given a
clicked origin point, estimates the affected area for **every hour of the wind forecast, up to
24h**, using the **elliptical fire growth model** (Huygens' wavelet principle) that Canada's FBP
System and FARSITE both use - wind speed sets the ellipse's length-to-breadth ratio
(`LB = 1.0 + 0.0012 * W^2.155`, Van Wagner 1969/Alexander 1985), which splits one base rate of
spread into head/flank/back rates (eccentricity-based split, same family of formulas). The
wind-speed multiplier on the base rate itself reuses the Canadian FWI System's own ISI wind
function (`exp(0.05039 * W)`, Van Wagner 1987) - real, cited constants, not invented ones.

Unlike the original single-snapshot version, this now pulls Open-Meteo's **hourly forecast**
(not just the current reading) and accumulates spread hour by hour: each hour computes its own
head/flank/back rate of spread from *that hour's* forecast wind speed, adds `rate * 60` to a
running cumulative distance, and the overall spread bearing is a rate-of-spread-weighted vector
average of every hour's downwind direction so far - so the ellipse can visibly bend over the
24h window if the forecast wind shifts, instead of being frozen in one direction the whole time.
This is still a simplification: it's one growing ellipse whose axis can reorient, not a true
multi-point Huygens' wavelet perimeter (which would need polygon-union geometry across many
ignition points along the fire edge) - a reasonable stand-in for a POC, not a claim of matching
FARSITE's actual perimeter propagation.

**This is explicitly a POC, not an operational tool** - said plainly in the API response's own
`disclaimer` field and in the UI. What it's missing relative to a real fire behavior model
(Rothermel-grade): no calibrated fuel model (fuel load, depth, moisture-of-extinction by
type), no fuel moisture at all, no fire weather index, no consideration of fire suppression
in progress. The base rate of spread is a rough per-vegetation-type lookup table
(`FUEL_TABLE` in `fire_spread.py`), not a physical model. Slope and fuel type are still sampled
once at the origin (not resampled as the fire front advances) - a deliberate POC-level tradeoff.

Three free, keyless, live-verified data sources feed it:
- **Wind** (speed + direction): [Open-Meteo](https://api.open-meteo.com/) hourly forecast, up
  to 24 hours starting from the current UTC hour (`fetch_wind_series`).
- **Vegetation/fuel proxy**: [Corine Land Cover 2018](https://land.copernicus.eu/) via the
  EEA's own ArcGIS `identify` endpoint - mapped to a rough fuel category, not a real fuel model.
- **Slope**: [Open-Elevation](https://www.open-elevation.com/) - two-point sample (origin +
  ~300m downwind) rather than a proper DEM/raster (Copernicus GLO-30 DEM has no simple point-
  query API, just raw tiles - not worth the complexity for a POC).

On the map: click "Place origin" then click anywhere - draws the current-hour ellipse plus a
**time slider** (Windy-style scrubber, +1h through +24h) that redraws the ellipse and its
info panel (that hour's wind/fuel/slope/ROS/cumulative distance) as you drag, all from a single
fetch - no per-hour API round-trips. Also added an "Outdoor / topographic" basemap option
(OpenTopoMap, free, no key) alongside the plain OpenStreetMap one, closer to Windy's own default
map style and genuinely useful for reading terrain/slope context around a fire.

## Proximity alerts (experimental POC)

`app/services/proximity.py` + `GET /api/proximity/check?lat=&lon=`: reuses the fire spread
model above, but automatically, for real active incidents rather than a manually-placed origin.
Given a point (the browser's own geolocation), it finds every `active` `FireIncident` within
~30km, runs `predict_spread` for each, and checks whether the point falls inside that incident's
predicted footprint at any hour up to 24h - if so, returns the incident and the earliest hour at
which the prediction reaches it.

This is Phase 1 of a two-phase design (explicitly chosen this way, not a shortcut): an **in-app
alert** using the browser's own Geolocation + Notification APIs while the tab is open, not a real
push-notification backend. That means no user accounts, no server-side subscription storage, and
no delivery when the tab/browser is closed - the frontend just polls `/api/proximity/check`
periodically (see `frontend/public/app.js`'s location-alerts code) while the user has opted in,
and fires a browser `Notification` locally if a match comes back. A real push-notification
version (Phase 2) would need a service worker, VAPID keys, a stored per-user push subscription,
and a background job on this side matching every subscribed location against active predictions
- deliberately deferred; this endpoint's shape (incident id, distance, hours-until-reach) is
designed to work for either delivery mechanism without changing.

Same caveats as the manual fire-spread tool apply - this is a POC growth model, not a calibrated
evacuation-planning tool. Runs live per request (not cached/pre-computed), so a request checking
several nearby incidents can take a few seconds - fine for an occasional poll, not for tight
loops.

## Schema changes

There's no migration tool (Alembic etc.) yet - `Base.metadata.create_all()` on startup only
creates tables that don't exist, it never alters an existing table. If you add/rename a column
on a model that's already been created in your running Postgres volume, you'll need to `ALTER
TABLE` by hand (or `docker compose down -v` to wipe the dev DB and let it recreate from
scratch) - otherwise inserts will fail with `UndefinedColumn`. Worth adding Alembic before this
schema churns much more.

## Next steps to consider

- Swap the manual `/api/reports` submission for a real X API v2 listener once you have
  paid API access.
- Add PostGIS if you need radius/polygon queries ("fires within 20km of X").
- Add auth before exposing this publicly — right now `/api/reports` accepts anonymous
  uploads with no validation beyond file type inference.
- Add more `AdminBulletinSource` regions beyond JCyL (`app/services/admin_bulletins/registry.py`).
- Improve Telegram→incident matching beyond locality-name substring search (e.g. fuzzy
  matching, or extracting coordinates/place names with an NLP pass).
