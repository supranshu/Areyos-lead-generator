from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SizeClass = Literal["small", "medium", "large", "mega", "unknown"]
PriorityLevel = Literal["high", "medium", "low"]
Status = Literal["new", "researching", "qualified", "contacted", "meeting_scheduled", "demo_scheduled",
                 "negotiation", "pilot_proposed", "pilot_confirmed", "converted", "rejected"]
ProviderName = Literal["google_places", "osm"]


class RunCreate(BaseModel):
    state: str = Field(min_length=1)
    city: str = Field(min_length=1)
    industries: list[str] = Field(default_factory=list, description="Empty means all industries")
    venue_sizes: list[SizeClass] = Field(default_factory=list, description="Result filter; empty means any")
    priority_levels: list[PriorityLevel] = Field(default_factory=list, description="Result filter; empty means any")
    providers: list[ProviderName] = Field(default_factory=list, description="Empty means every configured provider")
    find_people: bool = Field(False, description="Also identify decision-makers at qualifying prospects")
    assess_opportunity: bool = Field(False, description="Also score indoor-navigation opportunity (0-100)")
    generate_reports: bool = Field(False, description="Also write an evidence-cited opportunity report per prospect (implies scoring)")


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    state: str
    city: str
    industries: list[str]
    venue_sizes: list[str]
    priority_levels: list[str]
    providers: list[str]
    find_people: bool
    assess_opportunity: bool
    generate_reports: bool
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    stats: dict = Field(default_factory=dict)


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    value: str
    source_url: str
    found_at: datetime


class ProspectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    industry: str
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    lat: float | None
    lng: float | None
    website: str | None
    phone: str | None
    google_maps_url: str | None
    rating: float | None
    rating_count: int | None
    business_status: str | None
    size_class: str
    priority_score: int
    priority_level: str
    status: str
    notes: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    enriched_at: datetime | None
    opportunity_score: int | None
    opportunity_level: str | None
    opportunity_confidence: float | None
    opportunity_assessed_at: datetime | None
    owner_id: int | None
    deal_value: float | None
    expected_close_date: date | None
    status_changed_at: datetime | None
    rejection_reason: str | None
    last_activity_at: datetime | None
    source: str
    next_follow_up_at: datetime | None = None


class PersonContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    value: str
    source_url: str


class PersonSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    source_url: str
    evidence: str
    designation: str
    confidence: float
    found_at: datetime


class PersonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prospect_id: int
    full_name: str
    designation: str
    role: str
    company: str
    linkedin_url: str | None
    match_confidence: float
    relevance: int
    status: str
    notes: str | None
    contacts: list[PersonContactOut]
    sources: list[PersonSourceOut]


class PersonListItem(PersonOut):
    prospect_name: str
    prospect_city: str | None
    prospect_industry: str


class PersonPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PersonListItem]


class PersonUpdate(BaseModel):
    status: Literal["unverified", "verified", "rejected"] | None = None
    notes: str | None = None


class EvidenceOut(BaseModel):
    source: str
    url: str | None
    quote: str


class FactorOut(BaseModel):
    key: str
    label: str
    basis: Literal["verified", "estimated", "unknown"]
    numeric_value: float | None
    category: str | None
    finding: str
    reasoning: str
    evidence: list[EvidenceOut]
    method: str
    notes: list[str]
    points: float
    max_points: int
    points_explained: str


class AssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    score: int
    level: str
    confidence: float
    method: str
    model: str | None
    summary: str
    note: str | None
    factors: list[FactorOut]
    documents: list[dict]
    created_at: datetime

    @field_validator("documents")
    @classmethod
    def _drop_page_text(cls, docs: list[dict]) -> list[dict]:
        return [{k: v for k, v in d.items() if k != "text"} for d in docs]


class CitationOut(BaseModel):
    ref: str
    quote: str | None
    source: str
    url: str | None
    basis: str


class StatementOut(BaseModel):
    text: str
    kind: Literal["evidence", "inference", "suggestion", "gap"]
    citations: list[CitationOut]


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    assessment_id: int | None
    method: str
    model: str | None
    evidence_level: str
    sections: dict[str, list[StatementOut]]
    sources: list[dict]
    notes: list[str]
    note: str | None
    created_at: datetime


class ProspectDetail(ProspectOut):
    types: list[str]
    attributes: dict
    size_signals: dict
    score_breakdown: dict
    contacts: list[ContactOut]
    sources: list[str]
    people: list[PersonOut]
    people_checked_at: datetime | None
    opportunity: AssessmentOut | None
    report: ReportOut | None


class ProspectPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ProspectOut]


class ProspectUpdate(BaseModel):
    status: Status | None = None
    rejection_reason: str | None = None
    notes: str | None = None
