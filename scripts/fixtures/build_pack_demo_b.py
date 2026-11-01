"""Build `pack.json` and hand-authored fixture responses for Demo B (recovery lifecycle).

Run after gen_receipt.py and gen_official_update.py. Labels are authored from the generator
specifications (synthetic images); never copied from model output.

Usage: uv run --project backend python scripts/fixtures/build_pack_demo_b.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "evals" / "fixtures" / "development" / "demo-b"

TYPED_ACCOUNT = (
    "I ordered a phone from Gadget Galaxy Deals on 15 October 2026 after a social media ad and paid INR 20,000 "
    "in two UPI payments of INR 10,000 each. The phone never arrived and the seller blocked me."
)
REFUND_UPDATE = "Update: I received INR 5,000 back from the seller on 20 October 2026."


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def receipt(amount_raw: str, reference: str, status_raw: str, direction_raw: str, date_raw: str, kind: str = "payment") -> dict:
    return {"kind": kind, "amountRaw": amount_raw, "currency": "INR", "referenceRaw": reference, "referenceType": "UPI Ref No",
            "statusRaw": status_raw, "directionRaw": direction_raw, "dateRaw": date_raw,
            "counterpartyRaw": "Gadget Galaxy Deals (gadgetgalaxy.demo@demobank)", "accountScopeRaw": "citizen.demo@demobank",
            "supportingQuote": f"{amount_raw} · UPI Ref No {reference}", "issues": []}


def envelope(source_type: str, visible_text: str, observations: list[dict], unresolved: list[str] | None = None) -> dict:
    return {"envelope": {"schemaVersion": "1", "sourceType": source_type, "language": "en", "visibleText": visible_text,
                         "observations": observations, "unresolvedFields": unresolved or []},
            "usage": {"note": "fixture replay; no tokens billed"}}


RECEIPT_TEXT = "DemoPay Demo build {status} {dir} Gadget Galaxy Deals gadgetgalaxy.demo@demobank {amount} {date} UPI Ref No {ref} {scope} citizen.demo@demobank Status {short}"
STATUS_TEXT = ("Your complaint has been acknowledged and forwarded to the concerned bank for necessary action. "
               "You will be informed of further developments.")

FIXTURES = {
    "S01": envelope("receipt", RECEIPT_TEXT.format(status="Payment Successful", dir="Paid to", amount="₹10,000.00", date="15 Oct 2026, 06:12 PM", ref="DEMO-B-001", scope="From", short="Successful"),
                    [receipt("₹10,000.00", "DEMO-B-001", "Payment Successful", "Paid to", "15 Oct 2026, 06:12 PM")]),
    "S02": envelope("receipt", RECEIPT_TEXT.format(status="Payment Successful", dir="Paid to", amount="₹10,000.00", date="15 Oct 2026, 06:20 PM", ref="DEMO-B-002", scope="From", short="Successful"),
                    [receipt("₹10,000.00", "DEMO-B-002", "Payment Successful", "Paid to", "15 Oct 2026, 06:20 PM")]),
    "R1": envelope("receipt", RECEIPT_TEXT.format(status="Payment Received", dir="Received from", amount="₹5,000.00", date="20 Oct 2026, 11:03 AM", ref="REF-B-RF1", scope="To", short="Received"),
                   [receipt("₹5,000.00", "REF-B-RF1", "Payment Received", "Received from", "20 Oct 2026, 11:03 AM", kind="refund_credit")]),
    "R2": envelope("receipt", RECEIPT_TEXT.format(status="Payment Received", dir="Received from", amount="₹15,000.00", date="24 Oct 2026, 04:45 PM", ref="REF-B-RF2", scope="To", short="Received"),
                   [receipt("₹15,000.00", "REF-B-RF2", "Payment Received", "Received from", "24 Oct 2026, 04:45 PM", kind="refund_credit")]),
    "O1": envelope("official_communication",
                   f"Demo Cyber Helpdesk (fictional) Complaint acknowledgement Complaint reference: DEMO-B-ACK-7781 Date: 20 Oct 2026 Dear complainant, {STATUS_TEXT} This communication is fictional demo material.",
                   [{"kind": "status_message", "amountRaw": None, "currency": None, "referenceRaw": "DEMO-B-ACK-7781", "referenceType": "complaint reference",
                     "statusRaw": STATUS_TEXT, "directionRaw": None, "dateRaw": "20 Oct 2026", "counterpartyRaw": "Demo Cyber Helpdesk (fictional)",
                     "accountScopeRaw": None, "supportingQuote": STATUS_TEXT[:80], "issues": []}]),
    "S05": envelope("typed_account", TYPED_ACCOUNT,
                    [{"kind": "claim", "amountRaw": "INR 20,000", "currency": "INR", "referenceRaw": None, "referenceType": None, "statusRaw": None,
                      "directionRaw": "paid", "dateRaw": "15 October 2026", "counterpartyRaw": "Gadget Galaxy Deals", "accountScopeRaw": None,
                      "supportingQuote": "paid INR 20,000 in two UPI payments", "issues": []}]),
    "U1": envelope("typed_account", REFUND_UPDATE,
                   [{"kind": "claim", "amountRaw": "INR 5,000", "currency": "INR", "referenceRaw": None, "referenceType": None, "statusRaw": None,
                     "directionRaw": "received back", "dateRaw": "20 October 2026", "counterpartyRaw": "the seller", "accountScopeRaw": None,
                     "supportingQuote": "I received INR 5,000 back from the seller", "issues": []}]),
}


def main() -> int:
    images = {entry["source"]: entry for entry in json.loads((PACK_DIR / "images.json").read_text(encoding="utf-8"))}
    (PACK_DIR / "S05_typed_account.txt").write_text(TYPED_ACCOUNT, encoding="utf-8")
    (PACK_DIR / "U1_refund_update.txt").write_text(REFUND_UPDATE, encoding="utf-8")
    responses = PACK_DIR / "fixture_responses"
    responses.mkdir(exist_ok=True)
    sources = []
    for sid in ("S01", "S02", "R1", "R2", "O1"):
        entry = images[sid]
        (responses / f"{sid}.json").write_text(json.dumps(FIXTURES[sid], indent=2, ensure_ascii=False), encoding="utf-8")
        sources.append({"fixtureSourceId": sid, "file": entry["file"], "sha256": entry["sha256"],
                        "kind": "supplied_official_update" if sid == "O1" else "image",
                        "sourceType": "official_communication" if sid == "O1" else "receipt", "mediaType": entry["mediaType"],
                        "imageSize": entry["imageSize"], "injectedText": None, "regionsFile": f"{sid}.regions.json",
                        "fixtureResponse": f"fixture_responses/{sid}.json"})
    for sid, text in (("S05", TYPED_ACCOUNT), ("U1", REFUND_UPDATE)):
        (responses / f"{sid}.json").write_text(json.dumps(FIXTURES[sid], indent=2, ensure_ascii=False), encoding="utf-8")
        sources.append({"fixtureSourceId": sid, "file": "S05_typed_account.txt" if sid == "S05" else "U1_refund_update.txt",
                        "sha256": sha256_text(text), "kind": "typed_account", "sourceType": "typed_account",
                        "mediaType": "text/plain; charset=utf-8", "injectedText": None, "fixtureResponse": f"fixture_responses/{sid}.json"})
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_packs import audio_sources  # noqa: E402

    sources.extend(audio_sources("demo-b", responses))
    pack = {
        "packId": "demo-b", "split": "development", "schemaVersion": "1",
        "title": "Demo B — recovery lifecycle (partial then full refund, separate official status)",
        "labelProvenance": {"author": "owner (fixture designer)", "method": "authored from generator specifications; synthetic images; not model output",
                            "labelledAt": datetime.now(UTC).strftime("%Y-%m-%d")},
        "generator": {"scripts": ["scripts/fixtures/gen_receipt.py", "scripts/fixtures/gen_official_update.py"], "layout": "variant_a", "seed": 1},
        "sources": sources,
        "approvedCanonical": {
            "payments": [{"label": "DEMO-B-001", "amountMinor": "1000000", "status": "successful", "direction": "debit", "sources": ["S01"]},
                         {"label": "DEMO-B-002", "amountMinor": "1000000", "status": "successful", "direction": "debit", "sources": ["S02"]}],
            "refunds": [{"label": "REF-B-RF1", "amountMinor": "500000", "sources": ["R1"], "allocations": [["DEMO-B-001", "500000"]]},
                        {"label": "REF-B-RF2", "amountMinor": "1500000", "sources": ["R2"], "allocations": [["DEMO-B-001", "500000"], ["DEMO-B-002", "1000000"]]}],
            "officialStatus": [{"source": "O1", "category": "forwarded", "effectiveDate": "2026-10-20", "reference": "DEMO-B-ACK-7781"}],
            "citizenReportedAmountMinor": "2000000",
            "expectedTotals": {"reviewedDebitMinor": "2000000", "pendingMinor": "0", "matchedRefundMinorFinal": "2000000", "outstandingMinorFinal": "0"},
        },
        "updates": [
            {"step": 1, "sources": ["S01", "S02"], "expectedRecoveryState": "no_refund_reported", "expectedOutstandingMinor": "2000000"},
            {"step": 2, "sources": ["U1"], "expectedRecoveryState": "refund_reported_awaiting_review", "expectedOutstandingMinor": "2000000"},
            {"step": 3, "sources": ["R1"], "expectedRecoveryState": "partial_recovery_reviewed", "expectedOutstandingMinor": "1500000"},
            {"step": 5, "sources": ["R2"], "expectedRecoveryState": "reviewed_baseline_fully_offset", "expectedOutstandingMinor": "0"},
            {"step": 8, "sources": ["O1"], "expectedOfficialCategory": "forwarded", "expectedOfficialEffective": "2026-10-20"},
        ],
        "knownAmbiguities": ["Refund references differ from the debit references by design; matching is an explicit reviewed allocation."],
    }
    (PACK_DIR / "pack.json").write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {PACK_DIR / 'pack.json'} with {len(sources)} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
