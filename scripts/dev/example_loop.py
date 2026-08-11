#!/usr/bin/env python
"""Drive examples/platform-demo end to end, headlessly, against the regtest stack.

    COMPOSE_PROFILES=example docker compose -f deploy/docker-compose.regtest.yml up -d --build
    python scripts/bootstrap_btcpay.py
    COMPOSE_PROFILES=example docker compose -f deploy/docker-compose.regtest.yml \
        up -d --force-recreate api worker
    python scripts/dev/example_loop.py

This is the reason the tutorial cannot rot. It talks to the demo's own HTTP
surface — the same pages a browser gets — rather than to the API underneath, so
every assertion is about something a reader would actually see:

  1. sign in as a demo user
  2. ask the demo for a deposit address
  3. pay it with bitcoin-cli and mine two blocks
  4. wait for the deposit card to reach `settled`
  5. wait for the **balance table** to show the credit — the demo stores no
     balances, so this proves it read the API and rendered the answer
  6. wait for an activity line marked `[webhook]` — the delivery arrived, its
     signature verified, and the resource was re-read before anything was
     recorded
  7. ask for a withdrawal, mine, and wait for `confirmed` with a txid

Everything about the chain, the compose stack and waiting is imported from
`smoke_test`: two dev scripts spelling `bitcoin-cli` differently is how one of
them stops working without anybody noticing.

The nightly end-to-end job runs this after the drills.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import uuid
from decimal import Decimal

import httpx
from smoke_test import (
    SATS,
    Api,
    SmokeFailure,
    bitcoin_cli,
    create_api_key,
    fund_node,
    mine,
    pay,
    wait_until,
)

DEMO_URL = "http://127.0.0.1:8096"
DEMO_USER = "alice"

#: 0.5 BTC in, 400_000 sat back out. The withdrawal is under the seeded
#: 500_000 sat auto-approval limit, so an ordinary run needs no operator.
DEPOSIT_SATS = 50_000_000
WITHDRAW_SATS = 400_000


def log(message: str) -> None:
    print(f"[example] {message}", file=sys.stderr, flush=True)


def attribute(html: str, name: str) -> str | None:
    """One `data-` attribute out of a rendered fragment.

    The demo's templates carry these deliberately, and say so in a comment: a
    headless check of a page has to read *something*, and an attribute is
    stabler than the prose next to it.
    """
    found = re.search(rf'{name}="([^"]*)"', html)
    return found.group(1) if found else None


def refused(html: str) -> str | None:
    """The demo renders every refusal into the same fragment."""
    if 'data-problem="1"' not in html:
        return None
    message = re.search(r"<p>(.*?)</p>", html, re.DOTALL)
    return message.group(1).strip() if message else "the demo refused the request"


def btc_available(html: str) -> Decimal:
    found = re.search(r'data-asset="BTC" data-available="([^"]+)"', html)
    if not found:
        raise SmokeFailure(f"no BTC row in the balances fragment: {html[:300]!r}")
    return Decimal(found.group(1))


class Demo:
    """The demo application's own HTTP surface, as a browser sees it."""

    def __init__(self) -> None:
        self.client = httpx.Client(base_url=DEMO_URL, timeout=30.0, follow_redirects=True)

    def wait_until_up(self) -> None:
        def healthy() -> bool:
            try:
                return self.client.get("/healthz").status_code == 200
            except httpx.HTTPError:
                return False

        wait_until(healthy, timeout=180, description="the demo app to answer /healthz")

    def sign_in(self, user: str) -> None:
        self.client.post("/login", data={"user": user})
        page = self.client.get("/").text
        if attribute(page, "data-user") != user:
            raise SmokeFailure(f"signing in as {user} did not produce a dashboard")

    def get(self, path: str) -> str:
        response = self.client.get(path)
        response.raise_for_status()
        return response.text

    def post(self, path: str, data: dict[str, str]) -> str:
        response = self.client.post(path, data=data)
        response.raise_for_status()
        html = response.text
        problem = refused(html)
        if problem:
            raise SmokeFailure(f"the demo refused {path}: {problem}")
        return html


def wait_for_card(demo: Demo, path: str, status: str, *, timeout: float) -> str:
    def check() -> str | None:
        html = demo.get(path)
        seen = attribute(html, "data-status")
        if seen == status:
            return html
        if seen in ("failed", "rejected", "dismissed"):
            raise SmokeFailure(f"{path} ended in {seen} instead of reaching {status}")
        return None

    return wait_until(  # type: ignore[no-any-return]
        check, timeout=timeout, description=f"{path} to show {status}"
    )


def approve_if_queued(demo: Demo, withdrawal_id: str) -> None:
    """Let the run finish when the rolling 24h cap sent this one to an operator.

    The demo has no operator screen and should not have one — approving is an
    `admin` scope action and a platform backend holds a `readwrite` key. So the
    approval happens here, out of band, exactly as it would in real life.
    """
    html = demo.get(f"/withdrawals/{withdrawal_id}")
    if attribute(html, "data-status") != "pending_approval":
        return
    log("  queued for an operator (the rolling 24h cap); approving out of band")
    Api(create_api_key()).approve(withdrawal_id)


def run(*, check_webhooks: bool) -> None:
    demo = Demo()
    demo.wait_until_up()
    fund_node()
    demo.sign_in(DEMO_USER)
    log(f"signed in as {DEMO_USER}")

    opening = btc_available(demo.get("/balances"))
    log(f"  balance on the page before anything: {opening} BTC")

    # -- deposit ----------------------------------------------------------
    card = demo.post("/deposits", {"amount_sats": str(DEPOSIT_SATS)})
    deposit_id = attribute(card, "data-deposit-id")
    address = attribute(card, "data-address")
    if not deposit_id or not address:
        raise SmokeFailure(f"the deposit card carried no id or address: {card[:300]!r}")
    log(f"  deposit {deposit_id} at {address}")

    amount = Decimal(DEPOSIT_SATS) / SATS
    txid = pay(address, amount)
    log(f"  paid {amount} BTC in {txid[:16]}...")
    mine(2)

    wait_for_card(demo, f"/deposits/{deposit_id}", "settled", timeout=240)
    log("  the deposit card reached settled")

    expected = opening + amount

    def credited() -> bool:
        return btc_available(demo.get("/balances")) >= expected

    wait_until(credited, timeout=120, description=f"the balance table to show {expected} BTC")
    log(f"  the balance table shows {btc_available(demo.get('/balances'))} BTC")

    # -- the webhook path -------------------------------------------------
    if check_webhooks:

        def delivered() -> bool:
            return 'data-source="webhook"' in demo.get("/activity")

        wait_until(
            delivered,
            timeout=120,
            description=(
                "an activity line the webhook wrote. If this is the only failure, the "
                "stack was probably started with `--profile example` instead of "
                "COMPOSE_PROFILES=example, and the api has no webhook URL to deliver to"
            ),
        )
        log("  the activity feed shows a line a verified webhook wrote")

    # -- withdrawal -------------------------------------------------------
    destination = str(bitcoin_cli("-rpcwallet=regtest", "getnewaddress"))
    card = demo.post(
        "/withdrawals",
        {
            "amount": str(WITHDRAW_SATS),
            "destination": destination,
            # What the demo's own form puts in a hidden field, and what the SDK
            # sends as the Idempotency-Key.
            "request_id": str(uuid.uuid4()),
        },
    )
    withdrawal_id = attribute(card, "data-withdrawal-id")
    if not withdrawal_id:
        raise SmokeFailure(f"the withdrawal card carried no id: {card[:300]!r}")
    log(f"  withdrawal {withdrawal_id} is {attribute(card, 'data-status')}")

    approve_if_queued(demo, withdrawal_id)
    wait_for_card(demo, f"/withdrawals/{withdrawal_id}", "broadcast", timeout=300)
    mine(2)
    final = wait_for_card(demo, f"/withdrawals/{withdrawal_id}", "confirmed", timeout=300)
    sent = attribute(final, "data-txid")
    if not sent:
        raise SmokeFailure("the withdrawal reached confirmed with no txid on the page")
    log(f"  the withdrawal card reached confirmed, txid {sent[:16]}...")

    # The debit is gross: the fee comes out of what left the user's balance.
    def debited() -> bool:
        return btc_available(demo.get("/balances")) <= expected - Decimal(WITHDRAW_SATS) / SATS

    wait_until(debited, timeout=120, description="the balance table to show the debit")
    log(f"  the balance table shows {btc_available(demo.get('/balances'))} BTC")

    log("the example app did the whole loop: deposit, credit, webhook, withdrawal")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive examples/platform-demo end to end against the regtest stack."
    )
    parser.add_argument(
        "--no-webhooks",
        action="store_true",
        help=(
            "skip the assertion that a webhook arrived. For a stack started with "
            "`--profile example`, where the api has no webhook URL and the demo stays "
            "correct by polling alone."
        ),
    )
    args = parser.parse_args()

    started = time.monotonic()
    run(check_webhooks=not args.no_webhooks)
    log(f"done in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as failure:
        log(f"FAILED: {failure}")
        raise SystemExit(1) from failure
