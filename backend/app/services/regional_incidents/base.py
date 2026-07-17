from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RegionalFireRecord:
    """One fire's live status, normalized from a region's own feed shape."""

    external_id: str
    status: str  # the region's own label, kept as-is (e.g. "Activo")
    municipality: Optional[str] = None
    province: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    started_at: Optional[datetime] = None
    controlled_at: Optional[datetime] = None
    extinguished_at: Optional[datetime] = None
    area_ha: Optional[float] = None
    cause: Optional[str] = None
    personnel_summary: dict = field(default_factory=dict)  # counts by resource category
    raw: Optional[dict] = None


class RegionalIncidentSource:
    """Common contract every region's live-status feed implements."""

    region_code: str
    name: str
    portal_url: str

    def fetch(self) -> list[RegionalFireRecord]:
        raise NotImplementedError
