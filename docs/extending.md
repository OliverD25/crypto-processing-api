# Adding your own asset

An asset here is **one database row plus one registry entry**. The row is data —
decimals, limits, fees, invoice currency. The entry is behavior — how to
validate a destination, price a fee, send the money, and ask what you actually
hold.

This guide is honest about which parts you can plug into and which are welded
shut. Read the next section before you plan anything, because roughly half the
questions people ask are answered by "you can't, and here is why".

---

## 1. What you can and cannot plug in

### Deliberately fixed

**BTCPay is the only deposit rail, and the only webhook source.** Every deposit
flows through a BTCPay invoice, and `apply_invoice_state` is the single proven
transition path for one. Your asset must be payable through *some* BTCPay
payment method — a chain plugin, the Lightning node, the USDt plugin all
qualify. A non-BTCPay deposit rail is not supported, and inventing a
`DepositRail` protocol with exactly one implementation would be decoration
rather than design.

**`post_entry` is the only code that writes postings.** If your asset needs a
money movement that does not exist, add a caller — never a second writer. A
semgrep rule enforces this and will fail your build.

**Both status matrices are shared.** Deposit and withdrawal states, and which
transitions are legal, are not per-asset. If your rail needs a state that does
not exist, that is a change to the matrix with tests for every illegal pair, not
a special case for your asset.

**BTC is the anchor.** `required=True` on its profile means a store that cannot
serve BTC fails startup rather than degrading. Your asset should almost
certainly not be required.

**`decimals` is capped at 8**, by a CHECK constraint. Amounts are BIGINT in the
smallest unit, and an 18-decimal token overflows above ~9.2e9 tokens. Adding one
is a deliberate migration and a real conversation about the integer type — not a
config row.

**Job C, the reconciliation invariants, and the ledger's zero-sum rules apply to
your asset whether you like them or not.** You may not weaken them.

### Genuinely pluggable

Four facets, each a protocol:

| Facet | Protocol | Lives in |
|---|---|---|
| Destination validation | `Callable[[Session, Settings, str], None]` | your validator |
| Fee policy | `FeePolicy.quote(*, gross) -> FeeQuote` | `services/asset_registry.py` |
| Withdrawal backend | `AutomatedWithdrawalBackend` or `OperatorWithdrawalBackend` | `services/backends.py` |
| Custody source | `CustodySource.balance() -> int \| None` | `services/asset_registry.py` |

Plus two capability flags and a payment-method matcher on the profile itself.

---

## 2. The protocols

### Withdrawal backend — pick one of two

There are honestly two kinds, and pretending otherwise cost this project a
missing type check for a whole release.

```python
class AutomatedWithdrawalBackend(Protocol):
    name: str
    def initiate(self, withdrawal, *, net: int, decimals: int) -> BackendPayout: ...
    def poll_status(self, backend_ref: str) -> BackendPayout: ...
    def cancel(self, backend_ref: str) -> bool: ...
    def find_for_withdrawal(self, withdrawal) -> tuple[BackendPayout | None, list[BackendPayout]]: ...
```

`find_for_withdrawal` is part of the contract, not a helper. Any backend that
can create a payout can crash immediately after creating one, and a backend with
no answer to *"did I already send this?"* leaves exactly two options: resubmit
and risk paying twice, or freeze the row forever.

```python
class OperatorWithdrawalBackend(Protocol):
    name: str
    def new_reference(self) -> str: ...
    def verify_broadcast(self, withdrawal, txid: str) -> Verification: ...
    def confirmations(self, block_number: int | None) -> int: ...
```

Use this when a human sends the money. `ManualTronBackend` is the worked
example: the USDt plugin has no payout handler of any kind, so BTCPay genuinely
cannot send USDT and the operator does it by hand.

**Return canonical states, never your rail's vocabulary.** `BackendPayout.state`
is a `BackendPayoutState`, and an unrecognised state must normalize to `UNKNOWN`
rather than raise — a rail inventing a state should leave the row alone and log,
not stop the poller for every other withdrawal in the batch.

### Fee policy

```python
class FeePolicy(Protocol):
    def quote(self, *, gross: int) -> FeeQuote: ...
```

`FeeQuote.committed` must equal `net + wallet_fee`. This is not a convention: it
is the number the submit entry moves into `payouts_in_flight`, and if it is
wrong the settle entry cannot sum to zero and the database trigger rejects the
whole withdrawal.

Raise `DustAmount` from `quote` when nothing useful would arrive. Raise it at
quote time — a dust check that only runs at submission has already taken the
user's balance hostage.

### Custody source

```python
class CustodySource(Protocol):
    @property
    def source_name(self) -> str: ...
    def balance(self) -> int | None: ...
```

**Return `None` when your API is unreachable. Never `0`.** Zero means "we hold
nothing", which is an insolvency emergency and pages a human. `None` means "we
do not know", which Job C reports honestly and does not alarm on. Getting this
wrong does not merely produce a false alarm — it teaches the operator to ignore
the one signal that matters.

### Destination validator

```python
def validate_my_destination(session: Session, settings: Settings, address: str) -> None:
    ...  # raise AddressError on anything you will not send to
```

Raise `AddressError` and nothing else; the caller converts it into the API's
`InvalidDestination`. Reject your own hot wallet here — paying it burns a fee to
move money between your own pockets.

---

## 3. Step by step

### a. Add the seed spec

`cli.py`, `asset_specs()`. This is read once, at `migrate`; afterwards the row is
the only source of truth.

```python
AssetSpec(
    id="MYCOIN",
    display_name="My Coin",
    decimals=8,
    unit_name="satoshi-equivalent",
    btcpay_payment_method=settings.seed_mycoin_payment_method,
    withdrawal_auto_limit=...,
    withdrawal_daily_cap=...,       # this is your loss ceiling; choose it as one
    withdrawal_user_daily_cap=None,
    withdrawal_min=1,
    withdrawal_flat_fee=0,
    invoice_currency="MYCOIN",
    pooled_addresses=False,         # True only if addresses come from a shared pool
)
```

### b. A migration, only if you need new columns

Most assets need none. If yours does, read the migration rules in
[`CONTRIBUTING.md`](../CONTRIBUTING.md): the downgrade must undo the upgrade
exactly, `alembic check` must agree the models match, and the frozen dumps in
`tests/fixtures/upgrade/` must still upgrade cleanly.

### c. The registry entry

`services/asset_registry.py`. Add a `_mycoin_profile()` and register it in
`build_registry`:

```python
AssetProfile(
    asset_id="MYCOIN",
    fee_policy=lambda ctx: FlatFee(ctx.asset.withdrawal_flat_fee),
    destination_validator=validate_my_destination,
    payment_method_matcher=matches_mycoin,
    withdrawal_backend="mycoin_payout",
    has_btcpay_wallet=True,          # does BTCPay expose a wallet API for it?
    automated_backend=lambda ctx: MyPayoutBackend(ctx.gateway),
    custody_source=lambda ctx: MyCustody(ctx.gateway),
)
```

`sweep` is derived: set `automated_backend` and it is `"automated"`, set
`operator_backend` instead and it is `"operator"`. You do not declare it, so it
cannot contradict the factories.

**On the matcher: err fuzzy, not strict.** If it fails to match the store's real
payment method id, `sync_payment_methods` *disables your asset at startup* and
deposits begin 404ing in production. That failure is silent. Read the comment on
`matches_usdt_tron` before writing yours.

### d. Startup will check you

`assert_every_enabled_asset_has_a_profile` runs in both the API and the worker,
after the payment-method sync. An enabled asset with no profile is fatal — the
alternative is an asset that looks configured, accepts deposits, and only
discovers at withdrawal time that nothing knows how to price it.

---

## 4. Run the conformance suite

This is the part that decides whether your asset actually works. The suite ships
inside the package, so it is the same one CI runs.

```python
from crypto_processing_api.testing.contracts import (
    AutomatedBackendContract,
    CustodySourceContract,
    FeePolicyContract,
)


class TestMyBackend(AutomatedBackendContract):
    @pytest.fixture
    def backend(self, my_fake):
        return MyPayoutBackend(my_fake)

    @pytest.fixture
    def withdrawal(self, session):
        return make_withdrawal_row(session, asset="MYCOIN", ...)

    @pytest.fixture
    def simulate_completion(self, my_fake):
        return lambda ref: my_fake.complete(ref)


class TestMyFee(FeePolicyContract):
    @pytest.fixture
    def policy(self):
        return FlatFee(flat_fee=1_000)

    @pytest.fixture
    def dust_gross(self):
        return 500


class TestMyCustody(CustodySourceContract):
    @pytest.fixture
    def source(self, my_fake):
        return MyCustody(my_fake)

    @pytest.fixture
    def broken_source(self, my_fake):
        my_fake.fail_next["balance"] = MyApiError("down")
        return MyCustody(my_fake)
```

`tests/integration/test_backend_contracts.py` is the worked example — both
shipped backends subclass the same classes you will.

**Passing this suite unmodified is the acceptance test.** If you find yourself
wanting to change `contracts.py` to make your backend pass, that is the contract
telling you something, and it is worth listening to before you edit it.

---

## 5. Regtest and a drill

`deploy/docker-compose.regtest.yml` is hand-rolled on purpose — keep it that
way. Add whatever node your asset needs, then extend
`scripts/dev/smoke_test.py` with a drill that asserts about **money**: the exact
number of units credited, and the number of times.

The existing seven are the standard to match. Each one names a failure it would
catch — the outage drill exists because "the poller credits what webhooks never
delivered" is the entire claim of the reconciliation design, and a drill that
only tests the happy path would not notice it breaking.

If your asset cannot be exercised on regtest — as USDT cannot — say so
explicitly in your PR and in the compose file. An untestable path that nobody
flags reads exactly like a tested one.

---

## 6. What the reconciliation jobs will do to your asset

Automatically, whether or not you asked:

- **Job B** polls your automated backend for every withdrawal in `submitted` or
  `broadcast` and applies the result through the shared state machine.
- **Job C** compares, hourly, what the ledger says you hold against what your
  `CustodySource` reports, and alerts on insolvency. It **alerts and never
  repairs** — a job that silently corrects the books destroys the evidence that
  something was wrong.
- **The wallet scan** looks for unattributed receives, but only if
  `has_btcpay_wallet=True`. Set it false and you lose that detector; that gap is
  real and should be documented for your asset the way it is for USDT.

Invariants you may not weaken: entries sum to zero, no zero-amount postings,
materialized balances equal the sum of postings, no credit-normal account goes
positive, postings are never updated or deleted, one effect per
`(kind, source_ref)`, one payout and one txid per withdrawal, a hold released
exactly once.

---

## 7. What this design does *not* prove

Being straight about the limits of the contract:

- Every asset so far rides a **BTCPay invoice** for deposits. The contract has
  never been tested against a rail that does not.
- Both shipped backends talk to **an HTTP API with a request/response shape**. A
  backend that had to hold a signing key and construct transactions locally
  would stress parts of this contract nothing has stressed yet.
- The USDT path cannot be exercised on regtest, so "USDT behavior is unchanged"
  rests on unit tests against a fake and on live Nile verification, not on a
  drill.

## 8. Worked example

`ManualTronBackend` (operator flow, no automation possible) and
`BtcpayPayoutBackend` (automated, crash recovery through metadata) are both
complete implementations in `services/backends.py`, with their registry entries
in `services/asset_registry.py` and their conformance runs in
`tests/integration/test_backend_contracts.py`. Read those three files together —
between them they cover every facet this guide describes.
