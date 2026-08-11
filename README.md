# crypto-processing-api

**The accounting layer BTCPay Server does not have.** Run it next to your own
BTCPay node and your platform gets per-user crypto balances, deposits and
withdrawals through a small authenticated JSON API — backed by an append-only
double-entry ledger that a webhook cannot corrupt, a reconciliation sweep that
does not trust the fast path, and a threat model that starts from "the hot
wallet is the loss ceiling". Self-hosted, single-tenant, MIT.

Your platform calls it backend-to-backend. This service owns the money ledger;
BTCPay owns the blockchain. It runs on one small VPS next to BTCPay — built for
a €4/month Hetzner CAX11.

## Who this is for

- You need **per-user custodial balances** in BTC or USDT — a marketplace, a
  game economy, SaaS credits — and "one invoice, one payment" is not enough.
- You are willing to run BTCPay Server and one VPS yourself.
- You want the money-correctness machinery already built: idempotency,
  double-entry accounting, approval queues, velocity caps, and a reconciliation
  job that finds what the fast path missed.

## Who this is not for

- **You want non-custodial checkout.** Then you want BTCPay on its own. Adding
  this means you now hold customer funds, which is a legal and operational
  position, not a feature.
- **You want zero operations.** A hosted processor takes the work by taking
  your float. That is a real trade and sometimes the right one.
- **You need one BTC balance spendable over either rail.** `BTC` and `BTC_LN`
  are separate floats here — see the box below — and merging them is on the
  [not-planned list](ROADMAP.md#not-planned).
- **You need multi-tenancy, many assets, or something audited at scale.** One
  deployment serves one platform, three assets ship, and there has been no
  external audit. See [`ROADMAP.md`](ROADMAP.md).

## How it compares

|  | Raw BTCPay Server | Hosted processor | **This project** | Building it yourself |
|---|---|---|---|---|
| Per-user ledger and balances | none — invoices only | their database, their rules | double-entry, append-only, yours | months of subtle work |
| **Custody** | you | **them** | **you** | you |
| Fees | node costs | a percentage of volume | node + VPS costs | your time |
| KYC / account risk | none | their policies; they can freeze | none | none |
| Withdrawal controls | manual | theirs | holds, velocity caps, approval queue | build them |
| Correctness under crash and retry | not applicable | opaque | idempotency + a reconciliation sweep, drilled on regtest | the hard part |
| Audit trail | an invoice list | a CSV export | an immutable journal in your PostgreSQL | build it |
| Trust required | your node | a third party holding your float | your node + this code (MIT, readable) | your code |

The honest row is custody. **This project does not remove custody risk — it
gives you the controls and the books.** A hosted processor removes the work by
taking the float. Pick the one whose risk you would rather hold.

## What it does

| | Deposits | Withdrawals |
|---|---|---|
| **BTC** (on-chain, pruned node) | automatic | automatic via BTCPay's payout processor |
| **BTC_LN** (Lightning, opt-in) | automatic, instant | automatic via BTCPay's Lightning payout processor |
| **USDT-TRC20** (USDt plugin + TronGrid) | automatic | operator-sent, verified on chain |

> **Read this before "we need Lightning" becomes a yes.** `BTC_LN` is a
> **separate asset with its own float**. A user has an on-chain BTC balance and
> a Lightning BTC balance, and they are not interchangeable: Lightning funds
> cannot be withdrawn on chain, on-chain funds cannot be withdrawn over
> Lightning, and nothing moves between them automatically. Moving value between
> the two floats is an operator action —
> [the rebalancing runbook](https://oliverd25.github.io/crypto-processing-api/operating/runbook-ln-rebalance/).
>
> Most people who say they need Lightning mean *one balance, either rail out*.
> This is not that, and building that means a rebalancing entry kind that does
> not exist yet. It is off by default (`LIGHTNING_ENABLED=false`), partly for
> that reason and partly because enabling it makes the bootstrap request one
> server-level BTCPay permission —
> the [security model](https://oliverd25.github.io/crypto-processing-api/operating/security/) explains the trade-off.
>
> Two smaller behaviours worth knowing: a Lightning deposit invoice can be paid
> **exactly once** (an on-chain deposit address accepts several payments), and a
> Lightning withdrawal destination is a BOLT11 invoice that **expires**, so a
> request that waits too long in the approval queue is refunded rather than
> sent.

- **Ledger** — append-only double-entry journal in PostgreSQL, integer amounts
  (satoshis, micro-USDT), enforced by database constraints and triggers. No
  floats anywhere.
- **Deposits** — a BTCPay invoice per request, credited per on-chain payment.
  Webhooks are the fast path; a reconciliation sweep is what makes it correct.
- **Withdrawals** — balance held atomically with the approval decision,
  auto-approved below a limit, capped by a rolling 24-hour velocity gate.
- **Operations** — an admin API for the approval and review queues, an hourly
  invariant and custody check, alerts to ntfy or Telegram.

## Quickstart

```sh
git clone https://github.com/OliverD25/crypto-processing-api && cd crypto-processing-api
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

docker compose -f deploy/docker-compose.test.yml up -d   # test database
pytest                                                    # 1207 tests

docker compose -f deploy/docker-compose.regtest.yml up -d # bitcoind, BTCPay, api, worker
python scripts/bootstrap_btcpay.py                        # configure BTCPay
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker
python scripts/dev/smoke_test.py                          # end-to-end drills
```

The last command creates a deposit, pays it from a regtest node, mines,
withdraws, and asserts the balances to the satoshi. It also stops the API
mid-payment to prove the reconciliation path credits without help.

For a real deployment: [Deploying](https://oliverd25.github.io/crypto-processing-api/operating/deployment/).

## Operating safely

> **The hot wallet float is the loss ceiling.**

If BTCPay or the host is compromised, whatever is in the hot wallet is gone —
no control in this service changes that. Keep one to three days of payout
volume there and sweep the rest to cold storage.

The second number that matters is `SEED_BTC_WITHDRAWAL_DAILY_CAP`. Under a
stolen platform API key that rolling 24-hour cap is the most an attacker can
take before every withdrawal starts waiting for a human. Set it to what you can
afford to lose in a day.

Everything else — key hashing, HMAC verification, velocity gates, the
append-only ledger — serves one of two goals: keeping that number small, and
making sure you find out quickly. The full picture, with its honest residual
risks, is in the [security model](https://oliverd25.github.io/crypto-processing-api/operating/security/).

## Documentation

**[oliverd25.github.io/crypto-processing-api](https://oliverd25.github.io/crypto-processing-api/)** —
the whole thing, searchable, tracking `main`. The same files are in
[`docs/`](docs/) if you would rather read them here or offline.

| Start at | If you are |
|---|---|
| [Integrating](https://oliverd25.github.io/crypto-processing-api/integrating/) | writing the platform that calls this: lifecycles, idempotency, webhook verification |
| [Client libraries](https://oliverd25.github.io/crypto-processing-api/integrating/sdks/) | on Python or Node and want the two hard parts already done |
| [API endpoints](https://oliverd25.github.io/crypto-processing-api/reference/api/) | looking up a route or an error code |
| [Deploying](https://oliverd25.github.io/crypto-processing-api/operating/deployment/) | going from a fresh VPS to a running deployment |
| [Security model](https://oliverd25.github.io/crypto-processing-api/operating/security/) | deciding whether to point real money at this |
| [Configuration](https://oliverd25.github.io/crypto-processing-api/reference/configuration/) | setting an environment variable and wanting to know what it does |
| [Runbooks](https://oliverd25.github.io/crypto-processing-api/operating/runbook-usdt-withdrawals/) | holding the pager at 3am |
| [Adding your own asset](https://oliverd25.github.io/crypto-processing-api/extending/adding-an-asset/) | adding a coin: the pluggable facets, and what is welded shut |
| [Design record](https://oliverd25.github.io/crypto-processing-api/design/) | asking why it is built this way, and how it survived being attacked on paper |

Three things live in this repository rather than on the site, because they are
about the repository: [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) (every control,
the file it lives in, the test that proves it),
[`ROADMAP.md`](ROADMAP.md) (what is coming and what is deliberately not
planned), and [`CONTRIBUTING.md`](CONTRIBUTING.md).

The machine-readable contracts are committed and drift-gated by CI:
[`openapi.json`](docs/reference/openapi.json),
[`webhook-events.json`](docs/reference/webhook-events.json), and
[`configuration.md`](docs/reference/configuration.md), which is generated from
the settings model itself.

## Status

**v0.1.1 released; v0.2 in progress on `main`.** 1207 tests, including
concurrency tests with real threads against real PostgreSQL, property-based
tests over the ledger, and end-to-end drills against a real BTCPay on regtest.
CI enforces **97% branch coverage over `src/`** and 85% over `ledger/`, and
branch coverage is the point: a line-only floor calls an `if` covered when only
one side of it has ever run, and on a money path that is the half that matters.
The API and webhook contracts are committed under
[`docs/reference/`](docs/reference/) and CI fails if the code and the spec
disagree.

**It has not been through an external audit and has not run at scale.** Read
the [security model](https://oliverd25.github.io/crypto-processing-api/operating/security/) before pointing real funds at it, and
start with amounts you would not mind losing.

Not in this version: automated USDT sending (the BTCPay plugin has no payout
handler — Phase 2 adds a signer), HMAC request signing for inbound auth, a
unified BTC balance spendable over either rail, multi-tenancy. Where the rest
is going, and what is deliberately not planned: [`ROADMAP.md`](ROADMAP.md).

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md). One rule stands out: a pull request
touching `ledger/` or `services/` has to say which of the nine ledger
invariants still hold and why — the pull-request template lists them so you do
not have to go looking.

Good places to start are the issues labelled
[`good first issue`](https://github.com/OliverD25/crypto-processing-api/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22),
which are self-contained and mostly outside the money paths. Two things are
worth more than any feature: an adversarial read of `ledger/service.py` and
`services/withdrawals.py`, and an operator's account of running this with real
money — there is an
[issue template](.github/ISSUE_TEMPLATE/operator-report.yml) for exactly that.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
