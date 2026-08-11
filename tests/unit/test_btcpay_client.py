"""The HTTP half of the BTCPay client: error taxonomy, retries, request shape.

Everything here runs against `httpx.MockTransport`, so the real request objects
are built, the real status handling runs, and only the socket is missing. The
things worth pinning are the ones a reviewer cannot see from the call site:

- **which exception a status becomes.** `retryable` on that exception is what
  Job B and the webhook processor branch on, so mapping a 500 to the wrong
  class either parks a recoverable withdrawal or hammers a permanent failure.
- **that a POST is never retried.** A retried payout creation on an ambiguous
  timeout is a second payout, which is a second withdrawal of real money.
- **that a response body never reaches an exception message.** Bodies echo
  request content, and these messages are logged.
"""

from __future__ import annotations

import json

import httpx
import pytest

from crypto_processing_api.gateway import btcpay_client
from crypto_processing_api.gateway.btcpay_client import (
    BACKOFF_SECONDS,
    GET_ATTEMPTS,
    BTCPayAuthError,
    BTCPayClient,
    BTCPayError,
    BTCPayNotFound,
    BTCPayRateLimited,
    BTCPayServerError,
    BTCPayUnavailable,
    BTCPayValidation,
    build_client,
)

BASE_URL = "http://btcpay.test"
STORE = "store-1"


class ScriptedTransport:
    """Answers requests from a script, and remembers what it was asked.

    The last entry repeats, so a retry test says `503, 503, 200` and a
    gives-up test says `503` once.
    """

    def __init__(self, *scripted: httpx.Response | Exception) -> None:
        self.scripted = list(scripted)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(*scripted: httpx.Response | Exception) -> tuple[BTCPayClient, ScriptedTransport]:
    transport = ScriptedTransport(*scripted)
    http = httpx.Client(transport=httpx.MockTransport(transport.handle), base_url=BASE_URL)
    client = BTCPayClient(base_url=BASE_URL, api_key="test-key", store_id=STORE, client=http)
    return client, transport


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff delays instead of waiting them out."""
    recorded: list[float] = []
    monkeypatch.setattr(btcpay_client.time, "sleep", recorded.append)
    return recorded


def json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


INVOICE = {
    "id": "inv-1",
    "storeId": STORE,
    "currency": "BTC",
    "status": "New",
    "additionalStatus": "None",
    "metadata": {"deposit_id": "d-1"},
}

PAYOUT = {
    "id": "payout-1",
    "destination": "bcrt1qexample",
    "originalAmount": "0.00050000",
    "payoutAmount": "0.00050000",
    "payoutMethodId": "BTC-CHAIN",
    "state": "AwaitingPayment",
    "metadata": {"withdrawal_id": "w-1"},
}


# -- retry policy ----------------------------------------------------------


def test_a_get_is_retried_until_it_succeeds(slept: list[float]) -> None:
    client, transport = make_client(
        httpx.Response(503),
        httpx.Response(503),
        json_response(200, INVOICE),
    )
    assert client.get_invoice("inv-1").id == "inv-1"
    assert len(transport.requests) == 3
    assert len(slept) == 2


def test_a_get_gives_up_after_three_attempts(slept: list[float]) -> None:
    client, transport = make_client(httpx.Response(503))
    with pytest.raises(BTCPayUnavailable):
        client.get_invoice("inv-1")
    assert len(transport.requests) == GET_ATTEMPTS
    # Slept between attempts, not after the last one.
    assert len(slept) == GET_ATTEMPTS - 1


def test_a_non_retryable_error_is_not_retried(slept: list[float]) -> None:
    client, transport = make_client(httpx.Response(404))
    with pytest.raises(BTCPayNotFound):
        client.get_invoice("inv-1")
    assert len(transport.requests) == 1
    assert slept == []


def test_the_backoff_grows_between_attempts(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(503))
    with pytest.raises(BTCPayUnavailable):
        client.get_invoice("inv-1")
    # Jittered by +/-25%, so the assertion is on the band, not the value.
    for delay, base in zip(slept, BACKOFF_SECONDS, strict=False):
        assert base * 0.75 <= delay <= base * 1.25
    assert slept[0] < slept[1]


def test_a_retry_after_hint_wins_over_the_backoff(slept: list[float]) -> None:
    """BTCPay asking for 60s must not be retried after 0.5s."""
    client, _ = make_client(
        httpx.Response(429, headers={"Retry-After": "60"}),
        json_response(200, INVOICE),
    )
    client.get_invoice("inv-1")
    assert slept[0] >= 60 * 0.75


def test_an_unparseable_retry_after_falls_back_to_the_backoff(slept: list[float]) -> None:
    """A HTTP-date Retry-After is legal and is not a number of seconds."""
    client, _ = make_client(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        json_response(200, INVOICE),
    )
    client.get_invoice("inv-1")
    assert slept[0] <= BACKOFF_SECONDS[0] * 1.25


def test_the_rate_limit_error_carries_the_hint() -> None:
    """A PUT goes through `_send`, so the error arrives unretried and intact."""
    client, _ = make_client(httpx.Response(429, headers={"Retry-After": "12"}))
    with pytest.raises(BTCPayRateLimited) as caught:
        client.upsert_payout_processor(
            "BTC-CHAIN",
            interval_seconds=60,
            fee_target_block=3,
            process_new_payouts_instantly=True,
        )
    assert caught.value.retry_after == 12
    assert caught.value.status_code == 429


def test_a_post_is_never_retried(slept: list[float]) -> None:
    """A retried payout creation on an ambiguous timeout is a second payout."""
    client, transport = make_client(httpx.Response(503))
    with pytest.raises(BTCPayUnavailable):
        client.create_payout(
            destination="bcrt1qexample",
            amount="0.00050000",
            payout_method_id="BTC-CHAIN",
            metadata={},
        )
    assert len(transport.requests) == 1
    assert slept == []


def test_a_post_with_an_empty_body_returns_none() -> None:
    client, transport = make_client(httpx.Response(204))
    assert client.redeliver_webhook("wh-1", "del-1") is None
    assert transport.last.url.path == "/api/v1/webhooks/wh-1/deliveries/del-1/redeliver"


# -- status taxonomy -------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected", "retryable"),
    [
        (502, BTCPayUnavailable, True),
        (503, BTCPayUnavailable, True),
        (504, BTCPayUnavailable, True),
        (429, BTCPayRateLimited, True),
        (401, BTCPayAuthError, False),
        (403, BTCPayAuthError, False),
        (404, BTCPayNotFound, False),
        (400, BTCPayValidation, False),
        (422, BTCPayValidation, False),
        (500, BTCPayServerError, True),
        (418, BTCPayServerError, True),
    ],
)
def test_each_status_becomes_its_own_error(
    status_code: int, expected: type[BTCPayError], retryable: bool, slept: list[float]
) -> None:
    client, _ = make_client(httpx.Response(status_code))
    with pytest.raises(expected) as caught:
        client.get_invoice("inv-1")
    assert caught.value.retryable is retryable
    assert isinstance(caught.value, BTCPayError)


def test_a_success_status_raises_nothing() -> None:
    client, _ = make_client(json_response(200, INVOICE))
    assert client.get_invoice("inv-1").store_id == STORE


def test_a_timeout_becomes_a_retryable_outage(slept: list[float]) -> None:
    client, transport = make_client(httpx.ConnectTimeout("read timed out"))
    with pytest.raises(BTCPayUnavailable) as caught:
        client.get_invoice("inv-1")
    assert "timeout" in str(caught.value)
    assert len(transport.requests) == GET_ATTEMPTS


def test_a_connect_error_becomes_a_retryable_outage(slept: list[float]) -> None:
    client, _ = make_client(httpx.ConnectError("connection refused"))
    with pytest.raises(BTCPayUnavailable) as caught:
        client.get_invoice("inv-1")
    assert "ConnectError" in str(caught.value)


def test_a_validation_message_quotes_the_body_so_the_operator_can_act() -> None:
    client, _ = make_client(httpx.Response(400, text="destination is not a valid address"))
    with pytest.raises(BTCPayValidation, match="not a valid address"):
        client.get_invoice("inv-1")


def test_a_long_validation_body_is_truncated() -> None:
    client, _ = make_client(httpx.Response(422, text="x" * 5_000))
    with pytest.raises(BTCPayValidation) as caught:
        client.get_invoice("inv-1")
    assert len(str(caught.value)) < 500


def test_a_server_error_never_echoes_the_body(slept: list[float]) -> None:
    """Bodies echo request content, and these messages go to the log."""
    client, _ = make_client(httpx.Response(500, text="token=cpk_live_supersecret"))
    with pytest.raises(BTCPayServerError) as caught:
        client.get_invoice("inv-1")
    assert "supersecret" not in str(caught.value)


# -- invoices --------------------------------------------------------------


def test_create_top_up_invoice_sends_every_checkout_option() -> None:
    client, transport = make_client(json_response(200, INVOICE))
    client.create_top_up_invoice(
        currency="BTC",
        metadata={"deposit_id": "d-1"},
        payment_methods=["BTC-CHAIN", "BTC-LN"],
        expiration_minutes=45,
        monitoring_minutes=1440,
        additional_search_terms=["cpapi:d-1"],
    )
    request = transport.last
    assert request.url.path == f"/api/v1/stores/{STORE}/invoices"
    body = _json_body(request)
    assert body["currency"] == "BTC"
    assert body["metadata"] == {"deposit_id": "d-1"}
    assert body["checkout"] == {
        "paymentMethods": ["BTC-CHAIN", "BTC-LN"],
        # The first listed method is what the checkout page opens on.
        "defaultPaymentMethod": "BTC-CHAIN",
        "expirationMinutes": 45,
        "monitoringMinutes": 1440,
    }
    assert body["additionalSearchTerms"] == ["cpapi:d-1"]
    # No amount: a top-up invoice treats any payment as a full payment.
    assert "amount" not in body


def test_create_top_up_invoice_omits_what_was_not_asked_for() -> None:
    client, transport = make_client(json_response(200, INVOICE))
    client.create_top_up_invoice(currency="BTC", metadata={})
    body = _json_body(transport.last)
    assert "checkout" not in body
    assert "additionalSearchTerms" not in body


def test_get_invoice_is_not_store_scoped() -> None:
    """2.4.2 moved per-invoice routes out of the store prefix."""
    client, transport = make_client(json_response(200, INVOICE))
    client.get_invoice("inv-1")
    assert transport.last.url.path == "/api/v1/invoices/inv-1"


def test_get_invoice_payment_methods_parses_the_payments() -> None:
    client, transport = make_client(
        json_response(
            200,
            [
                {
                    "paymentMethodId": "BTC-CHAIN",
                    "destination": "bcrt1qexample",
                    "totalPaid": "0.00050000",
                    "payments": [
                        {"id": "abc-0", "value": "0.00050000", "status": "Settled"},
                    ],
                }
            ],
        )
    )
    methods = client.get_invoice_payment_methods("inv-1")
    assert transport.last.url.path == "/api/v1/invoices/inv-1/payment-methods"
    assert len(methods) == 1
    assert methods[0].payments[0].txid == "abc"


def test_list_invoices_passes_every_filter() -> None:
    client, transport = make_client(json_response(200, [INVOICE]))
    invoices = client.list_invoices(
        start_date=1_700_000_000, end_date=1_800_000_000, text_search="cpapi:d-1", skip=5, take=10
    )
    assert [invoice.id for invoice in invoices] == ["inv-1"]
    params = transport.last.url.params
    assert params["startDate"] == "1700000000"
    assert params["endDate"] == "1800000000"
    assert params["textSearch"] == "cpapi:d-1"
    assert params["skip"] == "5"
    assert params["take"] == "10"


def test_list_invoices_omits_unset_filters() -> None:
    client, transport = make_client(json_response(200, []))
    assert client.list_invoices() == []
    params = transport.last.url.params
    assert "startDate" not in params
    assert "endDate" not in params
    assert "textSearch" not in params


# -- store and wallet ------------------------------------------------------


@pytest.mark.parametrize(("only_enabled", "expected"), [(True, "true"), (False, "false")])
def test_get_store_payment_methods_says_which_ones_it_wants(
    only_enabled: bool, expected: str
) -> None:
    client, transport = make_client(
        json_response(200, [{"paymentMethodId": "BTC-CHAIN", "enabled": True}])
    )
    methods = client.get_store_payment_methods(only_enabled=only_enabled)
    assert [method.payment_method_id for method in methods] == ["BTC-CHAIN"]
    assert transport.last.url.params["onlyEnabled"] == expected


def test_get_wallet_reads_the_store_wallet() -> None:
    client, transport = make_client(
        json_response(200, {"balance": "1.50000000", "confirmedBalance": "1.00000000"})
    )
    wallet = client.get_wallet("BTC-CHAIN")
    assert wallet.balance == "1.50000000"
    assert wallet.confirmed_balance == "1.00000000"
    assert transport.last.url.path == f"/api/v1/stores/{STORE}/payment-methods/BTC-CHAIN/wallet"


def test_get_wallet_transactions_pages() -> None:
    client, transport = make_client(
        json_response(
            200,
            [{"transactionHash": "a" * 64, "amount": "0.001", "confirmations": "3"}],
        )
    )
    rows = client.get_wallet_transactions("BTC-CHAIN", skip=20, limit=50)
    assert rows[0].transaction_hash == "a" * 64
    assert transport.last.url.params["skip"] == "20"
    assert transport.last.url.params["limit"] == "50"
    assert transport.last.url.path.endswith("/wallet/transactions")


def test_get_fee_rate_returns_a_float_whatever_btcpay_sent() -> None:
    """BTCPay reports the rate as a string; pricing a payout needs a number."""
    client, transport = make_client(json_response(200, {"feeRate": "12.5"}))
    rate = client.get_fee_rate("BTC-CHAIN", block_target=3)
    assert rate == 12.5
    assert isinstance(rate, float)
    assert transport.last.url.params["blockTarget"] == "3"


# -- lightning -------------------------------------------------------------


def test_get_lightning_balance_reads_the_outbound_liquidity() -> None:
    client, transport = make_client(
        json_response(
            200,
            {
                "onchain": {"confirmed": "0"},
                "offchain": {"local": "500000000", "remote": "1000000"},
            },
        )
    )
    balance = client.get_lightning_balance("BTC")
    assert balance.local_msat == "500000000"
    assert transport.last.url.path == f"/api/v1/stores/{STORE}/lightning/BTC/balance"


def test_get_lightning_payment_takes_a_crypto_code_not_a_method_id() -> None:
    client, transport = make_client(
        json_response(
            200,
            {
                "paymentHash": "f" * 64,
                "status": "Complete",
                "totalAmount": "1000",
                "feeAmount": "3",
            },
        )
    )
    payment = client.get_lightning_payment("BTC", "f" * 64)
    assert payment.status == "Complete"
    assert payment.fee_amount == "3"
    assert transport.last.url.path == f"/api/v1/stores/{STORE}/lightning/BTC/payments/{'f' * 64}"


def test_a_node_with_no_record_of_the_payment_is_a_not_found() -> None:
    """The 404 is the answer, not an error: that payout never left."""
    client, _ = make_client(httpx.Response(404))
    with pytest.raises(BTCPayNotFound):
        client.get_lightning_payment("BTC", "f" * 64)


# -- payouts ---------------------------------------------------------------


def test_create_payout_approves_it_here_not_in_btcpay() -> None:
    client, transport = make_client(json_response(200, PAYOUT))
    payout = client.create_payout(
        destination="bcrt1qexample",
        amount="0.00050000",
        payout_method_id="BTC-CHAIN",
        metadata={"withdrawal_id": "w-1", "cpapi": True},
    )
    assert payout.id == "payout-1"
    body = _json_body(transport.last)
    # Our limits and velocity caps already ran; BTCPay's own approval stage
    # would be a second queue nobody watches.
    assert body["approved"] is True
    assert body["payoutMethodId"] == "BTC-CHAIN"
    assert body["metadata"] == {"withdrawal_id": "w-1", "cpapi": True}
    assert transport.last.url.path == f"/api/v1/stores/{STORE}/payouts"


def test_get_payout_is_not_store_scoped() -> None:
    client, transport = make_client(json_response(200, PAYOUT))
    assert client.get_payout("payout-1").state == "AwaitingPayment"
    assert transport.last.url.path == "/api/v1/payouts/payout-1"


@pytest.mark.parametrize(("include", "expected"), [(True, "true"), (False, "false")])
def test_list_payouts_says_whether_it_wants_cancelled_ones(include: bool, expected: str) -> None:
    client, transport = make_client(json_response(200, [PAYOUT]))
    assert len(client.list_payouts(include_cancelled=include)) == 1
    assert transport.last.url.params["includeCancelled"] == expected


def test_cancel_payout_reports_success() -> None:
    client, transport = make_client(httpx.Response(200))
    assert client.cancel_payout("payout-1") is True
    assert transport.last.method == "DELETE"


@pytest.mark.parametrize("status_code", [400, 404])
def test_cancel_payout_is_best_effort(status_code: int) -> None:
    """BTCPay refuses once the payout is in flight; that is not our error."""
    client, _ = make_client(httpx.Response(status_code))
    assert client.cancel_payout("payout-1") is False


def test_cancel_payout_still_raises_on_an_outage() -> None:
    """An unreachable BTCPay is not the same answer as "it refused"."""
    client, transport = make_client(httpx.Response(503))
    with pytest.raises(BTCPayUnavailable):
        client.cancel_payout("payout-1")
    # DELETE goes through _send, so there is no retry loop around it.
    assert len(transport.requests) == 1


def test_upsert_payout_processor_sends_the_whole_schedule() -> None:
    client, transport = make_client(httpx.Response(200))
    client.upsert_payout_processor(
        "BTC-CHAIN",
        interval_seconds=60,
        fee_target_block=3,
        process_new_payouts_instantly=True,
        threshold="0.001",
    )
    assert transport.last.method == "PUT"
    assert transport.last.url.path == (
        f"/api/v1/stores/{STORE}/payout-processors/OnChainAutomatedPayoutSenderFactory/BTC-CHAIN"
    )
    assert _json_body(transport.last) == {
        "intervalSeconds": 60,
        "feeTargetBlock": 3,
        "threshold": "0.001",
        "processNewPayoutsInstantly": True,
    }


# -- construction ----------------------------------------------------------


@pytest.mark.parametrize("missing", ["base_url", "api_key", "store_id"])
def test_build_client_refuses_incomplete_configuration(missing: str) -> None:
    kwargs: dict[str, str | None] = {
        "base_url": BASE_URL,
        "api_key": "test-key",
        "store_id": STORE,
    }
    kwargs[missing] = None
    with pytest.raises(BTCPayValidation, match="must all be set"):
        build_client(**kwargs)  # type: ignore[arg-type]


def test_build_client_wires_the_store() -> None:
    client = build_client(base_url=BASE_URL, api_key="test-key", store_id=STORE)
    try:
        assert client.store_id == STORE
        assert client.base_url == BASE_URL
    finally:
        client.close()


def test_a_trailing_slash_on_the_base_url_is_trimmed() -> None:
    """Otherwise every path would be requested with a double slash."""
    client = BTCPayClient(base_url=f"{BASE_URL}/", api_key="test-key", store_id=STORE)
    try:
        assert client.base_url == BASE_URL
    finally:
        client.close()


def test_the_default_client_carries_the_token_and_the_timeouts() -> None:
    """The constructor is the only place auth and timeouts are set.

    Reaching for `_client` is deliberate: there is no public way to read a
    header the client will send, and a wrong Authorization header means every
    call answers 401 in production and nowhere else.
    """
    client = BTCPayClient(base_url=BASE_URL, api_key="test-key", store_id=STORE)
    try:
        assert client._client.headers["Authorization"] == "token test-key"
        assert client._client.headers["Content-Type"] == "application/json"
        assert client._client.timeout.connect == btcpay_client.CONNECT_TIMEOUT
        assert client._client.timeout.read == btcpay_client.READ_TIMEOUT
    finally:
        client.close()


def test_close_closes_the_underlying_client() -> None:
    client, _ = make_client(httpx.Response(200))
    client.close()
    assert client._client.is_closed


def _json_body(request: httpx.Request) -> dict[str, object]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed
