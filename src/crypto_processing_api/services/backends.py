"""Per-asset withdrawal backends.

BTC goes through BTCPay payouts. USDT cannot: the USDt plugin has no payout
handler of any kind, so M4 adds a manual backend behind this same protocol
rather than pretending BTCPay can send it.

The protocol is deliberately small — create, ask, cancel — because everything
that decides *whether* to send lives in `services/withdrawals.py`, and a
backend that could decide things would be a second place to look when money
goes missing.
"""

from __future__ import annotations

from typing import Protocol

from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway
from crypto_processing_api.gateway.btcpay_models import Payout
from crypto_processing_api.ledger.models import Withdrawal

logger = get_logger(__name__)

CPAPI_VERSION = 1


class WithdrawalBackend(Protocol):
    name: str

    def initiate(self, withdrawal: Withdrawal, *, net: int, decimals: int) -> Payout: ...

    def poll_status(self, backend_ref: str) -> Payout: ...

    def cancel(self, backend_ref: str) -> bool: ...


def payout_metadata(withdrawal: Withdrawal) -> dict[str, object]:
    """The correlation key, verified live against BTCPay 2.4.2.

    `metadata` is echoed back on GET /api/v1/payouts/{id} and in the store
    payout list, which is what lets a crashed submission be resolved by asking
    BTCPay "which payout is this withdrawal?" instead of guessing from
    destination and amount — a guess that is ambiguous exactly when it matters,
    because a user withdrawing the same amount to the same address twice in ten
    minutes is ordinary behaviour.
    """
    return {
        "cpapi": True,
        "cpapi_version": CPAPI_VERSION,
        "withdrawal_id": str(withdrawal.id),
        "external_user_id": withdrawal.external_user_id,
        # Shown on BTCPay's payout list page, so an operator looking at the UI
        # can tell our payouts from anything else.
        "source": "crypto-processing-api",
    }


class BtcpayPayoutBackend:
    """BTC withdrawals through Greenfield payouts."""

    name = "btcpay_payout"

    def __init__(self, gateway: BTCPayGateway, *, payout_method_id: str) -> None:
        self.gateway = gateway
        self.payout_method_id = payout_method_id

    def initiate(self, withdrawal: Withdrawal, *, net: int, decimals: int) -> Payout:
        return self.gateway.create_payout(
            destination=withdrawal.destination_address,
            amount=from_units(net, decimals),
            payout_method_id=self.payout_method_id,
            metadata=payout_metadata(withdrawal),
        )

    def poll_status(self, backend_ref: str) -> Payout:
        return self.gateway.get_payout(backend_ref)

    def cancel(self, backend_ref: str) -> bool:
        return self.gateway.cancel_payout(backend_ref)


def find_payout_for_withdrawal(
    gateway: BTCPayGateway, withdrawal: Withdrawal
) -> tuple[Payout | None, list[Payout]]:
    """Resolve a submission whose outcome was never recorded.

    Returns the payout that carries this withdrawal's id, plus every *other*
    payout that matches the same destination with no withdrawal id we can
    read.

    The second half is the safety rule from the adversarial review: before a
    stuck row may be submitted again, there must be zero unclaimed payouts to
    this destination. "No match for this row" is not enough — with two
    withdrawals to one address, binding the wrong payout to the wrong row
    clears the other for resubmission and sends the money twice.
    """
    mine: Payout | None = None
    unclaimed: list[Payout] = []

    for payout in gateway.list_payouts():
        if payout.state == "Cancelled":
            continue
        echoed = payout.metadata.get("withdrawal_id")
        if echoed is not None and str(echoed) == str(withdrawal.id):
            mine = payout
            continue
        if echoed is None and payout.destination == withdrawal.destination_address:
            unclaimed.append(payout)

    return mine, unclaimed
