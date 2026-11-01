"""Evaluation runner: process a dataset split through the real pipeline and score it against ground truth.

Scores per critical field (amount, currency, reference, status, direction, date) with explicit
denominators: correct / incorrect / abstained over readable labelled fields, plus abstention on
fields labelled unreadable (ambiguity handling), duplicate-candidate precision/recall, false
merges, citation integrity, latency and usage. Results are written to evals/reports/<timestamp>/.

The run is labelled `fixture` or `live` from AI_MODE. A fixture run measures the deterministic
pipeline (parsing, mapping, provenance) against declared labels; it is never reported as live.

Usage (repo root):
    uv run --project backend python scripts/run_evaluation.py --split holdout --mode fixture
    uv run --project backend python scripts/run_evaluation.py --split holdout --mode live   # needs MODEL_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - git describe only
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

parser = argparse.ArgumentParser()
parser.add_argument("--split", choices=["development", "holdout"], default="holdout")
parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
parser.add_argument("--packs", default="", help="comma-separated pack ids (default: all in the split)")
parser.add_argument("--out", default="")
args = parser.parse_args()

STAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
OUT = Path(args.out) if args.out else REPO_ROOT / "evals" / "reports" / f"{args.mode}-{args.split}-{STAMP}"
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="cyberbrief-eval-"))
os.environ["AI_MODE"] = args.mode
os.environ["JOB_RUNNER"] = "inline"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{(WORK / 'eval.db').as_posix()}"
os.environ["STORAGE_ROOT"] = str(WORK / "private")
os.environ["CHECKPOINT_DB"] = str(WORK / "checkpoints.db")

from eval_gate import extraction_gate, live_preflight  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.ai import run_workflow  # noqa: E402
from app.casework import add_audio_source, add_image_source, add_text_source, create_case  # noqa: E402
from app.core import (  # noqa: E402
    get_settings,
    make_session,
)
from app.main import run_migrations  # noqa: E402
from app.models import Fact, PaymentObservation, ProcessingRun, Refund, Source  # noqa: E402
from app.records import (  # noqa: E402
    Storage,
    bump_revision,
)
from app.schemas import (  # noqa: E402
    PROMPT_VERSION,
    SCHEMA_VERSION,
    CaseCreate,
)
from app.security import new_session  # noqa: E402

FIELDS = ("amount", "currency", "reference", "status", "direction", "date")


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip() or "unknown"  # noqa: S603, S607
    except OSError:
        return "unknown"


def load_pack(db, session, settings, storage, pack_dir: Path) -> tuple[str, dict[str, str], dict]:
    pack = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    case = create_case(db, session, settings, storage, CaseCreate(title=f"eval {pack['packId']}", language="en"))
    revision = bump_revision(db, case, kind="sources_added", summary="evaluation sources", actor_session_id=session.id, actor_role="system")
    sha_to_fixture: dict[str, str] = {}
    for src in pack["sources"]:
        kind, file = src.get("kind"), src.get("file")
        if not file or src.get("pendingOwnerRecording") or kind == "transcript":
            continue
        path = (pack_dir / file).resolve()
        if kind in ("image", "supplied_official_update"):
            source = add_image_source(db, storage, case, path.read_bytes(), None, path.name, revision=revision, settings=settings)
            source.kind = kind
        elif kind == "typed_account":
            source = add_text_source(db, storage, case, path.read_text(encoding="utf-8"), kind="typed_account", display_filename=src["fixtureSourceId"], revision=revision, settings=settings)
        elif kind == "audio":
            source = add_audio_source(db, storage, case, path.read_bytes(), path.name, revision=revision, settings=settings)
        else:
            continue
        sha_to_fixture.setdefault(source.sha256, src["fixtureSourceId"])
    db.commit()
    return case.id, sha_to_fixture, pack


def run_case(db, case_id: str, settings) -> tuple[ProcessingRun, float]:
    sources = db.scalars(select(Source).where(Source.case_id == case_id, Source.status == "stored")).all()
    run = ProcessingRun(case_id=case_id, status="queued", stage="queued", base_revision=1, input_source_ids=[s.id for s in sources],
                        input_hashes=[s.sha256 for s in sources], ai_mode=settings.ai_mode, provider="meta" if settings.ai_mode == "live" else "fixture",
                        model_id=settings.meta_text_model if settings.ai_mode == "live" else "fixture",
                        prompt_version=PROMPT_VERSION if settings.ai_mode == "live" else "fixture", schema_version=SCHEMA_VERSION)
    db.add(run)
    db.commit()
    run_id = run.id
    started = time.perf_counter()
    run_workflow(run_id)
    elapsed = time.perf_counter() - started
    db.expire_all()
    return db.get(ProcessingRun, run_id), elapsed


def fact_value(facts: dict[str, Fact], kind: str):
    fact = facts.get(kind)
    if fact is None or not fact.value:
        return None
    if kind == "amount":
        return fact.value.get("amountMinor")
    if kind == "currency":
        return fact.value.get("code")
    if kind == "occurred_at":
        return fact.value.get("date") or (fact.value.get("instantUtc") or fact.value.get("localDatetime") or "")[:10] or None
    if kind == "reference":
        return fact.value.get("value")
    return fact.value.get("value")


def score_pack(db, case_id: str, sha_to_fixture: dict[str, str], pack: dict, run: ProcessingRun) -> dict:
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.case_id == case_id)).all()}
    facts_by_subject: dict[str, dict[str, Fact]] = {}
    for fact in db.scalars(select(Fact).where(Fact.case_id == case_id, Fact.is_current.is_(True))).all():
        facts_by_subject.setdefault(fact.subject_id, {})[fact.kind] = fact
    # subject (payment/refund) per source id
    subject_by_source: dict[str, str] = {}
    for link in db.scalars(select(PaymentObservation).where(PaymentObservation.case_id == case_id)).all():
        subject_by_source[link.source_id] = link.payment_id
    for refund in db.scalars(select(Refund).where(Refund.case_id == case_id)).all():
        for fact in facts_by_subject.get(refund.id, {}).values():
            for anchor in fact.anchors or []:
                if anchor.get("sourceId"):
                    subject_by_source.setdefault(anchor["sourceId"], refund.id)
    truth_by_fixture = {s["fixtureSourceId"]: s for s in pack["sources"] if s.get("expectedObservations") and s.get("kind") in ("image",)}

    per_field = {f: {"correct": 0, "incorrect": 0, "abstained": 0, "unreadableAbstained": 0, "unreadableGuessed": 0} for f in FIELDS}
    failures: list[dict] = []
    for source_id, source in sources.items():
        fixture_id = sha_to_fixture.get(source.sha256)
        truth_src = truth_by_fixture.get(fixture_id or "")
        if not truth_src:
            continue
        truth = truth_src["expectedObservations"][0]
        subject = subject_by_source.get(source_id)
        facts = facts_by_subject.get(subject or "", {})
        proposed = {"amount": fact_value(facts, "amount"), "currency": fact_value(facts, "currency"), "reference": fact_value(facts, "reference"),
                    "status": fact_value(facts, "status"), "direction": fact_value(facts, "direction"), "date": fact_value(facts, "occurred_at")}
        expected = {"amount": truth.get("amountMinor"), "currency": truth.get("currency"), "reference": truth.get("reference"),
                    "status": truth.get("status"), "direction": truth.get("direction"), "date": truth.get("date")}
        readable = truth.get("readable", {})
        for field in FIELDS:
            is_readable = readable.get(field, True)
            got, want = proposed[field], expected[field]
            if field == "reference" and truth.get("referenceTruncatedInImage"):
                is_readable = False
                ref_fact = facts.get("reference")
                if ref_fact is not None and ref_fact.value and ref_fact.value.get("truncated"):
                    got = None  # a partial identifier kept raw and flagged truncated is a correct abstention, not a guess
            if not is_readable:
                if got is None:
                    per_field[field]["unreadableAbstained"] += 1
                else:
                    per_field[field]["unreadableGuessed"] += 1
                    failures.append({"pack": pack["packId"], "source": fixture_id, "field": field, "issue": "guessed_unreadable", "got": got})
                continue
            if got is None:
                per_field[field]["abstained"] += 1
                failures.append({"pack": pack["packId"], "source": fixture_id, "field": field, "issue": "abstained", "want": want})
            elif str(got) == str(want):
                per_field[field]["correct"] += 1
            else:
                per_field[field]["incorrect"] += 1
                failures.append({"pack": pack["packId"], "source": fixture_id, "field": field, "issue": "incorrect", "got": got, "want": want})

    # duplicate candidates: expected pairs (by fixture ids) vs found (payments sharing a reference)
    from app.accounting import find_duplicate_candidates
    from app.casework import payment_views
    from app.models import Case

    case_row = db.get(Case, case_id)
    payments, views, _, links = payment_views(db, case_row)
    found_pairs = set()
    for cand in find_duplicate_candidates(views):
        ids = []
        for pid in cand.payment_ids:
            src_ids = [lk.source_id for lk in links.get(pid, [])]
            ids.append(sha_to_fixture.get(sources[src_ids[0]].sha256, "?") if src_ids else "?")
        found_pairs.add(tuple(sorted(ids)))
    expected_pairs = {tuple(sorted(p)) for p in pack.get("approvedCanonical", {}).get("duplicateCandidates", [])}
    # byte duplicates reuse one fixture id twice; count them as a found pair with themselves
    false_merges = sum(1 for p in payments if len({sha_to_fixture.get(sources[lk.source_id].sha256) for lk in links.get(p.id, [])}) > 1)
    citation_ok = all(any(a.get("sourceId") in sources for a in (f.anchors or [])) for facts in facts_by_subject.values() for f in facts.values())
    per_source = run.per_source_status or {}
    latencies = [v.get("latencyMs") for v in per_source.values() if isinstance(v.get("latencyMs"), int | float)]
    return {"packId": pack["packId"], "runStatus": run.status, "aiMode": run.ai_mode, "model": run.model_id, "perField": per_field, "failures": failures,
            "duplicateCandidates": {"expected": sorted(expected_pairs), "found": sorted(found_pairs)}, "falseMerges": false_merges,
            "citationIntegrity": citation_ok, "latenciesMs": latencies, "usage": run.usage or {},
            "sourcesFailed": [sid for sid, v in per_source.items() if v.get("status") == "failed"]}


def main() -> int:
    settings = get_settings()
    if args.mode == "live" and not settings.model_api_key.get_secret_value().strip():
        print("BLOCKED: live evaluation requires MODEL_API_KEY in Cyberbrief/.env")
        return 2
    if args.mode == "live":
        blocked = live_preflight(settings.meta_base_url, settings.model_api_key.get_secret_value())
        if blocked:
            print("BLOCKED: " + blocked)
            return 2
    run_migrations()
    storage = Storage(settings)
    split_dir = REPO_ROOT / "evals" / "fixtures" / args.split
    wanted = {p for p in args.packs.split(",") if p}
    pack_dirs = sorted(d for d in split_dir.iterdir() if (d / "pack.json").exists() and (not wanted or d.name in wanted))
    results = []
    with make_session() as db:
        session = new_session(db, settings, label=f"eval-{STAMP}", role="citizen")
        db.commit()
        for pack_dir in pack_dirs:
            case_id, sha_map, pack = load_pack(db, session, settings, storage, pack_dir)
            run, elapsed = run_case(db, case_id, settings)
            scored = score_pack(db, case_id, sha_map, pack, run)
            scored["wallSeconds"] = round(elapsed, 2)
            results.append(scored)
            print(f"{pack['packId']}: run {run.status} · {sum(v['correct'] for v in scored['perField'].values())} correct fields · "
                  f"{len(scored['failures'])} failure(s) · {elapsed:.1f}s")

    totals = {f: {k: sum(r["perField"][f][k] for r in results) for k in ("correct", "incorrect", "abstained", "unreadableAbstained", "unreadableGuessed")} for f in FIELDS}
    all_lat = sorted(x for r in results for x in r["latenciesMs"])
    p50 = all_lat[len(all_lat) // 2] if all_lat else None
    p95 = all_lat[int(len(all_lat) * 0.95) - 1] if len(all_lat) >= 2 else (all_lat[0] if all_lat else None)
    report = {"runDate": STAMP, "commit": git_commit(), "split": args.split, "mode": args.mode, "modelIds": {"text": settings.meta_text_model if args.mode == "live" else "fixture",
              "speech": settings.meta_speech_model if args.mode == "live" else "fixture"}, "promptVersion": PROMPT_VERSION if args.mode == "live" else "fixture",
              "schemaVersion": SCHEMA_VERSION, "packs": results, "fieldTotals": totals, "latencyMs": {"p50": p50, "p95": p95, "n": len(all_lat)},
              "usage": {k: sum((r["usage"].get(k) or 0) for r in results if isinstance(r["usage"].get(k), int | float)) for k in {k for r in results for k in r["usage"]}},
              "falseMerges": sum(r["falseMerges"] for r in results), "citationIntegrity": all(r["citationIntegrity"] for r in results),
              "failures": [f for r in results for f in r["failures"]]}
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [f"## Evaluation run {STAMP} — {args.mode} · {args.split}", "", "```text", f"Run date: {STAMP}", f"Application commit: {report['commit']}",
             f"Dataset version and split: evals/fixtures/{args.split} ({len(results)} packs)", f"Model IDs: {json.dumps(report['modelIds'])}",
             f"Prompt/schema versions: {report['promptVersion']} / {SCHEMA_VERSION}", f"Live or fixture: {args.mode.upper()}"]
    assessed = sum(v["correct"] + v["incorrect"] + v["abstained"] for v in totals.values())
    lines.append(f"Cases and critical fields assessed: {len(results)} cases, {assessed} readable labelled fields")
    lines.append("Exact accuracy by field:")
    for f, v in totals.items():
        denom = v["correct"] + v["incorrect"] + v["abstained"]
        acc = f"{100 * v['correct'] / denom:.0f}% ({v['correct']}/{denom})" if denom else "n/a"
        lines.append(f"  {f:10s} {acc} · incorrect {v['incorrect']} · abstained {v['abstained']} · unreadable: abstained {v['unreadableAbstained']}, guessed {v['unreadableGuessed']}")
    lines += [f"False merges: {report['falseMerges']}", f"Unsupported critical details: {'0 (every current fact has a same-case anchor)' if report['citationIntegrity'] else 'see failures'}",
              "Median preparation/review time: not measured (no usability study yet)",
              f"Median/p95 latency and sample size: {p50} ms / {p95} ms over {len(all_lat)} source extractions ({'fixture replay, not provider latency' if args.mode == 'fixture' else 'live provider'})",
              f"Actual billed usage: {json.dumps(report['usage']) if args.mode == 'live' else 'none (fixture replay)'}",
              f"Known failures: {len(report['failures'])} (listed in {OUT.relative_to(REPO_ROOT).as_posix()}/report.json)", "```", ""]
    for fail in report["failures"][:25]:
        lines.append(f"- {fail['pack']} {fail['source']} {fail['field']}: {fail['issue']}" + (f" got {fail.get('got')!r} want {fail.get('want')!r}" if "want" in fail or "got" in fail else ""))
    summary = "\n".join(lines)
    (OUT / "summary.md").write_text(summary, encoding="utf-8")
    reasons = extraction_gate(results, len(pack_dirs))
    if reasons:
        print("NOT A VALID RESULT: " + "; ".join(reasons))
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
