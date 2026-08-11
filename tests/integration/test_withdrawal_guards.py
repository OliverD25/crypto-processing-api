"""The refusals in the withdrawal service.

Every one of these is a branch that says "no" on a money path, and a branch
that says no is only worth having if something proves it fires. The lifecycle
tests walk the happy path; this walks the edges of it:

- the guards that stop a payout being bound to the wrong withdrawal,
- the ones that stop a hold being released twice or released without proof,
- and the compare-and-swap misses, which is what two admins clicking at the
  same moment actually looks like.

Rows are taken to `submitted` by running the real submitter against
`FakeBTCPay`, never by writing the status by hand, so what is being guarded is
a row the production path produced.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    OutboundEvent,
    Withdrawal,
    WithdrawalStatus,
)
from crypto_processing_api.services import asset_registry
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.backends import (
    BACKEND_MANUAL_TRON,
    BackendPayout,
    BackendPayoutState,
    ManualTronBackend,
    TronTxVerifier,
)
from crypto_processing_api.services.fees import FeeQuote
from crypto_processing_api.workers import payout_submitter
from tests.fake_tron import HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, USDT, credit_user

DEST = regtest_address("guard-destination")
GROSS = 100_000
FUNDED = 1_000_000


@pytest.fixture
def manual_backend() -> ManualTronBackend:
    return ManualTronBackend(
        TronTxVerifier(
            FakeTronGrid(), contract_address=USDT_CONTRACT, hot_wallet_address=HOT_WALLET
        )
    )


def make_hold(
    session: Session,
    *,
    user: str,
    amount: int = GROSS,
    destination: str = DEST,
    funded: int = FUNDED,
) -> Withdrawal:
    if funded:
        credit_user(session, user=user, amount=funded)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id=user,
        asset_id=BTC,
        amount_gross=amount,
        destination_address=destination,
    )
    session.commit()
    return outcome.withdrawal


def submit(session_factory: sessionmaker[Session], fake: FakeBTCPay) -> None:
    report = payout_submitter.submit_approved(session_factory, fake, get_settings())
    assert report.submitted >= 1


def payout_for(
    withdrawal: Withdrawal, state: BackendPayoutState, **overrides: Any
) -> BackendPayout:
    fields: dict[str, Any] = {
        "id": withdrawal.backend_ref or "payout-x",
        "destination": withdrawal.destination_address,
        "state": state,
        "amount": "0.00100000",
        "metadata": {"withdrawal_id": str(withdrawal.id), "cpapi": True},
        "raw_state": state.value,
    }
    fields.update(overrides)
    return BackendPayout(**fields)


def available(session: Session, user: str) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == BTC,
            Account.kind == AccountKind.USER_AVAILABLE,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def outbound_count(session: Session) -> int:
    session.rollback()
    return int(session.execute(select(func.count()).select_from(OutboundEvent)).scalar_one())


# -- asset and destination lookups ----------------------------------------


def test_an_unknown_asset_is_refused(session: Session) -> None:
    with pytest.raises(withdrawal_service.WithdrawalError, match="no such asset"):
        withdrawal_service.get_asset(session, "DOGE")


def test_a_disabled_asset_takes_no_new_work_but_stays_readable(session: Session) -> None:
    """An asset BTCPay can no longer serve must not accept withdrawals, and the
    rows already holding user money must still be readable."""
    asset = session.get(Asset, USDT)
    assert asset is not None
    asset.enabled = False
    session.commit()

    with pytest.raises(withdrawal_service.WithdrawalError, match="not currently available"):
        withdrawal_service.get_asset(session, USDT)
    assert withdrawal_service.get_asset(session, USDT, require_enabled=False).id == USDT


def test_a_destination_for_an_asset_with_no_profile_is_refused(session: Session) -> None:
    """A row can exist in `assets` with no registry profile — a fork's
    half-added asset. Validating its address against nothing would accept
    anything, so the answer is that withdrawals are not available."""
    with pytest.raises(withdrawal_service.InvalidDestination, match="not available"):
        withdrawal_service.validate_destination(
            session, get_settings(), asset_id="DOGE", address=DEST
        )
    with pytest.raises(asset_registry.UnknownAsset):
        asset_registry.profile_for("DOGE")


# -- reads -----------------------------------------------------------------


def test_locking_a_withdrawal_that_is_not_there(session: Session) -> None:
    with pytest.raises(withdrawal_service.WithdrawalNotFound):
        withdrawal_service.lock(session, uuid.uuid4())


def test_reading_a_withdrawal_that_is_not_there(session: Session) -> None:
    with pytest.raises(withdrawal_service.WithdrawalNotFound):
        withdrawal_service.get(session, uuid.uuid4())


def test_a_users_list_pages_backwards_from_a_cursor(session: Session) -> None:
    first = make_hold(session, user="pager")
    second = make_hold(session, user="pager", funded=0)

    page = withdrawal_service.list_for_user(session, external_user_id="pager", limit=10)
    assert [row.id for row in page] == [second.id, first.id]

    tail = withdrawal_service.list_for_user(
        session, external_user_id="pager", limit=10, cursor=second.id
    )
    assert [row.id for row in tail] == [first.id]


def test_held_balance_is_zero_before_a_hold_and_the_gross_after(session: Session) -> None:
    assert withdrawal_service.held_balance(session, asset_id=BTC, external_user_id="holder") == 0
    make_hold(session, user="holder")
    session.rollback()
    assert (
        withdrawal_service.held_balance(session, asset_id=BTC, external_user_id="holder") == GROSS
    )


# -- releasing a hold ------------------------------------------------------


def test_whitespace_is_not_an_attestation(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """`min_length` on the request model counts characters, so ten spaces pass
    it. A release from `submitted` is a claim that the money is not going to
    arrive, and it needs a reason someone can be held to."""
    withdrawal = make_hold(session, user="blank")
    submit(session_factory, fake_btcpay)
    session.expire_all()

    with pytest.raises(withdrawal_service.ReleaseNotPermitted, match="attestation"):
        withdrawal_service.admin_release(
            session, withdrawal.id, actor="ops", attestation="          "
        )


def test_a_refunded_row_cannot_be_refunded_again(session: Session) -> None:
    """Two admins on the same row must produce one refund. The second one is
    refused by status rather than by silently posting nothing, so the operator
    is told the money already went back."""
    credit_user(session, user="twice", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="twice",
        asset_id=BTC,
        amount_gross=FUNDED,  # above the auto limit, so it queues
        destination_address=DEST,
    )
    session.commit()
    assert outcome.withdrawal.status is WithdrawalStatus.PENDING_APPROVAL

    withdrawal_service.reject(session, outcome.withdrawal.id, actor="ops", reason="no")
    session.commit()
    assert available(session, "twice") == FUNDED

    locked = withdrawal_service.lock(session, outcome.withdrawal.id)
    with pytest.raises(withdrawal_service.ReleaseNotPermitted, match="refunded cannot be released"):
        withdrawal_service.release_hold(session, locked, actor="ops2")
    session.rollback()
    assert available(session, "twice") == FUNDED


def test_releasing_an_approved_row_refunds_through_rejected(session: Session) -> None:
    """A queued withdrawal that never reached a payout is a rejection, not a
    failure. They are different events and the matrix says so."""
    withdrawal = make_hold(session, user="queued")
    assert withdrawal.status is WithdrawalStatus.APPROVED

    withdrawal_service.admin_release(
        session, withdrawal.id, actor="ops", attestation="never submitted, checked by hand"
    )
    session.commit()

    session.expire_all()
    assert withdrawal_service.get(session, withdrawal.id).status is WithdrawalStatus.REFUNDED
    assert available(session, "queued") == FUNDED


def test_a_confirmed_withdrawal_can_never_be_released(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    withdrawal = make_hold(session, user="done")
    submit(session_factory, fake_btcpay)
    session.expire_all()
    payout_id = withdrawal_service.get(session, withdrawal.id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    from crypto_processing_api.workers import reconciliation

    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal.id).status is WithdrawalStatus.CONFIRMED

    with pytest.raises(withdrawal_service.ReleaseNotPermitted, match="cannot be released"):
        withdrawal_service.admin_release(
            session, withdrawal.id, actor="ops", attestation="I looked and it is gone"
        )


# -- binding a payout to a row --------------------------------------------


def test_a_payout_carrying_another_withdrawals_id_is_refused(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The correlation key is not a hint. Applying the wrong payout's state
    would settle one user's withdrawal from another user's transaction."""
    mine = make_hold(session, user="mine")
    other = make_hold(session, user="other")
    submit(session_factory, fake_btcpay)
    session.expire_all()
    bound = withdrawal_service.get(session, mine.id)

    with pytest.raises(withdrawal_service.WithdrawalError, match="carries withdrawal_id"):
        withdrawal_service.apply_payout_state(
            session,
            withdrawal_id=mine.id,
            payout=payout_for(
                bound, BackendPayoutState.IN_FLIGHT, metadata={"withdrawal_id": str(other.id)}
            ),
        )


def test_a_row_already_bound_to_a_payout_refuses_a_different_one(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    withdrawal = make_hold(session, user="bound")
    submit(session_factory, fake_btcpay)
    session.expire_all()
    bound = withdrawal_service.get(session, withdrawal.id)

    with pytest.raises(withdrawal_service.WithdrawalError, match="is bound to payout"):
        withdrawal_service.apply_payout_state(
            session,
            withdrawal_id=withdrawal.id,
            payout=payout_for(bound, BackendPayoutState.IN_FLIGHT, id="payout-999"),
        )


def test_a_payout_with_no_echoed_id_is_still_applied(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """A payout created before metadata was echoed back. `backend_ref` already
    binds it, so there is nothing ambiguous left to guard against."""
    withdrawal = make_hold(session, user="unlabelled")
    submit(session_factory, fake_btcpay)
    session.expire_all()
    bound = withdrawal_service.get(session, withdrawal.id)

    withdrawal_service.apply_payout_state(
        session,
        withdrawal_id=withdrawal.id,
        payout=payout_for(bound, BackendPayoutState.IN_FLIGHT, metadata={}, txid="b" * 64),
    )
    session.commit()
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal.id).status is WithdrawalStatus.BROADCAST


def test_btcpay_accepting_the_payout_changes_the_row_but_announces_nothing(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """`AwaitingPayment` moves the row on, and the platform hears nothing: a
    withdrawal is news when it broadcasts, not when BTCPay queues it."""
    withdrawal = make_hold(session, user="accepted")
    submit(session_factory, fake_btcpay)
    session.expire_all()
    bound = withdrawal_service.get(session, withdrawal.id)
    assert bound.status is WithdrawalStatus.SUBMITTED

    before = outbound_count(session)
    withdrawal_service.apply_payout_state(
        session,
        withdrawal_id=withdrawal.id,
        payout=payout_for(bound, BackendPayoutState.AWAITING_APPROVAL),
    )
    session.commit()

    session.expire_all()
    assert withdrawal_service.get(session, withdrawal.id).status is WithdrawalStatus.SUBMITTED
    assert outbound_count(session) == before


# -- the approval compare-and-swap ----------------------------------------


def test_approving_a_withdrawal_that_is_not_there(session: Session) -> None:
    with pytest.raises(withdrawal_service.WithdrawalNotFound):
        withdrawal_service.approve(session, uuid.uuid4(), actor="ops")


def test_rejecting_a_withdrawal_that_is_not_there(session: Session) -> None:
    with pytest.raises(withdrawal_service.WithdrawalNotFound):
        withdrawal_service.reject(session, uuid.uuid4(), actor="ops")


def test_rejecting_something_already_approved_is_a_conflict(session: Session) -> None:
    """Only `pending_approval` may be rejected. Past that a payout may exist,
    and a rejection refunds immediately."""
    withdrawal = make_hold(session, user="approved")
    with pytest.raises(withdrawal_service.IllegalTransition, match="only pending_approval"):
        withdrawal_service.reject(session, withdrawal.id, actor="ops")


# -- the operator-sent path -----------------------------------------------


def test_only_an_approved_row_can_be_handed_to_an_operator(session: Session) -> None:
    """`submit_manual` posts the submit entry, so running it twice would commit
    the same balance twice."""
    withdrawal = make_hold(session, user="manual")
    quote = FeeQuote(fee=1_000, net=GROSS - 1_000, wallet_fee=0, sat_per_vb=0.0, source="flat")
    withdrawal_service.submit_manual(session, withdrawal, quote=quote)
    session.commit()

    with pytest.raises(withdrawal_service.IllegalTransition, match="only approved"):
        withdrawal_service.submit_manual(session, withdrawal, quote=quote)


def test_mark_broadcast_refuses_a_btcpay_backed_withdrawal(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    manual_backend: ManualTronBackend,
) -> None:
    """Nothing else stops an operator pasting a txid against an automated
    payout. That row's money is BTCPay's to send."""
    withdrawal = make_hold(session, user="automated")
    submit(session_factory, fake_btcpay)
    session.expire_all()

    with pytest.raises(withdrawal_service.IllegalTransition, match="mark-broadcast is for"):
        withdrawal_service.mark_broadcast(
            session,
            manual_backend,
            withdrawal_id=withdrawal.id,
            txid="c" * 64,
            actor="ops",
        )


def test_mark_broadcast_refuses_a_row_that_was_never_handed_over(
    session: Session, manual_backend: ManualTronBackend
) -> None:
    withdrawal = make_hold(session, user="early")
    withdrawal.backend = BACKEND_MANUAL_TRON
    session.commit()

    with pytest.raises(withdrawal_service.IllegalTransition, match="only submitted"):
        withdrawal_service.mark_broadcast(
            session,
            manual_backend,
            withdrawal_id=withdrawal.id,
            txid="c" * 64,
            actor="ops",
        )


def test_mark_broadcast_refuses_a_blank_transaction_id(
    session: Session, manual_backend: ManualTronBackend
) -> None:
    """`0x` and whitespace strip down to nothing, and an empty txid would go on
    to match every row with a NULL txid."""
    withdrawal = make_hold(session, user="blanktx")
    withdrawal.backend = BACKEND_MANUAL_TRON
    quote = FeeQuote(fee=1_000, net=GROSS - 1_000, wallet_fee=0, sat_per_vb=0.0, source="flat")
    withdrawal_service.submit_manual(session, withdrawal, quote=quote)
    session.commit()

    with pytest.raises(withdrawal_service.TronVerificationFailed, match="no transaction id"):
        withdrawal_service.mark_broadcast(
            session,
            manual_backend,
            withdrawal_id=withdrawal.id,
            txid="  0x  ",
            actor="ops",
        )


def test_polling_a_row_that_is_not_broadcast_yet_does_nothing(
    session: Session, manual_backend: ManualTronBackend
) -> None:
    withdrawal = make_hold(session, user="notyet")
    withdrawal.backend = BACKEND_MANUAL_TRON
    quote = FeeQuote(fee=1_000, net=GROSS - 1_000, wallet_fee=0, sat_per_vb=0.0, source="flat")
    withdrawal_service.submit_manual(session, withdrawal, quote=quote)
    session.commit()

    polled = withdrawal_service.poll_manual(
        session,
        manual_backend,
        withdrawal_id=withdrawal.id,
        required_confirmations=19,
    )
    assert polled.status is WithdrawalStatus.SUBMITTED


def test_verifying_a_withdrawal_with_no_net_amount_is_refused(
    session: Session, manual_backend: ManualTronBackend
) -> None:
    """`amount_net` is what the transfer is checked against. Without it there
    is no amount to compare, and "verified" would mean nothing."""
    withdrawal = make_hold(session, user="noamount")
    verification = manual_backend.verify_broadcast(withdrawal, "d" * 64)
    assert not verification.ok
    assert "no net amount" in (verification.reason or "")
    assert verification.detail == verification.reason
