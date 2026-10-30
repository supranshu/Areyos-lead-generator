"""Opportunity reports: seven sections of short statements, each tagged and cited.

Statement kinds:
  evidence    - a fact a cited source states
  inference   - a conclusion drawn from cited facts
  suggestion  - sales advice (allowed only in sections 4-7)
  gap         - the sources do not establish this (added by code, never by the model)

Citations reference:
  D<n>      an evidence document, with a quote that must appear in it
  F:<key>   a factor of the venue's opportunity assessment
  P:<id>    a decision-maker found for the venue
  C:<id>    a public contact of the venue

Every citation is resolved in code. Unresolvable citations are dropped; a
statement left without the support its kind needs is removed and listed in
the report's notes. That is what keeps the report from inventing problems.
"""

import re
from dataclasses import dataclass, field

from areyos.catalog.industries import get_industry
from areyos.people.roles import INDUSTRY_BOOST, ROLES, ROLES_BY_KEY

SECTIONS = [
    ("navigation_difficulty", "What makes this venue difficult to navigate?"),
    ("visitor_need", "Why might visitors need indoor navigation?"),
    ("business_problem", "What is the likely business problem?"),
    ("responsible", "Who is responsible for solving that problem?"),
    ("pitch", "What would be the strongest Areyos sales pitch?"),
    ("objections", "What objections might the decision-maker raise?"),
    ("demo_approach", "What would be the best approach for requesting a demo?"),
]
SECTION_KEYS = [k for k, _ in SECTIONS]
# Sections 1-3 describe the venue's situation: no advice, only cited facts and inferences.
FACT_SECTIONS = {"navigation_difficulty", "visitor_need", "business_problem"}
GAP_TEXT = "Public sources reviewed so far don't establish this. Ask about it on the first call."
QUALIFY_FIRST = (
    "The evidence does not yet show a navigation problem at this venue. Don't claim one: open with discovery "
    "questions about visitor complaints, help-desk load and signage upkeep before presenting Areyos."
)


@dataclass
class ReportContext:
    venue_name: str
    industry: str
    documents: dict[str, dict]  # doc_id -> {title, url, text}
    factors: dict[str, dict]  # factor key -> assessment factor dict
    people: dict[int, dict]  # person id -> {name, designation, role, relevance, linkedin, emails, phones, source_url}
    contacts: dict[int, dict]  # contact id -> {kind, value, source_url}


@dataclass
class VerifiedReport:
    sections: dict[str, list[dict]]
    evidence_level: str  # strong | partial | insufficient
    notes: list[str] = field(default_factory=list)

    def sources(self) -> list[dict]:
        seen, out = set(), []
        for statements in self.sections.values():
            for st in statements:
                for c in st["citations"]:
                    if c.get("url") and c["url"] not in seen:
                        seen.add(c["url"])
                        out.append({"url": c["url"], "source": c["source"]})
        return out


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def _names_person(text: str, name: str) -> bool:
    """The statement must name the person it cites (honorifics and initials aside)."""
    tokens = [t for t in re.findall(r"[a-z]+", name.lower()) if len(t) > 2 and t not in {"dr", "mr", "mrs", "ms", "prof"}]
    return bool(tokens) and all(t in text.lower() for t in tokens)


def resolve(ref: str, quote: str | None, ctx: ReportContext, text: str = "") -> dict | None:
    ref = (ref or "").strip()
    if re.fullmatch(r"D\d+", ref):
        doc = ctx.documents.get(ref)
        if doc and quote and _norm(quote) in _norm(doc["text"]):
            return {"ref": ref, "quote": quote.strip(), "source": doc["title"], "url": doc.get("url"), "basis": "verified"}
        return None
    if ref.startswith("F:"):
        f = ctx.factors.get(ref[2:])
        if not f or f["basis"] == "unknown":
            return None
        url = next((e.get("url") for e in f.get("evidence", []) if e.get("url")), None)
        return {"ref": ref, "quote": f["finding"], "source": f"{f['label']} ({f['basis']})", "url": url, "basis": f["basis"]}
    if ref.startswith("P:") and ref[2:].isdigit():
        p = ctx.people.get(int(ref[2:]))
        if not p or not _names_person(text, p["name"]):
            return None
        return {"ref": ref, "quote": f"{p['name']}, {p['designation']}", "source": "Decision-maker",
                "url": p.get("source_url") or p.get("linkedin"), "basis": "verified"}
    if ref.startswith("C:") and ref[2:].isdigit():
        c = ctx.contacts.get(int(ref[2:]))
        if not c:
            return None
        return {"ref": ref, "quote": f"{c['kind']}: {c['value']}", "source": "Venue public contact",
                "url": c["source_url"], "basis": "verified"}
    return None


def verify(raw_sections: dict, ctx: ReportContext) -> VerifiedReport:
    sections: dict[str, list[dict]] = {}
    notes: list[str] = []
    for key in SECTION_KEYS:
        kept = []
        for st in (raw_sections.get(key) or {}).get("statements", []):
            text = (st.get("text") or "").strip()
            if not text:
                continue
            kind = st.get("kind", "inference")
            if kind == "gap":
                continue  # gaps are decided in code
            if kind == "suggestion" and key in FACT_SECTIONS:
                kind = "inference"
            citations = [c for c in (resolve(x.get("ref"), x.get("quote"), ctx, text) for x in st.get("citations", [])) if c]
            dropped = len(st.get("citations", [])) - len(citations)
            if dropped:
                notes.append(f"{key}: dropped {dropped} citation(s) that did not match the sources.")
            if kind == "evidence" and not any(c["basis"] == "verified" for c in citations):
                kind = "inference"  # only estimates (or nothing) behind it
            if kind in ("evidence", "inference") and not citations:
                notes.append(f"{key}: removed unsupported claim: {text[:120]}")
                continue
            kept.append({"text": text, "kind": kind, "citations": citations})
        if key in FACT_SECTIONS and not kept:
            kept = [{"text": GAP_TEXT, "kind": "gap", "citations": []}]
        sections[key] = kept

    verified_facts = sum(
        1 for k in FACT_SECTIONS for st in sections[k]
        if st["kind"] in ("evidence", "inference") and any(c["basis"] == "verified" for c in st["citations"])
    )
    level = "strong" if verified_facts >= 3 else "partial" if verified_facts >= 1 else "insufficient"
    if level == "insufficient":
        sections["pitch"] = [{"text": QUALIFY_FIRST, "kind": "gap", "citations": []}] + sections.get("pitch", [])
    return VerifiedReport(sections, level, notes)


# ---------------------------------------------------------------- rules-based report

VALUE_PROP = {
    "healthcare": "patients and attendants reach the right OPD, ward, lab or billing counter without asking, which cuts missed appointments and help-desk queues",
    "retail": "shoppers find stores, offers and parking quickly, and the mall gets data on how visitors move",
    "convention": "exhibitors and delegates find halls, stalls and sessions during peak event days",
    "education": "new students, parents and visitors find departments, labs and exam halls across the campus",
    "transport": "travellers reach gates, platforms, counters and amenities with less stress and fewer missed connections",
    "corporate": "visitors and new employees find towers, floors, meeting rooms and amenities without reception escorts",
    "hospitality": "guests find banquet halls, restaurants, spa and meeting rooms without calling the front desk",
    "government": "citizens reach the right office or counter first time, reducing crowding and repeat enquiries",
    "event_venues": "attendees find halls, seating, exits and amenities during busy events",
    "sports": "spectators reach the right gate, stand and seat and find amenities and exits",
    "cultural": "visitors follow galleries and exhibits in order and find facilities easily",
    "industrial": "visitors, contractors and new staff find buildings, gates and safety points across the site",
}
VISITOR_WORD = {
    "healthcare": "patients and their families", "retail": "shoppers", "convention": "exhibitors and delegates",
    "education": "students and visitors", "transport": "travellers", "corporate": "visitors and employees",
    "hospitality": "guests", "government": "citizens", "event_venues": "attendees", "sports": "spectators",
    "cultural": "visitors", "industrial": "visitors and contractors",
}


def _f(ctx: ReportContext, key: str) -> dict | None:
    f = ctx.factors.get(key)
    return f if f and f["basis"] != "unknown" else None


def _st(text: str, kind: str, *refs: str) -> dict:
    return {"text": text, "kind": kind, "citations": [{"ref": r, "quote": None} for r in refs]}


def template_sections(ctx: ReportContext) -> dict:
    """A report built only from the assessment's factors and stored decision-makers."""
    industry = get_industry(ctx.industry)
    visitors = VISITOR_WORD.get(ctx.industry, "visitors")
    s = {k: {"statements": []} for k in SECTION_KEYS}

    def add(section: str, text: str, kind: str, *refs: str):
        s[section]["statements"].append(_st(text, kind, *refs))

    def kind_of(f: dict) -> str:
        return "evidence" if f["basis"] == "verified" else "inference"

    # 1. Navigation difficulty: physical scale and layout.
    for key in ("floors", "buildings", "departments", "building_size"):
        if f := _f(ctx, key):
            add("navigation_difficulty", f["finding"], kind_of(f), f"F:{key}")
    if (f := _f(ctx, "navigation_complexity")) and f["category"] in ("high", "very_high"):
        add("navigation_difficulty", f["finding"], kind_of(f), "F:navigation_complexity")

    # 2. Visitor need: who comes and how many.
    if f := _f(ctx, "footfall"):
        add("visitor_need", f"{f['finding']} Many of them are likely to be first-time {visitors}.", "inference", "F:footfall")
    if (f := _f(ctx, "navigation_complexity")) and f["category"] in ("high", "very_high"):
        add("visitor_need", f"{visitors.capitalize()} at a venue like this are often unfamiliar with the layout or under time pressure.",
            "inference", "F:navigation_complexity")

    # 3. Business problem: current wayfinding and change.
    wf, exp = _f(ctx, "existing_wayfinding"), _f(ctx, "expansion")
    if wf and wf["category"] in ("none_or_minimal", "static_signage", "static_maps_or_help_desk"):
        add("business_problem", f"{wf['finding']} Static signage and staffed help desks don't scale with visitor numbers and need reprinting when layouts change.",
            "inference", "F:existing_wayfinding")
    if wf and wf["category"] == "indoor_navigation_app":
        add("business_problem", f"{wf['finding']} Any opportunity is a replacement or upgrade, not a first deployment.",
            "inference", "F:existing_wayfinding")
    if exp and exp["category"] in ("under_construction", "planned_or_announced", "recently_completed"):
        add("business_problem", f"{exp['finding']} New or changed space means signage, maps and visitor directions must be redone.",
            "inference", "F:expansion")

    # 4. Responsible: named people first, otherwise the role to look for.
    people = sorted(ctx.people.items(), key=lambda kv: -kv[1]["relevance"])[:3]
    for pid, p in people:
        add("responsible", f"{p['name']} ({p['designation']}) is the most likely owner as {ROLES_BY_KEY[p['role']].label}.",
            "evidence", f"P:{pid}")
    if not people:
        boosts = INDUSTRY_BOOST.get(ctx.industry, {})
        role = max(ROLES, key=lambda r: r.weight + boosts.get(r.key, 0))
        add("responsible", f"No named decision-maker has been found yet. Look for the {role.label} or equivalent; "
            f"in {industry.label.lower()} that role usually owns visitor experience and facilities.", "suggestion")

    # 5. Pitch: lead with the strongest supported pain.
    pains = [st for k in ("business_problem", "navigation_difficulty") for st in s[k]["statements"]]
    if pains:
        lead = pains[0]
        add("pitch", f"Lead with what they already know: {lead['text'].split('. ')[0].rstrip('.')}. Areyos means "
            f"{VALUE_PROP.get(ctx.industry, 'visitors find their way without asking')}.", "suggestion",
            *[c["ref"] for c in lead["citations"]])
    if exp and exp["category"] in ("under_construction", "planned_or_announced"):
        add("pitch", "Tie the pitch to the new building: digital maps can be ready on opening day and updated without reprinting signs.",
            "suggestion", "F:expansion")

    # 6. Objections, each only where the evidence makes it likely.
    if wf and wf["category"] in ("static_signage", "static_maps_or_help_desk"):
        add("objections", "\"Our signage and help desk already work.\" Answer: ask how many direction queries the desk handles a day, and how often signs change.",
            "suggestion", "F:existing_wayfinding")
    if wf and wf["category"] == "indoor_navigation_app":
        add("objections", "\"We already have a navigation app.\" Answer: compare usage, map freshness and update effort before pitching a switch.",
            "suggestion", "F:existing_wayfinding")
    add("objections", "\"What's the return?\" Answer: tie it to help-desk staff time, missed appointments or bookings, and visitor satisfaction, measured in a pilot.",
        "suggestion")
    if ctx.industry in ("healthcare", "government", "corporate", "transport"):
        add("objections", "\"IT security and data privacy.\" Answer: explain what visitor data is and isn't collected, hosting, and DPDP Act compliance.",
            "suggestion")
    if exp and exp["category"] in ("under_construction", "planned_or_announced"):
        add("objections", "\"Let's wait until the new building opens.\" Answer: mapping before opening avoids a second signage cycle.",
            "suggestion", "F:expansion")

    # 7. Demo approach: who, through which channel, with which hook.
    if people:
        pid, p = people[0]
        channel = (f"email {p['emails'][0]}" if p["emails"] else "a LinkedIn message" if p["linkedin"]
                   else "the main switchboard, asking for their office")
        add("demo_approach", f"Contact {p['name']} through {channel}, referencing their role and offering a 20-minute demo on their own floor plan.",
            "suggestion", f"P:{pid}")
    else:
        contact = next(iter(ctx.contacts.items()), None)
        if contact:
            cid, c = contact
            add("demo_approach", f"Use the venue's public {c['kind'].replace('_', ' ')} to ask who owns visitor experience or facilities, then request a short demo.",
                "suggestion", f"C:{cid}")
        else:
            add("demo_approach", "Find the facilities or administration office through the venue's main number before requesting a demo.", "suggestion")
    if exp and exp["category"] in ("under_construction", "planned_or_announced"):
        add("demo_approach", "Time the request to the construction schedule, ideally before interior fit-out is finalised.", "suggestion", "F:expansion")
    return s
