import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from areyos import engine
from areyos.api.app import app
from areyos.models import OpportunityReport, Prospect
from areyos.reports.ai import SCHEMA, ClaudeReportWriter, build_prompt
from areyos.reports.report import GAP_TEXT, QUALIFY_FIRST, SECTION_KEYS, ReportContext, template_sections, verify
from areyos.worker import drain
from tests.test_opportunity import ABOUT, FakeAnthropic, ai_answer, fakes  # noqa: F401 - fixture

CTX = ReportContext(
    venue_name="Lilavati Hospital & Research Centre",
    industry="healthcare",
    documents={"D2": {"title": "Website: about-us", "url": "https://lilavati.example/about-us", "text": ABOUT}},
    factors={
        "floors": {"key": "floors", "label": "Number of floors", "basis": "verified", "finding": "12 floors.",
                   "category": None, "evidence": [{"url": "https://lilavati.example/about-us", "quote": "Spread over 12 floors"}]},
        "footfall": {"key": "footfall", "label": "Daily visitor footfall", "basis": "estimated", "finding": "About 5,000 a day.",
                     "category": None, "evidence": []},
        "expansion": {"key": "expansion", "label": "Expansion", "basis": "unknown", "finding": "No evidence found.",
                      "category": None, "evidence": []},
    },
    people={7: {"name": "Vikram Desai", "designation": "Head - Facilities", "role": "facility_manager", "relevance": 95,
                "linkedin": None, "emails": ["vikram@lilavati.example"], "phones": [], "source_url": "https://lilavati.example/team"}},
    contacts={},
)


def st(text, kind, *cites):
    return {"text": text, "kind": kind, "citations": [{"ref": r, "quote": q} for r, q in cites]}


def draft(**sections):
    return {k: {"statements": sections.get(k, [])} for k in SECTION_KEYS}


def test_unsupported_claims_are_removed_and_gaps_marked():
    report = verify(draft(
        navigation_difficulty=[
            st("12 floors.", "evidence", ("D2", "Spread over 12 floors")),
            st("Patients often get lost in the basement.", "evidence", ("D2", "patients often get lost")),  # invented quote
            st("It is a maze.", "inference"),  # no citation
        ],
        visitor_need=[st("Visitors need help.", "suggestion", ("F:footfall", ""))],  # advice not allowed here
        business_problem=[st("Expansion will disrupt signage.", "inference", ("F:expansion", ""))],  # unknown factor
        responsible=[st("Rahul Jain, CEO, decides.", "evidence", ("P:999", ""))],  # person not in the list
    ), CTX)
    nav = report.sections["navigation_difficulty"]
    assert [s["text"] for s in nav] == ["12 floors."]
    assert nav[0]["citations"][0]["url"] == "https://lilavati.example/about-us"
    assert report.sections["visitor_need"][0]["kind"] == "inference"  # suggestion demoted, estimate-backed
    assert report.sections["business_problem"] == [{"text": GAP_TEXT, "kind": "gap", "citations": []}]
    assert report.sections["responsible"] == []
    assert sum("removed unsupported claim" in n for n in report.notes) == 4
    assert report.evidence_level == "partial"


def test_evidence_backed_only_by_estimates_becomes_inference():
    report = verify(draft(visitor_need=[st("5,000 visitors a day.", "evidence", ("F:footfall", ""))]), CTX)
    assert report.sections["visitor_need"][0]["kind"] == "inference"


def test_insufficient_evidence_forces_qualify_first_pitch():
    report = verify(draft(pitch=[st("Areyos will fix their navigation chaos.", "suggestion")]), CTX)
    assert report.evidence_level == "insufficient"
    assert report.sections["pitch"][0] == {"text": QUALIFY_FIRST, "kind": "gap", "citations": []}
    assert all(report.sections[k][0]["kind"] == "gap" for k in ("navigation_difficulty", "visitor_need", "business_problem"))


def test_template_report_cites_only_supported_factors():
    report = verify(template_sections(CTX), CTX)
    cited = {c["ref"] for stmts in report.sections.values() for s in stmts for c in s["citations"]}
    assert "F:expansion" not in cited  # unknown factors are never used
    assert report.sections["responsible"][0]["citations"][0]["ref"] == "P:7"
    assert "vikram@lilavati.example" in report.sections["demo_approach"][0]["text"]
    assert not report.notes  # the template never produces claims the verifier has to remove


def test_ai_prompt_and_schema():
    prompt = build_prompt(CTX)
    assert "P:7 Vikram Desai" in prompt and "F:floors [verified]" in prompt and '<document id="D2"' in prompt
    assert set(SCHEMA["required"]) == set(SECTION_KEYS)
    kinds = SCHEMA["properties"]["pitch"]["properties"]["statements"]["items"]["properties"]["kind"]["enum"]
    assert "gap" not in kinds  # only code declares gaps


def ai_report_draft():
    return draft(
        navigation_difficulty=[
            st("The hospital spans 12 floors across two interconnected towers.", "evidence",
               ("D2", "Spread over 12 floors"), ("D2", "two interconnected towers")),
            st("Over 40 departments under one roof.", "evidence", ("D2", "More than 40 specialities and departments")),
        ],
        visitor_need=[st("More than 5,000 patients and visitors pass through the OPD daily.", "evidence",
                         ("D2", "Over 5,000 patients and visitors pass through our OPD every day"))],
        business_problem=[
            st("Tower C is under construction, so directions will change.", "inference",
               ("D2", "Construction of the new 10-storey Tower C is under way")),
            st("Visitors frequently complain about getting lost.", "evidence"),  # no source: must go
        ],
        # People ids follow discovery order: P:1 Dr. Asha Rao, P:2 Vikram Desai.
        responsible=[st("Vikram Desai heads facilities.", "evidence", ("P:2", "")),
                     st("Vikram Desai approves all purchases.", "evidence", ("P:1", ""))],  # cites someone else
        pitch=[st("Have maps ready for Tower C's opening.", "suggestion", ("F:expansion", ""))],
        objections=[st("\"The help desk copes today.\" Ask about queue times.", "suggestion", ("F:existing_wayfinding", ""))],
        demo_approach=[st("Email Vikram Desai offering a demo on the Tower C plan.", "suggestion", ("P:2", ""))],
    )


def test_pipeline_writes_reports_for_every_prospect(session, fakes):  # noqa: F811
    writer = FakeAnthropic(answer=ai_report_draft())
    engine.registry.set("report_ai", ClaudeReportWriter(client=writer))
    client = TestClient(app)
    run_id = client.post("/api/runs", json={"state": "Maharashtra", "city": "Mumbai", "industries": ["healthcare", "retail"],
                                            "find_people": True, "generate_reports": True}).json()["id"]
    drain()
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "completed" and run["assess_opportunity"] is True

    reports = session.scalars(select(OpportunityReport)).all()
    assert len(reports) == 4  # one per discovered prospect
    assert sorted(r.method for r in reports) == ["ai", "template", "template", "template"]
    assert len(writer.calls) == 1  # Claude only for the qualifying hospital

    hospital = session.scalar(select(Prospect).where(Prospect.industry == "healthcare"))
    detail = client.get(f"/api/prospects/{hospital.id}").json()
    rep = detail["report"]
    assert rep["evidence_level"] == "strong"
    problem = [s["text"] for s in rep["sections"]["business_problem"]]
    assert problem == ["Tower C is under construction, so directions will change."]
    assert any("removed unsupported claim: Visitors frequently complain" in n for n in rep["notes"])
    assert "https://lilavati.example/about-us" in {s["url"] for s in rep["sources"]}
    assert [s["text"] for s in rep["sections"]["responsible"]] == ["Vikram Desai heads facilities."]
    assert rep["sections"]["responsible"][0]["citations"][0]["quote"].startswith("Vikram Desai")

    # Small stores have no evidence of a navigation problem: the report says so and won't pitch a need.
    store = session.scalar(select(Prospect).where(Prospect.industry == "retail"))
    store_report = client.get(f"/api/prospects/{store.id}").json()["report"]
    assert store_report["method"] == "template"
    assert store_report["sections"]["business_problem"][0]["kind"] == "gap"

    md = client.get(f"/api/prospects/{hospital.id}/report.md").text
    assert md.startswith("# Lilavati Hospital") and "## What is the likely business problem?" in md
    assert '> "Construction of the new 10-storey Tower C is under way"' in md and "## Sources" in md
    combined = client.get("/api/reports.md", params={"sort": "opportunity"}).text
    assert combined.index("Lilavati") < combined.index("Kirana") and combined.count("\n---\n") == 3


def test_report_on_demand_without_assessment(session, fakes):  # noqa: F811
    p = Prospect(name="Small Museum", normalized_name="small museum", industry="cultural", priority_level="low",
                 size_class="small", types=[], attributes={}, size_signals={}, score_breakdown={})
    session.add(p)
    session.commit()
    engine.registry.set("report_ai", ClaudeReportWriter(client=FakeAnthropic(answer=draft())))
    client = TestClient(app)
    assert client.post(f"/api/prospects/{p.id}/report").status_code == 202
    drain()
    rep = client.get(f"/api/prospects/{p.id}").json()
    assert rep["opportunity"]["method"] == "heuristic"  # free assessment made first
    assert rep["report"]["method"] == "ai"  # forced request uses AI regardless of priority
    assert rep["report"]["evidence_level"] in ("partial", "insufficient")
