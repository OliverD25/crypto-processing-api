"""Testing helpers that ship with the package.

Only `schema_fingerprint` lives here for now. It is in `src/` rather than
`tests/` because `scripts/make_upgrade_fixture.py` needs it too, and a helper
that two entry points import should not live in a test tree.
"""
