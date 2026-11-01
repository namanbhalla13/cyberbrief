"""Seed the fictional reports the investigation assistant is demonstrated on: names, identifiers, amounts and stories are all made up.

Run `scripts/seed_workflow.py --staff-only` first, so the officers exist (Arjun CBI1042 and Meera CBI2001 in Hyderabad Central, Divya CBI3001 in
Hyderabad East). It adds nine reports of the set described in `backend/app/demo_investigation.py` and indexes them. It adds nothing if the set is
already there, deletes nothing, and refuses to run unless the app is in demo mode.

Usage (repo root, or inside the container):
    python scripts/seed_investigation_demo.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core import get_settings, make_session  # noqa: E402
from app.demo_investigation import build_dataset, build_eval_extras, title_of  # noqa: E402
from app.main import run_migrations  # noqa: E402
from app.models import Account, Case, OfficerProfile  # noqa: E402

STAFF = {"arjun": "CBI1042", "meera": "CBI2001", "divya": "CBI3001"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the fictional investigation set.")
    parser.add_argument("--tag", default="", help="Make this set's recipient and title its own, so browser tests can each have one")
    parser.add_argument("--json", action="store_true", help="Print the ids of the reports as JSON on the last line")
    parser.add_argument("--extras", action="store_true", help="Also add the seventeen further reports used by the labelled evaluation (repeated recipient, payment hub, contradictions, missing detail, ...)")
    args = parser.parse_args()
    mark = title_of(args.tag)
    if not get_settings().demo_mode:
        print("Refusing to run: this is fictional demonstration data and the app is not in demo mode.")
        return 2
    run_migrations()
    with make_session() as db:
        ids = {}
        for name, staff_id in STAFF.items():
            account = db.scalar(select(Account).where(Account.officer_id == staff_id))
            if account is None:
                print(f"Officer {staff_id} does not exist. Run scripts/seed_workflow.py --staff-only first.")
                return 2
            ids[name] = account.id
        if db.scalar(select(Case.id).where(Case.title == mark, Case.assigned_officer_id == ids["arjun"])) is not None:
            print("The fictional investigation set is already in place; nothing was added.")
            if args.json:
                existing = db.scalar(select(Case.id).where(Case.title == mark, Case.assigned_officer_id == ids["arjun"]))
                print(json.dumps({"current": existing}))
            return 0
        cases = build_dataset(db, ids, tag=args.tag)
        if args.extras:
            cases.update(build_eval_extras(db, ids, tag=args.tag))
        for case_id in cases.values():   # the officers' workloads reflect the reports they now hold
            case = db.get(Case, case_id)
            profile = db.get(OfficerProfile, case.assigned_officer_id)
            profile.active_count += 1
            profile.capacity = max(profile.capacity, profile.active_count + 2)
        db.commit()
    print(f"Added {len(cases)} fictional reports and indexed them. Open CBI1042's report \"{mark}\" and choose Connections.")
    if args.json:
        print(json.dumps(cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
