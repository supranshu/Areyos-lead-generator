"""Person-name checks. A name is only accepted if it looks like a person's name
and appears verbatim in the source text it was taken from."""

import re

from areyos.dedup import normalize_name

HONORIFIC = r"(?:Dr|Mr|Mrs|Ms|Miss|Prof|Shri|Smt|Sri|Capt|Col|Lt|Maj|Brig|Gen|Er|Adv|CA)\.?"
TOKEN = r"(?:[A-Z][a-zA-Z'\-]+|[A-Z]\.(?:\s?[A-Z]\.)*)"
NAME_RE = re.compile(rf"^(?:{HONORIFIC}\s+)?{TOKEN}(?:\s+{TOKEN}){{1,4}}$")
HONORIFIC_RE = re.compile(rf"^{HONORIFIC}\s+", re.I)

# Words that appear in capitalised headings but are never part of a person's name.
NOT_NAME_WORDS = {
    "about", "abroad", "academy", "administration", "advisory", "all", "and", "board", "care", "center", "centre",
    "chairman", "chief", "college", "committee", "company", "contact", "corporate", "council", "department",
    "director", "directors", "executive", "facilities", "facility", "general", "group", "head", "home", "hospital",
    "hotel", "india", "infrastructure", "institute", "leadership", "limited", "ltd", "mall", "management", "manager",
    "marketing", "medical", "meet", "message", "more", "our", "operations", "park", "private", "procurement",
    "profile", "read", "services", "team", "the", "trust", "university", "us", "view", "welcome", "who", "with",
    "linkedin", "officer", "president", "founder", "mumbai", "delhi", "bengaluru", "chennai", "pune", "hyderabad",
}


def clean(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" ,|-–—:;")


def is_person_name(text: str) -> bool:
    text = clean(text)
    if not 4 <= len(text) <= 60 or not NAME_RE.match(text):
        return False
    words = HONORIFIC_RE.sub("", text).lower().replace(".", " ").split()
    if any(w in NOT_NAME_WORDS for w in words):
        return False
    # At least one token longer than an initial.
    return any(len(w) > 1 for w in words)


def person_key(name: str) -> str:
    """Comparable form: no honorific, lowercase tokens."""
    return normalize_name(HONORIFIC_RE.sub("", clean(name)))


def appears_in(name: str, source_text: str) -> bool:
    """Guard against fabrication: the exact name must be present in the source."""
    flat = re.sub(r"\s+", " ", source_text)
    return clean(name).lower() in flat.lower()
