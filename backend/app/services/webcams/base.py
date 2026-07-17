from dataclasses import dataclass
from typing import Optional


@dataclass
class WebcamRecord:
    """One camera's metadata, normalized from a provider's own feed shape."""

    external_id: str
    latitude: float
    longitude: float
    image_url: str
    name: Optional[str] = None
    road: Optional[str] = None
    province: Optional[str] = None


class WebcamSource:
    """Common contract every webcam provider implements."""

    source_key: str
    name: str
    portal_url: str

    def fetch(self) -> list[WebcamRecord]:
        raise NotImplementedError
