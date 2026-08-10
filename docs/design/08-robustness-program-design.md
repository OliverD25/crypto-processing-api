# v0.2 Robustness Program — Design

Scope: the five robustness workstreams for `crypto-processing-api` v0.2, designed against the actual v0.1.0 code. All file paths below are repo-absolute under `E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api`.

Context that shaped every design below: `post_entry` in `src/crypto_processing_api/ledger/service.py` is the single postings writer with DB-level backstops (`ux_entry_source`, `no_overdraft`, `no_negative_asset`, append-only triggers); `src/crypto_processing_api/ledger/invariants.py` already provides `assert_ledger_consistent` (entry zero-sum, materialized-vs-derived, per-asset residual = 0) and it already runs after every integration test via the `ledger_stays_consistent` autouse fixture in `tests/integration/conftest.py`. The robustness program's job is to widen *what feeds* those checks, not to invent new ones.

---

## 1. Property-based ledger testing with Hypothesis

### Shape

A `RuleBasedStateMachine` in a new `tests/integration/test_ledger_properties.py`, running against the real PostgreSQL from `deploy/docker-compose.test.yml` — same reasoning as the existing concurrency tests (`tests/integration/test_ledger_concurrency.py`: "a mocked lock proves nothing about a lock"). SQLite is explicitly ruled out by the conftest docstring; the property machine inherits that stance because the CHECK constraints and `ux_entry_source` *are* part of the system under test.

The machine drives `ledger/service.py` and the withdrawal posting matrix documented at the top of `src/crypto_processing_api/services/withdrawals.py` (hold / submit / settle / un-submit reversal / release), keeping a plain-Python model of expected balances alongside.

**Rules (operations):**

| Rule | Calls | Model effect |
|---|---|---|
| `deposit(user, amount)` | `post_entry(DEPOSIT_CREDIT, [(hot,+a),(avail,-a)])` — same shape as `credit_user` in conftest | `avail[user]+=a; hot+=a` |
| `hold(user, amount)` | `post_entry(WITHDRAWAL_HOLD, ...)`; expects `InsufficientFunds` iff `amount > avail[user]` | move avail→hold or no-op |
| `submit(wid)` | `WITHDRAWAL_SUBMIT` with `C = net + miner_fee`; expects `InsufficientFunds` iff `C > hot` | hot→in_flight |
| `settle(wid, fee_mode)` | 4-posting `WITHDRAWAL_SETTLE` per the matrix, both `deduct` and `absorb` | extinguish hold, book fee + network fee |
| `unsubmit(wid)` | `REVERSAL` of the submit entry with `reverses_entry_id` | in_flight→hot |
| `release(wid)` | `WITHDRAWAL_RELEASE` hold→avail | |
| `replay(prior_ref)` | re-`post_entry` an already-used `(kind, source_ref)`; must raise `AlreadyPosted`, model unchanged — this is the webhook-redelivery / poller-race property | no-op |
| `crash()` | perform any of the above but `session.rollback()` instead of commit; model unchanged; DB unchanged | no-op |
| `race_hold(user)` | (nightly profile only) two threads race a hold via the barrier helper pattern from `test_ledger_concurrency.py`; exactly one wins | one hold |

**Invariant block** (`@invariant()` — Hypothesis runs it after *every* rule):
1. `assert_ledger_consistent(session)` — the three existing checks.
2. Model equality: every `user_available`/`user_hold`/`hot_wallet`/`payouts_in_flight`/`fee_income`/`network_fee_expense` balance equals the model's number exactly.
3. Sign discipline: credit-normal balances ≤ 0, debit-normal ≥ 0 (except `external`/`user_deficit` per `UNBOUNDED_ACCOUNT_KINDS` in `ledger/models.py`) — this asserts the CHECKs never *needed* to fire, i.e. application code refuses before the DB does.
4. Entry count matches model op count (catches silent double-posting).

**Strategies:** `amount = st.integers(min_value=1, max_value=10**9)` (shrinks toward 1); users from a fixed pool `st.sampled_from(["u0","u1","u2","u3"])` — a small pool forces account contention and re-use, which is where bugs live; withdrawal ids from a `Bundle` so settle/release can only reference holds the machine actually created; `fee` and `miner_fee` as small bounded ints with `fee <= gross`.

**DB lifecycle:** the autouse `clean_database` fixture runs once per test *function*, but Hypothesis executes many examples inside one function — so the machine's `__init__`/`teardown` must do its own `TRUNCATE ... RESTART IDENTITY CASCADE` + `seed_assets` (copy the exact statements from `clean_database` in `tests/integration/conftest.py:82-97`). TRUNCATE of an empty-ish DB is milliseconds; this is what keeps the budget sane.

### CI time budget

Two Hypothesis profiles registered in `tests/conftest.py`, selected by `HYPOTHESIS_PROFILE` env var:

- `ci` (PR/push): `max_examples=15`, `stateful_step_count=30`, `deadline=None` (DB latency jitter must not fail tests), `derandomize=True` (a PR must not go red on someone else's luck), `print_blob=True`. Cost: roughly 15 × 30 short transactions ≈ 60–90 s — one extra job-minute inside the existing `integration` job in `.github/workflows/ci.yml`.
- `nightly`: `max_examples=300`, `stateful_step_count=50`, random seed, includes the threaded `race_hold` rule. Runs in the nightly workflow (section 4), where an extra 10–15 minutes is free.

### Failure ergonomics

Stateful Hypothesis already shrinks to a minimal printed program (`state = LedgerMachine(); state.deposit(user='u0', amount=1); state.hold(...)`) — the rules take only small ints and short strings precisely so that this printout is directly runnable. `print_blob=True` adds a `@reproduce_failure` blob to the CI log so the maintainer can replay the exact failure locally without the `.hypothesis` example database. Upload `.hypothesis/` as a workflow artifact anyway (one `actions/upload-artifact` step, `if: failure()`) so nightly-found examples survive.

**Effort: 2–3 days** (1 day machine + model, 1 day tuning strategies/profiles so shrinking stays readable, 0.5 day CI wiring). New dev dependency: `hypothesis` (pinned) in `pyproject.toml [project.optional-dependencies].dev`.

---

## 2. Migration-upgrade robustness

Three layers, chosen to not rot:

**(a) Round-trip test — catches broken downgrades and irreversible residue.** The session fixture already does `downgrade(base)` → `upgrade(head)` once (`tests/integration/conftest.py:69-70`), which incidentally proves downgrade-from-head works. Make it deliberate: a test that does `upgrade head` → `pg_dump --schema-only` (normalized: strip comments, sort) → `downgrade base` → assert only `alembic_version` remains → `upgrade head` → dump again → assert the two dumps are byte-identical. Catches a downgrade that forgets an enum value, trigger, or partial index — exactly the objects (`account_kind` enum, append-only triggers, `ux_accounts_user` partial indexes) this schema is full of.

**(b) `alembic check` job — catches model/migration drift.** One CI step (`alembic check` exists since Alembic 1.9; the repo pins 1.19.1) against the service database: fails if `ledger/models.py` and `migrations/versions/0001..0005` have diverged, i.e. if someone edits a model and forgets the migration. Near-zero maintenance. Also assert single head (`alembic heads` count == 1) to catch merge-created branch points.

**(c) Frozen release dumps — proves v0.1.0 upgrades cleanly forever.** The anti-rot property comes from the artifact being *frozen data, never edited*:

1. One-time: check out tag `v0.1.0`, run migrations 0001–0005 against a scratch Postgres 16, execute a small seeding scenario through the real services (two users, a settled BTC deposit, a confirmed withdrawal, a USDT hold — enough to touch every table incl. `deposits`, `withdrawals`, `webhook_events`, `worker_heartbeats`), `pg_dump --no-owner` → commit as `tests/fixtures/upgrade/v0.1.0.sql` (a few hundred KB).
2. Test `tests/integration/test_migration_upgrade.py`: for each file in `tests/fixtures/upgrade/`, load into a fresh database (separate DB name, not the shared `cpapi_test`), run `alembic upgrade head`, then run `assert_ledger_consistent` and a handful of frozen expectations (user balances to the satoshi, entry counts). Because the ledger is append-only, a future migration that would rewrite `postings` fails loudly here — the triggers reject it — which is itself the invariant you want enforced.
3. Release checklist gains one line: "add `tests/fixtures/upgrade/vX.Y.Z.sql` produced by `scripts/make_upgrade_fixture.py`". The script (thin wrapper around steps in 1) is the only maintained code.

The alternative — regenerating old-version schemas from git tags inside CI — was rejected: it needs the old code importable next to the new code and breaks the moment a dependency pin bit-rots. Dumps don't execute old code at all.

**Effort: 1.5–2 days** (0.5 round-trip + `alembic check`, 1–1.5 dump script + fixture + test). Runs in the existing `integration` CI job; adds well under a minute per fixture.

---

## 3. Security static analysis

Current state to build on: ruff already runs the bandit ruleset (`"S"` in `[tool.ruff.lint].select`, `pyproject.toml:70`), and gitleaks scans full history in CI. So **standalone bandit adds nothing — skip it.** Current best practice for a Python/FastAPI service is CodeQL (GitHub-native, free for public repos) plus Semgrep for framework- and project-specific rules; Semgrep's registry carries FastAPI-specific rules that bandit-class tools lack ([Semgrep vs Bandit compared, 2026](https://dev.to/rahulxsingh/semgrep-vs-bandit-python-security-scanning-compared-2026-5e5j), [SAST tool comparison](https://sanj.dev/post/ai-code-security-tools-comparison/), [AppSec Santa SAST roundup](https://appsecsanta.com/sast-tools)).

Minimal high-signal set for one maintainer:

1. **CodeQL default setup** (repo Settings → Code security, `python`, default query suite — *not* the extended suite, which is where alert fatigue lives). Runs on push/PR/weekly with zero YAML to maintain. Effort: 30 minutes.
2. **Dependabot security updates** (native, on): replaces running pip-audit in PR CI. Optionally `pip-audit` in the nightly workflow only, where a new CVE alert costs nothing on the PR path.
3. **Semgrep CE, custom rules first, registry second.** The high-signal move is encoding this repo's *money invariants* as Semgrep rules in `.semgrep/` — architecture conformance, not generic CVE patterns:
   - `postings-writer`: forbid any `Posting(`/`insert(Posting`/`postings` INSERT outside `src/crypto_processing_api/ledger/service.py` — mechanizes the "only module allowed to write postings" docstring.
   - `no-raw-sql-in-money-path`: forbid `text(` / string-built SQL under `src/crypto_processing_api/{ledger,services}/` (threat #3 in `docs/security.md` says "ORM discipline makes this a review problem" — make it a CI problem instead).
   - `no-float-amounts`: forbid `float` on names matching `amount|balance|fee` in `src/`.
   - `lock-before-read`: flag `Account.balance` reads in `services/` not preceded by `lock_user_accounts`/`_lock_accounts` (best-effort pattern; even a coarse version catches the classic regression).
   - Registry: `p/python` only. Run as one CI job: `semgrep scan --config .semgrep/ --config p/python --error`. Pin the semgrep version.
4. **`SECURITY-AUDIT.md`** — a self-assessment keyed 1:1 to the 10-row threat table in `docs/security.md:45-56`. Per threat: *control → where implemented (file) → evidence (named test / CI job) → last verified (date + how) → accepted residual*. E.g. row 7 (double-withdrawal race) → `ledger/service.py:_lock_accounts` + `no_overdraft` → `tests/integration/test_ledger_concurrency.py::test_two_threads_racing_a_debit_only_one_wins` + property machine `race_hold` → date. Plus three new sections: SAST triage log (every dismissed finding gets one line of why), dependency policy (pins + Dependabot), and the runner-isolation statement from section 4. This document is what a prospective adopter reads to decide whether to trust the project — it is DX as much as security.

**Effort: 2 days total** (0.5 CodeQL+Dependabot, 1 custom semgrep rules, 0.5 SECURITY-AUDIT.md).

---

## 4. Nightly regtest e2e on the homelab runner — the security-critical design

### The threat, stated precisely

The repo is public. For `pull_request` events, the workflow files come from the PR's merge ref — **a fork PR can add `runs-on: [self-hosted]` to any workflow it touches**. GitHub's own guidance is that self-hosted runners should "almost never" be attached to public repositories, and the "require approval for outside collaborators" setting is a human gate, not a structural one — one absent-minded approval executes attacker code on the homelab box that also runs production workloads ([GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use), [community discussion #26722](https://github.com/orgs/community/discussions/26722), [StepSecurity on public-repo CI](https://www.stepsecurity.io/blog/defend-your-github-actions-ci-cd-environment-in-public-repositories), [Wiz Actions hardening guide](https://www.wiz.io/blog/github-actions-security-guide)). Runner *groups* with "allow public repositories = off" would be the org-level answer, but this repo lives under a personal account (`OliverD25`), where runner groups don't exist.

### The structural answer: the runner is never registered to the public repo

Create a **private repository** `OliverD25/crypto-processing-api-nightly` (the "ops repo"). The self-hosted runner is registered **only** there. Fork-PR code from the public repo cannot reach it for the same reason it cannot reach any other repository's runners: there is no registration. This is not a policy that can be misconfigured back into danger — the attack path does not exist. (An org + runner group would also work, but moving the repo into an org for this buys nothing over the ops-repo pattern and complicates the public repo's history/URLs.)

The ops repo contains exactly one workflow:

```yaml
name: nightly-e2e
on:
  schedule: [{cron: "17 3 * * *"}]   # box's quiet hours
  workflow_dispatch: {}
concurrency: {group: nightly, cancel-in-progress: false}
jobs:
  e2e:
    runs-on: [self-hosted, homelab-e2e]
    timeout-minutes: 60
    steps:
      - name: Check out the PUBLIC repo, default branch only
        uses: actions/checkout@v5
        with: {repository: OliverD25/crypto-processing-api, ref: main}
      - run: docker compose -f deploy/docker-compose.regtest.yml up -d --build
      - run: python scripts/bootstrap_btcpay.py
      - run: python scripts/dev/smoke_test.py --drill late   # the drills ARE the e2e
      - run: HYPOTHESIS_PROFILE=nightly pytest tests/integration -q   # vs the stack's postgres-ledger
      - if: always()
        run: docker compose -f deploy/docker-compose.regtest.yml down -v && docker system prune -f
      - if: failure()
        run: curl -s -d "nightly e2e FAILED $GITHUB_RUN_ID" "$NTFY_TOPIC_URL"
```

No `pull_request`, no `pull_request_target`, no `workflow_run` — triggers are `schedule` + `workflow_dispatch` only, and the checkout is pinned to the public repo's default branch, so the only code that ever executes is code the maintainer already merged. (Note also the 2026 hardening context: `actions/checkout@v7` now refuses fork-PR checkouts under privileged triggers — [GitHub changelog](https://github.blog/changelog/2026-06-18-safer-pull_request_target-defaults-for-github-actions-checkout/), [securely using pull_request_target](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target) — but the design does not rely on it; it relies on non-registration.)

### Runner deployment on the shared box (defense-in-depth + resource containment)

Because the structural isolation already removes *malicious* code, the remaining risk is *accidental* damage to the co-hosted production workloads: a runaway compose stack eating the box. Design:

- **Ephemeral containerized runner**: `myoung34/docker-github-actions-runner` with `EPHEMERAL=1` and a registration token from the ops repo (labels `homelab-e2e`), managed by a compose file on the box with `restart: always` — GitHub's own recommendation is ephemeral/JIT runners so each job starts clean ([secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use)).
- **A dedicated Docker daemon, not the production one.** The e2e needs to run the whole regtest stack (5 containers, `deploy/docker-compose.regtest.yml`), and `smoke_test.py` drives `docker compose` itself (including stopping/starting the api for the outage drill — see the compose helpers at `scripts/dev/smoke_test.py:54-77`). Give the runner a **sibling dind service** (`docker:dind`) on a private compose network and point `DOCKER_HOST` at it — the production daemon's socket is never mounted. The dind service carries the resource fence: `cpus: "3"`, `mem_limit: 8g` (regtest stack peaks ~3–4 GB), `pids_limit`, and its `data-root` on a dedicated volume so `down -v` + `docker system prune` bounds disk. The runner container itself gets `cpus: "1"`, `mem_limit: 1g`.
- The homelab manual (per the `homelab-remote-control` skill's warning about load-bearing files on that box) gets a section documenting this compose file as owned-by-nightly-e2e.
- The cron fires at 03:17 local — off-peak for whatever else the box does, off the top-of-hour thundering herd on GitHub's schedulers.

### Failure-notification path

Two independent channels, because "the nightly silently stopped running" is the failure mode that actually happens:

1. **Failure alert**: the `if: failure()` ntfy curl above (the operator already runs ntfy/Telegram — `NTFY_TOPIC_URL` mirrors `ntfy_topic_url` in `src/crypto_processing_api/config.py:110`), stored as an ops-repo Actions secret. GitHub's own failure email is the backup.
2. **Dead-man's switch**: a second tiny workflow in the ops repo running on **GitHub-hosted** `ubuntu-latest` (so it works even when the homelab is down) at 08:00: `gh api` the latest `nightly-e2e` run; if the last *successful* run is older than 26 h, ntfy. This also catches GitHub's 60-day auto-disable of scheduled workflows in inactive repos, runner offline after a power cut, and token expiry — everything that fails silent.

**Effort: 2–3 days** (0.5 ops repo + workflow, 1 runner/dind compose on the box + a dry run, 0.5 notifications + dead-man's switch, 0.5 docs: runner-isolation statement in SECURITY-AUDIT.md + homelab manual entry).

---

## 5. Live TRON Nile verification milestone

### What is currently unverified (the two flagged constants)

1. `USDT_CONTRACT_NILE = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"` — flagged "format-verified only, NOT confirmed against a live Nile node" in four places: `src/crypto_processing_api/gateway/trongrid.py:49-52`, `src/crypto_processing_api/config.py:191-193`, `docs/btcpay-setup.md:146-148`, `.env.example:280-281` (and the comment in `deploy/docker-compose.nile.override.yml:50-52`).
2. `USDT_CONTRACT_MAINNET = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"` — `trongrid.py:46` says "verified by round-tripping its hex form through base58check", i.e. also format-only; no live call has ever confirmed it either.

Beyond the constants, every TRON code path is fake-tested only (`tests/fake_tron.py` fakes the network but runs the real parser/verifier): the real `TronGridClient` HTTP/retry/429/403 behavior, real `gettransactioninfobyid` payload shape through `parse_transaction_info` (`trongrid.py:128-177`), `get_trc20_balance` hex decoding, the full-tuple verifier in `services/backends.ManualTronBackend`, the gas monitor, and the `TRON_CONFIRMATIONS=19` depth semantics.

### Division of labor

**USER (manual, ~2–3 hours, guided by a new `docs/runbook-nile-verification.md`):**
1. TronGrid account + API key (free tier; required — keyless is throttled, and `config.py:150-162` refuses production without it).
2. Create a Nile hot wallet (TronLink or similar); create a second "user" wallet.
3. Nile faucet: TRX + test-USDT into both wallets; note the faucet's USDT contract address.
4. Bring up `docker-compose.regtest.yml` + `docker-compose.nile.override.yml`; do the four UI-only steps the override file documents (`deploy/docker-compose.nile.override.yml:14-35`): install the USDt plugin, set the plugin's JSON-RPC endpoint + contract, paste a ≥20-address pool, restart api/worker. Export `TRONGRID_API_KEY`, `TRON_HOT_WALLET_ADDRESS`, `USDT_CONTRACT_ADDRESS`.
5. When the drill script prompts: send the deposit payment from the user wallet; send the withdrawal transfer from the hot wallet and paste the txid.

**AUTOMATED (new `scripts/verify_nile.py`, mirroring `smoke_test.py`'s style):**
- *Preflight (read-only, no funds):* `get_block_height()` with the key (proves auth + endpoint); `triggerconstantcontract` `symbol()`/`decimals()` against the configured Nile contract, asserting `USDT`/`6` — **this is the moment `USDT_CONTRACT_NILE` becomes confirmed**; the same two calls against `api.trongrid.io` for `USDT_CONTRACT_MAINNET` (read-only — confirms the mainnet constant without touching mainnet funds); `get_trx_balance`/`get_trc20_balance` on the hot wallet (exercises the gas-monitor path live).
- *Deposit drill:* create a USDT deposit via the API, display the invoice address, wait for the user's payment, poll to `settled`, assert the ledger credit to the micro-USDT and `assert_ledger_consistent`.
- *Withdrawal drill:* create + approve a withdrawal per `docs/runbook-usdt-withdrawals.md`, prompt for the txid, call `mark-broadcast` — the live full-tuple verification (contract/sender/recipient/amount/receipt/Transfer-event, the table at `runbook-usdt-withdrawals.md:102-109`) runs against real TronGrid; then wait ~19 blocks (~1 min on Nile) for the confirmation poller, assert `confirmed` and the settle entry.
- *Negative check:* re-submit the same txid against a second withdrawal, assert the 409 (`ux_withdrawals_txid`).

### What "verified" means, and what changes afterward

Verified = the preflight assertions passed **and** one deposit and one withdrawal completed end-to-end against live Nile, with the real parser consuming real TronGrid payloads. Afterward:
- Downgrade the caveat in the four flagged locations to "confirmed against Nile on <date>, tx <txid>" (keep the "check it matches your plugin config" advice — it is still correct operationally); update `trongrid.py:46` mainnet comment to "confirmed live via symbol()/decimals() read".
- Add `docs/verification-log.md` recording date, txids, block heights, TronGrid responses — the adopter-facing evidence, referenced from SECURITY-AUDIT.md.
- If any real payload shape differs from `tests/fake_tron.py`'s assumptions, fix the fake to match reality (the fake's docstring promises it is "built from the shape TronGrid actually returns" — this milestone is what makes that claim true) and add a captured-payload regression test.

**Effort: 1 day** for the script + runbook + doc updates, plus the user's 2–3 hour session. No new dependencies; `TronGridClient` already has everything the preflight needs except a `symbol()/decimals()` helper (~15 lines next to `get_trc20_balance`).

---

## Recommended build order

| # | Workstream | Effort | Why this position |
|---|---|---|---|
| 1 | Static analysis (CodeQL, Dependabot, semgrep invariant rules, SECURITY-AUDIT.md skeleton) | 2 d | Zero infra, immediate adopter-facing value; the semgrep `postings-writer` rule should exist *before* the asset-extension work invites contributors into the money path |
| 2 | Migration robustness (round-trip, `alembic check`, v0.1.0 dump) | 1.5–2 d | Must land before v0.2 ships any migration 0006+; the v0.1.0 fixture is cheapest to produce while v0.1.0 is the current release |
| 3 | Property-based ledger tests | 2–3 d | Hardens the ledger before the new-asset milestone leans on it; nightly profile is ready for workstream 4 to consume |
| 4 | Nightly e2e on homelab | 2–3 d | Needs 1–3's artifacts to be worth running nightly; needs a homelab session |
| 5 | Nile live verification | 1 d + user session | Gated on the user's manual TronGrid/faucet steps; schedule around their availability, independent of 1–4 |

Total: roughly 9–12 focused days. Nothing here touches `post_entry`'s semantics, adds a second postings writer, or changes the drills — items 1–4 only add detectors around the existing machinery, and item 5 converts existing fake-tested paths to live-tested without code changes beyond comments, one script, and one small client helper.

Sources: [GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use), [Securely using pull_request_target](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target), [Safer pull_request_target defaults (2026 changelog)](https://github.blog/changelog/2026-06-18-safer-pull_request_target-defaults-for-github-actions-checkout/), [community discussion #26722](https://github.com/orgs/community/discussions/26722), [StepSecurity public-repo CI defense](https://www.stepsecurity.io/blog/defend-your-github-actions-ci-cd-environment-in-public-repositories), [Wiz GitHub Actions security guide](https://www.wiz.io/blog/github-actions-security-guide), [Semgrep vs Bandit (2026)](https://dev.to/rahulxsingh/semgrep-vs-bandit-python-security-scanning-compared-2026-5e5j), [AI code security tools comparison](https://sanj.dev/post/ai-code-security-tools-comparison/), [AppSec Santa SAST tools](https://appsecsanta.com/sast-tools), [AI SAST tools compared](https://www.augmentcode.com/tools/best-ai-sast-tools).

### Critical Files for Implementation

- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\ledger\service.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\tests\integration\conftest.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\.github\workflows\ci.yml
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\scripts\dev\smoke_test.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\gateway\trongrid.py