"""The facade, against a transport that answers whatever a test needs.

Nothing here talks to a server. What is being checked is the behaviour codegen
cannot produce: that a key is minted, that a retry reuses it, that a refusal
raises something a caller can branch on, and that a proxy's HTML error page
does not surface as a JSON parse error from inside httpx.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from crypto_processing_client import (
    AuthenticationError,
    BadRequestError,
    ConflictError,
    CryptoProcessingClient,
    NotFoundError,
    PermissionDeniedError,
    RetryPolicy,
    ServerError,
    ServiceUnavailableError,
    TransportError,
    UpstreamRefusedError,
    ValidationError,
)

BASE_URL = "https://pay.example.test"
API_KEY = "cpk_live_not_a_real_key"

DEPOSIT_BODY: dict[str, Any] = {
    "deposit_id": "019feb96-7e52-771a-a8cb-a86dccc87339",
    "external_user_id": "user-42",
    "asset": "BTC",
    "status": "pending",
    "address": "bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq",
    "checkout_link": "https://btcpay.example.test/i/JRr",
    "expires_at": "2026-08-10T13:15:13+00:00",
    "address_reserved_until": None,
    "amount_expected": "0.50000000",
    "amount_credited": "0.00000000",
    "created_at": "2026-08-10T12:15:13+00:00",
    "payments": [],
}


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **(headers or {})},
    )


def build(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    retry: RetryPolicy | None = None,
) -> CryptoProcessingClient:
    return CryptoProcessingClient(
        BASE_URL,
        API_KEY,
        retry=retry or RetryPolicy(attempts=3, backoff_seconds=0.0),
        httpx_args={"transport": httpx.MockTransport(handler)},
    )


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record what the retry loop would have waited instead of waiting."""
    waited: list[float] = []
    monkeypatch.setattr(time, "sleep", waited.append)
    return waited


# -- configuration ---------------------------------------------------------


def test_the_api_key_becomes_a_bearer_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert str(seen[0].url) == f"{BASE_URL}/v1/deposits"


def test_a_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, {"assets": []})

    client = CryptoProcessingClient(
        BASE_URL + "/", API_KEY, httpx_args={"transport": httpx.MockTransport(handler)}
    )
    client.list_assets()
    assert str(seen[0].url) == f"{BASE_URL}/v1/assets"


def test_a_missing_api_key_fails_before_any_request() -> None:
    with pytest.raises(ValueError, match="api_key"):
        CryptoProcessingClient(BASE_URL, "")


# -- idempotency -----------------------------------------------------------


def test_a_key_is_minted_when_the_caller_does_not_supply_one() -> None:
    """The server answers 400 without one, so the default cannot be 'absent'."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert len(seen[0].headers["idempotency-key"]) >= 32


def test_an_explicit_key_is_used_unchanged() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC", idempotency_key="order-1234")
    assert seen[0].headers["idempotency-key"] == "order-1234"


def test_two_calls_get_two_different_keys() -> None:
    """One key per logical operation. A shared key would make the second a replay."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC")
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert seen[0].headers["idempotency-key"] != seen[1].headers["idempotency-key"]


def test_a_retry_reuses_the_key_it_started_with() -> None:
    """The rule the docs put in bold: a new key is a second deposit, not a retry."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return json_response(503, {"detail": "BTCPay is unreachable"}, {"Retry-After": "2"})
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        deposit = client.create_deposit(external_user_id="user-42", asset="BTC")
    assert len(seen) == 2
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]
    assert deposit.status == "pending"


def test_a_retry_sends_byte_identical_bodies() -> None:
    """The request hash is over the exact bytes; a re-serialization reads as 422."""
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            return json_response(503, {"detail": "unreachable"}, {"Retry-After": "0"})
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC", expected_amount="50000000")
    assert bodies[0] == bodies[1]


# -- retrying --------------------------------------------------------------


def test_retry_after_is_honoured(no_real_sleeping: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(503, {"detail": "unreachable"}, {"Retry-After": "7"})

    with build(handler) as client, pytest.raises(ServiceUnavailableError) as raised:
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert no_real_sleeping == [7.0, 7.0]
    assert raised.value.retry_after == 7.0


def test_a_long_retry_after_is_capped() -> None:
    """A server asking for an hour must not park a request handler for an hour."""
    policy = RetryPolicy(attempts=2, max_delay_seconds=5.0)
    assert (
        policy.delay_for(ServiceUnavailableError(503, "wait", retry_after=3600.0), attempt=0) == 5.0
    )


def test_an_in_flight_409_is_retried() -> None:
    """Retry-After present means 'your own earlier attempt is still running'."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["idempotency-key"])
        if len(calls) == 1:
            return json_response(409, {"detail": "still in progress"}, {"Retry-After": "1"})
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_an_illegal_transition_409_is_raised_at_once() -> None:
    """No Retry-After means waiting cannot help. Three tries would just be slower."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(409, {"detail": "withdrawal is not pending_approval"})

    with build(handler) as client, pytest.raises(ConflictError):
        client.admin_approve_withdrawal("019feb97-1c04-7b2e-9f3a-2d5c7e8b1a06")
    assert len(calls) == 1


def test_a_502_is_never_retried() -> None:
    """BTCPay answered and refused. The intent is dead; retrying wastes time."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(502, {"detail": "BTCPay rejected the invoice request"})

    with build(handler) as client, pytest.raises(UpstreamRefusedError):
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert len(calls) == 1


def test_attempts_of_one_disables_retrying_but_not_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(503, {"detail": "unreachable"}, {"Retry-After": "1"})

    client = build(handler, retry=RetryPolicy(attempts=1))
    with pytest.raises(ServiceUnavailableError):
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert len(seen) == 1
    assert seen[0].headers["idempotency-key"]


def test_a_dropped_connection_is_retried_with_the_same_key() -> None:
    """Safe only because the key is pinned. That is what the key is for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["idempotency-key"])
        if len(seen) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return json_response(201, DEPOSIT_BODY)

    with build(handler) as client:
        client.create_deposit(external_user_id="user-42", asset="BTC")
    assert seen[0] == seen[1]


def test_a_connection_that_never_comes_back_raises_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with build(handler) as client, pytest.raises(TransportError):
        client.get_user_balances("user-42")


# -- error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (502, UpstreamRefusedError),
        (500, ServerError),
    ],
)
def test_each_refusal_gets_its_own_class(status: int, expected: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(status, {"detail": "no"})

    with build(handler) as client, pytest.raises(expected):
        client.get_deposit("019feb96-7e52-771a-a8cb-a86dccc87339")


def test_a_business_rule_422_carries_its_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"detail": "amount is below the dust threshold"})

    with build(handler) as client, pytest.raises(ValidationError) as raised:
        client.create_withdrawal(
            external_user_id="user-42",
            asset="BTC",
            amount="1",
            destination_address="bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq",
        )
    assert "dust" in raised.value.message
    assert raised.value.field_errors == []


def test_a_body_shape_422_carries_the_field_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            422,
            {"detail": [{"loc": ["body", "amount"], "msg": "field required", "type": "missing"}]},
        )

    with build(handler) as client, pytest.raises(ValidationError) as raised:
        client.create_withdrawal(
            external_user_id="user-42", asset="BTC", amount="", destination_address="x"
        )
    assert raised.value.field_errors[0]["loc"] == ["body", "amount"]


def test_pool_exhaustion_keeps_its_machine_readable_code() -> None:
    """The one error a caller is meant to branch on rather than log."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            503,
            {
                "detail": {
                    "code": "DEPOSIT_TEMPORARILY_UNAVAILABLE",
                    "message": "no USDT_TRC20 deposit address is free right now",
                }
            },
            {"Retry-After": "60"},
        )

    with (
        build(handler, retry=RetryPolicy(attempts=1)) as client,
        pytest.raises(ServiceUnavailableError) as raised,
    ):
        client.create_deposit(external_user_id="user-42", asset="USDT_TRC20")
    assert raised.value.code == "DEPOSIT_TEMPORARILY_UNAVAILABLE"
    assert raised.value.retry_after == 60.0


def test_a_proxys_html_error_page_is_not_a_json_parse_error() -> None:
    """The failure an integrator would otherwise report as an SDK bug."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            504,
            content=b"<html><body>Gateway Timeout</body></html>",
            headers={"content-type": "text/html"},
        )

    with (
        build(handler, retry=RetryPolicy(attempts=1)) as client,
        pytest.raises(ServerError) as raised,
    ):
        client.get_user_balances("user-42")
    assert raised.value.status_code == 504
    assert "Gateway Timeout" in raised.value.message


def test_a_retry_after_http_date_is_understood(no_real_sleeping: list[float]) -> None:
    """RFC 9110 allows a date. A client that only parses integers waits zero seconds."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            503, {"detail": "unreachable"}, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
        )

    with (
        build(handler, retry=RetryPolicy(attempts=2, max_delay_seconds=30.0)) as client,
        pytest.raises(ServiceUnavailableError),
    ):
        client.list_assets()
    assert no_real_sleeping == [30.0]


# -- reads -----------------------------------------------------------------


def test_amounts_arrive_as_strings_and_stay_that_way() -> None:
    """A generator that mapped these to float would round a satoshi away."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, DEPOSIT_BODY)

    with build(handler) as client:
        deposit = client.get_deposit("019feb96-7e52-771a-a8cb-a86dccc87339")
    assert deposit.amount_expected == "0.50000000"
    assert deposit.created_at == "2026-08-10T12:15:13+00:00"


def test_a_degraded_readyz_is_an_answer_rather_than_an_exception() -> None:
    """503 there names the unhappy component; raising would hide the body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            503,
            {
                "status": "degraded",
                "components": [{"name": "btcpay", "status": "down", "detail": "connect refused"}],
            },
        )

    with build(handler) as client:
        ready = client.readyz()
    assert ready.status == "degraded"
    assert ready.components[0].name == "btcpay"


def test_a_uuid_path_parameter_accepts_the_string_form() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, DEPOSIT_BODY)

    with build(handler) as client:
        client.get_deposit("019feb96-7e52-771a-a8cb-a86dccc87339")
    assert str(seen[0].url).endswith("/v1/deposits/019feb96-7e52-771a-a8cb-a86dccc87339")
