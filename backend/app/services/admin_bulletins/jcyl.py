import io
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.services.admin_bulletins.base import AdminBulletinSource, BulletinRef

logger = logging.getLogger(__name__)

INDEX_URL = (
    "https://medioambiente.jcyl.es/web/es/medio-natural/"
    "informacion-diaria-incendios-forestales.html"
)


class JcylBulletinSource(AdminBulletinSource):
    region_code = "jcyl"
    name = "Junta de Castilla y León - Incendios forestales"
    portal_url = INDEX_URL

    def discover(self) -> list[BulletinRef]:
        # jcyl.es serves an incomplete certificate chain (confirmed with
        # `curl -k`/openssl, 2026-07) - verification is disabled only for
        # this one government source. Every other httpx client in the app
        # (FIRMS/EFFIS/Nominatim/Telegram) keeps verify=True.
        response = httpx.get(INDEX_URL, timeout=30.0, follow_redirects=True, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        refs: list[BulletinRef] = []
        seen_urls: set[str] = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            path = href.split("?")[0]
            if "/binarios/" not in path or not path.lower().endswith(".pdf"):
                continue
            file_url = urljoin(INDEX_URL, href)
            if file_url in seen_urls:
                continue
            seen_urls.add(file_url)
            title = link.get_text(strip=True) or path.rsplit("/", 1)[-1]
            refs.append(BulletinRef(title=title, file_url=file_url, file_type="pdf"))
        return refs

    def parse(self, file_bytes: bytes) -> list[dict] | None:
        """
        Best-effort table extraction. Most of JCyL's linked PDFs are
        periodic aggregate statistics ("Estadística comparativa ..."), not a
        clean per-fire table, so this returns rows only when pdfplumber
        actually finds a table on some page; otherwise the bulletin stays a
        reference-only document.
        """
        import pdfplumber  # local import: only needed by this one parser

        try:
            rows: list[dict] = []
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    table = page.extract_table()
                    if not table or len(table) < 2:
                        continue
                    header, *body = table
                    header = [
                        (str(cell).strip() if cell else f"col_{i}")
                        for i, cell in enumerate(header)
                    ]
                    for row in body:
                        rows.append(dict(zip(header, row)))
            return rows or None
        except Exception:
            logger.warning("JCyL PDF had no extractable table", exc_info=True)
            return None
