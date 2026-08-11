"""What the reconciliation jobs do when the answer is bad.

Reconciliation is the correctness mechanism, not a safety net — so its failure
handling is load-bearing in a way a retry loop's usually is not. Every job here
walks a batch, and the property under test is always the same one: **one bad
row must not stop the batch.** A sweep that aborts on the first BTCPay error
leaves every deposit behind it unpolled, and deposits are only correct because
they get polled.

The second property is that a failure is *counted*. `report.errors` is what a
heartbeat records and what an operator reads; a swallowed exception that
reports success is worse than a crash.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayNotFound, BTCPayUnavailable
from crypto_processing_api.gateway.trongrid import TronGridUnavailable
from crypto_processing_api.ledger.models import (
    Asset,
    Deposit,
    DepositStatus,
    WalletTxoAlert,
    WithdrawalStatus,
)
from crypto_processing_api.services import asset_registry
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.backends import ManualTronBackend, TronTxVerifier
from crypto_processing_api.services.fees import FeeQuote
from crypto_processing_api.workers import payout_submitter, reconciliation
from tests.fake_tron import DESTINATION, HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, USDT, credit_user

DEST = regtest_address("recon-destination")
HALF_BTC = "0.50000000"
GROSS = 100_000
FUNDED = 1_000_000


def make_deposit(session: Session, fake: FakeBTCPay, *, user: str) -> Deposit:
    deposit = deposit_service.create_deposit(session, external_user_id=user, asset_id=BTC)
    deposit_service.ensure_invoice(session, fake, get_settings(), deposit=deposit)
    session.commit()
    return deposit


def make_submitted_withdrawal(
    session: Session, session_factory: sessionmaker[Session], fake: FakeBTCPay, *, user: str
) -> uuid.UUID:
    credit_user(session, user=user, amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id=user,
        asset_id=BTC,
        amount_gross=GROSS,
        destination_address=DEST,
    )
    session.commit()
    payout_submitter.submit_approved(session_factory, fake, get_settings())
    session.expire_all()
    return outcome.withdrawal.id


# -- Job A: the deposit sweep ---------------------------------------------


def test_an_invoice_btcpay_has_forgotten_is_counted_not_raised(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """BTCPay pruning an invoice must not stop the sweep. The deposit stays
    exactly as it was, so a human can see it never resolved."""
    deposit = make_deposit(session, fake_btcpay, user="forgotten")
    fake_btcpay.fail_next["get_invoice"] = BTCPayNotFound("no invoice inv-1")

    report = reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    assert report.errors == 1
    assert report.checked == 0
    session.expire_all()
    assert session.get(Deposit, deposit.id).status is DepositStatus.PENDING  # type: ignore[union-attr]


def test_one_failing_deposit_does_not_stop_the_ones_behind_it(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    first = make_deposit(session, fake_btcpay, user="broken")
    second = make_deposit(session, fake_btcpay, user="fine")
    assert second.btcpay_invoice_id
    fake_btcpay.add_payment(second.btcpay_invoice_id, HALF_BTC)
    fake_btcpay.settle(second.btcpay_invoice_id)
    # Only the first call fails; the sweep visits them in creation order.
    fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("BTCPay is down", status_code=503)

    report = reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    assert report.errors == 1
    assert report.checked == 1
    assert report.credited_units == 50_000_000
    session.expire_all()
    assert session.get(Deposit, first.id).status is DepositStatus.PENDING  # type: ignore[union-attr]
    assert session.get(Deposit, second.id).status is DepositStatus.SETTLED  # type: ignore[union-attr]


# -- adoption of a stuck `creating` row ------------------------------------


def stick_in_creating(session: Session, *, user: str) -> Deposit:
    deposit = deposit_service.create_deposit(session, external_user_id=user, asset_id=BTC)
    deposit.created_at = datetime.now(UTC) - timedelta(minutes=10)
    session.commit()
    return deposit


def test_a_failed_adoption_is_counted_and_the_row_is_left_for_the_next_pass(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = stick_in_creating(session, user="adopt-fail")
    fake_btcpay.fail_next["list_invoices"] = BTCPayUnavailable("BTCPay is down", status_code=503)

    report = reconciliation.adopt_stuck_creating(session_factory, fake_btcpay, get_settings())

    assert report.errors == 1
    assert report.adopted == 0
    session.expire_all()
    assert session.get(Deposit, deposit.id).status is DepositStatus.CREATING  # type: ignore[union-attr]


def test_a_row_deleted_between_the_two_queries_is_skipped(
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate list is read in one transaction and each row worked in
    another. A row that disappeared in between is not an error."""

    class Vanished:
        id = uuid.uuid4()

    monkeypatch.setattr(
        deposit_service, "stuck_in_creating", lambda session, older_than: [Vanished()]
    )
    report = reconciliation.adopt_stuck_creating(session_factory, fake_btcpay, get_settings())
    assert report.adopted == 0
    assert report.errors == 0


# -- the orphan scan -------------------------------------------------------


def test_an_empty_store_scans_nothing(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    report = reconciliation.scan_for_orphan_invoices(session_factory, fake_btcpay, get_settings())
    assert report.scanned == 0
    assert report.orphans == []


def test_an_invoice_whose_row_lost_its_id_is_not_an_orphan(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The ambiguous-timeout shape: BTCPay made the invoice, our row never
    learned its id. The deposit id in the metadata still finds it, so calling
    it an orphan would page an operator about a deposit that is fine."""
    deposit = make_deposit(session, fake_btcpay, user="lost-id")
    deposit.btcpay_invoice_id = None
    session.commit()

    report = reconciliation.scan_for_orphan_invoices(session_factory, fake_btcpay, get_settings())

    assert report.scanned == 1
    assert report.orphans == []


def test_the_scan_pages_through_more_invoices_than_one_request_returns(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """A store with a year of invoices must be fully scanned, not just its
    first page — the orphan is as likely to be on page nine."""
    for index in range(3):
        make_deposit(session, fake_btcpay, user=f"paged-{index}")
    session.execute(text("UPDATE deposits SET btcpay_invoice_id = NULL"))
    session.commit()

    report = reconciliation.scan_for_orphan_invoices(
        session_factory, fake_btcpay, get_settings(), page_size=1
    )
    assert report.scanned == 3


# -- the unattributed-receive scan ----------------------------------------


def test_a_wallet_that_cannot_be_read_is_logged_and_skipped(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    fake_btcpay.fail_next["get_wallet_transactions"] = BTCPayUnavailable(
        "BTCPay is down", status_code=503
    )
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.scanned == 0
    assert report.new_alerts == 0


def test_a_transaction_with_no_hash_cannot_be_matched_and_is_skipped(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """Matching is by txid. Without one there is nothing to compare, and an
    alert naming no transaction would be unactionable."""
    fake_btcpay.wallet_transactions.append({"amount": "0.001", "confirmations": "3"})
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.scanned == 0


def test_a_zero_amount_transaction_is_not_a_receive(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    fake_btcpay.add_wallet_transaction("d" * 64, "0.00000000")
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.scanned == 0


@pytest.mark.parametrize("confirmations", [None, "many", "", 4])
def test_an_unreadable_confirmation_count_still_raises_the_alert(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    confirmations: Any,
) -> None:
    """The alert is about money in the wallet that nobody was credited for.
    Losing it because a count field was a word would be the wrong trade."""
    fake_btcpay.wallet_transactions.append(
        {"transactionHash": "e" * 64, "amount": "0.001", "confirmations": confirmations}
    )
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.new_alerts == 1

    session.rollback()
    alert = session.execute(select(WalletTxoAlert)).scalar_one()
    assert alert.confirmations == (4 if confirmations == 4 else None)
    assert alert.status == "open"


# -- Job B: the withdrawal sweep ------------------------------------------


def test_a_payout_that_cannot_be_polled_is_counted_and_left_alone(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    withdrawal_id = make_submitted_withdrawal(
        session, session_factory, fake_btcpay, user="unpollable"
    )
    fake_btcpay.fail_next["get_payout"] = BTCPayUnavailable("BTCPay is down", status_code=503)

    report = reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    assert report.errors == 1
    assert report.checked == 0
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.SUBMITTED


# -- Job B for operator-sent USDT -----------------------------------------


def test_the_manual_sweep_does_nothing_without_a_hot_wallet(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BTC-only deployment has no TRON wallet, and verification without one
    would compare every transfer's sender against an empty string."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", None)
    report = reconciliation.sweep_manual_withdrawals(session_factory, FakeTronGrid(), settings)
    assert report.checked == 0


def test_a_trongrid_outage_during_the_manual_sweep_is_counted(
    session: Session, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transfer may well have happened. Failing the row on an unreachable
    verifier would refund a user who has already been paid."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)

    tron = FakeTronGrid()
    withdrawal_id = broadcast_manual_usdt(session, user="tron-down", tron=tron)
    tron.fail_next["get_transaction"] = TronGridUnavailable("TronGrid is down", status_code=503)

    report = reconciliation.sweep_manual_withdrawals(session_factory, tron, settings)

    assert report.errors == 1
    assert report.checked == 0
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.BROADCAST


USDT_GROSS = 200_000_000
USDT_FEE = 1_000_000
USDT_TXID = "7" * 64


def broadcast_manual_usdt(session: Session, *, user: str, tron: FakeTronGrid) -> uuid.UUID:
    """A USDT withdrawal an operator has already sent, ready to be polled.

    Driven through `submit_manual` and `mark_broadcast` rather than by writing
    the status, so the row is the one the production path produces.
    """
    credit_user(session, user=user, amount=500_000_000, asset=USDT)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id=user,
        asset_id=USDT,
        amount_gross=USDT_GROSS,
        destination_address=DESTINATION,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        force_manual=True,
    )
    withdrawal_id = outcome.withdrawal.id
    session.commit()

    withdrawal_service.approve(session, withdrawal_id, actor="ops")
    locked = withdrawal_service.lock(session, withdrawal_id)
    withdrawal_service.submit_manual(
        session,
        locked,
        quote=FeeQuote(
            fee=USDT_FEE,
            net=USDT_GROSS - USDT_FEE,
            wallet_fee=0,
            sat_per_vb=0.0,
            source="asset_flat_fee",
        ),
    )
    session.commit()

    tron.add_transfer(USDT_TXID, recipient=DESTINATION, amount=USDT_GROSS - USDT_FEE)
    backend = ManualTronBackend(
        TronTxVerifier(tron, contract_address=USDT_CONTRACT, hot_wallet_address=HOT_WALLET)
    )
    withdrawal_service.mark_broadcast(
        session, backend, withdrawal_id=withdrawal_id, txid=USDT_TXID, actor="ops"
    )
    session.commit()
    return withdrawal_id


# -- Job C: custody --------------------------------------------------------


def test_an_asset_with_no_profile_reports_no_custody_source(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job C must survive a fork's half-added asset. Reporting `no_source` is
    honest; crashing would take the whole hourly check down with it."""
    credit_user(session, user="unprofiled", amount=FUNDED)
    monkeypatch.setattr(asset_registry, "get_registry", dict)

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())

    btc_line = next(line for line in report.custody if line.asset_id == BTC)
    assert btc_line.chain_source == "no_source"
    assert btc_line.chain_balance is None
    assert btc_line.difference is None
    # No chain number means no verdict, never a false clean bill of health.
    assert btc_line.insolvent is False
    assert report.healthy


def test_a_wallet_that_will_not_answer_is_reported_as_unavailable(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
) -> None:
    """An operator reading `btcpay_wallet` when the call actually failed would
    take a missing number for a real one."""
    credit_user(session, user="nowallet", amount=FUNDED)
    fake_btcpay.fail_next["get_wallet"] = BTCPayUnavailable("BTCPay is down", status_code=503)

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())

    btc_line = next(line for line in report.custody if line.asset_id == BTC)
    assert btc_line.chain_source == "btcpay_wallet_unavailable"
    assert btc_line.chain_balance is None


def test_an_asset_with_no_accounts_contributes_no_custody_line(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    session.add(
        Asset(
            id="DOGE",
            display_name="Dogecoin",
            decimals=8,
            unit_name="koinu",
            btcpay_payment_method="DOGE-CHAIN",
            withdrawal_auto_limit=0,
            withdrawal_daily_cap=0,
            invoice_currency="DOGE",
            deposit_expiry_minutes=60,
            enabled=False,
        )
    )
    session.commit()

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    assert "DOGE" not in {line.asset_id for line in report.custody}


@pytest.mark.no_ledger_check
def test_job_c_reports_an_entry_that_does_not_sum_to_zero(
    session: Session,
    session_factory: sessionmaker[Session],
    engine: Engine,
    fake_btcpay: FakeBTCPay,
    alerts: Any,
) -> None:
    """The detector exists for a database somebody wrote to by hand. Producing
    one takes turning the append-only trigger off, which is exactly how it
    would happen in real life.
    """
    credit_user(session, user="broken-books", amount=FUNDED)
    account_id = session.execute(
        text("SELECT id FROM accounts WHERE kind = 'hot_wallet' AND asset_id = :asset"),
        {"asset": BTC},
    ).scalar_one()

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE postings DISABLE TRIGGER USER"))
    try:
        with engine.begin() as connection:
            entry_id = connection.execute(
                text(
                    "INSERT INTO journal_entries (kind, asset_id, source_ref) "
                    "VALUES ('adjustment', :asset, :ref) RETURNING id"
                ),
                {"asset": BTC, "ref": f"hand-written:{uuid.uuid4()}"},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO postings (entry_id, account_id, amount) VALUES (:e, :a, 7)"),
                {"e": entry_id, "a": account_id},
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE postings ENABLE TRIGGER USER"))

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())

    assert not report.consistent
    assert any("sums to 7" in line for line in report.unbalanced_entries)
    assert "ledger.invariant_failure" in alerts.codes()
