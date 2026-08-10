"""In-memory BTCPay stand-in.

Implements `BTCPayGateway`, so the service layer cannot tell it from the real
client, and synthesizes webhook payloads with valid signatures so ingress and
handler tests exercise the same bytes BTCPay would send.

The point is not to reimplement BTCPay. It is to make the *sequences* testable:
a payment seen then confirmed, an invoice that expires with money in it, a
delivery replayed eight times, a call that times out with no answer.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from crypto_processing_api.core.addresses import (
    BECH32_CHARSET,
    BECH32_CONST,
    _bech32_hrp_expand,
    _bech32_polymod,
    _convert_bits,
)
from crypto_processing_api.core.signing import compute_btcpay_signature
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayNotFound
from crypto_processing_api.gateway.btcpay_models import (
    Invoice,
    InvoicePaymentMethod,
    Payout,
    StorePaymentMethod,
    WalletOverview,
    WalletTransaction,
)

BTC_METHOD = "BTC-CHAIN"


def regtest_address(seed: str, *, hrp: str = "bcrt") -> str:
    """Mint a real, checksum-valid regtest address.

    The fake has to produce addresses the rest of the service accepts.
    Inventing `bcrt1qdestination0001` would make the address validator reject
    our own deposit addresses for the wrong reason and hide whatever the test
    was actually about.
    """
    program = list(hashlib.sha256(seed.encode()).digest()[:20])
    data = [0, *_convert_bits(program, 8, 5)]
    values = _bech32_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]
    checksum = _bech32_polymod(values) ^ BECH32_CONST
    tail = [(checksum >> 5 * (5 - index)) & 31 for index in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[value] for value in data + tail)


@dataclass
class FakePayment:
    id: str
    value: str
    status: str = "Settled"
    destination: str = ""
    received_date: int = 0


@dataclass
class FakeInvoice:
    id: str
    store_id: str
    metadata: dict[str, Any]
    payment_method_id: str
    destination: str
    currency: str
    created_time: int
    expiration_time: int
    monitoring_expiration: int
    status: str = "New"
    additional_status: str = "None"
    payments: list[FakePayment] = field(default_factory=list)

    def to_model(self) -> Invoice:
        return Invoice.model_validate(
            {
                "id": self.id,
                "storeId": self.store_id,
                "amount": None,
                "currency": self.currency,
                "type": "TopUp",
                "checkoutLink": f"https://btcpay.test/i/{self.id}",
                "createdTime": self.created_time,
                "expirationTime": self.expiration_time,
                "monitoringExpiration": self.monitoring_expiration,
                "status": self.status,
                "additionalStatus": self.additional_status,
                "metadata": self.metadata,
            }
        )

    def to_payment_method(self) -> InvoicePaymentMethod:
        total = sum(
            (Decimal(p.value) for p in self.payments if p.status != "Invalid"), start=Decimal(0)
        )
        return InvoicePaymentMethod.model_validate(
            {
                "paymentMethodId": self.payment_method_id,
                "currency": self.currency,
                "destination": self.destination,
                "rate": "1",
                "totalPaid": f"{total:.8f}",
                "paymentMethodPaid": f"{total:.8f}",
                "due": "0",
                "amount": "0",
                "payments": [
                    {
                        "id": p.id,
                        "receivedDate": p.received_date,
                        "value": p.value,
                        "fee": "0",
                        "status": p.status,
                        "destination": p.destination,
                    }
                    for p in self.payments
                ],
            }
        )


@dataclass
class FakePayout:
    id: str
    destination: str
    amount: str
    payout_method_id: str
    metadata: dict[str, Any]
    state: str = "AwaitingPayment"
    txid: str | None = None

    def to_model(self) -> Payout:
        proof = (
            {"proofType": "PayoutTransactionOnChainBlob", "id": self.txid} if self.txid else None
        )
        return Payout.model_validate(
            {
                "id": self.id,
                "destination": self.destination,
                "originalAmount": self.amount,
                "originalCurrency": "BTC",
                "payoutAmount": self.amount,
                "payoutCurrency": "BTC",
                "payoutMethodId": self.payout_method_id,
                "state": self.state,
                "paymentProof": proof,
                "metadata": self.metadata,
            }
        )


class FakeBTCPay:
    def __init__(
        self,
        *,
        store_id: str = "store-test",
        webhook_secret: str = "test-webhook-secret",
        payment_methods: list[str] | None = None,
    ) -> None:
        self.store_id = store_id
        self.webhook_secret = webhook_secret
        self.payment_methods = payment_methods or [BTC_METHOD]
        self.invoices: dict[str, FakeInvoice] = {}
        self.payouts: dict[str, FakePayout] = {}
        self.fee_rate = 10.0
        self.wallet_transactions: list[dict[str, Any]] = []
        self.redelivered: list[tuple[str, str]] = []
        self.calls: list[str] = []
        #: Set to an exception to make the next call of that name blow up. The
        #: ambiguous-timeout path is only reachable this way.
        self.fail_next: dict[str, BTCPayError] = {}
        self._invoice_ids = itertools.count(1)
        self._payout_ids = itertools.count(1)
        self._payment_ids = itertools.count(1)
        self._delivery_ids = itertools.count(1)

    # -- test helpers -----------------------------------------------------

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        error = self.fail_next.pop(name, None)
        if error is not None:
            raise error

    def add_payment(
        self,
        invoice_id: str,
        value: str,
        *,
        status: str = "Settled",
        payment_id: str | None = None,
    ) -> str:
        invoice = self.invoices[invoice_id]
        identifier = payment_id or f"{'a' * 60}{next(self._payment_ids):04d}-0"
        invoice.payments.append(
            FakePayment(
                id=identifier,
                value=value,
                status=status,
                destination=invoice.destination,
                received_date=int(datetime.now(UTC).timestamp()),
            )
        )
        return identifier

    def settle(self, invoice_id: str, *, additional_status: str = "None") -> None:
        invoice = self.invoices[invoice_id]
        invoice.status = "Settled"
        invoice.additional_status = additional_status
        for payment in invoice.payments:
            if payment.status == "Processing":
                payment.status = "Settled"

    def set_processing(self, invoice_id: str) -> None:
        self.invoices[invoice_id].status = "Processing"

    def expire(self, invoice_id: str, *, additional_status: str = "None") -> None:
        invoice = self.invoices[invoice_id]
        invoice.status = "Expired"
        invoice.additional_status = additional_status

    def invalidate(self, invoice_id: str) -> None:
        invoice = self.invoices[invoice_id]
        invoice.status = "Invalid"
        invoice.additional_status = "Invalid"

    def add_wallet_transaction(self, txid: str, amount: str, *, confirmations: int = 1) -> None:
        self.wallet_transactions.append(
            {
                "transactionHash": txid,
                "amount": amount,
                "confirmations": str(confirmations),
                "status": "Confirmed",
                "timestamp": int(datetime.now(UTC).timestamp()),
            }
        )

    def webhook_payload(
        self,
        event_type: str,
        invoice_id: str,
        *,
        delivery_id: str | None = None,
        original_delivery_id: str | None = None,
        store_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invoice = self.invoices.get(invoice_id)
        payload: dict[str, Any] = {
            "deliveryId": delivery_id or f"del-{next(self._delivery_ids)}",
            "webhookId": "wh-1",
            "originalDeliveryId": original_delivery_id or delivery_id or "",
            "isRedelivery": bool(original_delivery_id),
            "type": event_type,
            "timestamp": int(datetime.now(UTC).timestamp()),
            "storeId": store_id or self.store_id,
            "invoiceId": invoice_id,
            "metadata": invoice.metadata if invoice else {},
        }
        if not payload["originalDeliveryId"]:
            payload["originalDeliveryId"] = payload["deliveryId"]
        if extra:
            payload.update(extra)
        return payload

    def sign(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        """Return the exact bytes and headers BTCPay would send."""
        raw = json.dumps(payload).encode("utf-8")
        return raw, {
            "BTCPay-Sig": compute_btcpay_signature(self.webhook_secret, raw),
            "Content-Type": "application/json",
        }

    # -- BTCPayGateway ----------------------------------------------------

    def create_top_up_invoice(
        self,
        *,
        currency: str,
        metadata: dict[str, Any],
        payment_methods: list[str] | None = None,
        expiration_minutes: int | None = None,
        monitoring_minutes: int | None = None,
        additional_search_terms: list[str] | None = None,
    ) -> Invoice:
        self._maybe_fail("create_top_up_invoice")
        number = next(self._invoice_ids)
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=expiration_minutes or 60)
        invoice = FakeInvoice(
            id=f"inv-{number}",
            store_id=self.store_id,
            metadata=dict(metadata),
            payment_method_id=(payment_methods or self.payment_methods)[0],
            destination=regtest_address(f"invoice-{number}"),
            currency=currency,
            created_time=int(now.timestamp()),
            expiration_time=int(expires.timestamp()),
            monitoring_expiration=int(
                (expires + timedelta(minutes=monitoring_minutes or 1440)).timestamp()
            ),
        )
        self.invoices[invoice.id] = invoice
        return invoice.to_model()

    def get_invoice(self, invoice_id: str) -> Invoice:
        self._maybe_fail("get_invoice")
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            raise BTCPayNotFound(f"no invoice {invoice_id}")
        return invoice.to_model()

    def get_invoice_payment_methods(self, invoice_id: str) -> list[InvoicePaymentMethod]:
        self._maybe_fail("get_invoice_payment_methods")
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            raise BTCPayNotFound(f"no invoice {invoice_id}")
        return [invoice.to_payment_method()]

    def list_invoices(
        self,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        text_search: str | None = None,
        skip: int = 0,
        take: int = 50,
    ) -> list[Invoice]:
        self._maybe_fail("list_invoices")
        rows = list(self.invoices.values())
        if text_search:
            needle = text_search.removeprefix("cpapi:")
            rows = [i for i in rows if i.metadata.get("deposit_id") == needle]
        if start_date is not None:
            rows = [i for i in rows if i.created_time >= start_date]
        return [i.to_model() for i in rows[skip : skip + take]]

    def get_store_payment_methods(self, *, only_enabled: bool = True) -> list[StorePaymentMethod]:
        self._maybe_fail("get_store_payment_methods")
        return [
            StorePaymentMethod.model_validate({"paymentMethodId": pmid, "enabled": True})
            for pmid in self.payment_methods
        ]

    def get_wallet(self, payment_method_id: str) -> WalletOverview:
        self._maybe_fail("get_wallet")
        return WalletOverview.model_validate({"balance": "0", "confirmedBalance": "0"})

    def get_wallet_transactions(
        self, payment_method_id: str, *, skip: int = 0, limit: int = 100
    ) -> list[WalletTransaction]:
        self._maybe_fail("get_wallet_transactions")
        rows = self.wallet_transactions[skip : skip + limit]
        return [WalletTransaction.model_validate(row) for row in rows]

    def redeliver_webhook(self, webhook_id: str, delivery_id: str) -> None:
        self._maybe_fail("redeliver_webhook")
        self.redelivered.append((webhook_id, delivery_id))

    # -- payouts ----------------------------------------------------------

    def get_fee_rate(self, payment_method_id: str, *, block_target: int) -> float:
        self._maybe_fail("get_fee_rate")
        return self.fee_rate

    def create_payout(
        self,
        *,
        destination: str,
        amount: str,
        payout_method_id: str,
        metadata: dict[str, Any],
    ) -> Payout:
        self._maybe_fail("create_payout")
        payout = FakePayout(
            id=f"payout-{next(self._payout_ids)}",
            destination=destination,
            amount=amount,
            payout_method_id=payout_method_id,
            metadata=dict(metadata),
        )
        self.payouts[payout.id] = payout
        return payout.to_model()

    def get_payout(self, payout_id: str) -> Payout:
        self._maybe_fail("get_payout")
        payout = self.payouts.get(payout_id)
        if payout is None:
            raise BTCPayNotFound(f"no payout {payout_id}")
        return payout.to_model()

    def list_payouts(self, *, include_cancelled: bool = False) -> list[Payout]:
        self._maybe_fail("list_payouts")
        return [
            p.to_model()
            for p in self.payouts.values()
            if include_cancelled or p.state != "Cancelled"
        ]

    def cancel_payout(self, payout_id: str) -> bool:
        self._maybe_fail("cancel_payout")
        payout = self.payouts.get(payout_id)
        if payout is None or payout.state in ("InProgress", "Completed"):
            return False
        payout.state = "Cancelled"
        return True

    # -- payout test helpers ---------------------------------------------

    def broadcast_payout(self, payout_id: str, txid: str | None = None) -> str:
        payout = self.payouts[payout_id]
        payout.state = "InProgress"
        payout.txid = txid or f"{payout_id}-txid".ljust(64, "0")
        return payout.txid

    def complete_payout(self, payout_id: str, txid: str | None = None) -> str:
        payout = self.payouts[payout_id]
        if payout.txid is None:
            self.broadcast_payout(payout_id, txid)
        payout.state = "Completed"
        assert payout.txid is not None
        return payout.txid

    def cancel_payout_externally(self, payout_id: str) -> None:
        """An operator clicking cancel in the BTCPay UI."""
        self.payouts[payout_id].state = "Cancelled"

    def create_foreign_payout(self, destination: str, amount: str) -> str:
        """A payout with no withdrawal id — the ambiguity the freeze rule exists for."""
        payout = FakePayout(
            id=f"foreign-{next(self._payout_ids)}",
            destination=destination,
            amount=amount,
            payout_method_id=BTC_METHOD,
            metadata={},
        )
        self.payouts[payout.id] = payout
        return payout.id

    def payout_webhook(
        self, event_type: str, payout_id: str, *, delivery_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "deliveryId": delivery_id or f"del-{next(self._delivery_ids)}",
            "originalDeliveryId": delivery_id or f"del-{next(self._delivery_ids)}",
            "webhookId": "wh-1",
            "isRedelivery": False,
            "type": event_type,
            "timestamp": int(datetime.now(UTC).timestamp()),
            "storeId": self.store_id,
            "payoutId": payout_id,
        }
