"""Where decision-makers come from. Every source returns PersonCandidates whose
name, title and contacts were all read from one public document, with that
document's URL. Nothing is guessed or composed (no email-pattern guessing).

* WebsitePeopleSource   - the venue's own leadership / team / contact pages
* SearchPeopleSource    - web search API results for LinkedIn profiles; only the
                          search result title and snippet are used, linkedin.com
                          itself is never fetched
* HunterPeopleSource    - Hunter.io domain search; only emails Hunter reports
                          with public source URLs are kept
"""

import json
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urlsplit

from areyos.config import get_settings
from areyos.dedup import normalize_name
from areyos.enrichment.crawler import SiteCrawler, find_links, site_domain
from areyos.enrichment.website import EMAIL_RE, _clean_phone
from areyos.http import PoliteClient
from areyos.people import names, roles

LEADERSHIP_HINTS = (
    "leadership", "management", "our-team", "our team", "team", "board", "director", "administration",
    "who-we-are", "who we are", "about", "contact",
)
LINKEDIN_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_\-%]+", re.I)
BLOCK_TAGS = r"p|div|li|h[1-6]|td|th|tr|br|dt|dd|section|article|header|footer|figcaption|span|strong|b|a|button"
GENERIC_COMPANY_WORDS = {
    "hospital", "hospitals", "mall", "hotel", "hotels", "center", "centre", "park", "india", "college",
    "university", "institute", "station", "airport", "stadium", "museum", "and", "of", "the", "group",
    "multispeciality", "multi", "speciality", "super", "research", "resort", "resorts", "convention",
    "exhibition", "office", "complex", "international", "city", "medical", "sports", "industrial", "estate",
}


@dataclass
class PersonCandidate:
    full_name: str
    designation: str
    role_key: str
    company: str
    source: str  # website | search | hunter
    source_url: str
    evidence: str
    confidence: float
    linkedin_url: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VenueContext:
    prospect_id: int
    name: str
    industry: str
    city: str | None
    website: str | None


def company_match(venue_name: str, text: str) -> float:
    """0-1: how strongly `text` names the venue. Distinctive words count; generic ones don't."""
    venue = normalize_name(venue_name)
    haystack = f" {normalize_name(text)} "
    if f" {venue} " in haystack:
        return 1.0
    distinctive = [t for t in venue.split() if t not in GENERIC_COMPANY_WORDS and len(t) > 1]
    if not distinctive:
        return 0.0  # "City Hospital": only an exact phrase match is trustworthy
    hits = sum(1 for t in distinctive if f" {t} " in haystack)
    return hits / len(distinctive)


# ---------------------------------------------------------------- website

def html_to_lines(html: str) -> list[str]:
    html = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", html)
    # Keep profile / mailto / tel targets as text so they survive tag stripping.
    html = re.sub(
        r"""(?is)<a\s[^>]*href\s*=\s*["']((?:mailto:|tel:|https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/)[^"']+)["'][^>]*>""",
        lambda m: f" [[{m.group(1)}]] ",
        html,
    )
    html = re.sub(rf"(?i)</?(?:{BLOCK_TAGS})\b[^>]*>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return [names.clean(line) for line in text.split("\n") if names.clean(line)]


def _strip_links(line: str) -> str:
    return names.clean(re.sub(r"\[\[[^\]]+\]\]", " ", line))


def _contacts_in(lines: list[str]) -> tuple[str | None, list[str], list[str]]:
    blob = " ".join(lines)
    linkedin = next(iter(LINKEDIN_PROFILE_RE.findall(blob)), None)
    emails = sorted({e.lower() for e in EMAIL_RE.findall(blob)})
    phones = sorted({p for p in (_clean_phone(t) for t in re.findall(r"tel:([^\]\s]+)", blob)) if p})
    return (linkedin.rstrip("/") if linkedin else None), emails, phones


def extract_people_from_lines(lines: list[str]) -> list[tuple[str, str, int, int]]:
    """(name, designation, name_line, last_line_of_card) for every name/role pair found."""
    found = []
    used_names: set[str] = set()
    plain = [_strip_links(line) for line in lines]
    for i, line in enumerate(plain):
        if len(line) > 140 or roles.classify(line) is None:
            continue
        pair = None
        # "Dr. Asha Rao, Medical Superintendent" / "Asha Rao - CEO" on one line.
        parts = [p for p in re.split(r"\s*(?:,|\||–|—|:|\s-\s)\s*", line) if p]
        if len(parts) >= 2:
            for j, part in enumerate(parts):
                rest = ", ".join(parts[:j] + parts[j + 1:])
                if names.is_person_name(part) and roles.classify(rest):
                    pair = (names.clean(part), rest, i)
                    break
        # Card layout: name on the line above or below the title.
        if pair is None and i > 0 and names.is_person_name(plain[i - 1]):
            pair = (plain[i - 1], line, i - 1)
        if pair is None and i + 1 < len(plain) and len(line) <= 70 and names.is_person_name(plain[i + 1]):
            pair = (plain[i + 1], line, i + 1)
        if pair is None or names.person_key(pair[0]) in used_names:
            continue
        used_names.add(names.person_key(pair[0]))
        name_line = pair[2]
        end = max(i, name_line)
        # A card's contact details follow it, up to the next person's name.
        while end + 1 < len(plain) and end + 1 <= max(i, name_line) + 4 and not names.is_person_name(plain[end + 1]):
            end += 1
        found.append((pair[0], pair[1], name_line, end))
    return found


def _jsonld_people(html: str) -> list[dict]:
    people = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            if (kind == "Person" or (isinstance(kind, list) and "Person" in kind)) and node.get("name") and node.get("jobTitle"):
                people.append(node)
            for v in node.values():
                walk(v)

    for block in re.findall(r"""(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", html):
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return people


def extract_people_from_html(html: str, page_url: str, venue: VenueContext) -> list[PersonCandidate]:
    out: list[PersonCandidate] = []
    lines = html_to_lines(html)
    page_text = "\n".join(lines)
    for name, title, start, end in extract_people_from_lines(lines):
        role = roles.classify(title)
        if role is None or not names.appears_in(name, page_text):
            continue
        linkedin, emails, phones = _contacts_in(lines[start:end + 1])
        out.append(PersonCandidate(
            full_name=name, designation=title[:200], role_key=role.key, company=venue.name, source="website",
            source_url=page_url, evidence=" / ".join(_strip_links(x) for x in lines[start:end + 1])[:500],
            confidence=0.9, linkedin_url=linkedin, emails=emails, phones=phones,
        ))
    for node in _jsonld_people(html):
        name, title = names.clean(str(node["name"])), str(node["jobTitle"])
        role = roles.classify(title)
        if role is None or not names.is_person_name(name):
            continue
        same_as = node.get("sameAs") or []
        same_as = [same_as] if isinstance(same_as, str) else same_as
        linkedin = next((u for u in same_as if LINKEDIN_PROFILE_RE.match(str(u))), None)
        email = str(node.get("email", "")).removeprefix("mailto:").lower()
        phone = _clean_phone(str(node.get("telephone", ""))) if node.get("telephone") else None
        out.append(PersonCandidate(
            full_name=name, designation=title[:200], role_key=role.key, company=venue.name, source="website",
            source_url=page_url, evidence=f"schema.org Person: {name}, {title}"[:500], confidence=0.9,
            linkedin_url=linkedin, emails=[email] if EMAIL_RE.fullmatch(email) else [], phones=[phone] if phone else [],
        ))
    return out


class WebsitePeopleSource:
    name = "website"

    def __init__(self, crawler: SiteCrawler | None = None):
        self.crawler = crawler or SiteCrawler()
        self.max_pages = get_settings().people_max_pages

    def find(self, venue: VenueContext) -> list[PersonCandidate]:
        if not venue.website:
            return []
        home = self.crawler.fetch(venue.website)
        if home is None:
            return []
        home_url, home_html = home
        results = extract_people_from_html(home_html, home_url, venue)
        for link in find_links(home_html, home_url, LEADERSHIP_HINTS)[: self.max_pages - 1]:
            page = self.crawler.fetch(link)
            if page:
                results.extend(extract_people_from_html(page[1], page[0], venue))
        return results


# ---------------------------------------------------------------- web search

@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class BraveSearch:
    """https://api-dashboard.search.brave.com/app/documentation/web-search"""

    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, client: PoliteClient | None = None):
        self.api_key = api_key
        self.client = client or PoliteClient(per_host_delay=1.0)

    def search(self, query: str, count: int = 20) -> list[SearchResult]:
        r = self.client.get(
            self.URL,
            params={"q": query, "count": count, "country": "IN"},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
        )
        r.raise_for_status()
        return [
            SearchResult(unescape(re.sub(r"<[^>]+>", "", x.get("title", ""))), x["url"],
                         unescape(re.sub(r"<[^>]+>", "", x.get("description", ""))))
            for x in (r.json().get("web") or {}).get("results", [])
        ]


class GoogleProgrammableSearch:
    """https://developers.google.com/custom-search/v1/overview"""

    URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cx: str, client: PoliteClient | None = None):
        self.api_key, self.cx = api_key, cx
        self.client = client or PoliteClient(per_host_delay=0.2)

    def search(self, query: str, count: int = 10) -> list[SearchResult]:
        r = self.client.get(self.URL, params={"key": self.api_key, "cx": self.cx, "q": query, "num": min(count, 10)})
        r.raise_for_status()
        return [SearchResult(x.get("title", ""), x["link"], x.get("snippet", "")) for x in r.json().get("items", [])]


def parse_linkedin_result(result: SearchResult, venue: VenueContext) -> PersonCandidate | None:
    """Turn a LinkedIn profile search hit into a candidate, or None if it is not
    clearly a decision-maker at this venue."""
    match = LINKEDIN_PROFILE_RE.match(result.url)
    if not match:
        return None
    title = re.sub(r"\s*[|\-–]\s*LinkedIn.*$", "", result.title, flags=re.I)
    parts = [names.clean(p) for p in re.split(r"\s+[-–—|]\s+", title) if names.clean(p)]
    if not parts or not names.is_person_name(parts[0]):
        return None
    name = parts[0]
    text = f"{title} . {result.snippet}"

    designation = next((p for p in parts[1:] if roles.classify(p)), None)
    if designation is None:
        for chunk in re.split(r"\s*[·|•\n]\s*|\.\s+", result.snippet):
            if roles.classify(chunk):
                designation = names.clean(re.split(r"\s+at\s+|\s+@\s+", chunk)[0])
                break
    role = roles.classify(designation or "")
    if role is None:
        return None

    match_score = company_match(venue.name, text)
    if match_score < 0.6:
        return None
    confidence = 0.55 + 0.3 * match_score
    if venue.city and venue.city.lower() in text.lower():
        confidence += 0.05
    company = next((p for p in parts[1:] if company_match(venue.name, p) >= 0.6), venue.name)
    return PersonCandidate(
        full_name=name, designation=designation[:200], role_key=role.key, company=company[:300], source="search",
        source_url=match.group(0), evidence=f"{result.title} — {result.snippet}"[:500],
        confidence=round(min(confidence, 0.9), 2), linkedin_url=match.group(0).rstrip("/"),
    )


def build_queries(venue: VenueContext, max_queries: int) -> list[str]:
    ranked = sorted(roles.ROLES, key=lambda r: -roles.relevance(r.key, venue.industry, ""))
    groups = [ranked[i:i + 4] for i in range(0, len(ranked), 4)]
    queries = []
    for group in groups[:max_queries]:
        terms = " OR ".join(f'"{t}"' for r in group for t in r.search_terms[:1])
        queries.append(f'site:linkedin.com/in "{venue.name}" ({terms})')
    return queries


class SearchPeopleSource:
    name = "search"

    def __init__(self, engine=None):
        settings = get_settings()
        if engine is None:
            if settings.people_search_provider == "brave" and settings.brave_api_key:
                engine = BraveSearch(settings.brave_api_key)
            elif settings.people_search_provider == "google_cse" and settings.google_cse_key and settings.google_cse_cx:
                engine = GoogleProgrammableSearch(settings.google_cse_key, settings.google_cse_cx)
        self.engine = engine
        self.max_queries = settings.people_max_search_queries

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    def find(self, venue: VenueContext) -> list[PersonCandidate]:
        if not self.enabled:
            return []
        out = []
        for query in build_queries(venue, self.max_queries):
            for result in self.engine.search(query):
                candidate = parse_linkedin_result(result, venue)
                if candidate:
                    out.append(candidate)
        return out


# ---------------------------------------------------------------- Hunter.io

class HunterPeopleSource:
    """https://hunter.io/api-documentation/v2#domain-search"""

    name = "hunter"
    URL = "https://api.hunter.io/v2/domain-search"

    def __init__(self, api_key: str | None = None, client: PoliteClient | None = None):
        self.api_key = api_key or get_settings().hunter_api_key
        self.client = client or PoliteClient(per_host_delay=0.5)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def find(self, venue: VenueContext) -> list[PersonCandidate]:
        if not self.enabled or not venue.website:
            return []
        domain = site_domain(venue.website)
        r = self.client.get(self.URL, params={"domain": domain, "type": "personal", "limit": 50, "api_key": self.api_key})
        if r.status_code in (400, 404):
            return []
        r.raise_for_status()
        out = []
        for e in (r.json().get("data") or {}).get("emails", []):
            sources = [s.get("uri") for s in e.get("sources") or [] if s.get("uri")]
            name = names.clean(f"{e.get('first_name') or ''} {e.get('last_name') or ''}")
            role = roles.classify(e.get("position") or "")
            # Only emails Hunter actually found published somewhere; never pattern guesses.
            if not sources or role is None or not names.is_person_name(name) or not e.get("value"):
                continue
            linkedin = e.get("linkedin")
            if linkedin and not linkedin.startswith("http"):
                linkedin = f"https://www.linkedin.com/in/{linkedin.strip('/')}"
            phone = _clean_phone(e["phone_number"]) if e.get("phone_number") else None
            out.append(PersonCandidate(
                full_name=name, designation=e["position"][:200], role_key=role.key, company=venue.name,
                source="hunter", source_url=sources[0][:1000],
                evidence=f"Hunter.io: {e['value']} found on {len(sources)} public page(s)"[:500],
                confidence=0.8 if urlsplit(sources[0]).netloc.endswith(domain) else 0.7,
                linkedin_url=linkedin, emails=[e["value"].lower()], phones=[phone] if phone else [],
            ))
        return out
