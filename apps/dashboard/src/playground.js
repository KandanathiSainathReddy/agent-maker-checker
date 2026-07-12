// ---- Nova playground page — live chat with the Amazon Nova agent + the
// checker admin (manual cap/allowlist controls + the NL policy copilot).
// Nova only ever *proposes* policy changes here; nothing touches the proxy
// until the human reviews the proposal and clicks Apply. Enforcement stays
// deterministic — Nova never evaluates a payment.
import { inr, escapeHtml } from "./format.js";
import { MODE } from "./connection.js";
import {
  resolveAgentConfig,
  fetchAgentHealth,
  fetchAgentSamples,
  postAgentChat,
  fetchPolicy,
  postPerCallCap,
  postAllowlistAdd,
  postAllowlistRemove,
  postVelocityThreshold,
  fetchNlSamples,
  postNlPolicy,
} from "./api.js";

const AGENT = "support-agent-1";
const PG = { agentUrl: null, initialized: false, tokens: 0, busy: false };

function pgEl(id) { return document.getElementById(id); }

function pgSetModel(model) {
  if (!model) return;
  const el = pgEl("pgModel");
  if (el) el.textContent = "model: " + model;
}
function pgAddTokens(n) {
  if (typeof n !== "number" || Number.isNaN(n)) return;
  PG.tokens += n;
  const el = pgEl("pgTokens");
  if (el) el.textContent = PG.tokens.toLocaleString("en-IN") + " tokens";
}
function pgSetStatus(text) { const el = pgEl("pgStatus"); if (el) el.textContent = text; }
function pgShowAgentNote(msg) { const el = pgEl("pgAgentNote"); if (el) el.textContent = msg || ""; }
function pgShowAdminNote(msg) { const el = pgEl("pgAdminNote"); if (el) el.textContent = msg || ""; }
function pgSetSendDisabled(disabled) {
  const send = pgEl("pgSend"); const input = pgEl("pgInput");
  if (send) send.disabled = disabled;
  if (input) input.disabled = disabled;
}

// Best-effort pull of a couple of "interesting" args out of a tool_call's
// arguments for the monospace chip, e.g. "issue_refund · ₹1,200 · cust_ravi".
function pgArgChip(tool, args) {
  const bits = [];
  if (args && typeof args === "object") {
    for (const [k, v] of Object.entries(args)) {
      if (v == null || v === "" || bits.length >= 3) continue;
      if (/amount|cap/i.test(k) && typeof v === "number") bits.push(inr(v));
      else if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") bits.push(String(v));
    }
  }
  return tool + (bits.length ? " · " + bits.join(" · ") : "");
}
function pgShortJSON(obj, max = 160) {
  let s;
  try { s = JSON.stringify(obj); } catch (_) { s = String(obj); }
  if (!s) return "";
  return s.length > max ? s.slice(0, max) + "…" : s;
}

function pgRenderEvent(ev) {
  const type = ev && ev.type;
  const div = document.createElement("div");
  switch (type) {
    case "assistant_text":
      div.className = "pg-ev pg-ev-text";
      div.innerHTML = `<span class="pg-ev-label">nova</span><span class="pg-ev-body">${escapeHtml(ev.text || "")}</span>`;
      break;
    case "tool_call":
      div.className = "pg-ev pg-ev-toolcall";
      div.innerHTML = `<span class="pg-ev-label">calls</span><span class="pg-chip">${escapeHtml(pgArgChip(ev.tool || "tool", ev.arguments))}</span>`;
      break;
    case "tool_result":
      div.className = "pg-ev pg-ev-toolresult";
      div.innerHTML = `<span class="pg-ev-label">result</span><span class="pg-ev-body mono">${escapeHtml(ev.tool ? ev.tool + " → " + pgShortJSON(ev.result) : pgShortJSON(ev.result))}</span>`;
      break;
    case "proxy_decision": {
      const decision = (ev.decision || "—").toLowerCase();
      const cls = decision === "allow" ? "allow" : decision === "deny" ? "deny" : "escalate";
      div.className = "pg-ev pg-ev-decision";
      div.innerHTML =
        `<span class="verdict ${cls}">${escapeHtml(ev.decision || "—")}</span>` +
        `<span class="pg-ev-body">${escapeHtml(ev.reason || "")} <span class="pid">[${escapeHtml(ev.policy_id || "—")}]</span></span>`;
      break;
    }
    case "proxy_error":
      div.className = "pg-ev pg-ev-error";
      div.innerHTML = `<span class="verdict deny">proxy error</span><span class="pg-ev-body">${escapeHtml(ev.reason || ev.message || ev.error || pgShortJSON(ev))}</span>`;
      break;
    case "tool_call_malformed":
      div.className = "pg-ev pg-ev-error";
      div.innerHTML = `<span class="verdict escalate">malformed call</span><span class="pg-ev-body">${escapeHtml(ev.reason || ev.message || pgShortJSON(ev))}</span>`;
      break;
    case "final":
      div.className = "pg-ev pg-ev-final";
      div.innerHTML = `<span class="pg-ev-label">nova ✓</span><span class="pg-ev-body">${escapeHtml(ev.text || "")}</span>`;
      break;
    default:
      div.className = "pg-ev pg-ev-unknown";
      div.innerHTML = `<span class="pg-ev-label">${escapeHtml(type || "event")}</span><span class="pg-ev-body mono">${escapeHtml(pgShortJSON(ev))}</span>`;
  }
  return div;
}

async function pgSendMessage(rawText) {
  const text = (rawText || "").trim();
  if (!text) return;
  if (PG.busy) return;
  if (!PG.agentUrl) {
    pgShowAgentNote("agent endpoint not configured (set VITE_AGENT_URL for local dev)");
    return;
  }

  PG.busy = true;
  pgSetSendDisabled(true);
  pgShowAgentNote("");

  const transcript = pgEl("pgTranscript");
  const emptyEl = pgEl("pgEmpty");
  if (emptyEl) emptyEl.remove();

  const turn = document.createElement("div");
  turn.className = "pg-turn";
  const userRow = document.createElement("div");
  userRow.className = "pg-user";
  userRow.innerHTML = `<span class="pg-user-label">you</span><span class="pg-user-body">${escapeHtml(text)}</span>`;
  const eventsWrap = document.createElement("div");
  eventsWrap.className = "pg-events";
  const pending = document.createElement("div");
  pending.className = "pg-pending";
  pending.textContent = "Nova is deciding…";
  eventsWrap.appendChild(pending);
  turn.appendChild(userRow);
  turn.appendChild(eventsWrap);
  transcript.appendChild(turn);
  transcript.scrollTop = transcript.scrollHeight;

  pgEl("pgInput").value = "";

  try {
    const resp = await postAgentChat(PG.agentUrl, text, AGENT);
    pending.remove();
    if (resp && resp.model) pgSetModel(resp.model);
    if (resp && resp.tokens && typeof resp.tokens.total === "number") pgAddTokens(resp.tokens.total);

    const evs = Array.isArray(resp && resp.events) ? resp.events : [];
    // Nova's event stream ends with a trailing `assistant_text` immediately
    // followed by a `final` event carrying the identical closing reply — drop
    // the plain duplicate and keep only the emphasized "nova ✓" final. Earlier
    // assistant_text turns (narrating a tool call) are untouched since their
    // text won't match the final's text.
    let lastAssistantEl = null;
    let lastAssistantText = null;
    for (const ev of evs) {
      if (ev && ev.type === "assistant_text") {
        const el = pgRenderEvent(ev);
        eventsWrap.appendChild(el);
        lastAssistantEl = el;
        lastAssistantText = (ev.text || "").trim();
        continue;
      }
      if (ev && ev.type === "final") {
        const finalText = (ev.text || "").trim();
        if (lastAssistantEl && lastAssistantText === finalText) {
          lastAssistantEl.remove();
        }
        eventsWrap.appendChild(pgRenderEvent(ev));
        lastAssistantEl = null;
        lastAssistantText = null;
        continue;
      }
      eventsWrap.appendChild(pgRenderEvent(ev));
    }
    if (resp && resp.final_text && !evs.some((e) => e && e.type === "final")) {
      eventsWrap.appendChild(pgRenderEvent({ type: "final", text: resp.final_text }));
    }

    const meta = document.createElement("div");
    meta.className = "pg-turn-meta";
    const turnsUsed = resp && resp.turns_used != null ? resp.turns_used : "—";
    const tIn = resp && resp.tokens ? resp.tokens.input : "—";
    const tOut = resp && resp.tokens ? resp.tokens.output : "—";
    meta.textContent = `${turnsUsed} turn(s) · tokens in/out ${tIn}/${tOut}`;
    eventsWrap.appendChild(meta);
  } catch (err) {
    pending.remove();
    const errRow = document.createElement("div");
    errRow.className = "pg-ev pg-ev-error";
    errRow.innerHTML = `<span class="verdict deny">unreachable</span><span class="pg-ev-body">Nova agent did not respond — ${escapeHtml(err && err.message ? err.message : String(err))}</span>`;
    eventsWrap.appendChild(errRow);
  } finally {
    transcript.scrollTop = transcript.scrollHeight;
    PG.busy = false;
    pgSetSendDisabled(false);
  }
}

async function pgFetchHealth() {
  try {
    const data = await fetchAgentHealth(PG.agentUrl);
    const model = data && (data.model || data.model_id);
    if (model) pgSetModel(model);
    pgSetStatus("agent reachable");
  } catch (err) {
    console.warn("[dashboard] agent healthz failed:", err);
    pgSetStatus("agent unreachable");
    pgShowAgentNote("agent endpoint configured but unreachable — chat requests may fail");
  }
}

async function pgLoadSamples() {
  const wrap = pgEl("pgSamples");
  if (!wrap) return;
  try {
    const data = await fetchAgentSamples(PG.agentUrl);
    const samples = Array.isArray(data && data.samples) ? data.samples : [];
    if (!samples.length) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = samples.map((s) =>
      `<button type="button" class="pg-chip-btn" data-msg="${escapeHtml(s.message || "")}">${escapeHtml(s.label || s.message || "sample")}</button>`
    ).join("");
    wrap.querySelectorAll(".pg-chip-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        pgEl("pgInput").value = btn.dataset.msg || "";
        pgEl("pgInput").focus();
      });
    });
  } catch (err) {
    console.warn("[dashboard] agent samples fetch failed:", err);
    wrap.innerHTML = "";
  }
}

// Dig an effective cap (in ₹) out of GET /admin/policy's per-policy `effective`
// blob — checks the plausible key names (and one level under `params`).
function pgExtractCapInr(eff) {
  if (!eff || typeof eff !== "object") return null;
  for (const k of ["cap_inr", "default_cap_inr"]) {
    if (typeof eff[k] === "number") return eff[k];
  }
  for (const k of ["cap_paise", "default_cap_paise"]) {
    if (typeof eff[k] === "number") return Math.round(eff[k] / 100);
  }
  if (eff.params && eff.params !== eff) return pgExtractCapInr(eff.params);
  return null;
}
async function pgLoadPolicy() {
  const el = pgEl("pgCapCurrent");
  if (!el) return;
  if (!MODE.proxyUrl) { el.textContent = "current: — (proxy not connected)"; return; }
  try {
    const data = await fetchPolicy(MODE.proxyUrl);
    const policies = Array.isArray(data && data.policies) ? data.policies : [];
    const capPolicy = policies.find((p) => p.policy_id === "per_call_amount_cap");
    const capInr = capPolicy ? pgExtractCapInr(capPolicy.effective) : null;
    el.textContent = capInr != null ? "current: " + inr(capInr * 100) : "current: —";
  } catch (err) {
    console.warn("[dashboard] admin/policy fetch failed:", err);
    el.textContent = "current: — (proxy unreachable)";
  }
}

function pgWireAdmin() {
  const capApply = pgEl("pgCapApply");
  if (capApply) capApply.addEventListener("click", async () => {
    const input = pgEl("pgCapInput");
    const val = Number(input.value);
    const confirmEl = pgEl("pgAdminConfirm");
    pgShowAdminNote("");
    if (!MODE.proxyUrl) { pgShowAdminNote("proxy not connected — cannot apply admin changes"); return; }
    if (!val || val <= 0) { pgShowAdminNote("enter a valid cap amount in ₹"); return; }
    try {
      await postPerCallCap(MODE.proxyUrl, val);
      confirmEl.textContent = `cap → ₹${val.toLocaleString("en-IN")} · next agent action respects it`;
      confirmEl.className = "pg-admin-confirm ok";
      await pgLoadPolicy();
    } catch (err) {
      pgShowAdminNote("failed to apply cap — " + (err && err.message ? err.message : String(err)));
    }
  });

  const allowApply = pgEl("pgAllowApply");
  if (allowApply) allowApply.addEventListener("click", async () => {
    const input = pgEl("pgAllowInput");
    const payee = (input.value || "").trim();
    const confirmEl = pgEl("pgAdminConfirm");
    pgShowAdminNote("");
    if (!MODE.proxyUrl) { pgShowAdminNote("proxy not connected — cannot apply admin changes"); return; }
    if (!payee) { pgShowAdminNote("enter a payee identifier"); return; }
    try {
      await postAllowlistAdd(MODE.proxyUrl, payee);
      confirmEl.textContent = `allowlist + ${payee} · next agent action respects it`;
      confirmEl.className = "pg-admin-confirm ok";
      input.value = "";
    } catch (err) {
      pgShowAdminNote("failed to update allowlist — " + (err && err.message ? err.message : String(err)));
    }
  });
}

function pgWireChat() {
  const send = pgEl("pgSend");
  const input = pgEl("pgInput");
  if (send) send.addEventListener("click", () => pgSendMessage(pgEl("pgInput").value));
  if (input) input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); pgSendMessage(pgEl("pgInput").value); }
  });
}

// ---- Nova NL-policy copilot -------------------------------------------------
// POST {agentUrl}/admin/nl-policy {text} -> {summary, changes:[...], model, tokens, note?}
// GET  {agentUrl}/agent/nl-samples       -> {samples:[{label,text}]}
let nlLastChanges = [];

function nlDescribeChange(change) {
  if (!change) return "change";
  switch (change.kind) {
    case "set_per_call_cap": return "per-call cap → " + inr(Number(change.cap_inr) * 100);
    case "set_velocity_threshold": return "velocity threshold → " + inr(Number(change.threshold_inr) * 100);
    case "add_payee": return "allowlist + " + change.payee;
    case "remove_payee": return "allowlist − " + change.payee;
    default: return change.kind || "change";
  }
}

function nlRenderResult(data) {
  const el = pgEl("nlPolicyResult");
  if (!el) return;
  nlLastChanges = Array.isArray(data && data.changes) ? data.changes : [];
  const parts = [];
  parts.push(`<div class="pg-nl-summary">${escapeHtml((data && data.summary) || "(no summary)")}</div>`);
  if (nlLastChanges.length) {
    parts.push(`<div class="pg-nl-chips">${nlLastChanges.map((c) =>
      `<span class="pg-chip pg-nl-chip">${escapeHtml(c.label || nlDescribeChange(c))}</span>`
    ).join("")}</div>`);
  } else {
    parts.push(`<div class="pg-nl-empty">Nova proposed no policy changes.</div>`);
  }
  if (data && data.note) parts.push(`<div class="pg-nl-note-line">${escapeHtml(data.note)}</div>`);
  const metaBits = [];
  if (data && data.model) metaBits.push(String(data.model));
  const totalTokens = data && data.tokens && typeof data.tokens.total === "number" ? data.tokens.total : null;
  if (totalTokens != null) metaBits.push(totalTokens + " tokens");
  if (metaBits.length) parts.push(`<div class="pg-nl-meta">${escapeHtml(metaBits.join(" · "))}</div>`);
  if (nlLastChanges.length) parts.push(`<button type="button" id="nlPolicyApply" class="btn-primary pg-nl-apply">Apply</button>`);
  el.innerHTML = parts.join("");
  const applyBtn = pgEl("nlPolicyApply");
  if (applyBtn) applyBtn.addEventListener("click", nlApplyChanges);
}

async function nlApplyChanges() {
  const noteEl = pgEl("nlPolicyNote");
  const confirmEl = pgEl("pgAdminConfirm");
  if (noteEl) noteEl.textContent = "";
  if (!MODE.proxyUrl) { if (noteEl) noteEl.textContent = "proxy not connected — cannot apply policy changes"; return; }
  if (!nlLastChanges.length) return;
  const applyBtn = pgEl("nlPolicyApply");
  if (applyBtn) applyBtn.disabled = true;
  const results = [];
  for (const change of nlLastChanges) {
    try {
      switch (change.kind) {
        case "set_per_call_cap":
          await postPerCallCap(MODE.proxyUrl, change.cap_inr);
          results.push("cap → ₹" + Number(change.cap_inr).toLocaleString("en-IN"));
          break;
        case "set_velocity_threshold":
          await postVelocityThreshold(MODE.proxyUrl, change.threshold_inr);
          results.push("velocity threshold → ₹" + Number(change.threshold_inr).toLocaleString("en-IN"));
          break;
        case "add_payee":
          await postAllowlistAdd(MODE.proxyUrl, change.payee);
          results.push("allowlist + " + change.payee);
          break;
        case "remove_payee":
          await postAllowlistRemove(MODE.proxyUrl, change.payee);
          results.push("allowlist − " + change.payee);
          break;
        default:
          results.push("skipped (unknown change kind: " + change.kind + ")");
      }
    } catch (err) {
      results.push("FAILED " + change.kind + " — " + (err && err.message ? err.message : String(err)));
    }
  }
  if (confirmEl) {
    confirmEl.textContent = "Nova's changes applied → " + results.join(" · ");
    confirmEl.className = "pg-admin-confirm ok";
  }
  await pgLoadPolicy();
  if (applyBtn) applyBtn.disabled = false;
}

function nlSetDisabled(disabled, note) {
  const askBtn = pgEl("nlPolicyAsk");
  const textEl = pgEl("nlPolicyText");
  if (askBtn) askBtn.disabled = disabled;
  if (textEl) textEl.disabled = disabled;
  if (disabled && note) { const n = pgEl("nlPolicyNote"); if (n) n.textContent = note; }
}

async function nlLoadSamples() {
  const wrap = pgEl("nlPolicySamples");
  if (!wrap) return;
  try {
    const data = await fetchNlSamples(PG.agentUrl);
    const samples = Array.isArray(data && data.samples) ? data.samples : [];
    if (!samples.length) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = samples.map((s) =>
      `<button type="button" class="pg-chip-btn" data-text="${escapeHtml(s.text || "")}">${escapeHtml(s.label || s.text || "sample")}</button>`
    ).join("");
    wrap.querySelectorAll(".pg-chip-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        pgEl("nlPolicyText").value = btn.dataset.text || "";
        pgEl("nlPolicyText").focus();
      });
    });
  } catch (err) {
    console.warn("[dashboard] nl-samples fetch failed:", err);
    wrap.innerHTML = "";
  }
}

function pgWireNlPolicy() {
  const askBtn = pgEl("nlPolicyAsk");
  if (!askBtn) return;
  askBtn.addEventListener("click", async () => {
    const textEl = pgEl("nlPolicyText");
    const text = (textEl.value || "").trim();
    const noteEl = pgEl("nlPolicyNote");
    const resultEl = pgEl("nlPolicyResult");
    noteEl.textContent = "";
    resultEl.innerHTML = "";
    if (!PG.agentUrl) { noteEl.textContent = "agent endpoint not configured (set VITE_AGENT_URL for local dev)"; return; }
    if (!text) { noteEl.textContent = "describe a policy change first"; return; }
    askBtn.disabled = true;
    resultEl.innerHTML = `<div class="pg-pending">Nova is drafting a change…</div>`;
    try {
      const data = await postNlPolicy(PG.agentUrl, text);
      nlRenderResult(data || {});
    } catch (err) {
      resultEl.innerHTML = "";
      noteEl.textContent = "Nova did not respond — " + (err && err.message ? err.message : String(err));
    } finally {
      askBtn.disabled = false;
    }
  });
}

// Called by the router every time the playground route is shown. Refreshes the
// effective cap on each visit and lazily connects to the agent the first time.
export async function showPlayground() {
  pgLoadPolicy();

  if (PG.initialized) return;
  PG.initialized = true;

  const cfg = await resolveAgentConfig();
  PG.agentUrl = cfg.agentUrl;
  if (!PG.agentUrl) {
    pgSetSendDisabled(true);
    pgSetStatus("not configured");
    pgShowAgentNote("agent endpoint not configured (set VITE_AGENT_URL for local dev)");
    nlSetDisabled(true, "agent endpoint not configured (set VITE_AGENT_URL for local dev)");
    return;
  }
  pgSetSendDisabled(false);
  pgFetchHealth();
  pgLoadSamples();
  nlLoadSamples();
}

export function initPlayground() {
  pgWireChat();
  pgWireAdmin();
  pgWireNlPolicy();
}
