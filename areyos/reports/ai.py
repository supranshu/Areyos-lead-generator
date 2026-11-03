"""Claude drafts the report from the assessment's evidence; report.verify checks every citation."""

import json

import anthropic

from areyos.catalog.industries import get_industry
from areyos.config import get_settings
from areyos.http import TransientError
from areyos.opportunity.extractor import AIUnavailable
from areyos.reports.report import SECTIONS, ReportContext

SYSTEM = """You write sales opportunity reports for Areyos, a QR-based indoor wayfinding product: a visitor scans a static QR code, picks a destination, and follows manual step-by-step directions on their phone. It has no app, no sign-up, no live positioning, no blue dot that follows the walker, and no automatic floor switching. Never describe it as anything more than that: do not claim live tracking, turn-by-turn navigation, kiosks or an app.

Answer seven questions about one venue as short statements. Each statement has a kind and citations:
- "evidence": a fact a source states. Cite it.
- "inference": a conclusion you draw from cited facts. Cite the facts it rests on.
- "suggestion": sales advice. Only in sections responsible, pitch, objections and demo_approach. Cite facts when the advice depends on them.

Citations (ref, quote):
- "D<n>" for an evidence document, with a quote copied character-for-character from it (under 300 characters).
- "F:<factor key>" for a finding of the opportunity assessment (quote may be empty).
- "P:<id>" for a listed decision-maker; "C:<id>" for a listed venue contact (quote may be empty).

Hard rules:
- Never invent problems, figures, complaints, projects, people or contact details. Every claim about the venue must be backed by a citation; uncited claims are deleted.
- Never state that the venue needs Areyos unless the cited evidence shows a navigation difficulty. If the evidence is thin, say what is unknown and make the pitch and demo approach about discovering the need.
- Mark estimated factors as estimates in your wording ("an estimated ...").
- In "responsible", only name people from the decision-maker list. If none fits, describe the role to look for as a suggestion.
- Objections should be ones this particular decision-maker would plausibly raise given the evidence, each with a short, honest answer.
- Statements are one or two plain sentences a salesperson can read aloud. No marketing hype."""


def _schema() -> dict:
    statement = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": ["evidence", "inference", "suggestion"]},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["ref", "quote"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["text", "kind", "citations"],
        "additionalProperties": False,
    }
    section = {
        "type": "object",
        "properties": {"statements": {"type": "array", "items": statement}},
        "required": ["statements"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {key: section for key, _ in SECTIONS},
        "required": [key for key, _ in SECTIONS],
        "additionalProperties": False,
    }


SCHEMA = _schema()


def build_prompt(ctx: ReportContext) -> str:
    factors = "\n".join(
        f"- F:{k} [{f['basis']}] {f['label']}: {f['finding']}"
        + "".join(f'\n    quote: "{e["quote"]}" ({e.get("source", "")})' for e in f.get("evidence", []))
        for k, f in ctx.factors.items()
    )
    people = "\n".join(
        f"- P:{pid} {p['name']}, {p['designation']} (role: {p['role']}; relevance {p['relevance']}/100; "
        f"LinkedIn: {'yes' if p['linkedin'] else 'no'}; email: {'yes' if p['emails'] else 'no'}; phone: {'yes' if p['phones'] else 'no'})"
        for pid, p in ctx.people.items()
    ) or "(none found)"
    contacts = "\n".join(f"- C:{cid} {c['kind']}: {c['value']}" for cid, c in ctx.contacts.items()) or "(none)"
    docs = "\n\n".join(
        f'<document id="{doc_id}" title="{d["title"]}">\n{d["text"]}\n</document>' for doc_id, d in ctx.documents.items()
    )
    questions = "\n".join(f"- {key}: {q}" for key, q in SECTIONS)
    return (
        f"Venue: {ctx.venue_name} ({get_industry(ctx.industry).label})\n\n"
        f"Questions:\n{questions}\n\nOpportunity assessment findings:\n{factors}\n\n"
        f"Decision-makers:\n{people}\n\nVenue public contacts:\n{contacts}\n\nEvidence documents:\n{docs}"
    )


class ClaudeReportWriter:
    def __init__(self, client: anthropic.Anthropic | None = None):
        settings = get_settings()
        self.model = settings.opportunity_model
        self.effort = settings.opportunity_effort
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            try:
                self._client = anthropic.Anthropic()
            except anthropic.AnthropicError as exc:
                raise AIUnavailable(str(exc)) from exc
        return self._client

    def draft(self, ctx: ReportContext) -> dict:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": build_prompt(ctx)}],
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise TransientError(f"Claude API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AIUnavailable(f"Claude API {exc.status_code}: {exc.message}") from exc
        except anthropic.AnthropicError as exc:
            raise AIUnavailable(str(exc)) from exc
        if response.stop_reason == "refusal":
            raise AIUnavailable("Claude declined to write this report")
        if response.stop_reason == "max_tokens":
            raise AIUnavailable("Claude's report was cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AIUnavailable("Claude returned unreadable output") from exc
