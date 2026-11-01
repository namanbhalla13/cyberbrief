"""Check the investigation search and assistant against labelled fictional cases, in a throwaway database. Prints plain counts with their denominators.

What it measures, on 26 invented reports and 10 questions (evals/investigation/retrieval_labels.json, expectations written by hand):
- whether each expected connection came back with the expected kind (exact identifier, generic, similar method, similar name),
- precision and recall at k for the exact-identifier matches, where k is the number of expected exact matches,
- false positives: a report returned that the labels say should be absent, or returned as a stronger kind than labelled,
- scope leaks: a report outside the viewer's scope that shows up anywhere in the response (its reference, text or owner),
- repeated recipients (an exact match that carries a note, not a downgrade), shared-service handles, contradictions, missing details,
  duplicates counted once, a source correction, and a permission withdrawn mid-session (no protected reference left in a saved run),
- three assistant runs (a known match, no match, missing evidence): the state each ended in, the tools that ran, citations kept and rejected,
  the time taken, and where the planner and the summary came from.

Modes (the exit code is 0 only when every criterion required by the mode passes):
  default          fixture mode. No model is called; the report says so. The model-execution criterion is "not applicable", not passed.
  --live           a LIVE-REQUIRED check. The planner and the summary of every run must come from the model; a run that fell back to fixed rules
                   fails the model-execution criterion, however well the search did. Run it where the model host's certificate can be verified.
  --fallback-check the model host is made unreachable on purpose (verification stays on). Every run must still finish, from fixed rules, and say
                   so. This is the resilience test; it is not evidence about the live model.

It checks that the rules do what they say on invented data. It is not a measure of accuracy on real reports.

Usage:  python scripts/run_investigation_eval.py [--live | --fallback-check] [--out evals/reports]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE = "--live" in sys.argv
FALLBACK = "--fallback-check" in sys.argv
_TMP = Path(tempfile.mkdtemp(prefix="cyberbrief-invest-eval-"))
os.environ.update(APP_ENV="test", DATABASE_URL=f"sqlite:///{(_TMP / 'eval.db').as_posix()}", STORAGE_ROOT=str(_TMP / "private"), CHECKPOINT_DB=str(_TMP / "ck.db"), JOB_RUNNER="inline")
if FALLBACK:   # an unreachable host, certificate checking left on, a short wait: the model cannot answer, and nothing else changes
    os.environ.update(AI_MODE="live", MODEL_API_KEY="fallback-check-not-a-real-key", META_BASE_URL="https://127.0.0.1:9/v1", MODEL_TIMEOUT_SECONDS="3")
elif not LIVE:
    os.environ.update(AI_MODE="fixture", MODEL_API_KEY="")
sys.path.insert(0, str(REPO_ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.assistant import case_ref  # noqa: E402
from app.core import make_session  # noqa: E402
from app.demo_investigation import build_dataset, build_eval_extras  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Account, Case, Fact, OfficerProfile  # noqa: E402
from app.records import bump_revision, supersede_fact  # noqa: E402
from app.security import hash_secret  # noqa: E402

LABELS = json.loads((REPO_ROOT / "evals" / "investigation" / "retrieval_labels.json").read_text(encoding="utf-8"))
TAG = "evl"
STAFF = {"arjun": ("CENTRAL", ["area_discovery"]), "meera": ("CENTRAL", ["area_discovery"]), "divya": ("EAST", ["area_discovery"])}
KINDS = LABELS["kind_order"]
PROTECTED_WORDS = ("Officer Meera", "Officer Divya", "OUT-OF-SCOPE-MARKER")


def csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies.get("cb_csrf", "")}


class Tally:
    """A numerator and a denominator, kept together so a figure is never shown without what it is out of."""

    def __init__(self) -> None:
        self.n = self.d = 0

    def add(self, ok: bool) -> None:
        self.d += 1
        self.n += int(ok)

    def __str__(self) -> str:
        return f"{self.n}/{self.d}"

    @property
    def ok(self) -> bool:
        return self.n == self.d


def missing_tools(r: dict) -> str:
    """When the tools expectation failed, which labelled tools did not run."""
    want = next((x["expect"]["tools"] for x in LABELS["runs"] if x["id"] == r["id"]), [])
    gone = [t for t in want if t not in r["tools"]]
    return f" (did not run: {', '.join(gone)})" if gone and not r["expectationsMet"]["tools"] else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--fallback-check", action="store_true")
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports"))
    args = parser.parse_args()
    mode = "live" if args.live else "fallback-check" if args.fallback_check else "fixture"
    with TestClient(create_app()) as app_client:
        ids, logins = {}, {}
        with make_session() as db:
            for name, (area, caps) in STAFF.items():
                login = f"{name.upper()}-{TAG}"
                account = Account(kind="officer", officer_id=login, display_name=f"Officer {name.title()}", password_hash=hash_secret("eval-demo-pass"), mobile=None)
                db.add(account)
                db.flush()
                db.add(OfficerProfile(account_id=account.id, areas=[f"{area.lower()}-{TAG}"], languages=["en"], on_duty=True, capacity=80, active_count=0, capabilities=caps))
                ids[name], logins[name] = account.id, login
            cases = build_dataset(db, ids, area=f"central-{TAG}", other_area=f"east-{TAG}", tag=TAG)
            cases.update(build_eval_extras(db, ids, area=f"central-{TAG}", tag=TAG))
            db.commit()
        clients: dict[str, TestClient] = {}

        def officer(name: str) -> TestClient:
            if name not in clients:
                c = TestClient(app_client.app)
                c.__enter__()
                c.post("/api/session")
                c.post("/api/auth/officer/login", headers=csrf(c), json={"officerId": logins[name], "password": "eval-demo-pass"}).raise_for_status()
                clients[name] = c
            return clients[name]

        names = {case_ref(v): k for k, v in cases.items()}

        def search(viewer: str, case: str) -> dict:
            o = officer(viewer)
            r = o.post(f"/api/officer/cases/{cases[case]}/connections/search?limit=10", headers=csrf(o))
            r.raise_for_status()
            return r.json()

        report: dict = {"mode": mode, "dataset": LABELS["dataset"], "reports": len(cases), "questions": len(LABELS["queries"]), "queries": []}
        connections, exact_p, exact_r, falsepos, leaks = Tally(), Tally(), Tally(), Tally(), Tally()
        checks = {k: Tally() for k in ("attribution_note", "attribution_limit", "contradictions", "missing_data", "same_report_as", "after_correction", "after_revocation")}
        categories: dict[str, list[str]] = {}

        def score(q: dict, out: dict, expect: dict) -> dict:
            got = {names[c["caseRef"]]: c for c in out["candidates"] if c["caseRef"] in names}
            blob = json.dumps(out)
            expected = {case: kind for kind in KINDS for case in expect.get(kind, [])}
            for case, kind in expected.items():
                connections.add(got.get(case, {}).get("resultKind") == kind)
            wrong = [c for c in got if c in expect.get("absent", [])] + [c for c, v in got.items() if c in expected and v["resultKind"] != expected[c]]
            for case in expect.get("absent", []):
                falsepos.add(case not in got)
            for c, v in got.items():
                if c in expected:
                    falsepos.add(v["resultKind"] == expected[c])
            exact_expected = set(expect.get("exact_identifier", []))
            ranked = [names.get(c["caseRef"]) for c in out["candidates"] if c["resultKind"] == "exact_identifier"]
            if exact_expected:
                top = ranked[: len(exact_expected)]
                exact_p.d += len(exact_expected)
                exact_p.n += sum(1 for t in top if t in exact_expected)
                exact_r.d += len(exact_expected)
                exact_r.n += sum(1 for e in exact_expected if e in top)
            if "returned" in expect:
                connections.add(len(out["candidates"]) == expect["returned"])
            hidden = expect.get("out_of_scope", [])
            for c in hidden:
                leaked = case_ref(cases[c]) in blob or (c == "out_of_scope" and ("OUT-OF-SCOPE-MARKER" in blob or ("Officer Divya" in blob and q["viewer"] != "divya")))
                leaks.add(not leaked)
            return {"returned": sorted(got), "wrong": wrong}

        for q in LABELS["queries"]:
            categories.setdefault(q["category"], []).append(q["id"])
            out = search(q["viewer"], q["case"])
            rec = {"id": q["id"], "category": q["category"], **score(q, out, q["expect"])}
            c = q.get("checks", {})
            got = {names[x["caseRef"]]: x for x in out["candidates"] if x["caseRef"] in names}
            if "attribution_note" in c:
                spec = c["attribution_note"]
                for case in spec["cases"]:
                    sigs = got.get(case, {}).get("matchedSignals", [])
                    checks["attribution_note"].add(any(s.get("independentReports") == spec["independentReports"] and "pattern worth reviewing" in (s.get("attribution") or "") and s["strength"] == "strong" for s in sigs))
            for case in c.get("attribution_limit", []):
                checks["attribution_limit"].add(any("shared service" in (s.get("attribution") or "") for s in got.get(case, {}).get("matchedSignals", [])))
            for case, n in c.get("contradictions_min", {}).items():
                checks["contradictions"].add(len(got.get(case, {}).get("contradictions", [])) >= n)
            if "missing_data" in c:
                checks["missing_data"].add(set(c["missing_data"]) <= set(out["missingData"]))
            for case, others in c.get("same_report_as", {}).items():
                checks["same_report_as"].add(sorted(got.get(case, {}).get("sameReportAs", [])) == sorted(case_ref(cases[o]) for o in others))
            if "after_correction" in q:
                spec = q["after_correction"]
                with make_session() as db:
                    case = db.get(Case, cases[spec["case"]])
                    fact = db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.kind == "counterparty", Fact.is_current.is_(True))).first()
                    revision = bump_revision(db, case, kind="review", summary="Corrected the recipient", actor_session_id=None, actor_role="citizen")
                    vpa = f"{spec['recipient']}{TAG}.demo@demobank"
                    supersede_fact(db, fact, revision=revision, review_state="user_confirmed", actor_role="citizen", value={"value": f"Corrected Shop ({vpa})"}, raw_text=vpa)
                    db.commit()
                after = search(q["viewer"], spec["case"])
                got_after = {names[x["caseRef"]]: x["resultKind"] for x in after["candidates"] if x["caseRef"] in names}
                for kind, wanted in spec["expect"].items():
                    for case_key in wanted:
                        checks["after_correction"].add(got_after.get(case_key) == kind)
            if "after_revocation" in q:
                spec = q["after_revocation"]
                with make_session() as db:
                    db.get(OfficerProfile, ids[spec["viewer"]]).capabilities = []
                    db.commit()
                try:
                    after = search(q["viewer"], q["case"])
                    blob = json.dumps(after)
                    for case_key in spec["expect"].get("absent", []):
                        checks["after_revocation"].add(case_ref(cases[case_key]) not in blob and "Officer Meera" not in blob)
                    if "returned" in spec["expect"]:
                        checks["after_revocation"].add(len(after["candidates"]) == spec["expect"]["returned"])
                finally:
                    with make_session() as db:
                        db.get(OfficerProfile, ids[spec["viewer"]]).capabilities = ["area_discovery"]
                        db.commit()
            report["queries"].append(rec)
        report["categories"] = {k: v for k, v in categories.items()}

        # ---- three assistant runs --------------------------------------------------------------------------------------------------
        runs: list[dict] = []
        run_checks = Tally()
        citations_kept = citations_total = 0
        for spec in LABELS["runs"]:
            o = officer(spec["viewer"])
            base = f"/api/officer/cases/{cases[spec['case']]}"
            started = time.monotonic()
            first = o.post(f"{base}/investigations", headers=csrf(o), json={"goal": spec["goal"]})
            done = first.json()
            for _ in range(360):
                if first.status_code != 200:
                    break
                done = o.get(f"{base}/investigations/{first.json()['id']}").json()
                if done["state"] not in ("queued", "running"):
                    break
                time.sleep(0.5)
            wall = time.monotonic() - started
            kept = sum(len(c["supporting"]) for c in done.get("cards", []))
            rejected = done.get("rejectedCitations", 0)
            citations_kept, citations_total = citations_kept + kept, citations_total + kept + rejected
            tele = done.get("telemetry") or {}
            tools = [t["tool"] for t in done.get("trace", []) if t["state"] == "done"]
            card = next((c for c in done.get("cards", []) if c["kind"] == "matches"), None)
            consultation = next((c for c in done.get("cards", []) if c["kind"] == "consultation"), None)
            ex = spec["expect"]
            ok_state = done.get("state") in ex["states"]
            ok_tools = set(ex["tools"]) <= set(tools)
            ok_consult = (consultation is not None and consultation["data"].get("state") == "draft") if ex["consultation"] == "drafted" else consultation is None
            ok_finding = ex.get("finding_contains", "") in (card or {}).get("finding", "") if "finding_contains" in ex else True
            for good in (ok_state, ok_tools, ok_consult, ok_finding):
                run_checks.add(good)
            reasons = [f"plan attempt: {a.get('error') or 'unusable plan'}" for a in tele.get("planAttempts", []) if not a.get("ok")]
            if tele.get("summary", {}).get("outcome") not in (None, "used"):
                reasons.append(f"summary: {tele['summary'].get('outcome')} {tele['summary'].get('error') or ''}".strip())
            runs.append({"id": spec["id"], "kind": spec["kind"], "state": done.get("state"), "origin": done.get("origin"), "planner": tele.get("planner"), "plannerReason": tele.get("reason"),
                         "summaryOrigin": (done.get("summary") or {}).get("origin"), "provider": done.get("provider"), "model": done.get("modelId"), "toolCalls": done.get("toolCalls"),
                         "tools": tools, "citationsKept": f"{kept}/{kept + rejected}", "rejectedCitations": rejected, "wallSeconds": round(wall, 2),
                         "medianToolMs": int(statistics.median(t["ms"] for t in done["trace"] if "ms" in t)) if any("ms" in t for t in done.get("trace", [])) else None,
                         "error": done.get("error"), "fallbackReasons": reasons, "expectationsMet": {"state": ok_state, "tools": ok_tools, "consultation": ok_consult, "finding": ok_finding},
                         "findingText": (card or {}).get("finding")})
            if spec["id"] == "R1":
                first_run = (o, base, done["id"])
        report["runs"] = runs

        # ---- a source correction makes the first run stale; a withdrawn permission leaves no protected reference in it ----------------
        o, base, run_id = first_run
        with make_session() as db:
            case = db.get(Case, cases["current"])
            fact = db.scalars(select(Fact).where(Fact.case_id == case.id, Fact.kind == "counterparty", Fact.is_current.is_(True))).first()
            revision = bump_revision(db, case, kind="review", summary="Corrected the recipient", actor_session_id=None, actor_role="citizen")
            supersede_fact(db, fact, revision=revision, review_state="user_confirmed", actor_role="citizen", value={"value": "Soundhub Online (soundhub.demo@demobank)"}, raw_text="soundhub.demo@demobank")
            db.commit()
        stale = Tally()
        stale.add(o.get(f"{base}/investigations/{run_id}").json()["state"] == "stale")
        with make_session() as db:
            db.get(OfficerProfile, ids["arjun"]).capabilities = []
            db.commit()
        try:
            blob = json.dumps(o.get(f"{base}/investigations/{run_id}").json()) + json.dumps(o.get(f"{base}/investigations").json())
            former = [case_ref(v) for k, v in cases.items() if k != "current"]
            revoked_clean = Tally()
            revoked_clean.add(not [r for r in (*former, *PROTECTED_WORDS) if r in blob])
        finally:
            with make_session() as db:
                db.get(OfficerProfile, ids["arjun"]).capabilities = ["area_discovery"]
                db.commit()
        for c in clients.values():
            c.__exit__(None, None, None)

    # ---- criteria: what each mode requires, each with the figure it rests on --------------------------------------------------------
    model_runs = Tally()
    for r in runs:
        model_runs.add(r["origin"] == "live" and r["planner"] == "model" and r["summaryOrigin"] == "model")
    fallback_runs = Tally()
    for r in runs:
        fallback_runs.add(r["origin"] == "rules" and r["planner"] == "rules" and r["summaryOrigin"] == "rules" and r["state"] in ("completed", "waiting_for_approval") and bool(r["fallbackReasons"]))
    run_states = Tally()
    for r in runs:
        run_states.add(r["state"] in ("completed", "waiting_for_approval"))
    cites = Tally()
    cites.n, cites.d = citations_kept, citations_total
    criteria: list[tuple[str, bool | None, str]] = [
        ("expected connections returned with the expected kind", connections.ok, str(connections)),
        ("exact-match precision at k", exact_p.ok, str(exact_p)),
        ("exact-match recall at k", exact_r.ok, str(exact_r)),
        ("no false positives (absent report returned, or a stronger kind than labelled)", falsepos.ok, str(falsepos)),
        ("no out-of-scope report appears anywhere in a response", leaks.ok, str(leaks)),
        ("repeated recipient keeps its exact match and carries the independent count and a note", checks["attribution_note"].ok, str(checks["attribution_note"])),
        ("a known payment hub carries an explicit attribution limit", checks["attribution_limit"].ok, str(checks["attribution_limit"])),
        ("contradictory details are listed", checks["contradictions"].ok, str(checks["contradictions"])),
        ("missing details are named", checks["missing_data"].ok, str(checks["missing_data"])),
        ("a payment reported twice is counted once", checks["same_report_as"].ok, str(checks["same_report_as"])),
        ("a source correction changes the result", checks["after_correction"].ok, str(checks["after_correction"])),
        ("a withdrawn permission removes the connection from a new search", checks["after_revocation"].ok, str(checks["after_revocation"])),
        ("assistant runs reach a successful state", run_states.ok, str(run_states)),
        ("assistant runs meet their labelled expectations (state, tools, consultation, finding)", run_checks.ok, str(run_checks)),
        ("every source link kept is real (no unsupported citation dropped)", report["runs"] and all(r["rejectedCitations"] == 0 for r in runs), f"{citations_kept} kept, {citations_total - citations_kept} rejected"),
        ("a source correction marks the saved run stale", stale.ok, str(stale)),
        ("a withdrawn permission leaves no protected reference in a saved run", revoked_clean.ok, str(revoked_clean)),
    ]
    if mode == "live":
        criteria.append(("MODEL EXECUTION: planner and summary came from the model in every run", model_runs.ok, f"{model_runs} runs"))
    elif mode == "fallback-check":
        criteria.append(("FALLBACK RESILIENCE: every run finished from fixed rules with the failure recorded", fallback_runs.ok, f"{fallback_runs} runs"))
    else:
        criteria.append(("model execution", None, "not applicable: fixture mode, no model was called"))
    passed = all(ok for _n, ok, _d in criteria if ok is not None)
    report.update({"generatedAt": datetime.now(UTC).isoformat(), "criteria": [{"name": n, "result": "n/a" if ok is None else "PASS" if ok else "FAIL", "detail": d} for n, ok, d in criteria], "passed": passed,
                   "figures": {"connections": str(connections), "precisionAtK": str(exact_p), "recallAtK": str(exact_r), "falsePositiveChecks": str(falsepos), "scopeLeakChecks": str(leaks),
                               "citationsKept": f"{citations_kept}/{citations_total}", "staleAfterCorrection": str(stale)}})
    lines = [f"# Investigation check ({mode} mode, {report['generatedAt'][:16]}Z): {'PASSED' if passed else 'FAILED'}", "", f"Dataset: {report['dataset']}", "",
             "## Criteria", ""]
    lines += [f"- **{c['result']}** {c['name']}: {c['detail']}" for c in report["criteria"]]
    lines += ["", "## Assistant runs", "", "| run | kind | state | planner | summary | tools | citations kept | seconds | fallback reasons | labelled expectations not met |", "|---|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['kind']} | {r['state']} | {r['planner']} ({r['origin']}) | {r['summaryOrigin']} | {r['toolCalls']} | {r['citationsKept']} | {r['wallSeconds']} | {'; '.join(r['fallbackReasons']) or '-'} | {', '.join(k for k, v in r['expectationsMet'].items() if not v) or '-'}{missing_tools(r)} |" for r in runs]
    origin_line = {"live": "Every run's planner and summary came from the live model." if model_runs.ok else "NOT every run used the model: at least one planner or summary fell back to fixed rules (see the table). This is not a live-model pass.",
                   "fallback-check": "The model host was unreachable on purpose. This is a resilience check and says nothing about the live model.",
                   "fixture": "Fixture mode: no model was called. This is not live-model evidence."}[mode]
    lines += ["", origin_line, "", "Twenty-six invented reports and ten questions: this checks that the rules behave as written. It is not an accuracy figure for real reports.", ""]
    text = "\n".join(lines)
    out_dir = Path(args.out) / f"investigation-{mode}-{report['generatedAt'][:19].replace(':', '-')}Z"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.md").write_text(text, encoding="utf-8")
        (out_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {out_dir}")
    except OSError as exc:
        print(f"Could not write the report folder ({exc}); the figures are printed below.")
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
