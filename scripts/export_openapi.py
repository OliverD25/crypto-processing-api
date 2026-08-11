"""Write the API and webhook contracts to `docs/reference/`.

Two files, both generated, both committed, both drift-gated by CI:

- `openapi.json` — the request and response schema of every route.
- `webhook-events.json` — the JSON Schema of the eight outbound events, which
  no OpenAPI document can describe because they are not responses.

Committing generated files earns its keep here. Both are the input to the
generated SDKs, so a route or payload change that is not reflected in them
would ship a client that silently disagrees with the server. The CI job runs
this script and then `git diff --exit-code`: if the spec in the tree is not
what the code produces, the build fails and the fix is to run this script.

Deterministic on purpose — sorted keys, fixed indent, one trailing newline —
because a diff that changes on every run is a gate nobody can read.

    python scripts/export_openapi.py           # write
    python scripts/export_openapi.py --check   # exit 1 if the tree is stale
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

#: Importing `main` builds the module-level app, which reads the settings.
#: `setdefault`, so a developer with a real environment keeps it — the value is
#: never connected to either way.
PLACEHOLDER_DATABASE_URL = "postgresql+psycopg://spec:spec@localhost:5432/spec"
os.environ.setdefault("DATABASE_URL", PLACEHOLDER_DATABASE_URL)

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE = REPO_ROOT / "docs" / "reference"
OPENAPI_PATH = REFERENCE / "openapi.json"
WEBHOOK_PATH = REFERENCE / "webhook-events.json"


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build() -> dict[Path, str]:
    # Imported here rather than at module scope so `--help` works without a
    # settings environment.
    from crypto_processing_api.config import Settings
    from crypto_processing_api.main import create_app
    from crypto_processing_api.services.events import event_schema

    # The app factory only registers routers and middleware; nothing here opens
    # a connection. Building the settings explicitly rather than from the
    # environment is what lets a laptop and a CI runner produce the same bytes,
    # which is the only way the drift gate means anything.
    settings = Settings(database_url=PLACEHOLDER_DATABASE_URL)

    return {
        OPENAPI_PATH: render(create_app(settings).openapi()),
        WEBHOOK_PATH: render(event_schema()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; fail if a committed file is not what the code produces",
    )
    args = parser.parse_args(argv)

    documents = build()
    stale: list[Path] = []
    for path, rendered in documents.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == rendered:
            continue
        if args.check:
            stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT).as_posix()}")

    if stale:
        for path in stale:
            print(
                f"::error file={path.relative_to(REPO_ROOT).as_posix()}::"
                "out of date; run `python scripts/export_openapi.py` and commit the result",
                file=sys.stderr,
            )
        return 1
    if args.check:
        print("the committed contracts match the code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
