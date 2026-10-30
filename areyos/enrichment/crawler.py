"""robots.txt-aware fetching of a venue's own website, shared by contact and people extraction."""

import re
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from areyos.config import get_settings
from areyos.http import PoliteClient, TransientError

HREF_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def same_site(url: str, root: str) -> bool:
    a, b = urlsplit(url).netloc.lower(), urlsplit(root).netloc.lower()
    return a.removeprefix("www.") == b.removeprefix("www.")


def site_domain(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    return urlsplit(url).netloc.lower().removeprefix("www.")


def find_links(html: str, page_url: str, hints: tuple[str, ...]) -> list[str]:
    """Same-site links whose URL or label contains any hint, in hint order."""
    scored: dict[str, int] = {}
    for href, label in HREF_RE.findall(html):
        url = urljoin(page_url, href.strip()).split("#")[0]
        if not url.startswith("http") or not same_site(url, page_url) or url.rstrip("/") == page_url.rstrip("/"):
            continue
        haystack = f"{href} {TAG_RE.sub(' ', label)}".lower()
        ranks = [i for i, h in enumerate(hints) if h in haystack]
        if ranks and (url not in scored or min(ranks) < scored[url]):
            scored[url] = min(ranks)
    return sorted(scored, key=scored.get)


class SiteCrawler:
    def __init__(self, client: PoliteClient | None = None):
        self.client = client or PoliteClient()
        self.user_agent = get_settings().user_agent
        self._robots: dict[str, RobotFileParser] = {}

    def robots(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base in self._robots:
            return self._robots[base]
        parser = RobotFileParser(base + "/robots.txt")
        try:
            response = self.client.get(base + "/robots.txt", retries=0)
        except TransientError:
            # Unreachable robots.txt: be conservative.
            parser.disallow_all = True
        else:
            if response.status_code in (401, 403):
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True
            else:
                parser.parse(response.text.splitlines())
        self._robots[base] = parser
        return parser

    def allowed(self, url: str) -> bool:
        return self.robots(url).can_fetch(self.user_agent, url)

    def fetch(self, url: str) -> tuple[str, str] | None:
        """(final url, html) for an allowed HTML page, else None."""
        if not url.startswith("http"):
            url = "https://" + url
        if not self.allowed(url):
            return None
        try:
            response = self.client.get(url, retries=1)
        except TransientError:
            return None
        if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
            return None
        final = str(response.url)
        if final != url and not self.allowed(final):
            return None
        return final, response.text[:2_000_000]
