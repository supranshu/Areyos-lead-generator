import csv
import io
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from areyos import engine
from areyos.api.app import app
from areyos.http import TransientError
from areyos.models import Job, OpportunityAssessment, Prospect
from areyos.opportunity.evidence import Document
from areyos.opportunity.extractor import SCHEMA, AIUnavailable, ClaudeExtractor, verify
from areyos.opportunity.factors import (
    ESTIMATE_DISCOUNT,
    FACTORS,
    FACTORS_BY_KEY,
    UNKNOWN_FRACTION,
    FactorResult,
    classify,
    score_factor,
)
from areyos.people.sources import HunterPeopleSource, SearchPeopleSource
from areyos.worker import drain
from tests.test_engine import FakeEnricher, FakeGoogle, FakeOSM
from tests.test_people import FakePeopleWebsite

ABOUT = """Lilavati Hospital & Research Centre
Spread over 12 floors with a built-up area of 3,20,000 sq ft, the hospital has 323 beds.
More than 40 specialities and departments under one roof.
Over 5,000 patients and visitors pass through our OPD every day.
The campus has two interconnected towers - Tower A and Tower B.
Signage boards and a help desk at the main lobby guide visitors.
Construction of the new 10-storey Tower C is under way and will open in 2027."""


def ai_answer(**overrides) -> dict:
    answer = {
        "building_size": {"basis": "verified", "numeric_value": 29_728, "category": None,
                          "finding": "About 29,700 m2 built-up area.", "reasoning": "3,20,000 sq ft x 0.0929.",
                          "evidence": [{"doc_id": "D1", "quote": "built-up area of 3,20,000 sq ft"}]},
        "floors": {"basis": "verified", "numeric_value": 12, "category": None, "finding": "12 floors.",
                   "reasoning": "Stated.", "evidence": [{"doc_id": "D1", "quote": "Spread over 12 floors"}]},
        "departments": {"basis": "verified", "numeric_value": 40, "category": None,
                        "finding": "40+ specialities and departments.", "reasoning": "Stated.",
                        "evidence": [{"doc_id": "D1", "quote": "More than 40 specialities and departments"}]},
        "footfall": {"basis": "verified", "numeric_value": 5000, "category": None,
                     "finding": "5,000+ patients and visitors daily.", "reasoning": "Stated.",
                     "evidence": [{"doc_id": "D1", "quote": "Over 5,000 patients and visitors pass through our OPD every day"}]},
        "buildings": {"basis": "verified", "numeric_value": 2, "category": None, "finding": "Two towers.",
                      "reasoning": "Stated.", "evidence": [{"doc_id": "D1", "quote": "two interconnected towers"}]},
        "navigation_complexity": {"basis": "estimated", "numeric_value": None, "category": "very_high",
                                  "finding": "Multi-tower hospital with stressed first-time visitors.",
                                  "reasoning": "Two linked towers, 12 floors, 40+ departments, patients.", "evidence": []},
        "existing_wayfinding": {"basis": "verified", "numeric_value": None, "category": "static_maps_or_help_desk",
                                "finding": "Signage and a lobby help desk only.", "reasoning": "Stated.",
                                "evidence": [{"doc_id": "D1", "quote": "Signage boards and a help desk at the main lobby"}]},
        "expansion": {"basis": "verified", "numeric_value": None, "category": "under_construction",
                      "finding": "New 10-storey Tower C under construction.", "reasoning": "Stated.",
                      "evidence": [{"doc_id": "D1", "quote": "Construction of the new 10-storey Tower C is under way"}]},
    }
    answer.update(overrides)
    return answer


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: records the request, returns a canned response."""

    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.calls = []
        self.answer, self.stop_reason, self.error = answer or ai_answer(), stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])


class FakeCrawler:
    def fetch(self, url):
        if "lilavati" not in url:
            return None
        if url.rstrip("/").endswith("about-us"):
            return url, "<p>" + ABOUT.replace("\n", "</p><p>") + "</p>"
        return url, '<a href="/about-us">About us</a>'


DOCS = [Document("D0", "Platform data", None, "Google Places review count: 9000"),
        Document("D1", "Website: https://lilavati.example/about-us", "https://lilavati.example/about-us", ABOUT)]


# ---------------------------------------------------------------- rubric

def test_weights_and_thresholds():
    assert sum(f.weight for f in FACTORS) == 100
    assert classify(65) == "high" and classify(64) == "medium" and classify(40) == "medium" and classify(39) == "low"


def test_points_by_basis():
    floors = FACTORS_BY_KEY["floors"]
    verified = score_factor(FactorResult("floors", "verified", numeric_value=12))
    estimated = score_factor(FactorResult("floors", "estimated", numeric_value=12))
    unknown = score_factor(FactorResult("floors", "unknown"))
    assert verified.points == 0.8 * floors.weight
    assert estimated.points == round(0.8 * floors.weight * ESTIMATE_DISCOUNT, 1)
    assert unknown.points == UNKNOWN_FRACTION * floors.weight
    bad = score_factor(FactorResult("existing_wayfinding", "estimated", category="laser_beams"))
    assert bad.basis == "unknown" and bad.notes


def test_schema_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(SCHEMA)
    assert "decision_makers" not in SCHEMA["properties"]  # computed from data, never by the model


# ---------------------------------------------------------------- verification guard

def test_verify_keeps_real_quotes_and_downgrades_invented_ones():
    answer = ai_answer(
        floors={"basis": "verified", "numeric_value": 20, "category": None, "finding": "20 floors.", "reasoning": "",
                "evidence": [{"doc_id": "D1", "quote": "Spread over 20 floors"}]},  # not in the page
        buildings={"basis": "verified", "numeric_value": 2, "category": None, "finding": "", "reasoning": "",
                   "evidence": [{"doc_id": "D9", "quote": "two interconnected towers"}]},  # wrong document
        departments={"basis": "verified", "numeric_value": 40, "category": None, "finding": "", "reasoning": "",
                     "evidence": [{"doc_id": "D1", "quote": "specialities and departments"}]},  # no figure
    )
    results = {r.key: r for r in verify(answer, DOCS)}
    for key in ("floors", "buildings", "departments"):
        assert results[key].basis == "estimated", key
        assert any("treated as an estimate" in n for n in results[key].notes)
    assert results["floors"].evidence == [] and "Dropped a quote" in results["floors"].notes[0]
    assert results["footfall"].basis == "verified"
    assert results["footfall"].evidence[0].url == "https://lilavati.example/about-us"


def test_number_words_count_as_figures():
    results = {r.key: r for r in verify(ai_answer(), DOCS)}
    assert results["buildings"].basis == "verified"  # "two interconnected towers"


def test_verify_tolerates_whitespace_and_case_only():
    answer = ai_answer(floors={"basis": "verified", "numeric_value": 12, "category": None, "finding": "", "reasoning": "",
                               "evidence": [{"doc_id": "D1", "quote": "spread  over\n12 FLOORS"}]})
    assert {r.key: r for r in verify(answer, DOCS)}["floors"].basis == "verified"


def test_extractor_request_shape_and_failures():
    fake = FakeAnthropic()
    results = ClaudeExtractor(client=fake).extract("Lilavati", "Hospitals", DOCS)
    assert len(results) == 8
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["schema"] is SCHEMA
    assert "thinking" not in call and "temperature" not in call
    assert '<document id="D1"' in call["messages"][0]["content"]

    with pytest.raises(AIUnavailable, match="declined"):
        ClaudeExtractor(client=FakeAnthropic(stop_reason="refusal")).extract("x", "y", DOCS)
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    rate_limited = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    with pytest.raises(TransientError):
        ClaudeExtractor(client=FakeAnthropic(error=rate_limited)).extract("x", "y", DOCS)
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    with pytest.raises(AIUnavailable):
        ClaudeExtractor(client=FakeAnthropic(error=bad)).extract("x", "y", DOCS)


# ---------------------------------------------------------------- pipeline

@pytest.fixture
def fakes():
    engine.registry.set("google_places", FakeGoogle())
    engine.registry.set("osm", FakeOSM())
    engine.registry.set("enricher", FakeEnricher())
    engine.registry.set("people_website", FakePeopleWebsite())
    engine.registry.set("people_search", SearchPeopleSource(engine=None))
    engine.registry.set("people_hunter", HunterPeopleSource(api_key=""))
    engine.registry.set("opportunity_crawler", FakeCrawler())
    # In the pipeline D1 is the homepage and D2 the about-us page that states the facts.
    claude = FakeAnthropic(answer=json.loads(json.dumps(ai_answer()).replace('"D1"', '"D2"')))
    engine.registry.set("opportunity_ai", ClaudeExtractor(client=claude))
    return claude


def test_full_pipeline_scores_explains_and_ranks(session, fakes):
    client = TestClient(app)
    run_id = client.post("/api/runs", json={
        "state": "Maharashtra", "city": "Mumbai", "industries": ["healthcare", "retail"],
        "find_people": True, "assess_opportunity": True,
    }).json()["id"]
    drain()
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

    # Only the qualifying hospital was assessed, and only after its people were found.
    assert len(fakes.calls) == 1
    hospital_id = session.scalar(select(Prospect.id).where(Prospect.industry == "healthcare"))
    jobs = session.scalars(select(Job).where(Job.kind.in_(("people", "opportunity")),
                                             Job.payload["prospect_id"].as_integer() == hospital_id).order_by(Job.id)).all()
    assert [j.kind for j in jobs] == ["people", "opportunity"]

    ranked = client.get("/api/prospects", params={"sort": "opportunity"}).json()["items"]
    hospital = ranked[0]
    assert hospital["name"].startswith("Lilavati") and hospital["opportunity_level"] == "high"
    # Every other prospect still gets a free rules-only assessment.
    assert all(p["opportunity_score"] is not None for p in ranked[1:])
    others = session.scalars(select(OpportunityAssessment).where(OpportunityAssessment.method == "heuristic")).all()
    assert len(others) == 3 and all("Below the priority threshold" in o.note for o in others)

    a = client.get(f"/api/prospects/{hospital['id']}").json()["opportunity"]
    f = {x["key"]: x for x in a["factors"]}
    assert a["method"] == "ai" and a["model"] == "claude-opus-5-5"
    assert a["score"] == round(sum(x["points"] for x in a["factors"]))
    assert f["floors"]["basis"] == "verified" and f["floors"]["evidence"][0]["quote"] == "Spread over 12 floors"
    assert f["navigation_complexity"]["basis"] == "estimated" and "estimate discount" in f["navigation_complexity"]["points_explained"]
    # Decision-makers come from the people table: a buyer with a public email was found on the website.
    assert f["decision_makers"]["basis"] == "verified" and f["decision_makers"]["category"] == "reachable_buyer"
    assert f["decision_makers"]["method"] == "data"
    assert a["confidence"] == pytest.approx(sum(x["max_points"] for x in a["factors"] if x["basis"] == "verified") / 100)
    assert "High opportunity" in a["summary"] and "factors are backed by verified sources" in a["summary"]
    assert {d["doc_id"] for d in a["documents"]} == {"D0", "D1", "D2"}

    assert client.get("/api/prospects", params={"opportunity": "high"}).json()["total"] == 1
    assert all("text" not in d for d in a["documents"])
    rows = list(csv.DictReader(io.StringIO(client.get("/api/prospects.csv", params={"opportunity": "high"}).text)))
    assert rows[0]["opportunity_level"] == "high"


def test_rules_only_when_ai_unavailable(session, fakes):
    class NoAI:
        def extract(self, *a):
            raise AIUnavailable("no credentials")

    engine.registry.set("opportunity_ai", NoAI())
    engine.create_run(session, state="Maharashtra", city="Mumbai", industries=["healthcare"], assess_opportunity=True)
    session.commit()
    drain()
    a = session.scalar(select(OpportunityAssessment))
    f = {x["key"]: x for x in a.factors}
    assert a.method == "heuristic" and "no credentials" in a.note
    # OSM beds don't state floors; nothing from the rules is ever "verified".
    assert all(x["basis"] != "verified" for x in a.factors)
    assert f["footfall"]["basis"] == "estimated" and f["footfall"]["numeric_value"] == 15_000
    assert f["expansion"]["basis"] == "unknown" and f["decision_makers"]["basis"] == "unknown"
    assert a.confidence == 0
    assert "No evidence found for" in a.summary


def test_reassessment_is_deterministic_and_kept_as_history(session, fakes):
    client = TestClient(app)
    p = Prospect(name="Lilavati Hospital & Research Centre", normalized_name="lilavati hospital research center",
                 industry="healthcare", city="Mumbai", website="https://lilavati.example", priority_level="low",
                 size_class="large", types=[], attributes={}, size_signals={}, score_breakdown={})
    session.add(p)
    session.commit()
    for _ in range(2):
        assert client.post(f"/api/prospects/{p.id}/opportunity").status_code == 202  # forced despite low priority
        drain()
    history = client.get(f"/api/prospects/{p.id}/opportunity").json()
    assert len(history) == 2 and history[0]["score"] == history[1]["score"]


def test_transient_ai_error_retries_job(session, fakes):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    engine.registry.set("opportunity_ai", ClaudeExtractor(client=FakeAnthropic(
        error=anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=req), body=None))))
    p = Prospect(name="X Hospital", normalized_name="x hospital", industry="healthcare", priority_level="high",
                 size_class="large", types=[], attributes={}, size_signals={}, score_breakdown={})
    session.add(p)
    session.commit()
    TestClient(app).post(f"/api/prospects/{p.id}/opportunity")
    drain()
    job = session.scalar(select(Job).where(Job.kind == "opportunity"))
    assert job.status == "queued" and job.attempts == 1 and "Claude API" in job.last_error
    assert session.scalar(select(OpportunityAssessment)) is None
