"""The remaining refusals in the workers, the ledger and the registry.

Small branches, all of them the "and if not" half of something already tested.
They are worth a test each because every one of them is a decision the code
makes on its own, in the background, with nobody watching:

- a payment method that no longer exists disables its asset instead of
  crashing the process,
- an outbound delivery that cannot reach the platform retries instead of
  losing the event,
- an asset with no automated backend refuses to be sent by a worker rather
  than being sent by the wrong one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import InvariantViolation, assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    AccountKind,
    Asset,
    EntryKind,
    OutboundEvent,
    WithdrawalStatus,
)
from crypto_processing_api.services import asset_registry
from crypto_processing_api.services import assets as asset_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.assets import sync_payment_methods
from crypto_processing_api.workers import outbound_delivery, payout_submitter
from crypto_processing_api.workers.gas_monitor import GasMonitor
from tests.fake_tron import FakeTronGrid
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, USDT, credit_user

DEST = regtest_address("worker-edge-destination")
GROSS = 100_000
FUNDED = 1_000_000

#: Discard port. Connection refused immediately, with no DNS lookup and no
#: third party involved.
DEAD_PLATFORM = "http://127.0.0.1:9/hooks"


# -- payment-method discovery ---------------------------------------------


def test_an_asset_that_is_already_disabled_is_left_alone(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """Re-enabling an asset an operator turned off, because BTCPay happens to
    serve it again, would take a deliberate decision away from them."""
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    usdt.enabled = False
    session.commit()

    report = sync_payment_methods(session, FakeBTCPay(payment_methods=["BTC-CHAIN", "USDT_TRC20"]))
    session.commit()

    assert USDT not in report.resolved
    assert session.get(Asset, USDT).enabled is False  # type: ignore[union-attr]


def test_an_asset_with_no_registry_profile_is_disabled_rather_than_matched(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no profile there is no matcher, so nothing but an exact string
    equality can match — and an asset the code cannot operate must not stay
    enabled and take deposits."""
    monkeypatch.setattr(asset_service, "get_registry", dict)

    report = sync_payment_methods(session, FakeBTCPay(payment_methods=["SOMETHING-ELSE"]))
    session.commit()

    assert set(report.disabled) == {BTC, USDT}
    assert session.get(Asset, BTC).enabled is False  # type: ignore[union-attr]


def test_an_exact_match_still_wins_without_a_profile(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact agreement with what the row already says was never per-asset."""
    monkeypatch.setattr(asset_service, "get_registry", dict)

    report = sync_payment_methods(session, FakeBTCPay(payment_methods=["BTC-CHAIN"]))
    session.commit()

    assert report.resolved[BTC] == "BTC-CHAIN"
    assert session.get(Asset, BTC).enabled is True  # type: ignore[union-attr]


# -- the gas monitor -------------------------------------------------------


def test_the_gas_monitor_does_nothing_without_a_hot_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BTC-only deployment has no TRON wallet to watch, and asking TronGrid
    about an empty address would burn the free tier on nothing."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", None)
    tron = FakeTronGrid()

    report = GasMonitor().check(tron, settings)

    assert report.trx_balance is None
    assert report.alerted is False
    assert tron.calls == []


# -- outbound delivery -----------------------------------------------------


def queue_event(session: Session) -> OutboundEvent:
    from crypto_processing_api.services import events as event_service

    event = event_service.emit(
        session, event_type="deposit.settled", payload={"deposit_id": "d-1", "amount": "0.5"}
    )
    session.commit()
    return event


def test_an_unreachable_platform_is_retried_not_lost(
    session: Session, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No client is passed, so the worker owns and closes its own — the shape
    the runner actually calls it in. The platform being down must leave the
    event pending with its error, never delivered and never dropped."""
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_webhook_url", DEAD_PLATFORM)
    monkeypatch.setattr(settings, "platform_webhook_secret", "platform-secret")
    event = queue_event(session)

    report = outbound_delivery.deliver_pending(session_factory, settings)

    assert report.retried == 1
    assert report.delivered == 0
    session.rollback()
    session.refresh(event)
    assert event.status == outbound_delivery.STATUS_PENDING
    assert event.attempts == 1
    assert "Error" in (event.last_error or "")


def test_requeueing_a_delivered_event_is_refused(session: Session) -> None:
    """Re-sending something the platform already accepted is how an integrator
    double-credits a user."""
    event = queue_event(session)
    event.status = outbound_delivery.STATUS_DELIVERED
    session.commit()

    assert outbound_delivery.requeue(session, event.id) is False
    assert outbound_delivery.requeue(session, uuid.uuid4()) is False


def test_requeueing_a_dead_event_resets_its_attempts_and_keeps_the_error(
    session: Session,
) -> None:
    event = queue_event(session)
    event.status = outbound_delivery.STATUS_DEAD
    event.attempts = outbound_delivery.MAX_ATTEMPTS
    event.last_error = "HTTP 500"
    session.commit()

    assert outbound_delivery.requeue(session, event.id) is True
    session.commit()
    session.refresh(event)
    assert event.status == outbound_delivery.STATUS_PENDING
    assert event.attempts == 0
    # Kept on purpose: it is the only record of why it died.
    assert event.last_error == "HTTP 500"


# -- resolving a stuck submission -----------------------------------------


def test_a_lookup_failure_leaves_the_stuck_row_stuck(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """Resolution asks BTCPay which payout carries this withdrawal's id. If it
    cannot be asked, the only safe move is to change nothing — resubmitting
    would be a second payment."""
    credit_user(session, user="stuck", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="stuck",
        asset_id=BTC,
        amount_gross=GROSS,
        destination_address=DEST,
    )
    session.commit()
    withdrawal_id = outcome.withdrawal.id

    # Claimed, then abandoned before any payout existed: the shape a crash
    # between the CAS and the BTCPay call leaves behind.
    withdrawal_service.claim_for_submission(session, withdrawal_id)
    session.execute(
        text("UPDATE withdrawals SET updated_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": withdrawal_id},
    )
    session.commit()

    fake_btcpay.fail_next["list_payouts"] = BTCPayUnavailable("BTCPay is down", status_code=503)
    report = payout_submitter.resolve_stuck(session_factory, fake_btcpay, get_settings())

    assert report.adopted == 0
    assert report.resubmittable == 0
    assert report.frozen == 0
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.SUBMITTING


# -- ledger account lookups ------------------------------------------------


def test_a_user_account_kind_is_not_a_system_account(session: Session) -> None:
    """`get_system_account` filters on a NULL external_user_id, so asking it
    for a user kind would silently find nothing rather than say why."""
    with pytest.raises(ledger.AccountNotFound, match="not a system account"):
        ledger.get_system_account(session, asset_id=BTC, kind=AccountKind.USER_AVAILABLE)


def test_a_system_account_for_an_asset_that_was_never_seeded(session: Session) -> None:
    with pytest.raises(ledger.AccountNotFound, match="does not exist"):
        ledger.get_system_account(session, asset_id="DOGE", kind=AccountKind.HOT_WALLET)


def test_two_postings_that_cancel_leave_the_balance_untouched(session: Session) -> None:
    """An entry whose postings net to nothing on an account must not generate
    a balance UPDATE for it — the row is already locked, and writing zero would
    be a lock held for nothing."""
    hot = ledger.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
    before = hot.balance

    entry = ledger.post_entry(
        session,
        kind=EntryKind.ADJUSTMENT,
        asset_id=BTC,
        source_ref=f"cancels-out:{uuid.uuid4()}",
        postings=[(hot.id, 100), (hot.id, -100)],
    )
    session.commit()

    session.refresh(hot)
    assert hot.balance == before
    assert entry.id is not None


# -- the invariant assertions ----------------------------------------------


@pytest.mark.no_ledger_check
def test_the_consistency_assertion_names_the_entry_that_does_not_balance(
    session: Session, engine: Engine
) -> None:
    """The autouse fixture runs this after every test. What it says when it
    fires is the first thing a maintainer reads, so it has to name the row."""
    credit_user(session, user="broken", amount=FUNDED)
    account_id = session.execute(
        text("SELECT id FROM accounts WHERE kind = 'hot_wallet' AND asset_id = :asset"),
        {"asset": BTC},
    ).scalar_one()

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE postings DISABLE TRIGGER USER"))
    try:
        with engine.begin() as connection:
            entry_id = connection.execute(
                text(
                    "INSERT INTO journal_entries (kind, asset_id, source_ref) "
                    "VALUES ('adjustment', :asset, :ref) RETURNING id"
                ),
                {"asset": BTC, "ref": f"hand-written:{uuid.uuid4()}"},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO postings (entry_id, account_id, amount) VALUES (:e, :a, 11)"),
                {"e": entry_id, "a": account_id},
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE postings ENABLE TRIGGER USER"))

    session.rollback()
    with pytest.raises(InvariantViolation, match="sums to 11"):
        assert_ledger_consistent(session)


@pytest.mark.no_ledger_check
def test_the_consistency_assertion_names_an_asset_whose_books_do_not_close(
    session: Session, engine: Engine
) -> None:
    """Every posting for an asset must sum to zero across its accounts. A
    residual means value appeared from nowhere."""
    credit_user(session, user="residual", amount=FUNDED)
    session.commit()
    session.rollback()

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE accounts SET balance = balance + 13 WHERE kind = 'hot_wallet'")
        )
    try:
        with pytest.raises(InvariantViolation):
            assert_ledger_consistent(session)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE accounts SET balance = balance - 13 WHERE kind = 'hot_wallet'")
            )


# -- the registry ----------------------------------------------------------


def test_an_asset_nothing_can_send_refuses_to_be_handed_to_a_worker(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """USDT is operator-swept: no code exists that can send it. A worker asking
    for its backend is a bug, and answering with the on-chain one would submit
    a BTC payout for a USDT withdrawal."""
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    with pytest.raises(asset_registry.UnknownAsset, match="no automated backend"):
        asset_registry.automated_backend_for(usdt, gateway=fake_btcpay, settings=get_settings())


def test_a_usdt_backend_without_a_tron_gateway_says_what_to_configure(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    profile = asset_registry.profile_for(USDT)
    assert profile.operator_backend is not None
    with pytest.raises(asset_registry.UnknownAsset, match="TronGrid gateway"):
        profile.operator_backend(
            asset_registry.RegistryContext(
                settings=get_settings(), asset=usdt, gateway=fake_btcpay, tron=None
            )
        )


def test_usdt_reports_no_custody_source_without_a_tron_gateway(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """Not an error: a BTC-only deployment is legitimate, and Job C already
    knows how to report a line with no chain number."""
    usdt = session.get(Asset, USDT)
    assert usdt is not None
    profile = asset_registry.profile_for(USDT)
    assert profile.custody_source is not None
    assert (
        profile.custody_source(
            asset_registry.RegistryContext(
                settings=get_settings(), asset=usdt, gateway=fake_btcpay, tron=None
            )
        )
        is None
    )


def test_an_enabled_asset_with_no_profile_stops_the_process(session: Session) -> None:
    """Deliberately fatal. The quiet alternative is an asset that looks
    configured, takes money, and only discovers at withdrawal time that nothing
    knows how to price or send it."""
    session.add(
        Asset(
            id="DOGE",
            display_name="Dogecoin",
            decimals=8,
            unit_name="koinu",
            btcpay_payment_method="DOGE-CHAIN",
            withdrawal_auto_limit=0,
            withdrawal_daily_cap=0,
            invoice_currency="DOGE",
            deposit_expiry_minutes=60,
            enabled=True,
        )
    )
    session.commit()

    with pytest.raises(asset_registry.UnknownAsset, match="DOGE"):
        asset_registry.assert_every_enabled_asset_has_a_profile(
            session, asset_registry.get_registry()
        )


@pytest.mark.parametrize(
    "method_id",
    ["BTC-LN", "btc-ln", "BTC_LightningLike", "BTC-LightningNetwork"],
)
def test_every_spelling_of_the_lightning_method_matches(method_id: str) -> None:
    """A matcher that fails to match does not fail loudly: it disables the
    asset at startup, and the first anyone hears is a 404 in production."""
    assert asset_registry.matches_btc_lightning(method_id) is True


@pytest.mark.parametrize("method_id", ["BTC-CHAIN", "USDT_TRC20", "LTC-LN"])
def test_a_method_that_is_not_lightning_does_not_match(method_id: str) -> None:
    assert asset_registry.matches_btc_lightning(method_id) is False
