# crypto-processing-api

Open-source, single-tenant, custodial crypto payment and ledger service.
It sits between **your platform backend** and a **self-hosted
[BTCPay Server](https://btcpayserver.org/)**. Your platform calls this API
backend-to-backend to create deposits, request withdrawals, and read balances.
This service owns the double-entry money ledger; BTCPay owns the blockchain.

> **Status: pre-release, under active development.** Do not point real funds
> at this until v0.1.0 is tagged and you have read `docs/security.md`.

## What it does

- **Deposits** — creates BTCPay invoices per deposit request, credits the user's
  ledger balance when the payment settles (webhook fast path + reconciliation
  polling as the correctness mechanism).
- **Withdrawals** — validates and locks balance atomically, auto-approves below
  a configurable limit, executes BTC payouts through BTCPay's automated payout
  processor. USDT-TRC20 withdrawals are operator-verified in the MVP.
- **Ledger** — append-only double-entry journal in PostgreSQL, integer amounts
  (satoshis / micro-USDT), enforced invariants, no floats anywhere.

## Assets (MVP)

| Asset | Deposits | Withdrawals |
|---|---|---|
| BTC (on-chain, pruned node) | automatic | automatic via BTCPay payout processor |
| USDT-TRC20 (USDt plugin + TronGrid) | automatic | manual (admin-approved, on-chain verified) |

## Local development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# ledger database for the tests, on port 54329
docker compose -f deploy/docker-compose.test.yml up -d
pytest

# full BTC end-to-end stack: bitcoind regtest, NBXplorer, BTCPay, api, worker
docker compose -f deploy/docker-compose.regtest.yml up -d
python scripts/bootstrap_btcpay.py
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker

# deposit, webhook-outage and replay drills against the real stack
python scripts/dev/smoke_test.py
```

`make help` lists the rest. Every configuration variable is documented in
[`.env.example`](.env.example).

## Documents

The full architecture — verified BTCPay fact sheet, ledger design, integration
design, security model, and the adversarial review it survived — lives in
[`docs/design/`](docs/design/). Start with
[`00-implementation-plan.md`](docs/design/00-implementation-plan.md).

For platform developers: [`docs/integrating.md`](docs/integrating.md) — the
deposit lifecycle, idempotency semantics, and why polling is part of the truth
model rather than a fallback.

Operations: [`docs/backups.md`](docs/backups.md) — continuous WAL archiving,
the restore drill, and which records exist in this database and nowhere else.

## License

[MIT](LICENSE)
