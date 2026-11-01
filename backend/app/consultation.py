"""Officer-to-officer consultation: one officer asks the officer who holds an earlier, related report what was verified and which action helped.

The rules this module keeps:
- Nothing is sent by preparing a draft. A tool or a route may write a draft; only the sender's explicit approval, naming the digest of the exact
  wording they reviewed, sends it. Editing the wording changes the digest, so an earlier approval cannot be used. A double click or a retried
  request finds the consultation already sent and does not send a second one (the state change is one conditional UPDATE).
- The recipient is whoever holds the earlier report *at the moment of approval*, resolved here on the server and stored. If the report has moved
  to someone else, was changed, is no longer open, or the sender has lost the right to search it, the draft is out of date and must be
  refreshed and read again before it can be sent.
- The recipient reads only what was approved (the question, the report reference, the shared identifier) and the replies. Not the sender's case file,
  its evidence, its citizen or its amounts. A person who is neither the sender nor the recipient gets "not found".
- The record is append-only: the approved request, each reply and each state change are rows that are never edited or removed.
- A reply is an officer's account. It is stored with who wrote it and quoted with attribution; it is not treated as established fact.
- Administrators see no consultation content here: this first version has no oversight reader, and says so.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .assistant import case_ref
from .core import ApiError
from .intelligence import VISIBLE, _insert_once, resolve_candidate, search_related_cases, source_digest
from .models import Account, AuditEvent, Case, Consultation, ConsultationMessage, OfficerProfile, UserSession, utcnow
from .schemas import ApiModel
from .security import CurrentOfficer, Db, OfficerCase, require_csrf
from .workflow import area_name, digest_of, staff_name

log = logging.getLogger("cyberbrief.consultation")

LIVE = ("draft", "sent", "replied")
HELPED = {
    "ask_citizen_for_receipt": "Asking the citizen for a clearer copy of the receipt",
    "bank_request": "A verification request to the (simulated) bank",
    "compare_reports": "Comparing the two reports' evidence",
    "nothing_helped": "Nothing I recorded helped",
    "other": "Something else (see my note)",
}
QUESTION_LIMIT = 700


# ---- who may be asked, and whether a draft still holds ---------------------------------------------------------------------------

def resolve_recipient(db: Session, earlier: Case, sender_id: str) -> tuple[Account | None, str | None]:
    """(the officer who holds the earlier report now, or why there is nobody to ask). Decided on the current record, never on what an earlier draft said."""
    if earlier.review_state not in VISIBLE:
        return None, "That report is no longer open to a consultation."
    if not earlier.assigned_officer_id:
        return None, "No officer holds that report now."
    if earlier.assigned_officer_id == sender_id:
        return None, "That report is yours, so there is nobody else to ask."
    account = db.get(Account, earlier.assigned_officer_id)
    if account is None or account.kind != "officer" or account.disabled or db.get(OfficerProfile, account.id) is None:
        return None, "The officer who held that report is not available."
    return account, None


def _basis(db: Session, case: Case, other: Case) -> dict[str, Any]:
    return {"caseRevision": case.current_revision, "earlierRevision": other.current_revision, "mine": source_digest(db, case), "theirs": source_digest(db, other)}


def holds_case(db: Session, case: Case | None, account_id: str | None) -> bool:
    """Does this officer hold this report *now*? Being the one who drafted a request is history, not authority: every act for the sender's case needs this."""
    return bool(case is not None and account_id and case.assigned_officer_id == account_id and case.review_state in VISIBLE)


def require_authority(db: Session, c: Consultation) -> Case:
    """The domain-level guard for every change on the sender's side (edit, refresh, approve, withdraw). Checked here so no route, tool or worker can skip it."""
    case = db.get(Case, c.case_id)
    if not holds_case(db, case, c.sender_account_id):
        raise ApiError("forbidden", "You no longer hold the report this consultation is for, so you cannot act on it.", 403, "The officer who holds it now can start their own question.")
    return case


def staleness(db: Session, c: Consultation, case: Case | None = None) -> list[str]:
    """Why a draft can no longer be sent as written. Empty means it still holds. Read-only."""
    case = case or db.get(Case, c.case_id)
    other = db.get(Case, c.earlier_case_id)
    reasons: list[str] = []
    if case is None or other is None:
        return ["A report this draft is about is no longer available."]
    if not holds_case(db, case, c.sender_account_id):
        return ["You no longer hold the report this draft is for."]
    try:
        if not c.candidate_id:
            raise ApiError("not_found", "No connection.", 404)
        cand, _o, _p = resolve_candidate(db, c.sender_account_id, case, c.candidate_id)
    except ApiError:
        reasons.append("You no longer have access to the earlier report.")
        return reasons
    if cand.state == "dismissed":
        reasons.append("You marked the connection to that report as not related.")
    recipient, why = resolve_recipient(db, other, c.sender_account_id)
    if recipient is None:
        reasons.append(why or "There is nobody to ask.")
    elif recipient.id != (c.payload or {}).get("recipientAccountId"):
        reasons.append("A different officer holds that report now.")
    basis = c.source_revisions or {}
    if basis.get("mine") != source_digest(db, case):
        reasons.append("This report changed after the draft was written.")
    if basis.get("theirs") != source_digest(db, other):
        reasons.append("The earlier report changed after the draft was written.")
    return reasons


# ---- the draft ---------------------------------------------------------------------------------------------------------------------

def _default_question(case: Case, other: Case, kind: str, value: str) -> str:
    return (f"I am looking at report {case_ref(case.id)}, which names the same {kind} ({value}) as your report {case_ref(other.id)}. "
            "What did you verify on your report, and which action helped most?")


def _payload(case: Case, other: Case, signals: list[dict[str, Any]], recipient: Account, question: str | None, attempt: int = 0) -> dict[str, Any]:
    sig = next((s for s in signals if s.get("strength") == "strong"), signals[0] if signals else {})
    kind = "transaction reference" if sig.get("kind") == "same_reference" else "recipient UPI ID"
    value = str(sig.get("value") or "")
    text = (question or "").strip()[:QUESTION_LIMIT] or _default_question(case, other, kind, value)
    payload: dict[str, Any] = {"question": text, "senderCaseRef": case_ref(case.id), "earlierCaseRef": case_ref(other.id), "sharedKind": kind, "sharedIdentifier": value,
                               "recipientAccountId": recipient.id, "recipientName": recipient.display_name or recipient.officer_id,
                               "shares": ["Your report's reference", "The identifier both reports name", "The question above"],
                               "keeps_back": ["The citizen's name, story and contact details", "Amounts, dates and uploaded files", "Anything else in the sender's case"]}
    if attempt:
        payload["_attempt"] = attempt
    return payload


def draft_consultation(db: Session, viewer_id: str, case: Case, handle: str, *, run_id: str | None = None) -> tuple[Consultation, bool]:
    """(the consultation, whether this call created it). One live consultation per sender, case and earlier report: asking again returns it."""
    cand, other, _op = resolve_candidate(db, viewer_id, case, handle)
    if cand.state == "dismissed" or cand.result_kind not in ("exact_identifier", "possible_duplicate"):
        raise ApiError("invalid_request", "A consultation needs an exact identifier match that has not been dismissed.", 409)
    recipient, why = resolve_recipient(db, other, viewer_id)
    if recipient is None:
        raise ApiError("review_conflict", why or "There is nobody to ask.", 409, "Open the earlier report's connection and decide how to proceed yourself.")
    live = db.scalar(select(Consultation).where(Consultation.case_id == case.id, Consultation.earlier_case_id == other.id, Consultation.sender_account_id == viewer_id,
                                                Consultation.state.in_(LIVE)).order_by(Consultation.created_at.desc()))
    if live is not None:
        return live, False
    for attempt in range(20):
        payload = _payload(case, other, cand.signals or [], recipient, None, attempt)
        digest = digest_of("consultation", payload)
        row = Consultation(case_id=case.id, earlier_case_id=other.id, sender_account_id=viewer_id, candidate_id=cand.id, run_id=run_id, state="draft", payload=payload,
                           payload_digest=digest, source_revisions=_basis(db, case, other))
        made = _insert_once(db, row, select(Consultation).where(Consultation.case_id == case.id, Consultation.earlier_case_id == other.id,
                                                                 Consultation.sender_account_id == viewer_id, Consultation.payload_digest == digest))
        if made is row:
            return row, True
        if made.state in LIVE:
            return made, False
    raise ApiError("review_conflict", "Too many earlier attempts to ask about this report.", 409)


def edit_draft(db: Session, c: Consultation, question: str) -> Consultation:
    require_authority(db, c)
    if c.state != "draft":
        raise ApiError("review_conflict", f"This consultation is {c.state} and can no longer be edited.", 409)
    text = " ".join(question.split())
    if len(text) < 10:
        raise ApiError("invalid_request", "Write a question of at least a sentence.", 400)
    payload = {**c.payload, "question": text[:QUESTION_LIMIT]}
    c.payload, c.payload_digest = payload, digest_of("consultation", payload)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError("review_conflict", "An identical draft already exists.", 409) from exc
    return c


def refresh_draft(db: Session, c: Consultation, case: Case) -> Consultation:
    """Rebuild a draft from the current records, keeping the officer's wording. The digest changes, so it must be read and approved again."""
    case = require_authority(db, c)
    if c.state != "draft":
        raise ApiError("review_conflict", f"This consultation is {c.state}.", 409)
    cand, other, _ = resolve_candidate(db, c.sender_account_id, case, c.candidate_id or "")
    search_related_cases(db, c.sender_account_id, case)   # bring the connection up to date: a corrected report may no longer share anything
    if cand.state == "dismissed" or cand.result_kind not in ("exact_identifier", "possible_duplicate"):
        raise ApiError("review_conflict", "The two reports no longer share an identifier, so there is nothing to ask about.", 409, "Search again and decide how to proceed yourself.")
    recipient, why = resolve_recipient(db, other, c.sender_account_id)
    if recipient is None:
        raise ApiError("review_conflict", why or "There is nobody to ask.", 409)
    payload = _payload(case, other, cand.signals or [], recipient, c.payload.get("question"))
    c.payload, c.payload_digest, c.source_revisions = payload, digest_of("consultation", payload), _basis(db, case, other)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError("review_conflict", "An identical draft already exists.", 409) from exc
    return c


# ---- the append-only log -----------------------------------------------------------------------------------------------------------

def _append(db: Session, c: Consultation, kind: str, body: str, *, author: str | None, data: dict[str, Any] | None = None, key: str | None = None) -> ConsultationMessage:
    for _ in range(5):
        top = db.scalar(select(func.max(ConsultationMessage.seq)).where(ConsultationMessage.consultation_id == c.id)) or 0
        row = ConsultationMessage(consultation_id=c.id, seq=top + 1, kind=kind, author_account_id=author, body=body, data=data or {}, idempotency_key=key)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            return row
        except IntegrityError:
            if key:
                found = db.scalar(select(ConsultationMessage).where(ConsultationMessage.consultation_id == c.id, ConsultationMessage.idempotency_key == key))
                if found is not None:
                    return found
    raise ApiError("review_conflict", "Could not record this just now.", 409, "Try again.")


def _audit(db: Session, case_id: str, session: UserSession, kind: str, c: Consultation, **detail: Any) -> None:
    case = db.get(Case, case_id)
    if case is not None:
        db.add(AuditEvent(case_id=case.id, revision=case.current_revision, kind=kind, subject_type="consultation", subject_id=c.id, actor_session_id=session.id,
                          actor_role="officer", detail={"digest": c.approved_digest or c.payload_digest, **detail}))


def approve(db: Session, c: Consultation, session: UserSession, digest: str) -> Consultation:
    """The one way a consultation is sent. Idempotent: a retry of an approval that already went through returns it as it is."""
    if c.state in ("sent", "replied", "closed") and c.approved_digest == digest:
        return c
    require_authority(db, c)
    if c.state != "draft":
        raise ApiError("review_conflict", f"This consultation is {c.state}.", 409)
    if digest != c.payload_digest:
        raise ApiError("review_conflict", "The draft changed after you reviewed it.", 409, "Read the current wording and approve it again.")
    case = db.get(Case, c.case_id)
    reasons = staleness(db, c, case)
    if reasons:
        raise ApiError("review_conflict", "This draft is out of date: " + " ".join(reasons), 409, "Refresh the draft, read it again, then approve it.")
    other = db.get(Case, c.earlier_case_id)
    recipient, _why = resolve_recipient(db, other, c.sender_account_id)
    assert recipient is not None   # staleness() has just refused every case in which there is none
    now = utcnow()
    holds = select(Case.id).where(Case.id == c.case_id, Case.assigned_officer_id == c.sender_account_id, Case.review_state.in_(VISIBLE))   # re-tested inside the one UPDATE: an ownership change before commit cannot slip through
    won = db.execute(update(Consultation).where(Consultation.id == c.id, Consultation.state == "draft", Consultation.payload_digest == digest, Consultation.case_id.in_(holds))
                     .values(state="sent", approved_digest=digest, recipient_account_id=recipient.id, sent_at=now, sender_seen_at=now)).rowcount
    if won == 0:   # another click or retry got there first
        db.refresh(c)
        if c.state in ("sent", "replied", "closed") and c.approved_digest == digest:
            return c
        raise ApiError("review_conflict", "This consultation was changed by someone else.", 409, "Reload and check.")
    db.refresh(c)
    _append(db, c, "request", c.payload["question"], author=c.sender_account_id, data={"payloadDigest": digest, "senderCaseRef": c.payload["senderCaseRef"], "earlierCaseRef": c.payload["earlierCaseRef"]})
    _audit(db, c.case_id, session, "consultation_sent", c, recipient=staff_name(db, recipient.id), earlierCaseRef=c.payload["earlierCaseRef"])
    _audit(db, c.earlier_case_id, session, "consultation_received", c, senderCaseRef=c.payload["senderCaseRef"])
    return c


def reply(db: Session, c: Consultation, session: UserSession, verified: str, helped: str, note: str | None, key: str | None) -> ConsultationMessage:
    if c.state not in ("sent", "replied"):
        raise ApiError("review_conflict", f"This consultation is {c.state}, so it cannot be answered.", 409)
    if helped not in HELPED:
        raise ApiError("invalid_request", "Choose what helped from the list.", 400)
    text = " ".join(verified.split())
    body = f"What I verified: {text}\nWhat helped: {HELPED[helped]}" + (f"\nNote: {' '.join(note.split())}" if note and note.strip() else "")
    msg = _append(db, c, "reply", body, author=session.account_id, data={"verified": text, "helpedAction": helped, "note": (note or "").strip() or None}, key=key)
    db.execute(update(Consultation).where(Consultation.id == c.id, Consultation.state.in_(("sent", "replied"))).values(state="replied", recipient_seen_at=utcnow()))
    db.refresh(c)
    _audit(db, c.earlier_case_id, session, "consultation_replied", c, messageId=msg.id)
    _audit(db, c.case_id, session, "consultation_reply_received", c, messageId=msg.id)
    return msg


def close(db: Session, c: Consultation, session: UserSession) -> Consultation:
    if c.state == "closed":
        return c
    if c.state not in ("sent", "replied"):
        raise ApiError("review_conflict", f"This consultation is {c.state}.", 409)
    db.execute(update(Consultation).where(Consultation.id == c.id, Consultation.state.in_(("sent", "replied"))).values(state="closed", closed_at=utcnow()))
    db.refresh(c)
    _append(db, c, "system", "The consultation was closed.", author=session.account_id, data={"event": "closed"})
    _audit(db, c.case_id, session, "consultation_closed", c)
    return c


def cancel(db: Session, c: Consultation, session: UserSession) -> Consultation:
    if c.state == "cancelled":
        return c
    require_authority(db, c)
    replied = db.scalar(select(func.count()).select_from(ConsultationMessage).where(ConsultationMessage.consultation_id == c.id, ConsultationMessage.kind == "reply")) or 0
    if c.state not in ("draft", "sent") or replied:
        raise ApiError("review_conflict", "It has been answered, so it can only be closed.", 409)
    was = c.state
    db.execute(update(Consultation).where(Consultation.id == c.id, Consultation.state.in_(("draft", "sent"))).values(state="cancelled", closed_at=utcnow()))
    db.refresh(c)
    if was == "sent":
        _append(db, c, "system", "The sender withdrew this request.", author=session.account_id, data={"event": "cancelled"})
        _audit(db, c.case_id, session, "consultation_cancelled", c)
    return c


# ---- what a reader sees ------------------------------------------------------------------------------------------------------------

def role_of(c: Consultation, viewer_id: str) -> str | None:
    if viewer_id == c.sender_account_id:
        return "sender"
    if c.recipient_account_id and viewer_id == c.recipient_account_id and c.state != "draft":
        return "recipient"
    return None


def _messages(db: Session, c: Consultation) -> list[ConsultationMessage]:
    return list(db.scalars(select(ConsultationMessage).where(ConsultationMessage.consultation_id == c.id).order_by(ConsultationMessage.seq)).all())


def unread_for(c: Consultation, role: str, msgs: list[ConsultationMessage]) -> bool:
    if role == "recipient":
        return c.state == "sent" and c.recipient_seen_at is None
    last_reply = max((m.created_at for m in msgs if m.kind == "reply"), default=None)
    return bool(last_reply and (c.sender_seen_at is None or last_reply > c.sender_seen_at))


def consultation_out(db: Session, c: Consultation, viewer_id: str, *, detail: bool = True) -> dict[str, Any]:
    role = role_of(c, viewer_id)
    if role is None:
        raise ApiError("not_found", "No such consultation.", 404)
    msgs = _messages(db, c)
    case, other = db.get(Case, c.case_id), db.get(Case, c.earlier_case_id)
    reasons = staleness(db, c, case) if c.state == "draft" and role == "sender" else []
    lost_access = any("no longer have access" in r for r in reasons)
    replies = [m for m in msgs if m.kind == "reply"]
    pending_your_reply = role == "recipient" and c.state == "sent"
    out: dict[str, Any] = {
        "id": c.id, "role": role, "state": c.state, "caseId": c.case_id if role == "sender" else None, "createdAt": c.created_at.isoformat(), "sentAt": c.sent_at.isoformat() if c.sent_at else None,
        "unread": unread_for(c, role, msgs), "awaitingYourReply": pending_your_reply, "replyCount": len(replies),
        "lastActivityAt": (msgs[-1].created_at if msgs else c.created_at).isoformat(),
        "otherParty": ({"name": (c.payload or {}).get("recipientName") if not lost_access else None} if role == "sender" else {"name": staff_name(db, c.sender_account_id)}),
        "aboutCaseRef": (c.payload or {}).get("earlierCaseRef") if role == "sender" and not lost_access else (c.payload or {}).get("senderCaseRef") if role == "recipient" else None,
        "yourCaseRef": case_ref(c.case_id) if role == "sender" else case_ref(c.earlier_case_id),
    }
    if not detail:
        return out
    shown = {k: v for k, v in (c.payload or {}).items() if k not in ("recipientAccountId", "_attempt")}
    if lost_access:   # a draft about a report the sender can no longer search shows nothing derived from it
        shown = {"question": None, "withheld": "You no longer have access to the report this draft was about."}
    mine = case if role == "sender" else other
    readable = holds_case(db, mine, viewer_id)   # the thread is kept for its two participants; the live report behind it is shown only to whoever still holds it
    own_ref = (c.payload or {}).get("senderCaseRef" if role == "sender" else "earlierCaseRef")
    out.update({
        "payload": shown, "payloadDigest": c.payload_digest, "approvedDigest": c.approved_digest, "stale": reasons, "needsRefresh": bool(reasons),
        "sourceRevisions": c.source_revisions if role == "sender" else None,
        "yourReport": ({"caseRef": case_ref(mine.id), "title": mine.title, "area": area_name(mine.area), "workflowState": mine.workflow_state, "path": f"/officer/cases/{mine.id}", "available": True} if readable
                       else {"caseRef": own_ref, "title": None, "area": None, "workflowState": None, "path": None, "available": False}) if mine else None,
        "messages": [{"id": m.id, "seq": m.seq, "kind": m.kind, "authorName": staff_name(db, m.author_account_id), "authorRole": ("sender" if m.author_account_id == c.sender_account_id else "recipient" if m.author_account_id else "system"),
                      "body": m.body, "data": {k: v for k, v in (m.data or {}).items() if k in ("verified", "helpedAction", "note", "event")}, "at": m.created_at.isoformat()} for m in msgs],
        "actions": {"canEdit": role == "sender" and c.state == "draft", "canApprove": role == "sender" and c.state == "draft" and not reasons, "canRefresh": role == "sender" and c.state == "draft" and bool(reasons) and not lost_access,
                    "canCancel": role == "sender" and (c.state == "draft" or (c.state == "sent" and not replies)), "canReply": role == "recipient" and c.state in ("sent", "replied"),
                    "canClose": c.state in ("sent", "replied"), "canUpdatePlan": role == "sender" and c.state in ("replied", "closed") and bool(replies)},
        "helpedOptions": [{"id": k, "label": v} for k, v in HELPED.items()],
        "attribution": "An officer's account of their own report. It is not independently verified and does not show what happened in this one.",
    })
    return out


def mark_seen(db: Session, c: Consultation, role: str) -> None:
    now = utcnow()
    if role == "recipient" and c.recipient_seen_at is None:
        c.recipient_seen_at = now
    elif role == "sender":
        c.sender_seen_at = now


def _mine(db: Session, viewer_id: str, box: str) -> list[Consultation]:
    q = select(Consultation)
    if box == "inbox":
        q = q.where(Consultation.recipient_account_id == viewer_id, Consultation.state != "draft")
    elif box == "sent":
        q = q.where(Consultation.sender_account_id == viewer_id)
    else:
        q = q.where((Consultation.sender_account_id == viewer_id) | ((Consultation.recipient_account_id == viewer_id) & (Consultation.state != "draft")))
    return list(db.scalars(q.order_by(Consultation.updated_at.desc()).limit(100)).all())


# ---- routes ----------------------------------------------------------------------------------------------------------------------------

consultation_router = APIRouter(prefix="/api/officer", tags=["consultation"])


class DraftBody(ApiModel):
    handle: str = Field(min_length=8, max_length=64)


class EditBody(ApiModel):
    question: str = Field(min_length=10, max_length=2000)


class ApproveBody(ApiModel):
    digest: str = Field(min_length=16, max_length=64)


class ReplyBody(ApiModel):
    verified: str = Field(min_length=3, max_length=600)
    helped_action: str = Field(min_length=3, max_length=32)
    note: str | None = Field(default=None, max_length=800)
    key: str | None = Field(default=None, min_length=8, max_length=64)


def _retry(db: Session, work: Any, tries: int = 4) -> Any:
    """Run a write again from a fresh snapshot if SQLite says another writer got in first. Anything else is raised as it was."""
    for attempt in range(tries):
        try:
            return work()
        except OperationalError as exc:
            db.rollback()
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            time.sleep(0.15 * (attempt + 1))


def _mine_or_404(db: Session, session: UserSession, consultation_id: str) -> Consultation:
    c = db.get(Consultation, consultation_id)
    if c is None or role_of(c, session.account_id or "") is None:
        raise ApiError("not_found", "No such consultation.", 404)   # the same answer for a missing one and one that belongs to others
    return c


@consultation_router.get("/consultations")
def list_consultations(session: CurrentOfficer, db: Db, box: str = Query(default="all", pattern="^(inbox|sent|all)$")) -> dict[str, Any]:
    me = session.account_id or ""
    items = [consultation_out(db, c, me, detail=False) for c in _mine(db, me, box)]
    return {"items": items, "unread": sum(1 for i in items if i["unread"]), "awaitingYourReply": sum(1 for i in items if i["awaitingYourReply"])}


@consultation_router.get("/consultations/summary")
def consultation_summary(session: CurrentOfficer, db: Db) -> dict[str, Any]:
    """What the header badge shows: new things to read and requests still waiting for this officer's answer. Reads, writes nothing."""
    me = session.account_id or ""
    rows = [consultation_out(db, c, me, detail=False) for c in _mine(db, me, "all")]
    first = next((r for r in rows if r["unread"]), None)
    return {"unread": sum(1 for r in rows if r["unread"]), "awaitingYourReply": sum(1 for r in rows if r["awaitingYourReply"]),
            "waiting": sum(1 for r in rows if r["unread"] or r["awaitingYourReply"]), "firstUnreadId": first["id"] if first else None}


@consultation_router.get("/consultations/{consultation_id}")
def get_consultation(session: CurrentOfficer, db: Db, consultation_id: str) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        role = role_of(c, session.account_id or "") or ""
        msgs = _messages(db, c)
        if unread_for(c, role, msgs) or (role == "sender" and c.sender_seen_at is None):
            mark_seen(db, c, role)
            db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, read)


@consultation_router.get("/cases/{case_id}/consultations")
def case_consultations(case: OfficerCase, session: CurrentOfficer, db: Db) -> list[dict[str, Any]]:
    rows = db.scalars(select(Consultation).where(Consultation.case_id == case.id, Consultation.sender_account_id == session.account_id).order_by(Consultation.created_at.desc()).limit(10)).all()
    return [consultation_out(db, c, session.account_id or "") for c in rows]


@consultation_router.post("/cases/{case_id}/consultations/draft", dependencies=[Depends(require_csrf)])
def prepare_draft(case: OfficerCase, session: CurrentOfficer, db: Db, payload: DraftBody) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c, _created = draft_consultation(db, session.account_id or "", case, payload.handle)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.patch("/consultations/{consultation_id}", dependencies=[Depends(require_csrf)])
def edit(session: CurrentOfficer, db: Db, consultation_id: str, payload: EditBody) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        if role_of(c, session.account_id or "") != "sender":
            raise ApiError("forbidden", "Only the sender can change the wording.", 403)
        edit_draft(db, c, payload.question)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.post("/consultations/{consultation_id}/refresh", dependencies=[Depends(require_csrf)])
def refresh(session: CurrentOfficer, db: Db, consultation_id: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        if role_of(c, session.account_id or "") != "sender":
            raise ApiError("forbidden", "Only the sender can refresh a draft.", 403)
        refresh_draft(db, c, db.get(Case, c.case_id))
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.post("/consultations/{consultation_id}/approve", dependencies=[Depends(require_csrf)])
def approve_route(session: CurrentOfficer, db: Db, consultation_id: str, payload: ApproveBody) -> dict[str, Any]:
    """The explicit approval. Sends exactly the wording whose digest is named; a retry or a second click returns the one already sent."""
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        if role_of(c, session.account_id or "") != "sender":
            raise ApiError("forbidden", "Only the sender can approve this.", 403)
        approve(db, c, session, payload.digest)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.post("/consultations/{consultation_id}/reply", dependencies=[Depends(require_csrf)])
def reply_route(session: CurrentOfficer, db: Db, consultation_id: str, payload: ReplyBody) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        if role_of(c, session.account_id or "") != "recipient":
            raise ApiError("forbidden", "Only the officer who was asked can answer.", 403)
        reply(db, c, session, payload.verified, payload.helped_action, payload.note, payload.key)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.post("/consultations/{consultation_id}/close", dependencies=[Depends(require_csrf)])
def close_route(session: CurrentOfficer, db: Db, consultation_id: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        close(db, c, session)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)


@consultation_router.post("/consultations/{consultation_id}/cancel", dependencies=[Depends(require_csrf)])
def cancel_route(session: CurrentOfficer, db: Db, consultation_id: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        c = _mine_or_404(db, session, consultation_id)
        if role_of(c, session.account_id or "") != "sender":
            raise ApiError("forbidden", "Only the sender can withdraw it.", 403)
        cancel(db, c, session)
        db.commit()
        return consultation_out(db, c, session.account_id or "")

    return _retry(db, go)

