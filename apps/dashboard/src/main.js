import "./styles.css";
import {
  resolveProxyConfig,
  pingProxy,
  fetchMetrics,
  fetchDecisions,
  fetchApprovals,
  postApprovalDecision,
  postToolCall,
  POLL_MS,
} from "./api.js";

// ---- Indian rupee formatting (amounts held in paise, like the backend) ----
function inr(paise) {
  const r = Math.round(paise / 100);
  const s = String(r);
  if (s.length <= 3) return "₹" + s;
  const last3 = s.slice(-3);
  const rest = s.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return "₹" + rest + "," + last3;
}

// ---- state ----
const M = {
  attempted: 0, moved: 0, allowed: 0, denied: 0, escalated: 0,
  falseBlocks: 0, pending: 0, resolved: 0,
  latencies: [],
};
let events = [];      // feed rows (newest first)
let queue = [];        // pending HITL items
let running = false;
let seq = 0;

const AGENT = "support-agent-1";

// LIVE/SIMULATED mode
const MODE = { live: false, proxyUrl: null };
let pollTimer = null;
const seenDecisionIds = new Set();

const rand = (a, b) => a + Math.floor((Math.abs(Math.sin(seq++ * 12.9898)) * 43758.5453 % 1) * (b - a));

// ---- counters render ----
function p95(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(s.length * 0.95))];
}
function renderCounters(overrideP95) {
  const p95Val = overrideP95 != null ? Number(overrideP95) : p95(M.latencies);
  const cards = [
    { label: "₹ attempted", value: inr(M.attempted), sub: M.allowed + M.denied + M.escalated + " calls", cls: "" },
    { label: "₹ actually moved", value: inr(M.moved), sub: "blocked: " + inr(M.attempted - M.moved), cls: "" },
    { label: "legit passed", value: M.allowed, sub: "zero false blocks = precision", cls: "good" },
    { label: "false blocks", value: M.falseBlocks, sub: M.falseBlocks === 0 ? "clean" : "REVIEW", cls: M.falseBlocks === 0 ? "zero" : "warn" },
    { label: "escalations", value: M.pending + " / " + M.resolved, sub: "pending / resolved", cls: M.pending ? "warn" : "" },
    { label: "p95 proxy overhead", value: p95Val.toFixed(1), sub: "ms · hot-path tax", cls: "" },
  ];
  document.getElementById("counters").innerHTML = cards.map(c =>
    `<div class="stat ${c.cls}"><div class="label">${c.label}</div><div class="value">${c.value}</div><div class="sub">${c.sub}</div></div>`
  ).join("");
}

// ---- feed render ----
function stamp() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0");
}
function pushEvent(e) {
  events.unshift(e);
  const feed = document.getElementById("feed");
  if (events.length === 1) feed.innerHTML = "";
  const amt = e.amount != null ? inr(e.amount) : "—";
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML =
    `<div class="t">${e.t}</div>` +
    `<div class="agent">${e.agent}</div>` +
    `<div class="tool">${e.tool}</div>` +
    `<div class="amt">${amt}</div>` +
    `<div><span class="verdict ${e.cls}">${e.verdict}</span></div>` +
    `<div class="reason">${e.reason} <span class="pid">[${e.policy}] ${e.ms}ms</span></div>`;
  feed.prepend(row);
  document.getElementById("feedCount").textContent = events.length + " events";
}

// ---- queue render ----
function renderQueue() {
  const q = document.getElementById("queue");
  document.getElementById("qCount").textContent = queue.length + " pending";
  if (!queue.length) { q.innerHTML = '<div class="empty">No escalations. Actions that need a second pair of eyes land here.</div>'; return; }
  q.innerHTML = queue.map(item =>
    `<div class="qcard"><div class="qtool">${item.tool}</div>` +
    `<div class="qamt">${inr(item.amount)}</div>` +
    `<div class="qwhy">${item.why}</div>` +
    `<div class="acts"><button class="ok" onclick="resolve('${item.id}',true)">approve</button>` +
    `<button class="no" onclick="resolve('${item.id}',false)">deny</button></div></div>`
  ).join("");
}
function enqueue(item) { queue.push(item); M.pending++; renderQueue(); renderCounters(); }

// simulated resolve (unchanged behavior from the original inline script)
function resolveLocal(id, ok) {
  const i = queue.findIndex(x => x.id === id);
  if (i < 0) return;
  const item = queue[i];
  queue.splice(i, 1);
  M.pending--; M.resolved++;
  const ms = (rand(2, 6) + Math.random()).toFixed(1);
  if (ok) {
    M.moved += item.amount; M.allowed++;
    pushEvent({ t: stamp(), agent: item.agent, tool: item.tool, amount: item.amount, verdict: "executed", cls: "allow", policy: "hitl_approved", reason: "human approved → executed downstream on Razorpay test rails", ms });
  } else {
    M.denied++;
    pushEvent({ t: stamp(), agent: item.agent, tool: item.tool, amount: item.amount, verdict: "denied", cls: "deny", policy: "hitl_denied", reason: "human denied — action never reached the API", ms });
  }
  M.latencies.push(parseFloat(ms));
  renderQueue(); renderCounters();
}

// live resolve — POST to the real proxy, then refresh from it (CONTRACTS.md §1)
async function resolveLive(id, ok) {
  try {
    await postApprovalDecision(MODE.proxyUrl, id, ok);
  } catch (err) {
    console.warn("[dashboard] approval action failed:", err);
  } finally {
    await refreshLiveData();
  }
}

// single global entry point used by the onclick handlers rendered above
window.resolve = function (id, ok) {
  if (MODE.live) resolveLive(id, ok);
  else resolveLocal(id, ok);
};

// ---- decision helpers (mirror the intended proxy behavior, simulated mode) ----
function emit(ev) {
  const ms = (rand(2, 7) + Math.random()).toFixed(2);
  ev.ms = ms; ev.t = stamp();
  M.latencies.push(parseFloat(ms));
  if (ev.amount != null && ev.countAttempt !== false) M.attempted += ev.amount;
  if (ev.cls === "allow") { M.allowed++; if (ev.moves !== false && ev.amount != null) M.moved += ev.amount; }
  else if (ev.cls === "deny") { M.denied++; }
  else if (ev.cls === "escalate") { M.escalated++; }
  pushEvent(ev);
  renderCounters();
}
const wait = ms => new Promise(r => setTimeout(r, ms));
async function busy(on) {
  running = on;
  document.querySelectorAll("button.scn").forEach(b => { if (!b.classList.contains("reset")) b.disabled = on; });
}

// ---- scenario op lists (shared between SIMULATED narration and LIVE POSTs) ----
const CLEAN_OPS = [
  ["issue_refund", 120000, "cust_ravi@oksbi", "refund ≤ cap, known payee, trusted source"],
  ["create_payment_link", 499900, null, "invoice link, within limits"],
  ["list_orders", null, null, "read-only fetch"],
  ["pay_vendor", 850000, "acme_supplies", "known vendor, allowlisted"],
  ["issue_refund", 45000, "cust_meena@okhdfcbank", "small refund, known payee"],
  ["create_payment_link", 250000, null, "link within cap"],
  ["issue_refund", 300000, "cust_arjun@okaxis", "refund ≤ cap"],
  ["list_orders", null, null, "read-only fetch"],
  ["pay_vendor", 1200000, "logistics_co", "known vendor"],
  ["create_payment_link", 99900, null, "link within cap"],
  ["issue_refund", 78000, "cust_ravi@oksbi", "repeat known payee"],
  ["list_orders", null, null, "read-only fetch"],
  ["pay_vendor", 640000, "acme_supplies", "known vendor"],
  ["issue_refund", 150000, "cust_neha@okicici", "refund ≤ cap"],
  ["create_payment_link", 1750000, null, "large but within link cap"],
  ["issue_refund", 32000, "cust_meena@okhdfcbank", "small refund"],
  ["pay_vendor", 410000, "logistics_co", "known vendor"],
  ["list_orders", null, null, "read-only fetch"],
  ["create_payment_link", 305000, null, "link within cap"],
  ["issue_refund", 96000, "cust_arjun@okaxis", "known payee"],
];

// ---- scenarios: SIMULATED narration (unchanged from the original inline script) ----
async function scnClean() {
  for (const [tool, amount, payee, why] of CLEAN_OPS) {
    emit({ agent: AGENT, tool, amount, verdict: "allow", cls: "allow", policy: "pass", reason: (payee ? payee + " · " : "") + why });
    await wait(140);
  }
}
async function scnInjection() {
  emit({ agent: AGENT, tool: "get_ticket", amount: null, verdict: "allow", cls: "allow", policy: "pass", reason: "ticket #4471 fetched (customer email body is untrusted data)" });
  await wait(600);
  emit({ agent: AGENT, tool: "issue_refund", amount: 6500000, verdict: "deny", cls: "deny", policy: "provenance_check",
    reason: "payee <b>attacker@ybl</b> extracted from untrusted ticket body — payment instruction from untrusted source", moves: false });
}
async function scnStructuring() {
  emit({ agent: AGENT, tool: "issue_refund", amount: 20000000, verdict: "deny", cls: "deny", policy: "per_call_amount_cap",
    reason: "₹2,00,000 exceeds per-call refund ceiling of ₹50,000", moves: false });
  await wait(700);
  let sum = 0;
  const CAP = 4000000;      // ₹40,000 per call — passes per-call cap
  const WINDOW_THRESHOLD = 15000000; // ₹1,50,000 rolling window
  for (let i = 1; i <= 5; i++) {
    sum += CAP;
    await wait(520);
    if (sum >= WINDOW_THRESHOLD) {
      emit({ agent: AGENT, tool: "issue_refund", amount: CAP, verdict: "deny", cls: "deny", policy: "velocity_aggregation",
        reason: `call #${i}: ₹40,000 passes per-call cap, but rolling-window sum ${inr(sum)} to cust_ravi@oksbi crosses ₹1,50,000 → <b>tool frozen</b> for ${AGENT}`, moves: false });
      await wait(400);
      emit({ agent: AGENT, tool: "issue_refund", amount: null, verdict: "escalate", cls: "escalate", policy: "velocity_aggregation",
        reason: "structuring pattern detected — unfreeze escalated to human", amount: CAP, countAttempt: false });
      enqueue({ id: "esc" + Date.now(), agent: AGENT, tool: "issue_refund", amount: sum, why: "5×₹40k in 4 min to same payee — rolling window crossed ₹1,50,000; unfreeze needs human approval" });
      await wait(500);
      emit({ agent: AGENT, tool: "issue_refund", amount: CAP, verdict: "frozen", cls: "frozen", policy: "velocity_aggregation",
        reason: "auto-denied — tool is frozen pending human unfreeze", moves: false });
      break;
    } else {
      emit({ agent: AGENT, tool: "issue_refund", amount: CAP, verdict: "allow", cls: "allow", policy: "per_call_amount_cap",
        reason: `call #${i}: ₹40,000 under per-call cap · rolling sum now ${inr(sum)}` });
    }
  }
}
async function scnPayeeSwap() {
  emit({ agent: AGENT, tool: "pay_vendor", amount: 750000, verdict: "allow", cls: "allow", policy: "payee_allowlist", reason: "acme_supplies — known allowlisted vendor" });
  await wait(600);
  emit({ agent: AGENT, tool: "pay_vendor", amount: 750000, verdict: "escalate", cls: "escalate", policy: "payee_allowlist",
    reason: "payee changed mid-flow to <b>unknown a/c 5029-XXXX</b> — new payee needs a second pair of eyes", moves: false });
  enqueue({ id: "esc" + Date.now(), agent: AGENT, tool: "pay_vendor", amount: 750000, why: "payee switched to an unknown account not on the allowlist" });
}
async function scnFlood() {
  let n = 0;
  for (let i = 1; i <= 8; i++) {
    n += 900000;
    if (i < 6) { emit({ agent: AGENT, tool: "pay_vendor", amount: 900000, verdict: "allow", cls: "allow", policy: "velocity_aggregation", reason: `payout #${i} · ₹9,000 · window sum ${inr(n)}` }); }
    else { emit({ agent: AGENT, tool: "pay_vendor", amount: 900000, verdict: "deny", cls: "deny", policy: "velocity_aggregation", reason: `payout #${i}: rate over ${inr(n)}/window exceeds payout velocity limit`, moves: false }); }
    await wait(280);
  }
}
const SCN = { clean: scnClean, injection: scnInjection, structuring: scnStructuring, payeeswap: scnPayeeSwap, flood: scnFlood };

function resetAll() {
  Object.assign(M, { attempted: 0, moved: 0, allowed: 0, denied: 0, escalated: 0, falseBlocks: 0, pending: 0, resolved: 0, latencies: [] });
  events = []; queue = []; seq = 0;
  document.getElementById("feed").innerHTML = '<div class="empty">Press a scenario above — every tool call the agent attempts is intercepted and evaluated here before it reaches Razorpay.</div>';
  document.getElementById("feedCount").textContent = "0 events";
  renderQueue(); renderCounters();
}

// ---- scenarios: LIVE mode — POST a scripted sequence to {proxyUrl}/tool-call ----
// (CONTRACTS.md §1). Real decisions/reasons come back from the actual policy
// engine, so — unlike the simulated narration above — these don't hardcode a
// verdict; refreshLiveData() after each call pulls the true result into the feed.
function buildToolCall(tool, amount, payee, extra = {}) {
  const args = {};
  if (amount != null) args.amount = amount;
  if (payee) args.payee = payee;
  Object.assign(args, extra.arguments || {});
  const context = {};
  if (payee) context.payee = payee;
  if (extra.provenance) context.provenance = extra.provenance;
  const payload = { agent_id: AGENT, tool, arguments: args, context };
  if (extra.meta) payload.meta = extra.meta;
  return payload;
}
const LIVE_OPS = {
  clean: CLEAN_OPS.map(([tool, amount, payee]) => buildToolCall(tool, amount, payee, { meta: { labeled_legit: true } })),
  injection: [
    buildToolCall("get_ticket", null, null, { arguments: { ticket_id: "4471" } }),
    buildToolCall("issue_refund", 6500000, "attacker@ybl", {
      provenance: [{ source: "ticket:4471", trusted: false, tainted_fields: ["arguments.payee"] }],
    }),
  ],
  structuring: [
    buildToolCall("issue_refund", 20000000, "cust_ravi@oksbi"),
    ...Array.from({ length: 5 }, () => buildToolCall("issue_refund", 4000000, "cust_ravi@oksbi")),
  ],
  payeeswap: [
    buildToolCall("pay_vendor", 750000, "acme_supplies"),
    buildToolCall("pay_vendor", 750000, "unknown_acct_5029"),
  ],
  flood: Array.from({ length: 8 }, () => buildToolCall("pay_vendor", 900000, "logistics_co")),
};
async function runScenarioLive(name) {
  const ops = LIVE_OPS[name];
  if (!ops) return;
  for (const payload of ops) {
    try { await postToolCall(MODE.proxyUrl, payload); }
    catch (err) { console.warn("[dashboard] tool-call failed:", err); }
    await refreshLiveData();
    await wait(250);
  }
}

document.querySelectorAll("button.scn").forEach(btn => {
  btn.addEventListener("click", async () => {
    const s = btn.dataset.scn;
    if (s === "reset") {
      if (MODE.live) {
        seenDecisionIds.clear();
        events = [];
        document.getElementById("feed").innerHTML = '<div class="empty">Press a scenario above — every tool call the agent attempts is intercepted and evaluated here before it reaches Razorpay.</div>';
        document.getElementById("feedCount").textContent = "0 events";
        refreshLiveData();
      } else {
        resetAll();
      }
      return;
    }
    if (running) return;
    await busy(true);
    try {
      if (MODE.live && LIVE_OPS[s]) await runScenarioLive(s);
      else await SCN[s]();
    } finally { await busy(false); }
  });
});

// ---- LIVE mode: connect to the enforcement proxy, else stay SIMULATED ----
function setBadge(mode, text) {
  document.getElementById("modeDot").className = mode === "live" ? "dot live" : "dot sim";
  document.getElementById("modeText").textContent = text;
}

function pick(obj, keys, fallback = null) {
  for (const k of keys) {
    if (obj && obj[k] != null) return obj[k];
  }
  return fallback;
}
function normalizeMetrics(raw) {
  // Field names per proxy/metrics.py snapshot(). GET /metrics returns money in
  // RUPEES (rupees_attempted/rupees_moved — the one display-layer exception to
  // paise-everywhere); these counters work in paise, so multiply back by 100.
  return {
    attempted: Math.round(pick(raw, ["rupees_attempted", "attempted_paise"], 0) * 100),
    moved: Math.round(pick(raw, ["rupees_moved", "moved_paise"], 0) * 100),
    allowed: pick(raw, ["calls_allowed", "allowed"], 0),
    denied: pick(raw, ["calls_denied", "denied"], 0),
    escalated: pick(raw, ["calls_escalated", "escalated"], 0),
    falseBlocks: pick(raw, ["false_blocks", "falseBlocks"], 0),
    pending: pick(raw, ["approvals_pending", "pending"], 0),
    resolved: pick(raw, ["approvals_resolved", "resolved"], 0),
    p95: pick(raw, ["p95_overhead_ms", "p95_ms", "p95"], 0),
  };
}
function normalizeDecision(raw) {
  const decision = raw.decision || raw.verdict || "allow";
  const clsMap = { allow: "allow", deny: "deny", escalate: "escalate", frozen: "frozen" };
  const ts = raw.ts || raw.created_at || raw.timestamp;
  const t = ts ? new Date(Number(ts) * (String(ts).length <= 10 ? 1000 : 1)).toLocaleTimeString("en-GB") : stamp();
  const amount = pick(raw, ["amount_paise", "amount"], (raw.arguments && raw.arguments.amount) ?? null);
  return {
    id: String(raw.request_id ?? raw.seq ?? raw.id ?? `${raw.agent_id || ""}-${raw.tool || ""}-${ts || Math.random()}`),
    t,
    agent: raw.agent_id || raw.agent || AGENT,
    tool: raw.tool || "—",
    amount,
    verdict: decision,
    cls: clsMap[decision] || "allow",
    policy: raw.policy_id || raw.policy || "—",
    reason: raw.reason || "",
    ms: raw.evaluated_in_ms != null ? Number(raw.evaluated_in_ms).toFixed(2) : (raw.ms != null ? Number(raw.ms).toFixed(2) : "0.00"),
  };
}
function normalizeApproval(raw) {
  let args = raw.arguments;
  if (typeof args === "string") { try { args = JSON.parse(args); } catch (_) { args = {}; } }
  const amount = pick(raw, ["amount_paise", "amount"], (args && args.amount) ?? 0);
  return {
    id: String(raw.approval_id ?? raw.id),
    agent: raw.agent_id || raw.agent || AGENT,
    tool: raw.tool || (args && args.tool) || "—",
    amount,
    why: raw.reason || "pending human review",
  };
}

async function refreshLiveData() {
  if (!MODE.live || !MODE.proxyUrl) return;
  const [metricsRes, decisionsRes, approvalsRes] = await Promise.allSettled([
    fetchMetrics(MODE.proxyUrl),
    fetchDecisions(MODE.proxyUrl, 50),
    fetchApprovals(MODE.proxyUrl),
  ]);

  if (metricsRes.status === "fulfilled") {
    const nm = normalizeMetrics(metricsRes.value);
    Object.assign(M, {
      attempted: nm.attempted, moved: nm.moved, allowed: nm.allowed, denied: nm.denied,
      escalated: nm.escalated, falseBlocks: nm.falseBlocks, pending: nm.pending, resolved: nm.resolved,
    });
    renderCounters(nm.p95);
  }

  if (decisionsRes.status === "fulfilled") {
    const raw = decisionsRes.value;
    const list = Array.isArray(raw) ? raw : (raw.items || raw.decisions || []);
    // Assume /decisions returns newest-first (typical "most recent N" query) —
    // reverse so pushEvent's prepend-to-top restores newest-first display for
    // any items not already rendered.
    const normalized = list.map(normalizeDecision).reverse();
    for (const d of normalized) {
      if (!seenDecisionIds.has(d.id)) { seenDecisionIds.add(d.id); pushEvent(d); }
    }
  }

  if (approvalsRes.status === "fulfilled") {
    const raw = approvalsRes.value;
    const list = Array.isArray(raw) ? raw : (raw.items || raw.approvals || []);
    queue = list.filter(a => (a.status || "pending") === "pending").map(normalizeApproval);
    renderQueue();
  }
}

async function initMode() {
  const cfg = await resolveProxyConfig();
  if (cfg.proxyUrl) {
    setBadge("sim", "connecting to enforcement proxy…");
    // Two attempts: the first may hit a cold Lambda (image pull + init).
    let ok = await pingProxy(cfg.proxyUrl);
    if (!ok) ok = await pingProxy(cfg.proxyUrl);
    if (ok) {
      MODE.live = true;
      MODE.proxyUrl = cfg.proxyUrl;
      setBadge("live", `LIVE — enforcement proxy connected (${cfg.source})`);
      await refreshLiveData();
      pollTimer = setInterval(refreshLiveData, POLL_MS);
      return;
    }
  }
  setBadge("sim", "SIMULATED — proxy not connected");
}

renderCounters();
renderQueue();
initMode(); // connect to the proxy in the background while the landing screen is up

// ---- landing screen <-> dashboard toggle ----
const landingEl = document.getElementById("landing");
const appEl = document.getElementById("app");
const enterBtn = document.getElementById("enterBtn");
const backBtn = document.getElementById("backBtn");
if (enterBtn && landingEl && appEl) {
  enterBtn.addEventListener("click", () => {
    landingEl.style.display = "none";
    appEl.classList.remove("app-hidden");
    window.scrollTo(0, 0);
  });
}
if (backBtn && landingEl && appEl) {
  backBtn.addEventListener("click", (e) => {
    e.preventDefault();
    appEl.classList.add("app-hidden");
    landingEl.style.display = "";
    window.scrollTo(0, 0);
  });
}
