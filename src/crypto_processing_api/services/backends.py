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

import uuid
from dataclasses import dataclass
from typing import Protocol

from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway
from crypto_processing_api.gateway.btcpay_models import Payout
from crypto_processing_api.gateway.trongrid import TronGridGateway, TronTransfer
from crypto_processing_api.ledger.models import Withdrawal

logger = get_logger(__name__)

CPAPI_VERSION = 1
BACKEND_MANUAL_TRON = "manual_tron"


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


@dataclass(frozen=True, slots=True)
class TronVerification:
    """The answer to "is this txid the transfer the operator says it is?"."""

    ok: bool
    reason: str | None = None
    transfer: TronTransfer | None = None
    block_number: int | None = None

    @property
    def detail(self) -> str:
        return self.reason or "verified"


class TronTxVerifier:
    """Full-tuple verification of an operator-pasted transaction id.

    "Included and successful" is not enough, and it is the check that feels
    obviously sufficient right up until it is not. The failure it misses: the
    operator pastes a previous withdrawal's txid, or the txid of the TRX
    top-up they just sent to fund gas. Both are real transactions with healthy
    receipts, and both would mark a withdrawal complete while the user's USDT
    never moved.

    So every part of the claim is checked against the withdrawal:

    - the contract is the configured USDT contract (not some other token)
    - the sender is the configured hot wallet (not a third party's transfer)
    - the recipient is this withdrawal's destination
    - the amount equals `amount_net` exactly, in micro-units
    - the receipt succeeded, and a Transfer event is actually present
    """

    def __init__(
        self,
        gateway: TronGridGateway,
        *,
        contract_address: str,
        hot_wallet_address: str,
    ) -> None:
        self.gateway = gateway
        self.contract_address = contract_address
        self.hot_wallet_address = hot_wallet_address

    def verify(self, *, txid: str, destination: str, amount_net: int) -> TronVerification:
        transaction = self.gateway.get_transaction(txid)
        if transaction is None:
            return TronVerification(False, "TronGrid has no transaction with that id")

        if not transaction.receipt_succeeded:
            return TronVerification(
                False,
                f"the transaction failed on chain (receipt {transaction.receipt_result}, "
                f"contract {transaction.contract_result})",
                block_number=transaction.block_number,
            )

        if not transaction.transfers:
            # An out-of-energy TRC-20 call is included in a block and moves
            # nothing, so a healthy receipt with no Transfer event is exactly
            # the case worth catching.
            return TronVerification(
                False,
                "the transaction contains no TRC-20 Transfer event",
                block_number=transaction.block_number,
            )

        for transfer in transaction.transfers:
            if transfer.contract != self.contract_address:
                continue
            if transfer.from_address != self.hot_wallet_address:
                continue
            if transfer.to_address != destination:
                continue
            if transfer.amount != amount_net:
                continue
            return TronVerification(
                True, None, transfer=transfer, block_number=transaction.block_number
            )

        # Nothing matched. Report the first transfer's specifics: an operator
        # needs to know which part was wrong, not that "verification failed".
        first = transaction.transfers[0]
        reasons: list[str] = []
        if first.contract != self.contract_address:
            reasons.append(f"contract is {first.contract}, expected {self.contract_address}")
        if first.from_address != self.hot_wallet_address:
            reasons.append(f"sender is {first.from_address}, expected {self.hot_wallet_address}")
        if first.to_address != destination:
            reasons.append(f"recipient is {first.to_address}, expected {destination}")
        if first.amount != amount_net:
            reasons.append(f"amount is {first.amount} micro-units, expected {amount_net}")
        return TronVerification(
            False,
            "; ".join(reasons) or "no Transfer in this transaction matches this withdrawal",
            block_number=transaction.block_number,
        )

    def confirmations(self, block_number: int | None) -> int:
        if block_number is None:
            return 0
        return max(self.gateway.get_block_height() - block_number + 1, 0)


class ManualTronBackend:
    """USDT-TRC20 withdrawals, sent by a human.

    The USDt plugin has no payout handler of any kind, so BTCPay cannot send
    USDT — not automatically and not through its own UI. The operator sends
    from the TRON hot wallet with their own wallet software and records the
    transaction id here, where the verifier checks it.

    Phase 2 replaces `initiate` with a tronpy signer behind this same
    interface; nothing else in the money path changes.
    """

    name = BACKEND_MANUAL_TRON

    def __init__(self, verifier: TronTxVerifier) -> None:
        self.verifier = verifier

    @staticmethod
    def new_reference() -> str:
        """There is no payout id to correlate with, so we mint our own."""
        return f"manual:{uuid.uuid4()}"

    def verify_broadcast(self, withdrawal: Withdrawal, txid: str) -> TronVerification:
        if withdrawal.amount_net is None:
            return TronVerification(False, "this withdrawal has no net amount recorded")
        return self.verifier.verify(
            txid=txid,
            destination=withdrawal.destination_address,
            amount_net=withdrawal.amount_net,
        )
