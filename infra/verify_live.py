#!/usr/bin/env python
"""Manual, human-run smoke test for RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET.

Creates ONE real Razorpay test-mode Payment Link for Rs 100 and prints its
id + short_url, so a reviewer can find it in the Razorpay test dashboard
(dashboard.razorpay.com, Test Mode toggle, top-left) and know the whole
DEMO_MODE=live path is wired to a real, working pair of keys -- before
debugging anything about Docker or the MCP handshake.

This hits the plain Razorpay REST endpoint (POST /v1/payment_links) that the
razorpay-mcp-server `create_payment_link` tool itself calls under the hood --
same API, same auth, same dashboard result -- using only the Python standard
library (urllib), so it runs in any environment with Python and no other
setup, independent of whatever is or isn't installed for the proxy or
whether Docker is running.

Usage (PowerShell):
    $env:RAZORPAY_KEY_ID = "rzp_test_xxxxxxxxxxxx"
    $env:RAZORPAY_KEY_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxx"
    python infra/verify_live.py

Usage (bash):
    export RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
    export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
    python infra/verify_live.py

This script never prompts for or accepts credentials as arguments -- it only
reads them from the environment you already set, and it refuses to run
against anything that isn't an rzp_test_ key.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://api.razorpay.com/v1/payment_links"
DEFAULT_AMOUNT_PAISE = 10_000  # Rs 100.00


def main() -> int:
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    if not key_id or not key_secret:
        print(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in the environment.",
            file=sys.stderr,
        )
        print(
            "This script never prompts for credentials -- set them yourself first.",
            file=sys.stderr,
        )
        return 2

    if not key_id.startswith("rzp_test_"):
        print(
            f"Refusing to run: RAZORPAY_KEY_ID={key_id!r} is not an rzp_test_ key.",
            file=sys.stderr,
        )
        print(
            "This script is only for the test-mode dashboard proof -- never point it at live keys.",
            file=sys.stderr,
        )
        return 2

    try:
        amount = int(os.environ.get("VERIFY_AMOUNT_PAISE", DEFAULT_AMOUNT_PAISE))
    except ValueError:
        print("VERIFY_AMOUNT_PAISE must be an integer number of paise.", file=sys.stderr)
        return 2

    body = json.dumps(
        {
            "amount": amount,
            "currency": "INR",
            "description": "agent-maker-checker infra/verify_live.py smoke test",
            "notes": {"source": "agent-maker-checker", "script": "infra/verify_live.py"},
        }
    ).encode()

    auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Razorpay API returned HTTP {exc.code}:", file=sys.stderr)
        print(detail, file=sys.stderr)
        if exc.code == 401:
            print(
                "\n401 usually means the key id/secret pair is wrong, or the "
                "secret was rotated in the dashboard after you copied it.",
                file=sys.stderr,
            )
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach {API_URL}: {exc.reason}", file=sys.stderr)
        return 1

    amount_rupees = payload.get("amount", amount) / 100
    print("Created a real test-mode Payment Link:")
    print(f"  id:        {payload.get('id')}")
    print(f"  short_url: {payload.get('short_url')}")
    print(f"  amount:    {payload.get('amount')} paise (Rs {amount_rupees:.2f})")
    print(f"  status:    {payload.get('status')}")
    print()
    print("Proof for a reviewer:")
    print("  1. Open https://dashboard.razorpay.com and sign in.")
    print("  2. Flip the Test/Live toggle (top-left) to Test Mode.")
    print("  3. Go to Payments -> Payment Links.")
    print(
        f"  4. Find the link with id {payload.get('id')} "
        f"(created just now, Rs {amount_rupees:.2f})."
    )
    print(
        f"  5. Optionally open {payload.get('short_url')} and pay with a Razorpay test "
        "card or the test UPI VPA success@razorpay to mint a real pay_ id -- needed to "
        "exercise issue_refund end-to-end (see infra/razorpay-mcp.md, 'Test-mode caveats')."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
