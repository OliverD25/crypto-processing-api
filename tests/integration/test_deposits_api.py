"""POST/GET /v1/deposits against a real database and a fake BTCPay."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable, BTCPayValidation
from crypto_processing_api.ledger.models import Asset, Deposit, DepositStatus, IdempotencyKey
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, USDT, bearer


def create(
    client: TestClient,
    key: str,
    *,
    user: str = "u1",
    asset: str = BTC,
    idem: str = "d-1",
    **extra: Any,
) -> Any:
    body: dict[str, Any] = {"external_user_id": user, "asset": asset, **extra}
    return client.post("/v1/deposits", json=body, headers={**bearer(key), "Idempotency-Key": idem})


def test_create_returns_an_address_and_a_pending_deposit(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay
) -> None:
    response = create(client, readwrite_key)
    assert response.status_code == 201
    body = response.json()

    assert body["status"] == "pending"
    assert body["asset"] == BTC
    assert body["address"].startswith("bcrt1q")
    assert body["checkout_link"]
    assert body["expires_at"]
    assert body["amount_credited"] == "0.00000000"
    assert len(fake_btcpay.invoices) == 1


def test_invoice_carries_the_metadata_contract(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay
) -> None:
    body = create(client, readwrite_key, user="user-42").json()
    invoice = next(iter(fake_btcpay.invoices.values()))
    assert invoice.metadata == {
        "cpapi": True,
        "cpapi_version": 1,
        "external_user_id": "user-42",
        "deposit_id": body["deposit_id"],
        "asset": BTC,
    }


def test_amounts_are_strings_not_numbers(client: TestClient, readwrite_key: str) -> None:
    """A JSON number loses satoshis above 2^53 in any JavaScript client."""
    body = create(client, readwrite_key, expected_amount="50000000").json()
    assert body["amount_expected"] == "0.50000000"
    assert isinstance(body["amount_credited"], str)


def test_replay_returns_the_stored_response_and_creates_nothing(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    first = create(client, readwrite_key, idem="same-key")
    second = create(client, readwrite_key, idem="same-key")

    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(fake_btcpay.invoices) == 1
    assert session.execute(select(Deposit)).scalars().all().__len__() == 1


def test_same_key_different_body_is_422(client: TestClient, readwrite_key: str) -> None:
    create(client, readwrite_key, user="a", idem="k")
    assert create(client, readwrite_key, user="b", idem="k").status_code == 422


def test_idempotency_key_is_required(client: TestClient, readwrite_key: str) -> None:
    response = client.post(
        "/v1/deposits", json={"external_user_id": "u1", "asset": BTC}, headers=bearer(readwrite_key)
    )
    assert response.status_code == 400


def test_authentication_is_required(client: TestClient) -> None:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "u1", "asset": BTC},
        headers={"Idempotency-Key": "x"},
    )
    assert response.status_code == 401


def test_unknown_asset_is_404(client: TestClient, readwrite_key: str) -> None:
    assert create(client, readwrite_key, asset="DOGE").status_code == 404


def test_disabled_asset_is_503(client: TestClient, readwrite_key: str, session: Session) -> None:
    """USDT ships disabled until its plugin and address pool exist."""
    session.execute(select(Asset).where(Asset.id == USDT)).scalar_one().enabled = False
    session.commit()
    assert create(client, readwrite_key, asset=USDT).status_code == 503


def test_expected_amount_must_be_a_positive_integer_string(
    client: TestClient, readwrite_key: str
) -> None:
    assert create(client, readwrite_key, expected_amount="0.5").status_code == 422
    assert create(client, readwrite_key, expected_amount="-1", idem="d-2").status_code == 422
    assert create(client, readwrite_key, expected_amount="0", idem="d-3").status_code == 422


def test_btcpay_rejection_marks_the_deposit_failed(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """BTCPay answered and said no, so the intent is definitely dead."""
    fake_btcpay.fail_next["create_top_up_invoice"] = BTCPayValidation("bad request")
    response = create(client, readwrite_key)
    assert response.status_code == 502

    deposit = session.execute(select(Deposit)).scalar_one()
    assert deposit.status == DepositStatus.FAILED


def test_ambiguous_timeout_leaves_the_deposit_creating(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """The invoice may exist. Nothing may be concluded, so nothing is."""
    fake_btcpay.fail_next["create_top_up_invoice"] = BTCPayUnavailable("timeout")
    response = create(client, readwrite_key, idem="ambiguous")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"

    deposit = session.execute(select(Deposit)).scalar_one()
    assert deposit.status == DepositStatus.CREATING
    assert deposit.btcpay_invoice_id is None

    # The idempotency record stays claimed but unfinished, so a retry inside
    # the staleness window still gets 409 rather than a second deposit.
    record = session.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == "ambiguous")
    ).scalar_one()
    assert record.state == "in_progress"
    assert record.resource_id == str(deposit.id)


def test_retry_after_an_ambiguous_timeout_adopts_the_existing_invoice(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay, session: Session
) -> None:
    """The case that would otherwise create two deposit addresses for one request.

    The invoice is created, then the call that reads its payment methods dies.
    From the client's side that is indistinguishable from a timeout.
    """
    fake_btcpay.fail_next["get_invoice_payment_methods"] = BTCPayUnavailable("timeout")
    assert create(client, readwrite_key, idem="adopt-me").status_code == 503
    assert len(fake_btcpay.invoices) == 1

    # Age the idempotency claim past the staleness window so the retry may
    # reclaim it, exactly as a client retrying a minute later would.
    session.execute(text("UPDATE idempotency_keys SET updated_at = now() - interval '5 minutes'"))
    session.commit()

    second = create(client, readwrite_key, idem="adopt-me")
    assert second.status_code == 201
    # One invoice, not two: the retry found the one BTCPay had already made.
    assert len(fake_btcpay.invoices) == 1
    assert session.execute(select(Deposit)).scalars().all().__len__() == 1


def test_get_deposit_returns_payment_detail(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay
) -> None:
    created = create(client, readwrite_key).json()
    response = client.get(f"/v1/deposits/{created['deposit_id']}", headers=bearer(readwrite_key))
    assert response.status_code == 200
    assert response.json()["payments"] == []


def test_get_unknown_deposit_is_404(client: TestClient, readwrite_key: str) -> None:
    response = client.get(f"/v1/deposits/{uuid.uuid4()}", headers=bearer(readwrite_key))
    assert response.status_code == 404


def test_user_deposit_list_is_keyset_paginated(client: TestClient, readwrite_key: str) -> None:
    ids = [
        create(client, readwrite_key, user="pager", idem=f"p-{i}").json()["deposit_id"]
        for i in range(5)
    ]

    first = client.get("/v1/users/pager/deposits?limit=2", headers=bearer(readwrite_key)).json()
    assert [d["deposit_id"] for d in first["deposits"]] == ids[::-1][:2]
    assert first["next_cursor"] == ids[::-1][1]

    second = client.get(
        f"/v1/users/pager/deposits?limit=2&cursor={first['next_cursor']}",
        headers=bearer(readwrite_key),
    ).json()
    assert [d["deposit_id"] for d in second["deposits"]] == ids[::-1][2:4]


def test_user_deposit_list_only_shows_that_user(client: TestClient, readwrite_key: str) -> None:
    create(client, readwrite_key, user="alice", idem="a")
    create(client, readwrite_key, user="bob", idem="b")
    body = client.get("/v1/users/alice/deposits", headers=bearer(readwrite_key)).json()
    assert [d["external_user_id"] for d in body["deposits"]] == ["alice"]
