"""Seed the fictional department for the three-role demo: one admin, three officers and two earlier reports.

Everything here is fictional. The earlier reports are copies of Demo A sent for review by made-up citizens, so that a new
Demo A report has something real to be related to (they share a recipient UPI ID). One is in an area the demo officers
do not cover, which shows that a related report outside an officer's areas is not opened to them.

Usage (repo root, or inside the container):
    python scripts/seed_workflow.py            # staff, then the earlier reports once
    python scripts/seed_workflow.py --staff-only --reset-load   # browser tests: staff only, with room for every test case
Run it with AI_MODE=fixture: the earlier reports are the curated demo, and they should not spend live model calls.
Passwords: CB_ADMIN_PASSWORD / CB_OFFICER_PASSWORD, else the demo ones below (a local demo, never a real credential).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core import make_session  # noqa: E402
from app.main import create_app, run_migrations  # noqa: E402
from app.models import Account, Case, OfficerProfile  # noqa: E402
from app.security import hash_secret  # noqa: E402

ADMIN_PASSWORD = os.environ.get("CB_ADMIN_PASSWORD", "admin-demo-pass")
OFFICER_PASSWORD = os.environ.get("CB_OFFICER_PASSWORD", "officer-demo-pass")
CENTRAL, EAST = "hyderabad-central", "hyderabad-east"
STAFF = [
    ("admin", "ADM001", "Admin Kavita", ADMIN_PASSWORD, None),
    ("officer", "CBI1042", "Officer Arjun", OFFICER_PASSWORD, dict(areas=[CENTRAL], languages=["en"], capacity=3, on_duty=True, capabilities=["area_discovery"])),
    ("officer", "CBI2001", "Officer Meera", OFFICER_PASSWORD, dict(areas=[CENTRAL], languages=["hi", "en"], capacity=3, on_duty=True, capabilities=["area_discovery"])),
    ("officer", "CBI3001", "Officer Divya", OFFICER_PASSWORD, dict(areas=[EAST], languages=["hi"], capacity=2, on_duty=False)),
]


def seed_staff(*, reset_load: bool = False) -> dict[str, str]:
    ids: dict[str, str] = {}
    with make_session() as db:
        for kind, staff_id, name, password, profile in STAFF:
            account = db.scalar(select(Account).where(Account.officer_id == staff_id))
            if account is None:
                account = Account(kind=kind, officer_id=staff_id, display_name=name, password_hash=hash_secret(password), mobile=None)
                db.add(account)
                db.flush()
            else:
                account.kind, account.display_name, account.password_hash, account.disabled = kind, name, hash_secret(password), False
            if profile:
                existing = db.get(OfficerProfile, account.id)
                if existing is None:
                    existing = OfficerProfile(account_id=account.id, active_count=0, **profile)
                    db.add(existing)
                else:
                    for key, value in profile.items():
                        setattr(existing, key, value)
                if reset_load:   # a new profile gets the test capacity too, not only one that already existed
                    existing.capacity, existing.active_count, existing.on_duty = 500, 0, True
            ids[staff_id] = account.id
        db.commit()
    return ids


def _csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("cb_csrf", "")}


def seed_earlier_report(app, mobile: str, area: str, officer_staff_id: str, *, with_bank_reply: bool) -> None:
    with TestClient(app) as citizen:
        citizen.post("/api/session")
        code = citizen.post("/api/auth/otp/request", headers=_csrf(citizen), json={"mobile": mobile}).json()["demoCode"]
        citizen.post("/api/auth/otp/verify", headers=_csrf(citizen), json={"mobile": mobile, "code": code}).raise_for_status()
        case_id = citizen.post("/api/demo-cases/demo-a", headers=_csrf(citizen)).json()["case"]["id"]
        for _ in range(120):   # the demo's receipts are read in the background; the report is sent once they have been
            snapshot = citizen.get(f"/api/cases/{case_id}").json()
            if snapshot["case"]["activeRunId"] is None and snapshot["payments"]:
                break
            time.sleep(1)
        else:
            raise SystemExit("The demo receipts were not read in time; nothing further was seeded.")
        citizen.post(f"/api/cases/{case_id}/area", headers=_csrf(citizen), json={"area": area}).raise_for_status()
        citizen.post(f"/api/cases/{case_id}/submit-review", headers=_csrf(citizen)).raise_for_status()
    with make_session() as db:   # an approved assignment, written directly so seeding needs no admin session
        officer = db.scalar(select(Account).where(Account.officer_id == officer_staff_id))
        case = db.get(Case, case_id)
        case.assigned_officer_id, case.workflow_state = officer.id, "assigned"
        db.get(OfficerProfile, officer.id).active_count += 1
        db.commit()
    if with_bank_reply:
        with TestClient(app) as officer_client:
            officer_client.post("/api/session")
            officer_client.post("/api/auth/officer/login", headers=_csrf(officer_client), json={"officerId": officer_staff_id, "password": OFFICER_PASSWORD}).raise_for_status()
            payments = [p["id"] for p in officer_client.get(f"/api/officer/cases/{case_id}").json()["payments"] if not p["mergedIntoPaymentId"]][:2]
            draft = officer_client.post(f"/api/officer/cases/{case_id}/bank-requests/draft", headers=_csrf(officer_client), json={"paymentIds": payments}).json()
            officer_client.post(f"/api/officer/cases/{case_id}/proposals/{draft['id']}/approve", headers=_csrf(officer_client), json={"digest": draft["digest"]}).raise_for_status()
            file = officer_client.get(f"/api/officer/cases/{case_id}/file").json()
            request = next(m for m in file["messages"] if m["channel"] == "bank" and m["direction"] == "outgoing")
            officer_client.post(f"/api/officer/cases/{case_id}/bank-requests/{request['id']}/simulate-reply", headers=_csrf(officer_client)).raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the fictional department.")
    parser.add_argument("--staff-only", action="store_true", help="Create the staff and stop; no earlier reports")
    parser.add_argument("--reset-load", action="store_true", help="Give every officer room and clear their workload (browser tests)")
    args = parser.parse_args()
    run_migrations()
    seed_staff(reset_load=args.reset_load)
    if args.staff_only:
        print("Staff are in place.")
        return 0
    with make_session() as db:
        done = db.scalar(select(Account).where(Account.mobile == "9000000002")) is not None
    if done:
        print("Staff are in place. The earlier reports were seeded before.")
        return 0
    app = create_app()
    seed_earlier_report(app, "9000000001", CENTRAL, "CBI2001", with_bank_reply=True)
    seed_earlier_report(app, "9000000002", EAST, "CBI3001", with_bank_reply=False)
    print("Seeded: admin ADM001; officers CBI1042, CBI2001, CBI3001; two earlier fictional reports.")
    print("Admin password:", "from CB_ADMIN_PASSWORD" if "CB_ADMIN_PASSWORD" in os.environ else "admin-demo-pass (local demo)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
