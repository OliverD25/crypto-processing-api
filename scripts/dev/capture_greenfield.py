#!/usr/bin/env python
"""Record real Greenfield responses from the regtest stack as test fixtures.

    docker compose -f deploy/docker-compose.regtest.yml up -d
    python scripts/bootstrap_btcpay.py
    python scripts/dev/capture_greenfield.py

Why this exists, in one sentence: the integration suite drives `FakeBTCPay`,
so when the same change edits both the fake and the code that reads it, the
tests prove the two agree with each other and say nothing about whether either
agrees with BTCPay.

That is not hypothetical here. R3 replaces the boundary where BTCPay's literal
payout strings meet the withdrawal state machine with a canonical enum, and
co-mutates the fake to speak the new states in the same change. Without a
corpus captured from the real server, the suite after that change is circular.

So: drive real deposits and real payouts through the running 2.4.2, snapshot
the raw response bodies, and commit them. The normalizer is then asserted
against BTCPay's own bytes, with no fake involved.

The files are raw `response.json()`, not model dumps. A fixture that has been
through our parsing cannot prove our parsing is right.

Re-running overwrites the corpus. Do that deliberately — on a BTCPay version
bump, where a changed payload is exactly the news you want.
"""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dev"))

from smoke_test import (  # noqa: E402
    WALLET,
    Api,
    bitcoin_cli,
    create_api_key,
    fund_node,
    mine,
    pay,
    psql,
    read_generated_env,
    wait_for_api,
    wait_for_status,
    wait_for_withdrawal,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "greenfield"
BTCPAY_URL = "http://127.0.0.1:14142"
BTCPAY_VERSION = "2.4.2"


def log(message: str) -> None:
    print(f"[capture] {message}", flush=True)


class Greenfield:
    """A deliberately thin client. The point is the raw body."""

    def __init__(self, api_key: str, store_id: str) -> None:
        self.store_id = store_id
        self.client = httpx.Client(
            base_url=BTCPAY_URL,
            headers={"Authorization": f"token {api_key}"},
            timeout=30.0,
        )

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        response = self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def delete(self, path: str) -> int:
        return self.client.delete(path).status_code

    def payout(self, payout_id: str) -> Any:
        return self.get(f"/api/v1/payouts/{payout_id}")


def write(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"wrote {path.relative_to(REPO_ROOT)}")


def capture_payout_states(
    greenfield: Greenfield, payout_id: str, *, timeout: float = 300.0
) -> dict[str, Any]:
    """Snapshot the payout every time BTCPay changes its mind about the state.

    Polling rather than reacting to webhooks is deliberate: the point is to
    record what `GET /payouts/{id}` returns, because that is the exact call
    `poll_status` makes in production.
    """
    seen: dict[str, Any] = {}
    deadline = time.time() + timeout
    terminal = {"Completed", "Cancelled"}

    while time.time() < deadline:
        body = greenfield.payout(payout_id)
        state = str(body["state"])
        if state not in seen:
            seen[state] = body
            log(f"  payout {payout_id[:12]}... observed in {state}")
        if state in terminal:
            break
        # The regtest chain only moves when told, and the payout processor
        # runs on a 60s minimum interval, so nudge both.
        mine(1)
        time.sleep(5)

    return seen


def capture_invoice_states(api: Api, greenfield: Greenfield) -> None:
    log("capturing invoice payloads through a real deposit")
    deposit = api.create_deposit("capture-user", f"cap-{time.time()}")
    # The invoice id is deliberately not in the public deposit response, so
    # read it from the row rather than widening the API for a dev script.
    invoice_id = psql(
        f"SELECT btcpay_invoice_id FROM deposits WHERE id = '{deposit['deposit_id']}'"
    ).strip()
    if not invoice_id:
        raise RuntimeError(f"deposit {deposit['deposit_id']} has no invoice id yet")

    write("invoice_new", greenfield.get(f"/api/v1/invoices/{invoice_id}"))
    write(
        "invoice_payment_methods_new",
        greenfield.get(f"/api/v1/invoices/{invoice_id}/payment-methods"),
    )

    pay(deposit["address"], Decimal("0.4"))
    # One block is a confirmation but below the settle threshold, which is the
    # only way to see Processing rather than skipping straight to Settled.
    time.sleep(6)
    write("invoice_processing", greenfield.get(f"/api/v1/invoices/{invoice_id}"))

    mine(2)
    wait_for_status(api, deposit["deposit_id"], "settled")
    write("invoice_settled", greenfield.get(f"/api/v1/invoices/{invoice_id}"))
    write(
        "invoice_payment_methods_settled",
        greenfield.get(f"/api/v1/invoices/{invoice_id}/payment-methods"),
    )


def capture_awaiting_payment(greenfield: Greenfield, destination: str) -> None:
    """The state our payouts are born in, caught before the processor moves it.

    Captured from its own payout rather than from the withdrawal drill because
    the payout processor runs on a 60-second minimum interval and will claim
    the row between two polls — which is exactly what happened on the first
    capture run, leaving this state missing from the corpus.

    Cancelled immediately afterwards so a fixture run never leaves an approved
    payout for the processor to pay.
    """
    log("capturing AwaitingPayment (created approved, snapshotted, then cancelled)")
    body = greenfield.post(
        f"/api/v1/stores/{greenfield.store_id}/payouts",
        {
            "destination": destination,
            "amount": "0.0005",
            "payoutMethodId": "BTC-CHAIN",
            "approved": True,
            "metadata": {"cpapi": True, "source": "fixture-capture"},
        },
    )
    write("payout_awaiting_payment", greenfield.payout(body["id"]))
    greenfield.delete(f"/api/v1/payouts/{body['id']}")


def capture_awaiting_approval(greenfield: Greenfield, destination: str) -> None:
    """The one state our own code never produces.

    `create_payout` sends `approved: true` on purpose — the approval decision
    is ours and BTCPay's queue would be a second one nobody watches. So this
    payout is created unapproved solely to record the payload, then cancelled.
    It is a real BTCPay response either way, which is the whole requirement.
    """
    log("capturing AwaitingApproval (created unapproved, then cancelled)")
    body = greenfield.post(
        f"/api/v1/stores/{greenfield.store_id}/payouts",
        {
            "destination": destination,
            "amount": "0.001",
            "payoutMethodId": "BTC-CHAIN",
            "approved": False,
            "metadata": {"cpapi": True, "source": "fixture-capture"},
        },
    )
    write("payout_awaiting_approval", greenfield.payout(body["id"]))
    greenfield.delete(f"/api/v1/payouts/{body['id']}")
    write("payout_cancelled", greenfield.payout(body["id"]))


def capture_payout_lifecycle(api: Api, greenfield: Greenfield) -> None:
    log("capturing payout payloads through a real withdrawal")
    destination = str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress"))

    created = api.create_withdrawal("capture-user", 300_000, destination, f"cap-w-{time.time()}")
    if created["status"] != "approved":
        api.approve(created["withdrawal_id"])

    # The payout id only exists once the submitter has run.
    wait_for_withdrawal(api, created["withdrawal_id"], "submitted", timeout=180)
    payout_id = psql(
        f"SELECT backend_ref FROM withdrawals WHERE id = '{created['withdrawal_id']}'"
    ).strip()
    log(f"  withdrawal {created['withdrawal_id'][:12]}... -> payout {payout_id}")

    states = capture_payout_states(greenfield, payout_id)
    for state, body in states.items():
        write(f"payout_{_snake(state)}", body)


def _snake(state: str) -> str:
    out: list[str] = []
    for index, char in enumerate(state):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def write_manifest(captured: list[str]) -> None:
    write(
        "MANIFEST",
        {
            "btcpay_version": BTCPAY_VERSION,
            "captured_by": "scripts/dev/capture_greenfield.py",
            "source": "regtest stack, deploy/docker-compose.regtest.yml",
            "why": (
                "Real Greenfield bodies, so the normalizer can be asserted against "
                "BTCPay rather than against FakeBTCPay. See the module docstring."
            ),
            "files": sorted(captured),
        },
    )


def main() -> int:
    generated = read_generated_env()
    store_id = generated["BTCPAY_STORE_ID"]
    greenfield = Greenfield(generated["BTCPAY_API_KEY"], store_id)

    wait_for_api()
    fund_node()
    api = Api(create_api_key())

    capture_invoice_states(api, greenfield)
    capture_payout_lifecycle(api, greenfield)
    capture_awaiting_payment(greenfield, str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress")))
    capture_awaiting_approval(greenfield, str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress")))

    # Last, so the list carries every payout this run produced rather than
    # only the ones that existed halfway through.
    write(
        "payout_list",
        greenfield.get(
            f"/api/v1/stores/{store_id}/payouts",
            params={"includeCancelled": "true"},
        ),
    )

    captured = sorted(p.stem for p in FIXTURE_DIR.glob("*.json") if p.stem != "MANIFEST")
    write_manifest(captured)
    log(f"done — {len(captured)} payloads in {FIXTURE_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
