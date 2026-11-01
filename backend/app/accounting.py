"""The accounting rules: payment identity, totals, recovery, questions and resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import ApiError, Settings
from .models import (
    Case,
    Fact,
    Observation,
    OfficialStatusEvent,
    Payment,
    Question,
    Refund,
    RefundAllocation,
    ResolutionReview,
    UserSession,
    utcnow,
)
from .parsing import MoneyParseError, format_inr, parse_amount
from .records import Storage, current_facts, new_fact

# ---- from services/reconciliation.py -------------------------------------------------------------
# Payment identity, duplicate candidates and reviewed accounting. Deterministic, integer paise.
#
# Pure functions over `PaymentView` records (built from ORM rows by the case service). The model
# never computes these totals. Rules:
# - a payment counts in the reviewed debit baseline only when its critical fields and identity are
#   reviewed, currency is INR, direction is debit, status is successful, it is included in the
#   incident, and it is not part of an unresolved duplicate/conflict;
# - same amount/date alone never makes a duplicate; same full reference + type + scope + currency
#   makes a *candidate* that a human must confirm; a confirmed duplicate contributes once.

ReviewState = Literal["unreviewed", "user_confirmed", "reviewer_checked", "unresolved", "excluded"]
Direction = Literal["debit", "credit", "unknown"]
PaymentStatus = Literal["successful", "pending", "failed", "unknown"]

REVIEWED_STATES = {"user_confirmed", "reviewer_checked"}
CRITICAL_FIELDS = ("amount", "currency", "reference", "direction", "status")


@dataclass
class PaymentView:
    id: str
    case_id: str
    amount_minor: int | None = None
    currency: str | None = None
    reference: str | None = None
    reference_type: str | None = None
    account_scope: str | None = None
    direction: Direction = "unknown"
    status: PaymentStatus = "unknown"
    included_in_incident: bool = True
    field_review: dict[str, str] = field(default_factory=dict)
    identity_review_state: str = "unreviewed"
    merged_into: str | None = None
    excluded_reason: str | None = None
    reference_truncated: bool = False
    confirmed_distinct_from: set[str] = field(default_factory=set)

    def field_state(self, name: str) -> str:
        return self.field_review.get(name, "unreviewed")

    def critical_fields_reviewed(self) -> bool:
        return all(self.field_state(name) in REVIEWED_STATES for name in CRITICAL_FIELDS)


@dataclass(frozen=True)
class DuplicateCandidate:
    payment_ids: tuple[str, str]
    basis: str
    issues: tuple[str, ...]

    @property
    def is_conflict(self) -> bool:
        return any(i in ("amount_conflict", "status_conflict", "currency_conflict") for i in self.issues)


@dataclass
class Totals:
    reviewed_debit_minor: int = 0
    reviewed_debit_ids: list[str] = field(default_factory=list)
    pending_minor: int = 0
    pending_ids: list[str] = field(default_factory=list)
    unreviewed_ids: list[str] = field(default_factory=list)
    unresolved_ids: list[str] = field(default_factory=list)
    duplicate_candidate_ids: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    credit_ids: list[str] = field(default_factory=list)
    non_inr_ids: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    merged_ids: list[str] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str | None] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "reviewedDebitMinor": str(self.reviewed_debit_minor),
            "reviewedDebitIds": self.reviewed_debit_ids,
            "pendingMinor": str(self.pending_minor),
            "pendingIds": self.pending_ids,
            "unreviewedIds": self.unreviewed_ids,
            "unresolvedIds": self.unresolved_ids,
            "duplicateCandidateIds": self.duplicate_candidate_ids,
            "conflictIds": self.conflict_ids,
            "failedIds": self.failed_ids,
            "creditIds": self.credit_ids,
            "nonInrIds": self.non_inr_ids,
            "excluded": [{"paymentId": pid, "reason": reason} for pid, reason in self.excluded],
            "mergedIds": self.merged_ids,
            "categories": self.categories,
        }


def normalise_reference(reference: str | None) -> str | None:
    """Trim whitespace only. Leading zeros and case are significant."""
    if reference is None:
        return None
    cleaned = "".join(reference.split())
    return cleaned or None


def find_duplicate_candidates(payments: list[PaymentView]) -> list[DuplicateCandidate]:
    """Candidates share a full reference. Amount/date similarity alone is never a basis."""
    active = [p for p in payments if p.merged_into is None]
    candidates: list[DuplicateCandidate] = []
    for i, a in enumerate(active):
        ref_a = normalise_reference(a.reference)
        if ref_a is None or a.reference_truncated:
            continue
        for b in active[i + 1 :]:
            ref_b = normalise_reference(b.reference)
            if ref_b is None or b.reference_truncated or ref_a != ref_b:
                continue
            if b.id in a.confirmed_distinct_from or a.id in b.confirmed_distinct_from:
                continue
            if a.reference_type and b.reference_type and a.reference_type != b.reference_type:
                continue
            if a.account_scope and b.account_scope and a.account_scope != b.account_scope:
                continue  # same identifier in another account scope is not a duplicate
            issues: list[str] = []
            if not a.account_scope or not b.account_scope:
                issues.append("scope_unknown")
            if a.currency and b.currency and a.currency != b.currency:
                issues.append("currency_conflict")
            if a.amount_minor is not None and b.amount_minor is not None and a.amount_minor != b.amount_minor:
                issues.append("amount_conflict")
            if a.status != b.status and "unknown" not in (a.status, b.status):
                issues.append("status_conflict")
            candidates.append(DuplicateCandidate((a.id, b.id), "same_reference", tuple(issues)))
    return candidates


def categorise(payment: PaymentView, in_open_candidate: bool, in_conflict: bool) -> tuple[str, str | None]:
    """Return (category, reason). Order matters: the first applicable rule wins."""
    if payment.merged_into is not None:
        return "merged_duplicate", f"counted once via {payment.merged_into}"
    if payment.excluded_reason or payment.identity_review_state == "excluded":
        return "excluded", payment.excluded_reason or "excluded by reviewer"
    if not payment.included_in_incident:
        return "excluded", "not part of this incident"
    if in_conflict:
        return "conflict", "conflicting observations for one identity"
    if in_open_candidate:
        return "duplicate_candidate", "possible duplicate awaiting confirmation"
    if payment.identity_review_state == "unresolved" or any(
        payment.field_state(f) == "unresolved" for f in CRITICAL_FIELDS
    ):
        return "unresolved", "a critical field is unresolved"
    if not payment.critical_fields_reviewed() or payment.identity_review_state not in REVIEWED_STATES:
        return "unreviewed", "critical fields or identity not yet reviewed"
    if payment.currency != "INR":
        return "non_inr", f"currency {payment.currency or 'unknown'} is outside INR accounting"
    if payment.direction == "credit":
        return "credit", "incoming credit is not a debit"
    if payment.direction != "debit":
        return "unresolved", "direction unknown"
    if payment.status == "failed":
        return "failed", "failed payment"
    if payment.status == "pending":
        return "pending", "pending payment"
    if payment.status != "successful":
        return "unresolved", "status unknown"
    if payment.amount_minor is None:
        return "unresolved", "amount missing"
    return "reviewed_debit", None


@dataclass
class RefundView(PaymentView):
    """A refund record viewed like a payment, plus what makes it count as money received."""

    relates_to_incident: str = "unconfirmed"  # unconfirmed | confirmed | denied
    evidence_kind: str = "document"  # document | claim
    refund_kind: str = "unknown"


@dataclass(frozen=True)
class AllocationView:
    id: str
    refund_id: str
    payment_id: str
    amount_minor: int
    state: str  # proposed | user_confirmed | reviewer_checked | invalidated


RECOVERY_STATES = (
    "not_assessed",
    "no_refund_reported",
    "refund_reported_awaiting_review",
    "partial_recovery_reviewed",
    "full_recovery_reported_awaiting_review",
    "reviewed_baseline_fully_offset",
    "discrepancy_needs_review",
)


@dataclass
class Recovery:
    state: str
    reviewed_debit_minor: int
    matched_refund_minor: int
    outstanding_minor: int
    eligible_refund_minor: int = 0
    unallocated_eligible_minor: int = 0
    claimed_refund_minor: int = 0
    refund_categories: dict[str, str] = field(default_factory=dict)
    refund_reasons: dict[str, str | None] = field(default_factory=dict)
    allocation_issues: list[dict] = field(default_factory=list)
    counted_allocation_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "reviewedDebitMinor": str(self.reviewed_debit_minor),
            "matchedRefundMinor": str(self.matched_refund_minor),
            "outstandingMinor": str(self.outstanding_minor),
            "eligibleRefundMinor": str(self.eligible_refund_minor),
            "unallocatedEligibleMinor": str(self.unallocated_eligible_minor),
            "claimedRefundMinor": str(self.claimed_refund_minor),
            "refundCategories": self.refund_categories,
            "refundReasons": self.refund_reasons,
            "allocationIssues": self.allocation_issues,
            "countedAllocationIds": self.counted_allocation_ids,
            "reasons": self.reasons,
        }


def categorise_refund(r: RefundView, in_open_candidate: bool, in_conflict: bool) -> tuple[str, str | None]:
    """A refund enters reviewed recovery accounting only when every condition holds."""
    if r.merged_into is not None:
        return "merged_duplicate", f"counted once via {r.merged_into}"
    if r.excluded_reason or r.identity_review_state == "excluded":
        return "excluded", r.excluded_reason or "excluded by reviewer"
    if in_conflict:
        return "conflict", "conflicting observations for one refund"
    if in_open_candidate:
        return "duplicate_candidate", "possible duplicate refund awaiting confirmation"
    if r.evidence_kind == "claim":
        return "claim_only", "reported by the citizen; no credit record has been reviewed yet"
    if r.identity_review_state == "unresolved" or any(r.field_state(f) == "unresolved" for f in CRITICAL_FIELDS):
        return "unresolved", "a critical field is unresolved"
    if not r.critical_fields_reviewed() or r.identity_review_state not in REVIEWED_STATES:
        return "unreviewed", "critical fields or identity not yet reviewed"
    if r.currency != "INR":
        return "non_inr", f"currency {r.currency or 'unknown'} is outside INR accounting"
    if r.direction != "credit":
        return "not_credit", "not an incoming credit"
    if r.status == "pending":
        return "not_received", "initiated, pending, frozen or requested money is not money received"
    if r.status == "failed":
        return "failed", "failed credit"
    if r.status != "successful":
        return "unresolved", "status unknown"
    if r.relates_to_incident != "confirmed":
        return "relation_unconfirmed", "the citizen has not confirmed this credit relates to the incident"
    if r.amount_minor is None:
        return "unresolved", "amount missing"
    return "eligible", None


def compute_recovery(
    totals: Totals,
    payments: list[PaymentView],
    refunds: list[RefundView],
    allocations: list[AllocationView],
    *,
    citizen_outstanding_minor: int | None = None,
) -> Recovery:
    """D = reviewed debits, R = eligible confirmed allocations, O = D - R. Never clamps; flags instead."""
    debit_amounts = {p.id: p.amount_minor or 0 for p in payments if p.id in totals.reviewed_debit_ids}
    d_total = totals.reviewed_debit_minor

    candidates = find_duplicate_candidates(refunds)
    open_ids: set[str] = set()
    conflict_ids: set[str] = set()
    for cand in candidates:
        for rid in cand.payment_ids:
            open_ids.add(rid)
            if cand.is_conflict:
                conflict_ids.add(rid)
    categories: dict[str, str] = {}
    reasons: dict[str, str | None] = {}
    eligible_amounts: dict[str, int] = {}
    claimed = 0
    for r in refunds:
        category, reason = categorise_refund(r, r.id in open_ids, r.id in conflict_ids)
        categories[r.id] = category
        reasons[r.id] = reason
        if category == "eligible":
            eligible_amounts[r.id] = r.amount_minor or 0
        elif category == "claim_only" and r.amount_minor:
            claimed += r.amount_minor

    issues: list[dict] = []
    issue_ids: set[str] = set()
    confirmed = [a for a in allocations if a.state in REVIEWED_STATES]
    per_refund: dict[str, int] = {}
    per_debit: dict[str, int] = {}
    for a in confirmed:
        if a.refund_id not in eligible_amounts:
            issues.append({"allocationId": a.id, "issue": "refund_not_eligible", "refundId": a.refund_id})
            issue_ids.add(a.id)
        elif a.payment_id not in debit_amounts:
            issues.append({"allocationId": a.id, "issue": "debit_not_eligible", "paymentId": a.payment_id})
            issue_ids.add(a.id)
        elif a.amount_minor <= 0:
            issues.append({"allocationId": a.id, "issue": "non_positive"})
            issue_ids.add(a.id)
        else:
            per_refund[a.refund_id] = per_refund.get(a.refund_id, 0) + a.amount_minor
            per_debit[a.payment_id] = per_debit.get(a.payment_id, 0) + a.amount_minor
    for rid, total in per_refund.items():
        if total > eligible_amounts[rid]:
            for a in confirmed:
                if a.refund_id == rid and a.id not in issue_ids:
                    issues.append({"allocationId": a.id, "issue": "refund_overallocated", "refundId": rid})
                    issue_ids.add(a.id)
    for pid, total in per_debit.items():
        if total > debit_amounts[pid]:
            for a in confirmed:
                if a.payment_id == pid and a.id not in issue_ids:
                    issues.append({"allocationId": a.id, "issue": "debit_overallocated", "paymentId": pid})
                    issue_ids.add(a.id)

    counted = [a for a in confirmed if a.id not in issue_ids]
    r_total = sum(a.amount_minor for a in counted)
    invalidated = [a for a in allocations if a.state == "invalidated"]
    eligible_total = sum(eligible_amounts.values())
    allocated_from_eligible = sum(a.amount_minor for a in counted)

    why: list[str] = []
    if d_total == 0:
        state = "not_assessed"
        why.append("no reviewed debit baseline yet")
    elif issues or invalidated or conflict_ids or r_total > d_total:
        state = "discrepancy_needs_review"
        if issues:
            why.append(f"{len(issues)} allocation issue(s)")
        if invalidated:
            why.append(f"{len(invalidated)} allocation(s) invalidated by a later correction")
        if conflict_ids:
            why.append("conflicting refund observations")
        if r_total > d_total:
            why.append("matched refunds exceed the reviewed baseline")
    elif not refunds:
        state = "no_refund_reported"
    elif citizen_outstanding_minor == 0 and r_total < d_total:
        state = "full_recovery_reported_awaiting_review"
        why.append("the citizen reports nothing outstanding, but reviewed allocations do not yet cover the baseline")
    elif r_total == d_total:
        state = "reviewed_baseline_fully_offset"
    elif r_total > 0:
        state = "partial_recovery_reviewed"
    else:
        state = "refund_reported_awaiting_review"
        why.append("refunds exist but none is reviewed and allocated yet")

    outstanding = d_total - r_total if r_total <= d_total else 0
    return Recovery(
        state=state,
        reviewed_debit_minor=d_total,
        matched_refund_minor=r_total if r_total <= d_total else 0,
        outstanding_minor=outstanding,
        eligible_refund_minor=eligible_total,
        unallocated_eligible_minor=max(0, eligible_total - allocated_from_eligible),
        claimed_refund_minor=claimed,
        refund_categories=categories,
        refund_reasons=reasons,
        allocation_issues=issues,
        counted_allocation_ids=[a.id for a in counted],
        reasons=why,
    )


def compute_totals(payments: list[PaymentView]) -> Totals:
    totals = Totals()
    candidates = find_duplicate_candidates(payments)
    open_candidate_ids: set[str] = set()
    conflict_ids: set[str] = set()
    for cand in candidates:
        for pid in cand.payment_ids:
            open_candidate_ids.add(pid)
            if cand.is_conflict:
                conflict_ids.add(pid)

    for p in payments:
        category, reason = categorise(p, p.id in open_candidate_ids, p.id in conflict_ids)
        totals.categories[p.id] = category
        totals.reasons[p.id] = reason
        if category == "reviewed_debit":
            assert p.amount_minor is not None
            totals.reviewed_debit_minor += p.amount_minor
            totals.reviewed_debit_ids.append(p.id)
        elif category == "pending":
            totals.pending_minor += p.amount_minor or 0
            totals.pending_ids.append(p.id)
        elif category == "unreviewed":
            totals.unreviewed_ids.append(p.id)
        elif category == "unresolved":
            totals.unresolved_ids.append(p.id)
        elif category == "duplicate_candidate":
            totals.duplicate_candidate_ids.append(p.id)
        elif category == "conflict":
            totals.conflict_ids.append(p.id)
        elif category == "failed":
            totals.failed_ids.append(p.id)
        elif category == "credit":
            totals.credit_ids.append(p.id)
        elif category == "non_inr":
            totals.non_inr_ids.append(p.id)
        elif category == "merged_duplicate":
            totals.merged_ids.append(p.id)
        if category in ("excluded", "failed", "credit", "non_inr") and reason:
            totals.excluded.append((p.id, reason))
    return totals


# ---- from services/official_status.py ------------------------------------------------------------
# Official complaint status: recorded from supplied communications, never inferred from recovery.
#
# The latest status is chosen by *effective* date among confirmed events. A notice uploaded later
# with an older effective date does not supersede a newer one. Unknown or tied dates are reported
# as uncertainty rather than resolved silently.

CATEGORIES = (
    "acknowledged",
    "information_requested",
    "forwarded",
    "under_review_or_investigation",
    "closure_reported",
    "other",
    "unknown",
)
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "closure_reported": ("closed", "disposed", "resolved", "closure"),
    "information_requested": ("provide", "additional information", "documents required", "clarif", "furnish", "submit the"),
    "forwarded": ("forwarded", "transferred", "escalated", "referred to"),
    "under_review_or_investigation": ("under investigation", "under process", "being examined", "in progress", "under review", "investigation"),
    "acknowledged": ("acknowledg", "received your complaint", "registered", "has been recorded", "complaint number"),
}
QUALIFICATION = "Recorded from a supplied update; not live government tracking."


def map_status_category(text: str | None) -> str:
    if not text:
        return "unknown"
    lowered = text.casefold()
    for category, words in _CATEGORY_KEYWORDS.items():  # order matters: closure beats acknowledgement
        if any(w in lowered for w in words):
            return category
    return "other"


@dataclass(frozen=True)
class StatusView:
    id: str
    raw_status: str
    category: str
    review_state: str
    received_at: datetime
    effective_day: date | None  # None when unknown or unresolved
    issuer: str | None = None


def status_views(db, case) -> list[StatusView]:
    """ORM glue: build StatusView records for a case."""
    from sqlalchemy import select

    from .models import OfficialStatusEvent

    rows = db.scalars(select(OfficialStatusEvent).where(OfficialStatusEvent.case_id == case.id).order_by(OfficialStatusEvent.received_at)).all()
    views: list[StatusView] = []
    for e in rows:
        effective_day: date | None = None
        if e.effective_at and e.effective_at.get("kind") != "unresolved":
            raw = e.effective_at.get("date") or (e.effective_at.get("instantUtc") or e.effective_at.get("localDatetime") or "")[:10]
            try:
                effective_day = date.fromisoformat(raw) if raw else None
            except ValueError:
                effective_day = None
        views.append(StatusView(id=e.id, raw_status=e.raw_status, category=e.category, review_state=e.review_state,
                                received_at=e.received_at, effective_day=effective_day, issuer=e.issuer_as_supplied))
    return views


def select_latest(events: list[StatusView]) -> dict:
    """Pick the latest confirmed event by effective date; report undated events and ties as uncertainty."""
    confirmed = [e for e in events if e.review_state in ("user_confirmed", "reviewer_checked")]
    if not confirmed:
        return {"event": None, "undatedCount": 0, "conflict": False, "qualification": QUALIFICATION,
                "message": "No official update recorded."}
    dated = [e for e in confirmed if e.effective_day is not None]
    undated = [e for e in confirmed if e.effective_day is None]
    if not dated:
        # nothing has a known effective date: show the most recently recorded, flagged as undated
        latest = max(undated, key=lambda e: e.received_at)
        return {"event": latest, "undatedCount": len(undated), "conflict": len(undated) > 1, "qualification": QUALIFICATION,
                "message": "Effective date unknown for every recorded update; showing the most recently recorded one."}
    top_day = max(e.effective_day for e in dated)  # type: ignore[type-var]
    top = [e for e in dated if e.effective_day == top_day]
    latest = max(top, key=lambda e: e.received_at)
    conflict = len({e.category for e in top}) > 1
    message = f"Latest by effective date ({top_day.isoformat()})."
    if conflict:
        message += " Two or more updates share this date with different statuses; both are shown in history."
    if undated:
        message += f" {len(undated)} update(s) without a known effective date are also recorded."
    return {"event": latest, "undatedCount": len(undated), "conflict": conflict, "qualification": QUALIFICATION, "message": message}


# ---- from services/refunds.py --------------------------------------------------------------------
# Refund records, allocations and the server-derived recovery summary (ORM glue over reconciliation).

REFUND_KINDS = ("merchant_refund", "bank_reversal", "recovered_funds", "unknown")


def _value(facts: dict[str, Fact], kind: str, key: str = "value"):
    fact = facts.get(kind)
    if fact is None or not fact.value:
        return None
    return fact.value.get(key)


def refund_views(db: Session, case: Case) -> tuple[list[Refund], list[RefundView], dict[str, dict[str, Fact]]]:
    refunds = db.scalars(select(Refund).where(Refund.case_id == case.id).order_by(Refund.created_at, Refund.label)).all()
    facts_by: dict[str, dict[str, Fact]] = {}
    for fact in db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.subject_type == "refund", Fact.is_current.is_(True))).all():
        facts_by.setdefault(fact.subject_id, {})[fact.kind] = fact
    views: list[RefundView] = []
    for r in refunds:
        f = facts_by.get(r.id, {})
        amount = _value(f, "amount", "amountMinor")
        views.append(RefundView(
            id=r.id, case_id=case.id, amount_minor=int(amount) if amount else None, currency=_value(f, "currency", "code"),
            reference=_value(f, "reference"), reference_type=_value(f, "reference_type"), account_scope=_value(f, "account_scope"),
            direction=_value(f, "direction") or "unknown", status=_value(f, "status") or "unknown",
            field_review={k: (f[k].review_state if k in f else "unreviewed") for k in CRITICAL_FIELDS},
            identity_review_state=r.identity_review_state, merged_into=r.merged_into_refund_id, excluded_reason=r.excluded_reason,
            reference_truncated=bool(_value(f, "reference", "truncated")), confirmed_distinct_from=set(r.confirmed_distinct_from or []),
            relates_to_incident=r.relates_to_incident, evidence_kind=r.evidence_kind, refund_kind=r.kind,
        ))
    return refunds, views, facts_by


def allocation_views(db: Session, case: Case) -> tuple[list[RefundAllocation], list[AllocationView]]:
    rows = db.scalars(select(RefundAllocation).where(RefundAllocation.case_id == case.id).order_by(RefundAllocation.created_at)).all()
    return rows, [AllocationView(a.id, a.refund_id, a.payment_id, a.amount_minor, a.state) for a in rows]


def citizen_outstanding_minor(db: Session, case: Case) -> int | None:
    fact = current_facts(db, case.id, "case", case.id).get("citizen_outstanding_statement")
    if fact is None or not fact.value or fact.value.get("outstandingMinor") in (None, ""):
        return None
    return int(fact.value["outstandingMinor"])


def case_recovery(db: Session, case: Case, payments: list[PaymentView] | None = None, totals: Totals | None = None) -> Recovery:
    from .casework import payment_views

    if payments is None:
        _, payments, _, _ = payment_views(db, case)
    if totals is None:
        totals = compute_totals(payments)
    _, rviews, _ = refund_views(db, case)
    _, aviews = allocation_views(db, case)
    return compute_recovery(totals, payments, rviews, aviews, citizen_outstanding_minor=citizen_outstanding_minor(db, case))


def next_refund_label(db: Session, case: Case) -> str:
    count = len(db.scalars(select(Refund.id).where(Refund.case_id == case.id)).all())
    return f"R{count + 1}"


def create_refund_claim(db: Session, case: Case, session: UserSession, settings: Settings, storage: Storage, *,
                        text: str, amount_raw: str | None, kind: str, date_text: str | None, revision: int) -> Refund:
    """The citizen reports money came back. A claim is visible immediately but never counted until a credit record is reviewed."""
    from .casework import add_text_source
    from .parsing import parse_date_text

    if kind not in REFUND_KINDS:
        raise ApiError("invalid_request", f"Refund kind must be one of {', '.join(REFUND_KINDS)}.", 400)
    source = add_text_source(db, storage, case, text, kind="user_answer", display_filename="Refund report", revision=revision, settings=settings)
    source.meta = {**(source.meta or {}), "refundClaim": True}
    source.status = "recorded"  # captured deterministically below; never queued for model extraction
    refund = Refund(case_id=case.id, label=next_refund_label(db, case), kind=kind, evidence_kind="claim",
                    relates_to_incident="confirmed", identity_review_state="unreviewed", created_revision=revision)
    db.add(refund)
    db.flush()
    anchors = [{"sourceId": source.id, "quote": text[:200], "quoteVerified": True, "notes": ["citizen_claim"]}]
    amount_value = None
    amount_issue = None
    currency = None
    if amount_raw:
        try:
            money = parse_amount(amount_raw)
            amount_value = {"amountMinor": str(money.amount_minor)}
            currency = money.currency
        except MoneyParseError as exc:
            amount_issue = "unreadable" if exc.code not in ("missing",) else "missing"
    else:
        amount_issue = "missing"
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="amount", value=amount_value,
             raw_text=amount_raw, anchors=anchors, revision=revision, issue=amount_issue)
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="currency",
             value={"code": currency} if currency else None, raw_text=currency, anchors=anchors, revision=revision,
             issue=None if currency else "missing")
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="direction", value={"value": "credit"},
             raw_text="reported as money received", anchors=anchors, revision=revision)
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="status", value=None,
             raw_text="reported by citizen; no record", anchors=anchors, revision=revision, issue="missing")
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="reference", value=None,
             raw_text=None, anchors=anchors, revision=revision, issue="missing")
    parsed = parse_date_text(date_text) if date_text else None
    new_fact(db, case_id=case.id, subject_type="refund", subject_id=refund.id, kind="occurred_at",
             value=parsed.to_json() if parsed and parsed.kind != "unresolved" else None, raw_text=date_text,
             anchors=anchors, revision=revision,
             issue=None if parsed and parsed.kind != "unresolved" else ("ambiguous" if parsed and parsed.issue in ("relative", "ambiguous_day_month") else "missing"))
    return refund


def upsert_allocation(db: Session, case: Case, session: UserSession, *, refund_id: str, payment_id: str,
                      amount_minor: int, reason: str | None, revision: int) -> RefundAllocation | None:
    """Create or revise one allocation. Constraints reject the request; nothing is clamped."""
    from .casework import payment_views

    refund = db.get(Refund, refund_id)
    payment = db.get(Payment, payment_id)
    if refund is None or refund.case_id != case.id or payment is None or payment.case_id != case.id:
        raise ApiError("not_found", "Refund or payment not found in this case.", 404)
    _, pviews, _, _ = payment_views(db, case)
    totals = compute_totals(pviews)
    recovery = case_recovery(db, case, pviews, totals)
    category = recovery.refund_categories.get(refund.id)
    if category != "eligible":
        raise ApiError("review_conflict", f"{refund.label} cannot be allocated yet: {recovery.refund_reasons.get(refund.id)}.", 409,
                       "Review the refund's fields, confirm it relates to the incident, and resolve duplicates first.")
    if payment.id not in totals.reviewed_debit_ids:
        raise ApiError("review_conflict", f"{payment.label} is not a reviewed successful INR debit ({totals.reasons.get(payment.id)}).", 409)

    existing = db.scalar(select(RefundAllocation).where(RefundAllocation.refund_id == refund.id, RefundAllocation.payment_id == payment.id))
    if amount_minor <= 0:
        if existing is None:
            raise ApiError("invalid_request", "Allocation amount must be a positive integer number of paise.", 400)
        db.delete(existing)
        db.flush()
        return None

    _, rviews, _ = refund_views(db, case)
    refund_amount = next(v.amount_minor for v in rviews if v.id == refund.id) or 0
    debit_amount = next(v.amount_minor for v in pviews if v.id == payment.id) or 0
    others = [a for a in db.scalars(select(RefundAllocation).where(RefundAllocation.case_id == case.id,
                                                                     RefundAllocation.state.in_(tuple(REVIEWED_STATES)))).all()
              if not (a.refund_id == refund.id and a.payment_id == payment.id)]
    refund_sum = sum(a.amount_minor for a in others if a.refund_id == refund.id) + amount_minor
    debit_sum = sum(a.amount_minor for a in others if a.payment_id == payment.id) + amount_minor
    if refund_sum > refund_amount:
        raise ApiError("invalid_request", f"Allocations for {refund.label} would total more than its reviewed amount.", 400,
                       "Reduce this allocation; the excess is not discarded silently.",
                       {"refundAmountMinor": str(refund_amount), "proposedTotalMinor": str(refund_sum)})
    if debit_sum > debit_amount:
        raise ApiError("invalid_request", f"Allocations to {payment.label} would exceed that payment's amount.", 400,
                       "Allocate the remainder to another reviewed debit or leave it unallocated.",
                       {"debitAmountMinor": str(debit_amount), "proposedTotalMinor": str(debit_sum)})

    state = "reviewer_checked" if session.role == "officer" else "user_confirmed"
    if existing is None:
        existing = RefundAllocation(case_id=case.id, refund_id=refund.id, payment_id=payment.id, amount_minor=amount_minor,
                                    state=state, reason=reason, created_by_role=session.role, created_revision=revision)
        db.add(existing)
    else:
        existing.amount_minor = amount_minor
        existing.state = state
        existing.reason = reason
        existing.created_by_role = session.role
        existing.created_revision = revision
        existing.invalidated_revision = None
    db.flush()
    return existing


def invalidate_allocations(db: Session, case: Case, revision: int, *, payment_id: str | None = None,
                           refund_id: str | None = None, reason: str) -> int:
    """A correction to a counted debit or refund withdraws approval of the allocations that depend on it."""
    query = select(RefundAllocation).where(RefundAllocation.case_id == case.id, RefundAllocation.state.in_(tuple(REVIEWED_STATES)))
    if payment_id:
        query = query.where(RefundAllocation.payment_id == payment_id)
    if refund_id:
        query = query.where(RefundAllocation.refund_id == refund_id)
    rows = db.scalars(query).all()
    for a in rows:
        a.state = "invalidated"
        a.invalidated_revision = revision
        a.reason = f"{reason} (was: {a.reason})" if a.reason else reason
    return len(rows)


def record_outstanding_statement(db: Session, case: Case, session: UserSession, settings: Settings, storage: Storage, *,
                                 text: str, outstanding_minor: int | None, revision: int) -> Fact:
    """Capture the citizen's own statement about money still outstanding, as a source plus a case fact."""
    from .casework import add_text_source
    from .records import supersede_fact

    source = add_text_source(db, storage, case, text, kind="user_answer", display_filename="Statement about outstanding money",
                             revision=revision, settings=settings)
    source.meta = {**(source.meta or {}), "outstandingStatement": True}
    source.status = "recorded"  # captured as a case fact below; never queued for model extraction
    anchors = [{"sourceId": source.id, "quote": text[:200], "quoteVerified": True, "notes": ["citizen_statement"]}]
    value = {"outstandingMinor": None if outstanding_minor is None else str(outstanding_minor), "text": text}
    state = "reviewer_checked" if session.role == "officer" else "user_confirmed"
    previous = current_facts(db, case.id, "case", case.id).get("citizen_outstanding_statement")
    if previous is not None:
        return supersede_fact(db, previous, revision=revision, review_state=state, actor_role=session.role, value=value,
                              raw_text=text, anchors=anchors, keep_value=False, reason="updated statement")
    fact = new_fact(db, case_id=case.id, subject_type="case", subject_id=case.id, kind="citizen_outstanding_statement",
                    value=value, raw_text=text, anchors=anchors, revision=revision, review_state=state)
    fact.reviewed_by = session.role
    fact.reviewed_at = utcnow()
    return fact


# ---- from services/questions.py ------------------------------------------------------------------
# Targeted clarification questions.
#
# The issue list is derived deterministically from reviewed state. Wording uses
# neutral templates; a model may rephrase later but never invents an issue. At most five open
# questions are surfaced at a time, highest priority first. Answers become `user_answer` sources.

MAX_OPEN_SHOWN = 5
PRIORITY = {
    "amount_conflict": 10,
    "status_conflict": 12,
    "duplicate_candidate": 20,
    "missing_field": 30,
    "unallocated_credit": 35,
    "pending_status": 40,
    "reported_vs_reviewed_gap": 50,
    "relative_date": 60,
}
FIELD_NAMES = {
    "amount": "amount", "currency": "currency", "reference": "transaction reference",
    "status": "payment status", "direction": "direction", "occurred_at": "date",
}


@dataclass(frozen=True)
class Issue:
    kind: str
    key: str
    text: str
    payment_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 50)


@dataclass
class IssueContext:
    """Everything `build_issues` needs, decoupled from the ORM so it is unit-testable."""

    views: list[PaymentView]
    labels: dict[str, str]
    reported_minor: int | None = None
    missing_fields: dict[str, list[str]] = field(default_factory=dict)  # payment id -> field kinds without value
    missing_fact_ids: dict[str, list[str]] = field(default_factory=dict)  # payment id -> fact ids
    relative_dates: list[tuple[str, str, str]] = field(default_factory=list)  # (observation id, raw, source id)
    unallocated_credit_ids: list[str] = field(default_factory=list)


def _money(minor: int | None) -> str:
    return format_inr(minor) if minor is not None else "an unreadable amount"


def build_issues(ctx: IssueContext) -> list[Issue]:
    issues: list[Issue] = []
    by_id = {v.id: v for v in ctx.views}
    totals: Totals = compute_totals(ctx.views)
    label = lambda pid: ctx.labels.get(pid, pid[:8])  # noqa: E731

    for cand in find_duplicate_candidates(ctx.views):
        a, b = (by_id[pid] for pid in cand.payment_ids)
        ref = a.reference or b.reference or "the same reference"
        pair = tuple(sorted(cand.payment_ids))
        if "amount_conflict" in cand.issues:
            issues.append(Issue("amount_conflict", f"amount_conflict:{':'.join(pair)}",
                                f"{label(a.id)} and {label(b.id)} share reference {ref} but show different amounts "
                                f"({_money(a.amount_minor)} and {_money(b.amount_minor)}). Which is correct, or are they different payments?",
                                pair))
        elif "status_conflict" in cand.issues:
            issues.append(Issue("status_conflict", f"status_conflict:{':'.join(pair)}",
                                f"{label(a.id)} and {label(b.id)} share reference {ref} but show different statuses "
                                f"({a.status} and {b.status}). Is this a later status update or an error?", pair))
        else:
            extra = " The payer account is not visible on one of them." if "scope_unknown" in cand.issues else ""
            issues.append(Issue("duplicate_candidate", f"duplicate_candidate:{':'.join(pair)}",
                                f"{label(a.id)} and {label(b.id)} may show the same payment (reference {ref}). Can you confirm?{extra}",
                                pair))

    for pid, kinds in ctx.missing_fields.items():
        if not kinds or pid not in by_id or by_id[pid].merged_into or by_id[pid].excluded_reason:
            continue
        names = [FIELD_NAMES.get(k, k) for k in kinds]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        verb = "is" if len(names) == 1 else "are"
        issues.append(Issue("missing_field", f"missing_field:{pid}",
                            f"On {label(pid)}, the {joined} {verb} not readable. Can you supply {'it' if len(names) == 1 else 'them'} or a clearer image?",
                            (pid,), tuple(ctx.missing_fact_ids.get(pid, ()))))

    for pid in totals.pending_ids:
        v = by_id[pid]
        issues.append(Issue("pending_status", f"pending_status:{pid}",
                            f"{label(pid)} ({_money(v.amount_minor)}) is marked pending. Has it completed, failed, or remained pending?",
                            (pid,)))

    for pid in ctx.unallocated_credit_ids:
        issues.append(Issue("unallocated_credit", f"unallocated_credit:{pid}",
                            f"{label(pid)} is an incoming credit. Does it relate to one of the payments in this case?", (pid,)))

    nothing_open = not (totals.unreviewed_ids or totals.unresolved_ids or totals.duplicate_candidate_ids or totals.conflict_ids)
    if ctx.reported_minor is not None and nothing_open and totals.reviewed_debit_ids and ctx.reported_minor != totals.reviewed_debit_minor:
        pending_note = " or a pending transaction" if totals.pending_ids else ""
        issues.append(Issue("reported_vs_reviewed_gap", "reported_vs_reviewed_gap",
                            f"You reported {format_inr(ctx.reported_minor)}, while the reviewed successful receipts total "
                            f"{format_inr(totals.reviewed_debit_minor)}. Is there another payment{pending_note}?",
                            tuple(totals.reviewed_debit_ids)))

    # the same words ("yesterday") in two sources are one question, naming both sources, not two that look identical
    merged: dict[str, tuple[str, str, list[str]]] = {}
    for obs_id, raw, source_id in ctx.relative_dates:
        key = " ".join(raw.lower().split())
        if key in merged:
            merged[key][2].append(source_id)
        else:
            merged[key] = (obs_id, raw, [source_id])
    for obs_id, raw, source_ids in merged.values():
        issues.append(Issue("relative_date", f"relative_date:{obs_id}",
                            f"What date does “{raw}” refer to in your account?", (), (), tuple(dict.fromkeys(source_ids))))

    return sorted(issues, key=lambda i: (i.priority, i.key))


def issue_context(db: Session, case: Case) -> IssueContext:
    from .casework import payment_views
    from .records import current_facts

    payments, views, facts_by, _ = payment_views(db, case)
    labels = {p.id: p.label for p in payments}
    missing_fields: dict[str, list[str]] = {}
    missing_fact_ids: dict[str, list[str]] = {}
    for p in payments:
        for kind in ("amount", "currency", "reference", "status", "direction", "occurred_at"):
            fact = facts_by.get(p.id, {}).get(kind)
            if fact is not None and fact.value is None and fact.review_state in ("unreviewed", "unresolved"):
                missing_fields.setdefault(p.id, []).append(kind)
                missing_fact_ids.setdefault(p.id, []).append(fact.fact_id)
    reported = current_facts(db, case.id, "case", case.id).get("citizen_reported_amount")
    reported_minor = int(reported.value["amountMinor"]) if reported and reported.value and reported.value.get("amountMinor") else None
    relative: list[tuple[str, str, str]] = []
    for obs in db.scalars(select(Observation).where(Observation.case_id == case.id, Observation.kind == "claim")).all():
        date = (obs.parsed or {}).get("date") or {}
        if date.get("kind") == "unresolved" and date.get("issue") == "relative" and date.get("rawText"):
            relative.append((obs.id, date["rawText"], obs.source_id))
    return IssueContext(views=views, labels=labels, reported_minor=reported_minor, missing_fields=missing_fields,
                        missing_fact_ids=missing_fact_ids, relative_dates=relative)


def sync_questions(db: Session, case: Case, revision: int) -> list[Question]:
    """Upsert open questions from the current issue list; resolve open ones whose issue disappeared."""
    issues = build_issues(issue_context(db, case))
    existing = {q.key: q for q in db.scalars(select(Question).where(Question.case_id == case.id)).all()}
    live_keys = set()
    for issue in issues:
        live_keys.add(issue.key)
        question = existing.get(issue.key)
        if question is None:
            db.add(Question(case_id=case.id, kind=issue.kind, key=issue.key, text=issue.text, priority=issue.priority,
                            related_payment_ids=list(issue.payment_ids), related_fact_ids=list(issue.fact_ids),
                            related_source_ids=list(issue.source_ids), state="open", created_revision=revision))
        else:
            question.text = issue.text  # keep wording current (labels/amounts may have been corrected)
            if question.state == "resolved":
                question.state = "open"  # the issue came back (e.g. a merge was undone)
                question.resolved_revision = None
            question.updated_at = utcnow()
    for key, question in existing.items():
        if key not in live_keys and question.state in ("open", "answered"):
            question.state = "resolved"
            question.resolved_revision = revision
            question.updated_at = utcnow()
    db.flush()
    return db.scalars(select(Question).where(Question.case_id == case.id).order_by(Question.priority, Question.created_at)).all()


# ---- from services/resolution.py -----------------------------------------------------------------
# Resolution summary: prepared from reviewed records; reviewed by the simulated reviewer.
#
# None of this changes official complaint status. Full recovery can lead to human review of the
# CyberBrief packet; it never closes a complaint.
#
# A citizen may also ask for their CyberBrief record to be closed, giving a written reason
# (`request_note`). An officer answering `closure_agreed` closes **that record** — CyberBrief stops
# tracking it — and nothing else. It does not close, withdraw or affect an official complaint, and
# the wording on every screen and in the summary text says so.

def eligibility(totals: Totals, recovery: Recovery, refunds: list[Refund], open_questions: list[Question],
                outstanding_statement_present: bool, allow_partial: bool) -> tuple[str, list[str]]:
    blocking: list[str] = []
    awaiting = len(totals.unreviewed_ids) + len(totals.unresolved_ids) + len(totals.duplicate_candidate_ids) + len(totals.conflict_ids)
    if not totals.reviewed_debit_ids:
        blocking.append("no reviewed debit baseline")
    if awaiting:
        blocking.append(f"{awaiting} payment(s) still awaiting review")
    unreviewed_refunds = [r for r in refunds if recovery.refund_categories.get(r.id) in
                          ("unreviewed", "unresolved", "duplicate_candidate", "conflict", "relation_unconfirmed")]
    if unreviewed_refunds:
        blocking.append(f"{len(unreviewed_refunds)} refund record(s) not fully reviewed")
    if recovery.allocation_issues or recovery.state == "discrepancy_needs_review":
        blocking.append("allocation discrepancies need review")
    if not outstanding_statement_present:
        blocking.append("the citizen's statement about money still outstanding has not been captured")
    conflicts = [q for q in open_questions if q.kind in ("amount_conflict", "status_conflict")]
    if conflicts:
        blocking.append(f"{len(conflicts)} unresolved conflict question(s)")
    kind = "full_recovery" if recovery.state == "reviewed_baseline_fully_offset" else "partial_or_unresolved"
    if kind == "partial_or_unresolved" and not allow_partial:
        blocking.append("reviewed baseline is not fully offset; request a partial/unresolved summary explicitly")
    return kind, blocking


def build_summary_text(case: Case, totals: Totals, recovery: Recovery, refund_labels: dict[str, str], payment_labels: dict[str, str],
                       outstanding_statement: str | None, latest_status_message: str, kind: str, blocking: list[str],
                       request_note: str | None = None) -> str:
    if request_note:
        title = "Closure request — the citizen asked for this CyberBrief record to be closed"
    elif kind == "full_recovery":
        title = "Resolution summary — full recovery of reviewed payments"
    else:
        title = "Resolution summary — partial or unresolved outcome"
    lines = [title, f"Case {case.id} · revision {case.current_revision}", "",
             "Based on reviewed supplied information only. This summary does not verify payments, determine guilt, or change any official complaint.", ""]
    if request_note:
        lines += [f"The citizen's reason for asking to close: {request_note}",
                  "Closing this record ends CyberBrief's tracking only. It does not close, withdraw or affect any official complaint.", ""]
    lines += [f"Reviewed distinct successful debits: {format_inr(totals.reviewed_debit_minor)} "
              f"({', '.join(payment_labels.get(p, p[:8]) for p in totals.reviewed_debit_ids) or 'none'})",
              f"Reviewed matched refunds: {format_inr(recovery.matched_refund_minor)}",
              f"Outstanding against reviewed payments: {format_inr(recovery.outstanding_minor)}",
              f"Recovery state: {recovery.state.replace('_', ' ')}"]
    if totals.pending_ids:
        lines.append(f"Pending payments still shown separately: {', '.join(payment_labels.get(p, p[:8]) for p in totals.pending_ids)} ({format_inr(totals.pending_minor)})")
    if totals.citizen_reported_ids if hasattr(totals, 'citizen_reported_ids') else False:  # pragma: no cover - defensive
        pass
    eligible = [rid for rid, cat in recovery.refund_categories.items() if cat == "eligible"]
    others = [(rid, cat) for rid, cat in recovery.refund_categories.items() if cat not in ("eligible", "merged_duplicate")]
    if eligible:
        lines.append(f"Reviewed refund records: {', '.join(refund_labels.get(r, r[:8]) for r in eligible)}")
    if others:
        lines.append("Refund records outside reviewed totals: " + "; ".join(f"{refund_labels.get(r, r[:8])} ({cat.replace('_', ' ')})" for r, cat in others))
    lines.append(f"Citizen's statement on outstanding money: {outstanding_statement or 'not captured'}")
    lines.append(f"Official complaint status (independent track): {latest_status_message}")
    if blocking:
        lines.append("")
        if request_note:
            # a closure request is never gated on these: someone may want to stop pursuing an
            # unresolved case, and saying "not eligible" there would be wrong.
            lines.append("Still unreviewed in the packet: " + "; ".join(blocking) + ".")
        else:
            lines.append("Not yet eligible for review because: " + "; ".join(blocking) + ".")
    return "\n".join(lines)


def prepare_resolution(db: Session, case: Case, session: UserSession, *, allow_partial: bool, revision: int,
                       request_note: str | None = None) -> ResolutionReview:
    from .casework import payment_views
    from .records import current_facts

    payments, pviews, _, _ = payment_views(db, case)
    totals = compute_totals(pviews)
    recovery = case_recovery(db, case, pviews, totals)
    refunds, _, _ = refund_views(db, case)
    open_questions = db.scalars(select(Question).where(Question.case_id == case.id, Question.state == "open")).all()
    statement = current_facts(db, case.id, "case", case.id).get("citizen_outstanding_statement")
    statement_text = statement.value.get("text") if statement and statement.value else None
    kind, blocking = eligibility(totals, recovery, refunds, open_questions, statement is not None, allow_partial)

    latest = select_latest(status_views(db, case))
    text = build_summary_text(case, totals, recovery, {r.id: r.label for r in refunds}, {p.id: p.label for p in payments},
                              statement_text, latest["message"] if latest["event"] is None else
                              f"{latest['event'].raw_status} (effective {latest['event'].effective_day or 'unknown'}; {latest['qualification']})",
                              kind, blocking, request_note)
    resolution = ResolutionReview(case_id=case.id, revision=revision, kind=kind, recovery_state=recovery.state, summary_text=text,
                                  eligible=not blocking, blocking_reasons=blocking, prepared_by_role=session.role,
                                  request_note=request_note)
    db.add(resolution)
    db.flush()
    return resolution


def review_resolution(db: Session, case: Case, session: UserSession, *, resolution_id: str, outcome: str, note: str | None) -> ResolutionReview:
    if session.role != "officer" or not session.account_id:
        raise ApiError("forbidden", "Only a signed-in officer can record a packet review.", 403,
                       "Sign in with your officer ID. The officer role is a simulated reviewer for this demo, not an institutional account.")
    resolution = db.scalar(select(ResolutionReview).where(ResolutionReview.id == resolution_id, ResolutionReview.case_id == case.id))
    if resolution is None:
        raise ApiError("not_found", "Resolution summary not found.", 404)
    from .models import CaseRevision

    later = db.scalars(select(CaseRevision).where(CaseRevision.case_id == case.id, CaseRevision.revision > resolution.revision)).all()
    if any(rv.kind != "packet_reviewed" for rv in later):
        raise ApiError("stale_revision", "The case changed after this summary was prepared.", 409, "Prepare the summary again from the current revision.")
    if resolution.reviewed_at is not None:
        raise ApiError("review_conflict", "This summary has already been reviewed.", 409)
    if outcome not in ("packet_reviewed", "more_information_requested", "closure_agreed"):
        raise ApiError("invalid_request", "Outcome must be packet_reviewed, more_information_requested or closure_agreed.", 400)
    if outcome == "packet_reviewed" and not resolution.eligible:
        raise ApiError("review_conflict", "This summary is not eligible to be recorded as reviewed.", 409,
                       "Resolve the blocking items or request more information instead.")
    if outcome == "closure_agreed" and not resolution.request_note:
        # agreeing to close is an answer to a request; there is nothing to answer without one.
        raise ApiError("review_conflict", "This summary is not a closure request.", 409,
                       "A record can only be closed in answer to a citizen who asked for it to be closed.")
    resolution.reviewed_at = utcnow()
    resolution.reviewed_by_role = session.role
    resolution.review_outcome = outcome
    resolution.review_note = note
    db.flush()
    return resolution


def official_status_untouched_by(db: Session, case: Case) -> int:
    """Helper for tests and audits: number of official status events (never changed by resolution flows)."""
    return len(db.scalars(select(OfficialStatusEvent.id).where(OfficialStatusEvent.case_id == case.id)).all())
