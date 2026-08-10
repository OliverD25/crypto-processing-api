"""TronGrid client.

Used for one job in the MVP: proving that an operator-pasted transaction id is
the USDT transfer they claim it is. There is no automated sending here — the
BTCPay USDt plugin has no payout handler of any kind, so USDT leaves the hot
wallet by a human hand and this module is what checks their work.

Two TRON details the code depends on:

- addresses arrive as hex. In transaction bodies they are 21 bytes starting
  `41`; inside event topics they are left-padded to 32. Both are converted to
  the base58 form before comparison, so a mismatch reads as two `T...`
  addresses rather than two hex blobs.
- "the transaction succeeded" is not one flag. A TRC-20 transfer that ran out
  of energy is *included in a block* and reports a failed receipt, and a
  transaction whose receipt is fine can still have executed nothing. So the
  verifier checks the receipt, the contract result, and the presence of the
  Transfer event.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from crypto_processing_api.core.addresses import AddressError, tron_address_from_hex
from crypto_processing_api.core.redaction import get_logger

logger = get_logger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 20.0
GET_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 2.0, 8.0)

SUN_PER_TRX = 1_000_000

#: keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: Well-known mainnet USDT-TRC20 contract. Verified by round-tripping its hex
#: form (41a614f803b6fd780986a42c78ec9c7f77e6ded13c) through base58check.
USDT_CONTRACT_MAINNET = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

#: Commonly used Nile-testnet USDT contract. Format-verified only — this one is
#: NOT confirmed against a live Nile node, and the operator must check it
#: against whatever the USDt plugin is configured with before trusting it.
USDT_CONTRACT_NILE = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"

TRONGRID_MAINNET = "https://api.trongrid.io"
TRONGRID_NILE = "https://nile.trongrid.io"


class TronGridError(Exception):
    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TronGridUnavailable(TronGridError):
    retryable = True


class TronGridRateLimited(TronGridError):
    """429, or the 403 TronGrid returns after repeated violations."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class TronGridAuthError(TronGridError):
    """A missing or rejected API key. The free tier is unusable without one."""


class TronGridServerError(TronGridError):
    retryable = True


@dataclass(frozen=True, slots=True)
class TronTransfer:
    contract: str
    from_address: str
    to_address: str
    amount: int


@dataclass(frozen=True, slots=True)
class TronTransaction:
    txid: str
    block_number: int | None
    receipt_result: str | None
    contract_result: str | None
    transfers: tuple[TronTransfer, ...] = field(default_factory=tuple)

    @property
    def receipt_succeeded(self) -> bool:
        """SUCCESS on the receipt and, when present, on the contract result.

        An out-of-energy TRC-20 call is included in a block with
        `receipt.result = OUT_OF_ENERGY`; a reverted one reports `REVERT` in
        `contractRet`. Neither moved any tokens.
        """
        if (self.receipt_result or "").upper() != "SUCCESS":
            return False
        return self.contract_result is None or self.contract_result.upper() == "SUCCESS"


@runtime_checkable
class TronGridGateway(Protocol):
    def get_transaction(self, txid: str) -> TronTransaction | None: ...

    def get_block_height(self) -> int: ...

    def get_trx_balance(self, address: str) -> int: ...

    def get_trc20_balance(self, address: str, contract: str) -> int: ...


def parse_transaction_info(payload: dict[str, Any]) -> TronTransaction | None:
    """Turn `gettransactioninfobyid` output into something comparable.

    TronGrid answers an unknown transaction id with `{}`, not a 404.
    """
    if not payload or not payload.get("id"):
        return None

    receipt = payload.get("receipt") or {}
    transfers: list[TronTransfer] = []
    for entry in payload.get("log") or []:
        topics = entry.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue
        try:
            contract = tron_address_from_hex(str(entry.get("address", "")))
            sender = tron_address_from_hex(str(topics[1]))
            recipient = tron_address_from_hex(str(topics[2]))
        except AddressError as exc:
            logger.warning("trongrid.unparseable_transfer", txid=payload.get("id"), error=str(exc))
            continue
        data = str(entry.get("data") or "0")
        try:
            amount = int(data, 16)
        except ValueError:
            logger.warning("trongrid.unparseable_amount", txid=payload.get("id"))
            continue
        transfers.append(
            TronTransfer(
                contract=contract, from_address=sender, to_address=recipient, amount=amount
            )
        )

    contract_result: str | None = None
    ret = payload.get("contractResult")
    if isinstance(ret, list) and ret:
        contract_result = None  # raw return data, not a status
    if isinstance(payload.get("result"), str):
        # gettransactioninfobyid reports FAILED here and omits it on success.
        contract_result = "FAILED" if payload["result"].upper() == "FAILED" else "SUCCESS"

    return TronTransaction(
        txid=str(payload["id"]),
        block_number=payload.get("blockNumber"),
        receipt_result=receipt.get("result"),
        contract_result=contract_result,
        transfers=tuple(transfers),
    )


class TronGridClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT
            ),
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        code = response.status_code
        if code < 400:
            return
        if code == 429:
            header = response.headers.get("Retry-After")
            raise TronGridRateLimited(
                f"TronGrid rate limited {path}",
                retry_after=float(header) if header and header.isdigit() else None,
            )
        if code == 403:
            # TronGrid answers 403 for a bad key and also for a temporary block
            # after repeated rate-limit violations. Treated as retryable, and
            # the message says why it might not be.
            raise TronGridRateLimited(
                f"TronGrid refused {path} (403: bad API key, or throttled after "
                "repeated rate-limit violations)"
            )
        if code == 401:
            raise TronGridAuthError(f"TronGrid rejected the API key on {path}", status_code=code)
        if code in (502, 503, 504):
            raise TronGridUnavailable(f"TronGrid unavailable on {path}", status_code=code)
        raise TronGridServerError(f"TronGrid returned {code} on {path}", status_code=code)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        last: TronGridError | None = None
        for attempt in range(GET_ATTEMPTS):
            try:
                response = self._client.post(path, json=body)
                self._raise_for_status(response, path)
                return response.json()
            except httpx.HTTPError as exc:
                last = TronGridUnavailable(f"{type(exc).__name__} on {path}")
            except TronGridError as exc:
                if not exc.retryable:
                    raise
                last = exc
            if attempt == GET_ATTEMPTS - 1:
                break
            delay = BACKOFF_SECONDS[attempt]
            if isinstance(last, TronGridRateLimited) and last.retry_after:
                delay = max(delay, last.retry_after)
            time.sleep(delay * (0.75 + random.random() * 0.5))  # noqa: S311
            logger.warning("trongrid.retry", path=path, attempt=attempt + 1, error=str(last))
        raise last or TronGridUnavailable(f"POST {path} failed")

    def get_transaction(self, txid: str) -> TronTransaction | None:
        payload = self._post("/wallet/gettransactioninfobyid", {"value": txid})
        return parse_transaction_info(payload if isinstance(payload, dict) else {})

    def get_block_height(self) -> int:
        payload = self._post("/wallet/getnowblock", {})
        header = (payload or {}).get("block_header", {}).get("raw_data", {})
        number = header.get("number")
        if not isinstance(number, int):
            raise TronGridServerError("TronGrid did not report a block number")
        return number

    def get_trx_balance(self, address: str) -> int:
        """Balance in sun. 1 TRX = 1,000,000 sun."""
        payload = self._post("/wallet/getaccount", {"address": address, "visible": True})
        return int((payload or {}).get("balance") or 0)

    def get_trc20_balance(self, address: str, contract: str) -> int:
        payload = self._post(
            "/wallet/triggerconstantcontract",
            {
                "owner_address": address,
                "contract_address": contract,
                "function_selector": "balanceOf(address)",
                "parameter": _pad_address(address),
                "visible": True,
            },
        )
        results = (payload or {}).get("constant_result") or []
        if not results:
            return 0
        return int(results[0], 16)


def _pad_address(base58_address: str) -> str:
    """Encode a base58 address as a 32-byte ABI argument."""
    from crypto_processing_api.core.addresses import _base58_decode

    raw = _base58_decode(base58_address)[:21]
    return raw[1:].hex().rjust(64, "0")


def build_client(*, base_url: str, api_key: str | None) -> TronGridClient:
    return TronGridClient(base_url=base_url, api_key=api_key)
