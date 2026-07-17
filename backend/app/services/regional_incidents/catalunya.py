import logging
from datetime import datetime

import httpx
from pyproj import Transformer

from app.services.regional_incidents.base import RegionalFireRecord, RegionalIncidentSource

logger = logging.getLogger(__name__)

# Confirmed live and public (2026-07-15): no auth. Found embedded directly in
# the Bombers ArcGIS Experience Builder app's own config. This is a general
# "urgent actions" (all-hazards) layer, not wildfire-only - TAL_COD_ALARMA1='IV'
# ("incendi vegetació") isolates wildfires/vegetation fires specifically.
QUERY_URL = (
    "https://services7.arcgis.com/ZCqVt1fRXwwK6GF4/arcgis/rest/services"
    "/ACTUACIONS_URGENTS_online_PRO_AMB_FASE_VIEW/FeatureServer/0/query"
)
PORTAL_URL = "https://interior.gencat.cat/ca/incendis-forestals/inici/"

# Geometry comes back in ETRS89/UTM zone 31N (confirmed via the query
# response's spatialReference wkid 25831) - fixed zone, unlike JCyL where it
# varies per record.
_TRANSFORMER = Transformer.from_crs("EPSG:25831", "EPSG:4326", always_xy=True)


def _epoch_ms_to_datetime(ms: float | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.utcfromtimestamp(ms / 1000)


class CatalunyaIncidentSource(RegionalIncidentSource):
    region_code = "bombers"
    name = "Generalitat de Catalunya - Bombers (actuacions en viu)"
    portal_url = PORTAL_URL

    def fetch(self) -> list[RegionalFireRecord]:
        response = httpx.get(
            QUERY_URL,
            params={
                "where": "TAL_COD_ALARMA1='IV'",
                "outFields": "*",
                "f": "json",
                "orderByFields": "ACT_DAT_ACTUAL DESC",
                "resultRecordCount": 200,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"Bombers query error: {payload['error']}")

        records = []
        for feature in payload.get("features", []):
            attrs = feature.get("attributes", {})
            object_id = attrs.get("OBJECTID")
            if object_id is None:
                continue
            geometry = feature.get("geometry") or {}
            latitude = longitude = None
            if "x" in geometry and "y" in geometry:
                longitude, latitude = _TRANSFORMER.transform(geometry["x"], geometry["y"])

            extinguished_at = _epoch_ms_to_datetime(attrs.get("ACT_DAT_FI"))
            records.append(
                RegionalFireRecord(
                    external_id=f"CAT-{object_id}",
                    status=attrs.get("COM_FASE") or "Desconegut",
                    municipality=attrs.get("MUNICIPI_DPX") or attrs.get("MUNICIPI_SIG"),
                    province=None,  # not present in this layer
                    latitude=latitude,
                    longitude=longitude,
                    started_at=_epoch_ms_to_datetime(attrs.get("ACT_DAT_INICI")),
                    extinguished_at=extinguished_at,
                    # Only a vehicle count is available in this layer (no personnel
                    # breakdown like JCyL's medios[] or INFOCA's per-category fields).
                    personnel_summary={
                        "vehicles": attrs.get("ACT_NUM_VEH") or 0,
                        "total_actuando": attrs.get("ACT_NUM_VEH") or 0,
                    },
                    raw=attrs,
                )
            )
        return records
