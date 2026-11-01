"""Every HTTP route. Handlers validate, authorise and delegate; the rules live in the services."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from . import __version__
from .accounting import create_refund_claim, prepare_resolution, record_outstanding_statement, review_resolution, upsert_allocation
from .ai import get_runner, purge_checkpoints
from .casework import (
    add_audio_source,
    add_image_source,
    add_next_demo_update,
    add_text_source,
    add_transcript_correction,
    apply_review_batch,
    build_snapshot,
    case_summary,
    compute_changes,
    create_case,
    create_export,
    export_out,
    finalize_mutation,
    list_cases,
    list_demo_packs,
    load_demo_case,
    request_code,
    run_out,
    source_out,
    stamp_revision_totals,
    verify_code,
)
from .core import ApiError, Settings, get_settings, make_session
from .models import Account, Case, DerivedAsset, ExportSnapshot, OfficialStatusEvent, ProcessingRun, Question, Source, UserSession, utcnow
from .nextaction import actions_out, decide, propose_next_actions
from .parsing import parse_date_text
from .records import Storage, bump_revision, check_expected_revision, record_review_event, sanitise_filename
from .schemas import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ActionDecision,
    AllocationCreate,
    AnswerCreate,
    CaseCreate,
    CaseSnapshot,
    CaseSummary,
    DemoPackOut,
    DismissCreate,
    ExportCreate,
    ExportOut,
    HealthResponse,
    OfficerDeskOut,
    OfficerLogin,
    OtpRequest,
    OtpRequestOut,
    OtpVerify,
    OutstandingStatementCreate,
    RefundClaimCreate,
    ResolutionRequest,
    ReviewBatch,
    RunCreate,
    RunOut,
    SessionOut,
    SourceOut,
    StatusEventCreate,
    TranscriptCorrection,
    UpdateAcknowledge,
)
from .security import (
    CSRF_COOKIE,
    DUMMY_HASH,
    OFFICER_VISIBLE,
    Config,
    CurrentOfficer,
    CurrentSession,
    Db,
    OfficerCase,
    OwnedCase,
    clear_session_cookies,
    detach_account_cases,
    fingerprint,
    issue_csrf_token,
    mask_mobile,
    new_session,
    normalise_mobile,
    require_csrf,
    require_idempotency_key,
    rotate_session,
    run_idempotent,
    set_session_cookies,
    verify_secret,
)
from .verification import build_desk, run_bank_check
from .workflow import release_assignment

# ---- from api/health.py --------------------------------------------------------------------------

health_router = APIRouter(prefix="/api", tags=["health"])


def _migration_revision() -> str | None:
    try:
        with make_session() as db:
            return db.execute(text("select version_num from alembic_version")).scalar()
    except Exception:   # a health check never fails because of what it reports
        return None


@health_router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Readiness without secrets or case data."""
    s = get_settings()
    return HealthResponse(
        build_id=os.environ.get("BUILD_ID", "dev"),
        migration=_migration_revision(),
        features=sorted(name for name in ("intelligence", "investigation", "consultation") if importlib.util.find_spec(f"app.{name}") is not None),
        status="ok",
        version=__version__,
        app_env=s.app_env,
        ai_mode=s.ai_mode,
        demo_mode=s.demo_mode,
        public_fixtures_only=s.public_fixtures_only,
        text_model=s.meta_text_model,
        speech_model=s.meta_speech_model,
    )


# ---- from api/session.py -------------------------------------------------------------------------

session_router = APIRouter(prefix="/api/session", tags=["session"])


def session_out(db: Session, session: UserSession, csrf_token: str, settings) -> SessionOut:
    """Shared response builder. Identity fields are empty for an anonymous session."""
    account = db.get(Account, session.account_id) if session.account_id else None
    return SessionOut(
        role=session.role,
        label=session.label,
        demo_mode=session.demo_mode,
        expires_at=session.expires_at,
        csrf_token=csrf_token,
        public_fixtures_only=settings.public_fixtures_only,
        ai_mode=settings.ai_mode,
        authenticated=account is not None,
        account_kind=account.kind if account else None,
        mobile_masked=mask_mobile(account.mobile) if account else None,
        account_ref=hashlib.sha256(f"cb-account:{account.id}".encode()).hexdigest()[:24] if account else None,
        officer_id=account.officer_id if account else None,
        display_name=account.display_name if account else None,
    )


@session_router.post("", response_model=SessionOut, status_code=201)
def start_session(response: Response, db: Db, settings: Config) -> SessionOut:
    """Start an isolated demo session. The cookie proves possession only, not identity."""
    session = new_session(db, settings)
    db.commit()
    token = issue_csrf_token()
    set_session_cookies(response, session, token, settings)
    return session_out(db, session, token, settings)


@session_router.get("", response_model=SessionOut)
def get_session(request: Request, response: Response, session: CurrentSession, db: Db, settings: Config) -> SessionOut:
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = issue_csrf_token()
        set_session_cookies(response, session, token, settings)
    return session_out(db, session, token, settings)


# ---- from api/auth.py ----------------------------------------------------------------------------
# Sign-in for citizens (mobile + one-time code) and officers (id + password).
#
# Every route here needs an existing anonymous session, because the CSRF double-submit check
# depends on one. The browser already starts a session on boot.
#
# Signing in rotates the session id, so a cookie planted before sign-in carries no authority
# afterwards.

log = logging.getLogger("app.api")

auth_router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(require_csrf)])


def _session_ended() -> ApiError:
    return ApiError("unauthenticated", "This browser's session ended while the request was being saved.", 401, "Reload the page and try again.")


@auth_router.post("/otp/request", response_model=OtpRequestOut)
def otp_request(payload: OtpRequest, session: CurrentSession, db: Db, settings: Config) -> OtpRequestOut:
    mobile = normalise_mobile(payload.mobile)
    try:
        challenge, visible = request_code(db, settings, mobile, session_id=session.id)
        db.commit()
    except IntegrityError:
        # the session this browser held was ended (another tab signed in or out) between reading it and saving the code
        db.rollback()
        raise _session_ended() from None
    # neither the code nor the number is logged; only that a request happened
    log.info("otp requested")
    return OtpRequestOut(
        expires_at=challenge.expires_at,
        resend_after_seconds=settings.otp_resend_seconds,
        demo_code=visible,
    )


@auth_router.post("/otp/verify", response_model=SessionOut)
def otp_verify(
    payload: OtpVerify, response: Response, session: CurrentSession, db: Db, settings: Config
) -> SessionOut:
    mobile = normalise_mobile(payload.mobile)
    verify_code(db, settings, mobile, payload.code)

    account = db.scalar(select(Account).where(Account.mobile == mobile, Account.kind == "citizen"))
    if account is None:
        # first successful code creates the account; there is no separate registration step
        account = Account(kind="citizen", mobile=mobile, display_name=None)
        db.add(account)
        db.flush()
    if account.disabled:
        raise ApiError("forbidden", "This account is not active.", 403)

    try:
        fresh = rotate_session(db, settings, session, account=account, role="citizen", adopt_cases=True)
        db.commit()
    except (IntegrityError, StaleDataError):
        db.rollback()   # the old session was retired by a second sign-in in the same instant; the code stays consumed, so ask for a new one
        raise _session_ended() from None
    token = issue_csrf_token()
    set_session_cookies(response, fresh, token, settings)
    return session_out(db, fresh, token, settings)


@auth_router.post("/officer/login", response_model=SessionOut)
def officer_login(
    payload: OfficerLogin, response: Response, session: CurrentSession, db: Db, settings: Config
) -> SessionOut:
    account = db.scalar(
        select(Account).where(Account.officer_id == payload.officer_id.strip(), Account.kind == "officer")
    )
    # one message for an unknown id and a wrong password, and the same work either way
    stored = account.password_hash if account and account.password_hash else DUMMY_HASH
    ok = verify_secret(payload.password, stored)
    if account is None or not ok or account.disabled:
        raise ApiError(
            "invalid_credentials", "Officer ID or password is not correct.", 401,
            "Check the credentials and try again.",
        )

    # an officer signing in must not absorb citizen drafts left in this browser
    fresh = rotate_session(db, settings, session, account=account, role="officer", adopt_cases=False)
    db.commit()
    token = issue_csrf_token()
    set_session_cookies(response, fresh, token, settings)
    return session_out(db, fresh, token, settings)


@auth_router.post("/admin/login", response_model=SessionOut)
def admin_login(
    payload: OfficerLogin, response: Response, session: CurrentSession, db: Db, settings: Config
) -> SessionOut:
    """The admin coordinator's own sign-in. Same shape as the officer's, a separate account kind and role."""
    account = db.scalar(select(Account).where(Account.officer_id == payload.officer_id.strip(), Account.kind == "admin"))
    stored = account.password_hash if account and account.password_hash else DUMMY_HASH
    ok = verify_secret(payload.password, stored)
    if account is None or not ok or account.disabled:
        raise ApiError("invalid_credentials", "Admin ID or password is not correct.", 401, "Check the credentials and try again.")
    fresh = rotate_session(db, settings, session, account=account, role="admin", adopt_cases=False)
    db.commit()
    token = issue_csrf_token()
    set_session_cookies(response, fresh, token, settings)
    return session_out(db, fresh, token, settings)


@auth_router.post("/logout", status_code=204)
def logout(session: CurrentSession, db: Db) -> Response:
    detach_account_cases(db, session.id)  # signing out must never take the complaints with it
    db.delete(session)
    db.commit()
    out = Response(status_code=204)
    clear_session_cookies(out)
    return out


# ---- from api/cases.py ---------------------------------------------------------------------------

cases_router = APIRouter(prefix="/api/cases", tags=["cases"])


@cases_router.post("", response_model=CaseSnapshot, status_code=201, dependencies=[Depends(require_csrf)])
def register_case(payload: CaseCreate, session: CurrentSession, db: Db, settings: Config) -> CaseSnapshot:
    if settings.public_fixtures_only and payload.incident_account:
        raise ApiError("forbidden", "The public demo accepts curated fictional cases only.", 403,
                       "Load a demo case instead of typing a new account.")
    case = create_case(db, session, settings, Storage(settings), payload)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@cases_router.get("", response_model=list[CaseSummary])
def list_mine(session: CurrentSession, db: Db) -> list[CaseSummary]:
    return list_cases(db, session)


@cases_router.get("/{case_id}", response_model=CaseSnapshot)
def get_case(case: OwnedCase, db: Db) -> CaseSnapshot:
    return build_snapshot(db, case)


@cases_router.post("/{case_id}/submit-review", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def submit_for_review(case: OwnedCase, session: CurrentSession, db: Db) -> CaseSnapshot:
    """Send the packet to the officer queue.

    This is internal to the demo. It does not file an official complaint, does not reach any
    police or banking system, and does not change the official complaint status.

    Deliberately records a review event at the current revision rather than bumping one: a new
    revision would mark a prepared resolution summary stale.
    """
    if session.account_id is None:
        raise ApiError("forbidden", "Sign in to send this complaint for review.", 403,
                       "Use your mobile number to sign in first.")
    if case.review_state == "submitted":
        raise ApiError("review_conflict", "This complaint is already with an officer.", 409)
    if case.review_state == "closed":
        raise ApiError("review_conflict", "This complaint has been closed.", 409,
                       "Register a new complaint if something further has happened.")
    was_returned = case.review_state == "returned"
    case.review_state = "submitted"
    case.submitted_at = utcnow()
    if not was_returned or case.workflow_state is None:
        case.workflow_state = "awaiting_assignment"   # a returned case keeps the officer who returned it
    record_review_event(db, case_id=case.id, revision=case.current_revision,
                        actor_session_id=session.id, actor_role=session.role,
                        action="submit", target_type="case", target_id=case.id)
    db.flush()
    from .events import emit, run_events

    emit(db, case.id, "submitted", f"submitted:{case.id}:{case.submitted_at.isoformat()}", {"revision": case.current_revision})
    db.commit()
    run_events(get_settings())   # prepares the assignment for the admin; a failure is kept on the event, not shown here
    db.refresh(case)
    return build_snapshot(db, case)


@cases_router.post("/{case_id}/withdraw-review", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def withdraw_review(case: OwnedCase, session: CurrentSession, db: Db) -> CaseSnapshot:
    """Take a complaint back out of the officer queue."""
    if case.review_state not in ("submitted", "returned"):
        raise ApiError("review_conflict", "This complaint is not with an officer.", 409)
    case.review_state = "private"
    case.submitted_at = None
    release_assignment(db, case)   # takes the report out of the officer's workload as well as the queue
    record_review_event(db, case_id=case.id, revision=case.current_revision,
                        actor_session_id=session.id, actor_role=session.role,
                        action="withdraw", target_type="case", target_id=case.id)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@cases_router.delete("/{case_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_case(case: OwnedCase, db: Db, settings: Config) -> Response:
    """Remove the case and everything stored for it: originals, derivatives, exports, checkpoints, rows."""
    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.case_id == case.id)).all()
    for run in runs:
        if run.status in ("queued", "running"):
            run.status = "cancelled"
            run.stage = "cancelled"
    db.flush()
    run_ids = [r.id for r in runs]
    Storage(settings).purge_case(case.id)
    purge_checkpoints(run_ids)
    db.delete(case)  # child rows cascade at the database level (PRAGMA foreign_keys=ON)
    db.commit()
    return Response(status_code=204)


# ---- from api/sources.py -------------------------------------------------------------------------

sources_router = APIRouter(prefix="/api/cases/{case_id}/sources", tags=["sources"])


@sources_router.post("", response_model=SourceOut, status_code=201, dependencies=[Depends(require_csrf)])
def upload_source(
    case: OwnedCase,
    session: CurrentSession,
    db: Db,
    settings: Config,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
    kind: Annotated[Literal["image", "supplied_official_update", "audio", "typed_account", "user_answer"], Form()] = "image",
    expected_revision: Annotated[int | None, Form(alias="expectedRevision")] = None,
) -> Response:
    """Upload one supported source. Originals are preserved byte for byte."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept uploads.", 403,
                       "Use the curated demo cases, or run CyberBrief locally for your own fictional material.")
    storage = Storage(settings)
    data: bytes | None = None
    filename: str | None = None
    is_image = kind in ("image", "supplied_official_update")
    is_audio = kind == "audio"
    if is_image or is_audio:
        if file is None:
            raise ApiError("invalid_request", "A file is required for this source kind.", 400)
        data = file.file.read(settings.max_image_bytes + 1)
        filename = file.filename
        payload_hash = fingerprint(case.id, kind, data)
    else:
        if not text or not text.strip():
            raise ApiError("invalid_request", "Text is required for a typed source.", 400)
        payload_hash = fingerprint(case.id, kind, text)

    def produce() -> tuple[int, dict]:
        check_expected_revision(case, expected_revision)
        revision = bump_revision(db, case, kind="sources_added", summary=f"Added {kind.replace('_', ' ')}",
                                 actor_session_id=session.id, actor_role=session.role)
        if is_image:
            assert data is not None
            source = add_image_source(db, storage, case, data, file.content_type if file else None, filename,
                                      revision=revision, settings=settings)
            if kind == "supplied_official_update":
                source.kind = "supplied_official_update"
        elif is_audio:
            assert data is not None
            source = add_audio_source(db, storage, case, data, filename, revision=revision, settings=settings)
        else:
            display = "Typed account" if kind == "typed_account" else "Typed answer"
            source = add_text_source(db, storage, case, text.strip(), kind=kind, display_filename=display,  # type: ignore[union-attr]
                                     revision=revision, settings=settings)
        if case.packet_status in ("ready_for_export",):
            case.packet_status = "needs_review"
        from .casework import stamp_revision_totals

        stamp_revision_totals(db, case)
        has_thumbnail = db.scalar(select(DerivedAsset.id).where(DerivedAsset.source_id == source.id)) is not None
        return 201, source_out(source, has_thumbnail).model_dump(by_alias=True, mode="json")

    status, body = run_idempotent(db, session_id=session.id, scope="sources", key=idempotency_key,
                                  payload_hash=payload_hash, produce=produce)
    return JSONResponse(body, status_code=status)


@sources_router.post("/{source_id}/transcript", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def correct_transcript(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, source_id: str,
                       payload: TranscriptCorrection) -> CaseSnapshot:
    """Save a corrected transcript as a new derived source. The original transcript and audio are kept."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept free-form input.", 403)
    original = db.scalar(select(Source).where(Source.id == source_id, Source.case_id == case.id))
    if original is None:
        raise ApiError("not_found", "Source not found.", 404)
    check_expected_revision(case, payload.expected_revision)
    revision = bump_revision(db, case, kind="transcript_corrected", summary=f"Transcript corrected ({original.display_filename})",
                             actor_session_id=session.id, actor_role=session.role)
    corrected = add_transcript_correction(db, Storage(settings), case, original, payload.text.strip(), revision=revision,
                                          settings=settings, actor_role=session.role)
    record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                        action="correct", target_type="transcript", target_id=corrected.id, payload={"supersedes": original.id})
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@sources_router.get("/{source_id}")
def download_source(case: OwnedCase, source_id: str, db: Db, settings: Config,
                    variant: Literal["original", "thumbnail"] = "original") -> Response:
    """Authorised view of an original or its thumbnail. Never a public URL."""
    return _serve_source(case, source_id, db, settings, variant)


def _serve_source(case: Case, source_id: str, db: Session, settings: Settings, variant: str) -> Response:
    source = db.scalar(select(Source).where(Source.id == source_id, Source.case_id == case.id))
    if source is None:
        raise ApiError("not_found", "Source not found.", 404)
    storage = Storage(settings)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if variant == "thumbnail":
        asset = db.scalar(select(DerivedAsset).where(DerivedAsset.source_id == source.id, DerivedAsset.kind == "thumbnail"))
        if asset is None:
            raise ApiError("not_found", "No thumbnail for this source.", 404)
        return Response(storage.read(asset.storage_key), media_type=asset.media_type, headers=headers)
    if source.storage_key is None:
        return Response((source.text_content or "").encode("utf-8"), media_type="text/plain; charset=utf-8", headers=headers)
    safe_name = sanitise_filename(source.display_filename).replace('"', "")
    headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
    return Response(storage.read(source.storage_key), media_type=source.media_type, headers=headers)


# ---- from api/runs.py ----------------------------------------------------------------------------

runs_router = APIRouter(prefix="/api/cases/{case_id}/runs", tags=["runs"])
PROCESSABLE_KINDS = ("image", "supplied_official_update", "audio", "transcript", "typed_account", "user_answer")


@runs_router.post("", response_model=RunOut, status_code=202, dependencies=[Depends(require_csrf)])
def create_run(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: RunCreate,
               idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> JSONResponse:
    """Process new sources (or retry an interrupted/failed run). One run per case at a time."""
    payload_hash = fingerprint(case.id, payload.model_dump(mode="json"))

    def produce() -> tuple[int, dict]:
        check_expected_revision(case, payload.expected_revision)
        if case.active_run_id:
            active = db.get(ProcessingRun, case.active_run_id)
            if active is not None and active.status in ("queued", "running"):
                raise ApiError("run_in_progress", "A processing run is already in progress for this case.", 409,
                               "Wait for it to finish, then review the proposals.")
            case.active_run_id = None

        if payload.retry_run_id:
            prior = db.get(ProcessingRun, payload.retry_run_id)
            if prior is None or prior.case_id != case.id:
                raise ApiError("not_found", "Run to retry not found.", 404)
            if prior.status not in ("failed", "interrupted", "cancelled"):
                raise ApiError("review_conflict", "Only failed or interrupted runs can be retried.", 409)
            source_ids = list(prior.input_source_ids or [])
        elif payload.source_ids:
            source_ids = list(dict.fromkeys(payload.source_ids))
        else:
            source_ids = [s.id for s in db.scalars(select(Source).where(
                Source.case_id == case.id, Source.status.in_(("stored", "failed")), Source.kind.in_(PROCESSABLE_KINDS)
            ).order_by(Source.received_at)).all()]
        sources = db.scalars(select(Source).where(Source.id.in_(source_ids), Source.case_id == case.id)).all() if source_ids else []
        if not sources or len(sources) != len(source_ids):
            raise ApiError("invalid_request", "No unprocessed sources were found for this run.", 400,
                           "Upload a receipt or add a typed account first.")
        for s in sources:
            if s.kind not in PROCESSABLE_KINDS:
                raise ApiError("invalid_request", f"Source kind {s.kind!r} is not processed by this route.", 400)
            if s.status in ("queued", "processing"):
                raise ApiError("run_in_progress", "A source in this selection is already being processed.", 409)

        run = ProcessingRun(case_id=case.id, status="queued", stage="queued", base_revision=case.current_revision,
                            input_source_ids=[s.id for s in sources], input_hashes=[s.sha256 for s in sources],
                            ai_mode=settings.ai_mode, provider="meta" if settings.ai_mode == "live" else "fixture",
                            model_id=settings.meta_text_model if settings.ai_mode == "live" else "fixture",
                            prompt_version=PROMPT_VERSION if settings.ai_mode == "live" else "fixture",
                            schema_version=SCHEMA_VERSION, idempotency_key=idempotency_key)
        db.add(run)
        db.flush()
        for s in sources:
            s.status = "queued"
        case.active_run_id = run.id
        case.packet_status = "processing"
        return 202, run_out(run).model_dump(by_alias=True, mode="json")

    status, body = run_idempotent(db, session_id=session.id, scope="runs", key=idempotency_key,
                                  payload_hash=payload_hash, produce=produce)
    run = db.get(ProcessingRun, body["id"])
    if run is not None and run.status == "queued" and run.started_at is None:
        db.expunge_all()
        get_runner().submit(run.id)
        run = db.get(ProcessingRun, body["id"])
        if run is not None:
            body = run_out(run).model_dump(by_alias=True, mode="json")
    return JSONResponse(body, status_code=status)


@runs_router.get("/{run_id}", response_model=RunOut)
def get_run(case: OwnedCase, run_id: str, db: Db) -> RunOut:
    run = db.scalar(select(ProcessingRun).where(ProcessingRun.id == run_id, ProcessingRun.case_id == case.id))
    if run is None:
        raise ApiError("not_found", "Run not found.", 404)
    return run_out(run)


# ---- from api/reviews.py -------------------------------------------------------------------------

reviews_router = APIRouter(prefix="/api/cases/{case_id}/reviews", tags=["reviews"])


@reviews_router.post("", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def review(case: OwnedCase, session: CurrentSession, db: Db, batch: ReviewBatch) -> CaseSnapshot:
    """Confirm, correct, leave unresolved or exclude facts; confirm, exclude, merge or separate payments."""
    apply_review_batch(db, case, session, batch)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


# ---- from api/questions.py -----------------------------------------------------------------------

questions_router = APIRouter(prefix="/api/cases/{case_id}/questions", tags=["questions"])


def _question(db, case, question_id: str) -> Question:
    question = db.scalar(select(Question).where(Question.id == question_id, Question.case_id == case.id))
    if question is None:
        raise ApiError("not_found", "Question not found.", 404)
    return question


@questions_router.post("/{question_id}/answers", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def answer(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, question_id: str, payload: AnswerCreate) -> CaseSnapshot:
    """Store the answer as a new `user_answer` source linked to the question. It can then be processed like any source."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept free-form input.", 403)
    question = _question(db, case, question_id)
    if question.state not in ("open", "answered"):
        raise ApiError("review_conflict", "This question is no longer open.", 409, "Reload the case.")
    check_expected_revision(case, payload.expected_revision)
    revision = bump_revision(db, case, kind="answer", summary=f"Answered: {question.kind.replace('_', ' ')}",
                             actor_session_id=session.id, actor_role=session.role)
    source = add_text_source(db, Storage(settings), case, payload.text.strip(), kind="user_answer",
                             display_filename=f"Answer about {question.kind.replace('_', ' ')}", revision=revision, settings=settings)
    source.meta = {**(source.meta or {}), "questionId": question.id, "questionText": question.text}
    question.state = "answered"
    question.answer_source_id = source.id
    record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                        action="answer", target_type="question", target_id=question.id,
                        payload={"kind": question.kind, "answerSourceId": source.id})
    if case.packet_status == "ready_for_export":
        case.packet_status = "needs_review"
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@questions_router.post("/{question_id}/dismiss", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def dismiss(case: OwnedCase, session: CurrentSession, db: Db, question_id: str, payload: DismissCreate) -> CaseSnapshot:
    question = _question(db, case, question_id)
    if question.state not in ("open", "answered"):
        raise ApiError("review_conflict", "This question is no longer open.", 409, "Reload the case.")
    check_expected_revision(case, payload.expected_revision)
    revision = bump_revision(db, case, kind="review", summary=f"Dismissed question: {question.kind.replace('_', ' ')}",
                             actor_session_id=session.id, actor_role=session.role)
    question.state = "dismissed"
    question.dismiss_reason = payload.reason
    question.resolved_revision = revision
    record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                        action="dismiss", target_type="question", target_id=question.id, payload={"reason": payload.reason})
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


# ---- from api/recovery.py ------------------------------------------------------------------------
# Recovery track: refund claims, allocations, the citizen's outstanding statement; official status; resolution.

recovery_router = APIRouter(prefix="/api/cases/{case_id}", tags=["recovery"])


def _snapshot_body(db, case) -> dict:
    return build_snapshot(db, case).model_dump(by_alias=True, mode="json")


@recovery_router.post("/refunds", response_model=CaseSnapshot, status_code=201, dependencies=[Depends(require_csrf)])
def report_refund(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: RefundClaimCreate,
                  idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> JSONResponse:
    """The citizen reports money returned. Shown immediately as a claim; never counted until a credit record is reviewed."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept free-form input.", 403)

    def produce() -> tuple[int, dict]:
        check_expected_revision(case, payload.expected_revision)
        revision = bump_revision(db, case, kind="refund_reported", summary="Refund reported by citizen",
                                 actor_session_id=session.id, actor_role=session.role)
        refund = create_refund_claim(db, case, session, settings, Storage(settings), text=payload.text.strip(),
                                     amount_raw=payload.amount_raw, kind=payload.kind, date_text=payload.date_text, revision=revision)
        record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                            action="report", target_type="refund", target_id=refund.id, payload={"amountRaw": payload.amount_raw})
        case.packet_status = "needs_review"
        finalize_mutation(db, case, revision)
        return 201, _snapshot_body(db, case)

    status, body = run_idempotent(db, session_id=session.id, scope="refunds", key=idempotency_key,
                                  payload_hash=fingerprint(case.id, payload.model_dump(mode="json")), produce=produce)
    return JSONResponse(body, status_code=status)


@recovery_router.post("/refund-allocations", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def allocate(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: AllocationCreate,
             idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> JSONResponse:
    """Confirm or revise an allocation of a reviewed refund to a reviewed debit. Excess is rejected, never clamped."""

    def produce() -> tuple[int, dict]:
        check_expected_revision(case, payload.expected_revision)
        revision = bump_revision(db, case, kind="allocation", summary="Refund allocation confirmed or revised",
                                 actor_session_id=session.id, actor_role=session.role)
        allocation = upsert_allocation(db, case, session, refund_id=payload.refund_id, payment_id=payload.payment_id,
                                       amount_minor=int(payload.amount_minor), reason=payload.reason, revision=revision)
        record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                            action="allocate" if allocation else "unallocate", target_type="allocation",
                            target_id=allocation.id if allocation else payload.refund_id,
                            payload={"refundId": payload.refund_id, "paymentId": payload.payment_id, "amountMinor": payload.amount_minor})
        finalize_mutation(db, case, revision)
        return 200, _snapshot_body(db, case)

    status, body = run_idempotent(db, session_id=session.id, scope="allocations", key=idempotency_key,
                                  payload_hash=fingerprint(case.id, payload.model_dump(mode="json")), produce=produce)
    return JSONResponse(body, status_code=status)


@recovery_router.post("/recovery-statement", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def outstanding_statement(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: OutstandingStatementCreate) -> CaseSnapshot:
    """Capture what the citizen says is still outstanding. It is a statement, not a computed balance."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept free-form input.", 403)
    check_expected_revision(case, payload.expected_revision)
    revision = bump_revision(db, case, kind="statement", summary="Citizen statement about outstanding money",
                             actor_session_id=session.id, actor_role=session.role)
    fact = record_outstanding_statement(db, case, session, settings, Storage(settings), text=payload.text.strip(),
                                        outstanding_minor=None if payload.outstanding_minor is None else int(payload.outstanding_minor),
                                        revision=revision)
    record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                        action="statement", target_type="fact", target_id=fact.fact_id, payload={"outstandingMinor": payload.outstanding_minor})
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@recovery_router.post("/official-status-events", response_model=CaseSnapshot, status_code=201, dependencies=[Depends(require_csrf)])
def record_status(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: StatusEventCreate) -> CaseSnapshot:
    """Record a supplied official communication with its wording, issuer and dates. Not live tracking; never inferred."""
    if settings.public_fixtures_only:
        raise ApiError("forbidden", "The public demo does not accept free-form input.", 403)
    check_expected_revision(case, payload.expected_revision)
    if payload.source_ids:
        found = db.scalars(select(Source.id).where(Source.id.in_(payload.source_ids), Source.case_id == case.id)).all()
        if len(found) != len(set(payload.source_ids)):
            raise ApiError("not_found", "One or more referenced sources are not in this case.", 404)
    revision = bump_revision(db, case, kind="official_status", summary="Official status recorded from a supplied communication",
                             actor_session_id=session.id, actor_role=session.role)
    effective = parse_date_text(payload.effective_at_text).to_json() if payload.effective_at_text else None
    event = OfficialStatusEvent(case_id=case.id, raw_status=payload.raw_status.strip(), category=payload.category,
                                issuer_as_supplied=payload.issuer_as_supplied, source_ids=list(payload.source_ids), effective_at=effective,
                                entered_by_role=session.role, review_state="reviewer_checked" if session.role == "officer" else "user_confirmed",
                                origin="user_supplied_communication", created_revision=revision)
    db.add(event)
    db.flush()
    record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                        action="record", target_type="official_status", target_id=event.id, payload={"category": payload.category})
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@recovery_router.post("/resolution-reviews", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def resolution(case: OwnedCase, session: CurrentSession, db: Db, payload: ResolutionRequest) -> CaseSnapshot:
    """Prepare a resolution summary from reviewed records, or (as demo reviewer) record a packet review."""
    check_expected_revision(case, payload.expected_revision)
    if payload.action == "prepare":
        # on "prepare" the note is the citizen's reason for asking that this record be closed;
        # a closure request is never blocked by an unreviewed packet, so it implies allow_partial
        note = (payload.note or "").strip() or None
        summary = "Closure requested by the citizen" if note else "Resolution summary prepared"
        revision = bump_revision(db, case, kind="resolution_prepared", summary=summary,
                                 actor_session_id=session.id, actor_role=session.role)
        prepared = prepare_resolution(db, case, session, allow_partial=payload.allow_partial or bool(note),
                                      revision=revision, request_note=note)
        record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                            action="closure_requested" if note else "prepare", target_type="resolution",
                            target_id=prepared.id,
                            payload={"kind": prepared.kind, "eligible": prepared.eligible, "blocking": prepared.blocking_reasons})
    else:
        if not payload.resolution_id or not payload.outcome:
            raise ApiError("invalid_request", "resolutionId and outcome are required to record a review.", 400)
        reviewed = review_resolution(db, case, session, resolution_id=payload.resolution_id, outcome=payload.outcome, note=payload.note)
        revision = bump_revision(db, case, kind="packet_reviewed", summary=f"Packet review recorded: {payload.outcome.replace('_', ' ')}",
                                 actor_session_id=session.id, actor_role=session.role)
        # the summary keeps its preparation revision; a packet_reviewed revision alone does not make it stale
        record_review_event(db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
                            action=payload.outcome, target_type="resolution", target_id=reviewed.id, payload={"note": payload.note})
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


@recovery_router.get("/changes")
def changes(case: OwnedCase, db: Db, since: int | None = None) -> dict:
    """What changed since a revision (default: since the last acknowledged review)."""
    return compute_changes(db, case, case.last_reviewed_revision if since is None else since)


@recovery_router.post("/updates/acknowledge", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def acknowledge(case: OwnedCase, session: CurrentSession, db: Db, payload: UpdateAcknowledge) -> CaseSnapshot:
    """Mark the current revision as reviewed; later change summaries start from here. No revision bump."""
    from .models import CaseUpdate

    check_expected_revision(case, payload.expected_revision)
    summary = compute_changes(db, case, case.last_reviewed_revision)
    db.add(CaseUpdate(case_id=case.id, base_revision=case.last_reviewed_revision, new_revision=case.current_revision,
                      summary={k: v for k, v in summary.items() if k not in ("factsAdded", "factsChanged")},
                      acknowledged_by_role=session.role))
    case.last_reviewed_revision = case.current_revision
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


# ---- from api/exports.py -------------------------------------------------------------------------

exports_router = APIRouter(prefix="/api/cases/{case_id}/exports", tags=["exports"])


@exports_router.post("", response_model=ExportOut, status_code=201, dependencies=[Depends(require_csrf)])
def request_export(case: OwnedCase, session: CurrentSession, db: Db, settings: Config, payload: ExportCreate,
           idempotency_key: Annotated[str, Depends(require_idempotency_key)]) -> JSONResponse:
    """Generate a revision-pinned printable HTML report or JSON snapshot."""
    storage = Storage(settings)
    payload_hash = fingerprint(case.id, payload.model_dump(mode="json"), case.current_revision)

    def produce() -> tuple[int, dict]:
        export = create_export(db, storage, case, session, payload.format, payload.expected_revision)
        return 201, export_out(export).model_dump(by_alias=True, mode="json")

    status, body = run_idempotent(db, session_id=session.id, scope="exports", key=idempotency_key,
                                  payload_hash=payload_hash, produce=produce)
    return JSONResponse(body, status_code=status)


@exports_router.get("/{export_id}")
def download(case: OwnedCase, export_id: str, db: Db, settings: Config) -> Response:
    export = db.scalar(select(ExportSnapshot).where(ExportSnapshot.id == export_id, ExportSnapshot.case_id == case.id))
    if export is None:
        raise ApiError("not_found", "Export not found.", 404)
    data = Storage(settings).read(export.storage_key)
    media = "text/html; charset=utf-8" if export.format == "html" else "application/json"
    disposition = "inline" if export.format == "html" else "attachment"
    name = f"cyberbrief_{case.id[:8]}_r{export.revision}.{export.format}"
    return Response(data, media_type=media, headers={
        "Content-Disposition": f'{disposition}; filename="{name}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


# ---- from api/demo.py ----------------------------------------------------------------------------

demo_router = APIRouter(prefix="/api", tags=["demo"])
PROCESSABLE = ("image", "supplied_official_update", "audio", "transcript", "typed_account", "user_answer")


def _start_run(db, case, settings) -> None:
    sources = db.scalars(select(Source).where(Source.case_id == case.id, Source.status.in_(("stored", "failed")), Source.kind.in_(PROCESSABLE))).all()
    if not sources or case.active_run_id:
        return
    run = ProcessingRun(case_id=case.id, status="queued", stage="queued", base_revision=case.current_revision,
                        input_source_ids=[s.id for s in sources], input_hashes=[s.sha256 for s in sources], ai_mode=settings.ai_mode,
                        provider="meta" if settings.ai_mode == "live" else "fixture",
                        model_id=settings.meta_text_model if settings.ai_mode == "live" else "fixture",
                        prompt_version=PROMPT_VERSION if settings.ai_mode == "live" else "fixture", schema_version=SCHEMA_VERSION,
                        idempotency_key=f"demo:{case.id}:{case.current_revision}")
    db.add(run)
    db.flush()
    for s in sources:
        s.status = "queued"
    case.active_run_id = run.id
    case.packet_status = "processing"
    db.commit()
    db.expunge_all()
    get_runner().submit(run.id)


@demo_router.get("/demo-cases", response_model=list[DemoPackOut])
def demo_packs(settings: Config) -> list[DemoPackOut]:
    """Curated fictional cases available to load. Holdout packs are never listed."""
    return [DemoPackOut(**p) for p in list_demo_packs(settings)]


@demo_router.post("/demo-cases/{pack_id}", response_model=CaseSnapshot, status_code=201, dependencies=[Depends(require_csrf)])
def load_demo(pack_id: str, session: CurrentSession, db: Db, settings: Config) -> CaseSnapshot:
    """Create an isolated copy of a curated demo case in this session and start processing it."""
    case = load_demo_case(db, session, settings, Storage(settings), pack_id)
    stamp_revision_totals(db, case)
    db.commit()
    db.refresh(case)
    _start_run(db, case, settings)
    case = db.get(type(case), case.id)
    return build_snapshot(db, case)


@demo_router.post("/cases/{case_id}/demo-updates/next", response_model=CaseSnapshot, dependencies=[Depends(require_csrf)])
def next_demo_update(case: OwnedCase, session: CurrentSession, db: Db, settings: Config) -> CaseSnapshot:
    """Add the next staged update of the demo pack (new material arriving later) and start processing it."""
    add_next_demo_update(db, session, settings, Storage(settings), case)
    stamp_revision_totals(db, case)
    db.commit()
    db.refresh(case)
    _start_run(db, case, settings)
    case = db.get(type(case), case.id)
    return build_snapshot(db, case)


# ---- from api/officer.py -------------------------------------------------------------------------
# The officer desk: read complaints a citizen sent for review, and record a packet review.
#
# The officer here is a *simulated* reviewer for this demo. Nothing on these routes
# files an official complaint, contacts a bank or a police system, or changes the official
# complaint status — that is only ever recorded from a communication the citizen supplies.
#
# Officers never reach the citizen routes: `require_case` refuses an officer session outright,
# so uploads, deletions and corrections are unavailable here by construction.

officer_router = APIRouter(prefix="/api/officer", tags=["officer"])


@officer_router.get("/cases", response_model=list[CaseSummary])
def queue(session: CurrentOfficer, db: Db) -> list[CaseSummary]:
    """Complaints citizens have sent for review, newest submission first."""
    cases = db.scalars(
        select(Case)
        .where(Case.review_state.in_(OFFICER_VISIBLE), Case.assigned_officer_id == session.account_id)
        .order_by(Case.submitted_at.desc())
    ).all()
    return [case_summary(db, c) for c in cases]


@officer_router.get("/cases/{case_id}", response_model=CaseSnapshot)
def read_case(case: OfficerCase, db: Db) -> CaseSnapshot:
    return build_snapshot(db, case)


@officer_router.post("/cases/{case_id}/resolution-reviews", response_model=CaseSnapshot,
             dependencies=[Depends(require_csrf)])
def record_review(
    case: OfficerCase, session: CurrentOfficer, db: Db, payload: ResolutionRequest
) -> CaseSnapshot:
    """Record a packet review against a summary the citizen prepared."""
    if not payload.resolution_id or not payload.outcome:
        raise ApiError("invalid_request", "resolutionId and outcome are required to record a review.", 400)
    reviewed = review_resolution(
        db, case, session, resolution_id=payload.resolution_id, outcome=payload.outcome, note=payload.note
    )
    revision = bump_revision(
        db, case, kind="packet_reviewed",
        summary=f"Packet review recorded: {payload.outcome.replace('_', ' ')}",
        actor_session_id=session.id, actor_role=session.role,
    )
    record_review_event(
        db, case_id=case.id, revision=revision, actor_session_id=session.id, actor_role=session.role,
        action=payload.outcome, target_type="resolution", target_id=reviewed.id,
        payload={"note": payload.note},
    )
    # "closed" ends CyberBrief's tracking of this record and nothing else: no official complaint
    # is closed, withdrawn or affected by it.
    case.review_state = {"packet_reviewed": "reviewed", "closure_agreed": "closed"}.get(payload.outcome, "returned")
    case.reviewed_by_account_id = session.account_id
    if payload.outcome == "more_information_requested":
        case.submitted_at = case.submitted_at or utcnow()
    finalize_mutation(db, case, revision)
    db.commit()
    db.refresh(case)
    return build_snapshot(db, case)


# ---- the officer's review desk -------------------------------------------------------------------------------
# A simulated bank check, what supports each payment, the timeline with its evidence, the audit trail, and suggested
# next steps. None of it changes the case: a bank check is recorded in the audit trail only, and a suggestion does
# nothing until the officer presses the existing button it points at.

def _desk(db: Session, case: Case) -> OfficerDeskOut:
    return build_desk(db, case, build_snapshot(db, case), actions_out(db, case))


@officer_router.get("/cases/{case_id}/desk", response_model=OfficerDeskOut)
def desk(case: OfficerCase, db: Db) -> OfficerDeskOut:
    return _desk(db, case)


@officer_router.get("/cases/{case_id}/sources/{source_id}")
def officer_source(case: OfficerCase, source_id: str, db: Db, settings: Config,
                   variant: Literal["original", "thumbnail"] = "original") -> Response:
    """The evidence behind a payment, read-only. The same authorised view the citizen has."""
    return _serve_source(case, source_id, db, settings, variant)


@officer_router.post("/cases/{case_id}/bank-check", response_model=OfficerDeskOut, dependencies=[Depends(require_csrf)])
def bank_check(case: OfficerCase, session: CurrentOfficer, db: Db) -> OfficerDeskOut:
    """Check each payment against the simulated ledger. Recorded in the audit trail; the case itself is not touched."""
    run_bank_check(db, case, session, build_snapshot(db, case))
    db.commit()
    return _desk(db, case)


@officer_router.post("/cases/{case_id}/next-actions", response_model=OfficerDeskOut, dependencies=[Depends(require_csrf)])
def next_actions(case: OfficerCase, session: CurrentOfficer, db: Db, settings: Config) -> OfficerDeskOut:
    """Suggest next steps. The model proposes; nothing happens until the officer decides."""
    snapshot = build_snapshot(db, case)
    supports = build_desk(db, case, snapshot).payments
    propose_next_actions(db, settings, case, session, snapshot, supports)
    db.commit()
    db.refresh(case)
    return _desk(db, case)


@officer_router.post("/cases/{case_id}/next-actions/decision", response_model=OfficerDeskOut, dependencies=[Depends(require_csrf)])
def next_action_decision(case: OfficerCase, session: CurrentOfficer, db: Db, payload: ActionDecision) -> OfficerDeskOut:
    decide(db, case, session, payload)
    db.commit()
    return _desk(db, case)
