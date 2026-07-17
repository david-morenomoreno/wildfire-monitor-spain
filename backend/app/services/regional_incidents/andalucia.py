import logging
from datetime import datetime, timedelta

import httpx
from pyproj import Transformer

from app.services.regional_incidents.base import RegionalFireRecord, RegionalIncidentSource

logger = logging.getLogger(__name__)

# Confirmed live and public (2026-07-15): no auth. Found via the official
# INFOCA dashboard's ArcGIS Online item config -> its web map -> this hosted
# feature service (layer 2, "Incidentes"). Holds historical AND active
# incidents mixed together - bounded with orderByFields+resultRecordCount
# below rather than a date filter, since the exact WHERE date-literal syntax
# for this service wasn't independently verified.
QUERY_URL = (
    "https://utility.arcgis.com/usrsvcs/servers/d6d1c0079ddd4c7f8876d58e13fcf1ac"
    "/rest/services/INFOCA/AN_INCIDENTES_PRO/FeatureServer/2/query"
)
PORTAL_URL = (
    "https://www.juntadeandalucia.es/organismos/ema/areas/incendios-forestales"
    "/situacion/incendios-activos.html"
)

# X/Y come back in Web Mercator (confirmed via the query response's
# spatialReference wkid 102100/3857), not lat/lon.
_TRANSFORMER = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

_PERSONNEL_FIELDS = [
    "GRUPOS_ESPECIALISTAS",
    "BRICAS",
    "VEHICULOS",
    "TECNICOS",
    "MEDIOS_AEREOS",
    "UMIF",
    "GRUPOS_APOYO",
    "UNASIF_ACO",
]


def _personnel_summary(attrs: dict) -> dict:
    summary = {field: attrs.get(field) or 0 for field in _PERSONNEL_FIELDS if attrs.get(field)}
    summary["total_actuando"] = sum(summary.values())
    return summary


def _combined_datetime(fecha_ms: float | None, hora: str | None) -> datetime | None:
    if fecha_ms is None:
        return None
    date_part = datetime.utcfromtimestamp(fecha_ms / 1000).date()
    if hora:
        try:
            h, m, s = (int(part) for part in hora.split(":"))
            return datetime(date_part.year, date_part.month, date_part.day, h, m, s)
        except (ValueError, TypeError):
            pass
    return datetime(date_part.year, date_part.month, date_part.day)


class AndaluciaIncidentSource(RegionalIncidentSource):
    region_code = "infoca"
    name = "Junta de Andalucía - INFOCA (incidentes en vivo)"
    portal_url = PORTAL_URL

    def fetch(self) -> list[RegionalFireRecord]:
        response = httpx.get(
            QUERY_URL,
            params={
                "where": "TIPO_INCIDENTE='IIFF INCENDIOS FORESTALES'",
                "outFields": "*",
                "f": "json",
                "orderByFields": "OID_ENTERO DESC",
                "resultRecordCount": 200,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"INFOCA query error: {payload['error']}")

        records = []
        for feature in payload.get("features", []):
            attrs = feature.get("attributes", {})
            oid = attrs.get("OID_ENTERO")
            if oid is None:
                continue
            x, y = attrs.get("X"), attrs.get("Y")
            latitude = longitude = None
            if x is not None and y is not None:
                longitude, latitude = _TRANSFORMER.transform(x, y)

            when = _combined_datetime(attrs.get("FECHA"), attrs.get("HORA"))
            status = attrs.get("ESTADO") or "Desconocido"
            records.append(
                RegionalFireRecord(
                    external_id=f"AN-{oid}",
                    status=status,
                    municipality=attrs.get("TERMINO_MUNICIPAL"),
                    province=attrs.get("PROVINCIA"),
                    latitude=latitude,
                    longitude=longitude,
                    started_at=when,
                    controlled_at=when if status.upper() == "CONTROLADO" else None,
                    extinguished_at=when if status.upper() == "EXTINGUIDO" else None,
                    personnel_summary=_personnel_summary(attrs),
                    raw=attrs,
                )
            )
        return records
