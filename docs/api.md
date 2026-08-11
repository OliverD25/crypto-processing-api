# API reference

Complete endpoint list. For how to *use* these together — the deposit
lifecycle, retry semantics, what to show a user — read
[`integrating.md`](integrating.md) first; this is the lookup table.

The live OpenAPI schema is at `/openapi.json` and Swagger UI at `/docs`. The
same document is committed at
[`reference/openapi.json`](reference/openapi.json) so it can be read, diffed
and generated from without a running server — CI regenerates it and fails on
any difference, so a route change cannot land with a stale spec. The eight
outbound webhook payloads have their own schema at
[`reference/webhook-events.json`](reference/webhook-events.json), behind the
same gate. What a release may change about any of it is in
[`reference/versioning.md`](reference/versioning.md).

## Conventions

**Auth.** `Authorization: Bearer cpk_live_…`. Two scopes: `readwrite` for
platform calls, `admin` for operator calls. An admin key also satisfies
`readwrite`.

**Amounts** are decimal strings, always: `"0.50000000"`, `"199.000000"`. Never
JSON numbers — 21 million BTC in satoshis exceeds JavaScript's safe integer
range. The exceptions are the two request fields `expected_amount` and
`amount`, which are integer strings of smallest units (`"50000000"`).

**Idempotency.** Every mutating endpoint requires an `Idempotency-Key` header.

**Caching.** Every response carries `Cache-Control: no-store`.

**Errors** are `{"detail": "..."}`, except pool exhaustion which uses a
structured `detail` object with a `code`.

| Status | Meaning |
|---|---|
| `400` | missing `Idempotency-Key`, or an unparseable webhook body |
| `401` | missing, malformed, revoked or expired key; or a bad webhook signature |
| `402` | insufficient available balance |
| `403` | valid key, wrong scope |
| `404` | unknown resource or asset |
| `409` | a duplicate request is in flight, or an illegal state transition |
| `422` | validation failure, or an idempotency key reused with a different body |
| `502` | BTCPay refused the request definitively |
| `503` | a dependency is unreachable, or the asset is unavailable — retryable |

---

## Platform endpoints — `readwrite`

### `POST /v1/deposits`

`{external_user_id, asset, expected_amount?}` → `201` with the deposit
including `address`, `checkout_link`, `expires_at`.

`expected_amount` is display-only for BTC. **For USDT it is load-bearing**: a
payment far from it goes to an operator instead of being credited, which is
what stops one user's late payment being credited to another.

`503` here can be `{"code": "DEPOSIT_TEMPORARILY_UNAVAILABLE"}` when the USDT
address pool is exhausted. Retryable, with `Retry-After`.

### `GET /v1/deposits/{deposit_id}`

The deposit plus a `payments` array, one entry per on-chain payment, each with
`amount`, `credited`, `credited_at`, `after_expiration`.

### `GET /v1/users/{external_user_id}/deposits`

`?limit=25&cursor=<deposit_id>`. Keyset paginated; `next_cursor` is `null` on
the last page.

### `GET /v1/deposits/{deposit_id}/address-history`

Every deposit that has held this deposit's address, with its reservation
window. Only meaningful for USDT; it is what the attribution runbook uses.

### `POST /v1/withdrawals`

`{external_user_id, asset, amount, destination_address}` → `201`.

The hold is placed before the response returns, so `pending_approval` is **not**
a rejection — the funds are reserved. `fee` and `amount_net` are `null` until
submission, when the fee is fixed.

`approval_reason` says which gate sent it to the queue.

### `GET /v1/withdrawals/{withdrawal_id}`

### `GET /v1/users/{external_user_id}/withdrawals`

`?limit=25&cursor=<withdrawal_id>`.

### `GET /v1/users/{external_user_id}/balances`

Per asset: `available`, `held`, `total`. Separate ledger accounts, not
arithmetic, so they cannot disagree.

### `GET /v1/users/{external_user_id}/transactions`

`?asset=BTC&limit=50&cursor=<posting_id>`. Ledger history from the user's
perspective: `kind`, `amount`, `direction` (`credit`/`debit`), `source_ref`.

### `GET /v1/assets`

Enabled assets with decimals, limits and fees. Read this rather than hardcoding
— an operator can change a limit without a deployment.

---

## Admin endpoints — `admin`

### Deposits

- `GET /v1/admin/deposits/review` — items needing a human
- `POST /v1/admin/deposits/{id}/resolve` — `{action: "credit", payment_id}` or
  `{action: "dismiss"}`. **No amount field**: the server asks BTCPay what the
  payment was worth
- `GET /v1/admin/wallet-alerts` — wallet receives matching no deposit payment

### Withdrawals

- `GET /v1/admin/withdrawals?status=pending_approval`
- `POST /v1/admin/withdrawals/{id}/approve` — only from `pending_approval`.
  For USDT this is also the handover: it returns the exact `amount_net` to send
- `POST /v1/admin/withdrawals/{id}/reject` — `{reason?}`, refunds immediately
- `POST /v1/admin/withdrawals/{id}/mark-broadcast` — `{txid}` for USDT. The
  server verifies contract, sender, recipient, amount, receipt and the Transfer
  event before accepting. `422` with the specific mismatch if not
- `POST /v1/admin/withdrawals/{id}/release` — `{attestation}`, minimum 10
  characters, recorded on the row. The only way to return a hold once a payout
  may exist

### Operations

- `GET /v1/admin/events?status=dead` — the outbound queue
- `POST /v1/admin/events/{id}/redeliver` — re-queue a dead event
- `GET /v1/admin/reconciliation` — the ledger consistency and custody report
  Job C runs hourly

---

## Unauthenticated

### `GET /healthz`

Process and database only. The uptime pinger's target. It deliberately does not
check BTCPay: a routine BTCPay restart must not page anyone or, worse, restart
a healthy API through a compose healthcheck.

### `GET /readyz`

Component detail: database, BTCPay, TronGrid (when configured), worker
heartbeat staleness. `503` when anything is degraded.

### `POST /webhooks/btcpay`

BTCPay's ingress. Authenticated by `BTCPay-Sig` HMAC over the raw body, never
by API key. Answers `200` for anything it will not act on, `401` for a bad
signature, `400` for an unparseable body — and nothing else, because BTCPay's
redelivery budget is too small to be a retry mechanism.

---

## Outbound webhooks

Sent to `PLATFORM_WEBHOOK_URL` when configured.

```
POST /your/endpoint
X-CPA-Signature: t=1760000000,v1=<hex hmac-sha256>

{"id":"evt_…","type":"deposit.settled","created_at":"…","data":{…}}
```

| Event | Fired when |
|---|---|
| `deposit.detected` | a payment is visible, not yet credited |
| `deposit.settled` | credited to the ledger |
| `deposit.review_required` | needs an operator |
| `deposit.expired` | the window closed with nothing received |
| `withdrawal.pending_approval` | queued for an operator |
| `withdrawal.broadcast` | on chain, `txid` known |
| `withdrawal.completed` | confirmed |
| `withdrawal.failed` | will not proceed — **the money is not back yet** |

Every payload shape is in [`reference/webhook-events.json`](reference/webhook-events.json)
as a JSON Schema discriminated on `type` — generate your parser from it rather
than hand-writing one, because that file is generated from the models the
server builds the payloads with and CI fails if the two drift apart.

Retries at 1m, 5m, 30m, 2h, 6h then every 12h, ten attempts over roughly three
days, then dead-lettered with an operator alert. Signature verification code is
in [`integrating.md`](integrating.md#verifying-the-signature).

**These are notifications, not truth.** Treat one as a hint to re-read the
resource.
