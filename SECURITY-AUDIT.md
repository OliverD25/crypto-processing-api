# Security audit

[`docs/security.md`](docs/security.md) states the threat model in prose. This
file is the audit trail underneath it: for every threat, which control actually
implements it, which file that control lives in, and what evidence exists that
it works today rather than on the day it was written.

The distinction matters. A threat model is a claim. An audit is the paperwork
that makes the claim checkable by someone who does not trust the author — which,
for a custodial service, is the only kind of reader worth writing for.

**Scope.** One deployment, one operator, self-hosted BTCPay. No multi-tenancy,
no HSM, no cold-storage automation. The controls below are the ones that exist,
not the ones a bank would have.

**How to re-verify.** Every "evidence" cell names a test. Run the suite and they
all run:

```
docker compose -f deploy/docker-compose.test.yml up -d
pytest -q
```

"Last verified" is the date the evidence was last observed passing, not the date
the control was written.

---

## Threat control matrix

Numbering follows the threat table in [`docs/security.md`](docs/security.md#threat-model).

### 1. Webhook spoofing

| | |
|---|---|
| **Control** | HMAC-SHA256 over the exact request bytes, compared with `compare_digest`; deduplication on BTCPay's `originalDeliveryId`; a second, independent dedup at the ledger on `(kind, source_ref)`; every credit re-fetches the amount from Greenfield instead of trusting the payload |
| **Implementing files** | [`api/webhooks.py`](src/crypto_processing_api/api/webhooks.py), [`core/signing.py`](src/crypto_processing_api/core/signing.py), [`ledger/service.py`](src/crypto_processing_api/ledger/service.py) |
| **Evidence** | `test_a_forged_signature_does_not_verify`, `test_a_changed_body_does_not_verify`, `test_whitespace_difference_does_not_verify`, `test_reserialized_body_does_not_verify`, `test_signature_over_reserialized_body_is_rejected`, `test_bad_signature_is_401_and_stores_nothing`, `test_the_identical_delivery_twice_is_one_row`, `test_redeliveries_collapse_to_one_row`, `test_ingress_never_touches_the_ledger`, `test_replaying_every_event_five_times_changes_nothing` |
| **Last verified** | 2026-08-10 |
| **Residual risk** | Compromise of BTCPay itself (threat 5). A valid signature from a compromised signer is indistinguishable from a legitimate one, by construction. |

The re-serialization tests are the load-bearing ones. A verifier that parses
JSON and re-serializes it passes every test written with `json.dumps` on both
sides, and fails against real BTCPay. Two tests exist specifically to stop
anyone "simplifying" the raw-bytes handling back into that bug.

### 2. Platform API key leak

| | |
|---|---|
| **Control** | Keys stored as SHA-256, never in plaintext; `cpk_live_` prefix so secret scanners recognise them; per-withdrawal auto-approval limit; rolling 24h per-asset cap serialized on a single row; optional per-user cap; destination validation; alerts on approval-pending and cap-hit; revocation with several keys active at once |
| **Implementing files** | [`core/auth.py`](src/crypto_processing_api/core/auth.py), [`services/withdrawals.py`](src/crypto_processing_api/services/withdrawals.py), [`core/addresses.py`](src/crypto_processing_api/core/addresses.py) |
| **Evidence** | `test_only_the_hash_is_stored`, `test_hash_is_sha256_hex`, `test_key_id_is_a_prefix_of_the_secret_not_the_whole_key`, `test_generated_key_has_the_documented_shape`, `test_revoked_key_stops_working`, `test_expired_key_is_rejected`, `test_admin_covers_readwrite_but_not_the_other_way`, `test_readwrite_key_cannot_reach_admin`, `test_above_the_auto_limit_goes_to_the_approval_queue`, `test_a_hit_daily_cap_forces_everything_manual`, `test_asset_gate_serializes_concurrent_holders`, `test_parallel_withdrawals_across_distinct_users_never_exceed_the_cap`, `test_per_user_cap_when_enabled` |
| **Last verified** | 2026-08-10 |
| **Residual risk** | **Accepted and material.** An attacker with a live key drains up to the daily cap before anyone reacts. The cap is the loss ceiling; there is no other bound. Operators must set it to a number they can afford to lose. |

`test_parallel_withdrawals_across_distinct_users_never_exceed_the_cap` is the
one that matters. A cap enforced by reading a sum and then writing is bypassable
by concurrency; this proves the serialization on `lock_asset_gate` actually
holds under parallel requests.

### 3. SQL injection

| | |
|---|---|
| **Control** | SQLAlchemy bound parameters throughout; Pydantic length and format validation at the edge; address checksum validation; **and, new in this release, a Semgrep rule that fails the build on interpolated SQL anywhere in the money path** |
| **Implementing files** | [`.semgrep/money-invariants.yml`](.semgrep/money-invariants.yml) (`no-interpolated-sql-in-money-path`), [`core/addresses.py`](src/crypto_processing_api/core/addresses.py), the Pydantic schemas in `api/` |
| **Evidence** | `semgrep` CI job, zero findings on the current tree; rogue-write demonstration below; `test_single_character_typo_in_base58_is_caught`, `test_single_character_typo_in_bech32_is_caught`, `test_garbage_is_rejected`, `test_malformed_keys_refused` |
| **Last verified** | 2026-08-10 |
| **Residual risk** | Low, and lower than it was. `docs/security.md` calls this "a review problem"; the rule downgrades it to a build problem. The rule permits `text()` with a literal string and bound parameters — the advisory locks in `workers/runner.py` — and rejects f-strings, concatenation and `.format()`. A parameterized literal that is later edited into an f-string fails CI. |

### 4. Insider or host-level tampering

| | |
|---|---|
| **Control** | Append-only journal: `BEFORE UPDATE` and `BEFORE DELETE` triggers that raise on `postings` and `journal_entries`; balances derived from immutable postings; a deferred zero-sum constraint trigger; hourly Job C compares materialized balances against derived and alerts on any drift without repairing |
| **Implementing files** | [`migrations/versions/0001_initial.py`](migrations/versions/0001_initial.py), [`ledger/invariants.py`](src/crypto_processing_api/ledger/invariants.py), [`workers/reconciliation.py`](src/crypto_processing_api/workers/reconciliation.py) |
| **Evidence** | `test_updating_a_posting_raises`, `test_deleting_a_posting_raises`, `test_updating_a_journal_entry_raises`, `test_deleting_a_journal_entry_raises`, `test_unbalanced_entry_rejected_by_the_database_trigger`, `test_raw_update_cannot_push_a_liability_positive`, `test_job_c_alerts_on_any_drift`, `test_drift_is_detected_and_reported`, `test_job_c_is_quiet_on_a_healthy_ledger`, `test_materialized_matches_derived_after_a_random_entry_sequence`, and the property machine's `books_hold_together` invariant |
| **Last verified** | 2026-08-10 |
| **Residual risk** | **Accepted.** Root on the host can disable a trigger, rewrite rows and re-enable it between two hourly checks. Nothing here defends against the machine's own administrator. External anchoring is out of scope, and saying so plainly is more useful than implying otherwise. |

Job C alerts and never repairs. A job that silently corrects the books destroys
the evidence that something was wrong — which is the only thing that makes an
insider detectable at all.

### 5. BTCPay compromise

| | |
|---|---|
| **Control** | Cash management, not cryptography: a small hot-wallet float with manual cold sweeps. The Greenfield key is scoped to one store and is never server-admin. Velocity caps bound abuse routed through our payout path. Nothing is published to the internet |
| **Implementing files** | [`docs/security.md`](docs/security.md#hot-wallet-float-policy) (float policy and sweep runbook), [`deploy/docker-compose.yml`](deploy/docker-compose.yml), [`scripts/bootstrap_btcpay.py`](scripts/bootstrap_btcpay.py) |
| **Evidence** | `test_a_store_that_cannot_take_btc_is_a_startup_failure`, `test_foreign_invoices_are_not_ours`, `test_another_stores_event_is_ignored`, `test_a_payout_that_is_not_ours_is_ignored`; the bootstrap script requests `btcpay.store.canmanagepayouts` and not server-admin |
| **Last verified** | 2026-08-10 |
| **Residual risk** | **Accepted and total within its blast radius.** If the box is owned, the hot wallet is gone. There is no control here that changes that; the only lever is keeping the float small. This is a procedural control, and it degrades the moment an operator stops sweeping. |

### 6. TronGrid outage or rate limit

| | |
|---|---|
| **Control** | Reconciliation retries; the withdrawal state machine tolerates delay without a timeout-then-retry double-send; the gas monitor alerts on a failure streak rather than on one blip; a paid API key is a documented upgrade |
| **Implementing files** | [`workers/gas_monitor.py`](src/crypto_processing_api/workers/gas_monitor.py), [`workers/reconciliation.py`](src/crypto_processing_api/workers/reconciliation.py), [`services/withdrawals.py`](src/crypto_processing_api/services/withdrawals.py) |
| **Evidence** | `test_a_trongrid_outage_alerts_only_after_a_streak`, `test_healthy_trx_is_quiet`, `test_low_trx_raises_an_alert`, `test_an_ambiguous_submission_leaves_the_row_in_submitting`, `test_ambiguous_outcomes_go_to_review_uncredited`, `test_everything_ambiguous_goes_to_a_human` |
| **Last verified** | 2026-08-10 |
| **Residual risk** | Hours-long USDT delays. Funds stall; they do not vanish. **Verified against a fake TronGrid with recorded response shapes, not against live TRON** — see the honesty note at the end of this file. |

### 7. Double withdrawal via race

| | |
|---|---|
| **Control** | `SELECT ... FOR UPDATE` in ascending id order, with the decision and the reservation in one transaction; the `no_overdraft` CHECK as a database-level backstop that does not depend on application correctness; idempotency keys on the request path |
| **Implementing files** | [`ledger/service.py`](src/crypto_processing_api/ledger/service.py) (`_lock_accounts`, `post_entry`), [`migrations/versions/0001_initial.py`](migrations/versions/0001_initial.py), [`core/idempotency.py`](src/crypto_processing_api/core/idempotency.py) |
| **Evidence** | `test_eight_threads_cannot_overdraw_a_balance`, `test_two_threads_racing_a_debit_only_one_wins`, `test_two_racing_requests_for_one_user_cannot_both_hold`, `test_two_workers_cannot_submit_the_same_withdrawal`, `test_exactly_one_of_two_racing_reclaims_wins`, `test_overdraft_rejected_by_the_check_constraint`, `test_concurrent_credits_to_the_hot_wallet_all_land`, `test_replay_of_the_same_key_holds_once`, `test_idempotent_endpoint_runs_once_and_replays`, plus the whole property machine |
| **Last verified** | 2026-08-10 |
| **Residual risk** | Effectively closed at the database level. The CHECK constraint holds even if every line of Python above it is wrong, which is why `test_raw_update_cannot_push_a_liability_positive` bypasses the service layer entirely to prove it. |

These are real threads against real PostgreSQL, not mocked concurrency. A test
that simulates a race proves nothing about `FOR UPDATE`.

### 8. Cloudflare origin bypass

| | |
|---|---|
| **Control** | `ufw` permits 443 only from Cloudflare's published ranges, refreshed by cron; API keys carry 256 bits of entropy, so brute force is not a threat worth rate-limiting against |
| **Implementing files** | [`deploy/ufw/`](deploy/ufw), [`deploy/nginx/`](deploy/nginx), [`core/auth.py`](src/crypto_processing_api/core/auth.py) |
| **Evidence** | `test_generated_keys_do_not_repeat`, `test_verify_accepts_only_the_exact_key`, `test_unknown_key_is_rejected`, `test_error_responses_never_name_the_reason`. **The ufw rules themselves are deployment assets and are not covered by any automated test** |
| **Last verified** | 2026-08-10 (application controls only) |
| **Residual risk** | **Accepted, and partly unverified.** The bitcoind port reveals the origin IP. More importantly, the firewall control is a shell script nobody's CI runs — its correctness rests on the operator following [`docs/deployment.md`](docs/deployment.md). This is the weakest evidence row in the table. |

### 9. Supply chain

| | |
|---|---|
| **Control** | Exactly pinned direct dependencies; a deliberately small dependency set; gitleaks over full git history; SBOM and provenance attestation on published images; **and, new in this release, Dependabot alerts with automated security fixes, plus CodeQL default setup** |
| **Implementing files** | [`pyproject.toml`](pyproject.toml), [`.gitleaks.toml`](.gitleaks.toml), [`.github/workflows/ci.yml`](.github/workflows/ci.yml), [`.github/workflows/release.yml`](.github/workflows/release.yml) |
| **Evidence** | `gitleaks` CI job over `fetch-depth: 0`; `semgrep` job with the registry's `p/python` suite; CodeQL weekly scan; the repo-settings log below; `docker build` job |
| **Last verified** | 2026-08-10 |
| **Residual risk** | Zero-days in FastAPI or SQLAlchemy — the risk everyone carries. Exact pinning trades automatic patching for reproducibility, and Dependabot is what pays that trade back: it opens the PR, a human still merges it. |

### 10. Reorg after credit

| | |
|---|---|
| **Control** | Settlement pinned at 1 confirmation minimum, never 0-conf; the `user_deficit` account kind exists so a post-credit loss can be **booked** rather than rejected by the overdraft CHECK |
| **Implementing files** | [`services/deposits.py`](src/crypto_processing_api/services/deposits.py), [`ledger/models.py`](src/crypto_processing_api/ledger/models.py), [`docs/runbook-reorg.md`](docs/runbook-reorg.md) |
| **Evidence** | `test_reorg_loss_on_an_already_spent_balance_is_bookable`, `test_user_deficit_may_hold_a_credit_balance`, `test_a_reorg_that_removes_the_transaction_does_not_confirm_it`, `test_confirmations_are_counted_from_the_block_height`, `test_confirmations_never_go_negative` |
| **Last verified** | 2026-08-10 |
| **Residual risk** | A deep reorg still costs real money. The control does not prevent the loss — it makes the loss recordable, so the books stay correct and recovery is possible. Those are different things and the difference should not be blurred. |

---

## Static analysis

### Configuration

| Tool | Version | Scope | Failure mode | Where |
|---|---|---|---|---|
| **Semgrep** | `1.172.0`, pinned exactly | `.semgrep/` custom rules + registry `p/python` | `--error`: any finding fails the build | `semgrep` job in [`ci.yml`](.github/workflows/ci.yml) |
| **CodeQL** | GitHub default setup | `python`, `actions` | Alerts in the Security tab; does not block merge | Repository setting, weekly + on push |
| **gitleaks** | `v8.30.1`, pinned exactly | Full history (`fetch-depth: 0`) | Non-zero exit fails the build | `secrets` job |
| **mypy** | `2.3.0`, pinned | `src/` under `--strict` | Any error fails the build | `typecheck` job |
| **ruff** | `0.16.2`, pinned | Whole repo, check + format | Any finding fails the build | `lint` job |

Every scanner version is pinned exactly. A scanner whose rules change silently
between runs turns a green build into a claim that cannot be falsified — you
cannot tell a fixed codebase from a relaxed ruleset.

CodeQL runs the **default** query suite, not `security-extended`. That is a
deliberate choice: `security-extended` on a codebase this size produces a
backlog of low-confidence findings, and an alert list nobody finishes reading is
worse than a shorter one that gets triaged. Revisit if the default suite ever
goes quiet for a long stretch.

### The custom rules, and why they exist

[`.semgrep/money-invariants.yml`](.semgrep/money-invariants.yml) holds three
architectural rules that the project previously stated only in prose:

| Rule | Enforces | Prose it replaces |
|---|---|---|
| `postings-writer` | Only `ledger/service.py` constructs or inserts `Posting` / `JournalEntry` | "`post_entry` is the only code that writes postings" — [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| `no-interpolated-sql-in-money-path` | No f-string, concatenated or `.format()`-built SQL in `ledger/`, `services/`, `workers/` | "no string-built SQL in the money path" — threat 3 |
| `no-float-amounts` / `no-float-literal-amounts` | No `float()` conversion or float literal assigned to an amount- or balance-named target | "All monetary columns are BIGINT ... never floats" — [`ledger/models.py`](src/crypto_processing_api/ledger/models.py) |

`postings-writer` matches construction and insertion **by shape**, not by
excluding files that happen to touch the table. That is why
`services/withdrawals.py` keeps its `select(Posting.amount)` with no carve-out,
and why `ledger/service.py` is the only exclusion in the file — the one module
whose docstring already claims the privilege. A path-based rule would have
needed an exclusion per reader, and every exclusion is a place a future writer
can hide.

**Every rule is tuned to zero findings on the current tree.** This is a design
constraint, not a happy accident. An earlier draft of the float rule reported 71
findings, nearly all legitimate — parameterized `text()` calls and non-monetary
floats such as sat/vB rates and `Retry-After` intervals. A rule that produces
findings a reviewer has to dismiss is a rule that gets switched off, and then the
one real finding arrives to an audience of nobody.

### Verification that the rules actually fire

Zero findings proves nothing on its own — an empty ruleset also scores zero. The
rules were verified against a scratch file containing four deliberate
violations: a `session.add(Posting(...))` outside `ledger/service.py`, an
f-string-interpolated `text()` query, `amount = float(raw)`, and
`balance = 1.5`.

```
$ docker run --rm -v "$PWD:/src" semgrep/semgrep:1.172.0 \
    semgrep scan --config /src/.semgrep/ --metrics=off --error /src

    /src/src/crypto_processing_api/services/_rogue_demo.py
   ❯❯❱ semgrep.postings-writer
   ❯❯❱ semgrep.no-interpolated-sql-in-money-path
   ❯❯❱ semgrep.no-float-amounts
   ❯❯❱ semgrep.no-float-literal-amounts
 • Findings: 4 (4 blocking)
```

All four fired; the exit status was non-zero. The scratch file was then deleted
and the same command re-run:

```
 • Findings: 0 (0 blocking)
 Ran 4 rules on 47 files: 0 findings.
```

Re-run this whenever a rule is edited. A guardrail nobody has watched fail is
not known to be a guardrail.

### Triage log

Findings that were reviewed and deliberately not fixed. Every entry needs a
reason that survives being read back in a year.

| Date | Tool | Rule / CVE | Location | Decision | Reason |
|---|---|---|---|---|---|
| — | — | — | — | — | No suppressed findings. The tree is clean under every scanner listed above. |

**Rules for this table.** A finding is either fixed or it appears here with a
justification — never silenced with an inline ignore comment and no entry. If a
row would say "false positive", fix the rule instead: a rule that misfires once
will misfire again, on someone with less context.

---

## Dependency policy

**Direct dependencies are pinned exactly** (`==`), never with ranges or
compatible-release operators. A build that resolves differently on Tuesday than
it did on Monday cannot be audited, and "it worked in CI" stops meaning
anything.

**The dependency set is deliberately small.** Every addition is a supply-chain
decision, not a convenience decision. Before adding one, the question is whether
the alternative is more code in this repository than in the dependency — and if
so, write the code.

**Updates arrive as pull requests, not automatically.** Dependabot opens them;
a human merges them. Automated security fixes are enabled, which means
Dependabot raises the PR for a known vulnerability without waiting to be asked —
it still does not merge.

**Scanners are pinned like dependencies**, for the reason given above.

**Upgrading a pinned scanner is a normal PR**, and its diff is expected to
include any newly-reported findings. Bumping a version and suppressing what it
found in the same commit is how a ruleset quietly stops working.

### Update procedure

1. Dependabot opens the PR.
2. CI runs the full suite, including the property tests and migration checks.
3. For a security fix, read the advisory and confirm the affected path is
   actually reachable from this codebase before treating urgency as given.
4. Merge. The pin moves; the lockstep with CI is preserved.

---

## Repository settings log

Applied via `gh api` and verified by reading the setting back. Re-run the
verification commands below if you need current values rather than these
recorded ones.

| Setting | State | Verified | Notes |
|---|---|---|---|
| **CodeQL default setup** | `configured` | 2026-08-10 | `query_suite: default`, `threat_model: remote`, `schedule: weekly`, `languages: ["actions", "python"]` |
| **Dependabot vulnerability alerts** | Enabled | 2026-08-10 | `PUT /repos/{owner}/{repo}/vulnerability-alerts` returned `204 No Content` |
| **Dependabot automated security fixes** | Enabled, not paused | 2026-08-10 | `{"enabled": true, "paused": false}` |
| **Private vulnerability reporting** | Not configured via API | 2026-08-10 | See the limitation note below |

```
gh api repos/OliverD25/crypto-processing-api/code-scanning/default-setup
gh api repos/OliverD25/crypto-processing-api/vulnerability-alerts -i
gh api repos/OliverD25/crypto-processing-api/automated-security-fixes
```

**A note on `languages`.** Immediately after enabling CodeQL, the API reported
`languages: []`. That is the pre-analysis state, not a misconfiguration — the
field populated to `["actions", "python"]` once the first scan completed. Worth
knowing before someone re-applies a setting that was already correct.

**Token limitation.** The token available during this work carried the scopes
`gist, read:org, repo, workflow`. All three settings above were reachable and
were applied. Settings requiring `admin:org` or organization-owner rights were
not attempted, and none were needed for the controls in this file. Anything the
token could not reach would be listed here rather than omitted.

---

## What this audit does not cover

Stated plainly, because an audit that only lists its strengths is marketing.

- **The TRON paths are verified against a fake.** There is no TRON regtest.
  `FakeTronGrid` replays recorded response shapes. Full-tuple verification —
  contract, from, to, amount, receipt status and the `Transfer` event — is
  tested against those shapes, and the shapes were taken from real responses,
  but no test in this repository has ever talked to live TRON.
- **The firewall and nginx configuration is untested.** `deploy/ufw/` and
  `deploy/nginx/` are shell and config assets. No CI job asserts anything about
  them. Threat 8 rests on the operator, and its evidence row says so.
- **No penetration test, no third-party review.** Every control here was
  designed and verified by the same author. That is the single largest
  unmitigated weakness in this document, and no amount of internal testing
  substitutes for an adversarial reader. See "Things that would genuinely help"
  in [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **No runtime intrusion detection.** Job C compares the books hourly. Between
  two runs, there is no monitoring.
- **Coverage is not proof.** The ledger module has an 85% floor. The untested
  15% is untested.

## Reporting a vulnerability

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).
