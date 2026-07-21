"""
Copernicus EMS Rapid Mapping - official, analyst-produced fire-extent
delineation/grading maps. Unlike EFFIS's automated burnt-area detection, an
"activation" can only be triggered by an authorized body (Spain's Protección
Civil, or EU-level monitoring via EFFIS/GDACS alerts) for events serious
enough to warrant national civil-protection escalation - so this is a rare
"officially confirmed by the EU" marker on major incidents, not a routine
feed (confirmed live 2026-07-21: Spain gets roughly 0-15 wildfire activations
a year, spiking in severe seasons).

Confirmed LIVE (2026-07-21) against the real (undocumented, but public)
dashboard-backend API:
  - No auth needed - plain unauthenticated JSON.
  - Standard DRF pagination: {count, next, previous, results[]}. `next` is a
    complete, directly-fetchable URL - no param merging needed.
  - `category` and `country` (singular - NOT `countries`) query params both
    work server-side and AND together (e.g. ?category=Wildfire&country=Spain
    narrowed 233 total activations down to 19).
  - `centroid` is a WKT string "POINT (lon lat)" - NOT a coordinate array or
    GeoJSON geometry like this project's other sources.
  - `countries` (plural, in the response body) is a list of full country
    names ("Spain"), not ISO codes.

This only builds the timeline-surfacing MVP the app actually needs today:
polling the list endpoint and announcing a match on the incident's timeline.
It deliberately does NOT fetch/parse the per-activation detail endpoint's
delineation-product ZIP (shapefile/geopackage of the actual burnt-area
polygon) - that's a real follow-up (rendering an official extent polygon
alongside the FIRMS/EFFIS-derived hull), but out of scope until asked for.
"""

import json
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import CopernicusEmsActivation, FireIncident, IncidentEvent
from app.services.health import record_check

logger = logging.getLogger(__name__)

_WKT_POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")


def _parse_centroid(wkt: str | None) -> tuple[float, float] | None:
    """Parses EMS's "POINT (lon lat)" WKT string into (lat, lon)."""
    if not wkt:
        return None
    match = _WKT_POINT_RE.match(wkt.strip())
    if not match:
        return None
    lon, lat = float(match.group(1)), float(match.group(2))
    return lat, lon


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # eventTime/activationTime have no timezone suffix; lastUpdate has
        # microsecond precision - fromisoformat handles both as-is.
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def fetch_wildfire_activations(country: str = "Spain") -> list[dict]:
    """Fetches every Wildfire-category activation for a country, following pagination."""
    results: list[dict] = []
    url: str | None = settings.copernicus_ems_api_url
    params: dict | None = {"category": "Wildfire", "country": country}
    while url:
        response = httpx.get(url, params=params, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
        results.extend(payload.get("results", []))
        url = payload.get("next")
        params = None  # `next` already has query params baked in
    return results


def _find_matching_incident(db: Session, lat: float, lon: float) -> FireIncident | None:
    """
    Nearest FireIncident within copernicus_ems_match_deg, searched across ALL
    incidents regardless of status - an EMS activation can reference a fire
    that's since cooled/archived, and matching is about "is this the same
    real-world fire", not "is it still active".
    """
    candidates = [
        (incident, ((incident.centroid_lat - lat) ** 2 + (incident.centroid_lon - lon) ** 2) ** 0.5)
        for incident in db.query(FireIncident).all()
    ]
    in_range = [(incident, dist) for incident, dist in candidates if dist <= settings.copernicus_ems_match_deg]
    if not in_range:
        return None
    return min(in_range, key=lambda pair: pair[1])[0]


def _event_title_and_description(activation: dict) -> tuple[str, str]:
    code = activation.get("code", "")
    title = f"Copernicus EMS activó cartografía de emergencia ({code})"
    n_products = activation.get("n_products") or 0
    n_aois = activation.get("n_aois") or 0
    status = "Activación cerrada" if activation.get("closed") else "Activación en curso"
    description = (
        f"{status} · {n_aois} zona(s) de interés, {n_products} producto(s) cartográfico(s) publicado(s). "
        f"Detalle: https://rapidmapping.emergency.copernicus.eu/{code}"
    )
    return title, description


def ingest_copernicus_ems(db: Session) -> int:
    state.mark_attempt("copernicus_ems")
    try:
        count = _ingest_copernicus_ems(db)
    except Exception as exc:
        # See the matching comment in eumetsat.py - roll back before
        # record_check reuses this session so its own db.commit() doesn't
        # raise a second, unrelated PendingRollbackError and mask the cause.
        db.rollback()
        record_check(db, "copernicus_ems", "disrupted", str(exc))
        raise
    record_check(db, "copernicus_ems", "ok", f"{count} activations newly matched to incidents")
    return count


def _ingest_copernicus_ems(db: Session) -> int:
    activations = fetch_wildfire_activations()
    newly_matched = 0

    for activation in activations:
        code = activation.get("code")
        if not code:
            continue
        centroid = _parse_centroid(activation.get("centroid"))

        record = db.query(CopernicusEmsActivation).filter_by(code=code).first()
        if record is None:
            record = CopernicusEmsActivation(code=code)
            db.add(record)

        record.name = activation.get("name")
        record.event_time = _parse_dt(activation.get("eventTime"))
        record.activation_time = _parse_dt(activation.get("activationTime"))
        record.closed = bool(activation.get("closed"))
        record.n_aois = activation.get("n_aois")
        record.n_products = activation.get("n_products")
        record.raw_json = json.dumps(activation)
        record.updated_at = datetime.utcnow()
        if centroid:
            record.centroid_lat, record.centroid_lon = centroid

        if centroid and record.matched_incident_id is None:
            incident = _find_matching_incident(db, *centroid)
            if incident:
                record.matched_incident_id = incident.id

        if not record.matched_incident_id:
            continue

        title, description = _event_title_and_description(activation)
        if record.incident_event_id:
            # Already announced - just refresh the existing event (more
            # AOIs/products since discovered, or the activation closed)
            # rather than appending a duplicate row every poll.
            event = db.query(IncidentEvent).filter_by(id=record.incident_event_id).first()
            if event:
                event.title = title
                event.description = description
        else:
            event = IncidentEvent(
                incident_id=record.matched_incident_id,
                occurred_at=record.activation_time or datetime.utcnow(),
                event_type="ems_activation",
                source="copernicus_ems",
                title=title,
                description=description,
                raw_data=json.dumps({"code": code}),
            )
            db.add(event)
            db.flush()  # need event.id to store on record below
            record.incident_event_id = event.id
            newly_matched += 1

    db.commit()
    return newly_matched
