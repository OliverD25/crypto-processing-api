<!--
The pitch, the two audience lists and the comparison table are the same
argument as the top of README.md and are kept in sync by hand. That is a
deliberate choice, not an oversight: the README is what GitHub shows and has
to stand alone, and transcluding half of it here would drag its relative links
along. Two files, one edit — if you change the pitch, change both.
-->

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

!!! info "These docs track `main`"

    There is no version selector. For the documentation matching a release you
    have deployed, read `docs/` at that release's tag on GitHub. A version
    selector will arrive with 1.0, when there is more than one supported
    version to select between.

## Where to go

| If you are | Start at |
|---|---|
| trying it out | [Quickstart](getting-started/quickstart.md) — regtest in about ten minutes |
| writing the platform that calls it | [Integrating](integrating/index.md), then the [client libraries](integrating/sdks.md) |
| putting it on a server | [Deploying](operating/deployment.md) and the [security model](operating/security.md) |
| holding the pager | the [runbooks](operating/runbook-usdt-withdrawals.md) and [backups](operating/backups.md) |
| adding an asset | [Adding your own asset](extending/adding-an-asset.md) |
| looking something up | [API endpoints](reference/api.md), [configuration](reference/configuration.md), [webhook events](reference/webhooks.md) |
| asking why it is built this way | the [design record](design/index.md) |

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
  are separate floats here, and merging them is not planned.
- **You need multi-tenancy, many assets, or something audited at scale.** One
  deployment serves one platform, three assets ship, and there has been no
  external audit.

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

!!! warning "Read this before \"we need Lightning\" becomes a yes"

    `BTC_LN` is a **separate asset with its own float**. A user has an on-chain
    BTC balance and a Lightning BTC balance, and they are not interchangeable:
    Lightning funds cannot be withdrawn on chain, on-chain funds cannot be
    withdrawn over Lightning, and nothing moves between them automatically.
    Moving value between the two floats is an operator action —
    [the rebalancing runbook](operating/runbook-ln-rebalance.md).

    Most people who say they need Lightning mean *one balance, either rail
    out*. This is not that. It is off by default (`LIGHTNING_ENABLED=false`),
    partly for that reason and partly because enabling it makes the bootstrap
    request one server-level BTCPay permission — the
    [security model](operating/security.md) explains the trade-off.

## Status

**v0.1.1 released; v0.2 in progress on `main`.** The test suite includes
concurrency tests with real threads against real PostgreSQL, property-based
tests over the ledger, and end-to-end drills against a real BTCPay on regtest.
The API and webhook contracts are committed and CI fails if the code and the
specification disagree.

**It has not been through an external audit and has not run at scale.** Read
the [security model](operating/security.md) before pointing real funds at it,
and start with amounts you would not mind losing.
