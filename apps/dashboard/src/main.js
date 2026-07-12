// ---- entry point ------------------------------------------------------------
// Slim on purpose: each page owns its own module, the router owns navigation,
// and the connection module owns the shared LIVE/SIMULATED proxy link. This
// file just wires them together in the right order.
import "./styles.css";
import { initConnection } from "./connection.js";
import { initConsole, refreshLiveData } from "./console.js";
import { initPlayground, showPlayground } from "./playground.js";
import { initRouter } from "./router.js";

// 1. Pages first — render their initial (empty) state and wire their controls.
//    Their DOM lives in index.html and exists on load even while hidden.
initConsole();
initPlayground();

// 2. Connect to the enforcement proxy in the background. When live, the poll
//    calls the console's refresh; the landing screen stays up meanwhile.
initConnection({ onLiveData: refreshLiveData });

// 3. Router last — paints the current route and takes over navigation. The
//    playground lazily connects to the Nova agent the first time it's shown.
initRouter({
  onShow(view) {
    if (view === "playground") showPlayground();
  },
});
