# crypto-processing-api — MVP Implementation Plan (v0.1.0)

## Context

Open-source, single-tenant, custodial crypto payment & ledger service. It sits between one platform backend and a self-hosted BTCPay Server. The platform calls it backend-to-backend to create deposits, request withdrawals, and read balances. This service owns the money ledger; BTCPay owns the blockchain. Target: one Hetzner 4GB VPS (CAX11 ARM) behind Cloudflare free tier, ~$7/month + ~free homelab backups.

The design was produced by a 6-agent workflow (research → 3 design lenses → synthesis → adversarial critique). The BTCPay facts below were verified against current docs and plugin source code (Aug 2026). All 15 critique findings are folded into this plan. Full design documents are in the workflow output file
`C:\Users\Admin\AppData\Local\Temp\claude\E--codespace--claude-code--swift-punk-projects-crypto-processing-api\348375ed-1947-4db3-a5d0-764e772a7f68\tasks\w9nm1npte.output`
(JSON: `result.researchFacts / ledgerDesign / securityDesign / integrationDesign / mergedPlan / critique`). **First implementation step: copy these into `docs/design/` before temp cleanup eats them.** If the file is gone, this plan is self-contained enough to execute.

## Confirmed decisions (user-approved)

1. Python 3.12+, FastAPI, Pydantic v2, **sync** SQLAlchemy 2.0 + psycopg3, Alembic. Sync everywhere — auditable straight-line money transactions; a 4GB single-tenant box gains nothing from async.
2. PostgreSQL 16, dedicated instance (never shared with BTCPay's).
3. Single-tenant. Users = opaque `external_user_id` strings from the platform. No end-user auth here.
4. Crypto-native integer ledger: BTC in satoshis, USDT-TRC20 in micro-units (6 dec). BIGINT.
5. Withdrawals: auto below per-asset limit, manual admin approval above. USDT: **manual-only in MVP** (see corrections), automated tronpy sender is Phase 2.
6. Docker regtest stack for local BTC e2e; USDT tested against TRON **Nile** testnet (no TRON regtest exists).
7. Inbound auth: bearer API keys (`cpk_live_…`), no request-HMAC in MVP (versioned prefix allows adding later).
8. Outbound signed webhooks to the platform: **in MVP** (M5).
9. Backups: continuous WAL archiving to the **homelab** (`rdeserverpc`) — free. See Ops.
10. Build scope: **full M1–M5 in one push** after plan approval. git init + MIT + incremental commits per unit.

## Verified corrections to the original brief

1. **USDT withdrawals cannot go through BTCPay at all.** The USDt plugin (github.com/btcpayserver-tether/BTCPayServer.Plugins.USDt) has no payout handler — receive-only, confirmed by repo tree inspection. Withdrawal module needs a per-asset backend abstraction: BTCPay payouts for BTC; manual TRON flow (Phase 2: tronpy) for USDT.
2. **No permanent per-user deposit addresses.** BTC: fresh address per invoice, invoices expire. USDT: plugin reserves addresses from a pre-provisioned **pool**, reused across invoices/users. Deposit UX = "request → fresh invoice valid N minutes".
3. **BTC payouts ARE fully automatable**: `POST /api/v1/stores/{storeId}/payouts` (auto-approved) + `OnChainAutomatedPayoutSenderFactory` processor. Requires a **hot wallet** store (not watch-only xpub as the brief implied).
4. **Webhook redelivery is finite** (~8 tries/~1 hour) → reconciliation polling is the correctness mechanism, webhooks are a latency optimization. This is the guiding rule of the whole design.
5. **Fee deduction from the user's amount is our job.** Greenfield payouts have no fee-deduction field; miner fees come out of the hot wallet on top. We compute `net = gross − fee` ourselves.
6. Top-up invoices (amount-less) treat any payment as full payment — eliminates `paidPartial` limbo. Invoice statuses/webhooks, `BTCPay-Sig` = `sha256=HMAC256(secret, rawBody)`, payout endpoints/states: all verified against swagger.
7. TronGrid free tier: register an API key (100K req/day, 15 QPS) — key is **mandatory** in deployment docs.

## Architecture

```
platform ──HTTPS──> Cloudflare ──> nginx (btcpayserver-docker's) ── api.example.com
 ┌────────────────────────── Hetzner 4GB VPS ──────────────────────────┐
 │ [api]      FastAPI: REST + admin + /webhooks/btcpay + health        │
 │ [worker]   same image: webhook processor, reconciliation A/B/C,     │
 │            payout submitter, outbound delivery, TRX gas monitor     │
 │ [postgres] ledger DB (internal network only)                        │
 │ [btcpayserver-docker] BTCPay + NBXplorer + pruned bitcoind +        │
 │            its own postgres; USDt plugin → TronGrid                 │
 │  BTCPay webhooks → http://crypto-api:8000/webhooks/btcpay (internal │
 │  docker network; HMAC verified regardless)                          │
 └─────────────────────────────────────────────────────────────────────┘
Backups: WAL archive → homelab rdeserverpc (SSH)
```

Worker jobs run under Postgres advisory locks (double-run-safe). No Celery/Redis.

## Ledger (the money core)

- **Double-entry journal is the source of truth.** `journal_entries` (kind, asset, `source_ref`, immutable) + `postings` (signed BIGINT, + debit / − credit). Deferred constraint trigger: per-entry sum = 0. BEFORE UPDATE/DELETE triggers raise (append-only; corrections only via `reversal` entries).
- **Idempotency at the ledger**: `UNIQUE (kind, source_ref)`. Deposit credits use `source_ref='btcpay_payment:{invoiceId}:{paymentId}'` — any replay (duplicate webhook, poller racing webhook) is a caught IntegrityError treated as success.
- `accounts`: per (asset, kind, external_user_id). Kinds: `user_available`, `user_hold` (credit-normal liabilities), `hot_wallet`, `payouts_in_flight` (debit-normal assets), `fee_income`, `network_fee_expense`, `external`, **`user_deficit`** (debit-normal receivable — critique #5: makes a post-credit reorg loss on an already-spent balance representable: `DR user_deficit / CR hot_wallet`). Materialized `balance` updated in the same locked tx; derived-vs-materialized checked by tests + hourly Job C.
- CHECK constraints `no_overdraft` / `no_negative_asset` — **`external` and `user_deficit` are exempt** (critique #5: corrections legitimately swing both signs).
- **`payouts_in_flight` is actually used** (critique #9): at submission `DR payouts_in_flight (net+est.fee) / CR hot_wallet`; at confirmation resolve it and book estimate-vs-actual drift to `network_fee_expense`. Job C's insolvency tolerance is then *derived*, not a hand-tuned epsilon.
- Concurrency: READ COMMITTED + `SELECT … FOR UPDATE` on account rows in ascending id order; CAS status transitions (`UPDATE … WHERE status = :expected`, rowcount must be 1). External calls never inside an open DB tx.
- **Per-asset velocity gate is serialized** (critique #1 — CRITICAL): before summing the rolling-24h window, take `FOR UPDATE` on the asset's `hot_wallet` account row (every credit already contends there). Without this, N concurrent withdrawals across N users all pass the cap. Concurrency test in M3 done-criteria.
- Amounts serialize as **JSON strings** in the API. `decimal.Decimal` for all string↔integer conversion.

Other tables: `assets` (limits, fees — DB is the single config source, seeded once from env; critique #15), `deposits` + `deposit_payments`, `withdrawals`, `webhook_events` (ingress dedup), `idempotency_keys`, `api_keys`, `outbound_events`. Full DDL is in the mergedPlan §2 (salvage to docs/design/); Alembic migration 0001 carries all of it including triggers, CHECKs, partial indexes, plus: **unique partial index on `withdrawals.txid`** (critique #12) and idempotency-row `resource_id` column (critique #10).

## Deposits

- **Top-up invoices** for both assets. Metadata contract: `{cpapi: true, cpapi_version: 1, external_user_id, deposit_id, asset}` + `additionalSearchTerms: ["cpapi:<deposit_id>"]`. Non-cpapi invoices on the store: logged, skipped.
- Row commits as `creating` **before** the BTCPay call (ambiguous-timeout safety); POSTs to BTCPay are never auto-retried; reconciliation matches strays by metadata.
- **Crediting unit = per payment** (handles multi-payment/overpay). Handler always re-fetches truth from Greenfield — webhook payloads are triggers, never amount sources.
- States: `creating → pending → confirming → settled`; `→ expired | review`; `review → credited | dismissed`; `creating → failed`. All transitions CAS-guarded; webhook handlers and poller share one `apply_invoice_state()` function — conflicts impossible by construction.
- **Auto-credit policy**: in-window settled payments auto-credit. To REVIEW queue: `afterExpiration` payments, partial-at-expiry, `InvoiceInvalid`, `manuallyMarked`. Nothing silently dropped.
- **USDT pool reuse mitigations** (critique #3 — HIGH): USDT expiry 60 min (NOT shortened to recycle the pool), pool ≥20 addresses documented; amount-vs-expected tolerance routing to REVIEW for USDT; log address reservation windows (deposit_id, address, from, to) so manual attribution is possible; runbook documents that USDT attribution is heuristic.
- **Address afterlife** (critique #4 — HIGH): Job A polls non-terminal deposits (120s) **plus settled deposits for N days post-settlement**; `MonitoringExpiration` set explicitly and the poll window aligned to it; **wallet-level detector**: enumerate on-chain wallet txs via Greenfield wallet API and flag any receiving txo not matched to a `deposit_payments` row → REVIEW (the only thing that catches payments to week-old addresses that BTCPay no longer attributes).
- **Confirmation policy** (critique #5 — HIGH): mainnet SpeedPolicy pinned ≥1 conf in bootstrap; configurable post-credit withdrawal delay ("credited N confs ago") as cheap reorg insurance; reorg runbook written in M5, not after the first incident.

## Withdrawals

```
requested ──(hold+all gates, one tx)──┬─ approved(auto) ─┐
                                      └─ pending_approval ─admin─ approved | rejected
approved ─CAS─ submitting ─backend─ submitted ─ broadcast ─ confirmed
rejected/failed ── refunded (release entry)
```

- Hold tx: lock `user_available`+`user_hold` (asc id) + asset `hot_wallet` row → check available ≥ gross, min, auto-limit, per-asset 24h cap (cap hit ⇒ force manual), optional per-user cap — decision and reservation atomic.
- **Release rules** (critique #2 — HIGH): automatic release only from `requested / pending_approval / approved / submitting-with-no-payout-found`. From any post-`submitted` state, release is **admin-only with explicit attestation** ("txid verified double-spent / never broadcast") — never an automatic mapping from backend status. Encoded in a transition legality matrix, tested.
- Fees fixed at submission: BTC `estimate(feeTargetBlock) × configurable vsize` (default raised above 200 vB; later priced from rolling average of actual payout vsizes — critique #8), fallback chain (BTCPay → mempool.space → static). USDT: flat fee from the `assets` row (default 1 USDT). `net = gross − fee`; reject net ≤ 546 sat dust. Mode `deduct` (default) | `absorb`.
- **BTC backend**: `POST /stores/{id}/payouts` with `approved: true`, metadata `{cpapi, withdrawal_id}`. **Payout metadata is the load-bearing correlation key** (critique #6 — HIGH): verified against the pinned BTCPay tag as a hard M3 blocker; if that version can't echo it → policy is "never auto-resolve ambiguous matches; freeze for admin". Resubmission of a stuck `submitting` row additionally requires zero unmatched payouts for that (dest, amount) window. Payout processor bootstrap: `intervalSeconds 600, feeTargetBlock 3, processNewPayoutsInstantly true`. `PayoutUpdated` webhooks always followed by GET to extract txid from `paymentProof`.
- **USDT manual backend**: all USDT → `pending_approval`; admin approves → operator sends from TRON hot wallet → `mark-broadcast {txid}` → TronGrid poller verifies **the full tuple** (critique #12): contract == mainnet USDT-TRC20, from == hot wallet, to == destination, amount == net, receipt SUCCESS **with Transfer event**. txid unique across withdrawals. Phase 2: `tron_sender.py` drops into the same `WithdrawalBackend` protocol.
- **Destination validation at request time** (critique #13): BTC bech32/base58 checksum + network prefix; TRON base58check `T…`; reject the store's own deposit addresses. 422, no hold placed.

## Webhooks & reconciliation

- Ingress: raw-body HMAC (`hmac.compare_digest`, dedicated secret), ack-then-process — durable insert into `webhook_events` (`dedup_key = originalDeliveryId ?? deliveryId`, `ON CONFLICT DO NOTHING`) then 200. Worker owns retries. Only 401/400 non-200. Unknown cpapi invoices → `orphaned` review list.
- Job A deposit sweep 120s (scope per critique #4 above); Job B withdrawal sweep 60s; Job C hourly invariants: derived-vs-materialized, ledger `hot_wallet` vs live BTCPay wallet + TronGrid balance, insolvency alert `wallet < Σ user balances` with derived tolerance; warn-metric when the poller catches what webhooks missed.
- Outbound platform webhooks: Stripe-style `t=,v1=` HMAC, **`outbound_events` inserted in the same tx as the ledger change** (critique #15), `FOR UPDATE SKIP LOCKED` claims, backoff 1m→72h, dead-letter + alert + admin redeliver. Documented as notifications, not truth.

## API surface

Auth: **P** = bearer readwrite; **A** = admin; **H** = BTCPay HMAC. Mutating P/A endpoints require `Idempotency-Key` (replay/409/422 semantics; **stale `in_progress` > 60s is reclaimable and rows store the created `resource_id`** — critique #10).

| Method/Path | Auth | Purpose |
|---|---|---|
| POST `/v1/deposits` | P | `{external_user_id, asset, expected_amount?}` → `{deposit_id, address, checkout_link, expires_at}` |
| GET `/v1/deposits/{id}`, `/v1/users/{uid}/deposits` | P | status + per-payment detail; list |
| POST `/v1/withdrawals` | P | validate address, place hold + gates atomically |
| GET `/v1/withdrawals/{id}`, `/v1/users/{uid}/withdrawals` | P | status, gross/fee/net, txid; list |
| GET `/v1/users/{uid}/balances`, `/v1/users/{uid}/transactions` | P | `{available, held, total}`; ledger history (keyset) |
| GET `/v1/assets` | P | enabled assets, decimals, limits, fees |
| GET/POST `/v1/admin/withdrawals…` `/approve` `/reject` `/mark-broadcast` | A | queue + actions |
| GET `/v1/admin/deposits/review`, POST `/v1/admin/deposits/{id}/resolve` | A | **resolve takes `{action, payment_id}` only — server fetches the amount from Greenfield; no operator-supplied amounts** (critique #11) |
| GET `/v1/admin/reconciliation`, `/v1/admin/events?status=dead`, POST `…/redeliver` | A | ops |
| POST `/webhooks/btcpay` | H | ingress |
| GET `/healthz` | – | **process + DB only** (critique #14); components on `/readyz` |

CLI: `create-api-key`, `revoke-api-key`, `migrate`, `bootstrap-btcpay` (idempotent store/hot-wallet/webhook/scoped-key/processor/SpeedPolicy setup).

## Security & ops

- Keys `cpk_live_<32B base62>`, SHA-256 at rest, printed once by CLI; scopes `readwrite`/`admin`. Greenfield key minimally scoped, never server-admin. Webhook secret ≠ Greenfield key.
- structlog + redaction processor (secrets denylist, truncated addresses); no full keys ever; refuse to boot with debug=True in prod.
- VPS: join btcpayserver-docker's nginx via custom include; external docker network; ufw default-deny (443 from Cloudflare ranges only, cron-refreshed; 22 key-only; 8333 open); publish nothing — `expose:` only (Docker/ufw footgun documented). Cloudflare: orange-cloud, bypass-cache rule + `Cache-Control: no-store`, trust `CF-Connecting-IP` only from CF ranges. unattended-upgrades, fail2ban, 2GB swap.
- **Backups (critique #7 — HIGH, M1 not M5)**: continuous WAL archiving with pgBackRest to **homelab `rdeserverpc`** over SSH (repo on the box's storage; RPO minutes, not 24h). Production note: the VPS must reach the homelab — set up Tailscale (free) or a router port-forward for SSH; document both. Nightly `pg_dump` kept as a second, independent format. Restore runbook enumerates which record types have external truth (BTCPay invoices/payouts) and which exist only here (manual USDT withdrawals, adjustments, holds). Homelab: check `references/manual.md` of the homelab-remote-control skill before touching the box.
- Alerts via Telegram/ntfy: pending approval, cap hit, low TRX (<200), dead-letter, poller-caught miss, sig-failure spikes, invariant failure. Uptime pinger on `/healthz`.
- Threat model doc with honest residuals: "the MVP's real security budget is (a) how little sits in the hot wallet and (b) how fast the operator sees an alert." Hot float = loss ceiling; 1–3 days of payout volume; manual cold sweeps runbook.

## Repository layout (src layout; `ledger/` imports no FastAPI/BTCPay)

```
src/crypto_processing_api/
  main.py config.py cli.py
  api/        deposits withdrawals balances transactions admin webhooks health middleware
  core/       auth signing idempotency redaction
  ledger/     models service (post_entry — the ONLY writer of postings) invariants
  services/   deposits withdrawals backends fees events
  gateway/    btcpay_client btcpay_models trongrid
  workers/    runner webhook_processor reconciliation payout_submitter outbound_delivery gas_monitor
  alerts/     notifier
migrations/  tests/{unit,integration}  deploy/ (compose prod + regtest + nile override, nginx, ufw)
scripts/ (bootstrap_btcpay.py, dev/smoke_test.py, dev/mine.sh)
docs/ (deployment, btcpay-setup, security, api, integrating, runbooks, design/ ← salvaged docs)
.github/workflows (ci, release)  pyproject.toml Dockerfile Makefile LICENSE(MIT) README …
```

## Milestones (full M1–M5 in one push; commit incrementally per unit)

**M0 — repo bootstrap**: `git init`, salvage design docs → `docs/design/`, LICENSE, initial commit.

**M1 — scaffold, regtest stack, ledger core** *(includes critique blockers #1, #5-schema, #7)*
Scaffold + CI (ruff, mypy --strict, pytest, gitleaks, docker build). `docker-compose.regtest.yml` (BTCPay + NBXplorer + bitcoind regtest + both postgres). Idempotent bootstrap script. Alembic 0001 full DDL. `post_entry()` with locking/zero-sum/materialization/immutability. Auth + idempotency middleware (incl. staleness takeover). pgBackRest config + backup docs.
*Done*: compose up clean; all ledger invariants pass as pytest (incl. racing-withdrawal overdraft attempts, immutability violations, serialized per-asset gate); CI green.

**M2 — BTC deposits** *(includes #4 fixes)*
Intents, webhook receiver, processor worker, per-payment crediting, REVIEW + admin resolve (server-fetched amounts), Job A incl. settled-deposit window + wallet-level txo detector, orphan handling.
*Done*: regtest deposit credits exactly; 5x webhook replay changes nothing; kill-api-during-payment credits via poller; late payment → REVIEW → one-click credit.

**M3 — BTC withdrawals** *(includes #1, #2, #6, #8, #9, #13 fixes; metadata-echo verification is a hard blocker at milestone start)*
Holds + gates, approval flows, payout submitter, BTCPay backend + processor bootstrap, fee chain, in-flight accounting, settle/release + legality matrix, Job B, stuck-`submitting` resolution.
*Done*: regtest below-limit auto cycle exact to the satoshi; above-limit → approval; N parallel withdrawals across distinct users never exceed the asset cap in aggregate; two workers never double-submit; reject refunds; post-broadcast release requires admin attestation.

**M4 — USDT-TRC20** *(includes #3, #12 fixes)*
Plugin setup docs + Nile override; USDT deposits through the shared path; manual withdrawal backend + full-tuple TronGrid verification; gas monitor; pool-exhaustion 503; attribution runbook.
*Done*: Nile deposit credits e2e; manual withdrawal walks the machine with on-chain verification; wrong-txid paste rejected; gas alert fires.

**M5 — hardening, outbound webhooks, ops, release** *(includes #14, #15 fixes)*
Outbound webhook subsystem; alert wiring; Job C + `/v1/admin/reconciliation`; `/readyz`; deploy assets (compose, nginx, ufw, Cloudflare doc, pgBackRest cron + restore drill, reorg runbook); `check-btcbay-compat` CI job pinned to the chosen BTCPay tag; threat model + all docs; README quickstart ≤10 commands; release.yml (multi-arch amd64+arm64 GHCR, SBOM); SECURITY.md; v0.1.0 tag.
*Done*: docs-only deploy on a fresh VPS completes a real small mainnet BTC deposit+withdrawal; backup restores cleanly; alerts demonstrably fire; images publish.

## Verification

- **Unit**: ledger invariants 1–11 (zero-sum, materialized=derived, no-overdraft, immutability, single-credit-per-payment, single-payout-per-withdrawal, hold conservation, custody identity, legality matrix), HMAC vectors (raw-bytes-vs-reserialized trap), fee math + Decimal edges, idempotency semantics, out-of-order/redelivered webhook dispatch vs `FakeBTCPay`.
- **Integration** (dockerized postgres + FakeBTCPay): racing withdrawals → exactly one; racing workers → one payout; poller-vs-webhook race → one credit; full state walks; derived-vs-materialized after every scenario.
- **Regtest e2e** (`smoke_test.py`, nightly CI): bootstrap → mine 101 → deposit 0.5 BTC → credited 50,000,000 sats → withdraw below limit → confirmed exact → failure drills (late pay → REVIEW; webhook outage → poller credits; duplicate flood → one credit; crash between payout-create and commit → no double-send).
- CI gates: ruff, mypy --strict, coverage ≥85% on `ledger/`, gitleaks, compat check, pip-audit weekly. Money-path PRs require a ledger-invariant argument (CONTRIBUTING).

## Defaults chosen (all env/DB-configurable; no further questions needed)

Auto-approval: 0.005 BTC / 200 USDT. Per-asset 24h cap: 0.05 BTC / 2,000 USDT. Per-user cap off. `WITHDRAWAL_FEE_MODE=deduct`. USDT flat fee 1 USDT. `USDT_AUTO_WITHDRAW=false`. USDT invoice expiry 60 min, BTC 60 min. Pool ≥20 addresses. TronGrid API key mandatory. BTCPay image tag pinned at M1 start (latest stable; compat-checked in CI).

## Phase 2 (explicitly out of MVP)

Automated USDT sender (tronpy, TRON key custody on-box), HMAC request signing for inbound auth, additional assets (Lightning, LTC, XMR), automated cold sweeps, per-amount confirmation tiers, multi-tenant mode.

## Process

Implementation runs milestone-by-milestone, delegated to `coder` agents with self-contained prompts (they cannot see this conversation); same agent continued for follow-ups within a milestone. Commit after each discrete unit. Nothing destructive without asking.
