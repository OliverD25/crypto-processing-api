"""Job A, invoice adoption, orphan detection and the unattributed-receive scan."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Deposit,
    DepositStatus,
    JournalEntry,
    WalletTxoAlert,
)
from crypto_processing_api.services import assets as asset_service
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.workers import reconciliation
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, USDT

HALF_BTC = "0.50000000"
HALF_BTC_SATS = 50_000_000


def make_deposit(session: Session, fake: FakeBTCPay, *, user: str = "u1") -> Deposit:
    deposit = deposit_service.create_deposit(session, external_user_id=user, asset_id=BTC)
    deposit_service.ensure_invoice(session, fake, get_settings(), deposit=deposit)
    session.commit()
    return deposit


def balance(session: Session, user: str) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == BTC,
            Account.kind == AccountKind.USER_AVAILABLE,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


# -- Job A -----------------------------------------------------------------


def test_sweep_credits_what_webhooks_never_delivered(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = make_deposit(session, fake_btcpay, user="swept")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)

    report = reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())
    assert report.checked == 1
    assert report.changed == 1
    assert report.credited_units == HALF_BTC_SATS
    assert balance(session, "swept") == HALF_BTC_SATS


def test_sweep_is_idempotent(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = make_deposit(session, fake_btcpay, user="twice")
    fake_btcpay.add_payment(deposit.btcpay_invoice_id or "", HALF_BTC)
    fake_btcpay.settle(deposit.btcpay_invoice_id or "")

    for _ in range(3):
        reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    assert balance(session, "twice") == HALF_BTC_SATS
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 1


def test_sweep_survives_btcpay_being_down(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    make_deposit(session, fake_btcpay)
    fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("down")
    report = reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())
    assert report.errors == 1
    assert report.checked == 0


def test_settled_deposits_stay_in_the_sweep_while_monitoring_is_open(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """Users reuse saved addresses. A settled invoice is not done being watched."""
    deposit = make_deposit(session, fake_btcpay, user="afterlife")
    deposit.status = DepositStatus.SETTLED
    deposit.monitoring_expires_at = datetime.now(UTC) + timedelta(hours=6)
    session.commit()

    due = deposit_service.due_for_sweep(session, settled_window=timedelta(days=7), limit=10)
    assert [d.id for d in due] == [deposit.id]


def test_settled_deposits_leave_the_sweep_once_monitoring_closes(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """Polling past the window is theatre: BTCPay no longer links payments to it."""
    deposit = make_deposit(session, fake_btcpay, user="past-it")
    deposit.status = DepositStatus.SETTLED
    deposit.monitoring_expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    due = deposit_service.due_for_sweep(session, settled_window=timedelta(days=7), limit=10)
    assert due == []


def test_dismissed_deposits_are_never_swept(session: Session, fake_btcpay: FakeBTCPay) -> None:
    deposit = make_deposit(session, fake_btcpay)
    deposit.status = DepositStatus.DISMISSED
    session.commit()
    assert deposit_service.due_for_sweep(session, settled_window=timedelta(days=7), limit=10) == []


def test_review_deposits_keep_being_swept(session: Session, fake_btcpay: FakeBTCPay) -> None:
    deposit = make_deposit(session, fake_btcpay)
    deposit.status = DepositStatus.REVIEW
    session.commit()
    due = deposit_service.due_for_sweep(session, settled_window=timedelta(days=7), limit=10)
    assert [d.id for d in due] == [deposit.id]


def test_sweeping_a_review_deposit_never_credits_it(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """Once a human owns it, no poll may quietly hand the money over."""
    deposit = make_deposit(session, fake_btcpay, user="human-owned")
    invoice_id = deposit.btcpay_invoice_id or ""
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    deposit.status = DepositStatus.REVIEW
    session.commit()

    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())
    assert balance(session, "human-owned") == 0
    session.refresh(deposit)
    assert deposit.status == DepositStatus.REVIEW


# -- ambiguous creation ----------------------------------------------------


def test_adopt_finds_the_invoice_created_before_a_timeout(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = deposit_service.create_deposit(session, external_user_id="ghost", asset_id=BTC)
    session.commit()
    # BTCPay made the invoice; our process died before recording it.
    invoice = fake_btcpay.create_top_up_invoice(
        currency="BTC", metadata=deposit_service.build_metadata(deposit)
    )
    session.execute(text("UPDATE deposits SET created_at = now() - interval '10 minutes'"))
    session.commit()

    report = reconciliation.adopt_stuck_creating(session_factory, fake_btcpay, get_settings())
    assert report.adopted == 1

    session.refresh(deposit)
    assert deposit.btcpay_invoice_id == invoice.id
    assert deposit.status == DepositStatus.PENDING
    assert len(fake_btcpay.invoices) == 1


def test_adopt_creates_an_invoice_when_btcpay_never_made_one(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = deposit_service.create_deposit(session, external_user_id="ghost2", asset_id=BTC)
    session.execute(text("UPDATE deposits SET created_at = now() - interval '10 minutes'"))
    session.commit()

    report = reconciliation.adopt_stuck_creating(session_factory, fake_btcpay, get_settings())
    assert report.adopted == 1
    session.refresh(deposit)
    assert deposit.btcpay_invoice_id is not None
    assert len(fake_btcpay.invoices) == 1


def test_adopt_leaves_fresh_creating_rows_alone(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """A request still in flight is not stuck."""
    deposit_service.create_deposit(session, external_user_id="in-flight", asset_id=BTC)
    session.commit()
    report = reconciliation.adopt_stuck_creating(session_factory, fake_btcpay, get_settings())
    assert report.adopted == 0


# -- orphan scan -----------------------------------------------------------


def test_orphan_scan_flags_our_invoice_with_no_local_row(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """What a database restored from an older backup looks like."""
    invoice = fake_btcpay.create_top_up_invoice(
        currency="BTC",
        metadata={"cpapi": True, "cpapi_version": 1, "deposit_id": str(uuid.uuid4())},
    )
    report = reconciliation.scan_for_orphan_invoices(session_factory, fake_btcpay, get_settings())
    assert report.orphans == [invoice.id]


def test_orphan_scan_ignores_other_peoples_invoices(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    fake_btcpay.create_top_up_invoice(currency="BTC", metadata={"orderId": "their-shop"})
    report = reconciliation.scan_for_orphan_invoices(session_factory, fake_btcpay, get_settings())
    assert report.orphans == []


def test_orphan_scan_is_quiet_when_everything_is_known(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    make_deposit(session, fake_btcpay)
    report = reconciliation.scan_for_orphan_invoices(session_factory, fake_btcpay, get_settings())
    assert report.scanned == 1
    assert report.orphans == []


# -- the wallet-level detector --------------------------------------------


def test_unmatched_wallet_receive_raises_an_alert(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """A payment to an address BTCPay stopped watching reaches custody uncredited.

    No invoice mentions it, so every invoice-shaped check is blind, and the
    aggregate solvency check reads the surplus as healthy.
    """
    fake_btcpay.add_wallet_transaction("f" * 64, "0.25000000")
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.unmatched == 1
    assert report.new_alerts == 1

    alert = session.execute(select(WalletTxoAlert)).scalar_one()
    assert alert.txid == "f" * 64
    assert alert.amount == 25_000_000
    assert alert.status == "open"


def test_a_receive_matching_a_credited_payment_is_not_flagged(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """On-chain payment ids are `<txid>-<vout>`, so matching is by prefix."""
    deposit = make_deposit(session, fake_btcpay, user="known")
    invoice_id = deposit.btcpay_invoice_id or ""
    txid = "a" * 64
    fake_btcpay.add_payment(invoice_id, HALF_BTC, payment_id=f"{txid}-0")
    fake_btcpay.settle(invoice_id)
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    fake_btcpay.add_wallet_transaction(txid, HALF_BTC)
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.unmatched == 0
    assert session.execute(select(func.count()).select_from(WalletTxoAlert)).scalar_one() == 0


def test_outgoing_transactions_are_not_receives(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    fake_btcpay.add_wallet_transaction("b" * 64, "-0.30000000")
    report = reconciliation.detect_unattributed_receives(
        session_factory, fake_btcpay, get_settings()
    )
    assert report.scanned == 0
    assert report.new_alerts == 0


def test_the_same_receive_alerts_only_once(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    fake_btcpay.add_wallet_transaction("c" * 64, "0.10000000")
    for _ in range(3):
        reconciliation.detect_unattributed_receives(session_factory, fake_btcpay, get_settings())
    assert session.execute(select(func.count()).select_from(WalletTxoAlert)).scalar_one() == 1


def test_alerts_are_visible_to_an_admin(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    client,
    admin_key: str,
) -> None:
    fake_btcpay.add_wallet_transaction("d" * 64, "0.10000000")
    reconciliation.detect_unattributed_receives(session_factory, fake_btcpay, get_settings())

    body = client.get(
        "/v1/admin/wallet-alerts", headers={"Authorization": f"Bearer {admin_key}"}
    ).json()
    assert body["alerts"][0]["txid"] == "d" * 64
    assert body["alerts"][0]["amount"] == "0.10000000"


# -- payment method discovery ---------------------------------------------


def test_discovery_writes_the_real_payment_method_id(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    asset = session.get(Asset, BTC)
    assert asset
    asset.btcpay_payment_method = "stale-value"
    session.flush()

    report = asset_service.sync_payment_methods(session, fake_btcpay)
    session.commit()

    assert report.resolved[BTC] == "BTC-CHAIN"
    assert BTC in report.updated
    session.refresh(asset)
    assert asset.btcpay_payment_method == "BTC-CHAIN"


def test_discovery_is_a_no_op_when_already_correct(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    report = asset_service.sync_payment_methods(session, fake_btcpay)
    assert report.updated == []
    assert report.resolved[BTC] == "BTC-CHAIN"


def test_a_store_that_cannot_take_btc_is_a_startup_failure(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """Invoices for a method the store does not serve are addresses nobody watches."""
    fake_btcpay.payment_methods = ["LTC-CHAIN"]
    with pytest.raises(asset_service.PaymentMethodMissing, match="BTC"):
        asset_service.sync_payment_methods(session, fake_btcpay)


def test_usdt_is_disabled_rather_than_fatal(session: Session, fake_btcpay: FakeBTCPay) -> None:
    """The USDt plugin is M4. BTC must not be held hostage to it."""
    report = asset_service.sync_payment_methods(session, fake_btcpay)
    session.commit()
    assert USDT in report.disabled

    usdt = session.get(Asset, USDT)
    assert usdt
    assert usdt.enabled is False


def test_usdt_is_kept_when_the_plugin_is_present(session: Session, fake_btcpay: FakeBTCPay) -> None:
    fake_btcpay.payment_methods = ["BTC-CHAIN", "USDT_TRC20-TRON"]
    report = asset_service.sync_payment_methods(session, fake_btcpay)
    session.commit()
    assert report.resolved[USDT] == "USDT_TRC20-TRON"
    assert report.disabled == []


def test_an_invoice_status_this_version_never_heard_of_is_survivable(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """A future BTCPay invoice status must not take the deposit sweep down.

    The same latent bug the payout path had, on the deposit side.
    `Invoice.status` was a pydantic Literal, so an unrecognised status was
    rejected during parsing — before `_target_status` or the transition matrix
    could decline to act on it. That ValidationError is not a BTCPayError, so
    `sweep_deposits` did not skip the row: it raised through the batch and
    every deposit queued behind it stopped being polled.

    Two deposits on purpose. The first proves the odd status is ignored, the
    second proves the batch carrying it still made progress.
    """
    odd = make_deposit(session, fake_btcpay, user="odd-status")
    odd_invoice = odd.btcpay_invoice_id
    assert odd_invoice
    fake_btcpay.add_payment(odd_invoice, HALF_BTC)
    fake_btcpay.settle(odd_invoice)

    normal = make_deposit(session, fake_btcpay, user="normal-status")
    normal_invoice = normal.btcpay_invoice_id
    assert normal_invoice
    fake_btcpay.add_payment(normal_invoice, HALF_BTC)
    fake_btcpay.settle(normal_invoice)

    # BTCPay 2.4.2 cannot emit this; a later one might.
    fake_btcpay.invoices[odd_invoice].status = "SettledOnSomeFutureRail"

    report = reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    session.rollback()
    session.refresh(odd)
    assert odd.status == DepositStatus.PENDING, (
        "an unrecognised invoice status must leave the deposit exactly where it was"
    )
    assert balance(session, "odd-status") == 0, "credited on a status we cannot interpret"

    session.refresh(normal)
    assert normal.status == DepositStatus.SETTLED, (
        "the unknown status stopped the rest of the batch from being swept"
    )
    assert balance(session, "normal-status") == HALF_BTC_SATS
    assert report.errors == 0, "an unknown status is news, not an error"


def test_the_deposit_recovers_once_btcpay_says_something_known(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """Why a no-op beats routing to review: the poller heals it by itself."""
    deposit = make_deposit(session, fake_btcpay, user="recovers")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    fake_btcpay.invoices[invoice_id].status = "AStatusFromTheFuture"

    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())
    session.rollback()
    session.refresh(deposit)
    assert deposit.status == DepositStatus.PENDING

    fake_btcpay.invoices[invoice_id].status = "Settled"
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    session.rollback()
    session.refresh(deposit)
    assert deposit.status == DepositStatus.SETTLED
    assert balance(session, "recovers") == HALF_BTC_SATS
