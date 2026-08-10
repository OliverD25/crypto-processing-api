# Contributing

Thanks for looking. This is a custodial money service, so the bar for changes
to some parts of it is deliberately high — and deliberately explicit, so you
know which parts before you start.

## Setup

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pre-commit install
docker compose -f deploy/docker-compose.test.yml up -d
pytest
```

Python 3.12 or newer. The test database runs on port 54329 so it cannot collide
with a local PostgreSQL.

## The checks

```sh
make lint       # ruff check + format check
make typecheck  # mypy --strict over src/
make test       # unit + integration
make ci         # all of it, the same as the pipeline
```

CI runs exactly these. If they pass locally they pass there — that is on
purpose, and a change that only fails in CI is a bug in the setup worth
reporting.

## Test tiers

| Tier | Where | Needs | Speed |
|---|---|---|---|
| unit | `tests/unit/` | nothing | under a second |
| integration | `tests/integration/` | PostgreSQL 16 in Docker | ~30 seconds |
| regtest e2e | `scripts/dev/smoke_test.py` | the full regtest stack | minutes, manual |

**Integration tests use a real PostgreSQL, never SQLite.** Deferred constraint
triggers, partial unique indexes, `FOR UPDATE`, `ON CONFLICT` against a partial
index — those are the mechanisms under test, and SQLite has none of them.

**Concurrency is tested with real threads.** A mocked lock proves nothing about
a lock; the question is what two connections do when they contend for a row,
and only the database can answer it.

The end-to-end drills are manual because they need bitcoind and BTCPay. Run
them before anything that touches deposits or withdrawals — the last four
milestones each found at least one bug that no unit test caught.

## The money-path rule

**A pull request touching `src/crypto_processing_api/ledger/` or
`src/crypto_processing_api/services/` must say, in its description, which
ledger invariants still hold and why.**

Not a formality. The invariants are:

1. every journal entry's postings sum to zero
2. no posting has amount zero
3. every materialized balance equals the sum of its postings
4. no credit-normal account goes positive, no debit-normal account goes
   negative (except `external` and `user_deficit`, which exist to absorb
   corrections)
5. postings and entries are never updated or deleted — corrections are
   reversals
6. one ledger effect per `(kind, source_ref)`
7. one payout per withdrawal, one transaction id per withdrawal
8. a hold is released exactly once, by settle or by release, never both
9. custody equals obligations plus in-flight, minus booked losses

If your change cannot be described in those terms, it probably belongs outside
those directories.

Specifically:

- **`post_entry` is the only code that writes postings.** If you need a new
  money movement, add a caller, not a second writer.
- **New state transitions go in the legality matrix**, as data, with a test for
  every illegal pair.
- **External calls never happen inside an open database transaction.** Lock,
  decide, commit, then call out.

## Style

- `ruff` decides formatting. Do not argue with it; do not hand-format around it.
- `mypy --strict` over `src/`. Tests are exempt.
- **Comments explain a non-obvious "why", never a "what".** Most functions here
  have a docstring saying what would go wrong without them — that is the house
  style, and a comment restating the code will be asked to go.
- Prefer editing an existing module over adding one. The file tree is in
  `docs/design/05-merged-plan.md` §6 and deviations from it get explained.

## Commit messages

Explain **why**, not what — the diff already says what. Look at
`git log` for the register; the useful ones name the failure the change
prevents.

## Reporting a security issue

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Things that would genuinely help

- **an independent review of the money paths.** Adversarial reading of
  `ledger/service.py` and `services/withdrawals.py` is worth more than any
  feature.
- **the Phase-2 TRON sender**, behind the existing `WithdrawalBackend`
  protocol. It means holding a private key on the box, so the design matters
  more than the code.
- **more assets**, if they fit the integer-units model — `assets.decimals` is
  capped at 8 for a reason, and an 18-decimal asset needs a real conversation
  about BIGINT.
- **an operator's account of running it.** What alerted, what was noise, what
  the runbooks got wrong.
