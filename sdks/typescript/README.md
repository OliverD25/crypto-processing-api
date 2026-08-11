# @oliverd25/crypto-processing-client

<!-- The lines below are included verbatim into the docs site's "Client libraries" page. Keep the markers. -->
<!-- --8<-- [start:body] -->

TypeScript client for
[crypto-processing-api](https://github.com/OliverD25/crypto-processing-api) —
per-user custodial BTC and USDT balances on top of your own BTCPay Server.

This is the client. The service it talks to is self-hosted; there is no hosted
API to sign up for.

```bash
npm install @oliverd25/crypto-processing-client
```

ESM only, Node 20 or newer. The webhook helpers are built on WebCrypto, so they
also run on Deno, Bun, Cloudflare Workers and in a browser.

## Five minutes

```ts
import { CryptoProcessingClient } from '@oliverd25/crypto-processing-client';

const client = new CryptoProcessingClient({
  baseUrl: 'https://pay.example.com',
  apiKey: 'cpk_live_...',
});

const deposit = await client.createDeposit({ external_user_id: 'user-42', asset: 'BTC' });
console.log(deposit.address, deposit.checkout_link);

// ... the user pays, and you poll or wait for a webhook ...

const balances = await client.getUserBalances('user-42');
for (const balance of balances.balances) {
  console.log(balance.asset, balance.available);
}

const withdrawal = await client.createWithdrawal({
  external_user_id: 'user-42',
  asset: 'BTC',
  amount: '25000000', // gross, integer smallest units, a string
  destination_address: 'bc1q...',
});
console.log(withdrawal.status); // pending_approval, or already moving
```

**Every amount is a string, in both directions.** `amount` and
`expected_amount` are integer numbers of the asset's smallest unit
(`'25000000'` is 0.25 BTC). Everything the server returns is a decimal string
(`'0.25000000'`). `Number()` on either loses money: 21 million BTC in satoshis
is past JavaScript's safe integer range.

**Every timestamp is an ISO 8601 string with a literal `+00:00`.** They stay
strings here, not `Date`s, because the client must not re-render bytes an
integrator may be comparing.

Request bodies use the API's own field names (`external_user_id`, not
`externalUserId`) so there is no mapping layer between this package and the
generated types that could drift.

## Idempotency, which you get for free

Every mutating call carries an `Idempotency-Key`. One is minted per call, and
**the same one is reused on every retry of that call** — a retry with a new key
is a second deposit, not a retry. Pass your own when your system already has an
id for the operation:

```ts
await client.createDeposit(
  { external_user_id: 'user-42', asset: 'BTC' },
  { idempotencyKey: `order-${order.id}` },
);
```

The client retries a `503` always, a `409` when the server sent a `Retry-After`
(which is how "your earlier attempt is still running" is told apart from "this
transition is illegal"), and a dropped connection — which is safe precisely
because the key is pinned. Tune or switch it off with `retry: { attempts: 1 }`.

## Webhooks

```ts
import express from 'express';
import { parseEvent, UnknownEventTypeError, WebhookVerificationError }
  from '@oliverd25/crypto-processing-client';

// The raw bytes, not express.json(). Re-serializing the parsed object changes
// the whitespace and the signature can never match.
app.post('/platform-webhook', express.raw({ type: 'application/json' }), async (req, res) => {
  let event;
  try {
    event = await parseEvent(req.body, req.headers, process.env.WEBHOOK_SECRET!);
  } catch (error) {
    if (error instanceof UnknownEventTypeError) return res.sendStatus(200);
    if (error instanceof WebhookVerificationError) return res.sendStatus(401);
    throw error;
  }

  if (await alreadyHandled(event.id)) return res.sendStatus(200); // evt_ ids repeat
  res.sendStatus(200);                                            // acknowledge first

  if (event.type === 'deposit.settled') {
    const deposit = await client.getDeposit(event.data.deposit_id);
    await credit(deposit); // act on the GET, not on the webhook
  }
});
```

`parseEvent` checks the signature over the **raw body bytes**, in constant
time, inside a 300-second window — and refuses to give you an event if any of
that fails. The verifier is checked against
[`sdks/signature-vectors.json`](https://github.com/OliverD25/crypto-processing-api/blob/main/sdks/signature-vectors.json),
the same vectors the server and the Python client assert against, so the three
cannot quietly disagree.

`event.type` is a literal, so TypeScript narrows `event.data` for you inside
each branch.

Two Node-specific traps this helper removes: `express.json()` gives you an
object rather than bytes, and Node's `timingSafeEqual` throws when the two
buffers differ in length — the comparison here checks length first and returns
false instead of raising into a 500 that looks like an outage.

## Errors

Every refusal throws. The split is by what you can do next:

| Thrown | Status | What to do |
|---|---|---|
| `BadRequestError` | 400 | fix the request |
| `AuthenticationError` | 401 | fix the key |
| `PermissionDeniedError` | 403 | the key lacks the scope |
| `NotFoundError` | 404 | no such deposit, withdrawal, user or asset |
| `ConflictError` | 409 | an illegal transition, if it reached you |
| `ValidationError` | 422 | a rule refused it; see `.fieldErrors` |
| `UpstreamRefusedError` | 502 | BTCPay said no; the intent is dead |
| `ServiceUnavailableError` | 503 | temporary; already retried, see `.retryAfter` |
| `ServerError` | other 5xx | temporary |
| `TransportError` | — | never got an answer |

All of them extend `CryptoProcessingError`.

## Versions

`client 0.N.x` supports `server 0.N.y`. See
[`docs/reference/versioning.md`](https://github.com/OliverD25/crypto-processing-api/blob/main/docs/reference/versioning.md).

## What is generated and what is not

`src/generated` comes from the server's committed OpenAPI document and is
regenerated in CI, which fails on any difference — so it cannot describe a
server that does not exist. Everything else is handwritten: the idempotency and
retry behaviour, the error classes, and webhook verification. Those are the
things codegen cannot produce.

The package has no runtime dependencies.

MIT licensed, like the service.
<!-- --8<-- [end:body] -->
