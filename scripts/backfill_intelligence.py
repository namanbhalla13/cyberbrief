"""Rebuild the search index for the reports already in this application's own database. Deterministic and local.

It reads only this database: it fetches nothing, ingests no outside data set and sends nothing anywhere. A report that is not submitted gets no
index entry. It refuses to run unless the app is in demo mode, because indexing a real department's reports needs an authorised source and an
explicit retention and access policy, and that is a decision for people, not for this script. Pass --i-have-authority only when that exists.

Usage (repo root, or inside the container):
    python scripts/backfill_intelligence.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import func, select  # noqa: E402

from app.core import get_settings, make_session  # noqa: E402
from app.intelligence import PROJECTION_VERSION, refresh_projections  # noqa: E402
from app.main import run_migrations  # noqa: E402
from app.models import CaseProjection, EntityObservation  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the case search index.")
    parser.add_argument("--i-have-authority", action="store_true", help="Index a database that is not a demo, because a retention and access policy allows it")
    args = parser.parse_args()
    if not get_settings().demo_mode and not args.i_have_authority:
        print("Refusing to run: the app is not in demo mode. Indexing real reports needs an authorised source and a retention and access policy.")
        return 2
    run_migrations()
    with make_session() as db:
        changed = refresh_projections(db, deep=True, commit_each=True)
        db.commit()
        reports = db.scalar(select(func.count()).select_from(CaseProjection)) or 0
        active = db.scalar(select(func.count()).select_from(EntityObservation).where(EntityObservation.state == "active")) or 0
    print(f"Index version {PROJECTION_VERSION}: {reports} report(s) indexed, {active} identifier observation(s) active, {changed} rebuilt this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
