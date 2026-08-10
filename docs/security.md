# Security model

## The honest summary

> **The MVP's real security budget is (a) how little sits in the hot wallet and
> (b) how fast the operator sees an alert.**

Every control below serves one of those two. This is a custodial service on a
seven-dollar-a-month box; it does not have an HSM, a security team, or
twenty-four-hour monitoring, and pretending otherwise would be the most
dangerous thing in this document.

> **This file states the claims. [`SECURITY-AUDIT.md`](../SECURITY-AUDIT.md) is
> the evidence** — for every threat below, the file that implements the control,
> the tests that prove it, when that was last true, and what is left unmitigated.
> If you are auditing rather than reading, start there.

## Hot wallet float policy

**The float is the loss ceiling.** If BTCPay or the host is compromised, the
hot wallet is gone — no control in this service changes that.

So:

- keep **one to three days of expected payout volume** in the BTCPay hot wallet
- sweep the rest to cold storage by hand, on a schedule you actually keep
- set `SEED_BTC_WITHDRAWAL_DAILY_CAP` to what you can afford to lose in a day,
  because under a stolen API key that number *is* the loss

### Cold sweep runbook

There is no automated sweeping in the MVP; automating an outbound path to a
cold wallet means keeping something signable next to the thing you are
protecting against.

1. Read the wallet balance: `GET /v1/admin/reconciliation`, `chain_balance`.
2. Compute the float you want to keep: a few days of payouts, plus room for the
   in-flight amount the same report shows.
3. Send the excess from BTCPay's UI (Wallets > Send) to your cold address.
   Verify the address on the hardware device's own screen.
4. Wait for a confirmation, then re-read `/v1/admin/reconciliation`.

The report will now show `chain_balance` below `ledger_custody`, and if the
sweep took the wallet below what users are owed it will be flagged
`insolvent: true` with a `custody.insolvency_signal` alert. **That is correct
and it is your check on yourself**: never sweep below `user_obligations`.

## Threat model

| # | Threat | Vector | Impact | Mitigations here | Accepted residual |
|---|---|---|---|---|---|
| 1 | **Webhook spoofing** | Forged `InvoiceSettled` mints a balance, attacker withdraws real coins | Direct theft | HMAC over raw bytes with `compare_digest`; dedup on `originalDeliveryId`; a second dedup at the ledger on `(kind, source_ref)`; the webhook path is container-internal; every credit re-fetches the amount from Greenfield rather than trusting the payload | Compromise of BTCPay itself, which is #5 |
| 2 | **Platform API key leak** | Key in the platform's logs or repo; attacker creates withdrawals to their own address | Bounded theft | SHA-256 at rest; `cpk_live_` prefix for secret scanners; per-withdrawal auto-approval limit; **rolling 24h per-asset cap, serialized on one row so concurrency cannot bypass it**; optional per-user cap; destination validation; alerts on approval-pending and cap-hit; instant revocation with multiple active keys | The attacker drains up to the daily cap before you react. **The cap is the loss ceiling — set it accordingly** |
| 3 | **SQL injection** | Crafted `external_user_id`, address or memo | Ledger tampering | SQLAlchemy bound parameters throughout; no string-built SQL in the money path; Pydantic length and format validation; address checksums | Low. ORM discipline makes this a review problem, and the money path is small enough to review |
| 4 | **Insider or host-level tampering** | Someone with database access UPDATEs a balance | Silent theft | Append-only journal with BEFORE UPDATE/DELETE triggers that raise; balances derived from immutable postings; hourly Job C compares materialized against derived and alerts on any drift; off-box backups enable forensics | Root on the VPS can rewrite history between checks. External anchoring is out of MVP scope, and saying so is the honest answer |
| 5 | **BTCPay compromise** | BTCPay on the same box is owned; hot wallet keys stolen | Total hot-wallet loss, plus the Lightning channel balance if Lightning is on | Cash management is the control: small float, manual cold sweeps; the Greenfield runtime key is scoped to one store and is never server-admin; velocity caps limit abuse through our payout path, per asset, so Lightning has its own ceiling; nothing is published to the internet | **If the box is owned, the hot wallet is gone.** The answer is to keep it small. With `LIGHTNING_ENABLED=true` the bootstrap key also holds one server-level scope — see below |
| 6 | **TronGrid outage or rate limit** | Free tier throttled or down | USDT delayed, not lost | Reconciliation retries; the withdrawal state machine tolerates delay without a timeout-then-retry double-send; the gas monitor alerts on a failure streak; a paid key is a documented upgrade | Hours-long USDT delays. Funds stall, they do not vanish |
| 7 | **Double withdrawal via race** | Concurrent requests against one balance | Overdraft, which is theft from the operator | `SELECT ... FOR UPDATE` in ascending id order, decision and reservation in one transaction; `no_overdraft` CHECK as the database-level backstop; idempotency keys; **a test with real threads proves exactly one of two racing debits succeeds** | Effectively closed at the database level |
| 8 | **Cloudflare origin bypass** | Direct-to-IP requests skip the WAF | DoS, brute force | ufw allows 443 only from Cloudflare ranges, refreshed by cron; API keys carry 256 bits, so brute force is not a threat | The bitcoind port reveals the origin IP. Accepted |
| 9 | **Supply chain** | A malicious or vulnerable dependency | Anything | Exactly pinned direct dependencies; a deliberately small dependency set; gitleaks over full history; SBOM and provenance attestation on published images | Zero-days in FastAPI or SQLAlchemy — the same risk everyone carries |
| 10 | **Reorg after credit** | A deposit is credited, withdrawn, then the deposit tx is orphaned | Real loss | Settlement pinned at 1 confirmation minimum, never 0-conf; `user_deficit` exists so the loss can be *booked* rather than being rejected by the overdraft CHECK; see [`runbook-reorg.md`](runbook-reorg.md) | A deep reorg still costs money. The books stay correct, which is what makes recovery possible |

## The one server-level permission, and why it is off by default

Every Greenfield scope this project requests is pinned to one store. There is
exactly one exception, and it only exists if you ask for it.

Enabling the `BTC-LN` payment method on a store requires
`btcpay.server.canuseinternallightningnode`. It is **server-level**: BTCPay
answers 403 to a store-scoped key, so there is no narrower way to do it through
the API. That is why `LIGHTNING_ENABLED` defaults to false — the default is the
security decision, not a convenience. With it off, the scope lists are the exact
strings a deployment that has never heard of Lightning gets, and a test asserts
that rather than a comment claiming it.

Three things narrow the exception when you do turn it on:

- **The runtime key never holds it.** It is a bootstrap scope, used once to
  attach the node to the store. The key the service carries day to day gets
  `btcpay.store.canuselightningnode` — reading a channel balance and a payment
  fee — and nothing more.
- **What it grants is use of the server's internal Lightning node.** On a
  single-tenant box that is the same node this store would be using anyway, so
  the practical widening is small. On a BTCPay shared with other stores it is
  real, and you should decide about it deliberately.
- **You can keep the bootstrap key out of the running deployment.** It lives in
  `.env.regtest.generated` for the dev stack; in production, delete it after the
  setup run and re-issue when you next need to change store settings.

### The second float

Turning Lightning on creates a second pot of money with its own loss ceiling.
`SEED_LN_WITHDRAWAL_DAILY_CAP` bounds it and `SEED_BTC_WITHDRAWAL_DAILY_CAP`
says nothing about it — a stolen API key can drain up to each cap independently.
Channel balance also cannot be swept to cold storage the way wallet balance can:
what is committed to a channel stays there until the channel is closed. Size it
as the amount you are willing to have permanently warm.

## What protects the money, concretely

**Inbound auth.** `cpk_live_` / `cpk_test_` plus 32 random bytes in base62. The
first eight characters are a non-secret `key_id` for lookup and logs; the full
key is stored as SHA-256. With 256 bits of entropy a slow password hash buys
nothing — there is no dictionary — so the effort goes into constant-time
comparison and a lookup that costs the same for an unknown key as a wrong one.
Two scopes: `readwrite` and `admin`. Rotate by minting a second key and
revoking the first; both work at once.

**Webhook ingress.** The signature covers the exact bytes that arrived. A
verifier that parses and re-serializes passes every test written with
`json.dumps` on both sides and fails against real BTCPay — there is a test
sending the same JSON with different whitespace to keep it that way. A bad
signature is logged without the body, so an attacker cannot write into the logs.

**The ledger.** Double-entry, append-only, with database triggers that raise on
UPDATE and DELETE. Corrections are reversal entries. Balances are materialized
inside the same transaction as the postings, and Job C compares them hourly
against the sum of postings — alerting, never repairing, because a job that
silently fixes the books destroys the evidence.

**Secrets in logs.** A structlog processor redacts anything matching `*key*`,
`*secret*`, `*token*`, `*password*`, `*signature*` at every nesting depth, and
truncates addresses. It is a pipeline stage, not a rule to remember, so a new
call site cannot leak by omission. `key_id` is explicitly allow-listed because
it is the thing that is safe to log.

**Refusing to start.** `DEBUG=true` outside development, a non-mainnet Bitcoin
network in production, an auto-approval limit above the daily cap, a platform
webhook URL with no secret — all fatal at startup. A misconfigured custodial
service should not run.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md). Please do not open a public issue.

## What this service is not

- not multi-tenant. One platform, one store, one set of keys.
- not a wallet. BTCPay holds the keys; this holds the ledger.
- not HSM-backed, not SOC 2, not audited.
- not a substitute for the platform reconciling against the read endpoints.

If you are custodying an amount where those matter, this is a starting point to
build from, not a thing to deploy as-is.
