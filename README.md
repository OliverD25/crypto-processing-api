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
| **USDT-TRC20** (USDt plugin + TronGrid) | automatic | operator-sent, verified on chain |

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
pytest                                                    # 569 tests

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
| [`security.md`](docs/security.md) | threat model, float policy, cold sweeps |
| [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) | auditors: every control, the file it lives in, the test that proves it |
| [`backups.md`](docs/backups.md) | continuous archiving and the restore drill |
| [`runbook-usdt-withdrawals.md`](docs/runbook-usdt-withdrawals.md) | sending USDT by hand |
| [`runbook-usdt-attribution.md`](docs/runbook-usdt-attribution.md) | pooled-address deposits that need a human |
| [`runbook-reorg.md`](docs/runbook-reorg.md) | a credited deposit was orphaned |
| [`design/`](docs/design/) | the architecture, and the adversarial review it survived |

## Status

**v0.1.0.** Every money path has tests, including concurrency tests with real
threads against real PostgreSQL, and end-to-end drills against a real BTCPay on
regtest. It has not been through an external audit and has not run at scale.

Read [`docs/security.md`](docs/security.md) before pointing real funds at it,
and start with amounts you would not mind losing.

Not in this version: automated USDT sending (there is no payout handler in the
BTCPay plugin — Phase 2 adds a signer), HMAC request signing for inbound auth,
Lightning, multi-tenancy.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md). One rule stands out: a pull request
touching `ledger/` or `services/` has to say which invariants still hold and
why.

## License

[MIT](LICENSE)
