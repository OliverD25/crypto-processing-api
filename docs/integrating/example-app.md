# Example application

`examples/platform-demo/` is the platform side of this integration, written
out: one FastAPI file, Jinja2 templates, HTMX, and the
[Python client](sdks.md). A user signs in, deposits BTC, watches it credit, and
withdraws some of it again — against a real regtest Bitcoin network, in about
twenty seconds.

It is written to be **read**, not copied. `app.py` is a tutorial in eight
numbered sections, and it deliberately breaks this project's rule about
comments: everywhere else a comment explains a non-obvious *why*, and there the
narration is the point.

- [`examples/platform-demo/app.py`](https://github.com/OliverD25/crypto-processing-api/blob/main/examples/platform-demo/app.py)
  — the whole application
- [`examples/platform-demo/README.md`](https://github.com/OliverD25/crypto-processing-api/blob/main/examples/platform-demo/README.md)
  — what to click, and what each step proves

## What it shows

| Section of `app.py` | What it demonstrates |
|---|---|
| 1. Configuration | the API key and the webhook secret, and why they are two different secrets |
| 2. What a platform stores | a dict — and no balances in it |
| 3. One client per process | why the client is built once, not per request |
| 4. "Log in" | `external_user_id` is opaque; the API never sees your users |
| 5. Deposits | address, checkout link, and the `pending → confirming → settled` poll |
| 6. Balances | read on every render, never stored |
| 7. Withdrawals | gross amounts, `pending_approval`, and an idempotency key derived from the form |
| 8. `/platform-webhook` | the [five-step contract](index.md#handling-an-event), numbered to match |

The claim it exists to make concrete is the one in bold at the top of
[Integrating](index.md#the-one-rule): **the service is the source of truth for
balances, and your database is not.** Every number on every page of the demo
was read from the API a moment before it was rendered. There is nothing to
reconcile because there is nothing mirrored.

## Running it

Four commands from a checkout of the repository, with Docker running and the
package installed (`pip install -e ".[dev]"`, which is what the bootstrap
script needs). The demo lands on <http://127.0.0.1:8096>.

```sh
export COMPOSE_PROFILES=example
docker compose -f deploy/docker-compose.regtest.yml up -d --build
python scripts/bootstrap_btcpay.py
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker
```

On PowerShell the first line is `$env:COMPOSE_PROFILES = "example"`.

The demo is an **opt-in profile**. With the variable unset, none of it is
built, none of it runs, and the stack is exactly the one the
[regtest walkthrough](../getting-started/regtest.md) describes.

!!! warning "Use the variable, not `--profile example`"

    Both spellings start the demo container. Only the variable is visible to
    the interpolation in the compose file, and that is what sets
    `PLATFORM_WEBHOOK_URL` on the api so deliveries have somewhere to go.

    With `--profile` alone the demo still works — it polls, and polling is what
    is always correct — but no activity line will ever be marked `[webhook]`.
    That is a fair demonstration of the design and a confusing first
    impression, so prefer the variable.

To pay a deposit, mine regtest coins into the address the demo shows you:

```sh
sh scripts/dev/mine.sh 101          # once, so coinbase outputs mature
docker compose -f deploy/docker-compose.regtest.yml exec -T bitcoind \
  bitcoin-cli -datadir=/data -rpcwallet=regtest sendtoaddress <the address> 0.5
sh scripts/dev/mine.sh 2
```

Where the API key comes from: a one-shot container runs the service's own CLI
against the ledger database, mints a `readwrite` key and leaves it in a volume
the demo reads. In production an operator mints one and hands it over out of
band; here it happens so that bringing the demo up stays a single command.

## The webhook handler, annotated

This is section 8 of `app.py`, which is the part of any integration most worth
getting right. The numbering matches
[Handling an event](index.md#handling-an-event) exactly.

```python
@app.post("/platform-webhook")
async def platform_webhook(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()
```

**The raw bytes, first.** The signature covers exactly what arrived. Parse the
JSON and re-serialize it and the whitespace changes, so the signature can never
match again. Every framework has a way to ask for the bytes; in Express you
have to ask for it explicitly, which is why that trap has its own note in
[Integrating](index.md#verifying-the-signature).

```python
    try:
        event = parse_event(raw, request.headers, secret=WEBHOOK_SECRET)
    except WebhookVerificationError:
        return Response(status_code=401)
    except UnknownEventTypeError:
        return Response(status_code=200)
```

**Step 1 — verify.** `parse_event` does the three things that are easy to get
wrong on your own: it signs over the raw bytes, compares in constant time, and
enforces the five-minute timestamp window that stops a captured request being
replayed tomorrow.

The second `except` is not a failure path. A newer server can send an event
type this client has never heard of, and the right answer is to acknowledge it
and move on — a 500 there means the server retries an event you are never going
to handle.

```python
    if not STORE.remember_event(event["id"]):
        return Response(status_code=200)
```

**Step 2 — dedup on the id.** The same `evt_` id can arrive twice: a delivery
that timed out on your side was still retried. In the demo the dedup set is a
Python `set`; in your platform it is a unique index on the event id, checked in
the same transaction as whatever the event causes.

```python
    background.add_task(_apply, event)
    return Response(status_code=200)
```

**Step 3 — answer immediately.** The work goes to a background task, which runs
after the response is sent. A handler that does its work first eventually times
out under load, and the service then retries a delivery that in fact succeeded
— ten attempts over about three days, then a dead letter and an alert for an
operator with nothing to fix.

```python
def _apply(event: PlatformEvent) -> None:
    if event["type"] == "deposit.settled":
        deposit = CPA.get_deposit(event["data"]["deposit_id"])
        STORE.note(deposit.external_user_id, f"... {deposit.amount_credited} ...")
```

**Step 4 — re-read the resource. Step 5 — act on what the GET said.**

Note what is *not* used: `event["data"]["amount_credited"]`. It is present, and
it is correct, and reading it is still the habit that breaks integrations. A
webhook tells you *something changed*; the GET tells you *what is true*. Every
integration that skips step 5 eventually double-credits somebody, because a
retried delivery looks exactly like a second event.

## It cannot rot

A tutorial that has quietly stopped working fails in front of the worst
possible reader: somebody meeting the project for the first time. So the
tutorial is a test.

`scripts/dev/example_loop.py` drives the demo's **own HTTP surface** — the
pages a browser gets, not the API underneath — and asserts on what a reader
would see: the deposit card reaching `settled`, the balance table showing a
credit the demo does not store, an activity line that a verified webhook wrote,
and a withdrawal reaching `confirmed` with a txid on the page.

The [nightly end-to-end job](../operating/nightly-e2e.md) runs it against a
freshly built stack every night, after the drills. Run it yourself against a
stack that is already up:

```sh
python scripts/dev/example_loop.py
```

## What it deliberately is not

No passwords, no database, no CSRF token, no rate limit, and no error page a
customer should see. Each of those has a comment in `app.py` where it would go.
It is a demonstration of one contract, not a starting template — and the
contract is the part that is hard to get right.

If you want a worked example that asserts every number to the satoshi rather
than rendering it, that is `scripts/dev/smoke_test.py`, described in the
[regtest walkthrough](../getting-started/regtest.md).
