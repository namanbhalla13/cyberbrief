"""What an officer can check a case against, and see of its history.

A simulated bank, what supports each payment, a timeline with its evidence, and the audit trail. All of it is
plain code over the saved record: no model reads or writes anything here, and nothing here changes the case, its
totals, or any official status. A bank check is a separate, labelled track beside the citizen's own confirmation.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, Case, Payment, ReviewEvent, UserSession
from .parsing import format_inr
from .schemas import (
    AuditEventOut,
    BankCheckOut,
    BankFieldOut,
    CaseSnapshot,
    OfficerDeskOut,
    PaymentOut,
    PaymentSupportOut,
    TimelineItemOut,
)

_ASSETS = Path(__file__).parent / "assets"

LEVEL_LABELS = {
    "bank_matched": "Matches the simulated bank record",
    "bank_differs": "Differs from the simulated bank record",
    "bank_not_found": "Not in the simulated bank record",
    "cannot_verify": "Can't verify",
    "citizen_confirmed": "Confirmed by the citizen, not bank-checked",
    "ai_proposed": "Read by the system, not yet confirmed",
}


# ---- the simulated bank -----------------------------------------------------------------------------

def _reference_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


@lru_cache
def ledger() -> dict[str, Any]:
    data = json.loads((_ASSETS / "mock_bank_ledger.json").read_text(encoding="utf-8"))
    data["byReference"] = {_reference_key(e["reference"]): e for e in data["entries"]}
    return data


def _value(payment: PaymentOut, kind: str, key: str = "value") -> Any:
    fact = payment.fields.get(kind)
    return (fact.value or {}).get(key) if fact is not None and fact.value else None


def _date_of(payment: PaymentOut) -> str | None:
    fact = payment.fields.get("occurred_at")
    if fact is None or not fact.value:
        return None
    when = fact.value.get("date") or fact.value.get("localDatetime")
    return str(when)[:10] if when else None


def claim_from(payment: PaymentOut) -> dict[str, Any]:
    """What the case says about a payment, as the bank check will compare it."""
    return {
        "reference": _value(payment, "reference"), "truncated": bool(_value(payment, "reference", "truncated")),
        "amount": _value(payment, "amount", "amountMinor"), "currency": _value(payment, "currency", "code"),
        "direction": _value(payment, "direction"), "status": _value(payment, "status"),
        "date": _date_of(payment), "counterparty": _value(payment, "counterparty"),
    }


def payment_name(payment: PaymentOut) -> str:
    """A payment is called by what it was and who it went to, never by its internal label."""
    amount = _value(payment, "amount", "amountMinor")
    try:
        head = format_inr(int(amount)) if amount not in (None, "") else "A payment"
    except (TypeError, ValueError):
        head = "A payment"
    to = _value(payment, "counterparty")
    when = _date_of(payment)
    return head + (f" to {to}" if to else "") + (f", {when}" if when else "")


def _money(minor: Any) -> str | None:
    try:
        return format_inr(int(minor)) if minor not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _compare(name: str, claimed: Any, bank: Any, show=lambda v: str(v)) -> dict[str, Any]:
    if claimed in (None, ""):
        return {"field": name, "claimed": None, "bank": show(bank), "result": "not_stated"}
    same = str(claimed).casefold() == str(bank).casefold()
    return {"field": name, "claimed": show(claimed), "bank": show(bank), "result": "match" if same else "differs"}


def check_against_ledger(claim: dict[str, Any]) -> dict[str, Any]:
    """Compare one claimed payment with the simulated ledger. Pure code: it can only agree, differ or say it cannot."""
    book = ledger()
    base = {"reference": claim.get("reference"), "ledgerVersion": book["ledgerVersion"], "fields": []}
    if claim.get("truncated"):
        return {**base, "result": "cannot_verify", "reason": "The payment reference is cut off, so there is nothing reliable to look up."}
    if not claim.get("reference"):
        return {**base, "result": "cannot_verify", "reason": "No payment reference could be read, so there is nothing to look up."}
    entry = book["byReference"].get(_reference_key(claim["reference"]))
    if entry is None:
        return {**base, "result": "not_found", "reason": "No record with this reference in the simulated ledger."}
    fields = [
        _compare("amount", claim.get("amount"), entry["amountMinor"], lambda v: _money(v) or str(v)),
        _compare("currency", claim.get("currency"), entry["currency"]),
        _compare("direction", claim.get("direction"), entry["direction"]),
        _compare("status", claim.get("status"), entry["status"]),
        _compare("date", claim.get("date"), entry["date"]),
    ]
    named = str(claim.get("counterparty") or "")
    if named:   # a receipt may add a UPI address after the name, so the ledger's name only has to appear in it
        same = entry["counterparty"].casefold() in named.casefold()
        fields.append({"field": "paid to", "claimed": named, "bank": entry["counterparty"], "result": "match" if same else "differs"})
    else:
        fields.append({"field": "paid to", "claimed": None, "bank": entry["counterparty"], "result": "not_stated"})
    differing = [f["field"] for f in fields if f["result"] == "differs"]
    if differing:
        return {**base, "fields": fields, "result": "differs", "reason": "The simulated bank record differs on: " + ", ".join(differing) + "."}
    if any(f["field"] == "amount" and f["result"] == "match" for f in fields):
        return {**base, "fields": fields, "result": "matched", "reason": "Every detail that could be compared agrees with the simulated bank record."}
    return {**base, "fields": fields, "result": "cannot_verify", "reason": "The amount could not be read, so the record could not be compared."}


# ---- the audit trail ---------------------------------------------------------------------------------

def record_audit(db: Session, case: Case, session: UserSession, kind: str, *, subject_type: str | None = None,
                 subject_id: str | None = None, detail: dict[str, Any] | None = None) -> AuditEvent:
    event = AuditEvent(case_id=case.id, revision=case.current_revision, kind=kind, subject_type=subject_type, subject_id=subject_id,
                       actor_session_id=session.id, actor_role=session.role, detail=detail or {})
    db.add(event)
    db.flush()
    return event


def run_bank_check(db: Session, case: Case, session: UserSession, snapshot: CaseSnapshot) -> int:
    """Check every payment that stands on its own against the simulated ledger, and record each check."""
    checked = 0
    for payment in snapshot.payments:
        if payment.merged_into_payment_id:
            continue
        result = check_against_ledger(claim_from(payment))
        record_audit(db, case, session, "bank_check", subject_type="payment", subject_id=payment.id,
                     detail={**result, "name": payment_name(payment)})
        checked += 1
    return checked


# ---- what supports each payment ----------------------------------------------------------------------

_READABLE = (("amount", "amount"), ("reference", "reference"), ("status", "status"), ("occurred_at", "date"))


def _unconfirmed(payment: PaymentOut) -> bool:
    facts = [f for f in payment.fields.values() if f.value is not None and f.review_state in ("unreviewed", "unresolved")]
    return bool(facts) or payment.identity_review_state == "unreviewed"


def _bank_out(event: AuditEvent, case: Case) -> BankCheckOut:
    d = event.detail
    return BankCheckOut(
        result=d["result"], reason=d["reason"], reference=d.get("reference"),
        fields=[BankFieldOut(**f) for f in d.get("fields", [])], ledger_version=d.get("ledgerVersion", ""),
        checked_at=event.created_at, revision=event.revision, stale=event.revision != case.current_revision)


def latest_bank_checks(db: Session, case: Case) -> dict[str, AuditEvent]:
    rows = db.scalars(select(AuditEvent).where(AuditEvent.case_id == case.id, AuditEvent.kind == "bank_check")
                      .order_by(AuditEvent.created_at, AuditEvent.id)).all()
    return {row.subject_id: row for row in rows if row.subject_id}   # later rows replace earlier ones


def support_for(payment: PaymentOut, bank: BankCheckOut | None) -> PaymentSupportOut:
    """The one honest label for a payment, with the reasons, from what the case and the simulated bank actually say.
    There is no score: a payment is matched, differs, is not found, cannot be checked, or is waiting on the citizen."""
    unreadable = [word for kind, word in _READABLE if payment.fields.get(kind) is None or payment.fields[kind].value is None]
    # what was noted when the evidence was read, in plain words, leaving out what is already listed as unreadable
    words = {"occurred_at": "date", "account_scope": "account", "reference_type": "reference type"}
    flagged = [f"{words.get(kind, kind)}: {str(fact.issue).replace('_', ' ')}" for kind, fact in payment.fields.items()
               if fact.issue and words.get(kind, kind) not in unreadable]
    unconfirmed = _unconfirmed(payment)
    reasons: list[str] = []
    if bank is not None and bank.result == "differs":
        level = "bank_differs"
        reasons.append(bank.reason)
    elif bank is not None and bank.result == "matched":
        level = "bank_matched"
        reasons.append(bank.reason)
        if unconfirmed:
            reasons.append("The citizen has not yet confirmed every detail.")
    elif bank is not None and bank.result == "not_found":
        level = "bank_not_found"
        reasons.append(bank.reason)
    elif bank is not None:
        level = "cannot_verify"
        reasons.append(bank.reason)
    elif not _value(payment, "reference"):
        level = "cannot_verify"
        reasons.append("No payment reference could be read, so it cannot be checked against a bank record.")
    elif unconfirmed:
        level = "ai_proposed"
        reasons.append("The system read these details from the evidence and the citizen has not confirmed them.")
    else:
        level = "citizen_confirmed"
        reasons.append("The citizen confirmed these details. They have not been checked against a bank record.")
    if bank is not None and bank.stale:
        reasons.append("The case changed after this check. Run it again to include the change.")
    if level in ("cannot_verify", "ai_proposed") and unreadable:
        reasons.append("Not readable from the evidence: " + ", ".join(unreadable) + ".")
    reasons.extend(f"Noted when read: {f}" for f in flagged[:3])
    return PaymentSupportOut(payment_id=payment.id, name=payment_name(payment), level=level, label=LEVEL_LABELS[level], reasons=reasons, bank=bank)


# ---- the timeline ----------------------------------------------------------------------------------------

_SOURCE_WORDS = {
    "image": "Screenshot added", "typed_account": "Written account added", "audio": "Spoken account added",
    "transcript": "Transcript added", "supplied_official_update": "Official update supplied by the citizen", "user_answer": "Answer to a question",
}
_SKIPPED_REVISIONS = {"sources_added"}   # each source is shown as itself, with its evidence link


def build_timeline(db: Session, snapshot: CaseSnapshot, supports: dict[str, PaymentSupportOut], events: list[AuditEvent]) -> list[TimelineItemOut]:
    items: list[TimelineItemOut] = []
    for source in snapshot.sources:
        items.append(TimelineItemOut(at=source.received_at, kind="source", title=f"{_SOURCE_WORDS.get(source.kind, 'File added')}: {source.display_filename}",
                                     source_ids=[source.id]))
    created = {p.id: p.created_at for p in db.scalars(select(Payment).where(Payment.case_id == snapshot.case.id)).all()}
    for payment in snapshot.payments:
        if payment.merged_into_payment_id or payment.id not in created:
            continue
        support = supports.get(payment.id)
        items.append(TimelineItemOut(at=created[payment.id], kind="payment", title=f"Payment read: {payment_name(payment)}",
                                     detail=support.label if support else None, level=support.level if support else None,
                                     source_ids=payment.source_ids, payment_id=payment.id))
    for revision in snapshot.revisions:
        if revision.kind not in _SKIPPED_REVISIONS:
            items.append(TimelineItemOut(at=revision.created_at, kind=f"revision_{revision.kind}", title=revision.summary,
                                         detail=f"By the {revision.actor_role}"))
    for event in snapshot.official_status_events:
        items.append(TimelineItemOut(at=event.received_at, kind="official_status", title="Official update recorded, as supplied by the citizen",
                                     detail=event.raw_status[:200], source_ids=event.source_ids))
    for event in events:
        if event.kind == "bank_check":
            d = event.detail
            items.append(TimelineItemOut(at=event.created_at, kind="bank_check", title=f"Simulated bank check: {d.get('name', 'payment')}",
                                         detail=d.get("reason"), payment_id=event.subject_id,
                                         level={"matched": "bank_matched", "differs": "bank_differs", "not_found": "bank_not_found"}.get(d.get("result"), "cannot_verify")))
        elif event.kind == "action_decision":
            d = event.detail
            items.append(TimelineItemOut(at=event.created_at, kind="decision", title=f"Officer {d.get('decision')} a suggested step: {d.get('title', '')}".strip(),
                                         detail=d.get("note")))
    return sorted(items, key=lambda item: item.at)


# ---- the desk -----------------------------------------------------------------------------------------------

def audit_out(db: Session, case: Case, events: list[AuditEvent]) -> list[AuditEventOut]:
    rows = [AuditEventOut(id=e.id, revision=e.revision, kind=e.kind, subject_type=e.subject_type, subject_id=e.subject_id,
                          actor_role=e.actor_role, detail=e.detail, created_at=e.created_at) for e in events]
    for r in db.scalars(select(ReviewEvent).where(ReviewEvent.case_id == case.id, ReviewEvent.actor_role == "officer")).all():
        rows.append(AuditEventOut(id=r.id, revision=r.revision, kind="review_recorded", subject_type=r.target_type, subject_id=r.target_id,
                                  actor_role=r.actor_role, detail={"action": r.action, **r.payload}, created_at=r.created_at))
    return sorted(rows, key=lambda row: row.created_at, reverse=True)[:100]


def audit_events(db: Session, case: Case) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.case_id == case.id).order_by(AuditEvent.created_at, AuditEvent.id)).all())


def build_desk(db: Session, case: Case, snapshot: CaseSnapshot, actions: Any = None) -> OfficerDeskOut:
    book = ledger()
    checks = latest_bank_checks(db, case)
    supports = {p.id: support_for(p, _bank_out(checks[p.id], case) if p.id in checks else None)
                for p in snapshot.payments if not p.merged_into_payment_id}
    events = audit_events(db, case)
    return OfficerDeskOut(
        ledger_name=book["ledgerName"], ledger_version=book["ledgerVersion"], notice=book["notice"],
        payments=list(supports.values()), timeline=build_timeline(db, snapshot, supports, events),
        audit=audit_out(db, case, events), actions=actions)
