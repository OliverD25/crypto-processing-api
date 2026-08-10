"""Reconciliation. This is the correctness mechanism, not a safety net.

Webhooks only reduce latency: BTCPay gives up after roughly eight redeliveries
in an hour, so an API that is down longer than that would lose deposits
forever if webhooks were the truth path. These jobs ask BTCPay what actually
happened and feed the answer through the same `apply_invoice_state` the
webhook handler uses.

Job A here is deposits, Job B withdrawals, Job C the invariant and custody
check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crypto_processing_api.alerts.notifier import AlertCode, Severity, notify
from crypto_processing_api.config import Settings
from crypto_processing_api.core.amounts import AmountError, to_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway, BTCPayNotFound
from crypto_processing_api.gateway.trongrid import TronGridError, TronGridGateway
from crypto_processing_api.ledger.invariants import (
    balance_drifts,
    custody_reports,
    unbalanced_entries,
)
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Deposit,
    DepositPayment,
    WalletTxoAlert,
)
from crypto_processing_api.services import asset_registry
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.backends import (
    BtcpayPayoutBackend,
    ManualTronBackend,
    TronTxVerifier,
)

logger = get_logger(__name__)

@dataclass
class SweepReport:
    checked: int = 0
    changed: int = 0
    credited_units: int = 0
    adopted: int = 0
    errors: int = 0


@dataclass
class OrphanReport:
    scanned: int = 0
    orphans: list[str] | None = None


@dataclass
class WalletScanReport:
    scanned: int = 0
    unmatched: int = 0
    new_alerts: int = 0


def sweep_deposits(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    limit: int = 100,
) -> SweepReport:
    """Job A. Re-ask BTCPay about every deposit that is still worth asking about."""
    report = SweepReport()
    window = timedelta(days=settings.reconcile_settled_window_days)

    with session_factory() as session:
        due = [
            d.id for d in deposit_service.due_for_sweep(session, settled_window=window, limit=limit)
        ]

    for deposit_id in due:
        with session_factory() as session:
            try:
                result = deposit_service.refresh_deposit(
                    session,
                    gateway,
                    deposit_id=deposit_id,
                    tolerance_pct=settings.usdt_amount_tolerance_pct,
                )
                session.commit()
            except BTCPayNotFound:
                session.rollback()
                report.errors += 1
                logger.warning("sweep.invoice_gone", deposit_id=str(deposit_id))
                continue
            except (BTCPayError, deposit_service.DepositError) as exc:
                session.rollback()
                report.errors += 1
                logger.warning("sweep.failed", deposit_id=str(deposit_id), error=str(exc))
                continue

            report.checked += 1
            if result.changed:
                report.changed += 1
                report.credited_units += result.credited_units
                # A poller-caught change means webhooks missed something. Worth
                # a metric: it is the early warning that ingress is broken.
                logger.info(
                    "sweep.caught_change",
                    deposit_id=str(deposit_id),
                    status=result.status.value,
                    credited=result.credited_units,
                )
                if result.credited_units:
                    record_sweep_miss(settings, what="deposit", identifier=str(deposit_id))
    return report


def adopt_stuck_creating(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    older_than_seconds: int = 120,
) -> SweepReport:
    """Resolve deposits whose invoice creation ended ambiguously.

    The row committed before the BTCPay call, so a timeout leaves it in
    `creating` with no invoice id. Either BTCPay made the invoice, in which
    case the deposit id in its metadata finds it, or it did not, in which case
    one is created now.
    """
    report = SweepReport()
    with session_factory() as session:
        stuck = [
            d.id
            for d in deposit_service.stuck_in_creating(
                session, older_than=timedelta(seconds=older_than_seconds)
            )
        ]

    for deposit_id in stuck:
        with session_factory() as session:
            deposit = session.get(Deposit, deposit_id)
            if deposit is None:
                continue
            try:
                deposit_service.ensure_invoice(
                    session, gateway, settings, deposit=deposit, adopt_first=True
                )
                session.commit()
            except (BTCPayError, deposit_service.DepositError) as exc:
                session.rollback()
                report.errors += 1
                logger.warning("adopt.failed", deposit_id=str(deposit_id), error=str(exc))
                continue
            report.adopted += 1
            logger.info("adopt.resolved", deposit_id=str(deposit_id))
    return report


def scan_for_orphan_invoices(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    lookback_days: int = 2,
    page_size: int = 100,
) -> OrphanReport:
    """Page through the store's invoices and flag ours that we have no row for.

    The case this catches is a database restored from a backup taken before an
    invoice was created: BTCPay knows about the deposit, we do not, and no
    webhook will ever mention it again.
    """
    start = int((datetime.now(UTC) - timedelta(days=lookback_days)).timestamp())
    orphans: list[str] = []
    scanned = 0
    skip = 0

    while True:
        invoices = gateway.list_invoices(start_date=start, skip=skip, take=page_size)
        if not invoices:
            break
        scanned += len(invoices)
        with session_factory() as session:
            for invoice in invoices:
                if not deposit_service.is_cpapi_invoice(invoice.metadata):
                    continue
                known = session.execute(
                    select(Deposit.id).where(Deposit.btcpay_invoice_id == invoice.id)
                ).scalar_one_or_none()
                if known is not None:
                    continue
                deposit_id = deposit_service.deposit_id_from_metadata(invoice.metadata)
                if deposit_id is not None and session.get(Deposit, deposit_id) is not None:
                    continue
                orphans.append(invoice.id)
                logger.error(
                    "reconcile.orphan_invoice",
                    invoice=invoice.id,
                    deposit_id=str(deposit_id) if deposit_id else None,
                    status=invoice.status,
                )
        if len(invoices) < page_size:
            break
        skip += page_size

    return OrphanReport(scanned=scanned, orphans=orphans)


def detect_unattributed_receives(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    limit: int = 100,
) -> WalletScanReport:
    """The only detector for coins sent to an address BTCPay stopped watching.

    BTCPay attributes payments to an invoice only inside its monitoring window.
    A user who reuses a week-old address deposits into the hot wallet with no
    invoice at all, so every invoice-shaped check is blind to it — and the
    aggregate solvency check reads the surplus as healthy.

    Greenfield reports wallet transactions, not individual outputs, so matching
    happens on the txid: an on-chain payment id is `<txid>-<vout>`.
    """
    report = WalletScanReport()

    with session_factory() as session:
        registry = asset_registry.get_registry()
        assets = [
            asset
            for asset in session.execute(select(Asset).where(Asset.enabled.is_(True))).scalars()
            # `has_btcpay_wallet`, not a `-CHAIN` suffix test. The suffix was a
            # guess about a string; this is a statement about whether BTCPay
            # exposes a wallet API for the method at all.
            if (profile := registry.get(asset.id)) is not None and profile.has_btcpay_wallet
        ]
        targets = [(a.id, a.btcpay_payment_method, a.decimals) for a in assets]

    for asset_id, payment_method, decimals in targets:
        try:
            transactions = gateway.get_wallet_transactions(payment_method, limit=limit)
        except BTCPayError as exc:
            logger.warning("wallet_scan.failed", asset=asset_id, error=str(exc))
            continue

        for transaction in transactions:
            txid = transaction.transaction_hash
            if not txid:
                continue
            try:
                amount = to_units(transaction.amount, decimals)
            except AmountError:
                # A negative amount is an outgoing transaction — a payout, or
                # change coming back. Only receives can be uncredited deposits.
                continue
            if amount <= 0:
                continue
            report.scanned += 1

            with session_factory() as session:
                matched = session.execute(
                    select(DepositPayment.id).where(
                        DepositPayment.btcpay_payment_id.startswith(txid)
                    )
                ).first()
                if matched is not None:
                    continue
                report.unmatched += 1
                inserted = session.execute(
                    pg_insert(WalletTxoAlert)
                    .values(
                        asset_id=asset_id,
                        txid=txid,
                        amount=amount,
                        confirmations=_as_int(transaction.confirmations),
                        note="wallet receive with no matching deposit payment",
                    )
                    .on_conflict_do_nothing(index_elements=["asset_id", "txid"])
                    .returning(WalletTxoAlert.id)
                ).scalar_one_or_none()
                session.commit()
                if inserted is not None:
                    report.new_alerts += 1
                    logger.error(
                        "reconcile.unattributed_receive",
                        asset=asset_id,
                        txid=txid,
                        amount=amount,
                    )
    return report


def _as_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WithdrawalSweepReport:
    checked: int = 0
    changed: int = 0
    errors: int = 0


def sweep_withdrawals(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    limit: int = 100,
) -> WithdrawalSweepReport:
    """Job B. Ask BTCPay about every payout that has not reached a terminal state.

    Payout webhooks are the fast path and this is the correctness path, exactly
    as with deposits: both call `apply_payout_state`, so a webhook racing a
    poll cannot produce a conflict.
    """
    report = WithdrawalSweepReport()

    with session_factory() as session:
        due = withdrawal_service.due_for_polling(session, limit=limit)

    for withdrawal_id in due:
        with session_factory() as session:
            withdrawal = withdrawal_service.get(session, withdrawal_id)
            asset = withdrawal_service.get_asset(
                session, withdrawal.asset_id, require_enabled=False
            )
            backend_ref = withdrawal.backend_ref
            if not backend_ref:
                continue
            backend = BtcpayPayoutBackend(gateway, payout_method_id=asset.btcpay_payment_method)
            previous = withdrawal.status
            try:
                payout = backend.poll_status(backend_ref)
                withdrawal_service.apply_payout_state(
                    session, withdrawal_id=withdrawal_id, payout=payout
                )
                session.commit()
            except (BTCPayError, withdrawal_service.WithdrawalError) as exc:
                session.rollback()
                report.errors += 1
                logger.warning(
                    "withdrawal_sweep.failed", withdrawal_id=str(withdrawal_id), error=str(exc)
                )
                continue

            report.checked += 1
            refreshed = withdrawal_service.get(session, withdrawal_id)
            if refreshed.status != previous:
                report.changed += 1
                logger.info(
                    "withdrawal_sweep.caught_change",
                    withdrawal_id=str(withdrawal_id),
                    previous=previous.value,
                    status=refreshed.status.value,
                )
    return report


def sweep_manual_withdrawals(
    session_factory: Callable[[], Session],
    tron: TronGridGateway,
    settings: Settings,
    *,
    limit: int = 100,
) -> WithdrawalSweepReport:
    """Job B for operator-sent USDT.

    Shares `post_settle_entry` and the same status matrix as the BTCPay path —
    only the source of truth differs, TronGrid instead of Greenfield. Forking
    the money code for a second asset is how two assets end up with two
    different definitions of "settled".
    """
    report = WithdrawalSweepReport()
    if not settings.tron_hot_wallet_address:
        return report

    backend = ManualTronBackend(
        TronTxVerifier(
            tron,
            contract_address=settings.usdt_contract,
            hot_wallet_address=settings.tron_hot_wallet_address,
        )
    )

    with session_factory() as session:
        due = withdrawal_service.due_for_manual_polling(session, limit=limit)

    for withdrawal_id in due:
        with session_factory() as session:
            previous = withdrawal_service.get(session, withdrawal_id).status
            try:
                withdrawal_service.poll_manual(
                    session,
                    backend,
                    withdrawal_id=withdrawal_id,
                    required_confirmations=settings.tron_confirmations,
                )
                session.commit()
            except (TronGridError, withdrawal_service.WithdrawalError) as exc:
                session.rollback()
                report.errors += 1
                logger.warning(
                    "manual_sweep.failed", withdrawal_id=str(withdrawal_id), error=str(exc)
                )
                continue
            report.checked += 1
            if withdrawal_service.get(session, withdrawal_id).status != previous:
                report.changed += 1
    return report


@dataclass
class CustodyLine:
    asset_id: str
    ledger_custody: int
    ledger_in_flight: int
    user_obligations: int
    chain_balance: int | None
    chain_source: str
    #: Derived, not tuned: what the on-chain balance is expected to be short by.
    expected_shortfall: int

    @property
    def difference(self) -> int | None:
        """Chain minus what the ledger says should be there. Negative is bad."""
        if self.chain_balance is None:
            return None
        return self.chain_balance - (self.ledger_custody - self.expected_shortfall)

    @property
    def insolvent(self) -> bool:
        """Less on chain than we owe users. The one signal that matters."""
        if self.chain_balance is None:
            return False
        return self.chain_balance < self.user_obligations


@dataclass
class InvariantReport:
    consistent: bool = True
    drifts: list[str] = field(default_factory=list)
    unbalanced_entries: list[str] = field(default_factory=list)
    custody: list[CustodyLine] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.consistent and not self.alerts


def _chain_balance(
    asset: Asset,
    gateway: BTCPayGateway,
    tron: TronGridGateway | None,
    settings: Settings,
) -> tuple[int | None, str]:
    """What the outside world says we hold.

    The source name carries the `_unavailable` suffix on failure, exactly as
    the if/elif chain this replaced did. It is reported through the admin
    reconciliation endpoint, and an operator reading "trongrid" when the call
    actually failed would take a missing number for a real one.
    """
    profile = asset_registry.get_registry().get(asset.id)
    if profile is None or profile.custody_source is None:
        return None, "no_source"

    source = profile.custody_source(
        asset_registry.RegistryContext(settings=settings, asset=asset, gateway=gateway, tron=tron)
    )
    if source is None:
        return None, "no_source"

    balance = source.balance()
    if balance is None:
        return None, f"{source.source_name}_unavailable"
    return balance, source.source_name


def check_invariants(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    tron: TronGridGateway | None = None,
) -> InvariantReport:
    """Job C. The check that catches real-world loss.

    Three layers, cheapest first:

    1. every journal entry sums to zero
    2. every materialized balance equals the sum of its postings
    3. custody: what the ledger says we hold against what the chain says

    Layer 3 needs a tolerance, and the tolerance is **derived**, not tuned.
    Between submission and confirmation the coins have left the wallet while
    `payouts_in_flight` still carries them, so the expected shortfall is
    exactly that account's balance. A hand-picked epsilon would have to be
    loosened as volume grew, which is the same as switching the alarm off.
    """
    report = InvariantReport()

    with session_factory() as session:
        for imbalance in unbalanced_entries(session):
            report.unbalanced_entries.append(
                f"entry {imbalance.entry_id} sums to {imbalance.total}"
            )
        for drift in balance_drifts(session):
            report.drifts.append(
                f"account {drift.account_id} ({drift.kind.value}) materialized "
                f"{drift.materialized} vs derived {drift.derived}"
            )
        report.consistent = not report.drifts and not report.unbalanced_entries

        custody_by_asset = {row.asset_id: row for row in custody_reports(session)}
        assets = list(session.execute(select(Asset)).scalars())

        for asset in assets:
            summary = custody_by_asset.get(asset.id)
            if summary is None:
                continue
            in_flight = session.execute(
                select(func.coalesce(func.sum(Account.balance), 0)).where(
                    Account.asset_id == asset.id,
                    Account.kind == AccountKind.PAYOUTS_IN_FLIGHT,
                )
            ).scalar_one()
            chain, source = _chain_balance(asset, gateway, tron, settings)
            report.custody.append(
                CustodyLine(
                    asset_id=asset.id,
                    ledger_custody=summary.custody,
                    ledger_in_flight=int(in_flight),
                    user_obligations=summary.user_obligations,
                    chain_balance=chain,
                    chain_source=source,
                    expected_shortfall=int(in_flight) + settings.custody_tolerance_units,
                )
            )

    if not report.consistent:
        detail = "; ".join(report.unbalanced_entries + report.drifts)[:800]
        report.alerts.append(detail)
        notify(
            Severity.CRITICAL,
            AlertCode.LEDGER_INVARIANT_FAILURE,
            f"the ledger does not hold together: {detail}",
            settings=settings,
        )

    for line in report.custody:
        if line.insolvent:
            message = (
                f"{line.asset_id}: the wallet holds {line.chain_balance} but users are "
                f"owed {line.user_obligations}"
            )
            report.alerts.append(message)
            notify(
                Severity.CRITICAL,
                AlertCode.CUSTODY_INSOLVENCY_SIGNAL,
                message,
                settings=settings,
                asset=line.asset_id,
            )
    return report


def record_sweep_miss(settings: Settings, *, what: str, identifier: str) -> None:
    """The poller caught something the webhook path should have.

    Not an error on its own — reconciliation is supposed to catch things. A
    rising count means ingress is broken and nobody noticed, which is the
    condition worth an alert.
    """
    notify(
        Severity.INFO,
        AlertCode.RECONCILIATION_SWEEP_CAUGHT_MISS,
        f"reconciliation credited a {what} that no webhook delivered",
        settings=settings,
        identifier=identifier,
    )
