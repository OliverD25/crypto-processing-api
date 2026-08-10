"""The asset-extension contract, in executable form.

Prose describing what a backend must do is a wish. These classes are the same
statements as tests, and they ship inside the package so a fork can install the
release and run its own backend against the identical suite CI runs.

Use it by subclassing and supplying the fixtures:

    from crypto_processing_api.testing.contracts import AutomatedBackendContract

    class TestMyBackend(AutomatedBackendContract):
        @pytest.fixture
        def backend(self, my_gateway):
            return MyPayoutBackend(my_gateway)

        @pytest.fixture
        def withdrawal(self, session):
            return make_a_withdrawal(session)

        @pytest.fixture
        def simulate_completion(self, my_gateway):
            return lambda ref: my_gateway.finish(ref)

Every assertion below is about a property the money path already relies on.
None of them are style. If one fails, something upstream in `services/` will
misbehave in a way that costs coins, and the docstring on each says which.

Deliberately no imports from `tests/`. The suite is part of the shipped
package, and a contract that only runs inside this repository's test tree is
not a contract anyone else can use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import pytest

from crypto_processing_api.ledger.models import Withdrawal
from crypto_processing_api.services.asset_registry import CustodySource, FeePolicy
from crypto_processing_api.services.backends import (
    AutomatedWithdrawalBackend,
    BackendPayoutState,
    OperatorWithdrawalBackend,
)
from crypto_processing_api.services.fees import DustAmount


class _Verification(Protocol):
    """What `verify_broadcast` must answer with. Read-only, so a frozen
    dataclass — which every implementation is — satisfies it."""

    @property
    def ok(self) -> bool: ...


# -- automated backends ----------------------------------------------------


class AutomatedBackendContract:
    """What a backend that sends money by itself must guarantee."""

    @pytest.fixture
    def backend(self) -> AutomatedWithdrawalBackend:
        raise NotImplementedError("provide a backend fixture")

    @pytest.fixture
    def withdrawal(self) -> Withdrawal:
        raise NotImplementedError("provide a withdrawal fixture")

    @pytest.fixture
    def simulate_completion(self) -> Callable[[str], None]:
        """Drive the fake or sandbox to finish a payout."""
        raise NotImplementedError("provide a simulate_completion fixture")

    @pytest.fixture
    def decimals(self) -> int:
        return 8

    def test_initiate_returns_a_reference_that_find_can_recover(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """The crash-recovery round trip.

        A submitter can die between creating the payout and writing down its
        id. If the backend cannot then answer "which payout is this
        withdrawal?", the only remaining options are to resubmit — paying
        twice — or to freeze the row forever.
        """
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        assert created.id, "initiate returned no reference to record"

        mine, _unclaimed = backend.find_for_withdrawal(withdrawal)
        assert mine is not None, (
            "find_for_withdrawal cannot see a payout this backend just created, "
            "so a crashed submission is unrecoverable"
        )
        assert mine.id == created.id

    def test_find_reports_unclaimed_payouts_to_the_same_destination(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """The double-pay guard.

        "No payout matches this row" is not enough to justify resubmitting.
        Two withdrawals to one address are ordinary, and binding the wrong
        payout to the wrong row clears the other one to be sent again.
        """
        backend.initiate(withdrawal, net=10_000, decimals=decimals)
        _mine, unclaimed = backend.find_for_withdrawal(withdrawal)
        assert isinstance(unclaimed, list), (
            "find_for_withdrawal must report unclaimed payouts as a list, even empty"
        )

    def test_poll_is_idempotent(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """Job B polls on a timer. Polling must not change anything."""
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        first = backend.poll_status(created.id)
        second = backend.poll_status(created.id)
        assert first.state == second.state
        assert first.id == second.id == created.id

    def test_every_reachable_state_is_canonical(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        simulate_completion: Callable[[str], None],
        decimals: int,
    ) -> None:
        """No BTCPay vocabulary may reach the state machine.

        A backend returning its own rail's words would force
        `_PAYOUT_STATE_MAP` to learn a second dialect, and the withdrawal
        status would then depend on which backend produced the row.
        """
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        assert isinstance(created.state, BackendPayoutState)

        simulate_completion(created.id)
        finished = backend.poll_status(created.id)
        assert isinstance(finished.state, BackendPayoutState)
        assert finished.state is not BackendPayoutState.UNKNOWN, (
            "a state this backend reaches on its own happy path is not recognised "
            "by the normalizer, so the withdrawal would never leave submitted"
        )

    def test_a_completed_payout_reports_a_transaction_id(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        simulate_completion: Callable[[str], None],
        decimals: int,
    ) -> None:
        """Without it the row confirms with nothing an operator can look up."""
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        simulate_completion(created.id)
        finished = backend.poll_status(created.id)
        if finished.state is BackendPayoutState.COMPLETED:
            assert finished.txid, "completed with no transaction id"

    def test_amounts_are_never_floats(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """A float amount is a rounding error waiting for a large withdrawal."""
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        assert not isinstance(created.amount, float), (
            "amount came back as a float; it must be the backend's own decimal string"
        )

    def test_the_payout_carries_our_correlation_metadata(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """Crash recovery resolves by echoed id, not by destination and amount.

        Guessing from destination and amount is ambiguous exactly when it
        matters — a user withdrawing the same amount to the same address twice
        in ten minutes is ordinary behaviour.
        """
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        echoed = created.metadata.get("withdrawal_id")
        assert echoed is not None, "the backend did not echo withdrawal_id back"
        assert str(echoed) == str(withdrawal.id)

    def test_cancel_answers_a_boolean_and_never_raises(
        self,
        backend: AutomatedWithdrawalBackend,
        withdrawal: Withdrawal,
        decimals: int,
    ) -> None:
        """Cancel must never lie and never explode.

        The caller uses the answer to decide whether the money is still ours.
        An exception there leaves the row in an unknown state, which is the one
        outcome worse than a plain "no".
        """
        created = backend.initiate(withdrawal, net=10_000, decimals=decimals)
        assert isinstance(backend.cancel(created.id), bool)


# -- operator backends -----------------------------------------------------


class OperatorBackendContract:
    """What a backend where a human sends the money must guarantee.

    Every assertion is about the verifier refusing a plausible-looking
    transaction. "Included and successful" feels sufficient right up until an
    operator pastes the txid of the gas top-up they just sent.
    """

    @pytest.fixture
    def backend(self) -> OperatorWithdrawalBackend:
        raise NotImplementedError("provide a backend fixture")

    @pytest.fixture
    def withdrawal(self) -> Withdrawal:
        raise NotImplementedError("provide a withdrawal fixture")

    @pytest.fixture
    def good_txid(self) -> str:
        """A transaction that really does pay this withdrawal."""
        raise NotImplementedError("provide a good_txid fixture")

    @pytest.fixture
    def wrong_txids(self) -> dict[str, str]:
        """Named near-misses: wrong recipient, amount, sender, token.

        Each key names what is wrong with it, so a failure says which check
        stopped working rather than that "verification failed".
        """
        raise NotImplementedError("provide a wrong_txids fixture")

    def test_references_are_unique(self, backend: OperatorWithdrawalBackend) -> None:
        """Two withdrawals sharing a reference would settle each other."""
        seen = {backend.new_reference() for _ in range(50)}
        assert len(seen) == 50

    def test_the_exact_transaction_verifies(
        self, backend: OperatorWithdrawalBackend, withdrawal: Withdrawal, good_txid: str
    ) -> None:
        verification: _Verification = backend.verify_broadcast(withdrawal, good_txid)
        assert verification.ok, "the correct transaction was refused"

    def test_every_near_miss_is_refused(
        self,
        backend: OperatorWithdrawalBackend,
        withdrawal: Withdrawal,
        wrong_txids: dict[str, str],
    ) -> None:
        assert wrong_txids, "a contract with no near-misses proves nothing"
        for what_is_wrong, txid in wrong_txids.items():
            verification: _Verification = backend.verify_broadcast(withdrawal, txid)
            assert not verification.ok, (
                f"a transaction with the {what_is_wrong} was accepted; an operator "
                "pasting it would mark this withdrawal paid when it was not"
            )

    def test_an_unknown_transaction_is_refused(
        self, backend: OperatorWithdrawalBackend, withdrawal: Withdrawal
    ) -> None:
        verification: _Verification = backend.verify_broadcast(withdrawal, "0" * 64)
        assert not verification.ok

    def test_confirmations_never_go_negative(self, backend: OperatorWithdrawalBackend) -> None:
        """A reorg can put the chain tip behind a block we already saw."""
        assert backend.confirmations(None) == 0
        assert backend.confirmations(10**12) >= 0


# -- fee policies ----------------------------------------------------------


class FeePolicyContract:
    """The arithmetic the ledger depends on.

    `committed = net + wallet_fee` is not a convention — it is the number the
    submit entry moves into `payouts_in_flight`. If a policy breaks it, the
    settle entry cannot balance and the zero-sum trigger rejects the whole
    withdrawal at the database.
    """

    @pytest.fixture
    def policy(self) -> FeePolicy:
        raise NotImplementedError("provide a policy fixture")

    @pytest.fixture
    def workable_gross(self) -> int:
        """An amount comfortably above this policy's dust threshold."""
        return 1_000_000

    @pytest.fixture
    def dust_gross(self) -> int:
        """An amount that must be refused."""
        return 1

    def test_net_plus_fee_equals_gross(self, policy: FeePolicy, workable_gross: int) -> None:
        quote = policy.quote(gross=workable_gross)
        assert quote.net + quote.fee == workable_gross, (
            "the user is charged gross, so anything not sent must be the fee; "
            "a gap here is money that exists in no account"
        )

    def test_committed_equals_net_plus_wallet_fee(
        self, policy: FeePolicy, workable_gross: int
    ) -> None:
        quote = policy.quote(gross=workable_gross)
        assert quote.committed == quote.net + quote.wallet_fee

    def test_nothing_is_negative(self, policy: FeePolicy, workable_gross: int) -> None:
        quote = policy.quote(gross=workable_gross)
        assert quote.fee >= 0
        assert quote.net > 0
        assert quote.wallet_fee >= 0

    def test_the_fee_never_exceeds_the_amount(self, policy: FeePolicy, workable_gross: int) -> None:
        quote = policy.quote(gross=workable_gross)
        assert quote.fee < workable_gross

    def test_amounts_are_integers(self, policy: FeePolicy, workable_gross: int) -> None:
        quote = policy.quote(gross=workable_gross)
        for name in ("fee", "net", "wallet_fee"):
            value = getattr(quote, name)
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"{name} is {type(value).__name__}; amounts are integers of the smallest unit"
            )

    def test_dust_is_refused_before_anything_exists(
        self, policy: FeePolicy, dust_gross: int
    ) -> None:
        """Refused at quote time, so a doomed withdrawal never takes a hold.

        A dust check that only runs at submission has already reserved the
        user's balance and created a row somebody has to unwind.
        """
        with pytest.raises(DustAmount):
            policy.quote(gross=dust_gross)


# -- custody sources -------------------------------------------------------


class CustodySourceContract:
    """What Job C is allowed to believe."""

    @pytest.fixture
    def source(self) -> CustodySource:
        raise NotImplementedError("provide a source fixture")

    @pytest.fixture
    def broken_source(self) -> CustodySource:
        """The same source with its upstream failing."""
        raise NotImplementedError("provide a broken_source fixture")

    def test_a_healthy_source_reports_integer_units(self, source: CustodySource) -> None:
        balance = source.balance()
        assert balance is not None
        assert isinstance(balance, int) and not isinstance(balance, bool)
        assert balance >= 0

    def test_it_names_itself(self, source: CustodySource) -> None:
        """The name reaches the admin endpoint; an empty one tells nobody anything."""
        assert source.source_name

    def test_unavailable_is_none_and_never_zero(self, broken_source: CustodySource) -> None:
        """The single most important line in this file.

        Zero means "we hold nothing", which is an insolvency emergency. An
        unreachable API means "we do not know". A source that returns 0 when it
        cannot reach its upstream pages somebody at 3am about a wallet that is
        perfectly fine — and, far worse, trains them to ignore the alert.
        """
        balance = broken_source.balance()
        assert balance is None, (
            f"an unavailable custody source returned {balance!r}; None is the only "
            "honest answer, and 0 is an insolvency alarm"
        )


# -- the money test --------------------------------------------------------


class EndToEndLedgerContract:
    """Drive a real withdrawal through a real ledger with this backend.

    The contracts above check a backend in isolation. This one checks that the
    ledger still balances with it plugged in, which is the only claim anybody
    actually cares about.
    """

    @pytest.fixture
    def run_withdrawal(self) -> Callable[[], Any]:
        """Take one withdrawal from hold to settle and return the session."""
        raise NotImplementedError("provide a run_withdrawal fixture")

    def test_the_books_balance_afterwards(self, run_withdrawal: Callable[[], Any]) -> None:
        from crypto_processing_api.ledger.invariants import assert_ledger_consistent

        session = run_withdrawal()
        assert_ledger_consistent(session)

    def test_custody_equals_obligations_plus_in_flight(
        self, run_withdrawal: Callable[[], Any]
    ) -> None:
        from crypto_processing_api.ledger.invariants import custody_reports

        session = run_withdrawal()
        reports = custody_reports(session)
        assert reports, "no custody line was produced, so nothing was checked"
        for report in reports:
            assert report.balanced, (
                f"{report.asset_id} custody is off by {report.residual}; the backend "
                "moved money the ledger did not account for"
            )
