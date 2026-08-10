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
    LightningBalance,
    LightningPayment,
    Payout,
    StorePaymentMethod,
    WalletOverview,
    WalletTransaction,
)

BTC_METHOD = "BTC-CHAIN"
LN_METHOD = "BTC-LN"


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


def mint_bolt11(
    *,
    amount_sat: int | None = None,
    expiry_seconds: int = 3600,
    timestamp: int | None = None,
    payment_hash: str | None = None,
    prefix: str = "bcrt",
) -> str:
    """Build a decodable BOLT11 invoice with the properties a test needs.

    The committed corpus has real invoices from a real LND, and they are what
    the decoder is checked against. They cannot be used for anything about
    *time*, though: they were signed on a particular afternoon and are long
    expired, so "an invoice that is still alive" is a thing only a minted one
    can be.

    The signature bytes are filler. Nothing in this service verifies a BOLT11
    signature — BTCPay's node does that when it pays — so what has to be real
    here is the checksum, the amount encoding and the tagged fields, and all
    three are.
    """
    digest = payment_hash or hashlib.sha256(f"{amount_sat}:{expiry_seconds}".encode()).hexdigest()
    now = timestamp if timestamp is not None else int(datetime.now(UTC).timestamp())

    amount_part = ""
    if amount_sat is not None:
        # `n` is nano-bitcoin, a hundred millisatoshi, so one satoshi is ten of
        # them. Chosen over `u` because it can express any whole satoshi.
        amount_part = f"{amount_sat * 10}n"

    data = [(now >> (5 * (6 - index))) & 31 for index in range(7)]
    data += _tag("p", [((int(digest, 16) << 4) >> (5 * (51 - i))) & 31 for i in range(52)])
    data += _tag("x", _minimal_groups(expiry_seconds))
    data += [0] * 104  # where a 65-byte signature would be

    hrp = f"ln{prefix}{amount_part}"
    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0] * 6) ^ BECH32_CONST
    tail = [(checksum >> 5 * (5 - index)) & 31 for index in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[value] for value in data + tail)


def _tag(character: str, value: list[int]) -> list[int]:
    length = len(value)
    return [BECH32_CHARSET.index(character), length >> 5, length & 31, *value]


def _minimal_groups(value: int) -> list[int]:
    groups: list[int] = []
    while value:
        groups.insert(0, value & 31)
        value >>= 5
    return groups or [0]


#: A real regtest invoice from the committed corpus, used as the destination
#: FakeBTCPay hands out for a Lightning deposit. Constant rather than minted
#: per invoice because a BOLT11 is not an address: there is nothing to derive.
FAKE_LN_DESTINATION = (
    "lnbcrt1p485svwpp5fa66sdzy6dqcd3avav4qthcf3w06lf4rrrrksynlaf3uy8zepz9qdph2pskjepqw3hjq"
    "cmsv9cxjttjv4nhgetnwsszsnmjv3jhygzfgsazq2gcqzzsxqrrswsp5khgq3pqrzal42yp655s33mrp9yvky"
    "vaxvpkqsm2kveys7j27rpts9qxpqysgqrkx8xfl3efv2hzjfumfdltpvwdxy0wg94d2alrftrj2xkxkrw30zs"
    "gucquafa5y6rehts7098xylnx4fkek6ehctq0zr24ke5a4fh9gps0yzu8"
)


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
        self.wallet_balance = "1.00000000"
        self.wallet_transactions: list[dict[str, Any]] = []
        #: Outgoing Lightning payments the node knows about, keyed by payment
        #: hash. Absent means BTCPay answers 404, which is the whole point: the
        #: node having no record is the proof a payout never left.
        self.lightning_payments: dict[str, dict[str, Any]] = {}
        #: Millisatoshi, as BTCPay reports channel balances.
        self.lightning_local_msat = "500000000"
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
        method = (payment_methods or self.payment_methods)[0]
        invoice = FakeInvoice(
            id=f"inv-{number}",
            store_id=self.store_id,
            metadata=dict(metadata),
            payment_method_id=method,
            # Lightning hands out a payment request, not an address, and the
            # difference reaches `deposits.address` unchanged.
            destination=(
                FAKE_LN_DESTINATION if method == LN_METHOD else regtest_address(f"invoice-{number}")
            ),
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
        return WalletOverview.model_validate(
            {"balance": self.wallet_balance, "confirmedBalance": self.wallet_balance}
        )

    def get_wallet_transactions(
        self, payment_method_id: str, *, skip: int = 0, limit: int = 100
    ) -> list[WalletTransaction]:
        self._maybe_fail("get_wallet_transactions")
        rows = self.wallet_transactions[skip : skip + limit]
        return [WalletTransaction.model_validate(row) for row in rows]

    def redeliver_webhook(self, webhook_id: str, delivery_id: str) -> None:
        self._maybe_fail("redeliver_webhook")
        self.redelivered.append((webhook_id, delivery_id))

    # -- lightning --------------------------------------------------------

    def get_lightning_balance(self, crypto_code: str) -> LightningBalance:
        self._maybe_fail("get_lightning_balance")
        return LightningBalance.model_validate(
            {
                "onchain": {"confirmed": "0", "unconfirmed": "0", "reserved": "0"},
                "offchain": {
                    "opening": "0",
                    "local": self.lightning_local_msat,
                    "remote": "1000000",
                    "closing": "0",
                },
            }
        )

    def get_lightning_payment(self, crypto_code: str, payment_hash: str) -> LightningPayment:
        self._maybe_fail("get_lightning_payment")
        payment = self.lightning_payments.get(payment_hash)
        if payment is None:
            raise BTCPayNotFound(f"no lightning payment {payment_hash}")
        return LightningPayment.model_validate(payment)

    def record_lightning_payment(
        self,
        payment_hash: str,
        *,
        status: str = "Complete",
        total_msat: int = 0,
        fee_msat: int | None = 0,
    ) -> None:
        """Teach the node about one outgoing payment.

        `fee_msat=None` records a payment the node reports without a fee, which
        is the case that must book the estimate rather than guess at zero.
        """
        body: dict[str, Any] = {
            "paymentHash": payment_hash,
            "status": status,
            "totalAmount": str(total_msat),
        }
        if fee_msat is not None:
            body["feeAmount"] = str(fee_msat)
        self.lightning_payments[payment_hash] = body

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
