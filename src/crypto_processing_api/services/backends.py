"""Per-asset withdrawal backends.

BTC goes through BTCPay payouts. USDT cannot: the USDt plugin has no payout
handler of any kind, so M4 adds a manual backend rather than pretending BTCPay
can send it.

Two protocols, because there are honestly two kinds of backend and pretending
otherwise cost us a type check. `ManualTronBackend` never implemented the old
single `WithdrawalBackend` — it has `new_reference`/`verify_broadcast`, not
`initiate`/`poll_status`/`cancel` — and because nothing ever annotated it,
mypy never once looked at the discrepancy.

Both protocols are deliberately small, and neither can decide anything.
Whether to send lives in `services/withdrawals.py`; a backend that could decide
would be a second place to look when money goes missing.

`find_for_withdrawal` is part of the automated protocol rather than a helper
beside it. Crash recovery is not a BTCPay convenience — any backend that can
create a payout can crash after creating one, and a backend with no answer to
"did I already send this?" cannot safely be retried.

Two further protocols are **capabilities, not requirements**: `ReportsActualFee`
and `ProvesDefinitiveFailure`. A rail either can answer those questions or it
cannot, and folding them into `AutomatedWithdrawalBackend` would force every
backend — including forks' — to grow methods that would have to lie. They are
`runtime_checkable`, so the money path asks each backend once and takes the
plain answer when it cannot.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from crypto_processing_api.core.addresses import AddressError, decode_bolt11
from crypto_processing_api.core.amounts import from_units, msat_to_sat_round_up
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import (
    BTCPayError,
    BTCPayGateway,
    BTCPayNotFound,
)
from crypto_processing_api.gateway.btcpay_models import Payout
from crypto_processing_api.gateway.trongrid import TronGridGateway, TronTransfer
from crypto_processing_api.ledger.models import Withdrawal

logger = get_logger(__name__)

CPAPI_VERSION = 1
BACKEND_MANUAL_TRON = "manual_tron"

#: Lightning payment states that mean no money left the node and none can.
#: `Pending` and `Complete` are deliberately absent: both are compatible with
#: the user being paid, and releasing a hold on either is a double-pay.
LIGHTNING_DEFINITELY_UNPAID = frozenset({"Failed"})


class BackendPayoutState(StrEnum):
    """What a payout is doing, in terms no payment rail owns.

    The withdrawal state machine keys on these, not on BTCPay's spelling. That
    matters the day a second automated backend exists: `"InProgress"` is a
    Greenfield word, and a Lightning or TRON signer would have had to either
    adopt BTCPay's vocabulary or teach the state machine a second one.
    """

    AWAITING_APPROVAL = "awaiting_approval"
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    #: A state this version does not recognise. Deliberately has no entry in the
    #: status map, so an unrecognised payout is a logged no-op rather than a
    #: guess about money. A backend that grows a state must be taught here.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BackendPayout:
    """A payout as the money path sees it, whatever created it."""

    id: str
    state: BackendPayoutState
    destination: str
    #: Decimal string in display units, as the backend reported it. Kept as the
    #: backend's own text because it is the fact being adopted; converting early
    #: would lose the distinction between "not reported" and "zero".
    amount: str | None = None
    txid: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Exactly what the backend called this state. Only used for logs, and only
    #: interesting when `state` is UNKNOWN — where it is the whole message.
    raw_state: str = ""


#: BTCPay 2.4.2's five payout states. Verified against captured Greenfield
#: responses in tests/fixtures/greenfield/, not against the documentation.
_BTCPAY_STATES: dict[str, BackendPayoutState] = {
    "AwaitingApproval": BackendPayoutState.AWAITING_APPROVAL,
    "AwaitingPayment": BackendPayoutState.PENDING,
    "InProgress": BackendPayoutState.IN_FLIGHT,
    "Completed": BackendPayoutState.COMPLETED,
    "Cancelled": BackendPayoutState.CANCELLED,
}


def normalize_btcpay_payout(payout: Payout) -> BackendPayout:
    """Translate one Greenfield payout into the canonical shape.

    An unrecognised state becomes UNKNOWN rather than raising. A new BTCPay
    release inventing a state must not take the withdrawal poller down — the
    safe response to not understanding a payout is to leave the row alone and
    say so.
    """
    return BackendPayout(
        id=payout.id,
        state=_BTCPAY_STATES.get(payout.state, BackendPayoutState.UNKNOWN),
        destination=payout.destination,
        amount=payout.payout_amount or payout.original_amount,
        txid=payout.txid,
        metadata=payout.metadata,
        raw_state=payout.state,
    )


class AutomatedWithdrawalBackend(Protocol):
    """A backend that can send money by itself."""

    name: str

    def initiate(self, withdrawal: Withdrawal, *, net: int, decimals: int) -> BackendPayout: ...

    def poll_status(self, backend_ref: str) -> BackendPayout: ...

    def cancel(self, backend_ref: str) -> bool: ...

    def find_for_withdrawal(
        self, withdrawal: Withdrawal
    ) -> tuple[BackendPayout | None, list[BackendPayout]]: ...


class OperatorWithdrawalBackend(Protocol):
    """A backend where a human sends the money and reports back."""

    name: str

    def new_reference(self) -> str: ...

    def verify_broadcast(self, withdrawal: Withdrawal, txid: str) -> TronVerification: ...

    def confirmations(self, block_number: int | None) -> int: ...


@runtime_checkable
class ReportsActualFee(Protocol):
    """A backend that can say what the rail really charged for one payout.

    Optional because most rails cannot. On chain the fee is fixed when the
    payout is created and BTCPay batches several payouts into one transaction,
    so there is no per-withdrawal number to ask for. On Lightning the routing
    fee is unknowable in advance and knowable exactly afterwards, which is the
    case this exists for.

    Returns smallest units, or None when the rail has no number to give.
    Raises the rail's own transport error when it could not be asked — the
    caller distinguishes "no fee" from "no answer", and books the estimate with
    an alert in the second case.
    """

    def actual_wallet_fee(self, payout: BackendPayout) -> int | None: ...


@runtime_checkable
class ProvesDefinitiveFailure(Protocol):
    """A backend that can prove a payout's money never left.

    The proof is a sentence, stored verbatim as the release attestation, so the
    row records *why* a machine was allowed to do what normally needs a human.

    Returning None must always be safe: it means "not proven", and the hold
    then waits for an admin exactly as it does today. Anything short of
    certainty is None — a timeout, an unreachable node, a status the rail
    cannot pin down. The bar is that the rail has actively said the payment
    does not exist, not that we failed to find it.
    """

    def definitive_failure_proof(self, payout: BackendPayout) -> str | None: ...


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

    def initiate(self, withdrawal: Withdrawal, *, net: int, decimals: int) -> BackendPayout:
        return normalize_btcpay_payout(
            self.gateway.create_payout(
                destination=withdrawal.destination_address,
                amount=from_units(net, decimals),
                payout_method_id=self.payout_method_id,
                metadata=payout_metadata(withdrawal),
            )
        )

    def poll_status(self, backend_ref: str) -> BackendPayout:
        return normalize_btcpay_payout(self.gateway.get_payout(backend_ref))

    def cancel(self, backend_ref: str) -> bool:
        return self.gateway.cancel_payout(backend_ref)

    def find_for_withdrawal(
        self, withdrawal: Withdrawal
    ) -> tuple[BackendPayout | None, list[BackendPayout]]:
        """Resolve a submission whose outcome was never recorded.

        Returns the payout that carries this withdrawal's id, plus every
        *other* payout that matches the same destination with no withdrawal id
        we can read.

        The second half is the safety rule from the adversarial review: before
        a stuck row may be submitted again, there must be zero unclaimed
        payouts to this destination. "No match for this row" is not enough —
        with two withdrawals to one address, binding the wrong payout to the
        wrong row clears the other for resubmission and sends the money twice.
        """
        mine: BackendPayout | None = None
        unclaimed: list[BackendPayout] = []

        for raw in self.gateway.list_payouts():
            payout = normalize_btcpay_payout(raw)
            if payout.state is BackendPayoutState.CANCELLED:
                continue
            echoed = payout.metadata.get("withdrawal_id")
            if echoed is not None and str(echoed) == str(withdrawal.id):
                mine = payout
                continue
            if echoed is None and payout.destination == withdrawal.destination_address:
                unclaimed.append(payout)

        return mine, unclaimed


class LightningPayoutBackend(BtcpayPayoutBackend):
    """BTC_LN withdrawals: the same Greenfield payouts, plus what LN can answer.

    Every method the money path needs is inherited unchanged. That is the
    milestone's central claim rather than a convenience: `initiate`,
    `poll_status`, `cancel` and `find_for_withdrawal` are byte-for-byte the
    on-chain ones, and the conformance suite runs against this class unmodified
    to say so.

    What is added are two things Greenfield will only tell you if you ask a
    second endpoint, and both are about the Lightning node rather than about
    payouts:

    - the routing fee, because a completed Lightning payout reports its face
      value in both amount fields and the fee appears nowhere on it;
    - whether a payment ever left, because a payout that never routed carries
      no proof at all.

    Both start from the payment hash, and the payment hash is read out of the
    BOLT11 in `destination` rather than out of the payout. The invoice is the
    authoritative, checksum-protected source and it is present in every state —
    including the stuck one, where the payout has nothing.
    """

    def __init__(
        self,
        gateway: BTCPayGateway,
        *,
        payout_method_id: str,
        crypto_code: str,
        network: str,
    ) -> None:
        super().__init__(gateway, payout_method_id=payout_method_id)
        self.crypto_code = crypto_code
        self.network = network

    def payment_hash(self, payout: BackendPayout) -> str | None:
        try:
            return decode_bolt11(payout.destination, network=self.network).payment_hash
        except AddressError as exc:
            # Only reachable if BTCPay echoed back something that is not the
            # invoice we sent, so it is worth a log rather than a shrug.
            logger.error("lightning.destination_not_a_bolt11", payout=payout.id, error=str(exc))
            return None

    def actual_wallet_fee(self, payout: BackendPayout) -> int | None:
        payment_hash = self.payment_hash(payout)
        if payment_hash is None:
            return None
        payment = self.gateway.get_lightning_payment(self.crypto_code, payment_hash)
        if payment.fee_amount is None:
            return None
        try:
            millisatoshi = int(str(payment.fee_amount))
        except ValueError:
            logger.error(
                "lightning.unparseable_fee",
                payout=payout.id,
                fee_amount=str(payment.fee_amount),
            )
            return None
        return msat_to_sat_round_up(millisatoshi)

    def definitive_failure_proof(self, payout: BackendPayout) -> str | None:
        """Two questions, and the second one took a live stack to find.

        The node's verdict says whether money has left *so far*. On its own
        that is only enough if nothing can send it later — otherwise a released
        hold plus a retry that finally finds a route is a double payment.

        A payout BTCPay has cancelled needs only the verdict: a cancelled
        payout is never attempted again. A payout still live needs more, and
        this is the part the spike did not reach. BTCPay refuses to cancel a
        payout that is `InProgress` — `DELETE` answers 400 `invalid-state`,
        verified against 2.4.2 — and an unroutable Lightning payout sits
        `InProgress` indefinitely, carrying a `PayoutLightningBlob` whose
        preimage is all zeros because nothing settled. For those the invoice
        itself has to be dead: an expired BOLT11 cannot be paid by anybody,
        however many times anything retries it.

        That branch trades latency for certainty and it is the right trade.
        The hold comes back without a human either way; it comes back at the
        moment nothing in the world can still spend it.
        """
        payment_hash = self.payment_hash(payout)
        if payment_hash is None:
            return None

        verdict = self._node_verdict(payout, payment_hash)
        if verdict is None:
            return None

        if payout.state is BackendPayoutState.CANCELLED:
            return (
                f"payout {payout.id} was cancelled and {verdict}. A cancelled payout is "
                "never attempted again, so the funds did not leave and cannot."
            )

        expired_at = self._expired_at(payout)
        if expired_at is None:
            logger.info(
                "lightning.failure_not_definitive",
                payout=payout.id,
                detail="still live, and its invoice has not expired yet",
            )
            return None
        return (
            f"payout {payout.id} could not be cancelled — BTCPay leaves an unroutable "
            f"Lightning payout in {payout.raw_state or payout.state.value} — but "
            f"{verdict}, and the invoice expired at {expired_at}. An expired BOLT11 "
            "cannot be paid by anyone, so no retry can still spend these funds."
        )

    def _node_verdict(self, payout: BackendPayout, payment_hash: str) -> str | None:
        """What the node says about this payment, or None if it is not proof."""
        try:
            payment = self.gateway.get_lightning_payment(self.crypto_code, payment_hash)
        except BTCPayNotFound:
            # A 404 is also what a store with no Lightning node returns, and a
            # configuration fault must never read as proof that money stayed
            # put. Ask the node something it has to be able to answer first.
            if not self._node_is_answering():
                return None
            return (
                f"the node has no record of payment {payment_hash} while being reachable, "
                "so no HTLC was ever offered"
            )
        except BTCPayError as exc:
            logger.warning("lightning.failure_proof_unavailable", payout=payout.id, error=str(exc))
            return None

        if payment.status not in LIGHTNING_DEFINITELY_UNPAID:
            logger.info("lightning.failure_not_definitive", payout=payout.id, status=payment.status)
            return None
        return (
            f"the node reports payment {payment_hash} as {payment.status}, so every route "
            "attempt failed and no HTLC settled"
        )

    def _expired_at(self, payout: BackendPayout) -> str | None:
        try:
            invoice = decode_bolt11(payout.destination, network=self.network)
        except AddressError:
            return None
        if invoice.expires_at > datetime.now(UTC):
            return None
        return invoice.expires_at.isoformat()

    def _node_is_answering(self) -> bool:
        try:
            self.gateway.get_lightning_balance(self.crypto_code)
        except BTCPayError as exc:
            logger.warning("lightning.node_unreachable", error=str(exc))
            return False
        return True


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

    def confirmations(self, block_number: int | None) -> int:
        """On the protocol, not reached through `.verifier`.

        The caller in `services/withdrawals.py` used to do exactly that, which
        is how the money path ended up depending on this backend's private
        composition rather than on anything it promised.
        """
        return self.verifier.confirmations(block_number)


def _assert_protocols() -> None:
    """Make mypy check what nothing checked before.

    `ManualTronBackend` never satisfied the old single protocol, and no
    annotation ever forced the question. These lines are the whole point of
    splitting it: they are checked at type-check time and cost nothing at
    runtime.

    The capability lines matter for a different reason. `runtime_checkable`
    only checks that a method exists, so `isinstance` at the call site cannot
    catch a wrong signature. These do.
    """
    _automated: type[AutomatedWithdrawalBackend] = BtcpayPayoutBackend
    _operator: type[OperatorWithdrawalBackend] = ManualTronBackend
    _lightning_automated: type[AutomatedWithdrawalBackend] = LightningPayoutBackend
    _reports_fee: type[ReportsActualFee] = LightningPayoutBackend
    _proves_failure: type[ProvesDefinitiveFailure] = LightningPayoutBackend
