# v0.2 DX / SDK / Docs / Community plan — crypto-processing-api

Grounded in the code as it exists at v0.1.0. All paths below are repo-relative from `E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api`.

---

## 0. Ground truth from the code that shapes every decision below

These are facts I verified by reading the source, and each one constrains the design:

1. **Every router returns untyped dicts.** All endpoints declare `response_model=None` and build responses via hand-rolled serializers (`serialize_deposit` in `src/crypto_processing_api/api/deposits.py:78`, `serialize_withdrawal` in `api/withdrawals.py:51`, ad-hoc dicts in `api/balances.py`, `api/admin.py`, `api/health.py`). **Consequence: the current `/openapi.json` has request schemas but no response schemas.** Any generated SDK today would type every response as `Any`. This makes "OpenAPI hardening" a hard prerequisite (Workstream A) before any codegen.
2. **No `operation_id`s are set** (`main.py:73-94` app factory is bare). FastAPI's defaults produce method names like `create_deposit_v1_deposits_post` — ugly in a generated SDK. Additive fix.
3. **The outbound signature scheme is deliberately Stripe's** (`src/crypto_processing_api/core/signing.py:45-81`): `X-CPA-Signature: t=<unix>,v1=<hex hmac-sha256("{t}.{body}")>`, 300 s replay window, constant-time compare. `verify_platform_signature` already exists server-side "for the tests and the docs example" — it is the reference implementation both SDKs must port. The signed bytes are produced once in `workers/outbound_delivery.py:65-79` (`event_body`: `{"id": "evt_...", "type", "created_at", "data"}`, compact separators, sorted keys).
4. **Event types are a closed set of 8** in `src/crypto_processing_api/services/events.py`: `deposit.detected|settled|review_required|expired`, `withdrawal.pending_approval|broadcast|completed|failed`. SDKs can ship them as constants/unions.
5. **Idempotency contract** (`src/crypto_processing_api/api/middleware.py:222-273`): `Idempotency-Key` header required on mutating endpoints; 400 if missing, 409 + `Retry-After` if in flight, 422 on body mismatch, stale takeover after 60 s, and docs/integrating.md screams "retry with the *same* key." This is exactly the ergonomics an SDK must automate: auto-mint a UUID per logical call, pin it across retries, retry 503/409 honoring `Retry-After`.
6. **Amounts are decimal strings** everywhere (`from_units`), and the two request fields `expected_amount`/`amount` are integer-smallest-unit strings. SDK types must be `str` (+ helper converters), never `number`/`float`.
7. **Auth**: `Authorization: Bearer cpk_live_…`, scopes `readwrite`/`admin` (`api/middleware.py:130-170`).
8. **`release.yml` already has `id-token: write`** — the exact permission PyPI trusted publishing and npm provenance need; extending it is additive.
9. **Community surface is nearly empty**: `.github/` contains only `workflows/`. No issue templates, no PR template, no CODE_OF_CONDUCT, no ROADMAP. `SECURITY.md` and a strong `CONTRIBUTING.md` (with the 9-invariant money-path rule at lines 54-74) already exist and should be *referenced*, not duplicated.
10. **`docs/` is already a docs site in waiting**: 9 operator/integrator docs + 7 design-record docs, all plain markdown with relative links — MkDocs-compatible with almost no editing.

---

## Workstream A — OpenAPI hardening (prerequisite, ships first)

Everything downstream (SDKs, API reference page, drift gates) consumes one artifact: a committed, deterministic `openapi.json`.

**Changes (all additive, no wire-format change, so no versioning problem):**

1. **New module `src/crypto_processing_api/api/schemas.py`** — Pydantic response models mirroring the existing serializers exactly: `DepositResponse`, `DepositPaymentResponse`, `DepositListResponse`, `WithdrawalResponse`, `WithdrawalListResponse`, `BalancesResponse`, `TransactionsResponse`, `AssetsResponse`, `AddressHistoryResponse`, `HealthResponse`, `ReadyResponse`, plus the admin queue/resolve/requeue shapes from `api/admin.py`. Field-for-field copies of what `serialize_*` emits today — write a test that asserts `Model.model_validate(serialize_deposit(...))` round-trips, so the models cannot drift from the serializers.
2. **Annotate routers** with `response_model=` (or convert serializers to return the models; either way keep JSON identical — key order and string amounts included) and set `operation_id` on every route: `createDeposit`, `getDeposit`, `listUserDeposits`, `createWithdrawal`, `getWithdrawal`, `listUserWithdrawals`, `getUserBalances`, `getUserTransactions`, `listAssets`, `getAddressHistory`, plus `admin*` names. Also add `openapi_tags` metadata and a top-level `description` in `create_app()` (`main.py:77`).
3. **Document the `Idempotency-Key` header** in OpenAPI via a shared `Header(...)` parameter on the two POSTs, and the error envelope (`{"detail": ...}` + the structured pool-exhaustion `code`) as documented responses.
4. **`scripts/export_openapi.py`** — `json.dump(create_app().openapi(), sort_keys=True, indent=2)` to `docs/reference/openapi.json`. No server, no DB needed (app factory only touches settings at import; verify with defaulted `Settings`).
5. **CI drift gate** — new job in `.github/workflows/ci.yml`: run the export, `git diff --exit-code docs/reference/openapi.json`. A route change that isn't reflected in the committed spec fails CI. This one job is what makes SDK regeneration "a CI step, not a chore."

**Maintenance cost:** near zero after landing — the drift gate *reduces* maintenance by making spec staleness impossible.

---

## Workstream B — SDKs (Python on PyPI, TypeScript on npm)

### Decision: generated core + thin handwritten ergonomics layer, both languages

Rationale, verified against the 2026 ecosystem:

- **TypeScript: `@hey-api/openapi-ts`.** Actively developed through 2026 (v0.99, releases monthly, 10-30x perf improvements landed April 2026), used in production by Vercel and PayPal, generates a typed fetch-based SDK plus standalone types from OpenAPI 3.1 (which FastAPI 0.141 emits). It is the clear community default over `openapi-generator`'s Java toolchain.
- **Python: `openapi-python-client`.** Still the maintained community generator in 2026: httpx-based, sync + async clients, typed dataclass models, ships `py.typed`. The API surface here (~15 endpoints) is well inside what it handles cleanly.
- **Rejected: Fern / Stainless / Speakeasy.** Better output, but commercial services with free tiers that change; a one-maintainer MIT project should not have its release pipeline depend on a vendor account.
- **Rejected: fully handwritten SDKs.** Handwritten clients drift silently when a route changes. Generated cores + the Workstream A drift gate mean a route change *mechanically forces* an SDK regen in the same PR.
- **Why a handwritten layer at all:** codegen cannot produce the two things integrators actually need help with — webhook signature verification and idempotency-key discipline. Those are small, stable, and worth owning.

Sources: [Speakeasy's Python OSS generator comparison](https://www.speakeasy.com/docs/sdks/languages/python/oss-comparison-python/), [hey-api on npm](https://www.npmjs.com/package/@hey-api/openapi-ts), [hey-api 2026 releases](https://newreleases.io/project/github/hey-api/openapi-ts/release/2026-05-04), [TS client generation comparison](https://blog.api-fiddle.com/posts/best-ways-to-generate-ts-client), [Python SDK from OpenAPI in 2026](https://sourced.sh/blog/generate-python-sdk-from-openapi), [FastAPI SDK generation docs](https://fastapi.tiangolo.com/advanced/generate-clients/).

### Layout (monorepo, no submodules)

```
sdks/
  python/                      # PyPI: crypto-processing-client
    pyproject.toml
    crypto_processing_client/
      _generated/              # openapi-python-client output, never hand-edited
      client.py                # facade: auto Idempotency-Key, Retry-After-aware retry
      webhooks.py              # verify_signature(), event type constants, VerificationError
      amounts.py               # str <-> Decimal helpers (never float)
    tests/                     # incl. cross-language signature vectors
  typescript/                  # npm: crypto-processing-client
    package.json
    openapi-ts.config.ts
    src/
      generated/               # hey-api output
      client.ts                # facade (idempotency, retries)
      webhooks.ts              # verifySignature() via node:crypto timingSafeEqual
      index.ts
    test/
sdks/signature-vectors.json    # shared test vectors: secret, timestamp, body, header
```

### The two mandatory ergonomics features

1. **Webhook verification helper** — a direct port of `verify_platform_signature` (`core/signing.py:58-81`): parse `t=,v1=`, enforce the 300 s window, constant-time compare (`hmac.compare_digest` / `crypto.timingSafeEqual`), operate on **raw body bytes** (the doc trap at `docs/integrating.md:446-452`). Plus a `parse_event(raw, header, secret)` that verifies then returns a typed `{id, type, created_at, data}` union over the 8 event types. **Cross-language correctness is enforced by `sdks/signature-vectors.json`**: vectors generated once by the server's own `sign_platform_payload`, asserted by the server test suite *and* both SDK test suites — the three implementations can never disagree silently.
2. **Idempotency ergonomics** — `client.deposits.create(...)` mints a UUIDv4 `Idempotency-Key` automatically, accepts an explicit `idempotency_key=` for caller-controlled logical operations, and the built-in retry (on 503 and 409, honoring `Retry-After`, bounded attempts) **always reuses the same key** — encoding the "retry with the same key, do not generate a new one" rule from `docs/integrating.md:399` so integrators cannot get it wrong by default.

### Regeneration and publishing (CI, not chore)

- **New `.github/workflows/sdk.yml`:**
  - On PR: regenerate both SDKs from `docs/reference/openapi.json`, fail on diff (second drift gate), run both SDK test suites (including signature vectors).
  - On `v*` tag (or as jobs appended to `release.yml`): build and publish.
- **PyPI: trusted publishing (OIDC)** — `pypa/gh-action-pypi-publish` with `id-token: write` and a GitHub `pypi` environment; no long-lived token stored. Register the publisher on PyPI once against `OliverD25/crypto-processing-api` + `sdk.yml`.
- **npm: provenance** — `npm publish --provenance --access public` with `id-token: write`; the repo is public so provenance attestation works. One `NPM_TOKEN` secret is still required (npm's OIDC-only publishing is granular-token + provenance today); scope it to the single package.
- **Versioning: SDKs share the repo version.** One `v0.2.0` tag releases the image and both SDKs as `0.2.0`. Policy statement (goes in the versioning doc, Workstream E): *SDK minor tracks API minor — `client 0.2.x` supports `server 0.2.y`; SDK-only fixes bump patch; the server never bumps solely for an SDK fix (skip publish if unchanged).* For one maintainer, one version number is the only scheme that stays true without effort.

**Maintenance cost:** regeneration is automatic; the handwritten surface is ~300 lines per language of code that changes only when the signature scheme or idempotency contract changes (i.e., almost never, and loudly). Publishing is tag-triggered with no secrets to rotate except one npm token.

---

## Workstream C — Example integration app (`examples/platform-demo/`)

**Stack: single-file FastAPI + Jinja2 + HTMX, using the Python SDK.** Reasons: same language as the codebase (readers are already in Python-mode); a webhook *receiver* needs a server, which rules out a static page; HTMX keeps it to one `app.py` plus templates with zero build step; and it dog-foods `crypto-processing-client`, so the example doubles as an SDK acceptance test. Target: `app.py` under ~350 lines, written to be read top-to-bottom like a tutorial.

**What it demonstrates, end-to-end against the regtest stack:**

1. "Log in" as a fake user (a text box, users are just `external_user_id`s).
2. Create a BTC deposit → render the address + `checkout_link`; poll `GET /v1/deposits/{id}` and show the `pending → confirming → settled` transitions exactly as `docs/integrating.md:502-511` prescribes.
3. Show balances from `GET /v1/users/{id}/balances` — with a visible comment: *this app stores no balances; the API is the source of truth.*
4. Request a withdrawal → show `pending_approval` vs auto-approved, then `broadcast` + txid.
5. `/platform-webhook` endpoint implementing the 5-step contract from `docs/integrating.md:456-462`: verify (SDK helper) → dedup on `evt_` id → 200 immediately → re-read the resource → act on the GET. Each step is a numbered comment.

**Files:**

```
examples/platform-demo/
  README.md          # the tutorial: what to run, what to watch, what each step proves
  app.py
  templates/         # base.html, index.html, fragments
  Dockerfile
```

**Wiring: one compose profile.** Add an `example` service to `deploy/docker-compose.regtest.yml` under `profiles: ["example"]`, on the same network, with `PLATFORM_WEBHOOK_URL=http://example:8080/platform-webhook` and `PLATFORM_WEBHOOK_SECRET` set for the api/worker services so outbound delivery actually fires. `docker compose -f deploy/docker-compose.regtest.yml --profile example up -d` and `scripts/dev/mine.sh` complete the loop. The existing drills are untouched (profile is opt-in).

**Maintenance cost:** low — it compiles against the published SDK, so SDK CI catches breakage; it has no tests of its own beyond "container builds" in CI.

---

## Workstream D — Docs site

### Tooling: MkDocs-format markdown, built with pinned Material for MkDocs; Zensical-ready by construction

The 2026 wrinkle: **Material for MkDocs entered maintenance mode and reaches EOL on November 5, 2026**, with [Zensical announced as its successor](https://squidfunk.github.io/mkdocs-material/blog/2025/11/05/zensical/) — and Zensical natively reads `mkdocs.yml` ([EOL notice](https://github.com/squidfunk/mkdocs-material/issues/8523), [honest 2026 review](https://docsio.co/blog/mkdocs-material)). The low-maintenance play for one maintainer:

- Author everything as **plain markdown + a boring `mkdocs.yml`**: no custom theme overrides, no exotic plugins. That keeps the eventual `pip install zensical` migration a config-level change.
- Build with **pinned `mkdocs-material`** today (known-good, still receiving security fixes through Nov 2026; the generated static site does not stop working at EOL). Re-evaluate Zensical at v0.3 — if `mkdocs build` swaps to `zensical build` cleanly, switch then.
- **No `mike` / no versioned docs.** Pre-1.0, a version selector doubles publish complexity for a project with one deployed version per operator. Instead: a one-line admonition on the landing page — *"These docs track `main`. For the docs matching your deployed release, browse `docs/` at your release tag."* Revisit at 1.0.

### Information architecture (what moves vs. what is written new)

```
docs/
  index.md                      NEW — positioning + pitch (shares source with README top-fold)
  getting-started/
    quickstart.md               NEW (thin) — regtest in 10 minutes, lifted from README + smoke test
    deployment.md               MOVED from docs/deployment.md
    btcpay-setup.md             MOVED
  integrating/
    index.md                    MOVED from docs/integrating.md (kept whole — it reads well as one page)
    sdks.md                     NEW — install + 20-line example per language, webhook verify in both
    example-app.md              NEW (thin) — points at examples/platform-demo
  operating/
    security.md, backups.md,    MOVED
    runbook-*.md (3 files)      MOVED
  extending/
    adding-an-asset.md          NEW — the formal asset contract (authored by the asset workstream;
                                this IA reserves its home)
  reference/
    api.md                      MOVED from docs/api.md (the human-written lookup table stays)
    openapi.md                  NEW — one page embedding Scalar API Reference via CDN script tag
    openapi.json                Workstream A artifact (committed)
    webhooks.md                 NEW — the 8 event types, payload shapes, signature scheme, extracted
                                from integrating.md + services/events.py
    configuration.md            NEW, GENERATED — script renders every field of Settings
                                (src/crypto_processing_api/config.py) with type/default/constraint
                                into a table; CI drift-gates it like openapi.json. Kills the
                                .env.example-vs-code drift class permanently.
    versioning.md               NEW — release cadence, semver, deprecation policy (Workstream E)
  design/                       MOVED as-is — the design record, prominently linked as
                                "why it is built this way", with a banner: historical record, not reformatted
```

Keep git history by using `git mv`; add a link-check step (`mkdocs build --strict` catches internal link breakage) so the moves cannot silently 404.

**API reference generation:** no server-side plugin — `reference/openapi.md` embeds [Scalar](https://github.com/scalar/scalar)'s CDN script pointed at the committed `./openapi.json`. Zero build-time dependency, always in sync with the drift-gated spec. `docs/api.md` remains the curated human version (it contains judgment the spec can't: "`expected_amount` is load-bearing for USDT").

### Publish job

New `.github/workflows/docs.yml`: on PR touching `docs/**`/`mkdocs.yml` → `mkdocs build --strict`; on push to `main` → build + `actions/upload-pages-artifact` + `actions/deploy-pages` (GitHub Pages via Actions, `pages: write` + `id-token: write`). Enable Pages once in repo settings.

**Maintenance cost:** the site is the existing docs, moved; only `sdks.md`, `webhooks.md`, `index.md`, and quickstart are genuinely new prose. Two generated pages (openapi, configuration) are drift-gated, not maintained. Theme risk is contained by the no-customization rule.

---

## Workstream E — GitHub community kit

**Files:**

```
.github/
  ISSUE_TEMPLATE/
    bug.yml                 # version, asset, BTCPay version, redaction warning ("no keys, no addresses
                            # you care about"), what the reconciliation sweep said
    feature.yml             # includes "which ledger invariant does this touch, if any?"
    operator-report.yml     # UNUSUAL BUT EARNED: CONTRIBUTING.md:117 explicitly asks for "an
                            # operator's account of running it" — make that a first-class template
                            # (what alerted, what was noise, what the runbooks got wrong)
    config.yml              # blank_issues_enabled: false; contact links → SECURITY.md for vulns,
                            # Discussions Q&A for questions
  PULL_REQUEST_TEMPLATE.md  # checkbox: "touches ledger/ or services/?" → required section listing
                            # which of the 9 invariants (CONTRIBUTING.md:58-74) still hold and why —
                            # the template makes the existing rule mechanical, with the invariants
                            # inlined so authors don't tab away
  dependabot.yml            # pip + github-actions + npm (sdks/), monthly, grouped — deliberate
                            # cadence for a pinned-dependency money project
CODE_OF_CONDUCT.md          # Contributor Covenant 2.1, enforcement contact = muzexp@gmail.com
ROADMAP.md                  # v0.2 workstreams (asset contract, robustness, DX) with checkboxes;
                            # a "not planned" list (Lightning, multi-tenancy, hosted service) —
                            # saying no in writing prevents recurring issues
docs/reference/versioning.md  # release + deprecation policy (below)
```

**Discussions categories:** Announcements (maintainer-only posts), Q&A — Integration help, Operators (running it in production; feeds the operator-report loop), Asset proposals (the CONTRIBUTING "more assets" invitation, pointed at the v0.2 asset contract), Ideas.

**Release cadence + versioning policy (the actual text, condensed):**
- Pre-1.0 semver: **minor** releases may change behavior, with a "Breaking / Migration" section in `CHANGELOG.md` mandatory (the `release.yml` awk extraction at lines 84-97 already enforces a changelog section per tag — keep leaning on that). **Patch** releases never require operator action beyond pull + restart.
- API deprecation: an endpoint or field is marked deprecated in OpenAPI + docs for at least one minor before removal; removal is a minor with a migration note. No breaking change to amount encoding or the signature scheme without a `v2` signature version (`v1=` was chosen precisely so `v2=` can coexist).
- Migrations: alembic-only, always forward, every release upgradeable from the previous release (test: CI job upgrading a 0.1.0-schema database — belongs to the robustness workstream, referenced here).
- Cadence: minor roughly quarterly, security patches as needed. Honest for one maintainer.

**~10 good-first-issues, all verified against the code (label `good first issue` unless noted):**

1. **Wire the signature-failure spike alert.** `AlertCode.WEBHOOK_SIGNATURE_FAILURE_SPIKE` (`src/crypto_processing_api/alerts/notifier.py:51`) and `webhook_signature_failure_threshold` (`config.py:120`) both exist, but nothing counts 401s in `api/webhooks.py` or ever raises it. Self-contained, non-money.
2. **`list-api-keys` CLI command.** `cli.py` has `create-api-key`/`revoke-api-key` (lines 146-152) but no way to see key ids, scopes, or `last_used` — you cannot revoke what you cannot list.
3. **TypeScript webhook-verification example** in `docs/integrating.md` (§ line 423 has Python only) — until the SDK ships, and it becomes the SDK doc snippet after.
4. **`export-openapi` CLI/script + CI drift gate** (Workstream A item 4-5 — deliberately carved out as an onboarding-sized issue).
5. **Generated configuration reference** from the `Settings` model (Workstream D `configuration.md`).
6. **Structured error codes.** Only pool exhaustion returns `{"code": ...}` (`api/deposits.py:154-161`, noted in `docs/api.md:26`); define a small catalog and apply it consistently (additive: keep `detail` strings).
7. **Prometheus `/metrics` endpoint** (request counts, worker heartbeat ages already computed in `api/health.py:104-126`, outbound queue depth). Non-money, operator-visible.
8. **Response-model coverage for admin endpoints** (`api/admin.py`, 449 lines of dict responses) — a follow-up slice of Workstream A that touches no money logic.
9. *(label `help wanted`, money-path)* **Fee estimate-vs-actual drift journaling** — book the drift explicitly to `network_fee_expense` via a `post_entry` caller, per `docs/design/06-adversarial-critique.md` items #8-#9 (lines 54-60), instead of letting it live in Job C's tolerance.
10. *(label `help wanted`, money-path)* **Per-amount confirmation tiers** — `docs/runbook-reorg.md:154-155` names this as Phase 2 ("Per-amount confirmation tiers are Phase 2; a cheap approximation today is a per-asset delay").
11. **`.env.example` completeness check** against `Settings` fields (subsumed by #5 if the generator also validates — keep whichever lands first).

**Maintenance cost:** issue forms and templates are write-once. The operator-report template and Discussions category create the only ongoing obligation — reading them — which is the feedback the project explicitly asked for in CONTRIBUTING.

---

## Workstream F — Positioning

**README top-fold rewrite** (first 25 lines, before "What it does"):

- Pitch paragraph (draft): *"crypto-processing-api is the accounting layer BTCPay Server doesn't have. Run it next to your own BTCPay node and your platform gets per-user crypto balances, deposits, and withdrawals through a small authenticated JSON API — backed by an append-only double-entry ledger that a webhook cannot corrupt, a reconciliation sweep that doesn't trust the fast path, and a threat model that starts from 'the hot wallet is the loss ceiling.' Self-hosted, single-tenant, MIT."*
- **Who this is for:** a platform that needs *per-user* custodial BTC/USDT balances (marketplace, game economy, SaaS credits), is willing to self-host BTCPay + one VPS, and wants the money-correctness machinery already built and tested.
- **Who this is NOT for** (each line preempts a recurring issue): you want non-custodial checkout (use BTCPay alone); you want zero ops and accept custody/KYC/fees by a third party (use a hosted processor); you need Lightning, many assets, or multi-tenancy (not in scope — see ROADMAP "not planned"); you need something audited at scale (v0.x, no external audit — `README.md:84-89` already says this honestly; keep saying it).

**Comparison table** (README + `docs/index.md`):

| | Raw BTCPay Server | Hosted processor (NOWPayments-style) | This project | Build it yourself |
|---|---|---|---|---|
| Per-user ledger & balances | none — invoices only | their database, their rules | double-entry, append-only, yours | months of subtle work |
| Custody | you | **them** | you | you |
| Fees | node costs | percentage of volume | node + VPS costs | your time |
| KYC / account risk | none | their policies, can freeze | none | none |
| Withdrawal controls | manual | theirs | holds, velocity caps, approval queue | build them |
| Correctness under crash/retry | n/a | opaque | idempotency + reconciliation sweep, drilled on regtest | the hard part |
| Audit trail | invoice list | CSV export | immutable journal in your PostgreSQL | build it |
| Trust required | your node | a third party with your float | your node + this code (MIT, readable) | your code |

The honest row is custody: this project does *not* remove custody risk — it gives you the controls and the books; a hosted processor removes the work by taking the float. That framing is consistent with `docs/security.md` and should be stated verbatim.

---

## Sequencing and dependencies

1. **A (OpenAPI hardening)** — blocks B and the docs `reference/` pages. Small, additive, do first.
2. **E (community kit) + F (positioning)** — no dependencies, cheap, do in parallel with A; they change the repo's first impression immediately.
3. **B (SDKs)** — after A. Signature vectors file lands with the server test that generates it.
4. **D (docs site)** — after A (needs `openapi.json`); `sdks.md` finalized after B.
5. **C (example app)** — last; consumes the published Python SDK and validates the whole story.

Cross-workstream note: `extending/adding-an-asset.md` in the docs IA is authored by the asset-contract workstream; this plan only reserves its slot and nav entry. Nothing here touches `ledger/` or `services/` money paths except the two `help wanted` issues, which are deliberately *issues*, not part of this workstream's diffs — the money-code invariants and the drills are untouched by everything above.

### Critical Files for Implementation

- `src/crypto_processing_api/main.py` — app factory: OpenAPI metadata, tags, the surface every artifact derives from
- `src/crypto_processing_api/api/deposits.py` — the pattern file for response-model + operation_id retrofit (its serializers/`response_model=None` style repeats across all routers)
- `src/crypto_processing_api/core/signing.py` — reference implementation both SDK webhook helpers port, and the source of the shared test vectors
- `.github/workflows/release.yml` — gains the PyPI trusted-publishing and npm-provenance jobs; already holds `id-token: write`
- `docs/integrating.md` — the backbone of the docs site's integrator section and the contract (idempotency, webhook 5-step) the SDK ergonomics encode