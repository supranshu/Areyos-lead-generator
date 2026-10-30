"""Collect public business contact details from a venue's own website.

Only pages the site's robots.txt allows are fetched: the homepage plus a few
same-site contact/about pages. Every contact is stored with the URL it came
from so it can be checked before outreach.
"""

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlsplit

from areyos.config import get_settings
from areyos.enrichment.crawler import TAG_RE, SiteCrawler, find_links

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
# Indian numbers: +91 / 0 prefixes, mobile (10 digits) and landline with STD code.
PHONE_RE = re.compile(r"(?:\+91[\s-]?|0)?(?:\d[\s-]?){9,11}\d")
CONTACT_HINTS = ("contact", "reach-us", "reach us", "enquir", "inquir", "get-in-touch", "about", "location")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
IGNORED_EMAIL_DOMAINS = ("example.com", "sentry.io", "wixpress.com", "domain.com", "email.com")
MAX_PER_KIND = 5


@dataclass(frozen=True)
class FoundContact:
    kind: str  # email | phone | linkedin | contact_page
    value: str
    source_url: str


def _clean_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    digits = digits.lstrip("0")
    if len(digits) != 10:
        return None
    return f"+91 {digits}"


def extract_contacts(html: str, page_url: str) -> list[FoundContact]:
    found: list[FoundContact] = []
    text = unescape(html)
    for email in set(EMAIL_RE.findall(text)):
        email = email.lower().strip(".")
        if email.endswith(IGNORED_EMAIL_SUFFIXES) or email.split("@")[1] in IGNORED_EMAIL_DOMAINS:
            continue
        found.append(FoundContact("email", email, page_url))

    phones = set(re.findall(r"""href\s*=\s*["']tel:([^"']+)""", text, re.I))
    visible = TAG_RE.sub(" ", text)
    phones |= set(PHONE_RE.findall(visible))
    for phone in {p for p in (_clean_phone(x) for x in phones) if p}:
        found.append(FoundContact("phone", phone, page_url))

    for link in set(re.findall(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9_\-%.]+", text, re.I)):
        found.append(FoundContact("linkedin", link.rstrip("/"), page_url))
    return found


def contact_links(html: str, page_url: str) -> list[str]:
    return find_links(html, page_url, CONTACT_HINTS)


class WebsiteEnricher:
    def __init__(self, crawler: SiteCrawler | None = None):
        self.crawler = crawler or SiteCrawler()
        self.max_pages = get_settings().enrich_max_pages

    def enrich(self, website: str) -> list[FoundContact]:
        page = self.crawler.fetch(website)
        if page is None:
            return []
        final_url, html = page
        results = extract_contacts(html, final_url)
        for link in contact_links(html, final_url)[: self.max_pages - 1]:
            sub = self.crawler.fetch(link)
            if sub is None:
                continue
            results.append(FoundContact("contact_page", sub[0], sub[0]))
            results.extend(extract_contacts(sub[1], sub[0]))
        return _dedupe(results, final_url)


def _dedupe(results: list[FoundContact], site: str) -> list[FoundContact]:
    site_domain = urlsplit(site).netloc.lower().removeprefix("www.")
    seen: set[tuple[str, str]] = set()
    by_kind: dict[str, list[FoundContact]] = {}
    for c in results:
        if (c.kind, c.value) in seen:
            continue
        seen.add((c.kind, c.value))
        by_kind.setdefault(c.kind, []).append(c)
    # Emails on the venue's own domain first.
    if "email" in by_kind:
        by_kind["email"].sort(key=lambda c: 0 if c.value.split("@")[1].endswith(site_domain) else 1)
    return [c for kind in by_kind.values() for c in kind[:MAX_PER_KIND]]
