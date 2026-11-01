"""Create or update a demo officer account.

Officers cannot register themselves; this script is the only way an officer account exists.
The password is read from CB_OFFICER_PASSWORD, or generated and printed once. Only the hash is
stored, so a generated password cannot be recovered afterwards.

Usage (repo root):
    uv run --project backend python scripts/seed_officer.py --officer-id CBI1042 --name "Insp. R. Sharma"
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import os  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.core import make_session  # noqa: E402
from app.main import run_migrations  # noqa: E402
from app.models import Account  # noqa: E402
from app.security import hash_secret  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a demo officer account.")
    parser.add_argument("--officer-id", required=True)
    parser.add_argument("--name", default=None, help="Display name shown in the UI")
    parser.add_argument("--reset-password", action="store_true", help="Replace the password of an existing officer")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()

    run_migrations()
    officer_id = args.officer_id.strip()

    with make_session() as db:
        account = db.scalar(select(Account).where(Account.officer_id == officer_id, Account.kind == "officer"))

        if args.disable or args.enable:
            if account is None:
                print(f"No officer account with id {officer_id!r}.")
                return 1
            account.disabled = bool(args.disable)
            db.commit()
            print(f"Officer {officer_id} is now {'disabled' if account.disabled else 'enabled'}.")
            return 0

        if account is not None and not args.reset_password:
            print(f"Officer {officer_id} already exists. Use --reset-password to set a new password.")
            return 0

        supplied = os.environ.get("CB_OFFICER_PASSWORD", "").strip()
        password = supplied or secrets.token_urlsafe(12)

        if account is None:
            account = Account(kind="officer", officer_id=officer_id, display_name=args.name,
                              password_hash=hash_secret(password), mobile=None)
            db.add(account)
            action = "created"
        else:
            account.password_hash = hash_secret(password)
            if args.name:
                account.display_name = args.name
            action = "password reset for"
        db.commit()

    print(f"Officer account {action} {officer_id}.")
    if supplied:
        print("Password taken from CB_OFFICER_PASSWORD.")
    else:
        print(f"Generated password: {password}")
        print("Store it now. Only a hash is kept, so this cannot be shown again.")
    print("This is a simulated reviewer account for the demo, not a police credential.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
