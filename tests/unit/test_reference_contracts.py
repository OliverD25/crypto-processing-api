"""The committed contracts must be what the code produces.

CI runs `scripts/export_openapi.py --check` for the same reason, but a build
that only fails after a push is a build that wastes a round trip. This is the
same assertion, ten milliseconds into `make test`, with a message saying
exactly which command fixes it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT


def _export_module() -> Any:
    """Load `scripts/export_openapi.py`. It is a script, not an installed module."""
    path = REPO_ROOT / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["openapi.json", "webhook-events.json"])
def test_the_committed_contract_matches_the_code(name: str) -> None:
    module = _export_module()
    documents: dict[Path, str] = module.build()
    path = REPO_ROOT / "docs" / "reference" / name
    expected = documents[path]

    assert path.exists(), f"{name} is missing; run `python scripts/export_openapi.py`"
    assert path.read_text(encoding="utf-8") == expected, (
        f"docs/reference/{name} is out of date with the code. "
        "Run `python scripts/export_openapi.py` and commit the result."
    )
