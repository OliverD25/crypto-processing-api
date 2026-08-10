"""USDT-TRC20: the manual withdrawal walk, deposit mitigations, read API.

Everything TRON-facing here runs against `FakeTronGrid`. There is no TRON
regtest, so the network is faked — but the parser, the verifier, the ledger and
the state machine are all the real code.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.api.middleware import get_tron_gateway
from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Deposit,
    DepositStatus,
    EntryKind,
    WithdrawalStatus,
)
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import reconciliation
from crypto_processing_api.workers.gas_monitor import GasMonitor
from tests.fake_tron import DESTINATION, HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.integration.conftest import USDT, bearer

GROSS = 200_000_000  # 200 USDT
FLAT_FEE = 1_000_000  # 1 USDT, the seeded default
NET = GROSS - FLAT_FEE
FUNDED = 500_000_000
TXID = "9" * 64


@pytest.fixture
def fake_tron() -> FakeTronGrid:
    return FakeTronGrid()


@pytest.fixture(autouse=True)
def tron_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)
    monkeypatch.setattr(settings, "tron_confirmations", 19)


@pytest.fixture(autouse=True)
def wire_tron(app: FastAPI, fake_tron: FakeTronGrid) -> None:
    app.dependency_overrides[get_tron_gateway] = lambda: fake_tron


@pytest.fixture(autouse=True)
def usdt_enabled(session: Session) -> None:
    """USDT ships disabled until a store exposes it; these tests need it on."""
    asset = session.get(Asset, USDT)
    assert asset
    asset.enabled = True
    session.commit()


def credit_usdt(session: Session, user: str, amount: int) -> None:
    hot = ledger.get_system_account(session, asset_id=USDT, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(session, asset_id=USDT, external_user_id=user)
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=USDT,
        source_ref=f"btcpay_payment:{uuid.uuid4().hex}:0",
        postings=[(hot.id, amount), (available.id, -amount)],
    )
    session.commit()


def balance(session: Session, user: str, kind: AccountKind) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == USDT,
            Account.kind == kind,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def system_balance(session: Session, kind: AccountKind) -> int:
    session.rollback()
    return ledger.get_system_account(session, asset_id=USDT, kind=kind).balance


def request_usdt(
    client: TestClient, key: str, *, user: str, amount: int = GROSS, destination: str = DESTINATION
) -> Any:
    return client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": USDT,
            "amount": str(amount),
            "destination_address": destination,
        },
        headers={**bearer(key), "Idempotency-Key": f"u-{uuid.uuid4()}"},
    )


# -- destination validation ------------------------------------------------


def test_usdt_withdrawal_requires_a_tron_address(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_usdt(session, "tronuser", FUNDED)
    response = request_usdt(
        client,
        readwrite_key,
        user="tronuser",
        destination="bcrt1qakxe0syt7wkayqgccadyayhtfxhk9mvmvtq0sx",
    )
    assert response.status_code == 422
    assert balance(session, "tronuser", AccountKind.USER_HOLD) == 0


def test_an_ethereum_address_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """USDT exists on Ethereum too, and that mistake is unrecoverable."""
    credit_usdt(session, "evmuser", FUNDED)
    response = request_usdt(
        client,
        readwrite_key,
        user="evmuser",
        destination="0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
    )
    assert response.status_code == 422
    assert "EVM-style" in response.json()["detail"]


def test_our_own_tron_hot_wallet_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_usdt(session, "selfsend", FUNDED)
    response = request_usdt(client, readwrite_key, user="selfsend", destination=HOT_WALLET)
    assert response.status_code == 422
    assert "own TRON hot wallet" in response.json()["detail"]


# -- the manual walk -------------------------------------------------------


def test_every_usdt_withdrawal_waits_for_an_operator(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """Nothing can send USDT for us, so amount is irrelevant to the routing.

    5 USDT is far below the 200 USDT auto-approval limit that would clear a BTC
    withdrawal of the same relative size.
    """
    credit_usdt(session, "queued", FUNDED)
    small = 5_000_000
    body = request_usdt(client, readwrite_key, user="queued", amount=small).json()
    assert body["status"] == "pending_approval"
    assert body["approval_mode"] == "manual"
    assert balance(session, "queued", AccountKind.USER_HOLD) == small


def test_an_amount_at_or_below_the_flat_fee_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """Nothing would arrive, so nothing is reserved."""
    credit_usdt(session, "tiny", FUNDED)
    response = request_usdt(client, readwrite_key, user="tiny", amount=FLAT_FEE)
    assert response.status_code == 422
    assert balance(session, "tiny", AccountKind.USER_HOLD) == 0


def test_the_full_manual_walk_with_exact_balances(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
) -> None:
    credit_usdt(session, "walker", FUNDED)
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED

    created = request_usdt(client, readwrite_key, user="walker").json()
    withdrawal_id = created["withdrawal_id"]

    # hold
    assert balance(session, "walker", AccountKind.USER_AVAILABLE) == FUNDED - GROSS
    assert balance(session, "walker", AccountKind.USER_HOLD) == GROSS

    # approval is also the handover to the operator
    approved = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}, headers=bearer(admin_key)
    ).json()
    assert approved["status"] == "submitted"
    assert approved["fee"] == "1.000000"
    assert approved["amount_net"] == "199.000000"

    withdrawal = withdrawal_service.get(session, uuid.UUID(withdrawal_id))
    session.refresh(withdrawal)
    assert withdrawal.backend_ref is not None
    assert withdrawal.backend_ref.startswith("manual:")
    # USDT commits net only: the gas is TRX, not USDT.
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == NET
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - NET

    # the operator sends and reports the transaction
    fake_tron.add_transfer(TXID, recipient=DESTINATION, amount=NET)
    marked = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": TXID},
        headers=bearer(admin_key),
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["status"] == "broadcast"
    assert marked.json()["txid"] == TXID

    # not enough confirmations yet
    fake_tron.block_height = 1_005
    reconciliation.sweep_manual_withdrawals(session_factory, fake_tron, get_settings())
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.BROADCAST

    # solidified
    fake_tron.block_height = 1_100
    reconciliation.sweep_manual_withdrawals(session_factory, fake_tron, get_settings())
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED

    assert balance(session, "walker", AccountKind.USER_AVAILABLE) == FUNDED - GROSS
    assert balance(session, "walker", AccountKind.USER_HOLD) == 0
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - NET
    assert system_balance(session, AccountKind.FEE_INCOME) == -FLAT_FEE
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == 0
    assert_ledger_consistent(session)


def approve_and_get(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str, user: str
) -> str:
    credit_usdt(session, user, FUNDED)
    created = request_usdt(client, readwrite_key, user=user).json()
    client.post(
        f"/v1/admin/withdrawals/{created['withdrawal_id']}/approve",
        json={},
        headers=bearer(admin_key),
    )
    return str(created["withdrawal_id"])


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda f, w: None, "no transaction"),
        (
            lambda f, w: f.add_transfer(TXID, recipient=DESTINATION, amount=NET - 1),
            "amount is",
        ),
        (
            lambda f, w: f.add_transfer(TXID, recipient=HOT_WALLET, amount=NET),
            "recipient is",
        ),
        (
            lambda f, w: f.add_transfer(
                TXID, recipient=DESTINATION, amount=NET, sender=USDT_CONTRACT
            ),
            "sender is",
        ),
        (
            lambda f, w: f.add_transfer(
                TXID, recipient=DESTINATION, amount=NET, contract=HOT_WALLET
            ),
            "contract is",
        ),
        (
            lambda f, w: f.add_transfer(
                TXID, recipient=DESTINATION, amount=NET, receipt_result="OUT_OF_ENERGY"
            ),
            "failed on chain",
        ),
        (
            lambda f, w: f.add_transfer(
                TXID, recipient=DESTINATION, amount=NET, include_transfer_event=False
            ),
            "no TRC-20 Transfer event",
        ),
    ],
)
def test_a_txid_that_does_not_match_is_refused(
    client: TestClient,
    session: Session,
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
    mutate: Any,
    fragment: str,
) -> None:
    """Every one of these is a real, successful-looking transaction."""
    withdrawal_id = approve_and_get(client, session, readwrite_key, admin_key, "picky")
    mutate(fake_tron, withdrawal_id)

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": TXID},
        headers=bearer(admin_key),
    )
    assert response.status_code == 422
    assert fragment in response.json()["detail"]

    withdrawal = withdrawal_service.get(session, uuid.UUID(withdrawal_id))
    session.refresh(withdrawal)
    # Unchanged: the money is neither sent nor given back.
    assert withdrawal.status == WithdrawalStatus.SUBMITTED
    assert withdrawal.txid is None
    assert balance(session, "picky", AccountKind.USER_HOLD) == GROSS


def test_a_txid_already_used_by_another_withdrawal_is_refused(
    client: TestClient,
    session: Session,
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """Critique #12's other half: one transaction settles at most one withdrawal."""
    first = approve_and_get(client, session, readwrite_key, admin_key, "reuse-a")
    fake_tron.add_transfer(TXID, recipient=DESTINATION, amount=NET)
    assert (
        client.post(
            f"/v1/admin/withdrawals/{first}/mark-broadcast",
            json={"txid": TXID},
            headers=bearer(admin_key),
        ).status_code
        == 200
    )

    second = approve_and_get(client, session, readwrite_key, admin_key, "reuse-b")
    response = client.post(
        f"/v1/admin/withdrawals/{second}/mark-broadcast",
        json={"txid": TXID},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409
    assert "already recorded" in response.json()["detail"]


def test_mark_broadcast_needs_admin_scope(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    withdrawal_id = approve_and_get(client, session, readwrite_key, admin_key, "scoped")
    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": TXID},
        headers=bearer(readwrite_key),
    )
    assert response.status_code == 403


def test_a_reorg_that_removes_the_transaction_does_not_confirm_it(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_tron: FakeTronGrid,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """Re-verified on every poll, so a vanished transaction cannot settle."""
    withdrawal_id = approve_and_get(client, session, readwrite_key, admin_key, "reorged")
    fake_tron.add_transfer(TXID, recipient=DESTINATION, amount=NET)
    client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/mark-broadcast",
        json={"txid": TXID},
        headers=bearer(admin_key),
    )

    fake_tron.transactions.pop(TXID)
    fake_tron.block_height = 1_100
    reconciliation.sweep_manual_withdrawals(session_factory, fake_tron, get_settings())

    withdrawal = withdrawal_service.get(session, uuid.UUID(withdrawal_id))
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.BROADCAST
    assert "re-verification failed" in (withdrawal.failure_reason or "")
    # And nothing was released: the money may well have moved.
    assert balance(session, "reorged", AccountKind.USER_HOLD) == GROSS


# -- deposits --------------------------------------------------------------


def test_reservation_window_is_recorded(
    client: TestClient, session: Session, readwrite_key: str, fake_btcpay: Any
) -> None:
    """Without the window, a late pooled-address payment cannot be attributed."""
    fake_btcpay.payment_methods = ["BTC-CHAIN", "USDT_TRC20-TRON"]
    asset = session.get(Asset, USDT)
    assert asset
    asset.btcpay_payment_method = "USDT_TRC20-TRON"
    session.commit()

    created = client.post(
        "/v1/deposits",
        json={"external_user_id": "pooled", "asset": USDT},
        headers={**bearer(readwrite_key), "Idempotency-Key": "d-pool"},
    )
    assert created.status_code == 201, created.text

    deposit = session.execute(select(Deposit)).scalar_one()
    session.refresh(deposit)
    assert deposit.address_reserved_from is not None
    assert deposit.address_reserved_until is not None
    assert deposit.address_reserved_until > deposit.address_reserved_from

    history = client.get(
        f"/v1/deposits/{deposit.id}/address-history", headers=bearer(readwrite_key)
    ).json()
    assert history["address"] == deposit.address
    assert history["reservations"][0]["external_user_id"] == "pooled"


def test_pool_exhaustion_is_a_503_with_a_code(
    client: TestClient, session: Session, readwrite_key: str, fake_btcpay: Any
) -> None:
    from crypto_processing_api.gateway.btcpay_client import BTCPayValidation

    fake_btcpay.fail_next["create_top_up_invoice"] = BTCPayValidation("no address available")
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "unlucky", "asset": USDT},
        headers={**bearer(readwrite_key), "Idempotency-Key": "d-exhausted"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DEPOSIT_TEMPORARILY_UNAVAILABLE"
    assert response.headers["retry-after"] == "60"


# -- gas monitor -----------------------------------------------------------


def test_low_trx_raises_an_alert(fake_tron: FakeTronGrid) -> None:
    settings = get_settings()
    fake_tron.trx_balance_sun = 10 * 1_000_000
    report = GasMonitor().check(fake_tron, settings)
    assert report.alerted
    assert report.trx_balance == 10_000_000


def test_healthy_trx_is_quiet(fake_tron: FakeTronGrid) -> None:
    fake_tron.trx_balance_sun = 5_000 * 1_000_000
    report = GasMonitor().check(fake_tron, get_settings())
    assert not report.alerted


def test_a_trongrid_outage_alerts_only_after_a_streak(fake_tron: FakeTronGrid) -> None:
    """One blip is noise; a streak means verification has stopped working."""
    from crypto_processing_api.gateway.trongrid import TronGridRateLimited

    monitor = GasMonitor()
    settings = get_settings()
    for expected in (False, False, True):
        fake_tron.fail_next["get_trx_balance"] = TronGridRateLimited("429")
        assert monitor.check(fake_tron, settings).alerted is expected


# -- read API --------------------------------------------------------------


def test_balances_endpoint_reports_available_held_and_total(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_usdt(session, "reader", FUNDED)
    request_usdt(client, readwrite_key, user="reader", amount=GROSS)

    body = client.get("/v1/users/reader/balances", headers=bearer(readwrite_key)).json()
    entry = next(row for row in body["balances"] if row["asset"] == USDT)
    assert entry["available"] == "300.000000"
    assert entry["held"] == "200.000000"
    assert entry["total"] == "500.000000"


def test_transactions_endpoint_shows_the_ledger_history(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_usdt(session, "historian", FUNDED)
    request_usdt(client, readwrite_key, user="historian", amount=GROSS)

    body = client.get("/v1/users/historian/transactions", headers=bearer(readwrite_key)).json()
    kinds = [row["kind"] for row in body["transactions"]]
    assert EntryKind.WITHDRAWAL_HOLD.value in kinds
    assert EntryKind.DEPOSIT_CREDIT.value in kinds
    credit = next(row for row in body["transactions"] if row["kind"] == "deposit_credit")
    assert credit["direction"] == "credit"
    assert credit["amount"] == "500.000000"


def test_assets_endpoint_is_integration_reference_data(
    client: TestClient, readwrite_key: str
) -> None:
    body = client.get("/v1/assets", headers=bearer(readwrite_key)).json()
    usdt = next(row for row in body["assets"] if row["asset"] == USDT)
    assert usdt["decimals"] == 6
    assert usdt["unit_name"] == "microUSDT"
    assert usdt["withdrawal_flat_fee"] == "1.000000"


def test_reads_require_a_key(client: TestClient) -> None:
    for path in ("/v1/users/x/balances", "/v1/users/x/transactions", "/v1/assets"):
        assert client.get(path).status_code == 401


def _usdt_store(session: Session, fake_btcpay: Any) -> None:
    fake_btcpay.payment_methods = ["BTC-CHAIN", "USDT_TRC20-TRON"]
    asset = session.get(Asset, USDT)
    assert asset
    asset.btcpay_payment_method = "USDT_TRC20-TRON"
    session.commit()


def _make_usdt_deposit(
    client: TestClient, readwrite_key: str, *, expected: str | None, idem: str
) -> dict[str, Any]:
    body: dict[str, Any] = {"external_user_id": "pool-user", "asset": USDT}
    if expected is not None:
        body["expected_amount"] = expected
    response = client.post(
        "/v1/deposits", json=body, headers={**bearer(readwrite_key), "Idempotency-Key": idem}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_a_usdt_amount_far_from_the_expected_one_goes_to_review(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
) -> None:
    """Critique #3c. Pooled addresses make attribution heuristic.

    The address may have belonged to someone else minutes ago, so a payment
    that does not look like what this user said they would send is worth a
    human's attention rather than an automatic credit.
    """
    _usdt_store(session, fake_btcpay)
    created = _make_usdt_deposit(client, readwrite_key, expected="100000000", idem="d-tol-1")
    invoice_id = next(iter(fake_btcpay.invoices))

    fake_btcpay.add_payment(invoice_id, "250.000000")
    fake_btcpay.settle(invoice_id)
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    deposit = session.get(Deposit, uuid.UUID(created["deposit_id"]))
    assert deposit
    session.refresh(deposit)
    assert deposit.status == DepositStatus.REVIEW
    assert deposit.amount_credited == 0


def test_a_usdt_amount_matching_the_expectation_still_auto_credits(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
) -> None:
    _usdt_store(session, fake_btcpay)
    created = _make_usdt_deposit(client, readwrite_key, expected="100000000", idem="d-tol-2")
    invoice_id = next(iter(fake_btcpay.invoices))

    fake_btcpay.add_payment(invoice_id, "100.000000")
    fake_btcpay.settle(invoice_id)
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    deposit = session.get(Deposit, uuid.UUID(created["deposit_id"]))
    assert deposit
    session.refresh(deposit)
    assert deposit.status == DepositStatus.SETTLED
    assert deposit.amount_credited == 100_000_000


def test_without_an_expected_amount_there_is_nothing_to_compare(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
) -> None:
    """A top-up with no expectation must not be held hostage by the tolerance."""
    _usdt_store(session, fake_btcpay)
    created = _make_usdt_deposit(client, readwrite_key, expected=None, idem="d-tol-3")
    invoice_id = next(iter(fake_btcpay.invoices))

    fake_btcpay.add_payment(invoice_id, "37.500000")
    fake_btcpay.settle(invoice_id)
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    deposit = session.get(Deposit, uuid.UUID(created["deposit_id"]))
    assert deposit
    session.refresh(deposit)
    assert deposit.status == DepositStatus.SETTLED
    assert deposit.amount_credited == 37_500_000


def test_the_btc_path_ignores_the_tolerance(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
) -> None:
    """BTC addresses are single-use, so attribution is never in doubt."""
    created = client.post(
        "/v1/deposits",
        json={"external_user_id": "btc-user", "asset": "BTC", "expected_amount": "50000000"},
        headers={**bearer(readwrite_key), "Idempotency-Key": "d-btc-tol"},
    ).json()
    invoice_id = next(iter(fake_btcpay.invoices))

    fake_btcpay.add_payment(invoice_id, "2.00000000")
    fake_btcpay.settle(invoice_id)
    reconciliation.sweep_deposits(session_factory, fake_btcpay, get_settings())

    deposit = session.get(Deposit, uuid.UUID(created["deposit_id"]))
    assert deposit
    session.refresh(deposit)
    assert deposit.status == DepositStatus.SETTLED
    assert deposit.amount_credited == 200_000_000
