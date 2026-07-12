// ---- shared formatting + tiny helpers (no DOM, no state) -------------------
// Money is held in paise everywhere (like the backend); INR formatting is the
// only display-layer conversion.

export function inr(paise) {
  const r = Math.round(paise / 100);
  const s = String(r);
  if (s.length <= 3) return "₹" + s;
  const last3 = s.slice(-3);
  const rest = s.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return "₹" + rest + "," + last3;
}

// escape dynamic/network-sourced text before dropping it into innerHTML —
// playground content comes from the live agent/proxy and must not be trusted
// as markup.
export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function stamp() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0");
}

export function p95(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(s.length * 0.95))];
}

// first non-null value among the candidate keys (backends vary field names)
export function pick(obj, keys, fallback = null) {
  for (const k of keys) {
    if (obj && obj[k] != null) return obj[k];
  }
  return fallback;
}
