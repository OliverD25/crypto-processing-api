"""Payment-method discovery.

BTCPay's payment method identifiers are version-dependent strings. Hardcoding
`"BTC-CHAIN"` works until it does not, and the failure mode is invoices created
for a payment method nobody is watching. So the real values are read from the
store at startup and written into the `assets` rows, which stay the single
source of truth thereafter.

Policy when an enabled asset has no matching payment method on the store:

- **BTC fails loudly.** It is the asset this milestone exists for; a store that
  cannot take BTC is a broken deployment, not a degraded one.
- **Everything else is disabled with a warning.** USDT needs the USDt plugin
  installed and an address pool provisioned, which is M4 work. Refusing to boot
  until then would make the BTC path hostage to an asset nobody has configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway
from crypto_processing_api.ledger.models import Asset
from crypto_processing_api.services.asset_registry import AssetProfile, get_registry

logger = get_logger(__name__)


class PaymentMethodMissing(RuntimeError):
    """A required asset has no matching payment method on the BTCPay store."""


@dataclass
class SyncReport:
    resolved: dict[str, str] = field(default_factory=dict)
    updated: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)


def _matches(profile: AssetProfile | None, current: str, payment_method_id: str) -> bool:
    """Does this store payment method serve this asset?

    Exact agreement with what the row already says wins, for every asset — that
    part was never per-asset. Anything else is the profile's business, because
    what counts as "the same payment method" is exactly the kind of knowledge
    that differs per rail and changes between plugin releases.
    """
    if payment_method_id == current:
        return True
    if profile is None:
        return False
    return profile.payment_method_matcher(payment_method_id)


def sync_payment_methods(session: Session, gateway: BTCPayGateway) -> SyncReport:
    """Reconcile `assets.btcpay_payment_method` with the store. Does not commit."""
    available = [
        method.payment_method_id
        for method in gateway.get_store_payment_methods(only_enabled=True)
        if method.enabled
    ]
    report = SyncReport()

    registry = get_registry()
    for asset in session.execute(select(Asset).order_by(Asset.id)).scalars():
        if not asset.enabled:
            continue
        profile = registry.get(asset.id)
        match = next(
            (
                candidate
                for candidate in available
                if _matches(profile, asset.btcpay_payment_method, candidate)
            ),
            None,
        )
        if match is None:
            if profile is not None and profile.required:
                raise PaymentMethodMissing(
                    f"store {gateway.store_id} has no enabled payment method for {asset.id}; "
                    f"enabled methods are {available or '[]'}"
                )
            asset.enabled = False
            report.disabled.append(asset.id)
            logger.warning(
                "assets.payment_method_missing",
                asset=asset.id,
                available=available,
                action="asset disabled",
            )
            continue

        report.resolved[asset.id] = match
        if match != asset.btcpay_payment_method:
            logger.info(
                "assets.payment_method_updated",
                asset=asset.id,
                previous=asset.btcpay_payment_method,
                current=match,
            )
            asset.btcpay_payment_method = match
            report.updated.append(asset.id)

    return report
