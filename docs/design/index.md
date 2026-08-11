# The design record

!!! warning "Historical record, not living documentation"

    These are the design documents as they were written, before and during
    implementation. They are kept **unedited** on purpose: their value is that
    they say what was decided and why, including the parts that later turned
    out to be wrong. Reformatting them, or quietly correcting them against the
    code, would destroy exactly that.

    **Where one of these disagrees with the rest of the site, the rest of the
    site is right.** For what the software does today, read
    [Integrating](../integrating/index.md), the
    [API endpoints](../reference/api.md) and the
    [configuration reference](../reference/configuration.md).

Read them when you want to know *why* something is built the way it is — why
the ledger is append-only, why a webhook is never trusted on its own, why USDT
withdrawals are sent by hand, why Lightning is a separate asset. The answers
are here in more detail than any reference page would carry.

## v0.1 — the original build

| Document | What it is |
|---|---|
| [MVP implementation plan](00-implementation-plan.md) | the first plan, milestone by milestone |
| [BTCPay Server fact sheet](01-btcpay-fact-sheet.md) | what BTCPay's API actually offers, verified against it |
| [Ledger and data model](02-ledger-design.md) | double-entry design through the lens of money correctness |
| [BTCPay integration layer](03-btcpay-integration-design.md) | the gateway, webhooks and the reconciliation jobs |
| [Security and operations](04-security-ops-design.md) | threat model, key handling, float policy |
| [Merged v0.1 plan](05-merged-plan.md) | the four above, reconciled into one plan |
| [Adversarial review of v0.1](06-adversarial-critique.md) | the plan attacked on purpose, before it was built |

## v0.2 — the current cycle

| Document | What it is |
|---|---|
| [Asset-extension contract](07-extension-contract-design.md) | what an asset has to provide, and what is welded shut |
| [Robustness program](08-robustness-program-design.md) | the drills, the nightly, and what they must prove |
| [DX, SDKs and docs](09-dx-community-design.md) | the clients, this site, and the community kit |
| [v0.2 roadmap](10-v02-roadmap.md) | the workstreams, merged |
| [Adversarial review of v0.2](11-v02-adversarial-critique.md) | the same treatment, with every claim checked against the working tree |
| [v0.2 implementation plan](12-v02-implementation-plan.md) | the milestones being built now |

The two adversarial reviews are the most useful pages here for anyone
evaluating the project. They are the project arguing against itself in writing,
and several of the things they found are still open — see
[`ROADMAP.md`](https://github.com/OliverD25/crypto-processing-api/blob/main/ROADMAP.md).
