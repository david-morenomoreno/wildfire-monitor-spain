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

Once an activation matches an incident, its detail endpoint
(public-activations/?code=EMSRxxx) is also fetched for `reason` (the
analyst's own incident description), `activator` (who requested it), the
top-level `stats` object (population/roads/built-up area affected), and
`reportLink` - a public ArcGIS StoryMap, which is the closest thing to an
actual rendered, browser-viewable map this API offers.

Additionally, per-AOI PRODUCT-level detail is now also parsed: the detail
response's `aois[].products[]` array carries a much richer per-product
`stats` object (land-use/vegetation categories affected in hectares, burnt
area, active-flame count, population/infrastructure impact) than the
activation's own top-level `stats`. Confirmed live (2026-07-23) that an AOI
is often re-monitored multiple times (`monitoringNumber` 0, 1, 2, ...) as a
fire evolves, and a still-in-progress pass has `version.statusCode == "W"`
(awaiting delivery) with `stats: null` - only the latest DELIVERED
(`statusCode == "F"`) pass per AOI is kept, see `_select_best_products`.

Per-product `layers[]` includes a "cog" entry - confirmed live to be a real
Cloud-Optimized GeoTIFF (not just a plain full-res TIFF given a COG-shaped
name): GDAL's /vsicurl/ virtual filesystem can read a small preview off its
internal overviews via HTTP range requests, so a low-res JPEG IS rendered
server-side and cached, same lazy pattern as services/copernicus.py's
Sentinel Hub thumbnails - see services/copernicus_ems_imagery.py. The raw
full-resolution file itself (~40MB) is never downloaded.
"""

import json
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app import state
from app.config import settings
from app.models import CopernicusEmsActivation, CopernicusEmsProduct, FireIncident, IncidentEvent
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


def fetch_activation_detail(code: str) -> dict | None:
    """Per-activation detail - reason/activator/reportLink/stats. See module docstring."""
    response = httpx.get(settings.copernicus_ems_detail_url, params={"code": code}, timeout=30.0)
    response.raise_for_status()
    results = response.json().get("results") or []
    return results[0] if results else None


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


# Common CORINE-style land-use category names seen in the "Land use" stats
# block (confirmed live against EMSR898/EMSR896's real product stats,
# 2026-07-23) - translated for a readable Spanish timeline summary. Keys are
# matched after stripping whitespace (the raw API returns some with a
# trailing space, e.g. "Forests ", "Pastures "). Anything not in this map
# falls back to the raw category name as-is, since the full CORINE
# nomenclature has dozens of categories not worth hardcoding every one.
_LAND_USE_LABELS_ES = {
    "Forests": "bosque",
    "Shrub and/or herbaceous vegetation association": "matorral y vegetación herbácea",
    "Arable land": "cultivos de secano/regadío",
    "Heterogeneous agricultural areas": "mosaico agrícola",
    "Pastures": "pastos",
    "Open spaces with little or no vegetation": "espacios con poca vegetación",
    "Permanent crops": "cultivos permanentes",
    "Other": "otros usos",
}


def _select_best_products(detail: dict) -> list[dict]:
    """
    Picks the single latest DELIVERED "DEL" (delineation) product per AOI
    from a detail response's `aois[].products[]` - an AOI can carry several
    monitoring passes (monitoringNumber 0, 1, 2, ...), and a pass still
    awaiting analyst delivery has `version.statusCode == "W"` and
    `stats: null` rather than being simply absent, so both conditions are
    checked rather than just picking the highest monitoringNumber outright.
    Returns one dict per AOI: {aoi_number, aoi_name, monitoring_number,
    stats, cog_url}.
    """
    aws_bucket = detail.get("aws_bucket") or ""
    selected: list[dict] = []
    for aoi in detail.get("aois") or []:
        delivered = [
            product
            for product in (aoi.get("products") or [])
            if product.get("type") == "DEL"
            and product.get("stats") is not None
            and (product.get("version") or {}).get("statusCode") == "F"
        ]
        if not delivered:
            continue
        best = max(delivered, key=lambda p: p.get("monitoringNumber") or 0)
        cog_layer = next(
            (layer for layer in best.get("layers") or [] if layer.get("format") == "cog"),
            None,
        )
        cog_url = f"{aws_bucket}/{cog_layer['name']}" if aws_bucket and cog_layer else None
        selected.append(
            {
                "aoi_number": aoi.get("number"),
                "aoi_name": aoi.get("name"),
                "monitoring_number": best.get("monitoringNumber"),
                "stats": best.get("stats"),
                "cog_url": cog_url,
            }
        )
    return selected


def _numeric(value) -> float | None:
    """Product stats mix real numbers with sentinel strings ("NA", "-") for
    not-applicable/unset fields - this is the shared guard for pulling a
    usable number out of one `{"unit":..., "total":..., "affected":...}` leaf."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _aggregate_product_stats(products: list[dict]) -> dict:
    """
    Sums the per-AOI `stats` blocks selected by `_select_best_products`
    across every AOI in the activation - summing hectares/counts across AOIs
    has real precedent in this codebase (services/area_estimate.py, and the
    incident-merge endpoint summing detection_count/area_ha), and each AOI
    here is a genuinely distinct chunk of the same fire's footprint rather
    than an overlapping remeasurement of it.
    """
    land_use_ha: dict[str, float] = {}
    burnt_area_ha = 0.0
    active_flames = 0.0
    population_affected = 0.0
    has_burnt_area = has_active_flames = has_population = False

    for product in products:
        stats = product.get("stats") or {}

        for category, leaf in (stats.get("Land use") or {}).items():
            affected = _numeric(leaf.get("affected")) if isinstance(leaf, dict) else None
            if affected:
                label = _LAND_USE_LABELS_ES.get(category.strip(), category.strip().lower())
                land_use_ha[label] = land_use_ha.get(label, 0.0) + affected

        burnt = _numeric(((stats.get("Burnt area") or {}).get("None") or {}).get("affected"))
        if burnt is not None:
            burnt_area_ha += burnt
            has_burnt_area = True

        flames = _numeric(((stats.get("Active Flames") or {}).get("None") or {}).get("affected"))
        if flames is not None:
            active_flames += flames
            has_active_flames = True

        population = _numeric(((stats.get("Estimated population") or {}).get("None") or {}).get("affected"))
        if population is not None:
            population_affected += population
            has_population = True

    top_land_use = sorted(land_use_ha.items(), key=lambda pair: -pair[1])[:3]
    return {
        "top_land_use": top_land_use,
        "burnt_area_ha": burnt_area_ha if has_burnt_area else None,
        "active_flames": int(active_flames) if has_active_flames else None,
        "population_affected": int(population_affected) if has_population else None,
    }


def _format_product_stats_summary(products: list[dict]) -> str:
    """Readable Spanish one-liner from `_aggregate_product_stats` - dropped
    entirely (returns "") when an activation has no delivered products yet."""
    if not products:
        return ""
    aggregate = _aggregate_product_stats(products)
    parts = []
    if aggregate["top_land_use"]:
        breakdown = ", ".join(f"{label} ({ha:,.0f} ha)".replace(",", ".") for label, ha in aggregate["top_land_use"])
        parts.append(f"Vegetación/terreno más afectado: {breakdown}")
    if aggregate["burnt_area_ha"] is not None:
        parts.append(f"área quemada: {aggregate['burnt_area_ha']:,.0f} ha".replace(",", "."))
    if aggregate["active_flames"]:
        parts.append(f"{aggregate['active_flames']} foco(s) activo(s) detectado(s) en la pasada")
    if aggregate["population_affected"]:
        parts.append(f"~{aggregate['population_affected']:,} persona(s) en el área afectada".replace(",", "."))
    return " · ".join(parts)


def _format_stats(stats_json: str | None) -> str:
    """Top-level `stats` keys/units vary by disaster type (e.g. "Roads [km]",
    "Population [No.]") - formatted generically rather than hardcoded, with
    unset ("-") or not-applicable ("NA") values dropped."""
    if not stats_json:
        return ""
    try:
        stats = json.loads(stats_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(stats, dict):
        return ""
    return " · ".join(f"{key}: {value}" for key, value in stats.items() if value not in (None, "-", "NA"))


def _event_title_and_description(
    activation: dict, record: CopernicusEmsActivation, products: list[dict]
) -> tuple[str, str]:
    code = activation.get("code", "")
    title = f"Copernicus EMS activó cartografía de emergencia ({code})"
    n_products = activation.get("n_products") or 0
    n_aois = activation.get("n_aois") or 0
    status = "Activación cerrada" if activation.get("closed") else "Activación en curso"

    lines = [f"{status} · {n_aois} zona(s) de interés, {n_products} producto(s) cartográfico(s) publicado(s)."]
    if record.reason:
        reason = record.reason.strip()
        if len(reason) > 300:
            reason = reason[:300].rsplit(" ", 1)[0] + "…"
        lines.append(reason)
    # Per-AOI product stats (land-use/burnt-area/active-flames/population) -
    # much richer than the activation-level `stats` below, so this is listed
    # first when available.
    product_stats_line = _format_product_stats_summary(products)
    if product_stats_line:
        lines.append(product_stats_line)
    stats_line = _format_stats(record.stats_json)
    if stats_line:
        lines.append(stats_line)
    if record.activator:
        lines.append(f"Activado por: {record.activator}")
    lines.append(
        f"Mapa oficial: {record.report_link}" if record.report_link
        else f"Detalle: https://rapidmapping.emergency.copernicus.eu/{code}"
    )
    return title, "\n".join(lines)


def _store_products(db: Session, activation_id: int, selected: list[dict]) -> None:
    """Upserts one CopernicusEmsProduct row per AOI (unique on
    activation_id+aoi_number) from `_select_best_products`' output."""
    for item in selected:
        aoi_number = item.get("aoi_number")
        if aoi_number is None:
            continue
        product = (
            db.query(CopernicusEmsProduct)
            .filter_by(activation_id=activation_id, aoi_number=aoi_number)
            .first()
        )
        if product is None:
            product = CopernicusEmsProduct(activation_id=activation_id, aoi_number=aoi_number)
            db.add(product)
        product.aoi_name = item.get("aoi_name")
        product.monitoring_number = item.get("monitoring_number")
        stats = item.get("stats")
        product.stats_json = json.dumps(stats) if stats else None
        product.cog_url = item.get("cog_url")
        product.updated_at = datetime.utcnow()


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

        db.flush()  # need record.id below (for a brand-new record) before touching its products

        # Skip re-fetching detail for an already-closed activation that
        # already has one - reason/stats/reportLink are stable once closed.
        # An open activation is refetched every poll to pick up newly
        # published products or updated impact stats.
        if not record.closed or not record.reason:
            try:
                detail = fetch_activation_detail(code)
            except Exception:
                logger.exception("Copernicus EMS detail fetch failed for %s", code)
                detail = None
            if detail:
                record.reason = detail.get("reason")
                # Raw activator is "Country|Full Agency Name" (confirmed
                # live, e.g. "Spain|Ministry of Interior - Centro Nacional
                # de Emergencias...") - the country is redundant with this
                # activation's own countries field, so just the agency name
                # reads better in a timeline description.
                activator = detail.get("activator")
                record.activator = activator.split("|", 1)[-1] if activator else None
                record.report_link = detail.get("reportLink")
                stats = detail.get("stats")
                record.stats_json = json.dumps(stats) if stats else None
                _store_products(db, record.id, _select_best_products(detail))
                db.flush()

        # Reloaded regardless of whether detail was refetched this poll, so
        # an already-closed activation's description still reflects
        # whatever products were captured on a previous (pre-closed) poll.
        product_rows = (
            db.query(CopernicusEmsProduct)
            .filter_by(activation_id=record.id)
            .order_by(CopernicusEmsProduct.aoi_number)
            .all()
        )
        products_for_stats = [
            {"stats": json.loads(row.stats_json)} for row in product_rows if row.stats_json
        ]
        product_ids_with_imagery = [row.id for row in product_rows if row.cog_url]

        title, description = _event_title_and_description(activation, record, products_for_stats)
        # ems_product_ids lets the frontend request a lazily-rendered
        # satellite preview per AOI at
        # GET /api/copernicus-ems/products/{id}/thumbnail - see
        # services/copernicus_ems_imagery.py. Only products with a "cog"
        # layer are included; others render nothing.
        raw_data = json.dumps({"code": code, "ems_product_ids": product_ids_with_imagery})
        if record.incident_event_id:
            # Already announced - just refresh the existing event (more
            # AOIs/products since discovered, or the activation closed)
            # rather than appending a duplicate row every poll.
            event = db.query(IncidentEvent).filter_by(id=record.incident_event_id).first()
            if event:
                event.title = title
                event.description = description
                event.raw_data = raw_data
        else:
            event = IncidentEvent(
                incident_id=record.matched_incident_id,
                occurred_at=record.activation_time or datetime.utcnow(),
                event_type="ems_activation",
                source="copernicus_ems",
                title=title,
                description=description,
                raw_data=raw_data,
            )
            db.add(event)
            db.flush()  # need event.id to store on record below
            record.incident_event_id = event.id
            newly_matched += 1

    db.commit()
    return newly_matched
