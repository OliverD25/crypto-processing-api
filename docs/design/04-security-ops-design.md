The project directory is empty — this is a greenfield design task, so no codebase exploration is needed. Below is the full design document.

---

# crypto-processing-api — Security, Operations & Open-Source Packaging Design

Lens: **what gets us robbed or embarrassed.** Robbed = hot wallet drained, ledger inflated, payouts spoofed. Embarrassed = secrets in the public repo, webhook replay double-credits, CI that doesn't run, a README that promises what the code doesn't do.

---

## 1. Inbound auth (platform → API)

### Recommendation: plain bearer API keys for MVP. No HMAC request signing.

Rationale, honestly scoped:

- The transport is backend-to-backend over TLS (Cloudflare + origin TLS). HMAC request signing defends against (a) TLS-stripped middleboxes, (b) key leak into logs of intermediaries, (c) replay. On a single-tenant deployment where the operator controls both ends, these are marginal, and HMAC signing doubles the integration cost for every platform that adopts the open-source project. Bad DX in an OSS project is an embarrassment vector.
- Replay of an *authenticated platform request* is already neutralized where it matters: every mutating endpoint (`POST /withdrawals`, `POST /deposits/invoice`) **requires a client-supplied `Idempotency-Key`** (UUID, unique-indexed in Postgres). A replayed request returns the original response and never double-executes. Idempotency keys give us most of what HMAC replay protection would, with value beyond security (network retry safety).
- Leave a documented seam: version the scheme in the key prefix so HMAC can be added later without breaking anyone.

### Key design

- **Format:** `cpk_live_<32 bytes base62>` and `cpk_test_<...>`. The prefix makes keys grep-able in leaked logs/repos and lets GitHub secret-scanning partners be registered later. First 8 chars after the prefix are a non-secret **key ID** used for lookup and safe logging.
- **Generation:** `secrets.token_bytes(32)`, generated only server-side by a CLI command (`python -m app.cli create-api-key --name platform-prod`). Printed once to stdout, never stored in plaintext, never emailed.
- **Storage:** `api_keys` table: `id`, `key_id` (lookup), `key_hash`, `name`, `created_at`, `expires_at NULL`, `revoked_at NULL`, `last_used_at`. Hash = **SHA-256 of the full key** — not bcrypt/argon2. Justification: the key is 256 bits of entropy, so brute-forcing a fast hash is infeasible; slow hashes exist to protect low-entropy human passwords and would add ~100ms per request. This is the industry-standard design (Stripe, GitHub do the same).
- **Comparison:** look up row by `key_id`, then `secrets.compare_digest(sha256(presented), stored_hash)`. Constant-time compare is cheap insurance even though hash comparison already resists timing attacks.
- **Rotation:** because multiple active keys are allowed, rotation is: create new key → deploy to platform → revoke old key (`revoked_at`). Zero-downtime. Document a 90-day rotation suggestion but don't enforce expiry in MVP (an expired key silently breaking payouts at 3 a.m. is its own incident).
- **Scopes (MVP-light):** two scopes only — `readwrite` (platform key) and `admin` (manual withdrawal approval endpoints). Do not build a full RBAC system.
- Header: `Authorization: Bearer cpk_live_...`. Reject keys anywhere else (query params get logged everywhere — Cloudflare, uvicorn access log).

---

## 2. Webhook ingress (BTCPay → this API)

This is the endpoint that **mints money in the ledger**. It gets the most paranoia per line of code.

### Signature verification

- BTCPay Greenfield webhooks send `BTCPay-Sig: sha256=<hex hmac>` computed over the **raw request body** with the webhook's shared secret. Verify with `hmac.new(secret, raw_body, sha256)` + `hmac.compare_digest`. Critically: read the **raw bytes** before Pydantic parsing — re-serializing JSON and signing that is the classic bug (key ordering/whitespace differ → false negatives, or worse, devs "fix" it by disabling verification).
- The webhook secret is a dedicated random value (32+ bytes) configured in both BTCPay's webhook settings and our env. It is **not** the Greenfield API key.
- Reject unsigned or bad-signature requests with `401` **before** any parsing/DB work. Log the source IP.
- Defense in depth at the network layer: the webhook path can additionally be restricted since BTCPay runs on the **same VPS** — point BTCPay's webhook URL at the API over the internal Docker network / localhost (`http://crypto-api:8000/webhooks/btcpay`), so the path never needs to be internet-reachable at all. This is the single-tenant advantage; take it. Keep signature verification anyway (belt and suspenders, and some deployments will split hosts).

### Replay and dedup

- BTCPay does not send a signed timestamp, so timestamp-window checks add little; **event-ID dedup is the real control.** Every webhook payload carries a unique delivery/event ID plus `invoiceId` and event `type`.
- `webhook_events` table: `event_id` (unique), `invoice_id`, `type`, `raw_payload jsonb`, `received_at`, `processed_at`, `status`. Insert with `ON CONFLICT (event_id) DO NOTHING`; if no row inserted → duplicate → return `200` immediately (returning 2xx stops BTCPay redelivery loops).
- **The dedup that actually protects the ledger is one level deeper:** crediting is keyed on the *invoice*, not the event. `deposits` table has `invoice_id` unique; the credit transaction is `INSERT deposit ... ON CONFLICT DO NOTHING` + conditional ledger entry, all in one DB transaction. Even if BTCPay sends `InvoiceSettled` twice with different event IDs (it can, on webhook reconfiguration), the user is credited once.
- Process order-safely: handle `InvoiceSettled` and `InvoicePaymentSettled` idempotently regardless of arrival order; ignore event types we don't consume (return 200, log at debug).
- Return 200 **after** the DB transaction commits, not before. If we crash mid-processing, BTCPay redelivers and dedup handles it.

### Reconciliation poller (the safety net)

Webhooks *will* be missed (BTCPay restarts, our downtime, Cloudflare hiccup). A background task (asyncio task in-process, or the same worker that runs payout polling) every **2 minutes**:

1. `GET /api/v1/stores/{storeId}/invoices?status=Settled&startDate=<now - 48h>` — for each settled invoice with no corresponding `deposits` row → run the exact same credit path as the webhook handler (same idempotent function, one code path).
2. Same for payouts: poll payout states and reconcile `withdrawals` rows stuck in `broadcasting` state.
3. Log a **warning metric** every time the poller finds something the webhook missed — if that count is nonzero routinely, webhooks are misconfigured and someone should know before users do.

The poller is not optional or "phase 2." It is the correctness mechanism; webhooks are the latency optimization.

---

## 3. Outbound webhooks (this API → platform)

### Event types (MVP)

| Event | Fired when |
|---|---|
| `deposit.detected` | Invoice has an unconfirmed payment (optional, for UX) |
| `deposit.settled` | Ledger credited (the only one the platform must trust) |
| `withdrawal.pending_approval` | Amount above auto-limit, waiting for admin |
| `withdrawal.broadcast` | Payout sent to network |
| `withdrawal.completed` | Confirmed on-chain, ledger finalized |
| `withdrawal.failed` | Payout cancelled/failed, funds unlocked back to balance |

Payload: `{ "id": "evt_...", "type": "...", "created_at": ..., "data": {...} }` with stable event IDs so the platform can dedup — we demand dedup from BTCPay's consumers (us), so we provide the same courtesy downstream.

### Signing

- HMAC-SHA256 over `"{timestamp}.{raw_body}"`, sent as `X-CPA-Signature: t=<unix ts>,v1=<hex>`. Timestamp inside the signed string gives the platform replay-window protection (recommend they enforce ±5 min). This is the Stripe scheme; copying a well-documented scheme is a feature for an OSS project — integrators recognize it.
- Secret: separate `PLATFORM_WEBHOOK_SECRET`, random, generated at setup, distinct from every other secret.

### Delivery, retry, dead-letter

- `outbound_events` table is the queue (Postgres, no Redis/RabbitMQ on a 4GB VPS): `id`, `type`, `payload`, `attempts`, `next_attempt_at`, `status (pending|delivered|dead)`, `last_error`.
- Worker loop claims due rows with `FOR UPDATE SKIP LOCKED`, POSTs with a 10s timeout, treats any 2xx as delivered.
- Backoff: 1m, 5m, 30m, 2h, 6h, then every 12h up to 72h total (~10 attempts). Jitter ±20%.
- After exhaustion → `status=dead` + **operator alert** (section 4 channel). Dead events are not deleted; an admin endpoint `POST /admin/events/{id}/redeliver` re-queues them.
- Crucial framing in the docs: outbound webhooks are **notifications, not the source of truth**. The platform must be able to `GET /users/{id}/balance` and `GET /withdrawals/{id}` to reconcile. This sentence in the README prevents the most common integrator-built double-spend bug.

---

## 4. Hot wallet risk controls

The threat: an attacker who owns a platform API key (or the platform itself is compromised) tries to drain the hot wallet through legitimate-looking withdrawals. Controls are about **bounding the blast radius**, because on a $7/month budget we cannot prevent it — only cap it and notice fast.

### Layered limits (all configurable per asset, all enforced in the API before any Greenfield call)

1. **Per-withdrawal auto-approval threshold** (`WITHDRAW_AUTO_LIMIT_BTC=0.005`, `WITHDRAW_AUTO_LIMIT_USDT=200_000000`). Above → `pending_approval`, requires admin-scoped key + explicit approve call. Default values ship conservative.
2. **Daily payout velocity cap per asset** (`WITHDRAW_DAILY_CAP_*`): sum of all payouts (auto + approved) in a rolling 24h window. Hitting the cap forces everything to manual approval, even small amounts. This is the control that actually stops a drain via many-small-withdrawals — the auto-threshold alone does not.
3. **Per-user daily cap** (optional, default on): bounds a single compromised platform user account.
4. **Hot wallet float policy (procedural, documented):** keep only 1–3 days of expected payout volume in BTCPay's hot wallet; sweep the rest to a hardware/cold wallet manually. The API can't enforce this, but the README's "Operating safely" section must say it in bold, because the real cap on losses is *what's in the wallet*.

Velocity accounting lives in the `withdrawals` table (sum over window at request time) — no separate counter to drift out of sync.

### TRX gas monitoring

- USDT-TRC20 payouts silently fail when the hot wallet's TRX runs out — this is the most likely *embarrassing* outage. Background check every 15 min: query the wallet's TRX balance via TronGrid (`GET /v1/accounts/{addr}`); alert when below `TRX_MIN_BALANCE` (default ~200 TRX). Also alert on TronGrid request failures (free-tier rate limits / outage).

### Alerting on a $7/month budget

- One tiny `notify(severity, message)` helper that POSTs to a **Telegram bot chat and/or an ntfy.sh topic** (both free; configurable via `ALERT_TELEGRAM_BOT_TOKEN`/`ALERT_NTFY_URL`, either optional). No PagerDuty, no OpsGenie.
- Alert-worthy events (keep the list short so alerts stay meaningful): withdrawal pending approval; daily cap reached; low TRX; outbound event dead-lettered; reconciliation poller found a missed settlement; webhook signature failures > N/hour (someone probing); API 5xx spike (via a simple in-process counter); ledger invariant check failure (see §8).
- Uptime: free UptimeRobot/healthchecks.io ping on `GET /healthz` (returns 200 + DB connectivity + BTCPay reachability). That's the "the whole box is down" alarm.

---

## 5. Deployment on the Hetzner VPS

### Coexistence with btcpayserver-docker

btcpayserver-docker is opinionated: it generates its own compose stack and runs its own nginx (or traefik) bound to 80/443, with built-in Let's Encrypt. **Do not fight it — join it.** Recommended layout:

- Install btcpayserver-docker normally (it owns 80/443 and TLS).
- Run crypto-processing-api as a **separate docker-compose project** (`/opt/crypto-processing-api/docker-compose.yml`) containing: `api` (uvicorn), `worker` (poller + outbound delivery; can be the same image with a different command), `postgres:16` (its own instance — do not share BTCPay's Postgres; version/upgrade coupling and a `postgres` superuser blast radius are not worth 100MB of RAM).
- Bridge the two stacks with an **external Docker network**: btcpayserver-docker creates `generated_default`; attach the `api` container to it so (a) BTCPay can reach `http://crypto-api:8000` for webhooks internally, (b) the API reaches BTCPay's Greenfield at `http://btcpayserver:49392` without leaving the box. No Greenfield traffic over the public internet at all.
- Expose the API publicly via btcpayserver-docker's supported custom-nginx include mechanism (an extra server block / `nginx-mounted-conf` for `api.example.com` proxying to `crypto-api:8000`). Alternative if that proves brittle across btcpay updates: bind the API to `127.0.0.1:8000` and run a tiny separate Caddy on a different port — but first choice is riding the existing nginx.

RAM budget on 4GB: BTCPay + pruned bitcoind + NBXplorer + its Postgres ≈ 2.5–3GB. Our stack must fit in ~700MB: uvicorn single worker (`--workers 1`, async handles the concurrency; this is one platform's traffic), Postgres with `shared_buffers=128MB`, worker process ~80MB. Add a 2GB swapfile — bitcoind's initial sync will OOM the box otherwise. This goes in the deployment doc because "my VPS died during IBD" is the #1 support issue we'd otherwise get.

### Cloudflare specifics

- `api.example.com` **orange-clouded** (proxied). Everything—including any inbound webhook path if it's ever exposed—goes through it. BTCPay's own domain: per BTCPay docs (grey-cloud is often recommended for BTCPay; that's their problem, not ours).
- **Restore real client IPs:** nginx `set_real_ip_from` for Cloudflare's published IP ranges + `real_ip_header CF-Connecting-IP`. Without this, rate-limiting and abuse logs see only Cloudflare IPs. Trust `CF-Connecting-IP` **only** when the peer is in Cloudflare's ranges — otherwise it's spoofable.
- **Caching:** API responses must never be cached. Cache Rule: `api.example.com/* → Bypass cache`. Also send `Cache-Control: no-store` from FastAPI middleware on every response — defense against future Cloudflare config mistakes. An accidentally cached `GET /users/x/balance` served to the wrong request is an embarrassment with a screenshot.
- **Lock the origin:** the origin's 443 should only accept Cloudflare. With ufw, allow 443 only from Cloudflare's IP ranges (scripted, refreshed monthly via cron since the ranges change rarely but do change). Otherwise attackers bypass Cloudflare (and its rate limits) by hitting the origin IP directly — origin IP is discoverable via bitcoind's P2P port anyway.
- Optional hardening (documented, not default): Cloudflare WAF rule requiring a secret header set by a Cloudflare Transform Rule, so origin nginx can verify traffic really transited Cloudflare.

### ufw baseline

```
default deny incoming; default allow outgoing
allow 22/tcp        # SSH — key-only auth, PasswordAuthentication no; optionally restrict to home IP
allow 8333/tcp      # bitcoind P2P (needed for sync; pruned node)
allow from <CF ranges> to any port 443 proto tcp
# NOT exposed: 80 (only if btcpay's LE needs HTTP-01 — then allow 80 from CF ranges too),
# 5432 (both Postgres instances — internal only), 8000 (API — internal only),
# 49392 (Greenfield — internal only), 8332 (bitcoind RPC — internal only)
```

**Docker/ufw footgun (must be in the docs):** Docker's iptables rules bypass ufw for published ports. Any `ports: "5432:5432"` in compose is internet-exposed regardless of ufw. Rule: publish nothing except through the nginx path; use `expose:`/internal networks, or bind to `127.0.0.1:`. This exact mistake has drained real hot wallets (exposed Redis/Postgres → RCE → wallet).

Also: unattended-upgrades on, fail2ban for sshd, and **nightly `pg_dump` of the ledger** shipped off-box (rclone to any free object storage / Hetzner Storage Box €3 — the one place we bend the budget, because a dead disk with no ledger backup is both robbed *and* embarrassed).

---

## 6. Secrets

### Inventory

| Secret | Used for |
|---|---|
| `DATABASE_URL` | Postgres (contains password) |
| `BTCPAY_API_KEY` | Greenfield calls (scoped: invoice create/view, payout create — **not** server admin) |
| `BTCPAY_WEBHOOK_SECRET` | Verifying inbound webhooks |
| `PLATFORM_WEBHOOK_SECRET` | Signing outbound webhooks |
| Platform API keys | Only hashes in DB; plaintext exists nowhere |
| `ALERT_TELEGRAM_BOT_TOKEN` | Alerts (low sensitivity, still a secret) |

### Handling

- Runtime config via env vars only, loaded by `pydantic-settings` from `/opt/crypto-processing-api/.env`, `chmod 600`, owned by the deploy user. No secrets in `docker-compose.yml` itself (compose reads `env_file:`).
- Repo ships **`.env.example`** with every variable, safe placeholder values, and a one-line comment each — including the non-secret config (limits, thresholds) so it doubles as the configuration reference. `.env` in `.gitignore` from commit #1. Add a CI check (gitleaks or trufflehog action) so a leaked secret fails the build — public repo + crypto project = actively scanned by bots within minutes of any push; a leaked Greenfield key is a same-day wallet drain.
- Greenfield API key scoping: create it with the minimum Greenfield permissions (`btcpay.store.cancreateinvoice`, `btcpay.store.canviewinvoices`, `btcpay.store.canmanagepullpayments`/payout perms) for the one store. Never use a server-admin key; document this in setup.

### Logging policy

- **Never logged:** full API keys (log `key_id` prefix only), any secret env value, full webhook signature headers, `Authorization` headers, DB URLs.
- **Logged but truncated:** crypto addresses as `bc1qxy...k7f2` (first 6 / last 4). Full addresses live in the DB where they belong; logs get copied into GitHub issues by users asking for help — that's the leak path this policy defends.
- **Amounts:** logged in full at INFO for deposits/withdrawals — operators need them for incident response, and amounts alone (without user identity linkage in the same line) are acceptable for a custodial operator's own logs. Policy knob `LOG_REDACT_AMOUNTS=true` for operators who disagree. External user IDs are opaque platform strings — log them (they identify nothing without the platform's DB).
- Implementation: structlog with a **redaction processor** applied at the pipeline level (denylist of key names: `*key*`, `*secret*`, `*token*`, `*password*`, `*signature*`) — redaction that relies on every call site remembering is redaction that fails. Uvicorn access log: disable default, use our own middleware that logs method/path/status/duration/key_id and strips query strings.
- FastAPI error responses: generic 500 body, never a stack trace (`debug=False` enforced when `ENV=production`; refuse to boot otherwise).

---

## 7. Open-source repo packaging

### Directory layout (src layout)

```
crypto-processing-api/
├── src/crypto_processing_api/
│   ├── main.py                 # FastAPI app factory
│   ├── config.py               # pydantic-settings, fail-fast validation
│   ├── cli.py                  # create-api-key, run-migrations, etc.
│   ├── api/                    # routers: deposits, withdrawals, balances, admin, webhooks, health
│   ├── core/                   # auth (key hashing/verify), signing (HMAC in+out), redaction
│   ├── ledger/                 # THE money code: models, service, invariants — isolated & maximally tested
│   ├── gateway/                # btcpay Greenfield client + webhook payload models
│   ├── workers/                # reconciliation poller, outbound delivery, gas monitor
│   └── alerts/                 # telegram / ntfy notifier
├── migrations/                 # alembic
├── tests/
│   ├── unit/
│   └── integration/            # against dockerized postgres; regtest e2e marked slow
├── deploy/
│   ├── docker-compose.yml          # production app stack
│   ├── docker-compose.regtest.yml  # full local BTCPay+bitcoind regtest stack
│   └── nginx/, ufw/                # snippets referenced by docs
├── docs/                       # deployment.md, security.md, api.md, integrating.md
├── .github/workflows/ci.yml, release.yml
├── .env.example  .gitignore  .pre-commit-config.yaml
├── pyproject.toml  Dockerfile  LICENSE (MIT)
├── README.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
```

Src layout specifically because it forces installed-package imports in tests (catches packaging bugs) and is the current Python packaging guidance. `ledger/` is deliberately dependency-free of FastAPI/BTCPay so its tests are pure and fast.

### README structure (the storefront — most embarrassment risk per file)

1. One-paragraph pitch + explicit **"custodial — you hold user funds; read the security docs"** warning box up top. 2. Feature/non-feature table (no Lightning, no fiat — say it before issues ask). 3. Architecture diagram (platform ↔ API ↔ BTCPay ↔ chains). 4. Quickstart: regtest compose up → create key → curl a deposit → settle → check balance, in ≤10 commands. 5. Production deployment link. 6. API overview + link to generated OpenAPI docs. 7. **Security model & threat model link** — publishing the honest threat model (§8) builds more trust than pretending. 8. Status: "beta, not audited, cap your hot wallet" — honesty is the anti-embarrassment strategy. 9. License.

Also **SECURITY.md**: private vulnerability reporting via GitHub advisories, no bug bounty (say so), 90-day disclosure.

### CONTRIBUTING.md

Dev setup (`uv sync` or pip, pre-commit install, regtest stack), test commands, conventional-commit requirement, "money-path changes require tests + a ledger-invariant argument in the PR description," DCO or plain MIT inbound=outbound.

### CI (GitHub Actions, single `ci.yml`)

Jobs: **lint** (`ruff check` + `ruff format --check`), **types** (`mypy --strict` on `src/`), **test** (pytest with Postgres service container; coverage floor ~85% on `ledger/`), **secrets** (gitleaks), **docker** (buildx build, no push, on PRs). All required for merge. Weekly `pip-audit`/Dependabot for dependency CVEs. Optional nightly job runs the regtest e2e (too slow for every PR).

### Pre-commit

`ruff` (lint+format), `mypy` (or leave to CI if too slow), `gitleaks`, end-of-file/trailing-whitespace, `check-added-large-files`.

### Versioning, changelog, images

- **SemVer** with 0.x during beta; API-breaking changes bump minor pre-1.0 and are called out in CHANGELOG. **Keep a Changelog** format; conventional commits make it semi-automatable (release-please is fine, but a hand-edited changelog is acceptable at this scale).
- `release.yml` on tag `v*`: run tests → build multi-arch image (**linux/amd64 + linux/arm64** — the CAX11 target is ARM; shipping amd64-only would be the flagship deploy target not working) → push to **GHCR** (`ghcr.io/<org>/crypto-processing-api:{v1.2.3, 1.2, latest}`) → generate SBOM + provenance attestation (free flags on `docker/build-push-action`). Dockerfile: multi-stage, `python:3.12-slim`, non-root user, `HEALTHCHECK`.

---

## 8. Threat model (MVP-honest)

| # | Threat | Vector | Impact | MVP mitigations | Accepted residual risk |
|---|---|---|---|---|---|
| 1 | **Webhook spoofing / replay** | Forged `InvoiceSettled` → mint fake balance, then withdraw real crypto | Direct theft | HMAC verify on raw body; event-ID + invoice-ID dedup; webhook path internal-only on same box; credits reconciled against Greenfield invoice state by poller | Compromise of BTCPay itself (see #5) |
| 2 | **Platform API key leak** | Key in platform's logs/repo/env leak → attacker creates withdrawals to own addresses | Bounded theft | Hashed at rest; prefix for secret-scanning; auto-approval threshold; daily velocity caps; per-user caps; alert on approval-pending & cap-hit; instant revocation + multiple active keys for rotation | Attacker drains up to daily cap before operator reacts — the cap **is** the loss ceiling; set it accordingly |
| 3 | **SQL injection** | Crafted external_user_id / address / memo strings | Ledger tampering, data theft | SQLAlchemy bound parameters everywhere; zero string-built SQL (CI grep for `text(` usage requires review); Pydantic validation (length/charset on IDs, address format per asset); least-privilege DB role (app user: no DDL outside migrations, no superuser) | Low — ORM discipline makes this mostly a review problem |
| 4 | **Insider / host-level ledger tampering** | Anyone with DB access UPDATEs balances | Silent theft | Append-only double-entry design: balances derived from immutable `ledger_entries`, each entry linked to a deposit/withdrawal record; **nightly invariant job**: Σ(entries) == balances table, Σ(user balances) ≤ on-chain wallet holdings (via Greenfield), alert on mismatch; off-box backups enable forensics | Root on the VPS can rewrite history between checks. Real fix (external anchoring/HSM) is out of MVP scope — documented as such |
| 5 | **BTCPay server compromise** | BTCPay (same box) owned → hot wallet keys stolen or payout API abused | Total hot-wallet loss | **Primary control is cash management: small hot wallet float, manual cold sweeps** (documented procedure); scoped Greenfield key limits what *our* API can be tricked into; velocity caps limit abuse via our payout path; ufw/no-exposed-ports reduces attack surface | If BTCPay/the box is owned, hot wallet is gone. MVP answer: keep it small. Say this in the README verbatim |
| 6 | **TronGrid outage / rate limit** | Free public endpoint down or throttled | USDT deposits/payouts delayed (availability, not theft) | Reconciliation poller retries; payout states machine tolerates delay (no timeout-then-retry double-send — payouts keyed by idempotent payout ID); gas monitor alerts on TronGrid errors; document paid-key upgrade path | Hours-long USDT delays during outages. Acceptable: funds stall, they don't vanish |
| 7 | **Double-withdrawal via race** | Concurrent withdrawal requests vs. one balance | Overdraft = theft from operator | `SELECT ... FOR UPDATE` on balance row + debit-and-lock in one transaction *before* calling Greenfield; DB CHECK constraint `balance >= 0` as the last line; idempotency keys on the endpoint | Effectively closed at the DB level |
| 8 | **Origin bypass of Cloudflare** | Direct-to-IP attacks skip WAF/rate limits | DoS, brute force | ufw allows 443 from CF ranges only; API key auth means brute force needs 256-bit luck anyway | bitcoind port still reveals the IP; acceptable |
| 9 | **Dependency / supply chain** | Malicious or vulnerable PyPI package | Anything | Locked deps (`uv.lock`/hash-pinned), Dependabot + pip-audit in CI, minimal dependency set, GHCR images built from lockfile with SBOM | Zero-days in FastAPI/SQLAlchemy — same risk as everyone |

Cross-cutting honesty statement for the docs: **the MVP's real security budget is (a) how little sits in the hot wallet and (b) how fast the operator sees an alert.** Every control above serves one of those two.

---

## Implementation sequencing note (for the build phase)

Security-relevant build order: ledger schema + invariants → inbound auth → webhook ingress with dedup → reconciliation poller → withdrawal limits/state machine → outbound webhooks → alerts → deploy docs. The regtest compose stack comes first of all, because every one of these is only trustworthy with an end-to-end regtest test proving it.

### Critical Files for Implementation

(To be created — repository is currently empty; paths per the layout in §7.)

- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\core\auth.py — API key generation, SHA-256 hashing, constant-time verification, scopes
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\api\webhooks.py — raw-body HMAC verification, event dedup, idempotent credit path
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\ledger\service.py — double-entry ledger, FOR UPDATE balance locking, invariant checks
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\src\crypto_processing_api\workers\reconciliation.py — Greenfield polling safety net + outbound delivery/backoff
- E:\codespace\_claude_code\_swift-punk-projects\crypto-processing-api\deploy\docker-compose.yml — production stack, external network bridge to btcpayserver-docker, no published DB ports