"""Regressions for the three v0.1.1 defects.

Each of these shipped in v0.1.0 and each is silent: nothing crashes, the tests
passed, and the damage shows up as an error drip in the logs, a held balance
that never moves, or an alarm that never fires.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.ledger.models import Withdrawal, WithdrawalStatus
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import payout_submitter, reconciliation, runner
from tests.fake_tron import HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.integration.conftest import BTC, USDT, bearer, credit_user

BTC_DEST = "bcrt1qakxe0syt7wkayqgccadyayhtfxhk9mvmvtq0sx"
TRON_DEST = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def make_row(
    session: Session,
    *,
    backend: str,
    status: WithdrawalStatus,
    backend_ref: str | None = None,
    asset: str = BTC,
) -> uuid.UUID:
    """A withdrawal row in an exact state, without walking the whole flow."""
    from crypto_processing_api.core.ids import uuid7

    # 200 USDT sits above the 1 USDT flat fee; 100_000 sat above the BTC dust
    # rule. A row too small to pay its own fee would fail for the wrong reason.
    withdrawal = Withdrawal(
        id=uuid7(),
        external_user_id="filter-user",
        asset_id=asset,
        destination_address=BTC_DEST if asset == BTC else TRON_DEST,
        amount_gross=100_000 if asset == BTC else 200_000_000,
        status=status,
        approval_mode="auto",
        backend=backend,
        backend_ref=backend_ref,
    )
    session.add(withdrawal)
    session.commit()
    return withdrawal.id


# -- fix 1: the BTCPay submitter must not claim manual rows ----------------


def test_the_submitter_query_skips_manual_backends(session: Session) -> None:
    """A manual row here is quoted a BTC fee and offered to Greenfield."""
    btcpay_id = make_row(
        session, backend=withdrawal_service.BACKEND_BTCPAY, status=WithdrawalStatus.APPROVED
    )
    manual_id = make_row(
        session,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        status=WithdrawalStatus.APPROVED,
        asset=USDT,
    )

    due = withdrawal_service.due_for_submission(session)
    assert btcpay_id in due
    assert manual_id not in due


def test_the_polling_query_skips_manual_backends(session: Session) -> None:
    """`manual:<uuid>` is not a payout id; asking Greenfield about it errors."""
    btcpay_id = make_row(
        session,
        backend=withdrawal_service.BACKEND_BTCPAY,
        status=WithdrawalStatus.SUBMITTED,
        backend_ref="payout-1",
    )
    manual_id = make_row(
        session,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        status=WithdrawalStatus.BROADCAST,
        backend_ref=f"manual:{uuid.uuid4()}",
        asset=USDT,
    )

    due = withdrawal_service.due_for_polling(session)
    assert btcpay_id in due
    assert manual_id not in due


def test_the_submitter_leaves_a_manual_row_untouched(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: Any
) -> None:
    """End to end: no payout is created and the row does not move."""
    manual_id = make_row(
        session,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        status=WithdrawalStatus.APPROVED,
        asset=USDT,
    )

    report = payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings())
    assert report.submitted == 0
    assert fake_btcpay.payouts == {}

    withdrawal = withdrawal_service.get(session, manual_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.APPROVED
    assert withdrawal.backend_ref is None


def test_the_btcpay_path_still_works(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
) -> None:
    """The filters must not break the asset they were added for."""
    credit_user(session, user="still-works", amount=1_000_000)
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": "still-works",
            "asset": BTC,
            "amount": "100000",
            "destination_address": BTC_DEST,
        },
        headers={**bearer(readwrite_key), "Idempotency-Key": "w-still-works"},
    )
    assert response.status_code == 201
    withdrawal_id = uuid.UUID(response.json()["withdrawal_id"])

    assert (
        payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings()).submitted
        == 1
    )

    session.rollback()
    assert withdrawal_id in withdrawal_service.due_for_polling(session)

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED


# -- fix 2: the misconfiguration cannot boot -------------------------------


def test_usdt_auto_withdraw_is_refused_at_startup() -> None:
    """Otherwise `place_hold` creates a row born approved that nothing can send.

    The only code that hands a manual withdrawal to an operator is the admin
    approve endpoint, and its compare-and-swap requires `pending_approval`. An
    auto-approved manual row has no legal next step at all, so the user's
    balance would sit held with no way forward and no alert.
    """
    with pytest.raises(ValidationError, match="USDT_AUTO_WITHDRAW=true is not supported"):
        Settings(
            database_url="postgresql://cpapi:cpapi@localhost:5432/cpapi",
            usdt_auto_withdraw=True,
            _env_file=None,
        )  # type: ignore[call-arg]


def test_the_default_still_boots() -> None:
    settings = Settings(
        database_url="postgresql://cpapi:cpapi@localhost:5432/cpapi",
        _env_file=None,
    )  # type: ignore[call-arg]
    assert settings.usdt_auto_withdraw is False


# -- fix 3: Job C never computed USDT custody ------------------------------


def _invariants_job(settings: Settings, gateway: Any) -> runner.Job:
    return next(job for job in runner.build_jobs(settings, gateway) if job.name == "invariants")


def test_job_c_checks_usdt_custody_when_tron_is_configured(
    session: Session,
    fake_btcpay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hourly job had never once computed USDT insolvency.

    `build_jobs` called check_invariants without `tron=`, the parameter
    defaulted to None, and `insolvent` short-circuits to False when there is no
    chain balance. The alarm existed and was wired to nothing.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)

    fake_tron = FakeTronGrid()
    fake_tron.trc20_balance = 12_345_678
    monkeypatch.setattr(runner, "get_tron_gateway", lambda: fake_tron)

    report = _invariants_job(settings, fake_btcpay).run()

    usdt = next(line for line in report.custody if line.asset_id == USDT)
    assert usdt.chain_source == "trongrid"
    assert usdt.chain_balance == 12_345_678


def test_job_c_is_unchanged_without_tron_configured(
    session: Session, fake_btcpay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BTC-only deployment must not start calling TronGrid."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", None)

    def explode() -> Any:
        raise AssertionError("a BTC-only deployment must not build a TronGrid client")

    monkeypatch.setattr(runner, "get_tron_gateway", explode)

    report = _invariants_job(settings, fake_btcpay).run()

    usdt = next(line for line in report.custody if line.asset_id == USDT)
    assert usdt.chain_source == "no_source"
    assert usdt.chain_balance is None


def test_job_c_raises_the_usdt_insolvency_alert(
    session: Session,
    fake_btcpay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal that could not fire before this fix."""
    from crypto_processing_api.alerts import notifier
    from crypto_processing_api.alerts.notifier import AlertCode, Severity

    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(settings, "usdt_contract_address", USDT_CONTRACT)

    # A user is owed USDT that the TRON wallet does not hold.
    from crypto_processing_api.ledger import service as ledger
    from crypto_processing_api.ledger.models import AccountKind, EntryKind

    hot = ledger.get_system_account(session, asset_id=USDT, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(
        session, asset_id=USDT, external_user_id="short-changed"
    )
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=USDT,
        source_ref=f"btcpay_payment:{uuid.uuid4().hex}:0",
        postings=[(hot.id, 100_000_000), (available.id, -100_000_000)],
    )
    session.commit()

    fake_tron = FakeTronGrid()
    fake_tron.trc20_balance = 1_000_000  # far less than the 100 USDT owed
    monkeypatch.setattr(runner, "get_tron_gateway", lambda: fake_tron)

    sent: list[str] = []

    class Capture:
        def send(self, severity: Severity, code: str, message: str) -> None:
            sent.append(code)

    notifier.set_transport(Capture())
    try:
        report = _invariants_job(settings, fake_btcpay).run()
    finally:
        notifier.set_transport(None)

    usdt = next(line for line in report.custody if line.asset_id == USDT)
    assert usdt.insolvent is True
    assert AlertCode.CUSTODY_INSOLVENCY_SIGNAL.value in sent


# -- the remediation SQL from the runbook ----------------------------------


def test_the_remediation_sql_routes_stranded_rows_to_the_approval_queue(
    session: Session,
) -> None:
    """Exactly the statement in docs/runbook-usdt-withdrawals.md.

    A row stranded by the old bug sits in `approved` (or `submitting`) with a
    manual backend and no `backend_ref`.

    `approved -> pending_approval` is deliberately **not** in the transition
    matrix — nothing in the running service may walk a withdrawal backwards.
    The remediation is therefore raw SQL, and it is safe for exactly one
    reason: `backend_ref IS NULL` proves no payout was ever created, so no
    transaction can be in flight. It touches no ledger row; the hold stays
    exactly as it was.
    """
    assert (
        WithdrawalStatus.PENDING_APPROVAL
        not in withdrawal_service._ALLOWED[WithdrawalStatus.APPROVED]
    ), "if the matrix ever allows this, the runbook should use the service, not SQL"
    stranded_approved = make_row(
        session,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        status=WithdrawalStatus.APPROVED,
        asset=USDT,
    )
    stranded_submitting = make_row(
        session,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        status=WithdrawalStatus.SUBMITTING,
        asset=USDT,
    )
    healthy_btcpay = make_row(
        session, backend=withdrawal_service.BACKEND_BTCPAY, status=WithdrawalStatus.APPROVED
    )

    session.execute(
        text(
            """
            UPDATE withdrawals
               SET status = 'pending_approval',
                   approval_mode = 'manual',
                   approved_by = NULL,
                   updated_at = now()
             WHERE backend = 'manual_tron'
               AND status IN ('approved', 'submitting')
               AND backend_ref IS NULL
            """
        )
    )
    session.commit()

    for stranded in (stranded_approved, stranded_submitting):
        row = withdrawal_service.get(session, stranded)
        session.refresh(row)
        assert row.status == WithdrawalStatus.PENDING_APPROVAL
        assert row.approval_mode == "manual"

    # The BTCPay row is untouched: it has a working path already.
    btcpay_row = withdrawal_service.get(session, healthy_btcpay)
    session.refresh(btcpay_row)
    assert btcpay_row.status == WithdrawalStatus.APPROVED


def test_a_remediated_row_can_be_approved_normally(
    client: TestClient, session: Session, admin_key: str
) -> None:
    """The point of the remediation: the ordinary admin flow takes over.

    The stranded row is built the way v0.1.0 actually produced it — a real hold
    against real custody, placed with force_manual off, so it is born
    `approved` with a manual backend and no legal next step.
    """
    from crypto_processing_api.ledger import service as ledger
    from crypto_processing_api.ledger.models import AccountKind, EntryKind

    hot = ledger.get_system_account(session, asset_id=USDT, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(session, asset_id=USDT, external_user_id="stranded")
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=USDT,
        source_ref=f"btcpay_payment:{uuid.uuid4().hex}:0",
        postings=[(hot.id, 500_000_000), (available.id, -500_000_000)],
    )
    session.commit()

    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="stranded",
        asset_id=USDT,
        amount_gross=200_000_000,
        destination_address=TRON_DEST,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        force_manual=False,
    )
    session.commit()
    stranded = outcome.withdrawal.id
    # The defect, reproduced: approved, manual backend, nothing can send it.
    assert outcome.withdrawal.status == WithdrawalStatus.APPROVED
    assert outcome.withdrawal.backend == withdrawal_service.BACKEND_MANUAL_TRON

    session.execute(
        text(
            """
            UPDATE withdrawals
               SET status = 'pending_approval',
                   approval_mode = 'manual',
                   approved_by = NULL,
                   updated_at = now()
             WHERE backend = 'manual_tron'
               AND status IN ('approved', 'submitting')
               AND backend_ref IS NULL
            """
        )
    )
    session.commit()

    response = client.post(
        f"/v1/admin/withdrawals/{stranded}/approve", json={}, headers=bearer(admin_key)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
    assert response.json()["amount_net"] == "199.000000"
