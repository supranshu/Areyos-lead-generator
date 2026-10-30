"""Claude reads the evidence documents and reports what they say about each
factor. Its output is checked in code: a "verified" claim survives only if its
quote is literally present in the cited document."""

import json
import re

import anthropic

from areyos.config import get_settings
from areyos.http import TransientError
from areyos.opportunity.evidence import Document
from areyos.opportunity.factors import AI_FACTORS, Evidence, FactorResult

SYSTEM = """You assess commercial venues for Areyos, an indoor wayfinding and navigation product.

You receive numbered evidence documents (D0 is structured platform data; the others are pages from the venue's own website). For each factor, report what the documents establish.

Rules:
- basis "verified": the value is stated in a document. Give at least one evidence item whose quote is copied character-for-character from that document (a short span, under 300 characters, containing the figure or statement). Numeric values must come from the quoted text (unit conversion and simple arithmetic such as annual / 365 are allowed; say so in reasoning).
- basis "estimated": no document states it, but you can make a reasonable estimate from the evidence and general knowledge of this kind of venue in India. Explain the estimate in reasoning. Quotes are optional, but any quote must still be copied exactly.
- basis "unknown": there is not enough to estimate. Set numeric_value and category to null.
- Never invent quotes, figures, projects or features. Absence of evidence is not evidence: only choose "none_found" for expansion, or "none_or_minimal" for existing wayfinding, when you are estimating from what the pages show.
- Keep finding to one plain sentence a salesperson can read. Use the factor's unit for numeric_value."""


def _factor_schema(factor) -> dict:
    nullable_number = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    category = (
        {"anyOf": [{"type": "string", "enum": list(factor.categories)}, {"type": "null"}]}
        if factor.categories
        else {"type": "null"}
    )
    return {
        "type": "object",
        "properties": {
            "basis": {"type": "string", "enum": ["verified", "estimated", "unknown"]},
            "numeric_value": nullable_number if not factor.categories else {"type": "null"},
            "category": category,
            "finding": {"type": "string"},
            "reasoning": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"doc_id": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["doc_id", "quote"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["basis", "numeric_value", "category", "finding", "reasoning", "evidence"],
        "additionalProperties": False,
    }


SCHEMA = {
    "type": "object",
    "properties": {f.key: _factor_schema(f) for f in AI_FACTORS},
    "required": [f.key for f in AI_FACTORS],
    "additionalProperties": False,
}


# A quote supporting a numeric value must contain a number, as digits or words.
NUMBER_RE = re.compile(
    r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|"
    r"hundred|thousand|lakh|crore|million|single|twin|dual|double|dozen)\b",
    re.I,
)


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def build_prompt(venue_name: str, industry_label: str, documents: list[Document]) -> str:
    factor_lines = "\n".join(
        f"- {f.key} ({f.label}; numeric_value unit: {f.unit}"
        + (f"; category one of {', '.join(f.categories)}" if f.categories else "")
        + f"): {f.question}"
        for f in AI_FACTORS
    )
    docs = "\n\n".join(
        f'<document id="{d.doc_id}" title="{d.title}">\n{d.text}\n</document>' for d in documents
    )
    return (
        f"Venue: {venue_name} ({industry_label})\n\nFactors:\n{factor_lines}\n\n"
        f"Evidence documents:\n{docs}\n\nReport every factor."
    )


def verify(raw: dict, documents: list[Document]) -> list[FactorResult]:
    """Turn the model's JSON into FactorResults, dropping quotes that are not in their document."""
    by_id = {d.doc_id: d for d in documents}
    results = []
    for factor in AI_FACTORS:
        item = raw.get(factor.key) or {}
        result = FactorResult(
            key=factor.key,
            basis=item.get("basis", "unknown"),
            numeric_value=item.get("numeric_value"),
            category=item.get("category"),
            finding=item.get("finding", ""),
            reasoning=item.get("reasoning", ""),
            method="ai",
        )
        for ev in item.get("evidence") or []:
            doc = by_id.get(ev.get("doc_id"))
            quote = (ev.get("quote") or "").strip()
            if doc and quote and _norm(quote) in _norm(doc.text):
                result.evidence.append(Evidence(doc.title, doc.url, quote))
            else:
                result.notes.append(f"Dropped a quote not found in {ev.get('doc_id')}: {quote[:80]!r}")
        if result.basis == "verified":
            numeric = not factor.categories
            supported = [e for e in result.evidence if not numeric or NUMBER_RE.search(e.quote)]
            if not supported:
                result.basis = "estimated"
                result.notes.append("Claimed as verified, but no supporting quote was found in the sources; treated as an estimate.")
        if result.basis == "unknown":
            result.numeric_value, result.category = None, None
        results.append(result)
    return results


class AIUnavailable(Exception):
    """Claude could not be used for this venue; fall back to the rules-based assessment."""


class ClaudeExtractor:
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
            except anthropic.AnthropicError as exc:  # no credentials configured
                raise AIUnavailable(str(exc)) from exc
        return self._client

    def extract(self, venue_name: str, industry_label: str, documents: list[Document]) -> list[FactorResult]:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": build_prompt(venue_name, industry_label, documents)}],
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise TransientError(f"Claude API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AIUnavailable(f"Claude API {exc.status_code}: {exc.message}") from exc
        except anthropic.AnthropicError as exc:
            raise AIUnavailable(str(exc)) from exc

        if response.stop_reason == "refusal":
            raise AIUnavailable("Claude declined to assess this venue")
        if response.stop_reason == "max_tokens":
            raise AIUnavailable("Claude's answer was cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise AIUnavailable("Claude returned unreadable output") from exc
        return verify(raw, documents)
