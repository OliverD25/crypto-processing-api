"""Response models for every route.

These exist so `/openapi.json` describes what comes back, which is what makes a
generated SDK typed instead of `Any`. They are **descriptions of an existing
wire format, not a new one**, and two rules keep them that way:

1. **Every amount and every timestamp is typed `str`.** The serializers emit
   `from_units(...)` decimal strings and `datetime.isoformat()` with a
   `+00:00` offset. Typing those fields as `Decimal` or `datetime` would let
   Pydantic re-serialize them — a decimal string becomes a JSON number, and
   `+00:00` becomes `Z` — and every client that parses either would break on a
   release that promised no wire change.
2. **Field order matches the serializer's dict order, key for key.** FastAPI
   dumps a model in field-declaration order, so reordering a field here
   reorders it on the wire.

`tests/integration/test_wire_bytes.py` compares raw response bytes against a
corpus captured before these models existed. That test, not review, is what
enforces both rules.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """The error envelope. Every failure that is not a request-shape failure."""

    detail: str = Field(description="Human-readable reason. Not a stable machine identifier.")


class ValidationErrorResponse(BaseModel):
    """A 422 has two sources and two shapes.

    A business rule (a dust amount, a destination the asset's validator
    refuses, an idempotency key reused with a different body) answers with a
    string. A request that does not fit the request model answers with
    FastAPI's list of field errors.
    """

    detail: str | list[dict[str, Any]]


class DepositUnavailableDetail(BaseModel):
    """The one error with a machine-readable code.

    A pooled asset hands out addresses from a fixed set. When they are all
    reserved the answer is temporary, so it carries a code the caller can
    branch on and a `Retry-After` header, rather than looking like a bad
    request.
    """

    code: str = Field(examples=["DEPOSIT_TEMPORARILY_UNAVAILABLE"])
    message: str


class DepositUnavailableResponse(BaseModel):
    """Deposit creation answers 503 with two different shapes.

    Pool exhaustion carries the structured detail above. An unreachable BTCPay
    (`deposits.py`, the ambiguous `BTCPayError` branch) carries a plain string,
    because there is nothing machine-readable to say about it. Both are
    retryable with the same key.

    The union is here because a generated client parses this schema literally:
    typed as the structured detail alone, every SDK crashes on the string
    branch — which is the branch that fires when BTCPay is down, i.e. exactly
    when the platform most needs a usable error.
    """

    detail: DepositUnavailableDetail | str


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------


class DepositPaymentResponse(BaseModel):
    """One on-chain payment against a deposit invoice."""

    payment_id: str
    amount: str = Field(description="Decimal string in the asset's display units.")
    credited: bool
    credited_at: str | None
    after_expiration: bool = Field(
        description="The payment arrived after the invoice expired, so a human decided it."
    )
    resolved_by: str | None = Field(
        description="`auto`, or the api-key id of the admin who resolved it."
    )


class DepositResponse(BaseModel):
    deposit_id: str
    external_user_id: str
    asset: str
    status: str = Field(
        description="creating, pending, confirming, settled, expired, review, dismissed, failed."
    )
    address: str | None = Field(
        description="What the user pays. An on-chain address, or a BOLT11 invoice for BTC_LN."
    )
    checkout_link: str | None
    expires_at: str | None
    address_reserved_until: str | None = Field(
        description="Pooled assets only: when this address goes back to the pool."
    )
    amount_expected: str | None = Field(
        description="Display only for BTC. Load-bearing for USDT: a payment far from it "
        "goes to an operator instead of being credited."
    )
    amount_credited: str
    created_at: str
    payments: list[DepositPaymentResponse]


class DepositListResponse(BaseModel):
    deposits: list[DepositResponse]
    next_cursor: str | None = Field(
        description="Keyset cursor. `null` on the last page — a page boundary cannot "
        "shift under an insert."
    )


class AddressReservationResponse(BaseModel):
    deposit_id: str
    external_user_id: str
    status: str
    reserved_from: str | None
    reserved_until: str | None


class AddressHistoryResponse(BaseModel):
    """Who owned a pooled address, and when. The USDT attribution query."""

    address: str | None
    reservations: list[AddressReservationResponse]


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------


class WithdrawalResponse(BaseModel):
    withdrawal_id: str
    external_user_id: str
    asset: str
    status: str = Field(
        description="requested, pending_approval, approved, rejected, submitting, "
        "submitted, broadcast, confirmed, failed, refunded."
    )
    destination_address: str
    amount_gross: str
    fee: str | None = Field(description="`null` until submission, when the fee is fixed.")
    amount_net: str | None = Field(description="`null` until submission.")
    approval_mode: str | None = Field(description="`auto` or `manual`.")
    txid: str | None
    failure_reason: str | None
    created_at: str
    updated_at: str


class WithdrawalCreatedResponse(WithdrawalResponse):
    """The 201 body. `approval_reason` is present only when a gate fired."""

    approval_reason: str | None = Field(
        default=None,
        description="Which gate sent this to the approval queue: the per-withdrawal "
        "auto-approval limit, the rolling 24-hour cap, or a per-user cap.",
    )


class WithdrawalListResponse(BaseModel):
    withdrawals: list[WithdrawalResponse]
    next_cursor: str | None


class AdminWithdrawalResponse(WithdrawalResponse):
    """The queue view. Everything above plus who decided what."""

    approved_by: str | None
    rejected_by: str | None
    released_by: str | None
    release_attestation: str | None
    backend_ref: str | None


class AdminWithdrawalListResponse(BaseModel):
    withdrawals: list[AdminWithdrawalResponse]


# ---------------------------------------------------------------------------
# Balances, history, reference data
# ---------------------------------------------------------------------------


class BalanceResponse(BaseModel):
    asset: str
    available: str
    held: str = Field(
        description="Reserved by a withdrawal. Held money physically sits in a "
        "different ledger account, so this is not arithmetic on a side table."
    )
    total: str


class BalancesResponse(BaseModel):
    external_user_id: str
    balances: list[BalanceResponse]


class TransactionResponse(BaseModel):
    posting_id: int
    entry_id: int
    kind: str
    asset: str
    account: str = Field(description="`user_available` or `user_hold`.")
    amount: str
    direction: str = Field(
        description="`credit` or `debit`, from the user's point of view: a deposit "
        "reads positive and a withdrawal hold negative."
    )
    source_ref: str | None
    memo: str | None
    created_at: str


class TransactionsResponse(BaseModel):
    transactions: list[TransactionResponse]
    next_cursor: int | None


class AssetResponse(BaseModel):
    asset: str
    display_name: str
    decimals: int
    unit_name: str
    enabled: bool
    withdrawal_min: str
    withdrawal_auto_limit: str
    withdrawal_daily_cap: str
    withdrawal_flat_fee: str


class AssetsResponse(BaseModel):
    """Read this rather than hardcoding: an operator can change a limit without a deploy."""

    assets: list[AssetResponse]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(description="`ok`, or `unhealthy` with a 503.")
    database: str
    version: str


class ComponentResponse(BaseModel):
    name: str
    status: str = Field(description="`ok` or `degraded`.")
    detail: str | None = Field(
        default=None, description="Present only when the component is degraded, or for the workers."
    )


class ReadyResponse(BaseModel):
    status: str = Field(description="`ready`, or `degraded` with a 503.")
    components: list[ComponentResponse]


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class AdminDepositQueueResponse(BaseModel):
    deposits: list[DepositResponse]


class AdminResolveDepositResponse(BaseModel):
    deposit: DepositResponse
    credited: str = Field(
        description="What the server asked BTCPay the payment was worth. There is "
        "deliberately no amount field on the request."
    )


class WalletAlertResponse(BaseModel):
    id: int
    asset: str
    txid: str
    amount: str
    confirmations: int | None
    detected_at: str
    note: str | None


class WalletAlertsResponse(BaseModel):
    alerts: list[WalletAlertResponse]


class OutboundEventResponse(BaseModel):
    id: str = Field(description="The `evt_`-prefixed id the platform dedups on.")
    raw_id: str
    type: str
    status: str = Field(description="`pending`, `delivered` or `dead`.")
    attempts: int
    next_attempt_at: str
    last_error: str | None
    created_at: str
    payload: dict[str, Any] = Field(
        description="The event body. Its shape per event type is in "
        "`docs/reference/webhook-events.json`."
    )


class OutboundEventsResponse(BaseModel):
    events: list[OutboundEventResponse]


class RedeliverEventResponse(BaseModel):
    id: str
    status: str


class CustodyLineResponse(BaseModel):
    """One asset's answer to: is there still enough on chain to cover what users are owed?"""

    asset: str
    ledger_custody: str
    ledger_in_flight: str
    user_obligations: str
    chain_balance: str | None = Field(
        description="`null` means the source could not be reached. Not the same as zero."
    )
    chain_source: str
    expected_shortfall: str = Field(
        description="Derived from in-flight postings, not a tuned epsilon."
    )
    difference: str | None = Field(description="Chain minus what the ledger says. Negative is bad.")
    insolvent: bool


class ReconciliationResponse(BaseModel):
    healthy: bool
    ledger_consistent: bool
    materialized_vs_derived_drifts: list[str]
    unbalanced_entries: list[str]
    alerts: list[str]
    custody: list[CustodyLineResponse]


# ---------------------------------------------------------------------------
# BTCPay ingress
# ---------------------------------------------------------------------------


class WebhookAckResponse(BaseModel):
    """Ack then process. `reason` is present only when the event was ignored."""

    status: str = Field(description="`accepted` or `ignored`.")
    reason: str | None = None


# ---------------------------------------------------------------------------
# Documented failures
# ---------------------------------------------------------------------------

#: What each status code means here, written once so the spec says the same
#: thing on every route that can return it. `docs/api.md` carries the same
#: table for humans.
ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "The `Idempotency-Key` header is missing or malformed.",
    401: "Missing, malformed, revoked or expired API key.",
    402: "Not enough available balance. Held funds do not count.",
    403: "A valid key with the wrong scope.",
    404: "No such resource, or no such asset.",
    409: "A request with this `Idempotency-Key` is still in flight, or the "
    "state transition is illegal from the current status.",
    422: "A business rule refused the request, or the request body does not fit "
    "the model, or an `Idempotency-Key` was reused with a different body.",
    502: "BTCPay answered and refused. The intent is dead; a retry will not help.",
    503: "A dependency is unreachable, or the asset is unavailable. Retryable "
    "with the **same** `Idempotency-Key`.",
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` entries for the failures a route can actually return."""
    documented: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        model = ValidationErrorResponse if code == 422 else ErrorResponse
        documented[code] = {"model": model, "description": ERROR_DESCRIPTIONS[code]}
    return documented


#: The pooled-address 503 is the only failure with a machine-readable code, so
#: it replaces the generic entry rather than sitting beside it.
DEPOSIT_UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {
        "model": DepositUnavailableResponse,
        "description": "Every address in the pool is reserved, or BTCPay is unreachable. "
        "Retryable with the **same** `Idempotency-Key`; honour `Retry-After`.",
    }
}
