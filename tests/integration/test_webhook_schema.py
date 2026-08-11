"""The outbound event contract, checked against events the code really emits.

`docs/reference/webhook-events.json` is generated from the models in
`services/events.py`, so the schema and the models cannot disagree. What that
does *not* prove is that the models describe what the running code emits — a
model nobody constructs would export a beautiful schema for an event that never
looks like that.

So this drives every one of the eight event types through the real flows and
validates the delivered bytes, the ones `outbound_delivery.event_body` signs,
against the exported union.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.api.middleware import get_tron_gateway
from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import AccountKind, Asset, EntryKind, OutboundEvent
from crypto_processing_api.services import events as event_service
from crypto_processing_api.workers import outbound_delivery, reconciliation, webhook_processor
from tests.fake_tron import DESTINATION as TRON_DESTINATION
from tests.fake_tron import HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, USDT, bearer, post_webhook

HALF_BTC = "0.50000000"
DEST = "bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq"
USDT_GROSS = 200_000_000
USDT_NET = USDT_GROSS - 1_000_000
TRON_TXID = "7" * 64


@pytest.fixture
def fake_tron() -> FakeTronGrid:
    return FakeTronGrid()


def test_every_event_type_constant_has_a_model() -> None:
    """A new event type with no envelope would ship untyped to both SDKs."""
    modelled = set(event_service.PLATFORM_EVENT_ADAPTER.json_schema()["discriminator"]["mapping"])
    assert modelled == set(event_service.EVENT_TYPES)


def _credit(session: Session, *, user: str, amount: int, asset: str) -> None:
    hot = ledger.get_system_account(session, asset_id=asset, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(session, asset_id=asset, external_user_id=user)
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=asset,
        source_ref=f"schema:{asset}:{uuid.uuid4().hex}",
        postings=[(hot.id, amount), (available.id, -amount)],
    )
    session.commit()


def _deposit(client: TestClient, key: str, user: str, idem: str) -> dict[str, Any]:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": user, "asset": BTC},
        headers={**bearer(key), "Idempotency-Key": idem},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _withdraw(
    client: TestClient, key: str, *, user: str, asset: str, amount: int, dest: str
) -> str:
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": asset,
            "amount": str(amount),
            "destination_address": dest,
        },
        headers={**bearer(key), "Idempotency-Key": f"schema-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    withdrawal_id: str = response.json()["withdrawal_id"]
    return withdrawal_id


def test_every_emitted_event_validates_against_the_exported_schema(
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
    admin = bearer(admin_key)

    # deposit.detected + deposit.settled
    _deposit(client, readwrite_key, "schema-settled", "schema-d1")
    settled_invoice = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(settled_invoice, HALF_BTC)
    fake_btcpay.settle(settled_invoice)
    post_webhook(
        client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", settled_invoice)
    )
    webhook_processor.process_pending(session_factory, fake_btcpay)

    # deposit.review_required — a payment that arrived after expiry
    _deposit(client, readwrite_key, "schema-review", "schema-d2")
    review_invoice = next(i for i in fake_btcpay.invoices if i != settled_invoice)
    fake_btcpay.add_payment(review_invoice, HALF_BTC)
    fake_btcpay.expire(review_invoice, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", review_invoice))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    # deposit.expired — the window closed with nothing received
    _deposit(client, readwrite_key, "schema-expired", "schema-d3")
    empty_invoice = next(
        i for i in fake_btcpay.invoices if i not in (settled_invoice, review_invoice)
    )
    fake_btcpay.expire(empty_invoice)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", empty_invoice))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    # withdrawal.pending_approval + withdrawal.failed
    _credit(session, user="schema-btc", amount=200_000_000, asset=BTC)
    queued = _withdraw(
        client, readwrite_key, user="schema-btc", asset=BTC, amount=1_000_000, dest=DEST
    )
    rejected = client.post(
        f"/v1/admin/withdrawals/{queued}/reject", json={"reason": "wrong address"}, headers=admin
    )
    assert rejected.status_code == 200, rejected.text

    # withdrawal.broadcast + withdrawal.completed, over the operator-sent rail
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)
    monkeypatch.setattr(settings, "tron_confirmations", 19)
    app.dependency_overrides[get_tron_gateway] = lambda: fake_tron
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    usdt.enabled = True
    session.commit()

    _credit(session, user="schema-usdt", amount=500_000_000, asset=USDT)
    sending = _withdraw(
        client,
        readwrite_key,
        user="schema-usdt",
        asset=USDT,
        amount=USDT_GROSS,
        dest=TRON_DESTINATION,
    )
    assert (
        client.post(f"/v1/admin/withdrawals/{sending}/approve", json={}, headers=admin).status_code
        == 200
    )
    fake_tron.add_transfer(TRON_TXID, recipient=TRON_DESTINATION, amount=USDT_NET)
    marked = client.post(
        f"/v1/admin/withdrawals/{sending}/mark-broadcast", json={"txid": TRON_TXID}, headers=admin
    )
    assert marked.status_code == 200, marked.text
    fake_tron.block_height = 1_100
    reconciliation.sweep_manual_withdrawals(session_factory, fake_tron, settings)

    session.rollback()
    rows = list(session.execute(select(OutboundEvent).order_by(OutboundEvent.created_at)).scalars())
    assert rows, "no events were emitted, so this test proves nothing"

    for event in rows:
        # The delivered bytes, not the stored row: this is what the platform
        # verifies the signature over and then parses.
        delivered = json.loads(outbound_delivery.event_body(event))
        parsed = event_service.PLATFORM_EVENT_ADAPTER.validate_python(delivered)
        assert parsed.type == event.event_type
        # `model_dump` round-trips to the same document, so the schema is not
        # quietly dropping a field the server actually sends.
        assert parsed.model_dump() == delivered, f"{event.event_type} carries an unmodelled field"

    produced = {event.event_type for event in rows}
    assert produced == set(event_service.EVENT_TYPES), (
        f"this test did not exercise {sorted(set(event_service.EVENT_TYPES) - produced)}"
    )


def test_a_failed_withdrawal_always_says_why() -> None:
    """`reason` is required on the failure envelope, so it can never be absent.

    An operator reading `withdrawal.failed` with no reason has a held balance
    and nothing to act on. Every emit site names a cause, so the model makes
    the field required rather than optional and the schema says so.
    """
    decision = event_service.PLATFORM_EVENT_ADAPTER.json_schema()["$defs"]["WithdrawalDecisionData"]
    assert "reason" in decision["required"]
