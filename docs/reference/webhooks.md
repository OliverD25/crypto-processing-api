# Webhook events

The eight events this service sends to your platform, and the header that
proves they came from it. [Integrating](../integrating/index.md#outbound-webhooks)
is the narrative version — how to build a handler, and the five-step rule that
keeps one correct. This page is the lookup table.

Outbound delivery is optional. Leave `PLATFORM_WEBHOOK_URL` unset and nothing
is lost: events queue server-side, and turning delivery on later ships the
backlog.

## The contract in one line

**A webhook tells you that something changed. It does not tell you what is
true.** Re-read the resource with a `GET` and act on that. Every integration
that skips this eventually double-credits a user, because a retried delivery
looks exactly like a second event.

## The machine-readable version

[`webhook-events.json`](webhook-events.json) is the JSON Schema of all eight
payloads — one discriminated union over the `type` field, generated from the
same models the server builds the payloads with, and gated by CI so it cannot
describe a payload the server does not send.

Generate your parser from it rather than writing one by hand. Both
[client libraries](../integrating/sdks.md) already do.

## The events

| `type` | Sent when | `data` |
|---|---|---|
| `deposit.detected` | a payment has been seen but is not final yet | the deposit fields **plus `payments`**, the per-payment detail |
| `deposit.settled` | the payment is confirmed and the balance credited | the deposit fields; `amount_credited` is the number to act on |
| `deposit.review_required` | a human is needed before it can be credited — a late payment, or a USDT amount outside tolerance | the deposit fields, `status` is `review` |
| `deposit.expired` | the invoice window closed with nothing to credit | the deposit fields, `status` is `expired` |
| `withdrawal.pending_approval` | above the auto-approval limit, or the 24-hour cap is spent. The balance is already held | the withdrawal fields **plus `reason`** |
| `withdrawal.broadcast` | a transaction exists on the network | the withdrawal fields; `txid` is informational until confirmed |
| `withdrawal.completed` | confirmed and final | the withdrawal fields |
| `withdrawal.failed` | it will not be sent. The money is not necessarily back yet | the withdrawal fields **plus `reason`**; `status` is `failed`, `rejected` or `refunded` |

The deposit fields are `deposit_id`, `external_user_id`, `asset`, `status` and
`amount_credited`. The withdrawal fields are `withdrawal_id`,
`external_user_id`, `asset`, `status`, `amount_gross`, `amount_net`, `fee`,
`destination_address` and `txid`. Every one of them is always present, and
`txid` is `null` until a transaction exists. `reason` says which gate fired on
`pending_approval`, and why it will not proceed on `failed`.

**Amounts are decimal strings, never JSON numbers.** A JavaScript `number`
cannot hold a satoshi count safely, so the wire format never asks it to.

Every payload has the same envelope:

```json
{
  "id": "evt_019f...",
  "type": "deposit.settled",
  "created_at": "2026-08-10T13:11:02+00:00",
  "data": { "...": "..." }
}
```

`id` is stable and prefixed `evt_`. **Deduplicate on it** — the same id may
arrive more than once.

!!! warning "Handle an unknown `type` by returning 200"

    A newer server may send an event your handler has never heard of. Treat
    that as acknowledged rather than as an error, or a single new event type
    fills your retry queue. Both client libraries raise a distinct
    `UnknownEventTypeError` for exactly this.

## Signature

```
POST /your/endpoint
Content-Type: application/json
X-CPA-Signature: t=1760000000,v1=4f2a…
```

The scheme is Stripe's. The signed string is `<timestamp>.<raw body bytes>`,
HMAC-SHA256 with `PLATFORM_WEBHOOK_SECRET`, hex-encoded. The timestamp is
inside the signed string, so a captured request cannot be replayed tomorrow.

Three things that are easy to get wrong, in order of how often they are:

1. **Verify over the raw request body.** Parsing the JSON and re-serializing it
   changes the whitespace, and the signature will never match again.
2. **Compare in constant time** — `hmac.compare_digest`, `timingSafeEqual`.
3. **Enforce the timestamp window.** Five minutes is the convention. Without
   it the signature proves authenticity but not freshness.

Working verifiers in Python and Node, with no dependencies, are in
[Integrating](../integrating/index.md#verifying-the-signature). The cases every
implementation is checked against — including the ones that must be *refused* —
are published as
[`sdks/signature-vectors.json`](https://github.com/OliverD25/crypto-processing-api/blob/main/sdks/signature-vectors.json).

`v1=` is versioned deliberately. A future change to the scheme ships as `v2=`
alongside it rather than breaking every receiver at once — see
[versioning](versioning.md).

## Retries

Return any 2xx to acknowledge. Anything else, or a timeout, is retried at 1
minute, 5 minutes, 30 minutes, 2 hours, 6 hours, then every 12 hours: ten
attempts over roughly three days. After that the event is dead-lettered and an
operator is alerted.

**A dead-lettered event is never deleted.** It is parked with its last error
and an operator can re-queue it. The schedule itself is fixed in code and is
not configurable; only how often the delivery worker drains the queue is
([`OUTBOUND_DELIVERY_INTERVAL_SECONDS`](configuration.md)).
