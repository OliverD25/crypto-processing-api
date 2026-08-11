# Roadmap

Where this is going, and — just as usefully — where it is not.

v0.1.0 shipped a working custodial service. v0.2 is about **adoption**: making
it something another developer can deploy next to their own BTCPay, integrate
against, and extend, without reading the whole source tree first.

The design record for all of it is in [`docs/design/`](docs/design/), including
the [adversarial review](docs/design/11-v02-adversarial-critique.md) that
changed the plan. Boxes are ticked when the work is on `main` and green in CI,
not when it is started.

## v0.2 — adoption and robustness

### Robustness (done before anything touched money code)

- [x] **Static analysis in CI** — Semgrep on a pinned image with custom rules in
  [`.semgrep/`](.semgrep/) that fail the build on a `Posting` write outside
  `ledger/service.py`, raw SQL in a money path, or a float amount. Plus
  gitleaks over the full history, and CodeQL.
- [x] **Migration robustness** — a round-trip test (up, fingerprint, down, up,
  identical fingerprint), `alembic check`, a single-head check, and frozen
  seeded database dumps in `tests/fixtures/upgrade/` that every future
  migration has to keep upgrading.
- [x] **Property-based ledger tests** — a Hypothesis state machine driving
  `post_entry` and the withdrawal posting matrix against real PostgreSQL, with
  a small derandomized profile on pull requests and a wide random one nightly.
- [x] **[`SECURITY-AUDIT.md`](SECURITY-AUDIT.md)** — every threat in
  `docs/operating/security.md` mapped to the control, the file, and the test.

### The extension contract, proved by adding an asset

- [x] **An asset is one database row plus one registry entry.**
  `services/asset_registry.py` holds fee policy, destination validator,
  withdrawal backend, custody source, payment-method matcher and sweep mode;
  startup fails loudly if an enabled asset has no profile.
- [x] **A conformance suite inside the package** —
  `crypto_processing_api/testing/contracts.py`. A backend that passes it is
  wired correctly; that is the acceptance test, not a code review.
- [x] **[`docs/extending/adding-an-asset.md`](docs/extending/adding-an-asset.md)** — the four pluggable facets,
  what is deliberately welded shut, and the worked example.
- [x] **Lightning (`BTC_LN`) added through the contract without changing it**,
  after a time-boxed feasibility spike. Off by default. Fee-drift journalling
  and a payout deadline came with it, because routing fees and unroutable
  payouts are money problems, not Lightning trivia.

### Developer experience

- [x] **OpenAPI hardening** — a response model and a stable `operation_id` on
  every route, the `Idempotency-Key` header and the error envelope in the
  spec, and a committed [`docs/reference/openapi.json`](docs/reference/openapi.json)
  that CI regenerates and diffs. A route change that skips the regeneration
  fails the build.
- [x] **Webhook payload schemas** — the eight outbound events are typed models
  that the emit sites build through, exported as JSON Schema to
  [`docs/reference/webhook-events.json`](docs/reference/webhook-events.json)
  behind the same gate. Until this, the payloads were the one public contract
  with nothing watching it.
- [x] **[`docs/reference/versioning.md`](docs/reference/versioning.md)** — what
  a minor may change, what a patch may never require, and what will not move
  without a coexisting v2 scheme.
- [x] **Community kit** — issue forms, a pull-request template that inlines the
  nine ledger invariants, a code of conduct, Dependabot, and this file.
- [ ] **Nightly end-to-end on a self-hosted runner** — the full regtest stack,
  every drill, and the wide property profile, once a night on hardware
  somebody owns. Design and isolation model are written up in
  [`docs/operating/nightly-e2e.md`](docs/operating/nightly-e2e.md); the runner install is not done.
- [x] **SDKs** — `crypto-processing-client` on PyPI and npm. Generated core
  from the committed spec, plus a small handwritten layer for the two things
  codegen cannot do: webhook signature verification over raw bytes, and
  idempotency-key discipline across retries. Both are built and tested in
  [`sdks/`](sdks/); publishing waits on two registry accounts that do not exist
  yet, listed in [`sdks/README.md`](sdks/README.md).
- [ ] **Docs site** — the existing markdown, published, with the OpenAPI
  reference rendered from the committed spec and a generated configuration
  page so `.env.example` can never drift from `Settings` again.
- [x] **Example integration app** — a small platform that creates a deposit,
  polls it, shows balances, requests a withdrawal, and receives webhooks the
  way the docs say to. It lives in
  [`examples/platform-demo/`](examples/platform-demo/) and is written as a
  tutorial; [`scripts/dev/example_loop.py`](scripts/dev/example_loop.py) drives
  its whole loop headlessly through its own pages, and the nightly runs that
  script — because a tutorial that rots fails in front of the worst possible
  reader.
- [ ] **Live TRON Nile verification** — **kit ready, awaiting the live
  session.** USDT cannot run on regtest, so its matcher and verifier are
  format-verified only. Everything that can be prepared in advance is:
  [`docs/operating/runbook-nile-verification.md`](docs/operating/runbook-nile-verification.md)
  is the manual half, `scripts/verify_nile.py` is the guided half, and
  [`docs/operating/verification-log.md`](docs/operating/verification-log.md)
  is where the evidence lands. What is left is the session itself — one real
  deposit and one real withdrawal on Nile, which needs a person with two
  wallets and faucet funds. It stays a hard gate before the v0.2.0 tag.
- [ ] **Release v0.2.0** — changelog with a Breaking / Migration section, a
  frozen `v0.2.0` database dump, and a proven `v0.1.x → v0.2.0` upgrade by pull
  plus `alembic upgrade head` plus restart.

## Not planned

Saying no in writing is cheaper for everyone than saying it in ten issues.

- **Multi-tenancy.** One deployment serves one platform. Isolating tenants
  inside a shared ledger is a different product with a different threat model:
  every query grows a tenant predicate, and one missing predicate is one
  customer's balance credited to another. Run a second deployment.
- **A hosted version.** The entire premise is that you hold your own float. A
  hosted service would mean *we* hold it, which is the thing this exists to
  avoid — and it is the honest row in the README's comparison table.
- **Deposit rails that are not BTCPay.** BTCPay is the single source of
  payment truth: one webhook signature scheme, one invoice model, one place
  where "was this paid" is answered. A second rail doubles that surface and
  every reconciliation path with it. Withdrawal backends *are* pluggable, and
  that asymmetry is deliberate.
- **An external security audit before 1.0.** Worth doing and not affordable
  yet. Until then the honest statement stays in the README:
  [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) is the paperwork a reviewer can
  check, not a third party's sign-off.
- **A unified BTC balance spendable over either rail.** `BTC` and `BTC_LN` are
  separate floats and separate balances. Merging them needs a rebalancing
  entry kind and a policy for which rail pays — real work, wanted, and not
  something to fake with a display trick that lies about what is spendable.
  See [`docs/operating/runbook-ln-rebalance.md`](docs/operating/runbook-ln-rebalance.md) for what
  moving value between them means today.
- **Assets with more than 8 decimals.** `assets.decimals` is capped at 8 and
  amounts are `BIGINT`. An 18-decimal token needs a real conversation about
  numeric width before any code.
- **Custody of keys by this service.** BTCPay holds the Bitcoin hot wallet;
  USDT is operator-sent. A signer living in this process is a Phase-2
  conversation about key handling, not a feature request.

## Want to help?

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the rules that matter, and the issues
labelled [`good first issue`](https://github.com/OliverD25/crypto-processing-api/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
are self-contained and mostly outside the money paths. The two things worth
more than any feature are an adversarial read of `ledger/service.py` and
`services/withdrawals.py`, and an operator's account of running this with real
money — there is an issue template for exactly that.
