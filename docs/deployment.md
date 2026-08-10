# Deploying

A fresh VPS to a running deployment, next to BTCPay. Written for a Hetzner
CAX11 (4GB, ARM) because that is what it was built and budgeted for, but
nothing here is Hetzner-specific.

## What you need first

- a VPS with BTCPay already installed via
  [btcpayserver-docker](https://github.com/btcpayserver/btcpayserver-docker),
  synced, with a **hot wallet** on its store
- a domain for this API (`api.example.com`) on Cloudflare
- somewhere to send backups: another host you can SSH into. A homelab machine
  is ideal and free.

If BTCPay is not up yet, do that first and come back. This service is useless
without it.

## The ten commands

```sh
# 1. get the code
sudo git clone https://github.com/OliverD25/crypto-processing-api \
  /opt/crypto-processing-api && cd /opt/crypto-processing-api

# 2. configuration — read .env.example, it documents every variable
sudo cp .env.example .env && sudo chmod 600 .env && sudo nano .env

# 3. the database's data directory, on the host so pgBackRest can see it
sudo mkdir -p /var/lib/crypto-processing-api/pgdata

# 4. bring it up (migrations run on start)
sudo docker compose -f deploy/docker-compose.yml up -d

# 5. configure BTCPay: store, hot wallet, webhook, scoped keys, processor
sudo docker compose -f deploy/docker-compose.yml exec api \
  python /app/scripts/bootstrap_btcpay.py

# 6. copy the generated ids and secrets into .env, then restart
sudo nano .env && sudo docker compose -f deploy/docker-compose.yml up -d

# 7. mint the key your platform will use
sudo docker compose -f deploy/docker-compose.yml exec api \
  python -m crypto_processing_api.cli create-api-key --name platform --scope readwrite

# 8. publish it
sudo cp deploy/nginx/api.conf.example /etc/nginx/conf.d/crypto-api.conf
sudo nano /etc/nginx/conf.d/crypto-api.conf   # set your hostname
sudo nginx -t && sudo systemctl reload nginx

# 9. firewall — READ THIS SCRIPT FIRST, it ends with `ufw enable`
sudo SSH_PORT=22 sh deploy/ufw/rules.sh

# 10. check
curl -s https://api.example.com/healthz
```

Step 6 exists because the bootstrap cannot write your `.env` for you — it
prints what it created into `.env.regtest.generated` style output, and the
values (`BTCPAY_STORE_ID`, `BTCPAY_API_KEY`, `BTCPAY_WEBHOOK_SECRET`) go into
`.env` by hand. Do it once.

## Before you point real money at it

Backups are not optional and not "later". See
[`backups.md`](backups.md) — the argument in one line: manual USDT withdrawals
exist **only** in this database, so a restore from an old dump silently
un-debits money that has already left custody.

Set up continuous archiving before the first real deposit.

## The .env values that actually matter

Everything is documented in `.env.example`. These are the ones that will hurt
if you get them wrong:

| Variable | Why it matters |
|---|---|
| `ENVIRONMENT=production` | refuses `DEBUG=true`, requires mainnet, requires a TronGrid key when TRON is configured |
| `BITCOIN_NETWORK=mainnet` | withdrawal addresses are validated against this. Wrong value, wrong chain |
| `POSTGRES_PASSWORD` | the database is not exposed, but generate a real one |
| `SEED_BTC_WITHDRAWAL_AUTO_LIMIT` | read once, at first `migrate`. After that the DB row is the truth |
| `SEED_BTC_WITHDRAWAL_DAILY_CAP` | **this is your loss ceiling under a stolen API key.** Set it to what you can afford to lose in a day |
| `PLATFORM_WEBHOOK_SECRET` | leave empty and outbound events park as pending — legitimate for a polling integration |
| `LIGHTNING_ENABLED` | off by default. Turning it on adds a second float, a second daily cap, and one server-level BTCPay scope — see below |

The seed values are read **once**. Changing them later does nothing; change the
`assets` row with SQL.

## Turning Lightning on

Default is off, and off is a deployment that has never heard of Lightning: no
`BTC_LN` row, no Lightning scope requested, nothing to think about. Read the
README's Lightning note and [`security.md`](security.md) before changing that.
The short version is that `BTC_LN` is a **separate float** — users get a second,
non-fungible BTC balance — and enabling it makes the bootstrap request
`btcpay.server.canuseinternallightningnode`, the one server-level scope this
project ever asks for.

If you still want it:

```sh
# 1. make sure BTCPay itself has a Lightning node (BTCPAY_BTCLIGHTNING)
# 2. set LIGHTNING_ENABLED=true and the SEED_LN_* values in .env
# 3. re-run the bootstrap so the scope, the payment method and the
#    Lightning payout processor are configured
LIGHTNING_ENABLED=true python scripts/bootstrap_btcpay.py
# 4. restart so `migrate` seeds the BTC_LN row and the registry picks it up
docker compose up -d --force-recreate api worker
```

Then set the caps deliberately. `SEED_LN_WITHDRAWAL_DAILY_CAP` is a **second**
loss ceiling: the BTC cap says nothing about how much can leave the channel.
Channel balance also cannot be swept to cold storage — what is committed to a
channel stays there until the channel closes — so size it as the amount you are
willing to leave permanently warm.

**Turning it back off is not a matter of unsetting the variable.** Once the
`BTC_LN` row exists, a build without the profile refuses to start, on purpose:
an asset holding user balances must not disappear because an environment
variable did. Zero the balances and disable the row deliberately, or leave it on.

Operating notes live in
[`runbook-ln-rebalance.md`](runbook-ln-rebalance.md); the one to know in advance
is that low outbound liquidity shows up as withdrawals that refund themselves
with a definitive-failure proof, not as an error.

## Cloudflare

1. Add the `api.example.com` A record, **proxied** (orange cloud).
2. SSL/TLS mode **Full (strict)** — the origin has a real certificate.
3. A cache rule for `api.example.com/*` with **Bypass cache**. The application
   sends `Cache-Control: no-store` and nginx repeats it, but a cached balance
   is bad enough to be worth all three.
4. Optionally a WAF rate-limit rule on `/v1/*`.

With `deploy/ufw/rules.sh` applied, 443 accepts traffic only from Cloudflare's
ranges, so learning the origin IP does not get an attacker past it. The
bitcoind port still reveals the IP; that is an accepted residual, documented in
[`security.md`](security.md).

## Verifying the install

```sh
curl -s https://api.example.com/healthz          # process + database
curl -s https://api.example.com/readyz | jq      # BTCPay, TronGrid, worker
sudo docker compose -f deploy/docker-compose.yml logs -f worker
```

`/readyz` is the interesting one. A `worker` component reporting `degraded`
means jobs have stopped, and nothing else would tell you: reads keep answering
200 while deposits quietly stop being credited.

Then do a real end-to-end with an amount you would not mind losing:

```sh
curl -s -X POST https://api.example.com/v1/deposits \
  -H "Authorization: Bearer $PLATFORM_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d '{"external_user_id":"install-test","asset":"BTC"}' | jq
```

Send a small amount to the address, wait for a confirmation, then check
`GET /v1/users/install-test/balances`. Withdraw it back. Until that round trip
works, the install is not finished.

## Operating it

- **Alerts**: set `NTFY_TOPIC_URL` or the Telegram pair. Free, and the threat
  model's honest conclusion is that the security budget is how little sits in
  the hot wallet and how fast you see an alert.
- **Uptime**: point a free pinger at `/healthz`.
- **Hot wallet float**: keep one to three days of payout volume. Sweep the rest
  to cold storage. See [`security.md`](security.md#hot-wallet-float-policy).
- **Approval queue**: `GET /v1/admin/withdrawals?status=pending_approval`
  needs a human. It alerts, but check it.

## Upgrading

```sh
cd /opt/crypto-processing-api && sudo git pull
sudo docker compose -f deploy/docker-compose.yml pull
sudo docker compose -f deploy/docker-compose.yml up -d
```

Migrations run on api start. Take a backup first — for a money database that is
not a formality, it is the rollback plan.

When bumping the pinned BTCPay image, run `python scripts/check_btcpay_compat.py`
first. It asserts the endpoints and fields this service depends on, several of
which have already moved once, and the failure mode of drift is a deposit that
silently stops crediting.

## Sizing

The whole stack fits in 4GB with bitcoind pruned, but not comfortably during
initial block download.

```sh
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Postgres is configured for `shared_buffers=128MB` and 50 connections; the API
runs a single uvicorn worker. On a single-tenant backend-to-backend service
that is not the bottleneck, and the memory belongs to bitcoind.

Also worth having: `unattended-upgrades` for security patches, and `fail2ban`
on SSH.
