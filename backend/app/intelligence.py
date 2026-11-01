"""Case intelligence: what an officer may search about other reports, and the read-only tools built on it.

The rules this module keeps:
- Only the projection of a *submitted* report is ever searched. A private draft has no projection, so it cannot be found.
- What an officer may search is decided in SQL, before any identifier or text is compared: reports assigned to them, plus other
  officers' reports in the areas they cover only when they hold the explicit `area_discovery` capability. A match outside that
  scope is not returned, counted or hinted at. Discovery never opens a report: its original evidence still needs the exact-link guard.
- An exact identifier is the whole normalised identifier (a UPI ID, not its provider suffix), never a masked or partial value. Equality
  is a fact and stays a fact however many reports repeat it; how far it can be attributed is a separate note. An identifier that is a
  shared service (a marketplace or customer-care handle) is labelled so, and a recipient named by many independent reports carries a
  note that the repeat is a pattern to review, not proof of one person. A similar name or a similar story is a weaker lead, shown
  apart and never promoted into a link.
- Everything here is computed from stored records by code: the ledger arithmetic is in integer paise, an outcome is only what an officer
  recorded, and a bank reply or a closed packet is never turned into "solved". Nothing here writes to a case or sends anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .assistant import case_ref
from .casework import build_snapshot
from .core import ApiError
from .models import (
    AuditEvent,
    Case,
    CaseCandidate,
    CaseMessage,
    CaseProjection,
    CaseTask,
    ClaimAssessment,
    EntityObservation,
    Fact,
    HandlingLesson,
    OfficerProfile,
    OfficialStatusEvent,
    Payment,
    RelatedLink,
    ResolutionReview,
    Source,
    UserSession,
    utcnow,
)
from .workflow import area_name, staff_name

PROJECTION_VERSION = "p1"
POLICY_VERSION = "discovery-v1"
METHOD_TAXONOMY_VERSION = "method-taxonomy-v1"
VISIBLE = ("submitted", "returned", "reviewed", "closed")
MAX_RESULTS = 5
REPEAT_NOTE_AT = 4         # independent earlier reports naming one recipient before the result carries a note that a repeat is a pattern, not proof
KNOWN_SHARED_SERVICES = frozenset({"quickpay.hub@demobank", "payzone.agg@demobank"})   # fictional aggregator handles; real ones would come from a maintained list
CAPABILITY_AREA = "area_discovery"

_VPA = re.compile(r"(?<![A-Za-z0-9._\-])([A-Za-z0-9._\-]+@[A-Za-z][A-Za-z0-9\-]*)")
_MASKED_VPA = re.compile(r"(?:[xX*•]{3,}[A-Za-z0-9._\-]*|[A-Za-z0-9._\-]*[*•]{2,}[A-Za-z0-9._\-]*)@[A-Za-z][A-Za-z0-9\-]*")
# Words that only say what an account is for. An identifier made of nothing else (marketplace.pay@bank) names a service, not one seller.
GENERIC_TOKENS = frozenset({"pay", "payment", "payments", "merchant", "store", "shop", "support", "help", "collect", "upi", "refund", "care",
                            "customer", "customercare", "marketplace", "checkout", "billing", "wallet"})


# ---- normalisation: one rule per identifier type -------------------------------------------------------------------------

def normalize_upi(raw: str | None) -> list[dict[str, Any]]:
    """Every UPI ID in a text, whole and lower-cased. A masked one is kept as partial and never equals a full one; a generic one is labelled."""
    text = raw or ""
    found: list[dict[str, Any]] = []
    for m in _MASKED_VPA.finditer(text):
        found.append({"normalized": m.group(0).lower(), "display": m.group(0), "quality": "partial"})
    for m in _VPA.finditer(text):
        whole = m.group(1).rstrip(".-_")
        value = whole.lower()
        if any(f["normalized"] == value for f in found):
            continue
        local = value.split("@")[0]
        if re.search(r"x{3,}", local):
            found.append({"normalized": value, "display": whole, "quality": "partial"})
            continue
        tokens = [t for t in re.split(r"[._\-\d]+", local) if t]
        generic = bool(tokens) and all(t in GENERIC_TOKENS for t in tokens)
        found.append({"normalized": value, "display": whole, "quality": "generic" if generic else "full"})
    return found


def normalize_reference(raw: Any, truncated: bool = False) -> dict[str, Any] | None:
    """A transaction reference with spacing and case removed. A cut-off, masked or very short one is partial and never matches."""
    text = str(raw or "").strip()
    if not text:
        return None
    value = re.sub(r"\s+", "", text).upper()
    partial = truncated or "*" in value or "…" in value or value.endswith("...") or "XXXX" in value or len(value) < 8
    return {"normalized": value, "display": text, "quality": "partial" if partial else "full"}


def payee_name(raw: str | None) -> dict[str, Any] | None:
    """The shop or person name, without the UPI ID in brackets. A lead for a human to read, never an identity key."""
    text = _VPA.sub("", raw or "")
    text = re.sub(r"[()\[\]]", " ", text)
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        return None
    return {"normalized": " ".join(tokens), "display": " ".join(text.split()), "quality": "full"}


# ---- reported method: a small versioned taxonomy of what a report *says happened*, not a finding about anyone -------------

METHOD_DEFINITIONS: list[dict[str, Any]] = [
    {"id": "advance_payment_non_delivery", "label": "Advance payment, goods not delivered",
     "definition": "The report says the person paid for something and it did not arrive.",
     "patterns": [r"\b(?:never|not|didn'?t|did not|hasn'?t|has not|haven'?t)\s+(?:arrived|received|delivered|come|got)\b", r"\bnot (?:yet )?delivered\b",
                  r"\bno delivery\b", r"\bnever (?:got|received) (?:the|my|any)\b", r"\bpaid (?:in )?advance\b", r"\badvance (?:payment|amount)\b"]},
    {"id": "additional_payment_requested", "label": "Additional payment requested after purchase",
     "definition": "The report says a further payment, fee or charge was asked for after the first payment.",
     "patterns": [r"\b(?:extra|additional|more|another)\s+(?:fee|charge|payment|amount|money|deposit)\b", r"\basked (?:me )?to pay (?:more|again)\b"]},
    {"id": "refund_related_payment_request", "label": "Payment requested in connection with a refund",
     "definition": "The report says money was asked for before a refund would be given.",
     "patterns": [r"\brefund\b.{0,60}\b(?:fee|charge|pay|send|deposit)\b", r"\b(?:fee|charge|pay|send|deposit)\b.{0,60}\brefund\b"]},
    {"id": "impersonated_support", "label": "Contact presented as customer support",
     "definition": "The report says someone claiming to be customer care or support got in touch.",
     "patterns": [r"\b(?:customer care|customer support|support team|helpline|care executive)\b.{0,40}\b(?:called|rang|messaged|contacted|said|told)\b",
                  r"\b(?:called|rang|messaged|contacted)\b.{0,40}\b(?:customer care|customer support|support team|helpline)\b"]},
    {"id": "remote_access_request", "label": "Remote-access or screen-sharing request",
     "definition": "The report says the person was asked to install a remote-access app or share their screen.",
     "patterns": [r"\b(?:anydesk|teamviewer|quicksupport|screen[- ]?shar\w*|remote access)\b"]},
]

STAGES: list[tuple[str, str, str]] = [
    ("contact", "First contact", r"\b(?:contacted|messaged|called|reached out|saw (?:an? )?(?:ad|advert|listing|post)|found\b.{0,40}\b(?:online|page|website|instagram|facebook|listing))"),
    ("offer", "An offer or listing", r"\b(?:offered|listed|advertis\w*|on sale|discount|selling)\b"),
    ("payment_request", "A payment requested", r"\b(?:asked (?:me )?to (?:pay|send|transfer)|requested (?:a )?payment|advance)\b"),
    ("delivery_promise", "A delivery promised", r"\b(?:promised|will (?:be )?(?:deliver\w*|ship\w*|dispatch\w*)|delivery (?:in|within|by))\b"),
    ("further_demand", "A further demand", r"\b(?:extra|additional|more|another)\s+(?:fee|charge|payment|amount|money|deposit)\b"),
    ("disappearance", "No reply, or contact ended", r"\b(?:stopped (?:replying|responding|answering)|no (?:reply|response)|blocked me|switched off|not (?:answering|responding))\b"),
]
STAGE_ORDER = ["contact", "offer", "payment_request", "payment", "delivery_promise", "further_demand", "disappearance"]
STAGE_LABEL = {**{s: label for s, label, _ in STAGES}, "payment": "A payment made"}
TEXT_SOURCES = ("typed_account", "user_answer", "transcript")


def method_definitions() -> list[dict[str, Any]]:
    return [{"id": d["id"], "label": d["label"], "definition": d["definition"], "version": METHOD_TAXONOMY_VERSION} for d in METHOD_DEFINITIONS]


def _quote(text: str, start: int, end: int, width: int = 120) -> str:
    lo, hi = max(0, start - 30), min(len(text), end + 30)
    return " ".join(text[lo:hi].split())[:width]


def _method_and_stages(db: Session, case: Case, payment_sources: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reported-method tags and the stages the sources describe, each with the exact place in a source it was read from."""
    sources = db.scalars(select(Source).where(Source.case_id == case.id, Source.kind.in_(TEXT_SOURCES), Source.text_content.is_not(None)).order_by(Source.received_at)).all()
    tags: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    for d in METHOD_DEFINITIONS:
        for s in sources:
            hit = next((m for p in d["patterns"] if (m := re.search(p, s.text_content or "", re.IGNORECASE | re.DOTALL))), None)
            if hit:
                tags.append({"tag": d["id"], "label": d["label"], "sourceId": s.id, "span": [hit.start(), hit.end()], "quote": _quote(s.text_content or "", hit.start(), hit.end()),
                             "reviewState": "unreviewed", "definitionVersion": METHOD_TAXONOMY_VERSION})
                break
    for stage, label, pattern in STAGES:
        for s in sources:
            hit = re.search(pattern, s.text_content or "", re.IGNORECASE | re.DOTALL)
            if hit:
                stages.append({"stage": stage, "label": label, "sourceId": s.id, "span": [hit.start(), hit.end()], "quote": _quote(s.text_content or "", hit.start(), hit.end()), "state": "observed"})
                break
    if payment_sources:
        stages.append({"stage": "payment", "label": STAGE_LABEL["payment"], "sourceId": next(iter(payment_sources.values()), None), "span": None, "quote": None, "state": "observed"})
    stages.sort(key=lambda x: STAGE_ORDER.index(x["stage"]))
    return tags, stages


# ---- facts as the projection reads them ----------------------------------------------------------------------------------

def _origin(f: Fact) -> str:
    notes = {n for a in (f.anchors or []) for n in a.get("notes", [])}
    if any(str(n).startswith("corrected_by") for n in notes):
        return "corrected"
    return {"reviewer_checked": "officer_reviewed", "user_confirmed": "citizen_confirmed"}.get(f.review_state, "extracted")


def _anchor_source(f: Fact) -> str | None:
    return next((a.get("sourceId") for a in (f.anchors or []) if a.get("sourceId")), None)


def _fact_text(f: Fact | None) -> str | None:
    v = f.value if f else None
    if not v:
        return None
    if "amountMinor" in v:
        minor = int(v["amountMinor"])
        return f"Rs {minor // 100:,}.{minor % 100:02d}"
    if f.kind == "occurred_at":
        return (str(v.get("date") or v.get("localDatetime") or v.get("instantUtc") or f.raw_text or "")[:10]) or None
    if v.get("value") is not None:
        return str(v["value"])
    if "code" in v:
        return str(v["code"])
    return f.raw_text


def _current_payment_facts(db: Session, case_id: str) -> dict[str, dict[str, Fact]]:
    live = {p.id for p in db.scalars(select(Payment).where(Payment.case_id == case_id, Payment.merged_into_payment_id.is_(None))).all()}
    by: dict[str, dict[str, Fact]] = defaultdict(dict)
    for f in db.scalars(select(Fact).where(Fact.case_id == case_id, Fact.subject_type == "payment", Fact.is_current.is_(True))).all():
        if f.subject_id in live:
            by[f.subject_id][f.kind] = f
    return by


def source_digest(db: Session, case: Case) -> str:
    """A fingerprint of everything the projection reads: the case's state and routing, its current facts at their versions, and its text sources."""
    facts = sorted((f.fact_id, f.version) for f in db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.subject_type == "payment", Fact.is_current.is_(True))).all())
    sources = sorted((s.id, s.sha256) for s in db.scalars(select(Source).where(Source.case_id == case.id, Source.kind.in_(TEXT_SOURCES))).all())
    blob = json.dumps([case.current_revision, case.review_state, case.area, case.assigned_officer_id, facts, sources, PROJECTION_VERSION, POLICY_VERSION], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---- the projection --------------------------------------------------------------------------------------------------------

def _drop_projection(db: Session, case_id: str, revision: int) -> None:
    for o in db.scalars(select(EntityObservation).where(EntityObservation.case_id == case_id, EntityObservation.state == "active")).all():
        o.state, o.retracted_revision = "retracted", revision
    db.execute(delete(CaseProjection).where(CaseProjection.case_id == case_id))


def build_projection(db: Session, case: Case, *, force: bool = False) -> CaseProjection | None:
    """(Re)build one report's projection if what it was built from has changed. A report that is not submitted gets none."""
    existing = db.get(CaseProjection, case.id)
    if case.review_state not in VISIBLE:
        if existing is not None:
            _drop_projection(db, case.id, case.current_revision)
        return None
    digest = source_digest(db, case)
    if existing is not None and existing.source_revision_digest == digest and not force:
        return existing
    for o in db.scalars(select(EntityObservation).where(EntityObservation.case_id == case.id, EntityObservation.state == "active")).all():
        o.state, o.retracted_revision = "retracted", case.current_revision   # what the old source said stays on record, but is no longer searched
    by_payment = _current_payment_facts(db, case.id)
    payment_sources: dict[str, str] = {}
    for payment_id, facts in by_payment.items():
        for kind in ("counterparty", "reference"):
            f = facts.get(kind)
            if f is None or not f.value:
                continue
            raw = (f.value or {}).get("value")
            items: list[tuple[str, dict[str, Any]]] = []
            if kind == "counterparty":
                items = [("upi_vpa", v) for v in normalize_upi(str(raw or ""))]
                if (name := payee_name(str(raw or ""))):
                    items.append(("payee_name", name))
            elif (ref := normalize_reference(raw, bool((f.value or {}).get("truncated")))):
                items = [("transaction_reference", ref)]
            for entity_type, item in items:
                src = _anchor_source(f)
                if src:
                    payment_sources.setdefault(payment_id, src)
                db.add(EntityObservation(case_id=case.id, case_revision=case.current_revision, projection_version=PROJECTION_VERSION, entity_type=entity_type,
                                         normalized_value=item["normalized"][:200], display_value=item["display"][:200], raw_text=str(raw)[:400], quality=item["quality"],
                                         source_id=src, fact_id=f.fact_id, source_version=f.version, field_pointer=f"payment:{payment_id}/{kind}",
                                         assertion_origin=_origin(f), observed_at=f.created_at, supersedes_version=f.supersedes_version))
    tags, stages = _method_and_stages(db, case, payment_sources)
    refs = {"resolutionReviewIds": [r.id for r in db.scalars(select(ResolutionReview).where(ResolutionReview.case_id == case.id, ResolutionReview.review_outcome.is_not(None))).all()],
            "lessonIds": [x.id for x in db.scalars(select(HandlingLesson).where(HandlingLesson.case_id == case.id)).all()]}
    row = existing or CaseProjection(case_id=case.id, projection_version=PROJECTION_VERSION, visibility_policy_version=POLICY_VERSION, source_revision_digest=digest,
                                     case_revision=case.current_revision, review_state=case.review_state, indexed_at=utcnow(), method_tags=[], event_sequence=[], outcome_refs={})
    row.case_revision, row.projection_version, row.visibility_policy_version, row.source_revision_digest = case.current_revision, PROJECTION_VERSION, POLICY_VERSION, digest
    row.review_state, row.area, row.assigned_officer_id, row.submitted_at, row.indexed_at = case.review_state, case.area, case.assigned_officer_id, case.submitted_at, utcnow()
    row.method_tags, row.event_sequence, row.outcome_refs = tags, stages, refs
    if existing is None:
        db.add(row)
    db.flush()
    return row


def refresh_projections(db: Session, *, deep: bool = False, commit_each: bool = False, limit: int | None = None) -> int:
    """Bring visible reports' projections up to date and remove any that should no longer exist. Cheap unless something changed: the routing columns
    and revision are compared first. `deep` recomputes each fingerprint, which is what the backfill command does. With `limit`, only that many of the
    most recently changed are rebuilt (a request never does a long catch-up; the timer finishes it), and a clash with another writer ends the pass quietly."""
    rows = db.execute(select(Case.id, Case.current_revision, Case.review_state, Case.area, Case.assigned_officer_id).order_by(Case.updated_at.desc())).all()
    have = {p.case_id: p for p in db.scalars(select(CaseProjection)).all()}
    rebuilt = 0
    for case_id, revision, state, area, officer in rows:
        p = have.get(case_id)
        if state not in VISIBLE:
            if p is not None:
                _drop_projection(db, case_id, revision)
                rebuilt += 1
            continue
        if p is None or deep or (p.case_revision, p.review_state, p.area, p.assigned_officer_id) != (revision, state, area, officer) or p.projection_version != PROJECTION_VERSION:
            if limit is not None and rebuilt >= limit:
                break
            case = db.get(Case, case_id)
            before = p.source_revision_digest if p else None
            try:
                row = build_projection(db, case, force=p is None)
                if commit_each:
                    db.commit()   # one short write at a time, so a long catch-up never holds the database against everyone else
            except (IntegrityError, OperationalError):
                db.rollback()     # another writer built it first, or has the database: leave the rest to the next pass
                break
            rebuilt += 1 if row is not None and row.source_revision_digest != before else 0
    db.flush()
    return rebuilt


def ensure_projection(db: Session, case: Case) -> CaseProjection | None:
    return build_projection(db, case)


# ---- what an officer may search ------------------------------------------------------------------------------------------

class Scope:
    """Discovery permission, kept apart from permission to open a report or change it."""

    def __init__(self, officer_id: str, areas: set[str], area_discovery: bool) -> None:
        self.officer_id, self.areas, self.area_discovery = officer_id, areas, area_discovery

    def clause(self):
        mine = CaseProjection.assigned_officer_id == self.officer_id
        if self.area_discovery and self.areas:
            return or_(mine, CaseProjection.area.in_(self.areas))
        return mine

    def allows(self, p: CaseProjection | None) -> bool:
        if p is None or p.review_state not in VISIBLE:
            return False
        return p.assigned_officer_id == self.officer_id or bool(self.area_discovery and p.area in self.areas)

    @property
    def label(self) -> str:
        return ("reports assigned to you and to other officers in areas you cover" if self.area_discovery and self.areas else "reports assigned to you")


def discovery_scope(db: Session, officer_account_id: str) -> Scope:
    profile = db.get(OfficerProfile, officer_account_id)
    return Scope(officer_account_id, set(profile.areas or []) if profile else set(), CAPABILITY_AREA in (profile.capabilities or []) if profile else False)


def _scoped_ids(db: Session, scope: Scope, exclude: str) -> list[str]:
    return list(db.scalars(select(CaseProjection.case_id).where(CaseProjection.review_state.in_(VISIBLE), CaseProjection.case_id != exclude, scope.clause())).all())


def reference_groups(db: Session, case_ids: list[str]) -> list[set[str]]:
    """Reports among these that give the same full transaction reference: one payment reported more than once. Each group is one matter for counting."""
    if not case_ids:
        return []
    by_ref: dict[str, set[str]] = defaultdict(set)
    for o in db.scalars(select(EntityObservation).where(EntityObservation.entity_type == "transaction_reference", EntityObservation.state == "active", EntityObservation.quality == "full",
                                                         EntityObservation.case_id.in_(case_ids))).all():
        by_ref[o.normalized_value].add(o.case_id)
    groups: list[set[str]] = [{c} for c in case_ids]
    for members in by_ref.values():
        hit = [g for g in groups if g & members]
        merged = set().union(*hit) | members if hit else set(members)
        groups = [g for g in groups if not (g & members)] + [merged]
    return groups


# ---- search ----------------------------------------------------------------------------------------------------------------

BAND = {"exact_identifier": "Exact identifier match", "possible_duplicate": "Possible duplicate report", "generic_identifier": "Shared but generic identifier",
        "similar_method": "Similar reported method", "similar_name": "Similar account name", "none": "No longer connected"}
BAND_NOTE = {
    "exact_identifier": "Both reports name the same whole UPI ID. That is a lead to review: it does not show the reports have one person behind them.",
    "possible_duplicate": "Both reports give the same transaction reference. It may be one payment reported twice, so it is not counted as a separate loss.",
    "generic_identifier": "The identifier is a shared service handle, so the match is real but cannot point at one seller.",
    "similar_method": "The reports describe a similar sequence in the citizen's own words. No identifier is shared, so this is only a weak lead.",
    "similar_name": "The account names are alike but the identifiers differ. A similar name alone is not a link.",
    "none": "These reports no longer share an identifier or a reported method. A detail in one of them was changed after they were matched.",
}
RANK = {"exact_identifier": 0, "possible_duplicate": 1, "generic_identifier": 3, "similar_method": 4, "similar_name": 5, "none": 9}   # a heuristic order, never shown as a probability
ORIGIN_LABEL = {"extracted": "Read from evidence, not confirmed", "citizen_confirmed": "Confirmed by the citizen", "corrected": "Corrected", "officer_reviewed": "Checked by an officer"}


def _names_alike(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    return bool(ta and tb) and (a == b or len(ta & tb) / len(ta | tb) >= 0.5)


def _src_ref(db: Session, case: Case, obs: EntityObservation, *, link_id: str | None = None, mine: bool) -> dict[str, Any]:
    src = db.get(Source, obs.source_id) if obs.source_id else None
    url = None
    if src is not None:
        if mine:
            url = f"/api/officer/cases/{case.id}/sources/{src.id}"
        elif link_id:
            url = f"/api/officer/cases/{{case}}/related/{link_id}/sources/{src.id}"
    return {"kind": "fact", "factId": obs.fact_id, "factVersion": obs.source_version, "pointer": obs.field_pointer, "sourceId": src.id if src else None,
            "filename": src.display_filename if src else None, "origin": obs.assertion_origin, "excerpt": obs.raw_text, "openUrl": url}


def _ranges(db: Session, case_id: str) -> dict[str, Any]:
    dates = sorted(d for d in (_fact_text(f.get("occurred_at")) for f in _current_payment_facts(db, case_id).values()) if d)
    return {"from": dates[0] if dates else None, "to": dates[-1] if dates else None}


def search_related_cases(db: Session, viewer_id: str, case: Case, *, kinds: list[str] | None = None, limit: int = MAX_RESULTS, persist: bool = True) -> dict[str, Any]:
    """Ranked candidate earlier reports within this officer's discovery scope, each with the signals that put it there and why they fall short of proof.

    With `persist=False` it only reads: the index is used as it stands and nothing is created or remembered."""
    if persist:
        refresh_projections(db, limit=20)
        mine_p = ensure_projection(db, case)
    else:
        mine_p = db.get(CaseProjection, case.id)
    scope = discovery_scope(db, viewer_id)
    scoped = _scoped_ids(db, scope, case.id)
    mine = db.scalars(select(EntityObservation).where(EntityObservation.case_id == case.id, EntityObservation.state == "active")).all()
    by_type: dict[str, list[EntityObservation]] = defaultdict(list)
    for o in mine:
        by_type[o.entity_type].append(o)
    missing: list[str] = []
    if not any(o.quality in ("full", "generic") for o in by_type["upi_vpa"]):
        missing.append("recipient_upi" if not by_type["upi_vpa"] else "recipient_upi_complete")
    if not any(o.quality == "full" for o in by_type["transaction_reference"]):
        missing.append("transaction_reference")
    keys = {o.normalized_value: o for o in by_type["upi_vpa"] if o.quality in ("full", "generic")}
    refs = {o.normalized_value: o for o in by_type["transaction_reference"] if o.quality == "full"}
    names = {o.normalized_value: o for o in by_type["payee_name"]}
    my_tags = {t["tag"]: t for t in (mine_p.method_tags if mine_p else [])}

    hits: dict[str, dict[str, Any]] = {}

    def slot(other_id: str) -> dict[str, Any]:
        return hits.setdefault(other_id, {"signals": [], "kinds": set()})

    if scoped:
        if keys:
            theirs = db.scalars(select(EntityObservation).where(EntityObservation.entity_type == "upi_vpa", EntityObservation.state == "active", EntityObservation.quality.in_(("full", "generic")),
                                                                 EntityObservation.normalized_value.in_(list(keys)), EntityObservation.case_id.in_(scoped))).all()
            spread: dict[str, set[str]] = defaultdict(set)
            for o in theirs:
                spread[o.normalized_value].add(o.case_id)
            # one payment reported more than once is one matter: copies sharing a full transaction reference are counted once
            independent = {value: len(reference_groups(db, sorted(ids))) for value, ids in spread.items()}
            for o in theirs:
                n = independent[o.normalized_value]
                service = o.quality == "generic" or keys[o.normalized_value].quality == "generic" or o.normalized_value in KNOWN_SHARED_SERVICES
                h = slot(o.case_id)
                h["kinds"].add("generic_identifier" if service else "exact_identifier")
                h["signals"].append({"kind": "generic_identifier" if service else "exact_identifier", "label": "Same UPI ID (a shared service handle)" if service else "Same recipient UPI ID",
                                     "strength": "weak" if service else "strong", "entityType": "upi_vpa", "value": o.display_value, "mineObs": keys[o.normalized_value], "theirObs": o,
                                     "independentReports": n,
                                     "attribution": ("A shared service handle: many unrelated buyers pay it, so a match cannot point at one seller." if service else
                                                     f"Named by {n} independent earlier reports you may search. A repeated recipient is a pattern worth reviewing; it does not show that one person is behind them." if n >= REPEAT_NOTE_AT else None)})
        if refs:
            for o in db.scalars(select(EntityObservation).where(EntityObservation.entity_type == "transaction_reference", EntityObservation.state == "active", EntityObservation.quality == "full",
                                                                 EntityObservation.normalized_value.in_(list(refs)), EntityObservation.case_id.in_(scoped))).all():
                h = slot(o.case_id)
                h["kinds"].add("possible_duplicate")
                h["signals"].append({"kind": "same_reference", "label": "Same transaction reference", "strength": "strong", "entityType": "transaction_reference", "value": o.display_value,
                                     "mineObs": refs[o.normalized_value], "theirObs": o})
        if names:
            for o in db.scalars(select(EntityObservation).where(EntityObservation.entity_type == "payee_name", EntityObservation.state == "active", EntityObservation.case_id.in_(scoped))).all():
                match = next((n for n in names if _names_alike(n, o.normalized_value)), None)
                if match:
                    h = slot(o.case_id)
                    h["kinds"].add("similar_name")
                    h["signals"].append({"kind": "similar_name", "label": "Similar account name", "strength": "weak", "entityType": "payee_name", "value": o.display_value, "mineObs": names[match], "theirObs": o})
        if my_tags:
            for p in db.scalars(select(CaseProjection).where(CaseProjection.case_id.in_(scoped))).all():
                shared = [t for t in (p.method_tags or []) if t["tag"] in my_tags]
                for t in shared:
                    h = slot(p.case_id)
                    h["kinds"].add("similar_method")
                    h["signals"].append({"kind": "similar_method", "label": t["label"], "strength": "weak", "entityType": "method", "value": t["label"], "mineTag": my_tags[t["tag"]], "theirTag": t})

    results: list[dict[str, Any]] = []
    asof = utcnow().isoformat()
    # Rank first on what is already known, then build the detail for only the reports that will be shown: every undecided report up to the limit, and
    # every one the officer has already decided on. A busy department can match hundreds of reports; none of that work is spent on the ones not shown.
    projections = {x.case_id: x for x in db.scalars(select(CaseProjection).where(CaseProjection.case_id.in_(list(hits)))).all()} if hits else {}
    decided = {c.other_case_id for c in db.scalars(select(CaseCandidate).where(CaseCandidate.case_id == case.id, CaseCandidate.viewer_account_id == viewer_id, CaseCandidate.state != "possible")).all()}
    order = []
    for other_id, h in hits.items():
        op = projections.get(other_id)
        kind = min(h["kinds"], key=lambda k: RANK[k])
        if not scope.allows(op) or (kinds and kind not in kinds):
            continue   # re-checked against the current record, never trusted from the earlier query alone
        order.append((RANK[kind], -(op.submitted_at.timestamp() if op.submitted_at else 0), other_id))
    order.sort()
    undecided = [o for o in order if o[2] not in decided]
    selected = {o[2] for o in order if o[2] in decided} | {o[2] for o in undecided[:limit]}
    for other_id, h in hits.items():
        if other_id not in selected:
            continue
        other = db.get(Case, other_id)
        op = projections[other_id]
        kind = min(h["kinds"], key=lambda k: RANK[k])
        exact = [s for s in h["signals"] if s["kind"] in ("exact_identifier", "generic_identifier")]
        link_id = None
        if kind == "exact_identifier" and exact:
            value = exact[0]["theirObs"].display_value.lower()
            link = db.scalar(select(RelatedLink).where(RelatedLink.case_id == case.id, RelatedLink.other_case_id == other.id, RelatedLink.signal == "recipient_upi", RelatedLink.value == value))
            if link is None and persist:
                link = _insert_once(db, RelatedLink(case_id=case.id, other_case_id=other.id, signal="recipient_upi", value=value),
                                    select(RelatedLink).where(RelatedLink.case_id == case.id, RelatedLink.other_case_id == other.id, RelatedLink.signal == "recipient_upi", RelatedLink.value == value))
            link_id = link.id if link else None
        basis = {"mine": mine_p.source_revision_digest if mine_p else None, "theirs": op.source_revision_digest}
        cand = db.scalar(select(CaseCandidate).where(CaseCandidate.case_id == case.id, CaseCandidate.other_case_id == other.id, CaseCandidate.viewer_account_id == viewer_id))
        if cand is None:
            cand = CaseCandidate(case_id=case.id, other_case_id=other.id, viewer_account_id=viewer_id, result_kind=kind, state="possible", signals=[], basis=basis)
            if persist:
                cand = _insert_once(db, cand, select(CaseCandidate).where(CaseCandidate.case_id == case.id, CaseCandidate.other_case_id == other.id, CaseCandidate.viewer_account_id == viewer_id))
        if cand.state == "possible" and persist:
            cand.basis = basis
        cand.result_kind = kind
        signals = []
        for s in h["signals"]:
            sig = {"kind": s["kind"], "label": s["label"], "strength": s["strength"], "entityType": s["entityType"], "value": s["value"]}
            if "independentReports" in s:
                sig["independentReports"], sig["attribution"] = s["independentReports"], s["attribution"]
            if "mineObs" in s:
                sig["mine"], sig["theirs"] = _src_ref(db, case, s["mineObs"], mine=True), _src_ref(db, other, s["theirObs"], link_id=link_id, mine=False)
                if sig["theirs"]["openUrl"]:
                    sig["theirs"]["openUrl"] = sig["theirs"]["openUrl"].replace("{case}", case.id)
            else:
                sig["mine"] = {"kind": "source", "sourceId": s["mineTag"]["sourceId"], "excerpt": s["mineTag"]["quote"], "span": s["mineTag"]["span"], "origin": "citizen_account",
                               "openUrl": f"/api/officer/cases/{case.id}/sources/{s['mineTag']['sourceId']}"}
                sig["theirs"] = {"kind": "tag", "excerpt": s["theirTag"]["quote"], "origin": "citizen_account", "openUrl": None}   # a matched phrase only: the earlier report is not opened by a weak lead
            signals.append(sig)
        cand.signals = [{k: v for k, v in s.items() if k not in ("mine", "theirs")} for s in signals]
        stale_decision = cand.state != "possible" and cand.basis != basis
        diffs, contras = _differences(db, case, other, kind)
        results.append({
            "handle": cand.id, "resultKind": kind, "band": BAND[kind], "bandNote": BAND_NOTE[kind], "state": cand.state, "reason": cand.reason, "decisionStale": stale_decision,
            "possibleDuplicate": kind == "possible_duplicate", "caseRef": case_ref(other.id), "title": other.title, "area": area_name(other.area), "workflowState": other.workflow_state,
            "reviewState": other.review_state, "owner": {"name": staff_name(db, other.assigned_officer_id), "isYou": other.assigned_officer_id == viewer_id},
            "dates": {"submittedAt": other.submitted_at.isoformat() if other.submitted_at else None, "payments": _ranges(db, other.id)},
            "matchedSignals": signals, "differences": diffs, "contradictions": contras,
            "sourceQuality": {"mine": ORIGIN_LABEL.get(signals[0]["mine"].get("origin", ""), "Citizen's own words"), "theirs": ORIGIN_LABEL.get(signals[0]["theirs"].get("origin", ""), "Citizen's own words")},
            "revisions": {"mine": case.current_revision, "theirs": other.current_revision}, "asOf": asof,
            "missingData": [], "linkId": link_id, "_oid": other.id, "_rank": (RANK[kind], cand.state == "dismissed", -(other.submitted_at.timestamp() if other.submitted_at else 0)),
        })
    if persist and not kinds:   # a connection shown before that no longer holds is marked so, instead of silently keeping its old reasons
        shown = set(hits)
        for cand in db.scalars(select(CaseCandidate).where(CaseCandidate.case_id == case.id, CaseCandidate.viewer_account_id == viewer_id)).all():
            if cand.other_case_id not in shown and cand.result_kind != "none":
                cand.result_kind, cand.signals = "none", []
    results.sort(key=lambda r: (r["state"] == "dismissed", r["_rank"]))
    total = len(order)
    refs_of = {r["_oid"]: r["caseRef"] for r in results}
    for g in reference_groups(db, list(refs_of)):
        if len(g) > 1:
            for r in results:
                if r["_oid"] in g:
                    r["sameReportAs"] = sorted(refs_of[i] for i in g if i != r["_oid"])
    for r in results:
        r.pop("_rank")
        r.pop("_oid")
    return {"asOf": asof, "scope": {"label": scope.label, "policyVersion": POLICY_VERSION, "areaDiscovery": scope.area_discovery}, "scanned": len(scoped), "total": total,
            "truncated": total > len(results), "candidates": results, "missingData": missing, "basis": mine_p.source_revision_digest if mine_p else None,
            "caveats": ["Only reports you are permitted to search are included. A report outside that scope is neither shown nor counted.",
                        "A shared identifier is a lead to review; it does not show that the reports share a cause or a person."]}


def _insert_once(db: Session, row: Any, existing: Any) -> Any:
    """Add a row that has a unique key. Two searches from one page can reach for the same new row at once; the loser uses the winner's."""
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        return db.scalar(existing)


def lookup_identifier(db: Session, viewer_id: str, entity_type: str, raw: str) -> list[dict[str, Any]]:
    """Exact lookup of one identifier among the reports this officer may search. Returns source-backed records; a value that is missing, partial or masked finds nothing."""
    refresh_projections(db, limit=20)
    scope = discovery_scope(db, viewer_id)
    if entity_type == "upi_vpa":
        norm = [n for n in normalize_upi(raw) if n["quality"] in ("full", "generic")]
    elif entity_type == "transaction_reference":
        n = normalize_reference(raw)
        norm = [n] if n and n["quality"] == "full" else []
    else:
        raise ApiError("invalid_request", "That identifier type cannot be looked up.", 400)
    if not norm:
        return []
    scoped = list(db.scalars(select(CaseProjection.case_id).where(CaseProjection.review_state.in_(VISIBLE), scope.clause())).all())
    rows = db.scalars(select(EntityObservation).where(EntityObservation.entity_type == entity_type, EntityObservation.state == "active", EntityObservation.quality.in_(("full", "generic")),
                                                      EntityObservation.normalized_value.in_([n["normalized"] for n in norm]), EntityObservation.case_id.in_(scoped))).all() if scoped else []
    return [{"caseRef": case_ref(o.case_id), "caseId": o.case_id, "value": o.display_value, "origin": o.assertion_origin, "sourceId": o.source_id, "factId": o.fact_id, "pointer": o.field_pointer,
             "revision": o.case_revision} for o in rows]


# ---- comparison ------------------------------------------------------------------------------------------------------------

def _payment_cells(db: Session, case: Case) -> list[dict[str, Any]]:
    out = []
    labels = {p.id: p.label for p in db.scalars(select(Payment).where(Payment.case_id == case.id)).all()}
    for pid, f in _current_payment_facts(db, case.id).items():
        cp = (f.get("counterparty").value or {}).get("value") if f.get("counterparty") and f["counterparty"].value else None
        vpas = [v["normalized"] for v in normalize_upi(str(cp or "")) if v["quality"] in ("full", "generic")]
        out.append({"paymentId": pid, "label": labels.get(pid, "?"), "facts": f, "vpas": vpas, "payee": payee_name(str(cp or "")), "counterparty": cp})
    return sorted(out, key=lambda x: x["label"])


def _cell(db: Session, f: Fact | None, *, text: str | None = None) -> dict[str, Any]:
    src = _anchor_source(f) if f else None
    name = db.get(Source, src).display_filename if src and db.get(Source, src) else None
    return {"text": text if text is not None else _fact_text(f), "origin": _origin(f) if f else None, "originLabel": ORIGIN_LABEL.get(_origin(f)) if f else None,
            "sourceId": src, "filename": name, "factId": f.fact_id if f else None, "version": f.version if f else None}


def _differences(db: Session, case: Case, other: Case, kind: str) -> tuple[list[str], list[str]]:
    """Short, plain differences and contradictions between the paired payments. A contradiction is something to look at, never a finding about a person."""
    mine, theirs = _payment_cells(db, case), _payment_cells(db, other)
    diffs: list[str] = []
    contras: list[str] = []
    for a in mine:
        for b in theirs:
            shared = set(a["vpas"]) & set(b["vpas"])
            same_ref = _fact_text(a["facts"].get("reference")) and _fact_text(a["facts"].get("reference")) == _fact_text(b["facts"].get("reference"))
            if not shared and not same_ref:
                continue
            if shared and a["payee"] and b["payee"] and a["payee"]["normalized"] != b["payee"]["normalized"]:
                contras.append(f"The same UPI ID is named as \"{a['payee']['display']}\" here and \"{b['payee']['display']}\" there.")
            for kind_, label in (("amount", "amount"), ("occurred_at", "date")):
                x, y = _fact_text(a["facts"].get(kind_)), _fact_text(b["facts"].get(kind_))
                if x and y and x != y:
                    (contras if same_ref else diffs).append(f"The {label} differs: {x} here, {y} there." if not same_ref else f"The same reference carries a different {label}: {x} here, {y} there.")
    if kind in ("similar_method", "similar_name") and not diffs:
        diffs.append("No identifier is shared.")
    if kind == "similar_name":
        diffs.append("The recipient identifiers differ.")
    return list(dict.fromkeys(diffs))[:4], list(dict.fromkeys(contras))[:4]


def resolve_candidate(db: Session, viewer_id: str, case: Case, handle: str) -> tuple[CaseCandidate, Case, CaseProjection]:
    """An opaque handle becomes a report only for the officer it was shown to, for the case it was found from, and only while it is still in their scope."""
    cand = db.scalar(select(CaseCandidate).where(CaseCandidate.id == handle, CaseCandidate.case_id == case.id, CaseCandidate.viewer_account_id == viewer_id))
    other = db.get(Case, cand.other_case_id) if cand else None
    proj = db.get(CaseProjection, cand.other_case_id) if cand else None
    if cand is None or other is None or not discovery_scope(db, viewer_id).allows(proj):
        raise ApiError("not_found", "No such connection.", 404)
    return cand, other, proj


def _method_labels(p: CaseProjection | None) -> list[str]:
    return [t["label"] for t in (p.method_tags if p else [])]


def _stale_reason(db: Session, case: Case, other: Case, mp: CaseProjection | None, op: CaseProjection) -> str | None:
    """Read-only: has either report changed since it was indexed? Then what is shown may no longer match the record."""
    if mp is None or mp.source_revision_digest != source_digest(db, case) or op.source_revision_digest != source_digest(db, other):
        return "A report changed after it was last searched. Search again to refresh this."
    return None


def _no_longer(cand: CaseCandidate) -> str | None:
    return "These reports no longer share an identifier: a detail in one of them was corrected after they were matched." if cand.result_kind == "none" else None


def compare_cases(db: Session, viewer_id: str, case: Case, handle: str, *, basis: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """The current report beside an earlier one, row by row, each value labelled as read, confirmed, corrected or checked, with where to open it.

    Reading never rebuilds the index: if either report has changed since it was searched, the answer says so instead of quietly differing."""
    if refresh:
        refresh_projections(db, limit=20)
    cand, other, op = resolve_candidate(db, viewer_id, case, handle)
    mp = db.get(CaseProjection, case.id)
    link = db.scalar(select(RelatedLink).where(RelatedLink.case_id == case.id, RelatedLink.other_case_id == other.id, RelatedLink.signal == "recipient_upi"))
    mine, theirs = _payment_cells(db, case), _payment_cells(db, other)
    pairs: list[tuple[dict | None, dict | None]] = []
    used: set[str] = set()
    for a in mine:
        match = next((b for b in theirs if b["paymentId"] not in used and (set(a["vpas"]) & set(b["vpas"]) or (
            _fact_text(a["facts"].get("reference")) and _fact_text(a["facts"].get("reference")) == _fact_text(b["facts"].get("reference"))))), None)
        if match:
            used.add(match["paymentId"])
        pairs.append((a, match))
    pairs += [(None, b) for b in theirs if b["paymentId"] not in used]
    if not pairs:
        pairs = [(None, None)]

    def rel(x: str | None, y: str | None) -> str:
        return "not_stated" if not x and not y else "only_mine" if x and not y else "only_theirs" if y and not x else "same" if x == y else "different"

    def cell(side: dict | None, kind: str, *, mine_side: bool) -> dict[str, Any]:
        f = side["facts"].get(kind) if side else None
        c = _cell(db, f)
        if c["sourceId"]:
            c["openUrl"] = f"/api/officer/cases/{case.id}/sources/{c['sourceId']}" if mine_side else (f"/api/officer/cases/{case.id}/related/{link.id}/sources/{c['sourceId']}" if link else None)
        else:
            c["openUrl"] = None
        if not mine_side and not link:
            c["filename"], c["sourceId"] = None, None   # no exact-identifier link: the earlier report's files are not opened by a weak lead
        return c

    rows: list[dict[str, Any]] = []
    for a, b in pairs:
        group = f"{a['label'] if a else '—'} / {b['label'] if b else '—'}"
        for kind, label in (("counterparty", "Recipient"), ("reference", "Transaction reference"), ("amount", "Amount"), ("occurred_at", "Date"), ("status", "Payment status")):
            ca, cb = cell(a, kind, mine_side=True), cell(b, kind, mine_side=False)
            relation = rel(ca["text"], cb["text"])
            if kind == "counterparty" and a and b and set(a["vpas"]) & set(b["vpas"]):
                relation = "same_identifier"
            rows.append({"group": group, "label": label, "mine": ca, "theirs": cb, "relation": relation})
    diffs, contras = _differences(db, case, other, cand.result_kind)
    owner = staff_name(db, other.assigned_officer_id)
    rows += [
        {"group": "Report", "label": "Reported method", "mine": {"text": "; ".join(_method_labels(mp)) or None, "origin": "citizen_account", "originLabel": "Citizen's own words"},
         "theirs": {"text": "; ".join(_method_labels(op)) or None, "origin": "citizen_account", "originLabel": "Citizen's own words"},
         "relation": rel("; ".join(_method_labels(mp)) or None, "; ".join(_method_labels(op)) or None)},
        {"group": "Report", "label": "Handling status", "mine": {"text": f"{case.review_state}, {case.workflow_state}", "origin": "record"},
         "theirs": {"text": f"{other.review_state}, {other.workflow_state}", "origin": "record"}, "relation": "info"},
        {"group": "Report", "label": "Authorised owner", "mine": {"text": staff_name(db, case.assigned_officer_id), "origin": "record"}, "theirs": {"text": owner, "origin": "record"}, "relation": "info"},
    ]
    digest = hashlib.sha256(f"{mp.source_revision_digest if mp else ''}|{op.source_revision_digest}".encode()).hexdigest()[:32]
    shared = [{"label": s["label"], "value": s["value"], "strength": s["strength"]} for s in cand.signals if s.get("strength") == "strong" or s["kind"] != "similar_method"]
    stale_reason = _stale_reason(db, case, other, mp, op) or _no_longer(cand)
    return {"handle": cand.id, "resultKind": cand.result_kind, "band": BAND[cand.result_kind], "bandNote": BAND_NOTE[cand.result_kind], "state": cand.state, "reason": cand.reason,
            "current": {"caseRef": case_ref(case.id), "title": case.title, "revision": case.current_revision}, "earlier": {"caseRef": case_ref(other.id), "title": other.title, "revision": other.current_revision,
                                                                                                                       "area": area_name(other.area), "owner": owner},
            "rows": rows, "sharedEvidence": shared, "differences": diffs, "contradictions": contras, "basis": digest, "stale": bool(stale_reason or (basis and basis != digest)),
            "decisionStale": cand.state != "possible" and (cand.basis or {}) != {"mine": mp.source_revision_digest if mp else None, "theirs": op.source_revision_digest},
            "staleReason": stale_reason, "asOf": utcnow().isoformat(), "linkId": link.id if link else None,
            "note": "Each value is labelled by how it got into the record. A contradiction is a prompt to look, not a finding about anyone."}


def decide_candidate(db: Session, viewer_id: str, case: Case, handle: str, state: str, reason: str | None) -> CaseCandidate:
    """The officer's review of one connection, with a reason for a dismissal. It records a decision about evidence; it does not say the reports are one matter."""
    cand, other, op = resolve_candidate(db, viewer_id, case, handle)
    if state not in ("reviewed", "dismissed", "possible"):
        raise ApiError("invalid_request", "Unknown decision.", 400)
    if state == "dismissed" and not (reason or "").strip():
        raise ApiError("invalid_request", "Say why this is not related.", 400)
    mp = ensure_projection(db, case)
    cand.state, cand.reason, cand.decided_at = state, (reason or "").strip() or None, utcnow()
    cand.basis = {"mine": mp.source_revision_digest if mp else None, "theirs": op.source_revision_digest}
    link = db.scalar(select(RelatedLink).where(RelatedLink.case_id == case.id, RelatedLink.other_case_id == other.id, RelatedLink.signal == "recipient_upi"))
    if link is not None:
        link.state, link.reviewed_by = ("possible" if state == "possible" else state), viewer_id
    db.flush()
    return cand


# ---- how an earlier report was handled ---------------------------------------------------------------------------------------

_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_REF = re.compile(r"\b[A-Z0-9]{8,}\b")
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s?[\d,]+(?:\.\d+)?", re.IGNORECASE)


def scrub(text: str) -> str:
    """Wording kept from an earlier request with anything that names a person, payment or report taken out. Used to *show* a pattern; never pasted into a new draft."""
    out = _VPA.sub("[recipient]", text)
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[number]", out)
    out = re.sub(r"\bCB-[A-Z0-9]{4,}\b", "[report]", out)
    out = _AMOUNT.sub("[amount]", out)
    return _REF.sub("[reference]", out)


def _actor(db: Session, a: AuditEvent) -> dict[str, Any]:
    session = db.get(UserSession, a.actor_session_id)
    return {"role": a.actor_role, "name": staff_name(db, session.account_id) if session and session.account_id else None}


AUDIT_LABEL = {"assignment_acknowledged": "Acknowledged the assignment", "bank_check": "Checked the payments against the simulated bank record",
               "reply_reviewed": "Reviewed a reply", "related_reviewed": "Reviewed a connection to another report"}


def _lessons(db: Session, case_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(HandlingLesson).where(HandlingLesson.case_id == case_id).order_by(HandlingLesson.version)).all()
    superseded = {r.supersedes_id for r in rows if r.supersedes_id}
    return [{"id": r.id, "version": r.version, "outcomeKind": r.outcome_kind, "text": r.text, "reviewState": r.review_state, "author": staff_name(db, r.author_account_id),
             "at": r.created_at.isoformat(), "caseRevision": r.case_revision, "records": r.record_refs} for r in rows if r.id not in superseded]


def recorded_recovery(db: Session, other: Case) -> dict[str, Any]:
    """What the existing ledger records as returned, in integer paise, and who checked it. A reply or a closed packet adds nothing here."""
    s = build_snapshot(db, other)
    counted = [a for a in s.allocations if a.counted]
    states = {a.state for a in counted}
    return {"recoveredMinor": int(s.recovery.matched_refund_minor or 0), "reviewedDebitMinor": int(s.recovery.reviewed_debit_minor or 0),
            "outstandingMinor": int(s.recovery.outstanding_minor or 0), "state": s.recovery.state,
            "checkedBy": "officer" if "reviewer_checked" in states else "citizen" if states else None, "allocations": len(counted)}


def get_case_handling_history(db: Session, viewer_id: str, case: Case, handle: str) -> dict[str, Any]:
    """The earlier report's own recorded actions, outcome fields and loose ends, apart from each other. Nothing here says the matter was solved."""
    cand, other, op = resolve_candidate(db, viewer_id, case, handle)
    owner = staff_name(db, other.assigned_officer_id)
    msgs = db.scalars(select(CaseMessage).where(CaseMessage.case_id == other.id).order_by(CaseMessage.created_at)).all()
    timeline: list[dict[str, Any]] = []
    for m in msgs:
        if m.channel == "citizen" and m.direction == "outgoing":
            timeline.append({"at": m.created_at.isoformat(), "type": "citizen_question", "label": "Asked the citizen for something", "actor": {"role": "officer", "name": staff_name(db, m.author_account_id) or owner},
                             "wording": scrub(m.body)[:300], "recordType": "message", "recordId": m.id, "state": m.state})
        elif m.channel == "citizen":
            timeline.append({"at": m.created_at.isoformat(), "type": "citizen_reply", "label": "The citizen replied", "actor": {"role": "citizen", "name": None},
                             "detail": f"{len(m.source_ids or [])} file(s) attached" if m.source_ids else "A written answer", "recordType": "message", "recordId": m.id, "state": m.state})
        elif m.direction == "outgoing":
            timeline.append({"at": m.created_at.isoformat(), "type": "bank_request", "label": "Placed a verification request in the simulated outbox", "actor": {"role": "officer", "name": staff_name(db, m.author_account_id) or owner},
                             "detail": f"{len((m.data or {}).get('references', []))} transaction(s)", "recordType": "message", "recordId": m.id, "state": m.state, "simulated": True})
        else:
            results = (m.data or {}).get("results", [])
            timeline.append({"at": m.created_at.isoformat(), "type": "bank_reply", "label": "A simulated bank reply arrived", "actor": {"role": "simulated bank", "name": None},
                             "detail": f"{sum(1 for r in results if r.get('found'))} of {len(results)} reference(s) on the simulated record", "recordType": "message", "recordId": m.id,
                             "state": m.state, "reviewed": m.state == "reviewed", "simulated": True})
    for a in db.scalars(select(AuditEvent).where(AuditEvent.case_id == other.id, AuditEvent.kind.in_(tuple(AUDIT_LABEL)))).all():
        timeline.append({"at": a.created_at.isoformat(), "type": a.kind, "label": AUDIT_LABEL[a.kind], "actor": _actor(db, a), "recordType": "audit", "recordId": a.id})
    for a in db.scalars(select(ClaimAssessment).where(ClaimAssessment.case_id == other.id)).all():
        timeline.append({"at": a.created_at.isoformat(), "type": "assessment", "label": f"Assessed a claim: {a.assessment.replace('_', ' ')}", "actor": {"role": "officer", "name": staff_name(db, a.officer_account_id)},
                         "recordType": "assessment", "recordId": a.id})
    timeline.sort(key=lambda e: e["at"])
    tasks = db.scalars(select(CaseTask).where(CaseTask.case_id == other.id)).all()
    unresolved = [{"label": t.title, "kind": t.kind} for t in tasks if t.state == "open"]
    unresolved += [{"label": "A reply has arrived and has not been reviewed", "kind": "unreviewed_reply"} for m in msgs if m.direction == "incoming" and m.state == "received"]
    rec = recorded_recovery(db, other)
    if rec["outstandingMinor"] > 0:
        unresolved.append({"label": "Part of the reviewed amount is not recorded as returned", "kind": "outstanding"})
    lessons = _lessons(db, other.id)
    if not lessons:
        unresolved.append({"label": "No officer has recorded what this case taught", "kind": "no_lesson"})
    resolution = db.scalar(select(ResolutionReview).where(ResolutionReview.case_id == other.id).order_by(ResolutionReview.created_at.desc()))
    status = db.scalar(select(OfficialStatusEvent).where(OfficialStatusEvent.case_id == other.id).order_by(OfficialStatusEvent.received_at.desc()))
    evidence_after = [s for s in db.scalars(select(Source).where(Source.case_id == other.id)).all() if other.assigned_at and s.received_at and s.received_at > other.assigned_at and s.kind != "typed_account"]
    bank_in = [m for m in msgs if m.channel == "bank" and m.direction == "incoming"]
    cits_in = [m for m in msgs if m.channel == "citizen" and m.direction == "incoming"]
    kinds = {t["type"] for t in timeline}
    generated = (f"{len(timeline)} recorded action(s)" + (": " + ", ".join(sorted({t['label'].lower() for t in timeline if t['type'] in ('citizen_question', 'bank_request')})) if kinds & {"citizen_question", "bank_request"} else "") + ". "
                 "This sentence is generated from the records and is not an officer's conclusion.")
    return {
        "handle": cand.id, "caseRef": case_ref(other.id), "title": other.title, "area": area_name(other.area), "owner": {"name": owner, "isYou": other.assigned_officer_id == viewer_id},
        "scope": "A summary of the earlier report's recorded handling. Its original evidence opens only through an exact-identifier link.",
        "sharedSignals": [{"label": s["label"], "value": s["value"], "strength": s["strength"]} for s in cand.signals], "differences": _differences(db, case, other, cand.result_kind)[0],
        "timeline": timeline,
        "evidenceObtained": {"files": sum(1 for s in evidence_after if s.kind == "image"), "written": sum(1 for s in evidence_after if s.kind == "user_answer")},
        "citizenResponse": {"replies": len(cits_in), "reviewed": sum(1 for m in cits_in if m.state == "reviewed")},
        "simulatedBankResponse": {"replies": len(bank_in), "reviewed": sum(1 for m in bank_in if m.state == "reviewed"), "note": "A simulated reply is not a real bank verification and not a recovery."},
        "recovery": {**rec, "note": "Amounts come from the case ledger. A message arriving or a packet closing adds nothing to them."},
        "caseStatus": {"reviewState": other.review_state, "workflowState": other.workflow_state, "note": "CyberBrief's own tracking of the packet. It is not an investigation outcome."},
        "officialStatus": {"category": status.category, "receivedAt": status.received_at.isoformat(), "issuer": status.issuer_as_supplied, "note": "Supplied by the citizen, not verified."} if status else None,
        "resolution": {"kind": resolution.kind, "reviewOutcome": resolution.review_outcome, "reviewedAt": resolution.reviewed_at.isoformat() if resolution.reviewed_at else None} if resolution else None,
        "unresolved": unresolved,
        "outcome": {"label": (lessons[-1]["outcomeKind"] if lessons else "unknown"), "recordedBy": "an officer's lesson" if lessons else None,
                    "note": "The outcome is what an officer recorded. With no lesson recorded it is unknown, however many messages exist."},
        "lessons": lessons, "generatedSummary": {"text": generated, "origin": "generated from the records"}, "asOf": utcnow().isoformat(),
        "staleReason": _stale_reason(db, case, other, db.get(CaseProjection, case.id), op),
    }


# ---- a short playbook drawn from earlier handling ------------------------------------------------------------------------

def get_resolution_playbook(db: Session, viewer_id: str, case: Case, handles: list[str], *, fictional: bool = True) -> dict[str, Any]:
    """At most three steps earlier officers recorded on comparable reports, with plain counts and what limits them. No success rate, no causal claim, no copied wording."""
    cohort: list[tuple[CaseCandidate, Case]] = []
    for h in dict.fromkeys(handles):
        try:
            cand, other, _ = resolve_candidate(db, viewer_id, case, h)
        except ApiError:
            continue
        if cand.result_kind == "exact_identifier" and cand.state != "dismissed":
            cohort.append((cand, other))
    seen: set[str] = set()
    cohort = [(c, o) for c, o in cohort if not (o.id in seen or seen.add(o.id))]
    groups = reference_groups(db, [o.id for _, o in cohort])
    duplicates_collapsed = len(cohort) - len(groups)
    by_id = {o.id: (c, o) for c, o in cohort}
    steps_seen: dict[str, list[tuple[CaseCandidate, Case]]] = defaultdict(list)
    recovered = unknown = unresolved = reviewed_outcomes = 0
    for group in groups:   # a payment reported twice is one matter: it is counted once, with what either report recorded
        members = [by_id[i] for i in group]
        asked = bank = has_recovery = has_lesson = open_work = False
        for _cand, other in members:
            msgs = db.scalars(select(CaseMessage).where(CaseMessage.case_id == other.id)).all()
            asked = asked or any(m.channel == "citizen" and m.direction == "outgoing" for m in msgs)
            bank = bank or any(m.channel == "bank" and m.direction == "outgoing" for m in msgs)
            lessons = _lessons(db, other.id)
            has_recovery = has_recovery or recorded_recovery(db, other)["recoveredMinor"] > 0
            has_lesson = has_lesson or bool(lessons)
            open_work = open_work or bool(lessons and lessons[-1]["outcomeKind"] == "unresolved") or bool(db.scalar(select(CaseTask.id).where(CaseTask.case_id == other.id, CaseTask.state == "open")))
        lead = members[0]
        if asked:
            steps_seen["ask_citizen"].append(lead)
        if bank:
            steps_seen["bank_request"].append(lead)
        recovered += int(has_recovery)
        reviewed_outcomes += int(has_lesson)
        unknown += int(not (has_lesson or has_recovery))
        unresolved += int(open_work)
    from .verification import build_desk
    from .workflow import _problem_text

    snapshot = build_snapshot(db, case)
    desk = build_desk(db, case, snapshot)
    problem, about = _problem_text(snapshot, desk.payments, [])
    cannot = any(s.level == "cannot_verify" for s in desk.payments)
    steps: list[dict[str, Any]] = []
    n = len(groups)
    if steps_seen.get("ask_citizen"):
        k = len(steps_seen["ask_citizen"])
        steps.append({"id": "ask_citizen", "title": "Ask the citizen for the clearest copy of the receipt", "why": f"{k} of {n} comparable earlier report(s) recorded a question to the citizen.",
                      "difference": f"Your report's {about} payment: {'its reference cannot be read' if cannot else 'check its details'}.", "prerequisites": ["You read and approve the exact wording"],
                      "supportingCases": [c.id for c, _ in steps_seen["ask_citizen"]], "limitations": ["A question being asked does not show it led to any outcome.", f"Based on {k} earlier report(s) only."],
                      "draft": {"text": problem, "origin": "current case only", "note": "Built from this report's own record; no wording or detail from the earlier report is copied."}})
    if steps_seen.get("bank_request"):
        k = len(steps_seen["bank_request"])
        steps.append({"id": "bank_request", "title": "Prepare the simulated bank verification request", "why": f"{k} of {n} comparable earlier report(s) recorded a request in the simulated outbox.",
                      "difference": "Your report's reference cannot be read yet, so this cannot be prepared." if cannot else "Your report has a readable reference.",
                      "prerequisites": ["A readable transaction reference", "A bank in the contact directory", "You approve the request"], "blocked": cannot,
                      "supportingCases": [c.id for c, _ in steps_seen["bank_request"]], "limitations": ["The bank here is simulated: a reply is not a real verification.", f"Based on {k} earlier report(s) only."], "draft": None})
    steps = steps[:3]
    return {"asOf": utcnow().isoformat(), "fictionalData": fictional, "steps": steps,
            "counts": {"comparable": n, "withRecordedRecovery": recovered, "withOfficerLesson": reviewed_outcomes, "outcomeUnknown": unknown, "stillUnresolved": unresolved,
                       "scope": "exact recipient-identifier matches within the reports you may search", "duplicatesCollapsed": duplicates_collapsed, "percentages": "not shown: too few reports"},
            "limitations": ["These are plain counts of recorded facts, not a success rate and not a prediction for this report.",
                            "What happened after an action is not shown to be caused by it." + (" The data here is fictional." if fictional else "")],
            "insufficient": n == 0 or not steps}


# ---- what is missing in this report -----------------------------------------------------------------------------------------

def inspect_evidence_gaps(db: Session, case: Case) -> dict[str, Any]:
    """Missing, unreadable or conflicting details in this report and what each blocks. Items come from the record; none is a judgement of the citizen."""
    from .workflow import claims

    gaps: list[dict[str, Any]] = []
    for c in claims(db, case):
        for text in c["contradictions"]:
            gaps.append({"kind": "contradiction", "claim": c["claim"], "title": text, "blocks": "relying on this payment", "paymentId": c.get("paymentId")})
        if c["support"]["level"] in ("cannot_verify",) or "cannot" in c["support"]["label"].lower():
            gaps.append({"kind": "unreadable_or_missing", "claim": c["claim"], "title": c["support"]["label"], "blocks": "checking it against the simulated bank", "paymentId": c.get("paymentId")})
        if c["freshness"] == "needs_review":
            gaps.append({"kind": "new_evidence", "claim": c["claim"], "title": "New evidence arrived after your assessment", "blocks": "a current assessment", "paymentId": c.get("paymentId")})
        if c["freshness"] == "not_assessed":
            gaps.append({"kind": "not_assessed", "claim": c["claim"], "title": "Not yet assessed", "blocks": "a decision on the packet", "paymentId": c.get("paymentId")})
    for pid, f in _current_payment_facts(db, case.id).items():
        if "counterparty" not in f or not f["counterparty"].value:
            gaps.append({"kind": "missing_recipient", "claim": None, "title": "No recipient is stated for a payment", "blocks": "searching for earlier reports with the same recipient", "paymentId": pid})
        ref = f.get("reference")
        if ref is None or not ref.value or (ref.value or {}).get("truncated"):
            gaps.append({"kind": "missing_reference", "claim": None, "title": "A transaction reference is missing or cut off", "blocks": "a simulated bank request", "paymentId": pid})
    return {"asOf": utcnow().isoformat(), "gaps": list({(g["kind"], g["title"], g["paymentId"]): g for g in gaps}.values())}


def analyze_reported_method(db: Session, viewer_id: str, case: Case) -> dict[str, Any]:
    """What this report's own text says happened, stage by stage, and which stages it does not mention. A description of the account given, not a finding."""
    p = ensure_projection(db, case)
    observed = p.event_sequence if p else []
    have = {s["stage"] for s in observed}
    return {"definitionVersion": METHOD_TAXONOMY_VERSION, "tags": p.method_tags if p else [], "observedStages": observed,
            "notMentioned": [{"stage": s, "label": STAGE_LABEL[s]} for s in STAGE_ORDER if s not in have],
            "note": "Read from the citizen's own words. A stage that is not mentioned is unknown, not absent. These labels describe what was reported; they are not a finding."}


def connections_to_review(db: Session, viewer_id: str, case_ids: list[str]) -> set[str]:
    """Which of these cases have a strong connection the officer has not yet decided on: a whole recipient UPI ID or a full transaction reference shared with
    a report they may search. A few queries however many cases there are. It reads the index as it stands and stores nothing; the same rules as the search apply
    (a partial or generic identifier does not count; a recipient named by many reports still does)."""
    if not case_ids:
        return set()
    scope = discovery_scope(db, viewer_id)
    mine = db.scalars(select(EntityObservation).where(EntityObservation.case_id.in_(case_ids), EntityObservation.state == "active",
                                                      EntityObservation.entity_type.in_(("upi_vpa", "transaction_reference")), EntityObservation.quality == "full")).all()
    if not mine:
        return set()
    wanted = {o.normalized_value for o in mine}
    theirs = db.scalars(select(EntityObservation).join(CaseProjection, CaseProjection.case_id == EntityObservation.case_id)
                        .where(EntityObservation.state == "active", EntityObservation.quality == "full", EntityObservation.normalized_value.in_(wanted),
                               EntityObservation.entity_type.in_(("upi_vpa", "transaction_reference")), CaseProjection.review_state.in_(VISIBLE), scope.clause())).all()
    holders: dict[tuple[str, str], set[str]] = defaultdict(set)
    for o in theirs:
        holders[(o.entity_type, o.normalized_value)].add(o.case_id)
    pairs: dict[str, set[str]] = defaultdict(set)
    for o in mine:
        others = holders.get((o.entity_type, o.normalized_value), set()) - {o.case_id}
        pairs[o.case_id] |= others
    decided = {(c.case_id, c.other_case_id) for c in db.scalars(select(CaseCandidate).where(CaseCandidate.viewer_account_id == viewer_id, CaseCandidate.case_id.in_(case_ids),
                                                                                           CaseCandidate.state != "possible")).all()}
    return {cid for cid, others in pairs.items() if any((cid, other) not in decided for other in others)}
