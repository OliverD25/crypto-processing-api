"""Turns approved withdrawals into BTCPay payouts.

The whole worker exists to make one window as small and as recoverable as
possible: between "BTCPay created the payout" and "we wrote that down". A
crash in there leaves a row in `submitting` with no backend reference, and the
coins may or may not be on their way.

Two rules follow from that:

- **the BTCPay call is never retried** — a retry on an ambiguous timeout
  creates a second payout, which is a second payment
- **a stuck row is never blindly resubmitted** — resolution asks BTCPay which
  payout carries this withdrawal's id, and refuses to act at all if there is an
  unclaimed payout to the same destination
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings
from crypto_processing_api.core.amounts import to_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway
from crypto_processing_api.ledger.models import Withdrawal, WithdrawalStatus
from crypto_processing_api.services import fees, withdrawals
from crypto_processing_api.services.backends import BackendPayout, BtcpayPayoutBackend

logger = get_logger(__name__)


@dataclass
class SubmitReport:
    submitted: int = 0
    failed: int = 0
    skipped: int = 0
    adopted: int = 0
    frozen: int = 0
    resubmittable: int = 0


def submit_approved(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    limit: int = 20,
) -> SubmitReport:
    report = SubmitReport()

    with session_factory() as session:
        candidates = withdrawals.due_for_submission(session, limit=limit)

    for withdrawal_id in candidates:
        with session_factory() as session:
            # The compare-and-swap is the anti-double-submit gate: two workers
            # racing the same row produce exactly one winner, and the loser
            # sees rowcount 0.
            if not withdrawals.claim_for_submission(session, withdrawal_id):
                session.commit()
                report.skipped += 1
                continue
            session.commit()

        with session_factory() as session:
            withdrawal = withdrawals.get(session, withdrawal_id)
            asset = withdrawals.get_asset(session, withdrawal.asset_id, require_enabled=False)
            payment_method = asset.btcpay_payment_method
            decimals = asset.decimals
            gross = withdrawal.amount_gross

        try:
            # Re-priced here, not at request time: the fee is fixed at
            # submission, and between the two the mempool may have moved.
            quote = fees.quote_btc_fee(
                gateway, settings, gross=gross, payment_method_id=payment_method
            )
        except fees.DustAmount as exc:
            with session_factory() as session:
                withdrawal = withdrawals.lock(session, withdrawal_id)
                withdrawal.failure_reason = f"fee re-check failed: {exc}"
                withdrawals.release_hold(session, withdrawal, actor="auto")
                session.commit()
            report.failed += 1
            logger.warning("submit.dust_at_submission", withdrawal_id=str(withdrawal_id))
            continue

        backend = BtcpayPayoutBackend(gateway, payout_method_id=payment_method)
        try:
            with session_factory() as session:
                withdrawal = withdrawals.get(session, withdrawal_id)
                payout = backend.initiate(withdrawal, net=quote.net, decimals=decimals)
        except BTCPayError as exc:
            # Deliberately not retried and deliberately not failed: the payout
            # may exist. The row stays in `submitting` for resolution.
            logger.error(
                "submit.ambiguous",
                withdrawal_id=str(withdrawal_id),
                error=str(exc),
                detail="left in submitting for reconciliation",
            )
            report.failed += 1
            continue

        with session_factory() as session:
            withdrawal = withdrawals.lock(session, withdrawal_id)
            _record_submission(session, withdrawal, payout_id=payout.id, quote=quote)
            session.commit()
        report.submitted += 1
        logger.info(
            "submit.done",
            withdrawal_id=str(withdrawal_id),
            payout=payout.id,
            net=quote.net,
            fee=quote.fee,
            fee_source=quote.source,
        )

    return report


def _record_submission(
    session: Session,
    withdrawal: Withdrawal,
    *,
    payout_id: str,
    quote: fees.FeeQuote,
) -> None:
    """One transaction: the reference, the amounts, the status, the ledger entry."""
    withdrawal.backend_ref = payout_id
    withdrawal.fee_amount = quote.fee
    withdrawal.amount_net = quote.net
    withdrawal.submitted_at = datetime.now(UTC)
    withdrawals.post_submit_entry(session, withdrawal, quote)
    withdrawal.status = WithdrawalStatus.SUBMITTED
    withdrawal.updated_at = datetime.now(UTC)
    session.flush()


def resolve_stuck(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
) -> SubmitReport:
    """Decide what happened to submissions whose outcome was never written down.

    Three outcomes, in order of how much is known:

    1. BTCPay has a payout carrying this withdrawal's id — adopt it. This is
       the case the metadata echo makes unambiguous.
    2. No payout for this withdrawal and no unclaimed payout to the same
       destination — nothing was created, so the row goes back to `approved`.
    3. Anything else — freeze. An unclaimed payout to this destination might be
       ours, and resubmitting on a maybe is how a withdrawal gets paid twice.
    """
    report = SubmitReport()

    with session_factory() as session:
        stuck = withdrawals.stuck_submitting(
            session, older_than_seconds=settings.stuck_submitting_seconds
        )

    for withdrawal_id in stuck:
        with session_factory() as session:
            withdrawal = withdrawals.get(session, withdrawal_id)
            asset = withdrawals.get_asset(session, withdrawal.asset_id, require_enabled=False)
            backend = BtcpayPayoutBackend(gateway, payout_method_id=asset.btcpay_payment_method)
            try:
                mine, unclaimed = backend.find_for_withdrawal(withdrawal)
            except BTCPayError as exc:
                logger.warning(
                    "stuck.lookup_failed", withdrawal_id=str(withdrawal_id), error=str(exc)
                )
                continue

            if mine is not None:
                quote = _quote_from_payout(mine, withdrawal, asset.decimals)
                withdrawal = withdrawals.lock(session, withdrawal_id)
                _record_submission(session, withdrawal, payout_id=mine.id, quote=quote)
                session.commit()
                report.adopted += 1
                logger.info("stuck.adopted", withdrawal_id=str(withdrawal_id), payout=mine.id)
                continue

            if unclaimed:
                withdrawal = withdrawals.lock(session, withdrawal_id)
                withdrawal.failure_reason = (
                    f"{len(unclaimed)} payout(s) to this destination carry no withdrawal id; "
                    "an admin must decide before this may be submitted again"
                )
                session.commit()
                report.frozen += 1
                logger.error(
                    "stuck.frozen",
                    withdrawal_id=str(withdrawal_id),
                    unclaimed=[p.id for p in unclaimed],
                    detail="refusing to resubmit while an unclaimed payout could be this one",
                )
                continue

            withdrawal = withdrawals.lock(session, withdrawal_id)
            withdrawal.status = WithdrawalStatus.APPROVED
            withdrawal.failure_reason = None
            session.commit()
            report.resubmittable += 1
            logger.info(
                "stuck.no_payout_exists",
                withdrawal_id=str(withdrawal_id),
                detail="returned to approved for a clean submission",
            )

    return report


def _quote_from_payout(
    payout: BackendPayout, withdrawal: Withdrawal, decimals: int
) -> fees.FeeQuote:
    """Rebuild the amounts from what BTCPay actually accepted.

    The payout is the fact. Re-estimating here would book a fee the user was
    never charged.
    """
    amount = payout.amount
    net = to_units(str(amount), decimals) if amount else withdrawal.amount_gross
    fee = max(withdrawal.amount_gross - net, 0)
    return fees.FeeQuote(
        fee=fee, net=net, wallet_fee=fee, sat_per_vb=0.0, source="adopted_from_payout"
    )
