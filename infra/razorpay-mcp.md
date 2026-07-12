# Razorpay MCP server — self-hosting notes

Research trail for Phase 2 (self-hosting Razorpay's open-source MCP server +
the proxy's live upstream). Source: [github.com/razorpay/razorpay-mcp-server](https://github.com/razorpay/razorpay-mcp-server)
(Go), researched 2026-07-12 against `main` (latest tagged release `v1.2.1`,
2025-09-26). Everything below is cited against the repo's own source files,
not just its README, because the README undersells a few load-bearing
details (transport, image tags).

## 1. Deployment options

Razorpay offers two ways to use this server:

1. **Remote MCP server (Razorpay-hosted)** — zero setup, but a subset of
   tools and no local control over the process. Not what we want: this repo
   self-hosts so DEMO_MODE=live is fully within our own docker-compose stack
   and our own test keys.
2. **Local/self-hosted** — Docker image or build-from-source. This is what
   we use.

## 2. Image & tag

Docker Hub: **`razorpay/mcp`** — but it **publishes only commit-hash tags**
(40-char SHA, CI-generated per push), **no `latest`, no semver tag**. Plain
`docker pull razorpay/mcp` (implicit `:latest`) **fails** — there is no such
tag on the registry. You must pin an explicit tag.

Tag pinned in `docker-compose.yml` and `mcp_client.py`'s `DEFAULT_IMAGE`:

```
razorpay/mcp:8607a4d95e67c86db8201d728658eb5f00d790fe
```

This was the tip-of-`main` tag as of 2026-07-12 (pushed ~13h before
research). Because these are per-commit CI tags rather than curated
releases, **it can be pruned** — if `docker pull` 404s on it later, either
grab the current newest tag from
[hub.docker.com/r/razorpay/mcp/tags](https://hub.docker.com/r/razorpay/mcp/tags)
and set `RAZORPAY_MCP_IMAGE=razorpay/mcp:<new-tag>`, or build from source
(below) at a human-readable release tag instead.

**Build from source** (reproducible alternative, pinned to a real release):

```bash
git clone https://github.com/razorpay/razorpay-mcp-server.git
cd razorpay-mcp-server
git checkout v1.2.1
docker build -t razorpay-mcp-server:v1.2.1 .
# then set RAZORPAY_MCP_IMAGE=razorpay-mcp-server:v1.2.1
```

The repo's own `Dockerfile` (verbatim, relevant parts):

```dockerfile
FROM golang:1.24.2-alpine AS builder
...
RUN CGO_ENABLED=0 GOOS=linux go build ... -o razorpay-mcp-server ./cmd/razorpay-mcp-server

FROM alpine:latest
...
ENV CONFIG="" RAZORPAY_KEY_ID="" RAZORPAY_KEY_SECRET="" PORT="8090" MODE="stdio" LOG_FILE=""
USER rzp
ENTRYPOINT ["sh", "-c", "./razorpay-mcp-server stdio --key ${RAZORPAY_KEY_ID} --secret ${RAZORPAY_KEY_SECRET} ${CONFIG:+--config ${CONFIG}} ${LOG_FILE:+--log-file ${LOG_FILE}}"]
```

Note the `PORT=8090` env var: it is declared but **never referenced by the
entrypoint** (`MODE` isn't either — `stdio` is hardcoded into the shell
command). Both look like leftovers for a future HTTP transport that doesn't
exist yet in this codebase. Don't be misled by their presence into thinking
there's a port to publish — there isn't (see §3).

## 3. Transport: stdio ONLY

Checked directly against the source, not just the README:

- `cmd/razorpay-mcp-server/` contains exactly `main.go`, `main_test.go`,
  `stdio.go`, `stdio_test.go` — **no `sse.go`, `http.go`, or
  `streamable_http.go`**.
- `main.go`'s root cobra command registers exactly one subcommand:
  `rootCmd.AddCommand(stdioCmd)`.
- The Dockerfile `ENTRYPOINT` (above) hardcodes `stdio` mode.

**Conclusion: there is no SSE/HTTP transport to point a URL at.** The
server only speaks newline-delimited JSON-RPC 2.0 over its own
stdin/stdout, exactly like every other stdio MCP server (e.g. what
`claude_desktop_config.json` expects). This has two concrete consequences
we had to design around:

- **No port to publish in `docker-compose.yml`.** The `razorpay-mcp` service
  block has none.
- **A docker-compose "service" isn't how you actually talk to it.** stdio is
  a subprocess protocol between a parent and its direct child — not a
  network daemon other containers can dial into. `proxy/upstream/mcp_client.py`
  therefore spawns its own `docker run --rm -i ... <image>` (or a local
  binary) as a child process per live connection, rather than connecting to
  the `razorpay-mcp` compose service. Under plain `docker compose up`, the
  `razorpay-mcp` container starts, immediately receives stdin EOF (nothing
  is piping JSON-RPC into it), and exits 0 — harmless, just inert. It's kept
  in compose so the image is declared/pinned and
  `docker compose run --rm razorpay-mcp` works for manual stdio testing
  (`docker compose run` attaches stdin properly; `up` does not).

**Known gap, flagged for the orchestrator / whoever owns `proxy/Dockerfile`:**
spawning a sibling `docker run` from *inside* the proxy's own container (when
it runs containerized, e.g. under `docker compose up`) requires either (a)
mounting the host's `/var/run/docker.sock` into the proxy container plus a
docker CLI in its image ("Docker-outside-of-Docker"), or (b) baking the
compiled `razorpay-mcp-server` binary directly into the proxy image and
pointing `mcp_client.py` at it via `RAZORPAY_MCP_BIN=/usr/local/bin/razorpay-mcp-server`
(no Docker-in-Docker needed at all — simpler and recommended, but requires a
multi-stage `proxy/Dockerfile` build). Neither is in this workstream's file
scope (`proxy/Dockerfile` is orchestrator-owned). Until one of those lands,
`DEMO_MODE=live` works when the proxy process itself runs directly on a host
that has Docker (e.g. `uvicorn app:app` outside the container, or
`RAZORPAY_MCP_BIN` pointed at a locally installed binary) — which is exactly
how `infra/verify_live.py` and manual live-mode testing are meant to be run
today.

## 4. Running it directly (what `mcp_client.py` executes)

Straight from the server's own README example (Claude Desktop config),
which matches the Dockerfile `ENTRYPOINT` — no extra `stdio` argument needed,
it's baked in:

```bash
docker run --rm -i -e RAZORPAY_KEY_ID -e RAZORPAY_KEY_SECRET razorpay/mcp:8607a4d95e67c86db8201d728658eb5f00d790fe
```

`-e NAME` (no `=value`) forwards the variable from the *calling* process's
own environment — so `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` must already be
set wherever `mcp_client.py` runs, not just in `.env` read by compose.

## 5. Environment variables / flags

| var / flag | required | notes |
|---|---|---|
| `RAZORPAY_KEY_ID` (`--key`/`-k`) | yes | `rzp_test_...` for this project — never live keys |
| `RAZORPAY_KEY_SECRET` (`--secret`/`-s`) | yes | paired secret |
| `TOOLSETS` (`--toolsets`/`-t`) | no | comma-separated toolset names; default `all`. Valid names: `payments`, `payment_links`, `orders`, `refunds`, `payouts`, `qr_codes`, `settlements`, `tokens`, `registrationLinks`, `checkout_integration` |
| `READ_ONLY` (`--read-only`) | no | default `false`; disables write tools when set |
| `LOG_FILE` (`--log-file`/`-l`) | no | path to a log file |
| `CONFIG` (`--config`) | no | path to a config file |

No `X-Razorpay-Account` header or RazorpayX-specific auth is used by this
server anywhere in the researched surface — payouts tools just take an
`account_number` string parameter (§7).

## 6. Tool catalog (by toolset)

Tool names are flat strings — **no `razorpay_` prefix, no toolset prefix**.
Full detail on the toolsets we actually map (§7); the rest are listed for
completeness.

| toolset | tools |
|---|---|
| `payment_links` | `create_payment_link`, `payment_link_upi_create`, `fetch_payment_link`, `fetch_all_payment_links`, `payment_link_notify`, `update_payment_link` |
| `refunds` | `create_refund`, `fetch_refund`, `update_refund`, `fetch_multiple_refunds_for_payment`, `fetch_specific_refund_for_payment`, `fetch_all_refunds` |
| `orders` | `create_order`, `fetch_order`, `fetch_all_orders`, `fetch_order_payments`, `update_order` |
| `payments` | `fetch_payment`, `fetch_payment_card_details`, `update_payment`, `capture_payment`, `fetch_all_payments`, `initiate_payment`, `resend_otp`, `submit_otp` |
| `payouts` | `fetch_payout_with_id`, `fetch_all_payouts` — **read-only, no create/initiate tool exists** |
| `qr_codes` | `create_qr_code`, `fetch_qr_code`, `close_qr_code` (and more; not needed by our vocabulary) |
| `settlements` | `fetch_all_settlements`, `fetch_settlement_with_id`, `fetch_all_instant_settlements` |
| `tokens` | `fetch_tokens`, `revoke_token` |
| `registrationLinks` | registration-link CRUD (e-mandate related; not needed by our vocabulary) |
| `checkout_integration` | `detect_stack`, `integrate_razorpay_checkout` (dev-tooling helpers, not runtime payment tools) |

## 7. Our tool vocabulary → server tool mapping

(Our vocabulary is frozen in `infra/CONTRACTS.md` §1: `issue_refund`,
`create_payment_link`, `pay_vendor`, `get_ticket`, `list_orders`.)

| our tool | server tool | required server params | notes |
|---|---|---|---|
| `create_payment_link` | `create_payment_link` | `amount` (paise, min 100), `currency` | optional: `description`, `notes`, `reference_id`, `customer_name/email/contact`, `callback_url`, `callback_method`. Response `id` is `plink_...`, `short_url` is `https://rzp.io/i/...`. |
| `issue_refund` | `create_refund` | `payment_id` (`pay_...`), `amount` (paise, min 100) | optional `speed` (`normal`\|`optimum`), `notes`, `receipt`. **Needs an existing captured payment — see §8, there is no "create a fake payment" tool.** |
| `list_orders` | `fetch_all_orders` | none required | optional `count` (max 100), `skip`, `from`, `to`, `authorized`, `receipt`. |
| `pay_vendor` | **none — gap** | — | payouts toolset is fetch-only (`fetch_payout_with_id`, `fetch_all_payouts`); there is no create/initiate payout tool in this server as of v1.2.1. `MCPUpstream.execute("pay_vendor", ...)` returns `ok=False` immediately with this explanation — never spawns a process for it. `DEMO_MODE=cached` (`proxy/upstream/cached.py`) is the only place this tool can be demoed end-to-end (see `pay_vendor.json`-shaped fixture there). |
| `get_ticket` | **none — not applicable** | — | Not a Razorpay concept at all; nothing in this MCP server's surface resembles a support ticket. `MCPUpstream` returns a clean `ok=False` explanation; `CachedUpstream` returns a deterministic generic echo so the offline demo still runs end-to-end. |

## 8. Test-mode caveats

- Keys must be `rzp_test_...` / matching secret from **Dashboard → Settings
  → API Keys** with Test Mode active. Test-mode and live-mode keys are
  separate; there is no `mode` flag on the MCP server — mode is entirely a
  property of which key pair you hand it.
- **`create_payment_link` and `fetch_all_orders` work immediately** in test
  mode with no extra setup — they don't depend on any prior state.
- **`create_refund` needs a real, previously *captured* `payment_id`.**
  There is no MCP tool that manufactures one out of thin air — Razorpay
  payments only come into existence through an actual checkout flow. To get
  one in test mode:
  1. Create a Payment Link (`create_payment_link` / `infra/verify_live.py`)
     or an Order (`create_order`), and open its checkout URL.
  2. Pay with a published Razorpay **test card** (any future expiry date,
     any 3-digit CVV) or the **test UPI success VPA `success@razorpay`**
     (there's also a mock bank page with explicit Success/Failure buttons).
     Exact current test card numbers:
     [razorpay.com/docs/payment-gateway/test-card-upi-details](https://razorpay.com/docs/payment-gateway/test-card-upi-details).
  3. The resulting payment is normally auto-captured for a Payment
     Link/Order checkout; if it instead lands `authorized`, call
     `capture_payment` (`payment_id`, `amount`, `currency`) first.
  4. Now `issue_refund` (`create_refund`) against that `pay_...` id will
     actually process.
- **`pay_vendor` cannot be exercised live at all** (§7) — this is a real
  capability gap in the upstream server, not a bug in our mapping. Flagged
  for `DECISIONS.md`.

## 9. Reviewer proof: an allowed action lands in the real Razorpay test dashboard

1. Get a `rzp_test_` key pair into `.env` (`RAZORPAY_KEY_ID`,
   `RAZORPAY_KEY_SECRET`), set `DEMO_MODE=live`.
2. Fastest sanity check, no Docker required: run
   `python infra/verify_live.py` — it prints an `id` (`plink_...`) and
   `short_url` straight away and tells you exactly where to look.
3. Or drive it through the real stack: `docker compose up`, then send
   `POST /tool-call` to the proxy with
   `{"tool": "create_payment_link", "arguments": {"amount": 10000, ...}}`
   for an agent/policy combination that resolves to `allow`. The response's
   `upstream_result.data` will contain a real `plink_...` id + `short_url`
   (mode `"live"`).
4. Open **[dashboard.razorpay.com](https://dashboard.razorpay.com)**, sign
   in, and flip the **Test/Live toggle** (top-left) to **Test Mode**.
5. Go to **Payments → Payment Links**. The link you just created appears
   with the same `id`, amount, and timestamp the proxy/script returned —
   that's the proof it's a real Razorpay-side effect, not a simulation.
6. For `issue_refund`: complete the checkout at the `short_url` per §8, then
   call `issue_refund` with that `pay_...` id through the proxy. Go to
   **Payments → Refunds** in the same Test Mode dashboard — the `rfnd_...`
   id appears with `status: processed`.
7. For `pay_vendor`: there is nothing to show here in live mode — see §7/§8.
   Demo this tool under `DEMO_MODE=cached` instead and say so explicitly;
   claiming otherwise would be dishonest about a real product gap.

## 10. Sources

- [github.com/razorpay/razorpay-mcp-server](https://github.com/razorpay/razorpay-mcp-server) — `README.md`, `Dockerfile`, `cmd/razorpay-mcp-server/*.go`, `pkg/razorpay/*.go` (`payment_links.go`, `refunds.go`, `orders.go`, `payments.go`, `payouts.go`, `tools.go`)
- [github.com/razorpay/razorpay-mcp-server/releases](https://github.com/razorpay/razorpay-mcp-server/releases) — v1.0.0 → v1.2.1
- [hub.docker.com/r/razorpay/mcp/tags](https://hub.docker.com/r/razorpay/mcp/tags)
- [razorpay.com/docs/api/payments/payment-links/create-standard/](https://razorpay.com/docs/api/payments/payment-links/create-standard/) — payment_link entity shape
- [razorpay.com/docs/api/refunds/entity/](https://razorpay.com/docs/api/refunds/entity/) — refund entity shape
- [razorpay.com/docs/api/orders/entity/](https://razorpay.com/docs/api/orders/entity/) — order entity shape
- [razorpay.com/docs/payments/payments/test-upi-details/](https://razorpay.com/docs/payments/payments/test-upi-details/) — test UPI success VPA
- [razorpay.com/docs/payment-gateway/test-card-upi-details](https://razorpay.com/docs/payment-gateway/test-card-upi-details) — test cards
