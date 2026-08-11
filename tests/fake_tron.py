"""In-memory TronGrid stand-in.

There is no TRON regtest, and a real Nile run needs a TronGrid key and faucet
funds. So the TRON paths are exercised against this fake, which is built from
the *shape* TronGrid actually returns — `gettransactioninfobyid` with a
`receipt`, a `log` array of raw event topics, and hex addresses — and then run
through the real `parse_transaction_info`.

That matters: the parser, the topic constant, the hex-to-base58 conversion and
the verifier are all real code here. What is faked is only the network.

Every payload shape below was copied from a payload TronGrid really sent during
the live Nile session of 2026-08-11, and the captures are committed under
`tests/fixtures/tron/`. `tests/unit/test_tron_payload_corpus.py` diffs this
module against them field by field, which is what stops the fake drifting back
into a shape only this repository has ever seen.
"""

from __future__ import annotations

import hashlib
from typing import Any

from crypto_processing_api.core.addresses import _base58_decode, _base58_encode
from crypto_processing_api.gateway.trongrid import (
    CONSTANT_CALL_OWNER,
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

#: The four-byte function selectors TronGrid echoes back in
#: `transaction.raw_data.contract[0].parameter.value.data`. Taken from the
#: captured Nile calls rather than recomputed, because the point of echoing
#: them is to match what a live node sent.
SELECTOR_SYMBOL = "95d89b41"
SELECTOR_DECIMALS = "313ce567"


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
    bandwidth_burned_trx: bool = False,
) -> dict[str, Any]:
    """A `gettransactioninfobyid` payload in TronGrid's real shape.

    `bandwidth_burned_trx` picks between the two receipts a TRC-20 transfer can
    come back with, and both were seen in the same live session. A sender with
    free bandwidth left gets `net_usage`, the bytes it spent. A sender without
    it burns TRX instead and gets `net_fee` plus a top-level `fee`, in sun, and
    no `net_usage` at all. Neither field is read by anything here — the reason
    to model both is that a reader of this file otherwise learns a receipt
    shape that only half of real transfers have.
    """
    receipt: dict[str, Any] = {
        "result": receipt_result,
        "energy_usage_total": 14_000,
        "origin_energy_usage": 14_000,
    }
    payload: dict[str, Any] = {
        "id": txid,
        "blockNumber": block_number,
        "blockTimeStamp": 1_760_000_000_000,
        "contractResult": ["0" * 64],
        "contract_address": to_hex(contract),
        "receipt": receipt,
    }
    if bandwidth_burned_trx:
        receipt["net_fee"] = 345_000
        payload["fee"] = 345_000
    else:
        receipt["net_usage"] = 345
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

    The standard head-and-tail encoding: a 32-byte offset of 32, a 32-byte
    length, then the characters right-padded to a multiple of 32. Confirmed
    against the live `symbol()` answers of both USDT contracts on 2026-08-11 —
    `tests/fixtures/tron/triggerconstantcontract_symbol_nile.json` is that
    exact string for `USDT`.
    """
    raw = text.encode("utf-8")
    padded = raw + b"\x00" * (-len(raw) % 32)
    return f"{32:064x}{len(raw):064x}{padded.hex()}"


def abi_uint(value: int) -> str:
    """One ABI-encoded `uint` return value, as a hex word."""
    return f"{value:064x}"


def _shaped_hex(seed: str, *, length: int) -> str:
    """Hex of the right length and shape, derived rather than invented.

    `txID` and `raw_data_hex` describe the unsigned transaction TronGrid builds
    around a constant call. Reproducing them means protobuf-encoding that
    transaction, which nothing here needs — no code reads either field. So they
    are hashes of the call, which keeps them deterministic and, because they
    cannot be decoded into anything, keeps anyone from mistaking one for a
    transaction that exists.
    """
    blob = b""
    while len(blob) * 2 < length:
        blob += hashlib.sha256(seed.encode() + blob).digest()
    return blob.hex()[:length]


def constant_result(
    *words: str,
    contract: str = USDT_CONTRACT,
    owner: str = CONSTANT_CALL_OWNER,
    selector: str = SELECTOR_SYMBOL,
) -> dict[str, Any]:
    """A `triggerconstantcontract` payload for a call that succeeded.

    The `transaction` half is the unsigned transaction the node built to
    simulate the call and is echoed back on every constant call, including the
    ones that only read. Only `constant_result` is read by the client, so its
    absence cost nothing in practice — but the fake claimed to be TronGrid's
    shape while omitting two thirds of the payload, and stage 5 of the Nile
    session reported sixteen missing fields because of it.

    Nile's answers carry no `energy_penalty`; mainnet's do. That difference is
    live, not a mistake here — see the corpus test.
    """
    return {
        "result": {"result": True},
        "energy_used": 1_082,
        "constant_result": list(words),
        "transaction": {
            "raw_data": {
                "contract": [
                    {
                        "parameter": {
                            "type_url": "type.googleapis.com/protocol.TriggerSmartContract",
                            "value": {
                                "contract_address": contract,
                                "data": selector,
                                "owner_address": owner,
                            },
                        },
                        "type": "TriggerSmartContract",
                    }
                ],
                "expiration": 1_760_000_060_000,
                "ref_block_bytes": "e0b6",
                "ref_block_hash": "5a6dbc75b7a5f6be",
                "timestamp": 1_760_000_000_000,
            },
            "raw_data_hex": _shaped_hex(f"{contract}:{owner}:{selector}:raw", length=270),
            "ret": [{}],
            "txID": _shaped_hex(f"{contract}:{owner}:{selector}:txid", length=64),
            "visible": True,
        },
    }


def constant_failure(*, code: str = "CONTRACT_VALIDATE_ERROR", message: str = "") -> dict[str, Any]:
    """A `triggerconstantcontract` payload for a call the node refused.

    Still uncaptured: the Nile session never asked a live node for a contract
    that is not there, so this remains the documented shape and TronGrid's own
    examples are the only evidence for it. A refused call is the one that turns
    a wrong contract address into an error rather than a quiet pass, so it is
    worth capturing on the next session.
    """
    return {"result": {"result": False, "code": code, "message": message.encode("utf-8").hex()}}


def trc20_metadata_payloads(*, symbol: str = "USDT", decimals: int = 6) -> list[dict[str, Any]]:
    """What `symbol()` then `decimals()` answer, in call order."""
    return [
        constant_result(abi_string(symbol), selector=SELECTOR_SYMBOL),
        constant_result(abi_uint(decimals), selector=SELECTOR_DECIMALS),
    ]


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
        bandwidth_burned_trx: bool = False,
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
            bandwidth_burned_trx=bandwidth_burned_trx,
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
