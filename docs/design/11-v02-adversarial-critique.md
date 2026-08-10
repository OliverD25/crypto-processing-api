All code claims below were verified against the working tree at `1eaa96e`.

# Adversarial review: v0.2 roadmap

## Verdict up front

**Sound after fixes** — the two-track structure, the ops-repo runner pattern, and the guardrails-before-refactor ordering are right. But the plan has one incomplete money fix (R1), one missed live defect it should have caught while auditing this exact area, a proof asset (Lightning) that does not fit the deposit contract as specified, and a runner threat model that stops one step too early. Details, most severe first.

---

## Severity 1 — R1 as scoped converts a thrashing bug into a silent deadlock, and misses a sibling defect

### 1a. The `due_for_submission` filter alone strands auto-approved manual rows forever

The plan's R1 adds `backend == BACKEND_BTCPAY` to `due_for_submission` (`src/crypto_processing_api/services/withdrawals.py:835`) and calls it done. Trace the full path:

- With `usdt_auto_withdraw=true`, `place_hold` creates the manual-TRON row **born in `APPROVED`** (`api/withdrawals.py:120-135`, `withdrawals.py:322`).
- The *only* code that ever calls `submit_manual` is the admin approve endpoint (`api/admin.py:213-222`), and `approve()` is a CAS that requires `PENDING_APPROVAL` (`withdrawals.py:701-716`). An auto-approved row can never take that path — approve returns 409.
- Today the row thrashes: submitter claims it, quotes a **BTC** fee against a USDT payment method, BTCPay rejects the payout, `resolve_stuck` finds no payout and flips it back to `APPROVED` (`workers/payout_submitter.py:208-212`), forever, every 10 seconds.
- **After R1's filter, nothing consumes it at all.** The user's balance is held, status reads `approved`, no `WITHDRAWAL_PENDING_APPROVAL` event was emitted (manual=False so `place_hold` skips it), and no admin action is legal. Loud thrashing becomes a silent stall — strictly worse for an operator.

**Fix:** v0.1.1 must do one of: (a) make `usdt_auto_withdraw=true` a startup `ValueError` in `config.py` until a Phase-2 signer exists (smallest, safest — the flag currently buys nothing but this bug), or (b) route approved manual-backend rows through `submit_manual` from the worker. Plus a documented remediation (CLI or runbook SQL) for rows already stranded in `approved`/`submitting` on upgraded v0.1.0 deployments — the plan's "no other changes ride along" DoD ships the filter and leaves those rows dead.

### 1b. Missed live defect: the hourly Job C never checks USDT custody

`build_jobs` wires the invariants job as `reconciliation.check_invariants(factory, gateway, settings)` — **no `tron=` argument** (`workers/runner.py:180-185`), and the parameter defaults to `None` (`workers/reconciliation.py:503-508`). `_chain_balance` then returns `(None, "no_source")` for USDT (`reconciliation.py:490`), and `insolvent` short-circuits to `False` on `chain_balance is None` (`reconciliation.py:455-459`). Only the on-demand admin endpoint passes `tron` (`api/admin.py:422-427`).

**Consequence:** the "one signal that matters" — USDT insolvency — has never once been computed by the background job on any deployment. A plan that audited this exact file for leaks 8/9 and proposes a `CustodySource` refactor on top of it should have found this. It belongs in R1 (one-line fix mirroring the `tron_configured` gate), with a regression test asserting Job C produces a `trongrid`-sourced custody line when TRON is configured.

---

## Severity 2 — Runner security: the threat model stops at fork PRs; the constraint was "production workloads on the same box"

The ops-repo pattern correctly makes fork-PR execution structurally impossible. But the stated hard constraint is broader: unrelated production workloads share the machine. The design's fences are CPU/mem only. Missed paths:

1. **LAN blast radius of code that legitimately runs.** The nightly executes public-repo `main` *and its full dependency tree* (pip resolution at image build, `btcpayserver/*`, `lnd`, `postgres` images pulled by tag). A malicious transitive PyPI release or a mutated image tag executes inside dind with **routable access to 192.168.88.0/24** — the production services, the Remote Control sessions, the NAS. The dind sibling isolates the Docker socket, not the network. **Fix:** run the nightly stack on an internal-only bridge; give the dind container an egress policy (nftables/docker `iptables` rules or a dedicated VLAN) allowing only registry + GitHub + TronGrid-if-needed, explicitly dropping RFC1918; pin all nightly images **by digest**; `pip install --require-hashes` (or at least a constraints lockfile) for the nightly path. Write this into the runner-isolation statement — it is the difference between "forks can't run here" and "this box's other tenants are safe."
2. **Runner credential hygiene.** An ephemeral runner needs a registration credential on the box, and workflow steps run *inside the runner container* — they can read its environment. If the compose passes a PAT as env, merged code can exfiltrate a token that can re-register runners against the ops repo. **Fix:** JIT runner config (`--jitconfig`) minted by a host-side script, credential never in the runner container's env; fine-grained PAT scoped to the ops repo only.
3. **Dead-man's-switch placement and the 60-day trap.** The switch must read the *private* ops repo's run history. If it lives in the public repo it needs a cross-repo PAT stored as a public-repo secret — readable by any workflow change that lands on `main`. If it lives in the ops repo, note that GitHub auto-disables scheduled workflows in repos with **no activity for 60 days**, and a quiet ops repo is exactly that: the watchdog watches the nightly, and both silently stop together. **Fix:** switch lives in the ops repo on GitHub-hosted runners with plain `GITHUB_TOKEN`; the nightly workflow's last step commits a heartbeat file (activity keeps both schedules alive); document the 60-day rule in the DoD test ("simulated 26-hour silence" does not catch mutual auto-disable).
4. **Disk is not fenced.** `cpus`/`mem_limit`/`pids_limit` are listed; a wedged build or leaked volumes fill the disk under the production workloads. **Fix:** dedicated data-root on a size-bounded volume/partition, plus a post-run assertion on free space that fails the nightly loudly.

---

## Severity 3 — Lightning does not fit the deposit contract as written; the plan discovers this in week 2 of R4

The plan's deposit facet is "data only: `btcpay_payment_method`, `invoice_currency`, `pooled_addresses`, `deposit_expiry_minutes`." That parameterization was extracted from BTC and USDT — and it silently bakes in a fifth, unlisted invariant: **every deposit is a top-up (amountless) invoice.** `ensure_invoice` calls `gateway.create_top_up_invoice(...)` (`services/deposits.py:302-309`), and that is the *only* invoice-creation path in the codebase (`gateway/btcpay_client.py:238-248`, "Create an invoice with no amount").

A plain BOLT11 invoice requires an amount. BTCPay can offer Lightning on a top-up invoice only via LNURL-Pay, which (a) needs store/payment-method settings that may not be settable through Greenfield in `bootstrap_btcpay.py`, (b) changes what `method.destination` contains (LNURL string, not an address — this becomes the platform-facing `deposit.address`), and (c) changes the payment-attribution shape `apply_invoice_state` consumes. If LNURL doesn't work out, the contract needs an *amount-mode* facet (fixed-amount invoices, with `PaidPartial`/`PaidOver` semantics the current `_target_status` maps straight to `review`) — a contract change mid-R4, exactly the destabilization R3 was supposed to prevent.

**Withdrawal side has three more leaks the plan waves past:**

- **Routing fees are unbooked money loss.** The plan assigns LN `FlatFee`, and `flat_fee_quote` sets `wallet_fee=0` (`services/fees.py:170`), so `committed == net` and `post_settle_entry` books no `network_fee_expense` (`withdrawals.py:453-472`). Every LN payment pays a routing fee the ledger never records; the channel balance drifts below `ledger_custody − expected_shortfall` cumulatively, and Job C's `difference` goes negative with no explanation until the honest-but-unactionable insolvency alarm fires. The deferred item "fee estimate-vs-actual drift journaling" is not deferrable — it is an R4 **prerequisite**.
- **BOLT11 expiry vs. an unbounded approval horizon.** The proposed validator checks "expiry > approval horizon" at request time — but `pending_approval` has no horizon; an operator approving a two-day-old LN withdrawal creates a payout against an expired invoice that can never pay. And BTCPay's Lightning payout processor *retries* `AwaitingPayment` rather than moving to `Cancelled`, so the row sits in `SUBMITTED` forever — the state machine has no timeout out of `SUBMITTED` (`withdrawals.py:102-108`), and `_PAYOUT_STATE_MAP` has no state that ever expresses "will never complete."
- **Failure is routine on LN, and the matrix makes every failure an attested admin release.** Route-not-found and liquidity failures are everyday events, cryptographically definitive (no HTLC settled), yet each one lands in `FAILED` requiring a ≥10-char attestation (`ATTESTED_RELEASABLE`, `withdrawals.py:127-134`; `api/admin.py:165`). Drill 10 ("liquidity exhaustion with **no automatic release**") is presented as safety; operationally it is a support queue. The contract needs a backend capability like `definitive_failure_proof` gating a narrow auto-release — a real contract extension, not reuse.

**Also a product-level honesty problem:** `BTC_LN` as a separate float means users have two non-fungible BTC balances and cannot withdraw deposited on-chain BTC over LN. Adopters who "need Lightning" almost always mean *unified balance, either rail out* — which requires the deferred rebalancing `EntryKind`. The amended README line ("Lightning: yes") oversells what ships.

**Fix:** insert a time-boxed 2-day spike before committing R4: prove (1) top-up + LNURL deposit attribution end-to-end on regtest, (2) LN payout-processor failure/expiry semantics, (3) Greenfield can configure all of it headlessly. Pre-agree the Litecoin fallback (open question 6 already exists — make the spike its decision input). If LN proceeds, move fee-drift journaling and a `SUBMITTED` timeout/definitive-failure semantics into R4's scope, and state the separate-float limitation in every positioning artifact.

---

## Severity 4 — R3 regression risk: the drills cannot see the layer R3 changes, and USDT is untestable until Nile

What the drills actually are (`scripts/dev/smoke_test.py`): BTC deposit / outage / replay / late, plus BTC withdrawal happy-path with approve. Everything R3 touches most is **not** drill-covered:

- The `BackendPayout` canonical-state normalization replaces the boundary where real BTCPay strings (`"AwaitingApproval"`, unknown future states) meet the state machine. Today an unknown payout state is a logged no-op (`withdrawals.py:655-658`) — a load-bearing safety behavior. The integration tests exercise this via `FakeBTCPay`, and R3 will **co-mutate the fake** to speak the new canonical states in the same PR — the tests then prove the fake matches the new code, not that the new code matches BTCPay 2.4.2. Classic fake-drift. **Fix:** add a recorded-payload corpus (real Greenfield payout/invoice JSON captured from the regtest stack, committed as fixtures) asserted through the normalizer, independent of the fakes; make "unknown state → no-op" an explicit conformance-suite case.
- `resolve_stuck`'s three branches (adopt / freeze / resubmit, `workers/payout_submitter.py:148-219`), `_quote_from_payout` adoption, absorb mode, and velocity-cap manual routing are covered only by fakes — same exposure.
- **The entire USDT surface cannot be exercised on regtest at all** (the compose says so: "USDT is not testable here"). R3 moves USDT's `_matches` substring heuristic (`services/assets.py:57-59` — deliberately fuzzy because "the id string has changed shape between plugin releases"), its pooled-asset tolerance, and its custody special case into the registry, and then claims "behavior byte-identical" — a claim no test against a real USDt plugin can verify. The plan lets R10 (Nile) *float*. **Fix:** make R10 a hard gate between R3 and the v0.2.0 tag, explicitly framed as the USDT regression check for the refactor, not just a caveat-downgrade exercise. If a registry matcher accidentally strictens the USDT match, the failure mode is `sync_payment_methods` silently disabling the asset at startup (`assets.py:89`) — deposits 404 in production and nothing in CI ever noticed.

---

## Severity 5 — SDK/docs rot: two gaps the drift gates don't cover, one wire-format trap

1. **Webhook event payloads have no schema anywhere.** The 8 event types are string constants (`services/events.py:22-30`) and payloads are ad-hoc dicts built in `_event_payload` (`withdrawals.py:358-382`) and `_emit_events` (`deposits.py:599-619`). They are not API responses, so R6's OpenAPI drift gate never sees them — yet R7 hand-writes `parse_event` types over them in two languages. When a field is added (R4 will: LN payloads, or `approval_reason`), both SDKs go stale with zero CI signal; the signature-vectors file only pins the *signing*, not the *shape*. **Fix:** define the payloads as typed models server-side, export JSON Schema next to `openapi.json`, drift-gate it identically, and generate `parse_event` types from it. This is cheaper than it sounds and closes the only unmonitored contract surface.
2. **`response_model=` can silently change wire bytes.** Serializers currently emit `created_at.isoformat()` (`+00:00` suffix) and pre-formatted amount strings. If the new response models type these as `datetime`/`Decimal`, pydantic v2 re-serializes them (UTC datetimes get `Z`, key order can shift) — breaking the "no wire-format changes" promise while every test that compares parsed JSON still passes. **Fix:** type all amount and timestamp fields as `str` in the response models, and make the round-trip test compare **raw bytes** of old serializer output vs. new route output, not parsed equality.
3. Smaller: dual-registry publish must be idempotent per registry (a failed npm publish after a successful PyPI publish makes tag re-runs fail on "version exists" — add skip-if-exists to both); Scalar via un-pinned CDN URL is third-party JS injection on the docs site — pin the version or vendor the file.

---

## Severity 6 — Robustness program frictions (R2)

- **Threaded `race_hold` inside a Hypothesis rule is a trap.** Thread scheduling is outside the seed's control, so a nightly failure with `derandomize` off is frequently non-reproducible — the worst kind of artifact for a solo maintainer to triage at 8am. The repo already has `tests/integration/test_ledger_concurrency.py`; keep races there as plain stress tests and keep the state machine single-threaded. Costs nothing, saves triage hours.
- **"Byte-identical dump" needs normalization.** `pg_dump` output is not byte-stable across runs (sequence values, row order without `--inserts` ordering). Specify: schema-only dump, sorted, fixed pg version — or the round-trip test flakes on day one.
- The semgrep `postings-writer` rule must distinguish writes from reads — `withdrawals.py` legitimately imports `Posting` for a SELECT (`_committed_from_entry`, line 490). Fine, just needs to be in the rule spec so the "deliberately-introduced rogue write" DoD test isn't passed by an over-broad rule that also has a permanent legit-read exclusion nobody reviews.

---

## Severity 7 — Maintainer arithmetic

32–44 focused days for a part-time solo maintainer is 4–6 calendar months, not "roughly a quarter" — and that is before the *recurring* load the plan creates: nightly triage (with LN regtest bootstrap being famously flaky — channel opens, gossip sync — which erodes exactly the alert-signal quality R5's dead-man's-switch depends on), Dependabot across three ecosystems, two SDK toolchains, three drift gates, community surface (issue forms invite reports; `muzexp@gmail.com` becomes a CoC contact), and an example app whose CI only checks "container builds" — meaning its actual deposit→withdrawal loop rots silently until a newcomer follows the tutorial and it fails, which is the worst possible reader to fail in front of. Either wire the example's loop into the nightly (cheap, the stack is already up) or don't ship it as a tutorial.

---

## Verdict and cut list

**Sound after fixes.** The skeleton (R1→R2→R3 ordering, ops-repo runner, in-code registry, generated-core SDKs, frozen-dump upgrades) survives adversarial pressure. The mandatory amendments: complete the R1 fix per §1 (including the Job C `tron` defect and upgrade remediation), harden the runner design per §2 (network egress, digest pins, JIT tokens, watchdog placement), gate R4 behind a Lightning feasibility spike with Litecoin pre-agreed as fallback and fee-journaling pulled into scope, make R10 (Nile) a hard post-R3 gate, and add the webhook-payload schema to the drift-gate family.

**Cut first if time runs short:**

1. **R9 (example app)** — it validates the story but protects no money and rots fastest; the SDK READMEs plus `docs/integrating.md` carry 80% of its value.
2. **TypeScript SDK (half of R7)** — keep the Python SDK, the signature-vectors file, and a verified TS webhook-verification snippet in the docs (good-first-issue #3 already exists for exactly this); a generated-but-unloved npm package is negative-value surface.
3. **Lightning (R4) → Litecoin or defer** — if the spike shows LNURL/payout-processor friction, do not burn 2+ weeks mid-roadmap forcing the contract to fit; the retrofit of BTC + USDT through the conformance suite (already in R3's DoD) is itself a meaningful proof, and a weaker-but-shipped Litecoin proof beats an unshipped Lightning one.

### Critical Files for Implementation
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\withdrawals.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\workers\runner.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\deposits.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\workers\payout_submitter.py
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\services\fees.py