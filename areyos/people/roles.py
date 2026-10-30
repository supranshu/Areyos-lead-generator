"""Decision-maker roles Areyos sells to, how to recognise them in a job title,
and how much each matters per industry.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    pattern: re.Pattern
    # Phrases used in search queries.
    search_terms: tuple[str, ...]
    # Base relevance to an indoor-wayfinding purchase, 0-100.
    weight: int


def _p(rx: str) -> re.Pattern:
    return re.compile(rx, re.I)


# Order matters: the first match wins, so specific functional roles come before
# generic titles ("General Manager - Facilities" is a facility role).
ROLES: list[Role] = [
    Role("hospital_administrator", "Hospital Administrator",
         _p(r"hospital administrator|medical superintendent|medical director|\bchief administrative officer\b|"
            r"\b(head|director|chief)\b\W+(of\s+)?(hospital\s+)?administration|"
            r"(?<!system )(?<!systems )(?<!network )(?<!database )(?<!it )(?<!web )\badministrator\b"),
         ("Hospital Administrator", "Medical Superintendent"), 90),
    Role("facility_manager", "Facility Manager",
         _p(r"\bfacilit(y|ies)\b[\w\s&-]{0,20}\b(manager|head|director|in-?charge|lead)\b|"
            r"\b(manager|head|director|vp|vice president)\b\W+(of\s+)?(\w+\s+)?facilit(y|ies)\b"),
         ("Facility Manager", "Facilities Head"), 95),
    Role("venue_manager", "Venue Manager",
         _p(r"\b(venue|centre|center|mall|stadium|arena|campus|terminal|airport)\s+(manager|director|head)\b"),
         ("Venue Manager", "Centre Manager", "Mall Manager"), 90),
    Role("property_manager", "Property Manager",
         _p(r"\b(property|estate|estates|asset)\s+manager\b|"
            r"\b(head|director|vp)\b\W+(of\s+)?(real estate|property|properties|estates?)\b"),
         ("Property Manager",), 85),
    Role("infrastructure_head", "Infrastructure Head",
         _p(r"\binfrastructure\s+(head|manager|director|lead)\b|chief engineer|"
            r"\b(head|director|vp|vice president|chief)\b\W+(of\s+)?(infrastructure|projects|engineering|maintenance)\b"),
         ("Head of Infrastructure", "Infrastructure Head"), 85),
    Role("operations_director", "Operations Director",
         _p(r"\bcoo\b|chief operating officer|operations\s+(director|head)|"
            r"\b(director|head|vp|vice president)\b\W+(of\s+)?operations\b"),
         ("Operations Director", "Head of Operations", "COO"), 80),
    Role("procurement_manager", "Procurement Manager",
         _p(r"\bprocurement\b[\w\s-]{0,15}\b(manager|head|director|lead|officer)\b|chief procurement|"
            r"\bpurchase\s+(manager|head|director)\b|"
            r"\b(head|director|manager)\b\W+(of\s+)?(procurement|purchase|purchasing|sourcing)\b"),
         ("Procurement Manager", "Purchase Head"), 70),
    Role("marketing_director", "Marketing Director",
         _p(r"\bcmo\b|chief marketing officer|marketing\s+(director|head|manager)|"
            r"\b(director|head|vp|vice president)\b\W+(of\s+)?marketing\b"),
         ("Marketing Director", "Head of Marketing"), 65),
    Role("ceo", "CEO",
         _p(r"\bceo\b|chief executive( officer)?"),
         ("CEO",), 75),
    Role("managing_director", "Managing Director",
         _p(r"\bmanaging director\b"),
         ("Managing Director",), 75),
    Role("general_manager", "General Manager",
         _p(r"\bgeneral manager\b|\bgm\b"),
         ("General Manager",), 70),
]

ROLES_BY_KEY = {r.key: r for r in ROLES}

# Titles that look like a role but are not a decision-maker.
JUNIOR_RE = _p(r"\b(intern|trainee|student|executive assistant|aspiring|ex-|former|retired)\b")
DEPUTY_RE = _p(r"\b(assistant|asst\.?|deputy|associate|junior|jr\.?)\b")

# Which roles to favour for each industry (added to the base weight).
INDUSTRY_BOOST: dict[str, dict[str, int]] = {
    "healthcare": {"hospital_administrator": 10, "facility_manager": 5, "infrastructure_head": 5},
    "retail": {"venue_manager": 10, "property_manager": 10, "marketing_director": 10},
    "convention": {"venue_manager": 10, "operations_director": 5, "general_manager": 5},
    "education": {"infrastructure_head": 10, "facility_manager": 5, "hospital_administrator": -40},
    "transport": {"operations_director": 10, "infrastructure_head": 10, "venue_manager": 5},
    "corporate": {"facility_manager": 5, "property_manager": 10, "infrastructure_head": 5},
    "hospitality": {"general_manager": 20, "operations_director": 5, "hospital_administrator": -40},
    "government": {"infrastructure_head": 10, "procurement_manager": 10},
    "event_venues": {"venue_manager": 10, "general_manager": 10},
    "sports": {"venue_manager": 10, "operations_director": 5},
    "cultural": {"venue_manager": 5, "marketing_director": 5},
    "industrial": {"facility_manager": 5, "infrastructure_head": 10, "procurement_manager": 10},
}


def classify(title: str) -> Role | None:
    if not title or JUNIOR_RE.search(title):
        return None
    for role in ROLES:
        if role.pattern.search(title):
            return role
    return None


def relevance(role_key: str, industry_key: str, title: str) -> int:
    score = ROLES_BY_KEY[role_key].weight + INDUSTRY_BOOST.get(industry_key, {}).get(role_key, 0)
    if DEPUTY_RE.search(title):
        score -= 15
    return max(0, min(100, score))
