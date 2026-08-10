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
    TronGridError,
    TronTransaction,
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


class FakeTronGrid:
    def __init__(self, *, block_height: int = 1_020) -> None:
        self.transactions: dict[str, dict[str, Any]] = {}
        self.block_height = block_height
        self.trx_balance_sun = 500 * 1_000_000
        self.trc20_balance = 1_000_000_000
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
