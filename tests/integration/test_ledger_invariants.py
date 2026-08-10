"""Every invariant from the ledger design, against a real PostgreSQL.

Each one is checked twice where that is meaningful: once as a typed error from
`post_entry`, and once as a database error when the constraint is attacked
directly. The application check is the useful error message; the database check
is the guarantee.
"""

from __future__ import annotations

import contextlib
import random
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.ledger import service
from crypto_processing_api.ledger.invariants import (
    InvariantViolation,
    assert_ledger_consistent,
    balance_drifts,
    custody_reports,
    derived_balances,
    unbalanced_entries,
)
from crypto_processing_api.ledger.models import Account, AccountKind, EntryKind, JournalEntry
from tests.integration.conftest import BTC, USDT, credit_user


def hot(session: Session, asset: str = BTC) -> Account:
    return service.get_system_account(session, asset_id=asset, kind=AccountKind.HOT_WALLET)


def system(session: Session, kind: AccountKind, asset: str = BTC) -> Account:
    return service.get_system_account(session, asset_id=asset, kind=kind)


def unique_ref(prefix: str = "test") -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# seed and provisioning
# --------------------------------------------------------------------------


def test_seed_creates_both_assets_and_their_system_accounts(session: Session) -> None:
    for asset in (BTC, USDT):
        for kind in service.SYSTEM_ACCOUNT_KINDS:
            account = service.get_system_account(session, asset_id=asset, kind=kind)
            assert account.balance == 0
            assert account.external_user_id is None


def test_seed_is_idempotent_and_never_overwrites(session: Session) -> None:
    from crypto_processing_api.cli import asset_specs
    from crypto_processing_api.config import get_settings

    specs = asset_specs(get_settings())
    assert service.seed_assets(session, specs) == []
    session.commit()


def test_user_accounts_are_created_on_first_use(session: Session, user_id: str) -> None:
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    session.commit()
    again_available, again_held = service.get_user_accounts(
        session, asset_id=BTC, external_user_id=user_id
    )
    assert (available.id, held.id) == (again_available.id, again_held.id)


def test_system_account_kinds_reject_a_user_id(session: Session) -> None:
    with pytest.raises(service.AccountNotFound):
        service.get_or_create_account(
            session, asset_id=BTC, kind=AccountKind.HOT_WALLET, external_user_id="u1"
        )


def test_user_account_kinds_require_a_user_id(session: Session) -> None:
    with pytest.raises(service.AccountNotFound):
        service.get_or_create_account(session, asset_id=BTC, kind=AccountKind.USER_AVAILABLE)


# --------------------------------------------------------------------------
# invariant 1 — zero sum
# --------------------------------------------------------------------------


def test_unbalanced_entry_rejected_in_python(session: Session, user_id: str) -> None:
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    with pytest.raises(service.UnbalancedEntry, match="sum to 1"):
        service.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(hot(session).id, 100), (available.id, -99)],
        )
    session.rollback()


def test_single_posting_entry_rejected(session: Session) -> None:
    with pytest.raises(service.UnbalancedEntry, match="at least two postings"):
        service.post_entry(
            session,
            kind=EntryKind.ADJUSTMENT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(hot(session).id, 0)],
        )
    session.rollback()


def test_unbalanced_entry_rejected_by_the_database_trigger(
    session_factory: sessionmaker[Session],
) -> None:
    """Bypass post_entry entirely: raw SQL must not be able to leave the books open.

    The trigger is DEFERRABLE INITIALLY DEFERRED, so the failure lands at
    COMMIT — which is what allows a legitimate multi-posting entry to be built
    up one row at a time.
    """
    with session_factory() as session:
        account_id = hot(session).id

    with session_factory() as session, pytest.raises(DBAPIError) as excinfo:
        entry_id = session.execute(
            text(
                "INSERT INTO journal_entries (kind, asset_id, source_ref) "
                "VALUES ('adjustment', :asset, :ref) RETURNING id"
            ),
            {"asset": BTC, "ref": unique_ref("raw")},
        ).scalar_one()
        session.execute(
            text("INSERT INTO postings (entry_id, account_id, amount) VALUES (:entry, :acct, 7)"),
            {"entry": entry_id, "acct": account_id},
        )
        session.commit()
    assert "does not balance" in str(excinfo.value)


def test_unbalanced_entries_detector_finds_nothing_in_a_healthy_ledger(
    session: Session, user_id: str
) -> None:
    credit_user(session, user=user_id, amount=1_000)
    assert unbalanced_entries(session) == []


# --------------------------------------------------------------------------
# invariant 2 — no zero postings
# --------------------------------------------------------------------------


def test_zero_posting_rejected_in_python(session: Session, user_id: str) -> None:
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    with pytest.raises(service.ZeroPosting):
        service.post_entry(
            session,
            kind=EntryKind.ADJUSTMENT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(available.id, 0), (held.id, 0)],
        )
    session.rollback()


def test_zero_posting_rejected_by_the_check_constraint(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        account_id = hot(session).id

    with (
        session_factory() as session,
        pytest.raises(IntegrityError, match="postings_amount_nonzero"),
    ):
        entry_id = session.execute(
            text(
                "INSERT INTO journal_entries (kind, asset_id, source_ref) "
                "VALUES ('adjustment', :asset, :ref) RETURNING id"
            ),
            {"asset": BTC, "ref": unique_ref("raw")},
        ).scalar_one()
        session.execute(
            text("INSERT INTO postings (entry_id, account_id, amount) VALUES (:entry, :acct, 0)"),
            {"entry": entry_id, "acct": account_id},
        )
        session.flush()


# --------------------------------------------------------------------------
# invariant 3 — materialized equals derived
# --------------------------------------------------------------------------


def test_materialized_matches_derived_after_a_random_entry_sequence(
    session: Session, user_id: str
) -> None:
    rng = random.Random(20260810)
    users = [f"{user_id}-{index}" for index in range(4)]
    for user in users:
        credit_user(session, user=user, amount=rng.randrange(10_000, 100_000))

    for _ in range(40):
        user = rng.choice(users)
        available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user)
        balance = -available.balance
        if balance <= 1:
            continue
        amount = rng.randrange(1, balance)
        service.post_entry(
            session,
            kind=EntryKind.WITHDRAWAL_HOLD,
            asset_id=BTC,
            source_ref=unique_ref("withdrawal_hold"),
            postings=[(available.id, amount), (held.id, -amount)],
        )
        session.commit()
        if rng.random() < 0.5:
            service.post_entry(
                session,
                kind=EntryKind.WITHDRAWAL_RELEASE,
                asset_id=BTC,
                source_ref=unique_ref("withdrawal_release"),
                postings=[(held.id, amount), (available.id, -amount)],
            )
            session.commit()

    derived = derived_balances(session)
    for account in session.query(Account).all():
        session.refresh(account)
        assert account.balance == derived[account.id]
    assert balance_drifts(session) == []


def test_drift_is_detected_and_reported(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=5_000)
    hot_account = hot(session)
    session.execute(
        text("UPDATE accounts SET balance = balance + 1 WHERE id = :id"), {"id": hot_account.id}
    )
    session.expire_all()

    drifts = balance_drifts(session)
    assert [drift.account_id for drift in drifts] == [hot_account.id]
    assert drifts[0].difference == 1
    with pytest.raises(InvariantViolation, match="drifted"):
        assert_ledger_consistent(session)
    # Never committed: the autouse fixture re-checks a clean database.
    session.rollback()


# --------------------------------------------------------------------------
# invariant 4 — no overdraft
# --------------------------------------------------------------------------


def test_overdraft_rejected_by_the_check_constraint(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)

    with pytest.raises(service.InsufficientFunds) as excinfo:
        service.post_entry(
            session,
            kind=EntryKind.WITHDRAWAL_HOLD,
            asset_id=BTC,
            source_ref=unique_ref("withdrawal_hold"),
            postings=[(available.id, 1_001), (held.id, -1_001)],
        )
    assert excinfo.value.constraint == "no_overdraft"

    session.refresh(available)
    assert available.balance == -1_000


def test_spending_the_exact_balance_is_allowed(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=BTC,
        source_ref=unique_ref("withdrawal_hold"),
        postings=[(available.id, 1_000), (held.id, -1_000)],
    )
    session.commit()
    session.refresh(available)
    session.refresh(held)
    assert available.balance == 0
    assert held.balance == -1_000


def test_custody_cannot_go_negative(session: Session) -> None:
    """Debit the exempt receivable so the only sign check left is the custody one."""
    deficit = system(session, AccountKind.USER_DEFICIT)
    with pytest.raises(service.InsufficientFunds) as excinfo:
        service.post_entry(
            session,
            kind=EntryKind.ADJUSTMENT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(deficit.id, 500), (hot(session).id, -500)],
        )
    assert excinfo.value.constraint == "no_negative_asset"
    session.rollback()


def test_raw_update_cannot_push_a_liability_positive(session: Session, user_id: str) -> None:
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    session.commit()
    with pytest.raises(IntegrityError, match="no_overdraft"):
        session.execute(
            text("UPDATE accounts SET balance = 1 WHERE id = :id"), {"id": available.id}
        )
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------
# critique #5 — external and user_deficit are exempt from the sign CHECKs
# --------------------------------------------------------------------------


def test_external_may_hold_a_debit_balance(session: Session, user_id: str) -> None:
    """A correction crediting a user out of nowhere pushes `external` positive."""
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    external = system(session, AccountKind.EXTERNAL)
    service.post_entry(
        session,
        kind=EntryKind.ADJUSTMENT,
        asset_id=BTC,
        source_ref=unique_ref("adjustment"),
        postings=[(external.id, 750), (available.id, -750)],
    )
    session.commit()
    session.refresh(external)
    assert external.balance == 750


def test_user_deficit_may_hold_a_credit_balance(session: Session) -> None:
    external = system(session, AccountKind.EXTERNAL)
    deficit = system(session, AccountKind.USER_DEFICIT)
    service.post_entry(
        session,
        kind=EntryKind.ADJUSTMENT,
        asset_id=BTC,
        source_ref=unique_ref("adjustment"),
        postings=[(deficit.id, -400), (external.id, 400)],
    )
    session.commit()
    session.refresh(deficit)
    assert deficit.balance == -400


def test_reorg_loss_on_an_already_spent_balance_is_bookable(session: Session, user_id: str) -> None:
    """The scenario the whole `user_deficit` account exists for.

    Deposit credited at 1 confirmation, user withdraws all of it, then the
    deposit is reorged out. The reversal cannot debit user_available — that
    money is gone — so the loss lands on a receivable instead. The float that
    absorbs it is other users' money, which is exactly why this is an
    insolvency event and not an accounting curiosity.
    """
    credit_user(session, user=f"{user_id}-float", amount=500_000)
    credit_user(session, user=user_id, amount=100_000)
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)

    # The user's withdrawal has already settled: hold, then settle out of custody.
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=BTC,
        source_ref=unique_ref("withdrawal_hold"),
        postings=[(available.id, 100_000), (held.id, -100_000)],
    )
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_SETTLE,
        asset_id=BTC,
        source_ref=unique_ref("withdrawal_settle"),
        postings=[(held.id, 100_000), (hot(session).id, -100_000)],
    )
    session.commit()

    deposit_credit = (
        session.query(JournalEntry)
        .filter_by(kind=EntryKind.DEPOSIT_CREDIT)
        .order_by(JournalEntry.id.desc())
        .first()
    )
    assert deposit_credit is not None
    deficit = system(session, AccountKind.USER_DEFICIT)
    reversal = service.post_entry(
        session,
        kind=EntryKind.REVERSAL,
        asset_id=BTC,
        source_ref=unique_ref("reorg"),
        postings=[(deficit.id, 100_000), (hot(session).id, -100_000)],
        memo="deposit reorged out after the balance was withdrawn",
        reverses_entry_id=deposit_credit.id,
    )
    session.commit()

    assert reversal.reverses_entry_id == deposit_credit.id
    session.refresh(deficit)
    assert deficit.balance == 100_000
    report = next(r for r in custody_reports(session) if r.asset_id == BTC)
    # Custody is short by exactly the reorged amount, and the shortfall has a
    # name in the books instead of being an unexplained divergence.
    assert report.custody == 400_000
    assert report.user_available == 500_000
    assert report.user_deficit == 100_000
    assert report.balanced


# --------------------------------------------------------------------------
# invariant 5 — immutability
# --------------------------------------------------------------------------


def test_updating_a_posting_raises(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("UPDATE postings SET amount = amount + 1"))
        session.flush()
    session.rollback()


def test_deleting_a_posting_raises(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("DELETE FROM postings"))
        session.flush()
    session.rollback()


def test_updating_a_journal_entry_raises(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("UPDATE journal_entries SET memo = 'edited'"))
        session.flush()
    session.rollback()


def test_deleting_a_journal_entry_raises(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=1_000)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("DELETE FROM journal_entries"))
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------
# invariant 6 — one ledger effect per source_ref
# --------------------------------------------------------------------------


def test_duplicate_source_ref_raises_already_posted_with_the_first_entry(
    session: Session, user_id: str
) -> None:
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    source_ref = "btcpay_payment:inv_1:pay_1"
    first = service.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=BTC,
        source_ref=source_ref,
        postings=[(hot(session).id, 5_000), (available.id, -5_000)],
    )
    session.commit()

    with pytest.raises(service.AlreadyPosted) as excinfo:
        service.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=BTC,
            source_ref=source_ref,
            postings=[(hot(session).id, 5_000), (available.id, -5_000)],
        )
    session.commit()

    assert excinfo.value.entry_id == first.id
    assert excinfo.value.source_ref == source_ref
    session.refresh(available)
    assert available.balance == -5_000


def test_replaying_a_credit_five_times_changes_nothing(session: Session, user_id: str) -> None:
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    for _ in range(5):
        # AlreadyPosted is what a caller treats as success — this loop is the
        # webhook replay drill written the way a handler would write it.
        with contextlib.suppress(service.AlreadyPosted):
            service.post_entry(
                session,
                kind=EntryKind.DEPOSIT_CREDIT,
                asset_id=BTC,
                source_ref="btcpay_payment:inv_9:pay_9",
                postings=[(hot(session).id, 42_000), (available.id, -42_000)],
            )
        session.commit()

    session.refresh(available)
    assert available.balance == -42_000
    assert session.query(JournalEntry).count() == 1


def test_the_same_source_ref_under_a_different_kind_is_a_different_effect(
    session: Session, user_id: str
) -> None:
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    credit_user(session, user=user_id, amount=1_000)
    shared_ref = "withdrawal:w-1"
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=BTC,
        source_ref=shared_ref,
        postings=[(available.id, 1_000), (held.id, -1_000)],
    )
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_RELEASE,
        asset_id=BTC,
        source_ref=shared_ref,
        postings=[(held.id, 1_000), (available.id, -1_000)],
    )
    session.commit()
    assert session.query(JournalEntry).count() == 3


# --------------------------------------------------------------------------
# asset isolation and the per-asset gate
# --------------------------------------------------------------------------


def test_postings_must_belong_to_the_entry_asset(session: Session, user_id: str) -> None:
    btc_available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    usdt_hot = hot(session, USDT)
    with pytest.raises(service.AssetMismatch):
        service.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(usdt_hot.id, 100), (btc_available.id, -100)],
        )
    session.rollback()


def test_posting_to_a_missing_account_raises(session: Session, user_id: str) -> None:
    available, _ = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    with pytest.raises(service.AccountNotFound):
        service.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=BTC,
            source_ref=unique_ref(),
            postings=[(999_999, 100), (available.id, -100)],
        )
    session.rollback()


def test_lock_asset_gate_returns_the_hot_wallet_row(session: Session) -> None:
    gate = service.lock_asset_gate(session, BTC)
    assert gate.kind == AccountKind.HOT_WALLET
    assert gate.asset_id == BTC
    assert gate.id == hot(session).id
    session.commit()


def test_lock_asset_gate_rejects_an_unknown_asset(session: Session) -> None:
    with pytest.raises(service.AccountNotFound, match="hot_wallet"):
        service.lock_asset_gate(session, "DOGE")
    session.rollback()


# --------------------------------------------------------------------------
# invariant 9 — custody identity
# --------------------------------------------------------------------------


def test_custody_identity_holds_across_a_full_withdrawal_with_fee(
    session: Session, user_id: str
) -> None:
    credit_user(session, user=user_id, amount=1_000_000)
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    gross, fee = 400_000, 2_500

    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=BTC,
        source_ref="withdrawal_hold:w-77",
        postings=[(available.id, gross), (held.id, -gross)],
    )
    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_SETTLE,
        asset_id=BTC,
        source_ref="withdrawal_settle:w-77",
        postings=[
            (held.id, gross),
            (hot(session).id, -gross),
            (system(session, AccountKind.NETWORK_FEE_EXPENSE).id, fee),
            (system(session, AccountKind.FEE_INCOME).id, -fee),
        ],
    )
    session.commit()

    report = next(r for r in custody_reports(session) if r.asset_id == BTC)
    assert report.custody == 600_000
    assert report.user_available == 600_000
    assert report.user_hold == 0
    assert report.fee_income == fee
    assert report.network_fee_expense == fee
    assert report.balanced
    assert_ledger_consistent(session)


def test_payouts_in_flight_carries_real_postings(session: Session, user_id: str) -> None:
    """Critique #9: the account is used, so Job C's tolerance is derived rather than guessed."""
    credit_user(session, user=user_id, amount=1_000_000)
    available, held = service.get_user_accounts(session, asset_id=BTC, external_user_id=user_id)
    in_flight = system(session, AccountKind.PAYOUTS_IN_FLIGHT)
    net_plus_fee = 300_000

    service.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=BTC,
        source_ref="withdrawal_hold:w-88",
        postings=[(available.id, net_plus_fee), (held.id, -net_plus_fee)],
    )
    service.post_entry(
        session,
        kind=EntryKind.ADJUSTMENT,
        asset_id=BTC,
        source_ref="payout_submitted:w-88",
        postings=[(in_flight.id, net_plus_fee), (hot(session).id, -net_plus_fee)],
    )
    session.commit()

    report = next(r for r in custody_reports(session) if r.asset_id == BTC)
    assert report.custody == 1_000_000
    session.refresh(in_flight)
    assert in_flight.balance == net_plus_fee
    assert_ledger_consistent(session)


def test_assets_do_not_leak_into_each_other(session: Session, user_id: str) -> None:
    credit_user(session, user=user_id, amount=250_000, asset=BTC)
    credit_user(session, user=user_id, amount=7_500_000, asset=USDT)

    reports = {report.asset_id: report for report in custody_reports(session)}
    assert reports[BTC].custody == 250_000
    assert reports[USDT].custody == 7_500_000
    assert all(report.balanced for report in reports.values())
