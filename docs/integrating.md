# Integrating with crypto-processing-api

For developers of the platform backend that calls this service. It assumes
nothing about your stack beyond HTTP and JSON.

> **Status: v0.1.0.** Deposits, withdrawals and outbound webhooks work for BTC
> and USDT-TRC20. Every endpoint is listed in [`api.md`](api.md).

## The one rule

**This service is the source of truth for balances. Your database is not.**

Do not mirror balances and reconcile later. Ask this API. Every number it
returns comes from a double-entry journal that cannot be edited, only appended
to, and that refuses to let an account go negative even if the application
above it has a bug.

## Authentication

```http
Authorization: Bearer cpk_live_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

Keys are minted by the operator with the CLI and shown once. Two scopes exist:
`readwrite` for everything your backend does, and `admin` for the review queue.
Your backend should hold a `readwrite` key and nothing more.

Keys go in the header, never in a query string. If a key leaks, the operator
revokes it and mints another; several keys can be active at once, so rotation
needs no downtime.

## Amounts are strings of integer smallest units

```json
{ "amount_credited": "0.50000000" }
```

Amounts in and out are **decimal strings**. Internally everything is an integer
count of the asset's smallest unit — satoshis for BTC, micro-USDT for
USDT-TRC20 — and no float ever touches the ledger.

Parse them with a decimal type, not a JSON number. `0.1 + 0.2` is not `0.3` in
IEEE 754, and 21 million BTC in satoshis is larger than JavaScript's safe
integer range.

`expected_amount` on deposit creation is the exception: it is an integer string
of smallest units (`"50000000"`), because it is a hint, not a balance.

## Idempotency

Every mutating request needs an `Idempotency-Key` header. Use a UUID per
logical operation — not per retry.

```http
POST /v1/deposits
Idempotency-Key: 9f1c2e5a-...
```

| What happens | Response |
|---|---|
| First use | the operation runs |
| Same key, same body | the stored response is replayed, one effect |
| Same key, different body | `422` — that is a bug in your retry logic |
| Duplicate still in flight | `409` with `Retry-After` |
| Duplicate, previous attempt died over 60s ago | the retry takes over and completes it |

The last row matters. If we crash between creating your deposit and getting an
address from BTCPay, retrying the same key later picks the same deposit back up
rather than creating a second one. **So retry with the same key. Do not
generate a new one.**

The request hash is over the exact bytes you send. Serialize the body the same
way on a retry, or it looks like a different request.

## Creating a deposit

```http
POST /v1/deposits
Authorization: Bearer cpk_live_...
Idempotency-Key: <uuid>
Content-Type: application/json

{ "external_user_id": "user-42", "asset": "BTC", "expected_amount": "50000000" }
```

```json
{
  "deposit_id": "019feb96-7e52-771a-a8cb-a86dccc87339",
  "external_user_id": "user-42",
  "asset": "BTC",
  "status": "pending",
  "address": "bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq",
  "checkout_link": "https://btcpay.example.com/i/JRr...",
  "expires_at": "2026-08-10T13:15:13+00:00",
  "amount_expected": "0.50000000",
  "amount_credited": "0.00000000",
  "created_at": "2026-08-10T12:15:13+00:00",
  "payments": []
}
```

`external_user_id` is opaque to us. Use whatever your system calls a user; we
never interpret it and there is no end-user authentication here.

Show the user `address`, or embed `checkout_link` if you want BTCPay's checkout
page with its QR code and payment tracking.

### Two things about addresses

**A deposit address is single use.** Every deposit request gets a fresh
address. There is no permanent per-user address — BTCPay does not reuse
addresses, and for USDT the addresses come from a shared pool. Tell your users
this, and do not cache an address for reuse.

**They keep working after they expire.** `expires_at` is when we stop treating
a payment as ordinary, not when the address stops receiving coins. Money sent
to an old address still arrives, and it lands in the operator's review queue
rather than being credited automatically. Nothing is lost; it just needs a
human, so it is slower.

### Errors

| Status | Meaning |
|---|---|
| `400` | missing `Idempotency-Key` |
| `401` | missing, malformed, revoked or expired key |
| `404` | unknown asset |
| `422` | invalid body, or a key reused with a different body |
| `502` | BTCPay rejected the request — the deposit is dead, start a new one |
| `503` | asset unavailable, or BTCPay unreachable — **retry with the same key** |

`503` on creation is the one that needs care. The invoice may or may not exist.
Retry the same `Idempotency-Key` after `Retry-After` and you will get the real
deposit, whichever way it went.

## Reading a deposit

```http
GET /v1/deposits/{deposit_id}
GET /v1/users/{external_user_id}/deposits?limit=25&cursor=<deposit_id>
```

The list is keyset-paginated: pass the `next_cursor` from the previous page.
Pages cannot shift under you the way an offset can. `next_cursor` is `null` on
the last page.

### Statuses

| Status | Meaning | Terminal |
|---|---|---|
| `creating` | we are asking BTCPay for an invoice | no |
| `pending` | address issued, nothing seen yet | no |
| `confirming` | a payment is visible, not yet confirmed | no |
| `settled` | confirmed and credited | effectively |
| `expired` | the window closed with nothing received | effectively |
| `review` | money arrived that needs a human | no |
| `dismissed` | an operator closed a review item without crediting | yes |
| `failed` | BTCPay refused to create the invoice | yes |

`settled` and `expired` are not permanently final: a later payment to the same
address can move a deposit into `review`. Read status when you display it;
never cache it as final.

**`review` is not an error.** It means the money is in custody but attribution
or timing needs an operator's eye — a payment after expiry, an invoice a human
marked settled in BTCPay, or an amount we refuse to round. It becomes `settled`
when the operator credits it. Show the user "processing, being verified", not a
failure.

### Per-payment detail

```json
"payments": [
  {
    "payment_id": "0df8748c...-0",
    "amount": "0.50000000",
    "credited": true,
    "credited_at": "2026-08-10T12:15:31+00:00",
    "after_expiration": false,
    "resolved_by": "auto"
  }
]
```

One entry per on-chain payment BTCPay reports. An invoice can have several: a
user who pays twice, or tops up an amount. Each is credited independently and
exactly once. `amount_credited` on the deposit is their sum.

`credited: false` means the money is visible but not yet in the balance —
either awaiting confirmations or waiting in the review queue.

## Balances and history

```http
GET /v1/users/{external_user_id}/balances
GET /v1/users/{external_user_id}/transactions?asset=BTC&limit=50&cursor=<posting_id>
GET /v1/assets
```

```json
{
  "balances": [
    {"asset": "BTC", "available": "0.49600000", "held": "0.00400000",
     "total": "0.50000000"}
  ]
}
```

`available` is what a withdrawal can use. `held` is reserved by a withdrawal in
flight. They are separate accounts in the ledger, not a subtraction, so they
cannot disagree.

`GET /v1/assets` is the reference data to build your UI from — decimals, limits
and fees per asset, and whether the asset is currently enabled. Read it rather
than hardcoding, because an operator can change a limit without a deployment.

## USDT-TRC20

Same endpoints, same semantics, three differences that matter.

**Every USDT withdrawal waits for an operator.** Not because of the amount —
the BTCPay plugin this service uses cannot send USDT at all, so a human does
it. Expect `pending_approval` on every request and a slower turnaround than BTC.
`fee` is a flat service fee covering TRX gas rather than a network estimate.

**Deposit addresses are shared, not just single-use.** They come from a pool and
are reused across users over time. Two consequences for you:

- a deposit request can answer `503` with
  `{"code": "DEPOSIT_TEMPORARILY_UNAVAILABLE"}` when every address in the pool
  is busy. It is retryable and carries `Retry-After`.
- **send `expected_amount` when you know it.** For USDT it is not just display:
  a payment far from the expected amount is routed to an operator instead of
  being credited automatically, which is what stops one user's late payment
  being credited to another. Without it there is nothing to compare against.

**Destination addresses are TRON base58check (`T...`).** An Ethereum-style
`0x...` address is refused with `422` — USDT exists on Ethereum and Polygon too,
and that mistake cannot be undone.

## Truth, and why polling is part of it

Our reconciliation loop asks BTCPay what happened every two minutes and credits
anything the webhook path missed. Webhooks only make it faster. BTCPay gives up
redelivering after roughly eight attempts in an hour, so if this service were
down longer than that, webhooks alone would lose deposits.

The same reasoning applies one level up, to you:

- **Poll `GET /v1/deposits/{id}` while a deposit is not terminal.** Every 10–30
  seconds is plenty.
- **Outbound webhooks are notifications, not truth.** Treat one as a hint to
  re-read the resource, never as the fact itself. See below.
- A credit is final once `credited: true`. It is never quietly reversed. A
  correction, if one is ever needed, is a new journal entry with its own
  record.

## Withdrawals

```http
POST /v1/withdrawals
Authorization: Bearer cpk_live_...
Idempotency-Key: <uuid>
Content-Type: application/json

{
  "external_user_id": "user-42",
  "asset": "BTC",
  "amount": "400000",
  "destination_address": "bc1q..."
}
```

```json
{
  "withdrawal_id": "019febcb-37ed-7388-a6a2-03b6a9a3d010",
  "status": "approved",
  "approval_mode": "auto",
  "destination_address": "bc1q...",
  "amount_gross": "0.00400000",
  "fee": null,
  "amount_net": null,
  "txid": null,
  "created_at": "2026-08-10T13:11:02+00:00"
}
```

`amount` is the **gross** amount in integer smallest units: what leaves the
user's balance. The fee comes out of it, so the destination receives less.

### The hold is placed before you get an answer

By the time this endpoint returns, the money has already moved from the user's
available balance to a hold. That is deliberate — the balance check, the limit
decision and the reservation happen in one database transaction, so two
simultaneous requests cannot both pass a check that only one of them can
afford.

Which means: **a `201` with `status: "pending_approval"` is not a rejection.**
The funds are reserved and an operator has to look at it. Only a 4xx means
nothing was reserved.

### Statuses

| Status | Meaning | Money |
|---|---|---|
| `pending_approval` | waiting for an operator | held |
| `approved` | cleared, waiting for the submitter | held |
| `submitting` | being handed to BTCPay right now | held |
| `submitted` | BTCPay accepted the payout | held |
| `broadcast` | on chain, `txid` is set | held |
| `confirmed` | done | debited |
| `rejected` / `failed` | will not proceed | still held until released |
| `refunded` | the hold has gone back to available | returned |

`confirmed` and `refunded` are terminal. Everything else moves on its own.

Note the gap between `failed` and `refunded`. A withdrawal that failed after
submission does **not** return the money automatically, because a payout that
looks failed can still confirm — refunding it first would pay the user twice.
An operator releases it explicitly, with a written attestation that they
checked the chain. Show the user "under review", not "refunded", until the
status actually says `refunded`.

### Fees

The fee is **fixed when the payout is created**, not when you ask. Until then
`fee` and `amount_net` are `null`, and after that they do not change.

- `deduct` (the default) — the user pays: `amount_net = amount_gross - fee`
- `absorb` — the user receives the whole amount and the operator pays the miner

For BTC the fee is a live fee-rate estimate multiplied by an assumed
transaction size. It is an estimate of what the operator's wallet will pay, so
it will not match the miner fee on the transaction exactly. That difference
stays with the operator either way; it is never billed back to the user.

A withdrawal whose net amount would land at or below the dust limit (546 sat)
is refused with `422` **before** any hold is placed. So is one below the
asset's minimum.

### Destination addresses

Validated on the way in, with the checksum and the network prefix:

- a mainnet address on a testnet deployment is refused, and the reverse
- a single mistyped character is refused
- bech32, bech32m (taproot) and base58check are all accepted
- one of this service's own deposit addresses is refused

All of these are `422` with nothing reserved. There is no way to discover this
later, which is the point: without it the user's balance sits on hold until a
human notices.

### Lightning (`BTC_LN`), if the deployment has it enabled

A Lightning destination is not an address, and three of the differences reach
your integration.

**It is a BOLT11 invoice, and it expires.** Ask the user's wallet for one at the
moment they confirm, not when they open the withdrawal screen. An invoice with
less than a couple of minutes left is refused with `422`.

**The invoice must be for the net amount, or for no amount at all.** BTCPay pays
an amount-carrying BOLT11 at *its* amount rather than at the payout's, so the two
have to agree. Under `deduct`, net is `amount_gross - fee`, and the `BTC_LN` fee
is the flat one from `GET /v1/assets` — so you can compute it before asking the
wallet for the invoice. An amountless invoice sidesteps the arithmetic entirely
and is the simpler integration.

**A withdrawal can be refunded after it was accepted.** If the invoice expires
while the request waits for an operator, or the payment cannot be routed, the
withdrawal ends `refunded` with the balance returned. Handle that terminal state
— on chain it is rare, on Lightning it is ordinary.

On the deposit side there is one difference worth surfacing in your UI: a
Lightning deposit invoice can be paid **exactly once**. An on-chain deposit
address accepts several payments and credits each; a BOLT11 that has been paid
is finished, and the user needs a new deposit for a second payment.

Lightning balances are also a **separate asset**. `BTC` and `BTC_LN` are two
rows in `GET /v1/assets` and two balances in `GET /v1/users/{id}/balances`, and
one cannot pay out over the other's rail. Show them as two balances, because
that is what they are.

### Limits and why a small withdrawal can still need approval

Three gates, all evaluated in the same transaction as the hold:

1. **per-withdrawal auto-approval limit** — above it, an operator approves
2. **per-asset rolling 24h cap** — once the last 24 hours of withdrawals reach
   it, *everything* goes to manual approval, including small amounts
3. **optional per-user daily cap**

Gate 2 is why `status` can be `pending_approval` for an amount that was
auto-approved an hour earlier. The response carries `approval_reason` saying
which gate fired. This is the control that bounds the damage from a stolen API
key, so it is deliberately blunt.

### Reading a withdrawal

```http
GET /v1/withdrawals/{withdrawal_id}
GET /v1/users/{external_user_id}/withdrawals?limit=25&cursor=<withdrawal_id>
```

Poll while the status is not terminal. `txid` appears at `broadcast` and is
what you show the user; treat it as informational until `confirmed`.

### What to show a user

| Status | Suggested message |
|---|---|
| `pending_approval` | "Being reviewed" |
| `approved`, `submitting`, `submitted` | "Processing" |
| `broadcast` | "Sent" plus the txid |
| `confirmed` | "Complete" |
| `failed`, `rejected` | "Being reviewed" — the money is not back yet |
| `refunded` | "Returned to your balance" |

### Errors

| Status | Meaning |
|---|---|
| `402` | not enough available balance (held funds do not count) |
| `404` | unknown asset |
| `422` | bad address, dust, below the minimum, or a reused idempotency key |
| `409` | a duplicate request is in flight |

Retry a `5xx` with the **same** `Idempotency-Key`. A retry with a new key is a
second withdrawal.

## Outbound webhooks

Optional. If you would rather poll, leave `PLATFORM_WEBHOOK_URL` unset and
nothing changes — events queue up server-side and are never lost, so you can
turn delivery on later and receive the backlog.

```
POST /your/endpoint
Content-Type: application/json
X-CPA-Signature: t=1760000000,v1=4f2a…

{"id":"evt_019f…","type":"deposit.settled","created_at":"2026-08-10T13:11:02+00:00",
 "data":{"deposit_id":"019f…","external_user_id":"user-42","asset":"BTC",
         "status":"settled","amount_credited":"0.50000000"}}
```

Return any 2xx to acknowledge. Anything else is retried at 1m, 5m, 30m, 2h, 6h
then every 12 hours — ten attempts over about three days, after which the event
is dead-lettered and an operator is alerted. It is never deleted; they can
re-queue it.

### Verifying the signature

The scheme is Stripe's. The timestamp is inside the signed string, so a captured
request cannot be replayed tomorrow.

```python
import hashlib
import hmac
import time

def verify(secret: str, raw_body: bytes, header: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, presented = parts.get("t"), parts.get("v1")
    if not timestamp or not presented:
        return False
    if abs(int(time.time()) - int(timestamp)) > tolerance:
        return False          # outside the replay window
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, presented)
```

Three things that are easy to get wrong:

1. **Use the raw request body.** Parsing the JSON and re-serializing it changes
   the whitespace and the signature will never match. Read the bytes first.
2. **Use `compare_digest`**, not `==`.
3. **Enforce the timestamp window** — five minutes is the convention. Without
   it the signature proves authenticity but not freshness.

### Handling an event

```
1. verify the signature      -> 401 if it fails
2. dedup on "id"             -> the same evt_ id may arrive twice
3. return 200 immediately
4. re-read the resource      -> GET /v1/deposits/{id}
5. act on what the GET said, not on what the webhook said
```

Step 5 is the whole contract. A webhook tells you *something changed*; the GET
tells you *what is true*. Every integration that skips it eventually
double-credits a user, because a retried delivery looks exactly like a second
event.

Ids are stable and prefixed `evt_`. We ask BTCPay's consumers — us — to dedup,
so we provide the same courtesy.

## Operator endpoints

`GET /v1/admin/deposits/review` and `POST /v1/admin/deposits/{id}/resolve`
require an `admin` key and are for your operations team, not your backend.

Resolve takes `{"action": "credit", "payment_id": "..."}` or
`{"action": "dismiss"}`. There is deliberately **no amount field**: the server
asks BTCPay what the payment was worth. An operator confirms which payment
belongs to which deposit and nothing else.

`GET /v1/admin/wallet-alerts` lists coins that reached the hot wallet matching
no known deposit — usually a payment to a long-dead address. Those need manual
attribution.

`GET /v1/admin/withdrawals?status=pending_approval` is the approval queue, with
`POST .../approve` and `POST .../reject` beside it. Reject returns the money
immediately, because nothing has been sent yet.

`POST /v1/admin/withdrawals/{id}/release` takes
`{"attestation": "..."}` and is the only way to return a hold once a payout may
exist. The text is stored on the withdrawal. It exists because "the payout
looks failed" and "the coins are definitely not arriving" are different claims,
and only a human can make the second one.

## Health

`GET /healthz` needs no key and reports this process and its database only. It
deliberately does not check BTCPay: a BTCPay restart is routine and must not
make the API look down while balances and reads are fine.

## A worked example

```
POST /v1/deposits  {user-42, BTC}          -> 201, status pending, address A
  show A to the user
GET  /v1/deposits/{id}   (every 15s)
  -> pending      nothing yet
  -> confirming   payment seen, "waiting for confirmations"
  -> settled      amount_credited "0.50000000", credit the user's account
```

If it goes to `review` instead, show "being verified" and keep polling. It will
become `settled` once the operator resolves it, and `amount_credited` will then
be correct.
