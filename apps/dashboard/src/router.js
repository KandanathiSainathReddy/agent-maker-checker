// ---- the router — single source of truth for navigation --------------------
// Hash-based so refresh / back-forward / deep links work. Exactly one page is
// visible at any time: we drive visibility with inline `style.display`, which
// always wins over stylesheet rules, so pages can never stack (the bug the old
// class-toggle approach hit). Routes:
//   #/            -> landing
//   #/console     -> console (dashboard)
//   #/playground  -> Nova playground

const ROUTES = ["landing", "console", "playground"];
const HASH = { landing: "#/", console: "#/console", playground: "#/playground" };

function routeFromHash() {
  const h = (location.hash || "").replace(/^#\/?/, "").toLowerCase();
  if (h === "console") return "console";
  if (h === "playground") return "playground";
  return "landing";
}

function show(el, on) { if (el) el.style.display = on ? "" : "none"; }

export function initRouter({ onShow } = {}) {
  const landing = document.getElementById("landing");
  const appShell = document.getElementById("appShell");
  const consoleView = document.getElementById("view-console");
  const pgView = document.getElementById("view-playground");
  const nav = {
    landing: document.getElementById("navHome"),
    console: document.getElementById("navConsole"),
    playground: document.getElementById("navPlayground"),
  };
  const enterBtn = document.getElementById("enterBtn");

  function setActiveNav(view) {
    for (const key of ROUTES) {
      if (nav[key]) nav[key].classList.toggle("active", key === view);
    }
  }

  function render() {
    const view = routeFromHash();
    const isLanding = view === "landing";
    show(landing, isLanding);
    show(appShell, !isLanding);
    if (!isLanding) {
      show(consoleView, view === "console");
      show(pgView, view === "playground");
      setActiveNav(view);
      if (typeof onShow === "function") onShow(view);
    }
    window.scrollTo(0, 0);
  }

  function go(view) {
    const target = HASH[view] || HASH.landing;
    if (location.hash === target || (target === HASH.landing && !location.hash)) {
      render(); // same hash → hashchange won't fire, render directly
    } else {
      location.hash = target; // triggers hashchange -> render
    }
  }

  if (enterBtn) enterBtn.addEventListener("click", () => go("playground"));
  for (const key of ROUTES) {
    if (nav[key]) nav[key].addEventListener("click", () => go(key));
  }
  window.addEventListener("hashchange", render);

  render(); // initial paint from whatever hash we loaded with
}
