"""The officer's request to a bank: composing it, delivering it truthfully, and attaching and linking what comes back.

The rules this module keeps:
- The recipient is always the bank's address from the configured directory. A model never supplies one, and a draft carries no address of its own.
- A request is a draft until the officer approves exactly the words shown. Sending is claimed in one conditional UPDATE (approved or failed -> queued), so a
  duplicate approval, a double click or a retry cannot send twice.
- "Sent" is written only after the email service accepted the message, and it means accepted, not delivered. A failed send keeps its error and can be retried.
  With no email configured nothing is sent: the officer copies or downloads the draft, and can record that they sent it themselves.
- A reply attached by an officer is officer-provided evidence. It is never given the standing of an authenticated bank answer and never changes a verification.
  It is linked by the response reference it quotes; anything less certain is flagged for the officer to check.
"""

from __future__ import annotations

import hashlib
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Protocol

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .assistant import case_ref
from .bankconnector import mask_handle, mask_value
from .bankverify import SIMULATED_BANNER, officer_identity, payment_inputs, section_state
from .core import ApiError, Settings
from .models import BankRequest, Case, CaseMessage, Source, TransactionVerification, utcnow
from .records import Storage
from .schemas import ApiModel, CaseSnapshot, PaymentOut
from .security import Config, CurrentOfficer, Db, OfficerCase, require_csrf
from .verification import record_audit
from .workflow import close_tasks, config, log_activity, open_task, staff_name

log = logging.getLogger("cyberbrief.bankmail")

REF_PATTERN = re.compile(r"CBR-[A-Z0-9]{8}")
STATE_LABELS = {"approved": "Approved, not sent", "queued": "Queued for sending", "sent": "Sent", "failed": "Failed to send", "response_received": "Response received"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


# ---- who may be written to, and how --------------------------------------------------------------------------------

def official_contact(settings: Settings) -> dict[str, str | None]:
    demo = config().get("officialContact", {}) if settings.bank_sandbox_enabled else {}
    return {"department": settings.official_department or demo.get("department"), "email": settings.official_contact_email or demo.get("email"),
            "phone": settings.official_contact_phone or demo.get("phone")}


def bank_entry(bank_id: str) -> dict[str, Any] | None:
    return next((b for b in config()["banks"] if b["id"] == bank_id), None)


def delivery_for(settings: Settings, bank: dict[str, Any]) -> tuple[str, str | None]:
    """(mode, why not email). simulated: the fictional outbox. email: a real send through the configured service. manual: nothing is sent by CyberBrief."""
    if (bank.get("integration") or {}).get("type") == "sandbox" and settings.bank_sandbox_enabled:
        return "simulated", None
    if not bank.get("emailEnabled") or not bank.get("contact"):
        return "manual", "No email address is configured for this bank."
    if settings.email_mode != "smtp" or not settings.smtp_host or not settings.email_from_address:
        return "manual", "Email sending is not configured on this deployment."
    if not official_contact(settings)["email"]:
        return "manual", "The officer's official contact email is not configured."
    return "email", None


def _new_ref(db: Session, seed: str) -> str:
    for n in range(100):
        ref = "CBR-" + hashlib.sha256(f"{seed}|{n}".encode()).hexdigest()[:8].upper()
        if db.scalar(select(BankRequest.id).where(BankRequest.response_ref == ref)) is None:
            return ref
    raise ApiError("review_conflict", "Could not make a response reference.", 409)


# ---- composing the draft -------------------------------------------------------------------------------------------

def _payment_sources(payment: PaymentOut) -> set[str]:
    """The files a payment's details were read from: its observations' sources and wherever one of its facts is anchored."""
    return set(payment.source_ids) | {a.source_id for f in payment.fields.values() for a in getattr(f, "anchors", [])}


def compose(db: Session, settings: Settings, case: Case, session: Any, snapshot: CaseSnapshot, chosen: list[PaymentOut], bank: dict[str, Any],
            pairs: list[tuple[PaymentOut, str]], missing: list[str]) -> dict[str, Any]:
    """A bank request from the record, by code. Every statement is from the case, with where it was read; questions are asked only where the record leaves them open."""
    mode, why = delivery_for(settings, bank)
    simulated = mode == "simulated"
    contact = official_contact(settings)
    who = officer_identity(db, getattr(session, "account_id", None))
    refs = list(dict.fromkeys(r for _, r in pairs))
    ids = [p.id for p, _ in pairs]
    prior = db.scalars(select(BankRequest).where(BankRequest.case_id == case.id, BankRequest.bank_id == bank["id"])).all()
    n = sum(1 for r in prior if set(r.payment_ids or []) & set(ids))
    response_ref = _new_ref(db, f"{case.id}|{sorted(refs)}|{n}")
    latest: dict[str, TransactionVerification | None] = {}
    for p, _ in pairs:
        latest[p.id] = db.scalar(select(TransactionVerification).where(TransactionVerification.payment_id == p.id, TransactionVerification.inflight.is_(None))
                                 .order_by(TransactionVerification.created_at.desc()))
    tx_lines, known, questions = [], [], []
    for p, ref in pairs:
        inp = payment_inputs(snapshot, p, bank)
        fields = {f["key"]: f for f in inp["fields"]}
        amount, date = fields["amount"]["value"], fields["date"]["value"]
        tx_lines.append(f"- {amount or 'amount not stated'}{' on ' + date if date else ''}, reference {ref}" + (f", recipient {fields['recipient']['value']}" if fields["recipient"]["value"] else ""))
        for key in ("amount", "date", "reference", "recipient", "sender"):
            f = fields[key]
            if f["value"] and f["source"]:
                known.append({"field": f["label"], "value": f["value"], "source": f["source"]["label"], "basis": f["source"]["basis"]})
        row = latest[p.id]
        live_found = row is not None and not row.simulated and row.status in ("no_match", "discrepancy", "bank_confirmed", "unavailable", "more_info_required")
        if row is None or row.status != "bank_confirmed" or row.simulated:
            questions.append(f"Does a transaction with reference {ref} exist in your records?")
            questions.append("What is its recorded status (for example completed, failed, pending or reversed)?")
        if amount or date:
            questions.append("Does the amount" + (f" ({amount})" if amount else "") + (" and the date" if date else "") + (f" ({date})" if date else "") + " recorded for this transaction match the complaint?")
        if row is not None and not row.simulated and row.status in ("no_match", "more_info_required"):
            questions.append(f"No record was returned to CyberBrief's direct check for {ref}. What additional information do you need to locate it?")
        if live_found and row.status == "discrepancy":
            diffs = ", ".join(c["label"] for c in (row.comparison or []) if c["result"] == "differs")
            questions.append(f"A direct check returned a record that differs from the complaint on: {diffs}. What does your record show for each?")
    questions = list(dict.fromkeys(questions))
    for name in missing:
        tx_lines.append(f"- {name}: no readable reference, not included in this request")
    attachments = sorted({sid for p, _ in pairs for sid in _payment_sources(p)})
    by_id = {s.id: s for s in snapshot.sources}
    lines = [
        f"Case reference: {case_ref(case.id)}",
        f"Response reference: {response_ref}. Please quote it in the subject line of your reply so that it reaches this case.",
        "",
        f"From: {who['name'] or 'the assigned officer'}" + (f" (officer ID {who['officerId']})" if who["officerId"] else "") + (f", {contact['department']}" if contact["department"] else ""),
        "Official contact: " + (" · ".join(x for x in (contact["email"], contact["phone"]) if x) or "not configured"),
        "",
        "We are asking about:",
        *tx_lines,
        "",
        "What we already know, and where it was read:",
        *[f"- {k['field']}: {k['value']} ({k['source']}; {k['basis'].lower()})" for k in known],
        "",
        "Our questions:",
        *[f"{i}. {q}" for i, q in enumerate(questions, 1)],
        "",
        "Selected evidence is attached (see the attachment list)." if attachments else "No evidence files are attached.",
        "",
        f"Requested by {who['name'] or 'the assigned officer'} on {utcnow().date().isoformat()}.",
    ]
    if simulated:
        lines.append("This is a simulated request made in a demo. No bank has been contacted.")
    body = "\n".join(lines)
    return {"bankId": bank["id"], "bankName": bank["name"], "to": bank["contact"], "subject": f"Verification request: {case_ref(case.id)} [{response_ref}]", "body": body,
            "paymentIds": ids, "references": refs, "attachments": attachments, "attachmentNames": {i: by_id[i].display_filename for i in attachments if i in by_id},
            "responseRef": response_ref, "questions": questions, "known": known, "officer": {**who, **contact}, "delivery": mode, "deliveryNote": why,
            "contactMissing": not contact["email"]}


# ---- approval, delivery --------------------------------------------------------------------------------------------

def create_request(db: Session, case: Case, session: Any, proposal: Any, message: CaseMessage, settings: Settings) -> BankRequest:
    """Called when the officer approves a bank draft. The address comes from the directory entry, never from the draft."""
    payload = proposal.payload
    bank = bank_entry(payload["bankId"])
    if bank is None:
        raise ApiError("invalid_request", "That bank is no longer in the directory.", 422, "Draft the request again.")
    mode, _why = delivery_for(settings, bank)
    ref = payload.get("responseRef") or _new_ref(db, f"{case.id}|{proposal.id}")
    if db.scalar(select(BankRequest.id).where(BankRequest.response_ref == ref)) is not None:   # an earlier approval used this reference: this one gets its own
        new = _new_ref(db, f"{case.id}|{proposal.id}|re")
        payload = {**payload, "body": payload["body"].replace(ref, new), "subject": payload["subject"].replace(ref, new)}
        ref = new
    sources = set(db.scalars(select(Source.id).where(Source.case_id == case.id)).all())
    req = BankRequest(case_id=case.id, proposal_id=proposal.id, message_id=message.id, payment_ids=payload["paymentIds"], bank_id=bank["id"], bank_name=bank["name"], to_address=bank["contact"],
                      subject=payload["subject"][:300], body=payload["body"], attachments=[i for i in payload.get("attachments", []) if i in sources], response_ref=ref,
                      state="sent" if mode == "simulated" else "approved", delivery=mode, approved_by_account_id=session.account_id, sent_at=utcnow() if mode == "simulated" else None)
    db.add(req)
    db.flush()
    message.state = "sent" if mode == "simulated" else "approved"
    message.data = {**message.data, "bankRequestId": req.id, "responseRef": ref, "delivery": mode, "to": req.to_address, "subject": req.subject}
    message.body = payload["body"]
    record_audit(db, case, session, "bank_request_approved", subject_type="bank_request", subject_id=req.id,
                 detail={"proposalId": proposal.id, "digest": proposal.digest, "delivery": mode, "bankId": bank["id"], "responseRef": ref, "references": [mask_value(r) for r in payload["references"]]})
    close_tasks(db, case.id, "follow_up", message.id)
    if mode != "simulated":
        open_task(db, case.id, "follow_up", "Send the approved bank request: it has not been sent yet", ref_id=message.id)
    return req


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpSender:
    """Sends through the configured SMTP service. Accepted means the service took the message; nothing here shows it was delivered."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def send(self, message: EmailMessage) -> None:
        s = self.s
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds) as smtp:
            if s.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())   # certificate verification stays on
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


_SENDER_OVERRIDE: EmailSender | None = None


def set_sender_for_tests(sender: EmailSender | None) -> None:
    global _SENDER_OVERRIDE
    _SENDER_OVERRIDE = sender


def get_sender(settings: Settings) -> EmailSender:
    return _SENDER_OVERRIDE if _SENDER_OVERRIDE is not None else SmtpSender(settings)


def build_email(db: Session, settings: Settings, req: BankRequest) -> EmailMessage:
    contact = official_contact(settings)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.email_from_address, req.to_address, req.subject
    msg["Reply-To"] = contact["email"] or settings.email_from_address
    msg["X-CyberBrief-Response-Ref"] = req.response_ref
    msg["Message-ID"] = make_msgid(domain=(settings.email_from_address.rpartition("@")[2] or "cyberbrief.invalid"))
    msg.set_content(req.body)
    storage, total = Storage(settings), 0
    for source in db.scalars(select(Source).where(Source.id.in_(req.attachments or []), Source.case_id == req.case_id)).all():
        data = storage.read(source.storage_key) if source.storage_key else (source.text_content or "").encode("utf-8")
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ApiError("invalid_request", "The selected evidence is too large to email.", 422, "Remove an attachment and send again.")
        maintype, _, subtype = (source.media_type or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=source.display_filename)
    return msg


def deliver(db: Session, settings: Settings, request_id: str, session: Any) -> BankRequest:
    """Claim the send, send once, record the truth. Safe to call twice: only the call that wins the conditional UPDATE sends."""
    req = db.get(BankRequest, request_id)
    if req is None or req.delivery != "email":
        return req   # type: ignore[return-value]
    won = db.execute(update(BankRequest).where(BankRequest.id == request_id, BankRequest.delivery == "email", BankRequest.state.in_(("approved", "failed")))
                     .values(state="queued", queued_at=utcnow(), attempts=BankRequest.attempts + 1, last_error=None)).rowcount
    db.commit()
    db.refresh(req)
    if not won:
        return req
    case = db.get(Case, req.case_id)
    message = db.get(CaseMessage, req.message_id) if req.message_id else None
    error: str | None = None
    message_id = None
    try:
        email = build_email(db, settings, req)
        message_id = str(email["Message-ID"])
        get_sender(settings).send(email)
    except ApiError as exc:
        error = exc.message
    except smtplib.SMTPAuthenticationError:
        error = "The email service did not accept the sign-in. Check the email configuration."
    except smtplib.SMTPRecipientsRefused:
        error = "The email service refused the bank's address."
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:   # the type is useful, the text can carry a host or a login
        error = f"The email service could not take the message ({type(exc).__name__})."
    except Exception as exc:
        log.exception("unexpected send failure (%s)", type(exc).__name__)
        error = "Sending failed unexpectedly."
    if error is None:
        req.state, req.sent_at, req.provider_message_id, req.last_error = "sent", utcnow(), message_id, None
        if message is not None:
            message.state = "sent"
        close_tasks(db, req.case_id, "follow_up", req.message_id)
        open_task(db, req.case_id, "bank_response", "Waiting for the bank's reply", ref_id=req.message_id, days=config()["bankReplyDays"])
    else:
        req.state, req.last_error = "failed", error
        if message is not None:
            message.state = "failed"
    record_audit(db, case, session, "bank_request_delivery", subject_type="bank_request", subject_id=req.id,
                 detail={"state": req.state, "delivery": req.delivery, "attempt": req.attempts, "error": error, "acceptedNotDelivered": error is None})
    log_activity(db, case.id, "officer_assistant", [f"Tried to send the approved bank request (attempt {req.attempts})", "The email service accepted it" if error is None else "Sending failed; the request is kept and can be retried"],
                 "Sent" if error is None else "Send failed")
    db.commit()
    db.refresh(req)
    return req


def mark_sent_manually(db: Session, case: Case, session: Any, req: BankRequest) -> BankRequest:
    """The officer sent the copied draft themselves. Recorded as the officer's statement; CyberBrief did not send it and cannot say it arrived."""
    if req.delivery == "simulated":
        raise ApiError("review_conflict", "This request is in the simulated outbox.", 409)
    if req.state == "sent" or req.state == "response_received":
        return req
    if req.state == "queued":
        raise ApiError("review_conflict", "This request is being sent right now.", 409)
    won = db.execute(update(BankRequest).where(BankRequest.id == req.id, BankRequest.state.in_(("approved", "failed"))).values(state="sent", sent_at=utcnow(), delivery="manual")).rowcount
    if won:
        message = db.get(CaseMessage, req.message_id) if req.message_id else None
        if message is not None:
            message.state = "sent"
        close_tasks(db, case.id, "follow_up", req.message_id)
        open_task(db, case.id, "bank_response", "Waiting for the bank's reply", ref_id=req.message_id, days=config()["bankReplyDays"])
        record_audit(db, case, session, "bank_request_marked_sent", subject_type="bank_request", subject_id=req.id, detail={"statement": "sent by the officer outside CyberBrief"})
    db.commit()
    db.refresh(req)
    return req


# ---- replies -------------------------------------------------------------------------------------------------------

_KEYWORDS = re.compile(r"\b(confirm|confirmed|no record|not found|unable|reversed|reversal|successful|failed|pending|settled|credited|debited|amount|status|require|need)\w*", re.I)


def summarise(text: str, refs: list[str]) -> list[dict[str, Any]]:
    """Verbatim excerpts of the reply, chosen by code (they name a reference, an amount or a status word), each with where it sits in the reply. Never a paraphrase."""
    parts = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[^\n.!?]+[.!?]?", text) if m.group(0).strip()]
    chosen = [p for p in parts if any(r.lower() in p[2].lower() for r in refs if r) or _KEYWORDS.search(p[2])][:4] or parts[:2]
    return [{"text": t.strip()[:300], "start": s, "end": e} for s, e, t in chosen]


def _response_out(m: CaseMessage) -> dict[str, Any]:
    d = m.data or {}
    return {"id": m.id, "body": m.body, "createdAt": m.created_at.isoformat(), "receivedAt": d.get("receivedAt"), "from": d.get("fromAddress"), "filename": d.get("filename"),
            "association": d.get("association", "unlinked"), "needsReview": bool(d.get("needsReview", True)), "associationNote": d.get("associationNote"),
            "summary": d.get("summary", []), "state": m.state, "simulated": m.simulated, "requestId": d.get("bankRequestId"),
            "provenance": "Officer-provided evidence: not an authenticated bank response", "authenticated": False}


def request_out(db: Session, r: BankRequest, settings: Settings) -> dict[str, Any]:
    by_id = {s.id: s.display_filename for s in db.scalars(select(Source).where(Source.id.in_(r.attachments or []))).all()}
    replies = [m for m in db.scalars(select(CaseMessage).where(CaseMessage.case_id == r.case_id, CaseMessage.direction == "incoming", CaseMessage.channel == "bank")
                                     .order_by(CaseMessage.created_at)).all() if (m.data or {}).get("bankRequestId") == r.id]
    label = {"simulated": "Simulated outbox: no email left this system", "email": "Email through the configured service", "manual": "Not sent by CyberBrief: copy or download the draft"}[r.delivery]
    sent_words = ("Placed in the simulated outbox. No email was sent." if r.delivery == "simulated" else "Accepted by the email service. That is not proof of delivery." if r.delivery == "email" else "Recorded by the officer as sent outside CyberBrief.")
    can_email = r.delivery == "email" and r.state in ("approved", "failed")
    return {"id": r.id, "caseId": r.case_id, "state": r.state, "stateLabel": STATE_LABELS[r.state], "delivery": r.delivery, "deliveryLabel": label, "simulated": r.delivery == "simulated",
            "banner": SIMULATED_BANNER if r.delivery == "simulated" else None, "bankId": r.bank_id, "bankName": r.bank_name, "to": r.to_address, "subject": r.subject, "body": r.body,
            "attachments": [{"id": i, "filename": by_id.get(i, "File")} for i in r.attachments or []], "responseRef": r.response_ref, "paymentIds": r.payment_ids, "attempts": r.attempts,
            "lastError": r.last_error, "approvedBy": staff_name(db, r.approved_by_account_id), "approvedAt": r.approved_at.isoformat(), "sentAt": r.sent_at.isoformat() if r.sent_at else None,
            "sentNote": sent_words if r.sent_at else None, "canSend": can_email, "canMarkSent": r.delivery == "manual" and r.state in ("approved", "failed"),
            "replies": [_response_out(m) for m in replies], "messageId": r.message_id}


def unlinked_responses(db: Session, case: Case) -> list[dict[str, Any]]:
    return [_response_out(m) for m in db.scalars(select(CaseMessage).where(CaseMessage.case_id == case.id, CaseMessage.direction == "incoming", CaseMessage.channel == "bank")
                                                  .order_by(CaseMessage.created_at)).all() if not (m.data or {}).get("bankRequestId")]


class ResponseBody(ApiModel):
    text: str = Field(min_length=1, max_length=20000)
    request_id: str | None = None
    from_address: str | None = Field(default=None, max_length=254)
    received_at: str | None = Field(default=None, max_length=40)
    filename: str | None = Field(default=None, max_length=200)


def attach_response(db: Session, case: Case, session: Any, body: ResponseBody) -> CaseMessage:
    """Record a reply the officer attached, linked by the response reference it quotes. A reference from anywhere else is refused, not guessed around."""
    text = body.text.strip()
    if not text:
        raise ApiError("invalid_request", "The response is empty.", 422)
    quoted = set(REF_PATTERN.findall(text.upper()))
    own = {r.response_ref: r for r in db.scalars(select(BankRequest).where(BankRequest.case_id == case.id)).all()}
    foreign = {q for q in quoted if q not in own}
    if foreign:
        raise ApiError("invalid_request", "This response quotes a reference that does not belong to this case.", 422, "Check that it is the reply to this case's request. It was not recorded.")
    chosen = db.get(BankRequest, body.request_id) if body.request_id else None
    if body.request_id and (chosen is None or chosen.case_id != case.id):
        raise ApiError("not_found", "No such request on this case.", 404)
    matched = [own[q] for q in sorted(quoted)]
    if len(matched) > 1 and chosen is None:
        raise ApiError("invalid_request", "This response names more than one request.", 422, "Choose which request it answers.")
    if len(matched) == 1 and chosen is not None and matched[0].id != chosen.id:
        raise ApiError("invalid_request", "The reference in the response belongs to a different request than the one you chose.", 422, "It was not recorded.")
    req = matched[0] if len(matched) == 1 else chosen
    if len(matched) == 1:
        association, note = "reference_match", f"Linked by the response reference {matched[0].response_ref} quoted in the reply."
    elif chosen is not None:
        association, note = "officer_selected", "No response reference was found in the reply. You chose the request it answers: check that it belongs there."
    else:
        association, note = "unlinked", "No response reference was found and no request was chosen. It is kept on the case, not tied to any request."
    squeezed = re.sub(r"\s+", " ", text)
    digest = hashlib.sha256(f"{case.id}|{req.id if req else ''}|{squeezed}".encode()).hexdigest()
    for m in db.scalars(select(CaseMessage).where(CaseMessage.case_id == case.id, CaseMessage.direction == "incoming", CaseMessage.channel == "bank")).all():
        if (m.data or {}).get("digest") == digest:
            return m   # the same reply attached twice is the one reply
    origin_message = db.get(CaseMessage, req.message_id) if req and req.message_id else None
    watch = list((origin_message.data or {}).get("references", [])) if origin_message else []
    reply = CaseMessage(case_id=case.id, channel="bank", direction="incoming", request_id=req.message_id if req else None, body=text, payment_ids=req.payment_ids if req else [],
                        simulated=bool(req and req.delivery == "simulated"), state="received",
                        data={"provenance": "officer_provided", "authenticated": False, "association": association, "needsReview": association != "reference_match",
                              "associationNote": note, "bankRequestId": req.id if req else None, "responseRef": req.response_ref if req else None, "fromAddress": body.from_address,
                              "receivedAt": body.received_at, "filename": body.filename, "digest": digest, "attachedBy": staff_name(db, session.account_id),
                              "summary": summarise(text, watch)})
    db.add(reply)
    db.flush()
    if req is not None and association != "unlinked":
        if req.state == "sent":
            req.state = "response_received"
            origin = db.get(CaseMessage, req.message_id) if req.message_id else None
            if origin is not None:
                origin.state = "answered"
            close_tasks(db, case.id, "bank_response", req.message_id)
        else:
            reply.data = {**reply.data, "needsReview": True, "associationNote": note + f" This request is recorded as '{STATE_LABELS[req.state].lower()}', not as sent."}
    open_task(db, case.id, "review_reply", "Review the bank's reply: officer-provided, not authenticated" + ("" if association == "reference_match" else ", and check it belongs here"), ref_id=reply.id)
    record_audit(db, case, session, "bank_response_attached", subject_type="message", subject_id=reply.id,
                 detail={"requestId": req.id if req else None, "association": association, "responseRef": req.response_ref if req else None, "provenance": "officer_provided",
                         "from": mask_handle(body.from_address) if body.from_address else None})
    log_activity(db, case.id, "officer_assistant", ["The officer attached a bank reply", "Linked it by its response reference" if association == "reference_match" else "Could not link it with certainty; flagged for review",
                                                    "Recorded it as officer-provided evidence, not a bank-authenticated answer"], "Reply attached")
    db.flush()
    return reply


# ---- routes --------------------------------------------------------------------------------------------------------

mail_router = APIRouter(prefix="/api/officer", tags=["bank-requests"])


def _request(db: Session, case: Case, request_id: str) -> BankRequest:
    r = db.scalar(select(BankRequest).where(BankRequest.id == request_id, BankRequest.case_id == case.id))
    if r is None:
        raise ApiError("not_found", "No such bank request on this case.", 404)
    return r


@mail_router.post("/cases/{case_id}/bank-requests/{request_id}/send", dependencies=[Depends(require_csrf)])
def send_request(case: OfficerCase, session: CurrentOfficer, db: Db, settings: Config, request_id: str) -> dict[str, Any]:
    """Send an approved request, or try again after a failure. A request that is queued or sent is returned as it is."""
    r = _request(db, case, request_id)
    if r.delivery != "email":
        raise ApiError("invalid_request", "CyberBrief cannot send this request.", 409, "Copy or download the draft and send it yourself, then record that you did.")
    deliver(db, settings, r.id, session)
    return section_state(db, settings, case)


@mail_router.post("/cases/{case_id}/bank-requests/{request_id}/mark-sent", dependencies=[Depends(require_csrf)])
def mark_sent(case: OfficerCase, session: CurrentOfficer, db: Db, settings: Config, request_id: str) -> dict[str, Any]:
    mark_sent_manually(db, case, session, _request(db, case, request_id))
    return section_state(db, settings, case)


@mail_router.post("/cases/{case_id}/bank-responses", dependencies=[Depends(require_csrf)])
def post_response(case: OfficerCase, session: CurrentOfficer, db: Db, settings: Config, payload: ResponseBody) -> dict[str, Any]:
    reply = attach_response(db, case, session, payload)
    db.commit()
    return {"response": _response_out(reply), "section": section_state(db, settings, case)}


