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

## Design documents

The full architecture — verified BTCPay fact sheet, ledger design, integration
design, security model, and the adversarial review it survived — lives in
[`docs/design/`](docs/design/). Start with
[`00-implementation-plan.md`](docs/design/00-implementation-plan.md).

## License

[MIT](LICENSE)
