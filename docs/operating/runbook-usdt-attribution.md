# Runbook: attributing a USDT deposit by hand

## Why this document exists

BTC deposit addresses are single-use. A payment to one belongs to exactly one
invoice and therefore one user, forever, and attribution is never in doubt.

USDT is not like that. The USDt plugin does not derive addresses — it reserves
one from a pool the operator provisioned, and releases it when the invoice
settles or expires. The same address serves different users over time.

Which produces this:

> User A gets pool address X. Their invoice expires. X goes back to the pool.
> User B's new invoice reserves X. User A pays 40 minutes late, from a saved
> address.
>
> BTCPay sees an in-window payment on **B's** invoice. `afterExpiration` is
> false. Nothing is flagged. Left alone, A's money would be credited to B.

**USDT deposit attribution is heuristic.** This runbook is how a human resolves
the cases the automatic checks route to review.

## What the system does automatically

Three mitigations, none of them complete on its own:

1. **USDT invoice expiry is 60 minutes and is not shortened.** Shortening it to
   recycle the pool faster makes the collision window worse. Size the pool up
   instead — at least 20 addresses.
2. **Reservation windows are recorded.** Every deposit stores which address it
   held and between which instants. This is what makes the query below possible
   at all.
3. **Amount tolerance.** When the platform said what to expect, a settled USDT
   payment more than `USDT_AMOUNT_TOLERANCE_PCT` away from it goes to review
   instead of crediting. Crude, and it catches most cross-attribution.

None of that helps when two users send similar amounts. That is what the rest of
this page is for.

## When you are called

A deposit in `review`, or a user saying money never arrived.

```sh
curl -s -H "Authorization: Bearer $ADMIN_KEY" "$API/v1/admin/deposits/review" | jq
```

## Step 1 — find the transfer on chain

Open the receiving address on TronScan (or query TronGrid directly) and find the
incoming USDT transfer. Write down three things:

- the **timestamp** of the transfer
- the **amount**, in USDT
- the **sending address**

The timestamp is the one that decides everything.

## Step 2 — who owned the address then?

```sh
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "$API/v1/deposits/$DEPOSIT_ID/address-history" | jq
```

```json
{
  "address": "TX...",
  "reservations": [
    {"deposit_id": "019f...", "external_user_id": "user-77",
     "status": "pending", "reserved_from": "2026-08-10T12:00:00+00:00",
     "reserved_until": "2026-08-11T13:00:00+00:00"},
    {"deposit_id": "019e...", "external_user_id": "user-42",
     "status": "expired", "reserved_from": "2026-08-10T10:30:00+00:00",
     "reserved_until": "2026-08-11T11:30:00+00:00"}
  ]
}
```

Find the reservation whose window contains the transfer timestamp. **That user
is the owner of the money**, regardless of which invoice BTCPay attached the
payment to.

The same query in SQL, if you are on the box:

```sql
SELECT id, external_user_id, status, address_reserved_from, address_reserved_until
FROM deposits
WHERE asset_id = 'USDT_TRC20' AND address = 'TX...'
ORDER BY created_at DESC;
```

### When the windows overlap or none contains it

Both happen. Fall back to, in order:

1. **The sending address.** If this user has deposited before, the same sending
   address is strong evidence. Check their earlier `deposit_payments`.
2. **The amount.** Compare against the `amount_expected` of each candidate
   deposit.
3. **Ask the user.** "What time did you send it, from which address, and how
   much?" A user who can answer all three is almost certainly the owner.

Do not guess between two live candidates. Leave both in review and escalate.
Crediting the wrong user is not reversible by any automatic path: the ledger
would need a reversal entry against a balance that may already be spent.

## Step 3 — credit the right deposit

Only when the deposit BTCPay attributed the payment to is the correct one:

```sh
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"action": "credit", "payment_id": "<payment id from the review item>"}' \
  "$API/v1/admin/deposits/$DEPOSIT_ID/resolve"
```

Note there is no amount field. The server asks BTCPay what the payment was
worth. An operator clearing a queue at 2am cannot fat-finger an extra zero, and
if they could, the unique source reference would then stop the correct credit
from ever posting.

### When BTCPay attributed it to the wrong user

The resolve endpoint credits the deposit that owns the payment, so it cannot
move money to a different user. That case needs an adjustment entry written by
someone who understands the ledger:

- `DR external / CR user_available` for the user who should have it
- and the corresponding correction against the user BTCPay credited, if it was
  already credited

Both go through the normal journal. `external` is exempt from the balance sign
checks precisely so corrections like this can be booked in either direction.
Record what you did and why in the memo.

## Step 4 — if nothing is claimed

A transfer that matches no reservation window at all is money in custody that
no user has been credited for. It stays uncredited — never dismiss it to make
the queue tidy.

Dismissing is for review items that are genuinely not a deposit: a duplicate
record, or a payment that turned out to be someone else's.

## Reducing how often this happens

- **more addresses in the pool.** The single most effective change. Collisions
  are a function of pool size against concurrent deposits.
- **do not shorten the invoice expiry.** It feels like it frees the pool faster;
  it widens the window where a late payment lands on someone else.
- **tell users the address is single-use.** Documentation never stopped anyone
  reusing a saved address, but it moves the numbers.
- **watch the review queue.** A rising count means the pool is too small for the
  volume, and that is a capacity problem with a cheap fix.
