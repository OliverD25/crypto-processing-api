# Integrating with crypto-processing-api

For developers of the platform backend that calls this service. It assumes
nothing about your stack beyond HTTP and JSON.

> **Status: M2.** Deposits work end to end. Withdrawals arrive in M3, USDT in
> M4, outbound webhooks in M5. Endpoints described here are stable; anything not
> described here does not exist yet.

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

## Truth, and why polling is part of it

Our reconciliation loop asks BTCPay what happened every two minutes and credits
anything the webhook path missed. Webhooks only make it faster. BTCPay gives up
redelivering after roughly eight attempts in an hour, so if this service were
down longer than that, webhooks alone would lose deposits.

The same reasoning applies one level up, to you:

- **Poll `GET /v1/deposits/{id}` while a deposit is not terminal.** Every 10–30
  seconds is plenty.
- When outbound webhooks arrive in M5, they will be **notifications, not
  truth**. Treat one as a hint to re-read the deposit, never as the fact
  itself.
- A credit is final once `credited: true`. It is never quietly reversed. A
  correction, if one is ever needed, is a new journal entry with its own
  record.

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
