"""Evidence documents an assessment may cite.

D0 is the platform's own data about the venue (Google Places, OpenStreetMap).
D1..Dn are pages from the venue's own website that robots.txt allows.
"""

from dataclasses import dataclass

from areyos.catalog.industries import get_industry
from areyos.config import get_settings
from areyos.enrichment.crawler import SiteCrawler, find_links
from areyos.people.sources import html_to_lines

PAGE_HINTS = (
    "about", "facilit", "infrastructure", "department", "speciali", "centre-of-excellence", "services", "campus",
    "floor", "map", "direction", "visitor", "how-to-reach", "reach-us", "store", "brands", "directory", "hall",
    "venue", "terminal", "news", "press", "media", "expansion", "upcoming", "project", "new-block", "renovat",
)
MAX_PAGE_CHARS = 12_000


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    url: str | None
    text: str


def platform_document(prospect) -> Document:
    """Facts already stored for the venue, one per line, each naming its source."""
    lines = [
        f"Venue name: {prospect.name}",
        f"Industry: {get_industry(prospect.industry).label}",
        f"Location: {prospect.address or ''} {prospect.city or ''} {prospect.state or ''}".strip(),
    ]
    if prospect.rating_count is not None:
        lines.append(f"Google Places review count: {prospect.rating_count}")
    if prospect.rating is not None:
        lines.append(f"Google Places rating: {prospect.rating}")
    if prospect.types:
        lines.append(f"Place types: {', '.join(prospect.types)}")
    attrs = prospect.attributes or {}
    for key in ("beds", "rooms", "capacity", "building:levels", "stars", "iata"):
        if attrs.get(key):
            lines.append(f"OpenStreetMap {key}: {attrs[key]}")
    if attrs.get("footprint_m2"):
        lines.append(
            f"OpenStreetMap outline bounding area (approximate, not floor area): {int(attrs['footprint_m2'])} m2"
        )
    return Document("D0", "Platform data (Google Places / OpenStreetMap)", prospect.google_maps_url, "\n".join(lines))


def website_documents(website: str | None, crawler: SiteCrawler | None = None) -> list[Document]:
    if not website:
        return []
    crawler = crawler or SiteCrawler()
    home = crawler.fetch(website)
    if home is None:
        return []
    pages = [home]
    for link in find_links(home[1], home[0], PAGE_HINTS)[: get_settings().opportunity_max_pages - 1]:
        page = crawler.fetch(link)
        if page:
            pages.append(page)
    docs = []
    for i, (url, html) in enumerate(pages, start=1):
        text = "\n".join(html_to_lines(html))[:MAX_PAGE_CHARS]
        if text.strip():
            docs.append(Document(f"D{i}", f"Website: {url}", url, text))
    return docs
