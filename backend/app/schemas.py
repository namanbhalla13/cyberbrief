"""Wire types -- camelCase outside, snake_case inside -- and the model output contract."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ---- from schemas/common.py ----------------------------------------------------------------------
# Base API model: camelCase on the wire, snake_case in Python, unknown fields rejected.

class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        serialize_by_alias=True,
    )


# ---- from schemas/extraction.py ------------------------------------------------------------------
# Model output contract for extraction (schema version 1). Validated again server-side.

SCHEMA_VERSION = "1"
PROMPT_VERSION = "extract_v1"
_ASSETS = Path(__file__).resolve().parent / "assets"

ObservationKind = Literal["payment", "refund_credit", "status_message", "claim", "other"]


class ObservationProposal(ApiModel):
    kind: ObservationKind
    amount_raw: str | None = None
    currency: str | None = None
    reference_raw: str | None = None
    reference_type: str | None = None
    status_raw: str | None = None
    direction_raw: str | None = None
    date_raw: str | None = None
    counterparty_raw: str | None = None
    account_scope_raw: str | None = None
    supporting_quote: str = ""
    issues: list[str] = Field(default_factory=list)


class ExtractionEnvelope(ApiModel):
    schema_version: Literal["1"]
    source_type: str
    language: str
    visible_text: str = ""
    observations: list[ObservationProposal] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)


@lru_cache
def load_extraction_json_schema() -> dict:
    return json.loads((_ASSETS / "extraction_v1.json").read_text(encoding="utf-8"))


@lru_cache
def load_extraction_prompt() -> str:
    return (_ASSETS / "extract_v1.txt").read_text(encoding="utf-8").strip()


# ---- from schemas/health.py ----------------------------------------------------------------------

class HealthResponse(ApiModel):
    status: str
    version: str
    app_env: str
    ai_mode: str
    demo_mode: bool
    public_fixtures_only: bool
    text_model: str
    speech_model: str
    build_id: str                    # which source this process was built from ("dev" outside an image built by scripts/build_id.py)
    migration: str | None            # the database revision this process found, so a deployment can be checked against the source
    features: list[str]              # the officer-assistant modules that are present, so an old image is told apart from a current one


# ---- from schemas/api.py -------------------------------------------------------------------------
# Request and response models for the case API (camelCase on the wire).

Role = Literal["citizen", "officer", "admin"]
Language = Literal["en", "hi", "hinglish"]
ReviewState = Literal["unreviewed", "user_confirmed", "reviewer_checked", "unresolved", "excluded"]
CaseReviewState = Literal["private", "submitted", "returned", "reviewed", "closed"]


# ---- session -------------------------------------------------------------------------------
class SessionOut(ApiModel):
    role: Role
    label: str | None
    demo_mode: bool
    expires_at: datetime
    csrf_token: str
    public_fixtures_only: bool
    ai_mode: Literal["live", "fixture"]
    # identity, when the session is bound to an account
    authenticated: bool = False
    account_kind: Role | None = None
    mobile_masked: str | None = None
    # opaque and stable for one account; never the database id, and never shared between two accounts (a masked phone number can be)
    account_ref: str | None = None
    officer_id: str | None = None
    display_name: str | None = None


# ---- auth ----------------------------------------------------------------------------------
class OtpRequest(ApiModel):
    mobile: str = Field(min_length=10, max_length=20)


class OtpRequestOut(ApiModel):
    expires_at: datetime
    resend_after_seconds: int
    #: shown only in demo mode, where no SMS is sent. Never populated in production.
    demo_code: str | None = None


class OtpVerify(ApiModel):
    mobile: str = Field(min_length=10, max_length=20)
    code: str = Field(pattern=r"^\d{6}$")


class OfficerLogin(ApiModel):
    officer_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


# ---- cases ---------------------------------------------------------------------------------
class CaseCreate(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    language: Language = "en"
    complaint_reference: str | None = Field(default=None, max_length=100)
    incident_account: str | None = Field(default=None, max_length=10_000)


class CaseSummary(ApiModel):
    id: str
    title: str
    language: Language
    complaint_reference: str | None
    current_revision: int
    last_reviewed_revision: int
    packet_status: str
    active_run_id: str | None
    created_at: datetime
    updated_at: datetime
    source_count: int
    demo_pack_id: str | None = None
    demo_steps_remaining: int = 0
    review_state: CaseReviewState = "private"
    submitted_at: datetime | None = None
    #: the citizen has asked for this record to be closed and no officer has answered yet
    closure_requested: bool = False
    #: Headline figures so a list of cases can be summarised without fetching each snapshot.
    #: Read from the last stamped revision, never recomputed here; `None` means nothing has been
    #: stamped yet, which is not the same as zero and must not be rendered as a figure.
    citizen_reported_minor: str | None = None
    reviewed_debit_minor: str | None = None
    open_question_count: int = 0


class DemoPackOut(ApiModel):
    pack_id: str
    title: str
    tags: list[str]
    initial_sources: int
    staged_updates: int
    has_audio: bool


class SourceOut(ApiModel):
    id: str
    kind: str
    provenance_kind: str
    media_type: str
    display_filename: str
    byte_size: int
    sha256: str
    status: str
    received_at: datetime
    added_in_revision: int
    image_width: int | None
    image_height: int | None
    text_content: str | None
    duplicate_of_source_id: str | None
    has_thumbnail: bool
    audio_duration_ms: int | None = None
    audio_source_id: str | None = None  # for transcripts: the recording they came from
    supersedes_source_id: str | None = None  # corrected transcript -> original transcript
    superseded_by_source_id: str | None = None
    transcript_turns: list[dict[str, Any]] | None = None
    transcription_mode: str | None = None  # live | fixture


class TranscriptCorrection(ApiModel):
    text: str = Field(min_length=1, max_length=10_000)
    expected_revision: int | None = None


class AnchorOut(ApiModel):
    source_id: str
    segment_id: str | None = None
    quote: str | None = None
    quote_verified: bool = False
    image_region: dict[str, float] | None = None
    audio_range_ms: dict[str, int] | None = None
    notes: list[str] = Field(default_factory=list)


class FactOut(ApiModel):
    fact_id: str
    version: int
    kind: str
    value: dict[str, Any] | None
    raw_text: str | None
    review_state: ReviewState
    issue: str | None
    anchors: list[AnchorOut]
    reviewed_by: str | None
    reviewed_at: datetime | None
    reason: str | None
    supersedes_version: int | None
    source_observation_id: str | None


class PaymentOut(ApiModel):
    id: str
    label: str
    included_in_incident: bool
    identity_review_state: ReviewState
    merged_into_payment_id: str | None
    excluded_reason: str | None
    confirmed_distinct_from: list[str]
    category: str
    category_reason: str | None
    fields: dict[str, FactOut]
    source_ids: list[str]
    observation_ids: list[str]


class DuplicateCandidateOut(ApiModel):
    payment_ids: list[str]
    basis: str
    issues: list[str]


class TotalsOut(ApiModel):
    reviewed_debit_minor: str
    reviewed_debit_ids: list[str]
    pending_minor: str
    pending_ids: list[str]
    unreviewed_ids: list[str]
    unresolved_ids: list[str]
    duplicate_candidate_ids: list[str]
    conflict_ids: list[str]
    failed_ids: list[str]
    credit_ids: list[str]
    non_inr_ids: list[str]
    excluded: list[dict[str, str]]
    merged_ids: list[str]
    categories: dict[str, str]
    citizen_reported_minor: str | None


class RunOut(ApiModel):
    id: str
    status: str
    stage: str
    base_revision: int
    input_source_ids: list[str]
    ai_mode: Literal["live", "fixture"]
    model_id: str
    prompt_version: str
    schema_version: str
    attempts: int
    usage: dict[str, Any]
    per_source_status: dict[str, Any]
    error_type: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RevisionOut(ApiModel):
    revision: int
    kind: str
    summary: str
    actor_role: str
    created_at: datetime


class ReviewEventOut(ApiModel):
    id: str
    revision: int
    actor_role: str
    action: str
    target_type: str
    target_id: str
    payload: dict[str, Any]
    created_at: datetime


class QuestionOut(ApiModel):
    id: str
    kind: str
    text: str
    priority: int
    state: Literal["open", "answered", "resolved", "dismissed"]
    related_payment_ids: list[str]
    related_fact_ids: list[str]
    related_source_ids: list[str]
    answer_source_id: str | None
    dismiss_reason: str | None
    created_revision: int
    resolved_revision: int | None
    shown: bool  # within the "at most five open questions at a time" window


class AnswerCreate(ApiModel):
    text: str = Field(min_length=1, max_length=10_000)
    expected_revision: int | None = None


class DismissCreate(ApiModel):
    reason: str = Field(min_length=1, max_length=500)
    expected_revision: int | None = None


class RefundOut(ApiModel):
    id: str
    label: str
    kind: str
    evidence_kind: str
    relates_to_incident: str
    identity_review_state: ReviewState
    merged_into_refund_id: str | None
    excluded_reason: str | None
    confirmed_distinct_from: list[str]
    category: str
    category_reason: str | None
    fields: dict[str, FactOut]
    source_ids: list[str]


class AllocationOut(ApiModel):
    id: str
    refund_id: str
    payment_id: str
    amount_minor: str
    state: str
    reason: str | None
    created_by_role: str
    created_revision: int
    invalidated_revision: int | None
    counted: bool
    issue: str | None


class RecoveryOut(ApiModel):
    state: str
    reviewed_debit_minor: str
    matched_refund_minor: str
    outstanding_minor: str
    eligible_refund_minor: str
    unallocated_eligible_minor: str
    claimed_refund_minor: str
    allocation_issues: list[dict[str, Any]]
    reasons: list[str]
    citizen_outstanding_minor: str | None
    citizen_outstanding_text: str | None


class OfficialStatusEventOut(ApiModel):
    id: str
    raw_status: str
    category: str
    issuer_as_supplied: str | None
    source_ids: list[str]
    effective_at: dict[str, Any] | None
    received_at: datetime
    entered_by_role: str
    review_state: ReviewState
    origin: str
    created_revision: int


class LatestStatusOut(ApiModel):
    event: OfficialStatusEventOut | None
    undated_count: int
    conflict: bool
    qualification: str
    message: str


class ResolutionOut(ApiModel):
    id: str
    revision: int
    kind: str
    recovery_state: str
    summary_text: str
    eligible: bool
    blocking_reasons: list[str]
    prepared_by_role: str
    request_note: str | None
    reviewed_at: datetime | None
    reviewed_by_role: str | None
    review_outcome: str | None
    review_note: str | None
    stale: bool
    created_at: datetime


class CaseSnapshot(ApiModel):
    case: CaseSummary
    sources: list[SourceOut]
    payments: list[PaymentOut]
    totals: TotalsOut
    duplicate_candidates: list[DuplicateCandidateOut]
    case_facts: dict[str, FactOut]
    questions: list[QuestionOut]
    refunds: list[RefundOut]
    allocations: list[AllocationOut]
    recovery: RecoveryOut
    official_status_events: list[OfficialStatusEventOut]
    latest_official_status: LatestStatusOut
    resolutions: list[ResolutionOut]
    runs: list[RunOut]
    revisions: list[RevisionOut]
    review_events: list[ReviewEventOut]
    ai_modes_used: list[str]


# ---- recovery / status / resolution requests -------------------------------------------------
RefundKind = Literal["merchant_refund", "bank_reversal", "recovered_funds", "unknown"]
StatusCategory = Literal["acknowledged", "information_requested", "forwarded", "under_review_or_investigation",
                         "closure_reported", "other", "unknown"]


class RefundClaimCreate(ApiModel):
    text: str = Field(min_length=1, max_length=2000)
    amount_raw: str | None = Field(default=None, max_length=50)
    kind: RefundKind = "unknown"
    date_text: str | None = Field(default=None, max_length=50)
    expected_revision: int | None = None


class AllocationCreate(ApiModel):
    refund_id: str
    payment_id: str
    amount_minor: str = Field(pattern=r"^\d{1,15}$")
    reason: str | None = Field(default=None, max_length=300)
    expected_revision: int | None = None


class OutstandingStatementCreate(ApiModel):
    text: str = Field(min_length=1, max_length=2000)
    outstanding_minor: str | None = Field(default=None, pattern=r"^\d{1,15}$")
    expected_revision: int | None = None


class StatusEventCreate(ApiModel):
    raw_status: str = Field(min_length=1, max_length=2000)
    category: StatusCategory = "unknown"
    issuer_as_supplied: str | None = Field(default=None, max_length=300)
    effective_at_text: str | None = Field(default=None, max_length=50)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    expected_revision: int | None = None


class ResolutionRequest(ApiModel):
    action: Literal["prepare", "review"]
    allow_partial: bool = False
    resolution_id: str | None = None
    outcome: Literal["packet_reviewed", "more_information_requested", "closure_agreed"] | None = None
    # On "prepare" this is the citizen's reason for asking that the record be closed; on "review"
    # it is the officer's note back to them. Same field, opposite directions.
    note: str | None = Field(default=None, max_length=1000)
    expected_revision: int | None = None


class UpdateAcknowledge(ApiModel):
    expected_revision: int | None = None


# ---- mutations -----------------------------------------------------------------------------
class RunCreate(ApiModel):
    source_ids: list[str] | None = None
    retry_run_id: str | None = None
    expected_revision: int | None = None


ReviewActionType = Literal[
    "confirm", "correct", "unresolved", "exclude", "include", "merge", "unmerge", "distinct", "relates", "unrelated"
]


class ReviewAction(ApiModel):
    type: ReviewActionType
    target_type: Literal["fact", "payment", "refund", "official_status"]
    target_id: str
    value: dict[str, Any] | None = None
    raw_text: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)
    other_payment_id: str | None = None


class ReviewBatch(ApiModel):
    expected_revision: int
    actions: list[ReviewAction] = Field(min_length=1, max_length=50)


class ExportCreate(ApiModel):
    format: Literal["html", "json"]
    expected_revision: int | None = None


class ExportOut(ApiModel):
    id: str
    revision: int
    format: Literal["html", "json"]
    content_digest: str
    ai_mode: str
    generated_at: datetime
    download_path: str


# ---- officer desk: bank check, what supports each payment, timeline, audit, suggested next steps -----------
class BankFieldOut(ApiModel):
    field: str
    claimed: str | None
    bank: str | None
    result: Literal["match", "differs", "not_stated"]


class BankCheckOut(ApiModel):
    result: Literal["matched", "differs", "not_found", "cannot_verify"]
    reason: str
    reference: str | None
    fields: list[BankFieldOut]
    ledger_version: str
    checked_at: datetime
    revision: int
    stale: bool  # the case changed after this check


SupportLevel = Literal["bank_matched", "bank_differs", "bank_not_found", "cannot_verify", "citizen_confirmed", "ai_proposed"]


class PaymentSupportOut(ApiModel):
    payment_id: str
    name: str
    level: SupportLevel
    label: str
    reasons: list[str]
    bank: BankCheckOut | None


class TimelineItemOut(ApiModel):
    at: datetime
    kind: str
    title: str
    detail: str | None = None
    level: SupportLevel | None = None
    source_ids: list[str] = Field(default_factory=list)
    payment_id: str | None = None


class AuditEventOut(ApiModel):
    id: str
    revision: int
    kind: str
    subject_type: str | None
    subject_id: str | None
    actor_role: str
    detail: dict[str, Any]
    created_at: datetime


ProposalKind = Literal["run_bank_check", "request_more_information", "record_packet_reviewed", "agree_closure", "wait"]


class ProposalOut(ApiModel):
    key: str
    kind: ProposalKind
    title: str
    reason: str
    payment_ids: list[str]
    note_draft: str | None
    origin: Literal["model", "rules"]
    decision: Literal["accepted", "dismissed"] | None = None


class NextActionsOut(ApiModel):
    id: str
    ai_mode: Literal["live", "fixture"]
    model_id: str
    generated_at: datetime
    revision: int
    stale: bool
    notice: str | None
    proposals: list[ProposalOut]


class OfficerDeskOut(ApiModel):
    ledger_name: str
    ledger_version: str
    notice: str
    payments: list[PaymentSupportOut]
    timeline: list[TimelineItemOut]
    audit: list[AuditEventOut]
    actions: NextActionsOut | None


class ActionDecision(ApiModel):
    proposal_set_id: str
    key: str
    decision: Literal["accepted", "dismissed"]
    note: str | None = Field(default=None, max_length=500)
