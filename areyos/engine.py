"""Discovery engine.

A run fans out into jobs:  plan -> discover (per industry x provider x query x tile)
-> enrich (per new prospect with a website). Network calls happen outside
database transactions; merges into prospects happen under a per-city advisory
lock so parallel workers never insert the same venue twice.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from areyos import queue
from areyos.catalog.industries import INDUSTRIES, get_industry
from areyos.config import get_settings
from areyos.dedup import rescore, upsert_place
from areyos.enrichment.website import WebsiteEnricher
from areyos.http import TransientError
from areyos.models import Contact, DiscoveryRun, GeoCache, Job, OpportunityAssessment, Prospect, RunProspect
from areyos.enrichment.crawler import SiteCrawler
from areyos.opportunity import service as opportunity
from areyos.opportunity.evidence import platform_document, website_documents
from areyos.opportunity.extractor import AIUnavailable, ClaudeExtractor
from areyos.reports import service as reports
from areyos.reports.ai import ClaudeReportWriter
from areyos.people.service import merge_candidates, venue_context, wants_people
from areyos.people.sources import HunterPeopleSource, SearchPeopleSource, WebsitePeopleSource
from areyos.providers.base import Area, RawPlace
from areyos.providers.google_places import GooglePlacesError, GooglePlacesProvider
from areyos.providers.osm import NominatimGeocoder, OverpassProvider

log = logging.getLogger(__name__)

PROVIDERS = ("google_places", "osm")


class RunValidationError(ValueError):
    pass


def available_providers() -> list[str]:
    settings = get_settings()
    out = []
    if settings.google_places_api_key:
        out.append("google_places")
    if settings.osm_enabled:
        out.append("osm")
    return out


class Registry:
    """Lazily built provider clients, one set per worker process. Tests swap these out."""

    def __init__(self):
        self._cache: dict[str, object] = {}

    def get(self, name: str):
        if name not in self._cache:
            factories = {
                "google_places": GooglePlacesProvider,
                "osm": OverpassProvider,
                "nominatim": NominatimGeocoder,
                "enricher": WebsiteEnricher,
                "people_website": WebsitePeopleSource,
                "people_search": SearchPeopleSource,
                "people_hunter": HunterPeopleSource,
                "opportunity_ai": ClaudeExtractor,
                "opportunity_crawler": SiteCrawler,
                "report_ai": ClaudeReportWriter,
            }
            self._cache[name] = factories[name]()
        return self._cache[name]

    def set(self, name: str, obj) -> None:
        self._cache[name] = obj


registry = Registry()


# ---------------------------------------------------------------- runs

def create_run(
    session: Session,
    *,
    state: str,
    city: str,
    industries: list[str],
    venue_sizes: list[str] | None = None,
    priority_levels: list[str] | None = None,
    providers: list[str] | None = None,
    find_people: bool = False,
    assess_opportunity: bool = False,
    generate_reports: bool = False,
) -> DiscoveryRun:
    if not state.strip() or not city.strip():
        raise RunValidationError("state and city are required")
    industries = industries or list(INDUSTRIES)
    for key in industries:
        if key not in INDUSTRIES:
            raise RunValidationError(f"unknown industry {key!r}")
    usable = available_providers()
    providers = providers or usable
    missing = [p for p in providers if p not in usable]
    if missing:
        raise RunValidationError(f"provider(s) not configured: {', '.join(missing)}")
    if not providers:
        raise RunValidationError("no discovery provider configured; set AREYOS_GOOGLE_PLACES_API_KEY or enable OSM")

    run = DiscoveryRun(
        state=state.strip(),
        city=city.strip(),
        industries=industries,
        venue_sizes=venue_sizes or [],
        priority_levels=priority_levels or [],
        providers=providers,
        find_people=find_people,
        # A report is built on an assessment, so asking for reports implies scoring.
        assess_opportunity=assess_opportunity or generate_reports,
        generate_reports=generate_reports,
        status="queued",
        stats={},
    )
    session.add(run)
    session.flush()
    queue.enqueue(session, "plan", {}, run_id=run.id)
    return run


def geocode(session: Session, city: str, state: str) -> Area:
    key = f"{city.lower()}|{state.lower()}"
    cached = session.get(GeoCache, key)
    if cached:
        return Area.from_dict(cached.data)
    area = None
    if get_settings().google_places_api_key:
        area = registry.get("google_places").geocode(city, state)
    if area is None and get_settings().osm_enabled:
        area = registry.get("nominatim").geocode(city, state)
    if area is None:
        raise RunValidationError(f"could not locate {city}, {state}")
    session.merge(GeoCache(key=key, data=area.to_dict()))
    return area


def handle_plan(session: Session, job: Job) -> None:
    run = session.get(DiscoveryRun, job.run_id)
    run.status = "running"
    area = geocode(session, run.city, run.state)
    tiles = area.tiles(get_settings().google_grid_size)
    planned = 0
    for industry_key in run.industries:
        industry = get_industry(industry_key)
        if "google_places" in run.providers:
            for q in industry.google_queries:
                for tile in tiles:
                    queue.enqueue(
                        session,
                        "discover",
                        {"provider": "google_places", "industry": industry_key, "query": q, "area": tile.to_dict()},
                        run_id=run.id,
                    )
                    planned += 1
        if "osm" in run.providers:
            queue.enqueue(
                session, "discover", {"provider": "osm", "industry": industry_key, "area": area.to_dict()}, run_id=run.id
            )
            planned += 1
    run.stats = {**(run.stats or {}), "area": area.to_dict(), "discover_jobs": planned}


def _lock(session: Session, name: str) -> None:
    key = int.from_bytes(hashlib.sha1(name.lower().encode()).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def enqueue_once(session: Session, kind: str, payload: dict, run_id: int | None) -> None:
    """Skip if this run already has a live or finished job of this kind for the prospect.
    Callers hold a lock that serialises these checks."""
    exists = session.scalar(
        select(Job.id).where(
            Job.kind == kind,
            Job.run_id == run_id,
            Job.payload["prospect_id"].as_integer() == payload["prospect_id"],
            Job.status != "failed",
        ).limit(1)
    )
    if exists is None:
        queue.enqueue(session, kind, payload, run_id=run_id)


def merge_places(
    session: Session, run: DiscoveryRun, industry_key: str, places: list[RawPlace]
) -> list[tuple[Prospect, bool]]:
    _lock(session, f"city:{run.city}")
    merged = []
    for raw in places:
        if not raw.name:
            continue
        prospect, is_new = upsert_place(session, raw, industry_key, run.city, run.state)
        session.execute(
            insert(RunProspect)
            .values(run_id=run.id, prospect_id=prospect.id, is_new=is_new)
            .on_conflict_do_nothing()
        )
        merged.append((prospect, is_new))
    return merged


def needs_enrichment(prospect: Prospect) -> bool:
    if not prospect.website or not get_settings().enrich_enabled:
        return False
    if prospect.enriched_at is None:
        return True
    return prospect.enriched_at < datetime.now(timezone.utc) - timedelta(days=get_settings().enrich_refresh_days)


def handle_discover(session: Session, job: Job) -> None:
    p = job.payload
    run = session.get(DiscoveryRun, job.run_id)
    session.commit()  # release the row before the slow network call
    provider = registry.get(p["provider"])
    places = provider.search(p["industry"], Area.from_dict(p["area"]), p.get("query"))
    for prospect, _ in merge_places(session, run, p["industry"], places):
        if needs_enrichment(prospect):
            enqueue_once(session, "enrich", {"prospect_id": prospect.id}, run.id)
        else:
            queue_next_step(session, run, prospect, after="enrich")


def handle_enrich(session: Session, job: Job) -> None:
    prospect = session.get(Prospect, job.payload["prospect_id"])
    if prospect is None:
        return
    if not needs_enrichment(prospect):
        _after_enrich(session, job, prospect)
        return
    website = prospect.website
    session.commit()
    found = registry.get("enricher").enrich(website)
    for c in found:
        session.execute(
            insert(Contact)
            .values(prospect_id=prospect.id, kind=c.kind, value=c.value[:1000], source_url=c.source_url[:1000])
            .on_conflict_do_nothing()
        )
    prospect.enriched_at = datetime.now(timezone.utc)
    session.flush()
    rescore(session, prospect)
    _after_enrich(session, job, prospect)


PIPELINE = ("enrich", "people", "opportunity", "report")


def queue_next_step(session: Session, run: DiscoveryRun | None, prospect: Prospect, after: str) -> None:
    """Per prospect the pipeline is enrich -> people -> opportunity; each step is
    included when the run asked for it and the prospect qualifies. People come
    after enrichment (an email found there can raise priority) and the
    opportunity score comes last because decision-makers are one of its factors."""
    if run is None:
        return
    for step in PIPELINE[PIPELINE.index(after) + 1:]:
        if step == "people" and run.find_people and wants_people(prospect):
            enqueue_once(session, "people", {"prospect_id": prospect.id}, run.id)
            return
        if step == "opportunity" and run.assess_opportunity and opportunity.wants_assessment(prospect):
            enqueue_once(session, "opportunity", {"prospect_id": prospect.id}, run.id)
            return
        if step == "report" and run.generate_reports:
            enqueue_once(session, "report", {"prospect_id": prospect.id}, run.id)
            return


def _after(session: Session, job: Job, prospect: Prospect, step: str) -> None:
    if job.run_id is None:
        return
    _lock(session, f"prospect:{prospect.id}")
    queue_next_step(session, session.get(DiscoveryRun, job.run_id), prospect, after=step)


def _after_enrich(session: Session, job: Job, prospect: Prospect) -> None:
    _after(session, job, prospect, "enrich")


def handle_people(session: Session, job: Job) -> None:
    prospect = session.get(Prospect, job.payload["prospect_id"])
    if prospect is None or not (job.payload.get("force") or wants_people(prospect)):
        return
    venue = venue_context(prospect)
    session.commit()
    candidates = []
    for name in ("people_website", "people_search", "people_hunter"):
        source = registry.get(name)
        if getattr(source, "enabled", True):
            candidates.extend(source.find(venue))
    _lock(session, f"prospect:{prospect.id}")
    prospect = session.get(Prospect, prospect.id)
    stats = merge_candidates(session, prospect, candidates)
    log.info("people for prospect %s: %s", prospect.id, stats)
    session.flush()
    session.expire(prospect, ["people"])
    _after(session, job, prospect, "people")


def handle_opportunity(session: Session, job: Job) -> None:
    prospect = session.get(Prospect, job.payload["prospect_id"])
    if prospect is None or not opportunity.wants_assessment(prospect, force=job.payload.get("force", False)):
        return
    force = job.payload.get("force", False)
    use_ai = opportunity.ai_eligible(prospect, force)
    documents = [platform_document(prospect)]
    website, name, industry_label = prospect.website, prospect.name, get_industry(prospect.industry).label
    session.commit()  # no transaction held during crawling and the model call

    if use_ai:  # rules-only scoring cannot read web pages, so don't crawl for it
        documents += website_documents(website, registry.get("opportunity_crawler"))
    ai_results, note = None, None
    if use_ai:
        try:
            ai_results = registry.get("opportunity_ai").extract(name, industry_label, documents)
        except AIUnavailable as exc:
            note = f"AI assessment unavailable ({exc}); scored from platform data and rules only."
    elif get_settings().opportunity_ai:
        note = "Below the priority threshold for AI review; scored from platform data and rules only."
    else:
        note = "AI assessment disabled; scored from platform data and rules only."

    _lock(session, f"prospect:{prospect.id}")
    prospect = session.get(Prospect, prospect.id)
    opportunity.save(session, prospect, opportunity.build_assessment(prospect, documents, ai_results, note))
    session.flush()
    _after(session, job, prospect, "opportunity")


def handle_report(session: Session, job: Job) -> None:
    prospect = session.get(Prospect, job.payload["prospect_id"])
    if prospect is None:
        return
    assessment = reports.latest_assessment(session, prospect.id)
    if assessment is None:
        # No score yet: make the free rules-only assessment first.
        assessment = opportunity.build_assessment(prospect, [platform_document(prospect)], None,
                                                  "Assessed from platform data and rules only, for a report.")
        opportunity.save(session, prospect, assessment)
        session.flush()
    ctx = reports.build_context(prospect, assessment)
    use_ai = opportunity.ai_eligible(prospect, job.payload.get("force", False))
    session.commit()

    draft, note = None, None
    if use_ai:
        try:
            draft = registry.get("report_ai").draft(ctx)
        except AIUnavailable as exc:
            note = f"AI report unavailable ({exc}); built from the assessment's findings only."
    else:
        note = "Built from the assessment's findings only (below the priority threshold for AI, or AI disabled)."
    prospect = session.get(Prospect, prospect.id)
    session.add(reports.make_report(prospect, session.get(OpportunityAssessment, assessment.id), ctx, draft, note))


def enqueue_people_for_run(session: Session, run_id: int) -> int:
    """Queue decision-maker discovery for a finished run's prospects."""
    run = session.get(DiscoveryRun, run_id)
    run.find_people = True
    count = 0
    for prospect in session.scalars(
        select(Prospect).join(RunProspect, RunProspect.prospect_id == Prospect.id).where(RunProspect.run_id == run_id)
    ):
        if wants_people(prospect):
            enqueue_once(session, "people", {"prospect_id": prospect.id}, run_id)
            count += 1
    if count:
        run.status, run.finished_at = "running", None
    return count


def enqueue_opportunity_for_run(session: Session, run_id: int) -> int:
    """Queue opportunity assessments for an existing run's qualifying prospects."""
    run = session.get(DiscoveryRun, run_id)
    run.assess_opportunity = True
    count = 0
    for prospect in session.scalars(
        select(Prospect).join(RunProspect, RunProspect.prospect_id == Prospect.id).where(RunProspect.run_id == run_id)
    ):
        if opportunity.wants_assessment(prospect):
            enqueue_once(session, "opportunity", {"prospect_id": prospect.id}, run_id)
            count += 1
    if count:
        run.status, run.finished_at = "running", None
    return count


HANDLERS = {
    "plan": handle_plan,
    "discover": handle_discover,
    "enrich": handle_enrich,
    "people": handle_people,
    "opportunity": handle_opportunity,
    "report": handle_report,
}


def enqueue_reports_for_run(session: Session, run_id: int) -> int:
    """Queue reports for every prospect of an existing run."""
    run = session.get(DiscoveryRun, run_id)
    run.generate_reports = True
    count = 0
    for prospect in session.scalars(
        select(Prospect).join(RunProspect, RunProspect.prospect_id == Prospect.id).where(RunProspect.run_id == run_id)
    ):
        enqueue_once(session, "report", {"prospect_id": prospect.id}, run_id)
        count += 1
    if count:
        run.status, run.finished_at = "running", None
    return count
# Errors that will not go away by retrying.
PERMANENT = (RunValidationError, GooglePlacesError, ValueError, KeyError)


def finalize_run(session: Session, run_id: int) -> None:
    if queue.pending_count(session, run_id):
        return
    failed = session.scalar(select(func.count(Job.id)).where(Job.run_id == run_id, Job.status == "failed"))
    run = session.get(DiscoveryRun, run_id, with_for_update=True)
    if run.status in ("completed", "completed_with_errors", "failed"):
        return
    plan_failed = session.scalar(
        select(func.count(Job.id)).where(Job.run_id == run_id, Job.kind == "plan", Job.status == "failed")
    )
    run.status = "failed" if plan_failed else "completed_with_errors" if failed else "completed"
    run.finished_at = datetime.now(timezone.utc)


def process_job(session: Session, job: Job) -> None:
    try:
        HANDLERS[job.kind](session, job)
        queue.complete(session, job)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded on the job
        session.rollback()
        job = session.get(Job, job.id)
        retryable = isinstance(exc, TransientError) or not isinstance(exc, PERMANENT)
        log.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
        queue.fail(session, job, f"{type(exc).__name__}: {exc}", retryable)
        if job.kind == "plan" and job.status == "failed":
            run = session.get(DiscoveryRun, job.run_id)
            run.error = job.last_error
        session.commit()
    if job.run_id:
        finalize_run(session, job.run_id)
        session.commit()


def run_stats(session: Session, run_id: int) -> dict:
    jobs = dict(
        session.execute(
            select(Job.status, func.count(Job.id)).where(Job.run_id == run_id).group_by(Job.status)
        ).all()
    )
    found = session.scalar(select(func.count()).select_from(RunProspect).where(RunProspect.run_id == run_id))
    new = session.scalar(
        select(func.count()).select_from(RunProspect).where(RunProspect.run_id == run_id, RunProspect.is_new)
    )
    return {"jobs": jobs, "prospects_found": found, "prospects_new": new}
