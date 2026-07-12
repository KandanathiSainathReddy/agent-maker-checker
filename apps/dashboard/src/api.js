// ---- API client for the enforcement proxy ----------------------------------
// Routes + shapes are frozen in infra/CONTRACTS.md §1. Money is integer paise
// on the wire; formatting to INR happens only in the display layer (main.js).

// 12s, not 1.5s: a cold container-Lambda takes ~8s to first respond, so a short
// ping would read a healthy proxy as unreachable and leave the dashboard stuck
// in SIMULATED on first load.
const PING_TIMEOUT_MS = 12000;
export const POLL_MS = 4000;

function stripTrailingSlash(url) {
  return String(url).replace(/\/+$/, "");
}

// Resolve the proxy base URL. Precedence (CONTRACTS.md §6, extended for local
// docker-compose — see the Dockerfile / public/env-config.js comment):
//   1. amplify_outputs.json -> custom.proxyUrl   Amplify Hosting writes this at
//                                                 build/deploy time.
//   2. window.__PROXY_URL__                      Runtime-injected. docker-compose
//                                                 sets PROXY_URL on the `dashboard`
//                                                 container at *start*, which a
//                                                 pre-built static bundle can't read
//                                                 directly — the nginx image's
//                                                 entrypoint writes it into
//                                                 env-config.js, loaded before this
//                                                 bundle (see index.html).
//   3. import.meta.env.VITE_PROXY_URL            Vite build-time env, for
//                                                 `npm run dev` against a proxy
//                                                 running outside compose.
//   4. none -> caller falls back to SIMULATED mode.
export async function resolveProxyConfig() {
  try {
    const res = await fetch("./amplify_outputs.json", { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      const proxyUrl = data && data.custom && data.custom.proxyUrl;
      if (proxyUrl) return { proxyUrl: stripTrailingSlash(proxyUrl), source: "amplify_outputs.json" };
    }
  } catch (_) {
    // absent, not JSON, or blocked — keep looking down the precedence chain
  }

  if (typeof window !== "undefined" && window.__PROXY_URL__) {
    return { proxyUrl: stripTrailingSlash(window.__PROXY_URL__), source: "runtime PROXY_URL" };
  }

  const viteUrl = import.meta.env.VITE_PROXY_URL;
  if (viteUrl) return { proxyUrl: stripTrailingSlash(viteUrl), source: "VITE_PROXY_URL" };

  return { proxyUrl: null, source: "none" };
}

export async function pingProxy(proxyUrl) {
  try {
    const res = await fetch(`${proxyUrl}/metrics`, { signal: AbortSignal.timeout(PING_TIMEOUT_MS) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

// GET /healthz -> {ok: bool, demo_mode: "cached"|"live"} (proxy/app.py). Drives
// the "Razorpay MCP" status chip — cached = replaying recorded Razorpay
// responses, live = real test-mode rails via the self-hosted MCP server.
export function fetchProxyHealthz(proxyUrl) {
  return getJSON(`${proxyUrl}/healthz`, PING_TIMEOUT_MS);
}

// Resolve the Nova agent base URL. Precedence mirrors resolveProxyConfig above:
//   1. amplify_outputs.json -> custom.agentUrl   Amplify Hosting writes this at
//                                                 build/deploy time.
//   2. window.__AGENT_URL__                      Runtime-injected (e.g. by a
//                                                 hosting entrypoint), same seam
//                                                 pattern as window.__PROXY_URL__.
//   3. import.meta.env.VITE_AGENT_URL            Vite build-time env, for
//                                                 `npm run dev` against an agent
//                                                 service running locally.
//   4. none -> caller shows an "agent endpoint not configured" note.
export async function resolveAgentConfig() {
  try {
    const res = await fetch("./amplify_outputs.json", { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      const agentUrl = data && data.custom && data.custom.agentUrl;
      if (agentUrl) return { agentUrl: stripTrailingSlash(agentUrl), source: "amplify_outputs.json" };
    }
  } catch (_) {
    // absent, not JSON, or blocked — keep looking down the precedence chain
  }

  if (typeof window !== "undefined" && window.__AGENT_URL__) {
    return { agentUrl: stripTrailingSlash(window.__AGENT_URL__), source: "runtime AGENT_URL" };
  }

  const viteAgentUrl = import.meta.env.VITE_AGENT_URL;
  if (viteAgentUrl) return { agentUrl: stripTrailingSlash(viteAgentUrl), source: "VITE_AGENT_URL" };

  return { agentUrl: null, source: "none" };
}

async function getJSON(url, timeoutMs = PING_TIMEOUT_MS) {
  const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

export function fetchMetrics(proxyUrl) {
  return getJSON(`${proxyUrl}/metrics`);
}
export function fetchDecisions(proxyUrl, limit = 50) {
  return getJSON(`${proxyUrl}/decisions?limit=${limit}`);
}
export function fetchApprovals(proxyUrl) {
  return getJSON(`${proxyUrl}/approvals`);
}

export async function postApprovalDecision(proxyUrl, id, approve) {
  const path = approve ? "approve" : "deny";
  const res = await fetch(`${proxyUrl}/approvals/${encodeURIComponent(id)}/${path}`, { method: "POST" });
  if (!res.ok) throw new Error(`approval ${path} -> HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}

// payload shape per CONTRACTS.md §1 POST /tool-call.
export async function postToolCall(proxyUrl, payload) {
  const res = await fetch(`${proxyUrl}/tool-call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json().catch(() => ({}));
  // response shape: {request_id, decision, policy_id, reason, evaluated_in_ms, status, upstream_result}
}

// ---- Nova agent playground (separate service, base = agentUrl) -------------
// A chat turn can take several seconds (Nova may make several tool-call round
// trips through the proxy before it settles on a final reply), so this gets
// its own, much longer, timeout than the proxy's read endpoints.
const AGENT_CHAT_TIMEOUT_MS = 60000;
const AGENT_READ_TIMEOUT_MS = 12000;

export function fetchAgentHealth(agentUrl) {
  return getJSON(`${agentUrl}/agent/healthz`, AGENT_READ_TIMEOUT_MS);
}
export function fetchAgentSamples(agentUrl) {
  return getJSON(`${agentUrl}/agent/samples`, AGENT_READ_TIMEOUT_MS);
}
// response shape: {final_text, events:[...], turns_used, model, tokens:{input,output,total}}
export async function postAgentChat(agentUrl, message, agentId) {
  const body = { message };
  if (agentId) body.agent_id = agentId;
  const res = await fetch(`${agentUrl}/agent/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(AGENT_CHAT_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`agent/chat -> HTTP ${res.status}`);
  return res.json();
}

// ---- checker admin (on the PROXY) — live policy controls for the playground ----
export function fetchPolicy(proxyUrl) {
  return getJSON(`${proxyUrl}/admin/policy`, AGENT_READ_TIMEOUT_MS);
}
export async function postPerCallCap(proxyUrl, capInr) {
  const res = await fetch(`${proxyUrl}/admin/policy/per_call_amount_cap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params: { default_cap_inr: capInr } }),
  });
  if (!res.ok) throw new Error(`admin/policy/per_call_amount_cap -> HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}
export async function postAllowlistAdd(proxyUrl, payee) {
  const res = await fetch(`${proxyUrl}/admin/allowlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payee, action: "add" }),
  });
  if (!res.ok) throw new Error(`admin/allowlist -> HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}
export async function postAllowlistRemove(proxyUrl, payee) {
  const res = await fetch(`${proxyUrl}/admin/allowlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payee, action: "remove" }),
  });
  if (!res.ok) throw new Error(`admin/allowlist -> HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}
// policies/velocity_aggregation.yaml's rolling-window structuring threshold
// param is `threshold_inr` (see params: block in that file).
export async function postVelocityThreshold(proxyUrl, thresholdInr) {
  const res = await fetch(`${proxyUrl}/admin/policy/velocity_aggregation`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params: { threshold_inr: thresholdInr } }),
  });
  if (!res.ok) throw new Error(`admin/policy/velocity_aggregation -> HTTP ${res.status}`);
  return res.json().catch(() => ({}));
}

// ---- Nova NL-policy copilot (on the AGENT service) — Nova only drafts a
// structured proposal here; nothing is applied until the human clicks Apply
// in the dashboard, which calls the admin endpoints above per change kind.
export function fetchNlSamples(agentUrl) {
  return getJSON(`${agentUrl}/agent/nl-samples`, AGENT_READ_TIMEOUT_MS);
}
// response shape: {summary, changes:[{kind,...,label}], model, tokens:{input,output,total}, note?}
export async function postNlPolicy(agentUrl, text) {
  const res = await fetch(`${agentUrl}/admin/nl-policy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal: AbortSignal.timeout(AGENT_CHAT_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`admin/nl-policy -> HTTP ${res.status}`);
  return res.json();
}
