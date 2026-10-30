import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from areyos.api.app import app
from areyos.models import Activity, Contact, Person, PersonContact, Prospect

client = TestClient(app)


def lead(session, name, industry="healthcare", city="Mumbai", **kw) -> Prospect:
    from areyos.dedup import normalize_name

    p = Prospect(name=name, normalized_name=normalize_name(name), industry=industry, city=city, state="Maharashtra",
                 types=[], attributes={}, size_signals={}, score_breakdown={}, priority_level="medium", **kw)
    session.add(p)
    session.commit()
    return p


def rep(name, email, industries=()):
    r = client.post("/api/reps", json={"name": name, "email": email, "industries": list(industries)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def as_rep(rep_id):
    return {"X-Areyos-Rep": str(rep_id)}


def kinds(session, pid):
    return [a.kind for a in session.scalars(select(Activity).where(Activity.prospect_id == pid).order_by(Activity.id))]


def test_meta_lists_the_pipeline():
    meta = client.get("/api/meta").json()
    assert meta["statuses"] == ["new", "researching", "qualified", "contacted", "meeting_scheduled", "demo_scheduled",
                                "negotiation", "pilot_proposed", "pilot_confirmed", "converted", "rejected"]
    assert meta["stage_probability"]["pilot_confirmed"] == 0.8


def test_status_changes_are_logged_and_rejection_needs_reason(session):
    asha = rep("Asha", "asha@areyos.example")
    p = lead(session, "Lilavati Hospital")
    r = client.patch(f"/api/leads/{p.id}", json={"status": "qualified"}, headers=as_rep(asha))
    assert r.status_code == 200 and r.json()["status"] == "qualified"
    assert client.patch(f"/api/leads/{p.id}", json={"status": "rejected"}).status_code == 422
    assert client.patch(f"/api/leads/{p.id}", json={"status": "won"}).status_code == 422  # old status names are gone
    client.patch(f"/api/leads/{p.id}", json={"status": "rejected", "rejection_reason": "Signed with a competitor"})
    session.expire_all()
    acts = session.scalars(select(Activity).where(Activity.prospect_id == p.id, Activity.kind == "status_change")).all()
    assert [a.details for a in acts] == [{"from": "new", "to": "qualified"}, {"from": "qualified", "to": "rejected"}]
    assert acts[0].rep_id == asha and session.get(Prospect, p.id).rejection_reason == "Signed with a competitor"


def test_assignment_manual_bulk_and_auto(session):
    general = rep("General", "g@areyos.example")
    health = rep("Health", "h@areyos.example", ["healthcare"])
    leads = [lead(session, f"Hospital {i}") for i in range(3)] + [lead(session, "Phoenix Mall", industry="retail")]

    assert client.post("/api/leads/assign", json={"prospect_ids": [leads[0].id], "rep_id": general}).status_code == 200
    r = client.post("/api/leads/auto-assign", json={})
    assigned = {int(k): v for k, v in r.json()["assigned"].items()}
    # Hospitals go to the healthcare specialist; the mall to the generalist (the only rep covering retail).
    assert assigned == {leads[1].id: health, leads[2].id: health, leads[3].id: general}
    assert client.post("/api/leads/assign", json={"prospect_ids": [leads[0].id], "rep_id": 999}).status_code == 422

    client.patch(f"/api/reps/{health}", json={"active": False})
    p = lead(session, "Hospital 4")
    assert client.post("/api/leads/auto-assign", json={"prospect_ids": [p.id]}).json()["assigned"] == {str(p.id): general}
    assert "assignment" in kinds(session, p.id)


def test_calls_meetings_emails_and_auto_advance(session):
    asha = rep("Asha", "asha@areyos.example")
    p = lead(session, "Lilavati Hospital")
    person = Person(prospect_id=p.id, full_name="Vikram Desai", person_key="vikram desai", designation="Head Facilities",
                    role="facility_manager", company="Lilavati", match_confidence=0.9, relevance=95)
    session.add(person)
    session.commit()

    r = client.post(f"/api/leads/{p.id}/calls", json={"outcome": "no_answer"}, headers=as_rep(asha))
    assert r.status_code == 201 and r.json()["subject"] == "Outbound call: no answer"
    assert session.get(Prospect, p.id).status == "new"  # no conversation, no stage change
    client.post(f"/api/leads/{p.id}/calls", json={"outcome": "connected", "duration_minutes": 7, "person_id": person.id,
                                                  "notes": "Interested; wants a demo after Diwali."}, headers=as_rep(asha))
    session.expire_all()
    assert session.get(Prospect, p.id).status == "contacted"

    other = lead(session, "Other Hospital")
    assert client.post(f"/api/leads/{p.id}/calls", json={"outcome": "connected", "person_id": 999}).status_code == 422
    assert client.post(f"/api/leads/{other.id}/calls", json={"outcome": "connected", "person_id": person.id}).status_code == 422

    m = client.post(f"/api/leads/{p.id}/meetings", json={"subject": "Site visit", "notes": "Walked OPD and Tower B.",
                                                         "attendees": ["Vikram Desai"], "duration_minutes": 45},
                    headers=as_rep(asha)).json()
    assert m["details"]["attendees"] == ["Vikram Desai"]
    session.expire_all()
    assert session.get(Prospect, p.id).status == "meeting_scheduled"

    # Later stages are never pulled back by logging older-stage activity.
    client.patch(f"/api/leads/{p.id}", json={"status": "negotiation"})
    client.post(f"/api/leads/{p.id}/emails", json={"to": "vikram@lilavati.example", "subject": "Proposal"})
    session.expire_all()
    assert session.get(Prospect, p.id).status == "negotiation"

    note = client.post(f"/api/leads/{p.id}/notes", json={"body": "Budget approved in Q3."}).json()
    timeline = client.get(f"/api/leads/{p.id}/activities").json()
    assert timeline[0]["id"] == note["id"] and len(timeline) >= 6
    assert session.get(Prospect, p.id).last_activity_at is not None


def test_email_tracking(session):
    p = lead(session, "Lilavati Hospital")
    e = client.post(f"/api/leads/{p.id}/emails", json={"to": "vikram@lilavati.example", "subject": "Intro",
                                                       "track_opens": True}).json()
    assert e["email_status"] == "sent" and e["tracking_token"]
    pixel = client.get(f"/api/t/{e['tracking_token']}.gif")
    assert pixel.status_code == 200 and pixel.headers["content-type"] == "image/gif"
    assert client.get(f"/api/t/unknown.gif").status_code == 200  # never reveals whether a token exists
    session.expire_all()
    a = session.get(Activity, e["id"])
    assert a.email_status == "opened" and a.email_opened_at

    replied = client.patch(f"/api/activities/{e['id']}/email-status", json={"status": "replied"}).json()
    assert replied["email_status"] == "replied"
    client.get(f"/api/t/{e['tracking_token']}.gif")  # a later open doesn't undo the reply
    assert client.get(f"/api/leads/{p.id}/activities").json()[0]["email_status"] == "replied"
    assert client.post(f"/api/leads/{p.id}/emails", json={"to": "not-an-email", "subject": "x"}).status_code == 422


def test_follow_ups(session):
    asha = rep("Asha", "asha@areyos.example")
    ravi = rep("Ravi", "ravi@areyos.example")
    p = lead(session, "Lilavati Hospital", owner_id=asha)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    f1 = client.post(f"/api/leads/{p.id}/follow-ups", json={"kind": "call", "due_at": past, "note": "Chase proposal"}).json()
    assert f1["rep_id"] == asha  # defaults to the lead owner
    client.post(f"/api/leads/{p.id}/follow-ups", json={"kind": "demo", "due_at": future}, headers=as_rep(asha))
    session.expire_all()
    assert session.get(Prospect, p.id).status == "demo_scheduled"

    overdue = client.get("/api/follow-ups", params={"overdue": True}).json()
    assert [f["id"] for f in overdue] == [f1["id"]] and overdue[0]["overdue"] and overdue[0]["lead_name"] == "Lilavati Hospital"
    page = client.get("/api/prospects", params={"owner_id": asha}).json()
    assert page["items"][0]["next_follow_up_at"].startswith(past[:10])

    # Reassigning moves the owner's open follow-ups with the lead.
    client.post("/api/leads/assign", json={"prospect_ids": [p.id], "rep_id": ravi})
    assert {f["rep_id"] for f in client.get("/api/follow-ups", params={"prospect_id": p.id}).json()} == {ravi}

    done = client.patch(f"/api/follow-ups/{f1['id']}", json={"status": "done"}).json()
    assert done["completed_at"] is not None
    client.patch(f"/api/leads/{p.id}", json={"status": "converted"})
    assert client.get("/api/follow-ups", params={"prospect_id": p.id}).json() == []  # closed lead: open ones cancelled
    assert client.post(f"/api/leads/{p.id}/follow-ups", json={"due_at": future}).status_code == 422


def test_manual_lead_duplicate_check(session):
    lead(session, "Lilavati Hospital & Research Centre", website="https://www.lilavatihospital.com/", phone="+91 22 2675 1000")
    body = {"name": "Lilavati Hospital and Research Center", "industry": "healthcare", "city": "Mumbai", "state": "Maharashtra"}
    r = client.post("/api/leads", json=body)
    assert r.status_code == 409 and "same name in the same city" in r.json()["detail"]["duplicates"][0]["reasons"]
    r = client.post("/api/leads", json={**body, "name": "LHRC Bandra", "phone": "022-26751000"})
    assert r.status_code == 409 and r.json()["detail"]["duplicates"][0]["reasons"] == ["same phone number"]
    r = client.post("/api/leads", json={**body, "name": "LHRC", "website": "lilavatihospital.com/contact"})
    assert r.status_code == 409 and "same website" in r.json()["detail"]["duplicates"][0]["reasons"][0]
    r = client.post("/api/leads", json={**body, "name": "Nanavati Max Hospital"})
    assert r.status_code == 201
    forced = client.post("/api/leads", json=body, params={"force": True})
    assert forced.status_code == 201 and forced.json()["duplicates"]
    assert session.get(Prospect, forced.json()["id"]).source == "manual"


def test_duplicate_scan_and_merge(session):
    asha = rep("Asha", "asha@areyos.example")
    keep = lead(session, "Phoenix Palladium", industry="retail", lat=18.9944, lng=72.8255, status="contacted")
    dup = lead(session, "Phoenix Palladium Mall", industry="retail", lat=18.9946, lng=72.8257, owner_id=asha,
               deal_value=1_500_000, phone="+91 22 4333 9999")
    lead(session, "Phoenix Marketcity", industry="retail", city="Pune", lat=18.56, lng=73.91)
    session.add_all([Contact(prospect_id=dup.id, kind="email", value="info@palladium.example", source_url="u"),
                     Person(prospect_id=dup.id, full_name="Neha Shah", person_key="neha shah", designation="Mall Manager",
                            role="venue_manager", company="Palladium", match_confidence=0.9, relevance=100)])
    session.commit()
    client.post(f"/api/leads/{dup.id}/notes", json={"body": "Met at RAI expo"})

    groups = client.get("/api/duplicates").json()
    assert len(groups) == 1 and groups[0]["duplicate"]["id"] == dup.id
    assert "similar name within ~250 m" in groups[0]["reasons"]

    assert client.post("/api/duplicates/merge", json={"keep_id": keep.id, "duplicate_id": dup.id},
                       headers=as_rep(asha)).status_code == 200
    session.expire_all()
    k = session.get(Prospect, keep.id)
    assert k.owner_id == asha and float(k.deal_value) == 1_500_000 and k.phone == "+91 22 4333 9999"
    assert [p.full_name for p in k.people] == ["Neha Shah"] and [c.value for c in k.contacts] == ["info@palladium.example"]
    assert {"note", "merge"} <= set(kinds(session, keep.id))
    assert session.get(Prospect, dup.id).merged_into_id == keep.id
    assert all(p["id"] != dup.id for p in client.get("/api/prospects").json()["items"])
    assert client.get("/api/duplicates").json() == []
    assert client.post(f"/api/leads/{dup.id}/notes", json={"body": "x"}).status_code == 409
    assert client.post("/api/duplicates/merge", json={"keep_id": keep.id, "duplicate_id": keep.id}).status_code == 422


def test_analytics_and_scorecards(session):
    asha = rep("Asha", "asha@areyos.example")
    ravi = rep("Ravi", "ravi@areyos.example")
    h1 = lead(session, "Hospital A", owner_id=asha, deal_value=1_000_000, status="negotiation")
    lead(session, "Hospital B", owner_id=asha, deal_value=500_000, status="converted")
    lead(session, "Hospital C", owner_id=ravi, status="rejected", rejection_reason="No budget")
    lead(session, "Mall A", industry="retail", owner_id=ravi, deal_value=2_000_000, status="pilot_proposed",
         opportunity_score=80, opportunity_level="high")
    lead(session, "Mall B", industry="retail")
    client.post(f"/api/leads/{h1.id}/calls", json={"outcome": "connected"}, headers=as_rep(asha))
    e = client.post(f"/api/leads/{h1.id}/emails", json={"to": "a@b.example", "subject": "x"}, headers=as_rep(asha)).json()
    client.patch(f"/api/activities/{e['id']}/email-status", json={"status": "replied"})

    ind = {r["industry"]: r for r in client.get("/api/analytics/industries").json()}
    hc, rt = ind["healthcare"], ind["retail"]
    assert (hc["leads"], hc["converted"], hc["rejected"], hc["win_rate"]) == (3, 1, 1, 0.5)
    assert hc["pipeline_value"] == 1_000_000 and hc["weighted_pipeline"] == 500_000 and hc["converted_value"] == 500_000
    assert rt["weighted_pipeline"] == 1_200_000 and rt["high_opportunity_leads"] == 1 and rt["win_rate"] is None
    assert hc["by_status"]["negotiation"] == 1

    funnel = {s["status"]: s for s in client.get("/api/analytics/funnel").json()}
    assert funnel["pilot_proposed"]["deal_value"] == 2_000_000 and funnel["new"]["leads"] == 1

    cards = {c["name"]: c for c in client.get("/api/reps/scorecards").json()}
    a = cards["Asha"]
    assert (a["open_leads"], a["converted"], a["calls"], a["emails"], a["email_reply_rate"]) == (1, 1, 1, 1, 1.0)
    assert a["pipeline_value"] == 1_000_000 and a["converted_value"] == 500_000
    assert cards["Ravi"]["rejected"] == 1


def test_csv_exports(session):
    asha = rep("Asha", "asha@areyos.example")
    p = lead(session, "Lilavati Hospital", owner_id=asha, deal_value=750_000)
    client.post(f"/api/leads/{p.id}/calls", json={"outcome": "connected", "notes": "Good call"}, headers=as_rep(asha))
    client.post(f"/api/leads/{p.id}/follow-ups", json={"kind": "email", "due_at": "2030-01-01T10:00:00Z"})

    leads = list(csv.DictReader(io.StringIO(client.get("/api/prospects.csv").text)))
    assert leads[0]["owner"] == "Asha" and leads[0]["deal_value"] == "750000.00"
    assert leads[0]["status"] == "contacted" and leads[0]["next_follow_up_at"].startswith("2030-01-01")
    acts = list(csv.DictReader(io.StringIO(client.get("/api/activities.csv", params={"kind": "call"}).text)))
    assert acts[0]["rep"] == "Asha" and acts[0]["notes"] == "Good call" and acts[0]["lead"] == "Lilavati Hospital"
    fus = list(csv.DictReader(io.StringIO(client.get("/api/follow-ups.csv").text)))
    assert fus[0]["kind"] == "email" and fus[0]["rep"] == "Asha"


def test_rep_email_unique():
    rep("Asha", "asha@areyos.example")
    assert client.post("/api/reps", json={"name": "A2", "email": "ASHA@areyos.example"}).status_code == 409
