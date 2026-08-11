<!--
The command block below is the same one in README.md and is kept in sync by
hand. If you change one, change the other. The README has to work on GitHub
with no site around it, which is why it is duplicated rather than included.
-->

# Quickstart

About ten minutes, on your own machine, with no real money anywhere near it.
At the end you will have a BTCPay Server on a private regtest chain, this
service beside it, and a script that deposits, mines, withdraws and asserts the
balances to the satoshi.

## What you need

- Docker with Compose, and roughly 4 GB of free memory for the stack.
- Python 3.12 or newer.
- About 6 GB of disk. Regtest blocks are tiny; the BTCPay images are not.

## Run it

```sh
git clone https://github.com/OliverD25/crypto-processing-api && cd crypto-processing-api
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

docker compose -f deploy/docker-compose.test.yml up -d   # test database
pytest                                                    # the test suite

docker compose -f deploy/docker-compose.regtest.yml up -d # bitcoind, BTCPay, api, worker
python scripts/bootstrap_btcpay.py                        # configure BTCPay
docker compose -f deploy/docker-compose.regtest.yml up -d --force-recreate api worker
python scripts/dev/smoke_test.py                          # end-to-end drills
```

On Windows the virtualenv puts its executables in `.venv/Scripts` rather than
`.venv/bin`. Everything else is the same.

The last command creates a deposit, pays it from a regtest node, mines,
withdraws, and asserts the balances to the satoshi. It also stops the API
mid-payment to prove the reconciliation path credits without help. What each
drill proves, and how to run one at a time, is the
[regtest walkthrough](regtest.md).

## What just happened

`bootstrap_btcpay.py` created a BTCPay store, a wallet, an API key scoped to
that one store, and a webhook pointed at this service. It wrote the values it
generated to `.env.regtest.generated`, which the API and worker containers
read. Re-running it is safe — it finds what already exists rather than making a
second one.

The `--force-recreate` step exists because the API and worker read that file at
startup, and it did not have anything in it the first time they booted.

## Make targets

The same steps, shorter, from a shell that has `make`:

```sh
make install      # venv + editable install with dev tooling
make db-up        # the throwaway test Postgres on port 54329
make test         # unit and integration suites
make regtest-up   # the BTCPay regtest stack
make bootstrap    # configure it
make mine N=101   # mine regtest blocks
make regtest-down # tear it down, volumes and all
```

`make help` lists all of them.

## Next

- **Build against it:** [Integrating](../integrating/index.md) is the contract —
  deposit and withdrawal lifecycles, idempotency, and the five-step webhook
  rule. If you are on Python or Node, start with the
  [client libraries](../integrating/sdks.md) instead.
- **Understand what the drills proved:** the
  [regtest walkthrough](regtest.md).
- **Put it on a server:** [Deploying](../operating/deployment.md). Read the
  [security model](../operating/security.md) first — the hot wallet float is
  the loss ceiling and no setting changes that.
