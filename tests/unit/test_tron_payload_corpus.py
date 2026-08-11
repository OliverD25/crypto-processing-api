"""The TRON client, asserted against TronGrid's own bytes.

Every other TRON test drives `tests/fake_tron.py`. There is no TRON regtest, so
that fake is the only network the suite has — and a fake and the code that
reads it can agree with each other forever while both drift away from the
server. Nothing would go red.

These fixtures are raw TronGrid responses recorded on 2026-08-11 by
`scripts/verify_nile.py` during the live Nile session (see
`docs/operating/verification-log.md`). Nothing here builds a payload. The
transaction they describe is the withdrawal that session really sent, and the
amounts, addresses and block numbers asserted below are the ones on the Nile
chain.

The last section is the anti-drift half, and it is the reason this file matters
more than its assertions: it diffs `tests/fake_tron.py` against the recorded
shapes field by field, the same comparison stage 5 of the session makes. The
fake claimed to be built from what TronGrid returns while omitting twenty
fields, and that went unnoticed until a live payload arrived. It cannot go
unnoticed again.

Recapture with `python scripts/verify_nile.py`; never hand-edit a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from crypto_processing_api.gateway.trongrid import (
    USDT_CONTRACT_MAINNET,
    USDT_CONTRACT_NILE,
    TronGridClient,
    parse_transaction_info,
)
from scripts.verify_nile import shape_diff
from tests.fake_tron import (
    SELECTOR_DECIMALS,
    SELECTOR_SYMBOL,
    abi_string,
    abi_uint,
    constant_result,
    transaction_info,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tron"

#: The three Nile accounts the session used. Written out rather than read from
#: the fixtures: an address the test derives from the payload it is checking
#: proves nothing about the payload.
HOT_WALLET = "TQ5xFzzVSzVCftz3Qdapm1W3MAyw4exD5z"
USER_WALLET = "TVp7RheYmAcaHNzkbe9smK6e8xjpEdWYLM"
POOL_ADDRESS = "TDwrbF6vc6B3NevFSe8xcwoTztfiKAYjN3"

WITHDRAWAL_TXID = "19637a3a1c804e87e7f9f196bffa09a5d86586a0be4cf92afe731a5cbe519ec2"
WITHDRAWAL_BLOCK = 69_984_870
WITHDRAWAL_MICRO = 1_000_000

DEPOSIT_TXID = "da207aa2554d84092f5d9966a2bbc5487090b78e1e619a6672a14020d5831063"
DEPOSIT_BLOCK = 69_984_724
DEPOSIT_MICRO = 5_000_000

BASE_URL = "https://nile.trongrid.test"


def load(name: str) -> Any:
    """One recorded capture's response body."""
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        pytest.fail(
            f"missing fixture {path.name}. Recapture with a live session:\n"
            "  python scripts/verify_nile.py\n"
            "then copy the payload out of spike-evidence-nile/. Never hand-write one."
        )
    captured = json.loads(path.read_text(encoding="utf-8"))
    return captured["response"]


def client_answering(*payloads: Any) -> TronGridClient:
    """A real `TronGridClient` whose network hands back recorded bodies."""
    remaining = list(payloads)

    def handle(request: httpx.Request) -> httpx.Response:
        body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url=BASE_URL)
    return TronGridClient(base_url=BASE_URL, client=http)


# -- the corpus itself -----------------------------------------------------


def test_the_corpus_is_present() -> None:
    """A deleted corpus must fail loudly, not skip quietly."""
    assert FIXTURES.is_dir(), "tests/fixtures/tron/ is gone"
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["captured_at"] == "2026-08-11"
    assert len(manifest["files"]) == 9
    missing = [name for name in manifest["files"] if not (FIXTURES / f"{name}.json").is_file()]
    assert not missing, f"the manifest lists payloads that are not here: {missing}"


def test_no_capture_carries_a_header() -> None:
    """The TronGrid API key travels in a header, and headers are not recorded.

    This is the property that makes the corpus committable at all, so it is
    asserted rather than trusted to the capture code staying as it is.
    """
    for name in ("gettransactioninfobyid_withdrawal", "triggerconstantcontract_symbol_nile"):
        captured = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        assert set(captured) == {"source", "recorded_at", "request", "response"}


# -- the parser, over a transaction that exists ----------------------------


def test_the_recorded_withdrawal_parses_into_the_transfer_that_was_sent() -> None:
    """The full tuple the withdrawal verifier compares against, from real bytes.

    Contract, sender, recipient and amount all come out of hex the parser had
    to convert. A regression in any one of them accepts a transfer that moved
    someone else's money, or refuses one that moved the right money.
    """
    parsed = parse_transaction_info(load("gettransactioninfobyid_withdrawal"))

    assert parsed is not None
    assert parsed.txid == WITHDRAWAL_TXID
    assert parsed.block_number == WITHDRAWAL_BLOCK
    assert parsed.receipt_result == "SUCCESS"
    assert parsed.receipt_succeeded

    assert len(parsed.transfers) == 1
    transfer = parsed.transfers[0]
    assert transfer.contract == USDT_CONTRACT_NILE
    assert transfer.from_address == HOT_WALLET
    assert transfer.to_address == USER_WALLET
    assert transfer.amount == WITHDRAWAL_MICRO


def test_the_recorded_deposit_parses_into_the_payment_that_was_credited() -> None:
    """The other direction: the user's wallet paying a pool address."""
    parsed = parse_transaction_info(load("gettransactioninfobyid_deposit"))

    assert parsed is not None
    assert parsed.txid == DEPOSIT_TXID
    assert parsed.block_number == DEPOSIT_BLOCK
    assert parsed.receipt_succeeded

    transfer = parsed.transfers[0]
    assert transfer.contract == USDT_CONTRACT_NILE
    assert transfer.from_address == USER_WALLET
    assert transfer.to_address == POOL_ADDRESS
    assert transfer.amount == DEPOSIT_MICRO


def test_a_successful_transfer_carries_all_zero_return_data() -> None:
    """`contractResult` is return bytes, and TRON's are zeros on success.

    Read as a status they say nothing, and read as a boolean they say the
    opposite of what happened. The parser has to leave `contract_result` unset
    here — a SUCCESS receipt with no `result` field is the whole evidence that
    the transfer executed.
    """
    payload = load("gettransactioninfobyid_withdrawal")

    assert payload["contractResult"] == ["0" * 64]
    assert "result" not in payload

    parsed = parse_transaction_info(payload)
    assert parsed is not None
    assert parsed.contract_result is None
    assert parsed.receipt_succeeded


def test_a_transfer_reports_either_bandwidth_spent_or_bandwidth_burned() -> None:
    """Why the fake models two receipts rather than one.

    The two transfers in one session came back with different receipts: the
    withdrawal spent free bandwidth (`net_usage`), the deposit burned TRX for
    it (`net_fee`, plus a top-level `fee` in sun). A fake that only knows one
    of them describes half of TRON.
    """
    withdrawal = load("gettransactioninfobyid_withdrawal")["receipt"]
    deposit = load("gettransactioninfobyid_deposit")

    assert "net_usage" in withdrawal and "net_fee" not in withdrawal
    assert "net_fee" in deposit["receipt"] and "net_usage" not in deposit["receipt"]
    assert deposit["fee"] == deposit["receipt"]["net_fee"]


# -- the client, over recorded bodies --------------------------------------


def test_the_client_reads_the_recorded_withdrawal_through_its_own_endpoint() -> None:
    """`get_transaction` end to end: HTTP body in, `TronTransaction` out."""
    client = client_answering(load("gettransactioninfobyid_withdrawal"))
    try:
        transaction = client.get_transaction(WITHDRAWAL_TXID)
    finally:
        client.close()

    assert transaction is not None
    assert transaction.txid == WITHDRAWAL_TXID
    assert transaction.transfers[0].to_address == USER_WALLET


@pytest.mark.parametrize(
    ("network", "contract"),
    [("nile", USDT_CONTRACT_NILE), ("mainnet", USDT_CONTRACT_MAINNET)],
)
def test_both_usdt_contracts_answer_usdt_and_six_from_their_own_bytes(
    network: str, contract: str
) -> None:
    """The claim the whole Nile session rests on, re-run without a network.

    A format check on a contract address says the characters are well-formed
    and nothing about what is deployed there. These are the answers the real
    contracts gave, decoded by the same ABI decoders production uses.
    """
    client = client_answering(
        load(f"triggerconstantcontract_symbol_{network}"),
        load(f"triggerconstantcontract_decimals_{network}"),
    )
    try:
        metadata = client.get_trc20_metadata(contract)
    finally:
        client.close()

    assert metadata.symbol == "USDT"
    assert metadata.decimals == 6


def test_the_hot_wallets_trc20_balance_decodes_out_of_its_return_word() -> None:
    """The gas monitor's other reading. 500 USDT, in micro-USDT."""
    client = client_answering(load("triggerconstantcontract_balanceof"))
    try:
        assert client.get_trc20_balance(HOT_WALLET, USDT_CONTRACT_NILE) == 500_000_000
    finally:
        client.close()


def test_the_hot_wallets_trx_balance_is_read_in_sun() -> None:
    """500 TRX. Reading this wrongly is how a gas alert never fires."""
    client = client_answering(load("getaccount_hot_wallet"))
    try:
        assert client.get_trx_balance(HOT_WALLET) == 500 * 1_000_000
    finally:
        client.close()


def test_the_block_height_is_read_from_the_nested_header() -> None:
    """`block_header.raw_data.number`, three levels down and easy to lose.

    Returning 0 would make every transaction look unconfirmed forever; this is
    the block the session's withdrawal confirmed at.
    """
    client = client_answering(load("getnowblock"))
    try:
        assert client.get_block_height() == 69_984_909
    finally:
        client.close()


# -- anti-drift: the fake against the recording ----------------------------


def test_the_fakes_transaction_matches_the_recorded_one_field_for_field() -> None:
    """`FakeTronGrid` may not invent or omit a field TronGrid does not.

    This is stage 5 of the live session, frozen. A field the fake makes up is
    a payload no node sends, and a field it omits is one no test can react to —
    both let a parser bug survive the suite and meet real money instead.
    """
    live = load("gettransactioninfobyid_withdrawal")
    fake = transaction_info(
        txid=WITHDRAWAL_TXID,
        contract=USDT_CONTRACT_NILE,
        sender=HOT_WALLET,
        recipient=USER_WALLET,
        amount=WITHDRAWAL_MICRO,
        block_number=WITHDRAWAL_BLOCK,
    )
    assert shape_diff(live=live, fake=fake) == []


def test_the_fakes_burned_bandwidth_receipt_matches_the_recorded_one() -> None:
    """The second receipt shape, held to the deposit that produced it."""
    live = load("gettransactioninfobyid_deposit")
    fake = transaction_info(
        txid=DEPOSIT_TXID,
        contract=USDT_CONTRACT_NILE,
        sender=USER_WALLET,
        recipient=POOL_ADDRESS,
        amount=DEPOSIT_MICRO,
        block_number=DEPOSIT_BLOCK,
        bandwidth_burned_trx=True,
    )
    assert shape_diff(live=live, fake=fake) == []


@pytest.mark.parametrize(
    ("name", "words", "selector"),
    [
        ("triggerconstantcontract_symbol_nile", abi_string("USDT"), SELECTOR_SYMBOL),
        ("triggerconstantcontract_decimals_nile", abi_uint(6), SELECTOR_DECIMALS),
    ],
)
def test_the_fakes_constant_call_matches_the_recorded_one(
    name: str, words: str, selector: str
) -> None:
    """Including the echoed `transaction`, which the fake used to omit whole.

    Nothing reads it. That was the argument for leaving it out, and it is the
    wrong one: a fake that is a subset of the real payload teaches everyone who
    reads it a shape TronGrid does not send.
    """
    live = load(name)
    fake = constant_result(words, contract=USDT_CONTRACT_NILE, selector=selector)
    assert shape_diff(live=live, fake=fake) == []


def test_the_only_mainnet_difference_is_the_energy_penalty() -> None:
    """A real difference between the two networks, pinned so it stays the only one.

    Mainnet USDT charges an energy penalty and reports it; Nile's contract does
    not. Nothing reads the field, and the fake deliberately does not model it —
    but a second unexpected field appearing here is TronGrid moving, which is
    exactly what this corpus exists to notice.
    """
    live = load("triggerconstantcontract_symbol_mainnet")
    fake = constant_result(abi_string("USDT"), contract=USDT_CONTRACT_MAINNET)
    assert shape_diff(live=live, fake=fake) == [
        "+ `energy_penalty`: live payload has int, the fake has no such field"
    ]
