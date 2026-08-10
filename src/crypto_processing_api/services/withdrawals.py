"""Withdrawals: holds, velocity gates, and the payout state machine.

## The posting matrix

`G` gross (debited from the user), `f` fee charged to the user, `net = G - f`
sent on-chain, `w` miner fee this wallet expects to pay, `C = net + w` the
custody committed to the payout.

    hold       withdrawal_hold      DR user_available +G   CR user_hold -G
    submit     withdrawal_submit    DR payouts_in_flight +C   CR hot_wallet -C
    settle     withdrawal_settle    DR user_hold +G
                                    DR network_fee_expense +w
                                    CR fee_income -f
                                    CR payouts_in_flight -C
    un-submit  reversal             DR hot_wallet +C   CR payouts_in_flight -C
    release    withdrawal_release   DR user_hold +G   CR user_available -G

Settle sums to `G + w - f - C = G - f - net = 0` under `deduct`, and to
`G + w - 0 - (G + w) = 0` under `absorb`, where `f = 0` and `net = G`.

The submit entry is why the insolvency check can be exact. Between broadcast
and confirmation the coins have really left the wallet; without it the ledger
would still count them as `hot_wallet` and "ledger versus live wallet" would
diverge by a growing amount that has to be tolerated rather than explained.

## Release legality

Releasing a hold after a transaction may exist is a double-pay path: the user
gets their balance back and the original transaction confirms anyway. So
automatic release happens only from `requested`, `pending_approval`, `approved`
and from `submitting` once reconciliation has proved no payout exists. From
`submitted` onward it takes an admin, an attestation recorded on the row, and
never an automatic mapping from a backend status.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings
from crypto_processing_api.core.addresses import (
    AddressError,
    validate_bitcoin_address,
    validate_tron_address,
)
from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.ids import uuid7
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_models import Payout
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Deposit,
    EntryKind,
    JournalEntry,
    Posting,
    Withdrawal,
    WithdrawalStatus,
)
from crypto_processing_api.services import events
from crypto_processing_api.services.backends import BACKEND_MANUAL_TRON as BACKEND_MANUAL_TRON
from crypto_processing_api.services.backends import ManualTronBackend
from crypto_processing_api.services.fees import FeeQuote

logger = get_logger(__name__)

AUTO_ACTOR = "auto"
BACKEND_BTCPAY = "btcpay_payout"

VELOCITY_WINDOW = timedelta(hours=24)

#: Legal status transitions. `submitting -> approved` exists for exactly one
#: case: reconciliation proved BTCPay never created a payout, so the row may be
#: submitted again.
_ALLOWED: dict[WithdrawalStatus, frozenset[WithdrawalStatus]] = {
    WithdrawalStatus.REQUESTED: frozenset(
        {
            WithdrawalStatus.PENDING_APPROVAL,
            WithdrawalStatus.APPROVED,
            WithdrawalStatus.REJECTED,
        }
    ),
    WithdrawalStatus.PENDING_APPROVAL: frozenset(
        {WithdrawalStatus.APPROVED, WithdrawalStatus.REJECTED}
    ),
    WithdrawalStatus.APPROVED: frozenset({WithdrawalStatus.SUBMITTING, WithdrawalStatus.REJECTED}),
    WithdrawalStatus.SUBMITTING: frozenset(
        {
            WithdrawalStatus.SUBMITTED,
            WithdrawalStatus.APPROVED,
            WithdrawalStatus.FAILED,
        }
    ),
    WithdrawalStatus.SUBMITTED: frozenset(
        {
            WithdrawalStatus.BROADCAST,
            WithdrawalStatus.CONFIRMED,
            WithdrawalStatus.FAILED,
        }
    ),
    WithdrawalStatus.BROADCAST: frozenset({WithdrawalStatus.CONFIRMED, WithdrawalStatus.FAILED}),
    WithdrawalStatus.CONFIRMED: frozenset(),
    WithdrawalStatus.REJECTED: frozenset({WithdrawalStatus.REFUNDED}),
    WithdrawalStatus.FAILED: frozenset({WithdrawalStatus.REFUNDED}),
    WithdrawalStatus.REFUNDED: frozenset(),
}

#: Releasing from here cannot double-pay: no payout has been created yet.
AUTO_RELEASABLE: frozenset[WithdrawalStatus] = frozenset(
    {
        WithdrawalStatus.REQUESTED,
        WithdrawalStatus.PENDING_APPROVAL,
        WithdrawalStatus.APPROVED,
        WithdrawalStatus.REJECTED,
    }
)

#: A transaction may exist. Only an admin with an attestation may release.
ATTESTED_RELEASABLE: frozenset[WithdrawalStatus] = frozenset(
    {
        WithdrawalStatus.SUBMITTING,
        WithdrawalStatus.SUBMITTED,
        WithdrawalStatus.BROADCAST,
        WithdrawalStatus.FAILED,
    }
)

#: Statuses whose gross still counts against the rolling velocity cap. A
#: released hold no longer reserves anything, so it stops counting.
COUNTS_TOWARD_CAP: frozenset[WithdrawalStatus] = frozenset(
    {
        WithdrawalStatus.REQUESTED,
        WithdrawalStatus.PENDING_APPROVAL,
        WithdrawalStatus.APPROVED,
        WithdrawalStatus.SUBMITTING,
        WithdrawalStatus.SUBMITTED,
        WithdrawalStatus.BROADCAST,
        WithdrawalStatus.CONFIRMED,
    }
)

_PAYOUT_STATE_MAP: dict[str, WithdrawalStatus] = {
    "AwaitingApproval": WithdrawalStatus.SUBMITTED,
    "AwaitingPayment": WithdrawalStatus.SUBMITTED,
    "InProgress": WithdrawalStatus.BROADCAST,
    "Completed": WithdrawalStatus.CONFIRMED,
    "Cancelled": WithdrawalStatus.FAILED,
}


class WithdrawalError(Exception):
    """Base class."""


class WithdrawalNotFound(WithdrawalError):
    pass


class InvalidDestination(WithdrawalError):
    pass


class AmountTooSmall(WithdrawalError):
    pass


class InsufficientBalance(WithdrawalError):
    pass


class IllegalTransition(WithdrawalError):
    pass


class ReleaseNotPermitted(WithdrawalError):
    """This status needs an admin attestation, not an automatic release."""


@dataclass
class HoldOutcome:
    withdrawal: Withdrawal
    auto_approved: bool
    reason: str | None = None


def get_asset(session: Session, asset_id: str, *, require_enabled: bool = True) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise WithdrawalError(f"no such asset {asset_id!r}")
    if require_enabled and not asset.enabled:
        raise WithdrawalError(f"asset {asset_id} is not currently available")
    return asset


def validate_destination(
    session: Session, settings: Settings, *, asset_id: str, address: str
) -> None:
    """Reject at request time, before any balance is reserved.

    Rejecting our own deposit addresses is not paranoia: paying one burns a
    miner fee to move money between our own pockets, and paying an *expired*
    one lands it somewhere BTCPay no longer attributes to any invoice.
    """
    try:
        if asset_id == "BTC":
            validate_bitcoin_address(address, network=settings.bitcoin_network)
        elif asset_id == "USDT_TRC20":
            validate_tron_address(address)
            if settings.tron_hot_wallet_address == address:
                raise InvalidDestination("that address is this service's own TRON hot wallet")
        else:
            raise InvalidDestination(f"withdrawals for {asset_id} are not available")
    except AddressError as exc:
        raise InvalidDestination(str(exc)) from exc

    own = session.execute(select(Deposit.id).where(Deposit.address == address).limit(1)).first()
    if own is not None:
        raise InvalidDestination("that address is one of this service's own deposit addresses")


def velocity_sum(
    session: Session,
    *,
    asset_id: str,
    external_user_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Gross withdrawn in the rolling 24h window.

    Summed from the table rather than kept in a counter: a counter drifts, and
    the one number that bounds the hot wallet should not be able to drift.
    """
    since = (now or datetime.now(UTC)) - VELOCITY_WINDOW
    query = select(func.coalesce(func.sum(Withdrawal.amount_gross), 0)).where(
        Withdrawal.asset_id == asset_id,
        Withdrawal.created_at >= since,
        Withdrawal.status.in_(tuple(COUNTS_TOWARD_CAP)),
    )
    if external_user_id is not None:
        query = query.where(Withdrawal.external_user_id == external_user_id)
    return int(session.execute(query).scalar_one())


def place_hold(
    session: Session,
    *,
    external_user_id: str,
    asset_id: str,
    amount_gross: int,
    destination_address: str,
    backend: str = BACKEND_BTCPAY,
    force_manual: bool = False,
    now: datetime | None = None,
) -> HoldOutcome:
    """Reserve the funds and decide the approval route, atomically.

    Lock order is deliberate and load-bearing:

    1. the asset's `hot_wallet` row, via `lock_asset_gate`
    2. the user's `user_available` and `user_hold` rows, ascending id

    Step 1 is what makes the per-asset velocity cap mean anything. The cap is
    the control that bounds a drained-API-key burst, and summing the window
    without serializing on a single row lets N concurrent withdrawals across N
    different users each read the pre-cap total and all pass. System accounts
    are created at seed time, so the hot_wallet row always has a lower id than
    any user account and this order agrees with `post_entry`'s ascending lock.

    Does not commit.
    """
    now = now or datetime.now(UTC)
    asset = get_asset(session, asset_id)

    ledger.lock_asset_gate(session, asset_id)

    available_account, hold_account = ledger.lock_user_accounts(
        session, asset_id=asset_id, external_user_id=external_user_id
    )
    available = -available_account.balance
    if amount_gross < asset.withdrawal_min:
        raise AmountTooSmall(f"minimum withdrawal for {asset_id} is {asset.withdrawal_min} units")
    if available < amount_gross:
        raise InsufficientBalance(
            f"available balance {available} is less than the requested {amount_gross}"
        )

    reasons: list[str] = []
    manual = force_manual
    if force_manual:
        reasons.append("asset requires manual approval")
    if amount_gross > asset.withdrawal_auto_limit:
        manual = True
        reasons.append("above the per-withdrawal auto-approval limit")

    asset_window = velocity_sum(session, asset_id=asset_id, now=now)
    if asset_window + amount_gross > asset.withdrawal_daily_cap:
        manual = True
        reasons.append("would exceed the rolling 24h cap for this asset")

    if asset.withdrawal_user_daily_cap is not None:
        user_window = velocity_sum(
            session, asset_id=asset_id, external_user_id=external_user_id, now=now
        )
        if user_window + amount_gross > asset.withdrawal_user_daily_cap:
            manual = True
            reasons.append("would exceed this user's rolling 24h cap")

    withdrawal = Withdrawal(
        id=uuid7(),
        external_user_id=external_user_id,
        asset_id=asset_id,
        destination_address=destination_address,
        amount_gross=amount_gross,
        status=WithdrawalStatus.PENDING_APPROVAL if manual else WithdrawalStatus.APPROVED,
        approval_mode="manual" if manual else "auto",
        approved_by=None if manual else AUTO_ACTOR,
        backend=backend,
    )
    session.add(withdrawal)
    session.flush()

    entry = ledger.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_HOLD,
        asset_id=asset_id,
        source_ref=f"withdrawal_hold:{withdrawal.id}",
        postings=[(available_account.id, amount_gross), (hold_account.id, -amount_gross)],
        memo=f"withdrawal {withdrawal.id}",
    )
    withdrawal.hold_entry_id = entry.id
    session.flush()

    reason = "; ".join(reasons) if reasons else None
    if manual:
        events.emit(
            session,
            event_type=events.WITHDRAWAL_PENDING_APPROVAL,
            payload=_event_payload(withdrawal, asset, reason=reason),
        )
    logger.info(
        "withdrawal.held",
        withdrawal_id=str(withdrawal.id),
        amount=amount_gross,
        status=withdrawal.status.value,
        reason=reason,
    )
    return HoldOutcome(withdrawal=withdrawal, auto_approved=not manual, reason=reason)


def _event_payload(
    withdrawal: Withdrawal, asset: Asset, *, reason: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "withdrawal_id": str(withdrawal.id),
        "external_user_id": withdrawal.external_user_id,
        "asset": asset.id,
        "status": withdrawal.status.value,
        "amount_gross": from_units(withdrawal.amount_gross, asset.decimals),
        "amount_net": (
            from_units(withdrawal.amount_net, asset.decimals)
            if withdrawal.amount_net is not None
            else None
        ),
        "fee": (
            from_units(withdrawal.fee_amount, asset.decimals)
            if withdrawal.fee_amount is not None
            else None
        ),
        "destination_address": withdrawal.destination_address,
        "txid": withdrawal.txid,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _transition(withdrawal: Withdrawal, target: WithdrawalStatus) -> bool:
    if target == withdrawal.status:
        return False
    if target not in _ALLOWED[withdrawal.status]:
        raise IllegalTransition(
            f"withdrawal {withdrawal.id} cannot go {withdrawal.status.value} -> {target.value}"
        )
    logger.info(
        "withdrawal.transition",
        withdrawal_id=str(withdrawal.id),
        previous=withdrawal.status.value,
        status=target.value,
    )
    withdrawal.status = target
    withdrawal.updated_at = datetime.now(UTC)
    return True


def lock(session: Session, withdrawal_id: uuid.UUID) -> Withdrawal:
    withdrawal = session.execute(
        select(Withdrawal)
        .where(Withdrawal.id == withdrawal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if withdrawal is None:
        raise WithdrawalNotFound(f"no withdrawal {withdrawal_id}")
    return withdrawal


# -- ledger postings -------------------------------------------------------


def post_submit_entry(session: Session, withdrawal: Withdrawal, quote: FeeQuote) -> JournalEntry:
    """`DR payouts_in_flight +C / CR hot_wallet -C`."""
    hot = ledger.get_system_account(
        session, asset_id=withdrawal.asset_id, kind=AccountKind.HOT_WALLET
    )
    in_flight = ledger.get_system_account(
        session, asset_id=withdrawal.asset_id, kind=AccountKind.PAYOUTS_IN_FLIGHT
    )
    entry = ledger.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_SUBMIT,
        asset_id=withdrawal.asset_id,
        source_ref=f"withdrawal_submit:{withdrawal.id}",
        postings=[(in_flight.id, quote.committed), (hot.id, -quote.committed)],
        memo=f"withdrawal {withdrawal.id} committed to a payout",
    )
    withdrawal.submit_entry_id = entry.id
    return entry


def post_settle_entry(session: Session, withdrawal: Withdrawal) -> JournalEntry:
    """`DR user_hold +G, DR network_fee_expense +w, CR fee_income -f, CR payouts_in_flight -C`."""
    if withdrawal.submit_entry_id is None:
        raise WithdrawalError(
            f"withdrawal {withdrawal.id} has no submit entry; settling would credit "
            "payouts_in_flight for money that was never committed"
        )
    asset_id = withdrawal.asset_id
    gross = withdrawal.amount_gross
    fee = withdrawal.fee_amount or 0
    net = withdrawal.amount_net if withdrawal.amount_net is not None else gross - fee
    # The committed amount comes off the submit entry, not off a fresh
    # estimate: the ledger has to unwind exactly what it booked. Under
    # `absorb` the user was charged nothing while the wallet still paid a
    # miner fee, and the difference shows up here.
    committed = _committed_from_entry(session, withdrawal.submit_entry_id)
    wallet_fee = committed - net

    _available, hold_account = ledger.get_user_accounts(
        session, asset_id=asset_id, external_user_id=withdrawal.external_user_id
    )
    in_flight = ledger.get_system_account(
        session, asset_id=asset_id, kind=AccountKind.PAYOUTS_IN_FLIGHT
    )
    expense = ledger.get_system_account(
        session, asset_id=asset_id, kind=AccountKind.NETWORK_FEE_EXPENSE
    )
    income = ledger.get_system_account(session, asset_id=asset_id, kind=AccountKind.FEE_INCOME)

    postings: list[tuple[int, int]] = [
        (hold_account.id, gross),
        (in_flight.id, -committed),
    ]
    if wallet_fee:
        postings.append((expense.id, wallet_fee))
    if fee:
        postings.append((income.id, -fee))

    entry = ledger.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_SETTLE,
        asset_id=asset_id,
        source_ref=f"withdrawal_settle:{withdrawal.id}",
        postings=postings,
        memo=f"withdrawal {withdrawal.id} confirmed",
    )
    withdrawal.settle_entry_id = entry.id
    return entry


def _committed_from_entry(session: Session, entry_id: int) -> int:
    """Read the committed amount back off the submit entry."""
    amount = session.execute(
        select(Posting.amount)
        .join(Account, Account.id == Posting.account_id)
        .where(Posting.entry_id == entry_id, Account.kind == AccountKind.PAYOUTS_IN_FLIGHT)
    ).scalar_one()
    return int(amount)


def post_unsubmit_entry(session: Session, withdrawal: Withdrawal) -> JournalEntry | None:
    """Reverse the in-flight commitment when the payout will never be paid."""
    if withdrawal.submit_entry_id is None:
        return None
    committed = _committed_from_entry(session, withdrawal.submit_entry_id)
    hot = ledger.get_system_account(
        session, asset_id=withdrawal.asset_id, kind=AccountKind.HOT_WALLET
    )
    in_flight = ledger.get_system_account(
        session, asset_id=withdrawal.asset_id, kind=AccountKind.PAYOUTS_IN_FLIGHT
    )
    return ledger.post_entry(
        session,
        kind=EntryKind.REVERSAL,
        asset_id=withdrawal.asset_id,
        source_ref=f"withdrawal_submit_reversal:{withdrawal.id}",
        postings=[(hot.id, committed), (in_flight.id, -committed)],
        memo=f"withdrawal {withdrawal.id} payout will not be paid",
        reverses_entry_id=withdrawal.submit_entry_id,
    )


def post_release_entry(session: Session, withdrawal: Withdrawal) -> JournalEntry:
    """`DR user_hold +G / CR user_available -G`."""
    available, hold_account = ledger.get_user_accounts(
        session, asset_id=withdrawal.asset_id, external_user_id=withdrawal.external_user_id
    )
    entry = ledger.post_entry(
        session,
        kind=EntryKind.WITHDRAWAL_RELEASE,
        asset_id=withdrawal.asset_id,
        source_ref=f"withdrawal_release:{withdrawal.id}",
        postings=[
            (hold_account.id, withdrawal.amount_gross),
            (available.id, -withdrawal.amount_gross),
        ],
        memo=f"withdrawal {withdrawal.id} released",
    )
    withdrawal.release_entry_id = entry.id
    return entry


def release_hold(
    session: Session,
    withdrawal: Withdrawal,
    *,
    actor: str,
    attestation: str | None = None,
) -> None:
    """Return the held funds and mark the withdrawal refunded.

    From a status where a transaction may already exist this needs an
    attestation. The scenario it blocks: a payout lingers in the mempool, an
    operator (or a backend status map) calls it failed, the balance is
    restored, and then the original transaction confirms anyway. The user has
    both the coins and the balance, and the hot wallet is short the difference.
    """
    if withdrawal.status in ATTESTED_RELEASABLE:
        if not attestation or not attestation.strip():
            raise ReleaseNotPermitted(
                f"releasing a withdrawal in {withdrawal.status.value} requires an admin "
                "attestation that the transaction was verified never broadcast or "
                "double-spent"
            )
    elif withdrawal.status not in AUTO_RELEASABLE:
        raise ReleaseNotPermitted(f"a withdrawal in {withdrawal.status.value} cannot be released")

    if withdrawal.release_entry_id is not None:
        return

    post_unsubmit_entry(session, withdrawal)
    post_release_entry(session, withdrawal)

    # Route through the legal intermediate for where this row actually is, so
    # the matrix stays the single description of what may happen. Refusing a
    # queued withdrawal is a rejection; giving back a hold whose payout may
    # exist is a failure, and the two are not the same event.
    if withdrawal.status in (
        WithdrawalStatus.REQUESTED,
        WithdrawalStatus.PENDING_APPROVAL,
        WithdrawalStatus.APPROVED,
    ):
        _transition(withdrawal, WithdrawalStatus.REJECTED)
    elif withdrawal.status in (
        WithdrawalStatus.SUBMITTING,
        WithdrawalStatus.SUBMITTED,
        WithdrawalStatus.BROADCAST,
    ):
        _transition(withdrawal, WithdrawalStatus.FAILED)
    _transition(withdrawal, WithdrawalStatus.REFUNDED)

    withdrawal.released_by = actor
    if attestation:
        withdrawal.release_attestation = attestation.strip()
    session.flush()
    logger.info(
        "withdrawal.released",
        withdrawal_id=str(withdrawal.id),
        actor=actor,
        attested=bool(attestation),
    )


def _advance(withdrawal: Withdrawal, target: WithdrawalStatus) -> bool:
    """Move forward if the transition is legal, otherwise do nothing.

    Backend events arrive out of order. An `InProgress` that lands after a
    `Completed` must be a recorded no-op, not an exception and not a step
    backwards.
    """
    if target == withdrawal.status:
        return False
    if target not in _ALLOWED[withdrawal.status]:
        logger.info(
            "withdrawal.out_of_order",
            withdrawal_id=str(withdrawal.id),
            status=withdrawal.status.value,
            ignored_target=target.value,
        )
        return False
    return _transition(withdrawal, target)


def apply_payout_state(
    session: Session,
    *,
    withdrawal_id: uuid.UUID,
    payout: Payout,
    actor: str = AUTO_ACTOR,
) -> Withdrawal:
    """Bring a withdrawal in line with BTCPay's view of its payout.

    The single transition path, shared by the webhook handler and Job B, for
    the same reason `apply_invoice_state` is: two triggers of one function
    cannot disagree.

    A `Cancelled` payout moves the row to `failed` and stops. It does **not**
    release the hold, because from `submitted` onward a release could pay the
    user twice — that decision belongs to an admin with an attestation.
    """
    withdrawal = lock(session, withdrawal_id)
    asset = get_asset(session, withdrawal.asset_id, require_enabled=False)

    echoed = payout.metadata.get("withdrawal_id")
    if echoed is not None and str(echoed) != str(withdrawal.id):
        raise WithdrawalError(
            f"payout {payout.id} carries withdrawal_id {echoed}, not {withdrawal.id}"
        )
    if withdrawal.backend_ref and withdrawal.backend_ref != payout.id:
        raise WithdrawalError(
            f"withdrawal {withdrawal.id} is bound to payout {withdrawal.backend_ref}, "
            f"not {payout.id}"
        )

    if payout.txid and not withdrawal.txid:
        withdrawal.txid = payout.txid

    target = _PAYOUT_STATE_MAP.get(payout.state)
    if target is None:
        logger.warning("withdrawal.unknown_payout_state", state=payout.state)
        return withdrawal

    if target == WithdrawalStatus.CONFIRMED and withdrawal.settle_entry_id is None:
        post_settle_entry(session, withdrawal)

    changed = _advance(withdrawal, target)
    withdrawal.updated_at = datetime.now(UTC)
    session.flush()

    if changed:
        if withdrawal.status == WithdrawalStatus.BROADCAST:
            events.emit(
                session,
                event_type=events.WITHDRAWAL_BROADCAST,
                payload=_event_payload(withdrawal, asset),
            )
        elif withdrawal.status == WithdrawalStatus.CONFIRMED:
            events.emit(
                session,
                event_type=events.WITHDRAWAL_COMPLETED,
                payload=_event_payload(withdrawal, asset),
            )
        elif withdrawal.status == WithdrawalStatus.FAILED:
            withdrawal.failure_reason = withdrawal.failure_reason or "payout cancelled by BTCPay"
            events.emit(
                session,
                event_type=events.WITHDRAWAL_FAILED,
                payload=_event_payload(
                    withdrawal, asset, reason="payout cancelled; release needs an attestation"
                ),
            )
    return withdrawal


# -- admin actions ---------------------------------------------------------


def approve(session: Session, withdrawal_id: uuid.UUID, *, actor: str) -> Withdrawal:
    """Compare-and-swap out of the approval queue.

    Two admins clicking approve at the same moment must produce one approval,
    so the guard is the UPDATE itself rather than a read followed by a write.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.status == WithdrawalStatus.PENDING_APPROVAL,
            )
            .values(
                status=WithdrawalStatus.APPROVED,
                approved_by=actor,
                updated_at=datetime.now(UTC),
            )
        ),
    )
    if result.rowcount != 1:
        current = session.get(Withdrawal, withdrawal_id)
        if current is None:
            raise WithdrawalNotFound(f"no withdrawal {withdrawal_id}")
        raise IllegalTransition(
            f"withdrawal {withdrawal_id} is {current.status.value}, only pending_approval "
            "can be approved"
        )
    session.expire_all()
    withdrawal = lock(session, withdrawal_id)
    logger.info("withdrawal.approved", withdrawal_id=str(withdrawal_id), actor=actor)
    return withdrawal


def reject(
    session: Session, withdrawal_id: uuid.UUID, *, actor: str, reason: str | None = None
) -> Withdrawal:
    """Refuse a queued withdrawal and hand the money straight back.

    Legal only from `pending_approval`: nothing has been submitted, so the
    release cannot double-pay.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.status == WithdrawalStatus.PENDING_APPROVAL,
            )
            .values(
                status=WithdrawalStatus.REJECTED,
                rejected_by=actor,
                failure_reason=reason,
                updated_at=datetime.now(UTC),
            )
        ),
    )
    if result.rowcount != 1:
        current = session.get(Withdrawal, withdrawal_id)
        if current is None:
            raise WithdrawalNotFound(f"no withdrawal {withdrawal_id}")
        raise IllegalTransition(
            f"withdrawal {withdrawal_id} is {current.status.value}, only pending_approval "
            "can be rejected"
        )
    session.expire_all()
    withdrawal = lock(session, withdrawal_id)
    asset = get_asset(session, withdrawal.asset_id, require_enabled=False)
    release_hold(session, withdrawal, actor=actor)
    events.emit(
        session,
        event_type=events.WITHDRAWAL_FAILED,
        payload=_event_payload(withdrawal, asset, reason=reason or "rejected by an admin"),
    )
    return withdrawal


def admin_release(
    session: Session, withdrawal_id: uuid.UUID, *, actor: str, attestation: str
) -> Withdrawal:
    """Release a hold after a transaction may already exist.

    The attestation is stored verbatim on the row. It is the operator saying,
    on the record, that they checked the chain and the money is not going to
    arrive.
    """
    withdrawal = lock(session, withdrawal_id)
    asset = get_asset(session, withdrawal.asset_id, require_enabled=False)
    release_hold(session, withdrawal, actor=actor, attestation=attestation)
    events.emit(
        session,
        event_type=events.WITHDRAWAL_FAILED,
        payload=_event_payload(withdrawal, asset, reason="released by an admin with attestation"),
    )
    return withdrawal


# -- reads -----------------------------------------------------------------


def get(session: Session, withdrawal_id: uuid.UUID) -> Withdrawal:
    withdrawal = session.get(Withdrawal, withdrawal_id)
    if withdrawal is None:
        raise WithdrawalNotFound(f"no withdrawal {withdrawal_id}")
    return withdrawal


def list_for_user(
    session: Session, *, external_user_id: str, limit: int, cursor: uuid.UUID | None = None
) -> list[Withdrawal]:
    query = select(Withdrawal).where(Withdrawal.external_user_id == external_user_id)
    if cursor is not None:
        query = query.where(Withdrawal.id < cursor)
    return list(session.execute(query.order_by(Withdrawal.id.desc()).limit(limit)).scalars())


def list_by_status(
    session: Session, *, status: WithdrawalStatus | None = None, limit: int = 100
) -> list[Withdrawal]:
    query = select(Withdrawal)
    if status is not None:
        query = query.where(Withdrawal.status == status)
    return list(session.execute(query.order_by(Withdrawal.created_at).limit(limit)).scalars())


def claim_for_submission(session: Session, withdrawal_id: uuid.UUID) -> bool:
    """CAS `approved -> submitting`. Exactly one worker can win."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Withdrawal)
            .where(Withdrawal.id == withdrawal_id, Withdrawal.status == WithdrawalStatus.APPROVED)
            .values(status=WithdrawalStatus.SUBMITTING, updated_at=datetime.now(UTC))
        ),
    )
    return bool(result.rowcount == 1)


def due_for_submission(session: Session, *, limit: int = 20) -> list[uuid.UUID]:
    """Approved withdrawals the BTCPay payout submitter may claim.

    The backend filter is load-bearing. Without it a manual-TRON row is handed
    to the BTCPay submitter, which quotes a **BTC** fee against a USDT payment
    method and asks Greenfield to pay out a token it has no handler for. The
    payout is rejected, `resolve_stuck` finds nothing and returns the row to
    `approved`, and the whole cycle repeats every ten seconds.
    """
    rows = session.execute(
        select(Withdrawal.id)
        .where(
            Withdrawal.status == WithdrawalStatus.APPROVED,
            Withdrawal.backend == BACKEND_BTCPAY,
        )
        .order_by(Withdrawal.created_at)
        .limit(limit)
    ).scalars()
    return list(rows)


def due_for_polling(session: Session, *, limit: int = 100) -> list[uuid.UUID]:
    """Withdrawals Job B should re-check against Greenfield.

    Manual-TRON rows are excluded: their `backend_ref` is `manual:<uuid>`, not
    a BTCPay payout id, so polling one asks Greenfield about a payout that
    cannot exist and logs an error on every sweep. Their own sweep is
    `sweep_manual_withdrawals`, which asks TronGrid instead.
    """
    rows = session.execute(
        select(Withdrawal.id)
        .where(
            Withdrawal.status.in_(
                (
                    WithdrawalStatus.SUBMITTED,
                    WithdrawalStatus.BROADCAST,
                )
            ),
            Withdrawal.backend == BACKEND_BTCPAY,
            Withdrawal.backend_ref.isnot(None),
        )
        .order_by(Withdrawal.updated_at)
        .limit(limit)
    ).scalars()
    return list(rows)


def stuck_submitting(
    session: Session, *, older_than_seconds: int, limit: int = 20
) -> list[uuid.UUID]:
    """Submissions whose outcome was never written down, for `resolve_stuck`.

    The backend filter completes the pair added in v0.1.1. `resolve_stuck`
    answers one question — "did BTCPay create a payout for this row?" — and it
    is a meaningless question for a backend BTCPay cannot pay. Asked anyway, it
    gets the most dangerous available answer: no payout carries this id, no
    unclaimed payout matches this TRON destination, therefore nothing was
    created, therefore return the row to `approved`. There it stops, because
    `due_for_submission` correctly refuses to claim it, and `approved` is a
    status that looks entirely normal on an admin screen.

    Fresh rows cannot reach this state — `submit_manual` writes `backend_ref`
    before it transitions, and `usdt_auto_withdraw` is refused at startup. The
    rows this protects are legacy ones from a v0.1.0 deployment, and for those
    `submitting` is the better resting place: it is visibly wrong, so it gets
    the operator's attention and the remediation in
    `docs/runbook-usdt-withdrawals.md`.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    rows = session.execute(
        select(Withdrawal.id)
        .where(
            Withdrawal.status == WithdrawalStatus.SUBMITTING,
            Withdrawal.backend == BACKEND_BTCPAY,
            Withdrawal.backend_ref.is_(None),
            Withdrawal.updated_at < cutoff,
        )
        .order_by(Withdrawal.updated_at)
        .limit(limit)
    ).scalars()
    return list(rows)


def held_balance(session: Session, *, asset_id: str, external_user_id: str) -> int:
    account = session.execute(
        select(Account).where(
            Account.asset_id == asset_id,
            Account.kind == AccountKind.USER_HOLD,
            Account.external_user_id == external_user_id,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


class TronVerificationFailed(WithdrawalError):
    """The pasted transaction id is not the transfer it was claimed to be."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TxidAlreadyUsed(WithdrawalError):
    """That transaction id already settles another withdrawal."""


def submit_manual(session: Session, withdrawal: Withdrawal, *, quote: FeeQuote) -> Withdrawal:
    """Move an approved manual withdrawal to `submitted`.

    Nothing is sent here. The operator now has a task: send the net amount from
    the TRON hot wallet and come back with the transaction id. The submit entry
    is posted anyway, because the money is committed the moment the operator is
    told to send it — leaving it uncommitted would let the same balance be
    promised twice.

    In-flight accounting for USDT commits **net only**. TRC-20 gas is paid in
    TRX, not in USDT, so no USDT beyond the transfer itself leaves the wallet;
    the flat fee the user paid is service revenue, not a token cost.
    """
    if withdrawal.status != WithdrawalStatus.APPROVED:
        raise IllegalTransition(
            f"withdrawal {withdrawal.id} is {withdrawal.status.value}, only approved "
            "withdrawals can be handed to an operator"
        )
    withdrawal.backend_ref = ManualTronBackend.new_reference()
    withdrawal.fee_amount = quote.fee
    withdrawal.amount_net = quote.net
    withdrawal.submitted_at = datetime.now(UTC)
    post_submit_entry(session, withdrawal, quote)
    _transition(withdrawal, WithdrawalStatus.SUBMITTING)
    _transition(withdrawal, WithdrawalStatus.SUBMITTED)
    session.flush()
    logger.info(
        "withdrawal.manual_submitted",
        withdrawal_id=str(withdrawal.id),
        backend_ref=withdrawal.backend_ref,
        net=quote.net,
    )
    return withdrawal


def mark_broadcast(
    session: Session,
    backend: ManualTronBackend,
    *,
    withdrawal_id: uuid.UUID,
    txid: str,
    actor: str,
) -> Withdrawal:
    """Record the operator's transaction id, after checking it is real.

    The check is the whole point. "Included and successful" would accept a
    previous withdrawal's txid or the TRX top-up that funded the gas — both are
    real, healthy transactions that moved none of this user's USDT.
    """
    withdrawal = lock(session, withdrawal_id)
    if withdrawal.backend != BACKEND_MANUAL_TRON:
        raise IllegalTransition(
            f"withdrawal {withdrawal.id} uses the {withdrawal.backend} backend; "
            "mark-broadcast is for operator-sent withdrawals"
        )
    if withdrawal.status != WithdrawalStatus.SUBMITTED:
        raise IllegalTransition(
            f"withdrawal {withdrawal.id} is {withdrawal.status.value}, only submitted "
            "withdrawals can be marked broadcast"
        )

    candidate = txid.strip().lower().removeprefix("0x")
    if not candidate:
        raise TronVerificationFailed("no transaction id was given")

    duplicate = session.execute(
        select(Withdrawal.id).where(Withdrawal.txid == candidate, Withdrawal.id != withdrawal.id)
    ).first()
    if duplicate is not None:
        raise TxidAlreadyUsed(
            f"transaction {candidate} is already recorded against withdrawal {duplicate[0]}"
        )

    verification = backend.verify_broadcast(withdrawal, candidate)
    if not verification.ok:
        logger.error(
            "withdrawal.tron_verification_failed",
            withdrawal_id=str(withdrawal.id),
            reason=verification.reason,
            actor=actor,
        )
        raise TronVerificationFailed(verification.reason or "verification failed")

    asset = get_asset(session, withdrawal.asset_id, require_enabled=False)
    withdrawal.txid = candidate
    _transition(withdrawal, WithdrawalStatus.BROADCAST)
    session.flush()
    events.emit(
        session,
        event_type=events.WITHDRAWAL_BROADCAST,
        payload=_event_payload(withdrawal, asset),
    )
    logger.info(
        "withdrawal.marked_broadcast",
        withdrawal_id=str(withdrawal.id),
        actor=actor,
        block=verification.block_number,
    )
    return withdrawal


def poll_manual(
    session: Session,
    backend: ManualTronBackend,
    *,
    withdrawal_id: uuid.UUID,
    required_confirmations: int,
) -> Withdrawal:
    """Confirm a manual withdrawal once its transaction is deep enough.

    Re-runs the full tuple check on every poll rather than trusting the earlier
    pass. A reorg that replaced the transaction has to be able to un-verify it,
    and re-checking costs one request.
    """
    withdrawal = lock(session, withdrawal_id)
    if withdrawal.status != WithdrawalStatus.BROADCAST or not withdrawal.txid:
        return withdrawal

    verification = backend.verify_broadcast(withdrawal, withdrawal.txid)
    if not verification.ok:
        # Do not release, do not fail silently: the money may well have moved.
        logger.error(
            "withdrawal.tron_reverification_failed",
            withdrawal_id=str(withdrawal.id),
            reason=verification.reason,
        )
        withdrawal.failure_reason = f"re-verification failed: {verification.reason}"
        session.flush()
        return withdrawal

    confirmations = backend.verifier.confirmations(verification.block_number)
    if confirmations < required_confirmations:
        logger.info(
            "withdrawal.awaiting_confirmations",
            withdrawal_id=str(withdrawal.id),
            confirmations=confirmations,
            required=required_confirmations,
        )
        return withdrawal

    asset = get_asset(session, withdrawal.asset_id, require_enabled=False)
    if withdrawal.settle_entry_id is None:
        post_settle_entry(session, withdrawal)
    _transition(withdrawal, WithdrawalStatus.CONFIRMED)
    withdrawal.failure_reason = None
    session.flush()
    events.emit(
        session,
        event_type=events.WITHDRAWAL_COMPLETED,
        payload=_event_payload(withdrawal, asset),
    )
    logger.info(
        "withdrawal.manual_confirmed",
        withdrawal_id=str(withdrawal.id),
        confirmations=confirmations,
    )
    return withdrawal


def due_for_manual_polling(session: Session, *, limit: int = 100) -> list[uuid.UUID]:
    rows = session.execute(
        select(Withdrawal.id)
        .where(
            Withdrawal.backend == BACKEND_MANUAL_TRON,
            Withdrawal.status == WithdrawalStatus.BROADCAST,
            Withdrawal.txid.isnot(None),
        )
        .order_by(Withdrawal.updated_at)
        .limit(limit)
    ).scalars()
    return list(rows)
