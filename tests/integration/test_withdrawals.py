"""Withdrawals end to end: gates, the posting matrix, and the races."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Withdrawal,
    WithdrawalStatus,
)
from crypto_processing_api.services import withdrawals as withdrawal_service
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, bearer, credit_user

# A real regtest address, so validation is exercised rather than bypassed.
DEST = "bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq"
DEST2 = "bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj"

#: FakeBTCPay quotes 10 sat/vB and the default assumed size is 300 vB.
EXPECTED_FEE = 3_000


def balance(session: Session, user: str, kind: AccountKind = AccountKind.USER_AVAILABLE) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == BTC,
            Account.kind == kind,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def system_balance(session: Session, kind: AccountKind) -> int:
    session.rollback()
    account = ledger.get_system_account(session, asset_id=BTC, kind=kind)
    return account.balance


def request_withdrawal(
    client: TestClient,
    key: str,
    *,
    user: str,
    amount: int,
    destination: str = DEST,
    idem: str | None = None,
) -> Any:
    return client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": BTC,
            "amount": str(amount),
            "destination_address": destination,
        },
        headers={**bearer(key), "Idempotency-Key": idem or f"w-{uuid.uuid4()}"},
    )


# -- request, validation, gates -------------------------------------------


def test_request_places_a_hold_and_auto_approves(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="alice", amount=1_000_000)
    response = request_withdrawal(client, readwrite_key, user="alice", amount=100_000)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "approved"
    assert body["approval_mode"] == "auto"
    assert body["amount_gross"] == "0.00100000"
    # The fee is fixed at submission, so it is not known yet.
    assert body["fee"] is None

    assert balance(session, "alice") == 900_000
    assert balance(session, "alice", AccountKind.USER_HOLD) == 100_000
    assert_ledger_consistent(session)


def test_held_funds_are_not_available_again(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="bob", amount=100_000)
    assert request_withdrawal(client, readwrite_key, user="bob", amount=100_000).status_code == 201
    second = request_withdrawal(client, readwrite_key, user="bob", amount=100_000)
    assert second.status_code == 402


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "mainnet address on a regtest node"),
        ("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", "a TRON address"),
        ("not-an-address", "garbage"),
        ("bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yr", "one character changed"),
    ],
)
def test_bad_destinations_are_refused_before_any_hold(
    client: TestClient, session: Session, readwrite_key: str, address: str, why: str
) -> None:
    credit_user(session, user="careful", amount=1_000_000)
    response = request_withdrawal(
        client, readwrite_key, user="careful", amount=100_000, destination=address
    )
    assert response.status_code == 422, why
    assert balance(session, "careful") == 1_000_000
    assert balance(session, "careful", AccountKind.USER_HOLD) == 0


def test_our_own_deposit_address_is_refused(
    client: TestClient, session: Session, readwrite_key: str, fake_btcpay: FakeBTCPay
) -> None:
    """Paying ourselves burns a miner fee, and an expired one is a black hole."""
    credit_user(session, user="selfpay", amount=1_000_000)
    deposit = client.post(
        "/v1/deposits",
        json={"external_user_id": "selfpay", "asset": BTC},
        headers={**bearer(readwrite_key), "Idempotency-Key": "d-self"},
    ).json()

    response = request_withdrawal(
        client, readwrite_key, user="selfpay", amount=100_000, destination=deposit["address"]
    )
    assert response.status_code == 422
    assert "own deposit address" in response.json()["detail"]


def test_dust_is_refused_before_the_hold(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="dusty", amount=1_000_000)
    response = request_withdrawal(client, readwrite_key, user="dusty", amount=3_400)
    assert response.status_code == 422
    assert balance(session, "dusty") == 1_000_000


def test_insufficient_balance_is_402(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="poor", amount=50_000)
    assert request_withdrawal(client, readwrite_key, user="poor", amount=60_000).status_code == 402


def test_above_the_auto_limit_goes_to_the_approval_queue(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="whale", amount=10_000_000)
    # Seeded auto limit is 500_000 sat.
    body = request_withdrawal(client, readwrite_key, user="whale", amount=600_000).json()
    assert body["status"] == "pending_approval"
    assert body["approval_mode"] == "manual"
    assert "auto-approval limit" in body["approval_reason"]
    # The hold is placed either way: the decision and the reservation are atomic.
    assert balance(session, "whale", AccountKind.USER_HOLD) == 600_000


def test_a_hit_daily_cap_forces_everything_manual(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """The point of the cap: once it is reached, even small amounts wait."""
    asset = session.get(Asset, BTC)
    assert asset
    # 500_000 + 100_000 crosses 550_000, so the second request trips the cap
    # even though it is far below the per-withdrawal auto limit.
    asset.withdrawal_daily_cap = 550_000
    session.commit()

    credit_user(session, user="drainer", amount=10_000_000)
    first = request_withdrawal(client, readwrite_key, user="drainer", amount=500_000).json()
    assert first["status"] == "approved"

    second = request_withdrawal(client, readwrite_key, user="drainer", amount=100_000).json()
    assert second["status"] == "pending_approval"
    assert "24h cap" in second["approval_reason"]


def test_a_released_hold_stops_counting_against_the_cap(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    asset = session.get(Asset, BTC)
    assert asset
    asset.withdrawal_daily_cap = 700_000
    asset.withdrawal_auto_limit = 700_000
    session.commit()

    credit_user(session, user="refunded", amount=10_000_000)
    blocked = request_withdrawal(client, readwrite_key, user="refunded", amount=600_000).json()
    assert blocked["status"] == "approved"

    with_session = withdrawal_service.get(session, uuid.UUID(blocked["withdrawal_id"]))
    with_session.status = WithdrawalStatus.PENDING_APPROVAL
    session.commit()
    client.post(
        f"/v1/admin/withdrawals/{blocked['withdrawal_id']}/reject",
        json={"reason": "changed their mind"},
        headers=bearer(admin_key),
    )

    after = request_withdrawal(client, readwrite_key, user="refunded", amount=600_000).json()
    assert after["status"] == "approved"


def test_per_user_cap_when_enabled(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    asset = session.get(Asset, BTC)
    assert asset
    asset.withdrawal_user_daily_cap = 300_000
    session.commit()

    credit_user(session, user="capped", amount=10_000_000)
    credit_user(session, user="other", amount=10_000_000)

    assert (
        request_withdrawal(client, readwrite_key, user="capped", amount=200_000).json()["status"]
        == "approved"
    )
    second = request_withdrawal(client, readwrite_key, user="capped", amount=200_000).json()
    assert second["status"] == "pending_approval"
    assert "user's rolling 24h cap" in second["approval_reason"]
    # Another user is unaffected.
    assert (
        request_withdrawal(client, readwrite_key, user="other", amount=200_000).json()["status"]
        == "approved"
    )


def test_below_the_minimum_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    asset = session.get(Asset, BTC)
    assert asset
    asset.withdrawal_min = 200_000
    session.commit()
    credit_user(session, user="small", amount=1_000_000)
    response = request_withdrawal(client, readwrite_key, user="small", amount=100_000)
    assert response.status_code == 422
    assert "minimum withdrawal" in response.json()["detail"]


def test_replay_of_the_same_key_holds_once(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="retry", amount=1_000_000)
    first = request_withdrawal(client, readwrite_key, user="retry", amount=100_000, idem="same")
    second = request_withdrawal(client, readwrite_key, user="retry", amount=100_000, idem="same")
    assert second.json() == first.json()
    assert balance(session, "retry", AccountKind.USER_HOLD) == 100_000
    assert session.execute(select(func.count()).select_from(Withdrawal)).scalar_one() == 1


# -- critique #1: the serialized per-asset gate ---------------------------


def test_parallel_withdrawals_across_distinct_users_never_exceed_the_cap(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    """The flagship drain control, under the concurrency it is meant to survive.

    Ten users, ten simultaneous requests, a cap that allows three. Without the
    lock on the asset's hot_wallet row every request reads the same pre-cap
    total and all ten auto-approve. With it, the aggregate auto-approved amount
    cannot pass the cap.
    """
    per_request = 100_000
    cap = 300_000
    users = [f"burst-{index}" for index in range(10)]

    asset = session.get(Asset, BTC)
    assert asset
    asset.withdrawal_daily_cap = cap
    asset.withdrawal_auto_limit = per_request
    session.commit()

    for user in users:
        credit_user(session, user=user, amount=1_000_000)

    barrier = threading.Barrier(len(users))

    def attempt(index: int) -> str:
        with session_factory() as worker:
            barrier.wait(timeout=30)
            outcome = withdrawal_service.place_hold(
                worker,
                external_user_id=users[index],
                asset_id=BTC,
                amount_gross=per_request,
                destination_address=DEST,
            )
            worker.commit()
            return outcome.withdrawal.status.value

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        outcomes = [
            f.result(timeout=60) for f in [pool.submit(attempt, i) for i in range(len(users))]
        ]

    approved = outcomes.count("approved")
    assert approved * per_request <= cap, (
        f"{approved} withdrawals auto-approved for {approved * per_request} sat "
        f"against a {cap} sat cap"
    )
    assert approved == cap // per_request
    assert outcomes.count("pending_approval") == len(users) - approved
    # Every request still reserved its funds; only the route differed.
    assert session.execute(select(func.count()).select_from(Withdrawal)).scalar_one() == len(users)
    assert_ledger_consistent(session)


def test_two_racing_requests_for_one_user_cannot_both_hold(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    credit_user(session, user="racer", amount=100_000)
    barrier = threading.Barrier(2)

    def attempt(_index: int) -> str:
        with session_factory() as worker:
            barrier.wait(timeout=30)
            try:
                withdrawal_service.place_hold(
                    worker,
                    external_user_id="racer",
                    asset_id=BTC,
                    amount_gross=100_000,
                    destination_address=DEST,
                )
                worker.commit()
            except withdrawal_service.InsufficientBalance:
                worker.rollback()
                return "refused"
            return "held"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [f.result(timeout=60) for f in [pool.submit(attempt, i) for i in range(2)]]

    assert sorted(outcomes) == ["held", "refused"]
    assert balance(session, "racer") == 0
    assert balance(session, "racer", AccountKind.USER_HOLD) == 100_000
    assert_ledger_consistent(session)
