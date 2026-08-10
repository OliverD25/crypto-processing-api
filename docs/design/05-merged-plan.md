# crypto-processing-api — Merged Implementation Plan

Single-tenant custodial crypto payment & ledger service between a platform backend and a self-hosted BTCPay Server. Python 3.12+, FastAPI, Pydantic v2, PostgreSQL, Alembic. BTC (satoshis) + USDT-TRC20 (micro-units). MIT, public GitHub, one Hetzner 4GB VPS behind Cloudflare free tier.

Guiding rule inherited from all three designs: **the double-entry journal is the source of truth; webhooks are a latency optimization; reconciliation polling is the correctness mechanism; the hot wallet float is the real loss ceiling.**

---

## 1. Architecture overview

Components:

- **api** — FastAPI app (platform REST API, admin API, BTCPay webhook receiver, health). Sync SQLAlchemy sessions, `def` endpoints on the threadpool.
- **worker** — same Docker image, different command. Runs: reconciliation jobs (deposit sweep, withdrawal sweep, invariant check), payout submitter, outbound webhook delivery, TRX gas monitor. Each job wrapped in a Postgres advisory lock so an accidental second replica cannot double-run.
- **postgres** — dedicated Postgres 16 instance for the ledger (never shared with BTCPay's Postgres).
- **BTCPay Server stack** (btcpayserver-docker: BTCPay + NBXplorer + pruned bitcoind + its own Postgres) — same VPS, joined via an external Docker network. USDt plugin (`BTCPayServer.Plugins.USDt`) with TronGrid for USDT deposits.
- **Cloudflare** (proxied) in front of the public API domain; Greenfield and webhook traffic never leaves the box.

```
                    Internet
                       |
                 [Cloudflare proxy]           (443 open only to CF IP ranges)
                       |
   platform ──HTTPS──> nginx (btcpayserver-docker's) ── api.example.com ─┐
                                                                         v
 ┌──────────────────────────── Hetzner 4GB VPS ───────────────────────────────┐
 │  docker network: generated_default (external bridge)                       │
 │                                                                            │
 │  [api]  <──internal──  [btcpayserver] ── [nbxplorer] ── [bitcoind, pruned] │
 │    │  \__webhooks: btcpay -> http://crypto-api:8000/webhooks/btcpay        │
 │    │  \__Greenfield: api -> http://btcpayserver:49392                      │
 │    │                        [btcpay-postgres]                              │
 │  [worker] ──> TronGrid (USDT confirm / TRX gas)   ──> platform (outbound   │
 │    │                                                   signed webhooks)    │
 │  [postgres 16 — ledger]   (no published ports; internal network only)      │
 └────────────────────────────────────────────────────────────────────────────┘
```

Money paths at a glance:

- **Deposit**: platform `POST /v1/deposits` → api creates BTCPay top-up invoice (metadata carries `deposit_id` + `external_user_id`) → user pays → BTCPay webhook (fast path) or reconciliation poll (truth path) → per-payment ledger credit (`DR hot_wallet / CR user_available`).
- **Withdrawal**: platform `POST /v1/withdrawals` → hold placed atomically (`user_available → user_hold`), limits checked → auto-approve or admin queue → BTC: Greenfield payout via processor; USDT (MVP): manual operator send + txid entry → confirmation → settle entry releases hold and books the fee.

---

## 2. Final Postgres schema (consolidated)

All amounts are `BIGINT` in smallest units (satoshis / micro-USDT). Rationale: 4,000x headroom over BTC max supply, native indexing/arithmetic, loud overflow; `CHECK (decimals <= 8)` on `assets` guards against ever adding an 18-decimal asset without a deliberate migration. API serializes amounts as **JSON strings**.

```sql
-- ============ reference ============
CREATE TABLE assets (
    id                    TEXT PRIMARY KEY,           -- 'BTC', 'USDT_TRC20'
    display_name          TEXT NOT NULL,
    decimals              SMALLINT NOT NULL CHECK (decimals BETWEEN 0 AND 8),
    unit_name             TEXT NOT NULL,              -- 'sat', 'microUSDT'
    btcpay_payment_method TEXT NOT NULL,              -- discovered at startup, never hardcoded
    withdrawal_auto_limit BIGINT NOT NULL,            -- gross; above => manual approval
    withdrawal_daily_cap  BIGINT NOT NULL,            -- rolling 24h sum; above => all manual
    withdrawal_user_daily_cap BIGINT,                 -- NULL = disabled
    withdrawal_min        BIGINT NOT NULL DEFAULT 1,
    withdrawal_flat_fee   BIGINT NOT NULL DEFAULT 0,  -- USDT service fee (micro-units)
    enabled               BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============ chart of accounts (double-entry) ============
CREATE TYPE account_kind AS ENUM (
    'user_available','user_hold',        -- liabilities, credit-normal
    'hot_wallet','payouts_in_flight',    -- assets, debit-normal
    'fee_income','network_fee_expense',
    'external'                           -- counterparty for corrections
);

CREATE TABLE accounts (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id         TEXT NOT NULL REFERENCES assets(id),
    kind             account_kind NOT NULL,
    external_user_id TEXT,                            -- NULL for system accounts
    normal_side      TEXT NOT NULL CHECK (normal_side IN ('debit','credit')),
    balance          BIGINT NOT NULL DEFAULT 0,       -- materialized, signed debit-positive
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_acct_has_user CHECK (
        (kind IN ('user_available','user_hold')) = (external_user_id IS NOT NULL)),
    CONSTRAINT no_overdraft      CHECK (normal_side <> 'credit' OR balance <= 0),
    CONSTRAINT no_negative_asset CHECK (normal_side <> 'debit'  OR balance >= 0)
);
CREATE UNIQUE INDEX ux_accounts_user   ON accounts (asset_id, kind, external_user_id)
    WHERE external_user_id IS NOT NULL;
CREATE UNIQUE INDEX ux_accounts_system ON accounts (asset_id, kind)
    WHERE external_user_id IS NULL;

-- ============ journal (immutable, append-only) ============
CREATE TYPE entry_kind AS ENUM (
    'deposit_credit','withdrawal_hold','withdrawal_settle',
    'withdrawal_release','adjustment','reversal'
);

CREATE TABLE journal_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        entry_kind NOT NULL,
    asset_id    TEXT NOT NULL REFERENCES assets(id),
    source_ref  TEXT NOT NULL,     -- 'btcpay_payment:{invoiceId}:{paymentId}',
                                   -- 'withdrawal_hold:{id}', 'withdrawal_settle:{id}', ...
    reverses_entry_id BIGINT REFERENCES journal_entries(id),
    memo        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ux_entry_source UNIQUE (kind, source_ref)   -- ledger-level idempotency
);

CREATE TABLE postings (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id    BIGINT NOT NULL REFERENCES journal_entries(id),
    account_id  BIGINT NOT NULL REFERENCES accounts(id),
    amount      BIGINT NOT NULL CHECK (amount <> 0),  -- signed: + debit / - credit
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_postings_account ON postings (account_id, id DESC);
CREATE INDEX ix_postings_entry   ON postings (entry_id);

-- Deferred constraint trigger: per entry SUM(amount) = 0, checked at commit.
-- BEFORE UPDATE OR DELETE triggers on postings + journal_entries RAISE EXCEPTION
-- (immutability; corrections only via 'reversal' entries).
```

```sql
-- ============ deposits (intent lifecycle + per-payment crediting) ============
CREATE TYPE deposit_status AS ENUM (
    'creating',     -- row committed, BTCPay call not yet confirmed (ambiguous-timeout safety)
    'pending',      -- invoice issued
    'confirming',   -- payment detected, awaiting confirmations (InvoiceProcessing)
    'settled',      -- InvoiceSettled; all in-window payments credited
    'expired',      -- expired, nothing received
    'review',       -- late/partial/invalid/manually-marked — needs admin resolution
    'dismissed',    -- admin closed a review item without crediting
    'failed'        -- BTCPay invoice creation failed
);

CREATE TABLE deposits (
    id                UUID PRIMARY KEY,                -- UUIDv7, generated BEFORE BTCPay call
    external_user_id  TEXT NOT NULL,
    asset_id          TEXT NOT NULL REFERENCES assets(id),
    btcpay_invoice_id TEXT UNIQUE,                     -- NULL while 'creating'
    amount_expected   BIGINT,                          -- display/analytics only; invoices are top-up
    amount_credited   BIGINT NOT NULL DEFAULT 0,       -- running sum of credited payments
    status            deposit_status NOT NULL DEFAULT 'creating',
    address           TEXT,
    checkout_link     TEXT,
    expires_at        TIMESTAMPTZ,
    last_payment_seen_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_deposits_user   ON deposits (external_user_id, created_at DESC);
CREATE INDEX ix_deposits_active ON deposits (status)
    WHERE status IN ('creating','pending','confirming','review');

-- one row per on-chain payment BTCPay reports; the unit of crediting
CREATE TABLE deposit_payments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deposit_id        UUID NOT NULL REFERENCES deposits(id),
    btcpay_payment_id TEXT NOT NULL,                   -- BTCPay payment id / txid
    amount            BIGINT NOT NULL CHECK (amount > 0),
    after_expiration  BOOLEAN NOT NULL DEFAULT FALSE,
    ledger_entry_id   BIGINT REFERENCES journal_entries(id),  -- NULL until credited
    credited_at       TIMESTAMPTZ,
    resolved_by       TEXT,                            -- 'auto' or admin id (review credits)
    CONSTRAINT ux_deposit_payment UNIQUE (deposit_id, btcpay_payment_id)
);

-- ============ withdrawals ============
CREATE TYPE withdrawal_status AS ENUM (
    'requested','pending_approval','approved','rejected',
    'submitting','submitted','broadcast','confirmed','failed','refunded'
);

CREATE TABLE withdrawals (
    id                  UUID PRIMARY KEY,              -- UUIDv7
    external_user_id    TEXT NOT NULL,
    asset_id            TEXT NOT NULL REFERENCES assets(id),
    destination_address TEXT NOT NULL,
    amount_gross        BIGINT NOT NULL CHECK (amount_gross > 0), -- debited from user
    fee_amount          BIGINT,                        -- fixed at submission (estimate/flat fee)
    amount_net          BIGINT,                        -- gross - fee, sent on-chain
    status              withdrawal_status NOT NULL DEFAULT 'requested',
    approval_mode       TEXT CHECK (approval_mode IN ('auto','manual')),
    approved_by         TEXT,                          -- 'auto' or admin identifier
    backend             TEXT NOT NULL,                 -- 'btcpay_payout' | 'manual_tron'
    backend_ref         TEXT UNIQUE,                   -- BTCPay payoutId / 'manual:<uuid>'
    txid                TEXT,
    hold_entry_id       BIGINT REFERENCES journal_entries(id),
    settle_entry_id     BIGINT REFERENCES journal_entries(id),
    failure_reason      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_withdrawals_user    ON withdrawals (external_user_id, created_at DESC);
CREATE INDEX ix_withdrawals_pending ON withdrawals (status)
    WHERE status IN ('pending_approval','approved','submitting','submitted','broadcast');
-- velocity caps computed by summing this table over rolling windows (no drift-prone counters)

-- ============ webhook ingress (BTCPay -> us) ============
CREATE TABLE webhook_events (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dedup_key          TEXT NOT NULL UNIQUE,   -- originalDeliveryId if present else deliveryId
    delivery_id        TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    btcpay_invoice_id  TEXT,
    btcpay_payout_id   TEXT,
    payload            JSONB NOT NULL,         -- raw
    status             TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received','processed','failed','ignored','orphaned')),
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    attempts           SMALLINT NOT NULL DEFAULT 0,
    processing_error   TEXT
);
CREATE INDEX ix_webhook_pending ON webhook_events (received_at) WHERE status = 'received';

-- ============ client idempotency ============
CREATE TABLE idempotency_keys (
    key             TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    request_hash    TEXT NOT NULL,              -- sha256 of canonicalized body
    state           TEXT NOT NULL DEFAULT 'in_progress'
                        CHECK (state IN ('in_progress','completed')),
    response_status SMALLINT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, endpoint)
);
CREATE INDEX ix_idem_created ON idempotency_keys (created_at);  -- 72h TTL purge

-- ============ inbound auth ============
CREATE TABLE api_keys (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key_id       TEXT NOT NULL UNIQUE,   -- non-secret first-8-chars lookup handle
    key_hash     TEXT NOT NULL,          -- SHA-256 of full key (256-bit entropy => fast hash OK)
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('readwrite','admin')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ
);

-- ============ outbound webhooks (us -> platform) ============
CREATE TABLE outbound_events (
    id              UUID PRIMARY KEY,       -- 'evt_' UUIDv7, stable for platform dedup
    event_type      TEXT NOT NULL,          -- 'deposit.settled', 'withdrawal.completed', ...
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','delivered','dead')),
    attempts        SMALLINT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbound_due ON outbound_events (next_attempt_at) WHERE status = 'pending';
```

Balance semantics: **available** = `-balance` of `user_available`; **held** = `-balance` of `user_hold`. Holds are ledger entries between the two accounts — no side-table hold arithmetic. `accounts.balance` is materialized in the same locked transaction as postings; a derived `SUM(postings)` check verifies it in tests and a production reconciliation job (alerts, never auto-fixes).

Concurrency model: **READ COMMITTED + `SELECT ... FOR UPDATE`** on account rows (always ascending `accounts.id` order), CAS transitions on status columns (`UPDATE ... WHERE status = :expected`), CHECK constraints as DB-level backstop, `ux_entry_source` as ledger idempotency. External (BTCPay/TronGrid) calls are never made inside an open DB transaction.

---

## 3. Final REST API surface

Auth legend — **P**: `Authorization: Bearer cpk_live_...` with `readwrite` scope; **A**: bearer key with `admin` scope; **H**: BTCPay HMAC (`BTCPay-Sig`) only; **–**: none. All mutating P/A endpoints require an `Idempotency-Key` header (per-endpoint unique; same key + different body → 422; in-progress duplicate → 409 + Retry-After; completed → replayed response). All amounts in/out as strings of integer smallest-units.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/deposits` | P | Create deposit intent `{external_user_id, asset, expected_amount?}` → `{deposit_id, address, checkout_link, expires_at}` (top-up invoice) |
| GET | `/v1/deposits/{id}` | P | Deposit status + per-payment detail (txid, amount, credited_at) |
| GET | `/v1/users/{uid}/deposits` | P | List user deposits (keyset pagination) |
| POST | `/v1/withdrawals` | P | Request withdrawal `{external_user_id, asset, amount, destination_address}`; places hold atomically; returns status incl. `pending_approval` |
| GET | `/v1/withdrawals/{id}` | P | Withdrawal status, gross/fee/net, txid |
| GET | `/v1/users/{uid}/withdrawals` | P | List user withdrawals |
| GET | `/v1/users/{uid}/balances` | P | Per asset `{available, held, total}` from materialized balances |
| GET | `/v1/users/{uid}/transactions` | P | Ledger history (postings joined to entries), keyset-paginated, user-perspective signed amounts |
| GET | `/v1/assets` | P | Enabled assets, decimals, min/max/limits, fee mode (integration reference data) |
| GET | `/v1/admin/withdrawals?status=` | A | Approval queue (partial index backed) |
| POST | `/v1/admin/withdrawals/{id}/approve` | A | Approve pending withdrawal |
| POST | `/v1/admin/withdrawals/{id}/reject` | A | Reject → hold released (`refunded`) |
| POST | `/v1/admin/withdrawals/{id}/mark-broadcast` | A | Manual USDT backend: operator records `{txid}` after sending from TRON hot wallet |
| GET | `/v1/admin/deposits/review` | A | REVIEW queue: late/partial/invalid/manually-marked payments |
| POST | `/v1/admin/deposits/{id}/resolve` | A | `{action: "credit", payment_id, amount_units}` or `{action: "dismiss"}` — credits via the normal ledger path |
| GET | `/v1/admin/reconciliation` | A | Derived-vs-materialized ledger check + ledger hot_wallet vs live BTCPay/TronGrid wallet balances |
| GET | `/v1/admin/events?status=dead` | A | Dead-lettered outbound events |
| POST | `/v1/admin/events/{id}/redeliver` | A | Re-queue a dead outbound event |
| POST | `/webhooks/btcpay` | H | BTCPay webhook ingress (all subscribed event types, one endpoint) |
| GET | `/healthz` | – | 200 + DB connectivity + BTCPay reachability (uptime monitoring target) |

Outbound webhooks (API → platform), signed `X-CPA-Signature: t=<unix>,v1=<hex hmac-sha256("{t}.{body}")>` (Stripe scheme), with stable `evt_` IDs: `deposit.detected`, `deposit.settled`, `withdrawal.pending_approval`, `withdrawal.broadcast`, `withdrawal.completed`, `withdrawal.failed`. Documented explicitly as notifications, not source of truth — platform must reconcile via GET endpoints.

CLI (`python -m crypto_processing_api.cli`): `create-api-key --name --scope`, `revoke-api-key`, `migrate`, `bootstrap-btcpay` (idempotent store/wallet/webhook/key/payout-processor setup).

---

## 4. BTCPay integration design

### 4.1 Deposits

- **Invoice type: top-up** (no `amount`) for both assets — any payment is a full payment; no `paidPartial` limbo. `amount_expected` from the platform is metadata/display only. Per-invoice `checkout.expirationMinutes` from config (default 60 BTC, 30 USDT — shorter USDT to recycle the TRON address pool). `checkout.paymentMethods` restricted to the single requested asset; payment-method IDs discovered at startup from `GET /api/v1/stores/{storeId}/payment-methods` and cached (version-dependent strings, never hardcoded).
- **Metadata contract** on every invoice: `{cpapi: true, cpapi_version: 1, external_user_id, deposit_id, asset}` plus `additionalSearchTerms: ["cpapi:<deposit_id>"]`. Metadata echoes back in webhooks → attribution without BTCPay-side state; non-`cpapi` invoices on the store are logged and skipped.
- **Ambiguous creation**: the `deposits` row commits in status `creating` before the BTCPay call; on HTTP-timeout ambiguity, reconciliation matches by metadata/search term. **POSTs to BTCPay are never auto-retried** by the client (only GETs retry, 3x exponential backoff + jitter).
- **Crediting unit: per payment.** On each `InvoicePaymentSettled` (and on `InvoiceSettled` as a catch-all), the handler fetches truth from Greenfield (`GET .../invoices/{id}` + `.../payment-methods` — webhook payloads are triggers, never amount sources), converts decimal strings to integer units with `decimal.Decimal`, inserts a `deposit_payments` row and a `deposit_credit` journal entry with `source_ref='btcpay_payment:{invoiceId}:{paymentId}'` (`DR hot_wallet +X / CR user_available −X`). The `(kind, source_ref)` unique constraint makes any replay — duplicate webhook, poller racing webhook, redelivery under a new delivery id — a caught IntegrityError treated as success.
- **Auto-credit policy** (merged): payments settled **within the invoice window** auto-credit. Routed to the **REVIEW queue** instead of auto-credit: `afterExpiration=true` payments (late), `InvoiceExpired` with `partiallyPaid` needing residue inspection, `InvoiceInvalid`, and `InvoiceSettled` with `manuallyMarked=true`. Rationale: BTC late payments are attributable to the original invoice and the admin resolve path credits them in one click; USDT pool addresses are **reused across invoices/users**, so late USDT attribution requires a human checking the TRON txid timestamp against address reservation windows (documented runbook). Nothing is ever silently dropped — funds that reached custody either auto-credit or sit in REVIEW.
- **State machine**: `creating → pending → confirming → settled`; `pending/confirming → expired | review`; `review → settled-equivalent (credited) | dismissed`; `creating → failed`. All transitions guarded (`UPDATE ... WHERE status IN (allowed)` under `FOR UPDATE`), monotonic, order-safe.
- **USDT pool exhaustion** (D5): BTCPay invoice-creation failure when no free pool address maps to `503 DEPOSIT_TEMPORARILY_UNAVAILABLE`; ops doc: provision ≥ N addresses for N concurrent USDT deposits (recommend 20).

### 4.2 Withdrawals

Per-asset backend behind one protocol (`initiate / poll_status / cancel`), one shared state machine:

```
requested ──(hold placed atomically; limits checked in same tx)──┬── approved (auto)
                                                                 └── pending_approval ──admin──> approved | rejected
approved ──CAS──> submitting ──backend.initiate──> submitted ──(txid known)──> broadcast ──> confirmed
rejected / failed ──(release entry)──> refunded
```

- **Hold** (one transaction): lock `user_available` + `user_hold` rows `FOR UPDATE` (ascending id), check `available >= G`, `G >= withdrawal_min`, then **all three velocity gates in the same snapshot**: per-withdrawal auto-limit, per-asset rolling-24h daily cap (cap hit ⇒ force manual approval even for small amounts), optional per-user daily cap. Entry `withdrawal_hold` (`DR user_available +G / CR user_hold −G`). Decision and reservation are atomic.
- **Fee** (D4 — BTCPay has no fee-deduction field; miner fees are paid from the hot wallet *on top of* the payout amount): fee is **fixed at submission time**. BTC: `fee_amount = estimate(feeTargetBlock) × configured typical vsize (200 vB, conservative)` from BTCPay's fee endpoint if exposed, else mempool.space, else static `BTC_FALLBACK_FEE_SAT`. USDT: flat configurable service fee (`USDT_WITHDRAWAL_FEE_MICROS`, default 1 USDT) covering TRX gas. `amount_net = gross − fee`; reject if net ≤ dust (546 sat). `WITHDRAWAL_FEE_MODE = deduct (default) | absorb`. Estimate-vs-actual miner-fee drift stays in the hot wallet (safe direction for solvency) and is watched by reconciliation Job C.
- **Settle entry** on confirmation (`withdrawal_settle`, fee-from-user): `DR user_hold +G`, `CR hot_wallet −G`, `DR network_fee_expense +f`, `CR fee_income −f` (sums to zero; fee visible in user history). Release entry (`withdrawal_release`) on reject/fail: `DR user_hold +G / CR user_available −G`.
- **BTC backend**: `POST /api/v1/stores/{storeId}/payouts` with `approved: true` (approval policy lives in our service; BTCPay's AwaitingApproval stage is bypassed), amount = net as decimal string, metadata `{cpapi, withdrawal_id}` (convenience only — correlation is `backend_ref`). Worker claims rows with CAS (`WHERE status='approved'`, rowcount must be 1 — the anti-double-submit gate). Crash between BTCPay call and commit → row stuck in `submitting` is **never blindly retried**; reconciliation lists store payouts and matches by (destination, amount, window) first. On-chain payout processor configured once by bootstrap CLI (`intervalSeconds: 600, feeTargetBlock: 3, processNewPayoutsInstantly: true`) — **requires a hot wallet store**. Payout webhooks (`PayoutUpdated` InProgress → `broadcast`, Completed → `confirmed`) always followed by `GET .../payouts/{payoutId}` to extract txid from `paymentProof` (field shape pinned against the deployed swagger). Cancellation only attempted while `AwaitingPayment`. MVP "confirmed" = BTCPay payout `Completed`; no independent confirmation counting.
- **USDT backend (MVP): manual.** The USDt plugin has **no payout handler** — all USDT withdrawals go to `pending_approval` regardless of amount (`USDT_AUTO_WITHDRAW=false` default). Admin approves → `submitted` with `backend_ref='manual:<uuid>'` → operator sends from the TRON hot wallet with their own wallet software → `POST /v1/admin/withdrawals/{id}/mark-broadcast {txid}` → TronGrid poller verifies inclusion + success → `confirmed`. Phase 2: `tron_sender.py` (tronpy, key from env/secret store, serialized sends via advisory lock) drops into the same backend protocol.

### 4.3 Webhook receiver

1. Read **raw body bytes** before any parsing; verify `BTCPay-Sig` HMAC-SHA256 with `hmac.compare_digest` (dedicated webhook secret, distinct from the Greenfield key). Bad signature → 401, log source IP, never log the body.
2. Parse; drop-with-200 events for the wrong `storeId` or invoices without `metadata.cpapi`.
3. **Ack-then-process**: insert into `webhook_events` (`dedup_key = originalDeliveryId ?? deliveryId`, `ON CONFLICT DO NOTHING`), return 200 as soon as the row is durable. BTCPay's redelivery budget is tiny (~8 tries/~1 hour) — our own worker loop owns retries over `status='received'` rows. Only 401 (bad sig) and 400 (unparseable) are non-200.
4. Dispatch by type to deposit/withdrawal handlers. Out-of-order safety = guarded monotonic transitions + terminal effects re-fetch truth from Greenfield + ledger idempotency. Events referencing unknown `cpapi` invoices → `status='orphaned'` review list.

### 4.4 Reconciliation (the correctness mechanism, not phase 2)

- **Job A — deposit sweep** (120s): all non-terminal deposits (+ expired/review < 7 days for late payments) → `GET` invoice → feed through the **same transition function** the webhook handlers use (`apply_invoice_state`) — one code path, conflicts impossible by construction. Nightly full page-through of store invoices flags `cpapi` invoices with no local row.
- **Job B — withdrawal sweep** (60s): `submitted`/`broadcast` rows → `backend.poll_status()` → same shared transitions.
- **Job C — invariant check** (hourly): materialized-vs-derived balances; ledger hot_wallet vs live BTCPay wallet (`GET .../payment-methods/BTC-CHAIN/wallet`) and TronGrid account balance; alert on `wallet < Σ user balances` (insolvency signal, observational in MVP). Warn-metric every time the poller catches something webhooks missed.
- Jobs run in the worker process under Postgres advisory locks. No Celery/Redis.

### 4.5 Client module

Plain **sync `httpx.Client`** wrapper (no SDK — none official exists), Pydantic v2 transport models with `extra="ignore"`, monetary fields `str` in transport, converted via `Decimal` at the service layer. Error taxonomy: `Unavailable/RateLimited` (retryable), `AuthError/NotFound/Validation` (not), `ServerError` (capped retry). Timeouts: connect 5s, read 30s. A `BTCPayGateway` Protocol enables `FakeBTCPay` injection for tests (fake also synthesizes valid-HMAC webhook payloads). Version pinning: compose files pin an exact BTCPay image tag; `make check-btcpay-compat` asserts required swagger paths/fields in CI (webhook paths, payout metadata support, payout permission names, `paymentProof` shape are all version-dependent/unverified).

---

## 5. Security model

### Inbound auth (platform → API)
Plain bearer API keys, no HMAC request signing for MVP (backend-to-backend over TLS; mandatory Idempotency-Key neutralizes replay where it matters; scheme versioned in the key prefix so HMAC can be added later). Format `cpk_live_<32B base62>` / `cpk_test_` with non-secret 8-char key ID; generated only by CLI, printed once; stored as SHA-256 (256-bit entropy ⇒ fast hash is correct; constant-time compare anyway). Scopes: `readwrite` + `admin` only. Rotation via multiple active keys + revocation; no enforced expiry in MVP. Keys only in the `Authorization` header, never query params.

### Webhook ingress
HMAC over raw bytes (§4.3) + event dedup + **ledger-level dedup one layer deeper** (unique `source_ref` per payment — even two `InvoiceSettled` events with different delivery IDs credit once) + reconciliation against Greenfield truth. Network hardening: BTCPay posts to the API over the **internal Docker network** (`http://crypto-api:8000/...`) so the path need not be internet-reachable at all; signature verification kept regardless (split-host deployments exist).

### Outbound webhooks
Stripe-style `t=,v1=` HMAC with dedicated `PLATFORM_WEBHOOK_SECRET`; timestamp in signed string gives ±5 min replay window. Postgres-backed queue, `FOR UPDATE SKIP LOCKED` claims, backoff 1m/5m/30m/2h/6h then 12h up to 72h, dead-letter + operator alert + admin redeliver. Docs state loudly: notifications, not source of truth.

### Hot wallet risk controls (blast-radius bounding)
1. Per-withdrawal auto-approval threshold (per asset, ships conservative).
2. **Per-asset rolling-24h velocity cap** — the control that actually stops a many-small-withdrawals drain; cap hit ⇒ everything goes manual.
3. Optional per-user daily cap.
4. Procedural float policy: 1–3 days of payout volume hot; manual cold sweeps; **the float is the loss ceiling** — stated verbatim in README.
5. TRX gas monitor (15 min): alert below ~200 TRX and on TronGrid failures — the most likely embarrassing outage.
6. Alerting: `notify(severity, msg)` → Telegram bot and/or ntfy.sh (free). Alert list kept short: pending approval, cap hit, low TRX, dead-lettered event, poller-caught missed settlement, webhook signature failure spikes, 5xx spike, ledger invariant failure. Uptime via free pinger on `/healthz`.

### Secrets, logging, deploy hardening
- Env-only config (`pydantic-settings`), `.env` chmod 600, `.env.example` doubles as config reference; gitleaks in CI + pre-commit (public crypto repo = scanned by bots within minutes).
- Greenfield key minimally scoped to the one store (`cancreateinvoice`, `canviewinvoices`, `canmanagepullpayments`, settings scopes for bootstrap only — verify exact payout scope against deployed version); never server-admin.
- Logging: structlog with pipeline-level redaction processor (denylist `*key* *secret* *token* *password* *signature*`); addresses truncated `bc1qxy...k7f2`; key_id only, never full keys; no stack traces in responses; refuse to boot with `debug=True` in production.
- VPS: join btcpayserver-docker's nginx (custom include for `api.example.com`); external Docker network bridges the stacks; separate ledger Postgres; ufw default-deny, 443 from Cloudflare ranges only (refreshed by cron), 22 key-only, 8333 open; **Docker/ufw footgun documented** — publish nothing, `expose:`/localhost binds only. Cloudflare: orange-cloud API domain, Bypass-cache rule + `Cache-Control: no-store` middleware, `CF-Connecting-IP` trusted only from CF ranges. unattended-upgrades, fail2ban, 2GB swap (bitcoind IBD), **nightly `pg_dump` shipped off-box** (Hetzner Storage Box — the one budget exception).
- Threat model published in docs (webhook spoofing, key leak, SQLi, insider tampering, BTCPay compromise, TronGrid outage, withdrawal races, origin bypass, supply chain) with honest residual risks: "the MVP's real security budget is (a) how little sits in the hot wallet and (b) how fast the operator sees an alert."

---

## 6. Repository layout

```
crypto-processing-api/
├── src/crypto_processing_api/
│   ├── main.py                     # FastAPI app factory
│   ├── config.py                   # pydantic-settings, fail-fast validation
│   ├── cli.py                      # create-api-key, migrate, bootstrap-btcpay
│   ├── api/
│   │   ├── deposits.py  withdrawals.py  balances.py  transactions.py
│   │   ├── admin.py                # approval queue, review resolve, redeliver, reconciliation
│   │   ├── webhooks.py             # /webhooks/btcpay: raw-body HMAC, dedup insert, 200-fast
│   │   ├── health.py
│   │   └── middleware.py           # auth, idempotency, no-store, access logging
│   ├── core/
│   │   ├── auth.py                 # key gen/hash/verify, scopes
│   │   ├── signing.py              # inbound BTCPay HMAC + outbound platform HMAC
│   │   ├── idempotency.py          # Idempotency-Key insert/replay/conflict logic
│   │   └── redaction.py            # structlog processors
│   ├── ledger/                     # THE money code — no FastAPI/BTCPay imports
│   │   ├── models.py               # SQLAlchemy 2.0 declarative models (all tables §2)
│   │   ├── service.py              # post_entry(): locking, zero-sum assert, balance update
│   │   │                           #   (the ONLY module allowed to write postings)
│   │   └── invariants.py           # derived-vs-materialized checks, custody identity
│   ├── services/
│   │   ├── deposits.py             # intent creation, apply_invoice_state (shared webhook/poller path)
│   │   ├── withdrawals.py          # state machine, CAS transitions, limits/velocity gates
│   │   ├── backends.py             # WithdrawalBackend protocol, BtcpayPayoutBackend, ManualTronBackend
│   │   ├── fees.py                 # BTC estimate chain, USDT flat fee, dust check
│   │   └── events.py               # outbound event emission
│   ├── gateway/
│   │   ├── btcpay_client.py        # sync httpx Greenfield wrapper + error taxonomy + Protocol
│   │   ├── btcpay_models.py        # transport models (extra="ignore")
│   │   └── trongrid.py             # tx confirmation lookup, TRX balance (Phase 2: tron_sender.py)
│   ├── workers/
│   │   ├── runner.py               # advisory-locked job loop (worker container entrypoint)
│   │   ├── webhook_processor.py    # processes webhook_events rows, owns retries
│   │   ├── reconciliation.py       # jobs A (deposits), B (withdrawals), C (invariants)
│   │   ├── payout_submitter.py     # approved -> submitting -> submitted CAS pipeline
│   │   ├── outbound_delivery.py    # SKIP LOCKED claim, backoff, dead-letter
│   │   └── gas_monitor.py          # TRX balance / TronGrid health
│   └── alerts/notifier.py          # telegram / ntfy
├── migrations/                     # alembic (0001: full DDL incl. triggers, CHECKs, partial indexes)
├── tests/
│   ├── unit/                       # ledger, state machines, idempotency, HMAC, fees
│   └── integration/                # dockerized postgres; regtest e2e marked slow
├── deploy/
│   ├── docker-compose.yml          # api + worker + postgres; external network to btcpayserver-docker
│   ├── docker-compose.regtest.yml  # postgres-api, postgres-btcpay, bitcoind, nbxplorer, btcpayserver, api
│   ├── docker-compose.nile.override.yml   # USDt plugin -> Nile testnet (no TRON regtest exists)
│   └── nginx/  ufw/                # snippets referenced by docs
├── scripts/
│   ├── bootstrap_btcpay.py         # idempotent store/hot-wallet/webhook/key/processor setup
│   └── dev/  (smoke_test.py, mine.sh)
├── docs/                           # deployment.md, btcpay-setup.md, security.md (threat model),
│   │                               # api.md, integrating.md, runbook-usdt-withdrawals.md
├── .github/workflows/  (ci.yml, release.yml)
├── .env.example  .gitignore  .pre-commit-config.yaml
├── pyproject.toml  Dockerfile  Makefile  LICENSE (MIT)
└── README.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
```

Src layout (forces installed-package imports, current packaging guidance); `ledger/` deliberately dependency-free of FastAPI/BTCPay so its tests are pure and fast.

---

## 7. Milestones (build order)

**M1 — Scaffold, regtest stack, ledger core** (regtest stack first: every later milestone is only trustworthy with an e2e proof)
Repo scaffold (src layout, pyproject, Dockerfile, pre-commit, CI lint/type/test/gitleaks jobs). `docker-compose.regtest.yml` boots BTCPay+bitcoind+NBXplorer+both Postgres instances; bootstrap script creates store/hot wallet/webhook/scoped key idempotently. Alembic 0001 with the full §2 DDL including triggers/CHECKs. `ledger/service.post_entry()` with locking, zero-sum, materialized balances, immutability. Inbound auth (key hash/verify, CLI create/revoke) + Idempotency-Key middleware.
*Done when*: compose up succeeds on a clean machine; all §10-of-ledger-design invariants pass as pytest suites (including racing-withdrawal overdraft attempts and immutability violations); `create-api-key` works; CI green.

**M2 — BTC deposits**
Deposit intents (top-up invoices, metadata contract, `creating` ambiguity handling), webhook receiver (HMAC, dedup, ack-then-process), webhook processor worker, per-payment crediting, REVIEW queue + admin resolve, reconciliation Job A, orphan detection.
*Done when*: regtest cycle — create deposit → `sendtoaddress` → mine → balance credited exactly; replaying every webhook 5x changes nothing; killing the api container during payment and restarting credits via poller within one cycle; late-payment drill lands in REVIEW, admin resolve credits it.

**M3 — BTC withdrawals**
Hold placement with all velocity gates, auto/manual approval, admin approve/reject, payout submitter (CAS), BTCPay payout backend + payout processor bootstrap, fee estimation chain, settle/release entries, reconciliation Job B, stuck-`submitting` matching logic.
*Done when*: regtest cycle — request below limit → auto → broadcast → mined → confirmed, balance and fee exact, destination received net; above-limit goes to approval; two concurrent duplicate requests produce one payout; two concurrent workers never double-submit; reject/fail refunds the hold; daily-cap breach forces manual.

**M4 — USDT-TRC20**
USDt plugin setup docs + Nile override compose; USDT deposit intents through the same code path (payment-method id discovery); manual withdrawal backend (approve → mark-broadcast → TronGrid confirm); TRX gas monitor; pool-exhaustion 503 mapping; USDT late-payment runbook.
*Done when*: Nile-testnet deposit credits end-to-end; manual withdrawal walks the full state machine with TronGrid confirmation; gas alert fires below threshold; docs cover pool provisioning + attribution runbook.

**M5 — Hardening, outbound webhooks, ops, release**
Outbound platform webhooks (signing, queue, backoff, dead-letter, redeliver). Alert wiring (Telegram/ntfy). Reconciliation Job C + `/v1/admin/reconciliation`. Production deploy assets (compose, nginx include, ufw script, Cloudflare doc, backup cron), `check-btcpay-compat` CI job, threat model + all docs, README quickstart (≤10 commands), release.yml (multi-arch **amd64+arm64** GHCR images — CAX11 is ARM — with SBOM/provenance), SECURITY.md, CHANGELOG, v0.1.0 tag.
*Done when*: fresh Hetzner VPS deployed following only the docs completes a real mainnet BTC deposit+withdrawal with small amounts; nightly backup restores cleanly; all alerts demonstrably fire; release pipeline publishes images.

---

## 8. Verification plan

**Unit (fast, no containers)**
- Ledger invariants 1–11 (zero-sum, no-zero postings, materialized=derived, no-overdraft, immutability, single-credit-per-payment, single-payout-per-withdrawal, hold conservation, custody identity, state-machine legality matrix of illegal transitions, idempotent API semantics).
- HMAC verification vectors (inbound BTCPay incl. raw-bytes-vs-reserialized trap; outbound t/v1 scheme).
- Fee math (estimate chain fallbacks, dust rejection, deduct vs absorb), `Decimal` string→integer conversion edge cases.
- Idempotency-Key: replay, body-mismatch 422, concurrent 409.
- Webhook dispatch against `FakeBTCPay`-synthesized payloads: out-of-order events, redelivery, orphan routing, wrong-store/non-cpapi filtering.

**Integration (dockerized Postgres, FakeBTCPay)**
- Concurrency: two racing withdrawals (thread pool) → exactly one succeeds; two workers claiming the same approved row → one payout; poller racing webhook on the same payment → one credit.
- Full deposit/withdrawal state walks through the shared transition functions; REVIEW resolve path posts through the normal ledger code.
- Derived-vs-materialized assertion after every scenario (pytest fixture).

**Regtest e2e (nightly CI + local `smoke_test.py`)**
1. Bootstrap (idempotent): store, hot wallet, 1-conf speed policy, webhook, scoped key, payout processor at `intervalSeconds: 5`.
2. Mine 101 blocks; fund.
3. Deposit cycle: intent → pay 0.5 BTC → confirming on webhook → mine → CREDITED, balance = 50,000,000 sats.
4. Withdrawal cycle: 10,000,000 sats below limit → submitted → broadcast → mine → confirmed; balance, fee, and destination amount exact.
5. Failure drills: pay-after-expiry → REVIEW not auto-credit; webhook outage (stop api, pay, restart) → poller credits; duplicate webhook flood → single credit; crash between payout-create and commit → reconciliation matches, no double-send.

**CI gates**: ruff + format check, mypy --strict on `src/`, pytest with coverage floor ~85% on `ledger/`, gitleaks, docker build (PRs), `check-btcpay-compat` against the pinned tag, weekly pip-audit/Dependabot. Money-path PRs require tests + a ledger-invariant argument in the description (CONTRIBUTING).

---

## 9. Resolved conflicts

| # | Conflict | Positions | Resolution |
|---|---|---|---|
| 1 | **USDT withdrawals** | Brief + ledger design assume Greenfield payouts for both assets; integration fact sheet: USDt plugin has no payout handler at all | **Manual USDT withdrawals in MVP** (admin approval + operator send + txid entry + TronGrid confirmation), behind a `WithdrawalBackend` protocol so the Phase-2 tronpy sender is a drop-in. The fact sheet is verified reality; the brief's assumption is impossible. |
| 2 | **Deposit crediting unit & late/partial policy** | Ledger: credit per payment, `CREDIT_LATE_AND_PARTIAL_PAYMENTS=true` default (custody desync argument); integration: credit per invoice on `InvoiceSettled`, late/partial → REVIEW (USDT address-pool reuse makes late attribution unsafe) | **Merged**: per-payment crediting kept as the idempotency unit (`btcpay_payment:{invoice}:{payment}` — strictly stronger dedup), but **policy from the integration design**: only in-window settled payments auto-credit; `afterExpiration`, partial-at-expiry, invalid, and manually-marked go to REVIEW. The ledger's custody-desync concern is satisfied because REVIEW credits flow through the same ledger path — nothing is dropped, it's credited after a human check instead of automatically. |
| 3 | **Webhook processing model** | Ledger: process synchronously in the request, return 500 to trigger BTCPay redelivery; integration/security: ack-then-process, own retry loop | **Ack-then-process wins.** BTCPay's redelivery budget (~8 tries/~1 hour) is too small to be our retry mechanism; durable `webhook_events` insert + worker retries. Ledger idempotency makes both models safe, but this one can't lose events to a slow handler. |
| 4 | **Webhook dedup key** | Ledger: `btcpay_delivery_id`; integration: `originalDeliveryId ?? deliveryId` | Integration's key — redeliveries share `originalDeliveryId`, so retries dedupe naturally; plain `deliveryId` would treat each redelivery as new at the event layer. |
| 5 | **Credit dedup depth** | Security: credit keyed on invoice id; ledger: keyed per payment | Per payment (superset — handles multi-payment/overpay/late correctly; invoice-level would under-credit multi-payment invoices). |
| 6 | **Sync vs async** | Ledger: sync SQLAlchemy + psycopg3, `def` endpoints; integration: async httpx client + asyncio schedulers; security: "async handles the concurrency" | **Sync everywhere.** Money paths get straight-line auditable transactions; single-tenant traffic on a 4GB VPS gains nothing from async. BTCPay client becomes sync `httpx.Client`; integration's async method signatures become sync; background jobs are plain loops in a separate worker process. |
| 7 | **Scheduler placement** | Integration: in-process asyncio task in the API; security: separate `worker` container | Separate worker container (same image, different command). Cleaner failure isolation and restart semantics; advisory locks retained so any topology is double-run-safe. |
| 8 | **Repo layout** | Ledger + integration: `app/...`; security: `src/crypto_processing_api/...` | Src layout (packaging correctness in tests, current guidance). All module content from the other two designs mapped into it (§6). |
| 9 | **Deposit tables/states** | Ledger: `deposits` + `deposit_payments` with settled/expired_partial states; integration: `deposit_intents` with CREATING/REVIEW/DISMISSED | Merged single `deposits` table: keeps integration's `creating` (ambiguous-timeout safety), `review`/`dismissed` (policy #2), `failed`; keeps ledger's `deposit_payments` child table; `expired_partial` replaced by `review`. |
| 10 | **Withdrawal state machine** | Ledger: ...submitted → confirmed; integration adds BROADCAST (txid known) | Union: `submitted → broadcast → confirmed`. Broadcast is observable (payout InProgress/txid) and is the point where the platform can show users a txid. |
| 11 | **Fee timing** | Ledger: actual fee recorded at settle; integration D4: Greenfield can't deduct fees, so fee must be estimated up front and payout created for `gross − fee` | Fee **fixed at submission** (estimate for BTC, flat for USDT); settle entry books the charged fee. Actual miner fee is paid by the hot wallet on top of the payout; estimate-vs-actual drift stays in the hot wallet (solvency-safe direction) and is monitored by reconciliation Job C. |
| 12 | **Invoice type** | Ledger allowed `amount_requested` fixed invoices; integration mandates top-up | Top-up only; `amount_expected` is display metadata. Eliminates `paidPartial` limbo entirely. |
| 13 | **Webhook endpoint exposure** | Integration: public path + Cloudflare IP rule; security: internal Docker network only | Internal network primary (same-box single-tenant advantage), HMAC always; docs cover the split-host public variant with the CF rule. |
| 14 | **BTCPay wallet type** | Brief's implied watch-only xpub | Hot wallet required — the automated payout processor must sign. Risk posture: small float + documented cold sweeps (security §4/threat #5 carries the mitigation). |
| 15 | **Auto-approval decision point** | Security lists layered caps; ledger specifies threshold check inside the hold transaction | All gates (auto-limit, per-asset daily cap, per-user cap) evaluated **inside the hold transaction**, same snapshot — decision and reservation atomic, and velocity sums read from `withdrawals` (no drift-prone counters). |

Nothing from any design was dropped: every risk raised (finite webhook redelivery, USDT pool exhaustion D5, TronGrid rate limits, Docker/ufw footgun, IBD OOM, BTCPay version drift/UNVERIFIED fields, insider tampering residual, stuck-`submitting` payouts, orphaned events) appears in §4, §5, §7, or §10.

---

## 10. Open questions for the user

1. **USDT withdrawals — accept the manual MVP?** The BTCPay USDt plugin cannot send payouts at all. MVP = admin approves + operator sends from a TRON wallet + pastes txid. Is that operationally acceptable, and should Phase 2 (automated tronpy sender holding a TRON private key in the deployment's env/secret store — a real custody-risk increase) be scheduled immediately after M5 or deferred indefinitely?
2. **Late/partial payment policy.** Plan default routes all after-expiry/partial payments to an admin REVIEW queue (safe for USDT's shared address pool). Do you want a config option to auto-credit late **BTC** payments (attribution is unambiguous there), or is review-everything acceptable friction?
3. **Limit defaults.** Confirm shipping defaults for: per-withdrawal auto-approval limit (proposed 0.005 BTC / 200 USDT), per-asset 24h velocity cap, per-user daily cap on/off, `WITHDRAWAL_FEE_MODE=deduct`, USDT flat withdrawal fee (1 USDT), `USDT_AUTO_WITHDRAW=false`.
4. **Outbound platform webhooks in MVP?** The platform can poll `GET` endpoints; the signed-webhook subsystem (queue, backoff, dead-letter) is scheduled in M5. Keep it in MVP scope or cut to reduce surface?
5. **BTCPay version pin.** Which exact BTCPay Server image tag should the design be pinned/validated against? Several integration details are version-dependent and flagged UNVERIFIED (payout `metadata` support, payout permission scope name, `paymentProof` shape, webhook route shape) — they get resolved against that tag's swagger at implementation start.
6. **USDT address pool size.** Proposed 20 pre-provisioned TRON addresses (= max concurrent USDT deposit intents). Sufficient for your expected platform volume? Pool exhaustion surfaces as 503 on deposit creation.
7. **TronGrid API key.** Free tier requires registration (100K req/day). Confirm the deployment doc may make the key mandatory (unkeyed access throttling is unpredictable).
8. **Hot wallet float targets.** The threat model's loss ceiling is the float. What BTC/USDT amounts should the docs recommend keeping hot, and is a manual cold-sweep runbook (no automated sweeping in MVP) acceptable?
9. **Hetzner CAX11 (ARM) confirmed?** Release pipeline builds multi-arch either way, but docs and RAM budget (2GB swap, `shared_buffers=128MB`, single uvicorn worker) are tuned for the 4GB shared-with-BTCPay box — confirm the API and BTCPay stack really co-locate on one VPS.
10. **Backup destination.** Nightly off-box `pg_dump` is non-negotiable in the plan; Hetzner Storage Box (~€3/mo) slightly exceeds the $7 ceiling. Approve the overage or name a free object-storage alternative?

### Critical Files for Implementation
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\ledger\service.py (post_entry: locking, zero-sum, materialized balances — the only module that writes postings)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\migrations\versions\0001_initial.py (full DDL: triggers, CHECKs, partial unique indexes)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\withdrawals.py (holds, velocity gates, CAS state machine, backend protocol)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\api\webhooks.py (raw-body HMAC, dedup, ack-then-process ingress)
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\workers\reconciliation.py (deposit/withdrawal sweeps + invariant check — the correctness mechanism)