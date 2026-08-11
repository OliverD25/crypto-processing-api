# Versioning and deprecation policy

What a version number promises you, written down before it has to be argued
about. This project is pre-1.0, and pre-1.0 semver is usually a licence to
break things quietly. It is not used that way here, because the thing being
versioned holds other people's money.

One version number covers everything: the container image, the API, the
database schema and — once they ship — both SDKs. A tag `vX.Y.Z` releases them
together. For one maintainer that is the only scheme that stays true without
effort.

## What each part means

### Patch — `0.2.0` → `0.2.1`

**A patch never requires an operator to do anything beyond pull and restart.**

No new configuration, no new required environment variable, no manual
migration step, no change to a request or response body. If a fix cannot be
delivered under that constraint, it is not a patch, however small the diff.

### Minor — `0.2.x` → `0.3.0`

**A minor may change behaviour, and must say so where you will read it.**

Every minor with a behaviour change carries a **Breaking / Migration** section
in [`CHANGELOG.md`](../../CHANGELOG.md), naming what changed, what an operator
has to do, and in what order. The release workflow extracts the changelog
section for the tag, so a release with no section is a release with no notes —
which is the pressure that keeps the section honest.

A minor may add a required configuration value, change a default that alters
behaviour, add a database migration, add fields to a response, or add a new
outbound event type.

### Major — reserved

`1.0.0` is the point where the surface has been run in production by people who
are not the author, and where an external audit has happened. Neither has, so
the version starts with `0`.

## What does not change without a coexisting successor

These are the things that break an integration silently — the kind where every
test still passes and the money is wrong.

- **Amount encoding.** Amounts are decimal strings of the asset's display units
  (`"0.50000000"`), and the two request fields `expected_amount` and `amount`
  are integer strings of the smallest unit (`"50000000"`). Never JSON numbers.
  This will not change. If it ever has to, the new encoding arrives as an
  additional field alongside the old one, and the old one is removed no earlier
  than the deprecation rule below allows.
- **Timestamp encoding.** ISO 8601 with an explicit `+00:00` offset. Not `Z`,
  not epoch seconds. `tests/integration/test_wire_bytes.py` compares raw
  response bytes against a committed corpus precisely so this cannot move by
  accident — a `response_model` that re-serializes a datetime is the ordinary
  way that happens, and it is invisible to every test that compares parsed
  JSON.
- **The outbound signature scheme.** `X-CPA-Signature: t=<unix>,v1=<hex>`, HMAC
  SHA-256 over `"{t}.{raw_body}"`, 300-second window. The `v1=` prefix exists
  so that a `v2=` can be sent **beside** it: a future scheme ships as an extra
  element in the same header, both are sent for at least one minor, and only
  then is `v1=` dropped. No integrator has to redeploy on release day.
- **Idempotency semantics.** A completed key replays its stored response; a key
  in flight answers 409 with `Retry-After`; the same key with a different body
  answers 422; a missing key answers 400. Retrying with the same key is the
  documented behaviour and will keep working.

## Deprecation

**An endpoint or a field is marked deprecated for at least one full minor
release before it is removed.** Marked means all three of:

1. `deprecated: true` in the OpenAPI document, which is committed at
   [`openapi.json`](openapi.json) and therefore visible in a diff.
2. A note in [`api.md`](../api.md) saying what to use instead.
3. A line in the changelog for the release that marks it.

Removal is a minor release with a Breaking / Migration section. A field is
never repurposed: a name that meant one thing never comes back meaning
another.

**Additions are not deprecations.** New response fields, new optional request
fields, new event types and new endpoints can arrive in any minor. Parse
defensively: ignore fields you do not know, and treat an unknown event `type`
as something to skip rather than an error.

## Database migrations

- **Forward only.** Downgrades exist and are tested — `alembic upgrade`,
  fingerprint, `downgrade base`, upgrade again, demand the same fingerprint —
  but that test exists to prove a migration is reversible in development, not
  to make downgrading a supported production recovery. Production recovery is
  restore-from-backup; see [`backups.md`](../backups.md).
- **Every release is upgradeable from the previous release.** Not "from
  anywhere": from the release before it, in order. Skipping releases means
  applying them in sequence, which `alembic upgrade head` does for you.
- **This is proved by frozen dumps, not by intent.** Every release commits a
  real seeded database from that version to `tests/fixtures/upgrade/`, and a CI
  test loads each one, runs `alembic upgrade head`, then asserts the ledger is
  still consistent and the balances are unchanged. Those files are never
  edited after they land — a fixture someone has touched is no longer a real
  database from that version, which was its whole value. This is what catches
  the migration that is correct on empty tables and wrong on data.

## SDK versions

`client 0.N.x` supports `server 0.N.y`. An SDK-only fix bumps the SDK patch;
the server does not get a version bump just because a client changed, and a
release that leaves an SDK unchanged skips publishing it rather than shipping
an identical version.

Both SDKs are generated from the committed
[`openapi.json`](openapi.json) and [`webhook-events.json`](webhook-events.json)
in this directory, and CI fails if either file is not what the code produces.
That is the mechanism behind everything above: the spec cannot describe a
server that does not exist, so a client generated from it cannot quietly
disagree with the one you are running.

## Release cadence

Minor releases roughly quarterly. Security patches as soon as they are ready.
No schedule is promised beyond that, because promising one and missing it is
worse than not promising.
