/* Workforce Role Change Observatory - dashboard.
   Reads data/summary.json (written daily by scripts/build_dashboard_data.py).
   No external libraries. All text from data is inserted with textContent,
   never as HTML, because advert text is untrusted. */
"use strict";

const NATIONS = ["England", "Scotland", "Wales", "Northern Ireland"];
const NATION_VAR = { "England": "--england", "Scotland": "--scotland", "Wales": "--wales",
                     "Northern Ireland": "--ni", "UNKNOWN": "--unknown" };
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function pct(x, digits = 1) { return x === null || x === undefined ? "–" : (x * 100).toFixed(digits) + "%"; }
function num(x) { return x === null || x === undefined ? "–" : Number(x).toLocaleString("en-GB"); }
function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "unknown";
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
}
function colour(nation) { return `var(${NATION_VAR[nation] || "--unknown"})`; }
function safeUrl(u) { return typeof u === "string" && /^https:\/\//.test(u) ? u : null; }
function empty(container, message) {
  container.replaceChildren(el("p", { class: "empty" }, message));
}

// 95% Wilson interval, matching scripts/detect_changes.py
function wilson(k, n, z = 1.96) {
  if (!n) return [null, null];
  const p = k / n, d = 1 + z * z / n;
  const c = (p + z * z / (2 * n)) / d;
  const h = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}

function renderStatus(s) {
  const demand = s.sources.filter(x => x.role === "demand");
  const live = demand.filter(x => x.enabled && x.endpoint_configured);
  const status = document.getElementById("status");
  if (!live.length) {
    status.textContent = "No recruitment source is switched on yet. Each source stays off until its " +
      "official feed documentation has been checked and entered in config/sources.yaml.";
  } else {
    status.textContent = `Collecting from ${live.length} of ${demand.length} recruitment sources. ` +
      `${num(s.totals.vacancy_clusters)} distinct advertised vacancies observed so far.`;
  }
  document.getElementById("caveat").textContent = s.caveat + ` Last updated ${when(s.generated_at_utc)}.`;
}

function renderIntervals(s) {
  const box = document.getElementById("ai-intervals");
  const data = s.ai.by_nation_all_time || {};
  const rows = NATIONS.map(n => ({ nation: n, ...(data[n] || { n: 0 }) }));
  if (!rows.some(r => r.n)) return empty(box, "No adverts have been collected yet.");
  const W = Math.max(320, Math.round(box.clientWidth || 720));
  const narrow = W < 560;
  const rowH = narrow ? 64 : 46, left = narrow ? 0 : 150, right = 110, top = 28;
  const H = top + rows.length * rowH + 10;
  const shown = rows.filter(r => !r.suppressed_small_n && r.n);
  const maxHi = Math.max(0.05, ...shown.map(r => wilson(r.n_ai, r.n)[1]));
  const step = maxHi > 0.4 ? 0.2 : maxHi > 0.2 ? 0.1 : 0.05;
  const xMax = Math.min(1, Math.ceil(maxHi / step) * step);
  const x = v => left + (v / xMax) * (W - left - right);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", style: "font-size:15px",
    "aria-label": "Share of adverts with AI content in role text, by nation, with 95% intervals" });
  const axis = svg("g", { class: "axis" });
  for (let i = 0; i * step <= xMax + 1e-9; i++) {
    const t = i * step;
    axis.append(svg("line", { x1: x(t), x2: x(t), y1: top - 8, y2: H - 6 }));
    axis.append(svg("text", { x: x(t), y: top - 12, "text-anchor": "middle" }, pct(t, 0)));
  }
  g.append(axis);
  rows.forEach((r, i) => {
    const y = top + i * rowH + (narrow ? rowH * 0.62 : rowH / 2);
    g.append(svg("text", { x: 0, y: narrow ? y - 16 : y + 5, style: "font-weight:700" }, r.nation));
    if (!r.n || r.suppressed_small_n) {
      g.append(svg("text", { x: narrow ? 0 : left, y: y + 5, style: "fill: var(--ink-soft)" },
        r.n ? `${r.n} adverts: too few to report (minimum ${s.ai.min_cell_size})` : "no adverts yet"));
      return;
    }
    const [lo, hi] = wilson(r.n_ai, r.n);
    const c = colour(r.nation);
    const grp = svg("g", { class: "interval" });
    grp.append(svg("line", { x1: x(lo), x2: x(hi), y1: y, y2: y, style: `stroke:${c};stroke-width:6;stroke-linecap:round;opacity:.35` }));
    grp.append(svg("circle", { cx: x(r.share), cy: y, r: 7, style: `fill:${c}` }));
    grp.append(svg("text", { x: W - right + 12, y: y + 5, style: "fill: var(--ink-soft)" },
      `${pct(r.share)} of ${num(r.n)}`));
    grp.append(svg("title", {}, `${r.nation}: ${pct(r.share)} (95% interval ${pct(lo)} to ${pct(hi)}), ${r.n_ai} of ${r.n} adverts`));
    g.append(grp);
  });
  box.replaceChildren(g);
}

function renderTrend(s) {
  const box = document.getElementById("trend");
  const series = s.ai.monthly_series || {};
  const figures = [];
  for (const nation of NATIONS) {
    const pts = (series[nation] || []).filter(p => !p.suppressed_small_n && p.share_ai_role_content !== null);
    const fig = el("figure");
    fig.append(el("figcaption", {}, nation));
    if (pts.length < 2) {
      fig.append(el("p", { class: "empty" }, pts.length ? "One reportable month so far." : "Not enough data yet."));
      figures.push(fig);
      continue;
    }
    const W = 320, H = 150, L = 36, R = 8, T = 8, B = 22;
    const yMax = Math.min(1, Math.max(0.1, ...pts.map(p => p.ci95_high)));
    const x = i => L + (i / (pts.length - 1)) * (W - L - R);
    const y = v => T + (1 - v / yMax) * (H - T - B);
    const c = colour(nation);
    const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", style: "font-size:11px",
      "aria-label": `${nation}: monthly share of adverts with AI content in role text` });
    const axis = svg("g", { class: "axis" });
    axis.append(svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0) }));
    axis.append(svg("text", { x: L - 4, y: y(yMax) + 4, "text-anchor": "end" }, pct(yMax, 0)));
    axis.append(svg("text", { x: L - 4, y: y(0) + 4, "text-anchor": "end" }, "0%"));
    axis.append(svg("text", { x: L, y: H - 4 }, pts[0].month));
    axis.append(svg("text", { x: W - R, y: H - 4, "text-anchor": "end" }, pts[pts.length - 1].month));
    g.append(axis);
    const band = pts.map((p, i) => `${x(i)},${y(p.ci95_high)}`).concat(
      pts.slice().reverse().map((p, j) => `${x(pts.length - 1 - j)},${y(p.ci95_low)}`)).join(" ");
    g.append(svg("polygon", { points: band, style: `fill:${c};opacity:.18` }));
    g.append(svg("polyline", { points: pts.map((p, i) => `${x(i)},${y(p.share_ai_role_content)}`).join(" "),
      style: `fill:none;stroke:${c};stroke-width:2.5` }));
    pts.forEach((p, i) => {
      const dot = svg("circle", { cx: x(i), cy: y(p.share_ai_role_content), r: 3, style: `fill:${c}` });
      dot.append(svg("title", {}, `${p.month}: ${pct(p.share_ai_role_content)} of ${p.n_vacancy_clusters}`));
      g.append(dot);
    });
    fig.append(g);
    figures.push(fig);
  }
  box.replaceChildren(...figures);
}

function renderChanges(s) {
  const sig = document.getElementById("signals");
  const signals = s.ai.change_signals || [];
  if (signals.length) {
    const t = el("table", { class: "data" });
    const head = el("tr");
    ["Nation", "Role family", "Month", "Share", "Baseline", "Direction"].forEach(h => head.append(el("th", {}, h)));
    t.append(head);
    signals.forEach(x => {
      const tr = el("tr");
      [x.nation, x.role_family, x.month, `${pct(x.share)} (n=${x.n})`,
       `${pct(x.baseline_share)} (n=${x.baseline_n})`, x.direction].forEach(v => tr.append(el("td", {}, v)));
      t.append(tr);
    });
    sig.replaceChildren(el("h3", {}, "Statistically distinct months"), t);
  } else {
    sig.replaceChildren(el("p", { class: "lede" },
      "No month yet differs clearly from its baseline once uncertainty is taken into account."));
  }
  const list = document.getElementById("examples");
  const ex = s.ai.role_change_examples || [];
  if (!ex.length) {
    list.replaceChildren(el("li", { class: "empty" }, "No re-advertised roles with changed content yet."));
    return;
  }
  list.replaceChildren(...ex.slice(0, 20).map(p => {
    const li = el("li");
    li.append(el("p", { class: "meta" },
      `${p.employer || "Unknown employer"}, ${p.nation}. First advert ${p.first_seen_a}, later advert ${p.first_seen_b}.`));
    li.append(el("strong", {}, p.title_b || p.title_a || "Untitled"));
    p.ai_sentences_added.forEach(q => li.append(el("blockquote", {}, q)));
    p.ai_sentences_removed.forEach(q => li.append(el("blockquote", { class: "removed" }, q)));
    const tasks = [];
    if (p.task_categories_added.length) tasks.push("Tasks added: " + p.task_categories_added.join(", "));
    if (p.task_categories_removed.length) tasks.push("Tasks removed: " + p.task_categories_removed.join(", "));
    if (tasks.length) li.append(el("p", {}, tasks.join(". ")));
    const links = el("p");
    [["Earlier advert", p.url_a], ["Later advert", p.url_b]].forEach(([label, u], i) => {
      const href = safeUrl(u);
      if (i) links.append(document.createTextNode(" and "));
      links.append(href ? el("a", { href, rel: "noopener" }, label) : el("span", {}, label + " (no link)"));
    });
    li.append(links);
    return li;
  }));
}

function renderTerms(s) {
  const list = document.getElementById("terms");
  const items = (s.ai.term_emergence || []).filter(t => t.nation === "ALL");
  if (!items.length) {
    list.replaceChildren(el("li", { class: "empty" }, "No AI terms found in role text yet."));
    return;
  }
  list.replaceChildren(...items.map(t => {
    const li = el("li");
    li.append(el("span", { class: "month" }, t.first_month));
    const href = safeUrl(t.record_url);
    li.append(href ? el("a", { href, rel: "noopener", title: t.sentence }, t.evidence) : el("span", {}, t.evidence));
    li.append(document.createTextNode(` (${t.category.replace(/_/g, " ")})`));
    return li;
  }));
}

function rowOf(...cells) {
  const tr = el("tr");
  tr.append(...cells);
  return tr;
}

function table(id, header, rows) {
  const t = document.getElementById(id);
  const head = el("tr");
  header.forEach(h => head.append(el("th", {}, h)));
  t.replaceChildren(head, ...rows);
}

function renderCounts(s) {
  const byN = s.totals.clusters_by_nation || {};
  const max = Math.max(1, ...Object.values(byN));
  table("nations", ["Nation", "Adverts"], [...NATIONS, "UNKNOWN"].map(n => {
    const tr = el("tr");
    tr.append(el("td", {}, n === "UNKNOWN" ? "Nation not determined" : n));
    const td = el("td", { class: "num" }, num(byN[n] || 0));
    const bar = el("span", { class: "bar" });
    bar.style.width = `${((byN[n] || 0) / max) * 6}rem`;
    bar.style.background = colour(n);
    td.append(bar);
    tr.append(td);
    return tr;
  }));
  const fams = Object.entries(s.role_families || {});
  table("families", ["Role family", "Adverts"], fams.length ? fams.map(([k, v]) => {
    const tr = el("tr");
    tr.append(el("td", {}, k === "UNKNOWN" ? "Not determined (insufficient evidence)" :
      k === "AMBIGUOUS" ? "Ambiguous evidence" : k.replace(/_/g, " ")));
    tr.append(el("td", { class: "num" }, num(v)));
    return tr;
  }) : [rowOf(el("td", { colspan: 2 }, "None yet"))]);
}

function renderSources(s) {
  table("sources", ["Source", "Type", "Nations", "State", "Last result"], s.sources.map(x => {
    const tr = el("tr");
    tr.append(el("td", {}, x.name));
    tr.append(el("td", {}, { demand: "Job adverts", stock: "Workforce statistics", context: "Historical context" }[x.role] || x.role));
    tr.append(el("td", {}, (x.nation_scope || []).join(", ")));
    const state = x.enabled && x.endpoint_configured ? "On" : "Off: awaiting documented feed";
    const stateCell = el("td");
    stateCell.append(el("span", { class: "pill " + (x.enabled ? "ok" : "blocked") }, state));
    tr.append(stateCell);
    const st = x.last_status ? `${x.last_status}, ${when(x.last_run_utc)}` : "Not run";
    const cell = el("td", {}, st);
    if (x.last_message) cell.title = x.last_message;
    tr.append(cell);
    return tr;
  }));
}

function facts(id, pairs) {
  const dl = document.getElementById(id);
  dl.replaceChildren(...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

function renderQuality(s) {
  const q = s.quality;
  facts("quality", [
    ["Observations stored", num(s.totals.observations)],
    ["Distinct vacancies", num(s.totals.vacancy_clusters)],
    ["Cross-posted vacancies merged", num(s.totals.cross_posted_clusters)],
    ["Nation not determined", pct(q.share_nation_unknown)],
    ["Role family not determined", pct(q.share_role_family_unknown_or_ambiguous)],
    ["Refused: title-only evidence", num(q.title_only_refusals)],
    ["Last QA run", q.last_qa_run_id || "None yet"],
    ["Failing QA checks", q.last_qa_failures.length ? q.last_qa_failures.map(f => f.check_name).join(", ") : "None"],
    ["GenAI records", `${num(s.genai.records)}`],
  ]);
  facts("versions", Object.entries(s.versions).map(([k, v]) =>
    [k.replace(/_/g, " "), k === "git_sha" ? String(v).slice(0, 10) : v]));
}

async function main() {
  let s;
  try {
    const res = await fetch("data/summary.json", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    s = await res.json();
  } catch (e) {
    document.getElementById("status").textContent =
      "No data has been published yet. The daily workflow creates data/summary.json after its first successful run.";
    return;
  }
  const steps = [renderStatus, renderIntervals, renderTrend, renderChanges, renderTerms,
                 renderCounts, renderSources, renderQuality];
  for (const step of steps) {
    try { step(s); } catch (e) { console.error(step.name, e); }
  }
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => renderIntervals(s), 150);
  });
}

main();
