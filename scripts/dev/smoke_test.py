#!/usr/bin/env python
"""End-to-end deposit and withdrawal drills against the regtest stack.

    docker compose -f deploy/docker-compose.regtest.yml up -d
    python scripts/bootstrap_btcpay.py
    python scripts/dev/smoke_test.py                 # everything but `late`
    python scripts/dev/smoke_test.py --drill late    # adds ~3 minutes

Every assertion is about money: the exact number of satoshis credited, and the
number of times it was credited. Drills:

  deposit    create a deposit, pay it, mine, assert the balance to the satoshi
  outage     stop the api, pay, mine, restart — the poller has to credit it,
             because that is the whole claim of the reconciliation design
  replay     ask BTCPay to redeliver a delivery repeatedly; still one credit
  late       pay after the invoice expires — must land in review, not be
             credited, and an admin resolve then credits it
  withdraw   below the limit, auto-approved, exact to the satoshi
  approval   above the limit, waits for an admin, then completes
  crash      payout created, status never written, no double send

The Lightning drills need the overlay stack and are skipped without it:

    docker compose -f deploy/docker-compose.regtest.yml \
                   -f deploy/docker-compose.regtest.lightning.yml up -d
    sh scripts/dev/ln_bootstrap.sh
    LIGHTNING_ENABLED=true python scripts/bootstrap_btcpay.py
    python scripts/dev/smoke_test.py --drill lightning

  ln_deposit     pay a BOLT11 from another node; instant settle, exact sats
  ln_withdraw    pay an invoice; payment hash recorded, routing fee booked
  ln_exhausted   ask for more than the channel can route; the deadline
                 cancels it and the node's own answer releases the hold
  ln_expired     an expired invoice is refused before any balance is held

Before the first drill, a readiness gate waits for BTCPay to actually be able
to issue a deposit invoice — see `wait_for_deposit_capability`. On a cold stack
the 101 blocks mined above take NBXplorer a while to index, and until it has,
invoice creation is refused. `--readiness-timeout` bounds the wait.

This is a manual script, not part of pytest: it needs the whole stack, and two
of the drills wait on real wall-clock deadlines.
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
LIGHTNING_COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.regtest.lightning.yml"
GENERATED_ENV = REPO_ROOT / ".env.regtest.generated"

API_URL = "http://127.0.0.1:8095"
SATS = Decimal(100_000_000)
WALLET = "regtest"

BTC = "BTC"
BTC_LN = "BTC_LN"

#: Whose deposits the readiness gate creates. No drill reads this user, so the
#: probe cannot move a number any assertion depends on.
READINESS_USER = "readiness-probe"

#: Set once `main` knows whether a Lightning drill was asked for. The overlay
#: has to be on every compose call or `exec` cannot see the LND containers.
COMPOSE_FILES: list[Path] = [COMPOSE_FILE]


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
    files: list[str] = []
    for path in COMPOSE_FILES:
        files.extend(["-f", str(path)])
    return run("docker", "compose", *files, *args, **kwargs)


def lncli(node: str, *args: str) -> Any:
    """One `lncli` call against a node in the overlay stack."""
    raw = compose("exec", "-T", node, "lncli", "--network=regtest", "--lnddir=/data", *args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


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

    def create_deposit(
        self, user: str, idempotency_key: str, *, asset: str = BTC
    ) -> dict[str, Any]:
        response = self.client.post(
            "/v1/deposits",
            json={"external_user_id": user, "asset": asset},
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code != 201:
            raise SmokeFailure(f"deposit creation failed: {response.status_code} {response.text}")
        return dict(response.json())

    def deposit(self, deposit_id: str) -> dict[str, Any]:
        response = self.client.get(f"/v1/deposits/{deposit_id}")
        response.raise_for_status()
        return dict(response.json())

    def create_withdrawal(
        self,
        user: str,
        sats: int,
        destination: str,
        idempotency_key: str,
        *,
        asset: str = BTC,
    ) -> dict[str, Any]:
        response = self.client.post(
            "/v1/withdrawals",
            json={
                "external_user_id": user,
                "asset": asset,
                "amount": str(sats),
                "destination_address": destination,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code != 201:
            raise SmokeFailure(f"withdrawal request failed: {response.status_code} {response.text}")
        return dict(response.json())

    def deposit_attempt(
        self, user: str, idempotency_key: str, *, asset: str = BTC
    ) -> tuple[int, str]:
        """A creation attempt that reports its refusal instead of raising.

        The readiness gate needs the status code, and `create_deposit` folds it
        into a message.
        """
        response = self.client.post(
            "/v1/deposits",
            json={"external_user_id": user, "asset": asset},
            headers={"Idempotency-Key": idempotency_key},
        )
        return response.status_code, response.text

    def withdrawal_refusal(
        self, user: str, sats: int, destination: str, idempotency_key: str, *, asset: str = BTC
    ) -> tuple[int, str]:
        """A request expected to be refused. Returns the status and the detail."""
        response = self.client.post(
            "/v1/withdrawals",
            json={
                "external_user_id": user,
                "asset": asset,
                "amount": str(sats),
                "destination_address": destination,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except ValueError:
            detail = response.text[:200]
        return response.status_code, detail

    def withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        response = self.client.get(f"/v1/withdrawals/{withdrawal_id}")
        response.raise_for_status()
        return dict(response.json())

    def admin_withdrawals(self, status: str) -> list[dict[str, Any]]:
        response = self.client.get(f"/v1/admin/withdrawals?status={status}")
        response.raise_for_status()
        return list(response.json()["withdrawals"])

    def approve(self, withdrawal_id: str) -> dict[str, Any]:
        response = self.client.post(f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={})
        if response.status_code != 200:
            raise SmokeFailure(f"approve failed: {response.status_code} {response.text}")
        return dict(response.json())

    def ledger_balances(self, user: str, *, asset: str = BTC) -> dict[str, int]:
        """Read the ledger directly: there is no balances endpoint before M5."""
        available = psql(
            "SELECT COALESCE(-balance, 0) FROM accounts WHERE kind = 'user_available' "
            f"AND external_user_id = '{user}' AND asset_id = '{asset}'"
        )
        held = psql(
            "SELECT COALESCE(-balance, 0) FROM accounts WHERE kind = 'user_hold' "
            f"AND external_user_id = '{user}' AND asset_id = '{asset}'"
        )
        return {"available": int(available or 0), "held": int(held or 0)}

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


# -- readiness ------------------------------------------------------------
#
# `synchronized` from BTCPay before the drills start is not enough. The first
# thing a cold run does is mine 101 blocks so coinbase outputs mature, and
# NBXplorer then has 101 blocks to index — during which BTCPay refuses to issue
# an invoice, because it cannot get an address for a derivation scheme it is
# still catching up on. On fast hardware the indexing finishes before anyone
# asks; on slow hardware drill 1 fails 0.6 seconds in with a 502 that looks
# like a bug in this service and is not one.


#: The one refusal that means "still warming up". The api turns any 4xx from
#: BTCPay on invoice creation into this 502. Everything else a deposit request
#: can answer with is a real failure and must not be waited out.
NOT_READY_DETAIL = "BTCPay rejected the invoice request"


def btcpay_not_ready(status_code: int, body: str) -> bool:
    """True only for the cold-start refusal the readiness gate waits out."""
    return status_code == 502 and NOT_READY_DETAIL in body


def wait_for_btcpay_sync(generated: dict[str, str], *, timeout: float) -> bool:
    """The cheap check: has NBXplorer caught up with the node?

    `/api/v1/health` needs no key and answers `{"synchronized": false}` while
    any chain is behind, which is exactly the state freshly mined blocks put it
    in. It is a hint rather than a verdict — it says nothing about the store's
    wallet — so a timeout here is reported and not fatal. The invoice probe
    below is what decides.
    """
    url = f"{generated['BTCPAY_PUBLIC_URL'].rstrip('/')}/api/v1/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=10)
            if response.status_code == 200 and response.json().get("synchronized"):
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(2.0)
    log(f"BTCPay never reported synchronized within {timeout:.0f}s; trying an invoice anyway")
    return False


def wait_for_invoice_capability(
    probe: Callable[[], tuple[int, str]],
    *,
    timeout: float,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Block until `probe` can create a deposit invoice. Returns the attempt count.

    Retries the cold-start 502 and nothing else: a 401, a 422 or a 500 means
    the run is broken and waiting three more minutes only delays saying so.
    """
    deadline = monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        status_code, body = probe()
        if 200 <= status_code < 300:
            return attempt
        if not btcpay_not_ready(status_code, body):
            raise SmokeFailure(
                f"deposit creation answered {status_code}, which is not the cold-start "
                f"refusal this gate waits out: {body[:300]}"
            )
        if monotonic() >= deadline:
            raise SmokeFailure(
                f"BTCPay could still not issue a deposit invoice after {timeout:.0f}s and "
                f"{attempt} attempts. Last answer: {status_code} {body[:300]}"
            )
        log(f"BTCPay not ready for invoices yet (attempt {attempt}), waiting...")
        sleep(interval)


def wait_for_deposit_capability(api: Api, generated: dict[str, str], *, timeout: float) -> None:
    """Prove the stack can issue a deposit invoice before any drill asserts anything.

    The probe deposits belong to a user no drill touches, so the exact-satoshi
    assertions are unaffected. A successful one is abandoned rather than paid:
    it expires on its own, and the alternative — handing it to drill 1 — would
    make the first drill depend on a code path the other six do not use.
    """
    deadline = time.monotonic() + timeout
    wait_for_btcpay_sync(generated, timeout=timeout)
    # A floor, so that a slow sync check cannot leave the probe no time at all.
    remaining = max(15.0, deadline - time.monotonic())
    attempts = wait_for_invoice_capability(
        lambda: api.deposit_attempt(READINESS_USER, f"ready-{time.time()}"),
        timeout=remaining,
    )
    # Always, even when it passed first time. A gate that is silent when it
    # works cannot be told apart in a log from a gate that is not there.
    log(f"BTCPay can issue a deposit invoice (attempt {attempts}); starting the drills")


def psql(statement: str) -> str:
    """Run one query against the regtest ledger database."""
    return compose(
        "exec",
        "-T",
        "postgres-ledger",
        "psql",
        "-U",
        "cpapi",
        "-d",
        "cpapi",
        "-t",
        "-A",
        "-c",
        statement,
    )


def system_balance(kind: str, *, asset: str) -> int:
    raw = psql(
        "SELECT COALESCE(balance, 0) FROM accounts "
        f"WHERE kind = '{kind}' AND asset_id = '{asset}' AND external_user_id IS NULL"
    )
    return int(raw or 0)


def destination_received(address: str) -> Decimal:
    total = bitcoin_cli(f"-rpcwallet={WALLET}", "getreceivedbyaddress", address, "0")
    return Decimal(str(total))


def btcpay_client(generated: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=generated["BTCPAY_PUBLIC_URL"],
        headers={"Authorization": f"token {generated['BTCPAY_API_KEY']}"},
        timeout=30.0,
    )


def wait_for_settled_payment(
    generated: dict[str, str], deposit_id: str, payment_id: str, *, timeout: float = 180
) -> None:
    """Block until BTCPay calls this payment `Settled`.

    A deposit reaches `review` as soon as a payment is *recorded*, and a
    payment is recorded in any status but `Invalid` — so the review queue shows
    a late payment while it is still `Processing`. Resolving one in that state
    is refused with a 404 saying so, correctly: `Processing` can still become
    `Invalid`, and crediting it would mint a balance for money that never
    arrived.

    So the wait belongs here, before the resolve, not as a retry around it.
    Without it this drill passed on a fast machine and failed on a slower one,
    which is the worst kind of green.

    The invoice id is deliberately absent from the public deposit response, so
    it comes from the row — the same choice `capture_greenfield.py` makes,
    rather than widening the API for a dev script.
    """
    invoice_id = psql(f"SELECT btcpay_invoice_id FROM deposits WHERE id = '{deposit_id}'").strip()
    if not invoice_id:
        raise SmokeFailure(f"deposit {deposit_id} has no invoice id to ask BTCPay about")

    client = btcpay_client(generated)

    def settled() -> str | None:
        response = client.get(f"/api/v1/invoices/{invoice_id}/payment-methods")
        if response.status_code >= 400:
            raise SmokeFailure(
                f"could not read invoice {invoice_id}: {response.status_code} {response.text[:200]}"
            )
        for method in response.json():
            for payment in method.get("payments") or []:
                if payment.get("id") == payment_id:
                    status = str(payment.get("status"))
                    return status if status == "Settled" else None
        return None

    wait_until(
        settled,
        timeout=timeout,
        description=f"BTCPay to settle the late payment {payment_id[:16]}... before it is resolved",
    )


def wait_for_withdrawal(
    api: Api, withdrawal_id: str, status: str, *, timeout: float = 300
) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        body = api.withdrawal(withdrawal_id)
        if body["status"] == status:
            return body
        if body["status"] in ("rejected", "refunded", "failed") and status != body["status"]:
            raise SmokeFailure(
                f"withdrawal {withdrawal_id} ended in {body['status']}: {body['failure_reason']}"
            )
        return None

    return wait_until(  # type: ignore[no-any-return]
        check, timeout=timeout, description=f"withdrawal {withdrawal_id} to reach {status}"
    )


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


def drill_late(api: Api, generated: dict[str, str]) -> None:
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

    # The queue lists the payment before BTCPay has finished with it. Resolving
    # now is refused, and rightly so; wait for the settlement the resolve path
    # is going to re-check anyway.
    wait_for_settled_payment(generated, deposit_id, payments[0]["payment_id"])
    log("  BTCPay settled the late payment; asking an admin to credit it")

    resolved = api.resolve(deposit_id, payments[0]["payment_id"])
    if resolved["credited"] != f"{amount:.8f}":
        raise SmokeFailure(f"resolve credited {resolved['credited']}, expected {amount:.8f}")
    assert_credited(api.deposit(deposit_id), amount)
    log(f"  admin resolve credited exactly {amount:.8f} BTC")


def drill_withdraw(api: Api) -> None:
    log("drill: withdraw — below the limit, auto-approved, exact to the satoshi")
    deposit_amount = Decimal("0.5")
    # Below the seeded 500_000 sat auto-approval limit, so no admin is needed.
    withdraw_sats = 400_000

    deposit = api.create_deposit("wd-user", f"wd-{time.time()}")
    pay(deposit["address"], deposit_amount)
    mine(2)
    wait_for_status(api, deposit["deposit_id"], "settled")

    destination = str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress"))
    before = destination_received(destination)
    # A delta, not an absolute: the drill is re-runnable and the user keeps
    # whatever earlier runs left them.
    ledger_before = api.ledger_balances("wd-user")

    created = api.create_withdrawal("wd-user", withdraw_sats, destination, f"w-{time.time()}")
    if created["status"] == "approved":
        log(f"  withdrawal {created['withdrawal_id']} auto-approved")
    elif "24h cap" in (created.get("approval_reason") or ""):
        # Expected when the drill has been run several times today: the rolling
        # cap is per asset, not per run, and tripping it is the control doing
        # its job. Approve and carry on — the assertions that matter are about
        # amounts.
        log(f"  24h cap reached from earlier runs ({created['approval_reason']}); approving")
        api.approve(created["withdrawal_id"])
    else:
        raise SmokeFailure(
            f"expected auto-approval, got {created['status']}: {created.get('approval_reason')}"
        )

    # The payout has to be mined before BTCPay will call it Completed, and the
    # regtest chain only moves when we tell it to.
    wait_for_withdrawal(api, created["withdrawal_id"], "broadcast", timeout=180)
    mine(2)
    confirmed = wait_for_withdrawal(api, created["withdrawal_id"], "confirmed", timeout=300)
    fee = int(Decimal(confirmed["fee"]) * SATS)
    net = int(Decimal(confirmed["amount_net"]) * SATS)
    if net != withdraw_sats - fee:
        raise SmokeFailure(f"net {net} != gross {withdraw_sats} - fee {fee}")
    if not confirmed["txid"]:
        raise SmokeFailure("confirmed without a txid")
    short_txid = confirmed["txid"][:16]
    log(f"  confirmed: gross {withdraw_sats}, fee {fee}, net {net}, txid {short_txid}...")

    time.sleep(3)
    received = destination_received(destination) - before
    expected = Decimal(net) / SATS
    if received != expected:
        raise SmokeFailure(f"destination received {received} BTC, expected {expected}")
    log(f"  destination received exactly {received} BTC")

    balances = api.ledger_balances("wd-user")
    expected_available = ledger_before["available"] - withdraw_sats
    if balances["available"] != expected_available:
        raise SmokeFailure(f"available is {balances['available']}, expected {expected_available}")
    if balances["held"] != ledger_before["held"]:
        raise SmokeFailure(
            f"held moved from {ledger_before['held']} to {balances['held']}; this "
            "withdrawal's hold should have been extinguished by the settle entry"
        )
    log(f"  user debited exactly {withdraw_sats} sat, this hold fully released")


def drill_approval(api: Api) -> None:
    log("drill: approval — above the limit waits for an admin, then completes")
    deposit = api.create_deposit("approve-user", f"ap-{time.time()}")
    pay(deposit["address"], Decimal("0.2"))
    mine(2)
    wait_for_status(api, deposit["deposit_id"], "settled")

    destination = str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress"))
    # The seeded auto limit is 500_000 sat.
    created = api.create_withdrawal("approve-user", 900_000, destination, f"ap-w-{time.time()}")
    if created["status"] != "pending_approval":
        raise SmokeFailure(f"expected pending_approval, got {created['status']}")
    log(f"  queued: {created.get('approval_reason')}")

    queued = api.admin_withdrawals("pending_approval")
    if created["withdrawal_id"] not in [w["withdrawal_id"] for w in queued]:
        raise SmokeFailure("the withdrawal is not in the admin queue")

    api.approve(created["withdrawal_id"])
    wait_for_withdrawal(api, created["withdrawal_id"], "broadcast", timeout=180)
    mine(2)
    confirmed = wait_for_withdrawal(api, created["withdrawal_id"], "confirmed", timeout=300)
    log(f"  approved and confirmed, txid {confirmed['txid'][:16]}...")


def drill_crash(api: Api, generated: dict[str, str]) -> None:
    log("drill: crash — payout created, status never written, no double send")

    deposit = api.create_deposit("crash-user", f"cr-{time.time()}")
    pay(deposit["address"], Decimal("0.2"))
    mine(2)
    wait_for_status(api, deposit["deposit_id"], "settled")

    destination = str(bitcoin_cli(f"-rpcwallet={WALLET}", "getnewaddress"))
    before = destination_received(destination)

    # Stop the worker so nothing submits behind our back, then reproduce the
    # crash window by hand: create the payout in BTCPay exactly as the
    # submitter would, and never record it.
    log("  stopping the worker")
    compose("stop", "worker")
    try:
        created = api.create_withdrawal("crash-user", 400_000, destination, f"cr-w-{time.time()}")
        withdrawal_id = created["withdrawal_id"]

        btcpay = httpx.Client(
            base_url=generated["BTCPAY_PUBLIC_URL"],
            headers={"Authorization": f"token {generated['BTCPAY_API_KEY']}"},
            timeout=60.0,
        )
        response = btcpay.post(
            f"/api/v1/stores/{generated['BTCPAY_STORE_ID']}/payouts",
            json={
                "destination": destination,
                "amount": "0.00397000",
                "payoutMethodId": "BTC-CHAIN",
                "approved": True,
                "metadata": {"cpapi": True, "withdrawal_id": withdrawal_id},
            },
        )
        if response.status_code >= 400:
            raise SmokeFailure(f"could not stage the crash: {response.text[:300]}")
        payout_id = response.json()["id"]
        log(f"  payout {payout_id} created with no local record")

        # Put the row where a crash would have left it: submitting, no ref.
        result = psql(
            "UPDATE withdrawals SET status = 'submitting', "
            "updated_at = now() - interval '1 hour' WHERE id = '" + withdrawal_id + "'"
        )
        staged = psql(
            "SELECT status || ',' || COALESCE(backend_ref, 'none') FROM withdrawals "
            "WHERE id = '" + withdrawal_id + "'"
        )
        log(f"  staged the crash window: {result.strip()} -> {staged.strip()}")
        if not staged.startswith("submitting,none"):
            raise SmokeFailure(f"could not stage the crash window: row is {staged!r}")
    finally:
        log("  starting the worker")
        compose("start", "worker")

    def resolved() -> dict[str, Any] | None:
        body = api.withdrawal(withdrawal_id)
        return body if body["status"] in ("submitted", "broadcast", "confirmed") else None

    # The stuck-resolution job runs on the withdrawal sweep interval, so give
    # it a couple of cycles.
    body = wait_until(
        resolved, timeout=420, description="reconciliation to adopt the payout", interval=5
    )
    log(f"  reconciliation adopted the payout: status {body['status']}")

    payouts = httpx.get(
        f"{generated['BTCPAY_PUBLIC_URL']}/api/v1/stores/{generated['BTCPAY_STORE_ID']}/payouts",
        headers={"Authorization": f"token {generated['BTCPAY_API_KEY']}"},
        timeout=30,
    ).json()
    mine_ours = [
        p for p in payouts if (p.get("metadata") or {}).get("withdrawal_id") == withdrawal_id
    ]
    if len(mine_ours) != 1:
        raise SmokeFailure(
            f"expected exactly one payout for this withdrawal, found {len(mine_ours)}"
        )
    log("  exactly one payout exists: no double send")

    wait_for_withdrawal(api, withdrawal_id, "broadcast", timeout=180)
    mine(2)
    confirmed = wait_for_withdrawal(api, withdrawal_id, "confirmed", timeout=300)
    received = destination_received(destination) - before
    expected = Decimal(confirmed["amount_net"])
    if received != expected:
        raise SmokeFailure(f"destination received {received}, expected {expected}")
    log(f"  destination received exactly {received} BTC, once")


# -- lightning drills ------------------------------------------------------
#
# The counterparties are two separate nodes on purpose, and the reason is drill
# 10. `lnd-user` has a large channel *to* the store, so it can pay deposits;
# `lnd-payee` sits behind a small channel *from* the store, so its inbound
# capacity is what a withdrawal runs out of. Share one node between the two
# roles and every deposited satoshi becomes a satoshi we can pay straight back,
# which makes a liquidity failure impossible to stage.

#: Matches SEED_LN_WITHDRAWAL_FLAT_FEE, which the overlay leaves at its default.
LN_FLAT_FEE = 100
#: Comfortably above the payee's channel, so no route can carry it.
LN_UNROUTABLE_NET = 2_999_900
#: Long enough to clear the submission guard's two-minute margin, short enough
#: that the drill does not wait out a wallet's usual hour.
LN_UNROUTABLE_EXPIRY = 240


def ln_addinvoice(node: str, *, sats: int | None = None, expiry: int | None = None) -> str:
    args = ["addinvoice", "--memo", f"cpapi-drill-{time.time()}"]
    if sats is not None:
        args += ["--amt", str(sats)]
    if expiry is not None:
        args += ["--expiry", str(expiry)]
    body = lncli(node, *args)
    request = str(body.get("payment_request", "")) if isinstance(body, dict) else ""
    if not request.startswith("lnbcrt"):
        raise SmokeFailure(f"{node} did not return a regtest invoice: {body!r}")
    return request


def ln_pay(node: str, invoice: str, *, sats: int | None = None) -> None:
    args = ["payinvoice", "--force", "--json"]
    if sats is not None:
        args += ["--amt", str(sats)]
    args.append(invoice)
    body = lncli(node, *args)
    text = json.dumps(body) if isinstance(body, dict) else str(body)
    if '"SUCCEEDED"' not in text and "SUCCEEDED" not in text:
        raise SmokeFailure(f"{node} could not pay the invoice: {text[:400]}")


def ln_pubkey(node: str) -> str:
    body = lncli(node, "getinfo")
    key = str(body.get("identity_pubkey", "")) if isinstance(body, dict) else ""
    if len(key) != 66:
        raise SmokeFailure(f"{node} did not report an identity pubkey: {body!r}")
    return key


def ln_outbound_towards(peer_node: str) -> int:
    """What the store's node can send *through* one peer, in satoshis.

    Not `channelbalance`, which is the total across every channel. That number
    grows with each deposit, because a deposit paid to us lands on our side of
    the depositor's channel — so it says nothing about whether a withdrawal to
    somebody else can be routed, and using it made drill 10 refuse to run.
    """
    body = lncli("lnd-btcpay", "listchannels", "--peer", ln_pubkey(peer_node))
    if not isinstance(body, dict):
        raise SmokeFailure(f"unreadable listchannels: {body!r}")
    return sum(int(channel.get("local_balance", 0)) for channel in body.get("channels", []))


def drill_ln_deposit(api: Api) -> None:
    log("drill: ln_deposit — pay a BOLT11 from another node, settle at once")
    sats = 1_500_000

    deposit = api.create_deposit("ln-user", f"lnd-{time.time()}", asset=BTC_LN)
    invoice = deposit["address"]
    if not invoice.startswith("lnbcrt"):
        raise SmokeFailure(f"a Lightning deposit must hand out an invoice, got {invoice[:24]}...")
    log(f"  deposit {deposit['deposit_id']} -> {invoice[:32]}...")

    before = api.ledger_balances("ln-user", asset=BTC_LN)
    # The invoice is amountless, so the payer chooses. No blocks are mined:
    # a settled HTLC is final the moment it settles.
    ln_pay("lnd-user", invoice, sats=sats)

    body = wait_for_status(api, deposit["deposit_id"], "settled", timeout=120)
    expected = f"{Decimal(sats) / SATS:.8f}"
    if body["amount_credited"] != expected:
        raise SmokeFailure(f"expected {expected} credited, got {body['amount_credited']}")

    after = api.ledger_balances("ln-user", asset=BTC_LN)
    if after["available"] - before["available"] != sats:
        raise SmokeFailure(
            f"available moved by {after['available'] - before['available']}, expected {sats}"
        )
    log(f"  credited exactly {sats} sat with no confirmations to wait for")


def drill_ln_withdraw(api: Api) -> None:
    log("drill: ln_withdraw — routing fee booked, payment hash recorded")
    net = 100_000
    gross = net + LN_FLAT_FEE

    balances = api.ledger_balances("ln-user", asset=BTC_LN)
    if balances["available"] < gross:
        deposit = api.create_deposit("ln-user", f"lnw-fund-{time.time()}", asset=BTC_LN)
        ln_pay("lnd-user", deposit["address"], sats=gross * 3)
        wait_for_status(api, deposit["deposit_id"], "settled", timeout=120)
        balances = api.ledger_balances("ln-user", asset=BTC_LN)

    # `lnd-far`, not `lnd-payee`. A payment to a direct peer pays nobody to
    # forward it, so the routing fee would be exactly zero — a true number and
    # no evidence whatever that the fee is read from the node, converted from
    # millisatoshi and journalled. Going one hop further makes `lnd-payee`
    # charge for the forward.
    invoice = ln_addinvoice("lnd-far", sats=net)
    hot_before = system_balance("hot_wallet", asset=BTC_LN)
    expense_before = system_balance("network_fee_expense", asset=BTC_LN)
    channel_before = ln_outbound_towards("lnd-payee")

    created = api.create_withdrawal("ln-user", gross, invoice, f"lnw-{time.time()}", asset=BTC_LN)
    if created["status"] == "pending_approval":
        log(f"  queued ({created.get('approval_reason')}); approving")
        api.approve(created["withdrawal_id"])
    log(f"  withdrawal {created['withdrawal_id']} for {gross} sat gross")

    confirmed = wait_for_withdrawal(api, created["withdrawal_id"], "confirmed", timeout=300)

    txid = confirmed["txid"] or ""
    if len(txid) != 64:
        raise SmokeFailure(f"expected a 64-character payment hash, got {txid!r}")
    if int(Decimal(confirmed["fee"]) * SATS) != LN_FLAT_FEE:
        raise SmokeFailure(f"fee is {confirmed['fee']}, expected the flat {LN_FLAT_FEE} sat")
    if int(Decimal(confirmed["amount_net"]) * SATS) != net:
        raise SmokeFailure(f"net is {confirmed['amount_net']}, expected {net}")

    routing_fee = system_balance("network_fee_expense", asset=BTC_LN) - expense_before
    if routing_fee <= 0:
        raise SmokeFailure(
            f"the routing fee booked {routing_fee}; a forwarded payment must cost "
            "something, so a zero here means the fee was never read from the node "
            "(or the route went direct and this drill is not testing what it claims)"
        )

    hot_after = system_balance("hot_wallet", asset=BTC_LN)
    if hot_before - hot_after != net + routing_fee:
        raise SmokeFailure(
            f"hot_wallet fell by {hot_before - hot_after}, expected {net + routing_fee} "
            f"(net {net} + routing fee {routing_fee})"
        )
    if system_balance("payouts_in_flight", asset=BTC_LN) != 0:
        raise SmokeFailure("payouts_in_flight did not unwind")

    channel_drop = channel_before - ln_outbound_towards("lnd-payee")
    log(
        f"  paid {net} sat, hash {txid[:16]}..., routing fee {routing_fee} sat booked; "
        f"ledger custody -{net + routing_fee}, channel -{channel_drop}"
    )
    if channel_drop > net + routing_fee:
        raise SmokeFailure(
            f"the channel fell by {channel_drop} but the ledger only accounts for "
            f"{net + routing_fee}; that difference is unbooked custody"
        )


def drill_ln_exhausted(api: Api) -> None:
    log("drill: ln_exhausted — no route, the deadline cancels, the node proves it")
    gross = LN_UNROUTABLE_NET + LN_FLAT_FEE

    balances = api.ledger_balances("ln-drain", asset=BTC_LN)
    if balances["available"] < gross:
        deposit = api.create_deposit("ln-drain", f"lnx-fund-{time.time()}", asset=BTC_LN)
        ln_pay("lnd-user", deposit["address"], sats=gross + 100_000)
        wait_for_status(api, deposit["deposit_id"], "settled", timeout=120)
        balances = api.ledger_balances("ln-drain", asset=BTC_LN)

    outbound = ln_outbound_towards("lnd-payee")
    if outbound >= gross:
        raise SmokeFailure(
            f"the store node can send {outbound} sat through lnd-payee, which is more "
            f"than the {gross} sat this drill asks for — re-run "
            "scripts/dev/ln_bootstrap.sh, that channel is meant to be the limit"
        )

    # A short expiry, because the release waits for it. BTCPay does not leave
    # an unroutable payout where it can be cancelled — it hands it to the
    # Lightning processor, which parks it `InProgress`, and `DELETE` on one of
    # those answers 400. So "the node says Failed" is not on its own enough to
    # give the hold back: the rail has not stopped and could try again. What
    # settles it is the invoice dying, after which nobody can pay it.
    invoice = ln_addinvoice("lnd-payee", sats=LN_UNROUTABLE_NET, expiry=LN_UNROUTABLE_EXPIRY)
    created = api.create_withdrawal("ln-drain", gross, invoice, f"lnx-{time.time()}", asset=BTC_LN)
    withdrawal_id = created["withdrawal_id"]
    if created["status"] == "pending_approval":
        log(f"  queued ({created.get('approval_reason')}); approving")
        api.approve(withdrawal_id)

    def taken_up() -> dict[str, Any] | None:
        body = api.withdrawal(withdrawal_id)
        return body if body["status"] in ("submitted", "broadcast") else None

    held = wait_until(
        taken_up, timeout=180, description="the payout to be created and attempted", interval=3
    )
    log(f"  {held['status']} and unroutable; balance held ({held['amount_gross']})")

    def refunded() -> dict[str, Any] | None:
        body = api.withdrawal(withdrawal_id)
        return body if body["status"] == "refunded" else None

    # The deadline (60s in the overlay), then the invoice expiry, then the job
    # that acts on both. Minutes, not an afternoon.
    body = wait_until(
        refunded,
        timeout=480,
        description="the deadline to pass, the invoice to expire, and the node to prove it unpaid",
        interval=5,
    )

    row = psql(
        "SELECT released_by || '|' || COALESCE(release_attestation, '') FROM withdrawals "
        f"WHERE id = '{withdrawal_id}'"
    )
    released_by, _, attestation = row.partition("|")
    if released_by.strip() != "auto:definitive_failure_proof":
        raise SmokeFailure(
            f"released by {released_by!r}; the hold should have been given back on the "
            "node's own answer, not by anything else"
        )
    if "did not leave" not in attestation and "cannot be paid" not in attestation:
        raise SmokeFailure(f"the attestation does not say the funds stayed put: {attestation!r}")

    balances_after = api.ledger_balances("ln-drain", asset=BTC_LN)
    if balances_after["available"] != balances["available"]:
        raise SmokeFailure(
            f"available is {balances_after['available']}, expected the pre-withdrawal "
            f"{balances['available']}"
        )
    if balances_after["held"] != 0:
        raise SmokeFailure(f"{balances_after['held']} sat is still held")
    if system_balance("payouts_in_flight", asset=BTC_LN) != 0:
        raise SmokeFailure("payouts_in_flight did not unwind after the release")

    log(f"  refunded on proof, nothing held, status {body['status']}")


def drill_ln_expired(api: Api) -> None:
    log("drill: ln_expired — an expired invoice is refused before any hold")
    gross = 50_000 + LN_FLAT_FEE

    balances = api.ledger_balances("ln-user", asset=BTC_LN)
    before_rows = psql(
        "SELECT COUNT(*) FROM withdrawals WHERE external_user_id = 'ln-user' "
        f"AND asset_id = '{BTC_LN}'"
    )

    invoice = ln_addinvoice("lnd-payee", sats=50_000, expiry=1)
    log("  waiting for the invoice to expire")
    time.sleep(3)

    status, detail = api.withdrawal_refusal(
        "ln-user", gross, invoice, f"lnexp-{time.time()}", asset=BTC_LN
    )
    if status != 422:
        raise SmokeFailure(f"expected 422 for an expired invoice, got {status}: {detail}")
    if "expires at" not in detail:
        raise SmokeFailure(f"the refusal does not mention the expiry: {detail!r}")

    after_rows = psql(
        "SELECT COUNT(*) FROM withdrawals WHERE external_user_id = 'ln-user' "
        f"AND asset_id = '{BTC_LN}'"
    )
    if after_rows.strip() != before_rows.strip():
        raise SmokeFailure(
            f"a withdrawal row was created for a refused request "
            f"({before_rows.strip()} -> {after_rows.strip()})"
        )
    balances_after = api.ledger_balances("ln-user", asset=BTC_LN)
    if balances_after != balances:
        raise SmokeFailure(f"balances moved on a refused request: {balances} -> {balances_after}")

    log(f"  refused with 422, no row and no hold ({detail[:70]}...)")


LIGHTNING_DRILLS = ("ln_deposit", "ln_withdraw", "ln_exhausted", "ln_expired")
DRILLS = (
    "deposit",
    "outage",
    "replay",
    "late",
    "withdraw",
    "approval",
    "crash",
    *LIGHTNING_DRILLS,
)


#: The order drills run in. `deposit` first because everything else assumes a
#: funded wallet, and the Lightning ones last because they need the overlay.
DRILL_ORDER = DRILLS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drill",
        action="append",
        choices=[*DRILLS, "all", "fast", "lightning"],
        help=(
            "repeatable. 'fast' is the on-chain set without `late`; 'lightning' is the "
            "four BTC_LN drills; 'all' is everything. Default: fast."
        ),
    )
    parser.add_argument(
        "--readiness-timeout",
        type=float,
        default=180.0,
        help=(
            "seconds to wait for BTCPay to become able to issue a deposit invoice, "
            "before the first drill. A warm stack passes on the first attempt; a cold "
            "one on slow hardware needs the wait. Default: 180."
        ),
    )
    args = parser.parse_args()

    selected = set(args.drill or ["fast"])
    if "all" in selected:
        selected = set(DRILLS)
    else:
        if "fast" in selected:
            selected.discard("fast")
            selected |= {"deposit", "outage", "replay", "withdraw", "approval", "crash"}
        if "lightning" in selected:
            selected.discard("lightning")
            selected |= set(LIGHTNING_DRILLS)

    if selected & set(LIGHTNING_DRILLS):
        if not LIGHTNING_COMPOSE_FILE.is_file():
            raise SmokeFailure(f"{LIGHTNING_COMPOSE_FILE} is missing")
        COMPOSE_FILES.append(LIGHTNING_COMPOSE_FILE)

    generated = read_generated_env()
    if selected & set(LIGHTNING_DRILLS) and generated.get("LIGHTNING_ENABLED") != "true":
        raise SmokeFailure(
            "the Lightning drills need a stack bootstrapped with LIGHTNING_ENABLED=true. "
            "Run: LIGHTNING_ENABLED=true python scripts/bootstrap_btcpay.py, then recreate "
            "the api and worker containers."
        )

    wait_for_api()
    fund_node()
    api_key = create_api_key()
    api = Api(api_key)
    # After the mining above, not before: those 101 blocks are what NBXplorer
    # has to catch up on, and the whole point of this gate is to wait for that.
    wait_for_deposit_capability(api, generated, timeout=args.readiness_timeout)

    runners: dict[str, Callable[[], None]] = {
        "deposit": lambda: drill_deposit(api),
        "outage": lambda: drill_outage(api_key),
        "replay": lambda: drill_replay(api, generated),
        "late": lambda: drill_late(api, generated),
        "withdraw": lambda: drill_withdraw(api),
        "approval": lambda: drill_approval(api),
        "crash": lambda: drill_crash(api, generated),
        "ln_deposit": lambda: drill_ln_deposit(api),
        "ln_withdraw": lambda: drill_ln_withdraw(api),
        "ln_exhausted": lambda: drill_ln_exhausted(api),
        "ln_expired": lambda: drill_ln_expired(api),
    }
    for name in DRILL_ORDER:
        if name in selected:
            runners[name]()

    log(f"all drills passed: {', '.join(name for name in DRILL_ORDER if name in selected)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as failure:
        log(f"FAILED: {failure}")
        raise SystemExit(1) from failure
