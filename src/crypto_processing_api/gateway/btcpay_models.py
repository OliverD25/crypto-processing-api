"""Transport models for the BTCPay Greenfield API.

Shapes were read from the swagger of the pinned tag (2.4.2), not from memory.
Two things that version changed and that the design documents predate:

- per-invoice routes are no longer store-scoped: `/api/v1/invoices/{id}`
- `Payment` carries `id`, `value` and `status`; there is no separate txid field,
  so for on-chain payments the txid is the part of `id` before the first dash

Every model is `extra="ignore"`: BTCPay adds fields across versions and an
unknown field must never break a deposit credit. Monetary values stay strings
here and become integers only at the service boundary, through Decimal.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The invoice states BTCPay 2.4.2 is known to emit. Documentation and test
#: data — deliberately NOT the field types, for the reason spelled out on
#: `Invoice.status`. Server-owned enums are parsed as plain strings and
#: recognised later, so a value we have never seen is news rather than a crash.
InvoiceStatus = Literal["New", "Processing", "Expired", "Invalid", "Settled"]
InvoiceAdditionalStatus = Literal[
    "None", "PaidLate", "PaidPartial", "Marked", "Invalid", "PaidOver"
]
PaymentStatus = Literal["Invalid", "Processing", "Settled"]
TransactionStatus = Literal["Unconfirmed", "Confirmed", "Replaced"]


class TransportModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CheckoutOptions(TransportModel):
    speed_policy: str | None = Field(default=None, alias="speedPolicy")
    payment_methods: list[str] | None = Field(default=None, alias="paymentMethods")
    expiration_minutes: int | None = Field(default=None, alias="expirationMinutes")
    monitoring_minutes: int | None = Field(default=None, alias="monitoringMinutes")


class Invoice(TransportModel):
    id: str
    store_id: str = Field(alias="storeId")
    amount: str | None = None
    paid_amount: str | None = Field(default=None, alias="paidAmount")
    currency: str | None = None
    type: str | None = None
    checkout_link: str | None = Field(default=None, alias="checkoutLink")
    created_time: int | None = Field(default=None, alias="createdTime")
    expiration_time: int | None = Field(default=None, alias="expirationTime")
    monitoring_expiration: int | None = Field(default=None, alias="monitoringExpiration")
    #: `str`, not `InvoiceStatus`, and the difference is the whole deposit
    #: sweep. A Literal makes pydantic reject an unrecognised status during
    #: parsing, before `_target_status` or the transition matrix can decline to
    #: act on it. The resulting ValidationError is not a BTCPayError, so the
    #: sweep does not skip that deposit — it raises through the batch and every
    #: other deposit behind it stops being polled. A status we do not recognise
    #: is news, not a crash; `apply_invoice_state` logs it and leaves the row
    #: alone, and the next poll settles it once BTCPay says something known.
    status: str
    #: Same reasoning. This one decides review-vs-settle for late and
    #: over-payments, so a new value here is exactly the case worth surviving.
    additional_status: str = Field(default="None", alias="additionalStatus")
    archived: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    checkout: CheckoutOptions | None = None


class Payment(TransportModel):
    id: str
    received_date: int | None = Field(default=None, alias="receivedDate")
    value: str
    fee: str | None = None
    #: Server-owned, so parsed loosely. An unrecognised payment status is not
    #: `Settled`, so it is never credited, and not `Invalid`, so it still counts
    #: as a payment that exists — which is the safe reading of "unknown".
    status: str
    destination: str | None = None

    @property
    def txid(self) -> str:
        """On-chain payment ids are `<txid>-<vout>`; Lightning ids are opaque."""
        return self.id.split("-", 1)[0]


class InvoicePaymentMethod(TransportModel):
    payment_method_id: str = Field(alias="paymentMethodId")
    currency: str | None = None
    destination: str | None = None
    payment_link: str | None = Field(default=None, alias="paymentLink")
    rate: str | None = None
    payment_method_paid: str | None = Field(default=None, alias="paymentMethodPaid")
    total_paid: str | None = Field(default=None, alias="totalPaid")
    due: str | None = None
    amount: str | None = None
    payments: list[Payment] = Field(default_factory=list)
    activated: bool = True


class StorePaymentMethod(TransportModel):
    payment_method_id: str = Field(alias="paymentMethodId")
    enabled: bool = False
    config: dict[str, Any] | None = None


class WalletOverview(TransportModel):
    balance: str
    unconfirmed_balance: str | None = Field(default=None, alias="unconfirmedBalance")
    confirmed_balance: str | None = Field(default=None, alias="confirmedBalance")


class WalletTransaction(TransportModel):
    """One wallet-level transaction.

    Greenfield reports a net `amount` per transaction, not per output, so the
    unattributed-receive detector works at transaction granularity. A positive
    amount means coins arrived.
    """

    transaction_hash: str | None = Field(default=None, alias="transactionHash")
    comment: str | None = None
    amount: str
    block_hash: str | None = Field(default=None, alias="blockHash")
    block_height: str | int | None = Field(default=None, alias="blockHeight")
    confirmations: str | int | None = None
    timestamp: int | None = None
    status: TransactionStatus | str | None = None


class Webhook(TransportModel):
    id: str
    url: str
    enabled: bool = True
    secret: str | None = None


#: The payout states BTCPay 2.4.2 is known to emit. Documentation and test
#: data, deliberately NOT the type of `Payout.state` — see the note there.
PayoutState = Literal["AwaitingApproval", "AwaitingPayment", "InProgress", "Completed", "Cancelled"]


class PayoutPaymentProof(TransportModel):
    """How BTCPay says a payout was paid.

    The swagger documents `proofType`, `id` and `link`. The running 2.4.2 sends
    PascalCase for the on-chain handler:

        {"Accounted": true, "ProofType": "PayoutTransactionOnChainBlob",
         "Candidates": ["2c31..."], "TransactionId": "2c31..."}

    The fact sheet flagged this shape as version-dependent and it is. Both
    spellings are accepted, because reading the wrong one means a confirmed
    withdrawal with no transaction id — nothing to show the user and nothing to
    check on chain.
    """

    proof_type: str | None = None
    id: str | None = None
    link: str | None = None
    transaction_id: str | None = None
    candidates: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_either_casing(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        mapping = {
            "prooftype": "proof_type",
            "transactionid": "transaction_id",
            "id": "id",
            "link": "link",
            "candidates": "candidates",
        }
        normalised: dict[str, Any] = {}
        for key, item in value.items():
            normalised[mapping.get(str(key).lower(), str(key))] = item
        return normalised

    @property
    def txid(self) -> str | None:
        if self.transaction_id:
            return self.transaction_id
        if self.id:
            return self.id
        if self.candidates:
            return self.candidates[0]
        if self.link:
            # Block explorer links end in the transaction id.
            return self.link.rstrip("/").rsplit("/", 1)[-1] or None
        return None


class Payout(TransportModel):
    """A BTCPay payout.

    Field names verified against the running 2.4.2, not the design documents:
    it is `payoutMethodId`, not `paymentMethod`, and the amount after approval
    is `payoutAmount`.
    """

    id: str
    revision: int | None = None
    pull_payment_id: str | None = Field(default=None, alias="pullPaymentId")
    date: str | int | None = None
    destination: str
    original_amount: str | None = Field(default=None, alias="originalAmount")
    original_currency: str | None = Field(default=None, alias="originalCurrency")
    payout_amount: str | None = Field(default=None, alias="payoutAmount")
    payout_currency: str | None = Field(default=None, alias="payoutCurrency")
    payout_method_id: str | None = Field(default=None, alias="payoutMethodId")
    #: `str`, not `PayoutState`, and the difference is load-bearing.
    #:
    #: A Literal here means pydantic rejects any state this version has not
    #: heard of, and it rejects it during parsing — before the normalizer, the
    #: status map, or the "unknown state is a logged no-op" path can run. The
    #: resulting ValidationError is neither BTCPayError nor WithdrawalError, so
    #: Job B's sweep does not skip that row: it raises through the whole batch
    #: and every other withdrawal stops being polled too.
    #:
    #: A state we do not recognise is news, not a crash. It becomes
    #: BackendPayoutState.UNKNOWN in `normalize_btcpay_payout`, which has no
    #: entry in the status map, which leaves the row untouched and logs what
    #: BTCPay actually said. Strict validation is right for fields we send and
    #: wrong for an enum the server owns.
    state: str
    payment_proof: PayoutPaymentProof | None = Field(default=None, alias="paymentProof")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def txid(self) -> str | None:
        return self.payment_proof.txid if self.payment_proof else None


class FeeRate(TransportModel):
    """Sats per virtual byte, as BTCPay's wallet reports it."""

    fee_rate: str | float = Field(alias="feeRate")


#: What BTCPay's Lightning node says about one outgoing payment. `Unknown` is
#: the node having no record, which for a payout that was cancelled is the
#: answer that matters.
LightningPaymentStatus = Literal["Unknown", "Pending", "Complete", "Failed"]


class LightningPayment(TransportModel):
    """One outgoing Lightning payment, keyed by its payment hash.

    The reason this endpoint is called at all: a completed Lightning *payout*
    reports the same amount in `originalAmount` and `payoutAmount`, so the
    routing fee is nowhere on it. It is here instead.

    **Both amounts are millisatoshi**, unlike every other amount in this API,
    and they arrive as strings. Reading `feeAmount` as satoshis would book a
    thousand times the real fee.
    """

    id: str | None = None
    #: Server-owned enum, so a plain string — the same reasoning as
    #: `Payout.state`. An unrecognised status must not raise inside a poller.
    status: str = "Unknown"
    bolt11: str | None = Field(default=None, alias="BOLT11")
    payment_hash: str | None = Field(default=None, alias="paymentHash")
    preimage: str | None = None
    created_at: int | None = Field(default=None, alias="createdAt")
    total_amount: str | int | None = Field(default=None, alias="totalAmount")
    fee_amount: str | int | None = Field(default=None, alias="feeAmount")


class LightningBalance(TransportModel):
    """Channel and on-chain balances of the store's Lightning node.

    Only `offchain.local` is custody for `BTC_LN` purposes: it is the outbound
    liquidity, which is the only part of the node's money that can actually pay
    a withdrawal. On-chain funds in the same node belong to a different float
    and counting them would make the solvency check answer a question nobody
    asked.
    """

    onchain: dict[str, Any] | None = None
    offchain: dict[str, Any] | None = None

    @property
    def local_msat(self) -> str | int | None:
        """Outbound liquidity, in whatever units the node reported."""
        if not self.offchain:
            return None
        value = self.offchain.get("local")
        return value if isinstance(value, str | int) else None
