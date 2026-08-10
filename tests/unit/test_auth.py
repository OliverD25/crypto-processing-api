"""Key format, hashing and scope rules. No database involved."""

from __future__ import annotations

import pytest

from crypto_processing_api.core import auth


def test_generated_key_has_the_documented_shape() -> None:
    generated = auth.generate_api_key(auth.KEY_PREFIX_LIVE)
    assert generated.key.startswith(auth.KEY_PREFIX_LIVE)
    body = generated.key[len(auth.KEY_PREFIX_LIVE) :]
    assert len(body) == auth.SECRET_CHARS
    assert set(body) <= set(auth.BASE62_ALPHABET)
    assert generated.key_id == body[: auth.KEY_ID_LENGTH]


def test_key_id_is_a_prefix_of_the_secret_not_the_whole_key() -> None:
    generated = auth.generate_api_key(auth.KEY_PREFIX_TEST)
    assert generated.key_id in generated.key
    assert generated.key_id != generated.key


def test_generated_keys_do_not_repeat() -> None:
    keys = {auth.generate_api_key(auth.KEY_PREFIX_LIVE).key for _ in range(200)}
    assert len(keys) == 200


def test_unknown_prefix_refused() -> None:
    with pytest.raises(auth.InvalidApiKey):
        auth.generate_api_key("sk_live_")


def test_hash_is_sha256_hex() -> None:
    generated = auth.generate_api_key(auth.KEY_PREFIX_LIVE)
    assert len(generated.key_hash) == 64
    assert generated.key_hash == auth.hash_api_key(generated.key)
    assert generated.key not in generated.key_hash


def test_verify_accepts_only_the_exact_key() -> None:
    generated = auth.generate_api_key(auth.KEY_PREFIX_LIVE)
    assert auth.verify_api_key(generated.key, generated.key_hash)
    assert not auth.verify_api_key(generated.key + "x", generated.key_hash)
    assert not auth.verify_api_key(generated.key[:-1], generated.key_hash)
    assert not auth.verify_api_key(
        auth.generate_api_key(auth.KEY_PREFIX_LIVE).key, generated.key_hash
    )


def test_parse_round_trips_both_prefixes() -> None:
    for prefix in (auth.KEY_PREFIX_LIVE, auth.KEY_PREFIX_TEST):
        generated = auth.generate_api_key(prefix)
        parsed = auth.parse_api_key(generated.key)
        assert parsed.prefix == prefix
        assert parsed.key_id == generated.key_id


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "not-a-key",
        "cpk_live_",
        "cpk_live_tooshort",
        "cpk_live_" + "a" * 44,
        "cpk_live_" + "a" * 42 + "!",
        "CPK_LIVE_" + "a" * 43,
    ],
)
def test_malformed_keys_refused(bad_key: str) -> None:
    with pytest.raises(auth.InvalidApiKey):
        auth.parse_api_key(bad_key)


def test_admin_covers_readwrite_but_not_the_other_way() -> None:
    assert auth.has_scope(auth.SCOPE_ADMIN, auth.SCOPE_READWRITE)
    assert auth.has_scope(auth.SCOPE_ADMIN, auth.SCOPE_ADMIN)
    assert auth.has_scope(auth.SCOPE_READWRITE, auth.SCOPE_READWRITE)
    assert not auth.has_scope(auth.SCOPE_READWRITE, auth.SCOPE_ADMIN)


def test_unknown_scope_never_satisfies_anything() -> None:
    assert not auth.has_scope("wishful", auth.SCOPE_READWRITE)
    assert not auth.has_scope(auth.SCOPE_ADMIN, "wishful")


def test_require_scope_raises_with_both_sides_named() -> None:
    key = auth.AuthenticatedKey(id=1, key_id="abcd1234", name="platform", scope="readwrite")
    with pytest.raises(auth.InsufficientScope) as excinfo:
        auth.require_scope(key, auth.SCOPE_ADMIN)
    assert excinfo.value.required == auth.SCOPE_ADMIN
    assert excinfo.value.actual == auth.SCOPE_READWRITE
