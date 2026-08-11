# The regtest walkthrough

The [quickstart](quickstart.md) gets the stack running. This page says what is
in it, what each drill actually proves, and how to run one at a time while you
are changing something.

Regtest is a private Bitcoin network where you mine blocks on demand. Nothing
here touches a real chain and no key is worth anything.

## What is in the stack

`deploy/docker-compose.regtest.yml` starts seven containers:

| Container | What it is |
|---|---|
| `bitcoind` | a Bitcoin node in regtest mode — you mine its blocks yourself |
| `nbxplorer` | BTCPay's chain indexer, between BTCPay and `bitcoind` |
| `btcpayserver` | BTCPay itself, with its Greenfield API |
| `postgres-btcpay` | BTCPay's own database |
| `postgres-ledger` | **this service's** database — the ledger, never shared with BTCPay |
| `api` | this service's HTTP API |
| `worker` | the reconciliation loop: jobs A, B and C |

Two databases, deliberately. The ledger is the record of who owns the coins,
and it does not live in an instance another application can migrate.

Lightning needs a second overlay file, which adds an `lnd` node for the store
and two more (`lnd-user`, `lnd-payee`) to play the counterparties:

```sh
docker compose -f deploy/docker-compose.regtest.yml \
               -f deploy/docker-compose.regtest.lightning.yml up -d
sh scripts/dev/ln_bootstrap.sh
LIGHTNING_ENABLED=true python scripts/bootstrap_btcpay.py
```

## Mining, and the first 101 blocks

A freshly created regtest chain has no spendable coins, and a coinbase output
is only spendable after 100 confirmations. So the first thing anything does is
mine 101 blocks:

```sh
make mine N=101
```

After that, one block per payment is enough to confirm it. `scripts/dev/mine.sh`
is a thin wrapper around `bitcoin-cli generatetoaddress`.

## The drills

`scripts/dev/smoke_test.py` is not part of `pytest`. It needs the whole stack
running, and two of its drills wait on real wall-clock deadlines. Every
assertion in it is about money: the exact number of satoshis credited, and the
number of times it was credited.

```sh
python scripts/dev/smoke_test.py                 # everything except `late`
python scripts/dev/smoke_test.py --drill late    # adds about three minutes
python scripts/dev/smoke_test.py --drill deposit # just one
```

| Drill | What it proves |
|---|---|
| `deposit` | create a deposit, pay it, mine — the balance is exact to the satoshi |
| `outage` | stop the API, pay, mine, restart. The poller has to credit it with no webhook ever arriving. This is the whole claim of the reconciliation design |
| `replay` | ask BTCPay to redeliver the same webhook repeatedly — still exactly one credit |
| `late` | pay **after** the invoice expired. It must land in review rather than credit, and an admin resolve then credits it |
| `withdraw` | below the auto-approval limit: goes straight out, exact to the satoshi |
| `approval` | above the limit: waits for an admin, then completes |
| `crash` | the payout is created but its status is never written. The recovery path must not send a second one |

The Lightning drills need the overlay stack and are skipped without it:

```sh
python scripts/dev/smoke_test.py --drill lightning
```

| Drill | What it proves |
|---|---|
| `ln_deposit` | a BOLT11 paid from another node settles instantly, exact sats |
| `ln_withdraw` | the payment hash is recorded and the routing fee is booked to `network_fee_expense` |
| `ln_exhausted` | ask for more than the channel can route — the deadline cancels it and the node's own answer releases the hold |
| `ln_expired` | an expired invoice is refused before any balance is held |

`lnd-user` and `lnd-payee` are two separate nodes on purpose. `lnd-user` has a
large channel *to* the store so it can pay deposits; `lnd-payee` sits behind a
small channel *from* the store, so its inbound capacity is what a withdrawal
runs out of. Share one node between the two roles and every deposited satoshi
becomes one you can pay straight back, which makes `ln_exhausted` impossible to
stage.

## Looking at the books

The ledger is ordinary PostgreSQL and the drills read it with `psql`. So can
you:

```sh
docker compose -f deploy/docker-compose.regtest.yml exec postgres-ledger \
  psql -U cpapi -d cpapi -c "select * from journal_entries order by id desc limit 10"
```

Every credit and debit is a row, nothing is ever updated, and the balances are
derived from them. If a drill's number surprises you, the entries are where the
answer is — see the [ledger design](../design/02-ledger-design.md).

## Starting over

```sh
make regtest-down    # stops everything and deletes the volumes
```

That destroys the chain, both databases and the BTCPay store. Since the store
is gone, run `python scripts/bootstrap_btcpay.py` again after the next
`make regtest-up`.
