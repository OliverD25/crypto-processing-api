"""Every error response the API can produce, asserted as a status and a body.

An integrator writes their retry logic against these codes, so which code an
error gets is part of the contract and not an implementation detail. The three
that matter most, and the reason each is what it is:

- **404 vs 409** — "there is no such row" is a client bug; "the row is in the
  wrong state" is a race someone may legitimately retry.
- **422 vs 503** — "we will never accept this" against "ask again".
- **400 for a missing Idempotency-Key**, never 422, because `docs/api.md` says
  400 and every caller branching on it would break.

The admin surface gets the most attention here: it is the surface an operator
uses at 2am, and an unhandled exception there is a 500 with no explanation of
what to do next.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.api import health
from crypto_processing_api.api.middleware import (
    IdempotencyContext,
    get_tron_gateway,
)
from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import (
    BTCPayNotFound,
    BTCPayUnavailable,
)
from crypto_processing_api.gateway.trongrid import TronGridUnavailable
from crypto_processing_api.ledger.models import Deposit, DepositStatus, WithdrawalStatus
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import payout_submitter, webhook_processor
from tests.fake_tron import DESTINATION, HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, USDT, bearer, credit_user, post_webhook

DEST = regtest_address("api-error-destination")
HALF_BTC = "0.50000000"
GROSS = 100_000
FUNDED = 1_000_000
UNKNOWN = uuid.uuid4()


def create_deposit(client: TestClient, key: str, *, user: str = "u1", idem: str = "d-1") -> Any:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": user, "asset": BTC},
        headers={**bearer(key), "Idempotency-Key": idem},
    )
    assert response.status_code == 201, response.text
    return response.json()


def review_deposit(
    client: TestClient,
    fake: FakeBTCPay,
    session_factory: sessionmaker[Session],
    key: str,
    *,
    user: str = "review-user",
) -> tuple[str, str]:
    """A deposit sitting in the review queue, with its payment id."""
    create_deposit(client, key, user=user)
    invoice_id = next(iter(fake.invoices))
    payment_id = fake.add_payment(invoice_id, HALF_BTC)
    fake.settle(invoice_id, additional_status="PaidLate")
    post_webhook(client, fake, fake.webhook_payload("InvoiceSettled", invoice_id))
    webhook_processor.process_pending(session_factory, fake, settings=get_settings())
    return invoice_id, payment_id


def deposit_id_of(session: Session) -> uuid.UUID:
    session.rollback()
    return session.execute(select(Deposit)).scalar_one().id


def make_withdrawal(client: TestClient, key: str, *, user: str, amount: int = GROSS) -> uuid.UUID:
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": BTC,
            "amount": str(amount),
            "destination_address": DEST,
        },
        headers={**bearer(key), "Idempotency-Key": f"w-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["withdrawal_id"])


# -- request validation ----------------------------------------------------


@pytest.mark.parametrize("amount", ["0", "-1", "1.5", "abc", "", " 10"])
def test_a_withdrawal_amount_must_be_a_positive_integer_string(
    client: TestClient, readwrite_key: str, amount: str
) -> None:
    """Amounts are integer strings of the smallest unit. A float would lose
    satoshis to binary rounding on the way in."""
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": "u1",
            "asset": BTC,
            "amount": amount,
            "destination_address": DEST,
        },
        headers={**bearer(readwrite_key), "Idempotency-Key": f"w-{uuid.uuid4()}"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("expected", ["0", "-5", "1.0", "lots"])
def test_an_expected_deposit_amount_must_be_a_positive_integer_string(
    client: TestClient, readwrite_key: str, expected: str
) -> None:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "u1", "asset": BTC, "expected_amount": expected},
        headers={**bearer(readwrite_key), "Idempotency-Key": f"d-{uuid.uuid4()}"},
    )
    assert response.status_code == 422


def test_withdrawing_an_asset_that_does_not_exist_is_404(
    client: TestClient, readwrite_key: str
) -> None:
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": "u1",
            "asset": "DOGE",
            "amount": "1000",
            "destination_address": DEST,
        },
        headers={**bearer(readwrite_key), "Idempotency-Key": f"w-{uuid.uuid4()}"},
    )
    assert response.status_code == 404
    assert "no such asset" in response.json()["detail"]


def test_reading_a_withdrawal_that_does_not_exist_is_404(
    client: TestClient, readwrite_key: str
) -> None:
    response = client.get(f"/v1/withdrawals/{UNKNOWN}", headers=bearer(readwrite_key))
    assert response.status_code == 404
    assert response.json()["detail"] == "no such withdrawal"


# -- the Idempotency-Key contract -----------------------------------------


def test_a_missing_idempotency_key_is_400_not_422(client: TestClient, readwrite_key: str) -> None:
    """422 would come from FastAPI's own validation. docs/api.md promises 400,
    and every caller that branches on the code would break."""
    response = client.post(
        "/v1/deposits", json={"external_user_id": "u1", "asset": BTC}, headers=bearer(readwrite_key)
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_an_absurdly_long_idempotency_key_is_refused(
    client: TestClient, readwrite_key: str
) -> None:
    """The column is bounded; without this the insert would fail as a 500."""
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "u1", "asset": BTC},
        headers={**bearer(readwrite_key), "Idempotency-Key": "k" * 300},
    )
    assert response.status_code == 400
    assert "255" in response.json()["detail"]


def test_a_whitespace_only_idempotency_key_is_refused(
    client: TestClient, readwrite_key: str
) -> None:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "u1", "asset": BTC},
        headers={**bearer(readwrite_key), "Idempotency-Key": "   "},
    )
    assert response.status_code == 400


def test_the_context_reports_no_resource_before_one_exists(session: Session) -> None:
    context = IdempotencyContext(
        session=session, key="k", endpoint="POST /v1/deposits", start=None, replay=None
    )
    assert context.is_replay is False
    assert context.reclaimed is False
    assert context.resource_id is None


def test_replaying_a_first_run_request_is_a_programming_error(session: Session) -> None:
    """A handler that calls this without checking `is_replay` has a bug, and a
    silent empty response would hide it behind a 200."""
    context = IdempotencyContext(
        session=session, key="k", endpoint="POST /v1/deposits", start=None, replay=None
    )
    with pytest.raises(RuntimeError, match="first-run request"):
        context.replay_response()


# -- the admin deposit queue ----------------------------------------------


def test_resolving_a_deposit_that_does_not_exist_is_404(client: TestClient, admin_key: str) -> None:
    response = client.post(
        f"/v1/admin/deposits/{UNKNOWN}/resolve",
        json={"action": "dismiss"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "no such deposit"


def test_crediting_without_naming_a_payment_is_refused_by_the_model(
    client: TestClient, admin_key: str
) -> None:
    """There is deliberately no amount field, so the payment id is the only
    thing that says what to credit. Without it there is nothing to look up."""
    response = client.post(
        f"/v1/admin/deposits/{UNKNOWN}/resolve",
        json={"action": "credit"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 422
    assert "payment_id" in response.text


def test_btcpay_being_down_during_a_resolve_is_503_and_credits_nothing(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    admin_key: str,
    readwrite_key: str,
) -> None:
    """The amount is re-read from BTCPay, never taken from the operator. If
    BTCPay cannot be asked, the only honest answer is "try again"."""
    _invoice_id, payment_id = review_deposit(client, fake_btcpay, session_factory, readwrite_key)
    deposit_id = deposit_id_of(session)
    fake_btcpay.fail_next["get_invoice_payment_methods"] = BTCPayUnavailable(
        "BTCPay is down", status_code=503
    )

    response = client.post(
        f"/v1/admin/deposits/{deposit_id}/resolve",
        json={"action": "credit", "payment_id": payment_id},
        headers=bearer(admin_key),
    )
    assert response.status_code == 503
    assert "try again" in response.json()["detail"]

    session.rollback()
    assert session.get(Deposit, deposit_id).status is DepositStatus.REVIEW  # type: ignore[union-attr]


def test_resolving_a_deposit_that_is_not_in_review_is_409(
    client: TestClient,
    session: Session,
    fake_btcpay: FakeBTCPay,
    admin_key: str,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key, user="not-in-review")
    deposit_id = deposit_id_of(session)

    response = client.post(
        f"/v1/admin/deposits/{deposit_id}/resolve",
        json={"action": "dismiss"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409


# -- the admin withdrawal queue -------------------------------------------


def test_filtering_the_withdrawal_queue_by_a_status_that_does_not_exist_is_422(
    client: TestClient, admin_key: str
) -> None:
    """Silently ignoring an unknown filter would answer with every row, which
    an operator would read as "nothing is waiting"."""
    response = client.get("/v1/admin/withdrawals?status=nearly", headers=bearer(admin_key))
    assert response.status_code == 422
    assert "nearly" in response.json()["detail"]


def test_the_withdrawal_queue_carries_the_operator_only_columns(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    credit_user(session, user="queued", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="queued")

    body = client.get("/v1/admin/withdrawals", headers=bearer(admin_key)).json()
    row = next(item for item in body["withdrawals"] if item["withdrawal_id"] == str(withdrawal_id))
    assert row["approved_by"] == "auto"
    assert row["rejected_by"] is None
    assert row["released_by"] is None
    assert row["release_attestation"] is None
    assert "backend_ref" in row


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("approve", {}),
        ("reject", {}),
        ("release", {"attestation": "checked the chain, nothing was broadcast"}),
        ("mark-broadcast", {"txid": "a" * 64}),
    ],
)
def test_acting_on_a_withdrawal_that_does_not_exist_is_404(
    client: TestClient, admin_key: str, monkeypatch: pytest.MonkeyPatch, path: str, body: dict
) -> None:
    monkeypatch.setattr(get_settings(), "tron_hot_wallet_address", HOT_WALLET)
    response = client.post(
        f"/v1/admin/withdrawals/{UNKNOWN}/{path}", json=body, headers=bearer(admin_key)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "no such withdrawal"


def test_rejecting_an_already_approved_withdrawal_is_409(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    credit_user(session, user="autoapproved", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="autoapproved")

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/reject", json={}, headers=bearer(admin_key)
    )
    assert response.status_code == 409
    assert "only pending_approval" in response.json()["detail"]


def test_releasing_a_confirmed_withdrawal_is_409(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """The money is gone. Giving the balance back would be handing it out
    twice, and no attestation can make that true."""
    credit_user(session, user="settled", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="settled")
    payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings())
    session.expire_all()
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    from crypto_processing_api.workers import reconciliation

    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/release",
        json={"attestation": "I checked the chain and saw nothing"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.CONFIRMED


def test_a_short_attestation_is_refused_by_the_model(client: TestClient, admin_key: str) -> None:
    response = client.post(
        f"/v1/admin/withdrawals/{UNKNOWN}/release",
        json={"attestation": "no"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 422


# -- the operator-sent USDT surface ---------------------------------------


def test_mark_broadcast_without_a_configured_hot_wallet_is_503(
    client: TestClient, admin_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no hot wallet address there is nothing to check a sender against,
    and "verified" would mean nothing at all."""
    monkeypatch.setattr(get_settings(), "tron_hot_wallet_address", None)
    response = client.post(
        f"/v1/admin/withdrawals/{UNKNOWN}/mark-broadcast",
        json={"txid": "a" * 64},
        headers=bearer(admin_key),
    )
    assert response.status_code == 503
    assert "TRON_HOT_WALLET_ADDRESS" in response.json()["detail"]


def test_trongrid_being_down_during_mark_broadcast_is_503(
    client: TestClient,
    app: FastAPI,
    session: Session,
    admin_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)

    tron = FakeTronGrid()
    tron.fail_next["get_transaction"] = TronGridUnavailable("TronGrid is down", status_code=503)
    app.dependency_overrides[get_tron_gateway] = lambda: tron

    withdrawal_id = submitted_usdt(session, user="tron-down")
    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": "9" * 64},
        headers=bearer(admin_key),
    )
    assert response.status_code == 503
    assert "TronGrid is unreachable" in response.json()["detail"]

    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.SUBMITTED


def test_marking_a_withdrawal_that_was_never_handed_over_is_409(
    client: TestClient,
    app: FastAPI,
    session: Session,
    admin_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)
    app.dependency_overrides[get_tron_gateway] = FakeTronGrid

    withdrawal_id = queued_usdt(session, user="too-early")
    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": "9" * 64},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409
    assert "only submitted" in response.json()["detail"]


def queued_usdt(session: Session, *, user: str) -> uuid.UUID:
    credit_user(session, user=user, amount=500_000_000, asset=USDT)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id=user,
        asset_id=USDT,
        amount_gross=200_000_000,
        destination_address=DESTINATION,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        force_manual=True,
    )
    session.commit()
    return outcome.withdrawal.id


def submitted_usdt(session: Session, *, user: str) -> uuid.UUID:
    from crypto_processing_api.services.fees import FeeQuote

    withdrawal_id = queued_usdt(session, user=user)
    withdrawal_service.approve(session, withdrawal_id, actor="ops")
    locked = withdrawal_service.lock(session, withdrawal_id)
    withdrawal_service.submit_manual(
        session,
        locked,
        quote=FeeQuote(
            fee=1_000_000,
            net=199_000_000,
            wallet_fee=0,
            sat_per_vb=0.0,
            source="asset_flat_fee",
        ),
    )
    session.commit()
    return withdrawal_id


# -- the outbound queue ---------------------------------------------------


def test_filtering_the_event_queue_by_an_unknown_status_is_422(
    client: TestClient, admin_key: str
) -> None:
    response = client.get("/v1/admin/events?status=lost", headers=bearer(admin_key))
    assert response.status_code == 422
    assert "lost" in response.json()["detail"]


@pytest.mark.parametrize("status_filter", ["pending", "delivered", "dead"])
def test_every_documented_event_status_is_accepted(
    client: TestClient, admin_key: str, status_filter: str
) -> None:
    response = client.get(f"/v1/admin/events?status={status_filter}", headers=bearer(admin_key))
    assert response.status_code == 200
    assert response.json()["events"] == []


# -- webhook ingress ------------------------------------------------------


def test_a_json_body_that_is_not_an_object_is_400(
    client: TestClient, fake_btcpay: FakeBTCPay
) -> None:
    """BTCPay sends an object. A bare array reaching `payload.get` would be an
    AttributeError and a 500 on an unauthenticated endpoint."""
    import json as json_module

    from crypto_processing_api.core.signing import compute_btcpay_signature

    raw = json_module.dumps([1, 2, 3]).encode("utf-8")
    response = client.post(
        "/webhooks/btcpay",
        content=raw,
        headers={
            "BTCPay-Sig": compute_btcpay_signature(fake_btcpay.webhook_secret, raw),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "body must be a JSON object"


# -- balances and history -------------------------------------------------


def test_the_transaction_history_can_be_filtered_by_asset(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="mixed", amount=FUNDED)
    credit_user(session, user="mixed", amount=5_000_000, asset=USDT)

    everything = client.get("/v1/users/mixed/transactions", headers=bearer(readwrite_key)).json()[
        "transactions"
    ]
    assert {row["asset"] for row in everything} == {BTC, USDT}

    only_btc = client.get(
        f"/v1/users/mixed/transactions?asset={BTC}", headers=bearer(readwrite_key)
    ).json()["transactions"]
    assert {row["asset"] for row in only_btc} == {BTC}


def test_the_transaction_history_pages_from_a_cursor(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    for _ in range(3):
        credit_user(session, user="paged", amount=1_000)

    first = client.get("/v1/users/paged/transactions?limit=2", headers=bearer(readwrite_key)).json()
    assert len(first["transactions"]) == 2
    assert first["next_cursor"]

    second = client.get(
        f"/v1/users/paged/transactions?limit=2&cursor={first['next_cursor']}",
        headers=bearer(readwrite_key),
    ).json()
    assert len(second["transactions"]) == 1
    first_ids = {row["posting_id"] for row in first["transactions"]}
    assert not first_ids & {row["posting_id"] for row in second["transactions"]}


def test_the_address_history_of_a_deposit_that_does_not_exist_is_404(
    client: TestClient, readwrite_key: str
) -> None:
    response = client.get(f"/v1/deposits/{UNKNOWN}/address-history", headers=bearer(readwrite_key))
    assert response.status_code == 404


def test_a_deposit_with_no_address_yet_has_no_address_history(
    client: TestClient,
    session: Session,
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The row commits before the BTCPay call, so a deposit can legitimately
    exist with no address at all — and the runbook query must still answer."""
    create_deposit(client, readwrite_key, user="addressless")
    deposit_id = deposit_id_of(session)
    session.execute(Deposit.__table__.update().where(Deposit.id == deposit_id).values(address=None))
    session.commit()

    body = client.get(
        f"/v1/deposits/{deposit_id}/address-history", headers=bearer(readwrite_key)
    ).json()
    assert body == {"address": None, "reservations": []}


# -- readiness -------------------------------------------------------------


def test_readyz_names_the_component_that_is_down(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single "degraded" would send an operator looking at everything."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)

    def unreachable_tron() -> FakeTronGrid:
        tron = FakeTronGrid()
        tron.fail_next["get_block_height"] = TronGridUnavailable("TronGrid is down")
        return tron

    # Patched on the module: /readyz calls these directly, not through Depends,
    # so an app-level override would not reach them — and the real call would
    # put a third-party request in the middle of the test suite.
    monkeypatch.setattr(health, "get_tron_gateway", unreachable_tron)

    response = client.get("/readyz")
    assert response.status_code == 503
    components = {entry["name"]: entry for entry in response.json()["components"]}
    assert components["database"]["status"] == "ok"
    # BTCPay and TronGrid are both unreachable from the test process, and each
    # says which exception it was.
    assert components["btcpay"]["status"] == "degraded"
    assert components["btcpay"]["detail"]
    assert components["trongrid"]["status"] == "degraded"


def test_readyz_says_when_btcpay_was_never_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "btcpay_url", None)
    body = client.get("/readyz").json()
    btcpay = next(entry for entry in body["components"] if entry["name"] == "btcpay")
    assert btcpay["detail"] == "not configured"


def test_readyz_omits_trongrid_on_a_btc_only_deployment(client: TestClient) -> None:
    """A BTC-only deployment must not be marked degraded for a third party it
    has deliberately not configured."""
    body = client.get("/readyz").json()
    assert "trongrid" not in {entry["name"] for entry in body["components"]}


def test_an_invoice_btcpay_refuses_outright_fails_the_deposit(
    client: TestClient, session: Session, fake_btcpay: FakeBTCPay, readwrite_key: str
) -> None:
    """BTCPay answered and said no, so the intent is dead rather than pending.
    Leaving it in `creating` would have the adopter chase it forever."""
    fake_btcpay.fail_next["create_top_up_invoice"] = BTCPayNotFound("no such store")

    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "refused", "asset": BTC},
        headers={**bearer(readwrite_key), "Idempotency-Key": "d-refused"},
    )
    assert response.status_code == 502

    session.rollback()
    deposit = session.execute(select(Deposit)).scalar_one()
    assert deposit.status is DepositStatus.FAILED
    assert deposit.btcpay_invoice_id is None
