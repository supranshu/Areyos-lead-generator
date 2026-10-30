// Sales CRM: acting rep, tabs, bulk assignment, lead panel, follow-ups, analytics, duplicates, team.
// Relies on helpers from app.js: $, esc, api, cap, META, REPS, repName, stageLabel, inr, fmtDate, industryLabel, loadProspects, openDetail.

const ACTIVITY_LABEL = { call: "Call", meeting: "Meeting", email: "Email", note: "Note", status_change: "Stage",
  assignment: "Assigned", deal_update: "Deal", merge: "Merge" };
const SYSTEM_KINDS = new Set(["status_change", "assignment", "deal_update", "merge"]);

function setActingRep(id) {
  try { id ? localStorage.setItem("areyos.rep", id) : localStorage.removeItem("areyos.rep"); } catch { /* private mode */ }
}

async function loadReps() {
  REPS = await api("/api/reps");
  const active = REPS.filter((r) => r.active);
  const opts = active.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
  $("#acting-rep").innerHTML = `<option value="">No rep selected</option>${opts}`;
  $("#acting-rep").value = active.some((r) => String(r.id) === actingRep()) ? actingRep() : "";
  $("#bulk-rep").innerHTML = `<option value="">Assign selected to…</option><option value="none">Nobody (unassign)</option>${opts}`;
  const owner = $("#f-owner");
  const keep = owner.value;
  owner.innerHTML = `<option value="">Any owner</option><option value="me">My leads</option><option value="none">Unassigned</option>` +
    REPS.map((r) => `<option value="${r.id}">${esc(r.name)}${r.active ? "" : " (inactive)"}</option>`).join("");
  owner.value = keep;
  $("#reps").innerHTML = REPS.map((r) => `<tr>
      <td>${esc(r.name)}</td><td>${esc(r.email)}</td>
      <td>${r.industries.length ? r.industries.map((i) => esc(industryLabel(i))).join(", ") : `<span class="muted">All industries</span>`}</td>
      <td><button data-toggle-rep="${r.id}">${r.active ? "Deactivate" : "Reactivate"}</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No reps yet. Add your sales team above.</td></tr>`;
  document.querySelectorAll("[data-toggle-rep]").forEach((b) => b.addEventListener("click", async () => {
    const r = REPS.find((x) => x.id == b.dataset.toggleRep);
    await api(`/api/reps/${r.id}`, { method: "PATCH", body: JSON.stringify({ active: !r.active }) });
    loadReps();
  }));
}

// ---------------------------------------------------------------- tabs

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("[data-panel]").forEach((s) => (s.hidden = s.dataset.panel !== name));
  try { localStorage.setItem("areyos.tab", name); } catch { /* ignore */ }
  ({ followups: loadFollowUps, analytics: loadAnalytics, duplicates: loadDuplicates, team: loadReps })[name]?.();
}

// ---------------------------------------------------------------- bulk assignment and manual leads

function selectedLeads() {
  return [...document.querySelectorAll(".lead-pick:checked")].map((c) => Number(c.value));
}

window.onLeadsRendered = () => {
  $("#select-all").checked = false;
  $("#selected-count").textContent = "0 selected";
  document.querySelectorAll(".lead-pick").forEach((c) => c.addEventListener("change", () => {
    $("#selected-count").textContent = `${selectedLeads().length} selected`;
  }));
};

async function bulkAssign() {
  const ids = selectedLeads();
  const target = $("#bulk-rep").value;
  if (!ids.length || !target) return ($("#bulk-msg").textContent = "Pick leads and a rep first.");
  await api("/api/leads/assign", { method: "POST", body: JSON.stringify({ prospect_ids: ids, rep_id: target === "none" ? null : Number(target) }) });
  $("#bulk-msg").textContent = `${ids.length} lead(s) assigned.`;
  loadProspects();
}

async function autoAssign() {
  try {
    const res = await api("/api/leads/auto-assign", { method: "POST", body: JSON.stringify({ prospect_ids: selectedLeads() }) });
    $("#bulk-msg").textContent = `${Object.keys(res.assigned).length} lead(s) auto-assigned by industry and workload.`;
    loadProspects();
  } catch (err) { $("#bulk-msg").textContent = err.message; }
}

function openLeadDialog() {
  $("#lead-form").reset();
  $("#dup-warning").innerHTML = "";
  $("#lead-save").textContent = "Save lead";
  $("#lead-save").dataset.force = "";
  $("#lead-dialog").showModal();
}

async function saveLead(e) {
  e.preventDefault();
  const f = new FormData($("#lead-form"));
  const body = Object.fromEntries([...f].filter(([, v]) => v !== ""));
  if (body.deal_value) body.deal_value = Number(body.deal_value);
  if (actingRep()) body.owner_id = Number(actingRep());
  const force = $("#lead-save").dataset.force === "1";
  const res = await fetch(`/api/leads${force ? "?force=true" : ""}`, {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", ...(actingRep() ? { "X-Areyos-Rep": actingRep() } : {}) },
  });
  const data = await res.json();
  if (res.status === 409) {
    const dups = data.detail.duplicates;
    $("#dup-warning").innerHTML = `<p class="warn">This looks like an existing lead:</p><ul>${dups.map((d) =>
      `<li><a href="#" data-open="${d.id}">${esc(d.name)}</a> (${esc(d.city || "")}, ${esc(stageLabel(d.status))}): ${esc(d.reasons.join("; "))}</li>`).join("")}</ul>`;
    document.querySelectorAll("[data-open]").forEach((a) => a.addEventListener("click", (ev) => {
      ev.preventDefault(); $("#lead-dialog").close(); openDetail(a.dataset.open);
    }));
    $("#lead-save").textContent = "It's a different venue, save anyway";
    $("#lead-save").dataset.force = "1";
    return;
  }
  if (!res.ok) return ($("#dup-warning").innerHTML = `<p class="warn">${esc(JSON.stringify(data.detail))}</p>`);
  $("#lead-dialog").close();
  loadProspects();
  openDetail(data.id);
}

// ---------------------------------------------------------------- lead panel (inside the detail dialog)

function stageOptions(current) {
  return META.statuses.map((s) => `<option value="${s}" ${s === current ? "selected" : ""}>${esc(stageLabel(s))}</option>`).join("");
}

function repOptions(current, blank = "Unassigned") {
  return `<option value="">${blank}</option>` + REPS.filter((r) => r.active || r.id === current)
    .map((r) => `<option value="${r.id}" ${r.id === current ? "selected" : ""}>${esc(r.name)}</option>`).join("");
}

const LOG_FORMS = {
  call: () => `<div class="crm-grid">
      <label>Outcome <select name="outcome"><option value="connected">Connected</option><option value="no_answer">No answer</option><option value="voicemail">Voicemail</option><option value="busy">Busy</option><option value="wrong_number">Wrong number</option></select></label>
      <label>Direction <select name="direction"><option value="outbound">Outbound</option><option value="inbound">Inbound</option></select></label>
      <label>Minutes <input name="duration_minutes" type="number" min="0"></label>
      <label>When <input name="occurred_at" type="datetime-local"></label>
    </div><label>Call notes <textarea name="notes" rows="2"></textarea></label>`,
  meeting: () => `<div class="crm-grid">
      <label>Subject <input name="subject" value="Meeting"></label>
      <label>Outcome <select name="outcome"><option value="held">Held</option><option value="no_show">No-show</option><option value="rescheduled">Rescheduled</option><option value="cancelled">Cancelled</option></select></label>
      <label>Minutes <input name="duration_minutes" type="number" min="0"></label>
      <label>When <input name="occurred_at" type="datetime-local"></label>
    </div><label>Attendees (comma-separated) <input name="attendees"></label>
    <label class="toggle"><input type="checkbox" name="demo"> This was a product demo</label>
    <label>Meeting notes <textarea name="notes" rows="3"></textarea></label>`,
  email: (p) => `<div class="crm-grid">
      <label>To <input name="to" type="email" required value="${esc(firstEmail(p))}"></label>
      <label>Subject <input name="subject" required></label>
      <label>Sent <input name="occurred_at" type="datetime-local"></label>
    </div><label>Body or summary <textarea name="body" rows="2"></textarea></label>
    <label class="toggle"><input type="checkbox" name="track_opens"> Track opens (adds a pixel link to paste into the email)</label>`,
  note: () => `<label>Note <textarea name="body" rows="3" required></textarea></label>`,
  followup: () => `<div class="crm-grid">
      <label>Type <select name="kind"><option value="call">Call</option><option value="email">Email</option><option value="meeting">Meeting</option><option value="demo">Demo</option><option value="task">Task</option></select></label>
      <label>Due <input name="due_at" type="datetime-local" required></label>
      <label>For <select name="rep_id">${repOptions(Number(actingRep()) || null, "Lead owner")}</select></label>
    </div><label>Note <input name="note"></label>`,
};

function firstEmail(p) {
  for (const person of p.people || []) {
    const e = person.contacts.find((c) => c.kind === "email");
    if (e) return e.value;
  }
  return p.contacts.find((c) => c.kind === "email")?.value || "";
}

function isoOrNull(v) { return v ? new Date(v).toISOString() : null; }

async function submitLog(p, kind, form) {
  const f = Object.fromEntries(new FormData(form));
  const base = `/api/leads/${p.id}`;
  let path, body;
  if (kind === "call") {
    path = `${base}/calls`;
    body = { outcome: f.outcome, direction: f.direction, notes: f.notes || null,
      duration_minutes: f.duration_minutes ? Number(f.duration_minutes) : null, occurred_at: isoOrNull(f.occurred_at) };
  } else if (kind === "meeting") {
    path = `${base}/meetings`;
    body = { subject: f.subject || "Meeting", outcome: f.outcome, notes: f.notes || null, demo: !!f.demo,
      attendees: (f.attendees || "").split(",").map((x) => x.trim()).filter(Boolean),
      duration_minutes: f.duration_minutes ? Number(f.duration_minutes) : null, occurred_at: isoOrNull(f.occurred_at) };
  } else if (kind === "email") {
    path = `${base}/emails`;
    body = { to: f.to, subject: f.subject, body: f.body || null, track_opens: !!f.track_opens, occurred_at: isoOrNull(f.occurred_at) };
  } else if (kind === "note") {
    path = `${base}/notes`;
    body = { body: f.body };
  } else {
    path = `${base}/follow-ups`;
    body = { kind: f.kind, due_at: isoOrNull(f.due_at), note: f.note || null, rep_id: f.rep_id ? Number(f.rep_id) : null };
  }
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function timelineItem(a) {
  const who = a.rep_id ? repName(a.rep_id) : "System";
  let line = esc(a.subject || "");
  if (a.kind === "status_change") line = `${esc(stageLabel(a.details.from))} → ${esc(stageLabel(a.details.to))}`;
  if (a.kind === "assignment") line = `Owner: ${a.details.to ? esc(repName(a.details.to)) : "unassigned"}`;
  if (a.kind === "deal_update") line = Object.entries(a.details).map(([k, v]) => `${esc(k.replace(/_/g, " "))}: ${esc(v.to ?? "—")}`).join("; ");
  if (a.kind === "email") {
    line = `${esc(a.subject)} → ${esc(a.email_to)} <span class="muted">(${esc(a.email_status)})</span>`;
    if (a.email_status !== "replied" && a.email_status !== "bounced") {
      line += ` <button class="link" data-email="${a.id}" data-status="replied">mark replied</button><button class="link" data-email="${a.id}" data-status="bounced">bounced</button>`;
    }
    if (a.tracking_token) line += `<div class="muted">Tracking pixel: <code>${esc(location.origin)}/api/t/${esc(a.tracking_token)}.gif</code></div>`;
  }
  const meta = [a.outcome && a.kind !== "email" ? a.outcome.replace(/_/g, " ") : null, a.duration_minutes ? `${a.duration_minutes} min` : null,
    a.details?.attendees?.length ? `with ${a.details.attendees.join(", ")}` : null].filter(Boolean).join(" · ");
  return `<li class="${SYSTEM_KINDS.has(a.kind) ? "system" : ""}"><strong>${ACTIVITY_LABEL[a.kind] || a.kind}</strong> · ${line}
    <div class="muted">${fmtDate(a.occurred_at)} · ${esc(who)}${meta ? ` · ${esc(meta)}` : ""}</div>
    ${a.body ? `<div>${esc(a.body)}</div>` : ""}</li>`;
}

window.renderCrmPanel = async function (p) {
  const box = $("#crm-panel");
  const [acts, fus] = await Promise.all([api(`/api/leads/${p.id}/activities`), api(`/api/follow-ups?prospect_id=${p.id}`)]);
  box.innerHTML = `
    <div class="crm-grid">
      <label>Stage <select id="c-status">${stageOptions(p.status)}</select></label>
      <label>Owner <select id="c-owner">${repOptions(p.owner_id)}</select></label>
      <label>Deal value (₹) <input id="c-deal" type="number" min="0" step="1000" value="${p.deal_value ?? ""}"></label>
      <label>Expected close <input id="c-close" type="date" value="${p.expected_close_date ?? ""}"></label>
    </div>
    <label id="c-reason-wrap" ${p.status === "rejected" ? "" : "hidden"}>Rejection reason <input id="c-reason" value="${esc(p.rejection_reason || "")}"></label>
    <label>Notes <textarea id="c-notes" rows="2">${esc(p.notes || "")}</textarea></label>
    <p><button class="primary" id="c-save">Save lead</button> <span id="c-msg" class="muted" role="status"></span></p>
    <h3>Follow-ups</h3>
    <ul class="timeline">${fus.map((f) => `<li><strong>${esc(cap(f.kind))}</strong> · <span class="${f.overdue ? "overdue" : ""}">${fmtDate(f.due_at)}</span> · ${esc(f.rep_id ? repName(f.rep_id) : "unassigned")}
      ${f.note ? `<div>${esc(f.note)}</div>` : ""}<button class="link" data-fu-done="${f.id}">mark done</button></li>`).join("") || `<li class="system muted">None scheduled.</li>`}</ul>
    <h3>Log activity</h3>
    <div class="row log-tabs">${["call", "meeting", "email", "note", "followup"].map((k, i) =>
      `<button type="button" data-log="${k}" class="${i === 0 ? "active" : ""}">${k === "followup" ? "Schedule follow-up" : cap(k)}</button>`).join("")}</div>
    <form id="c-log"></form>
    <h3>Timeline</h3>
    <ul class="timeline">${acts.map(timelineItem).join("") || `<li class="system muted">No activity yet.</li>`}</ul>`;

  let kind = "call";
  const renderForm = () => {
    $("#c-log").innerHTML = LOG_FORMS[kind](p) + `<p><button class="primary">${kind === "followup" ? "Schedule" : "Log " + kind}</button> <span id="c-log-msg" class="muted"></span></p>`;
  };
  renderForm();
  box.querySelectorAll("[data-log]").forEach((b) => b.addEventListener("click", () => {
    kind = b.dataset.log;
    box.querySelectorAll("[data-log]").forEach((x) => x.classList.toggle("active", x === b));
    renderForm();
  }));
  $("#c-log").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await submitLog(p, kind, e.target);
      openDetail(p.id);
      loadProspects();
    } catch (err) { $("#c-log-msg").textContent = err.message; }
  });
  $("#c-status").addEventListener("change", () => ($("#c-reason-wrap").hidden = $("#c-status").value !== "rejected"));
  $("#c-save").onclick = async () => {
    const body = { notes: $("#c-notes").value, owner_id: $("#c-owner").value ? Number($("#c-owner").value) : null,
      deal_value: $("#c-deal").value === "" ? null : Number($("#c-deal").value), expected_close_date: $("#c-close").value || null };
    if ($("#c-status").value !== p.status) Object.assign(body, { status: $("#c-status").value, rejection_reason: $("#c-reason").value });
    try {
      await api(`/api/leads/${p.id}`, { method: "PATCH", body: JSON.stringify(body) });
      openDetail(p.id);
      loadProspects();
    } catch (err) { $("#c-msg").textContent = err.message; }
  };
  box.querySelectorAll("[data-fu-done]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/follow-ups/${b.dataset.fuDone}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) });
    openDetail(p.id);
  }));
  box.querySelectorAll("[data-email]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/activities/${b.dataset.email}/email-status`, { method: "PATCH", body: JSON.stringify({ status: b.dataset.status }) });
    openDetail(p.id);
  }));
};

// ---------------------------------------------------------------- follow-ups tab

async function loadFollowUps() {
  const f = Object.fromEntries(new FormData($("#fu-filters")));
  const params = new URLSearchParams();
  if (f.scope === "me") {
    if (!actingRep()) {
      $("#followups").innerHTML = `<tr><td colspan="7" class="muted">Choose who you are ("Acting as", top right) to see your follow-ups.</td></tr>`;
      return;
    }
    params.set("rep_id", actingRep());
  }
  if (f.when === "overdue") params.set("overdue", "true");
  params.set("status", f.when === "done" ? "done" : "open");
  $("#fu-export").href = `/api/follow-ups.csv?${params}`;
  const items = await api(`/api/follow-ups?${params}`);
  $("#followups").innerHTML = items.map((x) => `<tr>
      <td class="${x.overdue ? "overdue" : ""}">${fmtDate(x.due_at)}${x.overdue ? " (overdue)" : ""}</td>
      <td>${esc(cap(x.kind))}</td>
      <td><a href="#" data-lead="${x.prospect_id}">${esc(x.lead_name)}</a></td>
      <td>${esc(stageLabel(x.lead_status))}</td>
      <td>${x.rep_id ? esc(repName(x.rep_id)) : ""}</td>
      <td>${esc(x.note || "")}</td>
      <td>${x.status === "open" ? `<button data-done="${x.id}">Done</button>` : esc(x.status)}</td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">Nothing here.</td></tr>`;
  document.querySelectorAll("[data-lead]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openDetail(a.dataset.lead); }));
  document.querySelectorAll("[data-done]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/follow-ups/${b.dataset.done}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) });
    loadFollowUps();
    updateDueBadge();
  }));
}

async function updateDueBadge() {
  if (!actingRep()) return ($("#due-badge").hidden = true);
  const overdue = await api(`/api/follow-ups?rep_id=${actingRep()}&overdue=true`);
  $("#due-badge").hidden = !overdue.length;
  $("#due-badge").textContent = overdue.length;
}

// ---------------------------------------------------------------- analytics tab

let tipEl;
function tip(e, html) {
  tipEl ||= document.body.appendChild(Object.assign(document.createElement("div"), { className: "tip" }));
  tipEl.innerHTML = html;
  tipEl.hidden = false;
  tipEl.style.left = `${Math.min(e.clientX + 12, innerWidth - tipEl.offsetWidth - 8)}px`;
  tipEl.style.top = `${e.clientY + 12}px`;
}
function hideTip() { if (tipEl) tipEl.hidden = true; }

async function loadAnalytics() {
  const [funnel, industries] = await Promise.all([api("/api/analytics/funnel"), api("/api/analytics/industries")]);
  const max = Math.max(1, ...funnel.map((s) => s.leads));
  $("#funnel").innerHTML = funnel.map((s, i) => `
      <span class="lbl">${esc(stageLabel(s.status))}</span>
      <div class="track row-hit" data-i="${i}">${s.leads ? `<div class="fill" style="width:${(100 * s.leads) / max}%"></div>` : ""}</div>
      <span class="val">${s.leads}</span>`).join("");
  $("#funnel").querySelectorAll(".row-hit").forEach((el) => {
    const s = funnel[el.dataset.i];
    el.addEventListener("mousemove", (e) => tip(e, `<strong>${esc(stageLabel(s.status))}</strong><br>${s.leads} lead(s)<br>Deal value ₹${inr(s.deal_value)}<br>Win probability ${Math.round(s.probability * 100)}%`));
    el.addEventListener("mouseleave", hideTip);
  });

  const maxPipe = Math.max(1, ...industries.map((r) => r.pipeline_value));
  $("#industry-analytics").innerHTML = industries.map((r) => `<tr>
      <td>${esc(r.label)}</td><td>${r.leads}</td><td>${r.engaged}</td><td>${r.converted}</td>
      <td>${r.win_rate == null ? "—" : `${Math.round(r.win_rate * 100)}%`}</td>
      <td title="Open pipeline ₹${inr(r.pipeline_value)}">${inr(r.pipeline_value)}<span class="inline-bar" style="width:${(60 * r.pipeline_value) / maxPipe}px"></span></td>
      <td>${inr(r.weighted_pipeline)}</td><td>${inr(r.converted_value)}</td>
      <td>${r.avg_opportunity_score ?? "—"}</td></tr>`).join("") || `<tr><td colspan="9" class="muted">No leads yet.</td></tr>`;
  loadScorecards();
}

async function loadScorecards() {
  const cards = await api(`/api/reps/scorecards?days=${$("#sc-days").value}`);
  $("#scorecards").innerHTML = cards.map((c) => `<tr>
      <td>${esc(c.name)}${c.active ? "" : ` <span class="muted">(inactive)</span>`}</td>
      <td>${c.open_leads}</td><td>${c.converted}</td><td>${c.calls}</td><td>${c.meetings}</td><td>${c.emails}</td>
      <td>${c.email_reply_rate == null ? "—" : `${Math.round(c.email_reply_rate * 100)}%`}</td>
      <td class="${c.overdue_follow_ups ? "overdue" : ""}">${c.overdue_follow_ups}</td>
      <td>${inr(c.weighted_pipeline)}</td><td>${inr(c.converted_value)}</td></tr>`).join("")
    || `<tr><td colspan="10" class="muted">Add reps in the Team tab.</td></tr>`;
}

// ---------------------------------------------------------------- duplicates tab

async function loadDuplicates() {
  const pairs = await api("/api/duplicates");
  const cell = (x) => `<a href="#" data-lead="${x.id}">${esc(x.name)}</a><div class="muted">#${x.id} · ${esc(x.city || "")} · ${esc(stageLabel(x.status))}</div>`;
  $("#duplicates").innerHTML = pairs.map((g) => `<tr><td>${cell(g.keep)}</td><td>${cell(g.duplicate)}</td>
      <td>${esc(g.reasons.join("; "))}</td>
      <td><button data-merge="${g.keep.id}:${g.duplicate.id}">Merge</button> <button data-merge="${g.duplicate.id}:${g.keep.id}" title="Keep the second one instead">Merge the other way</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No likely duplicates.</td></tr>`;
  document.querySelectorAll("#duplicates [data-lead]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openDetail(a.dataset.lead); }));
  document.querySelectorAll("[data-merge]").forEach((b) => b.addEventListener("click", async () => {
    const [keep, dup] = b.dataset.merge.split(":").map(Number);
    if (!confirm(`Merge lead #${dup} into #${keep}? Its history, people and contacts move to #${keep}.`)) return;
    await api("/api/duplicates/merge", { method: "POST", body: JSON.stringify({ keep_id: keep, duplicate_id: dup }) });
    loadDuplicates();
    loadProspects();
  }));
}

// ---------------------------------------------------------------- setup

async function addRep(e) {
  e.preventDefault();
  const f = e.target;
  const industries = [...f.industries.selectedOptions].map((o) => o.value);
  try {
    await api("/api/reps", { method: "POST", body: JSON.stringify({ name: f.name.value, email: f.email.value, phone: f.phone.value || null, industries }) });
    f.reset();
    $("#rep-msg").textContent = "Added.";
    loadReps();
  } catch (err) { $("#rep-msg").textContent = err.message; }
}

(async function initCrm() {
  while (!META) await new Promise((r) => setTimeout(r, 50));
  await loadReps();
  loadProspects();
  $("#rep-industries").innerHTML = META.industries.map((i) => `<option value="${i.key}">${esc(i.label)}</option>`).join("");
  $("#lead-industry").innerHTML = META.industries.map((i) => `<option value="${i.key}">${esc(i.label)}</option>`).join("");
  $("#lead-state").innerHTML = Object.keys(META.states).map((s) => `<option>${esc(s)}</option>`).join("");
  $("#acting-rep").addEventListener("change", () => {
    setActingRep($("#acting-rep").value);
    loadProspects();
    updateDueBadge();
    if (!$("[data-panel=followups]").hidden) loadFollowUps();
  });
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#select-all").addEventListener("change", (e) => {
    document.querySelectorAll(".lead-pick").forEach((c) => (c.checked = e.target.checked));
    $("#selected-count").textContent = `${selectedLeads().length} selected`;
  });
  $("#bulk-assign").addEventListener("click", bulkAssign);
  $("#auto-assign").addEventListener("click", autoAssign);
  $("#add-lead").addEventListener("click", openLeadDialog);
  $("#lead-cancel").addEventListener("click", () => $("#lead-dialog").close());
  $("#lead-form").addEventListener("submit", saveLead);
  $("#lead-form").addEventListener("input", () => { $("#lead-save").dataset.force = ""; $("#lead-save").textContent = "Save lead"; });
  $("#fu-filters").addEventListener("input", loadFollowUps);
  $("#sc-days").addEventListener("change", loadScorecards);
  $("#rep-form").addEventListener("submit", addRep);
  let start = "leads";
  try { start = localStorage.getItem("areyos.tab") || "leads"; } catch { /* ignore */ }
  showTab(document.querySelector(`#tabs [data-tab="${start}"]`) ? start : "leads");
  updateDueBadge();
})();
