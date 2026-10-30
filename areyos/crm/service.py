"""Sales CRM: status changes, assignment, activities, follow-ups, duplicates, analytics.

Every change to a lead writes an Activity row, so the timeline doubles as an audit log.
"""

import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session

from areyos.catalog.industries import INDUSTRIES
from areyos.dedup import NAME_SIMILARITY, NEAR_DEGREES, name_similarity, normalize_name
from areyos.models import (
    CLOSED_STATUSES,
    PIPELINE_STAGES,
    Activity,
    Contact,
    FollowUp,
    OpportunityAssessment,
    OpportunityReport,
    Person,
    Prospect,
    ProspectSource,
    RunProspect,
    SalesRep,
)


class CRMError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(session: Session, prospect: Prospect, kind: str, rep_id: int | None, **fields) -> Activity:
    activity = Activity(prospect_id=prospect.id, rep_id=rep_id, kind=kind, details=fields.pop("details", {}), **fields)
    session.add(activity)
    occurred = fields.get("occurred_at") or now()
    if kind in ("call", "meeting", "email", "note") and (
        prospect.last_activity_at is None or occurred > prospect.last_activity_at
    ):
        prospect.last_activity_at = occurred
    return activity


# ---------------------------------------------------------------- status, owner, deal

def change_status(session: Session, prospect: Prospect, status: str, rep_id: int | None, reason: str | None = None) -> None:
    if status not in PIPELINE_STAGES:
        raise CRMError(f"unknown status {status!r}")
    if status == prospect.status:
        return
    if status == "rejected" and not (reason or "").strip():
        raise CRMError("a rejection reason is required")
    old = prospect.status
    prospect.status = status
    prospect.status_changed_at = now()
    prospect.rejection_reason = reason.strip() if status == "rejected" else None
    log(session, prospect, "status_change", rep_id, subject=f"{old} → {status}",
        body=reason if status == "rejected" else None, details={"from": old, "to": status})
    if status in CLOSED_STATUSES:
        # Nothing left to follow up on a closed lead.
        session.execute(
            update(FollowUp).where(FollowUp.prospect_id == prospect.id, FollowUp.status == "open").values(status="cancelled")
        )


def advance(session: Session, prospect: Prospect, status: str, rep_id: int | None) -> None:
    """Move forward to `status` if the lead is at an earlier, open stage; never backwards."""
    order = list(PIPELINE_STAGES)
    if prospect.status not in CLOSED_STATUSES and order.index(prospect.status) < order.index(status):
        change_status(session, prospect, status, rep_id)


def assign(session: Session, prospect: Prospect, rep_id: int | None, by_rep_id: int | None) -> None:
    if rep_id is not None:
        rep = session.get(SalesRep, rep_id)
        if rep is None or not rep.active:
            raise CRMError("sales rep not found or inactive")
    if prospect.owner_id == rep_id:
        return
    old = prospect.owner_id
    prospect.owner_id = rep_id
    log(session, prospect, "assignment", by_rep_id, details={"from": old, "to": rep_id})
    # Open follow-ups move with the lead unless someone else already owns them.
    session.execute(
        update(FollowUp)
        .where(FollowUp.prospect_id == prospect.id, FollowUp.status == "open",
               (FollowUp.rep_id == old) | FollowUp.rep_id.is_(None))
        .values(rep_id=rep_id)
    )


def update_deal(session: Session, prospect: Prospect, rep_id: int | None, **changes) -> None:
    before = {k: getattr(prospect, k) for k in changes}
    for k, v in changes.items():
        setattr(prospect, k, v)
    diff = {k: {"from": _plain(before[k]), "to": _plain(v)} for k, v in changes.items() if before[k] != v}
    if diff:
        log(session, prospect, "deal_update", rep_id, details=diff)


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    return v.isoformat() if hasattr(v, "isoformat") else v


def open_lead_counts(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(Prospect.owner_id, func.count(Prospect.id))
        .where(Prospect.owner_id.is_not(None), Prospect.status.not_in(CLOSED_STATUSES), Prospect.merged_into_id.is_(None))
        .group_by(Prospect.owner_id)
    ).all()
    return dict(rows)


def auto_assign(session: Session, prospects: list[Prospect], by_rep_id: int | None) -> dict[int, int]:
    """Give each unassigned lead to the least-loaded active rep who specialises in its industry,
    falling back to generalists (reps with no industries), then to anyone active."""
    reps = session.scalars(select(SalesRep).where(SalesRep.active).order_by(SalesRep.id)).all()
    if not reps:
        raise CRMError("no active sales reps")
    load = defaultdict(int, open_lead_counts(session))
    assigned = {}
    for p in prospects:
        if p.owner_id is not None or p.status in CLOSED_STATUSES:
            continue
        eligible = ([r for r in reps if p.industry in (r.industries or [])]
                    or [r for r in reps if not r.industries] or reps)
        rep = min(eligible, key=lambda r: (load[r.id], r.id))
        assign(session, p, rep.id, by_rep_id)
        load[rep.id] += 1
        assigned[p.id] = rep.id
    return assigned


# ---------------------------------------------------------------- activities and follow-ups

def log_email(session: Session, prospect: Prospect, rep_id: int | None, *, to: str, subject: str, body: str | None,
              person_id: int | None, occurred_at: datetime | None, track_opens: bool) -> Activity:
    return log(
        session, prospect, "email", rep_id, email_to=to, subject=subject, body=body, person_id=person_id,
        direction="outbound", email_status="sent", occurred_at=occurred_at or now(),
        tracking_token=secrets.token_urlsafe(24) if track_opens else None,
    )


EMAIL_STATUS_ORDER = {"sent": 0, "opened": 1, "replied": 2, "bounced": 3}


def update_email_status(activity: Activity, status: str, at: datetime | None = None) -> None:
    if activity.kind != "email":
        raise CRMError("not an email")
    if status not in EMAIL_STATUS_ORDER:
        raise CRMError(f"unknown email status {status!r}")
    at = at or now()
    if status == "opened" and activity.email_opened_at is None:
        activity.email_opened_at = at
    if status == "replied":
        activity.email_replied_at = activity.email_replied_at or at
        activity.email_opened_at = activity.email_opened_at or at
    # Status only moves forward (an open after a reply doesn't undo the reply).
    if EMAIL_STATUS_ORDER[status] > EMAIL_STATUS_ORDER.get(activity.email_status or "sent", 0):
        activity.email_status = status


def schedule_follow_up(session: Session, prospect: Prospect, *, rep_id: int | None, kind: str, due_at: datetime,
                       note: str | None) -> FollowUp:
    if prospect.status in CLOSED_STATUSES:
        raise CRMError("lead is closed")
    follow_up = FollowUp(prospect_id=prospect.id, rep_id=rep_id or prospect.owner_id, kind=kind, due_at=due_at,
                         note=note, status="open")
    session.add(follow_up)
    return follow_up


def next_follow_ups(session: Session, prospect_ids: list[int]) -> dict[int, datetime]:
    rows = session.execute(
        select(FollowUp.prospect_id, func.min(FollowUp.due_at))
        .where(FollowUp.prospect_id.in_(prospect_ids), FollowUp.status == "open")
        .group_by(FollowUp.prospect_id)
    ).all()
    return dict(rows)


# ---------------------------------------------------------------- duplicates

def _domain(url: str | None) -> str | None:
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    # Shared hosting / social pages are not identity.
    if not host or any(host.endswith(d) for d in ("facebook.com", "instagram.com", "google.com", "linktr.ee", "wixsite.com")):
        return None
    return host


def _phone(p: str | None) -> str | None:
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def duplicate_reasons(a: Prospect, b: Prospect) -> list[str]:
    reasons = []
    if (da := _domain(a.website)) and da == _domain(b.website):
        reasons.append(f"same website ({da})")
    if (pa := _phone(a.phone)) and pa == _phone(b.phone):
        reasons.append("same phone number")
    sim = name_similarity(a.normalized_name, b.normalized_name)
    same_city = a.city and b.city and a.city.lower() == b.city.lower()
    if sim >= 0.9 and same_city:
        reasons.append("same name in the same city")
    if (a.lat is not None and b.lat is not None and abs(a.lat - b.lat) <= NEAR_DEGREES
            and abs(a.lng - b.lng) <= NEAR_DEGREES and sim >= NAME_SIMILARITY):
        reasons.append("similar name within ~250 m")
    return reasons


def find_duplicates_for(session: Session, candidate: Prospect, limit: int = 10) -> list[tuple[Prospect, list[str]]]:
    """Existing leads that look like `candidate` (which need not be saved yet)."""
    if not candidate.normalized_name:
        candidate.normalized_name = normalize_name(candidate.name)
    q = select(Prospect).where(Prospect.merged_into_id.is_(None))
    if candidate.id:
        q = q.where(Prospect.id != candidate.id)
    filters = []
    if candidate.city:
        filters.append(func.lower(Prospect.city) == candidate.city.lower())
    if candidate.lat is not None:
        filters.append(Prospect.lat.between(candidate.lat - NEAR_DEGREES, candidate.lat + NEAR_DEGREES))
    if d := _domain(candidate.website):
        filters.append(Prospect.website.ilike(f"%{d}%"))
    if ph := _phone(candidate.phone):
        filters.append(func.regexp_replace(Prospect.phone, r"\D", "", "g").like(f"%{ph}"))
    if not filters:
        return []
    out = []
    for p in session.scalars(q.where(or_(*filters))):
        if reasons := duplicate_reasons(candidate, p):
            out.append((p, reasons))
    return out[:limit]


def duplicate_groups(session: Session, city: str | None = None) -> list[dict]:
    """Pairs of active leads that look like the same venue."""
    q = select(Prospect).where(Prospect.merged_into_id.is_(None))
    if city:
        q = q.where(func.lower(Prospect.city) == city.lower())
    prospects = session.scalars(q.order_by(Prospect.id)).all()
    # Only leads sharing a bucket are compared, which keeps this near-linear on big cities:
    # same website, same phone, same city + first name word, or neighbouring ~250 m map cells.
    buckets: dict[str, list[Prospect]] = defaultdict(list)
    for p in prospects:
        first = (p.normalized_name or "").split(" ")[0]
        keys = {f"name:{(p.city or '').lower()}:{first}"}
        if p.lat is not None and p.lng is not None:
            row, col = int(p.lat // NEAR_DEGREES), int(p.lng // NEAR_DEGREES)
            keys |= {f"geo:{row + dr}:{col + dc}" for dr in (-1, 0, 1) for dc in (-1, 0, 1)}
        if d := _domain(p.website):
            keys.add(f"web:{d}")
        if ph := _phone(p.phone):
            keys.add(f"tel:{ph}")
        for k in keys:
            buckets[k].append(p)
    seen, pairs = set(), []
    for group in buckets.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                key = (a.id, b.id)
                if key in seen:
                    continue
                seen.add(key)
                if reasons := duplicate_reasons(a, b):
                    pairs.append({"keep": a, "duplicate": b, "reasons": reasons})
    return pairs


def merge(session: Session, keep: Prospect, dup: Prospect, rep_id: int | None) -> None:
    """Fold `dup` into `keep`: history, people, contacts, sources and follow-ups move; `dup` is hidden."""
    if keep.id == dup.id or dup.merged_into_id:
        raise CRMError("cannot merge a lead into itself or merge one twice")
    for model in (Activity, FollowUp, OpportunityAssessment, OpportunityReport):
        session.execute(update(model).where(model.prospect_id == dup.id).values(prospect_id=keep.id))
    session.execute(update(ProspectSource).where(ProspectSource.prospect_id == dup.id).values(prospect_id=keep.id))
    # People and contacts: move, skipping ones the kept lead already has.
    have_people = set(session.scalars(select(Person.person_key).where(Person.prospect_id == keep.id)))
    for person in session.scalars(select(Person).where(Person.prospect_id == dup.id)):
        if person.person_key in have_people:
            session.delete(person)
        else:
            person.prospect_id = keep.id
    have_contacts = set(session.execute(select(Contact.kind, Contact.value).where(Contact.prospect_id == keep.id)).all())
    for contact in session.scalars(select(Contact).where(Contact.prospect_id == dup.id)):
        if (contact.kind, contact.value) in have_contacts:
            session.delete(contact)
        else:
            contact.prospect_id = keep.id
    for rp in session.scalars(select(RunProspect).where(RunProspect.prospect_id == dup.id)):
        exists = session.get(RunProspect, (rp.run_id, keep.id))
        if exists:
            session.delete(rp)
        else:
            session.add(RunProspect(run_id=rp.run_id, prospect_id=keep.id, is_new=rp.is_new))
            session.delete(rp)
    # Fill gaps on the kept lead; keep the further-advanced pipeline status.
    for field in ("website", "phone", "address", "postal_code", "lat", "lng", "owner_id", "deal_value",
                  "expected_close_date", "google_maps_url"):
        if getattr(keep, field) is None and getattr(dup, field) is not None:
            setattr(keep, field, getattr(dup, field))
    order = list(PIPELINE_STAGES)
    if dup.status != "rejected" and order.index(dup.status) > order.index(keep.status) and keep.status != "rejected":
        keep.status = dup.status
    dup.merged_into_id = keep.id
    log(session, keep, "merge", rep_id, subject=f"Merged duplicate lead #{dup.id} ({dup.name})", details={"merged_id": dup.id})


# ---------------------------------------------------------------- analytics

def _weighted():
    return func.sum(Prospect.deal_value * case(*[(Prospect.status == s, p) for s, p in PIPELINE_STAGES.items()], else_=0))


def industry_analytics(session: Session) -> list[dict]:
    active = Prospect.merged_into_id.is_(None)
    rows = session.execute(
        select(
            Prospect.industry,
            func.count(Prospect.id),
            func.count(Prospect.owner_id),
            func.count().filter(Prospect.status == "converted"),
            func.count().filter(Prospect.status == "rejected"),
            func.count().filter(Prospect.status.not_in(("new", "researching"))),
            func.coalesce(func.sum(Prospect.deal_value).filter(Prospect.status.not_in(CLOSED_STATUSES)), 0),
            func.coalesce(_weighted().filter(Prospect.status.not_in(CLOSED_STATUSES)), 0),
            func.coalesce(func.sum(Prospect.deal_value).filter(Prospect.status == "converted"), 0),
            func.avg(Prospect.opportunity_score),
            func.count().filter(Prospect.opportunity_level == "high"),
        ).where(active).group_by(Prospect.industry)
    ).all()
    by_status = defaultdict(dict)
    for industry, status, n in session.execute(
        select(Prospect.industry, Prospect.status, func.count()).where(active).group_by(Prospect.industry, Prospect.status)
    ):
        by_status[industry][status] = n
    out = []
    for (industry, total, assigned, won, lost, engaged, pipeline, weighted, won_value, avg_opp, high_opp) in rows:
        closed = won + lost
        out.append({
            "industry": industry,
            "label": INDUSTRIES[industry].label if industry in INDUSTRIES else industry,
            "leads": total,
            "assigned": assigned,
            "engaged": engaged,
            "converted": won,
            "rejected": lost,
            "win_rate": round(won / closed, 3) if closed else None,
            "conversion_rate": round(won / total, 3) if total else 0,
            "pipeline_value": float(pipeline),
            "weighted_pipeline": round(float(weighted), 2),
            "converted_value": float(won_value),
            "avg_opportunity_score": round(float(avg_opp), 1) if avg_opp is not None else None,
            "high_opportunity_leads": high_opp,
            "by_status": {s: by_status[industry].get(s, 0) for s in PIPELINE_STAGES},
        })
    return sorted(out, key=lambda r: -r["leads"])


def funnel(session: Session) -> list[dict]:
    counts = dict(session.execute(
        select(Prospect.status, func.count()).where(Prospect.merged_into_id.is_(None)).group_by(Prospect.status)
    ).all())
    values = dict(session.execute(
        select(Prospect.status, func.coalesce(func.sum(Prospect.deal_value), 0))
        .where(Prospect.merged_into_id.is_(None)).group_by(Prospect.status)
    ).all())
    return [{"status": s, "probability": p, "leads": counts.get(s, 0), "deal_value": float(values.get(s, 0))}
            for s, p in PIPELINE_STAGES.items()]


def rep_scorecards(session: Session, days: int = 30) -> list[dict]:
    since = now() - timedelta(days=days)
    reps = session.scalars(select(SalesRep).order_by(SalesRep.name)).all()
    leads = defaultdict(lambda: defaultdict(int))
    for owner, status, n in session.execute(
        select(Prospect.owner_id, Prospect.status, func.count())
        .where(Prospect.owner_id.is_not(None), Prospect.merged_into_id.is_(None))
        .group_by(Prospect.owner_id, Prospect.status)
    ):
        leads[owner][status] = n
    money = {owner: (float(pipe or 0), float(w or 0), float(won or 0)) for owner, pipe, w, won in session.execute(
        select(Prospect.owner_id,
               func.sum(Prospect.deal_value).filter(Prospect.status.not_in(CLOSED_STATUSES)),
               _weighted().filter(Prospect.status.not_in(CLOSED_STATUSES)),
               func.sum(Prospect.deal_value).filter(Prospect.status == "converted"))
        .where(Prospect.owner_id.is_not(None), Prospect.merged_into_id.is_(None)).group_by(Prospect.owner_id)
    )}
    acts = defaultdict(lambda: defaultdict(int))
    for rep_id, kind, n in session.execute(
        select(Activity.rep_id, Activity.kind, func.count())
        .where(Activity.occurred_at >= since, Activity.rep_id.is_not(None)).group_by(Activity.rep_id, Activity.kind)
    ):
        acts[rep_id][kind] = n
    replies = dict(session.execute(
        select(Activity.rep_id, func.count()).where(Activity.kind == "email", Activity.occurred_at >= since,
                                                     Activity.email_status == "replied").group_by(Activity.rep_id)
    ).all())
    cutoff = now()
    follow = {rep_id: (n, late) for rep_id, n, late in session.execute(
        select(FollowUp.rep_id, func.count(), func.count().filter(FollowUp.due_at < cutoff))
        .where(FollowUp.status == "open", FollowUp.rep_id.is_not(None)).group_by(FollowUp.rep_id)
    )}
    out = []
    for r in reps:
        by_status = leads[r.id]
        pipe, weighted, won = money.get(r.id, (0.0, 0.0, 0.0))
        emails = acts[r.id]["email"]
        out.append({
            "rep_id": r.id, "name": r.name, "email": r.email, "active": r.active, "industries": r.industries,
            "open_leads": sum(n for s, n in by_status.items() if s not in CLOSED_STATUSES),
            "converted": by_status.get("converted", 0), "rejected": by_status.get("rejected", 0),
            "by_status": dict(by_status),
            "pipeline_value": pipe, "weighted_pipeline": round(weighted, 2), "converted_value": won,
            "period_days": days, "calls": acts[r.id]["call"], "meetings": acts[r.id]["meeting"],
            "emails": emails, "email_reply_rate": round(replies.get(r.id, 0) / emails, 3) if emails else None,
            "open_follow_ups": follow.get(r.id, (0, 0))[0], "overdue_follow_ups": follow.get(r.id, (0, 0))[1],
        })
    return out
