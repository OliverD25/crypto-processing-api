# Runbook: verifying USDT live on TRON Nile

USDT cannot run on regtest. There is no TRON equivalent of `bitcoind -regtest`,
so every USDT path in this project — the deposit matcher, the withdrawal
verifier, the gas monitor, the confirmation depth — is tested against a fake
that imitates TronGrid rather than against TronGrid.

That fake is good. It runs the real parser over payloads shaped like real ones,
so a parser bug still fails a test. What it cannot do is notice that TronGrid
sends a field the fake never heard of, or that the contract address shipped as
a default is not the contract anyone deployed.

One live session on the Nile testnet closes that gap. It is the hard gate
before the `v0.2.0` tag, and it happens once, by hand, with real testnet money.
This page is the half you do yourself. The other half is
[`scripts/verify_nile.py`](https://github.com/OliverD25/crypto-processing-api/blob/main/scripts/verify_nile.py),
which walks you through the rest in six numbered stages.

**Budget two to three hours**, most of it waiting on BTCPay to restart and on
faucets to answer. None of it is difficult.

---

## What the session proves

| Claim | How it is proved |
|---|---|
| `USDT_CONTRACT_NILE` is the USDT contract | the contract answers `symbol()` = `USDT` and `decimals()` = `6` |
| `USDT_CONTRACT_MAINNET` is too | the same two reads against `api.trongrid.io`, read-only, no funds touched |
| a real Nile deposit is credited exactly | one payment, matched, credited to the micro-USDT |
| the withdrawal verifier works on real data | the full-tuple check runs against a transaction you actually sent |
| `TRON_CONFIRMATIONS=19` means what it says | the withdrawal confirms 19 blocks deep and not before |
| one transaction settles one withdrawal | the same txid against a second withdrawal is refused |
| the fake matches reality | every captured payload is diffed against `tests/fake_tron.py` |

---

## Before you start: the checklist

Tick these off in order. Each line links to the section that explains it.

- [ ] [TronGrid account and API key](#1-trongrid-api-key-10-minutes) — free tier
- [ ] [Two Nile wallets](#2-two-nile-wallets-10-minutes), hot and user, both `T…` addresses
- [ ] [Faucet claims](#3-faucet-trx-and-test-usdt-15-minutes): TRX in both, test USDT in both
- [ ] [The Nile stack is up](#4-bring-the-stack-up-10-minutes)
- [ ] [The USDt plugin is installed and configured for Nile](#5-the-usdt-plugin-30-minutes-mostly-restarts)
- [ ] [An address pool is pasted into the store](#6-the-address-pool-10-minutes)
- [ ] [`api` and `worker` restarted, `USDT_TRC20` enabled](#7-turn-the-asset-on-5-minutes)
- [ ] [The environment file has all five TRON values](#8-the-environment-5-minutes)
- [ ] [`python scripts/verify_nile.py` run to the end](#9-run-the-session)
- [ ] [The results committed](#afterwards)

---

## 1. TronGrid API key (10 minutes)

Register at [`www.trongrid.io`](https://www.trongrid.io/) and create a key. The
free tier is 100,000 requests a day at 15 QPS, which is far more than a session
uses.

**The key is not optional.** Keyless TronGrid is throttled unpredictably, and
the thing that gets throttled is the check that a withdrawal really happened.
The service refuses to start with a TRON hot wallet configured and no key.

The same key works for Nile and mainnet, which matters: the preflight reads the
mainnet USDT contract too.

## 2. Two Nile wallets (10 minutes)

Install [TronLink](https://www.tronlink.org/) (browser extension or mobile),
then switch the network to **TRON Nile Testnet** in the network dropdown. It is
not the default and it is easy to miss — a mainnet address looks identical.

Create two accounts:

| Role | What it does in the session |
|---|---|
| **hot wallet** | sends the withdrawal. `TRON_HOT_WALLET_ADDRESS` is this one |
| **user wallet** | pays the deposit, and receives the withdrawal |

Both addresses start with `T` and are 34 characters of base58 — for example
`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`. There is no separate testnet address
format: a Nile address and a mainnet address are indistinguishable by eye,
which is exactly why the script refuses to run unless every part of the
configuration says Nile.

Keep the two accounts separate. The verifier compares the sender against the
hot wallet and the recipient against the destination, so a withdrawal sent from
the wrong account is correctly rejected — and you will have spent a faucet
claim proving it.

## 3. Faucet: TRX and test USDT (15 minutes)

Both wallets need **TRX** (TRC-20 transfers cost energy and bandwidth, not
USDT) and both need **test USDT**.

**Option A — the official Nile faucet.**
[`nileex.io/join/getJoinPage`](https://nileex.io/join/getJoinPage). Paste an
address, solve the reCAPTCHA, click Obtain. It gives 2,000 TRX per address per
day and also hands out test USDT. No signup, no mainnet balance check. It needs
a real browser: the page is behind bot protection and will not answer a script.

**Option B — TronFAQBot, on Telegram or Discord.** Join
[the TRON developers Telegram group](https://t.me/TronOfficialDevelopersGroupEn)
or [the TRON Discord](https://discord.com/invite/hqKvyAM) and message the bot:

| Command | Gives |
|---|---|
| `!nile YOUR_ADDRESS` | up to 5,000 TRX per 24 hours |
| `!nile_usdt YOUR_ADDRESS` | up to 5,000 test USDT per 24 hours |

Two options are listed because testnet faucets go down, and finding that out
mid-session costs you the day. Both are documented by TRON itself in
[Getting testnet tokens](https://developers.tron.network/docs/getting-testnet-tokens-on-tron).

**Fund the hot wallet with USDT directly.** This is the non-obvious part. The
deposit you make in stage 2 lands on a *pool address*, not on the hot wallet,
and nothing in this system sweeps it across — USDT custody is the operator's
job. So the hot wallet needs its own faucet USDT to send the withdrawal from.
About 10 USDT and 200 TRX in each wallet is comfortable.

Note the contract address the faucet's USDT actually uses. The preflight checks
whatever you configure, so if the faucet hands out a different token than the
default, this is where you find out.

## 4. Bring the stack up (10 minutes)

The Nile override adds the TRON settings to the ordinary regtest stack. The BTC
half stays entirely offline; only the USDT half needs the internet.

```sh
docker compose -f deploy/docker-compose.regtest.yml \
               -f deploy/docker-compose.nile.override.yml up -d
python scripts/bootstrap_btcpay.py
```

`bootstrap_btcpay.py` writes `.env.regtest.generated`, which the verification
script reads to talk to BTCPay. It is idempotent.

## 5. The USDt plugin (30 minutes, mostly restarts)

Greenfield has no plugin management API and the plugin's own settings are not in
its schema either, so this part is BTCPay's UI. The four steps are documented in
[`deploy/docker-compose.nile.override.yml`](https://github.com/OliverD25/crypto-processing-api/blob/main/deploy/docker-compose.nile.override.yml)
next to the settings they belong to, and in full in
[BTCPay Server setup](btcpay-setup.md#usdt-plugin-usdt-trc20). In short:

1. **Server Settings > Plugins** — install **USDt**, then restart BTCPay. The
   restart is slow; this is most of the 30 minutes.
2. **Server Settings > USDt** — set the TRON JSON-RPC endpoint to
   `https://nile.trongrid.io/jsonrpc`, paste the TronGrid key, and set the USDT
   contract address for Nile.
3. The address pool — [section 6 below](#6-the-address-pool-10-minutes).
4. Restart `api` and `worker` — [section 7](#7-turn-the-asset-on-5-minutes).

The contract you set here and this service's `USDT_CONTRACT_ADDRESS` must be
the same string. BTCPay watches for transfers of that token; the withdrawal
verifier refuses transfers of any other one. The script's preflight refuses to
continue if it and the `api` container disagree.

## 6. The address pool (10 minutes)

**Store > Settings > USDt**: paste TRON addresses, one per line. The reasoning —
pool size is the maximum number of USDT deposits that can be open at once, and
addresses are reused across users over time — is in
[the pool section of the BTCPay setup page](btcpay-setup.md#3-address-pool-the-part-with-the-sharp-edge)
and is not repeated here.

For this session the pool needs to be big enough that a deposit can be created
at all. Production advice is at least 20; the drill opens one deposit at a time,
so five is enough to finish. If this stack is going to live longer than the
session, do the 20 now.

Use addresses from accounts you control in TronLink. The test USDT you deposit
lands there and stays there — nothing sweeps it — so addresses you cannot spend
from make the deposit unrecoverable. On a testnet that costs nothing but a
faucet claim; the habit is still worth keeping.

## 7. Turn the asset on (5 minutes)

```sh
docker compose -f deploy/docker-compose.regtest.yml \
               -f deploy/docker-compose.nile.override.yml \
               up -d --force-recreate api worker
```

Payment-method discovery runs at startup and re-enables `USDT_TRC20` once the
store reports a matching method. Until then the asset stays disabled and USDT
deposit requests answer `503` — deliberately, because an enabled asset with no
payment method would mint invoices for an address nobody is watching.

Confirm with `GET /v1/assets` that `USDT_TRC20` is enabled. If it is not, go
back to step 5: the plugin is installed but not configured, or BTCPay was not
restarted after installing it.

## 8. The environment (5 minutes)

Both docker compose and the verification script read `.env` in the repository
root, so one file configures both. It needs five values:

```sh
TRON_NETWORK=nile
TRONGRID_API_KEY=your-trongrid-key
USDT_CONTRACT_ADDRESS=the-contract-the-plugin-is-pointed-at
TRON_HOT_WALLET_ADDRESS=T...your-hot-wallet
TRON_CONFIRMATIONS=19
```

`.env` is gitignored. Do not put the TronGrid key anywhere else.

The script never prints the key, and never prints the admin API key either. It
reads `CPAPI_ADMIN_KEY` if you export one, and mints a throwaway admin key
inside the `api` container if you do not.

## 9. Run the session

```sh
python scripts/verify_nile.py
```

It runs six stages. Each one prints what it is about to do, what you must do by
hand, and what it collected.

| Stage | What it does | What it asks of you |
|---|---|---|
| 1 preflight | reads both USDT contracts, the hot wallet's balances and BTCPay's payment methods | nothing — no funds move |
| 2 deposit | creates a USDT deposit and waits | send exactly 5.000000 USDT from the user wallet to the address it prints, then paste the transaction id |
| 3 withdrawal | requests 2 USDT, approves it, verifies your transaction, waits for 19 confirmations | give it a destination address, send exactly the net amount from the hot wallet, paste the transaction id |
| 4 duplicate | submits the stage-3 transaction id against a second withdrawal | nothing — it must be refused with a `409` |
| 5 payloads | diffs every captured payload against `tests/fake_tron.py` | nothing |
| 6 report | writes the verification-log entry | nothing |

**If it stops**, fix what it complained about and resume:

```sh
python scripts/verify_nile.py --stage 3
```

State lives in `spike-evidence-nile/` (gitignored), so a resumed run picks up
the deposit and withdrawal the earlier run created. You do not need a second
faucet claim.

### What can go wrong, and what it means

| Symptom | Cause |
|---|---|
| preflight: the contract answers something other than `USDT`/`6` | `USDT_CONTRACT_ADDRESS` is not the USDT contract. Check it against the plugin's setting and against the faucet's token |
| preflight: the script and the `api` container disagree | `.env` changed after the containers started. Recreate `api` and `worker` |
| preflight: no enabled USDT payment method | step 5 is incomplete, or BTCPay was not restarted after installing the plugin |
| deposit goes to `review` instead of settling | the amount sent was not the amount asked for. That is the attribution guard working — see [attributing a USDT deposit](runbook-usdt-attribution.md) |
| `mark-broadcast` refused with `422` | the transaction is not a transfer of that amount from the hot wallet to that destination. The message names the part that did not match. Do **not** send again before reading it |
| the withdrawal never confirms | the transfer ran out of energy. It is in a block and moved nothing. Top up TRX and send again |

The `422` is worth dwelling on: a refusal there is the system working, and the
script gives you three attempts precisely because the ordinary cause is a
mis-pasted transaction id.

---

## Afterwards

Stage 6 prints a filled verification-log entry and writes it to
`spike-evidence-nile/verification-log-<date>.md`. Three things follow from a
green session:

1. **Paste the entry into [`verification-log.md`](verification-log.md)** and
   commit it. That page is the adopter-facing evidence that any of this was
   ever run for real.
2. **Downgrade the caveats the entry lists.** `USDT_CONTRACT_NILE` is described
   as "format-verified only, NOT confirmed against a live Nile node" in five
   places — `gateway/trongrid.py`, `config.py`, `.env.example`,
   `btcpay-setup.md` and the Nile override. Replace that with "confirmed
   against Nile on `<date>`, tx `<txid>`". Keep the advice to check it against
   your own plugin configuration: that stays true regardless.
3. **Fix `tests/fake_tron.py` against anything stage 5 found**, and add a
   regression test built from a captured payload. The fake's docstring claims
   it is built from the shape TronGrid actually returns. This session is what
   makes that claim true.

The raw payloads under `spike-evidence-nile/` are not committed. They hold
nothing secret — the API key travels in a header and is never captured — but
they are one session's scratch, and the assertions are the part worth keeping.
