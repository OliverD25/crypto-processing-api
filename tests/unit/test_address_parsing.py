"""Structurally broken addresses and invoices, one rejection at a time.

`test_addresses.py` and `test_bolt11.py` cover the inputs a person can type
wrong. These cover the ones a person cannot type at all: strings with a valid
checksum and a broken interior. Every one is built here rather than pasted,
because a hand-typed example fails its checksum first and the branch under
test never runs.

That distinction is the point. A checksum failure is a typo; a structurally
invalid witness program with a good checksum is either a bug in whatever
produced it or someone probing. Either way the parser must say which, and must
never let one through — accepting an unpayable address means a withdrawal that
burns a fee and delivers nothing.
"""

from __future__ import annotations

import pytest

from crypto_processing_api.core.addresses import (
    BECH32_CHARSET,
    BECH32_CONST,
    BECH32M_CONST,
    AddressError,
    _bech32_hrp_expand,
    _bech32_polymod,
    _convert_bits,
    decode_bolt11,
    tron_address_from_hex,
    validate_bitcoin_address,
)
from tests.fakes import mint_bolt11

REGTEST = "regtest"

#: 7 groups of timestamp, 104 where a 65-byte signature goes.
TIMESTAMP_GROUPS = 7
SIGNATURE_GROUPS = 104


def encode(hrp: str, data: list[int], *, const: int = BECH32_CONST) -> str:
    """A bech32 string with a correct checksum over whatever data it is given.

    The same construction the real encoder uses, so the checksum is genuine and
    the decoder gets as far as the structural check under test.
    """
    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0] * 6) ^ const
    tail = [(checksum >> 5 * (5 - index)) & 31 for index in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[value] for value in data + tail)


def tag(character: str, value: list[int]) -> list[int]:
    length = len(value)
    return [BECH32_CHARSET.index(character), length >> 5, length & 31, *value]


# -- bech32 addresses ------------------------------------------------------


def test_a_non_ascii_character_is_refused_before_anything_else() -> None:
    """A homoglyph in an address is a plausible way to be paid by mistake."""
    with pytest.raises(AddressError, match="printable ASCII"):
        validate_bitcoin_address("bcrt1qéwlh4s0wq0ke3", network=REGTEST)


def test_a_character_outside_the_bech32_alphabet_is_refused() -> None:
    """`b`, `i`, `o` and `1` are excluded from the alphabet on purpose, so that
    the characters people confuse cannot appear in the data part at all."""
    with pytest.raises(AddressError, match="invalid character"):
        validate_bitcoin_address("bcrt1bbbbbbbbb", network=REGTEST)


def test_an_address_with_no_witness_version_is_refused() -> None:
    """Six data characters are all checksum, so there is no payload at all."""
    with pytest.raises(AddressError, match="no witness version"):
        validate_bitcoin_address("bcrt1qqqqqq", network=REGTEST)


def test_a_witness_version_above_sixteen_is_refused() -> None:
    """Only 0 to 16 exist. A higher one is not a future version, it is a bug."""
    with pytest.raises(AddressError, match="invalid witness version"):
        validate_bitcoin_address(encode("bcrt", [17, 0, 0, 0]), network=REGTEST)


def test_a_witness_program_with_leftover_bits_is_refused() -> None:
    """Five-bit groups that do not pack into whole bytes. Accepting it would
    mean paying a script nobody can spend."""
    with pytest.raises(AddressError, match="invalid padding"):
        validate_bitcoin_address(encode("bcrt", [0, *([0] * 33)]), network=REGTEST)


def test_a_witness_program_longer_than_forty_bytes_is_refused() -> None:
    with pytest.raises(AddressError, match="invalid length"):
        validate_bitcoin_address(
            encode("bcrt", [1, *([0] * 66)], const=BECH32M_CONST), network=REGTEST
        )


def test_a_version_zero_program_that_is_not_twenty_or_thirty_two_bytes_is_refused() -> None:
    """P2WPKH is 20 and P2WSH is 32. Nothing else is a version 0 output."""
    with pytest.raises(AddressError, match="20 or 32 bytes"):
        validate_bitcoin_address(encode("bcrt", [0, *([0] * 40)]), network=REGTEST)


def test_a_version_one_address_must_use_the_bech32m_checksum() -> None:
    """BIP 350. Checking the wrong constant accepts taproot addresses that no
    node will pay, and rejects the ones that work."""
    with pytest.raises(AddressError, match="checksum does not match"):
        validate_bitcoin_address(encode("bcrt", [1, *([0] * 52)]), network=REGTEST)


def test_a_base58_string_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(AddressError, match="wrong length"):
        validate_bitcoin_address("1111111111", network=REGTEST)


def test_the_bit_converter_refuses_a_value_wider_than_its_input_size() -> None:
    """The fakes convert 8-bit program bytes down to 5-bit groups. A byte that
    is not a byte would silently produce a different address."""
    with pytest.raises(AddressError, match="invalid value"):
        _convert_bits([256], 8, 5)
    with pytest.raises(AddressError, match="invalid value"):
        _convert_bits([-1], 8, 5)


# -- BOLT11 invoices -------------------------------------------------------


def test_a_non_ascii_character_in_an_invoice_is_refused() -> None:
    with pytest.raises(AddressError, match="printable ASCII"):
        decode_bolt11("lnbcrt1éqqqqqqqq", network=REGTEST)


def test_a_mixed_case_invoice_is_refused() -> None:
    """The specification allows all-upper or all-lower, for QR efficiency.
    Mixed case means something has been edited by hand."""
    invoice = mint_bolt11(amount_sat=1_000)
    with pytest.raises(AddressError, match="mix upper and lower case"):
        decode_bolt11(invoice[:10].upper() + invoice[10:], network=REGTEST)


def test_a_character_outside_the_alphabet_in_an_invoice_is_refused() -> None:
    with pytest.raises(AddressError, match="invalid character"):
        decode_bolt11("lnbcrt1bbbbbbbbbb", network=REGTEST)


def test_an_invoice_too_short_to_hold_a_signature_is_refused() -> None:
    """111 groups is the floor: 7 of timestamp and 104 of signature. Anything
    shorter is not a truncated invoice, it is not an invoice."""
    with pytest.raises(AddressError, match="too short to carry a signature"):
        decode_bolt11(encode("lnbcrt", [0] * 50), network=REGTEST)


def test_an_invoice_with_no_payment_hash_is_refused() -> None:
    """The payment hash is what the node is asked about when a payout has to
    be proved unpaid. Without it a stuck withdrawal can never be resolved."""
    data = [0] * TIMESTAMP_GROUPS + tag("d", [0, 0, 0]) + [0] * SIGNATURE_GROUPS
    with pytest.raises(AddressError, match="no payment hash"):
        decode_bolt11(encode("lnbcrt", data), network=REGTEST)


def test_a_tagged_field_that_runs_past_the_end_is_refused() -> None:
    """A declared length longer than what follows. Reading it would hand the
    signature bytes back as a field value."""
    # Tag `d`, declared length 96 (3 * 32), with three groups behind it.
    overlong = [BECH32_CHARSET.index("d"), 3, 0, 0, 0, 0]
    data = [0] * TIMESTAMP_GROUPS + overlong + [0] * SIGNATURE_GROUPS
    with pytest.raises(AddressError, match="runs past the end"):
        decode_bolt11(encode("lnbcrt", data), network=REGTEST)


def test_an_amount_in_a_unit_nobody_defined_is_refused() -> None:
    with pytest.raises(AddressError, match="is not a BOLT11 amount"):
        decode_bolt11(mint_bolt11(amount_part="500z"), network=REGTEST)


def test_a_bare_number_is_whole_bitcoin() -> None:
    """No multiplier means BTC. Reading it as satoshis would be off by a
    hundred million."""
    invoice = decode_bolt11(mint_bolt11(amount_part="2"), network=REGTEST)
    assert invoice.amount_msat == 2 * 10**11
    assert invoice.amount_sat == 200_000_000


def test_a_pico_bitcoin_amount_must_be_a_multiple_of_ten() -> None:
    """One pico-bitcoin is a tenth of a millisatoshi, which does not exist."""
    with pytest.raises(AddressError, match="multiple of 10"):
        decode_bolt11(mint_bolt11(amount_part="15p"), network=REGTEST)


def test_a_pico_bitcoin_amount_becomes_millisatoshi() -> None:
    invoice = decode_bolt11(mint_bolt11(amount_part="10000p"), network=REGTEST)
    assert invoice.amount_msat == 1_000


def test_a_sub_satoshi_invoice_refuses_to_round() -> None:
    """Payable on Lightning, not representable in this ledger. Rounding it
    would make the amount sent disagree with the amount booked."""
    invoice = decode_bolt11(mint_bolt11(amount_part="1000p"), network=REGTEST)
    assert invoice.amount_msat == 100
    with pytest.raises(AddressError, match="not a whole number of satoshis"):
        _ = invoice.amount_sat


def test_an_amountless_invoice_has_no_satoshi_amount() -> None:
    invoice = decode_bolt11(mint_bolt11(), network=REGTEST)
    assert invoice.amount_msat is None
    assert invoice.amount_sat is None


# -- TRON hex forms --------------------------------------------------------


def test_a_tron_hex_form_that_is_not_hexadecimal_is_refused() -> None:
    """TronGrid event topics arrive as hex text. One that is the right length
    and not hexadecimal is corrupt, and guessing at it would name the wrong
    account in a verification failure."""
    with pytest.raises(AddressError, match="not hexadecimal"):
        tron_address_from_hex("41" + "zz" * 20)


@pytest.mark.parametrize("value", ["", "41", "0x" + "ab" * 30])
def test_a_tron_hex_form_of_the_wrong_length_is_refused(value: str) -> None:
    with pytest.raises(AddressError, match="not a TRON address in hex form"):
        tron_address_from_hex(value)
