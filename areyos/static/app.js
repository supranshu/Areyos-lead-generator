const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
let META = null;
let page = 1;
const PAGE_SIZE = 50;

function actingRep() {
  try { return localStorage.getItem("areyos.rep") || ""; } catch { return ""; }
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (actingRep()) headers["X-Areyos-Rep"] = actingRep();
  const res = await fetch(path, { headers, ...opts });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : res.statusText);
  return body;
}

function checkboxes(container, name, items, checked = () => false) {
  $(container).innerHTML = items
    .map((i) => `<label><input type="checkbox" name="${name}" value="${esc(i.key)}" ${checked(i) ? "checked" : ""}> ${esc(i.label)}</label>`)
    .join("");
}

function options(select, items) {
  select.insertAdjacentHTML("beforeend", items.map((i) => `<option value="${esc(i.key)}">${esc(i.label)}</option>`).join(""));
}

const industryLabel = (key) => META.industries.find((i) => i.key === key)?.label ?? key;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

async function init() {
  META = await api("/api/meta");
  const states = Object.keys(META.states).map((s) => ({ key: s, label: s }));
  options($("#state"), states);
  options($("#f-state"), states);
  $("#state").value = "Maharashtra";
  fillCities();
  $("#city").value = "Mumbai";

  checkboxes("#industries", "industries", META.industries, () => true);
  checkboxes("#venue_sizes", "venue_sizes", META.venue_sizes.map((k) => ({ key: k, label: cap(k) })));
  checkboxes("#priority_levels", "priority_levels", META.priority_levels.map((k) => ({ key: k, label: cap(k) })));
  checkboxes("#providers", "providers", META.providers.map((k) => ({ key: k, label: k === "osm" ? "OpenStreetMap" : "Google Places" })), () => true);
  if (!META.providers.length) $("#providers").textContent = "No source configured: set AREYOS_GOOGLE_PLACES_API_KEY or enable OSM.";

  options($("#f-industry"), META.industries);
  options($("#f-size"), META.venue_sizes.map((k) => ({ key: k, label: cap(k) })));
  options($("#f-priority"), META.priority_levels.map((k) => ({ key: k, label: cap(k) })));
  options($("#f-status"), META.statuses.map((k) => ({ key: k, label: stageLabel(k) })));
  options($("#p-role"), META.roles);
  options($("#p-industry"), META.industries);
  $("#people-sources").textContent = META.people_sources.length ? `(sources: ${META.people_sources.join(", ")})` : "(disabled)";
  if (!META.people_sources.length) { $("#find_people").checked = false; $("#find_people").disabled = true; }
  $("#people-filters").addEventListener("input", () => { peoplePage = 1; loadPeople(); });
  $("#p-prev").addEventListener("click", () => { if (peoplePage > 1) { peoplePage--; loadPeople(); } });
  $("#p-next").addEventListener("click", () => { peoplePage++; loadPeople(); });

  $("#state").addEventListener("change", () => { fillCities(); $("#city").value = ""; });
  $("[data-toggle=industries]").addEventListener("click", () => {
    const boxes = [...document.querySelectorAll("#industries input")];
    const all = boxes.every((b) => b.checked);
    boxes.forEach((b) => (b.checked = !all));
  });
  $("#run-form").addEventListener("submit", startRun);
  $("#filters").addEventListener("input", () => { page = 1; loadProspects(); });
  $("#prev").addEventListener("click", () => { if (page > 1) { page--; loadProspects(); } });
  $("#next").addEventListener("click", () => { page++; loadProspects(); });
  $("#clear-run").addEventListener("click", () => showRun(""));

  loadRuns();
  loadProspects();
  loadPeople();
  setInterval(() => { loadRuns(); loadPeople(); }, 5000);
}

function fillCities() {
  $("#city-list").innerHTML = (META.states[$("#state").value] || []).map((c) => `<option value="${esc(c)}">`).join("");
}

const checkedValues = (form, name) => [...form.querySelectorAll(`input[name=${name}]:checked`)].map((i) => i.value);

async function startRun(e) {
  e.preventDefault();
  const f = e.target;
  const body = {
    state: f.state.value,
    city: f.city.value.trim(),
    industries: checkedValues(f, "industries"),
    venue_sizes: checkedValues(f, "venue_sizes"),
    priority_levels: checkedValues(f, "priority_levels"),
    providers: checkedValues(f, "providers"),
    find_people: f.find_people.checked,
    assess_opportunity: f.assess_opportunity.checked,
    generate_reports: f.generate_reports.checked,
  };
  if (!body.industries.length) return ($("#form-msg").textContent = "Pick at least one industry.");
  $("#form-msg").textContent = "Starting…";
  try {
    const run = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    $("#form-msg").textContent = `Run #${run.id} queued.`;
    loadRuns();
    showRun(run);
  } catch (err) {
    $("#form-msg").textContent = `Could not start: ${err.message}`;
  }
}

let RUNS = [];
async function loadRuns() {
  RUNS = await api("/api/runs?limit=10");
  $("#runs").innerHTML = RUNS.map((r) => {
    const jobs = r.stats.jobs || {};
    const done = (jobs.done || 0) + (jobs.failed || 0);
    const total = Object.values(jobs).reduce((a, b) => a + b, 0);
    return `<tr>
      <td>${r.id}</td><td>${esc(r.city)}, ${esc(r.state)}</td>
      <td class="muted">${r.industries.length === META.industries.length ? "All" : r.industries.map(industryLabel).map(esc).join(", ")}</td>
      <td>${esc(r.status)}${r.error ? `<div class="muted">${esc(r.error)}</div>` : ""}</td>
      <td>${r.stats.prospects_found ?? 0}</td><td>${r.stats.prospects_new ?? 0}</td>
      <td class="muted">${done}/${total}${jobs.failed ? ` (${jobs.failed} failed)` : ""}</td>
      <td><button data-run="${r.id}">View</button>${!r.generate_reports ? ` <button data-report-run="${r.id}">Write reports</button>` : ""}${!r.assess_opportunity ? ` <button data-opp-run="${r.id}">Score opportunity</button>` : ""}${!r.find_people && META.people_sources.length ? ` <button data-people-run="${r.id}">Find people</button>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No runs yet.</td></tr>`;
  document.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", () => showRun(RUNS.find((r) => r.id == b.dataset.run))));
  document.querySelectorAll("[data-report-run]").forEach((b) => b.addEventListener("click", async () => {
    const res = await api(`/api/runs/${b.dataset.reportRun}/reports`, { method: "POST" });
    b.replaceWith(document.createTextNode(`${res.queued} reports queued`));
  }));
  document.querySelectorAll("[data-opp-run]").forEach((b) => b.addEventListener("click", async () => {
    const res = await api(`/api/runs/${b.dataset.oppRun}/opportunity`, { method: "POST" });
    b.replaceWith(document.createTextNode(`${res.queued} venues queued for scoring`));
  }));
  document.querySelectorAll("[data-people-run]").forEach((b) => b.addEventListener("click", async () => {
    const res = await api(`/api/runs/${b.dataset.peopleRun}/people`, { method: "POST" });
    b.replaceWith(document.createTextNode(`${res.queued} venues queued`));
  }));
}

function showRun(run) {
  const f = $("#filters");
  f.run_id.value = run ? run.id : "";
  $("#people-filters").run_id.value = run ? run.id : "";
  peoplePage = 1;
  loadPeople();
  $("#clear-run").hidden = !run;
  // A run's size and priority selections become the result filters.
  f.size.value = run?.venue_sizes?.length === 1 ? run.venue_sizes[0] : "";
  f.priority.value = run?.priority_levels?.length === 1 ? run.priority_levels[0] : "";
  window.__runFilters = run ? { size: run.venue_sizes, priority: run.priority_levels } : null;
  page = 1;
  loadProspects();
}

function filterParams() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData($("#filters"))) {
    if (!v) continue;
    if (k === "owner") {
      if (v === "none") params.append("unassigned", "true");
      else if (v === "me" && actingRep()) params.append("owner_id", actingRep());
      else if (v !== "me") params.append("owner_id", v);
    } else params.append(k, v);
  }
  const rf = window.__runFilters;
  if (rf) {
    if (!params.has("size")) rf.size.forEach((s) => params.append("size", s));
    if (!params.has("priority")) rf.priority.forEach((s) => params.append("priority", s));
  }
  return params;
}

async function loadProspects() {
  const params = filterParams();
  $("#export").href = `/api/prospects.csv?${params}`;
  $("#export-reports").href = `/api/reports.md?${params}`;
  params.set("page", page);
  params.set("page_size", PAGE_SIZE);
  const data = await api(`/api/prospects?${params}`);
  $("#total").textContent = `${data.total} matching`;
  $("#page").textContent = `Page ${data.page} of ${Math.max(1, Math.ceil(data.total / PAGE_SIZE))}`;
  $("#prev").disabled = page <= 1;
  $("#next").disabled = page * PAGE_SIZE >= data.total;
  $("#prospects").innerHTML = data.items.map((p) => `
    <tr class="clickable" data-id="${p.id}">
      <td><input type="checkbox" class="lead-pick" value="${p.id}" aria-label="Select ${esc(p.name)}"></td>
      <td>${p.opportunity_level ? `<span class="pill ${p.opportunity_level}">${p.opportunity_score} ${cap(p.opportunity_level)}</span><div class="muted">${pct(p.opportunity_confidence)} verified</div>` : `<span class="muted">not scored</span>`}</td>
      <td><strong>${esc(p.name)}</strong><div class="muted">${esc(p.address || "")}</div></td>
      <td>${esc(industryLabel(p.industry))}</td>
      <td>${esc(p.city || "")}</td>
      <td>${esc(stageLabel(p.status))}</td>
      <td>${p.owner_id ? esc(repName(p.owner_id)) : `<span class="muted">unassigned</span>`}</td>
      <td class="nowrap">${p.deal_value != null ? inr(p.deal_value) : ""}</td>
      <td>${p.next_follow_up_at ? `<span class="${new Date(p.next_follow_up_at) < new Date() ? "overdue" : ""}">${fmtDate(p.next_follow_up_at)}</span>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="muted">No leads match.</td></tr>`;
  document.querySelectorAll("#prospects tr[data-id]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest(".lead-pick")) return;
    openDetail(tr.dataset.id);
  }));
  if (window.onLeadsRendered) window.onLeadsRendered();
}

async function openDetail(id) {
  const p = await api(`/api/prospects/${id}`);
  const contacts = p.contacts.map((c) => `<li>${esc(c.kind)}: ${c.kind === "email" ? `<a href="mailto:${esc(c.value)}">${esc(c.value)}</a>` : esc(c.value)} <a class="muted" href="${esc(c.source_url)}" target="_blank" rel="noopener">source</a></li>`).join("");
  const b = p.score_breakdown;
  $("#detail-body").innerHTML = `
    <h2>${esc(p.name)} ${p.opportunity_level ? `<span class="pill ${p.opportunity_level}" title="Opportunity score">Opportunity ${p.opportunity_score}</span>` : ""} <span class="pill ${p.priority_level}" title="Lead score">Lead score ${p.priority_score}</span></h2>
    <div id="crm-panel"></div>
    <dl>
      <dt>Industry</dt><dd>${esc(industryLabel(p.industry))}</dd>
      <dt>Address</dt><dd>${esc(p.address || "")}</dd>
      <dt>Venue size</dt><dd>${cap(p.size_class)} <span class="muted">${esc(Object.entries(p.size_signals).map(([k, v]) => `${k}: ${v}`).join(", "))}</span></dd>
      <dt>Score</dt><dd class="muted">fit ${b.industry_fit} · size ${b.venue_size} · footfall ${b.footfall} · reach ${b.reachability} · operational ${b.operational}</dd>
      <dt>Website</dt><dd>${p.website ? `<a href="${esc(p.website)}" target="_blank" rel="noopener">${esc(p.website)}</a>` : "—"}</dd>
      <dt>Phone</dt><dd>${esc(p.phone || "—")}</dd>
      <dt>Reviews</dt><dd>${p.rating ?? "—"} ★ (${p.rating_count ?? 0})</dd>
      <dt>Map</dt><dd>${p.google_maps_url ? `<a href="${esc(p.google_maps_url)}" target="_blank" rel="noopener">open</a>` : "—"}</dd>
      <dt>Sources</dt><dd>${esc(p.sources.join(", "))}</dd>
      <dt>Public contacts</dt><dd>${contacts ? `<ul>${contacts}</ul>` : `<span class="muted">${p.enriched_at ? "none found on website" : "not checked yet"}</span>`}</dd>
    </dl>
    ${p.website ? `<button id="d-enrich">Re-check website</button>` : ""} <span id="d-msg"></span>
    ${reportSection(p)}
    ${opportunitySection(p)}
    ${peopleSection(p)}`;
  bindPeopleSection(id);
  if ($("#d-report")) $("#d-report").onclick = async () => {
    await api(`/api/prospects/${id}/report`, { method: "POST" });
    $("#d-report").replaceWith(document.createTextNode("Queued; a worker will write the report."));
  };
  if ($("#d-opp")) $("#d-opp").onclick = async () => {
    await api(`/api/prospects/${id}/opportunity`, { method: "POST" });
    $("#d-opp").replaceWith(document.createTextNode("Queued; a worker will gather evidence and score it."));
  };
  if (window.renderCrmPanel) window.renderCrmPanel(p);
  if ($("#d-enrich")) $("#d-enrich").onclick = async () => {
    await api(`/api/prospects/${id}/enrich`, { method: "POST" });
    $("#d-msg").textContent = "Queued; a worker will pick it up.";
  };
  if (!$("#detail").open) $("#detail").showModal();
}

init().catch((err) => { document.body.insertAdjacentHTML("afterbegin", `<p style="color:red;padding:16px">${esc(err.message)}</p>`); });

const roleLabel = (key) => META.roles.find((r) => r.key === key)?.label ?? key;
const pct = (x) => `${Math.round(x * 100)}%`;
let peoplePage = 1;

function contactLinks(person) {
  return person.contacts.map((c) => c.kind === "email"
    ? `<a href="mailto:${esc(c.value)}">${esc(c.value)}</a>`
    : esc(c.value)).join("<br>");
}

function sourceLinks(person) {
  return person.sources.map((s) => `<a href="${esc(s.source_url)}" target="_blank" rel="noopener" title="${esc(s.evidence)}">${esc(s.source)}</a>`).join(" · ");
}

async function loadPeople() {
  if (!META) return;
  const params = new URLSearchParams();
  for (const [k, v] of new FormData($("#people-filters"))) if (v) params.append(k, v);
  $("#people-export").href = `/api/people.csv?${params}`;
  params.set("page", peoplePage);
  params.set("page_size", PAGE_SIZE);
  const data = await api(`/api/people?${params}`);
  $("#people-total").textContent = `${data.total} found`;
  $("#p-page").textContent = `Page ${data.page} of ${Math.max(1, Math.ceil(data.total / PAGE_SIZE))}`;
  $("#p-prev").disabled = peoplePage <= 1;
  $("#p-next").disabled = peoplePage * PAGE_SIZE >= data.total;
  $("#people").innerHTML = data.items.map((x) => `<tr>
      <td><strong>${esc(x.full_name)}</strong><div class="muted">${esc(x.designation)}</div></td>
      <td>${esc(roleLabel(x.role))}</td>
      <td><a href="#" data-prospect="${x.prospect_id}">${esc(x.prospect_name)}</a><div class="muted">${esc(x.prospect_city || "")}</div></td>
      <td>${x.linkedin_url ? `<a href="${esc(x.linkedin_url)}" target="_blank" rel="noopener">profile</a>` : ""}</td>
      <td class="muted">${contactLinks(x)}</td>
      <td>${pct(x.match_confidence)}${x.status !== "unverified" ? `<div class="muted">${esc(x.status)}</div>` : ""}</td>
      <td class="muted">${sourceLinks(x)}</td></tr>`).join("") ||
    `<tr><td colspan="7" class="muted">No decision-makers yet. Start a run with "Identify decision-makers" ticked, or use "Find people" on a run.</td></tr>`;
  document.querySelectorAll("[data-prospect]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openDetail(a.dataset.prospect); }));
}

function peopleSection(p) {
  const rows = p.people.map((x) => `<li>
      <strong>${esc(x.full_name)}</strong> · ${esc(x.designation)} <span class="muted">(${esc(roleLabel(x.role))}, ${pct(x.match_confidence)} match)</span>
      <div>${x.linkedin_url ? `<a href="${esc(x.linkedin_url)}" target="_blank" rel="noopener">LinkedIn</a> ` : ""}${contactLinks(x)}</div>
      <div class="muted">Source: ${sourceLinks(x)}</div>
      <div><select data-person-status="${x.id}">${["unverified", "verified", "rejected"].map((s) => `<option ${s === x.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <button class="danger" data-person-delete="${x.id}">Do not contact</button></div>
    </li>`).join("");
  const state = p.people_checked_at ? "No decision-makers found in public sources." : "Not searched yet.";
  return `<h3>Decision-makers</h3>${rows ? `<ul class="people-list">${rows}</ul>` : `<p class="muted">${state}</p>`}
    ${META.people_sources.length ? `<button id="d-people">${p.people_checked_at ? "Search again" : "Find decision-makers"}</button>` : ""}`;
}

function bindPeopleSection(id) {
  document.querySelectorAll("[data-person-status]").forEach((sel) => sel.addEventListener("change", async () => {
    await api(`/api/people/${sel.dataset.personStatus}`, { method: "PATCH", body: JSON.stringify({ status: sel.value }) });
    loadPeople();
  }));
  document.querySelectorAll("[data-person-delete]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this person and never store them again?")) return;
    await fetch(`/api/people/${b.dataset.personDelete}?reason=do-not-contact`, { method: "DELETE" });
    openDetail(id);
    loadPeople();
  }));
  if ($("#d-people")) $("#d-people").onclick = async () => {
    await api(`/api/prospects/${id}/people`, { method: "POST" });
    $("#d-people").replaceWith(document.createTextNode("Queued; a worker will search public sources."));
  };
}

const BASIS_LABEL = { verified: "Verified", estimated: "Estimate", unknown: "Unknown" };

function opportunitySection(p) {
  const a = p.opportunity;
  const button = `<button id="d-opp">${a ? "Re-score" : "Score opportunity"}</button>`;
  if (!a) return `<h3>Opportunity</h3><p class="muted">Not scored yet.</p>${button}`;
  const rows = a.factors.map((f) => `<tr>
      <td><strong>${esc(f.label)}</strong><div><span class="basis ${f.basis}">${BASIS_LABEL[f.basis]}</span></div></td>
      <td>${esc(f.finding || "—")}
        ${f.evidence.map((e) => `<blockquote>“${esc(e.quote)}” — ${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source)}</a>` : esc(e.source)}</blockquote>`).join("")}
        ${f.reasoning ? `<div class="muted">${f.basis === "estimated" && !f.notes.length ? "How estimated: " : "Reasoning: "}${esc(f.reasoning)}</div>` : ""}
        ${f.notes.length ? `<div class="muted">⚠ ${f.notes.map(esc).join(" ")}</div>` : ""}</td>
      <td>${f.points}/${f.max_points}<div class="bar"><span style="width:${(100 * f.points) / f.max_points}%"></span></div>
        <div class="muted">${esc(f.points_explained)}</div></td></tr>`).join("");
  return `<h3>Opportunity <span class="pill ${a.level}">${a.score} ${cap(a.level)}</span></h3>
    <p class="summary">${esc(a.summary)}</p>
    <p class="muted">${pct(a.confidence)} of the score's weight rests on verified sources. Method: ${a.method === "ai" ? `AI reading of ${a.documents.length} public documents (${esc(a.model)}), checked against the sources` : "platform data and industry rules only"}. Assessed ${new Date(a.created_at).toLocaleString()}.${a.note ? ` ${esc(a.note)}` : ""}</p>
    <div class="table-wrap"><table class="factors"><thead><tr><th>Factor</th><th>Finding and evidence</th><th>Points</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${button}`;
}

const KIND_LABEL = { evidence: "Evidence", inference: "Inference", suggestion: "Suggestion", gap: "Unknown" };
const SECTION_QUESTIONS = [
  ["navigation_difficulty", "What makes this venue difficult to navigate?"],
  ["visitor_need", "Why might visitors need indoor navigation?"],
  ["business_problem", "What is the likely business problem?"],
  ["responsible", "Who is responsible for solving that problem?"],
  ["pitch", "What would be the strongest Areyos sales pitch?"],
  ["objections", "What objections might the decision-maker raise?"],
  ["demo_approach", "What would be the best approach for requesting a demo?"],
];

function citationLinks(st) {
  return st.citations.map((c) => {
    const label = c.ref.startsWith("D") ? "source" : c.ref.startsWith("F:") ? c.source : c.source;
    const title = esc(c.quote || c.source);
    return c.url ? `<a class="cite" href="${esc(c.url)}" target="_blank" rel="noopener" title="${title}">[${esc(label)}]</a>`
                 : `<span class="cite muted" title="${title}">[${esc(label)}]</span>`;
  }).join("");
}

function reportSection(p) {
  const r = p.report;
  const button = `<button id="d-report">${r ? "Rewrite report" : "Write report"}</button>`;
  if (!r) return `<h3>Opportunity report</h3><p class="muted">No report yet.</p>${button}`;
  const sections = SECTION_QUESTIONS.map(([key, q]) => `<h4>${esc(q)}</h4><ul>${(r.sections[key] || []).map((st) => `
      <li><span class="kind ${st.kind}">${KIND_LABEL[st.kind]}</span>${esc(st.text)}${citationLinks(st)}
        ${st.citations.filter((c) => c.ref.startsWith("D") && c.quote).map((c) => `<blockquote>“${esc(c.quote)}”</blockquote>`).join("")}</li>`).join("") || `<li class="muted">Nothing to add.</li>`}</ul>`).join("");
  return `<div class="report"><h3>Opportunity report <span class="muted">· evidence ${esc(r.evidence_level)}</span></h3>
    ${r.evidence_level === "insufficient" ? `<p class="warn">Public sources don't show a navigation problem yet. Qualify the need before pitching.</p>` : ""}
    <p class="muted">${r.method === "ai" ? `Drafted by ${esc(r.model)} and checked against the sources` : "Built from the assessment's findings"}. ${r.notes.length ? `${r.notes.length} unsupported claim(s) or citation(s) were removed.` : ""} ${esc(r.note || "")}
      <a href="/api/prospects/${p.id}/report.md">Download Markdown</a></p>
    ${sections}
    ${r.sources.length ? `<h4>Sources</h4><ol>${r.sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a> <span class="muted">${esc(s.source)}</span></li>`).join("")}</ol>` : ""}
    ${button}</div>`;
}

const stageLabel = (s) => cap(s.replace(/_/g, " "));
const inr = (v) => Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const fmtDate = (d) => new Date(d).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
let REPS = [];
const repName = (id) => REPS.find((r) => r.id === id)?.name ?? `#${id}`;
