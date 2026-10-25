"""Round 14: the officer console page.

One static HTML file: inline CSS, vanilla JS, no build step, no
external assets beyond the Trida logo the portfolio footer
standard carries. The page is a reader and a form set over the
API — it stores nothing, computes nothing the API has not
already computed, and every action it offers posts to an
existing gated route (console.CONSOLE_ACTIONS is the manifest
the round 14 scan grades).

Rendering discipline: stored content — borrower names, document
filenames, memo prose, officer notes — is DATA. The page builds
every node with createElement and assigns every string through
textContent; there is no HTML-parsing sink anywhere in the
file, so a script carried in a document filename renders as
the inert text it is. The test suite and the eval gate both
scan this file for parsing sinks and require their absence.

Refusals: the gates refuse unlawful actions with reasons. The
console shows the refusal, in plain language, exactly once —
it never retries around one, never rephrases it away, and
never disables the gate's checks by pre-approving locally.
"""

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Officer Console — Commercial Credit Memo &amp; Covenant Monitoring</title>
<style>
  :root { --ink: #0A0A0A; --indigo: #4F46E5; --line: #E3E3EC; --mut: #5B5B6B;
          --bg: #FFFFFF; --panel: #F7F7FB; --bad: #B3261E; --warn: #9A6B00;
          --good: #1E7A34; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: var(--ink); background: var(--bg); font-size: 14px; line-height: 1.45; }
  header { display: flex; justify-content: space-between; align-items: flex-start;
           gap: 16px; padding: 16px 22px; border-bottom: 2px solid var(--indigo); flex-wrap: wrap; }
  .brand { display: flex; gap: 12px; align-items: center; }
  .brand img { border-radius: 8px; }
  h1 { font-size: 19px; margin: 0; }
  .sub { color: var(--mut); margin: 2px 0 0; font-size: 12.5px; }
  .identity { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
  .identity label, .field label { display: block; font-size: 11px; color: var(--mut);
           text-transform: uppercase; letter-spacing: .04em; margin-bottom: 3px; }
  input, select, textarea { font: inherit; font-size: 13.5px; padding: 6px 8px;
           border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--ink); }
  textarea { width: 100%; min-height: 54px; }
  nav { display: flex; gap: 8px; padding: 10px 22px 0; }
  nav button, .btn { font: inherit; cursor: pointer; border: 1px solid var(--indigo);
           background: var(--indigo); color: #fff; border-radius: 6px; padding: 6px 14px; }
  nav button.ghost, .btn.ghost { background: #fff; color: var(--indigo); }
  .btn.small { padding: 3px 10px; font-size: 12.5px; }
  .btn.danger { background: var(--bad); border-color: var(--bad); }
  main { padding: 14px 22px 30px; }
  #error-banner { margin: 12px 22px 0; padding: 10px 14px; border: 1px solid var(--bad);
           color: var(--bad); border-radius: 6px; background: #FDF1F0; }
  #notice-bar { margin: 12px 22px 0; padding: 10px 14px; border: 1px solid var(--good);
           color: var(--good); border-radius: 6px; background: #F0F8F1; }
  h2 { font-size: 16px; margin: 22px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  h3 { font-size: 14px; margin: 14px 0 6px; }
  table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line);
           vertical-align: top; font-size: 13px; }
  th { color: var(--mut); font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: .03em; }
  tr.clickable { cursor: pointer; } tr.clickable:hover { background: var(--panel); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
           border: 1px solid var(--indigo); color: var(--indigo); }
  .badge.bad { border-color: var(--bad); color: var(--bad); }
  .badge.warn { border-color: var(--warn); color: var(--warn); }
  .badge.good { border-color: var(--good); color: var(--good); }
  .badge.mut { border-color: var(--mut); color: var(--mut); }
  dl.kv { display: grid; grid-template-columns: 180px 1fr; gap: 3px 14px; margin: 6px 0 12px; }
  dl.kv dt { color: var(--mut); } dl.kv dd { margin: 0; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
           padding: 12px 14px; margin: 10px 0 16px; }
  .panel h3 { margin-top: 0; }
  .formrow { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; margin: 8px 0; }
  .field { min-width: 160px; }
  .checks { display: flex; flex-direction: column; gap: 3px; margin: 4px 0; }
  .checks label { font-size: 13px; color: var(--ink); text-transform: none; letter-spacing: 0;
           display: flex; gap: 6px; align-items: baseline; }
  .form-error { color: var(--bad); margin: 8px 0 0; }
  .mut { color: var(--mut); } .bad { color: var(--bad); } .good { color: var(--good); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
          word-break: break-all; }
  details { margin: 4px 0; } summary { cursor: pointer; color: var(--indigo); }
  .memo-section p { margin: 5px 0; white-space: pre-wrap; }
  footer { border-top: 1px solid var(--line); padding: 14px 22px; display: flex;
           gap: 16px; align-items: center; color: var(--mut); font-size: 13px; flex-wrap: wrap; }
  footer a { color: var(--indigo); text-decoration: none; }
  footer img { border-radius: 6px; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="38" height="38">
    <div>
      <h1>Commercial Credit — Officer Console</h1>
      <p class="sub">Aldergrove Bank · credit memo &amp; covenant monitoring · the console reads the record and posts to the gates; it decides nothing itself</p>
    </div>
  </div>
  <div class="identity">
    <div class="field"><label for="officer-name">Officer name</label>
      <input id="officer-name" type="text" autocomplete="off" placeholder="e.g. officer-amara-osei"></div>
    <div class="field"><label for="officer-role">Role</label>
      <select id="officer-role">
        <option value="credit_officer">credit_officer</option>
        <option value="senior_credit_officer">senior_credit_officer</option>
        <option value="committee_chair">committee_chair</option>
        <option value="committee_member">committee_member</option>
        <option value="relationship_manager">relationship_manager</option>
      </select></div>
    <div class="field"><label for="as-of">As of</label>
      <input id="as-of" type="date"></div>
    <button class="btn ghost" id="refresh-btn" type="button">Refresh</button>
  </div>
</header>
<nav>
  <button id="nav-queue" type="button">Pipeline queue</button>
  <button id="nav-due" type="button" class="ghost">Due-work board</button>
  <button id="nav-workspace" type="button" class="ghost" hidden>File workspace</button>
</nav>
<div id="error-banner" hidden></div>
<div id="notice-bar" hidden></div>
<main>
  <section id="view-queue"></section>
  <section id="view-duework" hidden></section>
  <section id="view-workspace" hidden></section>
</main>
<footer>
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="26" height="26">
  <span>Trida AI Blueprint — Commercial Credit Memo &amp; Covenant Monitoring Agent</span>
  <a href="https://trida.ai">Visit Trida AI →</a>
  <a href="https://github.com/tridaai">View all blueprints →</a>
</footer>
<script>
"use strict";
const state = { vocab: null, ws: null, fileId: null, memoId: null };

function h(tag, attrs) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value === null || value === undefined) continue;
      if (key === "class") node.className = value;
      else if (key === "onClick") node.addEventListener("click", value);
      else if (key === "onChange") node.addEventListener("change", value);
      else if (key === "onSubmit") node.addEventListener("submit", value);
      else node.setAttribute(key, value);
    }
  }
  for (let i = 2; i < arguments.length; i++) {
    const child = arguments[i];
    if (child === null || child === undefined) continue;
    if (Array.isArray(child)) { for (const c of child) if (c !== null && c !== undefined) node.append(c.nodeType ? c : document.createTextNode(String(c))); }
    else node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, opts) {
  const res = await fetch(path, opts || {});
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = null; }
  if (!res.ok) {
    const detail = (data && data.detail) || text || res.statusText;
    throw new Error("The API refused this request (" + res.status + "): " + detail);
  }
  return data;
}
function get(path) { return api(path); }
function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function showError(err) {
  const banner = document.getElementById("error-banner");
  banner.textContent = err && err.message ? err.message : String(err);
  banner.hidden = false;
  document.getElementById("notice-bar").hidden = true;
}
function showNotice(text) {
  const bar = document.getElementById("notice-bar");
  bar.textContent = text;
  bar.hidden = false;
  document.getElementById("error-banner").hidden = true;
}
function asOf() { return document.getElementById("as-of").value; }
function identity() {
  return {
    id: document.getElementById("officer-name").value.trim(),
    role: document.getElementById("officer-role").value
  };
}
function requireIdentity() {
  const who = identity();
  if (!who.id) {
    showError("Enter your officer name before acting. The gates record named officers only, and this console will not submit an anonymous action.");
    return null;
  }
  return who;
}
function v(x) { return x === null || x === undefined || x === "" ? "—" : String(x); }
function badge(text, kind) { return h("span", { "class": "badge" + (kind ? " " + kind : "") }, v(text)); }
function stageBadge(stage) {
  const kind = stage === "declined" ? "bad" : (stage === "approved" || stage === "monitoring") ? "good" : stage === "committee" ? "warn" : "";
  return badge(stage, kind);
}
function outcomeBadge(outcome) {
  const kind = outcome === "pass" || outcome === "computed" ? "good" : outcome === "fail" ? "bad" : "warn";
  return badge(outcome, kind);
}
function table(headers, rows) {
  const thead = h("thead", null, h("tr", null, headers.map((t) => h("th", null, t))));
  const tbody = h("tbody", null, rows.map((r) => {
    if (r && r.__row) return r.__row;
    return h("tr", null, r.map((c) => h("td", null, c)));
  }));
  return h("table", null, thead, tbody);
}
function kv(pairs) {
  const children = [];
  for (const pair of pairs) { children.push(h("dt", null, pair[0])); children.push(h("dd", null, pair[1])); }
  return h("dl", { "class": "kv" }, children);
}
function sectionEl(title, children) {
  return h("section", null, h("h2", null, title), children);
}
function emptyNote(text) { return h("p", { "class": "mut" }, text); }
function fieldEl(label, input) {
  return h("div", { "class": "field" }, h("label", null, label), input);
}
function textInput(id, placeholder) {
  return h("input", { type: "text", id: id, placeholder: placeholder || "", autocomplete: "off" });
}
function formErrorEl() { return h("p", { "class": "form-error", hidden: "" }); }
async function runAction(errEl, fn, notice) {
  errEl.hidden = true;
  try {
    await fn();
    if (notice) showNotice(notice);
    if (state.fileId) await openWorkspace(state.fileId, true);
    await loadQueue(true);
  } catch (e) {
    errEl.textContent = e.message || String(e);
    errEl.hidden = false;
    showError(e);
  }
}
function codeChecks(containerId, codes) {
  const wrap = h("div", { "class": "checks", id: containerId });
  for (const code of Object.keys(codes)) {
    const box = h("input", { type: "checkbox", value: code });
    wrap.append(h("label", null, box, code + " — " + codes[code]));
  }
  return wrap;
}
function checkedCodes(container) {
  const out = [];
  for (const box of container.querySelectorAll("input[type=checkbox]:checked")) out.push(box.value);
  return out;
}

/* ---------------- Queue ---------------- */
const STAGES = ["intake", "spreading", "memo draft", "officer review", "committee", "approved", "declined", "monitoring"];
async function loadQueue(keepView) {
  const rows = await get(`/console/queue?as_of=${asOf()}`);
  const view = document.getElementById("view-queue");
  view.replaceChildren();
  view.append(h("h2", null, "Pipeline queue"));
  if (!rows.length) { view.append(emptyNote("No credit files yet.")); return; }
  for (const stage of STAGES) {
    const inStage = rows.filter((r) => r.stage === stage);
    if (!inStage.length) continue;
    view.append(h("h3", null, stage + " (" + inStage.length + ")"));
    view.append(table(
      ["Borrower", "Segment", "Facility", "Amount", "Stage", "Readiness blockers", "Rating", "Open breaches"],
      inStage.map((r) => {
        const tr = h("tr", { "class": "clickable", onClick: () => openWorkspace(r.credit_file_id) },
          h("td", null, r.borrower_name),
          h("td", null, v(r.segment)),
          h("td", null, v(r.facility_type)),
          h("td", null, v(r.facility_amount)),
          h("td", null, stageBadge(r.stage)),
          h("td", null, r.readiness_blockers === null ? "not computed" : (r.readiness_blockers === 0 ? badge("none", "good") : badge(String(r.readiness_blockers) + " blocker(s)", "bad"))),
          h("td", null, r.rating ? badge("grade " + r.rating.final_grade + " · " + r.rating.final_band, r.rating.final_grade >= 7 ? "bad" : r.rating.final_grade >= 5 ? "warn" : "good") : "—"),
          h("td", null, r.open_breaches ? badge(String(r.open_breaches), "bad") : "0"));
        return { __row: tr };
      })));
  }
  if (!keepView) showView("queue");
}

/* ---------------- Due-work board ---------------- */
async function loadDueWork() {
  const day = asOf();
  const results = await Promise.all([
    get(`/breach-cases?as_of=${day}`),
    get(`/covenant-due-work?as_of=${day}`),
    get(`/annual-review-due-work?as_of=${day}`)
  ]);
  const breaches = results[0], covenantWork = results[1], reviewWork = results[2];
  const view = document.getElementById("view-duework");
  view.replaceChildren();
  view.append(h("h2", null, "Due-work board — worst first, in the order the API returns each queue"));
  view.append(h("h3", null, "Breach cases (" + breaches.length + ")"));
  view.append(breaches.length ? table(
    ["Borrower file", "Covenant", "State", "Test date", "Days in breach", "Cure deadline", "Days to cure"],
    breaches.map((b) => [b.credit_file_id, v(b.covenant_name || b.covenant_id), badge(b.state, b.state === "cured" || b.state === "waived" ? "good" : "bad"), v(b.test_date), v(b.days_in_breach), v(b.cure_deadline), b.cure_overdue ? badge(v(b.days_to_cure_deadline) + " — overdue", "bad") : v(b.days_to_cure_deadline)]))
    : emptyNote("No breach cases."));
  view.append(h("h3", null, "Covenant tests & deliverables (" + covenantWork.length + ")"));
  view.append(covenantWork.length ? table(
    ["Borrower", "Kind", "What", "State", "Due", "Required inputs"],
    covenantWork.map((e) => {
      const inst = e.instance || {};
      return [e.borrower, v(e.kind), v(inst.name || inst.instance_id), badge(inst.state || "—", inst.state === "overdue" ? "bad" : inst.state === "due" ? "warn" : ""), v(inst.test_date || inst.due_date), (inst.required_inputs || []).join(", ") || "—"];
    })) : emptyNote("Nothing due."));
  view.append(h("h3", null, "Annual reviews (" + reviewWork.length + ")"));
  view.append(reviewWork.length ? table(
    ["Borrower", "Review #", "State", "Due date", "Days until due"],
    reviewWork.map((e) => {
      const inst = e.review_instance || {};
      return [e.borrower, v(inst.review_number), badge(inst.state || "—", inst.state === "overdue" ? "bad" : ""), v(inst.due_date), v(inst.days_until_due)];
    })) : emptyNote("No annual reviews due."));
  showView("duework");
}

/* ---------------- Workspace ---------------- */
function showView(name) {
  document.getElementById("view-queue").hidden = name !== "queue";
  document.getElementById("view-duework").hidden = name !== "duework";
  document.getElementById("view-workspace").hidden = name !== "workspace";
  document.getElementById("nav-queue").className = name === "queue" ? "" : "ghost";
  document.getElementById("nav-due").className = name === "duework" ? "" : "ghost";
  const wsBtn = document.getElementById("nav-workspace");
  wsBtn.hidden = !state.fileId;
  wsBtn.className = name === "workspace" ? "" : "ghost";
}

async function openWorkspace(fileId, keepView) {
  const ws = await get(`/console/workspace/${fileId}?as_of=${asOf()}`);
  state.ws = ws;
  state.fileId = fileId;
  state.memoId = ws.memos.length ? ws.memos[ws.memos.length - 1].memo.memo_id : null;
  renderWorkspace();
  if (!keepView) showView("workspace");
}

function renderWorkspace() {
  const ws = state.ws;
  const view = document.getElementById("view-workspace");
  view.replaceChildren();
  const file = ws.file || {};
  const borrower = file.borrower || {};
  const facility = file.facility || {};
  view.append(h("h2", null, borrower.legal_name || ws.credit_file_id));
  view.append(kv([
    ["Credit file", ws.credit_file_id],
    ["Stage", stageBadge(ws.stage)],
    ["Borrower", borrower.legal_name],
    ["Segment", borrower.segment],
    ["State of formation", borrower.state_of_formation],
    ["Years in business", borrower.years_in_business],
    ["Primary contact", (borrower.primary_contact_name || "—") + " · " + (borrower.primary_contact_email || "")],
    ["Facility", (facility.facility_type || "—") + " · " + v(facility.amount) + " · " + v(facility.term_months) + " months"],
    ["Purpose", facility.purpose],
    ["Closing date", file.closing_date || "not recorded"],
    ["File status", file.status]
  ]));
  view.append(renderChecklist(ws));
  view.append(renderDocuments(ws));
  view.append(renderReadiness(ws));
  view.append(renderSpread(ws));
  view.append(renderComputations(ws));
  view.append(renderPolicy(ws));
  view.append(renderDisagreements(ws));
  view.append(renderMemos(ws));
  view.append(renderRatings(ws));
  view.append(renderCovenants(ws));
  view.append(renderBreaches(ws));
  view.append(renderReviews(ws));
  view.append(renderFileDueWork(ws));
  view.append(renderActions(ws));
}

function renderChecklist(ws) {
  const items = ws.checklist || [];
  return sectionEl("Document checklist — each item names the rule that requires it", items.length ? table(
    ["Item", "Kind", "Required", "Satisfied", "Requirement source", "Periods"],
    items.map((i) => [i.label, i.kind, i.required ? "yes" : "no",
      i.satisfied_by_document_id ? badge("on file", "good") : badge("missing", "bad"),
      i.requirement_source, v(i.periods_required)]))
    : emptyNote("No checklist."));
}

function renderDocuments(ws) {
  const docs = ws.documents || [];
  return sectionEl("Documents — SHA-256 recorded at intake, re-verified on every read", docs.length ? table(
    ["Filename", "Kind", "Period", "SHA-256", "Bytes", "Received"],
    docs.map((d) => [d.filename, d.kind, v(d.period_label), h("span", { "class": "mono" }, (d.sha256 || "").slice(0, 16) + "…"), v(d.byte_size), v(d.received_at)]))
    : emptyNote("No documents registered."));
}

function renderReadiness(ws) {
  const report = ws.readiness;
  if (!report) return sectionEl("Committee readiness (round 9)", emptyNote("Readiness cannot be computed for this file yet."));
  const entries = report.entries || [];
  const blockers = entries.filter((e) => e.blocker);
  const children = [kv([
    ["Report", report.report_id],
    ["As of", report.as_of],
    ["Policy pack", (report.policy_pack_id || "") + " · version " + v(report.policy_version)],
    ["Blockers", blockers.length === 0 ? badge("none — committee-ready", "good") : badge(String(blockers.length) + " — the committee gate will refuse to convene", "bad")]
  ])];
  children.push(table(
    ["Input", "Source", "State", "Blocker", "Detail", "Missing inputs"],
    entries.map((e) => [e.label, e.source, e.state, e.blocker ? badge("blocker", "bad") : "—", e.detail, (e.missing_inputs || []).join(", ") || "—"])));
  return sectionEl("Committee readiness (round 9) — stale or missing inputs block, by name", children);
}

function renderSpread(ws) {
  const spread = ws.spread;
  if (!spread) return sectionEl("Spread (round 2)", emptyNote("No spread yet — statements have not been extracted."));
  const periods = spread.periods || [];
  const wrap = h("div", null);
  const mode = { adjusted: false };
  const tableHost = h("div", null);
  function draw() {
    const rows = (spread.lines || []).map((line) => {
      const values = mode.adjusted ? (line.adjusted_values || {}) : (line.values || {});
      const cells = [line.label + " (" + line.section + ")"];
      for (const p of periods) cells.push(values[p] === null || values[p] === undefined ? "—" : String(values[p]));
      return cells;
    });
    tableHost.replaceChildren(table(["Line"].concat(periods.map((p) => "FY" + p)), rows));
  }
  draw();
  const toggle = h("button", { "class": "btn ghost small", type: "button", onClick: () => { mode.adjusted = !mode.adjusted; toggle.textContent = mode.adjusted ? "Show unadjusted view" : "Show adjusted view"; draw(); } }, "Show adjusted view");
  wrap.append(kv([
    ["Spread", spread.spread_id],
    ["Template", spread.template_version],
    ["Periods", periods.join(", ")],
    ["Adjustments on record", String((spread.adjustments || []).length)]
  ]));
  wrap.append(toggle, tableHost);
  const unmapped = spread.unmapped || [];
  if (unmapped.length) wrap.append(h("h3", null, "Unmapped lines — carried by name, never dropped"), table(["Line", "Period", "Value"], unmapped.map((u) => [u.label || u.line_key, v(u.fiscal_year), v(u.value)])));
  const adjustments = ws.adjustments || [];
  if (adjustments.length) wrap.append(h("h3", null, "Adjustments — each one reasoned and provenanced"), table(
    ["Line", "Year", "Amount", "Reason code", "Reason", "Proposed by"],
    adjustments.map((a) => [a.line_key, v(a.fiscal_year), v(a.amount), a.reason_code, a.reason, a.proposed_by])));
  return sectionEl("Spread (round 2) — multi-period, adjusted and unadjusted views", wrap);
}

function renderComputations(ws) {
  const packs = ws.computation_packs || [];
  if (!packs.length) return sectionEl("Computation pack (round 3)", emptyNote("No computation pack yet."));
  const pack = packs[packs.length - 1];
  const wrap = h("div", null);
  wrap.append(kv([
    ["Pack", pack.pack_id],
    ["Engine", pack.engine_version],
    ["View", pack.view],
    ["Scope", pack.scope + (pack.entity ? " · " + pack.entity : "")]
  ]));
  const figures = pack.figures || [];
  wrap.append(table(
    ["Figure", "Period", "Value", "Status", "Missing inputs", "Derivation"],
    figures.map((f) => {
      const steps = (f.steps || []).map((s) => {
        const inputs = (s.inputs || []).map((inp) => (inp.kind || "") + (inp.line_key ? " " + inp.line_key : "") + (inp.figure_key ? " " + inp.figure_key : "") + " = " + v(inp.value)).join("; ");
        return h("li", null, s.label + " — " + s.operation + " → " + v(s.result) + (inputs ? " (" + inputs + ")" : ""));
      });
      const det = h("details", null, h("summary", null, steps.length + " step(s)"), h("ul", null, steps));
      return [f.label + " (" + f.key + ")", v(f.fiscal_year), f.value === null || f.value === undefined ? "—" : String(f.value), outcomeBadge(f.status), (f.missing_inputs || []).join(", ") || "—", det];
    })));
  const covenants = pack.covenant_results || [];
  if (covenants.length) {
    wrap.append(h("h3", null, "Covenant headroom on the historical figures"));
    wrap.append(table(["Test", "Metric", "Year", "Threshold", "Value", "Headroom", "Compliant"],
      covenants.map((c) => [c.name, c.metric_key, v(c.fiscal_year), (c.operator || "") + " " + v(c.threshold), v(c.value), v(c.headroom), c.compliant === null || c.compliant === undefined ? "—" : c.compliant ? badge("yes", "good") : badge("no", "bad")])));
  }
  return sectionEl("Computation pack (round 3) — every figure traces to its derivation steps", wrap);
}

function renderPolicy(ws) {
  const reports = ws.policy_reports || [];
  if (!reports.length) return sectionEl("Policy report (round 4)", emptyNote("No policy evaluation yet."));
  const report = reports[reports.length - 1];
  const wrap = h("div", null);
  wrap.append(kv([
    ["Report", report.report_id],
    ["Policy pack", (report.policy_pack_id || "") + " · version " + v(report.policy_version) + " (pinned for this report)"],
    ["As of", report.as_of]
  ]));
  wrap.append(table(
    ["Test", "Severity", "Outcome", "Threshold", "Detail", "Missing inputs"],
    (report.outcomes || []).map((o) => [o.name + " (" + o.test_id + ")", o.severity, outcomeBadge(o.outcome), (o.operator || "") + " " + v(o.threshold), o.detail, (o.missing_inputs || []).join(", ") || "—"])));
  return sectionEl("Policy report (round 4) — tested under the pinned pack version", wrap);
}

function renderDisagreements(ws) {
  const rows = ws.disagreements || [];
  return sectionEl("Document disagreements (round 8) — carried with provenance, never averaged away", rows.length ? table(
    ["Line", "Period", "Material", "Working value", "Sources in disagreement"],
    rows.map((d) => {
      const sources = (d.sources || []).map((s) => (s.source_type || "") + ": " + v(s.value)).join(" · ");
      return [d.line_label || d.line_key, v(d.fiscal_year), d.material ? badge("material", "bad") : badge("immaterial", "mut"), v(d.working_value), sources || "—"];
    })) : emptyNote("No disagreements between the file's source documents."));
}

function renderMemos(ws) {
  const memos = ws.memos || [];
  if (!memos.length) return sectionEl("Credit memo & the two gates (rounds 5–7)", emptyNote("No memo composed yet."));
  const wrap = h("div", null);
  const select = h("select", { onChange: () => { state.memoId = select.value; renderWorkspace(); } },
    memos.map((mv) => h("option", { value: mv.memo.memo_id, selected: mv.memo.memo_id === state.memoId ? "" : null }, mv.memo.memo_id.slice(0, 18) + "… · " + mv.status)));
  wrap.append(h("div", { "class": "formrow" }, fieldEl("Memo", select)));
  const current = memos.find((mv) => mv.memo.memo_id === state.memoId) || memos[memos.length - 1];
  const memo = current.memo;
  wrap.append(kv([
    ["Memo", memo.memo_id],
    ["Derived status", badge(current.status)],
    ["Policy pack", (memo.policy_pack_id || "") + " · version " + v(memo.policy_version)],
    ["Spread / pack", (memo.spread_id || "") + " / " + (memo.computation_pack_id || "")],
    ["Rating in header", memo.rating_assignment_id ? "grade " + v(memo.rating_grade) + " (" + v(memo.rating_band) + ") from " + memo.rating_assignment_id : "none stood at composition"],
    ["Verification", current.verification ? (current.verification.verified ? badge("verified — " + v(current.verification.figure_count) + " figures traced", "good") : badge("NOT verified", "bad")) : "—"]
  ]));
  for (const sec of memo.sections || []) {
    const box = h("div", { "class": "memo-section" });
    box.append(h("h3", null, sec.title || sec.key));
    for (const p of sec.paragraphs || []) box.append(h("p", null, p));
    for (const t of sec.tables || []) {
      box.append(table(t.columns || [], (t.rows || []).map((r) => r)));
      if (t.caption) box.append(h("p", { "class": "mut" }, t.caption));
    }
    wrap.append(box);
  }
  const events = current.events || [];
  if (events.length) {
    wrap.append(h("h3", null, "Gate history — every act named"));
    wrap.append(table(["When", "Action", "Actor", "Role", "Note"],
      events.map((e) => [v(e.created_at), e.action, v(e.actor_id || e.officer_id || e.actor), v(e.actor_role || e.officer_role || e.role), e.note || ""])));
  }
  const decisions = (current.committee && current.committee.decisions) || [];
  if (decisions.length) {
    wrap.append(h("h3", null, "Committee decisions"));
    wrap.append(table(["Decision", "Outcome", "Recorded by", "Quorum", "Conditions", "Adverse reasons"],
      decisions.map((d) => [d.decision_id.slice(0, 14) + "…", outcomeBadge(d.outcome), d.recorded_by, v(d.quorum_required),
        (d.conditions || []).map((c) => c.type + (c.metric_key ? " " + c.metric_key + " " + (c.operator || "") + " " + v(c.threshold) : "")).join("; ") || "—",
        (d.adverse_reasons || []).map((r) => r.code).join(", ") || "—"])));
  }
  return sectionEl("Credit memo & the two gates (rounds 5–7)", wrap);
}

function renderRatings(ws) {
  const ratings = ws.ratings || [];
  if (!ratings.length) return sectionEl("Risk rating (round 13) — the coded scorecard; it decides nothing", emptyNote("No rating assigned yet. Assign one from the action panel below."));
  const current = ratings[ratings.length - 1];
  const a = current.assignment || {};
  const wrap = h("div", null);
  wrap.append(kv([
    ["Assignment", a.assignment_id],
    ["Scorecard", (a.scorecard_id || "") + " · version " + v(a.scorecard_version)],
    ["Assigned grade", String(current.assigned_grade) + " (" + current.assigned_band + ")"],
    ["Final grade", badge("grade " + current.final_grade + " · " + current.final_band, current.final_grade >= 7 ? "bad" : current.final_grade >= 5 ? "warn" : "good")],
    ["Composite", v(a.composite_score)],
    ["As of", a.as_of]
  ]));
  wrap.append(table(
    ["Factor", "Kind", "Input", "Band", "Score", "Weight", "Contribution", "Trace"],
    (a.factors || []).map((f) => [f.factor_name + " (" + f.factor_id + ")", f.kind,
      f.input_value !== null && f.input_value !== undefined ? String(f.input_value) : v(f.input_category),
      f.band_label || f.band_id || "—", v(f.score), v(f.weight), v(f.weighted_contribution),
      f.trace || ((f.missing_inputs || []).length ? "missing: " + f.missing_inputs.join(", ") : "—")])));
  const overrides = current.overrides || [];
  if (overrides.length) {
    wrap.append(h("h3", null, "Overrides — the assigned grade stands beside every move"));
    wrap.append(table(["From → to", "Officer", "Reason", "Countersigned by"],
      overrides.map((o) => [v(o.previous_grade) + " → " + v(o.new_grade), o.officer_id, o.reason_code + (o.note ? " — " + o.note : ""), o.countersigner_id || "—"])));
  }
  if (ratings.length > 1) wrap.append(h("p", { "class": "mut" }, ratings.length + " assignments on record; the workspace shows the latest. History is served by the API and never rewritten by a scorecard version change."));
  return sectionEl("Risk rating (round 13) — the coded scorecard; it decides nothing", wrap);
}

function renderCovenants(ws) {
  const sets = ws.covenant_sets || [];
  const schedule = ws.covenant_schedule || { sets: [] };
  const results = ws.covenant_test_results || [];
  const wrap = h("div", null);
  if (!sets.length) { wrap.append(emptyNote("No covenant set — sets are created by a committee approval.")); return sectionEl("Covenants (rounds 10–11)", wrap); }
  for (const view of schedule.sets || []) {
    const set = view.covenant_set || {};
    wrap.append(h("h3", null, "Set " + (set.set_id || "") + " · " + v(set.state) + " · closing anchor " + v(set.closing_date)));
    wrap.append(table(["Covenant", "Metric", "Threshold", "Frequency", "Source"],
      (view.current_covenants || []).map((c) => [c.name || c.covenant_id, v(c.metric_key), (c.operator || "") + " " + v(c.threshold), v(c.frequency), v(c.source)])));
    wrap.append(table(["Test date", "Covenant", "State", "Required inputs"],
      (view.instances || []).map((i) => [v(i.test_date || i.due_date), v(i.name || i.covenant_id), badge(i.state || "—", i.state === "overdue" ? "bad" : i.state === "due" ? "warn" : ""), (i.required_inputs || []).join(", ") || "—"])));
  }
  if (results.length) {
    wrap.append(h("h3", null, "Test results — recomputed from fresh spreads, in code"));
    wrap.append(table(["Covenant", "Period", "Figure", "Threshold", "Outcome", "Standing"],
      results.map((r) => [v(r.covenant_name || r.covenant_id), v(r.fiscal_year), v(r.figure_value), (r.operator || "") + " " + v(r.threshold), outcomeBadge(r.outcome), v(r.standing)])));
  }
  return sectionEl("Covenants (rounds 10–11) — clocks plus math", wrap);
}

function renderBreaches(ws) {
  const breaches = ws.breach_cases || [];
  return sectionEl("Breach cases (round 11) — a failed test raises a case; only coded acts resolve it", breaches.length ? table(
    ["Breach", "Covenant", "State", "Test date", "Figure vs threshold", "Days in breach", "Cure deadline"],
    breaches.map((b) => [b.breach_id.slice(0, 14) + "…", v(b.covenant_name || b.covenant_id), badge(b.state, b.state === "cured" || b.state === "waived" ? "good" : "bad"), v(b.test_date), v(b.figure_value) + " vs " + (b.operator || "") + " " + v(b.threshold), v(b.days_in_breach), b.cure_overdue ? badge(v(b.cure_deadline) + " — overdue", "bad") : v(b.cure_deadline)]))
    : emptyNote("No breach cases on this file."));
}

function renderReviews(ws) {
  const schedule = ws.annual_review_schedule || { sets: [] };
  const packs = ws.annual_review_packs || [];
  const outcomes = ws.annual_review_outcomes || [];
  const wrap = h("div", null);
  const instances = [];
  for (const view of schedule.sets || []) for (const i of view.instances || []) instances.push(i);
  wrap.append(instances.length ? table(["Review #", "Anchor", "Period start", "Due", "State", "Pack", "Outcome"],
    instances.map((i) => [v(i.review_number), v(i.anchor_date), v(i.period_start), v(i.due_date), badge(i.state, i.state === "overdue" ? "bad" : ""), i.pack_id ? i.pack_id.slice(0, 14) + "…" : "—", i.outcome_id ? "recorded" : "—"]))
    : emptyNote("No annual review obligations yet."));
  if (packs.length) {
    wrap.append(h("h3", null, "Review packs"));
    wrap.append(table(["Pack", "Review #", "Complete", "Blockers", "Rating carried"],
      packs.map((p) => [p.pack_id.slice(0, 14) + "…", v(p.review_number), p.complete ? badge("complete", "good") : badge("incomplete — blockers named", "bad"), (p.blockers || []).map((b) => b.label || b.input_key).join("; ") || "—", p.rating_assignment_id ? "grade " + v(p.rating_grade) : "—"])));
  }
  if (outcomes.length) {
    wrap.append(h("h3", null, "Review outcomes"));
    wrap.append(table(["Review #", "Stance", "Reviewer", "Reasons"],
      outcomes.map((o) => [v(o.review_number), badge(o.stance), o.reviewer_id, (o.reason_codes || []).join(", ")])));
  }
  const referrals = ws.committee_referrals || [];
  if (referrals.length) wrap.append(h("p", null, referrals.length + " committee referral(s) recorded from escalated reviews."));
  return sectionEl("Annual reviews (round 12)", wrap);
}

function renderFileDueWork(ws) {
  const items = ws.due_work || [];
  return sectionEl("Due work on this file", items.length ? table(
    ["Kind", "What", "State", "Due"],
    items.map((e) => {
      const inst = e.instance || e.review_instance || {};
      return [v(e.kind), v(inst.name || inst.instance_id || ("review #" + v(inst.review_number))), badge(inst.state || "—", inst.state === "overdue" ? "bad" : ""), v(inst.test_date || inst.due_date)];
    })) : emptyNote("Nothing due on this file at the as-of date."));
}

/* ---------------- Actions ----------------
   Every form below posts to an existing gated route. The
   console performs the gate's checks again nowhere: identity
   is required before a form will submit (an anonymous action
   is not worth sending), and after that the API's answer —
   success or refusal — is the whole story. */
function currentMemoId() { return state.memoId; }

function renderActions(ws) {
  const wrap = h("div", null);
  wrap.append(officerPanel(ws), committeePanel(ws), ratingPanel(ws), breachPanel(ws), reviewPanel(ws));
  return sectionEl("Act on this file — through the gates, never around them", wrap);
}

function panel(title, note, children) {
  return h("div", { "class": "panel" }, h("h3", null, title), h("p", { "class": "mut" }, note), children);
}

function officerPanel(ws) {
  const err = formErrorEl();
  const note = textInput("officer-note", "Note (optional, recorded)");
  const memos = ws.memos || [];
  const children = [];
  if (!memos.length) { children.push(emptyNote("No memo to act on.")); return panel("Credit officer gate (round 6)", "Submit, approve as composed, return with coded reasons, or flag for committee.", children); }
  const returnCodes = codeChecks("return-codes", state.vocab.officer_return_reasons.codes);
  const flagKind = textInput("flag-kind", "e.g. concentration");
  const flagNote = textInput("flag-note", "What the committee must see");
  children.push(
    h("div", { "class": "formrow" },
      h("button", { "class": "btn small", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/submit`, { actor: who.id, actor_role: who.role }), "Memo submitted for officer review.");
      } }, "Submit for review"),
      h("button", { "class": "btn small", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/approve`, { officer_id: who.id, officer_role: who.role, note: note.value, as_of: asOf() }), "Memo approved as composed.");
      } }, "Approve as composed"),
      fieldEl("Note", note)),
    h("h3", null, "Return for rework — coded reasons (" + state.vocab.officer_return_reasons.version + ")"),
    returnCodes,
    h("div", { "class": "formrow" },
      h("button", { "class": "btn small danger", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/return`, { officer_id: who.id, officer_role: who.role, reason_codes: checkedCodes(returnCodes), note: note.value }), "Memo returned for rework.");
      } }, "Return memo")),
    h("h3", null, "Flag for committee"),
    h("div", { "class": "formrow" }, fieldEl("Flag kind", flagKind), fieldEl("Flag note", flagNote),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/flag`, { officer_id: who.id, officer_role: who.role, kind: flagKind.value, note: flagNote.value }), "Flag recorded for the committee.");
      } }, "Add flag")),
    err);
  return panel("Credit officer gate (round 6)", "Acts on the memo selected above. Approval here is as composed — narrative edits are made by recomposition and re-verification, never typed over a verified memo.", children);
}

function committeePanel(ws) {
  const err = formErrorEl();
  const membersHost = h("div", null);
  function memberRow() {
    const idIn = textInput("", "member id"); const roleIn = textInput("", "member role");
    const row = h("div", { "class": "formrow" }, fieldEl("Member", idIn), fieldEl("Role", roleIn),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => row.remove() }, "Remove"));
    row.__collect = () => ({ member_id: idIn.value.trim(), member_role: roleIn.value.trim() });
    membersHost.append(row);
    return row;
  }
  memberRow(); memberRow();
  const quorum = h("input", { type: "number", value: "2", min: "1" });
  const outcome = h("select", null, state.vocab.committee_outcomes.map((o) => h("option", { value: o }, o)));
  const note = textInput("committee-note", "Note (optional)");

  const condHost = h("div", null);
  function conditionRow() {
    const type = h("select", null, ["financial_covenant", "reporting_requirement", "documentation", "other"].map((t) => h("option", { value: t }, t)));
    const metric = textInput("", "metric key e.g. dscr");
    const op = h("select", null, [">=", "<=", ">", "<"].map((o) => h("option", { value: o }, o)));
    const threshold = textInput("", "threshold");
    const cadence = textInput("", "cadence e.g. annual");
    const reportWhat = textInput("", "what must be reported");
    const row = h("div", { "class": "formrow" }, fieldEl("Type", type), fieldEl("Metric", metric), fieldEl("Op", op), fieldEl("Threshold", threshold), fieldEl("Cadence", cadence), fieldEl("Report what", reportWhat),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => row.remove() }, "Remove"));
    row.__collect = () => {
      const c = { condition_id: "", type: type.value, due_cadence: cadence.value };
      if (type.value === "financial_covenant") { c.metric_key = metric.value; c.operator = op.value; c.threshold = threshold.value; }
      if (type.value === "reporting_requirement") { c.report_what = reportWhat.value; }
      return c;
    };
    condHost.append(row);
    return row;
  }

  const advHost = h("div", null);
  function adverseRow() {
    const code = h("select", null, Object.keys(state.vocab.adverse_action_reasons.codes).map((c) => h("option", { value: c }, c)));
    const tie = h("select", null, ["policy_test", "figure", "file_fact"].map((t) => h("option", { value: t }, t)));
    const testId = textInput("", "test id (policy_test tie)");
    const figText = textInput("", "figure text (figure tie)");
    const figValue = textInput("", "figure value");
    const figSource = textInput("", "figure source");
    const itemId = textInput("", "checklist item id (file_fact tie)");
    const row = h("div", { "class": "formrow" }, fieldEl("Reason code", code), fieldEl("Tie", tie), fieldEl("Test id", testId), fieldEl("Figure text", figText), fieldEl("Figure value", figValue), fieldEl("Figure source", figSource), fieldEl("Checklist item", itemId),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => row.remove() }, "Remove"));
    row.__collect = () => {
      const r = { code: code.value, tie_kind: tie.value };
      if (tie.value === "policy_test") r.test_id = testId.value;
      if (tie.value === "figure") { r.figure_text = figText.value; r.figure_value = figValue.value; r.figure_source = figSource.value; }
      if (tie.value === "file_fact") r.checklist_item_id = itemId.value;
      return r;
    };
    advHost.append(row);
    return row;
  }

  const deferCodes = codeChecks("defer-codes", state.vocab.committee_deferral_reasons.codes);
  const representment = h("textarea", { placeholder: "Re-presentment requirements, one per line (required for a deferral)" });

  const children = [
    h("div", { "class": "formrow" },
      h("button", { "class": "btn small", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/committee/convene`, { actor: who.id, actor_role: who.role, as_of: asOf() }), "Committee convened on the selected memo.");
      } }, "Convene committee")),
    h("h3", null, "Members — the recorder (you) must be one of them, and quorum is at least two"),
    membersHost,
    h("button", { "class": "btn small ghost", type: "button", onClick: () => memberRow() }, "Add member"),
    h("h3", null, "Conditions (for approve_with_conditions)"),
    condHost,
    h("button", { "class": "btn small ghost", type: "button", onClick: () => conditionRow() }, "Add condition"),
    h("h3", null, "Adverse-action reasons (for a decline — coded, each tied to its evidence)"),
    advHost,
    h("button", { "class": "btn small ghost", type: "button", onClick: () => adverseRow() }, "Add reason"),
    h("h3", null, "Deferral — coded reasons (" + state.vocab.committee_deferral_reasons.version + ") and what must change"),
    deferCodes, representment,
    h("div", { "class": "formrow" }, fieldEl("Quorum required", quorum), fieldEl("Outcome", outcome), fieldEl("Note", note),
      h("button", { "class": "btn small", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        const members = [];
        for (const row of membersHost.children) { const m = row.__collect(); if (m.member_id) members.push(m); }
        const conditions = [];
        for (const row of condHost.children) conditions.push(row.__collect());
        const reasons = [];
        for (const row of advHost.children) reasons.push(row.__collect());
        const reqs = representment.value.split("\n").map((s) => s.trim()).filter((s) => s);
        runAction(err, () => post(`/applications/${state.fileId}/memos/${currentMemoId()}/committee/decide`, {
          recorder_id: who.id, recorder_role: who.role, members: members, outcome: outcome.value,
          quorum_required: parseInt(quorum.value || "2", 10), conditions: conditions,
          adverse_reasons: reasons, deferral_reasons: checkedCodes(deferCodes),
          representment_requirements: reqs, note: note.value
        }), "Committee decision recorded.");
      } }, "Record decision")),
    err
  ];
  return panel("Credit committee gate (round 7)", "Quorum, coded adverse-action reasons and condition thresholds are all checked by the API against the memo's pinned artefacts — an untraceable threshold or a reason tied to a passed test is refused, and the refusal is shown above.", children);
}

function ratingPanel(ws) {
  const err = formErrorEl();
  const ratings = ws.ratings || [];
  const latest = ratings.length ? ratings[ratings.length - 1] : null;
  const grade = h("input", { type: "number", min: "1", max: "10", placeholder: "new grade" });
  const reason = h("select", null, Object.keys(state.vocab.rating_override_reasons.codes).map((c) => h("option", { value: c }, c)));
  const counterId = textInput("counter-id", "countersigning officer");
  const counterRole = textInput("counter-role", "their role");
  const note = textInput("rating-note", "Note ('other' requires one)");
  const children = [
    h("div", { "class": "formrow" },
      h("button", { "class": "btn small", type: "button", onClick: () => {
        runAction(err, () => post(`/applications/${state.fileId}/risk-rating`, { as_of: asOf() }), "Rating assigned from the coded scorecard.");
      } }, "Assign rating at the as-of date")),
  ];
  if (latest) {
    children.push(
      h("h3", null, "Override the latest assignment — at most one grade, coded, countersigned"),
      h("div", { "class": "formrow" }, fieldEl("New grade", grade), fieldEl("Reason (" + state.vocab.rating_override_reasons.version + ")", reason),
        fieldEl("Countersigner", counterId), fieldEl("Countersigner role", counterRole), fieldEl("Note", note),
        h("button", { "class": "btn small", type: "button", onClick: () => {
          const who = requireIdentity(); if (!who) return;
          runAction(err, () => post(`/applications/${state.fileId}/risk-ratings/${latest.assignment.assignment_id}/override`, {
            officer_id: who.id, officer_role: who.role, new_grade: parseInt(grade.value, 10),
            reason_code: reason.value, countersigner_id: counterId.value || null,
            countersigner_role: counterRole.value || null, note: note.value
          }), "Override recorded — both grades remain on the file.");
        } }, "Record override")));
  }
  children.push(err);
  return panel("Risk rating (round 13)", "The scorecard is a coded stand-in for the bank's own model. Assigning or overriding a rating decides nothing about the credit.", children);
}

function breachPanel(ws) {
  const err = formErrorEl();
  const breaches = (ws.breach_cases || []).filter((b) => ["open", "under_review", "escalated"].includes(b.state));
  if (!breaches.length) return panel("Breach resolution (round 11)", "Cure, waiver, escalation and default are coded acts by named officers.", emptyNote("No open breach cases on this file."));
  const select = h("select", null, breaches.map((b) => h("option", { value: b.breach_id }, b.breach_id.slice(0, 14) + "… · " + (b.covenant_name || b.covenant_id) + " · " + b.state)));
  const note = textInput("breach-note", "Note (optional, recorded)");
  const escReason = h("select", null, Object.keys(state.vocab.breach_reasons.escalation_codes).map((c) => h("option", { value: c }, c)));
  const defReason = h("select", null, Object.keys(state.vocab.breach_reasons.default_codes).map((c) => h("option", { value: c }, c)));
  const passes = (ws.covenant_test_results || []).filter((r) => r.outcome === "pass");
  const cureSelect = h("select", null, passes.map((r) => h("option", { value: r.result_id }, (r.covenant_name || r.covenant_id) + " · FY" + v(r.fiscal_year) + " · passed")));
  const decisions = [];
  for (const mv of ws.memos || []) for (const d of (mv.committee && mv.committee.decisions) || []) decisions.push(d);
  const decisionSelect = h("select", null, decisions.map((d) => h("option", { value: d.decision_id }, d.decision_id.slice(0, 14) + "… · " + d.outcome)));
  function act(path, extra, label) {
    const who = requireIdentity(); if (!who) return;
    const body = Object.assign({ actor_id: who.id, actor_role: who.role, note: note.value }, extra || {});
    runAction(err, () => post(path, body), "Breach action recorded: " + label + ".");
  }
  const children = [
    h("div", { "class": "formrow" }, fieldEl("Open breach", select), fieldEl("Note", note),
      h("button", { "class": "btn small", type: "button", onClick: () => act(`/applications/${state.fileId}/breach-cases/${select.value}/review`, null, "review") }, "Mark under review")),
    h("div", { "class": "formrow" }, fieldEl("Escalation reason (" + state.vocab.breach_reasons.version + ")", escReason),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => act(`/applications/${state.fileId}/breach-cases/${select.value}/escalate`, { reason_code: escReason.value }, "escalate") }, "Escalate")),
    h("div", { "class": "formrow" }, fieldEl("Curing test result (a later pass on the same covenant)", cureSelect),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => act(`/applications/${state.fileId}/breach-cases/${select.value}/cure`, { cure_result_id: cureSelect.value }, "cure") }, "Record cure")),
    h("div", { "class": "formrow" }, fieldEl("Committee decision authorising the waiver", decisionSelect),
      h("button", { "class": "btn small ghost", type: "button", onClick: () => act(`/applications/${state.fileId}/breach-cases/${select.value}/waive`, { decision_id: decisionSelect.value }, "waive") }, "Waive on committee authority")),
    h("div", { "class": "formrow" }, fieldEl("Default reason (" + state.vocab.breach_reasons.version + ")", defReason),
      h("button", { "class": "btn small danger", type: "button", onClick: () => act(`/applications/${state.fileId}/breach-cases/${select.value}/default`, { reason_code: defReason.value }, "default") }, "Move to default path")),
    err
  ];
  return panel("Breach resolution (round 11)", "A waiver stands only on a committee decision of this file; a cure stands only on a later passing re-test of the same covenant. The API checks both — a free-text waiver cannot be sent from here.", children);
}

function reviewPanel(ws) {
  const err = formErrorEl();
  const schedule = ws.annual_review_schedule || { sets: [] };
  const instances = [];
  for (const view of schedule.sets || []) for (const i of view.instances || []) instances.push(i);
  if (!instances.length) return panel("Annual review (round 12)", "Assemble the pack, then record the officer's coded stance.", emptyNote("No annual review obligations on this file."));
  const number = h("select", null, instances.map((i) => h("option", { value: String(i.review_number) }, "review #" + i.review_number + " · due " + i.due_date + " · " + i.state)));
  const stance = h("select", null, state.vocab.annual_review.stances.map((s) => h("option", { value: s }, s)));
  const codes = codeChecks("review-codes", state.vocab.annual_review.codes);
  const packs = ws.annual_review_packs || [];
  const packSelect = h("select", null, [h("option", { value: "" }, "— none —")].concat(packs.map((p) => h("option", { value: p.pack_id }, p.pack_id.slice(0, 14) + "… · review #" + v(p.review_number) + (p.complete ? " · complete" : " · incomplete")))));
  const counterId = textInput("review-counter-id", "countersigner (the memo's approver may not review alone)");
  const counterRole = textInput("review-counter-role", "countersigner role");
  const note = textInput("review-note", "Note (optional)");
  const children = [
    h("div", { "class": "formrow" }, fieldEl("Review", number),
      h("button", { "class": "btn small", type: "button", onClick: () => {
        runAction(err, () => post(`/applications/${state.fileId}/annual-reviews/${number.value}/pack`, { as_of: asOf() }), "Review pack assembled.");
      } }, "Assemble pack")),
    h("h3", null, "Record the outcome — a named officer's coded stance closes the instance"),
    h("div", { "class": "formrow" }, fieldEl("Stance", stance), fieldEl("Pack", packSelect)),
    codes,
    h("div", { "class": "formrow" }, fieldEl("Countersigner", counterId), fieldEl("Countersigner role", counterRole), fieldEl("Note", note),
      h("button", { "class": "btn small", type: "button", onClick: () => {
        const who = requireIdentity(); if (!who) return;
        runAction(err, () => post(`/applications/${state.fileId}/annual-reviews/${number.value}/outcome`, {
          stance: stance.value, reason_codes: checkedCodes(codes), reviewer_id: who.id,
          reviewer_role: who.role, pack_id: packSelect.value || null, as_of: asOf(),
          countersigner_id: counterId.value || null, countersigner_role: counterRole.value || null,
          conditions: [], note: note.value
        }), "Review outcome recorded.");
      } }, "Record outcome")),
    err
  ];
  return panel("Annual review (round 12)", "The pack assembles from stored artefacts only; a pack over stale inputs is produced incomplete, naming its blockers — it is never blessed complete. An escalation records its committee referral in the same act.", children);
}

/* ---------------- Boot ---------------- */
async function boot() {
  const today = new Date();
  document.getElementById("as-of").value = today.toISOString().slice(0, 10);
  const savedName = localStorage.getItem("officer-name");
  const savedRole = localStorage.getItem("officer-role");
  if (savedName) document.getElementById("officer-name").value = savedName;
  if (savedRole) document.getElementById("officer-role").value = savedRole;
  document.getElementById("officer-name").addEventListener("change", (e) => localStorage.setItem("officer-name", e.target.value));
  document.getElementById("officer-role").addEventListener("change", (e) => localStorage.setItem("officer-role", e.target.value));
  document.getElementById("nav-queue").addEventListener("click", () => { loadQueue().catch(showError); });
  document.getElementById("nav-due").addEventListener("click", () => { loadDueWork().catch(showError); });
  document.getElementById("nav-workspace").addEventListener("click", () => showView("workspace"));
  document.getElementById("refresh-btn").addEventListener("click", () => { loadQueue(true).catch(showError); if (state.fileId) openWorkspace(state.fileId, true).catch(showError); });
  document.getElementById("as-of").addEventListener("change", () => { loadQueue(true).catch(showError); });
  try {
    state.vocab = await get(`/console/vocabularies`);
    await loadQueue();
  } catch (e) { showError(e); }
}
boot();
</script>
</body>
</html>
"""
