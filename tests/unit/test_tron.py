"""TRON addresses, TronGrid parsing, and the full-tuple verifier.

The verifier tests walk one wrong field at a time, because the whole reason the
check exists is that "the transaction is real and succeeded" is true for every
one of these failures.
"""

from __future__ import annotations

import pytest

from crypto_processing_api.core.addresses import (
    AddressError,
    AddressKind,
    tron_address_from_hex,
    validate_tron_address,
)
from crypto_processing_api.gateway.trongrid import (
    USDT_CONTRACT_MAINNET,
    parse_transaction_info,
)
from crypto_processing_api.services.backends import TronTxVerifier
from tests.fake_tron import (
    DESTINATION,
    HOT_WALLET,
    OTHER_ADDRESS,
    USDT_CONTRACT,
    FakeTronGrid,
    to_hex,
    to_topic,
)

AMOUNT = 200_000_000  # 200 USDT in micro-units


# -- addresses -------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [USDT_CONTRACT_MAINNET, HOT_WALLET, OTHER_ADDRESS, DESTINATION],
)
def test_valid_tron_addresses(address: str) -> None:
    assert validate_tron_address(address) == AddressKind.TRON


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("", "empty"),
        ("   ", "whitespace"),
        ("0x742d35Cc6634C0532925a3b844Bc454e4438f44e", "an Ethereum address"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "a bitcoin address"),
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "a bitcoin P2PKH address"),
        ("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLS", "truncated"),
        ("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSEE", "too long"),
        ("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSF", "one character changed"),
        ("XQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", "wrong leading letter"),
        (" TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", "leading space"),
        ("TQn9Y2khEsLJW1ChVWFMSM0RDow5KcbLSE", "contains a zero, not in base58"),
    ],
)
def test_invalid_tron_addresses(address: str, why: str) -> None:
    with pytest.raises(AddressError):
        validate_tron_address(address)


def test_evm_address_gets_a_specific_message() -> None:
    """USDT exists on Ethereum too; paying that address on TRON loses the money."""
    with pytest.raises(AddressError, match="EVM-style"):
        validate_tron_address("0x742d35Cc6634C0532925a3b844Bc454e4438f44e")


def test_hex_forms_round_trip() -> None:
    for address in (USDT_CONTRACT_MAINNET, HOT_WALLET, DESTINATION):
        assert tron_address_from_hex(to_hex(address)) == address
        assert tron_address_from_hex(to_topic(address)) == address


def test_the_known_usdt_contract_hex_decodes_to_the_known_address() -> None:
    """An independent check on the converter, not just a round trip."""
    assert (
        tron_address_from_hex("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT_CONTRACT_MAINNET
    )


# -- parsing ---------------------------------------------------------------


def test_unknown_transaction_parses_to_none() -> None:
    """TronGrid answers an unknown id with `{}`, not a 404."""
    assert parse_transaction_info({}) is None


def test_transfer_event_is_extracted() -> None:
    fake = FakeTronGrid()
    fake.add_transfer("a" * 64, recipient=DESTINATION, amount=AMOUNT)
    transaction = fake.get_transaction("a" * 64)

    assert transaction is not None
    assert transaction.receipt_succeeded
    assert len(transaction.transfers) == 1
    transfer = transaction.transfers[0]
    assert transfer.contract == USDT_CONTRACT
    assert transfer.from_address == HOT_WALLET
    assert transfer.to_address == DESTINATION
    assert transfer.amount == AMOUNT


def test_an_out_of_energy_receipt_is_not_a_success() -> None:
    fake = FakeTronGrid()
    fake.add_transfer(
        "b" * 64, recipient=DESTINATION, amount=AMOUNT, receipt_result="OUT_OF_ENERGY"
    )
    transaction = fake.get_transaction("b" * 64)
    assert transaction is not None
    assert not transaction.receipt_succeeded


def test_a_failed_contract_result_is_not_a_success() -> None:
    fake = FakeTronGrid()
    fake.add_transfer("c" * 64, recipient=DESTINATION, amount=AMOUNT, failed=True)
    transaction = fake.get_transaction("c" * 64)
    assert transaction is not None
    assert not transaction.receipt_succeeded


# -- the verifier ----------------------------------------------------------


def make_verifier(fake: FakeTronGrid) -> TronTxVerifier:
    return TronTxVerifier(fake, contract_address=USDT_CONTRACT, hot_wallet_address=HOT_WALLET)


def test_a_matching_transfer_verifies() -> None:
    fake = FakeTronGrid()
    fake.add_transfer("d" * 64, recipient=DESTINATION, amount=AMOUNT)
    result = make_verifier(fake).verify(txid="d" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert result.ok
    assert result.transfer is not None
    assert result.block_number == 1_000


def test_unknown_txid_is_rejected() -> None:
    result = make_verifier(FakeTronGrid()).verify(
        txid="0" * 64, destination=DESTINATION, amount_net=AMOUNT
    )
    assert not result.ok
    assert "no transaction" in (result.reason or "")


def test_wrong_contract_is_rejected() -> None:
    """A transfer of some other TRC-20 token, for the right amount."""
    fake = FakeTronGrid()
    fake.add_transfer("e" * 64, recipient=DESTINATION, amount=AMOUNT, contract=OTHER_ADDRESS)
    result = make_verifier(fake).verify(txid="e" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "contract is" in (result.reason or "")


def test_wrong_sender_is_rejected() -> None:
    """Someone else's transfer to the right address for the right amount."""
    fake = FakeTronGrid()
    fake.add_transfer("f" * 64, recipient=DESTINATION, amount=AMOUNT, sender=OTHER_ADDRESS)
    result = make_verifier(fake).verify(txid="f" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "sender is" in (result.reason or "")


def test_wrong_recipient_is_rejected() -> None:
    """The previous withdrawal's txid: same wallet, same token, wrong user."""
    fake = FakeTronGrid()
    fake.add_transfer("1" * 64, recipient=OTHER_ADDRESS, amount=AMOUNT)
    result = make_verifier(fake).verify(txid="1" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "recipient is" in (result.reason or "")


def test_wrong_amount_is_rejected() -> None:
    fake = FakeTronGrid()
    fake.add_transfer("2" * 64, recipient=DESTINATION, amount=AMOUNT - 1)
    result = make_verifier(fake).verify(txid="2" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "amount is" in (result.reason or "")


def test_one_micro_unit_off_is_still_wrong() -> None:
    """Exact, not approximate. Six decimals means a micro-USDT is a real amount."""
    fake = FakeTronGrid()
    fake.add_transfer("3" * 64, recipient=DESTINATION, amount=AMOUNT + 1)
    assert (
        not make_verifier(fake).verify(txid="3" * 64, destination=DESTINATION, amount_net=AMOUNT).ok
    )


def test_failed_receipt_is_rejected() -> None:
    fake = FakeTronGrid()
    fake.add_transfer(
        "4" * 64, recipient=DESTINATION, amount=AMOUNT, receipt_result="OUT_OF_ENERGY"
    )
    result = make_verifier(fake).verify(txid="4" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "failed on chain" in (result.reason or "")


def test_missing_transfer_event_is_rejected() -> None:
    """A TRX transfer, or a contract call that moved no tokens."""
    fake = FakeTronGrid()
    fake.add_transfer("5" * 64, recipient=DESTINATION, amount=AMOUNT, include_transfer_event=False)
    result = make_verifier(fake).verify(txid="5" * 64, destination=DESTINATION, amount_net=AMOUNT)
    assert not result.ok
    assert "no TRC-20 Transfer event" in (result.reason or "")


def test_confirmations_are_counted_from_the_block_height() -> None:
    fake = FakeTronGrid(block_height=1_020)
    verifier = make_verifier(fake)
    assert verifier.confirmations(1_000) == 21
    assert verifier.confirmations(1_020) == 1
    assert verifier.confirmations(None) == 0


def test_confirmations_never_go_negative() -> None:
    """A node briefly behind the transaction must not read as -3 confirmations."""
    fake = FakeTronGrid(block_height=990)
    assert make_verifier(fake).confirmations(1_000) == 0
