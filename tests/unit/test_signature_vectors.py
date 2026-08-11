"""The committed signature vectors must be what the server signs and accepts.

`sdks/signature-vectors.json` is the only thing standing between three
implementations of one HMAC scheme — this server's, the Python SDK's and the
TypeScript SDK's. Each of the three asserts against this file, so a
disagreement fails a build instead of failing an integrator's endpoint.

This is the server's half. The two SDK suites run the same cases against their
own verifiers.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from crypto_processing_api.core.signing import (
    PLATFORM_SIGNATURE_HEADER,
    REPLAY_WINDOW_SECONDS,
    sign_platform_payload,
    verify_platform_signature,
)
from crypto_processing_api.services.events import EVENT_TYPES, PLATFORM_EVENT_ADAPTER
from tests.conftest import REPO_ROOT

VECTORS_PATH = REPO_ROOT / "sdks" / "signature-vectors.json"


def _export_module() -> Any:
    """Load `scripts/export_signature_vectors.py`. It is a script, not a module."""
    path = REPO_ROOT / "scripts" / "export_signature_vectors.py"
    spec = importlib.util.spec_from_file_location("export_signature_vectors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_signature_vectors"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    assert VECTORS_PATH.exists(), "run `python scripts/export_signature_vectors.py`"
    loaded: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    return loaded


def body_bytes(vectors: dict[str, Any], name: str) -> bytes:
    return base64.b64decode(vectors["bodies"][name]["base64"])


def test_the_committed_vectors_are_what_the_generator_produces() -> None:
    """Same gate as the OpenAPI document, for the same reason.

    A hand-edited vector is not evidence of anything, and this is what makes
    hand-editing it fail rather than pass quietly.
    """
    documents: dict[Path, str] = _export_module().build()
    expected = documents[VECTORS_PATH]
    assert VECTORS_PATH.read_text(encoding="utf-8") == expected, (
        "sdks/signature-vectors.json is out of date with the code. "
        "Run `python scripts/export_signature_vectors.py` and commit the result."
    )


def test_the_scheme_block_describes_this_server(vectors: dict[str, Any]) -> None:
    scheme = vectors["scheme"]
    assert scheme["header"] == PLATFORM_SIGNATURE_HEADER
    assert scheme["replay_window_seconds"] == REPLAY_WINDOW_SECONDS


def test_every_body_is_the_bytes_it_says_it_is(vectors: dict[str, Any]) -> None:
    """`utf8` is a convenience field. It must never disagree with `base64`."""
    for name, entry in vectors["bodies"].items():
        raw = base64.b64decode(entry["base64"])
        if "utf8" in entry:
            assert entry["utf8"].encode("utf-8") == raw, name
        else:
            with pytest.raises(UnicodeDecodeError):
                raw.decode("utf-8")


def test_a_body_that_is_not_utf8_is_present(vectors: dict[str, Any]) -> None:
    """Otherwise nothing proves the scheme signs bytes rather than a string."""
    assert any("utf8" not in entry for entry in vectors["bodies"].values())


def test_every_signature_is_what_the_server_produces(vectors: dict[str, Any]) -> None:
    assert vectors["sign"], "an empty sign matrix would pass every implementation"
    for case in vectors["sign"]:
        produced = sign_platform_payload(
            case["secret"], body_bytes(vectors, case["body"]), timestamp=case["timestamp"]
        )
        assert produced == case["signature"], case["name"]


def test_every_verify_case_agrees_with_the_server(vectors: dict[str, Any]) -> None:
    assert vectors["verify"], "an empty verify matrix would pass a verifier that returns true"
    for case in vectors["verify"]:
        accepted = verify_platform_signature(
            case["secret"],
            body_bytes(vectors, case["body"]),
            case["header"],
            now=case["now"],
        )
        assert accepted is case["valid"], f"{case['name']}: {case['why']}"


def test_the_verify_matrix_has_both_answers(vectors: dict[str, Any]) -> None:
    """A file of passing cases only is satisfied by `return True`."""
    answers = {case["valid"] for case in vectors["verify"]}
    assert answers == {True, False}


def test_every_event_type_has_a_sample_body(vectors: dict[str, Any]) -> None:
    """The SDK parser fixtures live here too, so every type must be covered."""
    sampled = {
        name.removeprefix("event.") for name in vectors["bodies"] if name.startswith("event.")
    }
    assert sampled == set(EVENT_TYPES)


def test_every_event_sample_is_a_real_envelope(vectors: dict[str, Any]) -> None:
    for name in vectors["bodies"]:
        if not name.startswith("event."):
            continue
        event = PLATFORM_EVENT_ADAPTER.validate_json(body_bytes(vectors, name))
        assert event.type == name.removeprefix("event.")
