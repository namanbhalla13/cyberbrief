"""Seed four fictional reports for the transaction-verification demonstration: a payment the simulated bank confirms, one that differs,
one it has no record of, and one with no transaction reference. Names, references and amounts are made up; nothing here is a real transaction.

Run `scripts/seed_workflow.py --staff-only` first, so the officer exists (Arjun, CBI1042, in Hyderabad Central). It adds nothing if the set
is already there, deletes nothing, and refuses to run unless the app is in demo mode.

Usage (repo root, or inside the container):
    python scripts/seed_verification_demo.py [--tag NAME] [--json]
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
from app.demo_investigation import make_case  # noqa: E402
from app.main import run_migrations  # noqa: E402
from app.models import Account, Case, OfficerProfile  # noqa: E402

OFFICER = "CBI1042"
SHOP = "Trendy Threads Store (trendythreads.demo@demobank)"


def _pay(reference: str | None, amount: int, date: str, status: str = "successful") -> dict:
    spec = {"payee": SHOP, "amountMinor": amount, "date": date, "state": "user_confirmed", "status": status}
    if reference:
        spec["reference"] = reference
    return spec


SET = {
    "match": ("Verification demo: bank record matches", "I paid an advance of Rs 10,000 for a jacket and it never arrived.", [_pay("DEMO-A-001", 1000000, "2026-10-12")]),
    "differs": ("Verification demo: bank record differs", "I paid Rs 5,000 as an advance. The receipt shows pending but the money left my account.", [_pay("DEMO-A-003", 500000, "2026-10-13", "pending")]),
    "nomatch": ("Verification demo: no record returned", "I paid Rs 7,500 to a seller I met online. I have the reference from the receipt.", [_pay("NO-SUCH-RECORD-7701", 750000, "2026-10-14")]),
    "missing": ("Verification demo: reference missing", "I paid Rs 2,000 to a seller; the receipt I kept does not show a reference.", [_pay(None, 200000, "2026-10-15")]),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the fictional verification set.")
    parser.add_argument("--tag", default="", help="Make this set's titles its own, so browser tests can each have one")
    parser.add_argument("--json", action="store_true", help="Print the ids of the reports as JSON on the last line")
    args = parser.parse_args()
    if not get_settings().demo_mode:
        print("Refusing to run: this is fictional demonstration data and the app is not in demo mode.")
        return 2
    run_migrations()
    with make_session() as db:
        officer = db.scalar(select(Account).where(Account.officer_id == OFFICER))
        if officer is None:
            print(f"Officer {OFFICER} does not exist. Run scripts/seed_workflow.py --staff-only first.")
            return 2
        profile = db.get(OfficerProfile, officer.id)
        area = (profile.areas[0] if profile and profile.areas else "hyderabad-central")
        ids: dict[str, str] = {}
        for key, (title, story, payments) in SET.items():
            full = title + (f" ({args.tag})" if args.tag else "")
            existing = db.scalar(select(Case.id).where(Case.title == full, Case.assigned_officer_id == officer.id))
            if existing is not None:
                ids[key] = existing
                continue
            case = make_case(db, title=full, area=area, officer=officer.id, story=story, payments=payments, days_ago=3)
            ids[key] = case.id
            if profile is not None:
                profile.active_count += 1
                profile.capacity = max(profile.capacity, profile.active_count + 2)
        db.commit()
    print(f"Fictional verification reports ready for {OFFICER}: " + ", ".join(SET[k][0] for k in ids))
    if args.json:
        print(json.dumps(ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
