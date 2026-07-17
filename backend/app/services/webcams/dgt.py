import logging
import xml.etree.ElementTree as ET

import httpx

from app.services.webcams.base import WebcamRecord, WebcamSource

logger = logging.getLogger(__name__)

# Confirmed live and public (2026-07-15): no auth, no API key - DATEX II v3.7
# feed of all DGT ITS devices; typeOfDevice=camera picks out just the traffic
# cameras (1937 of them at last check) out of the full device list. Each
# camera's own fse:deviceUrl is a direct, publicly-fetchable JPEG snapshot -
# confirmed by downloading one and viewing it (a real, current, timestamped
# road image), not just assuming the URL pattern works.
FEED_URL = "https://nap.dgt.es/datex2/v3/dgt/DevicePublication/camaras_datex2_v37.xml"
PORTAL_URL = "https://infocar.dgt.es/etraffic/"

NS = {
    "d2": "http://levelC/schema/3/d2Payload",
    "ns2": "http://levelC/schema/3/faultAndStatus",
    "loc": "http://levelC/schema/3/locationReferencing",
    "lse": "http://levelC/schema/3/locationReferencingSpanishExtension",
    "fse": "http://levelC/schema/3/faultAndStatusSpanishExtension",
}


class DgtWebcamSource(WebcamSource):
    source_key = "dgt"
    name = "DGT - Cámaras de tráfico"
    portal_url = PORTAL_URL

    def fetch(self) -> list[WebcamRecord]:
        response = httpx.get(FEED_URL, timeout=60.0)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        records = []
        for device in root.iter(f"{{{NS['ns2']}}}device"):
            type_el = device.find("ns2:typeOfDevice", NS)
            if type_el is None or type_el.text != "camera":
                continue

            external_id = device.get("id")
            url_el = device.find("fse:deviceUrl", NS)
            lat_el = device.find(".//loc:latitude", NS)
            lon_el = device.find(".//loc:longitude", NS)
            if not external_id or url_el is None or lat_el is None or lon_el is None:
                continue

            road_name_el = device.find(".//loc:roadName", NS)
            road_dest_el = device.find(".//loc:roadDestination", NS)
            province_el = device.find(".//lse:province", NS)

            road = road_name_el.text if road_name_el is not None else None
            destination = road_dest_el.text if road_dest_el is not None else None
            name = f"{road} → {destination}" if road and destination else (road or destination)

            try:
                latitude = float(lat_el.text)
                longitude = float(lon_el.text)
            except (TypeError, ValueError):
                continue

            records.append(
                WebcamRecord(
                    external_id=external_id,
                    latitude=latitude,
                    longitude=longitude,
                    image_url=url_el.text,
                    name=name,
                    road=road,
                    province=province_el.text if province_el is not None else None,
                )
            )
        return records
