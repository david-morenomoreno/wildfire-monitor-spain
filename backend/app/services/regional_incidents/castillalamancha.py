import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from app.services.regional_incidents.base import RegionalFireRecord, RegionalIncidentSource

logger = logging.getLogger(__name__)

# Confirmed live and public (2026-07-20): FIDIAS is a PHP form-based app, not a
# REST API, but it needs no real credentials - the login form's own JS
# (login.js: submitLogin) just resubmits the page with auth=ANONIMO as a GET
# param, which drops a PHPSESSID cookie and serves the "for media" fire
# listing directly. That listing page's own JS (listado.js: detalleIncendio)
# POSTs accion=detalle&CINCENDI=<code> back to the same URL for a per-fire
# detail page - reused here the same way, with a plain httpx.Client to keep
# the session cookie across both requests.
BASE_URL = "https://fidias.castillalamancha.es/consulta/forms/fidif001.php"
PORTAL_URL = BASE_URL

# The listing page's own heading calls this "Listado de los incendios más
# significativos de Castilla-La Mancha" - i.e. a curated significant-fires
# list, not necessarily every active fire in the region. That's the only
# breadth FIDIAS exposes publicly; there is no separate "all fires" feed.
_ID_RE = re.compile(r"detalleIncendio\('(\d+)'\)")

# No coordinates are published anywhere in this flow (list page or detail
# page) - only Provincia / T. Municipal / Entidad Menor / Paraje as free text.
# The linked infocam.castillalamancha.es site (Drupal, "Mapa de incendios")
# only shows a static image, no ArcGIS/WMS layer or embedded JSON was found
# behind it. So latitude/longitude are left None here (this source stays
# focused on parsing FIDIAS's own fields); sync.py applies a generic
# best-effort forward-geocoding fallback (municipality + province ->
# coordinates via Nominatim) for any regional source that reports None here.

_LABELS = {
    "ESTADO:": "estado",
    "Provincia:": "provincia",
    "T. Municipal:": "municipio",
    "DETECCIÓN:": "deteccion",
    "CONTROL:": "control",
    "EXTINCIÓN:": "extincion",
    "DETECTADO POR:": "detectado_por",
    "NÚMERO DE HECTÁREAS:": "hectareas",
    "CAUSA DEL INCENDIO:": "causa",
}

# The site's own placeholder text for "not published yet" - not a real value.
_UNSPECIFIED_PREFIX = "Sin especificar"


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split()).strip()
    if not text or text.startswith(_UNSPECIFIED_PREFIX):
        return None
    return text


def _parse_datetime(raw: str | None) -> datetime | None:
    raw = _clean(raw)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def _parse_area_ha(raw: str | None) -> float | None:
    raw = _clean(raw)
    if not raw:
        return None
    match = re.search(r"[\d]+(?:[.,]\d+)?", raw)
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


def _parse_fields(detail_soup: BeautifulSoup) -> dict:
    fields: dict[str, str | None] = {}
    for span in detail_soup.find_all("span"):
        label = span.get_text(strip=True)
        key = _LABELS.get(label)
        if key is None:
            continue
        value_span = span.find_next_sibling("span")
        fields[key] = value_span.get_text(" ", strip=True) if value_span else None
    return fields


def _current_personnel(detail_soup: BeautifulSoup) -> dict:
    """
    The detail page has two personnel tables: cumulative-since-start
    ("MEDIOS QUE HAN PARTICIPADO EN EL INCENDIO") and currently deployed
    ("MEDIOS QUE PARTICIPAN EN EL INCENDIO AHORA"). Use the latter, matching
    the "actuando now" semantics of the other regions' personnel_summary.
    """
    header = detail_soup.find(string=re.compile(r"PARTICIPAN EN EL INCENDIO AHORA"))
    if header is None:
        return {}
    table = header.find_parent("div").find_next("table")
    if table is None:
        return {}

    summary: dict[str, int] = {}
    total = 0
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 2:
            continue
        category = cells[0].get_text(strip=True).rstrip(":")
        if category.upper() == "TOTAL":
            continue
        match = re.search(r"(\d+)\s*personas?", cells[1].get_text())
        count = int(match.group(1)) if match else 0
        if count:
            summary[category] = count
            total += count
    summary["total_actuando"] = total
    return summary


class CastillaLaManchaIncidentSource(RegionalIncidentSource):
    region_code = "infocam"
    name = "Junta de Comunidades de Castilla-La Mancha - INFOCAM (listado de incendios significativos)"
    portal_url = PORTAL_URL

    def fetch(self) -> list[RegionalFireRecord]:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            listing = client.get(BASE_URL, params={"auth": "ANONIMO"})
            listing.raise_for_status()
            fire_ids = sorted(set(_ID_RE.findall(listing.text)))

            records = []
            for fire_id in fire_ids:
                detail_response = client.post(BASE_URL, data={"accion": "detalle", "CINCENDI": fire_id})
                detail_response.raise_for_status()
                soup = BeautifulSoup(detail_response.text, "html.parser")
                fields = _parse_fields(soup)
                personnel_summary = _current_personnel(soup)

                records.append(
                    RegionalFireRecord(
                        external_id=f"CLM-{fire_id}",
                        status=fields.get("estado") or "Desconocido",
                        municipality=fields.get("municipio"),
                        province=fields.get("provincia"),
                        latitude=None,
                        longitude=None,
                        started_at=_parse_datetime(fields.get("deteccion")),
                        controlled_at=_parse_datetime(fields.get("control")),
                        extinguished_at=_parse_datetime(fields.get("extincion")),
                        area_ha=_parse_area_ha(fields.get("hectareas")),
                        cause=_clean(fields.get("causa")),
                        personnel_summary=personnel_summary,
                        raw={"CINCENDI": fire_id, **fields},
                    )
                )
        return records
