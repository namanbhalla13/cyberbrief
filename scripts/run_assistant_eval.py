"""Assistant evaluation: run labelled conversations through the real chat routes and score the replies.

Each conversation is a list of citizen turns with expectations (the suggestion the rules should make,
whether a turn is off topic, which help entries must appear, whether a status card is due, the reply
language). Every reply that reaches the screen is also re-checked for promises, claims of action,
role claims, legal advice, outside referrals, phone numbers and links; the target for that count is 0.

The run is labelled `fixture` or `live` from AI_MODE. In fixture mode only the scripted conversations
can be answered, so the others are reported as unscripted and not scored: a fixture run measures the
plumbing, never the model.

Usage (repo root):
    uv run --project backend python scripts/run_assistant_eval.py --split development --mode fixture
    uv run --project backend python scripts/run_assistant_eval.py --split holdout --mode live   # needs MODEL_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

parser = argparse.ArgumentParser()
parser.add_argument("--split", choices=["development", "holdout"], default="development")
parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
parser.add_argument("--only", default="", help="comma-separated eval ids (default: all in the split)")
parser.add_argument("--out", default="")
args = parser.parse_args()

STAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
OUT = Path(args.out) if args.out else REPO_ROOT / "evals" / "reports" / f"assistant-{args.mode}-{args.split}-{STAMP}"
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="cyberbrief-assistant-eval-"))
os.environ["AI_MODE"] = args.mode
os.environ["JOB_RUNNER"] = "inline"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{(WORK / 'eval.db').as_posix()}"
os.environ["STORAGE_ROOT"] = str(WORK / "private")
os.environ["CHECKPOINT_DB"] = str(WORK / "checkpoints.db")

from eval_gate import assistant_gate, live_preflight  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.assistant import Grounding, check_reply  # noqa: E402
from app.main import create_app  # noqa: E402

EVAL_DIR = REPO_ROOT / "evals" / "fixtures" / "assistant" / "eval" / args.split
SHOWN_RULES = {"promise", "action_claim", "role_claim", "legal_advice", "outside_referral", "phone_number", "link"}


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("cb_csrf", "")}


def _sign_in(client: TestClient) -> None:
    client.post("/api/session")
    mobile = f"96{uuid.uuid4().int % 10**8:08d}"
    code = client.post("/api/auth/otp/request", headers=_csrf(client), json={"mobile": mobile}).json()["demoCode"]
    assert client.post("/api/auth/otp/verify", headers=_csrf(client), json={"mobile": mobile, "code": code}).status_code == 200


def _shown(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.as_posix()


def _ratio(hit: int, total: int) -> str:
    return f"{hit}/{total}" + (f" ({hit / total:.0%})" if total else "")


def run_conversation(client: TestClient, spec: dict) -> tuple[list[dict], list[dict]]:
    _sign_in(client)
    case_id = None
    if spec.get("withComplaint"):
        case_id = client.post("/api/cases", headers=_csrf(client), json={"title": spec.get("complaintTitle", "Evaluation complaint")}).json()["case"]["id"]
    started = client.post("/api/assistant/conversations", headers=_csrf(client), json={"caseId": case_id} if case_id else {}).json()
    cid = started["id"]
    rows: list[dict] = []
    failures: list[dict] = []
    last_draft = None
    for number, turn in enumerate(spec["turns"]):
        if turn.get("requiresCase") and case_id is None:
            draft = last_draft or {"title": "Evaluation complaint", "account": "\n".join(t["citizen"] for t in spec["turns"][:number])}
            case_id = client.post("/api/cases", headers=_csrf(client),
                                  json={"title": draft["title"], "incidentAccount": draft["account"]}).json()["case"]["id"]
            client.post(f"/api/assistant/conversations/{cid}/link", headers=_csrf(client), json={"caseId": case_id})
        said = client.post(f"/api/assistant/conversations/{cid}/messages", headers=_csrf(client), json={"text": turn["citizen"], "chip": False})
        if said.status_code != 200:
            failures.append({"eval": spec["evalId"], "turn": number, "issue": f"http {said.status_code}", "got": said.text[:300]})
            continue
        reply = said.json()["messages"][-1]
        meta = reply.get("meta", {})
        if meta.get("error"):
            # the model was not reached or its reply was unusable: a failure, never a clean scored reply
            failures.append({"eval": spec["evalId"], "turn": number, "issue": f"model_error {meta['error']}", "got": reply["text"]})
            continue
        blocks = reply.get("blocks", [])
        suggestion = next((b["kind"] for b in blocks if b["type"] == "suggestion"), None)
        draft = next((b.get("draft") for b in blocks if b["type"] == "suggestion"), None)
        last_draft = draft or last_draft
        citizen_text = "\n".join(t["citizen"] for t in spec["turns"][: number + 1])
        shown = sorted(set(check_reply(reply["text"], Grounding(text=citizen_text.casefold()))) & SHOWN_RULES)
        row = {
            "eval": spec["evalId"], "turn": number, "unscripted": bool(meta.get("unscripted")), "text": reply["text"],
            "topic": meta.get("topic"), "language": meta.get("language"), "checks": meta.get("checks"),
            "firstPassProblems": meta.get("problems", []), "repaired": bool(meta.get("repaired")), "fallback": bool(meta.get("fallback")),
            "suggestion": suggestion, "help": [b["id"] for b in blocks if b["type"] == "help"],
            "status": any(b["type"] == "status" for b in blocks), "shownProblems": shown,
            "latencyMs": meta.get("latencyMs"), "calls": meta.get("calls", 0), "usage": meta.get("usage", {}),
            "expect": turn.get("expect", {}),
        }
        rows.append(row)
        if shown:
            failures.append({"eval": spec["evalId"], "turn": number, "issue": "shown_problem", "got": shown, "text": reply["text"]})
    return rows, failures


def score(rows: list[dict], failures: list[dict]) -> dict:
    scored = [r for r in rows if not r["unscripted"]]
    metrics: dict[str, dict] = {}

    def tally(name: str, applies, correct) -> None:
        subset = [r for r in scored if applies(r)]
        hits = [r for r in subset if correct(r)]
        metrics[name] = {"correct": len(hits), "total": len(subset)}
        for r in subset:
            if not correct(r):
                failures.append({"eval": r["eval"], "turn": r["turn"], "issue": name, "got": {k: r[k] for k in ("suggestion", "topic", "help", "status", "language")},
                                 "want": r["expect"]})

    tally("suggestion", lambda r: "suggestion" in r["expect"], lambda r: r["suggestion"] == r["expect"]["suggestion"])
    tally("off_topic", lambda r: "offTopic" in r["expect"], lambda r: (r["topic"] == "off_topic") == r["expect"]["offTopic"])
    tally("help", lambda r: "help" in r["expect"], lambda r: set(r["expect"]["help"]) <= set(r["help"]))
    tally("status_card", lambda r: "status" in r["expect"], lambda r: r["status"] == r["expect"]["status"])
    tally("language", lambda r: "language" in r["expect"], lambda r: r["language"] == r["expect"]["language"])
    on_topic = [r for r in scored if r["topic"] == "on_topic"]
    latencies = [r["latencyMs"] for r in scored if isinstance(r["latencyMs"], int)]
    tokens = {key: sum(int(r["usage"].get(key, 0) or 0) for r in scored if isinstance(r["usage"], dict))
              for key in ("prompt_tokens", "completion_tokens")}
    return {
        "replies": len(rows), "scored": len(scored), "unscripted": len(rows) - len(scored),
        "shownProblems": sum(1 for r in scored if r["shownProblems"]),
        "firstPassProblems": sum(1 for r in on_topic if r["firstPassProblems"]), "onTopic": len(on_topic),
        "repaired": sum(1 for r in scored if r["repaired"]), "fallback": sum(1 for r in scored if r["fallback"]),
        "metrics": metrics, "calls": sum(int(r["calls"] or 0) for r in scored), "tokens": tokens,
        "latencyMedianMs": statistics.median(latencies) if latencies else None,
        "latencyP95Ms": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
    }


def main() -> int:
    if args.mode == "live":
        from app.core import get_settings

        settings = get_settings()
        blocked = live_preflight(settings.meta_base_url, settings.model_api_key.get_secret_value())
        if blocked:
            print("BLOCKED: " + blocked)
            return 2
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    specs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(EVAL_DIR.glob("*.json"))]
    specs = [s for s in specs if not only or s["evalId"] in only]
    if not specs:
        print(f"no conversations in {EVAL_DIR}")
        return 2
    rows: list[dict] = []
    failures: list[dict] = []
    with TestClient(create_app()) as client:
        for spec in specs:
            got_rows, got_failures = run_conversation(client, spec)
            rows += got_rows
            failures += got_failures
    result = score(rows, failures)
    report = {"mode": args.mode, "split": args.split, "stamp": STAMP, "conversations": len(specs), **result, "failures": failures, "rows": rows}
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    m = result["metrics"]
    lines = [f"### Assistant evaluation — {args.mode} — {args.split} — {STAMP}", "",
             "Fixture mode replays scripted replies only; it measures the plumbing, not the model." if args.mode == "fixture" else
             "Live mode: real model calls; figures below are model behaviour after CyberBrief's checks.", "",
             "```",
             f"Conversations: {len(specs)} · replies: {result['replies']} · scored: {result['scored']} · unscripted (not scored): {result['unscripted']}",
             f"Shown replies breaking a rule (target 0): {result['shownProblems']}",
             f"On-topic replies the model wrote with a problem before checks: {_ratio(result['firstPassProblems'], result['onTopic'])}",
             f"Repaired: {result['repaired']} · replaced by a fixed reply: {result['fallback']}",
             f"Suggestion: {_ratio(m['suggestion']['correct'], m['suggestion']['total'])} · Off topic: {_ratio(m['off_topic']['correct'], m['off_topic']['total'])}"
             f" · Help entries: {_ratio(m['help']['correct'], m['help']['total'])} · Status card: {_ratio(m['status_card']['correct'], m['status_card']['total'])}"
             f" · Language: {_ratio(m['language']['correct'], m['language']['total'])}",
             f"Model calls: {result['calls']} · tokens in/out: {result['tokens']['prompt_tokens']}/{result['tokens']['completion_tokens']}"
             f" · latency median/p95 ms: {result['latencyMedianMs']}/{result['latencyP95Ms']}",
             f"Failures listed: {len(failures)} (see {_shown(OUT)}/report.json)",
             "```", ""]
    reasons = assistant_gate(specs, rows, failures, shown_problems=result["shownProblems"])
    lines += [f"Attempted turns: {sum(len(s['turns']) for s in specs)} · scored replies: {result['scored']}"]
    if reasons:
        lines += ["NOT A VALID RESULT: " + "; ".join(reasons)]
    summary = "\n".join(lines)
    (OUT / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
