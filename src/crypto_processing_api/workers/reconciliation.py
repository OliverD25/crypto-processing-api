"""Reconciliation. This is the correctness mechanism, not a safety net.

Webhooks only reduce latency: BTCPay gives up after roughly eight redeliveries
in an hour, so an API that is down longer than that would lose deposits
forever if webhooks were the truth path. These jobs ask BTCPay what actually
happened and feed the answer through the same `apply_invoice_state` the
webhook handler uses.

Job A here is deposits. Job B (withdrawals) arrives with M3, Job C (invariants)
with M5.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings
from crypto_processing_api.core.amounts import AmountError, to_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway, BTCPayNotFound
from crypto_processing_api.ledger.models import Asset, Deposit, DepositPayment, WalletTxoAlert
from crypto_processing_api.services import deposits as deposit_service

logger = get_logger(__name__)

#: Only on-chain payment methods expose a wallet API. The USDt plugin does not,
#: so the unattributed-receive detector covers BTC only. That gap is real and
#: is documented in the USDT attribution runbook in M4.
ONCHAIN_SUFFIX = "-CHAIN"


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
                result = deposit_service.refresh_deposit(session, gateway, deposit_id=deposit_id)
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
        assets = list(
            session.execute(
                select(Asset).where(
                    Asset.enabled.is_(True),
                    Asset.btcpay_payment_method.endswith(ONCHAIN_SUFFIX),
                )
            ).scalars()
        )
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
