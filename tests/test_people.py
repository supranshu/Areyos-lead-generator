import csv
import io

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from areyos import engine
from areyos.api.app import app
from areyos.models import Person, PersonContact, Prospect, Suppression
from areyos.people import names, roles
from areyos.people.service import merge_candidates
from areyos.people.sources import (
    HunterPeopleSource,
    PersonCandidate,
    SearchPeopleSource,
    SearchResult,
    VenueContext,
    WebsitePeopleSource,
    build_queries,
    company_match,
    extract_people_from_html,
    parse_linkedin_result,
)
from areyos.worker import drain
from tests.test_engine import FakeEnricher, FakeGoogle, FakeOSM

LILAVATI = VenueContext(1, "Lilavati Hospital & Research Centre", "healthcare", "Mumbai", "https://lilavati.example")


@pytest.mark.parametrize("title,role", [
    ("Medical Superintendent", "hospital_administrator"),
    ("General Manager - Facilities", "facility_manager"),
    ("Head of Facility Management", "facility_manager"),
    ("Centre Manager", "venue_manager"),
    ("VP - Real Estate", "property_manager"),
    ("Head - Projects & Engineering", "infrastructure_head"),
    ("Chief Operating Officer", "operations_director"),
    ("Purchase Head", "procurement_manager"),
    ("Head of Marketing", "marketing_director"),
    ("Chief Executive Officer", "ceo"),
    ("Managing Director", "managing_director"),
    ("General Manager", "general_manager"),
    ("System Administrator", None),
    ("Marketing Executive", None),
    ("Former CEO", None),
    ("MBBS, MD (Medicine)", None),
])
def test_role_classification(title, role):
    got = roles.classify(title)
    assert (got.key if got else None) == role


def test_relevance_prefers_buyers_and_penalises_deputies():
    assert roles.relevance("hospital_administrator", "healthcare", "Hospital Administrator") == 100
    assert roles.relevance("general_manager", "hospitality", "General Manager") == 90
    assert roles.relevance("facility_manager", "corporate", "Assistant Facility Manager") < roles.relevance(
        "facility_manager", "corporate", "Facility Manager")


@pytest.mark.parametrize("text,ok", [
    ("Dr. Asha Rao", True), ("Rajesh K. Menon", True), ("A. P. J. Abdul Kalam", True),
    ("Board of Directors", False), ("Our Leadership Team", False), ("Medical Superintendent", False),
    ("ASHA", False), ("Contact Us", False), ("Lilavati Hospital", False),
])
def test_person_name_check(text, ok):
    assert names.is_person_name(text) is ok


def test_company_match_needs_distinctive_words():
    assert company_match("Lilavati Hospital & Research Centre", "Facility Manager at Lilavati Hospital") == 1.0
    assert company_match("Lilavati Hospital & Research Centre", "Facility Manager at Nanavati Hospital") == 0.0
    assert company_match("City Hospital", "Administrator at Apollo City Hospital Group") == 1.0
    assert company_match("City Hospital", "Administrator at City Care Hospital") == 0.0


LEADERSHIP_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Hospital","employee":[
  {"@type":"Person","name":"Kavita Shah","jobTitle":"Chief Executive Officer","sameAs":["https://www.linkedin.com/in/kavita-shah-123"]}
]}</script></head><body>
<h2>Our Leadership Team</h2>
<div class="card"><h3>Dr. Asha Rao</h3><p>Medical Superintendent</p>
  <a href="mailto:ms.office@lilavati.example">Email</a> <a href="https://in.linkedin.com/in/asharao/">LinkedIn</a></div>
<div class="card"><h3>Vikram Desai</h3><p>Head - Facilities &amp; Infrastructure</p></div>
<p>Sunil Mehta, Purchase Head</p>
<p>Board of Directors</p><p>Chairman</p>
<p>Our nursing team is led by experienced professionals.</p>
</body></html>
"""


def test_extract_people_from_leadership_page():
    found = {p.full_name: p for p in extract_people_from_html(LEADERSHIP_HTML, "https://lilavati.example/leadership", LILAVATI)}
    assert set(found) == {"Dr. Asha Rao", "Vikram Desai", "Sunil Mehta", "Kavita Shah"}

    asha = found["Dr. Asha Rao"]
    assert asha.role_key == "hospital_administrator" and asha.designation == "Medical Superintendent"
    assert asha.emails == ["ms.office@lilavati.example"]
    assert asha.linkedin_url == "https://in.linkedin.com/in/asharao"
    assert asha.source_url == "https://lilavati.example/leadership" and "Medical Superintendent" in asha.evidence

    # Contacts belong to the card they sit in, never to the next person.
    assert found["Vikram Desai"].emails == [] and found["Vikram Desai"].role_key == "facility_manager"
    assert found["Sunil Mehta"].role_key == "procurement_manager"
    assert found["Kavita Shah"].linkedin_url == "https://www.linkedin.com/in/kavita-shah-123"
    assert all(p.company == LILAVATI.name and p.confidence == 0.9 for p in found.values())


def test_every_extracted_name_is_present_in_source():
    for p in extract_people_from_html(LEADERSHIP_HTML, "u", LILAVATI):
        assert names.appears_in(p.full_name, LEADERSHIP_HTML)


@respx.mock
def test_website_source_follows_leadership_links_and_robots():
    respx.get("https://lilavati.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://lilavati.example/").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"},
        text='<a href="/about-us/leadership">Leadership</a><a href="/private/board">Board</a>'))
    respx.get("https://lilavati.example/about-us/leadership").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=LEADERSHIP_HTML))
    blocked = respx.get("https://lilavati.example/private/board").mock(return_value=httpx.Response(200))

    people = WebsitePeopleSource().find(LILAVATI)

    assert not blocked.called
    assert {p.source_url for p in people} == {"https://lilavati.example/about-us/leadership"}
    assert len(people) == 4


def test_parse_linkedin_results():
    good = SearchResult(
        "Vikram Desai - Head Facilities - Lilavati Hospital | LinkedIn",
        "https://in.linkedin.com/in/vikram-desai-9a1b2c",
        "Mumbai, Maharashtra · Head Facilities at Lilavati Hospital & Research Centre · 500+ connections",
    )
    c = parse_linkedin_result(good, LILAVATI)
    assert c.full_name == "Vikram Desai" and c.role_key == "facility_manager"
    assert c.linkedin_url == "https://in.linkedin.com/in/vikram-desai-9a1b2c" and c.source_url == c.linkedin_url
    assert c.emails == [] and c.phones == []  # snippets never supply contact details
    assert c.confidence == 0.9

    from_snippet = SearchResult(
        "Priya Nair - Lilavati Hospital | LinkedIn", "https://www.linkedin.com/in/priyanair",
        "Experience: Lilavati Hospital · Marketing Head at Lilavati Hospital. Location: Mumbai",
    )
    assert parse_linkedin_result(from_snippet, LILAVATI).designation == "Marketing Head"

    rejects = [
        SearchResult("Amit Roy - Facility Manager - Nanavati Hospital | LinkedIn", "https://www.linkedin.com/in/amitroy", ""),
        SearchResult("Amit Roy - Staff Nurse - Lilavati Hospital | LinkedIn", "https://www.linkedin.com/in/amitroy", ""),
        SearchResult("Facility Manager jobs at Lilavati Hospital", "https://www.linkedin.com/jobs/view/1", ""),
        SearchResult("Lilavati Hospital | LinkedIn", "https://www.linkedin.com/company/lilavati", "CEO"),
    ]
    assert [parse_linkedin_result(r, LILAVATI) for r in rejects] == [None] * 4


def test_search_queries_are_bounded_and_scoped():
    qs = build_queries(LILAVATI, 2)
    assert len(qs) == 2
    assert all(q.startswith('site:linkedin.com/in "Lilavati Hospital & Research Centre"') for q in qs)
    assert '"Hospital Administrator"' in qs[0]


@respx.mock
def test_hunter_keeps_only_sourced_emails():
    respx.get("https://api.hunter.io/v2/domain-search").mock(return_value=httpx.Response(200, json={"data": {"emails": [
        {"value": "asha.rao@lilavati.example", "first_name": "Asha", "last_name": "Rao", "position": "Medical Superintendent",
         "linkedin": "asharao", "sources": [{"uri": "https://lilavati.example/leadership"}]},
        {"value": "guess@lilavati.example", "first_name": "Ravi", "last_name": "Kumar", "position": "CEO", "sources": []},
        {"value": "nurse@lilavati.example", "first_name": "Mary", "last_name": "Dsouza", "position": "Staff Nurse",
         "sources": [{"uri": "https://x.example"}]},
    ]}}))
    people = HunterPeopleSource(api_key="k").find(LILAVATI)
    assert [p.full_name for p in people] == ["Asha Rao"]
    assert people[0].emails == ["asha.rao@lilavati.example"] and people[0].source_url == "https://lilavati.example/leadership"
    assert people[0].linkedin_url == "https://www.linkedin.com/in/asharao"


# ---------------------------------------------------------------- persistence and pipeline

def _hospital(session) -> Prospect:
    p = Prospect(name=LILAVATI.name, normalized_name="lilavati hospital research center", industry="healthcare",
                 city="Mumbai", state="Maharashtra", website=LILAVATI.website, priority_level="high", priority_score=90,
                 types=[], attributes={}, size_signals={}, score_breakdown={})
    session.add(p)
    session.flush()
    return p


def _cand(**kw) -> PersonCandidate:
    base = dict(full_name="Vikram Desai", designation="Head Facilities", role_key="facility_manager",
                company=LILAVATI.name, source="search", source_url="https://in.linkedin.com/in/vikram",
                evidence="...", confidence=0.85, linkedin_url="https://in.linkedin.com/in/vikram")
    return PersonCandidate(**{**base, **kw})


def test_merge_combines_sources_and_respects_threshold(session):
    p = _hospital(session)
    stats = merge_candidates(session, p, [
        _cand(),
        _cand(full_name="Vikram Desai", source="website", source_url="https://lilavati.example/team",
              designation="Head - Facilities & Infrastructure", confidence=0.9, linkedin_url=None,
              emails=["vikram.desai@lilavati.example"]),
        _cand(full_name="Low Match", source_url="https://in.linkedin.com/in/low", linkedin_url=None, confidence=0.4),
    ])
    session.commit()
    assert stats["new"] == 1 and stats["low_confidence"] == 1
    person = session.scalar(select(Person))
    assert person.designation == "Head - Facilities & Infrastructure"  # most trusted source wins
    assert person.linkedin_url == "https://in.linkedin.com/in/vikram"
    assert person.match_confidence == pytest.approx(0.95)
    assert {(c.kind, c.value, c.source_url) for c in person.contacts} == {
        ("email", "vikram.desai@lilavati.example", "https://lilavati.example/team")}
    assert {s.source for s in person.sources} == {"search", "website"}


class FakePeopleWebsite:
    def find(self, venue):
        return extract_people_from_html(LEADERSHIP_HTML, venue.website + "/leadership", venue) if "Lilavati" in venue.name else []


@pytest.fixture
def people_fakes():
    engine.registry.set("google_places", FakeGoogle())
    engine.registry.set("osm", FakeOSM())
    engine.registry.set("enricher", FakeEnricher())
    engine.registry.set("people_website", FakePeopleWebsite())
    engine.registry.set("people_search", SearchPeopleSource(engine=None))  # not configured
    engine.registry.set("people_hunter", HunterPeopleSource(api_key=""))


def test_run_with_find_people_end_to_end(session, people_fakes):
    client = TestClient(app)
    r = client.post("/api/runs", json={"state": "Maharashtra", "city": "Mumbai", "industries": ["healthcare", "retail"],
                                      "find_people": True})
    run_id = r.json()["id"]
    drain()
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

    # Only the high-priority hospital qualifies (default minimum priority: medium); the low kirana stores do not.
    people = client.get("/api/people", params={"run_id": run_id}).json()
    assert people["total"] == 4
    top = people["items"][0]
    assert top["full_name"] == "Dr. Asha Rao" and top["prospect_name"].startswith("Lilavati") and top["relevance"] == 100
    assert client.get("/api/people", params={"role": "procurement_manager"}).json()["items"][0]["full_name"] == "Sunil Mehta"
    assert client.get("/api/people", params={"has_contact": True}).json()["total"] == 1

    hospital_id = top["prospect_id"]
    detail = client.get(f"/api/prospects/{hospital_id}").json()
    assert len(detail["people"]) == 4 and detail["people_checked_at"]

    rows = list(csv.DictReader(io.StringIO(client.get("/api/people.csv").text)))
    asha = next(r for r in rows if r["full_name"] == "Dr. Asha Rao")
    assert asha["emails"] == "ms.office@lilavati.example" and asha["source_urls"] == "https://lilavati.example/leadership"

    assert client.patch(f"/api/people/{top['id']}", json={"status": "verified"}).json()["status"] == "verified"


def test_opt_out_deletes_and_blocks_rediscovery(session, people_fakes):
    client = TestClient(app)
    p = _hospital(session)
    session.commit()
    assert client.post(f"/api/prospects/{p.id}/people").status_code == 202
    drain()
    asha = session.scalar(select(Person).where(Person.full_name == "Dr. Asha Rao"))

    assert client.delete(f"/api/people/{asha.id}", params={"reason": "opt-out email"}).status_code == 204
    kinds = {s.kind for s in session.scalars(select(Suppression))}
    assert kinds == {"person", "linkedin", "email"}

    client.post(f"/api/prospects/{p.id}/people")
    drain()
    session.expire_all()
    assert session.scalar(select(func.count(Person.id)).where(Person.full_name == "Dr. Asha Rao")) == 0
    assert session.scalar(select(func.count(Person.id))) == 3
    assert session.scalar(select(func.count(PersonContact.id)).where(PersonContact.value.like("ms.office%"))) == 0


def test_people_for_existing_run(session, people_fakes):
    client = TestClient(app)
    run_id = client.post("/api/runs", json={"state": "Maharashtra", "city": "Mumbai", "industries": ["healthcare"]}).json()["id"]
    drain()
    assert client.get("/api/people").json()["total"] == 0
    assert client.post(f"/api/runs/{run_id}/people").json() == {"queued": 1}
    drain()
    assert client.get("/api/people").json()["total"] == 4
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"
