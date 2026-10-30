import csv
import io
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, object_session, selectinload

from areyos import engine, queue
from areyos.api.crm import RepDep
from areyos.api.crm import router as crm_router
from areyos.crm import service as crm
from areyos.api.schemas import (
    AssessmentOut,
    ReportOut,
    PersonListItem,
    PersonOut,
    PersonPage,
    PersonUpdate,
    ProspectDetail,
    ProspectOut,
    ProspectPage,
    ProspectUpdate,
    RunCreate,
    RunOut,
)
from areyos.catalog.geography import STATES
from areyos.catalog.industries import INDUSTRIES
from areyos.db import get_session, session_scope
from areyos.config import get_settings
from areyos.models import (
    PIPELINE_STAGES,
    PRIORITY_LEVELS,
    PROSPECT_STATUSES,
    SIZE_CLASSES,
    Contact,
    DiscoveryRun,
    FollowUp,
    OpportunityAssessment,
    Person,
    PersonContact,
    Prospect,
    RunProspect,
    SalesRep,
)
from areyos.people.roles import ROLES
from areyos.people.service import suppress_person
from areyos.reports import service as reports
from areyos.reports.report import SECTIONS

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Areyos Lead Generator", version="1.0")
app.include_router(crm_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

SessionDep = Annotated[Session, Depends(get_session)]


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(session: SessionDep):
    session.execute(select(1))
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {
        "states": STATES,
        "industries": [{"key": i.key, "label": i.label} for i in INDUSTRIES.values()],
        "venue_sizes": list(SIZE_CLASSES),
        "priority_levels": list(PRIORITY_LEVELS),
        "statuses": list(PROSPECT_STATUSES),
        "stage_probability": PIPELINE_STAGES,
        "providers": engine.available_providers(),
        "roles": [{"key": r.key, "label": r.label} for r in ROLES],
        "people_sources": people_sources(),
    }


def people_sources() -> list[str]:
    settings = get_settings()
    if not settings.people_enabled:
        return []
    out = ["website"]
    if settings.people_search_provider in ("brave", "google_cse"):
        out.append(f"search:{settings.people_search_provider}")
    if settings.hunter_api_key:
        out.append("hunter")
    return out


# ---------------------------------------------------------------- runs

def _run_out(session: Session, run: DiscoveryRun) -> RunOut:
    out = RunOut.model_validate(run)
    out.stats = {**(run.stats or {}), **engine.run_stats(session, run.id)}
    out.stats.pop("area", None)
    return out


@app.post("/api/runs", response_model=RunOut, status_code=201)
def create_run(body: RunCreate, session: SessionDep):
    try:
        run = engine.create_run(session, **body.model_dump())
    except engine.RunValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return _run_out(session, run)


@app.get("/api/runs", response_model=list[RunOut])
def list_runs(session: SessionDep, limit: int = Query(25, le=200)):
    runs = session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(limit)).all()
    return [_run_out(session, r) for r in runs]


@app.get("/api/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, session: SessionDep):
    run = session.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return _run_out(session, run)


# ---------------------------------------------------------------- prospects

SORTS = {
    "score": Prospect.priority_score.desc(),
    "name": Prospect.name.asc(),
    "recent": Prospect.first_seen_at.desc(),
    "reviews": Prospect.rating_count.desc().nulls_last(),
    "opportunity": Prospect.opportunity_score.desc().nulls_last(),
    "deal": Prospect.deal_value.desc().nulls_last(),
    "activity": Prospect.last_activity_at.desc().nulls_last(),
}


class ProspectFilters:
    def __init__(
        self,
        state: str | None = None,
        city: str | None = None,
        industry: list[str] = Query(default=[]),
        size: list[str] = Query(default=[]),
        priority: list[str] = Query(default=[]),
        status: list[str] = Query(default=[]),
        run_id: int | None = None,
        q: str | None = None,
        has_email: bool | None = None,
        min_score: int | None = None,
        opportunity: list[str] = Query(default=[]),
        min_opportunity: int | None = None,
        owner_id: int | None = None,
        unassigned: bool | None = None,
        sort: str = "score",
    ):
        self.__dict__.update(locals())
        del self.__dict__["self"]

    def apply(self, stmt: Select) -> Select:
        stmt = stmt.where(Prospect.merged_into_id.is_(None))
        if self.owner_id is not None:
            stmt = stmt.where(Prospect.owner_id == self.owner_id)
        if self.unassigned is not None:
            stmt = stmt.where(Prospect.owner_id.is_(None) if self.unassigned else Prospect.owner_id.is_not(None))
        if self.state:
            stmt = stmt.where(func.lower(Prospect.state) == self.state.lower())
        if self.city:
            stmt = stmt.where(func.lower(Prospect.city) == self.city.lower())
        if self.industry:
            stmt = stmt.where(Prospect.industry.in_(self.industry))
        if self.size:
            stmt = stmt.where(Prospect.size_class.in_(self.size))
        if self.priority:
            stmt = stmt.where(Prospect.priority_level.in_(self.priority))
        if self.status:
            stmt = stmt.where(Prospect.status.in_(self.status))
        if self.run_id is not None:
            stmt = stmt.where(
                Prospect.id.in_(select(RunProspect.prospect_id).where(RunProspect.run_id == self.run_id))
            )
        if self.q:
            like = f"%{self.q}%"
            stmt = stmt.where(or_(Prospect.name.ilike(like), Prospect.address.ilike(like)))
        if self.has_email is not None:
            exists = select(Contact.id).where(Contact.prospect_id == Prospect.id, Contact.kind == "email").exists()
            stmt = stmt.where(exists if self.has_email else ~exists)
        if self.min_score is not None:
            stmt = stmt.where(Prospect.priority_score >= self.min_score)
        if self.opportunity:
            stmt = stmt.where(Prospect.opportunity_level.in_(self.opportunity))
        if self.min_opportunity is not None:
            stmt = stmt.where(Prospect.opportunity_score >= self.min_opportunity)
        return stmt.order_by(SORTS.get(self.sort, SORTS["score"]), Prospect.id)


FiltersDep = Annotated[ProspectFilters, Depends()]


@app.get("/api/prospects", response_model=ProspectPage)
def list_prospects(
    session: SessionDep,
    filters: FiltersDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    stmt = filters.apply(select(Prospect))
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    due = crm.next_follow_ups(session, [p.id for p in items])
    out = []
    for p in items:
        item = ProspectOut.model_validate(p)
        item.next_follow_up_at = due.get(p.id)
        out.append(item)
    return ProspectPage(total=total, page=page, page_size=page_size, items=out)


CSV_COLUMNS = [
    "id", "name", "industry", "opportunity_level", "opportunity_score", "opportunity_confidence",
    "priority_level", "priority_score", "size_class", "status", "owner", "deal_value", "expected_close_date",
    "next_follow_up_at", "last_activity_at", "rejection_reason",
    "city", "state", "postal_code", "address", "website", "phone", "emails", "linkedin",
    "rating", "rating_count", "business_status", "google_maps_url", "lat", "lng",
]


@app.get("/api/prospects.csv")
def export_csv(filters: FiltersDep):
    stmt = filters.apply(select(Prospect).options(selectinload(Prospect.contacts)))

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_COLUMNS)
        # The stream outlives the request, so it uses its own session.
        with session_scope() as session:
            owners = dict(session.execute(select(SalesRep.id, SalesRep.name)).all())
            follow = dict(session.execute(
                select(FollowUp.prospect_id, func.min(FollowUp.due_at)).where(FollowUp.status == "open")
                .group_by(FollowUp.prospect_id)).all())
            for p in session.scalars(stmt).yield_per(500):
                emails = "; ".join(c.value for c in p.contacts if c.kind == "email")
                linkedin = "; ".join(c.value for c in p.contacts if c.kind == "linkedin")
                values = {**{c: getattr(p, c, None) for c in CSV_COLUMNS}, "emails": emails, "linkedin": linkedin,
                          "owner": owners.get(p.owner_id), "next_follow_up_at": follow.get(p.id)}
                writer.writerow([values[c] if values[c] is not None else "" for c in CSV_COLUMNS])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

    return StreamingResponse(
        rows(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="areyos-prospects.csv"'}
    )


PEOPLE_ORDER = (Person.relevance.desc(), Person.match_confidence.desc(), Person.id)


def _latest_assessment(p: Prospect) -> OpportunityAssessment | None:
    return object_session(p).scalar(
        select(OpportunityAssessment)
        .where(OpportunityAssessment.prospect_id == p.id)
        .order_by(OpportunityAssessment.id.desc())
        .limit(1)
    )


def _detail(p: Prospect) -> ProspectDetail:
    data = ProspectOut.model_validate(p).model_dump()
    latest = _latest_assessment(p)
    report = reports.latest_report(object_session(p), p.id)
    people = sorted(p.people, key=lambda x: (-x.relevance, -x.match_confidence, x.id))
    return ProspectDetail(
        **data,
        people=[PersonOut.model_validate(x) for x in people],
        people_checked_at=p.people_checked_at,
        opportunity=AssessmentOut.model_validate(latest) if latest else None,
        report=ReportOut.model_validate(report) if report else None,
        types=p.types or [],
        attributes=p.attributes or {},
        size_signals=p.size_signals or {},
        score_breakdown=p.score_breakdown or {},
        contacts=p.contacts,
        sources=sorted({s.source for s in p.sources}),
    )


def _get_prospect(session: Session, prospect_id: int) -> Prospect:
    p = session.get(
        Prospect,
        prospect_id,
        options=[
            selectinload(Prospect.contacts),
            selectinload(Prospect.sources),
            selectinload(Prospect.people).selectinload(Person.contacts),
            selectinload(Prospect.people).selectinload(Person.sources),
        ],
    )
    if p is None:
        raise HTTPException(404, "prospect not found")
    return p


@app.get("/api/prospects/{prospect_id}", response_model=ProspectDetail)
def get_prospect(prospect_id: int, session: SessionDep):
    return _detail(_get_prospect(session, prospect_id))


@app.patch("/api/prospects/{prospect_id}", response_model=ProspectDetail)
def update_prospect(prospect_id: int, body: ProspectUpdate, session: SessionDep, rep: RepDep):
    p = _get_prospect(session, prospect_id)
    data = body.model_dump(exclude_unset=True)
    if "status" in data:
        try:
            crm.change_status(session, p, data["status"], rep, data.get("rejection_reason"))
        except crm.CRMError as exc:
            raise HTTPException(422, str(exc)) from exc
    if "notes" in data:
        p.notes = data["notes"]
    session.commit()
    return _detail(p)


@app.post("/api/prospects/{prospect_id}/enrich", status_code=202)
def enrich_prospect(prospect_id: int, session: SessionDep):
    p = _get_prospect(session, prospect_id)
    if not p.website:
        raise HTTPException(422, "prospect has no website to enrich from")
    p.enriched_at = None
    job = queue.enqueue(session, "enrich", {"prospect_id": p.id})
    session.commit()
    return {"job_id": job.id}


# ---------------------------------------------------------------- decision-makers

@app.post("/api/prospects/{prospect_id}/people", status_code=202)
def find_people(prospect_id: int, session: SessionDep):
    p = _get_prospect(session, prospect_id)
    if not people_sources():
        raise HTTPException(422, "decision-maker discovery is disabled")
    job = queue.enqueue(session, "people", {"prospect_id": p.id, "force": True})
    session.commit()
    return {"job_id": job.id}


@app.post("/api/runs/{run_id}/people", status_code=202)
def find_people_for_run(run_id: int, session: SessionDep):
    if session.get(DiscoveryRun, run_id) is None:
        raise HTTPException(404, "run not found")
    count = engine.enqueue_people_for_run(session, run_id)
    session.commit()
    return {"queued": count}


class PeopleFilters:
    def __init__(
        self,
        role: list[str] = Query(default=[]),
        industry: list[str] = Query(default=[]),
        state: str | None = None,
        city: str | None = None,
        prospect_id: int | None = None,
        run_id: int | None = None,
        status: list[str] = Query(default=[]),
        min_confidence: float | None = None,
        has_contact: bool | None = None,
        q: str | None = None,
    ):
        self.role, self.industry, self.state, self.city = role, industry, state, city
        self.prospect_id, self.run_id, self.status = prospect_id, run_id, status
        self.min_confidence, self.has_contact, self.q = min_confidence, has_contact, q

    def apply(self, stmt: Select) -> Select:
        stmt = stmt.join(Prospect, Person.prospect_id == Prospect.id)
        if self.role:
            stmt = stmt.where(Person.role.in_(self.role))
        if self.industry:
            stmt = stmt.where(Prospect.industry.in_(self.industry))
        if self.state:
            stmt = stmt.where(func.lower(Prospect.state) == self.state.lower())
        if self.city:
            stmt = stmt.where(func.lower(Prospect.city) == self.city.lower())
        if self.prospect_id is not None:
            stmt = stmt.where(Person.prospect_id == self.prospect_id)
        if self.run_id is not None:
            stmt = stmt.where(
                Person.prospect_id.in_(select(RunProspect.prospect_id).where(RunProspect.run_id == self.run_id))
            )
        if self.status:
            stmt = stmt.where(Person.status.in_(self.status))
        if self.min_confidence is not None:
            stmt = stmt.where(Person.match_confidence >= self.min_confidence)
        if self.has_contact is not None:
            exists = select(PersonContact.id).where(PersonContact.person_id == Person.id).exists()
            stmt = stmt.where(exists if self.has_contact else ~exists)
        if self.q:
            like = f"%{self.q}%"
            stmt = stmt.where(or_(Person.full_name.ilike(like), Person.designation.ilike(like), Prospect.name.ilike(like)))
        return stmt.order_by(*PEOPLE_ORDER)


PeopleFiltersDep = Annotated[PeopleFilters, Depends()]
PERSON_LOAD = (selectinload(Person.contacts), selectinload(Person.sources), selectinload(Person.prospect))


def _person_item(x: Person) -> PersonListItem:
    return PersonListItem(
        **PersonOut.model_validate(x).model_dump(),
        prospect_name=x.prospect.name,
        prospect_city=x.prospect.city,
        prospect_industry=x.prospect.industry,
    )


@app.get("/api/people", response_model=PersonPage)
def list_people(
    session: SessionDep,
    filters: PeopleFiltersDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    stmt = filters.apply(select(Person))
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.scalars(stmt.options(*PERSON_LOAD).offset((page - 1) * page_size).limit(page_size)).all()
    return PersonPage(total=total, page=page, page_size=page_size, items=[_person_item(x) for x in rows])


PEOPLE_CSV = [
    "full_name", "designation", "role", "company", "venue", "venue_id", "industry", "city", "linkedin_url",
    "emails", "phones", "match_confidence", "relevance", "status", "source_urls",
]


@app.get("/api/people.csv")
def export_people(filters: PeopleFiltersDep):
    stmt = filters.apply(select(Person)).options(*PERSON_LOAD)

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(PEOPLE_CSV)
        with session_scope() as session:
            for x in session.scalars(stmt).yield_per(500):
                writer.writerow([
                    x.full_name, x.designation, x.role, x.company, x.prospect.name, x.prospect_id,
                    x.prospect.industry, x.prospect.city or "", x.linkedin_url or "",
                    "; ".join(c.value for c in x.contacts if c.kind == "email"),
                    "; ".join(c.value for c in x.contacts if c.kind == "phone"),
                    x.match_confidence, x.relevance, x.status,
                    " ".join(sorted({s.source_url for s in x.sources})),
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

    return StreamingResponse(
        rows(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="areyos-decision-makers.csv"'}
    )


def _get_person(session: Session, person_id: int) -> Person:
    person = session.get(Person, person_id, options=list(PERSON_LOAD))
    if person is None:
        raise HTTPException(404, "person not found")
    return person


@app.patch("/api/people/{person_id}", response_model=PersonOut)
def update_person(person_id: int, body: PersonUpdate, session: SessionDep):
    person = _get_person(session, person_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(person, field, value)
    session.commit()
    return person


@app.delete("/api/people/{person_id}", status_code=204)
def delete_person(person_id: int, session: SessionDep, reason: str | None = None):
    """Erase a person and suppress them from future discovery (opt-out / erasure request)."""
    suppress_person(session, _get_person(session, person_id), reason)
    session.commit()


# ---------------------------------------------------------------- opportunity scoring

@app.post("/api/prospects/{prospect_id}/opportunity", status_code=202)
def assess_prospect(prospect_id: int, session: SessionDep):
    p = _get_prospect(session, prospect_id)
    job = queue.enqueue(session, "opportunity", {"prospect_id": p.id, "force": True})
    session.commit()
    return {"job_id": job.id}


@app.post("/api/runs/{run_id}/opportunity", status_code=202)
def assess_run(run_id: int, session: SessionDep):
    if session.get(DiscoveryRun, run_id) is None:
        raise HTTPException(404, "run not found")
    count = engine.enqueue_opportunity_for_run(session, run_id)
    session.commit()
    return {"queued": count}


@app.get("/api/prospects/{prospect_id}/opportunity", response_model=list[AssessmentOut])
def assessment_history(prospect_id: int, session: SessionDep):
    _get_prospect(session, prospect_id)
    return session.scalars(
        select(OpportunityAssessment)
        .where(OpportunityAssessment.prospect_id == prospect_id)
        .order_by(OpportunityAssessment.id.desc())
    ).all()


# ---------------------------------------------------------------- opportunity reports

@app.get("/api/report-sections")
def report_sections():
    return [{"key": k, "question": q} for k, q in SECTIONS]


@app.post("/api/prospects/{prospect_id}/report", status_code=202)
def request_report(prospect_id: int, session: SessionDep):
    p = _get_prospect(session, prospect_id)
    job = queue.enqueue(session, "report", {"prospect_id": p.id, "force": True})
    session.commit()
    return {"job_id": job.id}


@app.post("/api/runs/{run_id}/reports", status_code=202)
def request_run_reports(run_id: int, session: SessionDep):
    if session.get(DiscoveryRun, run_id) is None:
        raise HTTPException(404, "run not found")
    count = engine.enqueue_reports_for_run(session, run_id)
    session.commit()
    return {"queued": count}


@app.get("/api/prospects/{prospect_id}/report.md", response_class=PlainTextResponse)
def report_markdown(prospect_id: int, session: SessionDep):
    p = _get_prospect(session, prospect_id)
    report = reports.latest_report(session, p.id)
    if report is None:
        raise HTTPException(404, "no report yet")
    return PlainTextResponse(
        reports.to_markdown(p, session.get(OpportunityAssessment, report.assessment_id) if report.assessment_id else None, report),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="areyos-report-{p.id}.md"'},
    )


@app.get("/api/reports.md", response_class=PlainTextResponse)
def reports_markdown(session: SessionDep, filters: FiltersDep, limit: int = Query(200, le=2000)):
    """Every matching prospect's latest report in one Markdown file, in ranking order."""
    parts = []
    for p in session.scalars(filters.apply(select(Prospect)).limit(limit)):
        report = reports.latest_report(session, p.id)
        if report:
            assessment = session.get(OpportunityAssessment, report.assessment_id) if report.assessment_id else None
            parts.append(reports.to_markdown(p, assessment, report))
    return PlainTextResponse(
        "\n---\n\n".join(parts) or "No reports match.\n",
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="areyos-reports.md"'},
    )
