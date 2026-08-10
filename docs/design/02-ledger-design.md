# crypto-processing-api — Ledger & Data Model Design (lens: money correctness)

Scope: PostgreSQL schema, state machines, concurrency/consistency strategy, and idempotency for the custodial ledger sitting between the platform and BTCPay Server. Assets: BTC (satoshis, 8 dp) and USDT-TRC20 (micro-units, 6 dp). All amounts are integers in the asset's smallest unit; no floats or fiat anywhere in the ledger.

---

## 0. Core principles

1. **The journal is the source of truth.** Everything that changes a balance is a journal entry with postings that sum to zero. Deposits, holds, releases, payouts, fees, refunds, and manual corrections are all entries. Nothing ever UPDATEs or DELETEs a posting; mistakes are fixed with reversal entries.
2. **Signed double-entry.** A posting's `amount` is signed: positive = debit, negative = credit. Per entry, `SUM(amount) = 0`. Asset-side accounts (hot wallet) are debit-normal; user balances are liabilities and credit-normal (their raw balance is negative; the API negates on read using `accounts.normal_side`).
3. **Holds live in the ledger, not in a side table.** Each user has two accounts per asset: `user_available` and `user_hold`. Placing a hold is a ledger entry, so the sum-zero invariant covers reserved funds and "available balance" is simply the balance of one account — no `balance - SUM(holds)` arithmetic to get wrong.
4. **Idempotency at two layers**: dedupe the *event* (webhook delivery id, client Idempotency-Key) and dedupe the *ledger effect* (unique key on the journal entry's business reference). Even if the event layer leaks, the ledger cannot double-post.

---

## 1. Amount type: `BIGINT`

Chosen over `NUMERIC(38,0)`:

- Range check: BTC max supply = 2.1e15 sats; BIGINT max ≈ 9.22e18 — 4,000x headroom. USDT in 1e-6 units: 9.22e18 = 9.2 trillion USDT, far above total supply. No realistic single-tenant balance or transfer overflows.
- BIGINT is 8 bytes, natively indexed, arithmetic in C; NUMERIC is variable-width and slower, and its only benefit (arbitrary precision) is unneeded for 6–8 decimal assets.
- Overflow behaves loudly: Postgres raises an error rather than wrapping, and `SUM(bigint)` returns `numeric`, so aggregate verification can't silently overflow.
- Recorded limitation: an 18-decimal asset (native ETH-style wei) would overflow BIGINT above ~9.2e9 tokens. Guard with a `CHECK (decimals <= 8)` on `assets` for MVP; adding such an asset is a schema migration decision, not a silent footgun.

Python side: Pydantic v2 `int` (arbitrary precision) serialized as **JSON strings** in API responses (`"amount": "150000000"`) to protect JavaScript consumers from 2^53 truncation.

---

## 2. Schema DDL sketch

```sql
-- ============ reference ============
CREATE TABLE assets (
    id                    TEXT PRIMARY KEY,           -- 'BTC', 'USDT_TRC20'
    display_name          TEXT NOT NULL,
    decimals              SMALLINT NOT NULL CHECK (decimals BETWEEN 0 AND 8),
    unit_name             TEXT NOT NULL,              -- 'sat', 'microUSDT'
    btcpay_payment_method TEXT NOT NULL,              -- 'BTC-CHAIN', 'USDT-TRC20' etc.
    withdrawal_auto_limit BIGINT NOT NULL,            -- gross amount; above => manual approval
    withdrawal_min        BIGINT NOT NULL DEFAULT 1,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============ chart of accounts ============
CREATE TYPE account_kind AS ENUM (
    'user_available',      -- liability, credit-normal
    'user_hold',           -- liability, credit-normal
    'hot_wallet',          -- asset (BTCPay custody float), debit-normal
    'payouts_in_flight',   -- asset: left hot wallet, not yet confirmed
    'fee_income',          -- credit-normal (fees recovered from users / service fees)
    'network_fee_expense', -- debit-normal (fees paid to miners/energy)
    'external'             -- counterparty for corrections/adjustments
);

CREATE TABLE accounts (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id         TEXT NOT NULL REFERENCES assets(id),
    kind             account_kind NOT NULL,
    external_user_id TEXT,                            -- NULL for system accounts
    normal_side      TEXT NOT NULL CHECK (normal_side IN ('debit','credit')),
    -- materialized balance, signed (debit-positive), maintained in the same tx as postings
    balance          BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT user_acct_has_user CHECK (
        (kind IN ('user_available','user_hold')) = (external_user_id IS NOT NULL)),
    -- liabilities may never flip to a debit balance => no overdraft, DB-enforced backstop
    CONSTRAINT no_overdraft CHECK (
        normal_side <> 'credit' OR balance <= 0),
    CONSTRAINT no_negative_asset CHECK (
        normal_side <> 'debit' OR balance >= 0)
);
CREATE UNIQUE INDEX ux_accounts_user  ON accounts (asset_id, kind, external_user_id)
    WHERE external_user_id IS NOT NULL;
CREATE UNIQUE INDEX ux_accounts_system ON accounts (asset_id, kind)
    WHERE external_user_id IS NULL;

-- ============ journal ============
CREATE TYPE entry_kind AS ENUM (
    'deposit_credit', 'withdrawal_hold', 'withdrawal_settle',
    'withdrawal_release', 'adjustment', 'reversal'
);

CREATE TABLE journal_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        entry_kind NOT NULL,
    asset_id    TEXT NOT NULL REFERENCES assets(id),
    -- business idempotency key for the ledger effect, e.g.
    -- 'btcpay_payment:{invoiceId}:{paymentId}', 'withdrawal_hold:{withdrawal_id}'
    source_ref  TEXT NOT NULL,
    reverses_entry_id BIGINT REFERENCES journal_entries(id),
    memo        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ux_entry_source UNIQUE (kind, source_ref)   -- << ledger-level idempotency
);

CREATE TABLE postings (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id    BIGINT NOT NULL REFERENCES journal_entries(id),
    account_id  BIGINT NOT NULL REFERENCES accounts(id),
    amount      BIGINT NOT NULL CHECK (amount <> 0),   -- signed: + debit / - credit
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_postings_account ON postings (account_id, id DESC);  -- history reads
CREATE INDEX ix_postings_entry   ON postings (entry_id);

-- sum-to-zero enforced at commit time (app also asserts it):
CREATE CONSTRAINT TRIGGER trg_entry_balanced
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
    -- fn: SELECT SUM(amount) FROM postings WHERE entry_id = NEW.entry_id; RAISE if <> 0

-- immutability: BEFORE UPDATE OR DELETE triggers on postings and journal_entries RAISE EXCEPTION.
```

```sql
-- ============ deposits ============
CREATE TYPE deposit_status AS ENUM (
    'created',            -- invoice issued
    'processing',         -- payment detected, awaiting confirmations (InvoiceProcessing)
    'settled',            -- InvoiceSettled
    'expired',            -- expired, nothing received
    'expired_partial',    -- expired with partial payment received
    'invalid'             -- marked invalid in BTCPay
);

CREATE TABLE deposits (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_user_id  TEXT NOT NULL,
    asset_id          TEXT NOT NULL REFERENCES assets(id),
    btcpay_invoice_id TEXT NOT NULL UNIQUE,
    amount_requested  BIGINT,                    -- NULL = open amount
    amount_credited   BIGINT NOT NULL DEFAULT 0, -- running sum of credited payments
    status            deposit_status NOT NULL DEFAULT 'created',
    address           TEXT,
    expires_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_deposits_user ON deposits (external_user_id, created_at DESC);

-- one row per on-chain payment BTCPay reports; the unit of crediting
CREATE TABLE deposit_payments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deposit_id        UUID NOT NULL REFERENCES deposits(id),
    btcpay_payment_id TEXT NOT NULL,             -- txid or BTCPay payment id
    amount            BIGINT NOT NULL CHECK (amount > 0),
    ledger_entry_id   BIGINT REFERENCES journal_entries(id),
    credited_at       TIMESTAMPTZ,
    CONSTRAINT ux_deposit_payment UNIQUE (deposit_id, btcpay_payment_id)
);

-- ============ withdrawals ============
CREATE TYPE withdrawal_status AS ENUM (
    'requested', 'pending_approval', 'approved', 'rejected',
    'submitting', 'submitted', 'confirmed', 'failed', 'refunded'
);

CREATE TABLE withdrawals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_user_id    TEXT NOT NULL,
    asset_id            TEXT NOT NULL REFERENCES assets(id),
    destination_address TEXT NOT NULL,
    amount_gross        BIGINT NOT NULL CHECK (amount_gross > 0),  -- debited from user
    fee_amount          BIGINT,                  -- actual network fee, known post-broadcast
    amount_net          BIGINT,                  -- gross - fee, what the user receives
    status              withdrawal_status NOT NULL DEFAULT 'requested',
    approval_mode       TEXT CHECK (approval_mode IN ('auto','manual')),
    approved_by         TEXT,                    -- 'auto' or admin identifier
    btcpay_payout_id    TEXT UNIQUE,             -- << at most one payout per withdrawal
    hold_entry_id       BIGINT REFERENCES journal_entries(id),
    settle_entry_id     BIGINT REFERENCES journal_entries(id),
    failure_reason      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_withdrawals_user    ON withdrawals (external_user_id, created_at DESC);
CREATE INDEX ix_withdrawals_pending ON withdrawals (status)
    WHERE status IN ('pending_approval','approved','submitting','submitted');

-- ============ idempotency & webhooks ============
CREATE TABLE webhook_events (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    btcpay_delivery_id TEXT NOT NULL UNIQUE,     -- BTCPay redelivers with same id
    event_type         TEXT NOT NULL,
    btcpay_invoice_id  TEXT,
    payload            JSONB NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    processing_error   TEXT
);

CREATE TABLE idempotency_keys (
    key             TEXT NOT NULL,
    endpoint        TEXT NOT NULL,               -- e.g. 'POST /v1/withdrawals'
    request_hash    TEXT NOT NULL,               -- sha256 of canonicalized body
    state           TEXT NOT NULL DEFAULT 'in_progress'
                        CHECK (state IN ('in_progress','completed')),
    response_status SMALLINT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, endpoint)
);
CREATE INDEX ix_idem_created ON idempotency_keys (created_at);  -- for TTL purge (72h)
```

---

## 3. Balances: materialized + derived verification

- **Materialized**: `accounts.balance` is updated inside the same transaction as the postings insert (`UPDATE accounts SET balance = balance + :delta WHERE id = :id` — the row is already locked, see §6). This gives O(1) balance reads and lets the `no_overdraft` CHECK act as a database-level last line of defense against double-spends.
- **Derived (verification)**: `SUM(postings.amount) GROUP BY account_id` must equal `accounts.balance`. Run as (a) a pytest invariant on every integration test, (b) a periodic reconciliation job in production. Any drift is a severity-1 bug — the job alerts, never "fixes".
- **Available balance** for user U in asset A = `-balance` of the `user_available` account (credit-normal). **Held** = `-balance` of `user_hold`. **Total** = available + held. No hold arithmetic at read time — holds already moved funds between the two accounts.

---

## 4. Deposit flow and state machine

Ledger crediting is done **per payment**, not per invoice. This makes partial, over-, and late payments the same code path and the same idempotency key.

```
                      +--> expired ----------------(payment settles late)--+
                      |                                                    |
created --(payment    +--> expired_partial <--(expiry w/ partial paid)     |
  detected: Invoice-  |                                                    v
  Processing)--> processing --(InvoiceSettled)--> settled          (payment credited
                      |                                             individually)
                      +--(InvoiceInvalid)--> invalid
```

Webhook handling (single DB transaction per event):

1. Verify `BTCPay-Sig` HMAC on the raw body.
2. `INSERT INTO webhook_events ... ON CONFLICT (btcpay_delivery_id) DO NOTHING`; if 0 rows inserted → already seen → 200 and stop. (Layer 1 idempotency.)
3. Dispatch by type:
   - `InvoiceProcessing` → `deposits.status = 'processing'`.
   - `InvoicePaymentSettled` (fires per payment once it has required confirmations) → **credit that payment**:
     - Fetch payment details via Greenfield (`GET .../invoices/{id}/payment-methods`) to get the exact received crypto amount; convert to integer units using `assets.decimals`. Never trust webhook-embedded amounts alone.
     - Insert `deposit_payments` row (`ux_deposit_payment` dedupes) and journal entry `kind='deposit_credit'`, `source_ref = 'btcpay_payment:{invoiceId}:{paymentId}'` (Layer 2 idempotency — `ux_entry_source` makes double-credit a constraint violation, not a bug).
     - Postings: `DR hot_wallet +X`, `CR user_available(U) −X`.
     - `deposits.amount_credited += X`.
   - `InvoiceSettled` → `status = 'settled'` (pure status; money already credited per payment).
   - `InvoiceExpired` → `status = 'expired'` or `'expired_partial'` (BTCPay's `partiallyPaid` flag). Config `CREDIT_LATE_AND_PARTIAL_PAYMENTS` (default **true** for a custodial ledger — the money physically arrived in the hot wallet, so refusing to credit it desyncs the ledger from custody): when true, `InvoicePaymentSettled` credits regardless of invoice status.
4. Mark `webhook_events.processed_at`; on exception, record `processing_error` and return 500 so BTCPay redelivers.

Edge cases:
- **Partial payment**: each settled payment credited as it confirms; invoice may end `expired_partial` but the user holds exactly what arrived.
- **Overpayment**: the extra payment is just another `InvoicePaymentSettled` → credited.
- **Late payment**: invoice already `expired`; payment-level crediting still fires; deposit remains `expired_partial`/`expired` for reporting but funds are credited.
- **Missed webhooks**: a poller (every N minutes) lists recent invoices via Greenfield and replays payment crediting through the same `source_ref` path — safe because idempotent at the ledger layer.

---

## 5. Withdrawal flow and state machine

Semantics: user requests gross `G`; `G` is debited from their balance; network fee `f` is deducted from `G`; user receives `G − f` on-chain (fee policy configurable per deployment: `fee_from_amount` default, `fee_from_platform` optional).

```
requested --(hold placed, same tx)--+--(G <= auto_limit)--> approved(auto) --+
                                    |                                        |
                                    +--(G >  auto_limit)--> pending_approval |
                                                              |    |         |
                                             (admin approves) |    | (admin rejects)
                                                              v    v         v
                                                        approved  rejected  submitting
                                                              \      |       |
                                                               \     v       v
                                                                \  refunded  submitted --(payout completed
                                                                 \  (hold      |          webhook/poll)--> confirmed
                                                                  \  released) +--(payout cancelled/failed)--> failed --> refunded
```

Steps and ledger entries:

1. **requested → hold** (one DB transaction):
   - Resolve/lock the user's `user_available` and `user_hold` account rows with `SELECT ... FOR UPDATE` (deterministic order: ascending `accounts.id`).
   - Check available `>= G` and `G >= withdrawal_min`. Insufficient → reject, no rows written beyond the idempotency record.
   - Entry `withdrawal_hold`, `source_ref='withdrawal_hold:{id}'`: `DR user_available +G`, `CR user_hold −G`.
   - **Auto-approval threshold check happens here**, inside this same transaction, against `assets.withdrawal_auto_limit` read in the same snapshot: `status = 'approved', approval_mode='auto'` if `G <= limit`, else `'pending_approval'`. Doing it inside the hold transaction means the decision and the reservation are atomic — an admin limit change mid-flight can't split them.
2. **approved → submitting → submitted**: a worker claims the row with a compare-and-swap: `UPDATE withdrawals SET status='submitting' WHERE id=:id AND status='approved'` (rowcount must be 1 — this is the gate that prevents two workers double-submitting). Then it calls Greenfield to create the payout, stores `btcpay_payout_id` (UNIQUE — at most one payout can ever attach to a withdrawal), sets `status='submitted'`. If the process crashes between the BTCPay call and the commit, a reconciliation job lists store payouts from BTCPay and matches them back by (destination, amount, time window) before any re-submission of rows stuck in `submitting` — stuck `submitting` rows are **never** auto-retried blindly.
3. **submitted → confirmed** (payout completed): record actual `fee_amount = f`, `amount_net = G − f`. Entry `withdrawal_settle`, `source_ref='withdrawal_settle:{id}'`, postings (fee-from-user policy):
   - `DR user_hold +G` (liability extinguished)
   - `CR hot_wallet −G` (custody decreases by net + fee = G)
   - `DR network_fee_expense +f` (fee incurred)
   - `CR fee_income −f` (fee recovered from the user)
   - Sum = 0. The expense/income pair nets to zero P&L — exactly the economics — while keeping the fee visible in the ledger and in the user's history. Under `fee_from_platform`: `DR user_hold +G`, `DR network_fee_expense +f`, `CR hot_wallet −(G+f)`.
4. **failed / rejected → refunded**: entry `withdrawal_release`, `source_ref='withdrawal_release:{id}'`: `DR user_hold +G`, `CR user_available −G`. Status → `refunded` (or `rejected` first, then `refunded` once released — release happens in the same transaction as the status change).

---

## 6. Concurrency and consistency

- **Isolation level: READ COMMITTED** (Postgres default) with explicit pessimistic row locks. Rationale over SERIALIZABLE: every money-moving path touches a small, known set of rows, so `FOR UPDATE` gives deterministic serialization with no retry loops; SERIALIZABLE would add serialization-failure retry machinery for no additional correctness on these access patterns. Correctness comes from three stacked mechanisms: row locks (serialization), CHECK `no_overdraft` (backstop), unique `ux_entry_source` (idempotency).
- **Where `SELECT ... FOR UPDATE` is required** (always on `accounts` rows, always ordered by ascending `accounts.id` to prevent deadlock):
  1. Withdrawal hold placement — lock `user_available` + `user_hold` for (user, asset).
  2. Withdrawal settle/release — lock the accounts being posted to.
  3. Deposit credit — lock `hot_wallet` + `user_available`. (Strictly, deposit credit can't overdraft, but locking keeps the materialized-balance update race-free and uniform.)
  4. Any manual adjustment entry.
- **Double-spend race (two withdrawals for the same user arrive concurrently)**: both transactions try to lock the same `user_available` row; the second blocks until the first commits, then re-reads `balance` (READ COMMITTED sees the committed decrement) and fails the `available >= G` check. If application code ever skips the lock, the `no_overdraft` CHECK aborts the transaction at the balance update — money cannot go negative even with buggy code.
- **Webhook double-credit**: (a) `webhook_events.btcpay_delivery_id` UNIQUE swallows redeliveries; (b) `journal_entries (kind, source_ref)` UNIQUE makes a duplicate credit — even from a *different* delivery id or from the poller racing the webhook — a caught `IntegrityError` treated as success (already credited).
- **One transaction per money movement.** A webhook event, a hold, a settle: each is exactly one DB transaction containing status change + entry + postings + balance updates. No cross-transaction partial states. External calls (BTCPay) are never made inside an open DB transaction; state machine CAS transitions bracket them (see §5 step 2).

---

## 7. Client idempotency: `Idempotency-Key` header

Required on all mutating endpoints (`POST /v1/deposits`, `POST /v1/withdrawals`, admin approve/reject). Flow:

1. `INSERT INTO idempotency_keys (key, endpoint, request_hash, state='in_progress') ON CONFLICT DO NOTHING`.
2. Insert won → execute the operation; on completion store `response_status/response_body`, `state='completed'` (same transaction as the business write, so a stored response implies the write committed).
3. Insert lost →
   - existing `request_hash` differs → `422` (key reuse with different body — client bug).
   - `state='completed'` → replay stored response verbatim.
   - `state='in_progress'` → `409 Conflict` with `Retry-After` (concurrent duplicate in flight).
4. Purge rows older than 72h. Keys are per-endpoint, opaque, max 255 chars.

---

## 8. SQLAlchemy 2.0: sync engine (psycopg 3)

Recommendation: **sync**, with FastAPI `def` endpoints (thread pool). Justification:

- Workload is a single tenant, backend-to-backend: request rates are tens/sec at most on a 4GB VPS; async concurrency buys nothing measurable.
- The money paths are lock-and-commit sequences where straight-line, blocking transaction scope is easier to reason about and audit; async sessions add greenlet indirection, session-per-task pitfalls, and subtle `await` points in the middle of critical sections — the exact places where correctness reviews happen.
- Sync psycopg 3 + SQLAlchemy 2.0 is the most battle-tested combination for Alembic and `FOR UPDATE` patterns. Outbound BTCPay calls (which do benefit from async) happen *outside* DB transactions and can use `httpx` in worker code regardless.

Small fixed pool (`pool_size=5, max_overflow=5`) — a 4GB VPS shares RAM with BTCPay/bitcoind; Postgres connections are the scarce resource.

---

## 9. Read API surface this schema supports

| Endpoint | Backing query |
|---|---|
| `GET /v1/users/{uid}/balances` | `accounts` where `external_user_id=:uid`; returns per asset `{available, held, total}` as strings, from materialized balances (sign-adjusted) |
| `GET /v1/users/{uid}/transactions?asset=&cursor=&limit=` | `postings JOIN journal_entries` on the user's two accounts, keyset-paginated on `(postings.id DESC)` via `ix_postings_account`; each row exposes entry kind, signed user-perspective amount, `source_ref`, timestamp |
| `GET /v1/deposits/{id}` / `GET /v1/users/{uid}/deposits` | `deposits` (+ `deposit_payments` detail: per-payment txid, amount, credited_at) |
| `GET /v1/withdrawals/{id}` / `GET /v1/users/{uid}/withdrawals` | `withdrawals` incl. status, gross/fee/net, `btcpay_payout_id` |
| `GET /v1/admin/withdrawals?status=pending_approval` | `ix_withdrawals_pending` partial index |
| `GET /v1/admin/reconciliation` | derived-vs-materialized check + ledger `hot_wallet` balance vs live BTCPay wallet balance |

---

## 10. Invariants (tests must enforce)

1. **Zero-sum**: for every `journal_entries` row, `SUM(postings.amount) = 0` (deferred constraint trigger + test assertion after every scenario).
2. **No zero postings**: `postings.amount <> 0` (CHECK).
3. **Materialized = derived**: `accounts.balance = COALESCE(SUM(postings.amount), 0)` for every account, after every test scenario and via the production reconciliation job.
4. **No overdraft**: every credit-normal account has `balance <= 0`; every debit-normal account `balance >= 0` (CHECK constraints; tests attempt to violate via racing withdrawals).
5. **Immutability**: any `UPDATE`/`DELETE` on `postings` or `journal_entries` raises (trigger); corrections exist only as `reversal` entries referencing `reverses_entry_id`.
6. **Single credit per payment**: at most one `journal_entries` row with `source_ref = 'btcpay_payment:{invoice}:{payment}'` — replaying any webhook N times changes nothing after the first.
7. **Single payout per withdrawal**: `withdrawals.btcpay_payout_id` unique, and no withdrawal ever leaves `submitting` into `submitted` twice (CAS transitions; test with two concurrent workers).
8. **Hold conservation**: for every withdrawal, hold entry exists iff status ∉ {`requested`,`rejected`-before-hold}; exactly one of {`withdrawal_settle`, `withdrawal_release`} exists iff status ∈ {`confirmed`, `refunded`}; a withdrawal can never have both.
9. **Custody identity**: ledger `hot_wallet + payouts_in_flight` balance equals `Σ user_available + Σ user_hold + fee accounts` net (automatic from zero-sum), and reconciles against the *actual* BTCPay wallet balance within a configured tolerance — the one invariant that catches real-world loss.
10. **State machine legality**: withdrawal and deposit status transitions only along the edges in §4/§5 (application-level guard + test matrix of illegal transitions).
11. **Idempotent API**: same `Idempotency-Key` + same body twice → identical response, one ledger effect; same key + different body → 422, zero ledger effect.

### Critical Files for Implementation
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\app\db\models.py (SQLAlchemy 2.0 declarative models for all tables above)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\alembic\versions\0001_initial_ledger.py (DDL incl. triggers, CHECKs, partial unique indexes)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\app\services\ledger.py (post_entry() — locking, zero-sum assert, balance update; the only module allowed to write postings)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\app\services\withdrawals.py (hold/approve/submit/settle state machine, CAS transitions)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\app\webhooks\btcpay.py (HMAC verification, delivery dedupe, per-payment crediting)