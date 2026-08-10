# Runbook: a deposit was reorged out after being credited

## The situation

A deposit confirmed, the ledger credited it, the user withdrew the balance, and
then the block containing the deposit was orphaned. The money never really
arrived. The user has the coins from their withdrawal and the deposit is gone.

This is rare and it is expensive. It is also the reason two design decisions
exist that otherwise look like over-engineering.

## Why the ledger can even represent this

The naive correction is to reverse the deposit credit: debit
`user_available`. That fails — the user already spent it, `user_available`
would go positive, and the `no_overdraft` CHECK rejects the whole transaction.
**The loss could not be booked at all.** The books would have to be left wrong.

So there is a `user_deficit` account: debit-normal, exempt from the sign
checks, meaning "money we are owed and may never collect".

The other decision: settlement is pinned to at least **1 confirmation**
(`MediumSpeed`), never 0-conf. A 0-conf policy makes this scenario ordinary
rather than rare.

## Detection

You will find out one of three ways:

1. **Job C alerts** with `custody.insolvency_signal`: the wallet holds less
   than users are owed.
2. **The unattributed-receive scan** and the deposit sweep disagree with
   BTCPay: an invoice that was `Settled` reads differently on a re-poll.
3. **BTCPay's wallet balance drops** with no payout to explain it.

The fastest confirmation:

```sh
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "$API/v1/admin/reconciliation" | jq '.custody'
```

`insolvent: true` means the chain holds less than `user_obligations`.

## Step 1 — stop the bleeding

**Freeze withdrawals before anything else.** While the ledger says users have
balances the wallet cannot cover, every withdrawal makes the hole bigger.

The blunt instrument, and the right one under time pressure:

```sql
UPDATE assets SET withdrawal_daily_cap = 0 WHERE id = 'BTC';
```

Every withdrawal now routes to `pending_approval` and no payout is created
without a human. Deposits are safe to keep accepting — they only add.

## Step 2 — establish what happened

For the affected deposit:

```sql
SELECT d.id, d.external_user_id, d.status, d.amount_credited,
       dp.btcpay_payment_id, dp.amount, dp.credited_at, dp.ledger_entry_id
FROM deposits d JOIN deposit_payments dp ON dp.deposit_id = d.id
WHERE d.btcpay_invoice_id = '<invoice id>';
```

Check the transaction on a block explorer. Three outcomes:

- **it was re-mined in another block** — nothing is wrong. BTCPay will settle it
  again; the ledger's unique `source_ref` means the credit does not repeat.
  Undo step 1 and stop.
- **it was replaced (RBF) with a smaller amount to the same address** — a
  partial loss. The difference is the deficit.
- **it is gone entirely** — the full amount is the deficit.

Also find out whether the user withdrew:

```sql
SELECT id, status, amount_gross, txid, created_at
FROM withdrawals WHERE external_user_id = '<user>' ORDER BY created_at DESC;
```

## Step 3 — book it

### The balance is still there

The easy case. Reverse the credit normally: `DR user_available / CR hot_wallet`
as a `reversal` entry referencing the original credit. The user's balance drops
by what never arrived, and the books are right.

### The balance was already withdrawn

The case `user_deficit` exists for.

```
kind:        reversal
asset:       BTC
source_ref:  reorg:<invoice id>:<payment id>
reverses:    <the deposit credit entry id>
memo:        deposit reorged out of block <height> after the balance was withdrawn

DR user_deficit  +<amount>
CR hot_wallet    -<amount>
```

Sums to zero. `hot_wallet` drops to what the chain actually holds, and the
shortfall is named: `user_deficit` is money the operator is carrying.

**Post it through `post_entry`, not by hand in SQL.** The append-only triggers
will reject direct edits, and going through the ledger keeps the balance
materialization and the zero-sum check intact. There is a test covering exactly
this sequence — `test_reorg_loss_on_an_already_spent_balance_is_bookable`.

Afterwards, `GET /v1/admin/reconciliation` shows `custody` reduced,
`user_deficit` carrying the loss, and the books balanced again.

## Step 4 — decide who carries it

The ledger records the loss; it does not decide who eats it. That is a business
call:

- **the operator absorbs it.** Leave the `user_deficit` balance and treat it as
  a loss. Usually the right answer for a small amount and an innocent user.
- **recover from the user.** Post an adjustment moving the deficit onto their
  balance — this makes them negative, which requires the same exemption
  reasoning, and it will need a conversation with them first.

Either way, write down what was decided and why in the entry memo. In a year
this will be an unexplained number otherwise.

## Step 5 — communicate

- **the affected user**, if their balance changed. Explain plainly: a deposit
  was reversed by the network, not by you.
- **the platform**, so support is not answering blind.
- **whoever holds the risk**, with the amount.

## Step 6 — resume

Only once `/v1/admin/reconciliation` shows the books consistent and custody
covering obligations:

```sql
UPDATE assets SET withdrawal_daily_cap = <the original value> WHERE id = 'BTC';
```

## Preventing the next one

- **never run 0-conf.** `MediumSpeed` (1 confirmation) is the floor; the
  bootstrap sets it.
- **consider more confirmations for large deposits.** Per-amount confirmation
  tiers are Phase 2; a cheap approximation today is a per-asset delay between
  credit and withdrawal eligibility.
- **keep the daily cap tight.** It bounds this exactly as it bounds a stolen
  key: the most that can leave before a human looks.
