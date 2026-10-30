"""Venue size estimation and priority scoring.

Priority score (0-100) =
    industry fit      0-30  how much the industry needs indoor wayfinding
  + venue size        0-30  bigger and more complex venues need it more
  + footfall          0-15  Google review volume, relative to the industry
  + reachability      0-15  website, phone, public email
  + operational       0-10  business status
A name or type that does not match the industry halves the industry fit.
"""

import math
import re

from areyos.catalog.industries import FOOTPRINT_THRESHOLDS, Industry, get_industry

SIZE_ORDER = ["small", "medium", "large", "mega"]
SIZE_POINTS = {"small": 5, "medium": 15, "large": 25, "mega": 30, "unknown": 10}
HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 45


def _to_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d[\d,]*", str(value))
    return float(match.group().replace(",", "")) if match else None


def classify_size(industry: Industry, name: str, rating_count: int | None, attributes: dict) -> tuple[str, dict]:
    signals: dict[str, str] = {}
    if (cls := industry.review_thresholds.classify(rating_count)) is not None:
        signals["reviews"] = cls
    if industry.capacity_tag and industry.capacity_thresholds:
        if (cls := industry.capacity_thresholds.classify(_to_number(attributes.get(industry.capacity_tag)))) is not None:
            signals[industry.capacity_tag] = cls
    if (cls := FOOTPRINT_THRESHOLDS.classify(_to_number(attributes.get("footprint_m2")))) is not None:
        signals["footprint"] = cls
    lowered = f" {name.lower()} "
    if any(k in lowered for k in industry.large_keywords):
        signals["name"] = "large"
    if attributes.get("iata"):
        signals["iata"] = "large"
    if not signals:
        return "unknown", signals
    return max(signals.values(), key=SIZE_ORDER.index), signals


def matches_industry(industry: Industry, name: str, types: list[str]) -> bool:
    if industry.google_types & set(types):
        return True
    # OSM types arrive as "key=value"; every OSM result came from an industry selector.
    if any("=" in t for t in types):
        return True
    lowered = name.lower()
    return any(k in lowered for k in industry.name_keywords)


def score(
    *,
    industry_key: str,
    name: str,
    size_class: str,
    rating_count: int | None,
    types: list[str],
    website: str | None,
    phone: str | None,
    has_email: bool,
    business_status: str | None,
) -> tuple[int, str, dict]:
    industry = get_industry(industry_key)
    relevant = matches_industry(industry, name, types)
    fit = industry.fit_weight if relevant else industry.fit_weight // 2

    size = SIZE_POINTS[size_class]

    footfall = 0
    if rating_count:
        footfall = round(min(15, 15 * math.log10(rating_count + 1) / math.log10(industry.review_thresholds.mega + 1)))

    reach = (5 if website else 0) + (5 if phone else 0) + (5 if has_email else 0)

    operational = {"OPERATIONAL": 10, None: 8, "CLOSED_TEMPORARILY": 0}.get(business_status, 0)

    breakdown = {
        "industry_fit": fit,
        "venue_size": size,
        "footfall": footfall,
        "reachability": reach,
        "operational": operational,
        "industry_match": relevant,
    }
    total = fit + size + footfall + reach + operational
    if business_status == "CLOSED_PERMANENTLY":
        total = 0
    level = "high" if total >= HIGH_THRESHOLD else "medium" if total >= MEDIUM_THRESHOLD else "low"
    return total, level, breakdown
