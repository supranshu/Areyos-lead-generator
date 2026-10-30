"""Opportunity factors, weights and deterministic scoring rubrics.

The score (0-100) is the sum of factor points. Points come from fixed rubrics,
never from the model, so the same facts always give the same score:

    verified   rubric fraction x weight
    estimated  rubric fraction x weight x ESTIMATE_DISCOUNT
    unknown    UNKNOWN_FRACTION x weight (neutral)
"""

from dataclasses import asdict, dataclass, field

ESTIMATE_DISCOUNT = 0.85
UNKNOWN_FRACTION = 0.3
HIGH_THRESHOLD = 65
MEDIUM_THRESHOLD = 40


@dataclass(frozen=True)
class Factor:
    key: str
    label: str
    weight: int
    unit: str  # what numeric_value measures, or "category"
    # Numeric rubric: (lower bound, fraction) pairs, highest bound first.
    bands: tuple[tuple[float, float], ...] = ()
    # Categorical rubric.
    categories: dict[str, float] = field(default_factory=dict)
    question: str = ""

    def fraction(self, numeric: float | None, category: str | None) -> float | None:
        if self.categories:
            return self.categories.get(category or "")
        if numeric is None:
            return None
        for bound, frac in self.bands:
            if numeric >= bound:
                return frac
        return self.bands[-1][1]


FACTORS: list[Factor] = [
    Factor("building_size", "Building size", 14, "square metres of built-up area",
           bands=((100_000, 1.0), (50_000, 0.85), (20_000, 0.7), (5_000, 0.45), (0, 0.2)),
           question="Total built-up / floor area of the venue. Convert sq ft to sq m (x 0.0929) and acres of land are NOT floor area."),
    Factor("floors", "Number of floors", 10, "floors including basements",
           bands=((13, 1.0), (7, 0.8), (4, 0.6), (2, 0.35), (0, 0.1)),
           question="Floors in the main building, counting basements and podium levels."),
    Factor("departments", "Departments or sections", 10, "distinct departments / specialities / stores / halls",
           bands=((100, 1.0), (40, 0.85), (15, 0.7), (5, 0.45), (0, 0.2)),
           question="Number of separate destinations a visitor might look for: medical departments and specialities, shops, halls, faculties, gates."),
    Factor("footfall", "Daily visitor footfall", 16, "visitors per day",
           bands=((50_000, 1.0), (10_000, 0.85), (2_000, 0.65), (500, 0.4), (0, 0.15)),
           question="Average visitors per day. Convert annual or monthly figures (annual / 365). Staff do not count."),
    Factor("buildings", "Number of buildings", 10, "buildings / blocks / towers / terminals",
           bands=((10, 1.0), (4, 0.8), (2, 0.55), (0, 0.2)),
           question="Separate buildings, blocks, towers, wings or terminals that visitors move between."),
    Factor("navigation_complexity", "Visitor navigation complexity", 14, "category",
           categories={"very_high": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2},
           question=("How hard it is for a first-time visitor to find their way: many entrances, interconnected blocks, "
                     "time-critical or stressed visitors (patients, travellers), changing layouts, multiple levels.")),
    Factor("existing_wayfinding", "Existing wayfinding infrastructure", 8, "category",
           categories={"none_or_minimal": 1.0, "static_signage": 0.85, "static_maps_or_help_desk": 0.7,
                       "digital_kiosks": 0.5, "indoor_navigation_app": 0.25},
           question=("The most advanced wayfinding the venue already has. Less existing digital wayfinding means a larger "
                     "opening; an existing indoor navigation app means a competitor is in place.")),
    Factor("expansion", "Expansion or renovation activity", 10, "category",
           categories={"under_construction": 1.0, "planned_or_announced": 0.8, "recently_completed": 0.6, "none_found": 0.25},
           question=("New wings, blocks, floors, terminals, renovations or campus expansions under way, announced, or finished "
                     "in the last two years. A new building is the natural moment to add wayfinding.")),
    Factor("decision_makers", "Identifiable decision-makers", 8, "category",
           categories={"reachable_buyer": 1.0, "buyer_identified": 0.8, "some_leadership": 0.5, "none_found": 0.1},
           question="Computed from the decision-makers table, not by the model."),
]

FACTORS_BY_KEY = {f.key: f for f in FACTORS}
AI_FACTORS = [f for f in FACTORS if f.key != "decision_makers"]
assert sum(f.weight for f in FACTORS) == 100


@dataclass
class Evidence:
    source: str  # document title, e.g. "Website: /about-us" or "OpenStreetMap"
    url: str | None
    quote: str


@dataclass
class FactorResult:
    key: str
    basis: str  # verified | estimated | unknown
    numeric_value: float | None = None
    category: str | None = None
    finding: str = ""
    reasoning: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    method: str = "heuristic"  # ai | data | heuristic
    notes: list[str] = field(default_factory=list)
    points: float = 0.0
    max_points: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def score_factor(result: FactorResult) -> FactorResult:
    factor = FACTORS_BY_KEY[result.key]
    result.max_points = factor.weight
    frac = None if result.basis == "unknown" else factor.fraction(result.numeric_value, result.category)
    if frac is None:
        if result.basis != "unknown":
            result.notes.append("No usable value; scored as unknown.")
        result.basis = "unknown"
        result.points = round(UNKNOWN_FRACTION * factor.weight, 1)
    elif result.basis == "estimated":
        result.points = round(frac * factor.weight * ESTIMATE_DISCOUNT, 1)
    else:
        result.points = round(frac * factor.weight, 1)
    return result


def classify(score: int) -> str:
    return "high" if score >= HIGH_THRESHOLD else "medium" if score >= MEDIUM_THRESHOLD else "low"


def explain_points(result: FactorResult) -> str:
    factor = FACTORS_BY_KEY[result.key]
    if result.basis == "unknown":
        return f"{result.points:g}/{factor.weight} (no evidence; neutral {int(UNKNOWN_FRACTION * 100)}% of weight)"
    frac = factor.fraction(result.numeric_value, result.category)
    text = f"{result.points:g}/{factor.weight} = {frac:.0%} of weight"
    if result.basis == "estimated":
        text += f" x {ESTIMATE_DISCOUNT} estimate discount"
    return text


def summarise(results: list[FactorResult], score: int, level: str) -> str:
    """A plain-language explanation built only from the factor findings."""
    ranked = sorted(results, key=lambda r: r.points / r.max_points, reverse=True)
    drivers = [r for r in ranked if r.basis != "unknown" and r.points / r.max_points >= 0.7][:3]
    weak = [r for r in reversed(ranked) if r.basis != "unknown" and r.points / r.max_points < 0.4][:2]
    unknown = [FACTORS_BY_KEY[r.key].label.lower() for r in results if r.basis == "unknown"]
    verified = sum(1 for r in results if r.basis == "verified")
    parts = [f"{level.capitalize()} opportunity ({score}/100)."]
    if drivers:
        parts.append("Main drivers: " + "; ".join(
            f"{FACTORS_BY_KEY[r.key].label.lower()} — {r.finding.rstrip('.')} ({r.basis})" for r in drivers) + ".")
    if weak:
        parts.append("Holding it back: " + "; ".join(
            f"{FACTORS_BY_KEY[r.key].label.lower()} — {r.finding.rstrip('.')} ({r.basis})" for r in weak) + ".")
    parts.append(f"{verified} of {len(results)} factors are backed by verified sources.")
    if unknown:
        parts.append("No evidence found for: " + ", ".join(unknown) + ".")
    return " ".join(parts)
