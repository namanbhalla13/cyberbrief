"""Print an identifier for the source an image is built from: today's date and a hash of the files that make the app (not tests, data or screenshots).

The same files always give the same identifier, so a running demo can be compared with the working tree: `curl localhost:8080/api/health` shows the id it was
built from. It does not need git, so it works before anything is committed.

Usage:  BUILD_ID=$(python scripts/build_id.py) docker compose up -d --build
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ("backend/app", "backend/migrations", "backend/pyproject.toml", "backend/uv.lock", "frontend/src", "frontend/package.json", "frontend/index.html", "scripts", "evals/fixtures", "Dockerfile")
SKIP = {"__pycache__", "node_modules", ".venv", "dist", ".pytest_cache"}


def digest() -> str:
    h = hashlib.sha256()
    for part in PARTS:
        base = ROOT / part
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file() and not (SKIP & set(p.parts)) and p.suffix != ".pyc")
        for f in files:
            h.update(f.relative_to(ROOT).as_posix().encode())
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


if __name__ == "__main__":
    sys.stdout.write(f"{datetime.now(UTC):%Y%m%d}-{digest()}\n")
