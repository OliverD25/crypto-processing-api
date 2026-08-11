"""The client you call.

Everything under `_generated` is produced from the committed OpenAPI document
and is regenerated whenever a route changes. This module is the part codegen
cannot write, and it is deliberately small:

- **Idempotency.** Every mutating call carries an `Idempotency-Key`. The server
  requires one and answers 400 without it, so minting a UUID by default is
  strictly better than making every caller remember. The key is minted once per
  logical call and **reused on every retry of that call**, which is the rule
  `docs/integrating/index.md` puts in bold: a retry with a new key is a second
  deposit, not a retry.
- **Retries.** 503 always, and 409 only when the server sent a `Retry-After` —
  that is how the "your own request is still in flight" 409 is told apart from
  the "this state transition is illegal" 409, which no amount of waiting fixes.
  Transport failures are retried too, which is safe precisely because the key
  is pinned.
- **Errors.** A refusal raises. The generated core returns error bodies as
  ordinary values, and a caller who forgets one check gets a `DepositResponse`
  variable holding an `ErrorResponse`.

Amounts are strings everywhere, in both directions, and this layer never
converts them. `expected_amount` and `amount` are integer smallest units
(`"50000000"`); everything the server returns is a decimal string
(`"0.50000000"`). Turning either into a float is how a satoshi goes missing.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from uuid import UUID

import httpx

from . import errors
from ._generated import AuthenticatedClient
from ._generated.api.admin import (
    admin_approve_withdrawal,
    admin_list_events,
    admin_mark_broadcast,
    admin_reconciliation,
    admin_redeliver_event,
    admin_reject_withdrawal,
    admin_release_withdrawal,
    admin_resolve_deposit,
    admin_review_queue,
    admin_wallet_alerts,
    admin_withdrawal_queue,
)
from ._generated.api.balances import (
    get_address_history,
    get_user_balances,
    get_user_transactions,
    list_assets,
)
from ._generated.api.deposits import create_deposit, get_deposit, list_user_deposits
from ._generated.api.health import healthz, readyz
from ._generated.api.withdrawals import create_withdrawal, get_withdrawal, list_user_withdrawals
from ._generated.models import (
    AddressHistoryResponse,
    AdminDepositQueueResponse,
    AdminResolveDepositResponse,
    AdminWithdrawalListResponse,
    ApproveWithdrawalRequest,
    AssetsResponse,
    BalancesResponse,
    CreateDepositRequest,
    CreateWithdrawalRequest,
    DepositListResponse,
    DepositResponse,
    HealthResponse,
    MarkBroadcastRequest,
    OutboundEventsResponse,
    ReadyResponse,
    ReconciliationResponse,
    RedeliverEventResponse,
    RejectWithdrawalRequest,
    ReleaseWithdrawalRequest,
    ResolveDepositRequest,
    TransactionsResponse,
    WalletAlertsResponse,
    WithdrawalCreatedResponse,
    WithdrawalListResponse,
    WithdrawalResponse,
)
from ._generated.models.resolve_deposit_request_action import ResolveDepositRequestAction
from ._generated.types import UNSET, Response, Unset

ModelT = TypeVar("ModelT")

#: Statuses the retry loop waits on rather than raising. 409 is conditional —
#: see `RetryPolicy.delay_for`.
_RETRYABLE = frozenset({409, 503})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard the client tries before it gives up and raises.

    `attempts` counts the first try. `attempts=1` disables retrying without
    disabling the idempotency key, which is the combination you want when your
    own job runner owns the retrying.
    """

    attempts: int = 3
    #: Used when the server did not send a `Retry-After`, doubled each time.
    backoff_seconds: float = 0.5
    #: A server may ask for a longer wait than a request handler can afford.
    max_delay_seconds: float = 60.0

    def delay_for(self, error: errors.APIError | None, attempt: int) -> float | None:
        """Seconds to wait before try number `attempt + 1`, or None to give up.

        `error` is None for a transport failure, where there is no status and
        no header to read.
        """
        if attempt + 1 >= self.attempts:
            return None
        if error is None:
            return self._backoff(attempt)
        if error.status_code not in _RETRYABLE:
            return None
        if error.status_code == 409 and error.retry_after is None:
            # A 409 with no Retry-After is an illegal state transition — the
            # withdrawal is not in the status this call needs. Waiting will
            # never change that, so raise it now rather than in three seconds.
            return None
        if error.retry_after is not None:
            return min(error.retry_after, self.max_delay_seconds)
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_seconds * (2.0**attempt), self.max_delay_seconds)


class _NonJsonFailure(Exception):
    """An error response the generated parser would choke on.

    A reverse proxy in front of the API answers 502 and 504 with HTML, and the
    generated code calls `.json()` on whatever came back. Catching it at the
    transport level keeps a proxy timeout looking like a `ServerError` instead
    of a `JSONDecodeError` from inside a library the caller never imported.
    """

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"non-JSON error response: {response.status_code}")
        self.response = response


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` is either a number of seconds or an HTTP date."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, target.timestamp() - time.time())


def _detail(parsed: Any, body: bytes) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Pull (message, code, field errors) out of whichever error shape arrived."""
    detail = getattr(parsed, "detail", None)
    if detail is None or isinstance(detail, Unset):
        text = body.decode("utf-8", errors="replace").strip()
        return (text[:500] or "the server gave no reason"), None, []
    if isinstance(detail, str):
        return detail, None, []
    if isinstance(detail, list):
        rendered = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in detail]
        return "the request body was refused; see field_errors", None, rendered
    # The one structured detail: pooled-address exhaustion, which carries a
    # code a caller is meant to branch on.
    code = getattr(detail, "code", None)
    message = getattr(detail, "message", None)
    return (message or "the request was refused"), code, []


class CryptoProcessingClient:
    """A configured connection to one crypto-processing-api deployment.

    ::

        from crypto_processing_client import CryptoProcessingClient

        client = CryptoProcessingClient("https://pay.example.com", api_key="cpk_live_...")
        deposit = client.create_deposit(external_user_id="user-42", asset="BTC")
        print(deposit.address)

    Use it as a context manager, or call `close()`, so the underlying
    connection pool is released. A long-lived client is the intended shape:
    building one per request throws away every kept-alive connection.

    Only the synchronous surface is handwritten. The generated core also has an
    async function for every endpoint — `_generated.api.deposits.create_deposit
    .asyncio_detailed` and so on — but they do not carry the idempotency and
    retry behaviour this class exists for.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        verify_ssl: bool | str = True,
        headers: Mapping[str, str] | None = None,
        httpx_args: Mapping[str, Any] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required, e.g. https://pay.example.com")
        if not api_key:
            raise ValueError("api_key is required; the API refuses unauthenticated calls")
        self.retry = retry or RetryPolicy()
        extra: dict[str, Any] = dict(httpx_args or {})
        hooks: dict[str, list[Any]] = dict(extra.pop("event_hooks", {}))
        hooks["response"] = [_reject_non_json, *hooks.get("response", [])]
        self._client = AuthenticatedClient(
            base_url=base_url.rstrip("/"),
            token=api_key,
            prefix="Bearer",
            timeout=httpx.Timeout(timeout),
            verify_ssl=verify_ssl,
            headers=dict(headers or {}),
            # False on purpose: an undocumented status is this layer's problem,
            # and it raises a typed error of its own below.
            raise_on_unexpected_status=False,
            httpx_args={**extra, "event_hooks": hooks},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.get_httpx_client().close()

    def __enter__(self) -> CryptoProcessingClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- deposits ----------------------------------------------------------

    def create_deposit(
        self,
        *,
        external_user_id: str,
        asset: str,
        expected_amount: str | None = None,
        idempotency_key: str | None = None,
    ) -> DepositResponse:
        """Create a deposit and its invoice. `POST /v1/deposits`.

        `expected_amount` is an integer number of the asset's smallest units,
        as a string. It is display-only for BTC and load-bearing for USDT,
        where a payment far from it goes to an operator instead of being
        credited.

        Pass `idempotency_key` when your own system already has an id for this
        logical operation — an order id, say. Leave it out and a UUID is minted
        for you and reused across every retry of this call.
        """
        return self._invoke(
            create_deposit.sync_detailed,
            DepositResponse,
            body=CreateDepositRequest(
                external_user_id=external_user_id,
                asset=asset,
                expected_amount=UNSET if expected_amount is None else expected_amount,
            ),
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    def get_deposit(self, deposit_id: str | UUID) -> DepositResponse:
        """Read one deposit. `GET /v1/deposits/{deposit_id}`.

        This is the call that tells you what is true. A webhook only tells you
        something changed.
        """
        return self._invoke(get_deposit.sync_detailed, DepositResponse, _uuid(deposit_id))

    def list_user_deposits(
        self, external_user_id: str, *, limit: int = 25, cursor: str | UUID | None = None
    ) -> DepositListResponse:
        """One user's deposits, newest first. `GET /v1/users/{id}/deposits`."""
        return self._invoke(
            list_user_deposits.sync_detailed,
            DepositListResponse,
            external_user_id,
            limit=limit,
            cursor=UNSET if cursor is None else _uuid(cursor),
        )

    def get_address_history(self, deposit_id: str | UUID) -> AddressHistoryResponse:
        """Which deposits have held this deposit's address. Pooled assets only."""
        return self._invoke(
            get_address_history.sync_detailed, AddressHistoryResponse, _uuid(deposit_id)
        )

    # -- withdrawals -------------------------------------------------------

    def create_withdrawal(
        self,
        *,
        external_user_id: str,
        asset: str,
        amount: str,
        destination_address: str,
        idempotency_key: str | None = None,
    ) -> WithdrawalCreatedResponse:
        """Request a withdrawal. `POST /v1/withdrawals`.

        `amount` is the **gross** amount in integer smallest units, as a
        string. The fee comes out of it, so the user receives less than this.

        The hold on the user's balance is placed before you get an answer, so a
        connection that drops mid-call may still have moved money. Retrying
        with the same key is what makes that safe, and this method does it for
        you.
        """
        return self._invoke(
            create_withdrawal.sync_detailed,
            WithdrawalCreatedResponse,
            body=CreateWithdrawalRequest(
                external_user_id=external_user_id,
                asset=asset,
                amount=amount,
                destination_address=destination_address,
            ),
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    def get_withdrawal(self, withdrawal_id: str | UUID) -> WithdrawalResponse:
        """Read one withdrawal. `GET /v1/withdrawals/{withdrawal_id}`."""
        return self._invoke(get_withdrawal.sync_detailed, WithdrawalResponse, _uuid(withdrawal_id))

    def list_user_withdrawals(
        self, external_user_id: str, *, limit: int = 25, cursor: str | UUID | None = None
    ) -> WithdrawalListResponse:
        """One user's withdrawals, newest first. `GET /v1/users/{id}/withdrawals`."""
        return self._invoke(
            list_user_withdrawals.sync_detailed,
            WithdrawalListResponse,
            external_user_id,
            limit=limit,
            cursor=UNSET if cursor is None else _uuid(cursor),
        )

    # -- balances and history ---------------------------------------------

    def get_user_balances(self, external_user_id: str) -> BalancesResponse:
        """Every asset balance for one user. `GET /v1/users/{id}/balances`.

        The API is the source of truth for these. Storing your own copy and
        reconciling it later is the thing this service exists to spare you.
        """
        return self._invoke(get_user_balances.sync_detailed, BalancesResponse, external_user_id)

    def get_user_transactions(
        self,
        external_user_id: str,
        *,
        asset: str | None = None,
        limit: int = 50,
        cursor: int | None = None,
    ) -> TransactionsResponse:
        """One user's ledger movements. `GET /v1/users/{id}/transactions`."""
        return self._invoke(
            get_user_transactions.sync_detailed,
            TransactionsResponse,
            external_user_id,
            asset=UNSET if asset is None else asset,
            limit=limit,
            cursor=UNSET if cursor is None else cursor,
        )

    def list_assets(self) -> AssetsResponse:
        """Which assets this deployment has enabled. `GET /v1/assets`."""
        return self._invoke(list_assets.sync_detailed, AssetsResponse)

    # -- health ------------------------------------------------------------

    def healthz(self) -> HealthResponse:
        """This process and its database. `GET /healthz`. No API key needed."""
        return self._invoke(healthz.sync_detailed, HealthResponse)

    def readyz(self) -> ReadyResponse:
        """Component readiness. `GET /readyz`.

        A degraded deployment answers 503 with the same body naming which
        component is unhappy, and that is a normal answer here rather than an
        exception — read `status` and `components`.
        """
        return self._invoke(readyz.sync_detailed, ReadyResponse, allow_statuses=frozenset({503}))

    # -- operator endpoints ------------------------------------------------

    def admin_review_queue(self, *, limit: int = 50) -> AdminDepositQueueResponse:
        """Payments waiting for a human. `GET /v1/admin/deposits/review`. Needs an admin key."""
        return self._invoke(
            admin_review_queue.sync_detailed, AdminDepositQueueResponse, limit=limit
        )

    def admin_resolve_deposit(
        self,
        deposit_id: str | UUID,
        *,
        action: ResolveDepositRequestAction,
        payment_id: str | None = None,
    ) -> AdminResolveDepositResponse:
        """Credit or dismiss a reviewed payment. `POST /v1/admin/deposits/{id}/resolve`.

        There is no amount to pass: the server asks BTCPay what the payment was
        worth. An operator confirms attribution and nothing else.
        """
        return self._invoke(
            admin_resolve_deposit.sync_detailed,
            AdminResolveDepositResponse,
            _uuid(deposit_id),
            body=ResolveDepositRequest(
                action=action,
                payment_id=UNSET if payment_id is None else payment_id,
            ),
        )

    def admin_withdrawal_queue(
        self, *, status: str | None = None, limit: int = 50
    ) -> AdminWithdrawalListResponse:
        """The approval queue. `GET /v1/admin/withdrawals?status=pending_approval`."""
        return self._invoke(
            admin_withdrawal_queue.sync_detailed,
            AdminWithdrawalListResponse,
            status=UNSET if status is None else status,
            limit=limit,
        )

    def admin_approve_withdrawal(
        self,
        withdrawal_id: str | UUID,
        *,
        note: str | None = None,
    ) -> WithdrawalResponse:
        """Let a held withdrawal proceed. `POST /v1/admin/withdrawals/{id}/approve`."""
        return self._invoke(
            admin_approve_withdrawal.sync_detailed,
            WithdrawalResponse,
            _uuid(withdrawal_id),
            body=ApproveWithdrawalRequest(note=UNSET if note is None else note),
        )

    def admin_reject_withdrawal(
        self,
        withdrawal_id: str | UUID,
        *,
        reason: str | None = None,
    ) -> WithdrawalResponse:
        """Refuse a held withdrawal and return the money. `POST .../reject`."""
        return self._invoke(
            admin_reject_withdrawal.sync_detailed,
            WithdrawalResponse,
            _uuid(withdrawal_id),
            body=RejectWithdrawalRequest(reason=UNSET if reason is None else reason),
        )

    def admin_mark_broadcast(self, withdrawal_id: str | UUID, *, txid: str) -> WithdrawalResponse:
        """Report the transaction an operator sent by hand. `POST .../mark-broadcast`."""
        return self._invoke(
            admin_mark_broadcast.sync_detailed,
            WithdrawalResponse,
            _uuid(withdrawal_id),
            body=MarkBroadcastRequest(txid=txid),
        )

    def admin_release_withdrawal(
        self, withdrawal_id: str | UUID, *, attestation: str
    ) -> WithdrawalResponse:
        """Return a hold after a payout may exist. `POST .../release`.

        `attestation` is a human stating that they looked and the coins are not
        arriving. It is stored on the withdrawal, so the decision has an author.
        """
        return self._invoke(
            admin_release_withdrawal.sync_detailed,
            WithdrawalResponse,
            _uuid(withdrawal_id),
            body=ReleaseWithdrawalRequest(attestation=attestation),
        )

    def admin_reconciliation(self) -> ReconciliationResponse:
        """The latest sweep: what the books say against what the chain says."""
        return self._invoke(admin_reconciliation.sync_detailed, ReconciliationResponse)

    def admin_wallet_alerts(self, *, limit: int = 50) -> WalletAlertsResponse:
        """Coins in the hot wallet that match no known deposit. BTC only."""
        return self._invoke(admin_wallet_alerts.sync_detailed, WalletAlertsResponse, limit=limit)

    def admin_list_events(
        self, *, status: str | None = None, limit: int = 50
    ) -> OutboundEventsResponse:
        """The outbound event queue, including anything dead-lettered."""
        return self._invoke(
            admin_list_events.sync_detailed,
            OutboundEventsResponse,
            status=UNSET if status is None else status,
            limit=limit,
        )

    def admin_redeliver_event(self, event_id: str | UUID) -> RedeliverEventResponse:
        """Put a dead-lettered event back in the queue. `POST /v1/admin/events/{id}/redeliver`."""
        return self._invoke(
            admin_redeliver_event.sync_detailed,
            RedeliverEventResponse,
            _uuid(event_id),
        )

    # -- the machinery -----------------------------------------------------

    def _invoke(
        self,
        call: Callable[..., Response[Any]],
        expect: type[ModelT],
        *args: Any,
        allow_statuses: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> ModelT:
        response = self._request(call, *args, allow_statuses=allow_statuses, **kwargs)
        parsed = response.parsed
        if isinstance(parsed, expect):
            return parsed
        raise errors.ServerError(
            int(response.status_code),
            f"expected a {expect.__name__} and the body did not parse as one",
            body=response.content,
        )

    def _request(
        self,
        call: Callable[..., Response[Any]],
        *args: Any,
        allow_statuses: frozenset[int],
        **kwargs: Any,
    ) -> Response[Any]:
        """One logical call, retried in place with the key it started with."""
        attempt = 0
        while True:
            failure: errors.APIError | None = None
            try:
                response = call(*args, client=self._client, **kwargs)
            except _NonJsonFailure as exc:
                failure = _api_error(
                    exc.response.status_code, exc.response.headers, exc.response.content, None
                )
            except httpx.TransportError as exc:
                delay = self.retry.delay_for(None, attempt)
                if delay is None:
                    raise errors.TransportError(str(exc)) from exc
                time.sleep(delay)
                attempt += 1
                continue

            if failure is None:
                status = int(response.status_code)
                if status < 400 or status in allow_statuses:
                    return response
                failure = _api_error(status, response.headers, response.content, response.parsed)

            delay = self.retry.delay_for(failure, attempt)
            if delay is None:
                raise failure
            time.sleep(delay)
            attempt += 1


def _api_error(
    status: int, headers: Mapping[str, str], body: bytes, parsed: Any
) -> errors.APIError:
    message, code, field_errors = _detail(parsed, body)
    return errors.error_class(status)(
        status,
        message,
        code=code,
        retry_after=_retry_after(headers),
        field_errors=field_errors,
        body=body,
    )


def _reject_non_json(response: httpx.Response) -> None:
    """Stop a proxy's HTML error page from reaching the generated JSON parser."""
    if response.status_code < 400:
        return
    if response.headers.get("content-type", "").split(";")[0].strip() == "application/json":
        return
    response.read()
    raise _NonJsonFailure(response)


def _uuid(value: str | UUID) -> UUID:
    """The generated core wants a UUID; callers usually have the string form."""
    return value if isinstance(value, UUID) else UUID(value)


__all__ = ["CryptoProcessingClient", "RetryPolicy"]
