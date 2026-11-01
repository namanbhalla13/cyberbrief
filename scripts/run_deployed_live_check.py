"""Three bounded investigation runs against a RUNNING deployment, over its real HTTP API, on fictional reports, and a plain record of what happened.

Use it on the demo container (docker compose, AI_MODE=live) after seeding the fictional set with `--extras`:
    docker exec <container> python /app/scripts/seed_investigation_demo.py --tag T --extras --json
    python scripts/run_deployed_live_check.py --base http://127.0.0.1:8080 --tag T --out evals/reports/live-check

What it records for each run: whether the planner and the summary came from the model or fell back to fixed rules (and the error code if they fell back), the tools that ran,
citations kept and dropped, the time end to end, the run's state and its error. It prints no credential: the demo officers' passwords come from CB_OFFICER_PASSWORD, else the
documented demo one. These are smoke checks of three runs; they are not a reliability or latency statistic. Exit code 0 only if all three runs reached a successful state;
the model-origin result is reported separately (`--require-model` makes it part of the exit code).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

GOAL = "Investigate this report: find earlier complaints with the same recipient or a similar reported method, show how they were handled and prepare next steps."


def login(base: str, officer_id: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=60)
    c.post("/api/session").raise_for_status()
    token = c.cookies.get("cb_csrf", "")
    c.post("/api/auth/officer/login", headers={"X-CSRF-Token": token}, json={"officerId": officer_id, "password": os.environ.get("CB_OFFICER_PASSWORD", "officer-demo-pass")}).raise_for_status()
    return c


def case_id(c: httpx.Client, title: str) -> str:
    page = 1
    while True:
        work = c.get(f"/api/officer/work?state=all&page={page}&pageSize=100").json()
        for item in work["items"]:
            if item["title"] == title:
                return item["id"]
        if page >= work.get("pages", 1):
            raise SystemExit(f"No report titled {title!r} is assigned to this officer. Seed it first.")
        page += 1


def run_one(c: httpx.Client, cid: str, label: str, wait: int) -> dict:
    token = {"X-CSRF-Token": c.cookies.get("cb_csrf", "")}
    started = time.monotonic()
    first = c.post(f"/api/officer/cases/{cid}/investigations", headers=token, json={"goal": GOAL})
    first.raise_for_status()
    run = first.json()
    while time.monotonic() - started < wait:
        run = c.get(f"/api/officer/cases/{cid}/investigations/{run['id']}").json()
        if run["state"] not in ("queued", "running"):
            break
        time.sleep(1)
    wall = time.monotonic() - started
    tele = run.get("telemetry") or {}
    kept = sum(len(card["supporting"]) for card in run["cards"])
    reasons = [f"plan attempt: {a.get('error') or 'unusable plan'}" for a in tele.get("planAttempts", []) if not a.get("ok")]
    if tele.get("summary", {}).get("outcome") not in (None, "used"):
        reasons.append(f"summary: {tele['summary'].get('outcome')} {tele['summary'].get('error') or ''}".strip())
    card = next((x for x in run["cards"] if x["kind"] == "matches"), None)
    return {"label": label, "state": run["state"], "origin": run["origin"], "provider": run["provider"], "model": run["modelId"], "planner": tele.get("planner"), "plannerReason": tele.get("reason"),
            "summaryOrigin": (run.get("summary") or {}).get("origin"), "toolCalls": run["toolCalls"], "tools": [t["tool"] for t in run["trace"] if t["state"] == "done"],
            "toolMs": {t["tool"]: t.get("ms") for t in run["trace"] if t["state"] == "done"}, "citationsKept": f"{kept}/{kept + run['rejectedCitations']}", "rejectedCitations": run["rejectedCitations"],
            "wallSeconds": round(wall, 2), "error": run["error"], "fallbackReasons": reasons, "finding": (card or {}).get("finding"), "summary": (run.get("summary") or {}).get("text"),
            "consultationDrafted": any(x["kind"] == "consultation" and x["data"].get("state") == "draft" for x in run["cards"]), "drafts": [d["kind"] for d in run["drafts"]],
            "runId": run["id"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", default="evals/reports/live-check")
    ap.add_argument("--wait", type=int, default=240, help="seconds to wait for each run")
    ap.add_argument("--require-model", action="store_true")
    args = ap.parse_args()
    health = httpx.get(f"{args.base}/api/health", timeout=15).json()
    if health.get("aiMode") != "live":
        print(f"The deployment at {args.base} is in {health.get('aiMode')} mode: this would not be a live check.")
        return 2
    arjun, meera = login(args.base, "CBI1042"), login(args.base, "CBI2001")
    from_title = {"known_match": (arjun, f"Sports shoes paid for, never arrived ({args.tag})"), "no_match": (meera, "No match: a recipient nobody else names"),
                  "missing_evidence": (arjun, "Missing detail: nothing complete to search by")}
    runs = []
    for label, (client, title) in from_title.items():
        runs.append(run_one(client, case_id(client, title), label, args.wait))
        r = runs[-1]
        print(f"{label}: {r['state']} planner={r['planner']} summary={r['summaryOrigin']} tools={r['toolCalls']} {r['wallSeconds']}s {r['fallbackReasons'] or ''}")
    model_ok = all(r["origin"] == "live" and r["planner"] == "model" and r["summaryOrigin"] == "model" for r in runs)
    states_ok = all(r["state"] in ("completed", "waiting_for_approval") for r in runs)
    report = {"generatedAt": datetime.now(UTC).isoformat(), "deployment": {k: health.get(k) for k in ("buildId", "migration", "features", "aiMode", "textModel", "appEnv")},
              "runs": runs, "allRunsSucceeded": states_ok, "modelUsedForPlannerAndSummaryInEveryRun": model_ok,
              "note": "Three smoke runs on fictional reports through the running deployment. Not a latency or reliability measurement."}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "live-runs.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out / 'live-runs.json'}; model used for planner and summary in every run: {model_ok}")
    return 0 if states_ok and (model_ok or not args.require_model) else 1


if __name__ == "__main__":
    sys.exit(main())
