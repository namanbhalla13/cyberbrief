"""The admin portal: a work queue with a coordinator attached.

The admin sees reports a citizen has sent for review, never private drafts. The coordinator does the work first (reads the
report, checks the roster, drafts an assignment) and the admin approves, chooses another officer, or leaves it in the
exception queue. Counts and lists come from the application's records; the model only chooses which fixed query a
question means, and the answer is the query's result with the time it was read.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .ai import ModelError
from .assistant import case_ref
from .core import ApiError, Settings
from .events import emit, event_health, retry_failed, run_events
from .models import Account, AgentActivity, AgentProposal, Case, CaseEvent, CaseTask, OfficerProfile, utcnow
from .schemas import ApiModel
from .security import Config, CurrentAdmin, Db, require_csrf
from .verification import record_audit
from .workflow import (
    _claim_capacity_and_case,
    _JsonModel,
    approve_assignment,
    area_name,
    cases_naming,
    config,
    log_activity,
    open_task,
    proposal_out,
    propose_assignment,
    release_assignment,
    roster,
    staff_name,
)

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
_SUBMITTED = ("submitted", "returned", "reviewed", "closed")


def _case(db: Session, case_id: str) -> Case:
    case = db.scalar(select(Case).where(Case.id == case_id, Case.review_state.in_(_SUBMITTED)))
    if case is None:
        raise ApiError("not_found", "Report not found.", 404)
    return case


def _row(db: Session, c: Case) -> dict[str, Any]:
    open_tasks = db.scalars(select(CaseTask.title).where(CaseTask.case_id == c.id, CaseTask.state == "open")).all()
    return {"id": c.id, "caseRef": case_ref(c.id), "title": c.title, "area": area_name(c.area), "areaId": c.area, "language": c.language,
            "reviewState": c.review_state, "workflowState": c.workflow_state or "awaiting_assignment" if c.review_state == "submitted" else c.workflow_state,
            "assignedTo": staff_name(db, c.assigned_officer_id), "submittedAt": c.submitted_at.isoformat() if c.submitted_at else None,
            "openTasks": list(open_tasks)}


@admin_router.get("/overview")
def overview(session: CurrentAdmin, db: Db, settings: Config) -> dict[str, Any]:
    run_events(settings)   # late events (a deadline passed, a restart) are caught up when the queue is opened
    db.expire_all()
    cases = db.scalars(select(Case).where(Case.review_state.in_(_SUBMITTED)).order_by(Case.submitted_at.desc()).limit(150)).all()
    rows = [_row(db, c) for c in cases]
    pending = db.scalars(select(AgentProposal).where(AgentProposal.kind == "assignment", AgentProposal.state == "pending").order_by(AgentProposal.created_at)).all()
    activity = db.scalars(select(AgentActivity).order_by(AgentActivity.created_at.desc()).limit(20)).all()
    officers = roster(db)
    for o in officers:
        o["unacknowledged"] = db.scalar(select(func.count(Case.id)).where(Case.assigned_officer_id == o["id"], Case.acknowledged_at.is_(None))) or 0
    overdue = db.scalars(select(CaseTask).where(CaseTask.state == "open", CaseTask.due_at.is_not(None), CaseTask.due_at < utcnow())).all()
    return {
        "asOf": utcnow().isoformat(),
        "intake": [r for r in rows if r["workflowState"] in ("awaiting_assignment", "exception", "submitted", None) and r["reviewState"] == "submitted" and not r["assignedTo"]],
        "cases": rows, "officers": officers, "events": event_health(db), "summary": _summary(db),
        "proposals": [proposal_out(db, p) | {"title": db.get(Case, p.case_id).title} for p in pending],
        "followUps": [{"caseId": t.case_id, "caseRef": case_ref(t.case_id), "task": t.title, "dueAt": t.due_at.isoformat() if t.due_at else None} for t in overdue],
        "activity": [{"caseId": a.case_id, "caseRef": case_ref(a.case_id) if a.case_id else None, "agent": a.agent, "steps": a.steps, "outcome": a.outcome, "at": a.created_at.isoformat()} for a in activity],
    }


def _summary(db: Session) -> dict[str, int]:
    """Counts over every submitted report, from the records, so the strip is true however long the list is."""
    out = {}
    for name, where in _STATE_QUERIES.items():
        out[name] = db.scalar(select(func.count(Case.id)).where(*where())) or 0
    out["all"] = db.scalar(select(func.count(Case.id)).where(Case.review_state.in_(_SUBMITTED))) or 0
    return out


def _blocker(db: Session, c: Case, pending: AgentProposal | None, tasks: list[CaseTask]) -> tuple[str, str, str]:
    """Who has this report, what is stopping it, and what happens next. Worked out from the records, never written by a model."""
    if c.review_state == "closed":
        return "Nobody", "Nothing: the record is closed.", "No action."
    if not c.area and not c.assigned_officer_id:
        return "The citizen", "The citizen has not chosen an area.", "Waiting for the citizen to choose an area; routing restarts by itself."
    if c.workflow_state == "exception" and not c.assigned_officer_id:
        return "The admin", "No officer was found for this area, or the proposal was set aside.", "Choose an officer, or put someone on duty."
    if not c.assigned_officer_id:
        return ("The admin", "An assignment is prepared and waiting for approval.", "Approve the assignment, or choose another officer.") if pending else                ("The coordinator", "No assignment is prepared yet.", "Ask the coordinator to propose an officer.")
    by_kind = {t.kind: t for t in tasks}
    officer = staff_name(db, c.assigned_officer_id)
    if c.acknowledged_at is None:
        return officer or "The officer", "The officer has not acknowledged the assignment.", "Wait for the acknowledgement, remind them, or move the report."
    if "citizen_info" in by_kind:
        return "The citizen", "A request from the reviewer is waiting for the citizen's answer.", "Waiting for the citizen."
    if "bank_response" in by_kind:
        return "The simulated bank", "A verification request is waiting for the simulated bank's reply.", "Waiting for the reply."
    if "review_reply" in by_kind:
        return officer or "The officer", "A reply has arrived and has not been reviewed.", "The officer reviews the reply."
    return officer or "The officer", "Nothing is blocking it.", "The officer works the case."


@admin_router.get("/cases/{case_id}")
def case_detail(case_id: str, session: CurrentAdmin, db: Db) -> dict[str, Any]:
    """One submitted report as the admin needs it: the owner, the blocker and the next action first, then the assignment, acknowledgement and follow-ups."""
    c = _case(db, case_id)
    pending = db.scalar(select(AgentProposal).where(AgentProposal.case_id == c.id, AgentProposal.kind == "assignment", AgentProposal.state == "pending").order_by(AgentProposal.created_at.desc()))
    tasks = list(db.scalars(select(CaseTask).where(CaseTask.case_id == c.id, CaseTask.state == "open")).all())
    owner, blocker, nxt = _blocker(db, c, pending, tasks)
    activity = db.scalars(select(AgentActivity).where(AgentActivity.case_id == c.id).order_by(AgentActivity.created_at.desc()).limit(10)).all()
    events = db.scalars(select(CaseEvent).where(CaseEvent.case_id == c.id).order_by(CaseEvent.created_at.desc()).limit(10)).all()
    return {**_row(db, c), "owner": owner, "blocker": blocker, "nextAction": nxt,
            "assignedAt": c.assigned_at.isoformat() if c.assigned_at else None, "acknowledgedAt": c.acknowledged_at.isoformat() if c.acknowledged_at else None,
            "proposal": proposal_out(db, pending) if pending else None,
            "followUps": [{"task": t.title, "dueAt": t.due_at.isoformat() if t.due_at else None} for t in tasks if t.kind == "follow_up"],
            "activity": [{"agent": a.agent, "steps": a.steps, "outcome": a.outcome, "at": a.created_at.isoformat()} for a in activity],
            "events": [{"kind": e.kind, "state": e.state, "error": e.error, "at": e.created_at.isoformat()} for e in events]}


_STATE_QUERIES = {
    "waiting": lambda: (Case.assigned_officer_id.is_(None), Case.review_state == "submitted"),
    "exception": lambda: (Case.workflow_state == "exception", Case.review_state == "submitted"),
    "assigned": lambda: (Case.assigned_officer_id.is_not(None), Case.acknowledged_at.is_(None), Case.review_state == "submitted"),
    "under_review": lambda: (Case.acknowledged_at.is_not(None), Case.review_state == "submitted"),
    "closed": lambda: (Case.review_state == "closed",),
}
@admin_router.get("/cases")
def list_cases(session: CurrentAdmin, db: Db, query: str = Query(default="", max_length=80), state: str = Query(default="all", max_length=16),
               page: int = Query(default=1, ge=1, le=10000), page_size: int = Query(default=20, ge=1, le=100, alias="pageSize")) -> dict[str, Any]:
    """Every submitted report, searchable and paged on the server, in a stable order. Private drafts are never in this list."""
    if state != "all" and state not in _STATE_QUERIES:
        raise ApiError("invalid_request", "Unknown state filter.", 400)
    q = select(Case).where(Case.review_state.in_(_SUBMITTED))
    if state != "all":
        q = q.where(*_STATE_QUERIES[state]())
    term = query.strip()
    if term:
        like = f"%{term.lower()}%"
        ref_like = f"%{term.lower().removeprefix('cb-').replace('-', '')}%"
        officer_ids = select(Account.id).where(func.lower(Account.display_name).like(like))
        q = q.where(or_(func.lower(Case.title).like(like), func.lower(func.coalesce(Case.area, "")).like(like),
                        func.lower(func.replace(Case.id, "-", "")).like(ref_like) if term.lower().startswith("cb-") else func.lower(Case.id).like(like),
                        Case.assigned_officer_id.in_(officer_ids)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    cases = db.scalars(q.order_by(Case.submitted_at.desc(), Case.id).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_row(db, c) for c in cases], "total": total, "page": page, "pageSize": page_size, "pages": max(1, -(-total // page_size)), "asOf": utcnow().isoformat()}


@admin_router.post("/events/retry", dependencies=[Depends(require_csrf)])
def retry_events(session: CurrentAdmin, db: Db, settings: Config) -> dict[str, Any]:
    """Put failed background work back in the queue and run it now. Nothing is done twice: every handler is idempotent."""
    n = retry_failed(db)
    run_events(settings)
    db.expire_all()
    return {"retried": n, "events": event_health(db)}


@admin_router.post("/cases/{case_id}/propose-assignment", dependencies=[Depends(require_csrf)])
def propose(case_id: str, session: CurrentAdmin, db: Db, settings: Config) -> dict[str, Any]:
    case = _case(db, case_id)
    out = propose_assignment(db, settings, case, session)
    db.commit()
    return out


class ApproveBody(ApiModel):
    digest: str
    officer_id: str | None = None


@admin_router.post("/proposals/{proposal_id}/approve", dependencies=[Depends(require_csrf)])
def approve(proposal_id: str, session: CurrentAdmin, db: Db, payload: ApproveBody) -> dict[str, Any]:
    proposal = db.scalar(select(AgentProposal).where(AgentProposal.id == proposal_id, AgentProposal.kind == "assignment"))
    if proposal is None:
        raise ApiError("not_found", "Proposal not found.", 404)
    case = db.get(Case, proposal.case_id)
    if proposal.state == "executed":
        return proposal_out(db, proposal)   # a retried approval finds the assignment it already made
    approve_assignment(db, case, session, proposal, payload.digest, payload.officer_id)
    db.commit()
    return proposal_out(db, proposal)


@admin_router.post("/proposals/{proposal_id}/reject", dependencies=[Depends(require_csrf)])
def reject(proposal_id: str, session: CurrentAdmin, db: Db) -> dict[str, Any]:
    proposal = db.scalar(select(AgentProposal).where(AgentProposal.id == proposal_id, AgentProposal.kind == "assignment"))
    if proposal is None:
        raise ApiError("not_found", "Proposal not found.", 404)
    if proposal.state == "pending":
        proposal.state, proposal.decided_by, proposal.decided_at = "rejected", session.id, utcnow()
        case = db.get(Case, proposal.case_id)
        if not case.assigned_officer_id:
            case.workflow_state = "exception"
        log_activity(db, case.id, "coordinator", ["The admin set the proposal aside"], "Left for the exception queue")
        db.commit()
    return proposal_out(db, proposal)


class OfficerBody(ApiModel):
    on_duty: bool | None = None
    capacity: int | None = Field(default=None, ge=0, le=50)
    areas: list[str] | None = Field(default=None, min_length=1, max_length=20)
    area_discovery: bool | None = None


@admin_router.post("/officers/{account_id}", dependencies=[Depends(require_csrf)])
def update_officer(account_id: str, session: CurrentAdmin, db: Db, settings: Config, payload: OfficerBody) -> list[dict[str, Any]]:
    profile = db.get(OfficerProfile, account_id)
    if profile is None:
        raise ApiError("not_found", "No such officer.", 404)
    if payload.on_duty is not None:
        profile.on_duty = payload.on_duty
    if payload.capacity is not None:
        profile.capacity = payload.capacity
    if payload.area_discovery is not None:   # may search, as a minimal projection, other officers' reports in their own areas
        caps = set(profile.capabilities or [])
        caps = caps | {"area_discovery"} if payload.area_discovery else caps - {"area_discovery"}
        profile.capabilities = sorted(caps)
    if payload.areas is not None:
        known = {a["id"] for a in config()["areas"]}
        if not set(payload.areas) <= known:
            raise ApiError("invalid_request", "One of those areas is not one this department covers.", 400)
        profile.areas = sorted(set(payload.areas))
    if payload.on_duty is not None or payload.areas is not None:   # who is eligible just changed: waiting reports are routed again
        stamp = utcnow().isoformat()
        for waiting in db.scalars(select(Case).where(Case.review_state == "submitted", Case.assigned_officer_id.is_(None), Case.area.is_not(None))).all():
            emit(db, waiting.id, "routing_changed", f"duty:{waiting.id}:{account_id}:{stamp}", {"officer": account_id})
    db.commit()
    run_events(settings)
    return roster(db)


class ReassignBody(ApiModel):
    officer_id: str
    reason: str | None = Field(default=None, max_length=300)
    from_officer_id: str | None = None   # who the admin saw as the owner; if the case has moved on since, nothing is changed


@admin_router.post("/cases/{case_id}/reassign", dependencies=[Depends(require_csrf)])
def reassign(case_id: str, session: CurrentAdmin, db: Db, payload: ReassignBody) -> dict[str, Any]:
    case = _case(db, case_id)
    if not case.assigned_officer_id:
        raise ApiError("review_conflict", "This report has no officer to replace. Use the assignment proposal.", 409)
    if payload.from_officer_id is not None and payload.from_officer_id != case.assigned_officer_id:
        raise ApiError("review_conflict", "This report has been moved since you opened it. Open it again to see who has it now.", 409)
    if payload.officer_id == case.assigned_officer_id:
        raise ApiError("review_conflict", "That officer already has this report.", 409)
    if payload.officer_id not in {o["id"] for o in roster(db, case) if o["eligible"]}:
        raise ApiError("review_conflict", "That officer does not qualify for this report right now.", 409)
    previous = case.assigned_officer_id
    release_assignment(db, case)
    _claim_capacity_and_case(db, case, payload.officer_id)
    db.refresh(case)
    open_task(db, case.id, "acknowledge", "Officer to acknowledge the assignment")
    record_audit(db, case, session, "reassignment", subject_type="officer", subject_id=payload.officer_id, detail={"from": previous, "reason": (payload.reason or "").strip() or None})
    log_activity(db, case.id, "coordinator", [f"The admin moved the report to {staff_name(db, payload.officer_id)}"], "Reassigned")
    db.commit()
    return _row(db, case)


# ---- questions ----------------------------------------------------------------------------------------------------------------

def _table(title: str, columns: list[str], rows: list[list[str]], text: str, case_ids: list[str] | None = None) -> dict[str, Any]:
    """`caseIds` runs alongside `rows`: the typed id of the report each row is about, so the screen can open it."""
    return {"title": title, "columns": columns, "rows": rows, "text": text, "asOf": utcnow().isoformat(), "caseIds": case_ids or []}


def _tool_unassigned(db: Session, _arg: str) -> dict[str, Any]:
    cases = db.scalars(select(Case).where(Case.review_state == "submitted", Case.assigned_officer_id.is_(None))).all()
    return _table("Submitted reports with no officer", ["Report", "Title", "Area", "State"],
                  [[case_ref(c.id), c.title, area_name(c.area) or "not given", c.workflow_state or "awaiting_assignment"] for c in cases],
                  f"{len(cases)} submitted report(s) have no officer.", [c.id for c in cases])


def _tool_unavailable(db: Session, _arg: str) -> dict[str, Any]:
    out = []
    for o in roster(db):
        waiting = db.scalars(select(Case).where(Case.assigned_officer_id == o["id"], Case.acknowledged_at.is_(None))).all()
        if not o["onDuty"] and (waiting or o["active"]):
            out.append([o["name"], "off duty", str(o["active"]), ", ".join(case_ref(c.id) for c in waiting) or "none"])
    return _table("Officers off duty with work assigned", ["Officer", "Duty", "Active cases", "Unacknowledged"], out, f"{len(out)} officer(s) are off duty with work assigned.")


def _tool_tasks(kind: str, title: str):
    def run(db: Session, _arg: str) -> dict[str, Any]:
        tasks = db.scalars(select(CaseTask).where(CaseTask.kind == kind, CaseTask.state == "open")).all()
        return _table(title, ["Report", "Waiting for", "Due"], [[case_ref(t.case_id), t.title, t.due_at.date().isoformat() if t.due_at else "no date"] for t in tasks],
                      f"{len(tasks)} open.", [t.case_id for t in tasks])
    return run


def _tool_pending_approval(db: Session, _arg: str) -> dict[str, Any]:
    pending = db.scalars(select(AgentProposal).where(AgentProposal.kind == "assignment", AgentProposal.state == "pending").order_by(AgentProposal.created_at)).all()
    rows = [[case_ref(p.case_id), db.get(Case, p.case_id).title, staff_name(db, (p.payload or {}).get("recommendedOfficerId")) or "no one eligible", p.created_at.strftime("%d %b %H:%M")] for p in pending]
    return _table("Assignments waiting for your approval", ["Report", "Title", "Suggested officer", "Prepared"], rows, f"{len(pending)} assignment(s) are waiting for the admin.", [p.case_id for p in pending])


def _tool_overdue(db: Session, _arg: str) -> dict[str, Any]:
    now = utcnow()
    late = db.scalars(select(CaseTask).where(CaseTask.state == "open", CaseTask.due_at.is_not(None), CaseTask.due_at < now).order_by(CaseTask.due_at)).all()
    rows = [[case_ref(t.case_id), t.title, t.due_at.date().isoformat()] for t in late]
    return _table("Overdue work", ["Report", "Waiting for", "Was due"], rows, f"{len(late)} open item(s) are past their due date.", [t.case_id for t in late])


def _tool_shared(db: Session, arg: str) -> dict[str, Any]:
    ident = arg.strip().lower()
    hits = [[case_ref(c.id), c.title, area_name(c.area) or "not given"] for c in (db.get(Case, i) for i in cases_naming(db, {ident}))]
    return _table(f"Reports that name {ident} as a recipient", ["Report", "Title", "Area"], hits, f"{len(hits)} report(s) share this exact UPI ID. A shared ID is a lead, not proof.", [i for i in cases_naming(db, {ident})])


def _tool_blockers(db: Session, arg: str) -> dict[str, Any]:
    ref = arg.strip().upper()
    case = next((c for c in db.scalars(select(Case).where(Case.review_state != "private")).all() if case_ref(c.id) == ref), None)
    if case is None:
        return _table("What is holding this up", [], [], f"No submitted report has the reference {ref}.")
    tasks = db.scalars(select(CaseTask).where(CaseTask.case_id == case.id, CaseTask.state == "open")).all()
    rows = [[t.title, t.due_at.date().isoformat() if t.due_at else "no date"] for t in tasks]
    if not case.assigned_officer_id:
        rows.insert(0, ["No officer is assigned yet", "-"])
    return _table(f"What is holding up {ref}", ["Waiting for", "Due"], rows, f"{len(rows)} open item(s). Workflow state: {case.workflow_state or 'awaiting_assignment'}.", [case.id] * len(rows))


def _tool_changes(db: Session, _arg: str) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=24)
    acts = db.scalars(select(AgentActivity).where(AgentActivity.created_at >= since).order_by(AgentActivity.created_at.desc())).all()
    return _table("Activity in the last 24 hours", ["When", "Report", "Who", "What"], [[a.created_at.strftime("%d %b %H:%M"), case_ref(a.case_id) if a.case_id else "-", a.agent, a.outcome] for a in acts],
                  f"{len(acts)} recorded action(s).", [a.case_id or "" for a in acts])


TOOLS = {
    "unassigned": _tool_unassigned, "officers_unavailable": _tool_unavailable, "pending_approval": _tool_pending_approval, "overdue": _tool_overdue,
    "waiting_bank": _tool_tasks("bank_response", "Reports waiting for a bank response"),
    "waiting_citizen": _tool_tasks("citizen_info", "Reports waiting for the citizen"),
    "shared_recipient": _tool_shared, "case_blockers": _tool_blockers, "changes": _tool_changes,
}
_TOOL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["tool", "argument"],
                "properties": {"tool": {"type": "string", "enum": list(TOOLS)}, "argument": {"type": ["string", "null"]}}}


def choose_tool(settings: Settings, question: str, telemetry: dict[str, Any] | None = None) -> tuple[str | None, str]:
    """Which fixed query a question means. Keyword rules always run; the model is asked only to read a question the rules cannot."""
    q = question.lower()
    vpa = re.search(r"[\w.\-]+@[\w\-]+", question)
    ref = re.search(r"CB-[0-9A-Fa-f]{6}", question)
    if vpa:
        return "shared_recipient", vpa.group(0)
    if ref:
        return "case_blockers", ref.group(0)
    for words, tool in ((("overdue", "past due", "late", "not acknowledged", "unacknowledged"), "overdue"), (("approval", "approve", "waiting for me"), "pending_approval"),
                        (("no officer", "unassigned", "not assigned", "without an officer"), "unassigned"), (("unavailable", "off duty", "not on duty"), "officers_unavailable"),
                        (("bank",), "waiting_bank"), (("citizen", "waiting for the"), "waiting_citizen"), (("changed", "since", "new"), "changes")):
        if any(w in q for w in words):
            return tool, ""
    if settings.ai_mode == "live":
        started, model = time.monotonic(), _JsonModel(settings)
        try:
            picked = model.call("Choose the one query that answers the admin's question about a report queue. argument is a CB- reference or a UPI ID when the question names one, else null. Return only the schema.",
                                               "QUESTION (data)\n" + question[:300], "cyberbrief_admin_tool_v1", _TOOL_SCHEMA)
            if telemetry is not None:
                telemetry.update({"origin": "model", "durationMs": int((time.monotonic() - started) * 1000), **model.last})
            if picked.get("tool") in TOOLS:
                return picked["tool"], picked.get("argument") or ""
        except (ModelError, ValueError):
            pass
    return None, ""


class AskBody(ApiModel):
    question: str = Field(min_length=1, max_length=300)


@admin_router.post("/ask")
def ask(session: CurrentAdmin, db: Db, settings: Config, payload: AskBody) -> dict[str, Any]:
    tele: dict[str, Any] = {"origin": "rules"}
    tool, arg = choose_tool(settings, payload.question, tele)
    if tool is None:
        return _table("I can't answer that from the records", [], [], "Try: reports with no officer, officers off duty with work, reports waiting for a bank response, or give a CB- reference or UPI ID.")
    return {**TOOLS[tool](db, arg), "tool": tool, "telemetry": tele}

