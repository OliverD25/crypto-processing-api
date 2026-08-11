<!--
Thanks for the change. Two sections below; the second one is only required if
your diff touches `src/crypto_processing_api/ledger/` or
`src/crypto_processing_api/services/`. If it does not, delete it.
-->

## What this changes and why

<!-- The failure it prevents, or the thing it makes possible. The diff already
says what; this says why. -->

## Money-path invariants

<!--
REQUIRED if this touches ledger/ or services/. Delete the whole section if it
does not.

The rule is in CONTRIBUTING.md and it is not a formality: these nine are what
the ledger is for, they are checked by
`src/crypto_processing_api/ledger/invariants.py` and by the conformance
contracts in `src/crypto_processing_api/testing/contracts.py`, and a change
that cannot be described in these terms probably belongs outside those
directories.

Tick each one you have reasoned about, and add a line saying WHY it still
holds. "Unaffected" is a valid answer when it is true and stated.
-->

- [ ] **1. Every journal entry's postings sum to zero.**
- [ ] **2. No posting has amount zero.**
- [ ] **3. Every materialized balance equals the sum of its postings.**
- [ ] **4. No credit-normal account goes positive, no debit-normal account goes negative** — except `external` and `user_deficit`, which exist to absorb corrections.
- [ ] **5. Postings and entries are never updated or deleted.** Corrections are reversals.
- [ ] **6. One ledger effect per `(kind, source_ref)`.**
- [ ] **7. One payout per withdrawal, one transaction id per withdrawal.**
- [ ] **8. A hold is released exactly once** — by settle or by release, never both.
- [ ] **9. Custody equals obligations plus in-flight, minus booked losses.**

Why each of the above still holds:

<!-- e.g. "1-3, 5-6: unaffected, no new post_entry caller. 4: the new refund
path credits user_available, which is credit-normal and moves negative. 7-8:
the new backend reuses find_for_withdrawal, so a crashed submit still adopts
rather than creating a second payout. 9: routing fees are booked to
network_fee_expense at settle, so the shortfall stays explained." -->

And the three specific rules:

- [ ] **`post_entry` is still the only code that writes postings** — a new money movement is a new caller, not a second writer.
- [ ] **New state transitions are in the legality matrix, as data,** with a test for every illegal pair.
- [ ] **No external call happens inside an open database transaction** — lock, decide, commit, then call out.

## Checks

- [ ] `make ci` passes locally (ruff, mypy --strict, unit + integration, ledger coverage floor).
- [ ] If a route, a request model or a response model changed: `make contracts` was run and `docs/reference/` is committed.
- [ ] If a wire byte changed: `tests/fixtures/wire/responses.json` was regenerated **deliberately**, and the change is described in `CHANGELOG.md` under Breaking / Migration.
- [ ] If deposits or withdrawals changed: the regtest drills were run (`python scripts/dev/smoke_test.py`), and the output is below or summarized.
- [ ] If a migration was added: it downgrades cleanly, `alembic check` passes, and it upgrades the frozen dumps in `tests/fixtures/upgrade/`.
