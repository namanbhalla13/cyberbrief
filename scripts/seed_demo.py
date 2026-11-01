"""Seed named demo sessions and curated cases for local testing.

Sessions: citizen-a (Demo A loaded), citizen-b (Demo B loaded, initial step only), stranger
(no cases; exists to prove isolation). Idempotent by label.
For an officer account, run scripts/seed_officer.py — officers now sign in rather than
switching a simulated role.
Processing runs inline in fixture mode unless AI_MODE=live is configured.

Usage (repo root):  uv run --project backend python scripts/seed_demo.py
Prints the session cookie values for local use only; never commit them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
os.environ.setdefault("JOB_RUNNER", "inline")

from sqlalchemy import select  # noqa: E402

from app.ai import init_runner  # noqa: E402
from app.casework import load_demo_case  # noqa: E402
from app.core import (  # noqa: E402
    get_settings,
    make_session,
)
from app.main import run_migrations  # noqa: E402
from app.models import Case, UserSession  # noqa: E402
from app.records import Storage  # noqa: E402
from app.security import new_session  # noqa: E402

SEEDS = [("citizen-a", "citizen", "demo-a"), ("citizen-b", "citizen", "demo-b"), ("stranger", "citizen", None)]


def main() -> int:
    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    run_migrations()
    init_runner(settings)
    from app.api import _start_run

    storage = Storage(settings)
    print(f"AI_MODE={settings.ai_mode} (fixture is labelled; it is not a live demonstration)")
    with make_session() as db:
        for label, role, pack in SEEDS:
            session = db.scalar(select(UserSession).where(UserSession.label == label))
            if session is None:
                session = new_session(db, settings, label=label, role=role)
                db.commit()
            if pack:
                existing = db.scalar(select(Case).where(Case.session_id == session.id, Case.demo_pack_id == pack))
                if existing is None:
                    case = load_demo_case(db, session, settings, storage, pack)
                    db.commit()
                    db.refresh(case)
                    _start_run(db, case, settings)
                    case = db.get(Case, case.id)
                    print(f"{label}: loaded {pack} as case {case.id} (revision {case.current_revision}, {case.packet_status})")
                else:
                    print(f"{label}: {pack} already loaded as case {existing.id}")
            print(f"  cookie cb_session={session.id} (role {session.role}) — local use only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
