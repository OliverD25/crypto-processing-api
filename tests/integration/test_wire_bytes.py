"""The wire format, pinned to the byte.

Every integration in the world parses these responses, and the promise made in
`docs/reference/versioning.md` is that a patch or minor release does not move a
byte of them. Comparing parsed JSON cannot keep that promise: `json.loads` is
blind to key order, to `"+00:00"` becoming `"Z"`, and to an amount string
turning into a JSON number. All three are things a Pydantic `response_model`
does silently and by default, and all three break a client.

So this compares **raw response bytes** against a committed corpus. The corpus
was captured from the routes before any response model existed, which is what
makes it evidence rather than a restatement of the current code.

Values that genuinely differ per run — uuids, clock timestamps, the package
version, the generated api-key ids — are replaced with fixed tokens before the
comparison. The replacement for a timestamp deliberately requires the
`+00:00` offset: if anything ever re-serializes a datetime into `...Z`, the
pattern stops matching and this test goes red, which is the whole point.

Regenerating the corpus is a deliberate act, never a fix for a red build:

    WIRE_GOLDEN_WRITE=1 pytest tests/integration/test_wire_bytes.py

A diff in `tests/fixtures/wire/responses.json` is a wire-format change and
belongs in the CHANGELOG's Breaking / Migration section.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.api import health
from crypto_processing_api.api.middleware import get_tron_gateway
from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import (
    AccountKind,
    Asset,
    EntryKind,
    OutboundEvent,
    WalletTxoAlert,
    WithdrawalStatus,
    WorkerHeartbeat,
)
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import webhook_processor
from tests.conftest import REPO_ROOT
from tests.fake_tron import DESTINATION as TRON_DESTINATION
from tests.fake_tron import HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, USDT, bearer, post_webhook

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "wire" / "responses.json"

DEST = "bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq"
DEST2 = "bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj"
HALF_BTC = "0.50000000"
TRON_TXID = "9" * 64
PINNED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

#: Uuids appear as resource ids, as cursors and inside `evt_` event ids.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: The offset is part of the pattern on purpose — see the module docstring.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?")


class Corpus:
    """Route name to the exact bytes that route answered with."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.entries: dict[str, dict[str, Any]] = {}
        self.volatile: list[str] = []

    def volatile_value(self, value: str) -> str:
        """Register a per-run value (an api-key id) for scrubbing."""
        self.volatile.append(value)
        return value

    def record(self, name: str, method: str, url: str, **kwargs: Any) -> Any:
        response = self.client.request(method, url, **kwargs)
        assert name not in self.entries, f"duplicate corpus entry {name}"
        self.entries[name] = {
            "status": response.status_code,
            "body": self._scrub(response.content),
        }
        return response

    def _scrub(self, raw: bytes) -> str:
        text = raw.decode("utf-8")
        for value in self.volatile:
            text = text.replace(value, "<volatile>")
        text = _UUID.sub("<uuid>", text)
        text = _TIMESTAMP.sub("<timestamp+00:00>", text)
        # Only inside the version field: a semver-shaped substring elsewhere
        # would be part of the contract, not noise.
        text = re.sub(r'("version":")' + _VERSION.pattern + r'(")', r"\1<version>\2", text, count=1)
        return text


def _credit(session: Session, *, user: str, amount: int, asset: str, ref: str) -> None:
    """A deposit credit with a fixed `source_ref`, so history reads the same twice."""
    hot = ledger.get_system_account(session, asset_id=asset, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(session, asset_id=asset, external_user_id=user)
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=asset,
        source_ref=ref,
        postings=[(hot.id, amount), (available.id, -amount)],
    )
    session.commit()


def _request_withdrawal(
    corpus: Corpus, name: str, key: str, *, user: str, amount: int, asset: str, destination: str
) -> Any:
    return corpus.record(
        name,
        "POST",
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": asset,
            "amount": str(amount),
            "destination_address": destination,
        },
        headers={**bearer(key), "Idempotency-Key": f"wire-{name}"},
    )


def build_corpus(
    *,
    client: TestClient,
    app: FastAPI,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Corpus:
    corpus = Corpus(client)
    rw = bearer(readwrite_key)
    admin = bearer(admin_key)
    corpus.volatile_value(readwrite_key.removeprefix("cpk_test_")[:8])
    corpus.volatile_value(admin_key.removeprefix("cpk_test_")[:8])

    # -- health ------------------------------------------------------------
    # `readyz` reaches for the gateway directly rather than through Depends,
    # so the app-level override does not reach it.
    monkeypatch.setattr(health, "get_gateway", lambda: fake_btcpay)
    session.add(WorkerHeartbeat(job_name="deposit_poller", last_run_at=datetime.now(UTC)))
    session.commit()

    corpus.record("healthz", "GET", "/healthz")
    corpus.record("readyz", "GET", "/readyz")

    # -- deposits ----------------------------------------------------------
    created = corpus.record(
        "createDeposit",
        "POST",
        "/v1/deposits",
        json={"external_user_id": "wire-user", "asset": BTC, "expected_amount": "50000000"},
        headers={**rw, "Idempotency-Key": "wire-deposit-1"},
    )
    deposit_id = created.json()["deposit_id"]
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    corpus.record("getDeposit", "GET", f"/v1/deposits/{deposit_id}", headers=rw)
    corpus.record("listUserDeposits", "GET", "/v1/users/wire-user/deposits?limit=1", headers=rw)
    corpus.record(
        "getAddressHistory", "GET", f"/v1/deposits/{deposit_id}/address-history", headers=rw
    )

    # -- balances, history, reference data ---------------------------------
    corpus.record("getUserBalances", "GET", "/v1/users/wire-user/balances", headers=rw)
    corpus.record(
        "getUserTransactions", "GET", "/v1/users/wire-user/transactions?limit=1", headers=rw
    )
    corpus.record("listAssets", "GET", "/v1/assets", headers=rw)

    # -- withdrawals -------------------------------------------------------
    _request_withdrawal(
        corpus,
        "createWithdrawal",
        readwrite_key,
        user="wire-user",
        amount=100_000,
        asset=BTC,
        destination=DEST,
    )
    pending = _request_withdrawal(
        corpus,
        "createWithdrawalPendingApproval",
        readwrite_key,
        user="wire-user",
        amount=1_000_000,
        asset=BTC,
        destination=DEST2,
    )
    pending_id = pending.json()["withdrawal_id"]

    corpus.record("getWithdrawal", "GET", f"/v1/withdrawals/{pending_id}", headers=rw)
    corpus.record(
        "listUserWithdrawals", "GET", "/v1/users/wire-user/withdrawals?limit=1", headers=rw
    )

    # -- admin: deposits ---------------------------------------------------
    review = corpus.record(
        "createDepositForReview",
        "POST",
        "/v1/deposits",
        json={"external_user_id": "wire-late", "asset": BTC},
        headers={**rw, "Idempotency-Key": "wire-deposit-2"},
    ).json()
    review_invoice = next(i for i in fake_btcpay.invoices if i != invoice_id)
    review_payment = fake_btcpay.add_payment(review_invoice, HALF_BTC)
    fake_btcpay.expire(review_invoice, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", review_invoice))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    corpus.record("adminReviewQueue", "GET", "/v1/admin/deposits/review", headers=admin)
    corpus.record(
        "adminResolveDeposit",
        "POST",
        f"/v1/admin/deposits/{review['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": review_payment},
        headers=admin,
    )

    session.add(
        WalletTxoAlert(
            asset_id=BTC,
            txid="c" * 64,
            amount=12_345,
            confirmations=3,
            status="open",
            note="no deposit payment matches this receive",
            detected_at=PINNED,
        )
    )
    session.commit()
    corpus.record("adminWalletAlerts", "GET", "/v1/admin/wallet-alerts", headers=admin)

    # -- admin: withdrawals ------------------------------------------------
    corpus.record("adminWithdrawalQueue", "GET", "/v1/admin/withdrawals?limit=1", headers=admin)
    corpus.record(
        "adminApproveWithdrawal",
        "POST",
        f"/v1/admin/withdrawals/{pending_id}/approve",
        json={"note": "checked against the ticket"},
        headers=admin,
    )

    to_reject = _request_withdrawal(
        corpus,
        "createWithdrawalToReject",
        readwrite_key,
        user="wire-user",
        amount=1_100_000,
        asset=BTC,
        destination=DEST,
    ).json()["withdrawal_id"]
    corpus.record(
        "adminRejectWithdrawal",
        "POST",
        f"/v1/admin/withdrawals/{to_reject}/reject",
        json={"reason": "destination is not the user's"},
        headers=admin,
    )

    # A release needs a status where a transaction may already exist, which is
    # exactly why it demands an attestation.
    to_release = _request_withdrawal(
        corpus,
        "createWithdrawalToRelease",
        readwrite_key,
        user="wire-user",
        amount=1_200_000,
        asset=BTC,
        destination=DEST2,
    ).json()["withdrawal_id"]
    withdrawal_service.approve(session, uuid.UUID(to_release), actor="wire-admin")
    row = withdrawal_service.get(session, uuid.UUID(to_release))
    row.status = WithdrawalStatus.SUBMITTING
    session.commit()
    corpus.record(
        "adminReleaseWithdrawal",
        "POST",
        f"/v1/admin/withdrawals/{to_release}/release",
        json={"attestation": "checked the mempool and the node; nothing was ever broadcast"},
        headers=admin,
    )

    # -- admin: USDT mark-broadcast ---------------------------------------
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)
    app.dependency_overrides[get_tron_gateway] = lambda: fake_tron
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    usdt.enabled = True
    session.commit()
    _credit(session, user="wire-usdt", amount=500_000_000, asset=USDT, ref="wire:usdt:0")
    usdt_id = _request_withdrawal(
        corpus,
        "createWithdrawalUsdt",
        readwrite_key,
        user="wire-usdt",
        amount=200_000_000,
        asset=USDT,
        destination=TRON_DESTINATION,
    ).json()["withdrawal_id"]
    client.post(f"/v1/admin/withdrawals/{usdt_id}/approve", json={}, headers=admin)
    fake_tron.add_transfer(TRON_TXID, recipient=TRON_DESTINATION, amount=199_000_000)
    corpus.record(
        "adminMarkBroadcast",
        "POST",
        f"/v1/admin/withdrawals/{usdt_id}/mark-broadcast",
        json={"txid": TRON_TXID},
        headers=admin,
    )

    # -- admin: operations -------------------------------------------------
    event = OutboundEvent(
        id=uuid.UUID("018f0000-0000-7000-8000-000000000001"),
        event_type="deposit.settled",
        payload={"deposit_id": str(uuid.UUID(int=1)), "amount_credited": HALF_BTC},
        status="dead",
        attempts=10,
        last_error="502 from the platform",
        next_attempt_at=PINNED,
        created_at=PINNED,
    )
    session.add(event)
    session.commit()
    corpus.record("adminListEvents", "GET", "/v1/admin/events?status=dead", headers=admin)
    corpus.record(
        "adminRedeliverEvent", "POST", f"/v1/admin/events/{event.id}/redeliver", headers=admin
    )
    corpus.record("adminReconciliation", "GET", "/v1/admin/reconciliation", headers=admin)

    # -- BTCPay ingress ----------------------------------------------------
    raw, headers = fake_btcpay.sign(
        fake_btcpay.webhook_payload("InvoiceSettled", invoice_id, delivery_id="wire-del-1")
    )
    corpus.record("btcpayWebhook", "POST", "/webhooks/btcpay", content=raw, headers=headers)
    raw, headers = fake_btcpay.sign(
        fake_btcpay.webhook_payload(
            "InvoiceSettled", invoice_id, delivery_id="wire-del-2", store_id="someone-else"
        )
    )
    corpus.record("btcpayWebhookIgnored", "POST", "/webhooks/btcpay", content=raw, headers=headers)

    # -- the error envelope ------------------------------------------------
    corpus.record("errorUnauthorized", "GET", "/v1/assets")
    corpus.record("errorForbidden", "GET", "/v1/admin/wallet-alerts", headers=rw)
    corpus.record("errorNotFound", "GET", f"/v1/deposits/{uuid.UUID(int=2)}", headers=rw)
    corpus.record(
        "errorMissingIdempotencyKey",
        "POST",
        "/v1/deposits",
        json={"external_user_id": "wire-user", "asset": BTC},
        headers=rw,
    )
    corpus.record(
        "errorValidation",
        "POST",
        "/v1/withdrawals",
        json={
            "external_user_id": "wire-user",
            "asset": BTC,
            "amount": "1000",
            "destination_address": "not-an-address",
        },
        headers={**rw, "Idempotency-Key": "wire-bad-destination"},
    )
    corpus.record(
        "errorInsufficientBalance",
        "POST",
        "/v1/withdrawals",
        json={
            "external_user_id": "wire-broke",
            "asset": BTC,
            "amount": "100000",
            "destination_address": DEST,
        },
        headers={**rw, "Idempotency-Key": "wire-broke-1"},
    )
    return corpus


@pytest.fixture
def fake_tron() -> FakeTronGrid:
    return FakeTronGrid()


def test_every_route_still_answers_with_the_same_bytes(
    client: TestClient,
    app: FastAPI,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credit(session, user="wire-user", amount=200_000_000, asset=BTC, ref="wire:btc:0")
    corpus = build_corpus(
        client=client,
        app=app,
        session=session,
        session_factory=session_factory,
        fake_btcpay=fake_btcpay,
        fake_tron=fake_tron,
        readwrite_key=readwrite_key,
        admin_key=admin_key,
        monkeypatch=monkeypatch,
    )

    if os.environ.get("WIRE_GOLDEN_WRITE"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(
            json.dumps(corpus.entries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip(f"rewrote {GOLDEN}")

    assert GOLDEN.exists(), f"{GOLDEN} is missing; regenerate it with WIRE_GOLDEN_WRITE=1"
    golden: dict[str, dict[str, Any]] = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert sorted(corpus.entries) == sorted(golden), "the set of covered routes changed"
    for name, expected in golden.items():
        actual = corpus.entries[name]
        assert actual["status"] == expected["status"], f"{name}: status changed"
        assert actual["body"] == expected["body"], f"{name}: response bytes changed"


def test_every_route_has_an_operation_id_and_pinned_bytes(app: FastAPI) -> None:
    """A route with no corpus entry is wire surface nobody is watching.

    The `operation_id` is what a generated SDK names the method, so it is also
    the natural key for the corpus: the two cannot drift apart without one of
    these assertions firing.
    """
    covered = set(json.loads(GOLDEN.read_text(encoding="utf-8")))

    operations: set[str] = set()
    unnamed: list[str] = []
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        if not isinstance(getattr(route, "methods", None), set):
            continue
        if not getattr(route, "include_in_schema", False) or path.startswith("/_probe"):
            continue
        operation_id = getattr(route, "operation_id", None)
        if operation_id is None:
            unnamed.append(path)
        else:
            operations.add(operation_id)

    assert not unnamed, f"routes without an operation_id: {sorted(unnamed)}"
    assert not operations - covered, (
        f"routes with no pinned wire bytes: {sorted(operations - covered)}"
    )
