# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-10

First release. A complete custodial deposit and withdrawal path for BTC and
USDT-TRC20 on top of BTCPay Server.

### Added

**Ledger**
- Append-only double-entry journal in PostgreSQL. Integer amounts in smallest
  units; no floats anywhere in the money path.
- `post_entry` as the only writer of postings: locks accounts in ascending id
  order, asserts zero-sum before flush, updates materialized balances in the
  same transaction.
- Database-enforced invariants — a deferred trigger for per-entry zero-sum,
  BEFORE UPDATE/DELETE triggers making history immutable, CHECK constraints
  preventing overdraft, and a unique `(kind, source_ref)` making a replayed
  effect impossible rather than merely unlikely.
- `user_deficit`, so a reorg loss on an already-spent balance can be booked at
  all.

**Deposits**
- BTCPay top-up invoices with a metadata contract for attribution; the row
  commits before the API call so an ambiguous timeout is recoverable.
- One shared transition function for the webhook and the poller, so the two
  cannot disagree.
- Per-payment crediting, with everything ambiguous routed to a review queue
  rather than credited or dropped.
- Reconciliation sweeps including settled deposits inside their monitoring
  window, plus a wallet-level scan for receives matching no deposit — the only
  detector for a payment to an address BTCPay has stopped watching.

**Withdrawals**
- Balance check, limit decision and hold in one transaction, serialized on the
  asset's hot-wallet row so the rolling 24-hour velocity cap survives
  concurrency.
- Fees fixed at submission with a live estimate, a mempool.space fallback and a
  static floor; dust refused before any hold is placed.
- BTCPay payouts with `metadata.withdrawal_id` as the correlation key, verified
  live against BTCPay 2.4.2.
- In-flight accounting through `payouts_in_flight`, making the insolvency
  tolerance derived rather than tuned.
- Release after submission requires an admin attestation recorded on the row.
- Bitcoin address validation (BIP 173, BIP 350, base58check) and TRON
  base58check, implemented rather than imported.

**USDT-TRC20**
- Operator-sent withdrawals with full-tuple on-chain verification: contract,
  sender, recipient, exact amount, receipt result and the Transfer event.
- Address reservation windows, an amount-tolerance policy and pool-exhaustion
  handling, because the plugin reuses addresses across users.
- TRX gas monitoring.

**Platform interface**
- API keys with scopes, `Idempotency-Key` on every mutating endpoint including
  a staleness takeover, and deposit, withdrawal, balance, transaction and asset
  reads.
- Signed outbound webhooks with retry, dead-letter and admin redelivery.

**Operations**
- `/healthz` for process and database; `/readyz` for components and worker
  heartbeats.
- Hourly invariant and custody check, exposed on demand at
  `/v1/admin/reconciliation`.
- Alerts to ntfy or Telegram with stable codes.
- Deployment assets, backup documentation with a restore drill, a threat model,
  and runbooks for USDT withdrawals, USDT attribution and reorgs.
- Multi-architecture images (amd64 and arm64) with SBOM and provenance.
- A compatibility check asserting the BTCPay endpoints and fields this service
  depends on still exist in the pinned tag.

### Known limitations

- USDT withdrawals are manual. The BTCPay USDt plugin registers no payout
  handler of any kind, so a signer is Phase 2.
- USDT deposit attribution is heuristic, because the plugin reuses pool
  addresses. See `docs/runbook-usdt-attribution.md`.
- Inbound authentication is a bearer key; HMAC request signing is deferred, and
  the key prefix is versioned so it can be added without breaking clients.
- Single-tenant. One platform, one store.
- No external audit.

[0.1.0]: https://github.com/OliverD25/crypto-processing-api/releases/tag/v0.1.0
