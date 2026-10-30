import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from areyos import engine
from areyos.api.app import app
from areyos.enrichment.website import FoundContact
from areyos.http import TransientError
from areyos.models import Contact, DiscoveryRun, Job, Prospect, ProspectSource
from areyos.providers.base import Area, RawPlace
from areyos.worker import drain

MUMBAI = Area(19.07, 72.87, 18.89, 72.77, 19.27, 72.98, "Mumbai, Maharashtra")


class FakeGoogle:
    name = "google_places"

    def __init__(self):
        self.calls = []

    def geocode(self, city, state):
        return MUMBAI

    def search(self, industry_key, area, query=None):
        self.calls.append((industry_key, query, area.label))
        if industry_key == "healthcare":
            # Every tile and query returns the same hospital: must dedupe to one prospect.
            return [RawPlace(
                source="google_places", source_id="g-lilavati", name="Lilavati Hospital & Research Centre",
                lat=19.0510, lng=72.8290, city="Mumbai", state="Maharashtra", website="https://lilavati.example",
                phone="+91 22 6931 8000", rating=4.2, rating_count=9000, business_status="OPERATIONAL",
                types=["hospital"],
            )]
        if industry_key == "retail":
            return [RawPlace(
                source="google_places", source_id=f"g-store-{query}", name=f"Kirana Store {len(query)}",
                lat=19.2, lng=72.9, rating_count=15, business_status="OPERATIONAL", types=["store"],
            )]
        return []


class FakeOSM:
    name = "osm"

    def search(self, industry_key, area, query=None):
        if industry_key == "healthcare":
            # Same hospital ~60 m away under a slightly different name, plus bed count.
            return [RawPlace(
                source="osm", source_id="way/1", name="Lilavati Hospital", lat=19.0515, lng=72.8293,
                tags={"beds": "323"}, types=["amenity=hospital"],
            )]
        return []


class FakeEnricher:
    def enrich(self, website):
        return [FoundContact("email", "info@lilavati.example", website + "/contact")]


@pytest.fixture
def fakes():
    google, osm = FakeGoogle(), FakeOSM()
    engine.registry.set("google_places", google)
    engine.registry.set("osm", osm)
    engine.registry.set("enricher", FakeEnricher())
    return google, osm


def test_full_run_dedupes_scores_and_enriches(session, fakes):
    google, _ = fakes
    run = engine.create_run(
        session, state="Maharashtra", city="Mumbai", industries=["healthcare", "retail"], providers=["google_places", "osm"]
    )
    session.commit()

    drain()

    session.expire_all()
    run = session.get(DiscoveryRun, run.id)
    assert run.status == "completed", run.error
    # 3 queries x 4 tiles per industry for Google.
    assert sum(1 for c in google.calls if c[0] == "healthcare") == 12

    hospitals = session.scalars(select(Prospect).where(Prospect.industry == "healthcare")).all()
    assert len(hospitals) == 1
    h = hospitals[0]
    assert {s.source for s in h.sources} == {"google_places", "osm"}
    assert h.name == "Lilavati Hospital & Research Centre"  # Google wins on name
    assert h.attributes["beds"] == "323"
    assert h.size_class == "mega"
    assert h.priority_level == "high" and h.score_breakdown["reachability"] == 15
    assert h.enriched_at is not None
    assert [c.value for c in h.contacts] == ["info@lilavati.example"]

    shops = session.scalars(select(Prospect).where(Prospect.industry == "retail")).all()
    assert len(shops) == 3 and all(p.priority_level == "low" for p in shops)

    stats = engine.run_stats(session, run.id)
    assert stats["prospects_found"] == 4 and stats["prospects_new"] == 4


def test_rerun_updates_instead_of_duplicating(session, fakes):
    for _ in range(2):
        engine.create_run(session, state="Maharashtra", city="Mumbai", industries=["healthcare"])
        session.commit()
        drain()
    assert session.scalar(select(func.count(Prospect.id))) == 1
    assert session.scalar(select(func.count(ProspectSource.id))) == 2
    assert session.scalar(select(func.count(Contact.id))) == 1


def test_api_filters_and_csv(session, fakes):
    client = TestClient(app)
    meta = client.get("/api/meta").json()
    assert len(meta["industries"]) == 12 and "Maharashtra" in meta["states"]

    r = client.post("/api/runs", json={
        "state": "Maharashtra", "city": "Mumbai", "industries": ["healthcare", "retail"],
        "venue_sizes": ["mega", "large"], "priority_levels": ["high"],
    })
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    drain()

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "completed" and run["stats"]["prospects_found"] == 4

    high = client.get("/api/prospects", params={"run_id": run_id, "priority": "high", "size": ["mega", "large"]}).json()
    assert high["total"] == 1 and high["items"][0]["name"].startswith("Lilavati")
    assert client.get("/api/prospects", params={"state": "maharashtra", "industry": "retail"}).json()["total"] == 3
    assert client.get("/api/prospects", params={"has_email": True}).json()["total"] == 1
    assert client.get("/api/prospects", params={"q": "kirana"}).json()["total"] == 3

    pid = high["items"][0]["id"]
    detail = client.patch(f"/api/prospects/{pid}", json={"status": "qualified", "notes": "Call facilities head"}).json()
    assert detail["status"] == "qualified" and detail["sources"] == ["google_places", "osm"]
    assert client.patch(f"/api/prospects/{pid}", json={"status": "bogus"}).status_code == 422

    rows = list(csv.DictReader(io.StringIO(client.get("/api/prospects.csv", params={"priority": "high"}).text)))
    assert len(rows) == 1 and rows[0]["emails"] == "info@lilavati.example" and rows[0]["status"] == "qualified"


def test_run_validation(session, monkeypatch):
    client = TestClient(app)
    assert client.post("/api/runs", json={"state": "X", "city": "Y", "industries": ["casinos"]}).status_code == 422
    monkeypatch.setattr(engine, "available_providers", lambda: ["osm"])
    r = client.post("/api/runs", json={"state": "X", "city": "Y", "providers": ["google_places"]})
    assert r.status_code == 422 and "not configured" in r.json()["detail"]


def test_transient_failure_is_retried_then_succeeds(session, fakes):
    google, _ = fakes
    original = google.search
    failures = {"left": 1}

    def flaky(industry_key, area, query=None):
        if failures["left"]:
            failures["left"] -= 1
            raise TransientError("HTTP 503")
        return original(industry_key, area, query)

    google.search = flaky
    run = engine.create_run(session, state="Maharashtra", city="Mumbai", industries=["healthcare"], providers=["google_places"])
    session.commit()
    drain()

    retrying = session.scalars(select(Job).where(Job.status == "queued")).all()
    assert len(retrying) == 1 and retrying[0].attempts == 1 and "503" in retrying[0].last_error
    session.expire_all()
    assert session.get(DiscoveryRun, run.id).status == "running"

    # Make the backed-off job due now and let it finish the run.
    retrying[0].run_after = func.now()
    session.commit()
    drain()
    session.expire_all()
    assert session.get(DiscoveryRun, run.id).status == "completed"


def test_failed_geocode_fails_run(session, fakes):
    google, _ = fakes
    google.geocode = lambda city, state: None
    engine.registry.set("nominatim", type("N", (), {"geocode": staticmethod(lambda c, s: None)})())
    run = engine.create_run(session, state="Nowhere", city="Atlantis", industries=["healthcare"])
    session.commit()
    drain()
    session.expire_all()
    run = session.get(DiscoveryRun, run.id)
    assert run.status == "failed" and "could not locate" in run.error


def test_concurrent_claims_split_the_queue(session, fakes):
    from concurrent.futures import ThreadPoolExecutor

    run = engine.create_run(
        session, state="Maharashtra", city="Mumbai", industries=["healthcare", "retail"], providers=["google_places", "osm"]
    )
    session.commit()
    from areyos.worker import work_once

    work_once("planner")  # fan out into 26 discover jobs
    with ThreadPoolExecutor(4) as pool:
        counts = list(pool.map(lambda i: drain(f"w{i}"), range(4)))
    session.expire_all()
    assert sum(counts) == 26 + 1  # discover jobs + one enrich job, each processed exactly once
    assert session.get(DiscoveryRun, run.id).status == "completed"
    assert session.scalar(select(func.count(Prospect.id))) == 4
    assert session.scalar(select(func.count(ProspectSource.id))) == 5
