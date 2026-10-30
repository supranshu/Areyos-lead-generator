from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from areyos.db import Base

SIZE_CLASSES = ("small", "medium", "large", "mega", "unknown")
PRIORITY_LEVELS = ("high", "medium", "low")
# Sales pipeline, in order. The probability weights deal value in pipeline forecasts.
PIPELINE_STAGES = {
    "new": 0.0,
    "researching": 0.02,
    "qualified": 0.1,
    "contacted": 0.15,
    "meeting_scheduled": 0.25,
    "demo_scheduled": 0.35,
    "negotiation": 0.5,
    "pilot_proposed": 0.6,
    "pilot_confirmed": 0.8,
    "converted": 1.0,
    "rejected": 0.0,
}
PROSPECT_STATUSES = tuple(PIPELINE_STAGES)
CLOSED_STATUSES = ("converted", "rejected")


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    industry: Mapped[str] = mapped_column(String(50), index=True)
    sub_type: Mapped[str | None] = mapped_column(String(100))

    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(120), index=True)
    postal_code: Mapped[str | None] = mapped_column(String(20))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)

    website: Mapped[str | None] = mapped_column(String(1000))
    phone: Mapped[str | None] = mapped_column(String(50))
    google_maps_url: Mapped[str | None] = mapped_column(String(1000))
    rating: Mapped[float | None] = mapped_column(Float)
    rating_count: Mapped[int | None] = mapped_column(Integer)
    business_status: Mapped[str | None] = mapped_column(String(40))
    types: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # Merged size-relevant facts from all sources: beds, rooms, capacity, iata, footprint_m2.
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)

    size_class: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    size_signals: Mapped[dict] = mapped_column(JSONB, default=dict)
    priority_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    priority_level: Mapped[str] = mapped_column(String(10), default="low", index=True)
    score_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)

    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # CRM
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id", ondelete="SET NULL"), index=True)
    deal_value: Mapped[float | None] = mapped_column(Numeric(14, 2))  # INR
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when this lead was merged into another; merged leads are hidden from lists.
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), default="discovery", server_default="discovery")  # discovery | manual

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    people_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Latest opportunity assessment (denormalised for ranking and filtering).
    opportunity_score: Mapped[int | None] = mapped_column(Integer, index=True)
    opportunity_level: Mapped[str | None] = mapped_column(String(10), index=True)
    opportunity_confidence: Mapped[float | None] = mapped_column(Float)
    opportunity_assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sources: Mapped[list["ProspectSource"]] = relationship(back_populates="prospect", cascade="all, delete-orphan")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="prospect", cascade="all, delete-orphan")
    people: Mapped[list["Person"]] = relationship(back_populates="prospect", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_prospects_geo", "lat", "lng"),)


class ProspectSource(Base):
    """One record per (source, source id); several sources can map to one prospect."""

    __tablename__ = "prospect_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(300))
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prospect: Mapped[Prospect] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_prospect_sources_source_id"),)


class Contact(Base):
    """Public business contact details, always with the page they were found on."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | phone | linkedin | contact_page
    value: Mapped[str] = mapped_column(String(1000))
    source_url: Mapped[str] = mapped_column(String(1000))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prospect: Mapped[Prospect] = relationship(back_populates="contacts")

    __table_args__ = (UniqueConstraint("prospect_id", "kind", "value", name="uq_contacts_value"),)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120))
    industries: Mapped[list[str]] = mapped_column(ARRAY(String))
    venue_sizes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    priority_levels: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    providers: Mapped[list[str]] = mapped_column(ARRAY(String))
    generate_reports: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    assess_opportunity: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    find_people: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunProspect(Base):
    __tablename__ = "run_prospects"

    run_id: Mapped[int] = mapped_column(ForeignKey("discovery_runs.id", ondelete="CASCADE"), primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), primary_key=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)


class Job(Base):
    """Postgres-backed work queue; workers claim rows with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # plan | discover | enrich
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)


class Person(Base):
    """A decision-maker at one venue. Every field was read from a cited public source."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    person_key: Mapped[str] = mapped_column(String(200))
    designation: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(40), index=True)
    company: Mapped[str] = mapped_column(String(300))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    match_confidence: Mapped[float] = mapped_column(Float)
    relevance: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unverified")  # unverified | verified | rejected
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prospect: Mapped[Prospect] = relationship(back_populates="people")
    contacts: Mapped[list["PersonContact"]] = relationship(back_populates="person", cascade="all, delete-orphan")
    sources: Mapped[list["PersonSource"]] = relationship(back_populates="person", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("prospect_id", "person_key", name="uq_people_prospect_person"),)


class PersonContact(Base):
    __tablename__ = "person_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | phone
    value: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(String(1000))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    person: Mapped[Person] = relationship(back_populates="contacts")

    __table_args__ = (UniqueConstraint("person_id", "kind", "value", name="uq_person_contacts_value"),)


class PersonSource(Base):
    __tablename__ = "person_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # website | search | hunter
    source_url: Mapped[str] = mapped_column(String(1000))
    evidence: Mapped[str] = mapped_column(Text)
    designation: Mapped[str] = mapped_column(String(300))
    confidence: Mapped[float] = mapped_column(Float)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    person: Mapped[Person] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("person_id", "source_url", name="uq_person_sources_url"),)


class Suppression(Base):
    """Opt-outs and erasure requests: matching people are never stored again."""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # linkedin | email | person (value = "<prospect_id>:<person_key>")
    value: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppressions_value"),)


class OpportunityAssessment(Base):
    """One explained 0-100 opportunity score. Kept as history; the prospect carries the latest."""

    __tablename__ = "opportunity_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(10))
    # Share of the score's weight backed by verified sources (0-1).
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(20))  # ai | heuristic
    model: Mapped[str | None] = mapped_column(String(60))
    summary: Mapped[str] = mapped_column(Text)
    factors: Mapped[list] = mapped_column(JSONB)
    documents: Mapped[list] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpportunityReport(Base):
    """Seven-question sales report. Every statement is tagged and its citations were checked in code."""

    __tablename__ = "opportunity_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_assessments.id", ondelete="SET NULL"))
    method: Mapped[str] = mapped_column(String(20))  # ai | template
    model: Mapped[str | None] = mapped_column(String(60))
    evidence_level: Mapped[str] = mapped_column(String(20), index=True)  # strong | partial | insufficient
    sections: Mapped[dict] = mapped_column(JSONB)
    sources: Mapped[list] = mapped_column(JSONB)
    notes: Mapped[list] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesRep(Base):
    __tablename__ = "sales_reps"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(300), unique=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    # Industries this rep covers, for automatic assignment; empty means all.
    industries: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Activity(Base):
    """One entry on a lead's timeline: calls, meetings, emails, notes and system events."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id", ondelete="SET NULL"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    # call | meeting | email | note | status_change | assignment | deal_update | merge
    kind: Mapped[str] = mapped_column(String(20), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(String(10))  # inbound | outbound
    # call: connected | no_answer | voicemail | busy | wrong_number
    # meeting: held | no_show | rescheduled | cancelled
    outcome: Mapped[str | None] = mapped_column(String(30))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    # Email tracking.
    email_to: Mapped[str | None] = mapped_column(String(300))
    email_status: Mapped[str | None] = mapped_column(String(20))  # sent | opened | replied | bounced
    email_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)  # from/to status, old/new owner, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowUp(Base):
    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="task")  # call | email | meeting | demo | task
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | done | cancelled
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeoCache(Base):
    __tablename__ = "geo_cache"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
