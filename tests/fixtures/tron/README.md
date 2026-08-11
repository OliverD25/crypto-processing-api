# Recorded TronGrid payloads

Nine raw TronGrid answers, captured on **2026-08-11** during the live TRON Nile
verification session. The session is written up in
[`docs/operating/verification-log.md`](../../../docs/operating/verification-log.md);
these are the bytes behind the claims on that page.

They exist because every other TRON test drives `tests/fake_tron.py`. A fake
and the code that reads it can agree with each other forever while both drift
away from the server, and there is no TRON regtest to catch it. So the parser
and the ABI decoders are also asserted against payloads TRON really sent —
that is `tests/unit/test_tron_payload_corpus.py`, which additionally diffs the
fake's shapes against these files field by field.

## Never hand-edit these files

Their whole value is that no one has touched them. Editing one to make a test
pass turns the corpus into a second fake, and a quieter one, because it still
looks recorded. If a payload is wrong for what you need, **recapture** it:

```sh
python scripts/verify_nile.py
```

Then copy the new files out of `spike-evidence-nile/`, and read the diff rather
than accepting it — a changed field is news about TronGrid.

## What is here

| File | The call it answers |
|---|---|
| `getnowblock.json` | block height, the confirmation poller's clock |
| `getaccount_hot_wallet.json` | the hot wallet's TRX balance in sun, for the gas monitor |
| `gettransactioninfobyid_withdrawal.json` | the verified withdrawal, 1.000000 USDT, receipt `SUCCESS` |
| `gettransactioninfobyid_deposit.json` | the credited deposit, 5.000000 USDT |
| `triggerconstantcontract_symbol_nile.json` | `symbol()` on the Nile USDT contract |
| `triggerconstantcontract_decimals_nile.json` | `decimals()` on the same |
| `triggerconstantcontract_symbol_mainnet.json` | `symbol()` on the mainnet USDT contract, read-only |
| `triggerconstantcontract_decimals_mainnet.json` | `decimals()` on the same |
| `triggerconstantcontract_balanceof.json` | `balanceOf(address)` for the hot wallet |

Each file is one capture as `scripts/verify_nile.py` wrote it: `source`,
`recorded_at`, the `request` body and the `response` body. The request is kept
because for `triggerconstantcontract` the answer alone does not say which
function was called. **Headers are not captured, which is where the TronGrid
API key travels** — there is no credential in this directory, and nothing was
removed from these files to make that true.

Two things the corpus does not have, both listed in `MANIFEST.json`: a constant
call the node refused, and any endpoint the client never calls. The refusal is
worth capturing next time — it is the path that turns a wrong contract address
into an error instead of a quiet pass.
