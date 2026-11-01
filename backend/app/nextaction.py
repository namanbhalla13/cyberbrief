"""Suggested next steps for the officer: the model proposes, the officer decides.

The model is shown what the case record and the simulated bank check say and may suggest up to four next steps from
a short list of kinds. Code decides which kinds are allowed right now, drops any suggestion whose words break the
assistant's reply rules (promises, outside referrals, figures that are not in the record), and falls back to fixed
rules when the model cannot be reached or nothing it said survives. A suggestion does nothing by itself: accepting or
dismissing it is recorded in the audit trail, and the action it points at is still the officer's own button.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError
from sqlalchemy.orm import Session

from .ai import ModelError
from .assistant import MetaAssistantModel, check_reply, grounding_from
from .core import ApiError, Settings
from .models import Case, UserSession
from .schemas import ActionDecision, ApiModel, CaseSnapshot, NextActionsOut, PaymentSupportOut, ProposalKind, ProposalOut
from .verification import audit_events, record_audit

log = logging.getLogger("cyberbrief.nextaction")
PROMPT_VERSION = "nextaction_v1"
_ASSETS = Path(__file__).parent / "assets"
MAX_PROPOSALS = 4


@lru_cache
def _asset(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8").strip()


# ---- what the record allows right now --------------------------------------------------------------------

def action_facts(case: Case, snapshot: CaseSnapshot, supports: list[PaymentSupportOut]) -> dict[str, Any]:
    latest = snapshot.resolutions[0] if snapshot.resolutions else None   # newest first
    with_officer = case.review_state == "submitted"
    closure_asked = bool(latest and latest.request_note and not latest.review_outcome)
    usable = bool(with_officer and latest and not latest.stale and not latest.review_outcome)
    return {
        "supports": supports,
        "unchecked": [s.payment_id for s in supports if s.bank is None or s.bank.stale],
        "differs": [s.payment_id for s in supports if s.level == "bank_differs"],
        "not_found": [s.payment_id for s in supports if s.level == "bank_not_found"],
        "cannot": [s.payment_id for s in supports if s.level == "cannot_verify"],
        "with_officer": with_officer,
        "can_ask": with_officer,   # asking the citizen for one detail never needs a resolution summary to exist first
        "can_review": usable and bool(latest and latest.eligible) and not closure_asked,
        "can_close": usable and closure_asked,
        "closure_asked": closure_asked,
        "open_questions": snapshot.case.open_question_count,
        "recovery": snapshot.recovery.state.replace("_", " "),
        "title": snapshot.case.title,
        "review_state": case.review_state,
        "summary_eligible": bool(latest and latest.eligible),
        "summary_stale": bool(latest and latest.stale),
    }


# The one table of what each action needs before it is allowed. The suggestions the officer sees and the buttons the officer
# presses both read it, so a step can never be offered that its own button would refuse, or the other way round.
ACTIONS: dict[str, Any] = {
    "wait": lambda f: True,
    "run_bank_check": lambda f: bool(f["supports"] and f["unchecked"]),
    "request_more_information": lambda f: bool(f["can_ask"]),
    "record_packet_reviewed": lambda f: bool(f["can_review"]),
    "agree_closure": lambda f: bool(f["can_close"]),
}


def allowed_kinds(facts: dict[str, Any]) -> set[str]:
    return {kind for kind, allowed in ACTIONS.items() if allowed(facts)}


def require_action(kind: str, case: Case) -> None:
    """The manual route for an action: the same precondition the suggestions use, applied to the case alone."""
    if kind == "request_more_information" and not (case.review_state == "submitted" and case.assigned_officer_id):
        raise ApiError("review_conflict", "This report is not with an officer, so nothing can be asked of the citizen.", 409)


def _names(facts: dict[str, Any], ids: list[str]) -> str:
    by_id = {s.payment_id: s.name for s in facts["supports"]}
    return "; ".join(by_id[i] for i in ids if i in by_id)


def rule_proposals(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed rules over the same facts. Used in fixture mode and whenever the model's suggestions cannot be used."""
    out: list[dict[str, Any]] = []

    def add(kind: str, title: str, reason: str, ids: list[str] | None = None, note: str | None = None) -> None:
        out.append({"key": kind, "kind": kind, "title": title, "reason": reason, "paymentIds": ids or [], "noteDraft": note, "origin": "rules"})

    if "run_bank_check" in allowed_kinds(facts):
        add("run_bank_check", "Check the payments against the simulated bank record",
            f"{len(facts['unchecked'])} payment(s) have not been checked, or the case changed after the last check.", facts["unchecked"])
    problem = [*facts["differs"], *facts["not_found"]]
    if problem and facts["can_ask"]:
        names = _names(facts, problem)
        add("request_more_information", "Ask the citizen to recheck a payment",
            f"The simulated bank record does not agree with, or has no entry for: {names}.", problem,
            f"The simulated bank record does not agree with these payments: {names}. Please check your receipt and the details you confirmed.")
    if facts["can_close"]:
        add("agree_closure", "Answer the closure request", "The citizen asked for this record to be closed and no answer has been recorded.")
    elif facts["can_review"] and not problem and not facts["cannot"] and not facts["unchecked"]:
        add("record_packet_reviewed", "Record the packet as reviewed",
            "The summary is current and eligible, and no payment differs from, or is missing from, the simulated bank record.")
    if not out:
        add("wait", "Nothing further is needed from you right now",
            "Nothing in the record calls for an officer action at this point." if facts["review_state"] == "submitted"
            else "The case is not waiting on the officer.")
    return out[:MAX_PROPOSALS]


def facts_text(facts: dict[str, Any]) -> str:
    lines = [f"case: {facts['title']}", f"state: {facts['review_state']}", f"questions still open: {facts['open_questions']}",
             f"money back, as reviewed: {facts['recovery']}",
             f"summary for review: {'current' if not facts['summary_stale'] else 'out of date'}, "
             f"{'eligible' if facts['summary_eligible'] else 'not eligible'}; closure asked by the citizen: {'yes' if facts['closure_asked'] else 'no'}",
             "payments:"]
    for s in facts["supports"]:
        bank = s.bank.result.replace("_", " ") if s.bank else "not checked"
        lines.append(f"- [{s.payment_id}] {s.name}; support: {s.label}; bank check: {bank}; " + " ".join(s.reasons))
    return "\n".join(lines)


# ---- the model --------------------------------------------------------------------------------------------

class ModelProposal(ApiModel):
    kind: ProposalKind
    title: str = Field(max_length=160)
    reason: str = Field(max_length=500)
    payment_ids: list[str] = Field(default_factory=list, max_length=10)
    note_draft: str | None = Field(default=None, max_length=500)


class ModelBatch(ApiModel):
    proposals: list[ModelProposal] = Field(max_length=8)


class MetaNextActionModel(MetaAssistantModel):
    def propose(self, context: str) -> tuple[ModelBatch, dict, int]:
        messages = [{"role": "system", "content": _asset("nextaction_v1.txt")}, {"role": "user", "content": context}]
        content, usage, calls = self._structured(messages, "cyberbrief_next_actions_v1", json.loads(_asset("nextaction_v1.json")))
        try:
            return ModelBatch.model_validate(json.loads(content)), usage, calls
        except (ValueError, ValidationError) as exc:
            raise ModelError("schema_invalid", "The suggested steps did not match the schema.") from exc


def _guard(batch: ModelBatch, facts: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Keep a suggestion only if its kind is allowed now and its words follow the reply rules."""
    allowed = allowed_kinds(facts)
    known = {s.payment_id for s in facts["supports"]}
    grounding = grounding_from([text])
    kept: list[dict[str, Any]] = []
    for p in batch.proposals:
        if p.kind not in allowed or any(k["kind"] == p.kind for k in kept):
            continue
        if any(check_reply(words, grounding) for words in (p.title, p.reason, p.note_draft or "") if words):
            continue
        kept.append({"key": p.kind, "kind": p.kind, "title": p.title, "reason": p.reason, "paymentIds": [i for i in p.payment_ids if i in known],
                     "noteDraft": p.note_draft if p.kind == "request_more_information" else None, "origin": "model"})
    return kept[:MAX_PROPOSALS]


def propose_next_actions(db: Session, settings: Settings, case: Case, session: UserSession, snapshot: CaseSnapshot,
                         supports: list[PaymentSupportOut]) -> None:
    facts = action_facts(case, snapshot, supports)
    text = facts_text(facts)
    proposals, notice, usage, model_id = rule_proposals(facts), None, {}, "rules"
    if settings.ai_mode == "live":
        context = "ALLOWED KINDS: " + ", ".join(sorted(allowed_kinds(facts))) + "\n\nCASE FACTS\n" + text
        db.commit()   # no database transaction is held open while the model works
        try:
            batch, usage, _calls = MetaNextActionModel(settings).propose(context)
            kept = _guard(batch, facts, text)
            if kept:
                proposals, model_id = kept, settings.meta_text_model
            else:
                notice = "Nothing the model suggested could be used, so these suggestions come from fixed rules."
        except ModelError as exc:
            log.warning("next-step suggestions failed: %s", exc.code)
            notice = "The model could not be reached, so these suggestions come from fixed rules."
    else:
        notice = "Replayed demo mode: these suggestions come from fixed rules, not a live model."
    record_audit(db, case, session, "next_actions_proposed", subject_type="case", subject_id=case.id, detail={
        "aiMode": settings.ai_mode, "modelId": model_id, "promptVersion": PROMPT_VERSION, "notice": notice, "usage": usage,
        "proposals": proposals})


def actions_out(db: Session, case: Case) -> NextActionsOut | None:
    events = audit_events(db, case)
    proposed = [e for e in events if e.kind == "next_actions_proposed"]
    if not proposed:
        return None
    latest = proposed[-1]
    decided: dict[str, str] = {}
    for e in events:
        if e.kind == "action_decision" and e.detail.get("proposalSetId") == latest.id:
            decided[e.detail["key"]] = e.detail["decision"]
    d = latest.detail
    return NextActionsOut(
        id=latest.id, ai_mode=d["aiMode"], model_id=d["modelId"], generated_at=latest.created_at, revision=latest.revision,
        stale=latest.revision != case.current_revision, notice=d.get("notice"),
        proposals=[ProposalOut(key=p["key"], kind=p["kind"], title=p["title"], reason=p["reason"], payment_ids=p["paymentIds"],
                               note_draft=p.get("noteDraft"), origin=p["origin"], decision=decided.get(p["key"])) for p in d["proposals"]])


def decide(db: Session, case: Case, session: UserSession, payload: ActionDecision) -> None:
    current = actions_out(db, case)
    if current is None or current.id != payload.proposal_set_id:
        raise ApiError("review_conflict", "These suggestions have been replaced.", 409, "Suggest next steps again.")
    proposal = next((p for p in current.proposals if p.key == payload.key), None)
    if proposal is None:
        raise ApiError("not_found", "That suggestion is not in the list.", 404)
    record_audit(db, case, session, "action_decision", subject_type="proposal", subject_id=None, detail={
        "proposalSetId": current.id, "key": proposal.key, "kind": proposal.kind, "title": proposal.title,
        "decision": payload.decision, "note": payload.note})

