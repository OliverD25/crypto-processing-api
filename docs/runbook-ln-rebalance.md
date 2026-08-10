# Runbook: moving value between the on-chain and Lightning floats

`BTC` and `BTC_LN` are two separate custodial floats. On-chain BTC lives in
BTCPay's hot wallet; Lightning BTC is channel balance. They are the same asset
to a user's eye and different money to yours, and nothing in this service moves
value between them. That is a deliberate omission — an automated path between
two floats is an automated path to drain both.

This runbook is what you do instead. It is short because the mechanism is
BTCPay's, not ours; the parts worth writing down are the ones about the ledger.

---

## When you need it

Two signals, and they mean opposite things.

**Outbound liquidity is running low.** `GET /v1/admin/reconciliation` shows the
`BTC_LN` line's `chain_balance` — that is outbound channel liquidity, the only
Lightning money that can pay a withdrawal. When it drops toward
`user_obligations`, withdrawals start failing to route and the payout deadline
starts cancelling them. Watch for `withdrawal.hold_needs_attestation` alerts and
for drill-10-shaped failures in production: a payout that sits `submitted` and
then refunds itself with a definitive-failure proof is telling you the channel
is empty, not that anything is broken.

**Inbound liquidity is running low.** Nothing alerts on this, because it is not
a solvency problem — it is a deposits problem. Symptom: users report that paying
your Lightning invoice fails. `chain_balance` will look healthy while it
happens, because a full channel on our side is exactly what a lot of successful
deposits produce.

---

## Moving on-chain BTC into a channel

1. Read both floats first: `GET /v1/admin/reconciliation`, the `BTC` and
   `BTC_LN` lines. Write down `chain_balance` for each.
2. In BTCPay's UI, open a channel from the store's Lightning node, funded from
   the on-chain wallet, or add to an existing one.
3. Wait for the funding transaction to confirm and the channel to become active.
4. Re-read `/v1/admin/reconciliation`.

**What you will see, and why it is correct.** The `BTC` line's `chain_balance`
is now lower than `ledger_custody`, so `difference` is negative. The `BTC_LN`
line's is higher, so its `difference` is positive. Neither is an error: the
ledger says nothing happened because, from a user's point of view, nothing did.
No user gained or lost a satoshi, so no user account moved.

If the on-chain side is now below `user_obligations`, the report will say
`insolvent: true` for `BTC` and raise `custody.insolvency_signal`. **That is the
check working.** You have funded a channel with money you owe on-chain
depositors. Close the channel or top the wallet back up.

---

## Booking it, if you want the books to say so

You do not have to. The two `difference` figures are self-explanatory if you
know a rebalance happened, and this service alerts rather than repairing on
purpose.

If you would rather the ledger carried the movement, it is two `adjustment`
entries against `external` — one per asset, because an entry is per asset and a
rebalance is by definition two assets:

```
BTC     adjustment   CR hot_wallet -X        DR external +X
BTC_LN  adjustment   DR hot_wallet +X        CR external -X
```

Both sum to zero on their own asset, which is what the zero-sum trigger checks.
Use the same memo on both so they can be read as one movement. There is no
endpoint for this; it is a `post_entry` call from a console, and it is the only
thing in this document that touches the ledger.

An `EntryKind.REBALANCE` that made this a first-class operation is a plausible
future change. It is not here because a movement nobody has automated does not
need a vocabulary yet.

---

## What not to do

- **Do not withdraw from one float to fund the other through the public API.**
  It would work, and it would charge a user's balance for an operator's action.
- **Do not size a channel from the daily cap.** The cap bounds a day of user
  withdrawals; channel capacity is locked up until the channel closes. They are
  different questions with different answers.
- **Do not close a channel to fix an insolvency alarm on `BTC_LN`.** Closing
  returns the funds on chain, which fixes the number by removing the ability to
  pay any Lightning withdrawal at all. If users are owed Lightning balances, they
  still are.
