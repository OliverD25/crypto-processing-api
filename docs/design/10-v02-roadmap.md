# crypto-processing-api v0.2 Roadmap — Merged Plan

One internally consistent plan merging the asset-extension contract design, the robustness program, and the DX/community plan. Verified against source at current head (leaks 8/9 confirmed live in `src/crypto_processing_api/services/withdrawals.py:835-860`; `response_model=None` confirmed on all 22 routes; `id-token: write` confirmed in `.github/workflows/release.yml:15`).

**Headline decision recorded up front:** the extension-contract design makes Lightning (`BTC_LN`) the proof asset for v0.2; the DX design's positioning listed Lightning under "not planned." These directly conflict. **Resolution: Lightning is IN scope** — it is the only candidate that genuinely stresses the contract (instant settle, no address, liquidity-bounded custody, flat fees) while staying on core-native BTCPay with the same Greenfield payout API `BtcpayPayoutBackend` already wraps. All positioning artifacts (README "not for" list, ROADMAP "not planned", comparison table) are amended accordingly. Details in the conflicts table (§6).

---

## 1. Milestones in build order

Two tracks run in parallel: the **money track** (R1–R5, sequential — each hardens the ground the next stands on) and the **adoption track** (R6–R9, sequential within itself, parallel to the money track after R3). **R10 (Nile)** is user-gated and floats freely. **R11** closes.

### R1 — v0.1.1 hotfix: backend-filter defects (ships immediately, standalone)
**Executes:** extension design §7, leaks 8 and 9. These are live defects, not extension blockers.
**Scope:**
- `due_for_submission` (`services/withdrawals.py:835`) gains `Withdrawal.backend == BACKEND_BTCPAY_PAYOUT` — today, with `usdt_auto_withdraw=true` (`config.py:74`), a manual-TRON row reaching `APPROVED` gets quoted a BTC miner fee by `workers/payout_submitter.py` and submitted to a BTCPay payout the USDt plugin cannot pay, stranding it in `submitting`.
- `due_for_polling` (`services/withdrawals.py:845`) gains the same filter — today manual rows in `SUBMITTED` with `backend_ref = manual:<uuid>` are polled against Greenfield every Job B sweep, producing a permanent `BTCPayNotFound` error drip.
- Regression tests for both.
**Definition of done:** tag `v0.1.1`, image published, CHANGELOG entry, both regressions covered. No other changes ride along.
**Effort:** half a day.

### R2 — Robustness guardrails (before anyone touches money code)
**Executes:** robustness design workstreams 1–3, in its recommended internal order (static analysis → migrations → property tests). Rationale for position: the semgrep `postings-writer` rule and the property machine must exist **before** R3 rearranges `services/`, and the v0.1.0 frozen dump is cheapest to produce while v0.1.0/1 is the current release.
**Scope:**
1. *Static analysis:* CodeQL default setup (default suite, not extended); Dependabot security updates on; Semgrep CE with custom `.semgrep/` rules first (`postings-writer` — no `Posting` writes outside `ledger/service.py`; `no-raw-sql-in-money-path`; `no-float-amounts`; best-effort `lock-before-read`) plus `p/python`, pinned version, `--error` in CI; `SECURITY-AUDIT.md` keyed 1:1 to the threat table in `docs/security.md:45-56` (control → file → evidence test/CI job → last verified → residual), with SAST triage log and dependency policy sections. Standalone bandit skipped — ruff already runs the `S` ruleset (`pyproject.toml`).
2. *Migration robustness:* schema round-trip test (upgrade→dump→downgrade→assert only `alembic_version`→upgrade→byte-identical dump); `alembic check` + single-head assertion as a CI step; frozen v0.1.0 seeded dump committed at `tests/fixtures/upgrade/v0.1.0.sql` + `scripts/make_upgrade_fixture.py` + `tests/integration/test_migration_upgrade.py` (load dump → `upgrade head` → `assert_ledger_consistent` + frozen balance expectations). Release checklist gains the one fixture line.
3. *Property-based ledger tests:* Hypothesis `RuleBasedStateMachine` in `tests/integration/test_ledger_properties.py` against real PostgreSQL, driving `post_entry` and the withdrawal posting matrix (deposit/hold/submit/settle/unsubmit/release/replay/crash rules; nightly-only threaded `race_hold`), invariants = `assert_ledger_consistent` + exact model equality + sign discipline + entry count. Profiles: `ci` (`max_examples=15`, `derandomize=True`, `deadline=None`) and `nightly` (`max_examples=300`, random seed). Own TRUNCATE lifecycle copied from `clean_database` (`tests/integration/conftest.py`). `.hypothesis/` uploaded as artifact on failure.
**Definition of done:** all guardrail jobs green in `.github/workflows/ci.yml`; a deliberately-introduced rogue `Posting` write fails semgrep; a deliberately-broken downgrade fails the round-trip; property machine finds nothing at nightly volume for one week.
**Effort:** 5.5–7 days.

### R3 — Asset-extension contract refactor + conformance suite
**Executes:** extension design §2, §4 (steps 1–11 minus Lightning), §6. This is the money-code milestone; everything after builds on it, hence early per the agreed risk ordering — but *after* R2's detectors exist.
**Scope (each step lands with all 569 tests green; R2's semgrep/property/migration gates active throughout):**
- New `services/asset_registry.py`: `AssetProfile` (fee_policy, destination_validator, withdrawal_backend name, custody_source, payment_method_matcher, has_btcpay_wallet, sweep mode, required flag), `build_registry(settings)`, startup fails loudly on enabled-asset-without-profile. Explicit in-code registry — not entry points, not DB-stored behavior names (data in DB, behavior in code; mypy --strict checks conformance).
- Migration `0006_asset_extension_columns`: `pooled_addresses`, `invoice_currency`, `deposit_expiry_minutes` on `assets`, server defaults reproducing v0.1.0 behavior exactly (this migration is the first customer of R2's round-trip + frozen-dump tests).
- Protocol split in `services/backends.py`: `AutomatedWithdrawalBackend` (initiate/poll_status/cancel/**find_for_withdrawal** promoted from the module-level function at `backends.py:87`) and `OperatorWithdrawalBackend` (new_reference/verify_broadcast/confirmations); `BackendPayout` canonical-state normalization so `_PAYOUT_STATE_MAP` stops keying on BTCPay's literal strings; `ManualTronBackend` finally type-checked against its protocol.
- Kill the remaining leaks (§7 items 1–7, 10–12): `INVOICE_CURRENCY`/`_expiry_minutes`/`POOLED_ASSETS` → columns; `_matches` and `REQUIRED_ASSETS` → registry; `validate_destination` if/elif → registry validator; fee/backend routing in `api/withdrawals.py:104-124`, `api/admin.py:213-222`, `workers/payout_submitter.py:83,96` → registry; `ONCHAIN_SUFFIX` heuristic and `_chain_balance` special case in `workers/reconciliation.py` → `has_btcpay_wallet` flag + `CustodySource` protocol.
- Conformance suite shipped **inside the package** at `src/crypto_processing_api/testing/contracts.py` (abstract pytest classes: `AutomatedBackendContract`, `OperatorBackendContract`, `FeePolicyContract`, `CustodySourceContract`, `EndToEndLedgerContract`). Retrofit proof: `BtcpayPayoutBackend` (vs `FakeBTCPay`) and `ManualTronBackend` (vs `tests/fake_tron.py`) pass their contracts before R4 starts.
- `docs/extending.md` authored per the §6 outline (honesty section: BTCPay deposit rail deliberately fixed; what stays hardcoded). Moves into the docs-site IA at R8 as `docs/extending/adding-an-asset.md`.
**Definition of done:** zero `if asset.id ==` routing outside the registry (semgrep-checkable); both existing backends pass the conformance suite; all 7 drills unchanged and green; migration 0006 passes round-trip and v0.1.0-dump upgrade tests; behavior byte-identical for BTC and USDT (no wire change).
**Effort:** 5–7 days.

### R4 — Lightning (`BTC_LN`): the contract proven
**Executes:** extension design §3, §5.
**Scope:** separate asset with its own accounts (zero ledger changes — on-chain BTC and channel BTC are different floats); BOLT11 destination validator (amount-match-exact, expiry > approval horizon; bech32 machinery already in `core/addresses.py`); `FlatFee` policy; `BtcpayPayoutBackend` reused with the `BTC-LN` payout method (the reuse *is* the proof); `LightningNodeCustody` via Greenfield lightning balance endpoint; `pooled_addresses=False`, `has_btcpay_wallet=False`. Regtest: two pinned LND nodes + `scripts/dev/ln_bootstrap.sh` + bootstrap changes in `deploy/docker-compose.regtest.yml`; drills 8–11 (LN deposit instant-settle; LN withdrawal; liquidity exhaustion with no automatic release; expired BOLT11 → clean failed). LN backend passes `AutomatedBackendContract` **unmodified** — that run is the acceptance test of R3.
**Definition of done:** drills 1–11 green on regtest; `docs/extending.md` gains the worked-example section linking the actual `BTC_LN` commits; runbook note for operator on-chain⇄LN rebalancing.
**Effort:** 7–11 days.

### R5 — Nightly regtest e2e on the homelab (security-critical)
**Executes:** robustness design workstream 4, unchanged (design committed to in §3 below).
**Scope:** private ops repo `OliverD25/crypto-processing-api-nightly` holding the only runner registration; schedule+dispatch-only workflow checking out the public repo's `main`; ephemeral containerized runner + sibling dind with resource fences on the shared Ubuntu box; ntfy failure alert + GitHub-hosted dead-man's switch. Nightly runs: full stack boot → `scripts/bootstrap_btcpay.py` → drills 1–11 (all of them, including R4's LN drills) → `HYPOTHESIS_PROFILE=nightly pytest tests/integration` → teardown + prune. Optional `pip-audit` step lives here, not on the PR path. Runner-isolation statement added to `SECURITY-AUDIT.md` and the homelab manual.
**Definition of done:** three consecutive green nightlies; a simulated failure produces an ntfy within the run; a simulated 26-hour silence trips the dead-man's switch; documented in SECURITY-AUDIT.md.
**Effort:** 2–3 days + one homelab session.
**Position note:** can start any time after R2 (running drills 1–7 nightly has value immediately); reaches its full DoD only after R4 lands. Scheduled here to avoid re-doing acceptance twice.

### R6 — OpenAPI hardening + community kit + positioning (adoption track opens)
**Executes:** DX design workstreams A, E, F. A is the hard prerequisite for everything downstream (all 22 routes are `response_model=None` today, so `/openapi.json` has no response schemas — any SDK generated now would type responses as `Any`). Sequenced **after R3** so the spec is cut once, not churned.
**Scope:**
- *A:* `api/schemas.py` response models mirroring the serializers field-for-field (round-trip test pins them); `response_model=` + `operation_id` on every route; `Idempotency-Key` header and error envelope documented in OpenAPI; `scripts/export_openapi.py` → committed `docs/reference/openapi.json`; CI drift gate (`git diff --exit-code`).
- *E:* issue forms (bug / feature / **operator-report** — earned by `CONTRIBUTING.md:117`), `config.yml` with blank issues off, PR template inlining the 9 money-path invariants as a required section, `dependabot.yml` (pip + actions + npm, security immediate, versions monthly grouped), `CODE_OF_CONDUCT.md`, `ROADMAP.md` (v0.2 workstreams with checkboxes; "not planned": **multi-tenancy, hosted service, non-BTCPay deposit rails, external audit pre-1.0** — Lightning removed from this list per the resolved conflict), Discussions categories, the ~10 verified good-first-issues (list per DX plan; issue #3's TS webhook example is explicitly interim-until-SDK), `docs/reference/versioning.md` (policy text in §5 below).
- *F:* README top-fold rewrite (pitch, who-for / who-NOT-for, comparison table) — with the "you need Lightning" line **removed** from who-NOT-for and the table's honest-custody framing kept verbatim.
**Definition of done:** committed spec is deterministic and drift-gated; a route change without spec regen fails CI; community files render on GitHub; issues filed and labeled.
**Effort:** 3–4 days.

### R7 — SDKs (PyPI + npm)
**Executes:** DX design workstream B, unchanged (decisions in §4 below).
**Scope:** `sdks/python` (openapi-python-client generated core + facade) and `sdks/typescript` (`@hey-api/openapi-ts` core + facade); the two mandatory handwritten features — webhook verification porting `core/signing.py:58-81` (raw-bytes, 300 s window, constant-time) with `parse_event` typed over the 8 event types from `services/events.py`, and idempotency ergonomics (auto-minted UUID key, same-key retry honoring `Retry-After`, encoding `docs/integrating.md`'s rule); `sdks/signature-vectors.json` generated by the server's own `sign_platform_payload` and asserted by all three test suites; `.github/workflows/sdk.yml` (PR: regen + diff-fail + tests; tag: publish — PyPI trusted publishing OIDC, npm `--provenance` with one scoped token). One version number: `v0.2.0` tag releases image + both SDKs.
**Definition of done:** both packages installable from the real registries at a pre-release version; signature vectors pass in all three implementations; a spec change on a PR fails `sdk.yml` until regenerated.
**Effort:** 4–5 days.

### R8 — Docs site
**Executes:** DX design workstream D, unchanged.
**Scope:** MkDocs-format markdown built with pinned `mkdocs-material` (maintenance-mode risk contained by the no-customization rule; Zensical re-evaluated at v0.3); IA per the DX plan with `git mv` preserving history; `docs/extending.md` from R3/R4 moves to `extending/adding-an-asset.md`; Scalar CDN embed over the committed `openapi.json`; generated `reference/configuration.md` from `Settings` with its own drift gate; `reference/webhooks.md`; no `mike`, no versioned docs pre-1.0 (banner points at release tags); `docs.yml` workflow (`--strict` on PR, Pages deploy on main).
**Definition of done:** site live on GitHub Pages; `mkdocs build --strict` green (no broken links from the moves); both generated pages drift-gated.
**Effort:** 2–3 days.

### R9 — Example integration app
**Executes:** DX design workstream C, unchanged. Last in the adoption track because it consumes the published Python SDK and validates the whole story.
**Scope:** `examples/platform-demo/` — single-file FastAPI + Jinja2 + HTMX, ~350-line `app.py` written as a tutorial: fake login, BTC deposit with status polling, balances (with the "API is the source of truth" comment), withdrawal, and a `/platform-webhook` endpoint implementing the 5-step contract from `docs/integrating.md:456-462` with numbered comments. Wired as an opt-in `profiles: ["example"]` service in `deploy/docker-compose.regtest.yml` (drills untouched). CI: container builds.
**Definition of done:** full deposit→balance→withdrawal loop demonstrable on regtest with `--profile example`; `docs/integrating/example-app.md` links it.
**Effort:** 2–3 days.

### R10 — Live TRON Nile verification (user-gated, parallel to everything)
**Executes:** robustness design workstream 5, unchanged. No code-path dependencies on any other milestone (one ~15-line `symbol()/decimals()` helper in `gateway/trongrid.py`, one script, doc edits) — schedule purely around the user's availability, ideally before the v0.2.0 tag so the release notes can claim it.
**Scope:** `docs/runbook-nile-verification.md` (user's ~2–3 h manual steps: TronGrid key, two Nile wallets, faucet, USDt plugin UI setup per `deploy/docker-compose.nile.override.yml:14-35`); `scripts/verify_nile.py` (preflight confirming `USDT_CONTRACT_NILE` live and `USDT_CONTRACT_MAINNET` via read-only mainnet `symbol()/decimals()`; guided deposit drill; guided withdrawal drill with live full-tuple verification and 19-block confirmation; negative duplicate-txid check). Afterward: downgrade the four "format-verified only" caveats with date+txid; `docs/verification-log.md`; fix `tests/fake_tron.py` if any real payload shape differs, with captured-payload regression.
**Definition of done:** preflight assertions passed, one live deposit and one live withdrawal end-to-end, verification log committed and referenced from SECURITY-AUDIT.md.
**Effort:** 1 day + the user's session.

### R11 — Release v0.2.0
**Scope:** CHANGELOG with Breaking/Migration section (the `release.yml` awk extraction already enforces presence); migration 0006 upgrade path exercised by R2's frozen-dump test; produce `tests/fixtures/upgrade/v0.2.0.sql` per the new release checklist; tag → image + PyPI + npm publish from one version; ROADMAP checkboxes closed; announcement in Discussions.
**Definition of done:** a v0.1.0 deployment upgraded to v0.2.0 by pull + `alembic upgrade head` + restart, verified by the dump test and one manual staging pass.

**Total effort:** ~32–44 focused days; roughly a quarter calendar for one part-time maintainer with the two tracks interleaved.

---

## 2. The asset-extension contract (committed)

An asset is **one DB row (data) plus one registry entry (behavior)**, four facets:

1. **Deposit rail — deliberately not pluggable.** Every deposit is a BTCPay invoice through `apply_invoice_state` (`services/deposits.py:454`), the single proven transition path. New assets declare data: `btcpay_payment_method`, `invoice_currency`, `pooled_addresses`, `deposit_expiry_minutes` (new `assets` columns, migration 0006), plus a payment-method matcher in the registry. Non-BTCPay rails are out of scope and the docs say so — no protocol theater with one implementation.
2. **Withdrawal backend — two honest protocols.** `AutomatedWithdrawalBackend` (initiate / poll_status / cancel / find_for_withdrawal — crash recovery is part of the contract) and `OperatorWithdrawalBackend` (new_reference / verify_broadcast / confirmations). `BackendPayout` normalizes to canonical states so `_PAYOUT_STATE_MAP` stops leaking BTCPay strings. State machine, hold/release legality, velocity caps, and all postings remain in `services/withdrawals.py` — backends never decide.
3. **Fee policy — one method.** `FeePolicy.quote(gross) -> FeeQuote`; `DynamicChainFee` and `FlatFee` wrap the existing `fees.py` functions verbatim, no math changes.
4. **Reconciliation hooks.** `CustodySource.balance() -> int | None` (None = unavailable, never 0) per asset; capability flags `has_btcpay_wallet` and `sweep: automated|operator` replace the `-CHAIN` suffix heuristic and the USDT special case.

**Registration:** explicit in-code registry (`services/asset_registry.py`, `AssetProfile`, `build_registry`), mypy --strict-checked, startup fails loudly on an enabled asset without a profile. Rejected: entry points (invisible in review, serves packages not forks), DB-stored behavior names (version-skew failure mode).

**Deliberately fixed and documented:** BTCPay as sole deposit orchestrator/webhook source; `post_entry` as the only postings writer (now semgrep-enforced); both status matrices; BTC as required anchor; `decimals BETWEEN 0 AND 8`.

**Executable form of the contract:** the in-package conformance suite (`crypto_processing_api/testing/contracts.py`), which both existing backends must pass before, and the new LN backend must pass unmodified after. **Proof asset: Lightning (`BTC_LN`)** as a separate asset with its own accounts — the honest custodial model, zero ledger changes.

---

## 3. Self-hosted-runner security design (committed)

**Threat:** public repo + fork PRs can rewrite workflow files; any runner registered to the public repo is one absent-minded approval away from executing attacker code on a box that runs unrelated production workloads. Runner groups don't exist on personal accounts.

**Structural answer — the runner is never registered to the public repo.** A private ops repo (`OliverD25/crypto-processing-api-nightly`) holds the only runner registration. Fork-PR code cannot reach it for the same reason it cannot reach any other repo's runners: no registration exists. This is not a policy that can be misconfigured back into danger.

- Ops-repo workflow: triggers `schedule` (03:17, quiet hours) + `workflow_dispatch` **only**; `actions/checkout` pinned to the public repo's `main` — only maintainer-merged code ever executes. `concurrency` group, 60-minute timeout, `down -v` + prune in `always()`.
- Runner deployment: ephemeral containerized runner (`EPHEMERAL=1`) + **sibling dind daemon** — the production Docker socket is never mounted; the dind carries the resource fence (`cpus: 3`, `mem_limit: 8g`, `pids_limit`, dedicated data-root volume) and the runner container gets `cpus: 1` / `mem_limit: 1g`. Compose file documented as owned-by-nightly-e2e in the homelab manual.
- Notifications: `if: failure()` ntfy curl (ops-repo secret) **plus** a GitHub-hosted dead-man's-switch workflow at 08:00 that alerts if the last successful nightly is older than 26 h — catches runner-offline, token expiry, and GitHub's 60-day schedule auto-disable, all the silent failure modes.
- The design does not rely on checkout@v7's fork-PR refusal or on approval settings; those are defense-in-depth at most. Runner-isolation statement recorded in `SECURITY-AUDIT.md`.

---

## 4. SDK, docs-site, and example-app decisions (committed)

| Decision | Choice | Rejected alternatives |
|---|---|---|
| SDK strategy | Generated core + thin handwritten ergonomics layer (~300 lines/language) | Fully handwritten (silent drift); Fern/Stainless/Speakeasy (vendor-account dependency for a one-maintainer MIT project) |
| TS generator | `@hey-api/openapi-ts` | openapi-generator (Java toolchain) |
| Python generator | `openapi-python-client` (httpx, sync+async, `py.typed`) | — |
| Handwritten surface | Webhook verify (port of `core/signing.py:58-81`) + typed `parse_event` over the 8 event types; auto idempotency key with same-key retry honoring `Retry-After` | — |
| Cross-language correctness | `sdks/signature-vectors.json` generated by the server's `sign_platform_payload`, asserted by server + both SDK suites | — |
| Publishing | PyPI trusted publishing (OIDC), npm `--provenance` + one scoped token; tag-triggered in `sdk.yml`/`release.yml` (already `id-token: write`) | Long-lived PyPI tokens |
| Versioning | SDKs share the repo version; one `v0.2.0` tag ships image + both SDKs; skip-publish-if-unchanged | Independent SDK versions |
| Docs tooling | Plain markdown + boring `mkdocs.yml`, pinned `mkdocs-material`, zero theme customization → Zensical-ready; re-evaluate at v0.3 | mike/versioned docs (deferred to 1.0); server-side OpenAPI plugins |
| API reference | Scalar CDN embed over committed, drift-gated `docs/reference/openapi.json`; curated `api.md` kept | — |
| Generated pages | `reference/configuration.md` from `Settings` + `openapi.json`, both CI drift-gated | Hand-maintained config docs |
| Example app | `examples/platform-demo/` — single-file FastAPI + Jinja2 + HTMX using the Python SDK, opt-in compose profile on the regtest stack | Static page (can't receive webhooks); separate repo |

Prerequisite for all of it: OpenAPI hardening (R6-A) — response models, operation_ids, documented idempotency header, committed deterministic spec, CI drift gate.

---

## 5. Backward-compatibility statement for v0.1.0 deployments

- **Upgrade path:** pull the v0.2.0 image, `alembic upgrade head`, restart. Migration 0006 is additive with server defaults that reproduce v0.1.0 behavior exactly (`pooled_addresses` default false with USDT data-migrated to true; `invoice_currency` backfilled; `deposit_expiry_minutes` NULL = current default). No operator config changes required unless enabling Lightning.
- **API:** no wire-format changes in v0.2. Response models are field-for-field copies of the current serializers (pinned by round-trip tests, string amounts and key order included); `operation_id`s and OpenAPI metadata are additive.
- **Proven, not promised:** R2's frozen `tests/fixtures/upgrade/v0.1.0.sql` test makes "v0.1.0 upgrades cleanly" a permanent CI invariant — a future migration that would rewrite append-only history fails loudly against the triggers. Every release adds its own frozen dump.
- **Policy (in `docs/reference/versioning.md`):** pre-1.0 semver — minors may change behavior with a mandatory Breaking/Migration changelog section (already enforced by `release.yml`'s extraction); patches never require operator action beyond pull + restart; endpoint/field deprecation marked in OpenAPI for ≥1 minor before removal; no change to amount encoding or the signature scheme without a coexisting `v2=` signature version; migrations alembic-only, always forward, every release upgradeable from the previous; minors ~quarterly, security patches as needed.
- **v0.1.1** (R1) is a pure defect fix: no schema change, no API change.

---

## 6. Resolved conflicts

| # | Conflict | Sources | Resolution |
|---|---|---|---|
| 1 | Lightning: proof asset vs "not planned" | Extension §3 vs DX workstreams E (ROADMAP not-planned list) and F (who-NOT-for line, comparison table) | **Lightning is in scope** as the v0.2 proof asset. DX positioning artifacts amended: Lightning removed from not-planned; README gains "Lightning: yes, as `BTC_LN` — a separate custodial float." The DX plan's "not planned" mechanism itself is kept (it now lists multi-tenancy, hosted service, non-BTCPay rails). |
| 2 | Who authors the extension doc, and where it lives | Extension §6 (`docs/extending.md`) vs DX IA (`extending/adding-an-asset.md`) | Authored in R3/R4 as `docs/extending.md` (the asset workstream owns the content, as the DX plan itself stipulated); `git mv` into the site IA at R8. |
| 3 | Build order: contract-first vs guardrails-first | User's ordering instruction ("contract refactor early") vs robustness build order (static analysis #1, "semgrep postings-writer should exist before the asset-extension work") | Both honored: R2's guardrails are short (≤7 days) and land immediately before R3; the contract refactor is still the first major milestone and everything downstream builds on it. The hotfix (R1) precedes everything because it is a live defect. |
| 4 | Nightly e2e content | Robustness §4 (drills + nightly Hypothesis) vs extension §5 (drills 8–11) | Merged: nightly runs drills 1–11 + `HYPOTHESIS_PROFILE=nightly` integration suite. R5's full DoD gates on R4, but the nightly may start earlier with drills 1–7. |
| 5 | Migration-upgrade CI test ownership | DX versioning policy references it; robustness designs it | Robustness (R2) owns implementation; `versioning.md` (R6) references it. |
| 6 | Dependabot configuration | Robustness (security updates on; pip-audit optional) vs DX (`dependabot.yml` monthly grouped, pip+actions+npm) | Merged: security updates immediate (native), version updates monthly grouped across all three ecosystems, `pip-audit` in the nightly workflow only. |
| 7 | TS webhook-verification doc example vs SDK helper | DX good-first-issue #3 vs workstream B | Kept as an interim good-first-issue, explicitly labeled as becoming the SDK doc snippet once R7 ships (the DX plan already said this; recorded so it isn't filed as permanent). |
| 8 | OpenAPI spec timing vs contract refactor | DX says "A ships first"; extension refactor touches `api/withdrawals.py`, `api/admin.py` | Spec is cut **after** R3 (R6 position) so the committed `openapi.json` is generated once against the post-refactor routers. R3 makes no wire changes, so this is churn avoidance, not correctness. |
| 9 | Leaks 8/9: part of refactor vs standalone | Extension §4 step 5 vs its own §7 note | Standalone v0.1.1 (R1), exactly as the extension design's stronger statement recommends; R3 re-verifies via regression tests. |
| 10 | Bandit | "security static analysis" in the goal vs robustness finding ruff-S already covers it | Skipped; CodeQL + custom Semgrep + ruff-S is the committed set. Recorded so "add bandit" doesn't resurface. |

---

## 7. Deferred (nothing silently dropped)

| Item | Raised by | Reason deferred |
|---|---|---|
| Non-BTCPay deposit rails (`DepositRail` protocol) | Extension §2.1 | One implementation would be contract theater; documented as fixed in `extending.md`. Revisit if a real second rail appears. |
| Litecoin support | Extension §3 (evaluated) | Community-plugin liability against pinned BTCPay 2.4.2; proves nothing LN doesn't. Available to a fork via the contract. |
| Entry-point / config-file asset registration | Extension §2.5 (rejected) | Invisible in review, serves packages not forks; explicit registry chosen. |
| 18-decimal assets (relax `decimals` CHECK) | Extension §2.6 | Deliberate future migration; out of v0.2. |
| Automated on-chain⇄LN rebalance (`EntryKind`) | Extension §3 | Operator runbook action for now; a future EntryKind only if automated. |
| Versioned docs (`mike`) | DX D | Doubles publish complexity pre-1.0; banner + release-tag browsing instead. Revisit at 1.0. |
| Zensical migration | DX D | Material pinned and viable through Nov 2026; re-evaluate at v0.3. |
| Fee estimate-vs-actual drift journaling | DX issue #9 | Money-path; filed as `help wanted` issue, not a v0.2 workstream diff. |
| Per-amount confirmation tiers | DX issue #10 / `docs/runbook-reorg.md` Phase 2 | Same — filed as `help wanted`. |
| Prometheus `/metrics`, structured error codes, `list-api-keys`, signature-spike alert wiring, admin response models, `.env.example` check | DX issues #1–2, #6–8, #11 | Filed as good-first-issues in R6; landed if/when contributors take them, not release-gating. |
| Regenerating old schemas from git tags in CI | Robustness §2 (rejected) | Bit-rots with old dependency pins; frozen dumps chosen. |
| Org migration / GitHub runner groups | Robustness §4 (rejected) | Buys nothing over the ops-repo pattern on a personal account; complicates public-repo history. |
| Fern / Stainless / Speakeasy SDK generators | DX B (rejected) | Vendor-account dependency in the release pipeline. |
| SQLite for property tests | Robustness §1 (rejected) | The CHECK constraints and `ux_entry_source` are part of the system under test. |
| pip-audit on the PR path | Robustness §3 | Nightly-only; Dependabot covers the PR path. |

---

## 8. Open questions for the user (genuinely user-owned)

1. **Nile session (R10):** when can you schedule the ~2–3 hour manual session (TronGrid account + API key, two Nile wallets, faucet funding, USDt plugin UI setup, sending the two payments)? R10 floats — but doing it before the v0.2.0 tag lets the release claim live-verified TRON.
2. **Homelab (R5):** confirm you approve creating the private `crypto-processing-api-nightly` repo and installing the ephemeral runner + dedicated dind compose stack on the shared Ubuntu box, and confirm 03:17 local is genuinely off-peak for its other workloads. Also: which ntfy topic/URL should the ops repo use as its secret?
3. **Registry accounts (R7):** package name `crypto-processing-client` needs to be claimed on both PyPI and npm under your accounts, the PyPI trusted publisher registered against `OliverD25/crypto-processing-api` + `sdk.yml`, and one scoped npm automation token created. Do you want a different package name (e.g. scoped `@oliverd25/...` on npm) if the bare name is taken?
4. **Repo settings (R2/R6/R8):** enabling CodeQL default setup, Dependabot, GitHub Discussions, and GitHub Pages are Settings-page clicks only you can make — OK to list these as your checklist items at each milestone?
5. **Code of Conduct contact (R6):** confirm `muzexp@gmail.com` as the published enforcement contact, or provide an alias.
6. **Scope sign-off on conflict #1:** the plan commits Lightning into v0.2 (reversing the DX draft's "not planned" positioning). This is a product-scope call — confirm, or the fallback is Litecoin (~1 week, weaker proof, plugin-compatibility liability) with Lightning re-deferred.

### Critical Files for Implementation
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\withdrawals.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\backends.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\workers\reconciliation.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\.github\workflows\ci.yml
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\core\signing.py