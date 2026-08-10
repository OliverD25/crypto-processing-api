# Runbook: sending a USDT withdrawal

USDT leaves this system by a human hand. The BTCPay USDt plugin has no payout
handler of any kind — not automated, not even through BTCPay's own UI — so
there is nothing to configure that would make this automatic. Phase 2 adds a
signer; until then, this is the procedure.

Everything below assumes an `admin`-scope API key.

---

## The short version

1. `GET /v1/admin/withdrawals?status=pending_approval` — see what is waiting
2. Check the destination address against the user's account
3. `POST /v1/admin/withdrawals/{id}/approve` — this commits the money and gives
   you the exact net amount to send
4. Send **exactly** `amount_net` from the TRON hot wallet
5. `POST /v1/admin/withdrawals/{id}/mark-broadcast {"txid": "..."}`
6. Confirmation happens on its own

---

## 1. The queue

```sh
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "$API/v1/admin/withdrawals?status=pending_approval" | jq
```

Every USDT withdrawal is here regardless of size. Nothing can send it for us,
so the auto-approval limit is irrelevant.

## 2. Before approving

Approval is not a formality. It is the point where the money becomes committed
and you take on the task of sending it.

- Does the destination look like an address this user would use?
- Is `amount_gross` consistent with their balance and history?
- Is the destination a TRON address (`T...`)? The API already refused anything
  else, including Ethereum-format addresses, but look anyway.

If something is wrong, reject instead — the hold goes straight back:

```sh
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "destination does not match the account"}' \
  "$API/v1/admin/withdrawals/$ID/reject"
```

Rejection is only possible from `pending_approval`. After approval the money is
committed and returning it needs the attestation flow at the bottom of this
page.

## 3. Approve

```sh
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{}' \
  "$API/v1/admin/withdrawals/$ID/approve" | jq
```

The response moves to `submitted` and now carries the numbers that matter:

```json
{
  "status": "submitted",
  "amount_gross": "200.000000",
  "fee": "1.000000",
  "amount_net": "199.000000",
  "destination_address": "T..."
}
```

**`amount_net` is what you send.** The fee is our service charge covering TRX
gas; it is not deducted by the network and not added on top.

## 4. Send

From the TRON hot wallet, using your own wallet software.

- exactly `amount_net`, in USDT
- to exactly `destination_address`
- the USDT contract configured for this deployment, not some other TRC-20 token

Then copy the transaction id.

## 5. Record it

```sh
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"txid": "9f3c..."}' \
  "$API/v1/admin/withdrawals/$ID/mark-broadcast" | jq
```

The server does not take your word for it. It fetches the transaction from
TronGrid and checks every part of the claim:

| Check | Why it is there |
|---|---|
| contract is the configured USDT contract | a transfer of some other TRC-20 token is not this withdrawal |
| sender is the hot wallet | someone else's transfer to the same address is not ours |
| recipient is this withdrawal's destination | **this catches pasting the previous withdrawal's txid** |
| amount equals `amount_net` exactly | to the micro-USDT; no tolerance |
| receipt succeeded | an out-of-energy call is included in a block and moves nothing |
| a Transfer event is present | a TRX top-up has a healthy receipt and moves no USDT |

### If it is rejected

A `422` is the system working. The message names the part that did not match:

```json
{"detail": "that transaction does not match this withdrawal: recipient is T..., expected T..."}
```

Nothing changed — the withdrawal is still `submitted` and the money is still
held. Common causes:

- **wrong txid pasted.** Find the right one and try again.
- **wrong amount sent.** The chain is the truth: what you sent is what left. Do
  not re-send. Escalate — the difference has to be settled by hand, and the
  withdrawal will need an attested release plus a correcting adjustment.
- **wrong destination.** Same: the money is gone to the wrong address. Escalate.
- **transaction failed on chain** (usually out of energy). Nothing moved. Top up
  TRX and send again.

A `409` means that transaction id already settles a different withdrawal. One
transaction settles at most one withdrawal — the database will not allow
otherwise.

## 6. Confirmation

The worker polls every minute and confirms once the transaction is
`TRON_CONFIRMATIONS` blocks deep (19 by default, roughly the solidified-block
distance). It re-runs the full check on every poll, so a transaction that
disappears in a reorg cannot settle: the withdrawal stays `broadcast` with a
`failure_reason` and waits for a human.

At confirmation the hold is extinguished, the in-flight commitment clears and
the fee is booked. `GET /v1/withdrawals/{id}` shows `confirmed`.

---

## Returning money after you have approved

Once a withdrawal is past `submitted`, releasing the hold is a claim about the
chain: that the money is not going to arrive. Refunding a transfer that then
confirms pays the user twice.

So it takes an explicit attestation, and it is recorded on the withdrawal:

```sh
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"attestation": "checked TronScan: no transfer from the hot wallet to this address; nothing was ever sent"}' \
  "$API/v1/admin/withdrawals/$ID/release"
```

Before writing that sentence, actually check:

1. the hot wallet's outgoing TRC-20 transfers on TronScan for the window
2. that none of them match this destination and amount
3. that no transaction is sitting unconfirmed

A `confirmed` withdrawal can never be released. That is not a permissions
problem — the coins have moved, and the correction is an adjustment entry, not
a release.

## Running out of TRX

The gas monitor alerts below the threshold with code `tron.low_trx_balance`.
Top up the hot wallet with TRX. Withdrawals already in `submitted` are
unaffected; they are waiting for you either way.
