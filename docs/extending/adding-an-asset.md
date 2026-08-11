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

Plus two capability flags, a payment-method matcher, and two optional hooks
(`submission_guard`, `submitted_timeout_seconds`) on the profile itself.

### One honest correction to "a row plus an entry"

That is the shape of the contract and it holds. It is not the whole diff. Adding
`BTC_LN` also needed **two new methods on the BTCPay gateway**, because
Greenfield's Lightning endpoints are not the payout endpoints and nothing had
called them before.

Expect the same. The contract governs how your asset plugs into the money path;
it does not promise that the rail you are adding has already been talked to. If
your asset needs a call BTCPay exposes and this codebase has never made, add it
to `BTCPayGateway` and to `FakeBTCPay` — that part is ordinary growth, not a
contract change.

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

#### Two optional capabilities

Some rails can answer questions most cannot. These are separate
`runtime_checkable` protocols rather than methods on the main one, so a backend
that cannot answer says nothing instead of lying:

```python
class ReportsActualFee(Protocol):
    def actual_wallet_fee(self, payout) -> int | None: ...

class ProvesDefinitiveFailure(Protocol):
    def definitive_failure_proof(self, payout) -> str | None: ...
```

`ReportsActualFee` matters when your rail's fee is **not** knowable at
submission. If you quote a `FlatFee` and the rail also charges you something
per payment, that charge is real money leaving custody and the ledger has to
see it — implement this, or every withdrawal quietly widens the gap between the
books and the wallet. Implement it only if you can be exact; returning a guess
is worse than returning None.

`ProvesDefinitiveFailure` is the *only* way an automatic release is permitted
after a payout exists. Read `services/withdrawals.py`'s "Release legality"
before implementing it. Anything short of certainty is `None`, and `None` is
always safe: the hold then waits for an admin exactly as it does now.

The bar is higher than it first looks, and getting it wrong is a double
payment. "The rail says this payment failed" answers *has money left yet*. The
question you actually have to answer is *can money still leave*, and those come
apart whenever the rail has not stopped trying. `LightningPayoutBackend` needs
two different arguments for the two cases:

- the payout was **cancelled** — a cancelled payout is never retried, so the
  node's verdict is the whole answer;
- the payout is **still live** — BTCPay refuses to cancel one it has already
  picked up, so the proof has to be that the *destination* is dead. An expired
  BOLT11 cannot be paid by anyone, whatever retries.

If your rail can keep trying and cannot be stopped, find the equivalent of that
second argument before returning a proof. If there is no equivalent, return
`None` and let a human decide; that is what the attestation is for.

### Two optional profile hooks

```python
submission_guard: Callable[[Settings, str, int], None] | None
submitted_timeout_seconds: Callable[[Settings], int | None] | None
```

**`submission_guard`** runs the destination checks that need the net amount,
which `destination_validator` cannot do because no fee has been quoted when a
destination is first seen. It runs twice: at request time, so the caller gets a
422 they can act on, and again immediately before submission. The second run is
not belt-and-braces — `pending_approval` has no horizon, and a destination that
was valid when the user asked can be worthless by the time an operator approves.
A bitcoin address needs none of this. An expiring, amount-carrying payment
request needs all of it.

**`submitted_timeout_seconds`** is how long a payout may sit `submitted` before
the service cancels it. Leave it `None` unless your rail can genuinely get
stuck forever, and be sure before you set it: on chain, a transaction in the
mempool is being worked on, and a deadline there would mean abandoning money
already in flight.

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
    withdrawal_min=1,               # keep it above withdrawal_flat_fee
    withdrawal_flat_fee=0,
    invoice_currency="MYCOIN",
    pooled_addresses=False,         # True only if addresses come from a shared pool
    deposit_expiry_minutes=None,    # None = the global default from settings
)
```

**Your asset gets its own caps, and they bound their own pot of money.** This is
easy to skim past. `SEED_BTC_WITHDRAWAL_DAILY_CAP` says nothing about how much
`MYCOIN` can lose in a day. If your asset draws on a separate float — a second
wallet, a channel, a different chain — then its cap is a second loss ceiling and
deserves the same thought as the first.

**If your asset is optional, make the spec conditional.** `BTC_LN` is appended
only when `LIGHTNING_ENABLED`, so a deployment that never wanted it has no row
rather than a disabled one. The reverse move is deliberately not silent: turning
the flag off once the row exists fails at startup, because an asset holding user
balances must not disappear because an environment variable did.

### b. A migration, only if you need new columns

Most assets need none. If yours does, read the migration rules in
[`CONTRIBUTING.md`](https://github.com/OliverD25/crypto-processing-api/blob/main/CONTRIBUTING.md): the downgrade must undo the upgrade
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

The existing eleven are the standard to match. Each one names a failure it would
catch — the outage drill exists because "the poller credits what webhooks never
delivered" is the entire claim of the reconciliation design, and a drill that
only tests the happy path would not notice it breaking.

**Put your nodes in an overlay file, not in the base stack.** Lightning is
`deploy/docker-compose.regtest.lightning.yml`, and somebody working on BTC
should not have to boot three LND containers to run one drill. **Pin images by
digest**: a nightly whose images move under it is a green build that proves
nothing about the version it claims.

**Stage the failure, not just the success.** Drill 10 asks for more than the
node can route, and getting there needed a *third* LND node. With two, deposits
and withdrawals share a channel, so every satoshi a user deposits becomes a
satoshi we can pay straight back and a liquidity failure is impossible to set
up. A separate payee behind a deliberately small channel makes its inbound
capacity the binding constraint — which is what runs out first in production
anyway. If the interesting failure for your asset cannot be staged, say so
rather than testing around it.

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
  real and should be documented for your asset the way it is for USDT and
  BTC_LN. How much it costs you depends on the rail: for USDT it is serious,
  because a pooled address can be paid again a week later. For `BTC_LN` it is
  close to harmless, because a BOLT11 invoice belongs to one deposit and can be
  paid exactly once. Work out which you are, and write it down.
- **The payout deadline**, if you set `submitted_timeout_seconds`, cancels a
  payout that has sat `submitted` too long and then asks your backend whether
  it can prove the money never left.

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
- All three shipped backends talk to **an HTTP API with a request/response
  shape**. A backend that had to hold a signing key and construct transactions
  locally would stress parts of this contract nothing has stressed yet.
- The USDT path cannot be exercised on regtest, so "USDT behavior is unchanged"
  rests on unit tests against a fake and on live Nile verification, not on a
  drill.
- The contract has now been used once by a rail it was not designed around
  (`BTC_LN`), and it needed two additive hooks to fit. One use is evidence, not
  proof. Expect the third asset to find something too — and prefer adding an
  optional hook to bending an existing protocol, which is what happened here.

## 8. Worked example: how `BTC_LN` was added

Lightning is the asset this contract was tested against, and it was chosen
because it stresses the seams rather than because it was easy. Read the commits
in order; each one is a stage of this guide.

| Commit | What it did | Which section |
|---|---|---|
| [`55fdab6`](https://github.com/OliverD25/crypto-processing-api/commit/55fdab6) | BOLT11 decoding in `core/addresses.py`, and the spike's captured Greenfield payloads into the corpus | §2, destination validator |
| [`afc94b2`](https://github.com/OliverD25/crypto-processing-api/commit/afc94b2) | Fee-drift journalling, the two capability protocols, `LightningPayoutBackend` | §2, backend + `ReportsActualFee` |
| [`b02c475`](https://github.com/OliverD25/crypto-processing-api/commit/b02c475) | The `assets` row, the registry entry, `LightningNodeCustody`, `submission_guard` | §3 |
| [`30f8501`](https://github.com/OliverD25/crypto-processing-api/commit/30f8501) | The `submitted` deadline and the definitive-failure release | §2, `ProvesDefinitiveFailure` |
| [`a227e18`](https://github.com/OliverD25/crypto-processing-api/commit/a227e18) | Three LND nodes on the regtest stack, and the bootstrap behind a flag | §5 |

### What went to plan

The deposit rail needed **nothing**. A top-up invoice already offers `BTC-LN`,
`attach_invoice` writes the BOLT11 into `deposits.address` without caring that
it is not an address, and a paid Lightning invoice goes straight to `Settled`
with no `Processing` in between — so the ordinary settled path credits it.

The withdrawal backend needed **nothing new either**.
`LightningPayoutBackend` inherits `initiate`, `poll_status`, `cancel` and
`find_for_withdrawal` from `BtcpayPayoutBackend` unchanged, and passes
`AutomatedBackendContract` unmodified. That passing run is the acceptance test
of everything in this guide.

The ledger needed **no change at all**. `BTC_LN` is a separate asset with its
own accounts, which is also the honest custodial model: channel balance and
wallet balance are different money with different risk.

### What did not

Three things fought back, and they are the useful part of this example.

**A fee you cannot know in advance still has to be booked.** `FlatFee` reserves
nothing for the rail, so `committed == net` and the whole routing fee was
drift the settle entry never recorded. Left alone, every Lightning withdrawal
would take slightly more out of the channel than the ledger said, cumulatively,
until Job C reported an insolvency with nothing to point at. Hence
`ReportsActualFee` and the drift posting.

**A destination can go bad while it waits.** `pending_approval` has no horizon.
A BOLT11 invoice that was alive when the user asked is often dead by the time an
operator approves, and a payout against an expired one can never be paid. Hence
`submission_guard`, run twice.

**A rail can fail routinely, never say so, and refuse to be stopped.** BTCPay's
Lightning processor takes an unroutable payout, parks it `InProgress` and
leaves it there — and `DELETE` on one of those answers 400 `invalid-state`.
Hence `submitted_timeout_seconds`, and — because route-not-found is an everyday
event rather than an incident — `ProvesDefinitiveFailure`, so the ordinary case
does not become an admin queue.

Both of those facts were found by drill 10 against a real stack, *after* a
two-day feasibility spike had recorded the opposite. The spike watched a payout
with no processor configured and saw it wait politely in `AwaitingPayment`; the
real thing has a processor. If you are adding an asset, assume the same: a
spike tells you what is possible, and only the drill tells you what happens.

### The other two backends

`ManualTronBackend` (operator flow, no automation possible) and
`BtcpayPayoutBackend` (automated, crash recovery through metadata) are complete
implementations in `services/backends.py`, with their registry entries in
`services/asset_registry.py` and their conformance runs in
`tests/integration/test_backend_contracts.py`. Read those three files together —
between them they cover every facet this guide describes.
