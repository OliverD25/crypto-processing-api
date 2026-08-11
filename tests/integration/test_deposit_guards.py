"""The refusals and the re-entry points in the deposit service.

A deposit is the only path that *creates* money in this ledger, so the branches
that decline to act are the ones worth proving. Three groups:

- **guards that refuse an impossible transition** — failing a deposit that is
  past `creating`, resolving one that is not in review, crediting without
  naming a payment.
- **re-entry after a crash** — the same credit arriving twice, an invoice
  adopted onto a row that never learned its id. Both must produce one credit.
- **the pooled-address tolerance** — the USDT-only check that decides between
  crediting a shared address automatically and sending it to a human.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Deposit,
    DepositPayment,
    DepositStatus,
    EntryKind,
    JournalEntry,
)
from crypto_processing_api.services import deposits as deposit_service
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, USDT

HALF_BTC = "0.50000000"
HALF_BTC_SATS = 50_000_000


def make_deposit(
    session: Session, fake: FakeBTCPay, *, user: str, expected: int | None = None
) -> Deposit:
    deposit = deposit_service.create_deposit(
        session, external_user_id=user, asset_id=BTC, amount_expected=expected
    )
    deposit_service.ensure_invoice(session, fake, get_settings(), deposit=deposit)
    session.commit()
    return deposit


def available(session: Session, user: str, asset: str = BTC) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == asset,
            Account.kind == AccountKind.USER_AVAILABLE,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


# -- transitions the service refuses --------------------------------------


def test_a_deposit_past_creating_cannot_be_failed(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """`mark_failed` is only for a creation BTCPay refused outright. Using it
    later would abandon a deposit that may already hold money."""
    deposit = make_deposit(session, fake_btcpay, user="pending")
    with pytest.raises(deposit_service.IllegalTransition, match="cannot fail a deposit in pending"):
        deposit_service.mark_failed(session, deposit=deposit, reason="too late")


def test_ensuring_an_invoice_twice_makes_only_one(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """A second invoice is a second deposit address nobody is watching."""
    deposit = make_deposit(session, fake_btcpay, user="once")
    invoice_id = deposit.btcpay_invoice_id

    deposit_service.ensure_invoice(session, fake_btcpay, get_settings(), deposit=deposit)
    session.commit()

    assert deposit.btcpay_invoice_id == invoice_id
    assert len(fake_btcpay.invoices) == 1


def test_refreshing_a_deposit_that_is_not_there(session: Session, fake_btcpay: FakeBTCPay) -> None:
    with pytest.raises(deposit_service.DepositNotFound):
        deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=uuid.uuid4())


def test_refreshing_a_deposit_with_no_invoice_yet(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The row commits before the BTCPay call. Asking Greenfield about an
    invoice that was never made would be a 404 the sweep counts as an error."""
    deposit = deposit_service.create_deposit(session, external_user_id="ghost", asset_id=BTC)
    session.commit()
    with pytest.raises(deposit_service.InvoiceMismatch, match="no invoice yet"):
        deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=deposit.id)


def test_applying_an_invoice_to_a_deposit_that_is_not_there(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    deposit = make_deposit(session, fake_btcpay, user="doomed")
    invoice = fake_btcpay.get_invoice(deposit.btcpay_invoice_id or "")
    with pytest.raises(deposit_service.DepositNotFound):
        deposit_service.apply_invoice_state(
            session, fake_btcpay, deposit_id=uuid.uuid4(), invoice=invoice
        )


def test_a_deposit_refuses_an_invoice_that_is_not_its_own(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The correlation is not a hint. Applying another invoice's payments here
    would credit this user from someone else's money."""
    mine = make_deposit(session, fake_btcpay, user="mine")
    theirs = make_deposit(session, fake_btcpay, user="theirs")
    other_invoice = fake_btcpay.get_invoice(theirs.btcpay_invoice_id or "")

    with pytest.raises(deposit_service.InvoiceMismatch, match="belongs to invoice"):
        deposit_service.apply_invoice_state(
            session, fake_btcpay, deposit_id=mine.id, invoice=other_invoice
        )


def test_a_terminal_deposit_is_left_alone(session: Session, fake_btcpay: FakeBTCPay) -> None:
    """A settled deposit that BTCPay later reports differently must not be
    re-credited, and a dismissed one must not be revived."""
    deposit = make_deposit(session, fake_btcpay, user="finished")
    invoice_id = deposit.btcpay_invoice_id or ""
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=deposit.id)
    session.commit()
    assert available(session, "finished") == HALF_BTC_SATS

    deposit.status = DepositStatus.DISMISSED
    session.commit()
    result = deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=deposit.id)
    session.commit()

    assert result.changed is False
    assert result.credited_units == 0
    assert available(session, "finished") == HALF_BTC_SATS


# -- the admin resolve path -----------------------------------------------


def review_deposit(session: Session, fake: FakeBTCPay, *, user: str) -> tuple[Deposit, str]:
    deposit = make_deposit(session, fake, user=user)
    invoice_id = deposit.btcpay_invoice_id or ""
    payment_id = fake.add_payment(invoice_id, HALF_BTC)
    fake.settle(invoice_id, additional_status="PaidLate")
    deposit_service.refresh_deposit(session, fake, deposit_id=deposit.id)
    session.commit()
    assert deposit.status is DepositStatus.REVIEW
    return deposit, payment_id


def test_an_action_the_service_does_not_know_is_refused(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The API model restricts this to two values; the service refuses anything
    else on its own, because a worker or a script can call it directly."""
    deposit, _payment_id = review_deposit(session, fake_btcpay, user="unknown-action")
    with pytest.raises(deposit_service.IllegalTransition, match="unknown resolve action"):
        deposit_service.resolve_review(
            session,
            fake_btcpay,
            deposit_id=deposit.id,
            action="approve",
            payment_id=None,
            actor="ops",
        )


def test_crediting_without_naming_a_payment_is_refused(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    deposit, _payment_id = review_deposit(session, fake_btcpay, user="no-payment")
    with pytest.raises(deposit_service.PaymentNotFound, match="requires payment_id"):
        deposit_service.resolve_review(
            session,
            fake_btcpay,
            deposit_id=deposit.id,
            action="credit",
            payment_id=None,
            actor="ops",
        )


def test_crediting_a_deposit_with_no_invoice_is_refused(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The amount comes from the invoice. Without one there is nothing to read
    it from, and the operator is deliberately not allowed to supply it."""
    deposit, payment_id = review_deposit(session, fake_btcpay, user="no-invoice")
    deposit.btcpay_invoice_id = None
    session.commit()

    with pytest.raises(deposit_service.InvoiceMismatch, match="no invoice to credit from"):
        deposit_service.resolve_review(
            session,
            fake_btcpay,
            deposit_id=deposit.id,
            action="credit",
            payment_id=payment_id,
            actor="ops",
        )


def test_an_admin_credit_records_a_payment_the_poller_never_saw(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The payment can arrive after the invoice went to review, so resolving
    has to be able to record it as well as credit it."""
    deposit, _first = review_deposit(session, fake_btcpay, user="late-payment")
    invoice_id = deposit.btcpay_invoice_id or ""
    second = fake_btcpay.add_payment(invoice_id, "0.25000000")

    result = deposit_service.resolve_review(
        session,
        fake_btcpay,
        deposit_id=deposit.id,
        action="credit",
        payment_id=second,
        actor="ops",
    )
    session.commit()

    assert result.recorded_payments == [second]
    assert result.credited_units == 25_000_000
    assert available(session, "late-payment") == 25_000_000
    assert deposit.status is DepositStatus.SETTLED


# -- crediting exactly once ------------------------------------------------


def test_a_credit_already_in_the_ledger_is_adopted_not_repeated(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """A crash between the ledger entry and the deposit_payments update leaves
    the money credited and the link missing. Posting again would double it; the
    ledger refuses, and the deposit adopts the entry that is already there.
    """
    deposit = make_deposit(session, fake_btcpay, user="already")
    invoice_id = deposit.btcpay_invoice_id or ""
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)

    # Post the credit by hand under the source_ref the deposit path will use.
    hot = ledger.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
    available_account, _held = ledger.get_user_accounts(
        session, asset_id=BTC, external_user_id="already"
    )
    orphan = ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=BTC,
        source_ref=f"btcpay_payment:{invoice_id}:{payment_id}",
        postings=[(hot.id, HALF_BTC_SATS), (available_account.id, -HALF_BTC_SATS)],
    )
    session.commit()

    deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=deposit.id)
    session.commit()

    assert available(session, "already") == HALF_BTC_SATS
    entries = session.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.kind == EntryKind.DEPOSIT_CREDIT)
    ).scalar_one()
    assert entries == 1

    row = session.execute(select(DepositPayment)).scalar_one()
    assert row.ledger_entry_id == orphan.id
    assert row.credited_at is not None


# -- payment methods that do not match the asset --------------------------


def test_an_invoice_serving_a_different_payment_method_leaves_no_address(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """`attach_invoice` matches the asset's payment method. A mismatch must not
    write another rail's destination onto a BTC deposit."""
    deposit = deposit_service.create_deposit(session, external_user_id="mismatch", asset_id=BTC)
    session.commit()
    invoice = fake_btcpay.create_top_up_invoice(currency="BTC", metadata={})

    deposit_service.attach_invoice(
        session,
        deposit=deposit,
        invoice=invoice,
        payment_methods=[],
        asset=deposit_service.get_asset(session, BTC),
    )
    session.commit()

    assert deposit.btcpay_invoice_id == invoice.id
    assert deposit.address is None
    assert deposit.address_reserved_from is None
    # The row still leaves `creating`, so nothing keeps retrying its creation.
    assert deposit.status is DepositStatus.PENDING


# -- the pooled-address tolerance -----------------------------------------


@pytest.fixture
def usdt_store(session: Session, fake_btcpay: FakeBTCPay) -> Any:
    fake_btcpay.payment_methods = ["BTC-CHAIN", "USDT_TRC20"]
    return fake_btcpay


def usdt_deposit(session: Session, fake: FakeBTCPay, *, user: str, expected: int | None) -> Deposit:
    deposit = deposit_service.create_deposit(
        session, external_user_id=user, asset_id=USDT, amount_expected=expected
    )
    deposit_service.ensure_invoice(session, fake, get_settings(), deposit=deposit)
    session.commit()
    return deposit


def test_a_zero_tolerance_switches_the_pooled_check_off(
    session: Session, usdt_store: FakeBTCPay
) -> None:
    """0 disables it, and .env.example says so. A disabled check must credit
    the mismatch rather than quietly routing everything to review."""
    deposit = usdt_deposit(session, usdt_store, user="no-tolerance", expected=200_000_000)
    invoice_id = deposit.btcpay_invoice_id or ""
    usdt_store.add_payment(invoice_id, "500.000000")
    usdt_store.settle(invoice_id)

    deposit_service.refresh_deposit(session, usdt_store, deposit_id=deposit.id, tolerance_pct=0.0)
    session.commit()

    assert deposit.status is DepositStatus.SETTLED
    assert available(session, "no-tolerance", USDT) == 500_000_000


def test_an_invoice_with_nothing_settled_yet_has_nothing_to_compare(
    session: Session, usdt_store: FakeBTCPay
) -> None:
    """A payment still processing is not evidence of the wrong amount, so the
    tolerance must not fire on it."""
    deposit = usdt_deposit(session, usdt_store, user="unsettled", expected=200_000_000)
    invoice_id = deposit.btcpay_invoice_id or ""
    usdt_store.add_payment(invoice_id, "500.000000", status="Processing")
    usdt_store.set_processing(invoice_id)

    deposit_service.refresh_deposit(session, usdt_store, deposit_id=deposit.id, tolerance_pct=1.0)
    session.commit()

    assert deposit.status is DepositStatus.CONFIRMING
    assert available(session, "unsettled", USDT) == 0
