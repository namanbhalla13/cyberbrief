"""The whole persistence model. Facts are versioned by superseding a row, never by mutating one."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .core import Base

# ---- from models/_types.py -----------------------------------------------------------------------
# Shared column types and helpers for ORM models.

def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as UTC; SQLite drops tzinfo so it is re-attached on read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use timezone-aware values")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---- from models/session.py ----------------------------------------------------------------------

class UserSession(Base):
    """A browser session. Proves possession of a cookie; identity comes from `account_id`.

    `account_id` is NULL for the anonymous demo path, which still works without signing in.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="citizen")  # citizen | officer | admin
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    demo_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    # No delete-orphan: an account-owned case must outlive the session that created it.
    # Retention detaches those cases (session_id -> NULL) before deleting the session row.
    cases: Mapped[list[Case]] = relationship(  # noqa: F821
        back_populates="session", cascade="save-update, merge", passive_deletes=True
    )


# ---- from models/account.py ----------------------------------------------------------------------
# Accounts outlive sessions.
#
# A session proves possession of a cookie; an account is who that cookie currently speaks for.
# Cases hang off an account once someone signs in, so they survive the session purge.
#
# The officer account is a *simulated* reviewer for this demo. It is not a police account and is
# not connected to any government or banking system. Citizens hold no password:
# the first successful one-time code creates the account.

class Account(Base):
    """A citizen (mobile, no password) or staff, an officer or an admin (id + password). Never both."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("kind IN ('citizen','officer','admin')", name="ck_accounts_kind"),
        CheckConstraint(
            "(kind='citizen' AND mobile IS NOT NULL AND officer_id IS NULL AND password_hash IS NULL)"
            " OR (kind IN ('officer','admin') AND officer_id IS NOT NULL AND password_hash IS NOT NULL AND mobile IS NULL)",
            name="ck_accounts_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # citizen | officer | admin
    # SQLite allows many NULLs in a unique index, so one table serves both kinds.
    mobile: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    officer_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # officer only; scrypt string
    display_name: Mapped[str | None] = mapped_column(String(64))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OtpChallenge(Base):
    """One demo one-time code. The code itself is never stored, only a salted scrypt hash."""

    __tablename__ = "otp_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mobile: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # single use
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # which session asked, so a stale challenge can be traced; never used for authorisation
    requested_by_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL")
    )


# ---- from models/case.py -------------------------------------------------------------------------

class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Which session created this (provenance). NULL once that session has been purged.
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    # Ownership once someone signs in. NULL means an anonymous case, purged with its session.
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    # private -> submitted -> reviewed | returned. Only `submitted`+ are visible to an officer.
    review_state: Mapped[str] = mapped_column(
        String(24), default="private", server_default="private", index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by_account_id: Mapped[str | None] = mapped_column(String(36))
    # Routing and ownership. `area` is what the citizen said, never inferred; `assigned_officer_id` is who may open the case.
    area: Mapped[str | None] = mapped_column(String(48))
    assigned_officer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # submitted | awaiting_assignment | exception | assigned | under_review | ready_for_closure | closed; NULL while private
    workflow_state: Mapped[str | None] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(16), default="en")  # en | hi | hinglish
    complaint_reference: Mapped[str | None] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(8), default="1")
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String(36))
    # draft -> processing -> needs_review -> ready_for_export
    packet_status: Mapped[str] = mapped_column(String(32), default="draft")
    last_reviewed_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    demo_pack_id: Mapped[str | None] = mapped_column(String(40))  # set when loaded from a curated fixture pack
    demo_steps_loaded: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    session: Mapped[UserSession] = relationship(back_populates="cases")  # noqa: F821


class CaseRevision(Base):
    """Append-only log of committed case revisions."""

    __tablename__ = "case_revisions"
    __table_args__ = (UniqueConstraint("case_id", "revision", name="uq_case_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))  # created | sources_added | proposals | review | ...
    summary: Mapped[str] = mapped_column(Text, default="")
    actor_session_id: Mapped[str | None] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(32), default="system")
    totals_json: Mapped[dict | None] = mapped_column(JSON)  # accounting snapshot after this revision
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---- from models/source.py -----------------------------------------------------------------------

class Source(Base):
    """An original piece of supplied material. Bytes are never rewritten after ingestion."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    # image | typed_account | audio | transcript | user_answer | supplied_official_update
    kind: Mapped[str] = mapped_column(String(32))
    # citizen_upload | typed | answer | demo_fixture | derived
    provenance_kind: Mapped[str] = mapped_column(String(32), default="citizen_upload")
    media_type: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(255))  # relative to STORAGE_ROOT
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    display_filename: Mapped[str] = mapped_column(String(200))
    text_content: Mapped[str | None] = mapped_column(Text)  # typed accounts, answers, transcripts
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    audio_duration_ms: Mapped[int | None] = mapped_column(Integer)
    duplicate_of_source_id: Mapped[str | None] = mapped_column(String(36))  # identical bytes in case
    # stored | queued | processing | processed | failed | unreadable | recorded (captured deterministically, not for extraction)
    status: Mapped[str] = mapped_column(String(32), default="stored")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    added_in_revision: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class DerivedAsset(Base):
    __tablename__ = "derived_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))  # thumbnail | preview | audio_pcm16
    media_type: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255))
    processing_version: Mapped[str] = mapped_column(String(16), default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SourceSegment(Base):
    """A span of a source: text excerpt, audio turn, or image region."""

    __tablename__ = "source_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text)
    audio_start_ms: Mapped[int | None] = mapped_column(Integer)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer)
    image_region: Mapped[dict | None] = mapped_column(JSON)


# ---- from models/fact.py -------------------------------------------------------------------------

class Observation(Base):
    """A source-specific proposal from a processing run. Never authoritative on its own."""

    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("processing_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32))  # payment | refund_credit | status_message | claim
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # raw proposal fields
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)  # server-parsed values + issues
    anchors: Mapped[list] = mapped_column(JSON, default=list)
    quality_state: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|unreadable|invalid
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Fact(Base):
    """One version of a reviewed fact. `fact_id` is stable across versions; history is append-only."""

    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint("fact_id", "version", name="uq_fact_version"),
        Index("ix_facts_subject", "case_id", "subject_type", "subject_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fact_id: Mapped[str] = mapped_column(String(36), index=True, default=new_id)
    version: Mapped[int] = mapped_column(Integer, default=1)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(32))  # payment | case | refund | official_status
    subject_id: Mapped[str] = mapped_column(String(36))
    # amount | currency | reference | reference_type | account_scope | direction | status | occurred_at
    # | counterparty | citizen_reported_amount | incident_account | ...
    kind: Mapped[str] = mapped_column(String(48))
    value: Mapped[dict | None] = mapped_column(JSON)  # parsed value; money as integer strings
    raw_text: Mapped[str | None] = mapped_column(Text)
    anchors: Mapped[list] = mapped_column(JSON, default=list)
    review_state: Mapped[str] = mapped_column(String(32), default="unreviewed")
    issue: Mapped[str | None] = mapped_column(String(32))  # missing|unreadable|ambiguous|conflicting
    supersedes_version: Mapped[int | None] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))  # "citizen" | "officer"
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reason: Mapped[str | None] = mapped_column(Text)
    source_observation_id: Mapped[str | None] = mapped_column(String(36))
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---- from models/payment.py ----------------------------------------------------------------------

class Payment(Base):
    """Canonical payment identity. Field values live in current `facts` rows for this subject."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(16))  # P1, P2 ... stable human label
    included_in_incident: Mapped[bool] = mapped_column(Boolean, default=True)
    identity_review_state: Mapped[str] = mapped_column(String(32), default="unreviewed")
    merged_into_payment_id: Mapped[str | None] = mapped_column(String(36))
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    confirmed_distinct_from: Mapped[list] = mapped_column(JSON, default=list)
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PaymentObservation(Base):
    """Links a canonical payment to the source observations that describe it."""

    __tablename__ = "payment_observations"
    __table_args__ = (UniqueConstraint("payment_id", "observation_id", name="uq_payment_observation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    observation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("observations.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(36))
    link_state: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|confirmed|rejected
    created_revision: Mapped[int] = mapped_column(Integer, default=0)


# ---- from models/refund.py -----------------------------------------------------------------------

class Refund(Base):
    """A proposed or reviewed return of money. Field values live in `facts` (subject_type refund)."""

    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(16))  # R1, R2 ...
    # merchant_refund | bank_reversal | recovered_funds | unknown
    kind: Mapped[str] = mapped_column(String(32), default="unknown")
    # document: a credit record supplied as evidence; claim: the citizen said money came back
    evidence_kind: Mapped[str] = mapped_column(String(16), default="document")
    relates_to_incident: Mapped[str] = mapped_column(String(16), default="unconfirmed")  # unconfirmed|confirmed|denied
    identity_review_state: Mapped[str] = mapped_column(String(32), default="unreviewed")
    merged_into_refund_id: Mapped[str | None] = mapped_column(String(36))
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    confirmed_distinct_from: Mapped[list] = mapped_column(JSON, default=list)
    source_observation_id: Mapped[str | None] = mapped_column(String(36))
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RefundAllocation(Base):
    """An explicit reviewed relationship: part of a refund covers part of an original debit."""

    __tablename__ = "refund_allocations"
    __table_args__ = (UniqueConstraint("refund_id", "payment_id", name="uq_refund_allocation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    refund_id: Mapped[str] = mapped_column(String(36), ForeignKey("refunds.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[str] = mapped_column(String(36), ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)  # integer paise
    # proposed | user_confirmed | reviewer_checked | invalidated
    state: Mapped[str] = mapped_column(String(32), default="proposed")
    reason: Mapped[str | None] = mapped_column(Text)
    created_by_role: Mapped[str] = mapped_column(String(32), default="citizen")
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    invalidated_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OfficialStatusEvent(Base):
    """A supplied communication about the official complaint. Recorded, not verified; never inferred."""

    __tablename__ = "official_status_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    raw_status: Mapped[str] = mapped_column(Text)
    # acknowledged | information_requested | forwarded | under_review_or_investigation | closure_reported | other | unknown
    category: Mapped[str] = mapped_column(String(40), default="unknown")
    issuer_as_supplied: Mapped[str | None] = mapped_column(Text)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    effective_at: Mapped[dict | None] = mapped_column(JSON)  # ParsedDate.to_json(); None when unknown
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    entered_by_role: Mapped[str] = mapped_column(String(32), default="citizen")
    review_state: Mapped[str] = mapped_column(String(32), default="unreviewed")
    origin: Mapped[str] = mapped_column(String(40), default="user_supplied_communication")
    source_observation_id: Mapped[str | None] = mapped_column(String(36))
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ResolutionReview(Base):
    """A prepared resolution summary and the (simulated) reviewer's action on the CyberBrief packet."""

    __tablename__ = "resolution_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))  # full_recovery | partial_or_unresolved
    recovery_state: Mapped[str] = mapped_column(String(48))
    summary_text: Mapped[str] = mapped_column(Text)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    blocking_reasons: Mapped[list] = mapped_column(JSON, default=list)
    prepared_by_role: Mapped[str] = mapped_column(String(32))
    # The citizen's own reason for asking that this record be closed. Set only when the summary was
    # prepared as a closure request; its presence is what makes `closure_agreed` a valid outcome.
    request_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by_role: Mapped[str | None] = mapped_column(String(32))
    # packet_reviewed | more_information_requested | closure_agreed
    review_outcome: Mapped[str | None] = mapped_column(String(40))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseUpdate(Base):
    """Acknowledgement that changes since `base_revision` were reviewed; drives the change summary."""

    __tablename__ = "case_updates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    base_revision: Mapped[int] = mapped_column(Integer)
    new_revision: Mapped[int] = mapped_column(Integer)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    acknowledged_by_role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---- from models/question.py ---------------------------------------------------------------------

class Question(Base):
    """A targeted clarification derived deterministically from a real gap or conflict."""

    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("case_id", "key", name="uq_question_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    # duplicate_candidate | amount_conflict | status_conflict | missing_field | pending_status
    # | reported_vs_reviewed_gap | relative_date | unallocated_credit
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(200))  # stable dedupe key per issue
    text: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    related_payment_ids: Mapped[list] = mapped_column(JSON, default=list)
    related_fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    related_source_ids: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(16), default="open")  # open | answered | resolved | dismissed
    answer_source_id: Mapped[str | None] = mapped_column(String(36))
    dismiss_reason: Mapped[str | None] = mapped_column(Text)
    created_revision: Mapped[int] = mapped_column(Integer, default=0)
    resolved_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---- from models/run.py --------------------------------------------------------------------------

class ProcessingRun(Base):
    """One bounded processing run over new sources. Durable so restarts are honest."""

    __tablename__ = "processing_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    # queued | running | proposed | applied | failed | interrupted | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    base_revision: Mapped[int] = mapped_column(Integer)
    input_source_ids: Mapped[list] = mapped_column(JSON, default=list)
    input_hashes: Mapped[list] = mapped_column(JSON, default=list)
    ai_mode: Mapped[str] = mapped_column(String(16))  # live | fixture
    provider: Mapped[str] = mapped_column(String(32), default="meta")
    model_id: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    stage_timings_ms: Mapped[dict] = mapped_column(JSON, default=dict)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)  # sanitised, no provider payloads
    per_source_status: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ---- from models/review.py -----------------------------------------------------------------------

class ReviewEvent(Base):
    """Who did what to which target at which revision. The role is a simulated label."""

    __tablename__ = "review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    actor_session_id: Mapped[str] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(32))  # citizen | officer (historic rows: demo_reviewer)
    action: Mapped[str] = mapped_column(String(32))  # confirm|correct|unresolved|exclude|merge|unmerge|...
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ExportSnapshot(Base):
    __tablename__ = "export_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(8))  # html | json
    content_digest: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255))
    generation_version: Mapped[str] = mapped_column(String(16), default="1")
    ai_mode: Mapped[str] = mapped_column(String(16))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("session_id", "scope", "key", name="uq_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---- from models/assistant.py --------------------------------------------------------------------
# The citizen chat assistant.
#
# A conversation belongs to a signed-in citizen and, once a complaint is started from it (or the
# chat is opened from one), to that complaint too: deleting the complaint deletes the chat. The
# assistant's own messages are kept for audit. They are never evidence and never become a source.

class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(16), default="en")  # en | hi | hinglish, as the citizen writes
    # What the citizen said, each value with a quote of their own words. Steers the chat only;
    # slots never appear in a complaint.
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    last_suggestion: Mapped[str | None] = mapped_column(String(32))
    model_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "ordinal", name="uq_assistant_message_ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # citizen | assistant | event
    text: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)  # cards and buttons, built by code
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # topic, checks, model id, usage; never another case's data
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---- from models/__init__.py ---------------------------------------------------------------------
# ORM models. Importing this package registers every table on `Base.metadata`.



class AuditEvent(Base):
    """Append-only: what an officer did on a case that is not a change to the case itself.

    A simulated bank check, a set of suggested next steps, the officer's decision on one. The app never updates or
    deletes a row; they go only with the case. `actor_role` is a simulated label, as on `ReviewEvent`.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)  # the case revision the officer was looking at
    kind: Mapped[str] = mapped_column(String(32))  # bank_check | next_actions_proposed | action_decision
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(36))
    actor_session_id: Mapped[str] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


# ---- the three-role workflow: staff profiles, ownership, proposals, requests, tasks, assessments, links ----------

class OfficerProfile(Base):
    """Structured availability for a staff account: where they may work, in which languages, and how much.

    `active_count` is the number of cases currently assigned. It is changed only by a conditional UPDATE that
    also tests `active_count < capacity`, so two approvals competing for the last place cannot both succeed.
    """

    __tablename__ = "officer_profiles"

    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    areas: Mapped[list] = mapped_column(JSON, default=list)
    languages: Mapped[list] = mapped_column(JSON, default=list)
    expertise: Mapped[list] = mapped_column(JSON, default=list)
    on_duty: Mapped[bool] = mapped_column(Boolean, default=True)
    capacity: Mapped[int] = mapped_column(Integer, default=3)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    # Explicit powers beyond the base policy. "area_discovery": may search, as a minimal projection, reports assigned to other officers in the areas they cover.
    capabilities: Mapped[list | None] = mapped_column(JSON, default=list)


class AgentProposal(Base):
    """Something an agent worked out and a person must approve before it happens.

    `digest` covers the exact payload and `base_revision` is the case revision it was worked out against. An
    approval names both, so editing the payload, or the case moving on, makes the old approval unusable. The
    unique key on (case, kind, digest) makes a retried workflow find the proposal it already made.
    """

    __tablename__ = "agent_proposals"
    __table_args__ = (UniqueConstraint("case_id", "kind", "digest", name="uq_proposal_digest"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(24))  # coordinator | officer_assistant
    kind: Mapped[str] = mapped_column(String(24))  # assignment | info_request | bank_request
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(64))
    base_revision: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(12), default="rules")  # model | rules
    state: Mapped[str] = mapped_column(String(16), default="pending")  # pending | executed | rejected | stale
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    # how it was produced: origin, model, schema version, duration and reported usage. Not part of the payload, so it never changes a digest.
    telemetry: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseMessage(Base):
    """What passes between an officer and the citizen, or between an officer and a (simulated) bank.

    Kept apart from internal notes: every row here is addressed to someone. `simulated` marks the bank channel,
    which is a fictional outbox and inbox. A bank reply is matched to its request by `request_id`.
    """

    __tablename__ = "case_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(12))  # citizen | bank
    direction: Mapped[str] = mapped_column(String(12))  # outgoing | incoming
    request_id: Mapped[str | None] = mapped_column(String(36))  # the outgoing message a reply answers
    author_account_id: Mapped[str | None] = mapped_column(String(36))
    body: Mapped[str] = mapped_column(Text)
    payment_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(16), default="sent")  # sent | answered
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseTask(Base):
    """An open piece of work on a case. Several can be open at once; one is closed only by what the record shows."""

    __tablename__ = "case_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # acknowledge | citizen_info | bank_response | review_reply | follow_up
    title: Mapped[str] = mapped_column(String(200))
    ref_id: Mapped[str | None] = mapped_column(String(36))  # the message or proposal this waits on
    state: Mapped[str] = mapped_column(String(8), default="open")  # open | done
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ClaimAssessment(Base):
    """The officer's view of one claim. Append-only versions; the citizen's own words are never overwritten."""

    __tablename__ = "claim_assessments"
    __table_args__ = (UniqueConstraint("case_id", "claim_key", "version", name="uq_assessment_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    claim_key: Mapped[str] = mapped_column(String(64))  # payment:<id> | refund:<id>
    version: Mapped[int] = mapped_column(Integer, default=1)
    assessment: Mapped[str] = mapped_column(String(24))  # supported | needs_evidence | contradicted | possible_duplicate
    note: Mapped[str | None] = mapped_column(Text)
    officer_account_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RelatedLink(Base):
    """A possible relationship between two reports, found by an identifier they share, and what an officer decided."""

    __tablename__ = "related_links"
    __table_args__ = (UniqueConstraint("case_id", "other_case_id", "signal", "value", name="uq_related_link"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    other_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    signal: Mapped[str] = mapped_column(String(24))  # recipient_upi
    value: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(16), default="possible")  # possible | reviewed | dismissed
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AgentActivity(Base):
    """What an agent observably did, step by step, so a person can see its work and its sources."""

    __tablename__ = "agent_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(24))
    steps: Mapped[list] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class CaseEvent(Base):
    """Something that happened to a case which an agent may need to act on: submitted, evidence added, reply received, deadline due.

    Written in the same transaction as the change it describes, so it cannot be lost or invented. `idempotency_key`
    is unique, so a redelivered event is the same event. A worker claims it with a lease; a lease that lapses
    (a crash) lets another worker take it, up to `attempts` limits, and the outcome or error is kept on the row.
    """

    __tablename__ = "case_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_case_event_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # submitted | evidence_added | reply_received | deadline_due | routing_changed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | claimed | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(300))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CaseProjection(Base):
    """What the investigation tools may search about one submitted report: derived from its current facts and sources, never a private draft.

    `source_revision_digest` fingerprints exactly what the projection was built from, so a stored comparison or plan can tell it is out of date.
    The routing columns are copied so a search can be limited to the viewer's scope in SQL before any text is read.
    """

    __tablename__ = "case_projections"

    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    case_revision: Mapped[int] = mapped_column(Integer, default=0)
    projection_version: Mapped[str] = mapped_column(String(16))
    indexed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    source_revision_digest: Mapped[str] = mapped_column(String(64))
    visibility_policy_version: Mapped[str] = mapped_column(String(24))
    review_state: Mapped[str] = mapped_column(String(24), index=True)
    area: Mapped[str | None] = mapped_column(String(48), index=True)
    assigned_officer_id: Mapped[str | None] = mapped_column(String(36), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    method_tags: Mapped[list] = mapped_column(JSON, default=list)      # [{tag, label, sourceId, span, quote, reviewState, definitionVersion}]
    event_sequence: Mapped[list] = mapped_column(JSON, default=list)   # [{stage, label, sourceId, quote, state}]
    outcome_refs: Mapped[dict] = mapped_column(JSON, default=dict)     # ids of reviewed resolutions, lessons and refunds: references, never a "solved" flag


class EntityObservation(Base):
    """One identifier a source states, with where it was read and how much anyone has checked it. Retracted, never edited, when the source changes."""

    __tablename__ = "entity_observations"
    __table_args__ = (Index("ix_entity_lookup", "entity_type", "normalized_value", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    case_revision: Mapped[int] = mapped_column(Integer)
    projection_version: Mapped[str] = mapped_column(String(16))
    entity_type: Mapped[str] = mapped_column(String(24))        # upi_vpa | transaction_reference | payee_name
    normalized_value: Mapped[str] = mapped_column(String(200))
    display_value: Mapped[str] = mapped_column(String(200))
    raw_text: Mapped[str | None] = mapped_column(Text)
    quality: Mapped[str] = mapped_column(String(12), default="full")   # full | partial | generic
    source_id: Mapped[str | None] = mapped_column(String(36))
    fact_id: Mapped[str | None] = mapped_column(String(36))
    source_version: Mapped[int | None] = mapped_column(Integer)         # the fact version it was read from
    field_pointer: Mapped[str] = mapped_column(String(80))              # payment:<id>/counterparty
    assertion_origin: Mapped[str] = mapped_column(String(24))           # reported | extracted | corrected | reviewed
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    state: Mapped[str] = mapped_column(String(12), default="active", index=True)   # active | retracted
    retracted_revision: Mapped[int | None] = mapped_column(Integer)
    supersedes_version: Mapped[int | None] = mapped_column(Integer)


class CaseCandidate(Base):
    """An earlier report one officer was shown as a possible connection to a case, and what that officer decided.

    The id is the opaque handle the API uses, so a request never names another report directly. It is tied to the officer it was shown to.
    """

    __tablename__ = "case_candidates"
    __table_args__ = (UniqueConstraint("case_id", "other_case_id", "viewer_account_id", name="uq_case_candidate"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    other_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    viewer_account_id: Mapped[str] = mapped_column(String(36), index=True)
    result_kind: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(12), default="possible")   # possible | reviewed | dismissed
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    signals: Mapped[list] = mapped_column(JSON, default=list)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)             # both cases' projection digests when last shown
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class InvestigationRun(Base):
    """One officer goal worked through bounded tools. What ran, in order, is on the row, so the trace shown is the trace executed."""

    __tablename__ = "investigation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    officer_account_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36))
    goal: Mapped[str] = mapped_column(String(400))
    # queued | running | waiting_for_approval | completed | failed | cancelled | stale
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    plan: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    input_revisions: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(24))
    proposal_ids: Mapped[list] = mapped_column(JSON, default=list)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(12), default="rules")     # live | fixture | rules
    provider: Mapped[str | None] = mapped_column(String(24))
    model_id: Mapped[str | None] = mapped_column(String(80))
    telemetry: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(300))
    stale_reason: Mapped[str | None] = mapped_column(String(120))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    phase: Mapped[int] = mapped_column(Integer, default=1)               # 1 = the first investigation; 2 = an update after a consulted officer replied
    parent_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trigger: Mapped[dict] = mapped_column(JSON, default=dict)            # what started a phase 2 run: {consultationId, messageId}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class HandlingLesson(Base):
    """What an officer concluded from handling a report, and how it ended as far as anyone recorded. Append-only: a change is a new version.

    The outcome is the officer's own words about what the records show; it is never inferred from a message arriving or a packet being closed.
    """

    __tablename__ = "handling_lessons"
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_lesson_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    outcome_kind: Mapped[str] = mapped_column(String(16))   # successful | partial | unresolved | declined
    text: Mapped[str] = mapped_column(Text)
    author_account_id: Mapped[str] = mapped_column(String(36))
    review_state: Mapped[str] = mapped_column(String(16), default="entered")   # entered | reviewed
    case_revision: Mapped[int] = mapped_column(Integer)
    record_refs: Mapped[list] = mapped_column(JSON, default=list)   # [{type, id}]: the messages, tasks or reviews the lesson rests on
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Consultation(Base):
    """One officer's question to another about an earlier report, and the answer.

    It belongs to the sender's case and names the earlier case it is about. `payload` is exactly what the recipient will be shown, covered by
    `payload_digest`; an approval names that digest, so editing the question makes the old approval useless. The recipient is whoever holds the
    earlier report *when the sender approves*, resolved on the server and stored here. States: draft, sent, replied, closed, cancelled.
    """

    __tablename__ = "consultations"
    __table_args__ = (UniqueConstraint("case_id", "earlier_case_id", "sender_account_id", "payload_digest", name="uq_consultation_digest"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    earlier_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    sender_account_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_account_id: Mapped[str | None] = mapped_column(String(36), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36))            # the connection handle this was prepared from
    run_id: Mapped[str | None] = mapped_column(String(36))                  # the investigation that prepared it, if one did
    state: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_digest: Mapped[str] = mapped_column(String(64))
    approved_digest: Mapped[str | None] = mapped_column(String(64))
    source_revisions: Mapped[dict] = mapped_column(JSON, default=dict)      # both reports' revisions and source digests when drafted
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sender_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recipient_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ConsultationMessage(Base):
    """The append-only record of a consultation: the approved request, each reply, and the events that changed its state. Never edited or deleted."""

    __tablename__ = "consultation_messages"
    __table_args__ = (UniqueConstraint("consultation_id", "seq", name="uq_consultation_seq"), UniqueConstraint("consultation_id", "idempotency_key", name="uq_consultation_idem"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    consultation_id: Mapped[str] = mapped_column(String(36), ForeignKey("consultations.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))                           # request | reply | system
    author_account_id: Mapped[str | None] = mapped_column(String(36))
    body: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)                  # a reply's structured part; a system event's name
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TransactionVerification(Base):
    """One attempt to verify a reported transaction with a bank integration. Append-only history: a later attempt is a new row.

    `status` is what the attempt established: checking, bank_confirmed, discrepancy, no_match, more_info_required, unavailable. `simulated`
    and `mode` say where the answer came from; only a row with mode "live" and simulated false is a production confirmation. `returned` holds
    only what the bank returned. `input_summary` holds masked identifiers, never a whole one. While a row is "checking" its `inflight` is set,
    and the unique key on (payment, inflight) lets only one attempt per payment be in flight, so a double click cannot call the bank twice.
    """

    __tablename__ = "transaction_verifications"
    __table_args__ = (UniqueConstraint("payment_id", "inflight", name="uq_verification_inflight"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[str] = mapped_column(String(36), index=True)
    requested_by_account_id: Mapped[str | None] = mapped_column(String(36))
    provider_id: Mapped[str | None] = mapped_column(String(40))
    provider_name: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(12), default="none")  # live | sandbox | none
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="checking")
    inflight: Mapped[str | None] = mapped_column(String(8))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)  # this payment's nth attempt
    provider_calls: Mapped[int] = mapped_column(Integer, default=0)  # calls made inside this attempt (a timeout may be retried once)
    input_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    response_reference: Mapped[str | None] = mapped_column(String(80))
    returned: Mapped[dict | None] = mapped_column(JSON)
    comparison: Mapped[list | None] = mapped_column(JSON)
    case_revision: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class BankRequest(Base):
    """An approved written request to a bank, and what is truthfully known about its delivery.

    A request is a draft (an `AgentProposal`) until the officer approves it; this row starts at approval. States: approved (not sent),
    queued (claimed for sending), sent (the email service accepted it, or the simulated outbox took it), failed, response_received.
    `delivery` is email, simulated or manual. Accepted by the email service is not delivered. `response_ref` is the reference a reply quotes.
    """

    __tablename__ = "bank_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(36), index=True)
    message_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payment_ids: Mapped[list] = mapped_column(JSON, default=list)
    bank_id: Mapped[str] = mapped_column(String(40))
    bank_name: Mapped[str] = mapped_column(String(120))
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    response_ref: Mapped[str] = mapped_column(String(24), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    delivery: Mapped[str] = mapped_column(String(12), default="manual")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    approved_by_account_id: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


__all__ = [
    "TransactionVerification",
    "BankRequest",
    "Consultation",
    "ConsultationMessage",
    "CaseProjection",
    "EntityObservation",
    "CaseCandidate",
    "InvestigationRun",
    "HandlingLesson",
    "CaseEvent",
    "AgentProposal",
    "AgentActivity",
    "Account",
    "AssistantConversation",
    "AssistantMessage",
    "AuditEvent",
    "Case",
    "CaseMessage",
    "CaseRevision",
    "CaseTask",
    "ClaimAssessment",
    "CaseUpdate",
    "DerivedAsset",
    "ExportSnapshot",
    "Fact",
    "IdempotencyKey",
    "Observation",
    "OfficialStatusEvent",
    "OfficerProfile",
    "OtpChallenge",
    "RelatedLink",
    "Payment",
    "PaymentObservation",
    "ProcessingRun",
    "Question",
    "Refund",
    "RefundAllocation",
    "ResolutionReview",
    "ReviewEvent",
    "Source",
    "SourceSegment",
    "UserSession",
]
