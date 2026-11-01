"""Transaction verification on the officer's case: what was reported and where it was read, what a bank integration returned, and what comes next.

The rules this module keeps:
- Reading a receipt, a screenshot or a matching complaint field is a preliminary check. Only an answer from a bank integration can produce
  "Bank confirmed", and only a live, authenticated, non-simulated one is a production confirmation (`is_production_confirmation`).
- A verification only ever compares fields the bank returned. A field it did not return is "Not provided"; nothing is filled in or inferred.
- "No matching record" is not a finding about fraud, and "Bank confirmed" confirms only the fields that were returned and matched.
- Every attempt is a new row with its own time. A later look at an old row is labelled by its own time, never as a fresh check.
- The case is not changed. Verification adds history; it never edits a fact, a total or an official status.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .bankconnector import ProviderError, VerifyQuery, get_registry, mask_handle, mask_value
from .casework import build_snapshot
from .core import ApiError, Settings
from .models import Account, BankRequest, Case, TransactionVerification, utcnow
from .parsing import format_inr
from .schemas import ApiModel, CaseSnapshot, PaymentOut
from .security import Config, CurrentOfficer, Db, OfficerCase, require_csrf
from .verification import claim_from, payment_name, record_audit
from .workflow import _bank_for, config, staff_name

log = logging.getLogger("cyberbrief.bankverify")

STATUS_LABELS = {
    "not_checked": "Not checked",
    "checking": "Checking",
    "bank_confirmed": "Bank confirmed",
    "discrepancy": "Discrepancy found",
    "no_match": "No matching record returned",
    "more_info_required": "More information required",
    "awaiting_bank": "Awaiting bank confirmation",
    "unavailable": "Temporarily unavailable",
}
SIMULATED_BANNER = "SIMULATED BANK RESPONSE"
SOURCE_WORDS = {"image": "Screenshot", "typed_account": "Written account", "audio": "Spoken account", "transcript": "Transcript",
                "supplied_official_update": "Official update supplied by the citizen", "user_answer": "Answer to a question"}
BASIS = {"unreviewed": "Read by the system, not yet confirmed", "user_confirmed": "Confirmed by the citizen", "reviewer_checked": "Checked by an officer",
         "unresolved": "Unresolved", "excluded": "Excluded"}


def is_production_confirmation(row: TransactionVerification) -> bool:
    """The one test for 'a bank confirmed this': a live provider, a real answer, a match. Simulated and sandbox rows can never pass it."""
    return row.status == "bank_confirmed" and row.mode == "live" and not row.simulated


def mask_name(value: Any) -> str:
    words = str(value or "").split()
    return " ".join([words[0], *[w[0] + "•••" for w in words[1:]]]) if words else ""


# ---- what the case says, and where it was read ----------------------------------------------------------------------

def _fact(payment: PaymentOut, kind: str, key: str = "value") -> Any:
    fact = payment.fields.get(kind)
    return (fact.value or {}).get(key) if fact is not None and fact.value else None


def _source_note(snapshot: CaseSnapshot, payment: PaymentOut, kind: str) -> dict[str, Any] | None:
    fact = payment.fields.get(kind)
    if fact is None or not fact.value:
        return None
    ids = list(dict.fromkeys(a.source_id for a in getattr(fact, "anchors", []))) or list(payment.source_ids)[:1]
    by_id = {s.id: s for s in snapshot.sources}
    labels = [f"{SOURCE_WORDS.get(by_id[i].kind, 'File')}: {by_id[i].display_filename}" for i in ids if i in by_id]
    return {"label": "; ".join(labels) or "Read from the case evidence", "sourceIds": ids, "basis": BASIS.get(getattr(fact, "review_state", "unreviewed"), "Read by the system, not yet confirmed"),
            "reviewState": getattr(fact, "review_state", "unreviewed"), "factId": getattr(fact, "fact_id", None), "factVersion": getattr(fact, "version", None)}


def payment_inputs(snapshot: CaseSnapshot, payment: PaymentOut, bank: dict[str, Any] | None) -> dict[str, Any]:
    """The extracted fields as the officer sees them (sender and recipient masked), what is missing, and whether a check can start at all."""
    claim = claim_from(payment)
    ref_type = _fact(payment, "reference_type")
    ref = claim["reference"]
    truncated = bool(claim["truncated"])
    amount = claim["amount"]
    try:
        amount_text = format_inr(int(amount)) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount_text = None
    when = claim["date"]
    sender, recipient = _fact(payment, "account_scope"), claim["counterparty"]
    kind_label = str(ref_type).upper() if ref_type and len(str(ref_type)) <= 6 else "Payment reference"
    fields = [
        {"key": "amount", "label": "Reported amount", "value": amount_text, "source": _source_note(snapshot, payment, "amount")},
        {"key": "date", "label": "Transaction date", "value": when, "source": _source_note(snapshot, payment, "occurred_at")},
        {"key": "reference", "label": f"Transaction reference ({kind_label})", "value": str(ref) if ref and not truncated else None,
         "note": "Cut off in the evidence, so it is not used." if truncated and ref else None, "source": _source_note(snapshot, payment, "reference")},
        {"key": "provider", "label": "Bank or payment provider", "value": bank["name"] if bank else None,
         "source": {"label": "Matched from the recipient's UPI handle against the bank directory", "sourceIds": [], "basis": "Derived by code, not read from the evidence", "reviewState": "derived"} if bank else None},
        {"key": "sender", "label": "Sender (masked)", "value": mask_handle(sender) if sender else None, "source": _source_note(snapshot, payment, "account_scope")},
        {"key": "recipient", "label": "Recipient (masked)", "value": mask_handle(recipient) if recipient else None, "source": _source_note(snapshot, payment, "counterparty")},
    ]
    missing = []
    if not ref or truncated:
        missing.append({"key": "reference", "label": "A complete transaction reference (UTR, RRN or payment reference)", "required": True,
                        "why": "Nothing can be looked up without it, and a reference is never guessed."})
    if amount_text is None:
        missing.append({"key": "amount", "label": "The amount", "required": True, "why": "Without it the bank's record cannot be compared with the complaint."})
    if not when:
        missing.append({"key": "date", "label": "The transaction date", "required": False, "why": "It is compared when the bank returns one."})
    if not bank:
        missing.append({"key": "provider", "label": "The bank or payment provider", "required": False, "why": "It could not be identified from the evidence, so no integration can be chosen."})
    return {"fields": fields, "missing": missing, "canVerify": not any(m["required"] for m in missing), "claim": claim}


# ---- comparison ----------------------------------------------------------------------------------------------------

def compare(claim: dict[str, Any], returned: dict[str, Any]) -> list[dict[str, Any]]:
    """Reported value beside the bank-returned value. Only returned fields are compared; the rest read "Not provided"."""
    def money(v: Any) -> str | None:
        try:
            return format_inr(int(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return str(v)

    def row(key: str, label: str, reported: Any, bank: Any, same: Any = None, show=str) -> dict[str, Any]:
        shown_r = show(reported) if reported not in (None, "") else None
        shown_b = show(bank) if bank not in (None, "") else None
        if bank in (None, ""):
            result = "not_provided"
        elif reported in (None, ""):
            result = "not_stated"
        else:
            result = "match" if (same if same is not None else str(reported).casefold() == str(bank).casefold()) else "differs"
        return {"field": key, "label": label, "reported": shown_r, "bank": shown_b, "result": result}

    rows = [{"field": "reference", "label": "Transaction reference", "reported": claim.get("reference"), "bank": None, "result": "looked_up"}]
    rows.append(row("amount", "Amount", claim.get("amount"), returned.get("amountMinor"),
                    same=str(claim.get("amount")) == str(returned.get("amountMinor")), show=lambda v: money(v) or str(v)))
    rows.append(row("currency", "Currency", claim.get("currency"), returned.get("currency")))
    rows.append(row("date", "Date", claim.get("date"), str(returned["date"])[:10] if returned.get("date") else None))
    rows.append(row("direction", "Direction", claim.get("direction"), returned.get("direction")))
    rows.append(row("status", "Status recorded", claim.get("status"), returned.get("status")))
    named, bank_name = str(claim.get("counterparty") or ""), str(returned.get("recipientName") or "")
    rows.append(row("recipient", "Recipient name", named, bank_name, same=bool(bank_name) and bank_name.casefold() in named.casefold(), show=mask_name))
    return rows


# ---- the history ---------------------------------------------------------------------------------------------------

def _rows(db: Session, case_id: str, payment_id: str | None = None) -> list[TransactionVerification]:
    q = select(TransactionVerification).where(TransactionVerification.case_id == case_id)
    if payment_id:
        q = q.where(TransactionVerification.payment_id == payment_id)
    return list(db.scalars(q.order_by(TransactionVerification.created_at, TransactionVerification.attempt, TransactionVerification.id)).all())


def _release_abandoned(db: Session, settings: Settings, case_id: str) -> None:
    """A check that never finished (the process stopped mid-call) is closed as unavailable once its lease has passed, so it cannot block a retry for ever."""
    cutoff = utcnow() - timedelta(seconds=settings.bank_check_lease_seconds)
    db.execute(update(TransactionVerification).where(TransactionVerification.case_id == case_id, TransactionVerification.inflight.is_not(None), TransactionVerification.created_at < cutoff)
               .values(status="unavailable", inflight=None, reason_code="abandoned", detail="This check did not finish, so it was closed. No answer was received.", checked_at=utcnow()))
    db.commit()


def verification_out(row: TransactionVerification, case: Case, db: Session) -> dict[str, Any]:
    simulated = bool(row.simulated)
    return {
        "id": row.id, "paymentId": row.payment_id, "attempt": row.attempt, "status": row.status, "label": STATUS_LABELS.get(row.status, row.status),
        "mode": row.mode, "simulated": simulated, "banner": SIMULATED_BANNER if simulated else None, "productionConfirmation": is_production_confirmation(row),
        "provider": {"id": row.provider_id, "name": row.provider_name}, "reasonCode": row.reason_code, "detail": row.detail,
        "responseReference": row.response_reference, "returned": row.returned, "comparison": row.comparison or [], "inputSummary": row.input_summary,
        "evidenceSource": ("Simulated bank adapter (fictional ledger): not a bank's answer" if simulated else f"Authenticated response from {row.provider_name}") if row.mode != "none" else "No bank was asked",
        "requestedBy": staff_name(db, row.requested_by_account_id), "providerCalls": row.provider_calls,
        "checkedAt": row.checked_at.isoformat() if row.checked_at else None, "createdAt": row.created_at.isoformat(), "caseRevision": row.case_revision,
        "stale": row.case_revision != case.current_revision,
    }


def requests_out(db: Session, case: Case, settings: Settings) -> list[dict[str, Any]]:
    from .bankmail import request_out  # delivery and replies live in bankmail
    rows = db.scalars(select(BankRequest).where(BankRequest.case_id == case.id).order_by(BankRequest.created_at, BankRequest.id)).all()
    return [request_out(db, r, settings) for r in rows]


def _awaiting(requests: list[BankRequest], payment_id: str, latest: TransactionVerification | None) -> BankRequest | None:
    for r in reversed(requests):
        if payment_id in (r.payment_ids or []) and r.state in ("queued", "sent") and (latest is None or r.created_at >= latest.created_at):
            return r
    return None


def _next_action(status: str, view_missing: list[dict[str, Any]], integration: dict[str, Any], latest: dict[str, Any] | None, awaiting: bool) -> dict[str, Any]:
    required = [m for m in view_missing if m["required"]]
    if required:
        return {"type": "ask_citizen", "label": "Ask the citizen for the missing detail",
                "text": "Missing: " + "; ".join(m["label"] for m in required) + ". Use the existing question to the citizen; nothing is guessed."}
    if status == "checking":
        return {"type": "wait", "label": "Wait for the check to finish", "text": "A check is in progress."}
    if awaiting:
        return {"type": "wait", "label": "Wait for the bank's reply", "text": "A request was sent to the bank. Attach its reply here when it arrives."}
    if status == "bank_confirmed":
        simulated = bool(latest and latest["simulated"])
        return {"type": "none", "label": "Review what the bank returned",
                "text": "Only the fields shown as matching were returned and confirmed." + (" This was a simulated response." if simulated else "")}
    if status == "discrepancy":
        fields = ", ".join(r["label"] for r in (latest or {}).get("comparison", []) if r["result"] == "differs")
        return {"type": "ask_citizen", "label": "Check the differing details", "text": f"Differs on: {fields}. Check the receipt with the citizen, or ask the bank what its record shows."}
    if status in ("no_match", "unavailable", "more_info_required", "not_checked") and not integration["available"]:
        return {"type": "request_bank", "label": "Request bank information", "text": "Direct verification is not available here. Draft a request for the bank; you review it before anything is sent."}
    if status == "no_match":
        return {"type": "request_bank", "label": "Request bank information",
                "text": "No record was returned. That does not prove anything about the complaint: check the reference with the citizen, or ask the bank."}
    if status == "unavailable":
        return {"type": "verify", "label": "Try again", "text": "The bank could not be reached. Try again, or draft a request for the bank."}
    return {"type": "verify", "label": "Verify transaction", "text": "Check this transaction with the bank integration."}


def _integration(settings: Settings, registry, bank: dict[str, Any] | None) -> dict[str, Any]:
    if bank is None:
        return {"available": False, "bankId": None, "bankName": None, "mode": "none", "simulated": False, "reason": "The bank could not be identified from the evidence."}
    provider = registry.for_bank(bank["id"])
    if provider is None:
        return {"available": False, "bankId": bank["id"], "bankName": bank["name"], "mode": "none", "simulated": False,
                "reason": registry.not_configured.get(bank["id"], "No verification integration is configured for this bank.")}
    return {"available": True, "bankId": bank["id"], "bankName": bank["name"], "mode": provider.mode, "simulated": bool(provider.simulated), "reason": None,
            "banner": SIMULATED_BANNER if provider.simulated else None, "sends": list(provider.send)}


def section_state(db: Session, settings: Settings, case: Case, snapshot: CaseSnapshot | None = None) -> dict[str, Any]:
    """Everything the case page shows for verification: per payment, its inputs, status, history and next action."""
    snapshot = snapshot or build_snapshot(db, case)
    _release_abandoned(db, settings, case.id)
    registry = get_registry(settings, config()["banks"])
    rows = _rows(db, case.id)
    requests = list(db.scalars(select(BankRequest).where(BankRequest.case_id == case.id).order_by(BankRequest.created_at, BankRequest.id)).all())
    out = []
    for payment in snapshot.payments:
        if payment.merged_into_payment_id:
            continue
        bank = _bank_for(snapshot, [payment.id])
        inputs = payment_inputs(snapshot, payment, bank)
        history = [verification_out(r, case, db) for r in rows if r.payment_id == payment.id]
        latest_row = next((r for r in reversed(rows) if r.payment_id == payment.id), None)
        latest = history[-1] if history else None
        waiting = _awaiting(requests, payment.id, latest_row)
        status = "not_checked" if latest is None else latest["status"]
        if waiting is not None and status != "checking":
            status = "awaiting_bank"
        integration = _integration(settings, registry, bank)
        action = _next_action(status, inputs["missing"], integration, latest, waiting is not None)
        out.append({"paymentId": payment.id, "name": payment_name(payment), "status": status, "statusLabel": STATUS_LABELS[status], "inputs": inputs["fields"],
                    "missing": inputs["missing"], "canVerify": inputs["canVerify"], "integration": integration,
                    "lastCheckedAt": next((h["checkedAt"] for h in reversed(history) if h["checkedAt"]), None), "latest": latest, "history": list(reversed(history)),
                    "awaitingRequestId": waiting.id if waiting else None, "nextAction": action,
                    "latestSimulated": bool(latest and latest["simulated"]), "anyProductionConfirmation": any(h["productionConfirmation"] for h in history)})
    from .bankmail import unlinked_responses
    return {"payments": out, "requests": requests_out(db, case, settings), "unlinkedResponses": unlinked_responses(db, case), "statuses": STATUS_LABELS, "simulatedBanner": SIMULATED_BANNER,
            "notices": ["A receipt, a screenshot or a matching complaint field is a preliminary check. Only an authenticated answer from an authorised bank integration is a bank confirmation.",
                        "No matching record does not prove fraud. Bank confirmed covers only the fields the bank returned: it does not show the seller is legitimate or that the complaint is resolved."]}


# ---- verifying -----------------------------------------------------------------------------------------------------

def _actor(session: Any) -> Any:
    """What `record_audit` needs of an actor: an id and a role. A tool run on an officer's behalf is its own actor, so the trail says who acted."""
    return session


def _claim(db: Session, case: Case, payment_id: str, account_id: str | None, **fields: Any) -> tuple[TransactionVerification, bool]:
    """Start an attempt, or find the one already in flight for this payment. The unique key on (payment, inflight) is what stops a double click."""
    attempt = 1 + len(_rows(db, case.id, payment_id))
    row = TransactionVerification(case_id=case.id, payment_id=payment_id, requested_by_account_id=account_id, attempt=attempt, case_revision=case.current_revision,
                                  inflight="1", status="checking", **fields)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(TransactionVerification).where(TransactionVerification.payment_id == payment_id, TransactionVerification.inflight == "1"))
        if existing is None:
            raise
        return existing, False
    return row, True


def _record_only(db: Session, case: Case, payment_id: str, account_id: str | None, status: str, code: str, detail: str, summary: dict[str, Any], **extra: Any) -> TransactionVerification:
    row = TransactionVerification(case_id=case.id, payment_id=payment_id, requested_by_account_id=account_id, attempt=1 + len(_rows(db, case.id, payment_id)),
                                  case_revision=case.current_revision, status=status, reason_code=code, detail=detail, input_summary=summary, checked_at=utcnow(), **extra)
    db.add(row)
    db.flush()
    return row


def verify_payment(db: Session, settings: Settings, case: Case, payment_id: str, session: Any, *, via: str = "officer") -> dict[str, Any]:
    """Verify one payment with the bank integration for its bank. Commits its own steps: the 'checking' row is saved before the provider is called,
    so a second click finds it and does not call the bank again. Returns `{verification, duplicate}`; read the whole section afterwards for the page."""
    snapshot = build_snapshot(db, case)
    payment = next((p for p in snapshot.payments if p.id == payment_id and not p.merged_into_payment_id), None)
    if payment is None:
        raise ApiError("not_found", "That payment is not on this case.", 404)
    _release_abandoned(db, settings, case.id)
    account_id = getattr(session, "account_id", None)
    bank = _bank_for(snapshot, [payment.id])
    inputs = payment_inputs(snapshot, payment, bank)
    claim = inputs["claim"]
    summary = {"reference": mask_value(claim["reference"]) if claim["reference"] and not claim["truncated"] else None, "amountMinor": claim["amount"], "date": claim["date"],
               "bankId": bank["id"] if bank else None, "via": via}

    def finish(row: TransactionVerification, duplicate: bool = False) -> dict[str, Any]:
        record_audit(db, case, _actor(session), "transaction_verification", subject_type="payment", subject_id=payment.id,
                     detail={"verificationId": row.id, "provider": row.provider_id, "mode": row.mode, "simulated": row.simulated, "status": row.status, "reasonCode": row.reason_code,
                             "reference": summary["reference"], "attempt": row.attempt, "providerCalls": row.provider_calls, "via": via})
        db.commit()
        return {"verification": verification_out(row, case, db), "duplicate": duplicate}

    required = [m for m in inputs["missing"] if m["required"]]
    if required:
        row = _record_only(db, case, payment.id, account_id, "more_info_required", "missing_inputs", "Missing: " + "; ".join(m["label"] for m in required), {**summary, "missing": [m["key"] for m in required]})
        return finish(row)
    if bank is None:
        row = _record_only(db, case, payment.id, account_id, "more_info_required", "provider_unknown", "The bank or payment provider could not be identified from the evidence, so no integration was chosen.", summary)
        return finish(row)
    registry = get_registry(settings, config()["banks"])
    provider = registry.for_bank(bank["id"])
    if provider is None:
        reason = registry.not_configured.get(bank["id"], "No verification integration is configured for this bank.")
        row = _record_only(db, case, payment.id, account_id, "unavailable", "no_integration", f"Direct verification is unavailable. {reason}", summary,
                           provider_id=bank["id"], provider_name=bank["name"])
        return finish(row)
    if provider.simulated != (provider.mode == "sandbox"):   # a provider that says one thing and is another is refused, not stored
        raise ApiError("invalid_request", "This integration is not configured consistently.", 500)
    row, created = _claim(db, case, payment.id, account_id, provider_id=provider.id, provider_name=provider.name, mode=provider.mode, simulated=bool(provider.simulated), input_summary=summary)
    if not created:
        return {"verification": verification_out(row, case, db), "duplicate": True}
    query = VerifyQuery(reference=str(claim["reference"]), amount_minor=int(claim["amount"]), currency=claim.get("currency") or "INR", date=claim.get("date"))
    answer, error = None, None
    for call in range(1 + max(0, settings.bank_provider_retries)):
        row.provider_calls = call + 1
        try:
            answer, error = provider.verify(query), None
            break
        except ProviderError as exc:
            error = exc
            if exc.kind not in ("timeout", "unavailable"):
                break
        except Exception as exc:   # an adapter bug must not leave a check "checking"; the message is generic, never the exception text
            log.exception("provider %s failed unexpectedly (%s)", provider.id, type(exc).__name__)
            error = ProviderError("unavailable", "The integration failed unexpectedly.")
            break
        time.sleep(0.05)
    row.checked_at = utcnow()
    row.inflight = None
    if error is not None:
        row.status = "more_info_required" if error.kind == "incomplete" else "unavailable"
        row.reason_code, row.detail = error.kind, error.message + (" Try again." if error.kind in ("timeout", "unavailable") else "")
        return finish(row)
    assert answer is not None
    row.response_reference = answer.response_reference
    if not answer.found:
        row.status, row.reason_code = "no_match", "not_found"
        row.detail = "The bank returned no record for this reference. That does not show the complaint is wrong: the reference may be mistyped or held elsewhere."
        row.returned = {}
        return finish(row)
    rows = compare(claim, answer.returned)
    row.returned, row.comparison = answer.returned, rows
    differing = [r for r in rows if r["result"] == "differs"]
    amount_row = next(r for r in rows if r["field"] == "amount")
    if differing:
        row.status, row.reason_code = "discrepancy", "fields_differ"
        row.detail = "The bank's record differs on: " + ", ".join(r["label"] for r in differing) + "."
    elif amount_row["result"] == "match":
        row.status, row.reason_code = "bank_confirmed", "matched"
        row.detail = "Confirmed fields: " + ", ".join(r["label"] for r in rows if r["result"] == "match") + ". Nothing else was returned or is confirmed."
    else:
        row.status, row.reason_code = "more_info_required", "amount_not_returned"
        row.detail = "The bank returned a record without the amount, so the complaint's amount could not be compared."
    return finish(row)


# ---- the case-level recommendation, used by the officer agent ------------------------------------------------------

def recommend(section_payment: dict[str, Any]) -> dict[str, Any]:
    """The next step from what the record actually holds. An assumption is never turned into a confirmed fact: only `anyProductionConfirmation` says a bank did."""
    p = section_payment
    return {"paymentId": p["paymentId"], "status": p["status"], "statusLabel": p["statusLabel"], "nextAction": p["nextAction"],
            "bankConfirmed": bool(p["latest"] and p["latest"]["productionConfirmation"]), "simulated": bool(p["latest"] and p["latest"]["simulated"])}


# ---- routes --------------------------------------------------------------------------------------------------------

bank_router = APIRouter(prefix="/api/officer", tags=["bank-verification"])


class VerifyBody(ApiModel):
    payment_id: str


@bank_router.get("/cases/{case_id}/verification")
def get_verification(case: OfficerCase, db: Db, settings: Config) -> dict[str, Any]:
    return section_state(db, settings, case)


@bank_router.post("/cases/{case_id}/verification/verify", dependencies=[Depends(require_csrf)])
def post_verify(case: OfficerCase, session: CurrentOfficer, db: Db, settings: Config, payload: VerifyBody) -> dict[str, Any]:
    result = verify_payment(db, settings, case, payload.payment_id, session)
    return {**result, "section": section_state(db, settings, case)}


def officer_identity(db: Session, account_id: str | None) -> dict[str, str | None]:
    account = db.get(Account, account_id) if account_id else None
    return {"name": (account.display_name or account.officer_id) if account else None, "officerId": account.officer_id if account else None}


