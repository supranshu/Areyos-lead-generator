# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Areyos lead generator: discovers B2B prospects for indoor wayfinding (12 venue industries, India) from Google Places, OpenStreetMap and venue websites, then stores, dedupes and scores them in PostgreSQL. Python / FastAPI / SQLAlchemy 2 / Alembic, plus a vanilla-JS UI in `areyos/static/`.

## Commands

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/areyos migrate                     # alembic upgrade head
.venv/bin/areyos serve [--reload]            # UI + API on :8000, OpenAPI at /docs
.venv/bin/areyos worker                      # job processor; run N of them to scale
.venv/bin/areyos discover --state Maharashtra --city Mumbai --industry healthcare --wait
.venv/bin/pytest                             # needs Postgres DB areyos_test
.venv/bin/pytest tests/test_units.py::test_extract_contacts
.venv/bin/alembic revision --autogenerate -m "..."   # after changing areyos/models.py
```

All config comes from `AREYOS_*` env vars or `.env` (see `areyos/config.py`, `.env.example`). Tests force `AREYOS_DATABASE_URL` to `areyos_test` (override with `AREYOS_TEST_DATABASE_URL`), migrate it, and truncate all tables before each test.

## Architecture

**Run pipeline (`engine.py`).** `create_run` stores a `DiscoveryRun` and enqueues a `plan` job. `plan` geocodes the city (Google first, then Nominatim, cached in `geo_cache`) and fans out `discover` jobs: one for each industry × Google query × grid tile, plus one OSM job per industry. A `discover` job calls the provider, then `merge_places` upserts the results and links them through `run_prospects`. New prospects that have a website get an `enrich` job. After every job, `finalize_run` marks the run `completed`, `completed_with_errors` or `failed` once no queued or running jobs remain. Run stats are computed from `jobs` and `run_prospects`, never stored, because concurrent workers would overwrite each other.

**Queue (`queue.py`).** This is the `jobs` table, claimed with `FOR UPDATE SKIP LOCKED`. Retries use exponential backoff via `run_after`. Jobs stuck in `running` for more than 15 minutes are requeued. `engine.PERMANENT` lists the exception types that fail a job without retrying; anything else is retried.

**Concurrency rule.** Handlers commit before slow network calls so they don't hold transactions open. Merges run under `pg_advisory_xact_lock(hash(city))`, which keeps parallel workers from creating the same venue twice. Keep both properties when adding job types.

**Dedup (`dedup.py`).** A raw place matches an existing prospect when its `(source, source_id)` is already in `prospect_sources`, or when a prospect lies within about 250 m with a token-similar normalized name. When fields conflict, Google wins and OSM fills the gaps. Size-relevant OSM tags go into `prospect.attributes`. Every upsert calls `rescore`.

**Industry catalog (`catalog/industries.py`).** This is the single place for per-industry behaviour: Google queries, OSM Overpass selectors, Google types and name keywords used for relevance, review and capacity thresholds for size, and `fit_weight`. To add an industry, add an entry there; the UI picks it up from `/api/meta`.

**Scoring (`scoring.py`).** Size is the strongest signal among reviews, capacity tag, footprint, name keyword and IATA code. Priority is fit (30) + size (30) + footfall (15) + reach (15) + operational (10); high is 70 or more, medium is 45 or more. Venue size and priority on a run are result filters only: everything discovered is stored.

**Providers** implement `search(industry_key, area, query)` and return `RawPlace`s. All HTTP goes through `http.PoliteClient`, which applies per-host delay, the configured user agent, and `TransientError` on 429/5xx/network errors. In tests, swap providers with `engine.registry.set(name, fake)`; see `tests/test_engine.py` for fakes covering the whole pipeline.

**Decision-makers (`people/`).** `people` jobs are queued after enrichment when the run has `find_people` and the prospect meets `people_min_priority`. They can also be queued by hand through the API (`force`). `handle_people` collects `PersonCandidate`s from `WebsitePeopleSource`, `SearchPeopleSource` and `HunterPeopleSource` in `people/sources.py`. Then `people/service.merge_candidates` merges them per prospect, under a `prospect:<id>` advisory lock, keyed by `person_key` (honorifics stripped) or LinkedIn URL, and skips anything in `suppressions`. `people/roles.py` maps titles to the 11 roles (first match wins, so functional roles are listed before generic ones) and sets the per-industry relevance. `enqueue_once` keeps a run from queueing the same enrich or people job twice.

**Opportunity scoring (`opportunity/`).** `handle_opportunity` builds D0 from stored data (`evidence.platform_document`). For AI-eligible prospects (`service.ai_eligible`: priority ≥ `opportunity_min_priority`, or forced), it also crawls D1..Dn and calls `extractor.ClaudeExtractor`. That uses the beta Messages API with `fallbacks: "default"` and a strict JSON schema, and `extractor.verify` checks quotes against the documents. `service.combine` keeps one result per factor: verified beats estimated beats unknown, and among equals ai beats data beats heuristic. `factors.score_factor` then applies the fixed rubrics. Points are always computed in code and never taken from the model. Every prospect can be assessed; non-eligible ones get the rules-only version without crawling. Assessments keep page text in `documents` so reports can re-check quotes, and the API strips it.

**Reports (`reports/`).** `handle_report` builds a `ReportContext` from the latest assessment, people and contacts. The draft comes from Claude (`reports/ai.py`) for AI-eligible prospects, otherwise from `report.template_sections`. Both go through `report.verify`, which resolves citations (`D<n>` + quote, `F:<factor>`, `P:<id>` (must be named in the text), `C:<id>`), drops unsupported statements, inserts `gap` statements, and sets `evidence_level`. A report request without an assessment first creates a rules-only one.

**CRM (`crm/service.py`, `api/crm.py`, `static/crm.js`).**
- A prospect *is* the lead. `models.PIPELINE_STAGES` defines the stages in order with their win probabilities.
- Every lead change goes through `crm.service`. `change_status` handles rejection reasons and cancels follow-ups when a lead closes. `advance` only moves forward. `assign` moves open follow-ups with the lead. `update_deal` and `merge` complete the set. Each of these writes an `Activity` row, so the timeline is also the audit log; don't set `Prospect.status` or `owner_id` directly.
- The acting rep comes from the `X-Areyos-Rep` header (`api/crm.acting_rep`). There is no authentication.
- Merged leads keep a row with `merged_into_id` set. `ProspectFilters.apply` hides them, and CRM endpoints return 409 for them.
- The duplicate scan (`duplicate_groups`) only compares leads that share a bucket (website domain, phone, city + first name word, or neighbouring ~250 m cells). Keep it bucketed; it must not become an all-pairs comparison.
- Analytics are SQL aggregates using `FILTER` clauses. Don't group by expressions that contain bound timestamps, because Postgres treats each bind as a different expression.
- The UI is two classic scripts that share globals. `app.js` owns `api()`, which adds the rep header, plus the lead list and detail dialog. `crm.js` adds the tabs, CRM panel, follow-ups, analytics, duplicates and team.

**Enrichment (`enrichment/website.py`, `enrichment/crawler.py`)** fetches only pages that robots.txt allows: the homepage plus up to `enrich_max_pages - 1` same-site contact/about pages. Every `Contact` row keeps its `source_url`.

## Constraints

- Use only legitimate public sources: official APIs, OSM, and the venue's own site where robots.txt allows. Don't add scrapers for sites whose terms forbid it (Google Maps HTML, LinkedIn, Justdial and similar).
- Never fabricate people data. A person's name must come verbatim from a cited source (`names.appears_in`). Contacts attach to a person only when found in that person's own card on the same page. Don't add email-pattern guessing, and don't take contacts from search snippets. Every `Person` must have at least one `PersonSource` carrying a URL and evidence.
- Deleting a person through the API must go through `suppress_person`, so they are never re-added.
- Opportunity scores and reports must stay explainable. Don't let the model set points, mark something verified without a checked quote, or write sales advice into report sections 1–3. Keep "gap" statements code-only.
- Claude calls use `claude-opus-5-5` through `client.beta.messages.create` with `output_config.format` (strict JSON schema), `fallbacks: "default"` and the `server-side-fallback-2026-07-01` beta. Thinking can't be disabled on that model; `opportunity_effort` controls depth. Map 429, 5xx and connection errors to `TransientError` (job retry) and everything else to `AIUnavailable` (rules fallback).
- The Google field mask determines the billing SKU. `google_contact_fields` toggles the expensive fields, so check cost before adding fields to `BASE_FIELDS`.
- Overpass `out` cannot combine `center` with `bb`, and `out tags` drops node coordinates. That's why `build_query` prints nodes with `body` and ways/relations with `tags bb`.
