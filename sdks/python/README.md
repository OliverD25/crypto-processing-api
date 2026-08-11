# crypto-processing-client

Python client for
[crypto-processing-api](https://github.com/OliverD25/crypto-processing-api) —
per-user custodial BTC and USDT balances on top of your own BTCPay Server.

This is the client. The service it talks to is self-hosted; there is no hosted
API to sign up for.

```bash
pip install crypto-processing-client
```

## Five minutes

```python
from crypto_processing_client import CryptoProcessingClient

client = CryptoProcessingClient("https://pay.example.com", api_key="cpk_live_...")

deposit = client.create_deposit(external_user_id="user-42", asset="BTC")
print(deposit.address, deposit.checkout_link)

# ... the user pays, and you poll or wait for a webhook ...

balances = client.get_user_balances("user-42")
for balance in balances.balances:
    print(balance.asset, balance.available)

withdrawal = client.create_withdrawal(
    external_user_id="user-42",
    asset="BTC",
    amount="25000000",  # gross, integer smallest units, a string
    destination_address="bc1q...",
)
print(withdrawal.status)  # pending_approval, or already moving
```

**Every amount is a string, in both directions.** `amount` and
`expected_amount` are integer numbers of the asset's smallest unit
(`"25000000"` is 0.25 BTC). Everything the server returns is a decimal string
(`"0.25000000"`). Do not put either through `float`.

**Every timestamp is an ISO 8601 string with a literal `+00:00`.** They are
strings here too, not `datetime`s, because the client must not re-render bytes
an integrator may be comparing.

## Idempotency, which you get for free

Every mutating call carries an `Idempotency-Key`. One is minted per call, and
**the same one is reused on every retry of that call** — a retry with a new key
is a second deposit, not a retry. Pass your own when your system already has an
id for the operation:

```python
client.create_deposit(external_user_id="user-42", asset="BTC", idempotency_key=f"order-{order.id}")
```

The client retries a `503` always, a `409` when the server sent a
`Retry-After` (which is how "your earlier attempt is still running" is told
apart from "this transition is illegal"), and a dropped connection — which is
safe precisely because the key is pinned. Tune or switch it off:

```python
from crypto_processing_client import RetryPolicy

client = CryptoProcessingClient(url, key, retry=RetryPolicy(attempts=1))
```

## Webhooks

```python
from crypto_processing_client import parse_event, UnknownEventTypeError, WebhookVerificationError


@app.post("/platform-webhook")
async def platform_webhook(request: Request):
    body = await request.body()  # the raw bytes, never the parsed JSON
    try:
        event = parse_event(body, request.headers, secret=WEBHOOK_SECRET)
    except WebhookVerificationError:
        return Response(status_code=401)
    except UnknownEventTypeError:
        return Response(status_code=200)  # a newer server sent a type you do not know

    if already_handled(event["id"]):  # the same evt_ id may arrive twice
        return Response(status_code=200)

    if event["type"] == "deposit.settled":
        deposit = client.get_deposit(event["data"]["deposit_id"])
        credit(deposit)  # act on the GET, not on the webhook
    return Response(status_code=200)
```

`parse_event` checks the signature over the **raw body bytes**, in constant
time, inside a 300-second window — and refuses to give you an event if any of
that fails. The verifier is checked against
[`sdks/signature-vectors.json`](https://github.com/OliverD25/crypto-processing-api/blob/main/sdks/signature-vectors.json),
the same vectors the server and the TypeScript client assert against, so the
three cannot quietly disagree.

Step 5 is the whole contract: a webhook tells you *something changed*; the GET
tells you *what is true*.

## Errors

Every refusal raises. The split is by what you can do next:

| Raised | Status | What to do |
|---|---|---|
| `BadRequestError` | 400 | fix the request |
| `AuthenticationError` | 401 | fix the key |
| `PermissionDeniedError` | 403 | the key lacks the scope |
| `NotFoundError` | 404 | no such deposit, withdrawal, user or asset |
| `ConflictError` | 409 | an illegal transition, if it reached you |
| `ValidationError` | 422 | a rule refused it; see `.field_errors` |
| `UpstreamRefusedError` | 502 | BTCPay said no; the intent is dead |
| `ServiceUnavailableError` | 503 | temporary; already retried, see `.retry_after` |
| `ServerError` | other 5xx | temporary |
| `TransportError` | — | never got an answer |

All of them derive from `CryptoProcessingError`.

## Versions

`client 0.N.x` supports `server 0.N.y`. See
[`docs/reference/versioning.md`](https://github.com/OliverD25/crypto-processing-api/blob/main/docs/reference/versioning.md).

## What is generated and what is not

`crypto_processing_client._generated` comes from the server's committed
OpenAPI document and is regenerated in CI, which fails on any difference — so
it cannot describe a server that does not exist. Everything else in this
package is handwritten: the idempotency and retry behaviour, the error classes,
and webhook verification. Those are the things codegen cannot produce.

MIT licensed, like the service.
