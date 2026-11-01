"""Sessions and CSRF, idempotency keys, password hashing, account lookup and case access."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .core import ApiError, Settings, get_db, get_settings
from .models import Account, Case, IdempotencyKey, UserSession, utcnow

# ---- from security/passwords.py ------------------------------------------------------------------
# Salted scrypt hashing for officer passwords and one-time codes.
#
# Stdlib only, by design: the project takes no new dependency for this.
#
# Stored format, one `$`-delimited string::
#
#     scrypt$16384$8$1$<b64 salt>$<b64 derived key>
#
# Verification re-derives with the parameters *parsed from the stored string*, so raising the
# cost later still verifies older hashes.

SCHEME = "scrypt"
N = 2**14  # 16384
R = 8
P = 1
DKLEN = 32
SALT_BYTES = 16
# n=2**14, r=8 needs 128 * r * n = 16 MiB. hashlib's default maxmem=0 maps to a 32 MiB cap in
# OpenSSL, so pass it explicitly rather than relying on that default staying generous enough.
MAXMEM = 64 * 1024 * 1024


def _derive(secret: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=MAXMEM)


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = _derive(secret, salt, n=N, r=R, p=P, dklen=DKLEN)
    return "$".join(
        [SCHEME, str(N), str(R), str(P), base64.b64encode(salt).decode(), base64.b64encode(dk).decode()]
    )


def verify_secret(secret: str, stored: str) -> bool:
    """Constant-time check. Returns False for a malformed stored string; never raises."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = stored.split("$")
        if scheme != SCHEME:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        candidate = _derive(secret, salt, n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(expected))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(candidate, expected)


# Verified against when the mobile or officer id is unknown, so an unknown identifier costs the
# same time as a wrong secret and cannot be distinguished by timing.
DUMMY_HASH = hash_secret(secrets.token_urlsafe(32))


# ---- from security/idempotency.py ----------------------------------------------------------------
# Idempotency keys for uploads, run creation, refunds, allocations and exports.
#
# Same key + same payload -> the stored response is returned. Same key + different payload ->
# 409 `idempotency_conflict`. The key row is committed in the same transaction as the business
# write, so a crash cannot leave a half-applied, replayable request.

IDEMPOTENCY_HEADER = "idempotency-key"


def require_idempotency_key(request: Request) -> str:
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key or len(key) > 128:
        raise ApiError(
            "invalid_request",
            "An Idempotency-Key header (1-128 characters) is required for this operation.",
            400,
            "Generate a random key per attempt and reuse it only when retrying the same request.",
        )
    return key


def fingerprint(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(b"\x00bytes:")
            digest.update(part)
        else:
            digest.update(b"\x00json:")
            digest.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def run_idempotent(
    db: Session,
    *,
    session_id: str,
    scope: str,
    key: str,
    payload_hash: str,
    produce: Callable[[], tuple[int, dict]],
) -> tuple[int, dict]:
    """Execute `produce` once per (session, scope, key). `produce` must not commit."""
    existing = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.session_id == session_id,
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ApiError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request.",
                409,
                "Use a new key for a new request.",
            )
        return existing.response_status, existing.response_body

    status, body = produce()
    db.add(
        IdempotencyKey(
            session_id=session_id, scope=scope, key=key, payload_hash=payload_hash,
            response_status=status, response_body=body,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # A concurrent identical request won the race; return its stored result.
        db.rollback()
        stored = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.session_id == session_id,
                IdempotencyKey.scope == scope,
                IdempotencyKey.key == key,
            )
        )
        if stored is None:
            raise
        return stored.response_status, stored.response_body
    return status, body


# ---- from security/accounts.py -------------------------------------------------------------------
# Account helpers: mobile normalisation, session rotation on sign-in, and case detaching.
#
# `detach_account_cases` is the invariant that keeps a signed-in citizen's complaints alive.
# A case can be destroyed three ways — the explicit loop in retention, the ORM relationship
# cascade, and the database's ON DELETE CASCADE — so an account-owned case must stop pointing
# at a session *before* that session row is deleted, anywhere it is deleted.

_DIGITS = re.compile(r"\D+")


def normalise_mobile(raw: str) -> str:
    """10-digit Indian mobile, or the same number with a 91 prefix, stored as 91XXXXXXXXXX."""
    digits = _DIGITS.sub("", raw or "")
    if len(digits) == 10:
        digits = f"91{digits}"
    if len(digits) != 12 or not digits.startswith("91") or digits[2] not in "6789":
        raise ApiError(
            "invalid_request",
            "Enter a 10-digit Indian mobile number.",
            400,
            "It should start with 6, 7, 8 or 9.",
        )
    return digits


def mask_mobile(mobile: str | None) -> str | None:
    """`919876543210` -> `••••• 43210`. Never show a full number back to the browser."""
    if not mobile:
        return None
    return f"••••• {mobile[-5:]}"


def detach_account_cases(db: Session, session_id: str) -> None:
    """Null out `session_id` on account-owned cases so deleting the session cannot take them."""
    db.execute(
        update(Case)
        .where(Case.session_id == session_id, Case.account_id.is_not(None))
        .values(session_id=None)
    )


def rotate_session(
    db: Session,
    settings: Settings,
    old: UserSession,
    *,
    account: Account,
    role: str,
    adopt_cases: bool,
) -> UserSession:
    """Issue a fresh session bound to `account` and retire the old one.

    Rotating defeats session fixation: the id that was in the browser before signing in is
    deleted, so a pre-planted cookie carries no authority afterwards.

    `adopt_cases` moves work started anonymously in this browser onto the account, so someone
    who begins as a guest and then signs in does not lose it.
    """
    fresh = new_session(db, settings, label=old.label, role=role)
    fresh.account_id = account.id
    db.flush()

    if adopt_cases:
        db.execute(
            update(Case)
            .where(Case.session_id == old.id, Case.account_id.is_(None))
            .values(account_id=account.id, session_id=fresh.id)
        )
    # anything still owned by an account must not follow the old session into the grave
    detach_account_cases(db, old.id)
    db.delete(old)
    account.last_login_at = utcnow()
    return fresh


# ---- from security/session.py --------------------------------------------------------------------
# Opaque cookie sessions, CSRF double-submit token, and case ownership checks.
#
# A session proves possession of a cookie. Identity, when there is any, comes from the account
# the session is bound to; an unbound session is the anonymous demo path and still works.
#
# The officer role is a real authorisation boundary here: officers are refused by `require_case`
# outright and read submitted cases through `require_submitted_case`. It remains a *simulated*
# reviewer for this demo — not police authority, and connected to no government system.

SESSION_COOKIE = "cb_session"
CSRF_COOKIE = "cb_csrf"
CSRF_HEADER = "x-csrf-token"


def new_session(db: Session, settings: Settings, *, label: str | None = None, role: str = "citizen") -> UserSession:
    session = UserSession(
        label=label,
        role=role,
        demo_mode=settings.demo_mode,
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    db.flush()
    return session


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookies(response: Response, session: UserSession, csrf_token: str, settings: Settings) -> None:
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        SESSION_COOKIE, session.id, max_age=max_age, httponly=True, samesite="lax",
        secure=settings.cookie_secure, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf_token, max_age=max_age, httponly=False, samesite="lax",
        secure=settings.cookie_secure, path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def current_session(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserSession:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise ApiError("unauthenticated", "No session.", 401, "Start a demo session first.")
    session = db.get(UserSession, session_id)
    if session is None or session.expires_at <= utcnow():
        raise ApiError("unauthenticated", "The session is missing or expired.", 401, "Start a new demo session.")
    return session


def require_csrf(request: Request) -> None:
    """Double-submit check for cookie-authenticated mutations."""
    if not request.cookies.get(SESSION_COOKIE):
        raise ApiError("unauthenticated", "No session.", 401, "Start a demo session first.")
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise ApiError("forbidden", "CSRF token missing or invalid.", 403, "Reload the page and retry.")


#: review states a submitted case can be in; anything else is private to the citizen.
#: "closed" stays visible so an officer can still open what they agreed to close.
OFFICER_VISIBLE = ("submitted", "returned", "reviewed", "closed")


def require_case(
    case_id: str,
    session: Annotated[UserSession, Depends(current_session)],
    db: Annotated[Session, Depends(get_db)],
) -> Case:
    """Citizen and anonymous ownership. Unknown and foreign cases look identical (404).

    Officers are refused here deliberately, so none of the citizen mutation routes that depend
    on this can be reached with an officer session. They use `require_submitted_case` instead.
    """
    if session.role in ("officer", "admin"):
        raise ApiError(
            "forbidden", "Staff accounts work from their own desk.", 403,
            "Open the complaint from the officer desk.",
        )
    stmt = select(Case).where(Case.id == case_id)
    if session.account_id:  # signed-in citizen: ownership follows the account
        stmt = stmt.where(Case.account_id == session.account_id)
    else:  # anonymous demo session: ownership follows the cookie, as before
        stmt = stmt.where(Case.session_id == session.id, Case.account_id.is_(None))
    case = db.scalar(stmt)
    if case is None:
        raise ApiError("not_found", "Case not found.", 404)
    return case


def current_officer(
    session: Annotated[UserSession, Depends(current_session)],
) -> UserSession:
    if session.role != "officer" or not session.account_id:
        raise ApiError("forbidden", "This is an officer-only view.", 403, "Sign in with your officer ID.")
    return session


def current_admin(
    session: Annotated[UserSession, Depends(current_session)],
) -> UserSession:
    if session.role != "admin" or not session.account_id:
        raise ApiError("forbidden", "This is an admin-only view.", 403, "Sign in with your admin ID.")
    return session


def require_submitted_case(
    case_id: str,
    session: Annotated[UserSession, Depends(current_officer)],
    db: Annotated[Session, Depends(get_db)],
) -> Case:
    """Officer access: only a complaint a citizen sent for review AND an admin assigned to this officer.

    A case that exists but was never submitted, or is assigned to someone else, is a 404 identical to one
    that does not exist, so the desk cannot be used to probe for other people's complaints.
    """
    case = db.scalar(
        select(Case).where(Case.id == case_id, Case.review_state.in_(OFFICER_VISIBLE),
                           Case.assigned_officer_id == session.account_id)
    )
    if case is None:
        raise ApiError("not_found", "Case not found.", 404)
    return case


CurrentSession = Annotated[UserSession, Depends(current_session)]
CurrentOfficer = Annotated[UserSession, Depends(current_officer)]
CurrentAdmin = Annotated[UserSession, Depends(current_admin)]
OwnedCase = Annotated[Case, Depends(require_case)]
OfficerCase = Annotated[Case, Depends(require_submitted_case)]
Db = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]
