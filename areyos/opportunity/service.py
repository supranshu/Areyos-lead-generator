"""Assess one venue: combine AI-extracted, stored-data and rules-based factor
results, score them, and save an explained assessment."""

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from areyos.catalog.industries import get_industry
from areyos.config import get_settings
from areyos.models import OpportunityAssessment, Prospect
from areyos.opportunity.evidence import Document, platform_document
from areyos.opportunity.factors import (
    FACTORS,
    Evidence,
    FactorResult,
    classify,
    explain_points,
    score_factor,
    summarise,
)

BASIS_RANK = {"verified": 0, "estimated": 1, "unknown": 2}
METHOD_RANK = {"ai": 0, "data": 1, "heuristic": 2}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Typical figures for an Indian venue of each size class, used only as labelled estimates.
FOOTFALL_BENCHMARK = {
    "healthcare": {"small": 300, "medium": 1_500, "large": 5_000, "mega": 15_000},
    "retail": {"small": 2_000, "medium": 10_000, "large": 40_000, "mega": 100_000},
    "transport": {"small": 2_000, "medium": 15_000, "large": 80_000, "mega": 300_000},
    "convention": {"small": 500, "medium": 3_000, "large": 15_000, "mega": 50_000},
    "sports": {"small": 300, "medium": 2_000, "large": 10_000, "mega": 40_000},
}
DEFAULT_FOOTFALL = {"small": 200, "medium": 1_000, "large": 5_000, "mega": 20_000}
AREA_BENCHMARK = {"small": 3_000, "medium": 15_000, "large": 40_000, "mega": 120_000}
NAVIGATION_BASELINE = {
    "healthcare": "very_high", "transport": "very_high", "convention": "high", "retail": "high",
    "education": "high", "sports": "high", "corporate": "medium", "government": "medium",
    "hospitality": "medium", "event_venues": "medium", "cultural": "medium", "industrial": "medium",
}


def wants_assessment(prospect: Prospect, force: bool = False) -> bool:
    """Every prospect can be assessed; the rules-only assessment costs nothing."""
    return force or get_settings().opportunity_enabled


def ai_eligible(prospect: Prospect, force: bool = False) -> bool:
    """Claude reads the evidence only for prospects at or above the configured priority."""
    settings = get_settings()
    if not settings.opportunity_ai:
        return False
    return force or PRIORITY_RANK[prospect.priority_level] <= PRIORITY_RANK.get(settings.opportunity_min_priority, 1)


def _num(value) -> float | None:
    match = re.search(r"\d[\d,]*\.?\d*", str(value or ""))
    return float(match.group().replace(",", "")) if match else None


def data_results(prospect: Prospect, doc: Document) -> list[FactorResult]:
    """Facts stored by other sources, with the D0 line that states them."""
    out = []
    attrs = prospect.attributes or {}

    def quote(prefix: str) -> Evidence:
        line = next(line for line in doc.text.splitlines() if line.startswith(prefix))
        return Evidence(doc.title, doc.url, line)

    if (levels := _num(attrs.get("building:levels"))) is not None:
        out.append(FactorResult(
            "floors", "verified", numeric_value=levels, method="data",
            finding=f"{int(levels)} floors recorded in OpenStreetMap.",
            reasoning="OpenStreetMap building:levels tag.", evidence=[quote("OpenStreetMap building:levels")],
        ))
    return out


def decision_maker_result(prospect: Prospect) -> FactorResult:
    people = prospect.people
    buyers = [p for p in people if p.relevance >= 80 and p.match_confidence >= 0.8 and p.status != "rejected"]
    reachable = [p for p in buyers if p.contacts or p.linkedin_url]
    evidence = [Evidence(f"{p.full_name}, {p.designation}", p.sources[0].source_url if p.sources else None,
                         p.sources[0].evidence[:200] if p.sources else "") for p in (reachable or buyers or people)[:3]]
    if reachable and any(p.contacts for p in reachable):
        category = "reachable_buyer"
        finding = f"{len(buyers)} likely buyer(s) identified, e.g. {reachable[0].full_name} ({reachable[0].designation}), with public contact details."
    elif buyers:
        category = "buyer_identified"
        finding = f"{len(buyers)} likely buyer(s) identified, e.g. {buyers[0].full_name} ({buyers[0].designation})."
    elif people:
        category = "some_leadership"
        finding = f"{len(people)} leader(s) identified, none in a core buying role."
    elif prospect.people_checked_at:
        category = "none_found"
        finding = "Public sources searched; no decision-makers identified."
    else:
        return FactorResult("decision_makers", "unknown", method="data",
                            finding="Decision-makers have not been searched for yet.")
    return FactorResult("decision_makers", "verified", category=category, method="data", finding=finding,
                        reasoning="From the decision-makers found for this venue, each with a cited public source.",
                        evidence=evidence)


def heuristic_results(prospect: Prospect) -> list[FactorResult]:
    """Rules-based estimates from industry and venue size. Always labelled as estimates."""
    industry = get_industry(prospect.industry)
    size = prospect.size_class
    out = []
    attrs = prospect.attributes or {}
    if size != "unknown":
        footfall = FOOTFALL_BENCHMARK.get(prospect.industry, DEFAULT_FOOTFALL)[size]
        out.append(FactorResult(
            "footfall", "estimated", numeric_value=footfall,
            finding=f"Roughly {footfall:,} visitors a day, typical for a {size} {industry.label.lower()} venue.",
            reasoning=f"Industry benchmark for venue size class '{size}' (from {', '.join(prospect.size_signals or {}) or 'no signals'}).",
        ))
        area = AREA_BENCHMARK[size]
        reasoning = f"Benchmark floor area for size class '{size}'."
        if attrs.get("footprint_m2") and _num(attrs.get("building:levels")):
            area = attrs["footprint_m2"] * _num(attrs["building:levels"])
            reasoning = "OpenStreetMap outline area x recorded floors (outline is approximate)."
        out.append(FactorResult(
            "building_size", "estimated", numeric_value=round(area),
            finding=f"About {round(area, -3):,.0f} m2 of floor area (estimate).", reasoning=reasoning,
        ))
    baseline = NAVIGATION_BASELINE.get(prospect.industry, "medium")
    out.append(FactorResult(
        "navigation_complexity", "estimated", category=baseline,
        finding=f"{baseline.replace('_', ' ').capitalize()} complexity is typical for {industry.label.lower()}.",
        reasoning="Industry baseline: visitor stress, first-time visitors and layout complexity for this venue type.",
    ))
    return out


def combine(candidates: list[FactorResult]) -> list[FactorResult]:
    """Per factor, keep the best-supported result: verified over estimated over unknown;
    then website-reading AI over stored data over rules."""
    chosen = []
    for factor in FACTORS:
        options = [c for c in candidates if c.key == factor.key]
        if not options:
            options = [FactorResult(factor.key, "unknown", finding="No evidence found.")]
        best = min(options, key=lambda c: (BASIS_RANK[c.basis], METHOD_RANK[c.method]))
        for other in options:
            if other is not best and other.method == "ai" and other.notes:
                best.notes.extend(other.notes)
        chosen.append(score_factor(best))
    return chosen


def build_assessment(
    prospect: Prospect, documents: list[Document], ai_results: list[FactorResult] | None, ai_note: str | None
) -> OpportunityAssessment:
    candidates = list(ai_results or [])
    candidates += data_results(prospect, documents[0])
    candidates.append(decision_maker_result(prospect))
    candidates += heuristic_results(prospect)
    results = combine(candidates)

    score = int(round(sum(r.points for r in results)))
    level = classify(score)
    verified_weight = sum(r.max_points for r in results if r.basis == "verified")
    factors = []
    for r in results:
        d = r.to_dict()
        d["label"] = next(f.label for f in FACTORS if f.key == r.key)
        d["points_explained"] = explain_points(r)
        factors.append(d)
    return OpportunityAssessment(
        prospect_id=prospect.id,
        score=score,
        level=level,
        confidence=verified_weight / 100,
        method="ai" if ai_results else "heuristic",
        model=get_settings().opportunity_model if ai_results else None,
        summary=summarise(results, score, level),
        factors=factors,
        # Page text is kept so later reports can check their quotes against the same evidence.
        documents=[{"doc_id": d.doc_id, "title": d.title, "url": d.url, "text": d.text} for d in documents],
        note=ai_note,
    )


def save(session: Session, prospect: Prospect, assessment: OpportunityAssessment) -> None:
    session.add(assessment)
    prospect.opportunity_score = assessment.score
    prospect.opportunity_level = assessment.level
    prospect.opportunity_confidence = assessment.confidence
    prospect.opportunity_assessed_at = datetime.now(timezone.utc)
