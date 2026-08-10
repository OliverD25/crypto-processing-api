"""Deposit intents and the one transition function that owns deposit state.

`apply_invoice_state` is the only code that moves a deposit forward. The
webhook handler and the reconciliation poller are two triggers of that one
function, which is why a webhook racing a poll cannot produce a conflict: they
contend on the same `FOR UPDATE` on the deposit row and the loser finds the
work already done.

Two rules the rest of this module exists to enforce:

1. **A webhook payload is a trigger, never an amount source.** Every credit
   re-fetches the invoice and its payment methods from Greenfield and converts
   the decimal string itself.
2. **Nothing that reached custody is ever silently dropped.** A payment either
   auto-credits or is recorded uncredited against a deposit in `review`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings
from crypto_processing_api.core.amounts import AmountError, from_units, to_units
from crypto_processing_api.core.ids import uuid7
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway
from crypto_processing_api.gateway.btcpay_models import Invoice, InvoicePaymentMethod
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import (
    AccountKind,
    Asset,
    Deposit,
    DepositPayment,
    DepositStatus,
    EntryKind,
)
from crypto_processing_api.services import events

logger = get_logger(__name__)

CPAPI_VERSION = 1
SEARCH_TERM_PREFIX = "cpapi:"
AUTO_ACTOR = "auto"

#: Invoice currency per asset. Top-up invoices carry no amount, so this only
#: drives display and BTCPay's rate lookup; using the asset's own currency
#: keeps the rate at 1 and removes an exchange-rate dependency from the
#: deposit path entirely.
INVOICE_CURRENCY: dict[str, str] = {"BTC": "BTC", "USDT_TRC20": "USDT"}

#: Statuses a deposit can move to from each status. `review` is deliberately a
#: dead end for automation: once a human has to look at it, only an admin
#: resolve moves it. `settled -> review` stays open because a late second
#: payment on a settled top-up invoice needs a human, and the payments already
#: credited stay credited.
_ALLOWED_TARGETS: dict[DepositStatus, frozenset[DepositStatus]] = {
    DepositStatus.CREATING: frozenset(
        {
            DepositStatus.PENDING,
            DepositStatus.CONFIRMING,
            DepositStatus.SETTLED,
            DepositStatus.EXPIRED,
            DepositStatus.REVIEW,
            DepositStatus.FAILED,
        }
    ),
    DepositStatus.PENDING: frozenset(
        {
            DepositStatus.CONFIRMING,
            DepositStatus.SETTLED,
            DepositStatus.EXPIRED,
            DepositStatus.REVIEW,
        }
    ),
    DepositStatus.CONFIRMING: frozenset(
        {DepositStatus.SETTLED, DepositStatus.EXPIRED, DepositStatus.REVIEW}
    ),
    DepositStatus.SETTLED: frozenset({DepositStatus.REVIEW}),
    DepositStatus.EXPIRED: frozenset({DepositStatus.REVIEW, DepositStatus.SETTLED}),
    DepositStatus.REVIEW: frozenset(),
    DepositStatus.DISMISSED: frozenset(),
    DepositStatus.FAILED: frozenset(),
}

TERMINAL_STATUSES: frozenset[DepositStatus] = frozenset(
    {DepositStatus.DISMISSED, DepositStatus.FAILED}
)

#: Statuses the deposit sweep keeps polling.
ACTIVE_STATUSES: frozenset[DepositStatus] = frozenset(
    {
        DepositStatus.CREATING,
        DepositStatus.PENDING,
        DepositStatus.CONFIRMING,
        DepositStatus.REVIEW,
    }
)


class DepositError(Exception):
    """Base class for deposit-service failures."""


class UnknownAsset(DepositError):
    pass


class AssetUnavailable(DepositError):
    """The asset exists but is disabled — usually a payment method that is not configured."""


class DepositNotFound(DepositError):
    pass


class InvoiceMismatch(DepositError):
    """The invoice handed in does not belong to this deposit."""


class IllegalTransition(DepositError):
    pass


class PaymentNotFound(DepositError):
    pass


@dataclass
class ApplyResult:
    deposit_id: uuid.UUID
    previous_status: DepositStatus
    status: DepositStatus
    credited_units: int = 0
    credited_payments: list[str] = field(default_factory=list)
    recorded_payments: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.previous_status != self.status or self.credited_payments or self.recorded_payments
        )


def get_asset(session: Session, asset_id: str, *, require_enabled: bool = True) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise UnknownAsset(f"no such asset {asset_id!r}")
    if require_enabled and not asset.enabled:
        raise AssetUnavailable(f"asset {asset_id} is not currently available for deposits")
    return asset


def build_metadata(deposit: Deposit) -> dict[str, Any]:
    """The attribution backbone: BTCPay echoes this in every invoice webhook.

    `cpapi` lets us ignore invoices created by anything else touching the same
    store, and `deposit_id` is what reconciliation matches on when an invoice
    creation call times out ambiguously.
    """
    return {
        "cpapi": True,
        "cpapi_version": CPAPI_VERSION,
        "external_user_id": deposit.external_user_id,
        "deposit_id": str(deposit.id),
        "asset": deposit.asset_id,
    }


def is_cpapi_invoice(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    return bool(metadata.get("cpapi"))


def deposit_id_from_metadata(metadata: dict[str, Any] | None) -> uuid.UUID | None:
    if not metadata:
        return None
    raw = metadata.get("deposit_id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _expiry_minutes(settings: Settings, asset_id: str) -> int:
    if asset_id == "USDT_TRC20":
        return settings.deposit_invoice_expiry_min_usdt
    return settings.deposit_invoice_expiry_min_btc


def _timestamp(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=UTC)


def create_deposit(
    session: Session,
    *,
    external_user_id: str,
    asset_id: str,
    amount_expected: int | None = None,
) -> Deposit:
    """Insert the intent in status `creating`.

    The row commits before BTCPay is called. If that call times out with no
    answer, the row is the only evidence the request existed, and
    reconciliation finds the invoice by the deposit id in its metadata.
    """
    asset = get_asset(session, asset_id)
    deposit = Deposit(
        id=uuid7(),
        external_user_id=external_user_id,
        asset_id=asset.id,
        amount_expected=amount_expected,
        status=DepositStatus.CREATING,
    )
    session.add(deposit)
    session.flush()
    return deposit


def _payment_method_for(
    asset: Asset, methods: list[InvoicePaymentMethod]
) -> InvoicePaymentMethod | None:
    for method in methods:
        if method.payment_method_id == asset.btcpay_payment_method:
            return method
    return None


def attach_invoice(
    session: Session,
    *,
    deposit: Deposit,
    invoice: Invoice,
    payment_methods: list[InvoicePaymentMethod],
    asset: Asset,
) -> None:
    method = _payment_method_for(asset, payment_methods)
    deposit.btcpay_invoice_id = invoice.id
    deposit.checkout_link = invoice.checkout_link
    deposit.expires_at = _timestamp(invoice.expiration_time)
    deposit.monitoring_expires_at = _timestamp(invoice.monitoring_expiration)
    if method is not None:
        deposit.address = method.destination
        # For USDT the address comes from a shared pool and goes back into it,
        # so record who held it and for how long. Without this window, a late
        # payment cannot be attributed to a user at all.
        deposit.address_reserved_from = _timestamp(invoice.created_time) or datetime.now(UTC)
        deposit.address_reserved_until = _timestamp(invoice.monitoring_expiration) or _timestamp(
            invoice.expiration_time
        )
    if deposit.status == DepositStatus.CREATING:
        deposit.status = DepositStatus.PENDING
    deposit.updated_at = datetime.now(UTC)
    session.flush()


def find_invoice_for_deposit(gateway: BTCPayGateway, deposit: Deposit) -> Invoice | None:
    """Look for an invoice BTCPay may have created before an ambiguous timeout."""
    candidates = gateway.list_invoices(
        text_search=f"{SEARCH_TERM_PREFIX}{deposit.id}", skip=0, take=20
    )
    for invoice in candidates:
        if deposit_id_from_metadata(invoice.metadata) == deposit.id:
            return invoice
    return None


def ensure_invoice(
    session: Session,
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    deposit: Deposit,
    adopt_first: bool = False,
) -> Deposit:
    """Give this deposit an invoice, creating one only if there is not one already.

    `adopt_first` is set when the caller knows this is a retry — a reclaimed
    idempotency key, or the reconciliation sweep finding a stuck `creating`
    row. It searches BTCPay before creating, so an ambiguous timeout costs an
    extra GET rather than a second unwatched deposit address.
    """
    if deposit.btcpay_invoice_id:
        return deposit

    asset = get_asset(session, deposit.asset_id)

    invoice = find_invoice_for_deposit(gateway, deposit) if adopt_first else None
    if invoice is not None:
        logger.info("deposit.invoice_adopted", deposit_id=str(deposit.id), invoice=invoice.id)
    else:
        invoice = gateway.create_top_up_invoice(
            currency=INVOICE_CURRENCY.get(asset.id, asset.id),
            metadata=build_metadata(deposit),
            payment_methods=[asset.btcpay_payment_method],
            expiration_minutes=_expiry_minutes(settings, asset.id),
            monitoring_minutes=settings.deposit_monitoring_minutes,
            additional_search_terms=[f"{SEARCH_TERM_PREFIX}{deposit.id}"],
        )

    methods = gateway.get_invoice_payment_methods(invoice.id)
    attach_invoice(session, deposit=deposit, invoice=invoice, payment_methods=methods, asset=asset)
    return deposit


def mark_failed(session: Session, *, deposit: Deposit, reason: str) -> None:
    """Only for creation failures BTCPay reported definitively.

    An ambiguous timeout must leave the row in `creating` so reconciliation can
    adopt whatever was actually made.
    """
    if deposit.status != DepositStatus.CREATING:
        raise IllegalTransition(f"cannot fail a deposit in {deposit.status.value}")
    deposit.status = DepositStatus.FAILED
    deposit.updated_at = datetime.now(UTC)
    logger.warning("deposit.creation_failed", deposit_id=str(deposit.id), reason=reason)
    session.flush()


#: Assets whose deposit addresses come from a shared pool rather than being
#: derived per invoice. Attribution for these is heuristic, so an amount that
#: does not match what the user said they would send is worth a human's time.
POOLED_ASSETS: frozenset[str] = frozenset({"USDT_TRC20"})


def _breaches_usdt_tolerance(
    deposit: Deposit,
    asset: Asset,
    parsed: list[tuple[str, int, str]],
    *,
    tolerance_pct: float,
) -> bool:
    """Would auto-crediting this pooled-address payment be a guess?

    Only applies when the platform told us what to expect. A top-up with no
    expected amount has nothing to compare against, and the tolerance is not a
    way to refuse money — it routes to a human, who credits it after checking
    the reservation window.
    """
    if asset.id not in POOLED_ASSETS or deposit.amount_expected is None:
        return False
    if tolerance_pct <= 0:
        return False
    received = sum(amount for _payment_id, amount, status in parsed if status == "Settled")
    if received == 0:
        return False
    allowed = deposit.amount_expected * tolerance_pct / 100.0
    return abs(received - deposit.amount_expected) > allowed


def _target_status(invoice: Invoice, *, has_payments: bool) -> DepositStatus:
    """Map BTCPay's invoice state to ours.

    Everything ambiguous lands in `review` rather than being credited or
    dropped: `Marked` means a human clicked "settled" in the BTCPay UI,
    `PaidLate` means the money arrived after the window closed, and `Invalid`
    means BTCPay itself gave up on the invoice.
    """
    if invoice.status == "New":
        return DepositStatus.CONFIRMING if has_payments else DepositStatus.PENDING
    if invoice.status == "Processing":
        return DepositStatus.CONFIRMING
    if invoice.status == "Settled":
        if invoice.additional_status in ("Marked", "PaidLate"):
            return DepositStatus.REVIEW
        return DepositStatus.SETTLED
    if invoice.status == "Expired":
        if has_payments or invoice.additional_status in ("PaidLate", "PaidPartial", "PaidOver"):
            return DepositStatus.REVIEW
        return DepositStatus.EXPIRED
    return DepositStatus.REVIEW


def _credit_payment(
    session: Session,
    *,
    deposit: Deposit,
    asset: Asset,
    row: DepositPayment,
    invoice_id: str,
    actor: str,
) -> None:
    hot = ledger.get_system_account(session, asset_id=asset.id, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(
        session, asset_id=asset.id, external_user_id=deposit.external_user_id
    )
    source_ref = f"btcpay_payment:{invoice_id}:{row.btcpay_payment_id}"
    try:
        entry = ledger.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=asset.id,
            source_ref=source_ref,
            postings=[(hot.id, row.amount), (available.id, -row.amount)],
            memo=f"deposit {deposit.id}",
        )
        entry_id = entry.id
    except ledger.AlreadyPosted as already:
        # The credit happened on an earlier run whose local bookkeeping did not
        # commit. Adopt the existing entry rather than losing the link.
        if already.entry is None:
            raise DepositError(
                f"ledger reports {source_ref} already posted but cannot produce the entry"
            ) from already
        entry_id = already.entry.id
        logger.info("deposit.credit_already_posted", deposit_id=str(deposit.id), ref=source_ref)

    row.ledger_entry_id = entry_id
    row.credited_at = datetime.now(UTC)
    row.resolved_by = actor
    deposit.amount_credited += row.amount


def _upsert_payment(
    session: Session,
    *,
    deposit: Deposit,
    payment_id: str,
    amount: int,
    after_expiration: bool,
) -> tuple[DepositPayment, bool]:
    inserted = session.execute(
        pg_insert(DepositPayment)
        .values(
            id=uuid7(),
            deposit_id=deposit.id,
            btcpay_payment_id=payment_id,
            amount=amount,
            after_expiration=after_expiration,
        )
        .on_conflict_do_nothing(index_elements=["deposit_id", "btcpay_payment_id"])
        .returning(DepositPayment.id)
    ).scalar_one_or_none()

    row = session.execute(
        select(DepositPayment).where(
            DepositPayment.deposit_id == deposit.id,
            DepositPayment.btcpay_payment_id == payment_id,
        )
    ).scalar_one()
    return row, inserted is not None


def apply_invoice_state(
    session: Session,
    gateway: BTCPayGateway,
    *,
    deposit_id: uuid.UUID,
    invoice: Invoice,
    payment_methods: list[InvoicePaymentMethod] | None = None,
    actor: str = AUTO_ACTOR,
    tolerance_pct: float = 0.0,
) -> ApplyResult:
    """Bring a deposit in line with BTCPay's current view of its invoice.

    The single transition path. Does not commit: the caller owns the
    transaction, which is what lets the ledger entry, the deposit row and the
    outbound event become durable together.
    """
    deposit = session.execute(
        select(Deposit)
        .where(Deposit.id == deposit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if deposit is None:
        raise DepositNotFound(f"no deposit {deposit_id}")

    previous = deposit.status
    result = ApplyResult(deposit_id=deposit.id, previous_status=previous, status=previous)

    if deposit.btcpay_invoice_id and deposit.btcpay_invoice_id != invoice.id:
        raise InvoiceMismatch(
            f"deposit {deposit.id} belongs to invoice {deposit.btcpay_invoice_id}, not {invoice.id}"
        )
    if previous in TERMINAL_STATUSES:
        return result

    asset = get_asset(session, deposit.asset_id, require_enabled=False)
    if payment_methods is None:
        payment_methods = gateway.get_invoice_payment_methods(invoice.id)

    if not deposit.btcpay_invoice_id:
        attach_invoice(
            session,
            deposit=deposit,
            invoice=invoice,
            payment_methods=payment_methods,
            asset=asset,
        )
        result.status = deposit.status

    method = _payment_method_for(asset, payment_methods)
    payments = [p for p in (method.payments if method else []) if p.status != "Invalid"]
    target = _target_status(invoice, has_payments=bool(payments))

    # A malformed amount must never be rounded into the ledger; it goes to a
    # human instead.
    unparseable = False
    parsed: list[tuple[str, int, str]] = []
    for payment in payments:
        try:
            parsed.append((payment.id, to_units(payment.value, asset.decimals), payment.status))
        except AmountError as exc:
            unparseable = True
            logger.error(
                "deposit.unparseable_amount",
                deposit_id=str(deposit.id),
                payment_id=payment.id,
                error=str(exc),
            )
    if unparseable:
        target = DepositStatus.REVIEW

    if target == DepositStatus.SETTLED and _breaches_usdt_tolerance(
        deposit, asset, parsed, tolerance_pct=tolerance_pct
    ):
        logger.warning(
            "deposit.usdt_amount_out_of_tolerance",
            deposit_id=str(deposit.id),
            expected=deposit.amount_expected,
            received=sum(amount for _id, amount, _status in parsed),
        )
        target = DepositStatus.REVIEW

    creditable = target == DepositStatus.SETTLED and deposit.status not in (
        DepositStatus.REVIEW,
        *TERMINAL_STATUSES,
    )
    after_expiration = invoice.additional_status == "PaidLate"

    for payment_id, amount, status in parsed:
        row, is_new = _upsert_payment(
            session,
            deposit=deposit,
            payment_id=payment_id,
            amount=amount,
            after_expiration=after_expiration,
        )
        if is_new:
            result.recorded_payments.append(payment_id)
            deposit.last_payment_seen_at = datetime.now(UTC)
        if creditable and status == "Settled" and row.credited_at is None:
            _credit_payment(
                session,
                deposit=deposit,
                asset=asset,
                row=row,
                invoice_id=invoice.id,
                actor=actor,
            )
            result.credited_payments.append(payment_id)
            result.credited_units += row.amount

    # Review means "money arrived and nobody has decided about it". Once an
    # admin has credited every recorded payment there is nothing left to
    # decide, and without this the sweep would push a resolved late payment
    # straight back into the queue on its next pass — the invoice is still
    # Expired, so the mapping alone never stops saying `review`.
    if target == DepositStatus.REVIEW and deposit.status == DepositStatus.SETTLED:
        uncredited = session.execute(
            select(DepositPayment.id).where(
                DepositPayment.deposit_id == deposit.id,
                DepositPayment.credited_at.is_(None),
            )
        ).first()
        if uncredited is None:
            target = DepositStatus.SETTLED

    if target != deposit.status and target in _ALLOWED_TARGETS[deposit.status]:
        logger.info(
            "deposit.transition",
            deposit_id=str(deposit.id),
            previous=deposit.status.value,
            status=target.value,
            invoice_status=invoice.status,
            invoice_additional_status=invoice.additional_status,
        )
        deposit.status = target

    result.status = deposit.status
    deposit.updated_at = datetime.now(UTC)
    session.flush()

    _emit_events(session, deposit=deposit, asset=asset, result=result)
    return result


def _emit_events(session: Session, *, deposit: Deposit, asset: Asset, result: ApplyResult) -> None:
    base = {
        "deposit_id": str(deposit.id),
        "external_user_id": deposit.external_user_id,
        "asset": asset.id,
        "status": deposit.status.value,
        "amount_credited": from_units(deposit.amount_credited, asset.decimals),
    }
    if result.recorded_payments:
        events.emit(
            session,
            event_type=events.DEPOSIT_DETECTED,
            payload={**base, "payments": result.recorded_payments},
        )
    if result.previous_status != result.status:
        if result.status == DepositStatus.SETTLED:
            events.emit(session, event_type=events.DEPOSIT_SETTLED, payload=base)
        elif result.status == DepositStatus.REVIEW:
            events.emit(session, event_type=events.DEPOSIT_REVIEW, payload=base)
        elif result.status == DepositStatus.EXPIRED:
            events.emit(session, event_type=events.DEPOSIT_EXPIRED, payload=base)


def refresh_deposit(
    session: Session,
    gateway: BTCPayGateway,
    *,
    deposit_id: uuid.UUID,
    actor: str = AUTO_ACTOR,
    tolerance_pct: float = 0.0,
) -> ApplyResult:
    """Fetch the invoice from BTCPay and feed it through `apply_invoice_state`."""
    deposit = session.get(Deposit, deposit_id)
    if deposit is None:
        raise DepositNotFound(f"no deposit {deposit_id}")
    if not deposit.btcpay_invoice_id:
        raise InvoiceMismatch(f"deposit {deposit_id} has no invoice yet")
    invoice = gateway.get_invoice(deposit.btcpay_invoice_id)
    return apply_invoice_state(
        session, gateway, deposit_id=deposit_id, invoice=invoice, actor=actor
    )


def resolve_review(
    session: Session,
    gateway: BTCPayGateway,
    *,
    deposit_id: uuid.UUID,
    action: str,
    payment_id: str | None,
    actor: str,
) -> ApplyResult:
    """Admin resolution of a deposit in `review`.

    The caller supplies which payment to credit and nothing else. The amount is
    fetched from Greenfield here, because an operator clearing a queue at 2am
    can fat-finger an extra zero, and the unique `source_ref` would then stop
    the correct credit from ever posting.
    """
    deposit = session.execute(
        select(Deposit)
        .where(Deposit.id == deposit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if deposit is None:
        raise DepositNotFound(f"no deposit {deposit_id}")
    if deposit.status != DepositStatus.REVIEW:
        raise IllegalTransition(
            f"deposit {deposit_id} is {deposit.status.value}, only review items can be resolved"
        )

    asset = get_asset(session, deposit.asset_id, require_enabled=False)
    result = ApplyResult(
        deposit_id=deposit.id, previous_status=deposit.status, status=deposit.status
    )

    if action == "dismiss":
        deposit.status = DepositStatus.DISMISSED
        deposit.updated_at = datetime.now(UTC)
        result.status = deposit.status
        session.flush()
        logger.info("deposit.review_dismissed", deposit_id=str(deposit.id), actor=actor)
        return result

    if action != "credit":
        raise IllegalTransition(f"unknown resolve action {action!r}")
    if not payment_id:
        raise PaymentNotFound("credit requires payment_id")
    if not deposit.btcpay_invoice_id:
        raise InvoiceMismatch(f"deposit {deposit_id} has no invoice to credit from")

    methods = gateway.get_invoice_payment_methods(deposit.btcpay_invoice_id)
    method = _payment_method_for(asset, methods)
    payment = next((p for p in (method.payments if method else []) if p.id == payment_id), None)
    if payment is None:
        raise PaymentNotFound(
            f"BTCPay reports no payment {payment_id} on invoice {deposit.btcpay_invoice_id}"
        )
    if payment.status != "Settled":
        raise PaymentNotFound(f"payment {payment_id} is {payment.status}, not Settled")

    amount = to_units(payment.value, asset.decimals)
    row, is_new = _upsert_payment(
        session,
        deposit=deposit,
        payment_id=payment.id,
        amount=amount,
        after_expiration=True,
    )
    if is_new:
        result.recorded_payments.append(payment.id)
    if row.credited_at is None:
        _credit_payment(
            session,
            deposit=deposit,
            asset=asset,
            row=row,
            invoice_id=deposit.btcpay_invoice_id,
            actor=actor,
        )
        result.credited_payments.append(payment.id)
        result.credited_units += row.amount

    deposit.status = DepositStatus.SETTLED
    deposit.updated_at = datetime.now(UTC)
    result.status = deposit.status
    session.flush()
    logger.info(
        "deposit.review_credited",
        deposit_id=str(deposit.id),
        payment_id=payment.id,
        actor=actor,
    )
    _emit_events(session, deposit=deposit, asset=asset, result=result)
    return result


# -- reads ----------------------------------------------------------------


def get_deposit(session: Session, deposit_id: uuid.UUID) -> Deposit:
    deposit = session.get(Deposit, deposit_id)
    if deposit is None:
        raise DepositNotFound(f"no deposit {deposit_id}")
    return deposit


def get_payments(session: Session, deposit_id: uuid.UUID) -> list[DepositPayment]:
    return list(
        session.execute(
            select(DepositPayment)
            .where(DepositPayment.deposit_id == deposit_id)
            .order_by(DepositPayment.id)
        ).scalars()
    )


def list_user_deposits(
    session: Session,
    *,
    external_user_id: str,
    limit: int,
    cursor: uuid.UUID | None = None,
) -> list[Deposit]:
    """Keyset pagination on the UUIDv7 primary key, which sorts by creation time."""
    query = select(Deposit).where(Deposit.external_user_id == external_user_id)
    if cursor is not None:
        query = query.where(Deposit.id < cursor)
    return list(session.execute(query.order_by(Deposit.id.desc()).limit(limit)).scalars())


def list_review_queue(session: Session, *, limit: int = 100) -> list[Deposit]:
    return list(
        session.execute(
            select(Deposit)
            .where(Deposit.status == DepositStatus.REVIEW)
            .order_by(Deposit.updated_at)
            .limit(limit)
        ).scalars()
    )


def due_for_sweep(
    session: Session, *, settled_window: timedelta, limit: int, now: datetime | None = None
) -> list[Deposit]:
    """Deposits the sweep should re-check.

    Active deposits always. Plus terminal-looking ones whose monitoring window
    is still open, because a second payment to a settled top-up invoice is
    credited only if someone is still asking — and if the API was down when the
    webhook fired, nobody ever will be.
    """
    now = now or datetime.now(UTC)
    horizon = now - settled_window
    query = (
        select(Deposit)
        .where(
            Deposit.btcpay_invoice_id.isnot(None),
            (Deposit.status.in_(tuple(ACTIVE_STATUSES)))
            | (
                Deposit.status.in_((DepositStatus.SETTLED, DepositStatus.EXPIRED))
                & (
                    (Deposit.monitoring_expires_at.is_(None) & (Deposit.updated_at >= horizon))
                    | (Deposit.monitoring_expires_at >= now)
                )
            ),
        )
        .order_by(Deposit.updated_at)
        .limit(limit)
    )
    return list(session.execute(query).scalars())


def stuck_in_creating(session: Session, *, older_than: timedelta, limit: int = 50) -> list[Deposit]:
    cutoff = datetime.now(UTC) - older_than
    return list(
        session.execute(
            select(Deposit)
            .where(
                Deposit.status == DepositStatus.CREATING,
                Deposit.btcpay_invoice_id.is_(None),
                Deposit.created_at < cutoff,
            )
            .order_by(Deposit.created_at)
            .limit(limit)
        ).scalars()
    )
