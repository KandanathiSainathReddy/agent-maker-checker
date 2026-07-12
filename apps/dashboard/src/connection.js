// ---- shared enforcement-proxy connection --------------------------------
// Owns the LIVE/SIMULATED mode + the Razorpay-MCP status chip. Both pages
// (console + playground) import the same MODE object; because it is mutated in
// place (never reassigned), the ES-module binding stays live across importers.
import {
  resolveProxyConfig,
  pingProxy,
  POLL_MS,
  fetchProxyHealthz,
} from "./api.js";

export const MODE = { live: false, proxyUrl: null };

let pollTimer = null;

// ---- enforcement-proxy mode badge (sidebar footer) -----------------------
function setModeBadge(mode, text) {
  const dot = document.getElementById("modeDot");
  const label = document.getElementById("modeText");
  if (dot) dot.className = mode === "live" ? "dot live" : "dot sim";
  if (label) label.textContent = text;
}

// ---- Razorpay MCP status chip — sidebar footer + console header echo ------
// GET {proxyUrl}/healthz -> {ok, demo_mode: "cached"|"live"} (proxy/app.py).
// "cached" = replaying recorded Razorpay responses, "live" = real test-mode
// rails via the self-hosted MCP server. Grey "—" when the proxy is unreachable.
const MCP_TARGETS = [
  { dot: "mcpDot", text: "mcpText", badge: "mcpBadge" },
  { dot: "mcpDotEcho", text: "mcpTextEcho", badge: "mcpBadgeEcho" },
];
function setMcpBadge(status, label, title) {
  const dotCls = status === "live" ? "mcp-live" : status === "cached" ? "mcp-cached" : "mcp-unknown";
  for (const t of MCP_TARGETS) {
    const dotEl = document.getElementById(t.dot);
    const textEl = document.getElementById(t.text);
    const badgeEl = document.getElementById(t.badge);
    if (dotEl) dotEl.className = "dot " + dotCls;
    if (textEl) textEl.textContent = label;
    if (badgeEl) badgeEl.title = title || "";
  }
}
export async function refreshMcpStatus(proxyUrl) {
  const url = proxyUrl || MODE.proxyUrl;
  if (!url) { setMcpBadge("unknown", "Razorpay MCP · —", "proxy not connected"); return; }
  try {
    const data = await fetchProxyHealthz(url);
    const demoMode = data && data.demo_mode;
    if (demoMode === "live") setMcpBadge("live", "Razorpay MCP · live", "real test-mode rails via the self-hosted MCP server");
    else if (demoMode === "cached") setMcpBadge("cached", "Razorpay MCP · cached", "replaying recorded Razorpay responses");
    else setMcpBadge("unknown", "Razorpay MCP · —", "unexpected /healthz response");
  } catch (_) {
    setMcpBadge("unknown", "Razorpay MCP · —", "proxy unreachable");
  }
}

// Resolve the proxy, try to connect, and — if reachable — flip to LIVE mode and
// start polling. `onLiveData` is the console's refresh (kept out of here so this
// module has no page dependencies). Runs in the background while the landing
// screen is up.
export async function initConnection({ onLiveData } = {}) {
  const refresh = typeof onLiveData === "function" ? onLiveData : () => {};
  const cfg = await resolveProxyConfig();

  if (cfg.proxyUrl) {
    setModeBadge("sim", "connecting to enforcement proxy…");
    // Two attempts: the first may hit a cold Lambda (image pull + init).
    let ok = await pingProxy(cfg.proxyUrl);
    if (!ok) ok = await pingProxy(cfg.proxyUrl);
    if (ok) {
      MODE.live = true;
      MODE.proxyUrl = cfg.proxyUrl;
      setModeBadge("live", `LIVE — enforcement proxy connected (${cfg.source})`);
      await refresh();
      await refreshMcpStatus();
      pollTimer = setInterval(() => { refresh(); refreshMcpStatus(); }, POLL_MS);
      return;
    }
  }

  setModeBadge("sim", "SIMULATED — proxy not connected");
  // /healthz is a separate, lighter endpoint than /metrics — worth trying even
  // when the pingProxy() check above didn't succeed.
  await refreshMcpStatus(cfg.proxyUrl);
}
