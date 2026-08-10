"""Bitcoin address validation.

Implemented here rather than pulled in as a dependency: it is about 120 lines
of well-specified arithmetic (BIP 173, BIP 350, base58check), and a withdrawal
path should not grow a transitive dependency tree to check a checksum.

The point of validating at request time is not elegance. Without it a
mistyped or wrong-network address is only discovered when BTCPay refuses the
payout — by which time the user's balance has been on hold for however long the
queue is, and someone has to file a support ticket for what should have been a
422.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class AddressKind(StrEnum):
    P2PKH = "p2pkh"
    P2SH = "p2sh"
    SEGWIT_V0 = "segwit_v0"
    SEGWIT_V1 = "segwit_v1"


@dataclass(frozen=True, slots=True)
class NetworkParams:
    name: str
    bech32_hrp: str
    p2pkh_version: int
    p2sh_version: int


#: testnet, signet and regtest share base58 version bytes; only the bech32
#: human-readable part tells regtest apart, which is exactly the confusion that
#: sends real coins to a test address.
NETWORKS: dict[str, NetworkParams] = {
    "mainnet": NetworkParams("mainnet", "bc", 0x00, 0x05),
    "testnet": NetworkParams("testnet", "tb", 0x6F, 0xC4),
    "signet": NetworkParams("signet", "tb", 0x6F, 0xC4),
    "regtest": NetworkParams("regtest", "bcrt", 0x6F, 0xC4),
}


class AddressError(ValueError):
    """The address is not usable on this network."""


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index in range(5):
            checksum ^= generator[index] if ((top >> index) & 1) else 0
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return (
        [ord(character) >> 5 for character in hrp]
        + [0]
        + [ord(character) & 31 for character in hrp]
    )


def _bech32_decode(address: str) -> tuple[str, list[int], int]:
    if any(ord(character) < 33 or ord(character) > 126 for character in address):
        raise AddressError("address contains characters outside printable ASCII")
    if address.lower() != address and address.upper() != address:
        raise AddressError("bech32 addresses must not mix upper and lower case")
    lowered = address.lower()
    separator = lowered.rfind("1")
    if separator < 1 or separator + 7 > len(lowered) or len(lowered) > 90:
        raise AddressError("malformed bech32 address")
    hrp = lowered[:separator]
    try:
        data = [BECH32_CHARSET.index(character) for character in lowered[separator + 1 :]]
    except ValueError as exc:
        raise AddressError("bech32 address contains an invalid character") from exc
    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    return hrp, data[:-6], checksum


def _convert_bits(data: list[int], from_bits: int, to_bits: int) -> list[int]:
    accumulator = 0
    bits = 0
    result: list[int] = []
    maximum = (1 << to_bits) - 1
    for value in data:
        if value < 0 or (value >> from_bits):
            raise AddressError("invalid value in witness program")
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & maximum)
    if bits >= from_bits or ((accumulator << (to_bits - bits)) & maximum):
        raise AddressError("witness program has invalid padding")
    return result


def _validate_bech32(address: str, params: NetworkParams) -> AddressKind:
    hrp, data, checksum = _bech32_decode(address)
    if hrp != params.bech32_hrp:
        raise AddressError(
            f"address is for '{hrp}', not {params.name} (expected '{params.bech32_hrp}')"
        )
    if not data:
        raise AddressError("bech32 address carries no witness version")

    witness_version = data[0]
    if witness_version > 16:
        raise AddressError("invalid witness version")
    # BIP 350: version 0 keeps the original constant, everything above it uses
    # bech32m. Checking the wrong one accepts addresses that no node will pay.
    expected = BECH32_CONST if witness_version == 0 else BECH32M_CONST
    if checksum != expected:
        raise AddressError("bech32 checksum does not match")

    program = _convert_bits(data[1:], 5, 8)
    if not 2 <= len(program) <= 40:
        raise AddressError("witness program has an invalid length")
    if witness_version == 0:
        if len(program) not in (20, 32):
            raise AddressError("version 0 witness program must be 20 or 32 bytes")
        return AddressKind.SEGWIT_V0
    return AddressKind.SEGWIT_V1


def _base58_decode(address: str) -> bytes:
    value = 0
    for character in address:
        index = BASE58_ALPHABET.find(character)
        if index < 0:
            raise AddressError("address contains a character outside base58")
        value = value * 58 + index
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    leading_zeros = len(address) - len(address.lstrip("1"))
    return b"\x00" * leading_zeros + raw


def _validate_base58(address: str, params: NetworkParams) -> AddressKind:
    decoded = _base58_decode(address)
    if len(decoded) != 25:
        raise AddressError("base58 address has the wrong length")
    payload, checksum = decoded[:21], decoded[21:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise AddressError("base58 checksum does not match")

    version = payload[0]
    if version == params.p2pkh_version:
        return AddressKind.P2PKH
    if version == params.p2sh_version:
        return AddressKind.P2SH
    raise AddressError(f"address version byte 0x{version:02x} is not valid on {params.name}")


def validate_bitcoin_address(address: str, *, network: str) -> AddressKind:
    """Return the address kind, or raise `AddressError`.

    Checksums are verified, so a single mistyped character is rejected, and the
    network prefix is checked, so a testnet address cannot be paid on mainnet.
    """
    if network not in NETWORKS:
        raise AddressError(f"unknown bitcoin network {network!r}")
    params = NETWORKS[network]

    candidate = address.strip()
    if not candidate:
        raise AddressError("address is empty")
    if candidate != address:
        raise AddressError("address has leading or trailing whitespace")

    if candidate.lower().startswith(f"{params.bech32_hrp}1"):
        return _validate_bech32(candidate, params)
    # Any other bech32-looking string is a different network's segwit address.
    if "1" in candidate and candidate.lower().split("1")[0] in {"bc", "tb", "bcrt"}:
        return _validate_bech32(candidate, params)
    return _validate_base58(candidate, params)
