"""Property-based testing of the ledger against real PostgreSQL.

The example-based tests check the sequences someone thought of. This drives
arbitrary sequences of the same operations and checks, after **every single
step**, that the books still hold together and that a plain-Python model of the
balances still agrees with the database exactly.

Single-threaded on purpose. Thread scheduling is outside Hypothesis's seed, so
a race inside a rule produces failures that do not reproduce — the worst
possible artifact to be handed at 8am. Races live in
`test_ledger_concurrency.py` as plain stress tests, where the barrier makes
them deterministic.

The rules take small integers and a four-name user pool so that when Hypothesis
shrinks a failure, the printed program is short enough to paste into a shell.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.cli import asset_specs
from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    UNBOUNDED_ACCOUNT_KINDS,
    Account,
    AccountKind,
    Base,
    EntryKind,
    JournalEntry,
    NormalSide,
)

pytestmark = pytest.mark.integration

ASSET = "BTC"
USERS = ["u0", "u1", "u2", "u3"]

amounts = st.integers(min_value=1, max_value=10**9)
users = st.sampled_from(USERS)
fees = st.integers(min_value=0, max_value=10_000)


@dataclass
class Hold:
    """A hold the machine actually created, so settle/release cannot invent one."""

    withdrawal_id: str
    user: str
    gross: int
    fee: int = 0
    committed: int = 0
    submitted: bool = False
    finished: bool = False


@dataclass
class Model:
    """What the balances should be, in plain Python.

    Signs here are the ledger's own: debit-positive. `available` and `hold` are
    credit-normal so they go negative, exactly as the accounts do.
    """

    available: dict[str, int] = field(default_factory=dict)
    hold: dict[str, int] = field(default_factory=dict)
    hot: int = 0
    in_flight: int = 0
    fee_income: int = 0
    network_fee_expense: int = 0
    entries: int = 0

    def user_available(self, user: str) -> int:
        return self.available.get(user, 0)

    def user_hold(self, user: str) -> int:
        return self.hold.get(user, 0)


class LedgerMachine(RuleBasedStateMachine):
    """Drives `post_entry` and the withdrawal posting matrix."""

    holds = Bundle("holds")

    def __init__(self) -> None:
        super().__init__()
        self.engine: Engine = _ENGINE
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.model = Model()
        self._reset_database()
        self.session: Session = self.factory()
        self._accounts: dict[tuple[str, AccountKind], int] = {}
        self._open_every_account()

    # -- lifecycle --------------------------------------------------------

    def _reset_database(self) -> None:
        """Hypothesis runs many examples inside one test function.

        The autouse `clean_database` fixture only fires once per function, so
        the machine has to clean up after itself or example two inherits
        example one's balances.
        """
        tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
        with self.engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        with self.factory() as session:
            ledger.seed_assets(session, asset_specs(get_settings()))
            session.commit()

    def teardown(self) -> None:
        self.session.close()

    def _open_every_account(self) -> None:
        """Create and commit every account before any rule runs.

        Opening accounts lazily inside a rule looks tidier and is wrong. An id
        only becomes stable once it is committed: `get_user_accounts` flushes to
        obtain one, so a rule that rolls back — which `crash` does deliberately —
        discards the account row along with the postings, and a cached id then
        points at nothing. The property machine found exactly that on its second
        `crash`, which is the bug being described rather than a ledger defect.

        Production never notices because no request caches account ids across
        transactions; each one re-resolves them. Committing up front models the
        real precondition: by the time money moves, the accounts already exist.
        """
        for kind in (
            AccountKind.HOT_WALLET,
            AccountKind.PAYOUTS_IN_FLIGHT,
            AccountKind.FEE_INCOME,
            AccountKind.NETWORK_FEE_EXPENSE,
        ):
            account = ledger.get_system_account(self.session, asset_id=ASSET, kind=kind)
            self._accounts[("", kind)] = account.id
        for user in USERS:
            available, hold = ledger.get_user_accounts(
                self.session, asset_id=ASSET, external_user_id=user
            )
            self._accounts[(user, AccountKind.USER_AVAILABLE)] = available.id
            self._accounts[(user, AccountKind.USER_HOLD)] = hold.id
        self.session.commit()

    # -- helpers ----------------------------------------------------------

    def _system(self, kind: AccountKind) -> int:
        return self._accounts[("", kind)]

    def _user_accounts(self, user: str) -> tuple[int, int]:
        return (
            self._accounts[(user, AccountKind.USER_AVAILABLE)],
            self._accounts[(user, AccountKind.USER_HOLD)],
        )

    def _post(self, kind: EntryKind, source_ref: str, postings: list[tuple[int, int]]) -> None:
        ledger.post_entry(
            self.session,
            kind=kind,
            asset_id=ASSET,
            source_ref=source_ref,
            postings=postings,
        )
        self.session.commit()
        self.model.entries += 1

    # -- rules ------------------------------------------------------------

    @rule(user=users, amount=amounts)
    def deposit(self, user: str, amount: int) -> None:
        available, _hold = self._user_accounts(user)
        self._post(
            EntryKind.DEPOSIT_CREDIT,
            f"btcpay_payment:{uuid.uuid4().hex}:0",
            [(self._system(AccountKind.HOT_WALLET), amount), (available, -amount)],
        )
        self.model.hot += amount
        self.model.available[user] = self.model.user_available(user) - amount

    @rule(target=holds, user=users, amount=amounts, fee=fees)
    def hold_funds(self, user: str, amount: int, fee: int) -> Hold | None:
        """Place a hold, or be refused for exactly the right reason."""
        available_account, hold_account = self._user_accounts(user)
        affordable = -self.model.user_available(user)
        withdrawal_id = uuid.uuid4().hex

        if amount > affordable:
            with pytest.raises(ledger.InsufficientFunds):
                ledger.post_entry(
                    self.session,
                    kind=EntryKind.WITHDRAWAL_HOLD,
                    asset_id=ASSET,
                    source_ref=f"withdrawal_hold:{withdrawal_id}",
                    postings=[(available_account, amount), (hold_account, -amount)],
                )
            self.session.rollback()
            return None

        self._post(
            EntryKind.WITHDRAWAL_HOLD,
            f"withdrawal_hold:{withdrawal_id}",
            [(available_account, amount), (hold_account, -amount)],
        )
        self.model.available[user] = self.model.user_available(user) + amount
        self.model.hold[user] = self.model.user_hold(user) - amount
        return Hold(withdrawal_id=withdrawal_id, user=user, gross=amount, fee=min(fee, amount - 1))

    @rule(item=holds)
    def submit(self, item: Hold | None) -> None:
        if item is None or item.submitted or item.finished:
            return
        committed = item.gross  # deduct mode: net + wallet fee == gross
        if committed > self.model.hot:
            with pytest.raises(ledger.InsufficientFunds):
                ledger.post_entry(
                    self.session,
                    kind=EntryKind.WITHDRAWAL_SUBMIT,
                    asset_id=ASSET,
                    source_ref=f"withdrawal_submit:{item.withdrawal_id}",
                    postings=[
                        (self._system(AccountKind.PAYOUTS_IN_FLIGHT), committed),
                        (self._system(AccountKind.HOT_WALLET), -committed),
                    ],
                )
            self.session.rollback()
            return

        self._post(
            EntryKind.WITHDRAWAL_SUBMIT,
            f"withdrawal_submit:{item.withdrawal_id}",
            [
                (self._system(AccountKind.PAYOUTS_IN_FLIGHT), committed),
                (self._system(AccountKind.HOT_WALLET), -committed),
            ],
        )
        self.model.in_flight += committed
        self.model.hot -= committed
        item.committed = committed
        item.submitted = True

    @rule(item=holds)
    def settle(self, item: Hold | None) -> None:
        if item is None or not item.submitted or item.finished:
            return
        _available, hold_account = self._user_accounts(item.user)
        wallet_fee = item.committed - (item.gross - item.fee)

        postings = [
            (hold_account, item.gross),
            (self._system(AccountKind.PAYOUTS_IN_FLIGHT), -item.committed),
        ]
        if wallet_fee:
            postings.append((self._system(AccountKind.NETWORK_FEE_EXPENSE), wallet_fee))
        if item.fee:
            postings.append((self._system(AccountKind.FEE_INCOME), -item.fee))

        self._post(EntryKind.WITHDRAWAL_SETTLE, f"withdrawal_settle:{item.withdrawal_id}", postings)
        self.model.hold[item.user] = self.model.user_hold(item.user) + item.gross
        self.model.in_flight -= item.committed
        self.model.network_fee_expense += wallet_fee
        self.model.fee_income -= item.fee
        item.finished = True

    @rule(item=holds)
    def unsubmit(self, item: Hold | None) -> None:
        """Reverse the in-flight commitment when the payout will never be paid."""
        if item is None or not item.submitted or item.finished:
            return
        self._post(
            EntryKind.REVERSAL,
            f"withdrawal_submit_reversal:{item.withdrawal_id}",
            [
                (self._system(AccountKind.HOT_WALLET), item.committed),
                (self._system(AccountKind.PAYOUTS_IN_FLIGHT), -item.committed),
            ],
        )
        self.model.hot += item.committed
        self.model.in_flight -= item.committed
        item.submitted = False
        item.committed = 0

    @rule(item=holds)
    def release(self, item: Hold | None) -> None:
        if item is None or item.submitted or item.finished:
            return
        available_account, hold_account = self._user_accounts(item.user)
        self._post(
            EntryKind.WITHDRAWAL_RELEASE,
            f"withdrawal_release:{item.withdrawal_id}",
            [(hold_account, item.gross), (available_account, -item.gross)],
        )
        self.model.hold[item.user] = self.model.user_hold(item.user) + item.gross
        self.model.available[item.user] = self.model.user_available(item.user) - item.gross
        item.finished = True

    @rule(item=holds)
    def replay(self, item: Hold | None) -> None:
        """A redelivered webhook, or the poller racing it.

        Re-posting a `(kind, source_ref)` that exists must raise AlreadyPosted
        and change nothing. This is the property the whole deposit pipeline
        leans on.
        """
        if item is None:
            return
        available_account, hold_account = self._user_accounts(item.user)
        with pytest.raises(ledger.AlreadyPosted):
            ledger.post_entry(
                self.session,
                kind=EntryKind.WITHDRAWAL_HOLD,
                asset_id=ASSET,
                source_ref=f"withdrawal_hold:{item.withdrawal_id}",
                postings=[(available_account, item.gross), (hold_account, -item.gross)],
            )
        self.session.rollback()

    @rule(user=users, amount=amounts)
    def crash(self, user: str, amount: int) -> None:
        """Do the work, then die before committing. Nothing may persist."""
        available, _hold = self._user_accounts(user)
        ledger.post_entry(
            self.session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id=ASSET,
            source_ref=f"btcpay_payment:crash-{uuid.uuid4().hex}:0",
            postings=[(self._system(AccountKind.HOT_WALLET), amount), (available, -amount)],
        )
        self.session.rollback()

    # -- invariants -------------------------------------------------------

    @invariant()
    def books_hold_together(self) -> None:
        self.session.rollback()
        assert_ledger_consistent(self.session)

    @invariant()
    def model_matches_the_database(self) -> None:
        self.session.rollback()
        for user in USERS:
            for kind, expected in (
                (AccountKind.USER_AVAILABLE, self.model.user_available(user)),
                (AccountKind.USER_HOLD, self.model.user_hold(user)),
            ):
                actual = self._balance(kind, user)
                assert actual == expected, (
                    f"{kind.value} for {user}: database {actual}, model {expected}"
                )
        for kind, expected in (
            (AccountKind.HOT_WALLET, self.model.hot),
            (AccountKind.PAYOUTS_IN_FLIGHT, self.model.in_flight),
            (AccountKind.FEE_INCOME, self.model.fee_income),
            (AccountKind.NETWORK_FEE_EXPENSE, self.model.network_fee_expense),
        ):
            actual = self._balance(kind, None)
            assert actual == expected, f"{kind.value}: database {actual}, model {expected}"

    @invariant()
    def signs_never_needed_the_database_to_intervene(self) -> None:
        """Application code refuses before the CHECK constraints have to.

        The CHECKs are a backstop. If a balance ever sits on the wrong side of
        zero, something got through that should have been refused earlier.
        """
        self.session.rollback()
        for account in self.session.execute(select(Account)).scalars():
            if account.kind in UNBOUNDED_ACCOUNT_KINDS:
                continue
            if account.normal_side == NormalSide.CREDIT:
                assert account.balance <= 0, f"{account.kind.value} went positive"
            else:
                assert account.balance >= 0, f"{account.kind.value} went negative"

    @invariant()
    def entry_count_matches(self) -> None:
        """Catches silent double-posting, which balances would hide."""
        self.session.rollback()
        actual = self.session.execute(select(func.count()).select_from(JournalEntry)).scalar_one()
        assert actual == self.model.entries, (
            f"{actual} journal entries, model expected {self.model.entries}"
        )

    def _balance(self, kind: AccountKind, user: str | None) -> int:
        query = select(Account.balance).where(Account.asset_id == ASSET, Account.kind == kind)
        query = (
            query.where(Account.external_user_id == user)
            if user is not None
            else query.where(Account.external_user_id.is_(None))
        )
        found = self.session.execute(query).scalar_one_or_none()
        return 0 if found is None else found


_ENGINE: Engine


@pytest.fixture(autouse=True, scope="module")
def _bind_engine(request: pytest.FixtureRequest) -> None:
    """Hypothesis constructs the machine itself, so the engine arrives by module global."""
    global _ENGINE
    _ENGINE = request.getfixturevalue("engine")


TestLedgerProperties = LedgerMachine.TestCase
TestLedgerProperties.settings = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
