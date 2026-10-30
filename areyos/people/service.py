"""Merge decision-maker candidates into `people`, honouring suppressions."""

from datetime import datetime, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from areyos.config import get_settings
from areyos.models import Person, PersonContact, PersonSource, Prospect, Suppression
from areyos.people import names, roles
from areyos.people.sources import PersonCandidate, VenueContext

AGREEMENT_BONUS = 0.05
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def venue_context(prospect: Prospect) -> VenueContext:
    return VenueContext(prospect.id, prospect.name, prospect.industry, prospect.city, prospect.website)


def wants_people(prospect: Prospect) -> bool:
    settings = get_settings()
    if not settings.people_enabled:
        return False
    if PRIORITY_RANK[prospect.priority_level] > PRIORITY_RANK.get(settings.people_min_priority, 1):
        return False
    if prospect.people_checked_at is None:
        return True
    age = datetime.now(timezone.utc) - prospect.people_checked_at
    return age.days >= settings.people_refresh_days


def suppression_keys(prospect_id: int, c: PersonCandidate) -> list[tuple[str, str]]:
    keys = [("person", f"{prospect_id}:{names.person_key(c.full_name)}")]
    if c.linkedin_url:
        keys.append(("linkedin", c.linkedin_url.lower().rstrip("/")))
    keys += [("email", e.lower()) for e in c.emails]
    return keys


def is_suppressed(session: Session, prospect_id: int, c: PersonCandidate) -> bool:
    keys = suppression_keys(prospect_id, c)
    return session.scalar(select(Suppression.id).where(tuple_(Suppression.kind, Suppression.value).in_(keys)).limit(1)) is not None


def suppress_person(session: Session, person: Person, reason: str | None) -> None:
    """Delete a person and make sure no later run stores them again."""
    keys = {("person", f"{person.prospect_id}:{person.person_key}")}
    if person.linkedin_url:
        keys.add(("linkedin", person.linkedin_url.lower().rstrip("/")))
    keys |= {("email", c.value.lower()) for c in person.contacts if c.kind == "email"}
    for kind, value in keys:
        session.execute(insert(Suppression).values(kind=kind, value=value, reason=reason).on_conflict_do_nothing())
    session.delete(person)


def _find(session: Session, prospect_id: int, c: PersonCandidate, key: str) -> Person | None:
    person = session.scalar(select(Person).where(Person.prospect_id == prospect_id, Person.person_key == key))
    if person is None and c.linkedin_url:
        person = session.scalar(
            select(Person).where(Person.prospect_id == prospect_id, Person.linkedin_url == c.linkedin_url)
        )
    return person


def merge_candidates(session: Session, prospect: Prospect, candidates: list[PersonCandidate]) -> dict:
    min_conf = get_settings().people_min_confidence
    stats = {"candidates": len(candidates), "stored": 0, "new": 0, "suppressed": 0, "low_confidence": 0}
    now = datetime.now(timezone.utc)
    for c in sorted(candidates, key=lambda c: -c.confidence):
        if c.confidence < min_conf:
            stats["low_confidence"] += 1
            continue
        if is_suppressed(session, prospect.id, c):
            stats["suppressed"] += 1
            continue
        key = names.person_key(c.full_name)
        person = _find(session, prospect.id, c, key)
        if person is None:
            person = Person(
                prospect_id=prospect.id, full_name=c.full_name, person_key=key, designation=c.designation,
                role=c.role_key, company=c.company, linkedin_url=c.linkedin_url, match_confidence=c.confidence,
                relevance=roles.relevance(c.role_key, prospect.industry, c.designation), status="unverified",
            )
            session.add(person)
            session.flush()
            stats["new"] += 1
        else:
            known = {s.source for s in person.sources}
            if c.confidence > person.match_confidence:
                # The most trustworthy source sets the title.
                person.designation, person.role = c.designation, c.role_key
                person.relevance = roles.relevance(c.role_key, prospect.industry, c.designation)
            person.match_confidence = min(
                0.98, max(person.match_confidence, c.confidence) + (AGREEMENT_BONUS if c.source not in known else 0)
            )
            person.linkedin_url = person.linkedin_url or c.linkedin_url
            person.last_seen_at = now
        session.execute(
            insert(PersonSource)
            .values(person_id=person.id, source=c.source, source_url=c.source_url, evidence=c.evidence,
                    designation=c.designation, confidence=c.confidence)
            .on_conflict_do_nothing()
        )
        for kind, values in (("email", c.emails), ("phone", c.phones)):
            for v in values:
                session.execute(
                    insert(PersonContact)
                    .values(person_id=person.id, kind=kind, value=v, source_url=c.source_url)
                    .on_conflict_do_nothing()
                )
        session.flush()
        session.expire(person, ["sources"])
        stats["stored"] += 1
    prospect.people_checked_at = now
    return stats
