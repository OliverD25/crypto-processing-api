"""The HTTP half of the TronGrid client, and the parser's rejection paths.

`tests/unit/test_tron.py` covers what a well-formed transaction means. This
covers what happens when TronGrid answers badly: the error taxonomy, the retry
loop, and log entries the parser must refuse rather than misread.

The parser's rejections are the money-relevant half. A log entry it silently
mis-parses becomes a `TronTransfer` that the withdrawal verifier then compares
against — so "this one is not a transfer we can read" has to stay a skip, never
a guess.
"""

from __future__ import annotations

import json

import httpx
import pytest

from crypto_processing_api.gateway import trongrid
from crypto_processing_api.gateway.trongrid import (
    BACKOFF_SECONDS,
    CONSTANT_CALL_OWNER,
    GET_ATTEMPTS,
    TRANSFER_TOPIC,
    USDT_CONTRACT_MAINNET,
    USDT_CONTRACT_NILE,
    TronGridAuthError,
    TronGridClient,
    TronGridContractError,
    TronGridError,
    TronGridRateLimited,
    TronGridServerError,
    TronGridUnavailable,
    _pad_address,
    build_client,
    parse_transaction_info,
)
from tests.fake_tron import (
    DESTINATION,
    HOT_WALLET,
    FakeTronGrid,
    abi_string,
    abi_uint,
    constant_failure,
    constant_result,
    to_hex,
    to_topic,
    trc20_metadata_payloads,
)

BASE_URL = "https://nile.trongrid.test"
TXID = "a" * 64


class ScriptedTransport:
    """Answers from a script; the last entry repeats."""

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


def make_client(*scripted: httpx.Response | Exception) -> tuple[TronGridClient, ScriptedTransport]:
    transport = ScriptedTransport(*scripted)
    http = httpx.Client(transport=httpx.MockTransport(transport.handle), base_url=BASE_URL)
    return TronGridClient(base_url=BASE_URL, client=http), transport


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(trongrid.time, "sleep", recorded.append)
    return recorded


def transfer_log(
    *,
    contract: str = USDT_CONTRACT_MAINNET,
    sender: str = HOT_WALLET,
    recipient: str = DESTINATION,
    amount_hex: str = f"{200_000_000:064x}",
    topics: list[str] | None = None,
) -> dict[str, object]:
    return {
        "address": to_hex(contract)[2:],
        "topics": topics
        if topics is not None
        else [TRANSFER_TOPIC, to_topic(sender), to_topic(recipient)],
        "data": amount_hex,
    }


def info(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": TXID,
        "blockNumber": 1_000,
        "receipt": {"result": "SUCCESS"},
        "log": [transfer_log()],
    }
    payload.update(overrides)
    return payload


# -- parser rejections -----------------------------------------------------


def test_a_payload_without_an_id_is_not_a_transaction() -> None:
    """TronGrid answers an unknown id with `{}`, and sometimes with a stub."""
    assert parse_transaction_info({"blockNumber": 5}) is None


def test_a_log_entry_for_another_event_is_skipped() -> None:
    """An approval or a mint is not a transfer, and must not read as one."""
    other_topic = "b" * 64
    parsed = parse_transaction_info(info(log=[transfer_log(topics=[other_topic, "x", "y"])]))
    assert parsed is not None
    assert parsed.transfers == ()


def test_a_log_entry_with_no_topics_is_skipped() -> None:
    parsed = parse_transaction_info(info(log=[{"address": "41" + "0" * 40, "topics": []}]))
    assert parsed is not None
    assert parsed.transfers == ()


def test_a_transfer_missing_its_recipient_topic_is_skipped() -> None:
    """Two topics is a Transfer signature with an argument missing."""
    parsed = parse_transaction_info(
        info(log=[transfer_log(topics=[TRANSFER_TOPIC, to_topic(HOT_WALLET)])])
    )
    assert parsed is not None
    assert parsed.transfers == ()


def test_an_unreadable_address_is_skipped_not_guessed() -> None:
    parsed = parse_transaction_info(
        info(log=[transfer_log(topics=[TRANSFER_TOPIC, "not-hex-at-all", to_topic(DESTINATION)])])
    )
    assert parsed is not None
    assert parsed.transfers == ()


def test_an_unreadable_amount_is_skipped_not_guessed() -> None:
    """A transfer whose amount cannot be read must never become amount 0."""
    parsed = parse_transaction_info(info(log=[transfer_log(amount_hex="0xnot-a-number")]))
    assert parsed is not None
    assert parsed.transfers == ()


def test_a_missing_data_field_reads_as_zero() -> None:
    parsed = parse_transaction_info(info(log=[transfer_log(amount_hex="")]))
    assert parsed is not None
    assert parsed.transfers[0].amount == 0


def test_raw_return_data_is_not_read_as_a_status() -> None:
    """`contractResult` holds the call's return bytes, not SUCCESS/FAILED."""
    parsed = parse_transaction_info(info(contractResult=["0000000000000001"]))
    assert parsed is not None
    assert parsed.contract_result is None
    assert parsed.receipt_succeeded


def test_a_failed_result_overrides_the_return_data() -> None:
    parsed = parse_transaction_info(info(contractResult=["00"], result="FAILED"))
    assert parsed is not None
    assert parsed.contract_result == "FAILED"
    assert not parsed.receipt_succeeded


def test_a_missing_receipt_is_not_a_success() -> None:
    parsed = parse_transaction_info(info(receipt={}))
    assert parsed is not None
    assert parsed.receipt_result is None
    assert not parsed.receipt_succeeded


# -- error taxonomy --------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected", "retryable"),
    [
        (429, TronGridRateLimited, True),
        (403, TronGridRateLimited, True),
        (401, TronGridAuthError, False),
        (502, TronGridUnavailable, True),
        (503, TronGridUnavailable, True),
        (504, TronGridUnavailable, True),
        (500, TronGridServerError, True),
        (418, TronGridServerError, True),
    ],
)
def test_each_status_becomes_its_own_error(
    status_code: int, expected: type[TronGridError], retryable: bool, slept: list[float]
) -> None:
    client, _ = make_client(httpx.Response(status_code))
    with pytest.raises(expected) as caught:
        client.get_block_height()
    assert caught.value.retryable is retryable


def test_a_403_says_why_it_might_not_be_the_key(slept: list[float]) -> None:
    """TronGrid uses 403 both for a bad key and for a throttle, so the message
    has to carry both readings or an operator rotates a working key."""
    client, _ = make_client(httpx.Response(403))
    with pytest.raises(TronGridRateLimited, match="bad API key"):
        client.get_block_height()


def test_a_rejected_key_is_not_retried(slept: list[float]) -> None:
    client, transport = make_client(httpx.Response(401))
    with pytest.raises(TronGridAuthError):
        client.get_block_height()
    assert len(transport.requests) == 1
    assert slept == []


# -- retry loop ------------------------------------------------------------


def test_a_call_is_retried_until_it_succeeds(slept: list[float]) -> None:
    client, transport = make_client(
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"block_header": {"raw_data": {"number": 42}}}),
    )
    assert client.get_block_height() == 42
    assert len(transport.requests) == 3
    assert len(slept) == 2


def test_a_call_gives_up_after_three_attempts(slept: list[float]) -> None:
    client, transport = make_client(httpx.Response(503))
    with pytest.raises(TronGridUnavailable):
        client.get_block_height()
    assert len(transport.requests) == GET_ATTEMPTS
    assert len(slept) == GET_ATTEMPTS - 1


def test_a_transport_failure_is_retried_as_an_outage(slept: list[float]) -> None:
    client, transport = make_client(httpx.ConnectError("connection refused"))
    with pytest.raises(TronGridUnavailable, match="ConnectError"):
        client.get_block_height()
    assert len(transport.requests) == GET_ATTEMPTS


def test_a_retry_after_hint_wins_over_the_backoff(slept: list[float]) -> None:
    client, _ = make_client(
        httpx.Response(429, headers={"Retry-After": "45"}),
        httpx.Response(200, json={"block_header": {"raw_data": {"number": 7}}}),
    )
    assert client.get_block_height() == 7
    assert slept[0] >= 45 * 0.75


def test_an_unparseable_retry_after_falls_back_to_the_backoff(slept: list[float]) -> None:
    client, _ = make_client(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(200, json={"block_header": {"raw_data": {"number": 7}}}),
    )
    client.get_block_height()
    assert slept[0] <= BACKOFF_SECONDS[0] * 1.25


# -- endpoints -------------------------------------------------------------


def test_get_transaction_parses_what_the_node_returned() -> None:
    client, transport = make_client(httpx.Response(200, json=info()))
    transaction = client.get_transaction(TXID)
    assert transaction is not None
    assert transaction.txid == TXID
    assert transaction.transfers[0].to_address == DESTINATION
    assert transport.last.url.path == "/wallet/gettransactioninfobyid"
    assert transport.last.method == "POST"


def test_an_unknown_transaction_is_none_not_an_error() -> None:
    """TronGrid answers an unknown id with `{}` and a 200."""
    client, _ = make_client(httpx.Response(200, json={}))
    assert client.get_transaction(TXID) is None


def test_a_non_object_answer_is_treated_as_unknown() -> None:
    client, _ = make_client(httpx.Response(200, json=[]))
    assert client.get_transaction(TXID) is None


def test_a_block_height_without_a_number_is_an_error(slept: list[float]) -> None:
    """Returning 0 here would make every transaction look unconfirmed."""
    client, _ = make_client(httpx.Response(200, json={"block_header": {}}))
    with pytest.raises(TronGridServerError, match="block number"):
        client.get_block_height()


def test_get_trx_balance_reads_sun() -> None:
    client, transport = make_client(httpx.Response(200, json={"balance": 250_000_000}))
    assert client.get_trx_balance(HOT_WALLET) == 250 * trongrid.SUN_PER_TRX
    assert transport.last.url.path == "/wallet/getaccount"


def test_an_unfunded_account_reads_as_zero() -> None:
    """An account that has never been activated comes back as `{}`."""
    client, _ = make_client(httpx.Response(200, json={}))
    assert client.get_trx_balance(HOT_WALLET) == 0


def test_get_trc20_balance_decodes_the_constant_result() -> None:
    client, transport = make_client(
        httpx.Response(200, json={"constant_result": [f"{1_234_567:064x}"]})
    )
    balance = client.get_trc20_balance(HOT_WALLET, USDT_CONTRACT_MAINNET)
    assert balance == 1_234_567
    assert transport.last.url.path == "/wallet/triggerconstantcontract"


def test_an_empty_constant_result_reads_as_zero() -> None:
    client, _ = make_client(httpx.Response(200, json={"constant_result": []}))
    assert client.get_trc20_balance(HOT_WALLET, USDT_CONTRACT_MAINNET) == 0


def test_the_balance_call_passes_the_address_as_an_abi_argument() -> None:
    client, transport = make_client(httpx.Response(200, json={"constant_result": ["00"]}))
    client.get_trc20_balance(HOT_WALLET, USDT_CONTRACT_MAINNET)
    body = json.loads(transport.last.content)
    assert body["function_selector"] == "balanceOf(address)"
    assert body["parameter"] == to_topic(HOT_WALLET)
    assert body["owner_address"] == HOT_WALLET
    assert body["contract_address"] == USDT_CONTRACT_MAINNET


# -- contract identity -----------------------------------------------------
#
# `symbol()` and `decimals()` are the preflight for the Nile session: a format
# check on a contract address proves the characters are well-formed, and
# nothing at all about what is deployed there.


def metadata_client() -> tuple[TronGridClient, ScriptedTransport]:
    symbol, decimals = trc20_metadata_payloads()
    return make_client(httpx.Response(200, json=symbol), httpx.Response(200, json=decimals))


def test_metadata_reads_the_symbol_and_the_decimals() -> None:
    client, _ = metadata_client()
    metadata = client.get_trc20_metadata(USDT_CONTRACT_NILE)
    assert metadata.symbol == "USDT"
    assert metadata.decimals == 6


def test_metadata_asks_the_contract_by_its_base58_address() -> None:
    client, transport = metadata_client()
    client.get_trc20_metadata(USDT_CONTRACT_NILE)
    body = json.loads(transport.requests[0].content)
    assert transport.requests[0].url.path == "/wallet/triggerconstantcontract"
    assert body["contract_address"] == USDT_CONTRACT_NILE
    assert body["function_selector"] == "symbol()"
    assert body["visible"] is True
    assert json.loads(transport.requests[1].content)["function_selector"] == "decimals()"


def test_the_placeholder_owner_is_used_when_no_caller_is_given() -> None:
    """Nothing is signed, so the caller is a formality the endpoint insists on."""
    client, transport = metadata_client()
    client.get_trc20_metadata(USDT_CONTRACT_NILE)
    assert json.loads(transport.requests[0].content)["owner_address"] == CONSTANT_CALL_OWNER


def test_a_caller_can_be_supplied_for_a_node_that_refuses_the_placeholder() -> None:
    client, transport = metadata_client()
    client.get_trc20_metadata(USDT_CONTRACT_NILE, owner=HOT_WALLET)
    assert json.loads(transport.requests[0].content)["owner_address"] == HOT_WALLET


def test_a_contract_that_answers_nothing_is_an_error_not_an_empty_symbol(
    slept: list[float],
) -> None:
    """The whole point is catching a wrong address, so "" must never pass."""
    client, _ = make_client(
        httpx.Response(200, json=constant_failure(message="contract not found"))
    )
    with pytest.raises(TronGridContractError, match="contract not found"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_the_nodes_reason_is_decoded_out_of_its_hex(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(200, json=constant_failure(message="no such method")))
    with pytest.raises(TronGridContractError, match=r"CONTRACT_VALIDATE_ERROR \(no such method\)"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_an_unreadable_reason_is_reported_as_it_arrived(slept: list[float]) -> None:
    refusal = {"result": {"code": "OTHER", "message": "zz"}}
    client, _ = make_client(httpx.Response(200, json=refusal))
    with pytest.raises(TronGridContractError, match=r"OTHER \(zz\)"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_a_silent_refusal_still_names_the_contract(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(200, json={}))
    with pytest.raises(TronGridContractError, match="no reason given"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_a_contract_error_is_not_retried(slept: list[float]) -> None:
    """Retrying a wrong contract address gets the same answer three times."""
    client, transport = make_client(httpx.Response(200, json=constant_failure()))
    with pytest.raises(TronGridContractError):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)
    assert len(transport.requests) == 1
    assert slept == []


def test_a_bytes32_symbol_is_read_as_well_as_a_string_one() -> None:
    """Some older tokens declare `bytes32`. Both readings are real encodings."""
    packed = b"USDT".ljust(32, b"\x00").hex()
    client, _ = make_client(
        httpx.Response(200, json=constant_result(packed)),
        httpx.Response(200, json=constant_result(abi_uint(6))),
    )
    assert client.get_trc20_metadata(USDT_CONTRACT_NILE).symbol == "USDT"


def test_a_longer_symbol_survives_its_padding() -> None:
    """The length word is authoritative; the padding is not part of the name."""
    client, _ = make_client(
        httpx.Response(200, json=constant_result(abi_string("Tether USD"))),
        httpx.Response(200, json=constant_result(abi_uint(6))),
    )
    assert client.get_trc20_metadata(USDT_CONTRACT_NILE).symbol == "Tether USD"


def test_a_string_whose_length_overruns_its_data_is_refused(slept: list[float]) -> None:
    """Truncated return data must not read as a shortened symbol."""
    truncated = f"{32:064x}{100:064x}{b'USDT'.ljust(32, b'0').hex()}"
    client, _ = make_client(httpx.Response(200, json=constant_result(truncated)))
    with pytest.raises(TronGridContractError, match="unreadable symbol"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_a_symbol_that_is_not_text_is_refused(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(200, json=constant_result("ff" * 32)))
    with pytest.raises(TronGridContractError, match="not UTF-8"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_an_all_zero_symbol_is_refused(slept: list[float]) -> None:
    """A contract that returns nothing but padding has not answered."""
    client, _ = make_client(httpx.Response(200, json=constant_result("00" * 32)))
    with pytest.raises(TronGridContractError, match="unreadable symbol"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_return_data_that_is_not_hexadecimal_is_refused(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(200, json=constant_result("nonsense")))
    with pytest.raises(TronGridContractError, match="not hexadecimal"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_a_truncated_word_is_refused_rather_than_padded(slept: list[float]) -> None:
    client, _ = make_client(httpx.Response(200, json=constant_result("41" * 8)))
    with pytest.raises(TronGridContractError, match="at least one 32-byte word"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_decimals_that_are_not_hexadecimal_are_refused(slept: list[float]) -> None:
    """Full-width junk. Reading it as anything would scale every amount."""
    client, _ = make_client(
        httpx.Response(200, json=constant_result(abi_string("USDT"))),
        httpx.Response(200, json=constant_result("z" * 64)),
    )
    with pytest.raises(TronGridContractError, match="not hexadecimal"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_a_truncated_decimals_word_is_refused(slept: list[float]) -> None:
    client, _ = make_client(
        httpx.Response(200, json=constant_result(abi_string("USDT"))),
        httpx.Response(200, json=constant_result("06")),
    )
    with pytest.raises(TronGridContractError, match="32-byte word"):
        client.get_trc20_metadata(USDT_CONTRACT_NILE)


def test_the_fake_and_the_client_agree_on_the_same_payload() -> None:
    """The fake is only worth anything while it answers what the client does.

    Both run the real decoders over the real encoding, so this fails the moment
    one of them starts inventing an answer.
    """
    client, _ = metadata_client()
    fake = FakeTronGrid()
    assert client.get_trc20_metadata(USDT_CONTRACT_MAINNET) == fake.get_trc20_metadata(
        USDT_CONTRACT_MAINNET
    )


def test_the_fake_refuses_a_contract_it_does_not_have() -> None:
    with pytest.raises(TronGridContractError):
        FakeTronGrid().get_trc20_metadata(USDT_CONTRACT_NILE)


def test_pad_address_matches_the_topic_encoding() -> None:
    """The same 32-byte left-padded form TronGrid puts in event topics."""
    padded = _pad_address(HOT_WALLET)
    assert len(padded) == 64
    assert padded == to_topic(HOT_WALLET)


# -- construction ----------------------------------------------------------


def test_an_api_key_is_sent_when_configured() -> None:
    client = TronGridClient(base_url=BASE_URL, api_key="tron-key")
    try:
        assert client._client.headers["TRON-PRO-API-KEY"] == "tron-key"
    finally:
        client.close()


def test_no_key_header_appears_when_none_is_configured() -> None:
    """Keyless access is legal and rate-limited; it must not send an empty key."""
    client = TronGridClient(base_url=BASE_URL, api_key=None)
    try:
        assert "TRON-PRO-API-KEY" not in client._client.headers
    finally:
        client.close()


def test_a_trailing_slash_on_the_base_url_is_trimmed() -> None:
    client = TronGridClient(base_url=f"{BASE_URL}/")
    try:
        assert client.base_url == BASE_URL
    finally:
        client.close()


def test_build_client_passes_the_endpoint_and_key_through() -> None:
    client = build_client(base_url=BASE_URL, api_key="tron-key")
    try:
        assert client.base_url == BASE_URL
        assert client._client.headers["TRON-PRO-API-KEY"] == "tron-key"
    finally:
        client.close()


def test_close_closes_the_underlying_client() -> None:
    client, _ = make_client(httpx.Response(200, json={}))
    client.close()
    assert client._client.is_closed
