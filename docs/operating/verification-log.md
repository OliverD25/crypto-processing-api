# Live verification log

!!! warning "No live run has been recorded yet"

    The kit is ready and nothing has been run through it. Until an entry
    appears below, every USDT claim in this project is **format-verified
    only**: the code paths are tested against `tests/fake_tron.py`, which
    imitates TronGrid rather than being it.

    Treat that as the honest state, not as a formality waiting to be signed
    off. It is the reason live TRON Nile verification is a hard gate before
    the `v0.2.0` tag rather than a nice-to-have after it.

BTC and Lightning are proven differently and are not in scope here: they run
end to end on regtest in the nightly, with real nodes and real blocks. USDT
cannot, because there is no TRON regtest. This page is where that gap gets
closed by hand.

Each entry below is one session of
[the Nile verification runbook](runbook-nile-verification.md), driven by
[`scripts/verify_nile.py`](https://github.com/OliverD25/crypto-processing-api/blob/main/scripts/verify_nile.py).
Stage 6 of that script prints the entry filled in; it is pasted here and
committed.

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
half-way is a session that failed, and the honest record of it is this warning
staying where it is.

## The caveats a green run downgrades

Two claims in this repository are assumptions today. A green session turns each
into a fact with a date and a transaction id attached, and the entry has to say
so explicitly:

- **`USDT_CONTRACT_NILE = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"`** is currently
  described as "format-verified only, NOT confirmed against a live Nile node"
  in `gateway/trongrid.py`, `config.py`, `.env.example`,
  [BTCPay Server setup](btcpay-setup.md) and the Nile compose override. The
  address round-trips through base58check, which proves the characters are
  well-formed and nothing about what is deployed there. Reading `symbol()` and
  `decimals()` off the contract is what settles it.

    The advice to check the address against your own plugin configuration stays
    correct after the downgrade. A confirmed default is still a default.

- **`TRON_CONFIRMATIONS=19`** is a documented assumption about TRON's
  solidified-block distance, applied by the confirmation poller and never
  observed. A green run records the block a withdrawal was mined in, the block
  it confirmed at, and the difference between them.

`USDT_CONTRACT_MAINNET` gets the same treatment from the same session. Its
comment claims verification by round-tripping the hex form through base58check,
which is the same format-only check under a more confident sentence. The
preflight reads the mainnet contract too — read-only, no funds involved.

---

## Runs

*None yet.*
