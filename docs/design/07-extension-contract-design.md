# v0.2 Asset-Extension Contract — Design Plan

Repository: `E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api`
All paths below are relative to `src/crypto_processing_api/` unless prefixed.

---

## 1. What "an asset" actually is in v0.1.0 (read from the code)

Before the contract can be formalized, here is where asset-specific behavior actually lives today:

| Concern | Where it lives | Shape |
|---|---|---|
| Identity, decimals, limits, flat fee, payment method id | `assets` DB row (`ledger/models.py:127-150`), seeded in `cli.py:30-62` | Data — already generic |
| Deposit orchestration | `services/deposits.py` — entirely BTCPay-invoice-shaped | Code, shared |
| Deposit quirks (currency, expiry, pooled addresses, tolerance) | Hardcoded dicts/frozensets keyed by asset id (`deposits.py:56,194,333`) | Code, per-asset |
| Withdrawal backend | `services/backends.py` — `WithdrawalBackend` Protocol + `BtcpayPayoutBackend`; `ManualTronBackend` **does not implement the protocol** (it has `verify_broadcast`/`new_reference`, not `initiate`/`poll_status`/`cancel`; nothing ever type-checks it against `WithdrawalBackend`) | Code, half-formalized |
| Fee policy | Two free functions (`fees.py:114` `quote_btc_fee`, `fees.py:159` `flat_fee_quote`), routed by `if asset.id == "BTC"` at three call sites | Code, per-asset, routed by string compare |
| Destination validation | `if/elif` chain in `services/withdrawals.py:203-226` | Code, per-asset |
| Reconciliation custody source | `_chain_balance` in `workers/reconciliation.py:475-500` — `-CHAIN` suffix heuristic + `asset.id == "USDT_TRC20"` special case | Code, per-asset |
| Wallet scan eligibility | `ONCHAIN_SUFFIX` string heuristic (`reconciliation.py:55,251,482`) | Heuristic |

The ledger itself (`post_entry`, account kinds, the posting matrices in `services/withdrawals.py:1-33`) is already fully asset-generic. Good — that stays untouched.

---

## 2. The formal contract

An asset is **one DB row (data) plus one registry entry (behavior)**. The contract splits into four independent facets, and being honest about which are pluggable and which are deliberately fixed:

### 2.1 Deposit rail — deliberately NOT pluggable in v0.2

Every deposit flows through a BTCPay invoice. That is a feature: `apply_invoice_state` (`services/deposits.py:454`) is the single proven transition path, and the drills prove it. The deposit contract for a new asset is therefore **declarative**: *"your asset must be payable via a BTCPay invoice payment method"* (a chain plugin, the LN node, the USDt plugin — all qualify). What a new asset provides:

- `btcpay_payment_method` (resolved at startup by `sync_payment_methods`)
- `invoice_currency` (new column; replaces `INVOICE_CURRENCY` dict)
- `pooled_addresses: bool` (new column; replaces `POOLED_ASSETS`) — drives the tolerance/review routing and address-reservation recording in `attach_invoice`
- `deposit_expiry_minutes: int | None` (new column; replaces `_expiry_minutes`'s if/else; NULL = global default)
- a payment-method matcher pattern (registry, see 2.5)

Non-BTCPay deposit rails are explicitly out of scope for v0.2 and `docs/extending.md` says so. Inventing a `DepositRail` protocol with exactly one implementation would be contract theater.

### 2.2 Withdrawal backend — the real protocol, honestly split in two

The current `WithdrawalBackend` Protocol (`services/backends.py:32-39`) describes only the automated case. Formalize the split that already exists in practice:

```python
class AutomatedWithdrawalBackend(Protocol):      # BtcpayPayoutBackend today
    name: str
    def initiate(self, withdrawal, *, net: int, decimals: int) -> BackendPayout: ...
    def poll_status(self, backend_ref: str) -> BackendPayout: ...
    def cancel(self, backend_ref: str) -> bool: ...
    def find_for_withdrawal(self, withdrawal) -> tuple[BackendPayout | None, list[BackendPayout]]: ...
        # crash recovery is PART of the contract, not a helper function —
        # promoted from module-level find_payout_for_withdrawal (backends.py:87)

class OperatorWithdrawalBackend(Protocol):       # ManualTronBackend today
    name: str
    def new_reference(self) -> str: ...
    def verify_broadcast(self, withdrawal, txid: str) -> Verification: ...
    def confirmations(self, block_number: int | None) -> int: ...
```

`BackendPayout` is a small normalization dataclass (id, state ∈ canonical enum, txid, paid amount) so `_PAYOUT_STATE_MAP` (`withdrawals.py:150-156`) maps from a **canonical backend state**, not BTCPay's literal strings — the one place a non-BTCPay automated backend (the future tronpy signer, an LN payout) would otherwise leak. Cheap: `BtcpayPayoutBackend` translates Greenfield's five strings; nothing else changes.

The state machine, hold/release legality, velocity caps, and all postings stay in `services/withdrawals.py` — the backend still cannot "decide things", preserving the design note at `backends.py:7-10`.

### 2.3 Fee policy — a one-method protocol

```python
class FeePolicy(Protocol):
    def quote(self, *, gross: int) -> FeeQuote: ...
```

Two implementations wrap the existing functions verbatim: `DynamicChainFee(gateway, settings, payment_method_id)` → `quote_btc_fee`; `FlatFee(flat_fee, dust_threshold)` → `flat_fee_quote`. `FeeQuote` (`fees.py:46-60`) is already backend-neutral — `committed = net + wallet_fee` is exactly the invariant the ledger needs. No changes to `fees.py` math.

### 2.4 Reconciliation hooks

```python
class CustodySource(Protocol):
    def balance(self) -> int | None: ...   # None = unavailable, NEVER 0
    source_name: str
```

Implementations: `BtcpayWalletCustody`, `TronGridCustody`, later `LightningNodeCustody`. Job C (`reconciliation.py:503`) asks the registry instead of `_chain_balance`'s if/elif. Plus two capability flags on the registry entry: `has_btcpay_wallet` (replaces the `-CHAIN` suffix heuristic for wallet-scan eligibility) and `sweep: "automated" | "operator"` (which Job B variant owns the rows).

### 2.5 Registration: an explicit in-code registry. Not entry points.

New module `services/asset_registry.py`:

```python
@dataclass(frozen=True)
class AssetProfile:
    asset_id: str
    fee_policy: Callable[[RegistryContext], FeePolicy]
    destination_validator: Callable[[Session, Settings, str], None]
    withdrawal_backend: str            # name; factory resolved via context
    custody_source: Callable[[RegistryContext], CustodySource] | None
    payment_method_matcher: Callable[[str], bool]   # from assets.py _matches
    has_btcpay_wallet: bool
    required: bool = False             # BTC's fail-loud startup policy

def build_registry(settings: Settings) -> dict[str, AssetProfile]: ...
```

Why explicit registry over entry points or config-file dispatch:
- **mypy --strict checks it.** A fork that registers a backend missing a method fails type-check, not production.
- The repo is a **deploy-first, fork-second** project with one maintainer. Entry points serve third-party *packages* extending an installed app; forks just edit one file. Entry points also make "what code runs" invisible in review — bad for money code.
- Behavior names in the DB (e.g. a `fee_policy` column saying `"flat"`) create a DB-vs-code version skew failure mode. **Data in DB, behavior in code**, keyed by asset id at startup; startup fails loudly if an enabled asset row has no registry entry.

### 2.6 What stays hardcoded on purpose (document, don't apologize)

- BTCPay as the sole deposit orchestrator and webhook source.
- `post_entry` as the only postings writer; account kinds; both status matrices.
- `REQUIRED_ASSETS = {"BTC"}` (`assets.py:32`) — becomes the `required` flag, defaulting BTC to true; BTC remains the anchor asset.
- `decimals BETWEEN 0 AND 8` check constraint (`ledger/models.py:149`) — an 18-decimal asset is a deliberate migration, exactly as the comment says.

---

## 3. Which asset proves the contract: **Lightning (asset id `BTC_LN`)**

Verified against current upstream state:
- **Litecoin** in BTCPay 2.x is a *community-maintained plugin* — altcoins were removed from core; "if a certain altcoin is not actively supported or tested, it may be removed in future versions." Against a pinned BTCPay 2.4.2 with a one-maintainer project, that is a standing compatibility liability, and semantically LTC is BTC with different prefixes: it would prove the registry mechanics and nothing else.
- **Lightning** is core-native (no plugin risk), and BTCPay's payout processors natively support "bitcoin addresses, BOLT11, lightning address, and LNURL for on-chain and off-chain payment methods" — i.e. the `BTC-LN` payout method rides the exact same Greenfield payout API that `BtcpayPayoutBackend` already wraps.

**What Lightning stresses that Litecoin cannot:**

| Contract seam | How LN stresses it |
|---|---|
| Deposit data flags | No address, no reservation window, instant settle — `pooled_addresses=False`, `attach_invoice`'s address block correctly no-ops (`method.destination` is a BOLT11, still recordable) |
| `_target_status` | LN invoices go `New → Settled` with no `Processing`; instant-settle path exercised |
| Destination validation | BOLT11 decoding: amount-encoded invoices must match `net` exactly, expiry must exceed the approval-queue horizon — a genuinely new validator (~150 lines in `core/addresses.py` style, bech32 already implemented there) |
| Fee policy | Routing fees are unknowable in advance → `FlatFee` (or flat + pct later); proves fee policy is truly per-asset, not "BTC vs rest" |
| Withdrawal backend | Same Greenfield payout API, different payout method id and different failure modes (no route, liquidity) — proves `BtcpayPayoutBackend` is reusable when the state normalization of 2.2 is real |
| Custody / Job C | The float is **channel local balance**, read from Greenfield `GET /api/v1/stores/{id}/lightning/BTC/balance` — a third `CustodySource`, and the first where "insolvent" means "outbound liquidity < obligations" |
| Wallet scan | `has_btcpay_wallet=False` — proves the capability flag, since `BTC-LN` has no wallet transactions API |

**Ledger model decision:** `BTC_LN` is a **separate asset** with its own accounts. This is the honest custodial model — on-chain BTC and channel BTC are different floats with different risk — and it means zero ledger changes. An internal on-chain⇄LN rebalance is an operator action outside user flows (documented in the runbook; a future `EntryKind` if ever automated).

**Honest cost estimate (one maintainer, part-time):**
- Contract refactor + conformance suite: **5-7 days**
- Lightning asset (validator, registry entry, custody source, seed): **4-6 days**
- Regtest stack + 3 new drills: **3-5 days**
- Total ≈ **3-4 weeks calendar**, vs ≈ 1 week for Litecoin.

**What LN does *not* prove:** a non-BTCPay deposit rail (still invoices), a community-plugin asset (that's what USDT already is), or per-user derived addresses beyond what BTC proves. Say so in `docs/extending.md`.

---

## 4. Refactor steps, by file (each step lands with all 569 tests green)

1. **`services/asset_registry.py` (new)** — protocols (`FeePolicy`, `CustodySource`), `AssetProfile`, `build_registry`. Pure addition.
2. **Migration `0006_asset_extension_columns`** — add to `assets`: `pooled_addresses bool NOT NULL DEFAULT false` (data-migrate USDT_TRC20→true), `invoice_currency text` (backfill BTC→'BTC', USDT_TRC20→'USDT'), `deposit_expiry_minutes int NULL`. Server defaults reproduce v0.1.0 behavior exactly → clean upgrade.
3. **`services/deposits.py`** — delete `INVOICE_CURRENCY` (:56), `_expiry_minutes` (:194), `POOLED_ASSETS` (:333); read the new columns. `_breaches_usdt_tolerance` renames to `_breaches_pooled_tolerance`.
4. **`services/backends.py`** — introduce `BackendPayout` + the two protocols; move `find_payout_for_withdrawal` onto `BtcpayPayoutBackend`; explicitly annotate `ManualTronBackend` as `OperatorWithdrawalBackend` so mypy finally checks the deviation.
5. **`services/withdrawals.py`** — `validate_destination` (:203) delegates to registry validator; `_PAYOUT_STATE_MAP` keys become canonical states; **add `Withdrawal.backend` filters to `due_for_submission` (:835) and `due_for_polling` (:845)** (see leaks 8/9 — this is a bug fix, do it first and standalone).
6. **`api/withdrawals.py`** (:104-124) and **`api/admin.py`** (:213-222) — fee quote and backend routing via registry; the approve-handover branch keys on `profile.sweep == "operator"` instead of `BACKEND_MANUAL_TRON`.
7. **`workers/payout_submitter.py`** (:83, :96) — fee via `profile.fee_policy`, backend via registry factory.
8. **`workers/reconciliation.py`** — `_chain_balance` → registry `custody_source`; `ONCHAIN_SUFFIX` checks (:251, :482) → `has_btcpay_wallet`.
9. **`services/assets.py`** — `_matches` (:46) → `profile.payment_method_matcher`; `REQUIRED_ASSETS` → `profile.required`.
10. **`cli.py`** — `asset_specs` gains the new columns; later the `BTC_LN` spec.
11. **Conformance suite** (section 6), then **Lightning** (M-plan below).

---

## 5. Regtest story for Lightning

Extend `deploy/docker-compose.regtest.yml` (hand-rolled, per its own doc comment — keep that):

- `lnd-btcpay`: `btcpayserver/lnd` (pin exact tag), REST on internal network, chain backend = existing `bitcoind`; BTCPay container gets `BTCPAY_BTCLIGHTNING=type=lnd-rest;server=...;macaroonfilepath=...` via shared volume.
- `lnd-user`: second LND node, the counterparty wallet (the LN analog of the existing drills' funded regtest wallet).
- `scripts/dev/ln_bootstrap.sh`: fund both nodes from mined coins, open a channel in each direction (user→btcpay funds deposits; btcpay→user funds withdrawal liquidity), mine 6 blocks, wait for `synced_to_chain`.
- `scripts/bootstrap_btcpay.py`: enable the `BTC-LN` payment method on the store; enable the Lightning automated payout processor.

**New drills (continuing the existing 7):**
8. *LN deposit*: create `BTC_LN` deposit → `lnd-user` pays the invoice's BOLT11 → assert instant `settled`, ledger credit, webhook + sweep agreement.
9. *LN withdrawal*: `lnd-user` generates a BOLT11 for `net` → POST withdrawal → payout via `BTC-LN` → assert `confirmed`, settle entry balances, custody source reflects reduced local balance.
10. *Liquidity exhaustion*: withdrawal exceeding outbound capacity → payout stuck/failed at BTCPay → row stays `submitted`/goes `failed` with **no automatic release** (the attestation rule holds for LN too), Job C shows the honest float.
11. *Expired BOLT11*: invoice expires while `pending_approval` → submission fails definitively → clean `failed` + admin release path.

Nightly e2e on the self-hosted runner runs drills 1-11; the LN services add ~40s to stack boot.

---

## 6. Conformance suite design

Ship it **inside the package** at `src/crypto_processing_api/testing/contracts.py` so forks can `pip install` the release and run their backend against the same suite CI runs (this is the highest-leverage DX move: the suite is the contract's executable form).

Pattern: abstract pytest classes; the implementer subclasses and provides fixtures.

```python
class AutomatedBackendContract:
    @pytest.fixture
    def backend(self) -> AutomatedWithdrawalBackend: ...          # implementer provides
    @pytest.fixture
    def withdrawal(self) -> Withdrawal: ...                        # implementer provides
    @pytest.fixture
    def simulate_completion(self) -> Callable[[str], None]: ...    # drive the fake/sandbox

    def test_initiate_returns_recoverable_reference(...)     # find_for_withdrawal round-trips
    def test_find_reports_unclaimed_same_destination(...)    # the double-pay safety rule from backends.py:96
    def test_poll_is_idempotent_and_side_effect_free(...)
    def test_poll_unknown_reference_raises_not_a_state(...)
    def test_states_are_canonical(...)                       # every reachable state ∈ enum
    def test_cancel_precommit_true_postcommit_false(...)     # cancel never lies
    def test_amounts_integer_units_no_float(...)

class OperatorBackendContract:
    def test_verify_rejects_wrong_destination/_amount/_sender/_token(...)
    def test_verify_accepts_exact_tuple_only(...)
    def test_references_unique(...)

class FeePolicyContract:
    def test_gross_equals_net_plus_fee(...)                  # deduct mode
    def test_committed_equals_net_plus_wallet_fee(...)       # the submit-entry invariant
    def test_dust_raises_before_any_state_exists(...)
    def test_never_negative_never_fee_exceeds_gross(...)

class CustodySourceContract:
    def test_unavailable_is_none_never_zero(...)             # zero would trip the insolvency alarm falsely
    def test_balance_is_integer_units(...)

class EndToEndLedgerContract:                                 # the money test
    # plugs the backend into place_hold → submit → settle against a real session
    # and asserts: all entries balance, hot_wallet + in_flight conservation,
    # release-legality matrix unbreached. Reuses tests/fakes.py machinery.
```

Retrofit proof: `BtcpayPayoutBackend` (against `FakeBTCPay` in `tests/fakes.py`) and `ManualTronBackend` (against `tests/fake_tron.py`) must pass their respective contracts **before** Lightning lands. The LN backend then passes `AutomatedBackendContract` unmodified — that passing run *is* the proof the contract is real.

`docs/extending.md` outline:
1. What you can and cannot plug in (the honesty section: BTCPay deposit rail is fixed)
2. The four facets, each with its protocol signature
3. Step-by-step: DB spec (`cli.py` AssetSpec) → migration if new columns needed → registry entry → destination validator → fee policy → backend (or operator flow) → custody source
4. Run the conformance suite (copy-paste subclass template)
5. Regtest: adding your compose services and a drill
6. What the reconciliation jobs will do to your asset automatically (and the invariants you may not weaken)
7. Worked example: the actual `BTC_LN` PR, linked commit-by-commit

---

## 7. BTCPay-assumption leak list (file:line → cheapest fix)

1. `services/deposits.py:56` — `INVOICE_CURRENCY` hardcoded dict → `assets.invoice_currency` column.
2. `services/deposits.py:194-197` — `_expiry_minutes` if/else on asset id → `assets.deposit_expiry_minutes` column, NULL = default.
3. `services/deposits.py:333` — `POOLED_ASSETS` frozenset → `assets.pooled_addresses` column.
4. `services/assets.py:46-60` — `_matches` hardcodes BTC/USDT matching rules → `payment_method_matcher` on the registry entry.
5. `services/withdrawals.py:203-226` — `validate_destination` if/elif, and any unknown asset is hard-rejected ("withdrawals for X are not available") → registry validator.
6. `api/withdrawals.py:108-124` — fee routed by `asset.id == "BTC"`, backend by `asset.id == "USDT_TRC20"` → registry `fee_policy` + `withdrawal_backend`.
7. `api/admin.py:213-222` — approve-handover keyed to `BACKEND_MANUAL_TRON` literally → key on operator-sweep capability.
8. **`services/withdrawals.py:835-842` — `due_for_submission` has no backend filter.** With `usdt_auto_withdraw=true`, a small USDT withdrawal lands directly in `APPROVED` at `place_hold`, and `payout_submitter.submit_approved` (`workers/payout_submitter.py:83,96`) will quote it a **BTC miner fee** and attempt a **BTCPay payout the USDt plugin cannot pay**, stranding the row in `submitting`. Cheapest fix: `WHERE backend = 'btcpay_payout'` (one line) — do this before anything else.
9. **`services/withdrawals.py:845-860` — `due_for_polling` has no backend filter**, so manual USDT rows in `submitted` (backend_ref `manual:<uuid>`) are polled against Greenfield by Job B every sweep, producing a permanent `BTCPayNotFound` error drip (`reconciliation.py:354-367`). Same one-line fix.
10. `workers/reconciliation.py:55,251,482` — `ONCHAIN_SUFFIX = "-CHAIN"` string heuristic decides wallet-API availability → `has_btcpay_wallet` capability flag.
11. `workers/reconciliation.py:490` — `asset.id == "USDT_TRC20"` custody special case → `CustodySource` registry hook.
12. `workers/payout_submitter.py:83` — `quote_btc_fee` called unconditionally for every submission candidate (same root as leak 8) → `profile.fee_policy`.

Leaks 8 and 9 are live defects at current head, not just extension blockers — ship them as a standalone v0.1.1 fix with regression tests.

Sources: [BTCPay Altcoins FAQ](https://docs.btcpayserver.org/FAQ/Altcoin/), [BTCPay Payouts docs](https://docs.btcpayserver.org/Payouts/), [BTCPay Lightning docs](https://docs.btcpayserver.org/LightningNetwork/), [Greenfield API](https://docs.btcpayserver.org/API/Greenfield/v1/), [BTCPay 1.5.0 payout processors announcement](https://blog.btcpayserver.org/btcpay-server-1-5-0/)

### Critical Files for Implementation
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\backends.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\withdrawals.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\deposits.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\workers\reconciliation.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\api\withdrawals.py