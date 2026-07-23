# Wildfire Monitor Spain

Real-time wildfire monitoring platform for Spain.

It consolidates satellite detections, regional operational feeds, and incident context into one map-first interface.

## Overview

Wildfire Monitor Spain helps you:

- visualize recent wildfire detections on a live map,
- aggregate multi-source incident signals into timelines,
- track source health and ingestion quality,
- experiment with spread/proximity projections for situational awareness.

## Architecture

```text
frontend (Node/Express + Leaflet)
        |
        v
backend (FastAPI + SQLAlchemy)
        |
        v
postgres (detections, incidents, source checks)
```

### Project structure

- `backend/` — APIs, ingestion jobs, source adapters, incident logic.
- `frontend/` — map UI, source/status pages.
- `docker-compose.yml` — local full-stack orchestration.

## Data sources

| Source | Status | Notes |
|---|---|---|
| NASA FIRMS | Live | Requires `FIRMS_MAP_KEY`; queries multiple VIIRS/MODIS sources via bbox endpoint. |
| EFFIS (Copernicus) | Live, experimental | Public WFS availability can vary by upstream status. |
| Regional live incident feeds | Live | Implemented: Castilla y León (INCYL), Andalucía (INFOCA), Catalunya (Bombers). |
| Regional admin bulletins | Live | Plugin-based parser framework under `app/services/admin_bulletins/`. |
| Telegram channels | Live (optional) | Requires Telethon session setup. |
| Copernicus Data Space | Live (optional) | OAuth client for scene discovery and lazy thumbnail rendering. |
| DGT webcams | Live | Public camera feed integrated with map overlays/nearby context. |
| Twitter/X hashtag ingestion | Stubbed | Listener/search integration not active on free tier. |

## Features

- Date-range filter (1/3/7/14/30 days) for detections and incidents.
- Density clustering + estimated fire-area concave hull polygons.
- Reverse geocoding helper for locality + `#IF<Locality>`.
- Incident timeline with multi-source events.
- Source catalog with 14-day status strip (`ok`, `degraded`, `disrupted`, `skipped`).
- Optional Copernicus Sentinel scene discovery + cached thumbnails.
- Experimental fire spread projection and proximity checks.
- DGT webcams by viewport + nearby strip in popups.

## Quick start

1. Request a free FIRMS API key: https://firms.modaps.eosdis.nasa.gov/api/
2. Copy environment template:

   ```bash
   cp .env.example .env
   ```

3. Set at least:

   ```env
   FIRMS_MAP_KEY=your_key_here
   ```

   Note: `.env` is gitignored and safe for secrets.

4. Start services:

   ```bash
   docker compose up --build
   ```

5. Open:
   - API docs: http://localhost:8000/docs
   - Map UI: http://localhost:3000

## Configuration

### Required

- `FIRMS_MAP_KEY`

### Core optional

- `FIRMS_BBOX`
- `FETCH_INTERVAL_MINUTES`

### Optional integrations

- Telegram: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`
- Copernicus: `COPERNICUS_CLIENT_ID`, `COPERNICUS_CLIENT_SECRET`

> `.env` is ignored by git. Keep all secrets there.

## Optional integration setup

### Telegram

1. Create credentials at https://my.telegram.org
2. Generate session:

   ```bash
   docker compose run --rm backend python scripts/generate_telegram_session.py
   ```

3. Add values to `.env`
4. Restart backend:

   ```bash
   docker compose up -d --build backend
   ```

### Copernicus Data Space

1. Create OAuth client at https://shapps.dataspace.copernicus.eu/dashboard/
2. Add client credentials to `.env`
3. Restart backend:

   ```bash
   docker compose up -d --build backend
   ```

## API overview

### Core

- `GET /api/fires?hours=72`
- `POST /api/fires/refresh/firms?days=5&force=false`
- `POST /api/fires/refresh/effis?force=false`
- `GET /api/incidents?status=&hours=&sort=severity`
- `GET /api/incidents/{id}`
- `GET /api/incidents/{id}/timeline?hours=`

### Reports and geocode

- `GET /api/reports`
- `POST /api/reports` (multipart)
- `GET /api/geocode?lat=&lon=`

### Sources and integrations

- `GET /api/sources`
- `GET /api/health?days=14`
- `GET/POST /api/telegram/channels`
- `GET /api/telegram/messages`
- `GET /api/copernicus/scenes?incident_id=`
- `POST /api/copernicus/discover/{incident_id}`
- `GET /api/webcams?bbox=minLon,minLat,maxLon,maxLat`

### Experimental

- `GET /api/fire-spread/predict?lat=&lon=&max_hours=`
- `GET /api/proximity/check?lat=&lon=`

## Source health model

Each ingestion attempt records a source check:

- `ok` — success (including zero new rows)
- `degraded` — success with partial issues
- `disrupted` — full failure
- `skipped` — intentionally not run (for example, missing optional credentials)

Used by `/sources.html` as a source directory + status timeline.

## Operational notes

- Manual refreshes use the same server-side cooldown as scheduled polling.
- Cooldown state is in-memory and resets on backend restart.
- Media is served under `/media/...` from backend upload storage.
- Schema changes are managed by Alembic - see "Database migrations" below.
  `Base.metadata.create_all()` still runs on startup as a legacy safety net
  for brand-new tables, but it never adds columns to an existing table, so
  Alembic is the actual source of truth going forward.

## Database migrations

Schema changes are managed with [Alembic](https://alembic.sqlalchemy.org/),
configured under `backend/alembic/`. `backend/alembic/env.py` imports the
app's own `app.config.settings.database_url` and `app.database.Base` directly
- there's no separate DB URL to keep in sync in `alembic.ini`, and
`--autogenerate` compares directly against the live model definitions in
`app/models.py`.

**After changing a model** (adding a column, table, index, etc.) in
`app/models.py`, generate a migration from `backend/`:

```bash
cd backend
alembic revision --autogenerate -m "add official_name to fire_incidents"
```

Always read the generated file under `backend/alembic/versions/` before
applying it - autogenerate is a good first draft, not a guarantee (it can
miss things like renames, and needs manual review for e.g. `NOT NULL` columns
added to a table that already has rows).

**To apply migrations** (creates any tables/columns that don't exist yet):

```bash
cd backend
alembic upgrade head
```

This runs automatically only where you invoke it - it is deliberately *not*
wired into the app's own startup, so a bad migration doesn't take the app
down on deploy. Run it as an explicit deploy step.

**One-time production adoption:** prod's schema already matches the current
models (including `official_name` and `copernicus_ems_activations`, added
manually before Alembic existed), so do **not** run the baseline migration
there - it would try to `CREATE TABLE` things that already exist. Instead,
tell Alembic prod is already at that revision:

```bash
cd backend
alembic stamp head
```

Run that once against prod. From then on, `alembic upgrade head` there
behaves normally for every migration generated after this one.

## Disclaimer

Fire spread and proximity endpoints are **experimental POC features** and not operational emergency decision tools.

## Publish to GitHub

Suggested repository name: **`wildfire-monitor-spain`**

If you created an **empty** GitHub repository:

```bash
# from the project root
git init
git add .
git commit -m "feat: initial project setup"
git branch -M main
git remote add origin https://github.com/david-morenomoreno/wildfire-monitor-spain.git
git push -u origin main
```

If the GitHub repository was created with initial files (README/license):

```bash
git pull origin main --allow-unrelated-histories
git push -u origin main
```

## Next steps

- Add Alembic migrations.
- Improve Telegram-to-incident matching (NLP/geo extraction).
- Add authentication and stricter upload controls before public deployment.
- Extend regional source adapters as new verified public feeds become available.
