"""The /webhooks/btcpay endpoint: signature, dedup, and what it refuses to do."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crypto_processing_api.ledger.models import WebhookEvent
from tests.fakes import FakeBTCPay
from tests.integration.conftest import post_webhook


def count_events(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one())


def make_invoice(fake: FakeBTCPay) -> str:
    invoice = fake.create_top_up_invoice(
        currency="BTC", metadata={"cpapi": True, "cpapi_version": 1, "deposit_id": "x"}
    )
    return invoice.id


def test_valid_delivery_is_stored_and_acked(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    invoice_id = make_invoice(fake_btcpay)
    response = post_webhook(
        client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id)
    )
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    event = session.execute(select(WebhookEvent)).scalar_one()
    assert event.status == "received"
    assert event.btcpay_invoice_id == invoice_id
    assert event.processed_at is None


def test_bad_signature_is_401_and_stores_nothing(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    payload = fake_btcpay.webhook_payload("InvoiceSettled", make_invoice(fake_btcpay))
    raw = json.dumps(payload).encode()
    response = client.post(
        "/webhooks/btcpay",
        content=raw,
        headers={"BTCPay-Sig": "sha256=" + "0" * 64, "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert count_events(session) == 0


def test_signature_over_reserialized_body_is_rejected(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """The raw-bytes trap, end to end.

    The signature is computed over compact JSON and the body is sent with
    spaces. A verifier that parsed and re-serialized would accept this.
    """
    payload = fake_btcpay.webhook_payload("InvoiceSettled", make_invoice(fake_btcpay))
    _compact, headers = fake_btcpay.sign(payload)
    spaced = json.dumps(payload, indent=2).encode()

    response = client.post("/webhooks/btcpay", content=spaced, headers=headers)
    assert response.status_code == 401
    assert count_events(session) == 0


def test_missing_signature_header_is_401(client: TestClient, fake_btcpay: FakeBTCPay) -> None:
    raw, _ = fake_btcpay.sign(
        fake_btcpay.webhook_payload("InvoiceSettled", make_invoice(fake_btcpay))
    )
    response = client.post(
        "/webhooks/btcpay", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401


def test_unparseable_body_is_400(client: TestClient, fake_btcpay: FakeBTCPay) -> None:
    from crypto_processing_api.core.signing import compute_btcpay_signature

    raw = b"{not json"
    response = client.post(
        "/webhooks/btcpay",
        content=raw,
        headers={"BTCPay-Sig": compute_btcpay_signature(fake_btcpay.webhook_secret, raw)},
    )
    assert response.status_code == 400


def test_missing_delivery_id_is_400(client: TestClient, fake_btcpay: FakeBTCPay) -> None:
    payload = fake_btcpay.webhook_payload("InvoiceSettled", make_invoice(fake_btcpay))
    del payload["deliveryId"]
    assert post_webhook(client, fake_btcpay, payload).status_code == 400


def test_another_stores_event_is_ignored(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    payload = fake_btcpay.webhook_payload(
        "InvoiceSettled", make_invoice(fake_btcpay), store_id="someone-elses-store"
    )
    response = post_webhook(client, fake_btcpay, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert count_events(session) == 0


def test_non_cpapi_invoice_is_ignored(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """Someone else's invoices on the same store must never become deposits."""
    other = fake_btcpay.create_top_up_invoice(currency="BTC", metadata={"orderId": "their-shop"})
    response = post_webhook(
        client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", other.id)
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "not a cpapi invoice"
    assert count_events(session) == 0


def test_redeliveries_collapse_to_one_row(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """BTCPay reuses originalDeliveryId, so a retry storm is one event."""
    invoice_id = make_invoice(fake_btcpay)
    first = fake_btcpay.webhook_payload("InvoiceSettled", invoice_id)
    assert post_webhook(client, fake_btcpay, first).status_code == 200

    for _ in range(7):
        retry = fake_btcpay.webhook_payload(
            "InvoiceSettled", invoice_id, original_delivery_id=first["deliveryId"]
        )
        assert post_webhook(client, fake_btcpay, retry).status_code == 200

    assert count_events(session) == 1


def test_the_identical_delivery_twice_is_one_row(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    payload = fake_btcpay.webhook_payload("InvoiceSettled", make_invoice(fake_btcpay))
    post_webhook(client, fake_btcpay, payload)
    post_webhook(client, fake_btcpay, payload)
    assert count_events(session) == 1


def test_distinct_events_are_distinct_rows(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    invoice_id = make_invoice(fake_btcpay)
    for event_type in ("InvoiceReceivedPayment", "InvoiceProcessing", "InvoiceSettled"):
        post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload(event_type, invoice_id))
    assert count_events(session) == 3


def test_payout_events_are_stored_for_m3(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    payload = {
        "deliveryId": "d-payout",
        "originalDeliveryId": "d-payout",
        "type": "PayoutUpdated",
        "storeId": fake_btcpay.store_id,
        "payoutId": "payout-1",
    }
    assert post_webhook(client, fake_btcpay, payload).status_code == 200
    event = session.execute(select(WebhookEvent)).scalar_one()
    assert event.btcpay_payout_id == "payout-1"


def test_ingress_never_touches_the_ledger(
    client: TestClient, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """Ack then process: the endpoint stores and returns, nothing else."""
    invoice_id = make_invoice(fake_btcpay)
    fake_btcpay.add_payment(invoice_id, "0.5")
    fake_btcpay.settle(invoice_id)

    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))

    from crypto_processing_api.ledger.models import JournalEntry

    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 0
