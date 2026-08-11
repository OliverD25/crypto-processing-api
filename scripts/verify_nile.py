#!/usr/bin/env python
"""Guided live verification of the USDT paths against the TRON Nile testnet.

    python scripts/verify_nile.py            # the whole session, stage 1 onwards
    python scripts/verify_nile.py --stage 3  # resume at the withdrawal drill

Everything USDT in this repository is fake-tested. `tests/fake_tron.py` fakes
the network and runs the real parser, which is worth a great deal and is still
not the same as a payload TronGrid actually sent. There is no TRON regtest, so
the only way to close that gap is one live session on Nile with real testnet
money — and that session is the hard gate before v0.2.0.

This script is that session. It is interactive by design: it cannot send TRON
transactions, because nothing in this system can. Each stage says what it is
about to do, what the operator must do by hand, and what evidence it collected.

    1 preflight    config, both USDT contracts, the hot wallet, the USDt method
    2 deposit      a real Nile deposit, credited to the micro-USDT
    3 withdrawal   a real Nile withdrawal, verified live and confirmed 19 deep
    4 duplicate    the same txid against a second withdrawal, which is refused
    5 payloads     every captured payload against `tests/fake_tron.py`
    6 report       the verification-log section, ready to commit

Run it with `docs/operating/runbook-nile-verification.md` open. That page has
the manual half: the TronGrid key, the two wallets, the faucet, and the USDt
plugin's UI steps.

State lives in `spike-evidence-nile/` (gitignored) so `--stage N` resumes a
session in a new terminal. Every raw TronGrid answer is saved there too: the
whole point of going live is to find out where the real payloads differ from
the fake, and stage 5 prints exactly that difference.

Secrets never appear in the output. The TronGrid key travels in a header, which
is not captured; the admin API key is read from the environment and never
echoed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from crypto_processing_api.gateway.trongrid import (
    SUN_PER_TRX,
    TRONGRID_MAINNET,
    TRONGRID_NILE,
    USDT_CONTRACT_MAINNET,
    USDT_CONTRACT_NILE,
    Trc20Metadata,
    TronGridClient,
    TronGridError,
)
from crypto_processing_api.services.asset_registry import matches_usdt_tron

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "spike-evidence-nile"
STATE_FILE = EVIDENCE_DIR / "session.json"
GENERATED_ENV = REPO_ROOT / ".env.regtest.generated"
COMPOSE_FILES = (
    REPO_ROOT / "deploy" / "docker-compose.regtest.yml",
    REPO_ROOT / "deploy" / "docker-compose.nile.override.yml",
)

DEFAULT_API_URL = "http://127.0.0.1:8095"
ASSET = "USDT_TRC20"
MICRO = 1_000_000

#: 5 USDT in, then 2 USDT and 1.5 USDT back out. Both withdrawals clear the
#: seeded 1 USDT flat fee with something left to send, and the pair fits inside
#: one faucet claim with room to re-run the session.
DEPOSIT_MICRO = 5 * MICRO
WITHDRAW_MICRO = 2 * MICRO
DUPLICATE_WITHDRAW_MICRO = MICRO + MICRO // 2

USER = "nile-verification"

#: Nile blocks are ~3s, so 19 confirmations is about a minute — but the worker
#: polls on its own schedule and the operator is a human with a wallet app.
DEPOSIT_TIMEOUT = 1_800.0
CONFIRM_TIMEOUT = 900.0
POLL_INTERVAL = 5.0

STAGES: tuple[tuple[int, str, str], ...] = (
    (1, "preflight", "config, both USDT contracts, the hot wallet, the USDt payment method"),
    (2, "deposit", "a real Nile USDT deposit, credited to the micro-USDT"),
    (3, "withdrawal", "a real Nile USDT withdrawal, verified live and confirmed 19 blocks deep"),
    (4, "duplicate", "the same txid against a second withdrawal, which must be refused"),
    (5, "payloads", "every captured payload against tests/fake_tron.py"),
    (6, "report", "the verification-log section, ready to commit"),
)
FIRST_STAGE = STAGES[0][0]
LAST_STAGE = STAGES[-1][0]


class NileFailure(AssertionError):
    """Something this session claims to prove did not hold."""


def log(message: str) -> None:
    print(f"[nile] {message}", file=sys.stderr, flush=True)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def micros(amount: str) -> int:
    """A decimal USDT string as an exact number of micro-USDT."""
    return int((Decimal(amount) * MICRO).to_integral_value())


def usdt(amount_micro: int) -> str:
    return f"{Decimal(amount_micro) / MICRO:.6f}"


# -- configuration ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NileConfig:
    """What this session was pointed at, read from the environment.

    Deliberately not `Settings`. This runs on the host while the service runs
    in a container, so the two read different environments — and a `Settings`
    that refuses to build because the host has no `DATABASE_URL` would stop the
    session over something it does not need. What matters is that the values
    match the container's, and the preflight checks that directly.
    """

    api_url: str
    network: str
    nile_endpoint: str
    nile_contract: str
    mainnet_endpoint: str
    mainnet_contract: str
    trongrid_key: str | None
    hot_wallet: str | None
    confirmations: int


def load_config(env: Mapping[str, str]) -> NileConfig:
    raw_confirmations = (env.get("TRON_CONFIRMATIONS") or "19").strip()
    if not raw_confirmations.isdigit() or int(raw_confirmations) < 1:
        raise NileFailure(f"TRON_CONFIRMATIONS is {raw_confirmations!r}, expected a whole number")
    return NileConfig(
        api_url=(env.get("CPAPI_URL") or DEFAULT_API_URL).rstrip("/"),
        network=(env.get("TRON_NETWORK") or "mainnet").strip().lower(),
        nile_endpoint=(env.get("TRONGRID_BASE_URL") or TRONGRID_NILE).rstrip("/"),
        nile_contract=(env.get("USDT_CONTRACT_ADDRESS") or USDT_CONTRACT_NILE).strip(),
        mainnet_endpoint=TRONGRID_MAINNET,
        mainnet_contract=USDT_CONTRACT_MAINNET,
        trongrid_key=(env.get("TRONGRID_API_KEY") or "").strip() or None,
        hot_wallet=(env.get("TRON_HOT_WALLET_ADDRESS") or "").strip() or None,
        confirmations=int(raw_confirmations),
    )


def guard_nile_only(config: NileConfig) -> None:
    """Refuse to run the drills against anything but Nile.

    The drills ask an operator to pay an invoice and to send a transfer by
    hand. Pointed at mainnet that is real money leaving a real wallet because
    a script printed an address, so every part of the configuration that could
    say "mainnet" has to say "nile" before anything is created.
    """
    problems: list[str] = []
    if config.network != "nile":
        problems.append(f"TRON_NETWORK is {config.network!r}, expected 'nile'")
    if "nile" not in config.nile_endpoint.lower():
        problems.append(f"{config.nile_endpoint} is not a Nile TronGrid endpoint")
    if config.nile_contract == USDT_CONTRACT_MAINNET:
        problems.append("USDT_CONTRACT_ADDRESS is the mainnet USDT contract")
    if problems:
        raise NileFailure(
            "refusing to run the drills: " + "; ".join(problems) + ". This session sends real "
            "transfers on whatever network it is pointed at, so it only runs on Nile."
        )


def read_generated_env(path: Path = GENERATED_ENV) -> dict[str, str]:
    if not path.is_file():
        raise NileFailure(f"{path} not found — run scripts/bootstrap_btcpay.py first")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


def load_env_file(path: Path) -> dict[str, str]:
    """The same `.env` docker compose reads, so one file configures both.

    Values already in the environment win, because that is what compose does
    too and a session where the script and the stack disagree is worse than
    one that will not start.
    """
    if not path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        loaded[key.strip()] = value.strip().strip("'\"")
    return loaded


# -- evidence --------------------------------------------------------------


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "payload"


class Evidence:
    """Every raw payload this session saw, on disk and reloadable.

    Reloadable matters: `--stage 5` in a fresh terminal has to diff payloads a
    previous run captured, and a capture that only lived in memory would make
    the resumable half of this script a lie.

    Request bodies are kept because they say what was asked. Headers are not,
    and that is where the API key is.
    """

    def __init__(self, directory: Path = EVIDENCE_DIR) -> None:
        self.directory = directory
        self.entries: list[dict[str, Any]] = []
        earlier = sorted(directory.glob("[0-9][0-9][0-9]-*.json")) if directory.is_dir() else []
        for path in earlier:
            try:
                self.entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                log(f"  ignoring an unreadable evidence file: {path.name}")

    def record(self, *, source: str, request: Any, response: Any) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "source": source,
            "recorded_at": now(),
            "request": request,
            "response": response,
        }
        path = self.directory / f"{len(self.entries) + 1:03d}-{slugify(source)}.json"
        path.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
        self.entries.append(entry)
        return path

    def responses(self, source_contains: str) -> list[Any]:
        return [
            entry["response"]
            for entry in self.entries
            if source_contains in str(entry.get("source", ""))
        ]

    def transaction(self, txid: str) -> dict[str, Any] | None:
        """The captured `gettransactioninfobyid` answer for one transaction."""
        for response in self.responses("gettransactioninfobyid"):
            if isinstance(response, dict) and str(response.get("id", "")).lower() == txid.lower():
                return response
        return None


class CapturingTronGrid(TronGridClient):
    """A real TronGrid client that keeps every answer it was given.

    Subclassed rather than wrapped because the capture has to happen where the
    raw payload still exists. Everything public on the client returns parsed
    objects, and the parsed object is exactly what this session exists to check
    the raw payload against.
    """

    def __init__(self, *, base_url: str, api_key: str | None, evidence: Evidence, label: str):
        super().__init__(base_url=base_url, api_key=api_key)
        self._evidence = evidence
        self._label = label

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        payload = super()._post(path, body)
        self._evidence.record(source=f"{self._label}{path}", request=body, response=payload)
        return payload


# -- payload shapes --------------------------------------------------------


def shape_of(value: Any, prefix: str = "") -> dict[str, str]:
    """Every field in a payload, mapped to the kind of value it holds.

    Lists collapse to their first element. TronGrid's `log` array repeats one
    shape, and reporting `log[0]` and `log[1]` apart would drown a real
    difference in noise.
    """
    shape: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            shape.update(shape_of(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        shape[prefix or "(root)"] = "list"
        if value:
            shape.update(shape_of(value[0], f"{prefix}[0]"))
    else:
        shape[prefix or "(root)"] = type(value).__name__
    return shape


def shape_diff(*, live: Any, fake: Any) -> list[str]:
    """Where a captured payload and `tests/fake_tron.py` disagree.

    The output is the input for fixing the fake: one line per field, saying
    which side has it and what it holds.
    """
    live_shape, fake_shape = shape_of(live), shape_of(fake)
    lines: list[str] = []
    for key in sorted(set(live_shape) | set(fake_shape)):
        seen, expected = live_shape.get(key), fake_shape.get(key)
        if seen == expected:
            continue
        if expected is None:
            lines.append(f"+ `{key}`: live payload has {seen}, the fake has no such field")
        elif seen is None:
            lines.append(f"- `{key}`: the fake has {expected}, the live payload has no such field")
        else:
            lines.append(f"~ `{key}`: live payload has {seen}, the fake has {expected}")
    return lines


def fake_transaction_payload(**kwargs: Any) -> dict[str, Any]:
    """What `tests/fake_tron.py` would have produced for the same transfer.

    Imported here rather than at the top of the file: `tests` is not on the
    path when this runs as `python scripts/verify_nile.py`, and it is only
    needed by the one stage that compares against it.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.fake_tron import transaction_info

    return transaction_info(**kwargs)


def fake_metadata_payloads() -> list[dict[str, Any]]:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.fake_tron import trc20_metadata_payloads

    return trc20_metadata_payloads()


# -- the record the report is written from ---------------------------------


@dataclass
class Record:
    """Everything the verification log needs, and nothing a later stage cannot
    re-read from disk."""

    started_at: str = ""
    finished_at: str = ""
    network: str = "nile"
    nile_endpoint: str = ""
    nile_contract: str = ""
    nile_symbol: str = ""
    nile_decimals: int = 0
    mainnet_contract: str = ""
    mainnet_symbol: str = ""
    mainnet_decimals: int = 0
    preflight_block_height: int = 0
    confirmations_required: int = 0
    hot_wallet: str = ""
    hot_trx_sun: int = 0
    hot_usdt_micro: int = 0
    btcpay_payment_methods: list[str] = field(default_factory=list)

    deposit_id: str = ""
    deposit_address: str = ""
    deposit_expected_micro: int = 0
    deposit_credited_micro: int = 0
    deposit_txid: str = ""
    deposit_requested_at: str = ""
    deposit_settled_at: str = ""

    withdrawal_id: str = ""
    withdrawal_destination: str = ""
    withdrawal_gross_micro: int = 0
    withdrawal_fee_micro: int = 0
    withdrawal_net_micro: int = 0
    withdrawal_txid: str = ""
    withdrawal_tx_block: int = 0
    withdrawal_broadcast_at: str = ""
    withdrawal_confirmed_at: str = ""
    withdrawal_confirm_height: int = 0
    withdrawal_confirmations_seen: int = 0
    balance_before_available: int = 0
    balance_before_held: int = 0

    duplicate_withdrawal_id: str = ""
    duplicate_status: int = 0
    duplicate_detail: str = ""
    duplicate_released: bool = False

    shape_diffs: list[str] = field(default_factory=list)
    stages_done: list[int] = field(default_factory=list)

    def mark_done(self, stage: int) -> None:
        if stage not in self.stages_done:
            self.stages_done.append(stage)
            self.stages_done.sort()


def load_record(path: Path = STATE_FILE) -> Record:
    if not path.is_file():
        return Record()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {item.name for item in fields(Record)}
    return Record(**{key: value for key, value in raw.items() if key in known})


def save_record(record: Record, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True), encoding="utf-8")


# -- the service's HTTP surface --------------------------------------------


class Api:
    def __init__(self, base_url: str, key: str) -> None:
        self.client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=60.0
        )

    def _json(self, response: httpx.Response, expected: int, what: str) -> dict[str, Any]:
        if response.status_code != expected:
            raise NileFailure(f"{what}: {response.status_code} {response.text[:400]}")
        return dict(response.json())

    def create_deposit(self, expected_micro: int, key: str) -> dict[str, Any]:
        response = self.client.post(
            "/v1/deposits",
            json={
                "external_user_id": USER,
                "asset": ASSET,
                "expected_amount": str(expected_micro),
            },
            headers={"Idempotency-Key": key},
        )
        return self._json(response, 201, "deposit creation failed")

    def deposit(self, deposit_id: str) -> dict[str, Any]:
        return self._json(self.client.get(f"/v1/deposits/{deposit_id}"), 200, "deposit read failed")

    def create_withdrawal(self, gross_micro: int, destination: str, key: str) -> dict[str, Any]:
        response = self.client.post(
            "/v1/withdrawals",
            json={
                "external_user_id": USER,
                "asset": ASSET,
                "amount": str(gross_micro),
                "destination_address": destination,
            },
            headers={"Idempotency-Key": key},
        )
        return self._json(response, 201, "withdrawal request failed")

    def withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        return self._json(
            self.client.get(f"/v1/withdrawals/{withdrawal_id}"), 200, "withdrawal read failed"
        )

    def approve(self, withdrawal_id: str) -> dict[str, Any]:
        return self._json(
            self.client.post(f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}),
            200,
            "approve failed",
        )

    def mark_broadcast(self, withdrawal_id: str, txid: str) -> tuple[int, str]:
        """Status and detail, because a refusal here is a result, not an error."""
        response = self.client.post(
            f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast", json={"txid": txid}
        )
        try:
            detail = str(response.json().get("detail", ""))
        except ValueError:
            detail = response.text[:400]
        return response.status_code, detail

    def release(self, withdrawal_id: str, attestation: str) -> dict[str, Any]:
        return self._json(
            self.client.post(
                f"/v1/admin/withdrawals/{withdrawal_id}/release",
                json={"attestation": attestation},
            ),
            200,
            "release failed",
        )

    def balances(self) -> dict[str, int]:
        body = self._json(self.client.get(f"/v1/users/{USER}/balances"), 200, "balance read failed")
        for entry in body["balances"]:
            if entry["asset"] == ASSET:
                return {
                    "available": micros(entry["available"]),
                    "held": micros(entry["held"]),
                }
        return {"available": 0, "held": 0}


def compose(*args: str, check: bool = True) -> str:
    argv = ["docker", "compose"]
    for path in COMPOSE_FILES:
        argv += ["-f", str(path)]
    argv += list(args)
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv, capture_output=True, text=True, check=False, cwd=REPO_ROOT
    )
    if check and result.returncode != 0:
        raise NileFailure(f"{' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return (result.stdout or "").strip()


def admin_key(env: Mapping[str, str]) -> str:
    """The admin key from the environment, or a fresh one from the container.

    Never printed either way. An operator who wants to reuse one across
    terminals exports `CPAPI_ADMIN_KEY`; everyone else gets one made here.
    """
    existing = (env.get("CPAPI_ADMIN_KEY") or "").strip()
    if existing:
        log("  using the admin key from CPAPI_ADMIN_KEY")
        return existing
    log("  minting an admin key inside the api container")
    key = compose(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "crypto_processing_api.cli",
        "create-api-key",
        "--name",
        "nile-verification",
        "--scope",
        "admin",
    )
    if not key.startswith("cpk_"):
        raise NileFailure("the api container did not return an API key")
    return key


# -- talking to the operator -----------------------------------------------


def ask(question: str) -> str:
    if not sys.stdin.isatty():
        raise NileFailure(
            f"this stage needs an answer to: {question}. Run it in a terminal — the "
            "drills cannot proceed without an operator sending real transfers."
        )
    print(f"\n[nile] {question}", file=sys.stderr, flush=True)
    return input("       > ").strip()


def ask_until(question: str, *, valid: Callable[[str], bool], hint: str) -> str:
    while True:
        answer = ask(question)
        if valid(answer):
            return answer
        log(f"  {hint}")


def confirm(instruction: str) -> None:
    ask(f"{instruction}\n       Press Enter when that is done.")


def looks_like_tron_address(value: str) -> bool:
    return value.startswith("T") and 30 <= len(value) <= 40


def looks_like_txid(value: str) -> bool:
    candidate = value.lower().removeprefix("0x")
    return len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate)


def wait_until(
    check: Callable[[], Any], *, timeout: float, description: str, interval: float = POLL_INTERVAL
) -> Any:
    deadline = time.monotonic() + timeout
    log(f"  waiting for {description} (up to {int(timeout / 60)} minutes)")
    last: Any = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise NileFailure(f"timed out after {timeout:.0f}s waiting for {description} (last: {last!r})")


# -- assertions ------------------------------------------------------------


def assert_usdt_contract(*, network: str, contract: str, metadata: Trc20Metadata) -> None:
    """The moment a format-checked constant becomes a confirmed one."""
    if metadata.symbol != "USDT" or metadata.decimals != 6:
        raise NileFailure(
            f"{contract} on {network} answers symbol()={metadata.symbol!r} and "
            f"decimals()={metadata.decimals}, expected 'USDT' and 6. Nothing after this "
            "would mean anything: the address configured for USDT is not the USDT "
            "contract. Check it against what the USDt plugin is pointed at."
        )
    log(f"  {network}: {contract} answers USDT / 6 decimals")


def assert_usdt_payment_method(methods: list[str]) -> None:
    if not any(matches_usdt_tron(method) for method in methods):
        raise NileFailure(
            "the BTCPay store has no enabled USDT-on-TRON payment method; enabled "
            f"methods are {methods or '[]'}. Install the USDt plugin and configure it "
            "for Nile — deploy/docker-compose.nile.override.yml lists the four UI steps."
        )


def read_metadata(
    client: TronGridClient, contract: str, *, fallback_owner: str | None
) -> Trc20Metadata:
    """`symbol()`/`decimals()`, retried once as a real address if needed.

    The default caller is TronWeb's all-zero placeholder. Both Nile and mainnet
    TronGrid accepted it on 2026-08-11, so the fallback is no longer the
    expected path — it stays because a different node or a paid provider may
    still refuse it, and losing a booked session to a formality is the wrong
    trade.
    """
    try:
        return client.get_trc20_metadata(contract)
    except TronGridError as refused:
        if not fallback_owner:
            raise
        log(f"  the placeholder caller was refused ({refused}); retrying as the hot wallet")
        return client.get_trc20_metadata(contract, owner=fallback_owner)


# -- stages ----------------------------------------------------------------


@dataclass
class Context:
    config: NileConfig
    evidence: Evidence
    record: Record
    api: Api
    nile: CapturingTronGrid
    mainnet: CapturingTronGrid


def stage_preflight(ctx: Context) -> None:
    config, record = ctx.config, ctx.record
    log("what this proves: the configuration is Nile's, both USDT contracts are")
    log("the real ones, the hot wallet can pay for gas, and BTCPay can take USDT.")
    log("nothing moves in this stage and no funds are needed.")

    log(f"  API                  {config.api_url}")
    log(f"  TRON_NETWORK         {config.network}")
    log(f"  TronGrid (Nile)      {config.nile_endpoint}")
    log(f"  TRONGRID_API_KEY     {'present' if config.trongrid_key else 'MISSING'}")
    log(f"  USDT contract (Nile) {config.nile_contract}")
    log(f"  hot wallet           {config.hot_wallet or 'MISSING'}")
    log(f"  TRON_CONFIRMATIONS   {config.confirmations}")

    if not config.trongrid_key:
        raise NileFailure(
            "TRONGRID_API_KEY is not set. Keyless TronGrid is throttled unpredictably, "
            "and what gets throttled is the check that a withdrawal really happened."
        )
    if not config.hot_wallet:
        raise NileFailure("TRON_HOT_WALLET_ADDRESS is not set; nothing can be verified without it")

    check_container_agrees(config)

    record.started_at = record.started_at or now()
    record.network = config.network
    record.nile_endpoint = config.nile_endpoint
    record.nile_contract = config.nile_contract
    record.mainnet_contract = config.mainnet_contract
    record.hot_wallet = config.hot_wallet
    record.confirmations_required = config.confirmations

    height = ctx.nile.get_block_height()
    record.preflight_block_height = height
    log(f"  the key works: Nile is at block {height}")

    nile_metadata = read_metadata(ctx.nile, config.nile_contract, fallback_owner=config.hot_wallet)
    assert_usdt_contract(network="nile", contract=config.nile_contract, metadata=nile_metadata)
    record.nile_symbol, record.nile_decimals = nile_metadata.symbol, nile_metadata.decimals

    log("  reading the mainnet contract too — read-only, and it touches no funds")
    mainnet_metadata = read_metadata(
        ctx.mainnet, config.mainnet_contract, fallback_owner=config.hot_wallet
    )
    assert_usdt_contract(
        network="mainnet", contract=config.mainnet_contract, metadata=mainnet_metadata
    )
    record.mainnet_symbol = mainnet_metadata.symbol
    record.mainnet_decimals = mainnet_metadata.decimals

    record.hot_trx_sun = ctx.nile.get_trx_balance(config.hot_wallet)
    record.hot_usdt_micro = ctx.nile.get_trc20_balance(config.hot_wallet, config.nile_contract)
    log(
        f"  hot wallet holds {record.hot_trx_sun / SUN_PER_TRX:.6f} TRX and "
        f"{usdt(record.hot_usdt_micro)} USDT"
    )
    if record.hot_trx_sun < 100 * SUN_PER_TRX:
        log("  WARNING: under 100 TRX. A TRC-20 transfer that runs out of energy is")
        log("  included in a block and moves nothing. Top up from the faucet first.")
    if record.hot_usdt_micro < WITHDRAW_MICRO + DUPLICATE_WITHDRAW_MICRO:
        log("  WARNING: the hot wallet may not hold enough USDT for both withdrawals.")

    record.btcpay_payment_methods = btcpay_payment_methods(ctx.evidence)
    log(f"  BTCPay store payment methods: {', '.join(record.btcpay_payment_methods) or 'none'}")
    assert_usdt_payment_method(record.btcpay_payment_methods)
    log("  preflight passed; USDT_CONTRACT_NILE is now confirmed against a live node")


def check_container_agrees(config: NileConfig) -> None:
    """The script and the api container have to be reading the same TRON.

    They read different environments — one the host's, one compose's — and a
    mismatch shows up as a withdrawal that fails verification for reasons
    nobody can see. Best effort: no docker here is a warning, a real
    disagreement is not.
    """
    try:
        seen = compose(
            "exec",
            "-T",
            "api",
            "printenv",
            "TRON_NETWORK",
            "USDT_CONTRACT_ADDRESS",
            "TRON_HOT_WALLET_ADDRESS",
            "TRON_CONFIRMATIONS",
        ).splitlines()
    except NileFailure as unavailable:
        log(f"  could not read the api container's config ({unavailable}); skipping the")
        log("  cross-check. If a drill fails later, compare them by hand first.")
        return
    if len(seen) != 4:
        log("  the api container did not report all four TRON variables; skipping the cross-check")
        return

    expected = (
        config.network,
        config.nile_contract,
        config.hot_wallet or "",
        str(config.confirmations),
    )
    names = (
        "TRON_NETWORK",
        "USDT_CONTRACT_ADDRESS",
        "TRON_HOT_WALLET_ADDRESS",
        "TRON_CONFIRMATIONS",
    )
    disagreements = [
        f"{name}: this script has {mine!r}, the api container has {theirs.strip()!r}"
        for name, mine, theirs in zip(names, expected, seen, strict=True)
        if mine != theirs.strip()
    ]
    if disagreements:
        raise NileFailure(
            "the script and the api container disagree about TRON: "
            + "; ".join(disagreements)
            + ". Fix the environment and recreate api and worker before going further."
        )
    log("  the api container reads the same TRON configuration")


def btcpay_payment_methods(evidence: Evidence) -> list[str]:
    """The enabled payment methods BTCPay reports for the store.

    Asked of Greenfield directly rather than of this service, because the
    question is whether the USDt plugin is installed and configured at all —
    and the service's own answer is downstream of that.
    """
    generated = read_generated_env()
    response = httpx.get(
        f"{generated['BTCPAY_PUBLIC_URL']}/api/v1/stores/{generated['BTCPAY_STORE_ID']}"
        "/payment-methods",
        params={"onlyEnabled": "true"},
        headers={"Authorization": f"token {generated['BTCPAY_API_KEY']}"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise NileFailure(
            f"BTCPay refused the payment-methods call: {response.status_code} {response.text[:300]}"
        )
    methods = response.json()
    evidence.record(source="btcpay/store-payment-methods", request={}, response=methods)
    return [
        str(method.get("paymentMethodId"))
        for method in methods
        if isinstance(method, dict) and method.get("enabled")
    ]


def stage_deposit(ctx: Context) -> None:
    record = ctx.record
    log("what this proves: a real Nile USDT transfer is credited to this user, to")
    log("the exact micro-USDT, through the same matcher production uses.")

    if not record.deposit_id:
        created = ctx.api.create_deposit(DEPOSIT_MICRO, f"nile-deposit-{time.time()}")
        record.deposit_id = created["deposit_id"]
        record.deposit_address = str(created["address"])
        record.deposit_expected_micro = DEPOSIT_MICRO
        record.deposit_requested_at = now()
        save_record(record)

    log(f"  deposit {record.deposit_id}")
    log(f"  address {record.deposit_address}")
    log(f"  amount  {usdt(DEPOSIT_MICRO)} USDT — exactly, not approximately")
    confirm(
        f"Send exactly {usdt(DEPOSIT_MICRO)} USDT on Nile from your second wallet\n"
        f"       to {record.deposit_address}."
    )

    def settled() -> dict[str, Any] | None:
        body = ctx.api.deposit(record.deposit_id)
        if body["status"] == "settled":
            return body
        if body["status"] in ("review", "expired", "failed"):
            raise NileFailure(
                f"the deposit went to {body['status']} instead of settling. That is the "
                "system working if the amount was wrong; check the amount you sent "
                "against docs/operating/runbook-usdt-attribution.md."
            )
        return None

    body = wait_until(settled, timeout=DEPOSIT_TIMEOUT, description="the deposit to settle")
    record.deposit_settled_at = now()
    credited = micros(body["amount_credited"])
    record.deposit_credited_micro = credited
    if credited != DEPOSIT_MICRO:
        raise NileFailure(f"credited {credited} micro-USDT, expected exactly {DEPOSIT_MICRO}")
    log(f"  credited exactly {usdt(credited)} USDT")

    if not record.deposit_txid:
        txid = ask_until(
            "Paste the TRON transaction id of that transfer (from your wallet or TronScan).",
            valid=looks_like_txid,
            hint="that is not a 64-character hex transaction id; try again",
        )
        record.deposit_txid = txid.lower().removeprefix("0x")
        save_record(record)
    transaction = ctx.nile.get_transaction(record.deposit_txid)
    if transaction is None:
        raise NileFailure("TronGrid has no transaction with that id")
    matching = [
        transfer
        for transfer in transaction.transfers
        if transfer.contract == ctx.config.nile_contract
        and transfer.to_address == record.deposit_address
        and transfer.amount == DEPOSIT_MICRO
    ]
    if not matching:
        raise NileFailure(
            f"transaction {record.deposit_txid} carries no {usdt(DEPOSIT_MICRO)} USDT transfer "
            f"to {record.deposit_address}: {transaction.transfers}"
        )
    log(f"  the same transfer read straight from TronGrid, in block {transaction.block_number}")


def stage_withdrawal(ctx: Context) -> None:
    record, config = ctx.record, ctx.config
    log("what this proves: the full-tuple check runs against a real transaction, and")
    log(f"the worker confirms it only once it is {config.confirmations} blocks deep.")

    if not record.withdrawal_destination:
        record.withdrawal_destination = ask_until(
            "Which Nile address should the withdrawal go to? Use your second wallet,\n"
            "       not the hot wallet — the check compares sender and recipient.",
            valid=looks_like_tron_address,
            hint="that does not look like a TRON address (they start with T)",
        )
        save_record(record)

    if not record.withdrawal_id:
        # Read before the request, and keep it: requesting a withdrawal moves
        # the gross out of `available` into `held`, so a resumed run reading
        # this now would compare the end state against itself.
        opening = ctx.api.balances()
        record.balance_before_available = opening["available"]
        record.balance_before_held = opening["held"]
        created = ctx.api.create_withdrawal(
            WITHDRAW_MICRO, record.withdrawal_destination, f"nile-wd-{time.time()}"
        )
        record.withdrawal_id = created["withdrawal_id"]
        if created["status"] != "pending_approval":
            raise NileFailure(
                f"a USDT withdrawal must wait for an operator, got {created['status']}"
            )
        log(f"  withdrawal {record.withdrawal_id} is waiting for approval, as every USDT one is")
        approved = ctx.api.approve(record.withdrawal_id)
        record.withdrawal_gross_micro = micros(approved["amount_gross"])
        record.withdrawal_fee_micro = micros(approved["fee"])
        record.withdrawal_net_micro = micros(approved["amount_net"])
        save_record(record)

    before = {
        "available": record.balance_before_available,
        "held": record.balance_before_held,
    }
    log(
        f"  balance before the request: {usdt(before['available'])} available, "
        f"{usdt(before['held'])} held"
    )
    log(
        f"  approved: gross {usdt(record.withdrawal_gross_micro)} USDT, "
        f"fee {usdt(record.withdrawal_fee_micro)}, net {usdt(record.withdrawal_net_micro)}"
    )

    if not record.withdrawal_txid:
        confirm(
            f"Send exactly {usdt(record.withdrawal_net_micro)} USDT on Nile from the hot\n"
            f"       wallet {config.hot_wallet}\n"
            f"       to {record.withdrawal_destination}."
        )
        for attempt in range(1, 4):
            txid = ask_until(
                "Paste the transaction id of the transfer you just sent.",
                valid=looks_like_txid,
                hint="that is not a 64-character hex transaction id; try again",
            )
            status, detail = ctx.api.mark_broadcast(record.withdrawal_id, txid)
            if status == 200:
                record.withdrawal_txid = txid.lower().removeprefix("0x")
                record.withdrawal_broadcast_at = now()
                save_record(record)
                log("  the server fetched that transaction and checked every part of the claim")
                break
            log(f"  attempt {attempt} refused with {status}: {detail}")
            log("  that refusal is the system working. Check the txid, amount and destination.")
        else:
            raise NileFailure("three transaction ids in a row failed the full-tuple check")
    else:
        log(f"  resuming with the transaction already recorded: {record.withdrawal_txid}")

    transaction = ctx.nile.get_transaction(record.withdrawal_txid)
    if transaction is None or transaction.block_number is None:
        raise NileFailure("TronGrid lost the transaction it just verified")
    record.withdrawal_tx_block = transaction.block_number
    log(f"  the transfer is in block {record.withdrawal_tx_block}")

    def confirmed() -> dict[str, Any] | None:
        body = ctx.api.withdrawal(record.withdrawal_id)
        if body["status"] == "confirmed":
            return body
        if body.get("failure_reason"):
            log(f"  the poller is unhappy: {body['failure_reason']}")
        return None

    body = wait_until(
        confirmed,
        timeout=CONFIRM_TIMEOUT,
        description=f"the worker to see {config.confirmations} confirmations",
    )
    record.withdrawal_confirmed_at = now()
    record.withdrawal_confirm_height = ctx.nile.get_block_height()
    record.withdrawal_confirmations_seen = (
        record.withdrawal_confirm_height - record.withdrawal_tx_block
    )
    if record.withdrawal_confirmations_seen < config.confirmations:
        raise NileFailure(
            f"the withdrawal confirmed {record.withdrawal_confirmations_seen} blocks deep, "
            f"shallower than the configured {config.confirmations}"
        )
    log(
        f"  confirmed at block {record.withdrawal_confirm_height}, "
        f"{record.withdrawal_confirmations_seen} blocks deep"
    )

    if micros(body["fee"]) != record.withdrawal_fee_micro:
        raise NileFailure(f"the fee changed after approval: {body['fee']}")
    if record.withdrawal_net_micro != record.withdrawal_gross_micro - record.withdrawal_fee_micro:
        raise NileFailure("net is not gross minus fee")

    after = ctx.api.balances()
    if before["available"] - after["available"] != record.withdrawal_gross_micro:
        raise NileFailure(
            f"available moved by {before['available'] - after['available']} micro-USDT, "
            f"expected exactly {record.withdrawal_gross_micro}"
        )
    if after["held"] != before["held"]:
        raise NileFailure(
            f"held is {after['held']} micro-USDT, expected the pre-withdrawal {before['held']}; "
            "the settle entry should have extinguished this hold"
        )
    log(f"  the user was debited exactly {usdt(record.withdrawal_gross_micro)} USDT, hold released")


def stage_duplicate(ctx: Context) -> None:
    record = ctx.record
    log("what this proves: one transaction settles at most one withdrawal. Pasting")
    log("the previous txid is the most ordinary operator mistake there is.")

    if not record.withdrawal_txid:
        raise NileFailure("stage 3 has to have recorded a transaction id first")

    if record.duplicate_status == 409:
        log(f"  already refused in an earlier run: {record.duplicate_detail}")
        return

    if not record.duplicate_withdrawal_id:
        created = ctx.api.create_withdrawal(
            DUPLICATE_WITHDRAW_MICRO, record.withdrawal_destination, f"nile-dup-{time.time()}"
        )
        record.duplicate_withdrawal_id = created["withdrawal_id"]
        ctx.api.approve(record.duplicate_withdrawal_id)
        save_record(record)
    log(f"  second withdrawal {record.duplicate_withdrawal_id}, approved and waiting")
    log("  nothing is sent for it. The txid from stage 3 is submitted instead.")

    status, detail = ctx.api.mark_broadcast(record.duplicate_withdrawal_id, record.withdrawal_txid)
    record.duplicate_status = status
    record.duplicate_detail = detail
    if status != 409:
        raise NileFailure(
            f"re-using a transaction id answered {status} ({detail}), expected 409. One "
            "transaction settling two withdrawals credits a send that never happened."
        )
    if record.withdrawal_id not in detail:
        raise NileFailure(f"the refusal does not name the withdrawal that owns the txid: {detail}")
    log(f"  refused with 409: {detail}")

    released = ctx.api.release(
        record.duplicate_withdrawal_id,
        "Nile verification session: this withdrawal exists only to prove a re-used "
        "transaction id is refused. Nothing was ever sent for it.",
    )
    record.duplicate_released = released["status"] == "refunded"
    log(f"  the second withdrawal was released ({released['status']}); no balance left held")


def stage_payloads(ctx: Context) -> None:
    record, evidence = ctx.record, ctx.evidence
    log("what this proves: how far tests/fake_tron.py is from what TronGrid sends.")
    log("every difference below is a line to change in the fake afterwards.")

    diffs: list[str] = []
    live_transaction = evidence.transaction(record.withdrawal_txid or record.deposit_txid)
    if live_transaction is None:
        log("  no captured transaction payload yet; run stages 2 and 3 first")
    else:
        expected = fake_transaction_payload(
            txid=str(live_transaction.get("id", "")),
            contract=ctx.config.nile_contract,
            sender=ctx.config.hot_wallet or "",
            recipient=record.withdrawal_destination or record.deposit_address,
            amount=record.withdrawal_net_micro or record.deposit_credited_micro,
            block_number=int(live_transaction.get("blockNumber") or 0),
        )
        diffs += [
            f"gettransactioninfobyid {line}"
            for line in shape_diff(live=live_transaction, fake=expected)
        ]

    live_metadata = evidence.responses("triggerconstantcontract")
    if live_metadata:
        fake_symbol, _ = fake_metadata_payloads()
        diffs += [
            f"triggerconstantcontract {line}"
            for line in shape_diff(live=live_metadata[0], fake=fake_symbol)
        ]

    record.shape_diffs = diffs
    if diffs:
        log(f"  {len(diffs)} shape differences found:")
        for line in diffs:
            log(f"    {line}")
        log("  fix tests/fake_tron.py to match, and add a captured-payload regression test.")
    else:
        log("  no shape differences: the fake matches what TronGrid sent, field for field.")
    log(f"  {len(evidence.entries)} raw payloads saved under {evidence.directory}")


def stage_report(ctx: Context) -> None:
    record = ctx.record
    record.finished_at = record.finished_at or now()
    section = render_log_section(record)
    destination = ctx.evidence.directory / f"verification-log-{now()[:10]}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(section, encoding="utf-8")
    log(f"  written to {destination}")
    log("  paste it into docs/operating/verification-log.md, then downgrade the")
    log("  format-verified-only caveats the table lists.")
    print(section)


def or_missing(value: str) -> str:
    """An em dash where a stage never got far enough to record anything."""
    return value or "—"


def downgraded_caveats(record: Record) -> str:
    """Only the caveats this run actually earned the right to downgrade.

    The report is written after a failed session too, and that is when it is
    worth the most. So each line is conditional on the evidence for it: a
    partial run must not print a sentence saying a constant is now confirmed.
    """
    lines: list[str] = []
    if record.nile_symbol == "USDT" and record.nile_decimals == 6:
        lines.append(
            '- `USDT_CONTRACT_NILE` was "format-verified only, NOT confirmed against a\n'
            '  live Nile node". It is now confirmed: the contract answers `USDT` / `6`,\n'
            f"  read from `{record.nile_contract}` on {record.nile_endpoint}."
        )
    if record.confirmations_required and (
        record.withdrawal_confirmations_seen >= record.confirmations_required
    ):
        lines.append(
            f"- `TRON_CONFIRMATIONS={record.confirmations_required}` was a documented "
            "assumption about TRON's\n  solidified-block distance. A withdrawal was confirmed "
            f"{record.withdrawal_confirmations_seen}\n  blocks deep and not before."
        )
    return "\n".join(lines) or (
        "- **None.** This session did not get far enough to downgrade anything. A\n"
        "  partial run is a failed run, and the caveats stay exactly as they are."
    )


def render_log_section(record: Record) -> str:
    """The verification-log entry, in the shape the template's table expects."""
    if record.shape_diffs:
        diffs = "; ".join(record.shape_diffs)
    elif 5 in record.stages_done:
        diffs = "none — the fake matched"
    else:
        diffs = "not compared yet"
    rows = [
        ("Date (UTC)", f"{or_missing(record.started_at)} → {or_missing(record.finished_at)}"),
        ("Network", f"{record.network} ({or_missing(record.nile_endpoint)})"),
        (
            "Nile USDT contract",
            f"`{or_missing(record.nile_contract)}` — `symbol()` = "
            f"`{or_missing(record.nile_symbol)}`, `decimals()` = `{record.nile_decimals}`",
        ),
        (
            "Mainnet USDT contract",
            f"`{or_missing(record.mainnet_contract)}` — `symbol()` = "
            f"`{or_missing(record.mainnet_symbol)}`, `decimals()` = "
            f"`{record.mainnet_decimals}` (read-only)",
        ),
        (
            "Deposit",
            f"`{or_missing(record.deposit_txid)}` — {usdt(record.deposit_credited_micro)} USDT "
            f"credited, paid to the pool address `{or_missing(record.deposit_address)}`",
        ),
        (
            "Withdrawal",
            f"`{or_missing(record.withdrawal_txid)}` — gross "
            f"{usdt(record.withdrawal_gross_micro)}, fee {usdt(record.withdrawal_fee_micro)}, "
            f"net {usdt(record.withdrawal_net_micro)} USDT to "
            f"`{or_missing(record.withdrawal_destination)}`",
        ),
        (
            "Confirmation depth",
            f"block {record.withdrawal_tx_block} → {record.withdrawal_confirm_height} "
            f"({record.withdrawal_confirmations_seen} blocks deep; `TRON_CONFIRMATIONS` "
            f"is {record.confirmations_required})",
        ),
        (
            "Duplicate txid",
            f"withdrawal `{or_missing(record.duplicate_withdrawal_id)}` answered "
            f"`{record.duplicate_status}`: {or_missing(record.duplicate_detail)}",
        ),
        ("Payload-shape differences", diffs),
    ]
    table = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return f"""## Run of {record.started_at[:10] or "—"}

| What | Evidence |
|---|---|
{table}

Caveats downgraded by this run:

{downgraded_caveats(record)}

Raw payloads: `spike-evidence-nile/` in the operator's working copy. They are
not committed — they contain nothing secret, but they are a session's
scratch, and the assertions above are the part that matters.
"""


# -- wiring ----------------------------------------------------------------


def stages_from(start: int) -> list[int]:
    if start < FIRST_STAGE or start > LAST_STAGE:
        raise NileFailure(f"there is no stage {start}; they run {FIRST_STAGE} to {LAST_STAGE}")
    return [number for number, _, _ in STAGES if number >= start]


def stage_needs_nile_config(stage: int) -> bool:
    """Stages 1 to 4 touch the live network; 5 and 6 only read what they left."""
    return stage <= 4


RUNNERS: dict[int, Callable[[Context], None]] = {
    1: stage_preflight,
    2: stage_deposit,
    3: stage_withdrawal,
    4: stage_duplicate,
    5: stage_payloads,
    6: stage_report,
}


def build_context(config: NileConfig, env: Mapping[str, str], *, needs_api: bool) -> Context:
    evidence = Evidence()
    return Context(
        config=config,
        evidence=evidence,
        record=load_record(),
        api=Api(config.api_url, admin_key(env) if needs_api else "unused"),
        nile=CapturingTronGrid(
            base_url=config.nile_endpoint,
            api_key=config.trongrid_key,
            evidence=evidence,
            label="nile",
        ),
        mainnet=CapturingTronGrid(
            base_url=config.mainnet_endpoint,
            api_key=config.trongrid_key,
            evidence=evidence,
            label="mainnet",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        type=int,
        default=FIRST_STAGE,
        help=f"resume at this stage and run the rest ({FIRST_STAGE} to {LAST_STAGE})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="the same file docker compose reads; values already exported win",
    )
    args = parser.parse_args(argv)

    env: dict[str, str] = {**load_env_file(args.env_file), **os.environ}
    # Subprocesses need the same merge. `docker compose exec` re-interpolates
    # the Nile override, whose `${TRONGRID_API_KEY:?}`-style required variables
    # look at the process environment — compose does not know about --env-file
    # here — so a value the script loaded but never exported would still abort
    # every compose() call.
    os.environ.update(env)
    config = load_config(env)
    selected = stages_from(args.stage)

    live = any(stage_needs_nile_config(stage) for stage in selected)
    if live:
        guard_nile_only(config)

    ctx = build_context(config, env, needs_api=live)
    for number in selected:
        _, name, purpose = STAGES[number - 1]
        log("")
        log(f"=== stage {number}/{LAST_STAGE}: {name} — {purpose}")
        RUNNERS[number](ctx)
        ctx.record.mark_done(number)
        save_record(ctx.record)

    log("")
    log(f"stages {selected[0]} to {selected[-1]} passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NileFailure as failure:
        log(f"FAILED: {failure}")
        raise SystemExit(1) from failure
