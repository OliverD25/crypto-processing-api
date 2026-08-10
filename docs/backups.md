# Backups and restore

This database is the ledger. If you lose it, you do not lose "some data" — you
lose the record of who owns the coins in your hot wallet.

Backups are therefore part of the first milestone, not part of hardening.

## Why continuous archiving, not a nightly dump

A nightly `pg_dump` gives a recovery point objective of up to 24 hours. Picture
the disk dying at 22:00 with the last dump taken at 03:00. What can you rebuild?

| Record type | External source of truth | Rebuildable after a 19-hour gap? |
|---|---|---|
| BTC deposits | BTCPay invoices | Yes, by replaying invoices |
| BTC payouts | BTCPay payout history | Yes |
| Deposit review decisions | none | **No** |
| Manual adjustments | none | **No** |
| Withdrawal holds | none | **No** |
| **Manual USDT withdrawals** | none | **No** |

The last row is the one that ends badly. USDT withdrawals are operator-driven:
the admin approves, a human sends from the TRON hot wallet, and the txid is
recorded here with `backend_ref = 'manual:<uuid>'`. The TRON chain shows an
outflow from your wallet and nothing else. It does not say which user it
belonged to. After a restore from an old dump, those users' balances quietly
go back up while the money has already left custody. You either absorb the
loss or reconcile TronScan against memory.

Continuous WAL archiving moves the recovery point from a day to minutes, for
the price of some disk on a machine you already own.

## The shape of it

```
  VPS                                   backup host (homelab, NAS, second VPS)
 ┌──────────────────────────┐          ┌───────────────────────────────┐
 │ postgres (ledger)        │          │ pgBackRest repository         │
 │   archive_command ───────┼──ssh────▶│   full + incremental backups  │
 │ pgbackrest (host binary) │          │   WAL segments                │
 │ nightly pg_dump ─────────┼──scp────▶│   dumps/                      │
 └──────────────────────────┘          └───────────────────────────────┘
```

pgBackRest pushes each WAL segment as Postgres finishes it, plus a weekly full
and nightly incremental backup. The `pg_dump` is a second, independent format:
if a pgBackRest repository is ever corrupt or a version upgrade makes it
unreadable, a plain SQL dump still restores anywhere.

Two formats, one destination is fine. Two destinations is better if you have
one.

## Reaching the backup host

pgBackRest talks to the repository over plain SSH. The only question is how the
VPS reaches a machine that is usually behind a home router.

**Option A — Tailscale (recommended).** Both machines join one tailnet and get
stable addresses. Nothing is exposed to the internet, and no router
configuration survives your ISP changing your IP.

```bash
# on both machines
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh=false
tailscale ip -4          # note the 100.x.y.z address of the backup host
```

Use the backup host's `100.x.y.z` address as the pgBackRest host below.

**Option B — SSH port forward on the router.** Forward an external port to the
backup host's port 22, and point pgBackRest at the router's public address with
`repo1-host-port`. This works, but it puts an SSH port on the public internet:
key-only authentication, `fail2ban`, and a non-default port are the minimum.
Also give the router a dynamic-DNS name, or the backup silently stops the day
your IP changes.

**Option C — any other reachable host.** A second VPS, a NAS with SSH, or an
object-storage bucket (pgBackRest speaks S3, Azure and GCS natively). The rest
of this document only assumes "a host you can SSH into".

Whichever you pick, the rule is the same: **the backup host must not be
reachable from the VPS with credentials that can also delete backups.** Use a
dedicated key, and give it a restricted account.

## Setup

### 1. Users and keys

On both machines pgBackRest runs as the `postgres` user.

```bash
# VPS
sudo -u postgres ssh-keygen -t ed25519 -N "" -f /var/lib/postgresql/.ssh/id_ed25519
sudo -u postgres cat /var/lib/postgresql/.ssh/id_ed25519.pub

# backup host: create the repo owner and authorise that key
sudo useradd -m -s /bin/bash pgbackrest
sudo mkdir -p /home/pgbackrest/.ssh /var/lib/pgbackrest
sudo chown -R pgbackrest:pgbackrest /home/pgbackrest/.ssh /var/lib/pgbackrest
sudo -u pgbackrest tee -a /home/pgbackrest/.ssh/authorized_keys   # paste the key
sudo chmod 600 /home/pgbackrest/.ssh/authorized_keys
```

Restrict the key. In `authorized_keys`, prefix it with:

```
command="/usr/bin/pgbackrest ${SSH_ORIGINAL_COMMAND#* }",restrict ssh-ed25519 AAAA...
```

That key can then run pgBackRest and nothing else.

### 2. `pgbackrest.conf` on the VPS

`/etc/pgbackrest/pgbackrest.conf`:

```ini
[global]
repo1-host=100.x.y.z              ; Tailscale address, or your router's DDNS name
repo1-host-user=pgbackrest
repo1-path=/var/lib/pgbackrest
repo1-retention-full=4            ; keep four weekly fulls
repo1-retention-diff=14
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<long random string, stored in your password manager>
process-max=2                     ; a 4GB box shares RAM with BTCPay and bitcoind
log-level-console=info
log-level-file=detail
start-fast=y
archive-async=y
spool-path=/var/spool/pgbackrest

[ledger]
pg1-path=/var/lib/postgresql/16/main
pg1-port=5432
```

`repo1-cipher-pass` is not optional. The repository holds every balance and
every user identifier you have. **Store that passphrase somewhere other than
the VPS and other than the backup host.** A backup you cannot decrypt is not a
backup.

### 3. `pgbackrest.conf` on the backup host

```ini
[global]
repo1-path=/var/lib/pgbackrest
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<the same passphrase>

[ledger]
pg1-host=100.x.y.z                ; the VPS
pg1-host-user=postgres
pg1-path=/var/lib/postgresql/16/main
```

### 4. Postgres configuration

```ini
# postgresql.conf
archive_mode = on
archive_command = 'pgbackrest --stanza=ledger archive-push %p'
wal_level = replica
max_wal_senders = 3
archive_timeout = 60      # cap the recovery point at one minute of idle time
```

`archive_timeout = 60` matters on a quiet ledger. Without it, a low-traffic
database can sit on a partly filled WAL segment for hours and that hour of
withdrawals is not archived anywhere.

Reload, then create and verify the stanza:

```bash
sudo systemctl restart postgresql
sudo -u postgres pgbackrest --stanza=ledger stanza-create
sudo -u postgres pgbackrest --stanza=ledger check
sudo -u postgres pgbackrest --stanza=ledger --type=full backup
```

`check` is the important one. It writes a test WAL segment and confirms it
arrived. If it fails, archiving is not working, whatever the backup command
says.

### Running Postgres in Docker

If the ledger Postgres runs from `deploy/docker-compose.yml`, install
pgBackRest on the **host** and bind-mount the data directory so both the
container and the host binary see the same files:

```yaml
services:
  postgres:
    volumes:
      - /var/lib/postgresql/16/main:/var/lib/postgresql/data
```

The container's `archive_command` then needs pgBackRest inside the container as
well. The simpler arrangement, and the one these instructions assume, is to run
the ledger Postgres directly on the VPS rather than in a container. It is one
service, it needs a stable data directory, and it gains nothing from being
containerised.

## Schedule

```cron
# /etc/cron.d/pgbackrest — as the postgres user
30 2 * * 0  postgres pgbackrest --stanza=ledger --type=full backup
30 2 * * 1-6 postgres pgbackrest --stanza=ledger --type=incr backup
0  3 * * *  postgres /usr/local/bin/ledger-dump.sh
```

`/usr/local/bin/ledger-dump.sh` — the second, independent format:

```bash
#!/bin/sh
set -eu
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="/tmp/ledger-${STAMP}.dump"

pg_dump --format=custom --compress=9 --file="$FILE" "$DATABASE_URL"
gpg --batch --yes --encrypt --recipient "$BACKUP_GPG_RECIPIENT" "$FILE"
scp "${FILE}.gpg" pgbackrest@100.x.y.z:/var/lib/pgbackrest/dumps/
rm -f "$FILE" "${FILE}.gpg"

# Keep 30 days on the backup host.
ssh pgbackrest@100.x.y.z \
    "find /var/lib/pgbackrest/dumps -name 'ledger-*.dump.gpg' -mtime +30 -delete"
```

The dump is encrypted before it leaves the machine, with a key whose private
half lives neither on the VPS nor on the backup host.

## Monitoring

A backup system nobody watches is a backup system that stopped working in
March. Alert on all three of these:

1. `pgbackrest --stanza=ledger check` fails (run it hourly from cron).
2. The newest backup in `pgbackrest info` is older than 26 hours.
3. `SELECT last_failed_time, failed_count FROM pg_stat_archiver` shows failures.

Wire them to the same notifier the service uses for withdrawal alerts. A silent
archiving failure is worth waking up for; it means the recovery point has been
sliding backwards since it started.

```bash
sudo -u postgres pgbackrest --stanza=ledger info
```

## Restore drill

**Run this quarterly, on a machine that is not the VPS.** An untested backup is
a belief, not a backup. Put the date of the last successful drill in your ops
notes.

```bash
# 1. Fresh machine, same Postgres major version, pgBackRest installed and
#    pointed at the repository (repo config + cipher passphrase).
sudo systemctl stop postgresql
sudo -u postgres rm -rf /var/lib/postgresql/16/main/*

# 2. Restore the latest backup and replay all archived WAL.
sudo -u postgres pgbackrest --stanza=ledger --delta restore

# 3. Start and let recovery finish.
sudo systemctl start postgresql
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"
```

Point-in-time recovery — restore to just before a bad migration or a mistaken
admin action:

```bash
sudo -u postgres pgbackrest --stanza=ledger \
    --type=time --target="2026-08-10 14:32:00+00" --delta restore
```

### Verify the restore, do not assume it

A restored ledger is only trustworthy if its own invariants hold. Run these
against the restored database before pointing anything at it:

```sql
-- Every journal entry must sum to zero.
SELECT entry_id, SUM(amount) AS total
FROM postings GROUP BY entry_id HAVING SUM(amount) <> 0;

-- Materialized balances must equal the sum of their postings.
SELECT a.id, a.balance, COALESCE(SUM(p.amount), 0) AS derived
FROM accounts a LEFT JOIN postings p ON p.account_id = a.id
GROUP BY a.id, a.balance
HAVING a.balance <> COALESCE(SUM(p.amount), 0);

-- Per asset, the signed balances must sum to zero.
SELECT asset_id, SUM(balance) FROM accounts GROUP BY asset_id HAVING SUM(balance) <> 0;
```

All three must return zero rows. The same checks live in
`crypto_processing_api.ledger.invariants` and run in the hourly reconciliation
job, so the restored database will keep checking itself once it is live.

Then compare custody against the outside world before accepting deposits or
withdrawals again:

- ledger `hot_wallet` for BTC against the BTCPay wallet balance
- ledger `hot_wallet` for USDT against the TRON hot wallet balance on TronScan
- `SUM` of user balances against both

If the wallet holds less than the sum of user balances, you are insolvent and
must stop withdrawals before anything else.

## After a restore: what to reconstruct by hand

Any gap between the recovery point and the failure has to be closed manually.
Work in this order.

1. **BTC deposits** — replay them. `GET /api/v1/stores/{storeId}/invoices` on
   BTCPay lists every invoice with our `cpapi` metadata. Reconciliation Job A
   re-credits anything missing on its own, because deposit credits are keyed on
   `btcpay_payment:{invoiceId}:{paymentId}` and re-posting an existing credit
   is a no-op. **Let the poller do this. Do not credit by hand.**
2. **BTC withdrawals** — BTCPay payout history has the payouts, their states
   and their txids. Match them to withdrawals by the `withdrawal_id` in the
   payout metadata.
3. **USDT withdrawals** — no external record exists. Pull the hot wallet's
   outgoing TRC-20 transfers from TronScan for the gap window and match each
   one to a user by amount, destination and time, against whatever support
   records you have. Write down what you decided and why. This is the step the
   whole continuous-archiving setup exists to keep short.
4. **Admin review decisions and adjustments** — also gone. Any deposit that was
   in the review queue re-appears there on the next sweep, which is the safe
   direction: it is unresolved rather than wrongly resolved.
5. **Withdrawal holds** — a lost hold means a user's balance shows as available
   while a payout may already be in flight. Freeze withdrawals until step 2 has
   accounted for every payout in the gap.

Freeze the platform's withdrawal path for the whole of this. Deposits are safe
to accept — they only ever add.

## Retention and cost

Four weekly fulls plus fourteen days of incrementals and their WAL is a few GB
for a ledger of this size. On a homelab disk that is free. Do not tune
retention down to save space you are not short of: the value of a backup is
mostly in how far back it goes when you discover the problem late.
