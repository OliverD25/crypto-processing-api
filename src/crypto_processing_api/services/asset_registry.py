"""What each asset *does*, as opposed to what it *is*.

An asset is one `assets` row plus one entry here. The row holds data — decimals,
limits, fees, invoice currency, whether addresses come from a pool. This module
holds behavior — which validator accepts a destination, how a fee is priced,
which backend sends the money, where to ask what the outside world thinks we
hold.

The split is not cosmetic. Behavior named by a string in a database row is a
way for the deployed code and the database to disagree about which function
runs, and to do it silently, in the middle of a withdrawal. Data in the
database, behavior in code, joined by asset id at startup.

**Why an explicit dict and not entry points.** mypy checks this file. A fork
that registers a backend missing a method fails type-check instead of failing
in production at 3am. Entry points exist so third-party *packages* can extend an
installed application; this is a deploy-first project where a fork edits one
file. They would also make "what code actually runs here" invisible during
review, which is a poor trade for money code.

Startup fails loudly when an enabled asset has no profile — see
`assert_every_enabled_asset_has_a_profile`. The alternative is an asset that
looks configured, takes deposits, and has no idea how to price a withdrawal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core.addresses import (
    AddressError,
    decode_bolt11,
    validate_bitcoin_address,
    validate_tron_address,
)
from crypto_processing_api.core.amounts import MSAT_PER_SAT, AmountError, to_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway
from crypto_processing_api.gateway.trongrid import TronGridError, TronGridGateway
from crypto_processing_api.ledger.models import Asset
from crypto_processing_api.services.backends import (
    BACKEND_MANUAL_TRON,
    AutomatedWithdrawalBackend,
    BtcpayPayoutBackend,
    LightningPayoutBackend,
    ManualTronBackend,
    OperatorWithdrawalBackend,
    TronTxVerifier,
)
from crypto_processing_api.services.fees import FeeQuote, flat_fee_quote, quote_btc_fee

logger = get_logger(__name__)

BACKEND_BTCPAY = "btcpay_payout"
#: A separate name from `BACKEND_BTCPAY`, even though both ride the same
#: Greenfield payout API. The column is the historical record of which rail
#: sent a payment, and on-chain and Lightning are not the same rail — they draw
#: on different floats and fail in different ways. `BTCPAY_PAYOUT_BACKENDS` is
#: what the worker queries use, so the distinction costs nothing.
BACKEND_BTCPAY_LN = "btcpay_payout_ln"

#: Backends the BTCPay payout worker owns. Manual TRON is deliberately absent —
#: see the docstrings on `due_for_submission` and `due_for_polling`.
BTCPAY_PAYOUT_BACKENDS: frozenset[str] = frozenset({BACKEND_BTCPAY, BACKEND_BTCPAY_LN})

BTC = "BTC"
BTC_LN = "BTC_LN"
USDT_TRC20 = "USDT_TRC20"

#: How much life a BOLT11 invoice must have left before this service will
#: accept it, and again before it will submit it.
#:
#: Both checks are needed and neither replaces the other. At request time it
#: refuses an invoice that is already useless, so the user is told immediately
#: rather than by a failed payout. At submission time it catches the invoice
#: that expired while the withdrawal sat in `pending_approval` — a queue with no
#: horizon, where "an operator gets to it tomorrow" is ordinary.
LIGHTNING_MIN_REMAINING_SECONDS = 120


class UnknownAsset(RuntimeError):
    """An enabled asset row with no registry entry."""


class FeePolicy(Protocol):
    """How much of a withdrawal is fee, and how much reaches the destination."""

    def quote(self, *, gross: int) -> FeeQuote: ...


class CustodySource(Protocol):
    """What the outside world says we hold."""

    # A read-only property rather than `source_name: str`. A plain attribute in
    # a Protocol has to be settable on the implementer, which rules out every
    # frozen dataclass — including all of ours.
    @property
    def source_name(self) -> str: ...

    def balance(self) -> int | None:
        """Smallest units, or None when the source could not be reached.

        None and 0 must never be confused. Zero is a solvency emergency; None
        is an API timeout. Returning 0 for an unreachable source would page
        someone at 3am to look at a wallet that is perfectly fine.
        """
        ...


@dataclass(frozen=True, slots=True)
class RegistryContext:
    """Everything a profile's factories are allowed to depend on."""

    settings: Settings
    asset: Asset
    gateway: BTCPayGateway
    tron: TronGridGateway | None = None


# -- fee policies ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DynamicChainFee:
    """A miner fee estimated per withdrawal. Wraps `quote_btc_fee` verbatim."""

    gateway: BTCPayGateway
    settings: Settings
    payment_method_id: str

    def quote(self, *, gross: int) -> FeeQuote:
        return quote_btc_fee(
            self.gateway,
            self.settings,
            gross=gross,
            payment_method_id=self.payment_method_id,
        )


@dataclass(frozen=True, slots=True)
class FlatFee:
    """A fixed service charge. Wraps `flat_fee_quote` verbatim.

    TRC-20 transfers cost TRX energy rather than USDT, so there is no per-
    withdrawal amount to estimate — the fee covers gas as a service charge.
    """

    flat_fee: int
    dust_threshold: int = 0

    def quote(self, *, gross: int) -> FeeQuote:
        return flat_fee_quote(
            gross=gross, flat_fee=self.flat_fee, dust_threshold=self.dust_threshold
        )


# -- custody sources -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BtcpayWalletCustody:
    gateway: BTCPayGateway
    payment_method_id: str
    decimals: int
    source_name: str = "btcpay_wallet"

    def balance(self) -> int | None:
        try:
            overview = self.gateway.get_wallet(self.payment_method_id)
            return to_units(overview.balance, self.decimals)
        except (BTCPayError, AmountError) as exc:
            logger.warning(
                "job_c.btcpay_balance_failed", method=self.payment_method_id, error=str(exc)
            )
            return None


@dataclass(frozen=True, slots=True)
class TronGridCustody:
    tron: TronGridGateway
    address: str
    contract: str
    source_name: str = "trongrid"

    def balance(self) -> int | None:
        try:
            return self.tron.get_trc20_balance(self.address, self.contract)
        except TronGridError as exc:
            logger.warning("job_c.tron_balance_failed", error=str(exc))
            return None


@dataclass(frozen=True, slots=True)
class LightningNodeCustody:
    """Outbound channel liquidity, which is the only Lightning money we can pay.

    Deliberately **not** the node's total. The same node holds on-chain funds
    and inbound capacity, and neither can settle a withdrawal; counting them
    would make the solvency check answer a question nobody asked and hide the
    one failure that matters for this asset — enough BTC in the node overall,
    and none of it on the right side of a channel.

    Millisatoshi round **down** here, the opposite of routing fees, and for the
    same reason: the safe direction. Understating what we hold makes the
    insolvency signal slightly more eager, which costs an operator a glance.
    Overstating it makes the signal late, which costs money.
    """

    gateway: BTCPayGateway
    crypto_code: str
    source_name: str = "lightning_node"

    def balance(self) -> int | None:
        try:
            reported = self.gateway.get_lightning_balance(self.crypto_code).local_msat
        except BTCPayError as exc:
            logger.warning("job_c.lightning_balance_failed", error=str(exc))
            return None
        if reported is None:
            logger.warning(
                "job_c.lightning_balance_shape",
                detail="the node reported no offchain.local balance",
            )
            return None
        try:
            millisatoshi = int(str(reported))
        except ValueError:
            logger.warning("job_c.lightning_balance_unparseable", reported=str(reported))
            return None
        return millisatoshi // MSAT_PER_SAT


# -- destination validators ------------------------------------------------
#
# These raise AddressError only. `validate_destination` converts it into the
# API-facing InvalidDestination, which keeps the error text identical to what
# the if/elif chain produced and keeps this module free of a dependency on
# services/withdrawals.py — which imports this one.


def validate_bitcoin_destination(session: Session, settings: Settings, address: str) -> None:
    validate_bitcoin_address(address, network=settings.bitcoin_network)


def validate_tron_destination(session: Session, settings: Settings, address: str) -> None:
    validate_tron_address(address)
    if settings.tron_hot_wallet_address == address:
        raise AddressError("that address is this service's own TRON hot wallet")


def validate_lightning_destination(session: Session, settings: Settings, address: str) -> None:
    """Structure, network and remaining life. Not the amount.

    The amount cannot be checked here: the net is a function of the fee quote,
    and this runs before one exists. `guard_lightning_submission` does it once
    the net is known, and is called from both places that know it.
    """
    invoice = decode_bolt11(address, network=settings.bitcoin_network)
    _require_remaining_life(invoice.expires_at, "this invoice")


def _require_remaining_life(expires_at: datetime, subject: str) -> None:
    horizon = datetime.now(UTC) + timedelta(seconds=LIGHTNING_MIN_REMAINING_SECONDS)
    if expires_at <= horizon:
        raise AddressError(
            f"{subject} expires at {expires_at.isoformat()}, which is less than "
            f"{LIGHTNING_MIN_REMAINING_SECONDS} seconds away; ask the payee's wallet for a "
            "fresh one. A payout against an expired invoice can never be paid and would "
            "hold the balance until someone unpicked it"
        )


def guard_lightning_submission(settings: Settings, destination: str, net: int) -> None:
    """The checks that need the net amount, run at request and at submission.

    Two of them, and each covers a way to lose money that the other does not:

    **The amount must match, or not be there at all.** BTCPay pays a BOLT11
    that carries its own amount at *that* amount, not at the payout's. An
    invoice for more than `net` would send more than the user asked for and
    more than the ledger booked; one for less would leave the difference
    stranded. An amountless invoice is fine — the payout amount is then the
    only amount there is.

    **The invoice must still be alive.** Checked again here because
    `pending_approval` has no horizon: an operator approving yesterday's
    request is ordinary, and the invoice underneath it may be hours dead.
    """
    invoice = decode_bolt11(destination, network=settings.bitcoin_network)
    _require_remaining_life(invoice.expires_at, "this invoice")

    requested = invoice.amount_sat
    if requested is not None and requested != net:
        raise AddressError(
            f"the invoice asks for {requested} sat but {net} sat would be sent "
            f"(the fee is deducted from the amount you requested). Ask for an invoice "
            f"for exactly {net} sat, or an amountless one"
        )


# -- payment method matchers -----------------------------------------------


def matches_btc_chain(payment_method_id: str) -> bool:
    return payment_method_id.upper() == "BTC-CHAIN"


def matches_btc_lightning(payment_method_id: str) -> bool:
    """`BTC-LN` on 2.4.2, and the pre-2.0 spelling as well.

    Looser than an equality check for the reason spelled out on
    `matches_usdt_tron`: a matcher that fails to match does not fail loudly. It
    makes `sync_payment_methods` disable the asset at startup, and the first
    anyone hears of it is a deposit endpoint returning 404 in production.
    """
    upper = payment_method_id.upper()
    return upper in ("BTC-LN", "BTC_LIGHTNINGLIKE", "BTC-LIGHTNINGNETWORK")


def crypto_code_of(payment_method_id: str) -> str:
    """`BTC-LN` -> `BTC`.

    Greenfield's Lightning routes are keyed by chain rather than by payment
    method — `/lightning/BTC/balance`, not `/lightning/BTC-LN/balance` — and it
    is the one place in this API where the same thing has two names.
    """
    for separator in ("-", "_"):
        if separator in payment_method_id:
            return payment_method_id.split(separator, 1)[0].upper()
    return payment_method_id.upper()


def matches_usdt_tron(payment_method_id: str) -> bool:
    """Deliberately fuzzy, and it must stay that way.

    The USDt plugin's payment method id has changed shape between releases. A
    stricter match does not fail loudly: `sync_payment_methods` disables the
    asset at startup, deposits start 404ing in production, and nothing in CI
    ever sees it because USDT cannot be exercised on regtest at all.
    """
    upper = payment_method_id.upper()
    return "USDT" in upper and ("TRON" in upper or "TRX" in upper)


# -- the profile -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssetProfile:
    """The behavior half of an asset."""

    asset_id: str
    fee_policy: Callable[[RegistryContext], FeePolicy]
    destination_validator: Callable[[Session, Settings, str], None]
    payment_method_matcher: Callable[[str], bool]
    #: Name written to `withdrawals.backend`. The column is the historical
    #: record of which rail sent a payment, so it stays a plain string.
    withdrawal_backend: str
    #: Whether BTCPay exposes a wallet API for this payment method. Replaces a
    #: `-CHAIN` suffix test on the payment method id, which was a guess about a
    #: string rather than a statement about a capability.
    has_btcpay_wallet: bool
    automated_backend: Callable[[RegistryContext], AutomatedWithdrawalBackend] | None = None
    operator_backend: Callable[[RegistryContext], OperatorWithdrawalBackend] | None = None
    custody_source: Callable[[RegistryContext], CustodySource | None] | None = None
    #: A second destination check, run once the net amount is known: at request
    #: time against the request-time quote, and again just before submission.
    #: Raises `AddressError` like `destination_validator`.
    #:
    #: Separate from `destination_validator` because it needs an argument that
    #: does not exist yet when a destination is first seen, and run twice
    #: because `pending_approval` has no horizon — a destination that was fine
    #: when the user asked can be worthless by the time an operator approves.
    #: Most assets need none: a bitcoin address does not expire and does not
    #: carry an amount of its own.
    submission_guard: Callable[[Settings, str, int], None] | None = None
    #: How long a payout may sit `submitted` before this service cancels it,
    #: or None for "as long as it takes".
    #:
    #: None is right for on-chain BTC: a transaction in the mempool is being
    #: worked on, and there is no such thing as giving up on it. It is wrong for
    #: Lightning, where BTCPay's processor retries an unroutable payment for as
    #: long as the invoice lives and never moves it to a failed state — so
    #: without a deadline the withdrawal stays `submitted` forever with the
    #: user's balance held and no status anywhere that says "this will not
    #: happen".
    submitted_timeout_seconds: Callable[[Settings], int | None] | None = None
    #: Missing from the store at startup is fatal rather than a warning. BTC is
    #: the anchor: a deployment that cannot take it is broken, not degraded.
    required: bool = False

    @property
    def sweep(self) -> str:
        """Who owns finishing a withdrawal: a worker, or a human.

        Derived rather than declared, so it cannot contradict the factories.
        """
        return "automated" if self.automated_backend is not None else "operator"


def _btc_profile() -> AssetProfile:
    return AssetProfile(
        asset_id=BTC,
        fee_policy=lambda ctx: DynamicChainFee(
            ctx.gateway, ctx.settings, ctx.asset.btcpay_payment_method
        ),
        destination_validator=validate_bitcoin_destination,
        payment_method_matcher=matches_btc_chain,
        withdrawal_backend=BACKEND_BTCPAY,
        has_btcpay_wallet=True,
        automated_backend=lambda ctx: BtcpayPayoutBackend(
            ctx.gateway, payout_method_id=ctx.asset.btcpay_payment_method
        ),
        custody_source=lambda ctx: BtcpayWalletCustody(
            ctx.gateway, ctx.asset.btcpay_payment_method, ctx.asset.decimals
        ),
        required=True,
    )


def _lightning_profile() -> AssetProfile:
    """BTC_LN: the same Greenfield payouts, a different float.

    `BTC_LN` is a **separate asset with its own accounts**, not a second way to
    spend the on-chain BTC balance. That is the honest custodial model —
    channel balance and wallet balance are different money with different risk,
    and a user's Lightning balance cannot pay an on-chain withdrawal or the
    reverse. It also means zero ledger changes: everything here is data plus
    this entry. Moving value between the two floats is an operator action, in
    docs/runbook-ln-rebalance.md.

    `has_btcpay_wallet=False` because there is no wallet API behind `BTC-LN`,
    so the unattributed-receive scan cannot run for it. That detector gap is
    real and is documented for this asset the way it is for USDT — though it
    matters much less here, because a Lightning invoice cannot be paid twice
    and there is no address to reuse a week later.
    """
    return AssetProfile(
        asset_id=BTC_LN,
        fee_policy=lambda ctx: FlatFee(ctx.asset.withdrawal_flat_fee),
        destination_validator=validate_lightning_destination,
        submission_guard=guard_lightning_submission,
        payment_method_matcher=matches_btc_lightning,
        withdrawal_backend=BACKEND_BTCPAY_LN,
        has_btcpay_wallet=False,
        automated_backend=lambda ctx: LightningPayoutBackend(
            ctx.gateway,
            payout_method_id=ctx.asset.btcpay_payment_method,
            crypto_code=crypto_code_of(ctx.asset.btcpay_payment_method),
            network=ctx.settings.bitcoin_network,
        ),
        custody_source=lambda ctx: LightningNodeCustody(
            ctx.gateway, crypto_code_of(ctx.asset.btcpay_payment_method)
        ),
        submitted_timeout_seconds=lambda settings: settings.ln_payout_timeout_seconds,
    )


def _usdt_profile(settings: Settings) -> AssetProfile:
    return AssetProfile(
        asset_id=USDT_TRC20,
        fee_policy=lambda ctx: FlatFee(ctx.asset.withdrawal_flat_fee),
        destination_validator=validate_tron_destination,
        payment_method_matcher=matches_usdt_tron,
        withdrawal_backend=BACKEND_MANUAL_TRON,
        # The USDt plugin is an invoice payment method with no wallet API, so
        # the wallet scan and the BTCPay balance call have nothing to talk to.
        has_btcpay_wallet=False,
        operator_backend=lambda ctx: ManualTronBackend(
            TronTxVerifier(
                _require_tron(ctx),
                contract_address=ctx.settings.usdt_contract,
                hot_wallet_address=ctx.settings.tron_hot_wallet_address or "",
            )
        ),
        custody_source=_usdt_custody,
    )


def _require_tron(ctx: RegistryContext) -> TronGridGateway:
    if ctx.tron is None:
        raise UnknownAsset(
            f"{ctx.asset.id} needs a TronGrid gateway and none was configured; "
            "set TRON_API_URL and TRON_HOT_WALLET_ADDRESS"
        )
    return ctx.tron


def _usdt_custody(ctx: RegistryContext) -> CustodySource | None:
    """None when TRON is not configured, which reads as 'no source'.

    Not an error: a deployment may run BTC only, and Job C already knows how to
    report a custody line it could not price.
    """
    if ctx.tron is None or not ctx.settings.tron_hot_wallet_address:
        return None
    return TronGridCustody(
        ctx.tron, ctx.settings.tron_hot_wallet_address, ctx.settings.usdt_contract
    )


def build_registry(settings: Settings) -> dict[str, AssetProfile]:
    """Every asset this build knows how to operate.

    `settings` is taken now rather than read inside the profiles so the set of
    registered assets is fixed at startup. An asset that appears or disappears
    depending on when a factory happens to run is not a registry.

    Lightning is absent unless it was asked for. With `LIGHTNING_ENABLED=false`
    the registry is byte-identical to the one before BTC_LN existed, no
    Lightning permission is requested from BTCPay, and `assets.BTC_LN` is never
    seeded — so a deployment that does not want a server-level BTCPay
    permission does not silently acquire one.

    Turning it back off after the row exists is deliberately not silent:
    `assert_every_enabled_asset_has_a_profile` refuses to start, because an
    asset holding user balances must not be made to disappear by unsetting an
    environment variable.
    """
    profiles = [_btc_profile(), _usdt_profile(settings)]
    if settings.lightning_enabled:
        profiles.append(_lightning_profile())
    return {profile.asset_id: profile for profile in profiles}


@lru_cache(maxsize=1)
def get_registry() -> dict[str, AssetProfile]:
    """The process-wide registry, mirroring `get_settings`.

    Cached because the answer cannot change while the process runs: profiles
    are code, and the settings they close over are themselves cached.
    """
    return build_registry(get_settings())


def profile_for(asset_id: str) -> AssetProfile:
    """Convenience for the many call sites that just want one profile."""
    return get_profile(get_registry(), asset_id)


def automated_backend_for(
    asset: Asset,
    *,
    gateway: BTCPayGateway,
    settings: Settings,
    tron: TronGridGateway | None = None,
) -> AutomatedWithdrawalBackend:
    """The backend that sends this asset, built from its profile.

    The three workers that submit and poll payouts used to construct
    `BtcpayPayoutBackend` themselves from `asset.btcpay_payment_method`. That
    worked while every automated asset was the same class, and it quietly
    undid the registry the moment one was not: a Lightning withdrawal would
    have been submitted and polled by the on-chain backend, which is correct in
    every method it has and missing both of the ones that keep a routing fee on
    the books.
    """
    profile = profile_for(asset.id)
    if profile.automated_backend is None:
        raise UnknownAsset(
            f"asset {asset.id} has no automated backend; its withdrawals are "
            f"{profile.sweep}-swept and nothing should be asking a worker to send them"
        )
    return profile.automated_backend(
        RegistryContext(settings=settings, asset=asset, gateway=gateway, tron=tron)
    )


def get_profile(registry: dict[str, AssetProfile], asset_id: str) -> AssetProfile:
    profile = registry.get(asset_id)
    if profile is None:
        raise UnknownAsset(
            f"asset {asset_id!r} has no registry entry, so this build does not know how to "
            "validate a destination, price a fee, or send it. See docs/extending.md"
        )
    return profile


def assert_every_enabled_asset_has_a_profile(
    session: Session, registry: dict[str, AssetProfile]
) -> None:
    """Startup gate. An asset the code cannot operate must not accept deposits.

    Deliberately fatal. The quiet alternative is an asset that looks configured
    and takes money, and only discovers at withdrawal time that nothing knows
    how to price or send it — by which point the deposit has already happened.
    """
    enabled = list(session.execute(select(Asset.id).where(Asset.enabled.is_(True))).scalars())
    orphans = sorted(asset_id for asset_id in enabled if asset_id not in registry)
    if orphans:
        raise UnknownAsset(
            f"enabled assets with no registry entry: {', '.join(orphans)}. "
            f"Registered: {', '.join(sorted(registry))}. Either add a profile in "
            "services/asset_registry.py or disable the row."
        )
