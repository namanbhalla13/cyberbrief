"""Durable case events and the small dispatcher that acts on them.

An event is written in the same transaction as the change it describes (a report submitted, evidence added, a reply received,
an acknowledgement overdue, routing changed), so it cannot be lost and cannot be invented. The dispatcher claims a pending event
with a lease, runs the handler for its kind, and records the outcome, the failure and the time taken on the row. Handlers only
*prepare*: they read authorised records and write proposals or tasks. Anything with a consequence still waits for a person.

Delivery is at-least-once and every handler is idempotent: the event key is unique, a proposal is found by its digest, a run is
found by its idempotency key. A crash mid-handler leaves a lease that lapses, after which the event is claimed again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .ai import get_runner
from .core import Settings, make_session
from .models import AgentProposal, Case, CaseEvent, CaseTask, ProcessingRun, Source, utcnow
from .schemas import PROMPT_VERSION, SCHEMA_VERSION
from .workflow import log_activity, open_task, propose_assignment

log = logging.getLogger("cyberbrief.events")

LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 3
ACK_OVERDUE = timedelta(hours=24)
PROCESSABLE = ("image", "supplied_official_update", "audio", "transcript", "typed_account", "user_answer")


def emit(db: Session, case_id: str, kind: str, key: str, payload: dict[str, Any] | None = None) -> CaseEvent:
    """Record an event once. Called inside the transaction that made the change; a repeated key returns the first event."""
    existing = db.scalar(select(CaseEvent).where(CaseEvent.idempotency_key == key))
    if existing is not None:
        return existing
    event = CaseEvent(case_id=case_id, kind=kind, idempotency_key=key, payload=payload or {})
    db.add(event)
    db.flush()
    return event


# ---- handlers ------------------------------------------------------------------------------------------------------------

def start_processing(db: Session, case: Case, settings: Settings) -> str | None:
    """Start one extraction run over the sources that are stored or failed. A case already being processed is left alone."""
    sources = db.scalars(select(Source).where(Source.case_id == case.id, Source.status.in_(("stored", "failed")), Source.kind.in_(PROCESSABLE))).all()
    if not sources or case.active_run_id:
        return None
    run = ProcessingRun(case_id=case.id, status="queued", stage="queued", base_revision=case.current_revision,
                        input_source_ids=[s.id for s in sources], input_hashes=[s.sha256 for s in sources], ai_mode=settings.ai_mode,
                        provider="meta" if settings.ai_mode == "live" else "fixture",
                        model_id=settings.meta_text_model if settings.ai_mode == "live" else "fixture",
                        prompt_version=PROMPT_VERSION if settings.ai_mode == "live" else "fixture", schema_version=SCHEMA_VERSION,
                        idempotency_key=f"events:{case.id}:{case.current_revision}:{len(sources)}")
    db.add(run)
    db.flush()
    for s in sources:
        s.status = "queued"
    case.active_run_id = run.id
    case.packet_status = "processing"
    db.commit()
    run_id = run.id
    db.expunge_all()
    get_runner().submit(run_id)
    return run_id


def _on_submitted(db: Session, settings: Settings, event: CaseEvent) -> dict[str, Any]:
    case = db.get(Case, event.case_id)
    if case is None or case.review_state != "submitted" or case.assigned_officer_id:
        return {"skipped": "the report is no longer waiting for an officer"}
    out = propose_assignment(db, settings, case, None)
    if "proposal" in out:
        return {"proposalId": out["proposal"]["id"]}
    return {"exception": out["exception"]}


class Deferred(Exception):
    """Raised by a handler that cannot act yet but must not be marked done: the event goes back to pending without using up an attempt."""


MAX_DEFERRALS = 40   # about 20 minutes at the 30 second tick; after that a stuck run is shown to the admin as a failure, not waited on forever


def _unread_sources(db: Session, case_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Source).where(Source.case_id == case_id, Source.status == "stored", Source.kind.in_(PROCESSABLE))) or 0


def drain_after_run(db: Session, case_id: str, run_id: str) -> None:
    """Called in the transaction that ends a run: evidence that arrived while it was reading is queued to be read next, exactly once."""
    if _unread_sources(db, case_id):
        emit(db, case_id, "evidence_added", f"drain:{case_id}:{run_id}", {"after": run_id})


def _on_evidence(db: Session, settings: Settings, event: CaseEvent) -> dict[str, Any]:
    case = db.get(Case, event.case_id)
    if case is None:
        return {"skipped": "no such case"}
    if case.active_run_id and _unread_sources(db, case.id):
        raise Deferred("a run is still reading this report")   # not done: the reply is read as soon as that run has finished
    run_id = start_processing(db, case, settings)
    log_activity(db, event.case_id, "evidence_agent", ["New evidence was added to the report", "Started reading it" if run_id else "Nothing new to read, or a read is already under way"],
                 "Reading new evidence" if run_id else "Nothing to read")
    db.commit()
    return {"runId": run_id}


def _on_deadline(db: Session, settings: Settings, event: CaseEvent) -> dict[str, Any]:
    case = db.get(Case, event.case_id)
    if case is None or not case.assigned_officer_id or case.acknowledged_at is not None:
        return {"skipped": "the assignment was acknowledged or changed"}
    exists = db.scalar(select(CaseTask).where(CaseTask.case_id == case.id, CaseTask.kind == "follow_up", CaseTask.state == "open", CaseTask.ref_id == case.assigned_officer_id))
    if exists is None:
        open_task(db, case.id, "follow_up", "The officer has not acknowledged this assignment: remind them or move the report", ref_id=case.assigned_officer_id)
    log_activity(db, case.id, "coordinator", ["The assignment passed its acknowledgement deadline", "Opened a follow-up for the admin"], "Acknowledgement overdue")
    return {"followUp": True}


def _on_routing_changed(db: Session, settings: Settings, event: CaseEvent) -> dict[str, Any]:
    case = db.get(Case, event.case_id)
    if case is None or case.review_state != "submitted" or case.assigned_officer_id:
        return {"skipped": "the report is not waiting for an officer"}
    for p in db.scalars(select(AgentProposal).where(AgentProposal.case_id == case.id, AgentProposal.kind == "assignment", AgentProposal.state == "pending")).all():
        p.state = "stale"
    db.flush()
    out = propose_assignment(db, settings, case, None)
    return {"proposalId": out["proposal"]["id"]} if "proposal" in out else {"exception": out["exception"]}


def _on_investigation(db: Session, settings: Settings, event: CaseEvent) -> dict[str, Any]:
    from .investigation import handle_investigation_event

    return handle_investigation_event(db, settings, event.payload, event.attempts, MAX_ATTEMPTS)


HANDLERS: dict[str, Callable[[Session, Settings, CaseEvent], dict[str, Any]]] = {
    "investigation_requested": _on_investigation,
    "submitted": _on_submitted, "evidence_added": _on_evidence, "reply_received": _on_evidence,
    "deadline_due": _on_deadline, "routing_changed": _on_routing_changed,
}


# ---- the dispatcher ------------------------------------------------------------------------------------------------------

def _claim(db: Session, event_id: str) -> bool:
    """One conditional UPDATE: only a pending event, or one whose lease has lapsed, can be taken, and only once."""
    now = utcnow()
    took = db.execute(update(CaseEvent).where(
        CaseEvent.id == event_id, CaseEvent.attempts < MAX_ATTEMPTS,
        ((CaseEvent.state == "pending") | ((CaseEvent.state == "claimed") & (CaseEvent.lease_until < now))),
    ).values(state="claimed", attempts=CaseEvent.attempts + 1, lease_until=now + LEASE)).rowcount
    db.commit()
    return took == 1


def dispatch_pending(settings: Settings, limit: int = 20) -> int:
    """Run what is waiting. Never raises: a failure is recorded on the event and retried later, not shown to the person whose request caused it."""
    done = 0
    try:
        with make_session() as db:
            now = utcnow()
            ids = list(db.scalars(select(CaseEvent.id).where(
                CaseEvent.attempts < MAX_ATTEMPTS,
                (CaseEvent.state == "pending") | ((CaseEvent.state == "claimed") & (CaseEvent.lease_until < now)),
            ).order_by(CaseEvent.created_at).limit(limit)).all())
    except Exception:  # noqa: BLE001
        log.exception("could not read the event queue")
        return 0
    for event_id in ids:
        started = time.monotonic()
        with make_session() as db:
            if not _claim(db, event_id):
                continue   # another worker has it, or it was finished meanwhile
            event = db.get(CaseEvent, event_id)
            handler = HANDLERS.get(event.kind)
            try:
                result = handler(db, settings, event) if handler else {"skipped": f"no handler for {event.kind}"}
                event = db.get(CaseEvent, event_id)
                event.state, event.result, event.error = "done", result, None
                event.finished_at = utcnow()
            except Deferred as wait:
                db.rollback()
                event = db.get(CaseEvent, event_id)
                waited = int((event.result or {}).get("deferred", 0)) + 1
                event.result = {"deferred": waited, "why": str(wait)}
                event.attempts = max(0, event.attempts - 1)   # waiting is not a failure
                event.lease_until = None
                event.state = "pending"
                if waited >= MAX_DEFERRALS:
                    event.state, event.error = "failed", "A processing run stayed active for too long; retry once it has been checked."
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                event = db.get(CaseEvent, event_id)
                event.error = f"{type(exc).__name__}: {str(exc)[:240]}"
                event.state = "failed" if event.attempts >= MAX_ATTEMPTS else "pending"
                event.lease_until = None
                log.warning("event %s (%s) failed on attempt %d: %s", event_id, event.kind, event.attempts, event.error)
            event.elapsed_ms = int((time.monotonic() - started) * 1000)
            db.commit()
            done += 1
    return done


def scan_deadlines(settings: Settings) -> int:
    """Turn an assignment nobody acknowledged within the allowed time into a deadline event, once per assignment."""
    cutoff = utcnow() - ACK_OVERDUE
    made = 0
    with make_session() as db:
        for case in db.scalars(select(Case).where(Case.assigned_officer_id.is_not(None), Case.acknowledged_at.is_(None), Case.assigned_at < cutoff)).all():
            before = db.scalar(select(CaseEvent.id).where(CaseEvent.idempotency_key == f"deadline:{case.id}:{case.assigned_at.isoformat()}"))
            if before is None:
                emit(db, case.id, "deadline_due", f"deadline:{case.id}:{case.assigned_at.isoformat()}", {"assignedTo": case.assigned_officer_id})
                made += 1
        db.commit()
    return made


def _refresh_index() -> None:
    """Bring the search index up to date with the reports it covers. A failure is logged and never stops the events."""
    from .intelligence import refresh_projections

    try:
        with make_session() as db:
            refresh_projections(db, commit_each=True)
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not refresh the case index")


def run_events(settings: Settings, *, index: bool = False) -> int:
    """Called after a change commits, at startup, and when the admin opens the queue. The search index is brought up to date only on the timer and at startup (`index=True`), never on the back of a request."""
    scan_deadlines(settings)
    done = dispatch_pending(settings)
    if index:
        _refresh_index()
    return done


# ---- the background loop and what the admin can see of it ------------------------------------------------------------------

INTERVAL_SECONDS = 30


class EventLoop:
    """Runs `run_events` every INTERVAL_SECONDS on one daemon thread, so a due deadline or an event whose lease lapsed is handled
    without anyone opening a page. A second process would be safe too: an event is claimed by a conditional UPDATE."""

    def __init__(self, settings: Settings, interval: float = INTERVAL_SECONDS) -> None:
        import threading

        self._settings, self._interval = settings, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="case-events", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def tick(self) -> int:
        try:
            return run_events(self._settings, index=True)
        except Exception:  # noqa: BLE001
            log.exception("the event loop tick failed")
            return 0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()


def event_health(db: Session) -> dict[str, Any]:
    """Counts by state and the most recent failures, for the admin: a model or network failure is shown, not hidden."""
    from sqlalchemy import func

    counts = dict(db.execute(select(CaseEvent.state, func.count()).group_by(CaseEvent.state)).all())
    failed = db.scalars(select(CaseEvent).where(CaseEvent.state == "failed").order_by(CaseEvent.created_at.desc()).limit(5)).all()
    return {"pending": counts.get("pending", 0) + counts.get("claimed", 0), "done": counts.get("done", 0), "failed": counts.get("failed", 0),
            "recentFailures": [{"id": e.id, "kind": e.kind, "error": e.error, "attempts": e.attempts, "caseId": e.case_id} for e in failed]}


def retry_failed(db: Session) -> int:
    """Put failed events back in the queue with a fresh set of attempts. Handlers are idempotent, so a retry cannot do anything twice."""
    n = 0
    for e in db.scalars(select(CaseEvent).where(CaseEvent.state == "failed")).all():
        e.state, e.attempts, e.lease_until, e.error = "pending", 0, None, None
        n += 1
    db.commit()
    return n
