#!/usr/bin/env python
"""End-to-end BTC deposit drills against the regtest stack.

    docker compose -f deploy/docker-compose.regtest.yml up -d
    python scripts/bootstrap_btcpay.py
    python scripts/dev/smoke_test.py                 # deposit + outage + replay
    python scripts/dev/smoke_test.py --drill late    # adds ~3 minutes

Every assertion is about money: the exact number of satoshis credited, and the
number of times it was credited. Drills:

  deposit  create a deposit, pay it, mine, assert the balance to the satoshi
  outage   stop the api, pay, mine, restart — the poller has to credit it,
           because that is the whole claim of the reconciliation design
  replay   ask BTCPay to redeliver a delivery repeatedly; still one credit
  late     pay after the invoice expires — must land in review, not be
           credited, and an admin resolve then credits it

This is a manual script, not part of pytest: it needs the whole stack, and the
late drill waits for real wall-clock expiry.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.regtest.yml"
GENERATED_ENV = REPO_ROOT / ".env.regtest.generated"

API_URL = "http://127.0.0.1:8095"
SATS = Decimal(100_000_000)
WALLET = "regtest"


class SmokeFailure(AssertionError):
    pass


def log(message: str) -> None:
    print(f"[smoke] {message}", file=sys.stderr, flush=True)


def run(*args: str, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        args,
        capture_output=capture,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if check and result.returncode != 0:
        raise SmokeFailure(f"{' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return (result.stdout or "").strip()


def compose(*args: str, **kwargs: Any) -> str:
    return run("docker", "compose", "-f", str(COMPOSE_FILE), *args, **kwargs)


def bitcoin_cli(*args: str) -> Any:
    raw = compose("exec", "-T", "bitcoind", "bitcoin-cli", "-datadir=/data", *args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def read_generated_env() -> dict[str, str]:
    if not GENERATED_ENV.is_file():
        raise SmokeFailure(f"{GENERATED_ENV} not found — run scripts/bootstrap_btcpay.py first")
    values: dict[str, str] = {}
    for line in GENERATED_ENV.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


def wait_until(
    predicate: Callable[[], Any], *, timeout: float, description: str, interval: float = 2.0
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise SmokeFailure(f"timed out after {timeout}s waiting for {description} (last: {last!r})")


class Api:
    def __init__(self, key: str) -> None:
        self.client = httpx.Client(
            base_url=API_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
        )

    def create_deposit(self, user: str, idempotency_key: str) -> dict[str, Any]:
        response = self.client.post(
            "/v1/deposits",
            json={"external_user_id": user, "asset": "BTC"},
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code != 201:
            raise SmokeFailure(f"deposit creation failed: {response.status_code} {response.text}")
        return dict(response.json())

    def deposit(self, deposit_id: str) -> dict[str, Any]:
        response = self.client.get(f"/v1/deposits/{deposit_id}")
        response.raise_for_status()
        return dict(response.json())

    def review_queue(self) -> list[dict[str, Any]]:
        response = self.client.get("/v1/admin/deposits/review")
        response.raise_for_status()
        return list(response.json()["deposits"])

    def resolve(self, deposit_id: str, payment_id: str) -> dict[str, Any]:
        response = self.client.post(
            f"/v1/admin/deposits/{deposit_id}/resolve",
            json={"action": "credit", "payment_id": payment_id},
        )
        if response.status_code != 200:
            raise SmokeFailure(f"resolve failed: {response.status_code} {response.text}")
        return dict(response.json())


# -- environment -----------------------------------------------------------


def ensure_wallet() -> None:
    compose(
        "exec", "-T", "bitcoind", "bitcoin-cli", "-datadir=/data", "loadwallet", WALLET, check=False
    )
    compose(
        "exec",
        "-T",
        "bitcoind",
        "bitcoin-cli",
        "-datadir=/data",
        "createwallet",
        WALLET,
        check=False,
    )


def spendable_balance() -> Decimal:
    return Decimal(str(bitcoin_cli(f"-rpcwallet={WALLET}", "getbalance")))


def mine(blocks: int) -> None:
    address = bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress")
    bitcoin_cli("generatetoaddress", str(blocks), str(address))


def fund_node() -> None:
    ensure_wallet()
    if spendable_balance() < Decimal("1"):
        log("mining 101 blocks so coinbase outputs mature")
        mine(101)


def create_api_key() -> str:
    key = compose(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "crypto_processing_api.cli",
        "create-api-key",
        "--name",
        "smoke",
        "--scope",
        "admin",
    )
    if not key.startswith("cpk_"):
        raise SmokeFailure(f"unexpected key output: {key[:80]!r}")
    return key


def wait_for_api() -> None:
    def healthy() -> bool:
        try:
            return httpx.get(f"{API_URL}/healthz", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    wait_until(healthy, timeout=120, description="the api to answer /healthz")


def pay(address: str, amount_btc: Decimal) -> str:
    return str(bitcoin_cli(f"-rpcwallet={WALLET}", "sendtoaddress", address, str(amount_btc)))


def wait_for_status(
    api: Api, deposit_id: str, status: str, *, timeout: float = 120
) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        body = api.deposit(deposit_id)
        return body if body["status"] == status else None

    return wait_until(  # type: ignore[no-any-return]
        check, timeout=timeout, description=f"deposit {deposit_id} to reach {status}"
    )


def assert_credited(body: dict[str, Any], expected_btc: Decimal) -> None:
    expected = f"{expected_btc:.8f}"
    if body["amount_credited"] != expected:
        raise SmokeFailure(f"expected {expected} credited, got {body['amount_credited']}")
    credited = [p for p in body["payments"] if p["credited"]]
    if len(credited) != 1:
        raise SmokeFailure(f"expected exactly one credited payment, got {len(credited)}")


# -- drills ----------------------------------------------------------------


def drill_deposit(api: Api) -> None:
    log("drill: deposit — create, pay, mine, assert exact satoshis")
    amount = Decimal("0.5")
    deposit = api.create_deposit("smoke-user", f"smoke-{time.time()}")
    log(f"  deposit {deposit['deposit_id']} at {deposit['address']}")

    txid = pay(deposit["address"], amount)
    log(f"  paid {amount} BTC in {txid}")
    mine(2)

    body = wait_for_status(api, deposit["deposit_id"], "settled")
    assert_credited(body, amount)
    log(f"  credited exactly {body['amount_credited']} BTC")


def drill_outage(api_key: str) -> None:
    log("drill: outage — pay while the api is stopped, poller must credit it")
    api = Api(api_key)
    amount = Decimal("0.25")
    deposit = api.create_deposit("outage-user", f"outage-{time.time()}")

    log("  stopping the api container")
    compose("stop", "api")
    try:
        txid = pay(deposit["address"], amount)
        log(f"  paid {amount} BTC in {txid} with nothing listening for webhooks")
        mine(2)
        time.sleep(5)
    finally:
        log("  starting the api container")
        compose("start", "api")
        wait_for_api()

    body = wait_for_status(Api(api_key), deposit["deposit_id"], "settled", timeout=180)
    assert_credited(body, amount)
    log(f"  poller credited {body['amount_credited']} BTC with no webhook to help it")


def drill_replay(api: Api, generated: dict[str, str]) -> None:
    log("drill: replay — redeliver webhooks repeatedly, expect one credit")
    amount = Decimal("0.125")
    deposit = api.create_deposit("replay-user", f"replay-{time.time()}")
    pay(deposit["address"], amount)
    mine(2)
    body = wait_for_status(api, deposit["deposit_id"], "settled")
    assert_credited(body, amount)

    webhook_id = generated["BTCPAY_WEBHOOK_ID"]
    # The bootstrap key, not the admin's password: BTCPay only accepts Basic
    # auth for an account younger than five minutes, so anything that has to
    # keep working uses a key.
    btcpay = httpx.Client(
        base_url=generated["BTCPAY_PUBLIC_URL"],
        headers={"Authorization": f"token {generated['BTCPAY_BOOTSTRAP_KEY']}"},
        timeout=30.0,
    )
    response = btcpay.get(f"/api/v1/webhooks/{webhook_id}/deliveries")
    if response.status_code != 200:
        raise SmokeFailure(
            f"could not list webhook deliveries: {response.status_code} {response.text[:200]}"
        )
    deliveries = response.json()
    if not isinstance(deliveries, list) or not deliveries:
        raise SmokeFailure(f"BTCPay recorded no webhook deliveries: {deliveries!r}")

    replayed = 0
    for delivery in deliveries[:5]:
        response = btcpay.post(
            f"/api/v1/webhooks/{webhook_id}/deliveries/{delivery['id']}/redeliver"
        )
        if response.status_code < 400:
            replayed += 1
    log(f"  asked BTCPay to redeliver {replayed} deliveries")
    time.sleep(10)

    after = api.deposit(deposit["deposit_id"])
    assert_credited(after, amount)
    log("  still exactly one credit after the replay storm")


def drill_late(api: Api) -> None:
    log("drill: late — pay after expiry, expect review and an admin resolve")
    amount = Decimal("0.0625")
    deposit = api.create_deposit("late-user", f"late-{time.time()}")
    deposit_id = deposit["deposit_id"]

    expires_at = deposit["expires_at"]
    log(f"  waiting for the invoice to expire at {expires_at}")
    wait_until(
        lambda: api.deposit(deposit_id)["status"] == "expired",
        timeout=300,
        description="the invoice to expire",
        interval=5,
    )

    txid = pay(deposit["address"], amount)
    log(f"  paid {amount} BTC late in {txid}")
    mine(2)

    body = wait_for_status(api, deposit_id, "review", timeout=180)
    if body["amount_credited"] != "0.00000000":
        raise SmokeFailure("a late payment was auto-credited; it must wait for a human")
    log("  landed in review, uncredited")

    queued = [d for d in api.review_queue() if d["deposit_id"] == deposit_id]
    if not queued:
        raise SmokeFailure("the deposit is not in the admin review queue")
    payments = queued[0]["payments"]
    if not payments:
        raise SmokeFailure("review item has no recorded payment — money was dropped")

    resolved = api.resolve(deposit_id, payments[0]["payment_id"])
    if resolved["credited"] != f"{amount:.8f}":
        raise SmokeFailure(f"resolve credited {resolved['credited']}, expected {amount:.8f}")
    assert_credited(api.deposit(deposit_id), amount)
    log(f"  admin resolve credited exactly {amount:.8f} BTC")


DRILLS = ("deposit", "outage", "replay", "late")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drill",
        action="append",
        choices=[*DRILLS, "all", "fast"],
        help="repeatable; default is 'fast' (everything except the late drill)",
    )
    args = parser.parse_args()

    selected = set(args.drill or ["fast"])
    if "all" in selected:
        selected = set(DRILLS)
    elif "fast" in selected:
        selected = {"deposit", "outage", "replay"}

    generated = read_generated_env()
    wait_for_api()
    fund_node()
    api_key = create_api_key()
    api = Api(api_key)

    if "deposit" in selected:
        drill_deposit(api)
    if "outage" in selected:
        drill_outage(api_key)
    if "replay" in selected:
        drill_replay(api, generated)
    if "late" in selected:
        drill_late(api)

    log(f"all drills passed: {', '.join(sorted(selected))}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as failure:
        log(f"FAILED: {failure}")
        raise SystemExit(1) from failure
