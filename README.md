# Areyos Lead Generator

Finds B2B prospects for Areyos indoor wayfinding across India: large, complex
venues where visitors get lost. It discovers them across 12 industries, merges
duplicates from different sources, estimates venue size, scores priority, and
collects public business contacts from each venue's own website. Everything is
stored in PostgreSQL and browsable in a web UI, a REST API, or a CSV export.

Industries: hospitals, malls, convention centres, universities, airports and
railway stations, IT parks, hotels, government buildings, event venues,
stadiums, museums, industrial campuses.

## Quick start (Docker)

```bash
cp .env.example .env        # add AREYOS_GOOGLE_PLACES_API_KEY
docker compose up --build --scale worker=2
```

Open http://localhost:8000, pick a state, city, industries, venue sizes and
priority levels, and start a discovery run.

## Quick start (local)

Needs Python 3.11+ and PostgreSQL.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/areyos migrate
.venv/bin/areyos serve          # UI + API on :8000
.venv/bin/areyos worker         # in another terminal; start more for more throughput
```

From the command line, without the UI:

```bash
.venv/bin/areyos discover --state Maharashtra --city Mumbai --industry healthcare --industry retail --wait
```

## Data sources

| Source | Used for | Needs |
| --- | --- | --- |
| Google Places API (New), Text Search | Main venue discovery, ratings, review counts, website and phone | `AREYOS_GOOGLE_PLACES_API_KEY` |
| OpenStreetMap Overpass | Second venue source; bed, room and capacity tags; building footprints | nothing (public, rate-limited) |
| OpenStreetMap Nominatim | Geocoding cities when no Google key is set | nothing |
| Venue websites | Public business emails, phones, LinkedIn and contact pages | robots.txt must allow it |

**Google cost:** each run makes `industries × queries per industry (3–4) ×
grid tiles (GRID²) × up to 3 pages` Text Search requests. With the defaults
(grid 2), all 12 industries in one city is about 160–500 requests. Set
`AREYOS_GOOGLE_CONTACT_FIELDS=false` to use the cheaper field set, or reduce
`AREYOS_GOOGLE_GRID_SIZE` for small cities.

## Decision-makers

Tick **Identify decision-makers** when you start a run, use **Find people** on
an earlier run, or use **Find decision-makers** on a single venue. The engine
looks for 11 roles: CEO, Managing Director, General Manager, Facility Manager,
Operations Director, Venue Manager, Hospital Administrator, Property Manager,
Infrastructure Head, Procurement Manager and Marketing Director. Common
variants are recognised (for example "Medical Superintendent" or "Head –
Facilities"); junior and former titles are ignored.

| Source | What it gives | Match confidence |
| --- | --- | --- |
| The venue's own website (leadership, team, board, about and contact pages, plus schema.org `Person` data) | Name, title, and any email, phone or LinkedIn link in the same profile card | 0.90 |
| A search API (`brave` or `google_cse`) run against `site:linkedin.com/in "<venue>"` | Name, title and LinkedIn URL from the result's title and snippet. linkedin.com itself is never fetched, and snippets never supply contact details. | 0.55–0.90, depending on how strongly the result names the venue |
| Hunter.io domain search (optional) | Business emails that Hunter found on public pages, with the page URL | 0.70–0.80 |

**No fabrication:**
- Every name must appear word-for-word in its source.
- A contact is attached to a person only if it sits in that person's own card on the same page.
- Email addresses are never guessed from patterns.
- Every person keeps each source URL with a text excerpt as evidence.

**Matching:**
- Search hits must name the venue through its distinctive words. For example, "Nanavati Hospital" does not match "Lilavati Hospital".
- Venues with generic names, such as "City Hospital", need the exact phrase.
- Results below `AREYOS_PEOPLE_MIN_CONFIDENCE` are dropped.
- When several sources agree, confidence goes up.
- Each person also gets a 0–100 *relevance* score for the Areyos sale. Facility and hospital-admin roles rank highest, with adjustments per industry.

**Do not contact** deletes the person and adds their name, LinkedIn URL and
emails to a suppression list, so later runs never store them again. Use it for
opt-outs and erasure requests.

## Opportunity scoring

Each venue gets a 0–100 score for how much it needs indoor navigation, built
from nine weighted factors:

| Factor | Weight | | Factor | Weight |
| --- | --- | --- | --- | --- |
| Daily visitor footfall | 16 | | Number of buildings | 10 |
| Building size | 14 | | Expansion or renovation activity | 10 |
| Visitor navigation complexity | 14 | | Existing wayfinding infrastructure | 8 |
| Number of floors | 10 | | Identifiable decision-makers | 8 |
| Departments or sections | 10 | | | |

Scores of **65 or more are high**, **40–64 medium**, and **below 40 low**.

**How a score is produced:**
1. Evidence is gathered: the platform's own data (Google review counts, OSM
   tags) as document D0, plus up to 6 pages of the venue's website that
   robots.txt allows (about, facilities, departments, campus, visitor info,
   news and projects).
2. Claude (`claude-opus-5-5`) reports what the documents say about each
   factor. Each answer is marked **verified** (stated in a source and
   quoted), **estimated** (reasoned from evidence, with the reasoning
   shown) or **unknown**.
3. Code checks every quote against its document. A "verified" claim whose
   quote isn't there, or a numeric one whose quote contains no number, is
   downgraded to an estimate, and a note records why.
4. Code computes the points from fixed rubrics, so the same facts always give
   the same score:
   - verified: the full rubric value
   - estimated: the rubric value × 0.85
   - unknown: a neutral 30% of the factor's weight
5. Decision-makers are scored from the decision-makers table, never by
   the model.

Each assessment stores:
- the explanation for every factor: finding, evidence quotes with URLs, reasoning and points arithmetic
- a summary built only from those findings
- a confidence figure: the share of the score's weight that rests on verified sources

Without Anthropic credentials, or for venues below
`AREYOS_OPPORTUNITY_MIN_PRIORITY`, a free rules-only version runs instead. It
uses industry benchmarks for the venue's size class, and every value is
labelled an estimate.

## Opportunity reports

Each prospect can get a report answering seven questions:
1. What makes the venue difficult to navigate?
2. Why might visitors need indoor navigation?
3. What is the likely business problem?
4. Who is responsible for solving it?
5. What would be the strongest pitch?
6. What objections might they raise?
7. How should we request a demo?

Every statement is tagged **Evidence**, **Inference**, **Suggestion** or
**Unknown**, and cites either a quoted source document, an assessment factor,
a decision-maker, or a venue contact, with URLs.

**How invented problems are prevented:**
- Code resolves every citation. A quote must appear in its document. A
  decision-maker citation only counts if the statement names that person.
  Unknown factors can't be cited.
- Statements without the support their kind needs are removed and listed in
  the report's notes.
- Sections 1–3 can't contain sales advice. If nothing supported is left,
  they say the sources don't establish it.
- When no verified fact supports sections 1–3, the report is marked
  `insufficient` and the pitch opens with an instruction to qualify the need
  rather than claim it.

Medium and high priority venues get a Claude-drafted report. The rest get a
rules-based report built from their assessment, so every discovered prospect
can have one. Export one report (`/api/prospects/{id}/report.md`) or all
matching reports in ranking order (`/api/reports.md`) as Markdown.

## Sales CRM

The **Leads** tab is the pipeline. Pick yourself under **Acting as** (top
right) so your actions are attributed to you. Every request then carries an
`X-Areyos-Rep` header.

**Pipeline stages and win probabilities** (used to weight deal values):

| Stage | Win probability | | Stage | Win probability |
| --- | --- | --- | --- | --- |
| New | 0% | | Demo scheduled | 35% |
| Researching | 2% | | Negotiation | 50% |
| Qualified | 10% | | Pilot proposed | 60% |
| Contacted | 15% | | Pilot confirmed | 80% |
| Meeting scheduled | 25% | | Converted | 100% |
| | | | Rejected | 0% (needs a reason) |

Every stage change is logged. Logging activity moves a lead *forward* only,
never backwards:
- a connected call or a sent email moves it to Contacted
- a held meeting moves it to Meeting scheduled
- a held demo, or a booked demo follow-up, moves it to Demo scheduled

Closing a lead (Converted or Rejected) cancels its open follow-ups.

**Features:**
- **Lead assignment.** Assign leads one by one, in bulk, or with
  **Auto-assign**. Auto-assign gives each unassigned open lead to the
  least-loaded active rep who specialises in its industry, then to
  generalists. Reassigning a lead moves its open follow-ups with it.
- **Rep tracking.** The **Team** tab holds your reps and their industries.
  **Analytics** shows a scorecard per rep: open and converted leads; calls,
  meetings and emails in the last 7, 30 or 90 days; email reply rate; overdue
  follow-ups; weighted pipeline; and won value.
- **Call history and meeting notes.** Each lead has one timeline:
  - calls: outcome, direction, duration, contact person, notes
  - meetings: outcome, attendees, notes, and whether it was a demo
  - notes
  - automatic entries for stage, owner, deal and merge changes
- **Follow-ups.** Each has a type (call, email, meeting, demo or task), a due
  date, an assignee and a note. The **Follow-ups** tab shows yours or
  everyone's, with overdue items flagged and counted on the tab badge.
- **Email tracking.** Log sent emails and mark them replied or bounced. An
  optional open-tracking pixel URL (`/api/t/<token>.gif`) can be pasted into
  the email. Opens are only indicative: image blocking hides some, and mail
  privacy proxies inflate others.
- **Deal value.** In ₹, with an expected close date; weighted by stage probability in forecasts.
- **Industry analytics.** A pipeline funnel, plus per-industry leads, engaged,
  converted, win rate, open, weighted and won pipeline, and average
  opportunity score.
- **CSV export.** Leads (`/api/prospects.csv`, now including owner, deal,
  next follow-up and last activity), activities (`/api/activities.csv`) and
  follow-ups (`/api/follow-ups.csv`).
- **Duplicate detection.** Leads added by hand are checked first: a likely
  duplicate returns 409 with the matches unless `force=true`. The
  **Duplicates** tab lists pairs that share a website domain or phone number,
  have the same name in the same city, or have a similar name within about
  250 m. **Merge** moves activities, follow-ups, people, contacts,
  assessments and reports into the kept lead, fills its gaps, and hides the
  duplicate.

**Security:** the app has no login. The "Acting as" choice is trust-based. Put
the app behind your company SSO or a reverse proxy before anyone outside a
trusted network can reach it.

## How prospects are scored

**Venue size** (small / medium / large / mega / unknown) takes the strongest of
these signals: Google review count against industry-specific thresholds; OSM
`beds`, `rooms` or `capacity`; OSM footprint area; an IATA code; and name
keywords such as "multi speciality" or "tech park".

These are the *lead score* used to filter what's worth deeper work. The
opportunity score above is the main ranking.

**Lead score** (0–100) is made up of industry fit (0–30), venue size
(0–30), footfall from review volume (0–15), reachability from website, phone
and email (0–15), and operating status (0–10). A score of 70 or more is
**high**, 45 or more is **medium**, and the rest are **low**. Permanently
closed venues score 0.

## API

Interactive docs are at `/docs`. The main endpoints are:

- `GET /api/meta`: states, cities, industries, sizes, priorities, configured providers
- `POST /api/runs`: `{state, city, industries[], venue_sizes[], priority_levels[], providers[], find_people, assess_opportunity, generate_reports}`
- `GET /api/runs`, `GET /api/runs/{id}`
- `GET /api/prospects`: filters `state, city, industry, size, priority, status, run_id, q, has_email, min_score, sort`
- `GET /api/prospects.csv`: same filters
- `GET|PATCH /api/prospects/{id}`: details, then update status and notes
- `POST /api/prospects/{id}/enrich`: check the website again
- `POST /api/prospects/{id}/people`, `POST /api/runs/{id}/people`: find decision-makers
- `GET /api/people`: filters `role, industry, state, city, prospect_id, run_id, status, min_confidence, has_contact, q`
- `GET /api/people.csv`: same filters
- `PATCH /api/people/{id}`: set status (`verified` or `rejected`) and notes
- `DELETE /api/people/{id}?reason=...`: erase and suppress (do not contact)
- `POST /api/prospects/{id}/opportunity`, `POST /api/runs/{id}/opportunity`: score opportunity; `GET /api/prospects/{id}/opportunity` lists the history
- `GET /api/prospects?sort=opportunity&opportunity=high&min_opportunity=...`: ranking
- `POST /api/prospects/{id}/report`, `POST /api/runs/{id}/reports`: write reports
- `GET /api/prospects/{id}/report.md`, `GET /api/reports.md?<prospect filters>`: Markdown export
- CRM (send `X-Areyos-Rep: <rep id>` to attribute actions):
  - `GET|POST /api/reps`, `PATCH /api/reps/{id}`, `GET /api/reps/scorecards?days=30`
  - `POST /api/leads` (manual lead, duplicate check), `PATCH /api/leads/{id}` (stage, rejection reason, owner, deal value, expected close, notes)
  - `POST /api/leads/assign`, `POST /api/leads/auto-assign`
  - `GET /api/leads/{id}/activities`; `POST /api/leads/{id}/calls | meetings | emails | notes | follow-ups`
  - `PATCH /api/activities/{id}/email-status`, `GET /api/follow-ups`, `PATCH /api/follow-ups/{id}`
  - `GET /api/duplicates`, `POST /api/duplicates/merge`, `GET /api/leads/{id}/duplicates`
  - `GET /api/analytics/funnel`, `GET /api/analytics/industries`
  - `GET /api/activities.csv`, `GET /api/follow-ups.csv`
  - `GET /api/prospects` also takes `owner_id`, `unassigned` and `sort=deal|activity`

## Tests

```bash
createdb areyos_test   # once
.venv/bin/pytest
```

## Responsible use

- The platform collects only business contact details that venues publish on
  their own sites. Each contact keeps the URL it came from.
- Decision-maker records hold professional details only (name, title,
  employer, public profile URL, and business contacts published next to them),
  each with its source. Mark people as verified before outreach, and act on
  opt-outs with **Do not contact**.
- Venue websites are crawled only where robots.txt allows. Requests are
  rate-limited per host and identify the bot with a user agent you configure.
- Follow Google Places' terms on caching and display, and the OSM usage
  policies and ODbL attribution.
- Before you email anyone, check that your outreach complies with India's
  DPDP Act 2023 and with anti-spam rules.
