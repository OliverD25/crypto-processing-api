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
make contracts  # regenerate docs/reference/ after a route or payload change
```

CI runs exactly these. If they pass locally they pass there — that is on
purpose, and a change that only fails in CI is a bug in the setup worth
reporting.

## The published contracts

`docs/reference/openapi.json` and `docs/reference/webhook-events.json` are
generated and committed, and CI regenerates them and fails on any difference.
**If you touch a route, a request model, a response model or an outbound event
payload, run `make contracts` and commit the result.** The SDKs are generated
from those two files, so a stale one ships a client that disagrees with the
server in somebody's production.

Two related things, in the same spirit:

- **`tests/integration/test_wire_bytes.py` compares raw response bytes** — not
  parsed JSON — against a corpus captured before the response models existed.
  It is what stops a `response_model` silently turning `+00:00` into `Z` or an
  amount string into a JSON number, both of which every `response.json()` test
  in the suite would sail straight past. If it goes red, the wire format moved;
  regenerating the corpus (`WIRE_GOLDEN_WRITE=1 pytest
  tests/integration/test_wire_bytes.py`) is a deliberate act that belongs in
  the changelog under Breaking / Migration, never a way to make a build green.
- **Outbound event payloads are built through the models in
  `services/events.py`.** A field that is not declared there is dropped rather
  than sent, which is on purpose: it makes the exported schema true by
  construction instead of by review.

What a release is allowed to change about any of this is in
[`docs/reference/versioning.md`](docs/reference/versioning.md).

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

## Migrations

A migration is the one change that runs against somebody's real data, once,
unattended. Two rules follow from that.

**Downgrade must undo upgrade exactly.** `tests/integration/test_migration_roundtrip.py`
migrates up, fingerprints the schema, goes down to base, comes back up and
demands the identical fingerprint. If your downgrade is a stub, that test is
where it stops being your problem and starts being everyone's.

**Keep the models honest.** `alembic check` runs in CI. Hand-written DDL is
fine — much of `0001` is — but whatever it creates has to be mirrored in
`ledger/models.py`, including indexes. Nothing here calls `create_all`, so the
metadata emits no DDL and drift is invisible until something like this looks
for it.

### Release checklist

**Every release adds a frozen database dump.** After tagging `vX.Y.Z`:

```
docker compose -f deploy/docker-compose.test.yml up -d
python scripts/make_upgrade_fixture.py --version X.Y.Z
```

That writes `tests/fixtures/upgrade/vX.Y.Z.sql`. Commit it and **never edit it
again** — its whole value is being a real database from that version, which a
file anyone has touched is not. From then on every future migration must be
able to upgrade it, which is what catches the migration that is correct on
empty tables and wrong on data.

**After the FIRST release that publishes the npm package**: the `NPM_TOKEN`
secret was bootstrapped with all-packages access because the package did not
exist yet (see `sdks/README.md`). Generate a new granular token limited to
`@oliverd25/crypto-processing-client`, replace the repository secret, and
revoke the bootstrap token.

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
