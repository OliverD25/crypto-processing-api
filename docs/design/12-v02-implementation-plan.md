# crypto-processing-api v0.2 — Adoption & Robustness Roadmap

## Context

v0.1.0 is built, tested, published (github.com/OliverD25/crypto-processing-api, GHCR images, CI green). The user's goal now: make it a project **other developers adopt** — deploy-as-is first (released image next to BTCPay, integrate via API), fork-friendly second. This plan was produced by a 5-agent design workflow (3 lenses reading the real code → synthesis → adversarial critique, verdict "sound after fixes") with all critique amendments folded in. The critique also found **two live defects in v0.1** — fixed first as v0.1.1.

Design documents from the workflow output
(`C:\Users\Admin\AppData\Local\Temp\claude\E--codespace--claude-code--swift-punk-projects-crypto-processing-api\348375ed-1947-4db3-a5d0-764e772a7f68\tasks\w6x54lpyj.output`,
JSON keys `contract / robustness / dx / mergedPlan / critique`) get salvaged into `docs/design/` as files 07–11 at R1 start.

## User decisions

- Deploy-as-is primary; fork-friendly secondary. Full DX set: docs site, Python + TypeScript SDKs, example app, community kit.
- Extension contract formalized and proven by adding one asset: **2-day Lightning spike first; Litecoin fallback pre-agreed**.
- All four robustness tracks (property tests, static analysis, homelab nightly e2e, live Nile) — staged as below.
- Homelab runner: **approved** (private ops repo + fenced runner on the shared Ubuntu box; check the homelab manual before touching the box).
- Nile session: user **committed** (~2–3h manual: TronGrid key, faucet, plugin UI). Hard gate before the v0.2.0 tag.

## Milestones (build order; two tracks after R3)

### R1 — v0.1.1 hotfix (live defects; ships alone, immediately)
1. `due_for_submission` (`services/withdrawals.py:835`) and `due_for_polling` (:845) gain `backend == BACKEND_BTCPAY_PAYOUT` filters (manual-TRON rows are today submitted to BTCPay / polled against Greenfield).
2. **Deadlock prevention** (critique 1a): `usdt_auto_withdraw=true` becomes a startup `ValueError` in `config.py` until an automated TRON signer exists — an auto-approved manual row is born `APPROVED`, and no code path can ever submit it (admin approve requires `PENDING_APPROVAL`). Plus a runbook section: SQL remediation for rows already stranded `approved`/`submitting` on existing deployments.
3. **Job C USDT custody defect** (critique 1b): `build_jobs` (`workers/runner.py:180`) never passes `tron=` — USDT insolvency has never been computed by the background job. Wire it (mirror the `tron_configured` gate); regression test asserts a `trongrid`-sourced custody line when TRON is configured.
4. Regression tests for all three; tag v0.1.1; CHANGELOG; nothing else rides along.

### R2 — Robustness guardrails (before touching money code)
- **Static analysis**: CodeQL default setup (repo setting via gh API); Dependabot (security immediate; monthly grouped versions for pip+actions+npm); Semgrep CE pinned, `--error`, custom `.semgrep/` rules — `postings-writer` (no `Posting` writes outside `ledger/service.py`; must distinguish writes from legit reads like `withdrawals.py:490`), `no-raw-sql-in-money-path`, `no-float-amounts` + `p/python`. Skip bandit (ruff `S` already on). `SECURITY-AUDIT.md` keyed 1:1 to the threat table in docs/security.md.
- **Migration robustness**: schema round-trip test (upgrade→schema-only normalized dump→downgrade→assert clean→upgrade→identical dump — normalized: sorted, fixed pg version, no volatile values); `alembic check` + single-head CI step; frozen seeded dump `tests/fixtures/upgrade/v0.1.0.sql` + `scripts/make_upgrade_fixture.py` + upgrade test (load → `upgrade head` → `assert_ledger_consistent` + frozen balances). Release checklist: every release adds its dump.
- **Property-based ledger tests**: Hypothesis `RuleBasedStateMachine` against real Postgres driving `post_entry` + the withdrawal posting matrix (deposit/hold/submit/settle/unsubmit/release/replay/crash rules). **Single-threaded only** (critique: thread races in Hypothesis rules are non-reproducible; races stay in the existing `test_ledger_concurrency.py`). Profiles: `ci` (max_examples=15, derandomized), `nightly` (300, random seed); `.hypothesis/` artifact on failure.
- DoD: a deliberately-introduced rogue `Posting` write fails semgrep; a deliberately-broken downgrade fails round-trip; nightly-volume property run finds nothing for a week (run locally until R5 exists).

### R3 — Asset-extension contract refactor + conformance suite
An asset = **one DB row (data) + one registry entry (behavior)**:
- `services/asset_registry.py`: `AssetProfile` (fee_policy, destination_validator, withdrawal_backend name, custody_source, payment_method_matcher, has_btcpay_wallet, sweep mode, required flag) + `build_registry(settings)`; startup fails loudly on enabled-asset-without-profile. Explicit in-code registry (mypy-checked) — NOT entry points, NOT DB-stored behavior names.
- Migration `0006`: `pooled_addresses`, `invoice_currency`, `deposit_expiry_minutes` columns on `assets`, server defaults reproducing v0.1.0 exactly (first customer of R2's upgrade tests).
- Protocol split in `services/backends.py`: `AutomatedWithdrawalBackend` (initiate/poll_status/cancel/**find_for_withdrawal** — crash recovery is part of the contract) and `OperatorWithdrawalBackend` (new_reference/verify_broadcast/confirmations); `BackendPayout` canonical-state dataclass so `_PAYOUT_STATE_MAP` stops keying on BTCPay literals; `ManualTronBackend` finally type-checks.
- Kill the BTCPay leaks (extension design §7): `INVOICE_CURRENCY`/`_expiry_minutes`/`POOLED_ASSETS` dicts → columns; `_matches`/`REQUIRED_ASSETS` → registry; `validate_destination` if/elif → registry; fee/backend routing in `api/withdrawals.py`, `api/admin.py`, `workers/payout_submitter.py` → registry; `ONCHAIN_SUFFIX` heuristic + `_chain_balance` USDT special case → `has_btcpay_wallet` + `CustodySource` protocol (`balance() -> int | None`, None ≠ 0).
- **Anti-fake-drift** (critique 4): recorded-payload corpus — real Greenfield payout/invoice JSON captured from the regtest stack, committed as fixtures, asserted through the normalizer independently of `FakeBTCPay`. "Unknown payout state → logged no-op" becomes an explicit conformance case.
- Conformance suite **inside the package** (`crypto_processing_api/testing/contracts.py`): `AutomatedBackendContract`, `OperatorBackendContract`, `FeePolicyContract`, `CustodySourceContract`, `EndToEndLedgerContract`. Both existing backends must pass before R4.
- `docs/extending.md` (the "add your own asset" guide; honest about what is deliberately fixed: BTCPay as sole deposit rail/webhook source, post_entry, status matrices, BTC anchor).
- DoD: zero `if asset.id ==` routing outside the registry (semgrep-checked); drills 1–7 unchanged; BTC/USDT behavior byte-identical (no wire change); migration 0006 passes both R2 gates.

### R4 — Proof asset: 2-day Lightning spike, then build
**Spike (time-boxed 2 days, regtest)**: (1) can a BTCPay top-up/LNURL invoice deliver Lightning deposits through `apply_invoice_state` (what does `destination` contain, how are payments attributed)? (2) Lightning payout-processor failure/expiry semantics (does `AwaitingPayment` retry forever?). (3) Can `bootstrap_btcpay.py` configure it headlessly? → decision memo.
**If Lightning**: separate `BTC_LN` asset with own accounts (separate custodial float — stated honestly in ALL positioning); BOLT11 validator; `BtcpayPayoutBackend` reuse via `BTC-LN` payout method; `LightningNodeCustody`; regtest LND nodes + drills 8–11 (instant settle; LN withdrawal; liquidity exhaustion; expired invoice). **Scope pulled in from the critique**: fee-drift journaling (routing fees booked to `network_fee_expense` — not deferrable for LN), a `SUBMITTED` timeout / definitive-failure semantics (backend capability `definitive_failure_proof` gating narrow auto-release for cryptographically-definitive failures), LN payloads added to the webhook schema (R6).
**If Litecoin**: LTC via BTCPay chain plugin, regtest litecoind, same conformance-suite proof; deferred-Lightning noted in ROADMAP.
DoD either way: the new backend passes the conformance suite **unmodified** — that run is R3's acceptance test; `docs/extending.md` gains the worked example linking real commits.

### R5 — Nightly e2e on the homelab (security-critical; may start after R2 with drills 1–7)
- Private ops repo `OliverD25/crypto-processing-api-nightly` holds the ONLY runner registration (fork-PR code structurally cannot reach it). Workflow: `schedule` (03:17) + `workflow_dispatch` only; checks out public `main`; concurrency group; 60-min timeout; `down -v` + prune in `always()`.
- Runner on the box (read the homelab-remote-control manual first): **ephemeral runner via JIT config** (`--jitconfig` minted by a host-side script — no PAT in the runner container's env; fine-grained PAT scoped to the ops repo only, held on the host) + sibling dind (production Docker socket never mounted). Fences: dind `cpus: 3`, `mem_limit: 8g`, `pids_limit`, **size-bounded dedicated data-root volume + post-run free-space assertion**; runner `cpus: 1` / `mem 1g`.
- **Network egress lockdown** (critique 2.1): nightly stack on an internal-only bridge; dind egress allows registries + GitHub (+ TronGrid if needed), **drops RFC1918** — nightly code can never reach the LAN. Images pinned **by digest**; pip installs from a hash-locked constraints file.
- **Watchdog** (critique 2.3): dead-man's-switch workflow in the ops repo on GitHub-hosted runners (plain GITHUB_TOKEN); nightly's last step commits a heartbeat file (keeps both schedules alive past GitHub's 60-day auto-disable); alerts if last green nightly > 26h. Failure alerts via ntfy (random-suffixed topic, ops-repo secret).
- Nightly content: full stack boot → bootstrap → drills 1–11 → `HYPOTHESIS_PROFILE=nightly` integration suite → **example-app loop (once R9 exists)** → pip-audit → teardown. Runner-isolation statement in SECURITY-AUDIT.md + homelab manual.

### R6 — OpenAPI hardening + community kit + positioning (adoption track; after R3 so the spec is cut once)
- `api/schemas.py` response models — **amounts and timestamps typed `str`** (no pydantic re-serialization); round-trip test compares **raw bytes** of old vs new output (critique 5.2); `response_model=` + `operation_id` on all routes; Idempotency-Key + error envelope in OpenAPI; `scripts/export_openapi.py` → committed `docs/reference/openapi.json` + CI drift gate.
- **Webhook payload schemas** (critique 5.1): typed models for all 8 outbound event types, JSON Schema exported next to openapi.json, same drift gate — closes the only unmonitored contract surface; R7's `parse_event` generates from it.
- Community kit: issue forms (bug/feature/operator-report; blank issues off), PR template inlining the 9 money-path invariants, dependabot.yml, CODE_OF_CONDUCT (contact muzexp@gmail.com), ROADMAP.md ("not planned": multi-tenancy, hosted service, non-BTCPay deposit rails, external audit pre-1.0), Discussions categories, ~10 verified good-first-issues, `docs/reference/versioning.md` (pre-1.0 semver; Breaking/Migration changelog section mandatory; migrations forward-only, every release upgradeable from previous).
- README top-fold rewrite: pitch, who-for/who-NOT-for, honest comparison table (raw BTCPay / hosted processors / build-yourself).

### R7 — SDKs (Python → PyPI, TypeScript → npm)
- Generated core + thin handwritten facade (~300 lines/language): Python `openapi-python-client` (httpx, sync+async, py.typed); TS `@hey-api/openapi-ts`.
- Mandatory handwritten surface: webhook verification (port of `core/signing.py:58-81` — raw bytes, 300s window, constant-time) + `parse_event` typed from the R6 JSON Schema; idempotency ergonomics (auto UUID key, same-key retry honoring Retry-After).
- Cross-language correctness: `sdks/signature-vectors.json` generated by the server's own signer, asserted by server + both SDKs.
- Publishing: PyPI trusted publishing (OIDC), npm `--provenance` with one scoped token; **idempotent per registry (skip-if-version-exists)**; one version — the repo tag ships image + both SDKs. Names: `crypto-processing-client` on PyPI, `@oliverd25/crypto-processing-client` on npm (scoped — bare npm names contested).
- **User checklist (accounts I cannot create)**: PyPI account + trusted-publisher registration; npm account + one automation token as a repo secret.

### R8 — Docs site
MkDocs Material (pinned, zero theme customization), IA: getting-started / integrating / operating / extending / reference / design-record; `git mv` preserves history; `extending.md` → `extending/adding-an-asset.md`; API reference via Scalar over the committed openapi.json — **Scalar JS pinned or vendored, never floating CDN** (critique 5.3); generated `reference/configuration.md` from `Settings` (drift-gated); no versioned docs pre-1.0; `docs.yml` (`--strict` on PR, Pages deploy on main).

### R9 — Example integration app
`examples/platform-demo/`: single-file FastAPI + Jinja2 + HTMX (~350 lines written as a tutorial) using the Python SDK — fake login, BTC deposit with polling, balances, withdrawal, `/platform-webhook` implementing the 5-step contract from docs/integrating.md. Opt-in compose profile on the regtest stack. **Its full loop runs in the nightly** (critique 7: a tutorial that rots fails in front of the worst possible reader).

### R10 — Live TRON Nile verification (HARD GATE between R3 and the v0.2.0 tag)
Framed as the USDT regression check for the refactor (USDT cannot run on regtest; a matcher regression would silently disable USDT deposits with no CI signal). `docs/runbook-nile-verification.md` (user's manual steps: TronGrid key, two Nile wallets, faucet, USDt plugin UI per the nile override); `scripts/verify_nile.py` (preflight: Nile + mainnet contract `symbol()/decimals()`; guided live deposit; guided live withdrawal with full-tuple verification + 19-block confirmation; duplicate-txid negative check). Afterward: downgrade the format-verified-only caveats with date+txid, `docs/verification-log.md`, fix `tests/fake_tron.py` against any real payload differences with captured-payload regressions.

### R11 — Release v0.2.0
CHANGELOG with Breaking/Migration section; `tests/fixtures/upgrade/v0.2.0.sql`; one tag ships image + both SDKs; ROADMAP checkboxes closed; Discussions announcement. Upgrade proof: a v0.1.x deployment upgrades by pull + `alembic upgrade head` + restart (CI-enforced by the frozen-dump test forever).

## Backward compatibility (commitment)

Migration 0006 additive with defaults reproducing v0.1.0 behavior; no wire-format changes in v0.2 (pinned by raw-bytes round-trip tests); v0.1.0→v0.2.0 upgrade is a permanent CI invariant via the frozen dump.

## Repo-settings actions (done via gh where the API allows; else listed for the user)

Enable: CodeQL default setup, Dependabot, Discussions, Pages. Create: private ops repo (approved). File: good-first-issues (R6). User-only: PyPI/npm accounts + publisher registration (R7 gate), Nile manual session (R10), homelab SSH session for the runner install (R5).

## Verification

- Every milestone: full local suite (569+ tests) + ruff + mypy --strict green; drills 1–7 (later 1–11) green on regtest; independent verification by the orchestrator after each coder-agent report.
- R2 gates prove themselves on R3's migration 0006. R3's acceptance = conformance suite passing for both existing backends + byte-identical wire behavior. R4's acceptance = new backend passes the suite unmodified. R5 DoD: three consecutive green nightlies + simulated failure alert + simulated silence trips the watchdog. R10 = live Nile deposit + withdrawal logged. R11 = staged v0.1.x→v0.2.0 upgrade pass.

## Process

Implementation milestone-by-milestone via `coder` agents (self-contained prompts; same agent continued per milestone), incremental commits, orchestrator verifies between milestones. Design docs salvaged to `docs/design/07-11` at R1. Nothing destructive without asking; no history rewriting; the homelab manual is read before any change on the box.
