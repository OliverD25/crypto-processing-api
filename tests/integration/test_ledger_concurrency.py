"""Races run with real threads against real PostgreSQL.

Nothing here is mocked. A mocked lock proves nothing about a lock: the whole
question is what two connections do when they contend for the same row, and
only the database can answer it.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.ledger import service
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import Account, AccountKind, EntryKind, JournalEntry
from tests.integration.conftest import BTC, credit_user

THREADS = 2


def run_in_threads(count: int, worker: Any) -> list[Any]:
    """Start `count` threads that all pass a barrier before doing any work."""
    barrier = threading.Barrier(count)

    def wrapped(index: int) -> Any:
        barrier.wait(timeout=30)
        return worker(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [
            future.result(timeout=60) for future in [pool.submit(wrapped, i) for i in range(count)]
        ]


def test_two_threads_racing_a_debit_only_one_wins(
    engine: Engine, session_factory: sessionmaker[Session], user_id: str
) -> None:
    """The headline double-spend race.

    The user has exactly enough for one hold. Two requests arrive at the same
    instant. The second must block on the row lock, re-read the committed
    balance and be rejected — and if the application ever forgot the lock, the
    no_overdraft CHECK would still reject it.
    """
    funds = 100_000
    with session_factory() as session:
        credit_user(session, user=user_id, amount=funds)

    def attempt(index: int) -> str:
        with session_factory() as session:
            available, held = service.get_user_accounts(
                session, asset_id=BTC, external_user_id=user_id
            )
            try:
                service.post_entry(
                    session,
                    kind=EntryKind.WITHDRAWAL_HOLD,
                    asset_id=BTC,
                    source_ref=f"withdrawal_hold:racer-{index}",
                    postings=[(available.id, funds), (held.id, -funds)],
                )
                session.commit()
            except service.InsufficientFunds:
                session.rollback()
                return "rejected"
            return "accepted"

    outcomes = run_in_threads(THREADS, attempt)
    assert sorted(outcomes) == ["accepted", "rejected"]

    with session_factory() as session:
        available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
        assert available.balance == 0
        assert held.balance == -funds
        assert session.query(JournalEntry).filter_by(kind=EntryKind.WITHDRAWAL_HOLD).count() == 1
        assert_ledger_consistent(session)


def test_eight_threads_cannot_overdraw_a_balance(
    session_factory: sessionmaker[Session], user_id: str
) -> None:
    """Same race widened: funds for three holds, eight simultaneous attempts."""
    hold = 10_000
    affordable = 3
    with session_factory() as session:
        credit_user(session, user=user_id, amount=hold * affordable)

    def attempt(index: int) -> str:
        with session_factory() as session:
            available, held = service.get_user_accounts(
                session, asset_id=BTC, external_user_id=user_id
            )
            try:
                service.post_entry(
                    session,
                    kind=EntryKind.WITHDRAWAL_HOLD,
                    asset_id=BTC,
                    source_ref=f"withdrawal_hold:wide-{index}",
                    postings=[(available.id, hold), (held.id, -hold)],
                )
                session.commit()
            except service.InsufficientFunds:
                session.rollback()
                return "rejected"
            return "accepted"

    outcomes = run_in_threads(8, attempt)
    assert outcomes.count("accepted") == affordable

    with session_factory() as session:
        available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
        assert available.balance == 0
        assert held.balance == -(hold * affordable)
        assert_ledger_consistent(session)


def test_racing_the_same_source_ref_credits_once(
    session_factory: sessionmaker[Session], user_id: str
) -> None:
    """A webhook and the reconciliation poller crediting the same payment."""
    amount = 50_000
    source_ref = f"btcpay_payment:{uuid.uuid4().hex}:0"

    def attempt(_index: int) -> str:
        with session_factory() as session:
            hot = service.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
            available, _held = service.get_user_accounts(
                session, asset_id=BTC, external_user_id=user_id
            )
            try:
                service.post_entry(
                    session,
                    kind=EntryKind.DEPOSIT_CREDIT,
                    asset_id=BTC,
                    source_ref=source_ref,
                    postings=[(hot.id, amount), (available.id, -amount)],
                )
                session.commit()
            except service.AlreadyPosted:
                session.rollback()
                return "already"
            return "credited"

    outcomes = run_in_threads(4, attempt)
    assert outcomes.count("credited") == 1
    assert outcomes.count("already") == 3

    with session_factory() as session:
        available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
        assert available.balance == -amount
        assert session.query(JournalEntry).count() == 1
        assert_ledger_consistent(session)


def test_concurrent_first_deposits_provision_one_account_pair(
    session_factory: sessionmaker[Session], user_id: str
) -> None:
    """Account provisioning must not duplicate under a cold-start burst."""

    def attempt(_index: int) -> tuple[int, int]:
        with session_factory() as session:
            available, held = service.get_user_accounts(
                session, asset_id=BTC, external_user_id=user_id
            )
            session.commit()
            return available.id, held.id

    pairs = run_in_threads(6, attempt)
    assert len(set(pairs)) == 1

    with session_factory() as session:
        count = session.execute(
            select(func.count())
            .select_from(Account)
            .where(Account.external_user_id == user_id, Account.asset_id == BTC)
        ).scalar_one()
        assert count == 2


def test_concurrent_credits_to_the_hot_wallet_all_land(
    session_factory: sessionmaker[Session], user_id: str
) -> None:
    """Deposits from different users contend on one hot_wallet row and must not lose writes."""
    per_user = 25_000
    users = [f"{user_id}-{index}" for index in range(6)]

    def attempt(index: int) -> None:
        with session_factory() as session:
            hot = service.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
            available, _ = service.get_user_accounts(
                session, asset_id=BTC, external_user_id=users[index]
            )
            service.post_entry(
                session,
                kind=EntryKind.DEPOSIT_CREDIT,
                asset_id=BTC,
                source_ref=f"btcpay_payment:burst:{index}",
                postings=[(hot.id, per_user), (available.id, -per_user)],
            )
            session.commit()

    run_in_threads(len(users), attempt)

    with session_factory() as session:
        hot = service.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
        assert hot.balance == per_user * len(users)
        assert_ledger_consistent(session)


def test_asset_gate_serializes_concurrent_holders(
    session_factory: sessionmaker[Session], user_id: str
) -> None:
    """Critique #1: lock_asset_gate must actually serialize, or the 24h cap is decorative.

    Each thread takes the gate, reads a shared counter, sleeps, then writes
    counter + 1. Without a real lock the increments interleave and the final
    value is less than the number of threads.
    """
    observed: list[int] = []
    lock_free_counter = {"value": 0}
    workers = 5

    def attempt(_index: int) -> None:
        with session_factory() as session:
            service.lock_asset_gate(session, BTC)
            current = lock_free_counter["value"]
            # Long enough that unserialized threads would certainly overlap.
            threading.Event().wait(0.05)
            lock_free_counter["value"] = current + 1
            observed.append(current)
            session.commit()

    run_in_threads(workers, attempt)

    assert lock_free_counter["value"] == workers
    assert sorted(observed) == list(range(workers))
