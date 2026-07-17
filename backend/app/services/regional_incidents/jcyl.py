import logging
from datetime import datetime

import httpx
from pyproj import Transformer

from app.services.regional_incidents.base import RegionalFireRecord, RegionalIncidentSource

logger = logging.getLogger(__name__)

# Confirmed live and public (2026-07-14): no auth, no API key, returns
# {"listaEmergencias": [...]} with one entry per fire currently tracked.
EMERGENCIAS_URL = "https://servicios.jcyl.es/incyl/json/emergencias"
PORTAL_URL = "https://servicios.jcyl.es/incyl/incyl"

# Coordinates come back as UTM (ETRS89), zone given by the "huso" field (29,
# 30, or 31 for mainland Spain) - not WGS84 lat/lon. EPSG:2582<huso> is the
# standard ETRS89/UTM-north-zone code IGN uses for each. Verified by
# converting a real sample (Villablino, León) and confirming it lands on
# the real town, not just structurally-plausible numbers.
_TRANSFORMERS = {
    huso: Transformer.from_crs(f"EPSG:{25800 + huso}", "EPSG:4326", always_xy=True)
    for huso in (29, 30, 31)
}


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None


def _to_wgs84(latitud: float | None, longitud: float | None, huso: int | None) -> tuple[float | None, float | None]:
    """
    JCyL's field names are misleading: "latitud"/"longitud" actually hold UTM
    northing/easting, not lat/lon - confirmed by their magnitude (millions,
    not -90..90/-180..180) and by successfully reprojecting them onto real
    known towns.
    """
    if latitud is None or longitud is None or huso not in _TRANSFORMERS:
        return None, None
    lon, lat = _TRANSFORMERS[huso].transform(longitud, latitud)
    return lat, lon


def _personnel_summary(medios: list[dict]) -> dict:
    summary: dict[str, int] = {}
    active_total = 0
    for medio in medios or []:
        if not medio.get("ACTUANDO"):
            continue
        active_total += 1
        category = ((medio.get("TIPO") or {}).get("CATEGORIA")) or "Otros"
        summary[category] = summary.get(category, 0) + 1
    summary["total_actuando"] = active_total
    return summary


def _area_ha(record: dict) -> float | None:
    fields = ["sup_arbolado", "sup_agricola", "sup_matorral", "sup_otra"]
    values = [record.get(f) for f in fields]
    if all(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


class JcylIncidentSource(RegionalIncidentSource):
    region_code = "jcyl"
    name = "Junta de Castilla y León - INCYL (estado operativo en vivo)"
    portal_url = PORTAL_URL

    def fetch(self) -> list[RegionalFireRecord]:
        response = httpx.get(EMERGENCIAS_URL, timeout=20.0)
        response.raise_for_status()
        fires = response.json().get("listaEmergencias", [])

        records = []
        for fire in fires:
            external_id = f"{fire.get('cpm')}-{fire.get('emergencia_cpm')}-{fire.get('emergencia_num1')}-{fire.get('emergencia_num2')}"
            latitude, longitude = _to_wgs84(fire.get("latitud"), fire.get("longitud"), fire.get("huso"))
            records.append(
                RegionalFireRecord(
                    external_id=external_id,
                    status=(fire.get("estado") or {}).get("NOMBRE", "Desconocido"),
                    municipality=(fire.get("municipio") or {}).get("nombre"),
                    province=(fire.get("provincia") or {}).get("nombre"),
                    latitude=latitude,
                    longitude=longitude,
                    started_at=_parse_date(fire.get("fecha_inicio")),
                    controlled_at=_parse_date(fire.get("fecha_control")),
                    extinguished_at=_parse_date(fire.get("fecha_extincion")),
                    area_ha=_area_ha(fire),
                    cause=fire.get("causa"),
                    personnel_summary=_personnel_summary(fire.get("medios")),
                    raw=fire,
                )
            )
        return records
