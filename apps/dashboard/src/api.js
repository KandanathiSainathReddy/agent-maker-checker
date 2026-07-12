// ---- API client for the enforcement proxy ----------------------------------
// Routes + shapes are frozen in infra/CONTRACTS.md §1. Money is integer paise
// on the wire; formatting to INR happens only in the display layer (main.js).

const PING_TIMEOUT_MS = 1500;
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

async function getJSON(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(PING_TIMEOUT_MS) });
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
