"""Case lifecycle: storage, revisions, review actions, exports, demo packs and retention."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import wave
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .accounting import (
    CRITICAL_FIELDS,
    PaymentView,
    compute_recovery,
    compute_totals,
    find_duplicate_candidates,
    invalidate_allocations,
    sync_questions,
)
from .core import ApiError, Settings, make_session
from .models import (
    Case,
    CaseRevision,
    DerivedAsset,
    ExportSnapshot,
    Fact,
    OfficialStatusEvent,
    OtpChallenge,
    Payment,
    PaymentObservation,
    ProcessingRun,
    Question,
    Refund,
    RefundAllocation,
    ResolutionReview,
    ReviewEvent,
    Source,
    UserSession,
    utcnow,
)
from .parsing import MoneyParseError, format_inr, parse_amount, parse_date_text, sha256_bytes
from .records import (
    Storage,
    bump_revision,
    check_expected_revision,
    current_fact,
    current_facts,
    record_review_event,
    sanitise_filename,
    supersede_fact,
)
from .schemas import (
    AllocationOut,
    AnchorOut,
    CaseCreate,
    CaseSnapshot,
    CaseSummary,
    DuplicateCandidateOut,
    ExportOut,
    FactOut,
    LatestStatusOut,
    OfficialStatusEventOut,
    PaymentOut,
    QuestionOut,
    RecoveryOut,
    RefundOut,
    ResolutionOut,
    ReviewAction,
    ReviewBatch,
    ReviewEventOut,
    RevisionOut,
    RunOut,
    SourceOut,
    TotalsOut,
)
from .security import DUMMY_HASH, detach_account_cases, hash_secret, verify_secret

# ---- from services/audio.py ----------------------------------------------------------------------
# Recorded-account audio: validation of PCM WAV, optional FFmpeg conversion, caps.
#
# The browser encodes 16 kHz mono 16-bit PCM WAV itself, so FFmpeg is optional and
# only used when `AUDIO_CONVERTER=ffmpeg` and a non-WAV file is uploaded. FFmpeg is invoked with a
# fixed executable and argument list, never a shell string.

ALLOWED_RATES = (16000, 24000)
FFMPEG_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_ms: int


def sniff_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def validate_wav(data: bytes, settings: Settings) -> WavInfo:
    if not sniff_wav(data):
        raise ApiError("invalid_file", "The recording is not a WAV file.", 400,
                       "Use the in-app recorder, or upload a 16 kHz mono 16-bit PCM WAV.")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            info = WavInfo(
                sample_rate=handle.getframerate(), channels=handle.getnchannels(), sample_width=handle.getsampwidth(),
                frames=handle.getnframes(), duration_ms=0,
            )
    except (wave.Error, EOFError) as exc:
        raise ApiError("invalid_file", "The WAV file could not be read.", 400,
                       "Re-export the recording as 16 kHz mono 16-bit PCM WAV.") from exc
    if info.channels != 1 or info.sample_width != 2 or info.sample_rate not in ALLOWED_RATES:
        raise ApiError("invalid_file", "The WAV must be mono, 16-bit PCM, at 16 kHz or 24 kHz.", 400,
                       "The in-app recorder produces this format automatically.",
                       {"channels": info.channels, "sampleWidth": info.sample_width, "sampleRate": info.sample_rate})
    duration_ms = round(info.frames * 1000 / info.sample_rate)
    if duration_ms <= 0:
        raise ApiError("invalid_file", "The recording is empty.", 400)
    if duration_ms > settings.max_audio_seconds_per_update * 1000:
        raise ApiError("limit_exceeded", f"Recordings are limited to {settings.max_audio_seconds_per_update} seconds per update.", 413,
                       "Split a longer account into shorter recordings, or type it.")
    return WavInfo(info.sample_rate, info.channels, info.sample_width, info.frames, duration_ms)


def convert_with_ffmpeg(data: bytes) -> bytes:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise ApiError("invalid_file", "Only WAV recordings are accepted on this deployment (FFmpeg is not installed).", 400,
                       "Use the in-app recorder, which produces 16 kHz WAV.")
    args = [executable, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
    try:
        completed = subprocess.run(args, input=data, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise ApiError("invalid_file", "Audio conversion timed out.", 400, "Upload a shorter recording.") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise ApiError("invalid_file", "The audio file could not be converted.", 400, "Upload a WAV recording instead.")
    return completed.stdout


def prepare_audio(data: bytes, settings: Settings) -> tuple[bytes, WavInfo]:
    """Return PCM WAV bytes and their metadata, converting first only if configured and needed."""
    if len(data) == 0:
        raise ApiError("invalid_file", "The upload is empty.", 400)
    if not sniff_wav(data):
        if settings.audio_converter != "ffmpeg":
            raise ApiError("invalid_file", "Only WAV recordings are accepted on this deployment.", 400,
                           "Use the in-app recorder, which produces 16 kHz WAV, or convert the file to WAV first.")
        data = convert_with_ffmpeg(data)
    return data, validate_wav(data, settings)


# ---- from services/otp.py ------------------------------------------------------------------------
# One-time codes for the citizen sign-in.
#
# This is CyberBrief's own demo login. No SMS is sent, nothing is read from a bank, and it is
# never a code issued by the official cybercrime portal — this app never collects those
# (§13). In demo mode the generated code is shown on screen so the journey can be completed
# without an SMS provider; `Settings.validate_runtime` forces that off in production.
#
# The code itself is never stored and never logged. Only a salted scrypt hash is kept.

log = logging.getLogger("app.casework")


def _live_challenges(mobile: str):
    return select(OtpChallenge).where(
        OtpChallenge.mobile == mobile,
        OtpChallenge.consumed_at.is_(None),
        OtpChallenge.expires_at > utcnow(),
    )


def request_code(
    db: Session, settings: Settings, mobile: str, *, session_id: str | None
) -> tuple[OtpChallenge, str | None]:
    """Issue a code, superseding any live one. Returns the challenge and the visible code."""
    now = utcnow()

    recent = db.scalar(
        _live_challenges(mobile)
        .where(OtpChallenge.created_at > now - timedelta(seconds=settings.otp_resend_seconds))
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
    )
    if recent is not None:
        wait = settings.otp_resend_seconds - int((now - recent.created_at).total_seconds())
        raise ApiError(
            "rate_limited", "A code was just sent.", 429,
            f"Wait {max(wait, 1)} seconds before asking for another.",
            {"retryAfterSeconds": max(wait, 1)},
        )

    hourly = db.scalar(
        select(func.count(OtpChallenge.id)).where(
            OtpChallenge.mobile == mobile,
            OtpChallenge.created_at > now - timedelta(hours=1),
        )
    ) or 0
    if hourly >= settings.otp_hourly_limit:
        raise ApiError(
            "rate_limited", "Too many codes requested for this number.", 429,
            "Try again in an hour.",
        )

    # an older code must stop working the moment a new one is issued
    db.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.mobile == mobile,
            OtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    code = f"{secrets.randbelow(10**6):06d}"
    challenge = OtpChallenge(
        mobile=mobile,
        code_hash=hash_secret(code),
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
        requested_by_session_id=session_id,
    )
    db.add(challenge)
    db.flush()

    visible = code if (settings.demo_mode and settings.demo_otp_visible) else None
    if visible is None:
        # Nothing can deliver the code, so issuing one would strand the user.
        raise ApiError(
            "otp_unavailable", "One-time codes cannot be delivered in this deployment.", 503,
            "No SMS provider is configured.",
        )
    return challenge, visible


def verify_code(db: Session, settings: Settings, mobile: str, code: str) -> None:
    """Consume the newest live code for `mobile`, or raise. Constant-time on every path."""
    challenge = db.scalar(_live_challenges(mobile).order_by(OtpChallenge.created_at.desc()).limit(1))

    if challenge is None:
        # Spend the same work as a real check so an unknown number is not detectable by timing.
        verify_secret(code, DUMMY_HASH)
        raise ApiError(
            "invalid_code", "That code is not valid.", 401, "Ask for a new code and try again."
        )

    if challenge.attempts >= settings.otp_max_attempts:
        challenge.consumed_at = utcnow()
        db.commit()
        raise ApiError("rate_limited", "Too many attempts.", 429, "Ask for a new code.")

    if not verify_secret(code, challenge.code_hash):
        challenge.attempts += 1
        remaining = settings.otp_max_attempts - challenge.attempts
        if remaining <= 0:
            challenge.consumed_at = utcnow()
        db.commit()  # the increment must survive the error response
        if remaining <= 0:
            raise ApiError("rate_limited", "Too many attempts.", 429, "Ask for a new code.")
        raise ApiError(
            "invalid_code", "That code is not valid.", 401,
            f"{remaining} attempt(s) left before you need a new code.",
        )

    challenge.consumed_at = utcnow()
    db.flush()


# ---- from services/finalize.py -------------------------------------------------------------------
# Common tail of every mutation: refresh derived questions and stamp accounting on the revision.

def finalize_mutation(db: Session, case: Case, revision: int) -> None:
    sync_questions(db, case, revision)
    stamp_revision_totals(db, case)


# ---- from services/changes.py --------------------------------------------------------------------
# Change summary since a revision, computed in code from revision-stamped rows.

def totals_now(db: Session, case: Case) -> dict:
    from .accounting import case_recovery, compute_totals
    from .records import current_facts

    _, views, _, _ = payment_views(db, case)
    totals = compute_totals(views)
    recovery = case_recovery(db, case, views, totals)
    reported = current_facts(db, case.id, "case", case.id).get("citizen_reported_amount")
    return {
        "reviewedDebitMinor": str(totals.reviewed_debit_minor),
        "pendingMinor": str(totals.pending_minor),
        "matchedRefundMinor": str(recovery.matched_refund_minor),
        "outstandingMinor": str(recovery.outstanding_minor),
        "recoveryState": recovery.state,
        "citizenReportedMinor": reported.value.get("amountMinor") if reported and reported.value else None,
        "unreviewedCount": len(totals.unreviewed_ids) + len(totals.unresolved_ids) + len(totals.duplicate_candidate_ids) + len(totals.conflict_ids),
    }


def stamp_revision_totals(db: Session, case: Case) -> None:
    """Record the accounting snapshot on the current revision row so later diffs can show balance changes."""
    row = db.scalar(select(CaseRevision).where(CaseRevision.case_id == case.id, CaseRevision.revision == case.current_revision))
    if row is not None:
        row.totals_json = totals_now(db, case)


def _fact_brief(fact: Fact) -> dict:
    return {"factId": fact.fact_id, "version": fact.version, "subjectType": fact.subject_type, "subjectId": fact.subject_id,
            "kind": fact.kind, "value": fact.value, "rawText": fact.raw_text, "reviewState": fact.review_state,
            "issue": fact.issue, "reviewedBy": fact.reviewed_by, "reason": fact.reason, "revision": fact.created_revision}


def compute_changes(db: Session, case: Case, since: int) -> dict:
    since = max(0, min(since, case.current_revision))
    then_row = db.scalar(select(CaseRevision).where(CaseRevision.case_id == case.id, CaseRevision.revision == since)) if since > 0 else None
    sources = db.scalars(select(Source).where(Source.case_id == case.id, Source.added_in_revision > since).order_by(Source.received_at)).all()
    payments = db.scalars(select(Payment).where(Payment.case_id == case.id, Payment.created_revision > since)).all()
    refunds = db.scalars(select(Refund).where(Refund.case_id == case.id, Refund.created_revision > since)).all()
    facts = db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.created_revision > since).order_by(Fact.created_at)).all()
    new_subjects = {p.id for p in payments} | {r.id for r in refunds}
    added_facts = [f for f in facts if f.version == 1 and f.subject_id not in new_subjects]
    changed_facts = [f for f in facts if f.version > 1]
    allocations = db.scalars(select(RefundAllocation).where(RefundAllocation.case_id == case.id)).all()
    questions = db.scalars(select(Question).where(Question.case_id == case.id)).all()
    statuses = db.scalars(select(OfficialStatusEvent).where(OfficialStatusEvent.case_id == case.id, OfficialStatusEvent.created_revision > since)).all()
    resolutions = db.scalars(select(ResolutionReview).where(ResolutionReview.case_id == case.id, ResolutionReview.revision > since)).all()
    revisions = db.scalars(select(CaseRevision).where(CaseRevision.case_id == case.id, CaseRevision.revision > since).order_by(CaseRevision.revision)).all()

    from .accounting import find_duplicate_candidates

    _, views, _, _ = payment_views(db, case)
    new_payment_ids = {p.id for p in payments}
    new_conflicts = [{"paymentIds": list(c.payment_ids), "issues": list(c.issues)}
                     for c in find_duplicate_candidates(views) if any(pid in new_payment_ids for pid in c.payment_ids)]

    now = totals_now(db, case)
    then = then_row.totals_json if then_row is not None else None
    balance_changes = []
    if then:
        for key in ("reviewedDebitMinor", "pendingMinor", "matchedRefundMinor", "outstandingMinor"):
            if then.get(key) != now.get(key):
                balance_changes.append({"field": key, "from": then.get(key), "to": now.get(key)})
        if then.get("recoveryState") != now.get("recoveryState"):
            balance_changes.append({"field": "recoveryState", "from": then.get("recoveryState"), "to": now.get("recoveryState")})

    return {
        "sinceRevision": since,
        "currentRevision": case.current_revision,
        "sourcesAdded": [{"id": s.id, "kind": s.kind, "displayFilename": s.display_filename, "revision": s.added_in_revision} for s in sources],
        "paymentsAdded": [{"id": p.id, "label": p.label, "revision": p.created_revision} for p in payments],
        "refundsAdded": [{"id": r.id, "label": r.label, "evidenceKind": r.evidence_kind, "revision": r.created_revision} for r in refunds],
        "factsAdded": [_fact_brief(f) for f in added_facts],
        "factsChanged": [_fact_brief(f) for f in changed_facts],
        "newConflicts": new_conflicts,
        "allocationsAdded": [{"id": a.id, "refundId": a.refund_id, "paymentId": a.payment_id, "amountMinor": str(a.amount_minor), "state": a.state}
                             for a in allocations if a.created_revision > since],
        "allocationsInvalidated": [{"id": a.id, "refundId": a.refund_id, "paymentId": a.payment_id, "reason": a.reason}
                                   for a in allocations if a.invalidated_revision is not None and a.invalidated_revision > since],
        "questionsOpened": [{"id": q.id, "kind": q.kind, "text": q.text} for q in questions if q.created_revision > since],
        "questionsResolved": [{"id": q.id, "kind": q.kind, "state": q.state} for q in questions if q.resolved_revision is not None and q.resolved_revision > since],
        "officialStatusAdded": [{"id": e.id, "rawStatus": e.raw_status, "category": e.category, "reviewState": e.review_state, "revision": e.created_revision} for e in statuses],
        "resolutions": [{"id": r.id, "kind": r.kind, "eligible": r.eligible, "reviewOutcome": r.review_outcome, "revision": r.revision} for r in resolutions],
        "balancesThen": then,
        "balancesNow": now,
        "balanceChanges": balance_changes,
        "revisions": [{"revision": r.revision, "kind": r.kind, "summary": r.summary, "actorRole": r.actor_role, "createdAt": r.created_at.isoformat()} for r in revisions],
    }


# ---- from services/exports.py --------------------------------------------------------------------
# Revision-pinned exports: printable HTML (offline-readable, with evidence thumbnails) and JSON.

GENERATION_VERSION = "1"
_TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(default=True, default_for_string=True))


def _money(value: str | None) -> str:
    if value is None or value == "":
        return "not stated"
    try:
        return format_inr(int(value))
    except ValueError:
        return "unreadable"


def _fact_text(fact: FactOut | None) -> str:
    if fact is None:
        return "—"
    if fact.kind in ("amount", "citizen_reported_amount") and fact.value and fact.value.get("amountMinor"):
        text = _money(fact.value["amountMinor"])
    elif fact.kind == "occurred_at" and fact.value:
        text = fact.value.get("instantUtc") or fact.value.get("localDatetime") or fact.value.get("date") or fact.raw_text or "—"
    elif fact.value and "value" in fact.value and fact.value["value"] is not None:
        text = str(fact.value["value"])
    elif fact.value and "code" in fact.value:
        text = str(fact.value["code"])
    else:
        text = fact.raw_text or "—"
    if fact.issue:
        text += f" [{fact.issue}]"
    return text


def _review_summary(payment: PaymentOut) -> str:
    states = {fact.review_state for fact in payment.fields.values()}
    parts = [f"identity: {payment.identity_review_state}"]
    parts.append("fields: " + ", ".join(sorted(states)) if states else "fields: none")
    return "; ".join(parts)


def evidence_index(db: Session, storage: Storage, case: Case, with_thumbnails: bool) -> list[dict]:
    sources = db.scalars(select(Source).where(Source.case_id == case.id).order_by(Source.received_at)).all()
    thumbs = {a.source_id: a for a in db.scalars(select(DerivedAsset).where(DerivedAsset.kind == "thumbnail",
                                                                              DerivedAsset.source_id.in_([s.id for s in sources]))).all()} if sources else {}
    rows = []
    for source in sources:
        row = {
            "sourceId": source.id, "shortId": source.id[:8], "filename": source.display_filename, "kind": source.kind,
            "mediaType": source.media_type, "sha256": source.sha256, "byteSize": source.byte_size,
            "receivedAt": source.received_at.isoformat(), "provenanceKind": source.provenance_kind,
            "textExcerpt": (source.text_content or (source.meta or {}).get("visibleText") or "")[:600],
            "duplicateOfSourceId": source.duplicate_of_source_id, "thumbnailDataUri": None,
        }
        if with_thumbnails and source.id in thumbs:
            try:
                data = storage.read(thumbs[source.id].storage_key)
                row["thumbnailDataUri"] = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
            except Exception:  # noqa: BLE001 - a missing thumbnail must not block the export
                row["thumbnailDataUri"] = None
        rows.append(row)
    return rows


def create_export(db: Session, storage: Storage, case: Case, session: UserSession, fmt: str,
                  expected_revision: int | None) -> ExportSnapshot:
    check_expected_revision(case, expected_revision)
    snapshot = build_snapshot(db, case)
    generated_at = utcnow()
    labels = ["demo", "fictional_data", "ai_assisted", *[f"extraction_{m}" for m in snapshot.ai_modes_used]]
    if fmt == "json":
        body = {
            "generator": {"name": "CyberBrief", "generationVersion": GENERATION_VERSION, "generatedAt": generated_at.isoformat(),
                          "caseRevision": case.current_revision, "labels": labels,
                          "note": "Metadata and reviewed facts only. Original files are not embedded."},
            "snapshot": snapshot.model_dump(by_alias=True, mode="json"),
            "evidenceIndex": evidence_index(db, storage, case, with_thumbnails=False),
        }
        data = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")
    else:
        template = _env.get_template("report.html.j2")
        labels_by_id = {**{p.id: p.label for p in snapshot.payments}, **{r.id: r.label for r in snapshot.refunds}}
        html = template.render(
            case=snapshot.case, sources=snapshot.sources, payments=snapshot.payments, totals=snapshot.totals,
            case_facts=snapshot.case_facts, candidates=snapshot.duplicate_candidates, runs=snapshot.runs,
            revisions=snapshot.revisions, events=snapshot.review_events, ai_modes_used=snapshot.ai_modes_used,
            questions=[q for q in snapshot.questions if q.state in ("open", "answered")],
            refunds=snapshot.refunds, allocations=snapshot.allocations, recovery=snapshot.recovery,
            status_events=snapshot.official_status_events, latest_status=snapshot.latest_official_status,
            resolutions=snapshot.resolutions,
            evidence=evidence_index(db, storage, case, with_thumbnails=True), labels=labels,
            generated_at=generated_at.isoformat(timespec="seconds"), generation_version=GENERATION_VERSION,
            money=_money, fact_text=_fact_text, review_summary=_review_summary,
            short=lambda value: (value or "")[:8],
            label_of=labels_by_id.get,
        )
        data = html.encode("utf-8")
    export = ExportSnapshot(case_id=case.id, revision=case.current_revision, format=fmt,
                            content_digest=sha256_bytes(data), storage_key="", generation_version=GENERATION_VERSION,
                            ai_mode=",".join(snapshot.ai_modes_used) or "none", generated_at=generated_at)
    db.add(export)
    db.flush()
    export.storage_key = storage.store_export(case.id, export.id, fmt, data)
    return export


def export_out(export: ExportSnapshot) -> ExportOut:
    return ExportOut(id=export.id, revision=export.revision, format=export.format, content_digest=export.content_digest,
                     ai_mode=export.ai_mode, generated_at=export.generated_at,
                     download_path=f"/api/cases/{export.case_id}/exports/{export.id}")


# ---- from services/reviews.py --------------------------------------------------------------------
# Human review: confirm / correct / unresolved / exclude facts; confirm, exclude, merge payments.
#
# Every accepted action creates a new fact version or payment state inside one transaction with a
# revision bump and a review event. A stale `expectedRevision` is rejected with 409.

MONEY_CRITICAL = ("amount", "currency", "status", "direction")

VALID_STATUS = ("successful", "pending", "failed")
VALID_DIRECTION = ("debit", "credit")
VALID_REFERENCE_TYPES = ("upi_ref", "utr", "transaction_id", "order_id", "other")


def _confirmed_state(session: UserSession) -> str:
    return "reviewer_checked" if session.role == "officer" else "user_confirmed"


def _corrected_value(kind: str, raw: str) -> tuple[dict, str]:
    raw = raw.strip()
    if not raw:
        raise ApiError("invalid_request", "A corrected value is required.", 400)
    try:
        if kind == "amount":
            money = parse_amount(raw)
            return {"amountMinor": str(money.amount_minor)}, raw
        if kind == "citizen_reported_amount":
            money = parse_amount(raw)
            return {"amountMinor": str(money.amount_minor), "currency": money.currency}, raw
    except MoneyParseError as exc:
        raise ApiError("invalid_request", f"Amount could not be parsed ({exc.code}).", 400,
                       "Enter the amount as shown, for example INR 10,000.00.") from exc
    if kind == "currency":
        code = raw.upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ApiError("invalid_request", "Currency must be a three-letter code such as INR.", 400)
        return {"code": code}, raw
    if kind == "reference":
        return {"value": raw, "truncated": False}, raw
    if kind == "reference_type":
        if raw not in VALID_REFERENCE_TYPES:
            raise ApiError("invalid_request", f"Reference type must be one of {', '.join(VALID_REFERENCE_TYPES)}.", 400)
        return {"value": raw}, raw
    if kind == "status":
        if raw not in VALID_STATUS:
            raise ApiError("invalid_request", f"Status must be one of {', '.join(VALID_STATUS)}.", 400)
        return {"value": raw}, raw
    if kind == "direction":
        if raw not in VALID_DIRECTION:
            raise ApiError("invalid_request", "Direction must be debit or credit.", 400)
        return {"value": raw}, raw
    if kind == "occurred_at":
        parsed = parse_date_text(raw)
        if parsed.kind == "unresolved":
            raise ApiError("invalid_request", f"Date could not be resolved ({parsed.issue}).", 400,
                           "Use an unambiguous form such as 12 Oct 2026 or 2026-10-12.")
        return parsed.to_json(), raw
    if kind in ("account_scope", "counterparty"):
        return {"value": raw}, raw
    raise ApiError("invalid_request", f"Field {kind!r} cannot be corrected directly.", 400)


def _apply_fact_action(db: Session, case: Case, session: UserSession, action: ReviewAction, revision: int) -> dict:
    fact = current_fact(db, action.target_id)
    if fact is None or fact.case_id != case.id:
        raise ApiError("not_found", "Review target not found in this case.", 404)
    confirmed = _confirmed_state(session)
    if action.type == "confirm":
        if fact.value is None:
            raise ApiError("review_conflict", "This field has no value to confirm.", 409,
                           "Correct the value or mark it unresolved.")
        nxt = supersede_fact(db, fact, revision=revision, review_state=confirmed, actor_role=session.role,
                             issue=None, reason=action.reason)
    elif action.type == "correct":
        raw = action.raw_text or (str(action.value.get("value")) if action.value and "value" in action.value else "")
        value, raw_text = _corrected_value(fact.kind, raw)
        anchors = [*(fact.anchors or [])]
        for anchor in anchors:
            anchor["notes"] = sorted({*anchor.get("notes", []), "corrected_by_" + session.role})
        nxt = supersede_fact(db, fact, revision=revision, review_state=confirmed, actor_role=session.role,
                             value=value, raw_text=raw_text, anchors=anchors, issue=None,
                             reason=action.reason or "corrected", keep_value=False)
    elif action.type == "unresolved":
        nxt = supersede_fact(db, fact, revision=revision, review_state="unresolved", actor_role=session.role,
                             issue=fact.issue or "ambiguous", reason=action.reason)
    elif action.type == "exclude":
        if not action.reason:
            raise ApiError("invalid_request", "A reason is required to exclude a field.", 400)
        nxt = supersede_fact(db, fact, revision=revision, review_state="excluded", actor_role=session.role,
                             issue=fact.issue, reason=action.reason)
    else:
        raise ApiError("invalid_request", f"Action {action.type!r} does not apply to a fact.", 400)
    invalidated = 0
    if action.type != "confirm" and fact.kind in MONEY_CRITICAL and fact.subject_type in ("payment", "refund"):
        key = {"payment": "payment_id", "refund": "refund_id"}[fact.subject_type]
        invalidated = invalidate_allocations(db, case, revision, **{key: fact.subject_id},
                                             reason=f"{fact.subject_type} {fact.kind} changed by {action.type}")
    return {"factId": nxt.fact_id, "fromVersion": fact.version, "toVersion": nxt.version,
            "reviewState": nxt.review_state, "reason": action.reason, "allocationsInvalidated": invalidated}


def _payment(db: Session, case: Case, payment_id: str | None) -> Payment:
    payment = db.get(Payment, payment_id) if payment_id else None
    if payment is None or payment.case_id != case.id:
        raise ApiError("not_found", "Payment not found in this case.", 404)
    return payment


def _apply_payment_action(db: Session, case: Case, session: UserSession, action: ReviewAction, revision: int) -> dict:
    payment = _payment(db, case, action.target_id)
    confirmed = _confirmed_state(session)
    if action.type in ("exclude", "merge", "unmerge"):
        invalidate_allocations(db, case, revision, payment_id=payment.id, reason=f"payment {action.type}")
    if action.type == "confirm":
        payment.identity_review_state = confirmed
    elif action.type == "exclude":
        if not action.reason:
            raise ApiError("invalid_request", "A reason is required to exclude a payment.", 400)
        payment.excluded_reason = action.reason
        payment.identity_review_state = "excluded"
    elif action.type == "include":
        payment.excluded_reason = None
        payment.included_in_incident = True
        payment.identity_review_state = confirmed
    elif action.type == "merge":
        other = _payment(db, case, action.other_payment_id)
        if other.id == payment.id or payment.merged_into_payment_id or other.merged_into_payment_id:
            raise ApiError("review_conflict", "These payments cannot be merged as requested.", 409,
                           "Unmerge first, or choose a different canonical payment.")
        payment.merged_into_payment_id = other.id
        payment.identity_review_state = confirmed
        other.identity_review_state = confirmed
    elif action.type == "unmerge":
        if not payment.merged_into_payment_id:
            raise ApiError("review_conflict", "This payment is not merged.", 409)
        payment.merged_into_payment_id = None
        payment.identity_review_state = "unreviewed"
    elif action.type == "distinct":
        other = _payment(db, case, action.other_payment_id)
        payment.confirmed_distinct_from = sorted({*(payment.confirmed_distinct_from or []), other.id})
        other.confirmed_distinct_from = sorted({*(other.confirmed_distinct_from or []), payment.id})
    else:
        raise ApiError("invalid_request", f"Action {action.type!r} does not apply to a payment.", 400)
    db.flush()
    return {"paymentId": payment.id, "identityReviewState": payment.identity_review_state,
            "mergedInto": payment.merged_into_payment_id, "reason": action.reason,
            "otherPaymentId": action.other_payment_id}


def _refund(db: Session, case: Case, refund_id: str | None) -> Refund:
    refund = db.get(Refund, refund_id) if refund_id else None
    if refund is None or refund.case_id != case.id:
        raise ApiError("not_found", "Refund not found in this case.", 404)
    return refund


def _apply_refund_action(db: Session, case: Case, session: UserSession, action: ReviewAction, revision: int) -> dict:
    refund = _refund(db, case, action.target_id)
    confirmed = _confirmed_state(session)
    if action.type in ("exclude", "merge", "unmerge", "unrelated"):
        invalidate_allocations(db, case, revision, refund_id=refund.id, reason=f"refund {action.type}")
    if action.type == "confirm":
        refund.identity_review_state = confirmed
    elif action.type == "relates":
        refund.relates_to_incident = "confirmed"
    elif action.type == "unrelated":
        refund.relates_to_incident = "denied"
        refund.excluded_reason = action.reason or "not related to this incident"
        refund.identity_review_state = "excluded"
    elif action.type == "exclude":
        if not action.reason:
            raise ApiError("invalid_request", "A reason is required to exclude a refund (for example: bank reversed the credit).", 400)
        refund.excluded_reason = action.reason
        refund.identity_review_state = "excluded"
    elif action.type == "include":
        refund.excluded_reason = None
        refund.identity_review_state = confirmed
        if refund.relates_to_incident == "denied":
            refund.relates_to_incident = "unconfirmed"
    elif action.type == "merge":
        other = _refund(db, case, action.other_payment_id)
        if other.id == refund.id or refund.merged_into_refund_id or other.merged_into_refund_id:
            raise ApiError("review_conflict", "These refund records cannot be merged as requested.", 409)
        refund.merged_into_refund_id = other.id
        refund.identity_review_state = confirmed
        other.identity_review_state = confirmed
    elif action.type == "unmerge":
        if not refund.merged_into_refund_id:
            raise ApiError("review_conflict", "This refund is not merged.", 409)
        refund.merged_into_refund_id = None
        refund.identity_review_state = "unreviewed"
    elif action.type == "distinct":
        other = _refund(db, case, action.other_payment_id)
        refund.confirmed_distinct_from = sorted({*(refund.confirmed_distinct_from or []), other.id})
        other.confirmed_distinct_from = sorted({*(other.confirmed_distinct_from or []), refund.id})
    else:
        raise ApiError("invalid_request", f"Action {action.type!r} does not apply to a refund.", 400)
    db.flush()
    return {"refundId": refund.id, "identityReviewState": refund.identity_review_state, "relatesToIncident": refund.relates_to_incident,
            "mergedInto": refund.merged_into_refund_id, "reason": action.reason, "otherRefundId": action.other_payment_id}


def _apply_status_action(db: Session, case: Case, session: UserSession, action: ReviewAction) -> dict:
    event = db.get(OfficialStatusEvent, action.target_id)
    if event is None or event.case_id != case.id:
        raise ApiError("not_found", "Official status record not found in this case.", 404)
    if action.type == "confirm":
        event.review_state = _confirmed_state(session)
    elif action.type == "exclude":
        if not action.reason:
            raise ApiError("invalid_request", "A reason is required to exclude a status record.", 400)
        event.review_state = "excluded"
    else:
        raise ApiError("invalid_request", f"Action {action.type!r} does not apply to an official status record.", 400)
    db.flush()
    return {"eventId": event.id, "reviewState": event.review_state, "reason": action.reason}


def apply_review_batch(db: Session, case: Case, session: UserSession, batch: ReviewBatch) -> int:
    check_expected_revision(case, batch.expected_revision)
    revision = bump_revision(db, case, kind="review", summary=f"{len(batch.actions)} review action(s) by {session.role}",
                             actor_session_id=session.id, actor_role=session.role)
    for action in batch.actions:
        if action.target_type == "fact":
            payload = _apply_fact_action(db, case, session, action, revision)
        elif action.target_type == "payment":
            payload = _apply_payment_action(db, case, session, action, revision)
        elif action.target_type == "refund":
            payload = _apply_refund_action(db, case, session, action, revision)
        else:
            payload = _apply_status_action(db, case, session, action)
        record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id,
                            actor_role=session.role, action=action.type, target_type=action.target_type,
                            target_id=action.target_id, payload=payload)
    db.flush()
    _, views, _, _ = payment_views(db, case)
    totals = compute_totals(views)
    pending = totals.unreviewed_ids or totals.unresolved_ids or totals.duplicate_candidate_ids or totals.conflict_ids
    case.packet_status = "needs_review" if pending else "ready_for_export"

    finalize_mutation(db, case, revision)
    return revision


# ---- from services/cases.py ----------------------------------------------------------------------
# Case creation, source registration and the read-side snapshot.

TEXT_KINDS = ("typed_account", "user_answer")


# ---- writes -------------------------------------------------------------------------------
def owned_cases(session: UserSession):
    """Rows this session may see: by account when signed in, by cookie when anonymous."""
    if session.account_id:
        return select(Case).where(Case.account_id == session.account_id)
    return select(Case).where(Case.session_id == session.id, Case.account_id.is_(None))


def create_case(db: Session, session: UserSession, settings: Settings, storage: Storage, payload: CaseCreate) -> Case:
    count = db.scalar(
        owned_cases(session).with_only_columns(func.count(Case.id)).order_by(None)
    ) or 0
    if count >= settings.session_case_quota:
        raise ApiError("limit_exceeded", "This demo session has reached its case quota.", 400,
                       "Delete an old case or start a new session.")
    case = Case(session_id=session.id, account_id=session.account_id,
                title=payload.title.strip(), language=payload.language,
                complaint_reference=(payload.complaint_reference or "").strip() or None)
    db.add(case)
    db.flush()
    revision = bump_revision(db, case, kind="created", summary="Case created", actor_session_id=session.id,
                             actor_role=session.role)
    if payload.incident_account and payload.incident_account.strip():
        add_text_source(db, storage, case, payload.incident_account.strip(), kind="typed_account",
                        display_filename="Typed account", revision=revision, settings=settings)
    return case


def add_text_source(db: Session, storage: Storage, case: Case, text: str, *, kind: str, display_filename: str,
                    revision: int, settings: Settings) -> Source:
    if kind not in TEXT_KINDS:
        raise ApiError("invalid_request", f"Unsupported text source kind {kind!r}.", 400)
    if len(text) > settings.max_typed_chars:
        raise ApiError("limit_exceeded", f"Typed text is limited to {settings.max_typed_chars} characters.", 413)
    key, digest, size = storage.store_text(case.id, text, kind)
    duplicate = db.scalar(select(Source.id).where(Source.case_id == case.id, Source.sha256 == digest))
    source = Source(case_id=case.id, kind=kind, provenance_kind="typed" if kind == "typed_account" else "answer",
                    media_type="text/plain; charset=utf-8", storage_key=key, sha256=digest, byte_size=size,
                    display_filename=display_filename, text_content=text, status="stored",
                    added_in_revision=revision, duplicate_of_source_id=duplicate)
    db.add(source)
    db.flush()
    return source


def add_image_source(db: Session, storage: Storage, case: Case, data: bytes, declared_type: str | None,
                     filename: str | None, *, revision: int, settings: Settings) -> Source:
    images = db.scalar(select(func.count(Source.id)).where(Source.case_id == case.id, Source.kind == "image")) or 0
    if images >= settings.max_images_per_case:
        raise ApiError("limit_exceeded", f"A case holds at most {settings.max_images_per_case} images.", 413,
                       "Remove an image you no longer need, or start a separate case.")
    stored = storage.store_image(case.id, data, declared_type)
    duplicate = db.scalar(select(Source.id).where(Source.case_id == case.id, Source.sha256 == stored.sha256))
    source = Source(case_id=case.id, kind="image", provenance_kind="citizen_upload", media_type=stored.media_type,
                    storage_key=stored.storage_key, sha256=stored.sha256, byte_size=stored.byte_size,
                    display_filename=sanitise_filename(filename, "image"), image_width=stored.width,
                    image_height=stored.height, status="stored", added_in_revision=revision,
                    duplicate_of_source_id=duplicate)
    db.add(source)
    db.flush()
    if stored.thumbnail_key:
        db.add(DerivedAsset(source_id=source.id, kind="thumbnail", media_type="image/jpeg",
                            storage_key=stored.thumbnail_key))
        db.flush()
    return source


def add_audio_source(db: Session, storage: Storage, case: Case, data: bytes, filename: str | None, *,
                     revision: int, settings: Settings) -> Source:
    """Store a recorded account (PCM WAV). Caps: recordings per case and total seconds per case."""

    existing = db.scalars(select(Source).where(Source.case_id == case.id, Source.kind == "audio")).all()
    if len(existing) >= settings.max_audio_recordings_per_case:
        raise ApiError("limit_exceeded", f"A case holds at most {settings.max_audio_recordings_per_case} recordings.", 413,
                       "Type any further account instead.")
    wav, info = prepare_audio(data, settings)
    total_ms = sum(s.audio_duration_ms or 0 for s in existing) + info.duration_ms
    if total_ms > settings.max_audio_seconds_per_case * 1000:
        raise ApiError("limit_exceeded", f"Recordings in a case are limited to {settings.max_audio_seconds_per_case} seconds in total.", 413)
    key, digest, size = storage.store_audio(case.id, wav)
    duplicate = db.scalar(select(Source.id).where(Source.case_id == case.id, Source.sha256 == digest))
    source = Source(case_id=case.id, kind="audio", provenance_kind="citizen_upload", media_type="audio/wav", storage_key=key,
                    sha256=digest, byte_size=size, display_filename=sanitise_filename(filename, "recording.wav"),
                    audio_duration_ms=info.duration_ms, status="stored", added_in_revision=revision, duplicate_of_source_id=duplicate,
                    meta={"sampleRate": info.sample_rate})
    db.add(source)
    db.flush()
    return source


def add_transcript_correction(db: Session, storage: Storage, case: Case, original: Source, text: str, *, revision: int,
                              settings: Settings, actor_role: str) -> Source:
    """A corrected transcript is a new derived source; the original transcript and audio are preserved."""
    if original.kind != "transcript":
        raise ApiError("invalid_request", "Only transcripts can be corrected this way.", 400)
    if len(text) > settings.max_typed_chars:
        raise ApiError("limit_exceeded", f"Transcripts are limited to {settings.max_typed_chars} characters.", 413)
    key, digest, size = storage.store_text(case.id, text, "transcript")
    meta = original.meta or {}
    source = Source(case_id=case.id, kind="transcript", provenance_kind="user_corrected", media_type="text/plain; charset=utf-8",
                    storage_key=key, sha256=digest, byte_size=size, display_filename=f"Corrected {original.display_filename}",
                    text_content=text, status="stored", added_in_revision=revision,
                    meta={"audioSourceId": meta.get("audioSourceId"), "supersedesSourceId": original.id, "correctedBy": actor_role,
                          "audioDurationMs": meta.get("audioDurationMs")})
    db.add(source)
    db.flush()
    original.meta = {**meta, "supersededBySourceId": source.id}
    return source


# ---- reads --------------------------------------------------------------------------------
def _value(facts: dict[str, Fact], kind: str, key: str = "value"):
    fact = facts.get(kind)
    if fact is None or not fact.value:
        return None
    return fact.value.get(key)


def payment_views(db: Session, case: Case) -> tuple[list[Payment], list[PaymentView], dict[str, dict[str, Fact]], dict[str, list[PaymentObservation]]]:
    payments = db.scalars(select(Payment).where(Payment.case_id == case.id).order_by(Payment.created_at, Payment.label)).all()
    facts_by: dict[str, dict[str, Fact]] = {}
    for fact in db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.subject_type == "payment", Fact.is_current.is_(True))).all():
        facts_by.setdefault(fact.subject_id, {})[fact.kind] = fact
    links_by: dict[str, list[PaymentObservation]] = {}
    for link in db.scalars(select(PaymentObservation).where(PaymentObservation.case_id == case.id)).all():
        links_by.setdefault(link.payment_id, []).append(link)

    views: list[PaymentView] = []
    for p in payments:
        f = facts_by.get(p.id, {})
        amount_minor = _value(f, "amount", "amountMinor")
        views.append(PaymentView(
            id=p.id, case_id=case.id,
            amount_minor=int(amount_minor) if amount_minor else None,
            currency=_value(f, "currency", "code"),
            reference=_value(f, "reference"),
            reference_type=_value(f, "reference_type"),
            account_scope=_value(f, "account_scope"),
            direction=_value(f, "direction") or "unknown",
            status=_value(f, "status") or "unknown",
            included_in_incident=p.included_in_incident,
            field_review={k: (f[k].review_state if k in f else "unreviewed") for k in CRITICAL_FIELDS},
            identity_review_state=p.identity_review_state,
            merged_into=p.merged_into_payment_id,
            excluded_reason=p.excluded_reason,
            reference_truncated=bool(_value(f, "reference", "truncated")),
            confirmed_distinct_from=set(p.confirmed_distinct_from or []),
        ))
    return payments, views, facts_by, links_by


def fact_out(fact: Fact) -> FactOut:
    return FactOut(
        fact_id=fact.fact_id, version=fact.version, kind=fact.kind, value=fact.value, raw_text=fact.raw_text,
        review_state=fact.review_state, issue=fact.issue,
        anchors=[AnchorOut.model_validate(a) for a in (fact.anchors or [])],
        reviewed_by=fact.reviewed_by, reviewed_at=fact.reviewed_at, reason=fact.reason,
        supersedes_version=fact.supersedes_version, source_observation_id=fact.source_observation_id,
    )


def source_out(source: Source, has_thumbnail: bool) -> SourceOut:
    meta = source.meta or {}
    return SourceOut(
        id=source.id, kind=source.kind, provenance_kind=source.provenance_kind, media_type=source.media_type,
        display_filename=source.display_filename, byte_size=source.byte_size, sha256=source.sha256,
        status=source.status, received_at=source.received_at, added_in_revision=source.added_in_revision,
        image_width=source.image_width, image_height=source.image_height, text_content=source.text_content,
        duplicate_of_source_id=source.duplicate_of_source_id, has_thumbnail=has_thumbnail,
        audio_duration_ms=source.audio_duration_ms or meta.get("audioDurationMs"),
        audio_source_id=meta.get("audioSourceId"), supersedes_source_id=meta.get("supersedesSourceId"),
        superseded_by_source_id=meta.get("supersededBySourceId"),
        transcript_turns=meta.get("turns"), transcription_mode=meta.get("aiMode"),
    )


def run_out(run: ProcessingRun) -> RunOut:
    return RunOut(
        id=run.id, status=run.status, stage=run.stage, base_revision=run.base_revision,
        input_source_ids=list(run.input_source_ids or []), ai_mode=run.ai_mode, model_id=run.model_id,
        prompt_version=run.prompt_version, schema_version=run.schema_version, attempts=run.attempts,
        usage=dict(run.usage or {}), per_source_status=dict(run.per_source_status or {}),
        error_type=run.error_type, error_message=run.error_message, created_at=run.created_at,
        started_at=run.started_at, finished_at=run.finished_at,
    )


def case_summary(db: Session, case: Case) -> CaseSummary:
    from .core import get_settings

    count = db.scalar(select(func.count(Source.id)).where(Source.case_id == case.id)) or 0
    closure_asked = db.scalar(
        select(func.count(ResolutionReview.id)).where(
            ResolutionReview.case_id == case.id,
            ResolutionReview.request_note.is_not(None),
            ResolutionReview.reviewed_at.is_(None),
        )
    ) or 0
    # The accounting snapshot is already denormalised onto the revision row by
    # stamp_revision_totals, so a list of cases costs one indexed read each rather than a full
    # payment_views recomputation. Stamping happens on the revisions that change money, not on
    # every bump, so take the most recent revision that actually carries totals.
    totals = db.scalar(
        select(CaseRevision.totals_json)
        .where(CaseRevision.case_id == case.id, CaseRevision.totals_json.is_not(None))
        .order_by(CaseRevision.revision.desc())
        .limit(1)
    ) or {}
    open_questions = db.scalar(
        select(func.count(Question.id)).where(Question.case_id == case.id, Question.state == "open")
    ) or 0
    return CaseSummary(
        citizen_reported_minor=totals.get("citizenReportedMinor"),
        reviewed_debit_minor=totals.get("reviewedDebitMinor"),
        open_question_count=open_questions,
        id=case.id, title=case.title, language=case.language, complaint_reference=case.complaint_reference,
        current_revision=case.current_revision, last_reviewed_revision=case.last_reviewed_revision or 0,
        packet_status=case.packet_status, active_run_id=case.active_run_id,
        created_at=case.created_at, updated_at=case.updated_at, source_count=count,
        demo_pack_id=case.demo_pack_id, demo_steps_remaining=demo_steps_remaining(get_settings(), case) if case.demo_pack_id else 0,
        review_state=case.review_state, submitted_at=case.submitted_at, closure_requested=closure_asked > 0,
    )


def list_cases(db: Session, session: UserSession) -> list[CaseSummary]:
    cases = db.scalars(owned_cases(session).order_by(Case.created_at.desc())).all()
    return [case_summary(db, c) for c in cases]


def build_snapshot(db: Session, case: Case) -> CaseSnapshot:
    sources = db.scalars(select(Source).where(Source.case_id == case.id).order_by(Source.received_at)).all()
    thumbs = {a.source_id for a in db.scalars(select(DerivedAsset).where(DerivedAsset.kind == "thumbnail",
                                                                           DerivedAsset.source_id.in_([s.id for s in sources]))).all()} if sources else set()
    payments, views, facts_by, links_by = payment_views(db, case)
    totals = compute_totals(views)
    candidates = find_duplicate_candidates(views)
    case_facts = current_facts(db, case.id, "case", case.id)
    reported = case_facts.get("citizen_reported_amount")
    reported_minor = reported.value.get("amountMinor") if reported and reported.value else None

    merged_children: dict[str, list[str]] = {}
    for p in payments:
        if p.merged_into_payment_id:
            merged_children.setdefault(p.merged_into_payment_id, []).append(p.id)

    payments_out: list[PaymentOut] = []
    for p in payments:
        own_links = links_by.get(p.id, [])
        child_links = [link for child in merged_children.get(p.id, []) for link in links_by.get(child, [])]
        all_links = own_links + child_links
        payments_out.append(PaymentOut(
            id=p.id, label=p.label, included_in_incident=p.included_in_incident,
            identity_review_state=p.identity_review_state, merged_into_payment_id=p.merged_into_payment_id,
            excluded_reason=p.excluded_reason, confirmed_distinct_from=list(p.confirmed_distinct_from or []),
            category=totals.categories.get(p.id, "unreviewed"), category_reason=totals.reasons.get(p.id),
            fields={kind: fact_out(f) for kind, f in facts_by.get(p.id, {}).items()},
            source_ids=sorted({link.source_id for link in all_links}),
            observation_ids=[link.observation_id for link in all_links],
        ))

    # ---- recovery track -------------------------------------------------------------------
    from .accounting import allocation_views, citizen_outstanding_minor, refund_views, select_latest, status_views
    from .models import OfficialStatusEvent, ResolutionReview

    refunds, rviews, rfacts_by = refund_views(db, case)
    allocations, aviews = allocation_views(db, case)
    outstanding_minor = citizen_outstanding_minor(db, case)
    recovery = compute_recovery(totals, views, rviews, aviews, citizen_outstanding_minor=outstanding_minor)
    statement = case_facts.get("citizen_outstanding_statement")
    issue_by_alloc = {i["allocationId"]: i["issue"] for i in recovery.allocation_issues}
    refunds_out = [RefundOut(
        id=r.id, label=r.label, kind=r.kind, evidence_kind=r.evidence_kind, relates_to_incident=r.relates_to_incident,
        identity_review_state=r.identity_review_state, merged_into_refund_id=r.merged_into_refund_id,
        excluded_reason=r.excluded_reason, confirmed_distinct_from=list(r.confirmed_distinct_from or []),
        category=recovery.refund_categories.get(r.id, "unreviewed"), category_reason=recovery.refund_reasons.get(r.id),
        fields={kind: fact_out(f) for kind, f in rfacts_by.get(r.id, {}).items()},
        source_ids=sorted({a.get("sourceId") for f in rfacts_by.get(r.id, {}).values() for a in (f.anchors or []) if a.get("sourceId")}),
    ) for r in refunds]
    allocations_out = [AllocationOut(
        id=a.id, refund_id=a.refund_id, payment_id=a.payment_id, amount_minor=str(a.amount_minor), state=a.state, reason=a.reason,
        created_by_role=a.created_by_role, created_revision=a.created_revision, invalidated_revision=a.invalidated_revision,
        counted=a.id in recovery.counted_allocation_ids, issue=issue_by_alloc.get(a.id),
    ) for a in allocations]
    recovery_json = recovery.to_json()
    recovery_out = RecoveryOut(
        state=recovery.state, reviewed_debit_minor=recovery_json["reviewedDebitMinor"], matched_refund_minor=recovery_json["matchedRefundMinor"],
        outstanding_minor=recovery_json["outstandingMinor"], eligible_refund_minor=recovery_json["eligibleRefundMinor"],
        unallocated_eligible_minor=recovery_json["unallocatedEligibleMinor"], claimed_refund_minor=recovery_json["claimedRefundMinor"],
        allocation_issues=recovery.allocation_issues, reasons=recovery.reasons,
        citizen_outstanding_minor=None if outstanding_minor is None else str(outstanding_minor),
        citizen_outstanding_text=statement.value.get("text") if statement and statement.value else None,
    )
    status_rows = db.scalars(select(OfficialStatusEvent).where(OfficialStatusEvent.case_id == case.id).order_by(OfficialStatusEvent.received_at)).all()
    status_out = {e.id: OfficialStatusEventOut(
        id=e.id, raw_status=e.raw_status, category=e.category, issuer_as_supplied=e.issuer_as_supplied, source_ids=list(e.source_ids or []),
        effective_at=e.effective_at, received_at=e.received_at, entered_by_role=e.entered_by_role, review_state=e.review_state,
        origin=e.origin, created_revision=e.created_revision) for e in status_rows}
    latest = select_latest(status_views(db, case))
    latest_out = LatestStatusOut(event=status_out.get(latest["event"].id) if latest["event"] else None, undated_count=latest["undatedCount"],
                                 conflict=latest["conflict"], qualification=latest["qualification"], message=latest["message"])
    resolutions = db.scalars(select(ResolutionReview).where(ResolutionReview.case_id == case.id).order_by(ResolutionReview.created_at.desc())).all()
    revision_rows = db.scalars(select(CaseRevision).where(CaseRevision.case_id == case.id).order_by(CaseRevision.revision)).all()

    def _stale(prepared_at: int) -> bool:
        # a summary goes stale when anything other than its own packet review happened after it was prepared
        return any(rv.kind != "packet_reviewed" for rv in revision_rows if rv.revision > prepared_at)

    resolutions_out = [ResolutionOut(
        id=r.id, revision=r.revision, kind=r.kind, recovery_state=r.recovery_state, summary_text=r.summary_text, eligible=r.eligible,
        blocking_reasons=list(r.blocking_reasons or []), prepared_by_role=r.prepared_by_role, request_note=r.request_note,
        reviewed_at=r.reviewed_at, reviewed_by_role=r.reviewed_by_role, review_outcome=r.review_outcome,
        review_note=r.review_note, stale=_stale(r.revision), created_at=r.created_at) for r in resolutions]

    questions = db.scalars(select(Question).where(Question.case_id == case.id).order_by(Question.priority, Question.created_at)).all()
    shown_ids = {q.id for q in [q for q in questions if q.state == "open"][:5]}
    questions_out = [QuestionOut(id=q.id, kind=q.kind, text=q.text, priority=q.priority, state=q.state,
                                 related_payment_ids=list(q.related_payment_ids or []), related_fact_ids=list(q.related_fact_ids or []),
                                 related_source_ids=list(q.related_source_ids or []), answer_source_id=q.answer_source_id,
                                 dismiss_reason=q.dismiss_reason, created_revision=q.created_revision,
                                 resolved_revision=q.resolved_revision, shown=q.id in shown_ids) for q in questions]

    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.case_id == case.id).order_by(ProcessingRun.created_at.desc())).all()
    revisions = revision_rows
    events = db.scalars(select(ReviewEvent).where(ReviewEvent.case_id == case.id).order_by(ReviewEvent.created_at).limit(500)).all()

    totals_json = totals.to_json()
    return CaseSnapshot(
        case=case_summary(db, case),
        sources=[source_out(s, s.id in thumbs) for s in sources],
        payments=payments_out,
        totals=TotalsOut(**{k: v for k, v in totals_json.items() if k != "reasons"}, citizen_reported_minor=reported_minor),
        duplicate_candidates=[DuplicateCandidateOut(payment_ids=list(c.payment_ids), basis=c.basis, issues=list(c.issues)) for c in candidates],
        case_facts={kind: fact_out(f) for kind, f in case_facts.items()},
        questions=questions_out,
        refunds=refunds_out,
        allocations=allocations_out,
        recovery=recovery_out,
        official_status_events=list(status_out.values()),
        latest_official_status=latest_out,
        resolutions=resolutions_out,
        runs=[run_out(r) for r in runs],
        revisions=[RevisionOut(revision=r.revision, kind=r.kind, summary=r.summary, actor_role=r.actor_role, created_at=r.created_at) for r in revisions],
        review_events=[ReviewEventOut(id=e.id, revision=e.revision, actor_role=e.actor_role, action=e.action, target_type=e.target_type,
                                      target_id=e.target_id, payload=e.payload or {}, created_at=e.created_at) for e in events],
        ai_modes_used=sorted({r.ai_mode for r in runs if r.status in ("proposed", "applied")}),
    )


# ---- from services/demo_cases.py -----------------------------------------------------------------
# Curated demo cases from development fixture packs (the only material the public demo processes).
#
# A pack's initial sources are those not listed under `updates[].sources`; the remaining ones are
# staged updates that can be added one step at a time to tell the Demo B story. Holdout packs are
# never offered.

PACK_ID = re.compile(r"^[a-z0-9-]{1,40}$")


def development_root(settings: Settings) -> Path:
    return Path(settings.fixture_roots[0]).resolve()


def list_demo_packs(settings: Settings) -> list[dict]:
    root = development_root(settings)
    packs: list[dict] = []
    if not root.exists():
        return packs
    for pack_file in sorted(root.glob("*/pack.json")):
        try:
            pack = json.loads(pack_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        steps = staged_steps(pack)
        staged = {sid for step in steps for sid in step.get("sources", [])}
        loadable = [s for s in pack.get("sources", []) if s.get("file") and not s.get("pendingOwnerRecording") and s.get("kind") != "transcript"]
        packs.append({"packId": pack["packId"], "title": pack.get("title", pack["packId"]), "tags": pack.get("tags", []),
                      "initialSources": len([s for s in loadable if s["fixtureSourceId"] not in staged]),
                      "stagedUpdates": len([step for step in steps if any(s["fixtureSourceId"] in step.get("sources", []) for s in loadable)]),
                      "hasAudio": any(s.get("kind") == "audio" for s in loadable)})
    return packs


def _read_pack(settings: Settings, pack_id: str) -> tuple[dict, Path]:
    if not PACK_ID.match(pack_id or ""):
        raise ApiError("not_found", "Demo pack not found.", 404)
    root = development_root(settings)
    pack_dir = (root / pack_id).resolve()
    if root not in pack_dir.parents or not (pack_dir / "pack.json").exists():
        raise ApiError("not_found", "Demo pack not found.", 404)
    return json.loads((pack_dir / "pack.json").read_text(encoding="utf-8")), pack_dir


def _resolve_file(pack_dir: Path, settings: Settings, relative: str) -> Path:
    fixtures_root = development_root(settings).parent
    path = (pack_dir / relative).resolve()
    if fixtures_root not in path.parents or not path.is_file():
        raise ApiError("not_found", "Demo file missing.", 404)
    return path


def _add_sources(db: Session, settings: Settings, storage: Storage, case: Case, pack: dict, pack_dir: Path,
                 wanted: set[str] | None, *, revision: int) -> list[Source]:
    added: list[Source] = []
    for src in pack.get("sources", []):
        sid = src.get("fixtureSourceId")
        if wanted is not None and sid not in wanted:
            continue
        kind, file = src.get("kind"), src.get("file")
        if not file or src.get("pendingOwnerRecording") or kind == "transcript":
            continue
        path = _resolve_file(pack_dir, settings, file)
        if kind in ("image", "supplied_official_update"):
            source = add_image_source(db, storage, case, path.read_bytes(), None, path.name, revision=revision, settings=settings)
            source.kind = kind
        elif kind == "typed_account":
            source = add_text_source(db, storage, case, path.read_text(encoding="utf-8"), kind="typed_account",
                                     display_filename=f"Typed account ({sid})", revision=revision, settings=settings)
        elif kind == "audio":
            source = add_audio_source(db, storage, case, path.read_bytes(), path.name, revision=revision, settings=settings)
        else:
            continue
        source.provenance_kind = "demo_fixture"
        added.append(source)
    return added


def staged_steps(pack: dict) -> list[dict]:
    """Later steps that bring new material. Step 1 describes the initial baseline and is loaded up front."""
    return sorted([step for step in pack.get("updates", []) if step.get("sources") and step.get("step", 0) > 1],
                  key=lambda s: s.get("step", 0))


def load_demo_case(db: Session, session: UserSession, settings: Settings, storage: Storage, pack_id: str) -> Case:
    pack, pack_dir = _read_pack(settings, pack_id)
    case = create_case(db, session, settings, storage, CaseCreate(title=pack.get("title", pack_id), language="en"))
    case.demo_pack_id = pack_id
    case.demo_steps_loaded = []
    revision = bump_revision(db, case, kind="sources_added", summary=f"Loaded curated demo pack {pack_id} (fictional data)",
                             actor_session_id=session.id, actor_role=session.role)
    staged = {sid for step in staged_steps(pack) for sid in step.get("sources", [])}
    initial = {s["fixtureSourceId"] for s in pack.get("sources", []) if s.get("fixtureSourceId") not in staged}
    _add_sources(db, settings, storage, case, pack, pack_dir, initial, revision=revision)
    return case


def add_next_demo_update(db: Session, session: UserSession, settings: Settings, storage: Storage, case: Case) -> dict:
    if not case.demo_pack_id:
        raise ApiError("invalid_request", "This case was not loaded from a demo pack.", 400)
    pack, pack_dir = _read_pack(settings, case.demo_pack_id)
    loaded = set(case.demo_steps_loaded or [])
    remaining = [step for step in staged_steps(pack) if step.get("step") not in loaded]
    if not remaining:
        raise ApiError("review_conflict", "No staged updates remain for this demo case.", 409)
    step = remaining[0]
    revision = bump_revision(db, case, kind="sources_added", summary=f"Demo update step {step.get('step')}: {', '.join(step.get('sources', []))}",
                             actor_session_id=session.id, actor_role=session.role)
    _add_sources(db, settings, storage, case, pack, pack_dir, set(step.get("sources", [])), revision=revision)
    case.demo_steps_loaded = [*loaded, step.get("step")]
    if case.packet_status == "ready_for_export":
        case.packet_status = "needs_review"
    return step


def demo_steps_remaining(settings: Settings, case: Case) -> int:
    if not case.demo_pack_id:
        return 0
    try:
        pack, _ = _read_pack(settings, case.demo_pack_id)
    except ApiError:
        return 0
    loaded = set(case.demo_steps_loaded or [])
    return len([s for s in staged_steps(pack) if s.get("step") not in loaded])


# ---- from services/retention.py ------------------------------------------------------------------
# Demo retention: expired sessions and the anonymous cases they own are purged periodically.
#
# Purging covers originals, derivatives, transcript text, exports, graph checkpoints and all rows.
# Hosting backups are outside this process and must be documented separately.
#
# Two retention rules, by ownership:
#
# * **Anonymous** cases (`account_id IS NULL`) keep the original rule — destroyed with their
#   session at `expires_at`, roughly 24 h.
# * **Account-owned** cases are not time-expired. The expiring session row still goes, but the
#   case is detached from it first and survives. A signed-in citizen removes their own data with
#   `DELETE /api/cases/{id}`.

def purge_expired(settings: Settings) -> dict[str, int]:
    """Delete expired sessions and their anonymous cases. Returns counts; never logs content."""
    from .ai import purge_checkpoints

    storage = Storage(settings)
    purged_cases = purged_sessions = 0
    with make_session() as db:
        expired = db.scalars(select(UserSession).where(UserSession.expires_at <= utcnow())).all()
        for session in expired:
            anonymous = db.scalars(
                select(Case).where(Case.session_id == session.id, Case.account_id.is_(None))
            ).all()
            for case in anonymous:
                run_ids = list(db.scalars(select(ProcessingRun.id).where(ProcessingRun.case_id == case.id)).all())
                storage.purge_case(case.id)
                purge_checkpoints(run_ids)
                db.delete(case)
                purged_cases += 1
            # must happen before the delete below, or ON DELETE CASCADE takes the rest
            detach_account_cases(db, session.id)
            db.delete(session)
            purged_sessions += 1
        purged_otp = db.execute(
            delete(OtpChallenge).where(OtpChallenge.created_at <= utcnow() - timedelta(hours=24))
        ).rowcount
        from .assistant import purge_unlinked_conversations

        purged_chats = purge_unlinked_conversations(db, settings)
        db.commit()
    if purged_sessions or purged_chats:
        log.info("retention: purged %d expired session(s), %d case(s) and %d unlinked chat(s)",
                 purged_sessions, purged_cases, purged_chats)
    return {"sessions": purged_sessions, "cases": purged_cases, "otp": purged_otp or 0, "chats": purged_chats}


class RetentionScheduler:
    """Runs `purge_expired` every `retention_check_minutes` on a daemon thread."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._settings.retention_check_minutes <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name="cb-retention", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = self._settings.retention_check_minutes * 60
        while not self._stop.wait(interval):
            try:
                purge_expired(self._settings)
            except Exception:  # noqa: BLE001 - keep the scheduler alive; details are in the log
                log.exception("retention purge failed")

    def stop(self) -> None:
        self._stop.set()
