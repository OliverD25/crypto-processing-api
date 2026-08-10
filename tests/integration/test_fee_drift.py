"""The settle entry when the rail charged something other than the estimate.

The number that has to come out right is `hot_wallet`. It is what Job C
compares against the live wallet, and the whole reason fee drift is journalled
is that an unbooked fee makes those two disagree by a growing amount with no
entry naming the difference.

So every test here asserts the balances after settle rather than the postings
that produced them, and the autouse ledger check in `conftest` re-derives them
from the postings afterwards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import custody_reports
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Posting,
    Withdrawal,
    WithdrawalStatus,
)
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.fees import FeeQuote
from tests.fakes import regtest_address
from tests.integration.conftest import BTC, credit_user

DEST = regtest_address("fee-drift-destination")
FUNDED = 10_000_000
GROSS = 100_000


def system_balance(session: Session, kind: AccountKind) -> int:
    session.rollback()
    return ledger.get_system_account(session, asset_id=BTC, kind=kind).balance


def hold_and_submit(session: Session, user: str, quote: FeeQuote) -> Withdrawal:
    """Take a withdrawal to `submitted` with a given quote, without a backend."""
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id=user,
        asset_id=BTC,
        amount_gross=GROSS,
        destination_address=DEST,
    )
    withdrawal = outcome.withdrawal
    withdrawal.fee_amount = quote.fee
    withdrawal.amount_net = quote.net
    withdrawal.status = WithdrawalStatus.SUBMITTED
    withdrawal_service.post_submit_entry(session, withdrawal, quote)
    session.commit()
    return withdrawal


def chain_fee_quote(wallet_fee: int) -> FeeQuote:
    """A miner-fee quote: the user pays it and the wallet expects to spend it."""
    return FeeQuote(
        fee=wallet_fee,
        net=GROSS - wallet_fee,
        wallet_fee=wallet_fee,
        sat_per_vb=10.0,
        source="test",
    )


def flat_fee_quote(service_fee: int) -> FeeQuote:
    """The Lightning shape: a service fee, and nothing reserved for the rail."""
    return FeeQuote(
        fee=service_fee,
        net=GROSS - service_fee,
        wallet_fee=0,
        sat_per_vb=0.0,
        source="test",
    )


def settle_postings(session: Session, withdrawal: Withdrawal) -> dict[AccountKind, int]:
    session.rollback()
    rows = session.execute(
        select(Account.kind, Posting.amount)
        .join(Posting, Posting.account_id == Account.id)
        .where(Posting.entry_id == withdrawal.settle_entry_id)
    ).all()
    return {AccountKind(kind): int(amount) for kind, amount in rows}


# -- no drift --------------------------------------------------------------


def test_an_exact_estimate_writes_the_entry_it_always_wrote(session: Session) -> None:
    """On-chain BTC must be unaffected by any of this.

    The fee is fixed when the payout is created and the wallet pays exactly it,
    so the drift posting is zero and is omitted. Four postings, not five.
    """
    credit_user(session, user="exact", amount=FUNDED)
    withdrawal = hold_and_submit(session, "exact", chain_fee_quote(3_000))
    hot_before = system_balance(session, AccountKind.HOT_WALLET)

    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=3_000)
    session.commit()

    postings = settle_postings(session, withdrawal)
    assert AccountKind.HOT_WALLET not in postings
    assert postings[AccountKind.NETWORK_FEE_EXPENSE] == 3_000
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before


def test_no_reported_fee_books_the_estimate(session: Session) -> None:
    """`None` is "the rail cannot say", and the estimate is all there is."""
    credit_user(session, user="unreported", amount=FUNDED)
    withdrawal = hold_and_submit(session, "unreported", chain_fee_quote(3_000))
    hot_before = system_balance(session, AccountKind.HOT_WALLET)

    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=None)
    session.commit()

    assert settle_postings(session, withdrawal)[AccountKind.NETWORK_FEE_EXPENSE] == 3_000
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before


# -- drift in both directions ----------------------------------------------


def test_a_rail_that_charged_more_takes_it_out_of_the_hot_wallet(session: Session) -> None:
    credit_user(session, user="over", amount=FUNDED)
    withdrawal = hold_and_submit(session, "over", chain_fee_quote(3_000))
    hot_before = system_balance(session, AccountKind.HOT_WALLET)

    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=3_500)
    session.commit()

    postings = settle_postings(session, withdrawal)
    assert postings[AccountKind.NETWORK_FEE_EXPENSE] == 3_500
    assert postings[AccountKind.HOT_WALLET] == -500
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before - 500


def test_a_rail_that_charged_less_gives_the_surplus_back(session: Session) -> None:
    """The reserve was too big, so the coins never left and stay ours."""
    credit_user(session, user="under", amount=FUNDED)
    withdrawal = hold_and_submit(session, "under", chain_fee_quote(3_000))
    hot_before = system_balance(session, AccountKind.HOT_WALLET)

    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=2_000)
    session.commit()

    postings = settle_postings(session, withdrawal)
    assert postings[AccountKind.NETWORK_FEE_EXPENSE] == 2_000
    assert postings[AccountKind.HOT_WALLET] == 1_000
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before + 1_000


# -- the Lightning shape ---------------------------------------------------


def test_a_routing_fee_against_a_flat_fee_quote_lands_entirely_on_the_hot_wallet(
    session: Session,
) -> None:
    """The case the whole mechanism was built for.

    `FlatFee` reserves nothing for the rail, so `committed == net` and the
    routing fee is pure drift. Every satoshi of it has to come off the hot
    wallet, because every satoshi of it really left the channel.
    """
    credit_user(session, user="ln", amount=FUNDED)
    withdrawal = hold_and_submit(session, "ln", flat_fee_quote(100))
    hot_before = system_balance(session, AccountKind.HOT_WALLET)
    in_flight_before = system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT)
    assert in_flight_before == GROSS - 100, "a flat fee commits net only"

    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=7)
    session.commit()

    postings = settle_postings(session, withdrawal)
    assert postings[AccountKind.NETWORK_FEE_EXPENSE] == 7
    assert postings[AccountKind.HOT_WALLET] == -7
    assert postings[AccountKind.FEE_INCOME] == -100
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0
    assert system_balance(session, AccountKind.HOT_WALLET) == hot_before - 7


def test_without_the_drift_posting_custody_would_read_short(session: Session) -> None:
    """States the failure the journalling prevents, in the ledger's own terms.

    `custody` is what Job C compares against the node's channel balance. The
    node is down by net + routing fee. Booked correctly, custody is down by the
    same. Booked without the fee it would be down by only the net, and the gap
    would grow with every withdrawal until the insolvency signal fired with
    nothing to point at.
    """
    credit_user(session, user="custody", amount=FUNDED)
    before = next(r for r in custody_reports(session) if r.asset_id == BTC).custody

    withdrawal = hold_and_submit(session, "custody", flat_fee_quote(100))
    withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=7)
    session.commit()
    session.rollback()

    after = next(r for r in custody_reports(session) if r.asset_id == BTC).custody
    left_the_node = (GROSS - 100) + 7
    assert before - after == left_the_node


# -- refusals --------------------------------------------------------------


def test_a_negative_fee_is_refused(session: Session) -> None:
    """A fee that pays us is a parsing bug, and booking it would credit the
    hot wallet with money nothing sent."""
    credit_user(session, user="negative", amount=FUNDED)
    withdrawal = hold_and_submit(session, "negative", chain_fee_quote(3_000))

    with pytest.raises(withdrawal_service.WithdrawalError, match="negative network fee"):
        withdrawal_service.post_settle_entry(session, withdrawal, actual_wallet_fee=-1)
    session.rollback()


def test_settling_without_a_submit_entry_is_still_refused(session: Session) -> None:
    """Unchanged, and worth pinning: the drift is computed from the submit
    entry, so a settle with nothing to unwind must not invent a baseline."""
    credit_user(session, user="nosubmit", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="nosubmit",
        asset_id=BTC,
        amount_gross=GROSS,
        destination_address=DEST,
    )
    session.commit()

    with pytest.raises(withdrawal_service.WithdrawalError, match="no submit entry"):
        withdrawal_service.post_settle_entry(session, outcome.withdrawal, actual_wallet_fee=5)
    session.rollback()
