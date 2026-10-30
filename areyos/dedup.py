"""Merge raw source records into canonical prospects.

A raw place maps onto an existing prospect when either
  * the same (source, source_id) was seen before, or
  * a prospect lies within ~250 m and its name is similar enough.
Callers hold a per-city advisory lock while merging (see engine.merge_places),
so concurrent workers cannot create the same venue twice.
"""

import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from areyos.catalog.geography import normalize_state
from areyos.catalog.industries import get_industry
from areyos.models import Contact, Prospect, ProspectSource
from areyos.providers.base import RawPlace
from areyos import scoring

NEAR_DEGREES = 0.0025  # ~250 m
NAME_SIMILARITY = 0.6
STOPWORDS = {"the", "pvt", "private", "ltd", "limited", "llp", "inc", "of", "and"}
SIZE_TAGS = ("beds", "rooms", "capacity", "iata", "stars", "building:levels")


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("centre", "center")
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(t for t in tokens if t not in STOPWORDS)


def name_similarity(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    if ta <= tb or tb <= ta:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def find_existing(session: Session, raw: RawPlace, normalized: str) -> Prospect | None:
    source = session.scalar(
        select(ProspectSource).where(ProspectSource.source == raw.source, ProspectSource.source_id == raw.source_id)
    )
    if source:
        return source.prospect
    if raw.lat is None or raw.lng is None:
        return None
    nearby = session.scalars(
        select(Prospect).where(
            Prospect.lat.between(raw.lat - NEAR_DEGREES, raw.lat + NEAR_DEGREES),
            Prospect.lng.between(raw.lng - NEAR_DEGREES, raw.lng + NEAR_DEGREES),
        )
    ).all()
    best = max(nearby, key=lambda p: name_similarity(normalized, p.normalized_name), default=None)
    if best and name_similarity(normalized, best.normalized_name) >= NAME_SIMILARITY:
        return best
    return None


def _fill(prospect: Prospect, attr: str, value, prefer: bool) -> None:
    if value in (None, "", []):
        return
    if prefer or getattr(prospect, attr) in (None, ""):
        setattr(prospect, attr, value)


def upsert_place(
    session: Session, raw: RawPlace, industry_key: str, default_city: str, default_state: str
) -> tuple[Prospect, bool]:
    normalized = normalize_name(raw.name)
    prospect = find_existing(session, raw, normalized)
    is_new = prospect is None
    if is_new:
        prospect = Prospect(
            name=raw.name,
            normalized_name=normalized,
            industry=industry_key,
            types=[],
            attributes={},
            size_signals={},
            score_breakdown={},
        )
        session.add(prospect)

    # Google is the better source for live business data; OSM fills gaps.
    google = raw.source == "google_places"
    _fill(prospect, "name", raw.name, google)
    if google:
        prospect.normalized_name = normalized
    _fill(prospect, "address", raw.address, google)
    _fill(prospect, "city", raw.city, google)
    _fill(prospect, "state", normalize_state(raw.state), google)
    _fill(prospect, "postal_code", raw.postal_code, google)
    _fill(prospect, "lat", raw.lat, google)
    _fill(prospect, "lng", raw.lng, google)
    _fill(prospect, "website", raw.website, google)
    _fill(prospect, "phone", raw.phone, google)
    _fill(prospect, "google_maps_url", raw.maps_url, google)
    _fill(prospect, "rating", raw.rating, google)
    _fill(prospect, "rating_count", raw.rating_count, google)
    _fill(prospect, "business_status", raw.business_status, google)
    prospect.city = prospect.city or default_city
    prospect.state = prospect.state or default_state
    prospect.types = sorted(set(prospect.types or []) | set(raw.types))

    attributes = dict(prospect.attributes or {})
    attributes.update({k: raw.tags[k] for k in SIZE_TAGS if k in raw.tags})
    if raw.footprint_m2:
        attributes["footprint_m2"] = max(raw.footprint_m2, attributes.get("footprint_m2") or 0)
    prospect.attributes = attributes
    prospect.last_seen_at = datetime.now(timezone.utc)

    session.flush()  # assigns prospect.id
    source = session.scalar(
        select(ProspectSource).where(ProspectSource.source == raw.source, ProspectSource.source_id == raw.source_id)
    )
    if source is None:
        session.add(ProspectSource(prospect_id=prospect.id, source=raw.source, source_id=raw.source_id, raw=raw.raw))
    else:
        source.raw = raw.raw
        source.fetched_at = datetime.now(timezone.utc)

    rescore(session, prospect)
    return prospect, is_new


def rescore(session: Session, prospect: Prospect) -> None:
    industry = get_industry(prospect.industry)
    prospect.size_class, prospect.size_signals = scoring.classify_size(
        industry, prospect.name, prospect.rating_count, prospect.attributes or {}
    )
    has_email = (
        session.scalar(
            select(Contact.id).where(Contact.prospect_id == prospect.id, Contact.kind == "email").limit(1)
        )
        is not None
        if prospect.id
        else False
    )
    prospect.priority_score, prospect.priority_level, prospect.score_breakdown = scoring.score(
        industry_key=prospect.industry,
        name=prospect.name,
        size_class=prospect.size_class,
        rating_count=prospect.rating_count,
        types=prospect.types or [],
        website=prospect.website,
        phone=prospect.phone,
        has_email=has_email,
        business_status=prospect.business_status,
    )
