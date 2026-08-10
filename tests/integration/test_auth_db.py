"""Key lifecycle against the api_keys table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from crypto_processing_api.core import auth


def make_key(session: Session, *, scope: str = auth.SCOPE_READWRITE) -> auth.GeneratedApiKey:
    generated, _ = auth.create_api_key(
        session, name="platform", scope=scope, prefix=auth.KEY_PREFIX_TEST
    )
    session.commit()
    return generated


def test_created_key_authenticates(session: Session) -> None:
    generated = make_key(session)
    authenticated = auth.authenticate(session, generated.key)
    assert authenticated.key_id == generated.key_id
    assert authenticated.scope == auth.SCOPE_READWRITE
    assert authenticated.name == "platform"


def test_only_the_hash_is_stored(session: Session) -> None:
    generated = make_key(session)
    from crypto_processing_api.ledger.models import ApiKey

    record = session.query(ApiKey).filter_by(key_id=generated.key_id).one()
    assert record.key_hash == auth.hash_api_key(generated.key)
    assert generated.key not in record.key_hash


def test_a_different_key_with_a_forged_key_id_is_rejected(session: Session) -> None:
    generated = make_key(session)
    impostor = auth.generate_api_key(auth.KEY_PREFIX_TEST)
    forged = auth.KEY_PREFIX_TEST + generated.key_id + impostor.key[-35:]
    with pytest.raises(auth.UnknownApiKey):
        auth.authenticate(session, forged)


def test_unknown_key_is_rejected(session: Session) -> None:
    with pytest.raises(auth.UnknownApiKey):
        auth.authenticate(session, auth.generate_api_key(auth.KEY_PREFIX_TEST).key)


def test_revoked_key_is_rejected(session: Session) -> None:
    generated = make_key(session)
    assert auth.revoke_api_key(session, generated.key_id) is True
    session.commit()
    with pytest.raises(auth.RevokedApiKey):
        auth.authenticate(session, generated.key)


def test_revoking_twice_reports_no_change(session: Session) -> None:
    generated = make_key(session)
    auth.revoke_api_key(session, generated.key_id)
    session.commit()
    assert auth.revoke_api_key(session, generated.key_id) is False
    assert auth.revoke_api_key(session, "nosuchid") is False


def test_expired_key_is_rejected(session: Session) -> None:
    generated, _ = auth.create_api_key(
        session,
        name="temporary",
        scope=auth.SCOPE_READWRITE,
        prefix=auth.KEY_PREFIX_TEST,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.commit()
    with pytest.raises(auth.ExpiredApiKey):
        auth.authenticate(session, generated.key)


def test_key_valid_until_its_expiry(session: Session) -> None:
    generated, _ = auth.create_api_key(
        session,
        name="temporary",
        scope=auth.SCOPE_ADMIN,
        prefix=auth.KEY_PREFIX_TEST,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.commit()
    assert auth.authenticate(session, generated.key).scope == auth.SCOPE_ADMIN


def test_invalid_scope_refused_at_creation(session: Session) -> None:
    with pytest.raises(ValueError, match="scope must be one of"):
        auth.create_api_key(session, name="bad", scope="superuser", prefix=auth.KEY_PREFIX_TEST)


def test_last_used_is_written_once_per_window(session: Session) -> None:
    generated = make_key(session)
    from crypto_processing_api.ledger.models import ApiKey

    record = session.query(ApiKey).filter_by(key_id=generated.key_id).one()
    assert record.last_used_at is None

    auth.touch_last_used(session, generated.key_id)
    session.commit()
    session.refresh(record)
    first = record.last_used_at
    assert first is not None

    auth.touch_last_used(session, generated.key_id)
    session.commit()
    session.refresh(record)
    assert record.last_used_at == first
