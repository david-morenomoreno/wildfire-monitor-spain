from dataclasses import dataclass


@dataclass
class BulletinRef:
    """One document link discovered on a region's bulletin index page."""

    title: str
    file_url: str
    file_type: str  # "pdf" or "csv"


class AdminBulletinSource:
    """
    Common contract every regional bulletin scraper implements. Regions vary
    wildly in what they actually publish - some link opaque periodic PDFs
    with no fixed naming pattern, others may offer a real CSV feed - so
    discover() always finds documents, but parse() is allowed to say "no
    structured rows available" by returning None; the bulletin is then kept
    as a linked reference document rather than dropped.
    """

    region_code: str
    name: str
    portal_url: str

    def discover(self) -> list[BulletinRef]:
        raise NotImplementedError

    def parse(self, file_bytes: bytes) -> list[dict] | None:
        return None
