"""Dump the FastAPI OpenAPI schema for the contract generator.

Run as a module from the repo root so apps/api/* resolves on sys.path:

    .venv/bin/python -m packages.contracts.scripts.dump_openapi \
        packages/contracts/openapi.json

Or, when called without a path, writes to
``packages/contracts/openapi.json`` under the repo root. The script does
NOT start a server; it imports ``app.main:app`` and calls ``app.openapi()``
directly, so it's safe to run offline in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _resolve_repo_root() -> Path:
    here = Path(__file__).resolve()
    # packages/contracts/scripts/dump_openapi.py -> repo root is parents[3]
    return here.parents[3]


def _default_output_path(repo_root: Path) -> Path:
    return repo_root / "packages" / "contracts" / "openapi.json"


def main() -> int:
    repo_root = _resolve_repo_root()
    api_root = repo_root / "apps" / "api"
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))

    # Import lazily so the sys.path mutation above takes effect.
    from app.main import app  # noqa: WPS433 (intentional runtime import)

    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1]).resolve()
    else:
        out_path = _default_output_path(repo_root)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    spec = app.openapi()
    # Stable formatting so diffs are reviewable.
    out_path.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        display_path = out_path.relative_to(repo_root)
    except ValueError:
        display_path = out_path
    print(f"wrote {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
