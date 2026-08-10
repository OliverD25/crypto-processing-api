# Adversarial Review — crypto-processing-api Implementation Plan

The ledger core (double-entry, `source_ref` idempotency, reconciliation-as-truth) is well designed. The failures below are concentrated where the plan hand-waves: global concurrency, the afterlife of deposit addresses, refund transitions, and reorg reality.

---

## 1. CRITICAL — Per-asset 24h velocity cap is raceable; the plan's primary drain control does not work under concurrency

**Scenario.** The cap is computed "by summing `withdrawals` over rolling windows … in the same snapshot" inside the hold transaction. The `FOR UPDATE` locks taken there are the *requesting user's* two account rows only. Two (or fifty) concurrent withdrawal requests from *different* users each lock disjoint rows, each run `SUM(withdrawals)` under READ COMMITTED, each see the pre-cap total, and all pass the gate simultaneously. The plan explicitly calls this cap "the control that actually stops a many-small-withdrawals drain" — i.e., its threat model is an attacker holding the stolen platform API key. That attacker controls request concurrency by definition: fire N withdrawals across N user accounts in one burst and the cap that should have flipped everything to manual approval never trips. The per-user cap is accidentally race-free (same-user requests serialize on the user's account row); the per-asset cap — the one that bounds the hot wallet — is not.

**Fix.** Serialize the per-asset gate: `pg_advisory_xact_lock(hash(asset_id))` (or `SELECT … FOR UPDATE` on the asset's `hot_wallet` account row, which every credit already contends on) *before* summing. Cheap at this traffic level. Add a concurrency test to M3's done-criteria: N parallel withdrawals across distinct users must never exceed the cap in aggregate.

## 2. HIGH — `failed → refunded` release with no broadcast guard is a double-pay path

**Scenario.** The state machine says "rejected / failed ──(release entry)──> refunded" with no restriction on how `failed` was reached. A BTC payout enters `broadcast` (txid known, tx in mempool), fee spikes, tx lingers; an operator cancels the payout in BTCPay's UI, or BTCPay reports a state Job B maps to `failed`. The API posts `withdrawal_release`, user's available balance is restored — then the original tx confirms anyway. User has both the balance and the coins; hot wallet is down the full amount. Same trap on manual USDT: admin marks failed, refunds, operator's earlier send confirms.

**Fix.** Make release from any post-`submitted` state a manual, admin-only action that requires an explicit attestation ("txid verified double-spent/never broadcast"), never an automatic mapping from backend status. Automatic release is only legal from `requested`/`pending_approval`/`approved`/`submitting`-with-no-payout-found. Encode this in the transition legality matrix and test it.

## 3. HIGH — USDT pool address reuse silently auto-credits the wrong user

**Scenario.** User A gets pool address X; invoice expires after the plan's deliberately short 30 minutes; X returns to the pool; user B's new invoice reserves X; user A pays 40 minutes late (mempool delay, slow wallet, saved address). From BTCPay's perspective this is an **in-window payment on B's invoice** — `afterExpiration` is false, the REVIEW routing never fires, and the plan's own policy *auto-credits user A's money to user B*. The 30-minute USDT expiry chosen "to recycle the pool" makes the collision window worse, and the late-payment runbook only covers payments BTCPay flags as late — this one isn't flagged at all.

**Fix.** (a) Add a quarantine period before an address returns to the pool (if the plugin supports it; if not, that's a hard constraint to verify in Q5/Q6). (b) Lengthen, not shorten, USDT expiry and size the pool up instead. (c) For USDT only, route payments whose amount deviates from `amount_expected` beyond a tolerance to REVIEW — crude, but it catches most cross-attribution. (d) Document that USDT deposit attribution is heuristic; log address reservation windows (deposit_id, address, from, to) so the runbook's manual attribution is actually possible.

## 4. HIGH — The deposit-address afterlife is unmonitored: settled deposits leave the truth path, and BTCPay itself stops watching after `MonitoringExpiration`

**Scenario A.** Job A sweeps "all non-terminal deposits (+ expired/review < 7 days)". `settled` is terminal — so a second payment to a settled top-up invoice's address (users habitually reuse saved BTC addresses) is credited only if the `InvoicePaymentSettled` webhook arrives. If the API is down at that moment, nothing ever polls that invoice again. This directly violates the plan's own guiding rule that polling is the correctness mechanism.
**Scenario B.** Worse: BTCPay only attributes payments to an invoice within its `MonitoringExpiration` window (default ~24h after expiry). A user who pays a week-old address deposits funds that land in the hot wallet attributed to *no invoice at all* — the 7-day poll of expired deposits is theater beyond day 1, the nightly invoice page-through sees nothing (there's no invoice-level record), and the only detector is Job C's aggregate `wallet > Σ balances` check, which reads a *surplus* as healthy. Funds in custody, permanently uncredited, invisible.

**Fix.** (a) Include `settled` deposits in Job A for N days post-settlement. (b) Set `MonitoringExpiration` explicitly and align the poll window to it — polling past it is meaningless. (c) Add a wallet-level detector: enumerate on-chain wallet transactions (Greenfield wallet API / NBXplorer) and flag any receiving txo not matched to a `deposit_payments` row → REVIEW. This is the only mechanism that catches Scenario B. (d) Document loudly to the platform: deposit addresses are single-use, with the caveat that documentation never stopped a user.

## 5. HIGH — No mainnet confirmation policy, and the schema cannot represent a post-credit reorg loss

**Scenario.** Credit fires on `InvoicePaymentSettled`, which fires at whatever the store's SpeedPolicy says. The plan pins the *regtest* bootstrap to "1-conf speed policy" and says nothing about mainnet. If the production bootstrap ships the same, the sequence is: 100M-sat deposit → 1 conf → credited → immediate withdrawal request (auto-approved if split under the limit) → 1-block reorg drops the deposit tx (or an RBF replacement wins under a 0-conf policy). Now try to reverse: the `reversal` entry must debit `user_available`, but the user already withdrew — `no_overdraft CHECK` **rejects the reversal transaction**. The loss cannot even be booked. Same CHECK problem bites the `external` account, whose corrections legitimately need to swing both signs.

**Fix.** (a) Pin mainnet SpeedPolicy explicitly (≥1 conf BTC minimum) and add per-asset, ideally per-amount, confirmation thresholds — or a configurable withdrawal delay after deposit credit ("credited N confs ago") which is cheaper to build. (b) Exempt `external` from the balance CHECKs and add a `user_deficit` (debit-normal receivable) account so a reorg loss on an already-spent balance is representable: `DR user_deficit / CR hot_wallet`. (c) Write the reorg runbook now, not after the first incident.

## 6. HIGH — Stuck-`submitting` reconciliation matches payouts by (destination, amount, window): ambiguous exactly when it matters

**Scenario.** User withdraws 0.01 BTC to their own wallet twice in ten minutes (completely normal behavior). W1's BTCPay call succeeds but the API crashes before commit → W1 stuck in `submitting`, payout P1 exists with no recorded `backend_ref`. W2 proceeds normally → P2. Reconciliation now sees two payouts and one stuck row where (dest, amount, window) matches *both* — or, in the crash ordering where only P1 exists, it can bind P1 to W2 and conclude W1 has no payout, clearing it for resubmission → double send. The one reliable disambiguator, `metadata.withdrawal_id` on the payout, is dismissed as "convenience only" and flagged UNVERIFIED for the pinned version.

**Fix.** Make payout metadata (or any echoable unique field) the *load-bearing* correlation key, verified against the pinned BTCPay tag as a hard M3 blocker — if the version can't echo it, the fallback is "never auto-resolve ambiguous matches; freeze both rows for admin resolution", stated explicitly. Additionally, before any resubmission of a `submitting` row, require zero unmatched payouts for that (dest, amount) in the window, not merely "no match found for this row".

## 7. HIGH — Nightly `pg_dump` gives a 24h RPO on a custodial ledger, and manual USDT withdrawals are unreconstructable

**Scenario.** Disk dies at 22:00; last dump was 03:00. Deposits since 03:00 are recoverable from BTCPay invoices; BTC payouts from BTCPay payout history. But: admin REVIEW credits, adjustments, holds, and — fatally — **manual USDT withdrawals** (`backend_ref='manual:<uuid>'`) exist *only* in this database. The TRON chain shows outflows from the hot wallet with no record of which user they belonged to. You restore, and some users' balances silently un-debit money that already left custody; the operator eats it or hand-reconciles TronScan against memory.

**Fix.** Continuous WAL archiving (wal-g/pgBackRest) to the Storage Box — the same €3/month buys minutes of RPO instead of a day. If genuinely impossible, hourly dumps plus a written restore-reconstruction runbook that enumerates which record types have external sources of truth and which don't. For a money DB this is not "hardening", it's M1.

## 8. MEDIUM — BTC fee estimate assumes 200 vB; a custodial deposit wallet guarantees the opposite

**Scenario.** The hot wallet accumulates hundreds of small deposit UTXOs (that's what a deposit wallet *is*). BTCPay's payout processor builds transactions spending many inputs (~68 vB each for P2WPKH). A payout consuming 8 inputs runs ~600+ vB; the user was charged for 200. The plan says drift "stays in the hot wallet (safe direction)" — but this drift is systematically *negative*: the float bleeds on every withdrawal, and Job C's insolvency margin erodes precisely as volume grows. Batching offsets it only when multiple payouts coincide in one interval.

**Fix.** Raise the fixed vsize assumption and make it configurable per deployment; better, price fees off recent actual payout vsizes (rolling average from `paymentProof` txs). Book estimate-vs-actual drift periodically as an explicit `network_fee_expense` adjustment entry (see #9) instead of letting it accumulate as unexplained wallet divergence.

## 9. MEDIUM — `payouts_in_flight` exists in the schema but no entry ever posts to it, so Job C's insolvency check needs slop from day one

**Scenario.** Between `broadcast` and `confirmed`, real coins have left the BTCPay wallet but ledger `hot_wallet` still carries them (settle posts only at `confirmed`). Add actual-vs-estimated miner fee drift (#8), which is never booked anywhere, and "ledger hot_wallet vs live wallet" diverges by a growing, unexplained amount. The operator either tunes the alert threshold loose (blinding the one real insolvency signal) or drowns in false positives.

**Fix.** Either use the account the schema already defines — post `DR payouts_in_flight / CR hot_wallet` for net+estimated-fee at submission, resolve at confirmation with the drift explicitly booked to `network_fee_expense` — or delete the account and have Job C compute expected divergence = Σ(in-flight net + est. fees) + accumulated booked drift. Any tolerance in the insolvency check must be *derived*, not a hand-tuned epsilon.

## 10. MEDIUM — Idempotency keys stuck `in_progress` after a crash produce an infinite 409 loop on money operations

**Scenario.** `POST /v1/deposits` inserts the idempotency row (`in_progress`), commits the `creating` deposit, then the process dies during the BTCPay call. Reconciliation heals the *deposit*, but the idempotency row stays `in_progress` until the 72h TTL purge. The platform retries with the same key (as instructed) and receives 409 + Retry-After for three days, with no way to learn the deposit_id it actually owns. Withdrawals: platform can't tell whether a hold exists.

**Fix.** Add a staleness takeover: `in_progress` older than T (say 60s) may be reclaimed by a retry, with the handler made re-entrant (deposit: look up existing `creating` row by idempotency key linkage; withdrawal: the hold's own idempotent creation). Store the created resource id on the idempotency row at first commit so a takeover can return it even before `completed`.

## 11. MEDIUM — Admin REVIEW resolve credits an operator-supplied `amount_units` instead of Greenfield truth

**Scenario.** `POST /v1/admin/deposits/{id}/resolve {action:"credit", payment_id, amount_units}` — the whole deposit pipeline insists "webhook payloads are triggers, never amount sources", then the human-driven path takes a free-text amount. Admin fat-fingers an extra zero at 2am clearing the review queue; the ledger happily credits 10x into `user_available`, and the unique `source_ref` now *prevents* the correct credit from ever posting.

**Fix.** Drop `amount_units` from the request. Server fetches the payment from Greenfield and credits that amount; the admin only confirms attribution (`deposit_id`/`payment_id`). For the genuinely-manual USDT attribution case, require a second confirmation echoing the server-fetched amount.

## 12. MEDIUM — TronGrid confirmation of manual USDT withdrawals verifies the wrong predicate

**Scenario.** "TronGrid poller verifies inclusion + success" of the operator-pasted txid. Operator pastes the wrong txid (a previous withdrawal's, or a TRX gas top-up tx). It's included and successful → withdrawal marked `confirmed`, settle entry posted — but the user's USDT never moved, and the tx that *should* have been sent may or may not exist. Also, "success" on TRON must mean the TRC20 `Transfer` executed, not merely that the tx was included — an out-of-energy contract execution is included *and* failed.

**Fix.** Validate the full tuple: contract address == USDT-TRC20 mainnet contract, `from` == hot wallet, `to` == `destination_address`, amount == `amount_net`, receipt result == SUCCESS with the Transfer event present. Reject the mark-broadcast otherwise. Also enforce txid uniqueness across withdrawals (the schema's `txid` column has no unique index — add one, partial on NOT NULL).

## 13. LOW — No destination address validation at request time

**Scenario.** Platform passes a testnet address, an Ethereum-format USDT address, or garbage. The hold is placed, the withdrawal sits in the queue, and it fails only at BTCPay payout creation (BTC) or at the human operator (USDT) — user funds locked and support tickets generated for what should have been a 422.

**Fix.** Validate at `POST /v1/withdrawals`: BTC bech32/base58 checksum + network prefix; TRON base58check `T…` format. Reject the store's own deposit addresses while you're at it (self-deposit loops burn miner fees, and withdrawing to an *expired* invoice address feeds finding #4's black hole).

## 14. LOW — `/healthz` couples API liveness to BTCPay reachability

**Scenario.** BTCPay restarts for an upgrade (routine on this shared box); `/healthz` fails; the uptime pinger pages, and if compose healthchecks gate on it, the API restarts too — while balances/reads/ledger are perfectly healthy.

**Fix.** `/healthz` = process + DB only; report BTCPay/TronGrid reachability as component statuses on a separate `/readyz` or in the admin reconciliation endpoint, alerted at lower severity.

## 15. LOW — Fee/config duplication and outbound-event transactionality unspecified

`assets.withdrawal_flat_fee` (DB) and `USDT_WITHDRAWAL_FEE_MICROS` (env) describe the same number — two sources of truth will drift; pick the DB row, seed it from env once. And the plan never states that `outbound_events` inserts happen in the same transaction as the ledger change they announce — if not, a crash emits balances the platform never hears about (or vice versa). One sentence in §4/§6 fixes it: emit in-tx, deliver async.

---

## Verdict

**Sound after fixes — not structurally flawed.** The skeleton (double-entry journal as truth, per-payment `source_ref` idempotency, ack-then-process ingress, reconciliation as the correctness path, CAS state machines) is the right architecture, and most findings above slot into it without reshaping it. But three findings gut the plan's own headline guarantees as written: #1 (the flagship drain control is raceable), #2/#5 (the state machine and CHECK constraints cannot safely represent refunds-after-broadcast or reorg losses), and #4 (the "polling is correctness" doctrine has a hole exactly where custodial deposits actually go wrong — the address afterlife). Those three, plus mandatory payout correlation (#6) and WAL archiving (#7), should be treated as M1–M3 blockers, not hardening backlog. Fix them in the design now; every one is cheap on paper and expensive in production.