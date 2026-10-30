"""Build, verify, store and render opportunity reports."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from areyos.catalog.industries import get_industry
from areyos.config import get_settings
from areyos.models import OpportunityAssessment, OpportunityReport, Prospect
from areyos.reports.report import SECTIONS, ReportContext, VerifiedReport, template_sections, verify


def latest_assessment(session: Session, prospect_id: int) -> OpportunityAssessment | None:
    return session.scalar(
        select(OpportunityAssessment)
        .where(OpportunityAssessment.prospect_id == prospect_id)
        .order_by(OpportunityAssessment.id.desc())
        .limit(1)
    )


def latest_report(session: Session, prospect_id: int) -> OpportunityReport | None:
    return session.scalar(
        select(OpportunityReport)
        .where(OpportunityReport.prospect_id == prospect_id)
        .order_by(OpportunityReport.id.desc())
        .limit(1)
    )


def build_context(prospect: Prospect, assessment: OpportunityAssessment) -> ReportContext:
    people = {
        p.id: {
            "name": p.full_name, "designation": p.designation, "role": p.role, "relevance": p.relevance,
            "linkedin": p.linkedin_url, "emails": [c.value for c in p.contacts if c.kind == "email"],
            "phones": [c.value for c in p.contacts if c.kind == "phone"],
            "source_url": p.sources[0].source_url if p.sources else None,
        }
        for p in prospect.people
        if p.status != "rejected"
    }
    return ReportContext(
        venue_name=prospect.name,
        industry=prospect.industry,
        documents={d["doc_id"]: d for d in assessment.documents if d.get("text")},
        factors={f["key"]: f for f in assessment.factors},
        people=people,
        contacts={c.id: {"kind": c.kind, "value": c.value, "source_url": c.source_url} for c in prospect.contacts},
    )


def make_report(
    prospect: Prospect, assessment: OpportunityAssessment, ctx: ReportContext, ai_draft: dict | None, note: str | None
) -> OpportunityReport:
    verified: VerifiedReport = verify(ai_draft if ai_draft else template_sections(ctx), ctx)
    return OpportunityReport(
        prospect_id=prospect.id,
        assessment_id=assessment.id,
        method="ai" if ai_draft else "template",
        model=get_settings().opportunity_model if ai_draft else None,
        evidence_level=verified.evidence_level,
        sections=verified.sections,
        sources=verified.sources(),
        notes=verified.notes,
        note=note,
    )


KIND_LABEL = {"evidence": "Evidence", "inference": "Inference", "suggestion": "Suggestion", "gap": "Unknown"}


def to_markdown(prospect: Prospect, assessment: OpportunityAssessment | None, report: OpportunityReport) -> str:
    lines = [
        f"# {prospect.name}",
        f"{get_industry(prospect.industry).label} · {prospect.city or ''} {prospect.state or ''}".strip(),
        "",
    ]
    if assessment:
        lines += [f"**Opportunity:** {assessment.score}/100 ({assessment.level}); "
                  f"{assessment.confidence:.0%} of the score rests on verified sources.", ""]
    lines += [f"**Evidence level:** {report.evidence_level}. Every statement below is tagged Evidence (a source states it), "
              "Inference (concluded from cited facts), Suggestion (sales advice) or Unknown.", ""]
    numbered = {}
    for key, question in SECTIONS:
        lines.append(f"## {question}")
        for st in report.sections.get(key, []):
            refs = []
            for c in st["citations"]:
                if c.get("url"):
                    numbered.setdefault(c["url"], len(numbered) + 1)
                    if (ref := f"[{numbered[c['url']]}]") not in refs:
                        refs.append(ref)
            lines.append(f"- **{KIND_LABEL[st['kind']]}:** {st['text']} {' '.join(refs)}".rstrip())
            for c in st["citations"]:
                if c.get("quote") and c["ref"].startswith("D"):
                    lines.append(f"  > \"{c['quote']}\"")
        lines.append("")
    if numbered:
        lines.append("## Sources")
        lines += [f"{n}. {url}" for url, n in numbered.items()]
    return "\n".join(lines) + "\n"
