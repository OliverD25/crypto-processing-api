# Live verification log

!!! success "One live run is recorded: 2026-08-11"

    The USDT paths were driven end to end against the real TRON Nile testnet
    on 2026-08-11 — a real deposit, a real withdrawal, real confirmations, and
    both USDT contracts read off the chain. The entry is at the bottom of this
    page.

    Everything that run touched is live-verified from that date. Everything it
    did not touch is not, and the entry says which is which.

This page is the dated record of live verification runs against real networks.
Each run's section is written by
[`scripts/verify_nile.py`](https://github.com/OliverD25/crypto-processing-api/blob/main/scripts/verify_nile.py)
at stage 6 and pasted here verbatim, so what you read is what the script
observed rather than a summary of it.

BTC and Lightning are proven differently and are not in scope here: they run
end to end on regtest in the nightly, with real nodes and real blocks. USDT
cannot, because there is no TRON regtest. This page is where that gap gets
closed by hand.

Each entry below is one session of
[the Nile verification runbook](runbook-nile-verification.md).

---

## What an entry has to say

| What | Evidence |
|---|---|
| Date (UTC) | when the session started and finished |
| Network | `nile`, and the TronGrid endpoint it used |
| Nile USDT contract | the address, and what `symbol()` and `decimals()` answered |
| Mainnet USDT contract | the same, read-only, touching no mainnet funds |
| Deposit | transaction id, amount, the pool address it was paid to |
| Withdrawal | transaction id, gross, fee, net, destination |
| Confirmation depth | the transaction's block, the block it confirmed at, the difference |
| Duplicate txid | the second withdrawal's id and the status it was refused with |
| Payload-shape differences | every field where the live payload and `tests/fake_tron.py` disagreed |

An entry is only worth writing if all of it holds. A session that stopped
half-way is a session that failed, and the honest record of it would be no
entry at all.

## What the 2026-08-11 run settled, and what it did not

Two claims in this repository were assumptions until that session. Each is now
a fact with a date and a transaction id attached:

- **`USDT_CONTRACT_NILE = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"`** was described
  as "format-verified only, NOT confirmed against a live Nile node" in
  `gateway/trongrid.py`, `config.py`, `.env.example`,
  [BTCPay Server setup](btcpay-setup.md) and the Nile compose override. It is
  confirmed: on `https://nile.trongrid.io` the contract answers `symbol()` =
  `USDT` and `decimals()` = `6`.

    The advice to check the address against your own plugin configuration
    stays correct after the downgrade. A confirmed default is still a default.

- **`TRON_CONFIRMATIONS=19`** was a documented assumption about TRON's
  solidified-block distance, applied by the confirmation poller and never
  observed. The withdrawal was mined in block 69984870 and confirmed at
  69984909, 39 blocks deep, and not before.

**`USDT_CONTRACT_MAINNET = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"` was read
live, and only read.** The same two calls ran against `https://api.trongrid.io`
and answered `USDT` / `6`, which settles the identity of the contract. No
mainnet transaction was created, sent or verified in that session, and no
mainnet send path has ever been exercised — the deposit, the withdrawal, the
duplicate refusal and the confirmation depth were all proved on Nile.

---

## Runs

## Run of 2026-08-11

| What | Evidence |
|---|---|
| Date (UTC) | 2026-08-11T16:37:38+00:00 → 2026-08-11T17:00:24+00:00 |
| Network | nile (https://nile.trongrid.io) |
| Nile USDT contract | `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf` — `symbol()` = `USDT`, `decimals()` = `6` |
| Mainnet USDT contract | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` — `symbol()` = `USDT`, `decimals()` = `6` (read-only) |
| Deposit | `da207aa2554d84092f5d9966a2bbc5487090b78e1e619a6672a14020d5831063` — 5.000000 USDT credited, paid to the pool address `TDwrbF6vc6B3NevFSe8xcwoTztfiKAYjN3` |
| Withdrawal | `19637a3a1c804e87e7f9f196bffa09a5d86586a0be4cf92afe731a5cbe519ec2` — gross 2.000000, fee 1.000000, net 1.000000 USDT to `TVp7RheYmAcaHNzkbe9smK6e8xjpEdWYLM` |
| Confirmation depth | block 69984870 → 69984909 (39 blocks deep; `TRON_CONFIRMATIONS` is 19) |
| Duplicate txid | withdrawal `019ff1c4-a43a-7d62-831b-7ecf73a27e33` answered `409`: transaction 19637a3a1c804e87e7f9f196bffa09a5d86586a0be4cf92afe731a5cbe519ec2 is already recorded against withdrawal 019ff1bf-f3ca-72a5-9b6f-7e0da71a820a |
| Payload-shape differences | gettransactioninfobyid + `contractResult`: live payload has list, the fake has no such field; gettransactioninfobyid + `contractResult[0]`: live payload has str, the fake has no such field; gettransactioninfobyid + `contract_address`: live payload has str, the fake has no such field; gettransactioninfobyid - `receipt.net_fee`: the fake has int, the live payload has no such field; gettransactioninfobyid + `receipt.net_usage`: live payload has int, the fake has no such field; gettransactioninfobyid + `receipt.origin_energy_usage`: live payload has int, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract`: live payload has list, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract[0].parameter.type_url`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract[0].parameter.value.contract_address`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract[0].parameter.value.data`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract[0].parameter.value.owner_address`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.contract[0].type`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.expiration`: live payload has int, the fake has no such field; triggerconstantcontract + `transaction.raw_data.ref_block_bytes`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.ref_block_hash`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.raw_data.timestamp`: live payload has int, the fake has no such field; triggerconstantcontract + `transaction.raw_data_hex`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.ret`: live payload has list, the fake has no such field; triggerconstantcontract + `transaction.txID`: live payload has str, the fake has no such field; triggerconstantcontract + `transaction.visible`: live payload has bool, the fake has no such field |

Caveats downgraded by this run:

- `USDT_CONTRACT_NILE` was "format-verified only, NOT confirmed against a
  live Nile node". It is now confirmed: the contract answers `USDT` / `6`,
  read from `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf` on https://nile.trongrid.io.
- `TRON_CONFIRMATIONS=19` was a documented assumption about TRON's
  solidified-block distance. A withdrawal was confirmed 39
  blocks deep and not before.

Raw payloads: `spike-evidence-nile/` in the operator's working copy. They are
not committed — they contain nothing secret, but they are a session's
scratch, and the assertions above are the part that matters.
