# platform-demo — a worked integration

The platform side of a crypto-processing-api integration: one `app.py`, a few
templates, and the whole loop against a real regtest Bitcoin network. A user
signs in, deposits BTC, watches it credit, and withdraws some of it again.

It is written to be **read**, not copied. `app.py` is a tutorial in numbered
sections, and it deliberately breaks this repository's rule about comments —
everywhere else a comment explains a non-obvious *why*, and here the narration
is the point.

The full walkthrough, with the webhook handler annotated line by line, is on
the documentation site:
<https://oliverd25.github.io/crypto-processing-api/integrating/example-app/>

## Run it

Four commands from the repository root, with Docker running and the package
installed (`pip install -e ".[dev]"` — the bootstrap script is what needs it).
The demo lands on <http://127.0.0.1:8096>.

```sh
export COMPOSE_PROFILES=example
docker compose -f deploy/docker-compose.regtest.yml up -d --build
python scripts/bootstrap_btcpay.py
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker
```

On PowerShell the first line is `$env:COMPOSE_PROFILES = "example"`.

**Use the variable, not `--profile example`.** Both start the demo container,
but only the variable is visible to the interpolation in the compose file, and
that is what tells the api where to deliver webhooks. With `--profile` alone
the demo still works — it polls, and polling is what is always correct — but
the activity feed will never show a line marked `[webhook]`.

Pay a deposit by mining regtest coins into it:

```sh
sh scripts/dev/mine.sh 101          # once, so coinbase outputs mature
docker compose -f deploy/docker-compose.regtest.yml exec -T bitcoind \
  bitcoin-cli -datadir=/data -rpcwallet=regtest sendtoaddress <the address> 0.5
sh scripts/dev/mine.sh 2
```

Tear it down with `docker compose -f deploy/docker-compose.regtest.yml down -v`.

## What to watch, and what each step proves

| Step | What to watch | What it proves |
|---|---|---|
| Sign in | the name in the cookie is all the demo knows | the processing API never sees your users, only an opaque `external_user_id` |
| Get a deposit address | the card polls itself every 3s | `pending → confirming → settled` is read from `GET /v1/deposits/{id}`, not guessed |
| Pay it, mine 2 blocks | the card reaches `settled` and stops polling | a credit is final once the payment says `credited` |
| Balances | the table updates on its own | this app stores no balances; every number came from the API a moment ago |
| Activity feed | a line marked `[webhook]` | the outbound delivery arrived, was verified, deduped, and the resource was **re-read** before anything was recorded |
| Request a withdrawal | `pending_approval` or straight to `approved` | the hold is placed before the call returns; `pending_approval` is not a rejection |
| Mine 2 more blocks | `broadcast` with a txid, then `confirmed` | the fee was fixed at payout creation, and net = gross − fee |

Submit the withdrawal form twice without reloading the page and you get **one**
withdrawal. The form carries an id generated when the page was rendered, and it
is passed as the `Idempotency-Key`. That is the ergonomic worth copying: key
the request on the operation your own system already has an id for, never on
the attempt.

## The parts

| File | What it is |
|---|---|
| `app.py` | the whole application, in eight numbered sections |
| `templates/` | Jinja2 pages and HTMX fragments; the `data-` attributes are read by the nightly |
| `requirements.txt` | pinned, with the SDK taken from `sdks/python` in this repository |
| `Dockerfile` | built from the repository root, because of the line above |

## It cannot rot

`scripts/dev/example_loop.py` drives this app's own HTTP surface headlessly —
sign in, create a deposit, pay it with `bitcoin-cli`, wait for the balance to
appear on the page, withdraw, wait for `confirmed`. The nightly end-to-end job
runs it against a freshly built stack every night.

A tutorial that has quietly stopped working fails in front of the worst
possible reader: somebody meeting the project for the first time. So the
tutorial is a test.

Run it yourself against a stack that is already up:

```sh
python scripts/dev/example_loop.py
```

## What is missing on purpose

No password, no database, no CSRF token, no rate limit, no error page a
customer should see. Each of those has a comment in `app.py` where it would go.
The demo is a demonstration of one contract, not a starting template.
