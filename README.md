# crypto-processing-api

Open-source, single-tenant, custodial crypto payment and ledger service. It
sits between **your platform backend** and a **self-hosted
[BTCPay Server](https://btcpayserver.org/)**. Your platform calls it
backend-to-backend to create deposits, request withdrawals and read balances.
This service owns the double-entry money ledger; BTCPay owns the blockchain.

Runs on one small VPS next to BTCPay. Built for a €4/month Hetzner CAX11.

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
> [`runbook-ln-rebalance.md`](docs/runbook-ln-rebalance.md).
>
> Most people who say they need Lightning mean *one balance, either rail out*.
> This is not that, and building that means a rebalancing entry kind that does
> not exist yet. It is off by default (`LIGHTNING_ENABLED=false`), partly for
> that reason and partly because enabling it makes the bootstrap request one
> server-level BTCPay permission —
> [`security.md`](docs/security.md) explains the trade-off.
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
pytest                                                    # 775 tests

docker compose -f deploy/docker-compose.regtest.yml up -d # bitcoind, BTCPay, api, worker
python scripts/bootstrap_btcpay.py                        # configure BTCPay
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker
python scripts/dev/smoke_test.py                          # end-to-end drills
```

The last command creates a deposit, pays it from a regtest node, mines,
withdraws, and asserts the balances to the satoshi. It also stops the API
mid-payment to prove the reconciliation path credits without help.

For a real deployment: [`docs/deployment.md`](docs/deployment.md).

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
risks, is in [`docs/security.md`](docs/security.md).

## Documentation

| Document | For |
|---|---|
| [`integrating.md`](docs/integrating.md) | platform developers: lifecycles, idempotency, webhook verification |
| [`api.md`](docs/api.md) | every endpoint, every error code |
| [`deployment.md`](docs/deployment.md) | a fresh VPS to a running deployment |
| [`btcpay-setup.md`](docs/btcpay-setup.md) | what BTCPay needs, including the manual USDt plugin steps |
| [`extending.md`](docs/extending.md) | adding your own asset: the four pluggable facets, what is welded shut, and how `BTC_LN` was added commit by commit |
| [`security.md`](docs/security.md) | threat model, float policy, cold sweeps |
| [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) | auditors: every control, the file it lives in, the test that proves it |
| [`backups.md`](docs/backups.md) | continuous archiving and the restore drill |
| [`runbook-usdt-withdrawals.md`](docs/runbook-usdt-withdrawals.md) | sending USDT by hand |
| [`runbook-usdt-attribution.md`](docs/runbook-usdt-attribution.md) | pooled-address deposits that need a human |
| [`runbook-reorg.md`](docs/runbook-reorg.md) | a credited deposit was orphaned |
| [`runbook-ln-rebalance.md`](docs/runbook-ln-rebalance.md) | moving value between the on-chain and Lightning floats |
| [`design/`](docs/design/) | the architecture, and the adversarial review it survived |

## Status

**v0.1.0.** Every money path has tests, including concurrency tests with real
threads against real PostgreSQL, and end-to-end drills against a real BTCPay on
regtest. It has not been through an external audit and has not run at scale.

Read [`docs/security.md`](docs/security.md) before pointing real funds at it,
and start with amounts you would not mind losing.

Not in this version: automated USDT sending (there is no payout handler in the
BTCPay plugin — Phase 2 adds a signer), HMAC request signing for inbound auth,
a unified BTC balance spendable over either rail, multi-tenancy.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md). One rule stands out: a pull request
touching `ledger/` or `services/` has to say which invariants still hold and
why.

## License

[MIT](LICENSE)
