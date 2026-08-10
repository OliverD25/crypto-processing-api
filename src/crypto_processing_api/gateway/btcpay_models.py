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

from pydantic import BaseModel, ConfigDict, Field

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
    status: InvoiceStatus
    additional_status: InvoiceAdditionalStatus = Field(default="None", alias="additionalStatus")
    archived: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    checkout: CheckoutOptions | None = None


class Payment(TransportModel):
    id: str
    received_date: int | None = Field(default=None, alias="receivedDate")
    value: str
    fee: str | None = None
    status: PaymentStatus
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
