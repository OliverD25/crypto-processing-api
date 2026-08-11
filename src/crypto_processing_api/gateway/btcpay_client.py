"""Sync BTCPay Greenfield client.

No SDK: the official Python package targets the legacy BitPay API, and the
third-party Greenfield one is unvetted. This is a thin typed wrapper over
`httpx.Client`.

The retry policy is the important part. GETs retry three times with
exponential backoff and jitter, because they are idempotent and BTCPay behind
its own proxy can be slow on a cold NBXplorer query. **POSTs are never retried
here.** A retried invoice creation on an ambiguous timeout creates two
invoices, and the second one is a deposit address nobody is watching. Ambiguity
is resolved one layer up instead: our deposit id is in the invoice metadata and
in `additionalSearchTerms`, the row stays `creating`, and the service adopts
whatever BTCPay actually made.
"""

from __future__ import annotations

import random
import time
from typing import Any, Protocol, runtime_checkable

import httpx

from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_models import (
    FeeRate,
    Invoice,
    InvoicePaymentMethod,
    LightningBalance,
    LightningPayment,
    Payout,
    StorePaymentMethod,
    WalletOverview,
    WalletTransaction,
)

logger = get_logger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
GET_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 2.0, 8.0)


class BTCPayError(Exception):
    """Base class. `retryable` says whether a caller may try the same call again."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BTCPayUnavailable(BTCPayError):
    """Connect error, timeout, or 502/503/504."""

    retryable = True


class BTCPayRateLimited(BTCPayError):
    """429. `retry_after` carries the server's hint when it sent one."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class BTCPayServerError(BTCPayError):
    retryable = True


class BTCPayAuthError(BTCPayError):
    """401/403 — a configuration bug, never a transient one."""


class BTCPayNotFound(BTCPayError):
    """404 — the caller decides whether that is fatal."""


class BTCPayValidation(BTCPayError):
    """400/422 — our request was wrong."""


@runtime_checkable
class BTCPayGateway(Protocol):
    """What the service layer is allowed to assume about BTCPay.

    `FakeBTCPay` in the test suite implements this, so nothing outside the
    wiring module ever names `BTCPayClient`.
    """

    store_id: str

    def create_top_up_invoice(
        self,
        *,
        currency: str,
        metadata: dict[str, Any],
        payment_methods: list[str] | None = None,
        expiration_minutes: int | None = None,
        monitoring_minutes: int | None = None,
        additional_search_terms: list[str] | None = None,
    ) -> Invoice: ...

    def get_invoice(self, invoice_id: str) -> Invoice: ...

    def get_invoice_payment_methods(self, invoice_id: str) -> list[InvoicePaymentMethod]: ...

    def list_invoices(
        self,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        text_search: str | None = None,
        skip: int = 0,
        take: int = 50,
    ) -> list[Invoice]: ...

    def get_store_payment_methods(
        self, *, only_enabled: bool = True
    ) -> list[StorePaymentMethod]: ...

    def get_wallet(self, payment_method_id: str) -> WalletOverview: ...

    def get_fee_rate(self, payment_method_id: str, *, block_target: int) -> float: ...

    def create_payout(
        self,
        *,
        destination: str,
        amount: str,
        payout_method_id: str,
        metadata: dict[str, Any],
    ) -> Payout: ...

    def get_payout(self, payout_id: str) -> Payout: ...

    def list_payouts(self, *, include_cancelled: bool = False) -> list[Payout]: ...

    def cancel_payout(self, payout_id: str) -> bool: ...

    def get_wallet_transactions(
        self, payment_method_id: str, *, skip: int = 0, limit: int = 100
    ) -> list[WalletTransaction]: ...

    def get_lightning_balance(self, crypto_code: str) -> LightningBalance: ...

    def get_lightning_payment(self, crypto_code: str, payment_hash: str) -> LightningPayment: ...

    def redeliver_webhook(self, webhook_id: str, delivery_id: str) -> None: ...


class BTCPayClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        store_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.store_id = store_id
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"token {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT
            ),
        )

    def close(self) -> None:
        self._client.close()

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        code = response.status_code
        if code < 400:
            return
        # Response bodies can echo request content; only the status and path
        # are safe to put in an exception message.
        if code in (502, 503, 504):
            raise BTCPayUnavailable(f"BTCPay unavailable on {path}", status_code=code)
        if code == 429:
            header = response.headers.get("Retry-After")
            raise BTCPayRateLimited(
                f"BTCPay rate limited {path}",
                retry_after=float(header) if header and header.isdigit() else None,
            )
        if code in (401, 403):
            raise BTCPayAuthError(f"BTCPay rejected our credentials on {path}", status_code=code)
        if code == 404:
            raise BTCPayNotFound(f"BTCPay has no {path}", status_code=code)
        if code in (400, 422):
            raise BTCPayValidation(
                f"BTCPay rejected our request to {path}: {response.text[:300]}", status_code=code
            )
        raise BTCPayServerError(f"BTCPay returned {code} on {path}", status_code=code)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BTCPayUnavailable(f"timeout on {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise BTCPayUnavailable(f"{type(exc).__name__} on {method} {path}") from exc
        self._raise_for_status(response, path)
        return response

    def _get(self, path: str, **kwargs: Any) -> Any:
        last: BTCPayError | None = None
        for attempt in range(GET_ATTEMPTS):
            try:
                return self._send("GET", path, **kwargs).json()
            except BTCPayError as exc:
                if not exc.retryable or attempt == GET_ATTEMPTS - 1:
                    raise
                last = exc
                delay = BACKOFF_SECONDS[attempt]
                if isinstance(exc, BTCPayRateLimited) and exc.retry_after:
                    delay = max(delay, exc.retry_after)
                # Jitter so several worker jobs recovering together do not
                # retry in lockstep.
                time.sleep(delay * (0.75 + random.random() * 0.5))  # noqa: S311
                logger.warning("btcpay.retry", path=path, attempt=attempt + 1, error=str(exc))
        raise last or BTCPayUnavailable(f"GET {path} failed")

    def _post(self, path: str, **kwargs: Any) -> Any:
        """Single attempt, always. See the module docstring."""
        response = self._send("POST", path, **kwargs)
        return response.json() if response.content else None

    # -- invoices ---------------------------------------------------------

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
        """Create an invoice with no amount.

        A top-up invoice treats any payment as a full payment, which removes
        the whole `paidPartial` limbo: the ledger credits what arrived, not
        what was quoted.
        """
        checkout: dict[str, Any] = {}
        if payment_methods:
            checkout["paymentMethods"] = payment_methods
            checkout["defaultPaymentMethod"] = payment_methods[0]
        if expiration_minutes is not None:
            checkout["expirationMinutes"] = expiration_minutes
        if monitoring_minutes is not None:
            checkout["monitoringMinutes"] = monitoring_minutes

        body: dict[str, Any] = {"currency": currency, "metadata": metadata}
        if checkout:
            body["checkout"] = checkout
        if additional_search_terms:
            body["additionalSearchTerms"] = additional_search_terms

        payload = self._post(f"/api/v1/stores/{self.store_id}/invoices", json=body)
        return Invoice.model_validate(payload)

    def get_invoice(self, invoice_id: str) -> Invoice:
        # Not store-scoped on 2.4.2 — the design documents predate that move.
        return Invoice.model_validate(self._get(f"/api/v1/invoices/{invoice_id}"))

    def get_invoice_payment_methods(self, invoice_id: str) -> list[InvoicePaymentMethod]:
        payload = self._get(f"/api/v1/invoices/{invoice_id}/payment-methods")
        return [InvoicePaymentMethod.model_validate(item) for item in payload]

    def list_invoices(
        self,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        text_search: str | None = None,
        skip: int = 0,
        take: int = 50,
    ) -> list[Invoice]:
        params: dict[str, Any] = {"skip": skip, "take": take}
        if start_date is not None:
            params["startDate"] = start_date
        if end_date is not None:
            params["endDate"] = end_date
        if text_search:
            params["textSearch"] = text_search
        payload = self._get(f"/api/v1/stores/{self.store_id}/invoices", params=params)
        return [Invoice.model_validate(item) for item in payload]

    # -- store and wallet -------------------------------------------------

    def get_store_payment_methods(self, *, only_enabled: bool = True) -> list[StorePaymentMethod]:
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/payment-methods",
            params={"onlyEnabled": str(only_enabled).lower()},
        )
        return [StorePaymentMethod.model_validate(item) for item in payload]

    def get_wallet(self, payment_method_id: str) -> WalletOverview:
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/payment-methods/{payment_method_id}/wallet"
        )
        return WalletOverview.model_validate(payload)

    def get_wallet_transactions(
        self, payment_method_id: str, *, skip: int = 0, limit: int = 100
    ) -> list[WalletTransaction]:
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/payment-methods/{payment_method_id}"
            "/wallet/transactions",
            params={"skip": skip, "limit": limit},
        )
        return [WalletTransaction.model_validate(item) for item in payload]

    def get_fee_rate(self, payment_method_id: str, *, block_target: int) -> float:
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/payment-methods/{payment_method_id}/wallet/feerate",
            params={"blockTarget": block_target},
        )
        return float(FeeRate.model_validate(payload).fee_rate)

    # -- lightning --------------------------------------------------------
    #
    # These two take a crypto code ("BTC"), not a payment method id ("BTC-LN").
    # Both need `btcpay.store.canuselightningnode`, which a deployment only
    # grants when it has turned Lightning on — see docs/operating/security.md.

    def get_lightning_balance(self, crypto_code: str) -> LightningBalance:
        payload = self._get(f"/api/v1/stores/{self.store_id}/lightning/{crypto_code}/balance")
        return LightningBalance.model_validate(payload)

    def get_lightning_payment(self, crypto_code: str, payment_hash: str) -> LightningPayment:
        """One outgoing payment, or `BTCPayNotFound` if the node has none.

        The 404 is not an error condition here — it is the answer. A node with
        no record of a payment hash never sent that payment, which is what lets
        a cancelled payout's hold be released without a human.
        """
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/lightning/{crypto_code}/payments/{payment_hash}"
        )
        return LightningPayment.model_validate(payload)

    # -- payouts ----------------------------------------------------------

    def create_payout(
        self,
        *,
        destination: str,
        amount: str,
        payout_method_id: str,
        metadata: dict[str, Any],
    ) -> Payout:
        """Create an already-approved payout.

        `approved: true` because the approval decision belongs to this service,
        not to BTCPay: our limits and velocity caps have already run, and
        BTCPay's own AwaitingApproval stage would just be a second queue nobody
        watches.

        Verified against the running 2.4.2: `metadata` is echoed back on
        GET /api/v1/payouts/{id} and in the store payout list, which is what
        makes `metadata.withdrawal_id` a correlation key rather than a
        convenience.
        """
        payload = self._post(
            f"/api/v1/stores/{self.store_id}/payouts",
            json={
                "destination": destination,
                "amount": amount,
                "payoutMethodId": payout_method_id,
                "approved": True,
                "metadata": metadata,
            },
        )
        return Payout.model_validate(payload)

    def get_payout(self, payout_id: str) -> Payout:
        return Payout.model_validate(self._get(f"/api/v1/payouts/{payout_id}"))

    def list_payouts(self, *, include_cancelled: bool = False) -> list[Payout]:
        payload = self._get(
            f"/api/v1/stores/{self.store_id}/payouts",
            params={"includeCancelled": str(include_cancelled).lower()},
        )
        return [Payout.model_validate(item) for item in payload]

    def cancel_payout(self, payout_id: str) -> bool:
        """Best effort. BTCPay refuses once the payout is already in flight."""
        try:
            self._send("DELETE", f"/api/v1/payouts/{payout_id}")
        except (BTCPayValidation, BTCPayNotFound) as exc:
            logger.warning("btcpay.cancel_refused", payout=payout_id, error=str(exc))
            return False
        return True

    def upsert_payout_processor(
        self,
        payout_method_id: str,
        *,
        interval_seconds: int,
        fee_target_block: int,
        process_new_payouts_instantly: bool,
        threshold: str = "0",
    ) -> None:
        """Configure the automated on-chain sender. Requires a hot wallet."""
        self._send(
            "PUT",
            f"/api/v1/stores/{self.store_id}/payout-processors/"
            f"OnChainAutomatedPayoutSenderFactory/{payout_method_id}",
            json={
                "intervalSeconds": interval_seconds,
                "feeTargetBlock": fee_target_block,
                "threshold": threshold,
                "processNewPayoutsInstantly": process_new_payouts_instantly,
            },
        )

    def redeliver_webhook(self, webhook_id: str, delivery_id: str) -> None:
        """Used by the regtest replay drill; BTCPay reuses `originalDeliveryId`."""
        self._post(f"/api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver")


def build_client(
    *, base_url: str | None, api_key: str | None, store_id: str | None
) -> BTCPayClient:
    if not base_url or not api_key or not store_id:
        raise BTCPayValidation(
            "BTCPAY_URL, BTCPAY_API_KEY and BTCPAY_STORE_ID must all be set. "
            "For the regtest stack, run scripts/bootstrap_btcpay.py and load "
            ".env.regtest.generated."
        )
    return BTCPayClient(base_url=base_url, api_key=api_key, store_id=store_id)
