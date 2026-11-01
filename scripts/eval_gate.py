"""When an evaluation run counts as having measured anything.

An evaluation that could not reach the model, or scored nothing, must not exit cleanly: a green exit would read as a result.
Both evaluators report the reasons returned here and exit non-zero when there are any.
"""

from __future__ import annotations

from typing import Any

import httpx


def live_preflight(base_url: str, api_key: str) -> str | None:
    """None when the provider can be reached over a verified TLS connection; otherwise the exact reason, so a live run is never started blind.

    Certificate verification stays on. If a corporate or approved trust chain is needed, point SSL_CERT_FILE at its PEM bundle.
    """
    if not api_key.strip():
        return "MODEL_API_KEY is not set"
    try:
        httpx.get(base_url.rstrip("/") + "/models", timeout=12, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.ConnectError as exc:
        return (f"could not open a verified connection to {base_url}: {str(exc)[:200]}. Certificate checking is never turned off; "
                "set SSL_CERT_FILE to an approved CA bundle (PEM) that contains the full chain.")
    except httpx.HTTPError as exc:
        return f"could not reach {base_url}: {type(exc).__name__}"
    return None


TRANSPORT_ISSUES = ("http ", "model_error")


def assistant_gate(specs: list[dict], rows: list[dict], failures: list[dict], *, shown_problems: int) -> list[str]:
    reasons: list[str] = []
    attempted = sum(len(s["turns"]) for s in specs)
    errors = [f for f in failures if str(f.get("issue", "")).startswith(TRANSPORT_ISSUES)]
    scored = [r for r in rows if not r["unscripted"]]
    if errors:
        reasons.append(f"{len(errors)} of {attempted} turns failed before a reply could be scored (first: {errors[0]['issue']})")
    if not scored:
        reasons.append("no reply was successfully scored")
    missing = sorted({s["evalId"] for s in specs} - {r["eval"] for r in rows})
    if missing:
        reasons.append("no scored reply for: " + ", ".join(missing))
    if shown_problems:
        reasons.append(f"{shown_problems} shown reply(ies) broke a rule")
    return reasons


def extraction_gate(results: list[dict[str, Any]], expected_packs: int) -> list[str]:
    reasons: list[str] = []
    if len(results) < expected_packs or not results:
        reasons.append(f"only {len(results)} of {expected_packs} packs were run")
    for r in results:
        if r.get("runStatus") == "failed":
            reasons.append(f"{r['packId']}: the run failed")
        if r.get("sourcesFailed"):
            reasons.append(f"{r['packId']}: {len(r['sourcesFailed'])} source(s) failed")
    assessed = sum(v["correct"] + v["incorrect"] + v["abstained"] for r in results for v in r["perField"].values())
    if assessed == 0:
        reasons.append("no field was successfully scored")
    return reasons
