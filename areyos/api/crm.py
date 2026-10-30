"""CRM endpoints. The acting sales rep is sent as the X-Areyos-Rep header (a rep id).

There is no authentication in this app: put it behind your SSO / reverse proxy
before exposing it beyond a trusted network.
"""

import base64
import csv
import io
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from areyos.catalog.industries import INDUSTRIES
from areyos.crm import service as crm
from areyos.dedup import normalize_name, rescore
from areyos.db import get_session, session_scope
from areyos.models import PIPELINE_STAGES, Activity, FollowUp, Person, Prospect, SalesRep

router = APIRouter(prefix="/api", tags=["crm"])
SessionDep = Annotated[Session, Depends(get_session)]
StatusName = Literal[tuple(PIPELINE_STAGES)]


def acting_rep(x_areyos_rep: Annotated[int | None, Header()] = None) -> int | None:
    return x_areyos_rep


RepDep = Annotated[int | None, Depends(acting_rep)]


def _lead(session: Session, prospect_id: int) -> Prospect:
    p = session.get(Prospect, prospect_id)
    if p is None:
        raise HTTPException(404, "lead not found")
    if p.merged_into_id:
        raise HTTPException(409, f"lead was merged into #{p.merged_into_id}")
    return p


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except crm.CRMError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- sales reps

class RepIn(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    phone: str | None = None
    industries: list[str] = Field(default_factory=list)
    active: bool = True


class RepPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    industries: list[str] | None = None
    active: bool | None = None


class RepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    phone: str | None
    industries: list[str]
    active: bool


@router.get("/reps", response_model=list[RepOut])
def list_reps(session: SessionDep):
    return session.scalars(select(SalesRep).order_by(SalesRep.name)).all()


@router.post("/reps", response_model=RepOut, status_code=201)
def create_rep(body: RepIn, session: SessionDep):
    if session.scalar(select(SalesRep).where(func.lower(SalesRep.email) == body.email.lower())):
        raise HTTPException(409, "a rep with this email already exists")
    rep = SalesRep(**body.model_dump())
    session.add(rep)
    session.commit()
    return rep


@router.patch("/reps/{rep_id}", response_model=RepOut)
def update_rep(rep_id: int, body: RepPatch, session: SessionDep):
    rep = session.get(SalesRep, rep_id)
    if rep is None:
        raise HTTPException(404, "rep not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(rep, k, v)
    session.commit()
    return rep


@router.get("/reps/scorecards")
def scorecards(session: SessionDep, days: int = Query(30, ge=1, le=365)):
    return crm.rep_scorecards(session, days)


# ---------------------------------------------------------------- leads

class LeadIn(BaseModel):
    name: str = Field(min_length=2)
    industry: str
    city: str
    state: str
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    owner_id: int | None = None
    deal_value: float | None = Field(None, ge=0)
    notes: str | None = None


class LeadPatch(BaseModel):
    status: StatusName | None = None
    rejection_reason: str | None = None
    owner_id: int | None = None
    deal_value: float | None = Field(None, ge=0)
    expected_close_date: date | None = None
    notes: str | None = None


def _dup_out(pairs) -> list[dict]:
    return [{"id": p.id, "name": p.name, "city": p.city, "website": p.website, "status": p.status, "reasons": r}
            for p, r in pairs]


@router.post("/leads", status_code=201)
def create_lead(body: LeadIn, session: SessionDep, rep: RepDep, force: bool = False):
    """Add a lead by hand. Returns 409 with likely duplicates unless force=true."""
    if body.industry not in INDUSTRIES:
        raise HTTPException(422, "unknown industry")
    lead = Prospect(
        name=body.name.strip(), normalized_name=normalize_name(body.name), industry=body.industry, city=body.city,
        state=body.state, address=body.address, website=body.website, phone=body.phone, notes=body.notes,
        deal_value=body.deal_value, source="manual", status="new", types=[], attributes={}, size_signals={},
        score_breakdown={}, size_class="unknown", priority_level="low", priority_score=0,
    )
    dups = crm.find_duplicates_for(session, lead)
    if dups and not force:
        raise HTTPException(409, {"message": "possible duplicate lead", "duplicates": _dup_out(dups)})
    session.add(lead)
    session.flush()
    rescore(session, lead)
    crm.log(session, lead, "note", rep, subject="Lead created by hand", body=body.notes)
    if body.owner_id:
        _guard(crm.assign, session, lead, body.owner_id, rep)
    session.commit()
    return {"id": lead.id, "duplicates": _dup_out(dups)}


@router.get("/leads/{prospect_id}/duplicates")
def lead_duplicates(prospect_id: int, session: SessionDep):
    return _dup_out(crm.find_duplicates_for(session, _lead(session, prospect_id)))


@router.patch("/leads/{prospect_id}")
def update_lead(prospect_id: int, body: LeadPatch, session: SessionDep, rep: RepDep):
    p = _lead(session, prospect_id)
    data = body.model_dump(exclude_unset=True)
    if "status" in data:
        _guard(crm.change_status, session, p, data.pop("status"), rep, data.pop("rejection_reason", None))
    data.pop("rejection_reason", None)
    if "owner_id" in data:
        _guard(crm.assign, session, p, data.pop("owner_id"), rep)
    if "notes" in data:
        p.notes = data.pop("notes")
    if data:
        crm.update_deal(session, p, rep, **data)
    session.commit()
    return {"id": p.id, "status": p.status, "owner_id": p.owner_id,
            "deal_value": float(p.deal_value) if p.deal_value is not None else None,
            "expected_close_date": p.expected_close_date}


class AssignIn(BaseModel):
    prospect_ids: list[int] = Field(min_length=1)
    rep_id: int | None


@router.post("/leads/assign")
def bulk_assign(body: AssignIn, session: SessionDep, rep: RepDep):
    for pid in body.prospect_ids:
        _guard(crm.assign, session, _lead(session, pid), body.rep_id, rep)
    session.commit()
    return {"assigned": len(body.prospect_ids)}


class AutoAssignIn(BaseModel):
    prospect_ids: list[int] = Field(default_factory=list, description="Empty: every unassigned open lead")


@router.post("/leads/auto-assign")
def auto_assign(body: AutoAssignIn, session: SessionDep, rep: RepDep):
    q = select(Prospect).where(Prospect.owner_id.is_(None), Prospect.merged_into_id.is_(None))
    if body.prospect_ids:
        q = q.where(Prospect.id.in_(body.prospect_ids))
    leads = session.scalars(q.order_by(Prospect.opportunity_score.desc().nulls_last(), Prospect.id)).all()
    assigned = _guard(crm.auto_assign, session, leads, rep)
    session.commit()
    return {"assigned": assigned}


# ---------------------------------------------------------------- activities

class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prospect_id: int
    rep_id: int | None
    person_id: int | None
    kind: str
    occurred_at: datetime
    subject: str | None
    body: str | None
    direction: str | None
    outcome: str | None
    duration_minutes: int | None
    email_to: str | None
    email_status: str | None
    email_opened_at: datetime | None
    email_replied_at: datetime | None
    tracking_token: str | None
    details: dict


class CallIn(BaseModel):
    occurred_at: datetime | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    outcome: Literal["connected", "no_answer", "voicemail", "busy", "wrong_number"]
    duration_minutes: int | None = Field(None, ge=0)
    person_id: int | None = None
    notes: str | None = None


class MeetingIn(BaseModel):
    occurred_at: datetime | None = None
    subject: str = "Meeting"
    outcome: Literal["held", "no_show", "rescheduled", "cancelled"] = "held"
    duration_minutes: int | None = Field(None, ge=0)
    person_id: int | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None
    demo: bool = Field(False, description="This meeting was a product demo")


class EmailIn(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1)
    body: str | None = None
    person_id: int | None = None
    occurred_at: datetime | None = None
    track_opens: bool = False


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


class EmailStatusIn(BaseModel):
    status: Literal["opened", "replied", "bounced"]
    at: datetime | None = None


def _person(session: Session, prospect: Prospect, person_id: int | None) -> int | None:
    if person_id is None:
        return None
    person = session.get(Person, person_id)
    if person is None or person.prospect_id != prospect.id:
        raise HTTPException(422, "person does not belong to this lead")
    return person_id


@router.get("/leads/{prospect_id}/activities", response_model=list[ActivityOut])
def list_activities(prospect_id: int, session: SessionDep):
    _lead(session, prospect_id)
    return session.scalars(
        select(Activity).where(Activity.prospect_id == prospect_id).order_by(Activity.occurred_at.desc(), Activity.id.desc())
    ).all()


@router.post("/leads/{prospect_id}/calls", response_model=ActivityOut, status_code=201)
def log_call(prospect_id: int, body: CallIn, session: SessionDep, rep: RepDep):
    p = _lead(session, prospect_id)
    a = crm.log(session, p, "call", rep, occurred_at=body.occurred_at or crm.now(), direction=body.direction,
                outcome=body.outcome, duration_minutes=body.duration_minutes, body=body.notes,
                person_id=_person(session, p, body.person_id), subject=f"{body.direction.capitalize()} call: {body.outcome.replace('_', ' ')}")
    if body.outcome == "connected":
        crm.advance(session, p, "contacted", rep)
    session.commit()
    return a


@router.post("/leads/{prospect_id}/meetings", response_model=ActivityOut, status_code=201)
def log_meeting(prospect_id: int, body: MeetingIn, session: SessionDep, rep: RepDep):
    p = _lead(session, prospect_id)
    a = crm.log(session, p, "meeting", rep, occurred_at=body.occurred_at or crm.now(), subject=body.subject,
                outcome=body.outcome, duration_minutes=body.duration_minutes, body=body.notes,
                person_id=_person(session, p, body.person_id), details={"attendees": body.attendees, "demo": body.demo})
    if body.outcome == "held":
        crm.advance(session, p, "demo_scheduled" if body.demo else "meeting_scheduled", rep)
    session.commit()
    return a


@router.post("/leads/{prospect_id}/emails", response_model=ActivityOut, status_code=201)
def log_email(prospect_id: int, body: EmailIn, session: SessionDep, rep: RepDep):
    p = _lead(session, prospect_id)
    a = crm.log_email(session, p, rep, to=body.to, subject=body.subject, body=body.body,
                      person_id=_person(session, p, body.person_id), occurred_at=body.occurred_at,
                      track_opens=body.track_opens)
    crm.advance(session, p, "contacted", rep)
    session.commit()
    return a


@router.post("/leads/{prospect_id}/notes", response_model=ActivityOut, status_code=201)
def add_note(prospect_id: int, body: NoteIn, session: SessionDep, rep: RepDep):
    a = crm.log(session, _lead(session, prospect_id), "note", rep, body=body.body)
    session.commit()
    return a


@router.patch("/activities/{activity_id}/email-status", response_model=ActivityOut)
def email_status(activity_id: int, body: EmailStatusIn, session: SessionDep):
    a = session.get(Activity, activity_id)
    if a is None:
        raise HTTPException(404, "activity not found")
    _guard(crm.update_email_status, a, body.status, body.at)
    session.commit()
    return a


PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@router.get("/t/{token}.gif", include_in_schema=False)
def open_pixel(token: str, session: SessionDep):
    """Open-tracking pixel for emails logged with track_opens. Opens are indicative only:
    image blocking hides some, and mail privacy proxies pre-fetch images and inflate others."""
    a = session.scalar(select(Activity).where(Activity.tracking_token == token))
    if a is not None:
        crm.update_email_status(a, "opened")
        session.commit()
    return Response(PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- follow-ups

class FollowUpIn(BaseModel):
    kind: Literal["call", "email", "meeting", "demo", "task"] = "task"
    due_at: datetime
    note: str | None = None
    rep_id: int | None = None


class FollowUpPatch(BaseModel):
    status: Literal["open", "done", "cancelled"] | None = None
    due_at: datetime | None = None
    note: str | None = None
    rep_id: int | None = None


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    prospect_id: int
    rep_id: int | None
    kind: str
    due_at: datetime
    note: str | None
    status: str
    completed_at: datetime | None
    created_at: datetime


class FollowUpItem(FollowUpOut):
    lead_name: str
    lead_status: str
    overdue: bool


@router.post("/leads/{prospect_id}/follow-ups", response_model=FollowUpOut, status_code=201)
def create_follow_up(prospect_id: int, body: FollowUpIn, session: SessionDep, rep: RepDep):
    p = _lead(session, prospect_id)
    f = _guard(crm.schedule_follow_up, session, p, rep_id=body.rep_id or rep, kind=body.kind, due_at=body.due_at,
               note=body.note)
    # Booking a meeting or demo moves the lead to that stage.
    if body.kind in ("meeting", "demo"):
        crm.advance(session, p, f"{body.kind}_scheduled", rep)
    session.commit()
    return f


@router.patch("/follow-ups/{follow_up_id}", response_model=FollowUpOut)
def update_follow_up(follow_up_id: int, body: FollowUpPatch, session: SessionDep):
    f = session.get(FollowUp, follow_up_id)
    if f is None:
        raise HTTPException(404, "follow-up not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("status") == "done" and f.status != "done":
        f.completed_at = crm.now()
    for k, v in data.items():
        setattr(f, k, v)
    session.commit()
    return f


def _follow_up_query(rep_id, status, overdue, due_before, prospect_id):
    q = select(FollowUp, Prospect.name, Prospect.status).join(Prospect, FollowUp.prospect_id == Prospect.id)
    if rep_id is not None:
        q = q.where(FollowUp.rep_id == rep_id)
    if status:
        q = q.where(FollowUp.status.in_(status))
    if overdue:
        q = q.where(FollowUp.status == "open", FollowUp.due_at < func.now())
    if due_before:
        q = q.where(FollowUp.due_at <= due_before)
    if prospect_id is not None:
        q = q.where(FollowUp.prospect_id == prospect_id)
    return q.order_by(FollowUp.due_at)


@router.get("/follow-ups", response_model=list[FollowUpItem])
def list_follow_ups(
    session: SessionDep,
    rep_id: int | None = None,
    status: list[str] = Query(default=["open"]),
    overdue: bool = False,
    due_before: datetime | None = None,
    prospect_id: int | None = None,
    limit: int = Query(200, le=2000),
):
    now = crm.now()
    rows = session.execute(_follow_up_query(rep_id, status, overdue, due_before, prospect_id).limit(limit)).all()
    return [FollowUpItem(**FollowUpOut.model_validate(f).model_dump(), lead_name=name, lead_status=lead_status,
                         overdue=f.status == "open" and f.due_at < now) for f, name, lead_status in rows]


# ---------------------------------------------------------------- duplicates

class MergeIn(BaseModel):
    keep_id: int
    duplicate_id: int


@router.get("/duplicates")
def list_duplicates(session: SessionDep, city: str | None = None):
    return [{"keep": {"id": g["keep"].id, "name": g["keep"].name, "city": g["keep"].city, "status": g["keep"].status,
                      "website": g["keep"].website},
             "duplicate": {"id": g["duplicate"].id, "name": g["duplicate"].name, "city": g["duplicate"].city,
                           "status": g["duplicate"].status, "website": g["duplicate"].website},
             "reasons": g["reasons"]} for g in crm.duplicate_groups(session, city)]


@router.post("/duplicates/merge")
def merge_duplicates(body: MergeIn, session: SessionDep, rep: RepDep):
    _guard(crm.merge, session, _lead(session, body.keep_id), _lead(session, body.duplicate_id), rep)
    session.commit()
    return {"kept": body.keep_id, "merged": body.duplicate_id}


# ---------------------------------------------------------------- analytics

@router.get("/analytics/industries")
def analytics_industries(session: SessionDep):
    return crm.industry_analytics(session)


@router.get("/analytics/funnel")
def analytics_funnel(session: SessionDep):
    return crm.funnel(session)


# ---------------------------------------------------------------- CSV exports

def _csv(filename: str, header: list[str], query, row_fn):
    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        with session_scope() as session:
            for row in session.execute(query).yield_per(500):
                writer.writerow(["" if v is None else v for v in row_fn(row)])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()

    return StreamingResponse(rows(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/activities.csv")
def export_activities(rep_id: int | None = None, kind: list[str] = Query(default=[]), since: datetime | None = None):
    q = (select(Activity, Prospect.name, SalesRep.name)
         .join(Prospect, Activity.prospect_id == Prospect.id)
         .outerjoin(SalesRep, Activity.rep_id == SalesRep.id))
    if rep_id is not None:
        q = q.where(Activity.rep_id == rep_id)
    if kind:
        q = q.where(Activity.kind.in_(kind))
    if since:
        q = q.where(Activity.occurred_at >= since)
    header = ["id", "occurred_at", "lead_id", "lead", "rep", "kind", "subject", "direction", "outcome",
              "duration_minutes", "email_to", "email_status", "email_opened_at", "email_replied_at", "notes"]
    return _csv("areyos-activities.csv", header, q.order_by(Activity.occurred_at.desc()), lambda r: [
        r[0].id, r[0].occurred_at, r[0].prospect_id, r[1], r[2], r[0].kind, r[0].subject, r[0].direction, r[0].outcome,
        r[0].duration_minutes, r[0].email_to, r[0].email_status, r[0].email_opened_at, r[0].email_replied_at, r[0].body])


@router.get("/follow-ups.csv")
def export_follow_ups(rep_id: int | None = None, status: list[str] = Query(default=[]), overdue: bool = False):
    q = _follow_up_query(rep_id, status, overdue, None, None).add_columns(SalesRep.name).outerjoin(
        SalesRep, FollowUp.rep_id == SalesRep.id)
    header = ["id", "due_at", "kind", "status", "lead_id", "lead", "lead_status", "rep", "note", "completed_at"]
    return _csv("areyos-follow-ups.csv", header, q, lambda r: [
        r[0].id, r[0].due_at, r[0].kind, r[0].status, r[0].prospect_id, r[1], r[2], r[3], r[0].note, r[0].completed_at])
