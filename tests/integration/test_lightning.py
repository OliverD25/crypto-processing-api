"""BTC_LN end to end, against a fake BTCPay with a Lightning node behind it.

The regtest drills prove this against the real thing. These prove the parts a
drill cannot reach cheaply: every refusal, the exact postings, and what happens
when the node answers something unhelpful.

Everything here needs the `lightning` fixture, because the suite's default is
Lightning off — which is also the shipped default, and the configuration CI
should be proving green.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.alerts import notifier
from crypto_processing_api.config import get_settings
from crypto_processing_api.core.addresses import decode_bolt11
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import Account, AccountKind, Asset, WithdrawalStatus
from crypto_processing_api.services import asset_registry
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.asset_registry import (
    BACKEND_BTCPAY_LN,
    LightningNodeCustody,
    build_registry,
    crypto_code_of,
)
from crypto_processing_api.workers import payout_submitter, reconciliation
from tests.fakes import FakeBTCPay, mint_bolt11
from tests.integration.conftest import BTC_LN, bearer

FUNDED = 5_000_000
FLAT_FEE = 100

pytestmark = pytest.mark.usefixtures("lightning")


def load_real_bolt11() -> str:
    """The longest invoice a real LND actually produced, from the corpus."""
    samples = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "greenfield"
            / "ln_bolt11_samples.json"
        ).read_text(encoding="utf-8")
    )
    return max(samples.values(), key=len)


def credit_ln(session: Session, *, user: str, amount: int = FUNDED) -> None:
    hot = ledger.get_system_account(session, asset_id=BTC_LN, kind=AccountKind.HOT_WALLET)
    available, _held = ledger.get_user_accounts(session, asset_id=BTC_LN, external_user_id=user)
    ledger.post_entry(
        session,
        kind=ledger.EntryKind.DEPOSIT_CREDIT,
        asset_id=BTC_LN,
        source_ref=f"btcpay_payment:{uuid.uuid4().hex}:0",
        postings=[(hot.id, amount), (available.id, -amount)],
    )
    session.commit()


def request_withdrawal(
    client: TestClient, key: str, *, user: str, gross: int, destination: str
) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": BTC_LN,
            "amount": str(gross),
            "destination_address": destination,
        },
        headers={**bearer(key), "Idempotency-Key": f"ln-{uuid.uuid4()}"},
    )
    return response.status_code, response.json()


def system_balance(session: Session, kind: AccountKind) -> int:
    session.rollback()
    return ledger.get_system_account(session, asset_id=BTC_LN, kind=kind).balance


def user_balance(session: Session, user: str, kind: AccountKind) -> int:
    session.rollback()
    account = session.execute(
        ledger.select(Account).where(
            Account.asset_id == BTC_LN,
            Account.kind == kind,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


# -- the asset exists only when asked for -----------------------------------


def test_the_row_and_the_profile_appear_together(session: Session) -> None:
    """Data and behaviour arrive as a pair, which is the whole contract."""
    assert session.get(Asset, BTC_LN) is not None
    assert BTC_LN in asset_registry.get_registry()

    profile = asset_registry.profile_for(BTC_LN)
    assert profile.sweep == "automated"
    assert profile.withdrawal_backend == BACKEND_BTCPAY_LN
    assert profile.has_btcpay_wallet is False


def test_the_seeded_row_says_what_lightning_needs(session: Session) -> None:
    asset = session.get(Asset, BTC_LN)
    assert asset is not None
    assert asset.decimals == 8
    assert asset.invoice_currency == "BTC"
    # A BOLT11 is minted per deposit and payable once, so there is no pool and
    # no reservation window to record.
    assert asset.pooled_addresses is False
    assert asset.withdrawal_flat_fee > 0
    assert asset.withdrawal_min > asset.withdrawal_flat_fee


def test_the_registry_is_untouched_when_lightning_is_off() -> None:
    """The opt-in claim, stated as a test rather than as a paragraph."""
    settings = get_settings()
    on = build_registry(settings.model_copy(update={"lightning_enabled": True}))
    off = build_registry(settings.model_copy(update={"lightning_enabled": False}))

    assert set(off) == {"BTC", "USDT_TRC20"}
    assert set(on) == {"BTC", "USDT_TRC20", BTC_LN}


# -- deposits ---------------------------------------------------------------


def test_a_lightning_deposit_hands_out_an_invoice_and_settles_at_once(
    client: TestClient,
    session: Session,
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The deposit rail needed no change at all, which was the bet.

    `deposits.address` holds a BOLT11 rather than an address, `attach_invoice`
    records it without caring, and a paid Lightning invoice goes straight to
    `Settled` with no `Processing` in between — so `apply_invoice_state` runs
    its ordinary settled path and credits what arrived.
    """
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "ln-depositor", "asset": BTC_LN},
        headers={**bearer(readwrite_key), "Idempotency-Key": f"lnd-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    destination = body["address"]
    assert destination.startswith("lnbcrt"), "a Lightning deposit is paid to an invoice"
    invoice = decode_bolt11(destination, network="regtest")
    assert invoice.amount_msat is None, "a top-up invoice asks for no particular amount"

    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, "0.00150000", payment_id=invoice.payment_hash)
    fake_btcpay.settle(invoice_id)

    deposit_service.refresh_deposit(
        session, fake_btcpay, deposit_id=uuid.UUID(str(body["deposit_id"]))
    )
    session.commit()

    assert user_balance(session, "ln-depositor", AccountKind.USER_AVAILABLE) == 150_000


def test_a_lightning_deposit_reserves_no_address_window(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """`pooled_addresses` is false and it means something here.

    A BOLT11 invoice belongs to one deposit and is payable once, so there is no
    shared pool, no reservation window to record and no late-payment
    attribution problem — the whole apparatus USDT needs.
    """
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "ln-window", "asset": BTC_LN},
        headers={**bearer(readwrite_key), "Idempotency-Key": f"lnw-{uuid.uuid4()}"},
    )
    deposit = deposit_service.get_deposit(session, uuid.UUID(str(response.json()["deposit_id"])))

    asset = session.get(Asset, BTC_LN)
    assert asset is not None and asset.pooled_addresses is False
    assert deposit.address is not None


# -- destination validation -------------------------------------------------


def test_an_invoice_longer_than_an_address_is_accepted(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """The regression that only a real node found.

    `destination_address` was capped at 255 characters, which is generous for
    base58 and bech32 and impossible for BOLT11: a real invoice from LND is
    over 300, and one with route hints is longer again. Every test passed,
    because the minted invoices were shorter than the cap — so the first thing
    to assert here is that this one is not.
    """
    credit_ln(session, user="ln-long")
    destination = mint_bolt11(amount_sat=49_900)
    assert len(destination) > 255, "this test cannot fail if the invoice is short"
    assert len(destination) >= len(load_real_bolt11()), (
        "a minted invoice must be at least as long as the ones a real node produced, "
        "or it is testing a smaller string than production will see"
    )

    status_code, body = request_withdrawal(
        client, readwrite_key, user="ln-long", gross=50_000, destination=destination
    )
    assert status_code == 201, body


def test_a_destination_beyond_every_rail_is_still_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """The cap moved; it did not go away."""
    credit_ln(session, user="ln-huge")
    status_code, _body = request_withdrawal(
        client, readwrite_key, user="ln-huge", gross=50_000, destination="lnbcrt1" + "q" * 4000
    )
    assert status_code == 422


def test_an_amountless_invoice_is_accepted(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """The payout amount is then the only amount there is."""
    credit_ln(session, user="ln-amountless")
    status_code, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-amountless",
        gross=50_000,
        destination=mint_bolt11(),
    )
    assert status_code == 201, body


def test_an_invoice_for_exactly_the_net_amount_is_accepted(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_ln(session, user="ln-exact")
    status_code, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-exact",
        gross=50_000,
        destination=mint_bolt11(amount_sat=50_000 - FLAT_FEE),
    )
    assert status_code == 201, body
    # `amount_net` is still null here: the fee is fixed at submission, and the
    # guard compared against the request-time quote rather than a promise.
    assert body["amount_net"] is None
    assert body["status"] == "approved"


def test_an_invoice_for_the_gross_amount_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """The mistake every integrator makes once.

    BTCPay pays a BOLT11 that carries an amount at *that* amount, not at the
    payout's. An invoice for the gross would send the fee as well, so the user
    would receive more than the ledger booked and the difference would come out
    of the operator's channel.
    """
    credit_ln(session, user="ln-gross")
    status_code, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-gross",
        gross=50_000,
        destination=mint_bolt11(amount_sat=50_000),
    )

    assert status_code == 422
    assert "49900 sat would be sent" in body["detail"]


def test_an_expired_invoice_is_refused_before_any_hold(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """No hold, no row, no support ticket. Drill 11's assertion, in a test."""
    credit_ln(session, user="ln-expired")
    available_before = user_balance(session, "ln-expired", AccountKind.USER_AVAILABLE)

    expired = mint_bolt11(
        amount_sat=49_900,
        expiry_seconds=60,
        timestamp=int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
    )
    status_code, body = request_withdrawal(
        client, readwrite_key, user="ln-expired", gross=50_000, destination=expired
    )

    assert status_code == 422
    assert "expires at" in body["detail"]
    assert user_balance(session, "ln-expired", AccountKind.USER_AVAILABLE) == available_before
    assert user_balance(session, "ln-expired", AccountKind.USER_HOLD) == 0
    assert withdrawal_service.list_for_user(session, external_user_id="ln-expired", limit=10) == []


def test_an_invoice_about_to_expire_is_refused(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    """A margin, not a hard deadline: an invoice with thirty seconds left
    cannot survive being queued, submitted and routed."""
    credit_ln(session, user="ln-soon")
    soon = mint_bolt11(amount_sat=49_900, expiry_seconds=30)

    status_code, _body = request_withdrawal(
        client, readwrite_key, user="ln-soon", gross=50_000, destination=soon
    )
    assert status_code == 422


def test_a_bitcoin_address_is_refused_for_a_lightning_withdrawal(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_ln(session, user="ln-onchain")
    status_code, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-onchain",
        gross=50_000,
        destination="bcrt1qkfycfk4zd2c907ychq502p5seze67nsny0kqxq",
    )

    assert status_code == 422
    assert "BOLT11" in body["detail"]


# -- the money path ---------------------------------------------------------


def submit_and_complete(
    session: Session,
    session_factory: sessionmaker[Session],
    fake: FakeBTCPay,
    withdrawal_id: uuid.UUID,
    *,
    fee_msat: int | None,
) -> None:
    """Take a Lightning withdrawal from approved to confirmed."""
    report = payout_submitter.submit_approved(session_factory, fake, get_settings())
    assert report.submitted == 1, report

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake.complete_payout(payout_id)
    if fee_msat is not None:
        destination = withdrawal_service.get(session, withdrawal_id).destination_address
        fake.record_lightning_payment(
            decode_bolt11(destination, network="regtest").payment_hash,
            total_msat=0,
            fee_msat=fee_msat,
        )
    reconciliation.sweep_withdrawals(session_factory, fake, get_settings())


def test_a_lightning_withdrawal_books_the_routing_fee_it_paid(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The whole milestone in one test.

    The flat fee is revenue. The routing fee is a cost, it is not knowable
    until the route is found, and it has to end up in `network_fee_expense`
    with the channel balance down by exactly that much — otherwise custody
    drifts a little further from the node on every withdrawal.
    """
    credit_ln(session, user="ln-money")
    gross = 50_000
    net = gross - FLAT_FEE
    status_code, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-money",
        gross=gross,
        destination=mint_bolt11(amount_sat=net),
    )
    assert status_code == 201, body
    withdrawal_id = uuid.UUID(str(body["withdrawal_id"]))

    hot_before = system_balance(session, AccountKind.HOT_WALLET)
    # 1200 millisatoshi of routing, which is 1.2 satoshis of real money.
    submit_and_complete(session, session_factory, fake_btcpay, withdrawal_id, fee_msat=1_200)

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED
    assert withdrawal.fee_amount == FLAT_FEE
    assert withdrawal.amount_net == net

    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0
    assert system_balance(session, AccountKind.FEE_INCOME) == -FLAT_FEE
    # Rounded up from 1200 msat: the satoshi that left is booked whole.
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == 2
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before - net - 2
    assert user_balance(session, "ln-money", AccountKind.USER_HOLD) == 0


def test_the_payment_hash_becomes_the_transaction_id(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """64 hex characters, unique per invoice, so the one-txid-per-withdrawal
    index means the same thing it does on chain."""
    credit_ln(session, user="ln-txid")
    _status, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-txid",
        gross=50_000,
        destination=mint_bolt11(amount_sat=49_900),
    )
    withdrawal_id = uuid.UUID(str(body["withdrawal_id"]))
    submit_and_complete(session, session_factory, fake_btcpay, withdrawal_id, fee_msat=0)

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.txid
    assert len(withdrawal.txid) == 64


def test_a_node_that_reports_no_fee_still_settles(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """A withdrawal that never confirms is worse than a fee booked short.

    So the settle happens with the estimate, and the gap is announced through
    the alert rather than left for Job C to discover as an unexplained drift.
    """
    credit_ln(session, user="ln-nofee")
    _status, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-nofee",
        gross=50_000,
        destination=mint_bolt11(amount_sat=49_900),
    )
    withdrawal_id = uuid.UUID(str(body["withdrawal_id"]))

    # No `record_lightning_payment`, so the node answers 404 for the hash.
    submit_and_complete(session, session_factory, fake_btcpay, withdrawal_id, fee_msat=None)

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == 0


def test_a_withdrawal_whose_invoice_expired_in_the_queue_is_refunded(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The gap the adversarial review found: `pending_approval` has no horizon.

    The invoice was alive when the user asked and dead by the time the worker
    got to it. Submitting anyway creates a payout BTCPay can never pay, and the
    row would then sit in `submitted` until the timeout job cancelled it. It is
    cheaper and kinder to refuse before the backend is called — at which point
    no payout exists, which is what the automatic release is allowed to assert.
    """
    credit_ln(session, user="ln-stale")
    available_before = user_balance(session, "ln-stale", AccountKind.USER_AVAILABLE)
    _status, body = request_withdrawal(
        client,
        readwrite_key,
        user="ln-stale",
        gross=50_000,
        # Alive now, dead in three minutes.
        destination=mint_bolt11(amount_sat=49_900, expiry_seconds=180),
    )
    withdrawal_id = uuid.UUID(str(body["withdrawal_id"]))

    # The queue took an hour, which is an ordinary morning for an admin.
    withdrawal = withdrawal_service.lock(session, withdrawal_id)
    stale = mint_bolt11(
        amount_sat=49_900,
        expiry_seconds=180,
        timestamp=int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
    )
    withdrawal.destination_address = stale
    session.commit()

    report = payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings())

    assert report.submitted == 0
    assert report.failed == 1
    assert not fake_btcpay.payouts, "an unpayable invoice must not reach BTCPay"

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.REFUNDED
    assert withdrawal.released_by == withdrawal_service.ACTOR_NEVER_SUBMITTED
    assert "no longer usable" in (withdrawal.failure_reason or "")
    assert user_balance(session, "ln-stale", AccountKind.USER_AVAILABLE) == available_before


# -- the deadline and the definitive-failure release ------------------------


def submit_only(
    session: Session,
    session_factory: sessionmaker[Session],
    fake: FakeBTCPay,
    client: TestClient,
    key: str,
    *,
    user: str,
    gross: int = 50_000,
) -> uuid.UUID:
    """Get a Lightning withdrawal to `submitted` and leave it there."""
    credit_ln(session, user=user)
    _status, body = request_withdrawal(
        client,
        key,
        user=user,
        gross=gross,
        destination=mint_bolt11(amount_sat=gross - FLAT_FEE),
    )
    withdrawal_id = uuid.UUID(str(body["withdrawal_id"]))
    payout_submitter.submit_approved(session_factory, fake, get_settings())
    return withdrawal_id


def age_submission(session: Session, withdrawal_id: uuid.UUID, *, minutes: int) -> None:
    withdrawal = withdrawal_service.lock(session, withdrawal_id)
    withdrawal.submitted_at = datetime.now(UTC) - timedelta(minutes=minutes)
    session.commit()


def test_a_payout_inside_its_deadline_is_left_alone(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """A route can take a while. Impatience here is a cancelled payment."""
    submit_only(session, session_factory, fake_btcpay, client, readwrite_key, user="ln-patient")

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.checked == 0
    assert report.cancelled == 0


def test_a_stuck_payout_is_cancelled_and_released_on_proof(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """Drill 10, in a test: no liquidity, no route, no money moved.

    Route-not-found is an everyday event on Lightning, not an incident. Making
    every one of them a support ticket for an attested admin release would be a
    queue nobody clears — and it would be dishonest, because the node knows
    perfectly well that no HTLC was ever offered.
    """
    available_before = user_balance(session, "ln-stuck", AccountKind.USER_AVAILABLE)
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-stuck"
    )
    available_after_hold = user_balance(session, "ln-stuck", AccountKind.USER_AVAILABLE)
    assert available_after_hold == available_before + FUNDED - 50_000

    age_submission(session, withdrawal_id, minutes=30)
    # The node was never asked to pay it, so it has no record of the hash.

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.checked == 1
    assert report.cancelled == 1
    assert report.released == 1
    assert report.needs_attestation == 0

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.REFUNDED
    assert withdrawal.released_by == withdrawal_service.ACTOR_DEFINITIVE_FAILURE
    attestation = withdrawal.release_attestation or ""
    assert "did not leave" in attestation
    # Names the payment hash, so an operator auditing the release can ask the
    # node the same question the machine asked.
    payment_hash = decode_bolt11(withdrawal.destination_address, network="regtest").payment_hash
    assert payment_hash in attestation

    assert (
        user_balance(session, "ln-stuck", AccountKind.USER_AVAILABLE)
        == available_after_hold + 50_000
    )
    assert user_balance(session, "ln-stuck", AccountKind.USER_HOLD) == 0
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0


def test_a_stuck_payout_the_node_will_not_vouch_for_waits_for_a_human(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The legality matrix is unchanged, and this is where that shows.

    The payment is `Pending`: an HTLC may still be in flight and may still
    settle. Releasing on that would be a double-pay, so the row goes `failed`
    and stops there, waiting for an admin who has looked.
    """
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-unsure"
    )
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    payment_hash = decode_bolt11(withdrawal.destination_address, network="regtest").payment_hash
    fake_btcpay.record_lightning_payment(payment_hash, status="Pending")
    age_submission(session, withdrawal_id, minutes=30)

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.cancelled == 1
    assert report.released == 0
    assert report.needs_attestation == 1

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.FAILED
    assert withdrawal.release_entry_id is None
    assert user_balance(session, "ln-unsure", AccountKind.USER_HOLD) == 50_000


def stage_unroutable_in_progress(
    session: Session,
    fake: FakeBTCPay,
    withdrawal_id: uuid.UUID,
    *,
    invoice_expiry_seconds: int,
) -> str:
    """Reproduce what a real BTCPay does with a payout it cannot route.

    Not `AwaitingPayment`. The live stack hands the payout to its Lightning
    processor, which moves it to `InProgress` and leaves it there permanently
    once routing fails — attaching a `PayoutLightningBlob` whose preimage is
    all zeros, because nothing settled. `DELETE` on it answers 400
    `invalid-state`, which is why the fake refuses to cancel one too.
    """
    withdrawal = withdrawal_service.lock(session, withdrawal_id)
    destination = mint_bolt11(
        amount_sat=withdrawal.amount_net or 49_900,
        expiry_seconds=invoice_expiry_seconds,
        timestamp=int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
    )
    withdrawal.destination_address = destination
    payout_id = withdrawal.backend_ref
    assert payout_id
    session.commit()

    fake.payouts[payout_id].destination = destination
    fake.payouts[payout_id].state = "InProgress"
    fake.record_lightning_payment(
        decode_bolt11(destination, network="regtest").payment_hash, status="Failed"
    )
    return payout_id


def test_a_payout_the_rail_will_not_cancel_is_released_once_the_invoice_dies(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The case a live BTCPay produced and the spike never saw.

    An unroutable payout is `InProgress`, and BTCPay refuses to cancel one. So
    "cancel, then ask the node" cannot be the whole story — the rail will not
    stop, and a node that says `Failed` today might be asked to try again
    tomorrow. What makes it safe is the invoice: an expired BOLT11 cannot be
    paid by anyone, so once it is dead no retry can spend the funds.
    """
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-inprogress"
    )
    available_after_hold = user_balance(session, "ln-inprogress", AccountKind.USER_AVAILABLE)
    # An hour old with a one-minute expiry: long dead.
    stage_unroutable_in_progress(session, fake_btcpay, withdrawal_id, invoice_expiry_seconds=60)
    age_submission(session, withdrawal_id, minutes=30)

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.checked == 1
    assert report.cancelled == 0, "BTCPay refuses to cancel an in-progress payout"
    assert report.released == 1

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.REFUNDED
    assert withdrawal.released_by == withdrawal_service.ACTOR_DEFINITIVE_FAILURE
    attestation = withdrawal.release_attestation or ""
    assert "could not be cancelled" in attestation
    assert "expired" in attestation
    assert (
        user_balance(session, "ln-inprogress", AccountKind.USER_AVAILABLE)
        == available_after_hold + 50_000
    )


def test_a_live_payout_whose_invoice_is_still_alive_is_not_released(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The safety half of the same rule.

    The node says `Failed`, which on its own looks like proof. It is not: the
    rail will not stop retrying and the invoice is still payable, so a route
    appearing in the next hour would pay a withdrawal whose hold we had already
    given back. The balance stays held and an operator is told.
    """
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-stillalive"
    )
    withdrawal = withdrawal_service.lock(session, withdrawal_id)
    destination = mint_bolt11(amount_sat=withdrawal.amount_net or 49_900, expiry_seconds=86_400)
    withdrawal.destination_address = destination
    payout_id = withdrawal.backend_ref
    assert payout_id
    session.commit()
    fake_btcpay.payouts[payout_id].destination = destination
    fake_btcpay.payouts[payout_id].state = "InProgress"
    fake_btcpay.record_lightning_payment(
        decode_bolt11(destination, network="regtest").payment_hash, status="Failed"
    )
    age_submission(session, withdrawal_id, minutes=30)

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.released == 0
    assert report.needs_attestation == 1

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.release_entry_id is None
    assert user_balance(session, "ln-stillalive", AccountKind.USER_HOLD) == 50_000


def test_the_operator_is_told_once_not_once_per_sweep(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """A stuck row is re-checked every sweep, on purpose — an invoice that is
    not dead yet will be later. Alerting on each pass would put the same
    withdrawal in front of an operator sixty times an hour."""
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-noisy"
    )
    withdrawal = withdrawal_service.lock(session, withdrawal_id)
    destination = mint_bolt11(amount_sat=withdrawal.amount_net or 49_900, expiry_seconds=86_400)
    withdrawal.destination_address = destination
    payout_id = withdrawal.backend_ref
    assert payout_id
    session.commit()
    fake_btcpay.payouts[payout_id].destination = destination
    fake_btcpay.payouts[payout_id].state = "InProgress"
    fake_btcpay.record_lightning_payment(
        decode_bolt11(destination, network="regtest").payment_hash, status="Failed"
    )
    age_submission(session, withdrawal_id, minutes=30)

    alerts: list[str] = []
    notifier.set_transport(
        type("T", (), {"send": lambda _self, _sev, code, _msg: alerts.append(code)})()
    )
    try:
        for _ in range(3):
            reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())
    finally:
        notifier.set_transport(None)

    assert alerts.count(notifier.AlertCode.WITHDRAWAL_HOLD_NEEDS_ATTESTATION.value) == 1


def test_a_payout_that_completes_just_before_the_deadline_is_not_cancelled(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The race the job has to lose safely.

    A route found at the last second must settle, not be cancelled out from
    under itself, so the state is re-read after the row is selected and before
    anything is decided.
    """
    withdrawal_id = submit_only(
        session, session_factory, fake_btcpay, client, readwrite_key, user="ln-lastsecond"
    )
    age_submission(session, withdrawal_id, minutes=30)

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)

    report = reconciliation.cancel_timed_out_payouts(session_factory, fake_btcpay, get_settings())

    assert report.moved_on == 1
    assert report.cancelled == 0

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED


def test_on_chain_btc_has_no_deadline(
    session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """There is no such thing as a transaction that will not happen.

    Giving up on one would mean abandoning money already in the mempool, so BTC
    sets no timeout and this job never looks at its rows.
    """
    assert asset_registry.profile_for("BTC").submitted_timeout_seconds is None


# -- custody ----------------------------------------------------------------


def test_custody_is_outbound_liquidity_in_satoshis(fake_btcpay: FakeBTCPay) -> None:
    fake_btcpay.lightning_local_msat = "4200500"
    source = LightningNodeCustody(fake_btcpay, "BTC")

    # 4_200_500 msat is 4200.5 sat; rounded down, because overstating custody
    # is what makes an insolvency signal arrive late.
    assert source.balance() == 4_200
    assert source.source_name == "lightning_node"


def test_job_c_reports_a_lightning_custody_line(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
) -> None:
    credit_ln(session, user="ln-custody", amount=100_000)
    fake_btcpay.lightning_local_msat = str(100_000 * 1000)

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    line = next(line for line in report.custody if line.asset_id == BTC_LN)

    assert line.chain_source == "lightning_node"
    assert line.chain_balance == 100_000
    assert line.insolvent is False


def test_a_drained_channel_is_an_insolvency_signal(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
) -> None:
    """For BTC_LN "insolvent" means outbound liquidity below what users are
    owed — the failure mode that has no on-chain equivalent."""
    credit_ln(session, user="ln-drained", amount=100_000)
    fake_btcpay.lightning_local_msat = "1000"

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    line = next(line for line in report.custody if line.asset_id == BTC_LN)

    assert line.insolvent is True
    assert any("BTC_LN" in alert for alert in report.alerts)


def test_the_crypto_code_is_the_chain_not_the_payment_method() -> None:
    assert crypto_code_of("BTC-LN") == "BTC"
    assert crypto_code_of("BTC_LightningLike") == "BTC"
    assert crypto_code_of("BTC") == "BTC"
