# BTCPay Server setup

What has to exist on the BTCPay side before this service can work, and which
parts a script can do for you.

`scripts/bootstrap_btcpay.py` does everything Greenfield exposes: the store, the
hot wallet, the settlement policy, the webhook, the API keys and the payout
processor. It is idempotent — run it again after any change and it reports what
already exists.

The USDt plugin is the exception. None of it is in the Greenfield API, so the
whole of the USDT section below is done by hand, once.

## Store

One store serves both assets.

| Setting | Value | Why |
|---|---|---|
| Speed policy | `MediumSpeed` (1 confirmation) | 0-conf credits a deposit a single-block reorg can take back, after the user has already withdrawn it |
| Monitoring expiration | 86400 seconds | how long BTCPay keeps attributing payments to an invoice; the deposit sweep aligns its polling window to this exact number |

Both are set by the bootstrap script.

## BTC wallet — it must be a hot wallet

The automated payout processor signs transactions, so a watch-only xpub cannot
work. The bootstrap generates the wallet inside BTCPay with `savePrivateKeys`.

This is a real risk decision, not a technicality: **the hot wallet balance is
the loss ceiling.** Keep one to three days of payout volume in it and sweep the
rest to cold storage manually.

## Payout processor

```
PUT /api/v1/stores/{storeId}/payout-processors/OnChainAutomatedPayoutSenderFactory/BTC-CHAIN
{"intervalSeconds": 600, "feeTargetBlock": 3, "threshold": "0", "processNewPayoutsInstantly": true}
```

BTCPay 2.4.2 **refuses an interval below 60 seconds** — "The minimum interval is
60 seconds". That costs nothing: `processNewPayoutsInstantly` sends a new payout
immediately, and the interval only drives the sweep that catches whatever the
instant path missed. Ten minutes in production lets the processor batch several
payouts into one transaction and pay one miner fee instead of several.

## API keys

The bootstrap mints two, both scoped to the single store, neither unrestricted.

**Runtime key** (`BTCPAY_API_KEY`), used by the api and worker:

```
btcpay.store.cancreateinvoice
btcpay.store.canviewinvoices
btcpay.store.cancreatepullpayments
btcpay.store.cancreatenonapprovedpullpayments
btcpay.store.canmanagepayouts
btcpay.store.canviewstoresettings
btcpay.store.canviewwallet
```

**Bootstrap key** (`BTCPAY_BOOTSTRAP_KEY`), used only by the setup script:

```
btcpay.store.canmodifystoresettings
btcpay.store.canviewstoresettings
btcpay.store.webhooks.canmodifywebhooks
btcpay.store.canviewwallet
```

Two things about scopes that the swagger gets wrong for 2.4.2, both found by
asking the running server:

- payout management needs `btcpay.store.canmanagepayouts`. The swagger says
  `canmanagepullpayments`; the server answers 403 naming the real one.
- `canmanagepullpayments` is not needed at all — this service creates payouts,
  never pull payments.

### The five-minute window

BTCPay accepts Greenfield Basic authentication **only for an account younger
than five minutes** (`BasicAuthenticationHandler`: "give new accounts time to
create API keys via the Greenfield API"). After that the user must enable it by
hand in the UI.

So the first bootstrap run spends that window minting the bootstrap key, and
every later run authenticates with it. If you ever need to change the runtime
key's scopes, minting a replacement needs Basic auth again — enable it in
Account > API Keys, or start from a fresh admin.

## Webhook

```
POST /api/v1/stores/{storeId}/webhooks
```

URL `http://crypto-api:8000/webhooks/btcpay` on a single-box deployment — the
container network, so the path never has to be internet-reachable. Signature
verification is on regardless, because split-host deployments exist.

Subscribed events: `InvoiceReceivedPayment`, `InvoiceProcessing`,
`InvoicePaymentSettled`, `InvoiceSettled`, `InvoiceExpired`, `InvoiceInvalid`,
`PayoutCreated`, `PayoutApproved`, `PayoutUpdated`. `InvoiceCreated` is skipped
as noise.

Automatic redelivery is on, but it is not the safety net. BTCPay gives up after
roughly eight attempts in an hour; the reconciliation sweep is what makes
deposits correct.

**The webhook secret is not the API key.** They are different values with
different jobs, and the bootstrap keeps them that way.

---

# Lightning (BTC_LN) — opt-in

Skip this section unless you have read the Lightning warning in the README and
still want it. `BTC_LN` is a separate custodial float, and it costs one
server-level BTCPay permission.

Everything below is done for you by:

```
LIGHTNING_ENABLED=true python scripts/bootstrap_btcpay.py
```

## 1. A Lightning node the server can use

BTCPay needs `BTCPAY_BTCLIGHTNING` pointing at a node before a store can use
one. On the regtest stack that is
`deploy/docker-compose.regtest.lightning.yml`; in production it is whatever your
BTCPay deployment already does for Lightning (the internal node, or a connection
string to your own LND or CLN).

Nothing in this service manages the node. If BTCPay cannot see a Lightning node,
the payment method cannot be enabled and `sync_payment_methods` will disable
`BTC_LN` at startup with a warning — which is the correct outcome, not a bug.

## 2. Enable the payment method

```
PUT /api/v1/stores/{storeId}/payment-methods/BTC-LN
{"enabled": true, "config": {"connectionString": "Internal Node"}}
```

**This call needs `btcpay.server.canuseinternallightningnode`.** A store-scoped
key gets a 403 naming that permission. There is no narrower way to do it through
the API, which is why `LIGHTNING_ENABLED` exists as a switch at all — see
[`security.md`](security.md).

The bootstrap key holds it; the runtime key does not. If you enabled Lightning
after a first bootstrap run, the stored bootstrap key predates the scope and the
call will 403. Delete `BTCPAY_BOOTSTRAP_KEY` from the generated env file and
re-run so a new one is issued.

## 3. Lightning payout processor

```
PUT /api/v1/stores/{storeId}/payout-processors/LightningAutomatedPayoutSenderFactory/BTC-LN
{"intervalSeconds": 60, "processNewPayoutsInstantly": true}
```

Without it, approved Lightning payouts are created and never paid.

**Two things to know, and together they are why this service has its own
deadline and its own release rule.**

`cancelPayoutAfterFailures` is accepted by this endpoint and then silently
dropped from the echoed configuration. BTCPay does **not** give up on a payout
it cannot route, however many times it fails.

And a payout it has picked up cannot be cancelled. An unroutable Lightning
payout goes to `InProgress` and stays there; `DELETE /api/v1/payouts/{id}`
answers `400 invalid-state`. It carries a `PayoutLightningBlob` whose preimage
is all zeros, which is the tell that nothing settled.

So a stuck Lightning withdrawal is ended by `LN_PAYOUT_TIMEOUT_SECONDS`, and
the user's hold is returned automatically only once the **invoice has expired**
— at which point no retry, BTCPay's or anyone's, can pay it. Until then the
balance stays held and the operator gets one
`withdrawal.hold_needs_attestation` alert.

## 4. What the runtime key gains

One scope: `btcpay.store.canuselightningnode`. It reads the node's channel
balance (the custody source for `BTC_LN`) and the routing fee of a completed
payment. Both are reads, both are store-scoped.

---

# USDt plugin (USDT-TRC20)

Everything here is manual. Greenfield has no plugin management, and the
plugin's own settings — the address pool above all — are not in its schema.

## 1. Install

BTCPay > Server Settings > Plugins > install **USDt**, then restart BTCPay.

The plugin lives at `github.com/btcpayserver-tether/BTCPayServer.Plugins.USDt`
and supports USDt on TRON, Ethereum and Polygon. Only TRON is in scope here.

## 2. Server configuration

BTCPay > Server Settings > USDt:

| Setting | Mainnet | Nile testnet |
|---|---|---|
| TRON JSON-RPC | `https://api.trongrid.io/jsonrpc` | `https://nile.trongrid.io/jsonrpc` |
| USDT contract | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` | `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf`, and check it against your own deployment |
| TronGrid API key | required | required |

**Register for a TronGrid API key.** The free tier is 100,000 requests a day at
15 QPS; without a key the rate is throttled unpredictably and gets worse over
time. What gets throttled is the check that a withdrawal really happened.

Set the same contract address in this service's `USDT_CONTRACT_ADDRESS`. The
two must agree: BTCPay watches for incoming transfers of that token, and the
withdrawal verifier refuses any transfer of a different one.

The Nile contract address shipped as a default in `config.py` was confirmed
against a live Nile node on 2026-08-11: it answers `symbol()` = `USDT` and
`decimals()` = `6`. The session is recorded in the
[live verification log](verification-log.md). Check it against whatever the
plugin is pointed at anyway — a confirmed default is still a default, and a
faucet may hand out a different test token. The guided check for exactly that
is the [Nile verification runbook](runbook-nile-verification.md) — its
preflight reads `symbol()` and `decimals()` from the live contract on both
networks.

## 3. Address pool — the part with the sharp edge

Store > Settings > USDt: paste pre-generated TRON addresses, one per line.

**Provision at least 20.** The pool size is the maximum number of USDT deposits
that can be open at once. The plugin reserves one address per invoice and
releases it afterwards, so:

- running out surfaces to the platform as `503
  DEPOSIT_TEMPORARILY_UNAVAILABLE`, which is retryable and says so
- **addresses are reused across users over time.** There is no per-user TRON
  address and there cannot be one.

That reuse is why USDT invoice expiry is 60 minutes and must not be shortened
to recycle the pool faster. A short window makes a late payment land while
someone else holds the address. See
[`runbook-usdt-attribution.md`](runbook-usdt-attribution.md).

## 4. TRX for gas

TRC-20 transfers cost TRX energy and bandwidth, not USDT. A hot wallet full of
USDT and empty of TRX cannot send anything, and the symptom looks like
"withdrawals are broken" rather than "top up the gas".

Keep the hot wallet funded; the gas monitor alerts below
`TRX_ALERT_THRESHOLD` (200 TRX by default) every 15 minutes.

## 5. Turn the asset on

Restart the api and worker. Payment-method discovery reads the store's methods
at startup and re-enables `USDT_TRC20` once one matches. Until then the asset
stays disabled and USDT deposit requests answer 503 — deliberately, because an
enabled asset with no payment method would mint invoices for an address nobody
is watching.

Confirm with `GET /v1/assets`.
