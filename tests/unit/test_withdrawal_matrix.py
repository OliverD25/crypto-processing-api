"""The transition legality matrix and the release rules, as data.

Both are encoded as sets in `services/withdrawals.py` precisely so they can be
checked exhaustively rather than sampled.
"""

from __future__ import annotations

import itertools

import pytest

from crypto_processing_api.ledger.models import WithdrawalStatus
from crypto_processing_api.services import withdrawals

ALL = list(WithdrawalStatus)

LEGAL: set[tuple[WithdrawalStatus, WithdrawalStatus]] = {
    (WithdrawalStatus.REQUESTED, WithdrawalStatus.PENDING_APPROVAL),
    (WithdrawalStatus.REQUESTED, WithdrawalStatus.APPROVED),
    (WithdrawalStatus.REQUESTED, WithdrawalStatus.REJECTED),
    (WithdrawalStatus.PENDING_APPROVAL, WithdrawalStatus.APPROVED),
    (WithdrawalStatus.PENDING_APPROVAL, WithdrawalStatus.REJECTED),
    (WithdrawalStatus.APPROVED, WithdrawalStatus.SUBMITTING),
    (WithdrawalStatus.APPROVED, WithdrawalStatus.REJECTED),
    (WithdrawalStatus.SUBMITTING, WithdrawalStatus.SUBMITTED),
    (WithdrawalStatus.SUBMITTING, WithdrawalStatus.APPROVED),
    (WithdrawalStatus.SUBMITTING, WithdrawalStatus.FAILED),
    (WithdrawalStatus.SUBMITTED, WithdrawalStatus.BROADCAST),
    (WithdrawalStatus.SUBMITTED, WithdrawalStatus.CONFIRMED),
    (WithdrawalStatus.SUBMITTED, WithdrawalStatus.FAILED),
    (WithdrawalStatus.BROADCAST, WithdrawalStatus.CONFIRMED),
    (WithdrawalStatus.BROADCAST, WithdrawalStatus.FAILED),
    (WithdrawalStatus.REJECTED, WithdrawalStatus.REFUNDED),
    (WithdrawalStatus.FAILED, WithdrawalStatus.REFUNDED),
}


def test_matrix_is_exactly_the_documented_edges() -> None:
    actual = {
        (source, target) for source, targets in withdrawals._ALLOWED.items() for target in targets
    }
    assert actual == LEGAL


@pytest.mark.parametrize(("source", "target"), sorted(set(itertools.product(ALL, ALL)) - LEGAL))
def test_every_other_transition_is_illegal(
    source: WithdrawalStatus, target: WithdrawalStatus
) -> None:
    assert target not in withdrawals._ALLOWED[source]


def test_confirmed_is_final() -> None:
    """Once the chain has it, nothing in this service may move it."""
    assert withdrawals._ALLOWED[WithdrawalStatus.CONFIRMED] == frozenset()


def test_refunded_is_final() -> None:
    assert withdrawals._ALLOWED[WithdrawalStatus.REFUNDED] == frozenset()


def test_a_withdrawal_cannot_go_straight_from_approved_to_submitted() -> None:
    """`submitting` is the compare-and-swap that stops two workers submitting."""
    assert WithdrawalStatus.SUBMITTED not in withdrawals._ALLOWED[WithdrawalStatus.APPROVED]


def test_submitting_can_return_to_approved() -> None:
    """The one resubmission path, and only after BTCPay proved nothing exists."""
    assert WithdrawalStatus.APPROVED in withdrawals._ALLOWED[WithdrawalStatus.SUBMITTING]


def test_confirmed_can_never_be_reached_from_a_released_state() -> None:
    for source in (WithdrawalStatus.REJECTED, WithdrawalStatus.REFUNDED, WithdrawalStatus.FAILED):
        assert WithdrawalStatus.CONFIRMED not in withdrawals._ALLOWED[source]


# -- release legality ------------------------------------------------------


def test_auto_release_only_where_no_transaction_can_exist() -> None:
    assert (
        frozenset(
            {
                WithdrawalStatus.REQUESTED,
                WithdrawalStatus.PENDING_APPROVAL,
                WithdrawalStatus.APPROVED,
                WithdrawalStatus.REJECTED,
            }
        )
        == withdrawals.AUTO_RELEASABLE
    )


def test_everything_past_submission_needs_an_attestation() -> None:
    """The double-pay path: refund a lingering payout, then watch it confirm."""
    assert (
        frozenset(
            {
                WithdrawalStatus.SUBMITTING,
                WithdrawalStatus.SUBMITTED,
                WithdrawalStatus.BROADCAST,
                WithdrawalStatus.FAILED,
            }
        )
        == withdrawals.ATTESTED_RELEASABLE
    )


def test_the_two_release_sets_do_not_overlap() -> None:
    assert not (withdrawals.AUTO_RELEASABLE & withdrawals.ATTESTED_RELEASABLE)


def test_confirmed_and_refunded_are_in_neither_set() -> None:
    releasable = withdrawals.AUTO_RELEASABLE | withdrawals.ATTESTED_RELEASABLE
    assert WithdrawalStatus.CONFIRMED not in releasable
    assert WithdrawalStatus.REFUNDED not in releasable


# -- what counts against the velocity cap ---------------------------------


def test_released_states_stop_counting_against_the_cap() -> None:
    """A refunded hold reserves nothing, so it must not keep the cap tripped."""
    assert WithdrawalStatus.REFUNDED not in withdrawals.COUNTS_TOWARD_CAP
    assert WithdrawalStatus.REJECTED not in withdrawals.COUNTS_TOWARD_CAP
    assert WithdrawalStatus.FAILED not in withdrawals.COUNTS_TOWARD_CAP


def test_everything_in_flight_counts_against_the_cap() -> None:
    for reserved in (
        WithdrawalStatus.REQUESTED,
        WithdrawalStatus.PENDING_APPROVAL,
        WithdrawalStatus.APPROVED,
        WithdrawalStatus.SUBMITTING,
        WithdrawalStatus.SUBMITTED,
        WithdrawalStatus.BROADCAST,
        WithdrawalStatus.CONFIRMED,
    ):
        assert reserved in withdrawals.COUNTS_TOWARD_CAP


# -- payout state mapping --------------------------------------------------


@pytest.mark.parametrize(
    ("payout_state", "expected"),
    [
        ("AwaitingApproval", WithdrawalStatus.SUBMITTED),
        ("AwaitingPayment", WithdrawalStatus.SUBMITTED),
        ("InProgress", WithdrawalStatus.BROADCAST),
        ("Completed", WithdrawalStatus.CONFIRMED),
        ("Cancelled", WithdrawalStatus.FAILED),
    ],
)
def test_payout_states_map_as_documented(payout_state: str, expected: WithdrawalStatus) -> None:
    assert withdrawals._PAYOUT_STATE_MAP[payout_state] == expected


def test_a_cancelled_payout_does_not_map_to_refunded() -> None:
    """Cancelled means stop, not "give the money back" — that needs a human."""
    assert withdrawals._PAYOUT_STATE_MAP["Cancelled"] == WithdrawalStatus.FAILED
    assert WithdrawalStatus.REFUNDED not in withdrawals._PAYOUT_STATE_MAP.values()
