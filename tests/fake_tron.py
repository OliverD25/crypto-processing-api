"""In-memory TronGrid stand-in.

There is no TRON regtest, and a real Nile run needs a TronGrid key and faucet
funds. So the TRON paths are exercised against this fake, which is built from
the *shape* TronGrid actually returns — `gettransactioninfobyid` with a
`receipt`, a `log` array of raw event topics, and hex addresses — and then run
through the real `parse_transaction_info`.

That matters: the parser, the topic constant, the hex-to-base58 conversion and
the verifier are all real code here. What is faked is only the network.
"""

from __future__ import annotations

import hashlib
from typing import Any

from crypto_processing_api.core.addresses import _base58_decode, _base58_encode
from crypto_processing_api.gateway.trongrid import (
    TRANSFER_TOPIC,
    Trc20Metadata,
    TronGridContractError,
    TronGridError,
    TronTransaction,
    _decode_abi_string,
    _decode_abi_uint,
    parse_transaction_info,
)


def tron_address(seed: str) -> str:
    """Mint a checksum-valid TRON address.

    Invented addresses do not work: a hand-typed `T...` string fails its own
    checksum, which the validator correctly rejects, and the test then looks
    like a bug in the code under test rather than in the fixture.
    """
    payload = bytes([0x41]) + hashlib.sha256(seed.encode()).digest()[:20]
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _base58_encode(payload + checksum)


USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
HOT_WALLET = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"
OTHER_ADDRESS = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
DESTINATION = tron_address("user-destination")


def to_hex(address: str) -> str:
    """Base58 address to the 21-byte hex TronGrid puts in a log's `address`."""
    return _base58_decode(address)[:21].hex()


def to_topic(address: str) -> str:
    """Base58 address to the 32-byte left-padded hex used in event topics."""
    return _base58_decode(address)[1:21].hex().rjust(64, "0")


def transaction_info(
    *,
    txid: str,
    contract: str = USDT_CONTRACT,
    sender: str = HOT_WALLET,
    recipient: str,
    amount: int,
    block_number: int = 1_000,
    receipt_result: str = "SUCCESS",
    failed: bool = False,
    include_transfer_event: bool = True,
) -> dict[str, Any]:
    """A `gettransactioninfobyid` payload in TronGrid's real shape."""
    payload: dict[str, Any] = {
        "id": txid,
        "blockNumber": block_number,
        "blockTimeStamp": 1_760_000_000_000,
        "receipt": {"result": receipt_result, "energy_usage_total": 14_000, "net_fee": 0},
    }
    if failed:
        payload["result"] = "FAILED"
    if include_transfer_event:
        payload["log"] = [
            {
                "address": to_hex(contract)[2:],  # TronGrid omits the 41 prefix here
                "topics": [TRANSFER_TOPIC, to_topic(sender), to_topic(recipient)],
                "data": f"{amount:064x}",
            }
        ]
    return payload


def abi_string(text: str) -> str:
    """One ABI-encoded `string` return value, as a hex word sequence.

    UNVERIFIED UNTIL NILE. This is the standard head-and-tail encoding every
    current TRC-20 compiler emits — a 32-byte offset of 32, a 32-byte length,
    then the characters right-padded to a multiple of 32 — but no live
    `symbol()` response has ever been read into this repository. The Nile
    session (`scripts/verify_nile.py`, stage 5) prints the difference between
    a captured payload and this, and that diff is what makes it true.
    """
    raw = text.encode("utf-8")
    padded = raw + b"\x00" * (-len(raw) % 32)
    return f"{32:064x}{len(raw):064x}{padded.hex()}"


def abi_uint(value: int) -> str:
    """One ABI-encoded `uint` return value, as a hex word."""
    return f"{value:064x}"


def constant_result(*words: str) -> dict[str, Any]:
    """A `triggerconstantcontract` payload for a call that succeeded.

    UNVERIFIED UNTIL NILE. The fields around `constant_result` are the shape
    TronGrid documents; the exact set it sends back for a read-only call has
    not been captured from a live node yet. Only `constant_result` is read by
    the client, so a surprise elsewhere costs nothing — but it is still a
    difference the Nile session is meant to find and record.
    """
    return {
        "result": {"result": True},
        "energy_used": 1_082,
        "constant_result": list(words),
    }


def constant_failure(*, code: str = "CONTRACT_VALIDATE_ERROR", message: str = "") -> dict[str, Any]:
    """A `triggerconstantcontract` payload for a call the node refused.

    UNVERIFIED UNTIL NILE. The hex-encoded `message` is the documented shape
    and matches what TronGrid's own examples show.
    """
    return {"result": {"result": False, "code": code, "message": message.encode("utf-8").hex()}}


def trc20_metadata_payloads(*, symbol: str = "USDT", decimals: int = 6) -> list[dict[str, Any]]:
    """What `symbol()` then `decimals()` answer, in call order."""
    return [constant_result(abi_string(symbol)), constant_result(abi_uint(decimals))]


class FakeTronGrid:
    def __init__(self, *, block_height: int = 1_020) -> None:
        self.transactions: dict[str, dict[str, Any]] = {}
        self.block_height = block_height
        self.trx_balance_sun = 500 * 1_000_000
        self.trc20_balance = 1_000_000_000
        #: Keyed by contract address; `None` means "no such contract here".
        self.metadata: dict[str, tuple[str, int]] = {USDT_CONTRACT: ("USDT", 6)}
        self.calls: list[str] = []
        #: Set to an exception to make the next call of that name fail.
        self.fail_next: dict[str, TronGridError] = {}

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        error = self.fail_next.pop(name, None)
        if error is not None:
            raise error

    # -- test helpers -----------------------------------------------------

    def add_transfer(
        self,
        txid: str,
        *,
        recipient: str,
        amount: int,
        contract: str = USDT_CONTRACT,
        sender: str = HOT_WALLET,
        block_number: int = 1_000,
        receipt_result: str = "SUCCESS",
        failed: bool = False,
        include_transfer_event: bool = True,
    ) -> str:
        self.transactions[txid] = transaction_info(
            txid=txid,
            contract=contract,
            sender=sender,
            recipient=recipient,
            amount=amount,
            block_number=block_number,
            receipt_result=receipt_result,
            failed=failed,
            include_transfer_event=include_transfer_event,
        )
        return txid

    # -- TronGridGateway --------------------------------------------------

    def get_transaction(self, txid: str) -> TronTransaction | None:
        self._maybe_fail("get_transaction")
        return parse_transaction_info(self.transactions.get(txid, {}))

    def get_block_height(self) -> int:
        self._maybe_fail("get_block_height")
        return self.block_height

    def get_trx_balance(self, address: str) -> int:
        self._maybe_fail("get_trx_balance")
        return self.trx_balance_sun

    def get_trc20_balance(self, address: str, contract: str) -> int:
        self._maybe_fail("get_trc20_balance")
        return self.trc20_balance

    def get_trc20_metadata(self, contract: str, *, owner: str | None = None) -> Trc20Metadata:
        """The same decoders the real client uses, over the same payload shape.

        Returning `Trc20Metadata("USDT", 6)` directly would be a fake that
        always agrees with itself. Encoding the answer and decoding it back
        means a decoder that cannot read the standard encoding fails here too.
        """
        self._maybe_fail("get_trc20_metadata")
        known = self.metadata.get(contract)
        if known is None:
            raise TronGridContractError(
                f"{contract} did not answer symbol(): CONTRACT_VALIDATE_ERROR "
                "(no contract at this address)"
            )
        symbol_payload, decimals_payload = trc20_metadata_payloads(
            symbol=known[0], decimals=known[1]
        )
        return Trc20Metadata(
            symbol=_decode_abi_string(symbol_payload["constant_result"][0]),
            decimals=_decode_abi_uint(decimals_payload["constant_result"][0]),
        )
