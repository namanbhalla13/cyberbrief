"""Build `pack.json` and hand-authored fixture responses for the Demo A pack.

Labels here are authored by the fixture designer from the generator specifications (the images are
synthetic, so the ground truth is known by construction). They are never copied from model output.
Fixture responses imitate what a well-behaved extraction would return; S02 deliberately lacks a date
and account scope because the crop removed them.

Usage: uv run --project backend python scripts/fixtures/build_packs.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "evals" / "fixtures" / "development" / "demo-a"

TYPED_ACCOUNT = (
    "I saw an ad for Trendy Threads Store and ordered clothes on 12 October 2026. I paid them INR 25,000 "
    "in total through UPI in a few payments. Nothing was delivered and they stopped replying to my messages. "
    "The last payment was yesterday and still shows as pending in my app."
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payment_observation(amount_raw: str, reference: str, status_raw: str, date_raw: str | None, *,
                        payer: str | None = "citizen.demo@demobank", issues: list[str] | None = None) -> dict:
    return {
        "kind": "payment", "amountRaw": amount_raw, "currency": "INR", "referenceRaw": reference,
        "referenceType": "UPI Ref No", "statusRaw": status_raw, "directionRaw": "Paid to", "dateRaw": date_raw,
        "counterpartyRaw": "Trendy Threads Store (trendythreads.demo@demobank)", "accountScopeRaw": payer,
        "supportingQuote": f"{amount_raw} · UPI Ref No {reference}", "issues": issues or [],
    }


def envelope(source_type: str, visible_text: str, observations: list[dict], unresolved: list[str] | None = None) -> dict:
    return {"envelope": {"schemaVersion": "1", "sourceType": source_type, "language": "en", "visibleText": visible_text,
                         "observations": observations, "unresolvedFields": unresolved or []},
            "usage": {"note": "fixture replay; no tokens billed"}}


RECEIPT_TEXT = "DemoPay Demo build {status} Paid to Trendy Threads Store trendythreads.demo@demobank {amount} {date} UPI Ref No {ref} From citizen.demo@demobank Status {short} Fictional demo receipt. Not a real payment."

FIXTURES = {
    "S01": envelope("receipt", RECEIPT_TEXT.format(status="Payment Successful", amount="₹10,000.00", date="12 Oct 2026, 10:42 AM", ref="DEMO-A-001", short="Successful"),
                    [payment_observation("₹10,000.00", "DEMO-A-001", "Payment Successful", "12 Oct 2026, 10:42 AM")]),
    "S02": envelope("receipt", "₹10,000.00 UPI Ref No DEMO-A-001",
                    [payment_observation("₹10,000.00", "DEMO-A-001", None, None, payer=None, issues=["date_missing", "cropped_image", "status_not_visible"])],
                    ["transaction_date", "payment_status", "payer_account"]),
    "S03": envelope("receipt", RECEIPT_TEXT.format(status="Payment Successful", amount="₹10,000.00", date="12 Oct 2026, 11:05 AM", ref="DEMO-A-002", short="Successful"),
                    [payment_observation("₹10,000.00", "DEMO-A-002", "Payment Successful", "12 Oct 2026, 11:05 AM")]),
    "S04": envelope("receipt", RECEIPT_TEXT.format(status="Payment Pending", amount="₹5,000.00", date="13 Oct 2026, 09:15 AM", ref="DEMO-A-003", short="Pending"),
                    [payment_observation("₹5,000.00", "DEMO-A-003", "Payment Pending", "13 Oct 2026, 09:15 AM")]),
    "S05": envelope("typed_account", TYPED_ACCOUNT,
                    [{"kind": "claim", "amountRaw": "INR 25,000", "currency": "INR", "referenceRaw": None, "referenceType": None,
                      "statusRaw": None, "directionRaw": "paid them", "dateRaw": "12 October 2026", "counterpartyRaw": "Trendy Threads Store",
                      "accountScopeRaw": None, "supportingQuote": "I paid them INR 25,000 in total through UPI", "issues": []},
                     {"kind": "claim", "amountRaw": None, "currency": None, "referenceRaw": None, "referenceType": None,
                      "statusRaw": "pending", "directionRaw": "paid", "dateRaw": "yesterday", "counterpartyRaw": None,
                      "accountScopeRaw": None, "supportingQuote": "The last payment was yesterday and still shows as pending", "issues": ["relative_date", "amount_missing"]}]),
}

EXPECTED = {
    "S01": {"amountMinor": "1000000", "currency": "INR", "reference": "DEMO-A-001", "status": "successful", "direction": "debit", "date": "2026-10-12", "readable": {"amount": True, "reference": True, "status": True, "date": True}},
    "S02": {"amountMinor": "1000000", "currency": "INR", "reference": "DEMO-A-001", "status": None, "direction": "debit", "date": None, "readable": {"amount": True, "reference": True, "status": False, "date": False}},
    "S03": {"amountMinor": "1000000", "currency": "INR", "reference": "DEMO-A-002", "status": "successful", "direction": "debit", "date": "2026-10-12", "readable": {"amount": True, "reference": True, "status": True, "date": True}},
    "S04": {"amountMinor": "500000", "currency": "INR", "reference": "DEMO-A-003", "status": "pending", "direction": "debit", "date": "2026-10-13", "readable": {"amount": True, "reference": True, "status": True, "date": True}},
    "S05": {"citizenReportedAmountMinor": "2500000", "relativeDateUnresolved": True},
}


AUDIO_INDEX = REPO_ROOT / "evals" / "fixtures" / "audio" / "index.json"

# Hand-authored extraction responses for the *transcript text* of each audio clip. A faithful model
# keeps spoken number words as raw text; the server cannot parse words, so the amount stays
# unreadable until the citizen corrects it. That is the intended conservative behaviour.
TRANSCRIPT_RESPONSES = {
    "A05": lambda script: envelope("typed_account", script, [
        {"kind": "claim", "amountRaw": "twenty five thousand rupees", "currency": "INR", "referenceRaw": None, "referenceType": None,
         "statusRaw": None, "directionRaw": "paid them", "dateRaw": "the twelfth of October", "counterpartyRaw": "Trendy Threads Store",
         "accountScopeRaw": None, "supportingQuote": "I paid them twenty five thousand rupees in total through U P I", "issues": ["amount_in_words"]},
        {"kind": "claim", "amountRaw": None, "currency": None, "referenceRaw": None, "referenceType": None, "statusRaw": "pending",
         "directionRaw": "paid", "dateRaw": "yesterday", "counterpartyRaw": None, "accountScopeRaw": None,
         "supportingQuote": "The last payment was yesterday and still shows as pending", "issues": ["relative_date", "amount_missing"]}]),
    "B05": lambda script: envelope("typed_account", script, [
        {"kind": "claim", "amountRaw": "twenty thousand rupees", "currency": "INR", "referenceRaw": None, "referenceType": None,
         "statusRaw": None, "directionRaw": "paid", "dateRaw": "the fifteenth of October", "counterpartyRaw": "Gadget Galaxy Deals",
         "accountScopeRaw": None, "supportingQuote": "paid twenty thousand rupees in two payments", "issues": ["amount_in_words"]}]),
}


def audio_sources(pack_id: str, responses_dir: Path) -> list[dict]:
    """Audio clips (with authored transcripts) plus the transcript-text extraction responses, from gen_audio.py output."""
    if not AUDIO_INDEX.exists():
        return []
    sources: list[dict] = []
    for entry in json.loads(AUDIO_INDEX.read_text(encoding="utf-8")):
        if entry["pack"] != pack_id:
            continue
        if entry.get("provenance") == "pending_owner_recording":
            sources.append({"fixtureSourceId": entry["source"], "kind": "audio", "language": entry["language"], "file": None, "sha256": None,
                            "pendingOwnerRecording": True, "script": entry["script"], "note": entry["note"]})
            continue
        transcript = json.loads((AUDIO_INDEX.parent / entry["transcriptFile"]).read_text(encoding="utf-8"))
        script = transcript["transcript"]
        sources.append({"fixtureSourceId": entry["source"], "kind": "audio", "language": entry["language"], "file": f"../../audio/{entry['file']}",
                        "sha256": entry["sha256"], "mediaType": "audio/wav", "durationMs": entry["durationMs"], "provenance": entry["provenance"],
                        "fixtureTranscript": f"../../audio/{entry['transcriptFile']}"})
        text_id = f"{entry['source']}T"
        builder = TRANSCRIPT_RESPONSES.get(entry["source"])
        if builder is not None:
            (responses_dir / f"{text_id}.json").write_text(json.dumps(builder(script), indent=2, ensure_ascii=False), encoding="utf-8")
            sources.append({"fixtureSourceId": text_id, "kind": "transcript", "derivedFrom": entry["source"], "sha256": sha256_text(script),
                            "mediaType": "text/plain; charset=utf-8", "fixtureResponse": f"fixture_responses/{text_id}.json",
                            "expectedObservations": [{"amountInWords": True, "expectedCorrection": "INR 25,000" if pack_id == "demo-a" else "INR 20,000"}]})
    return sources


def main() -> int:
    images = {entry["source"]: entry for entry in json.loads((PACK_DIR / "images.json").read_text(encoding="utf-8"))}
    (PACK_DIR / "S05_typed_account.txt").write_text(TYPED_ACCOUNT, encoding="utf-8")
    responses_dir = PACK_DIR / "fixture_responses"
    responses_dir.mkdir(exist_ok=True)
    sources = []
    for sid in ("S01", "S02", "S03", "S04"):
        entry = images[sid]
        (responses_dir / f"{sid}.json").write_text(json.dumps(FIXTURES[sid], indent=2, ensure_ascii=False), encoding="utf-8")
        sources.append({"fixtureSourceId": sid, "file": entry["file"], "sha256": entry["sha256"], "kind": "image",
                        "sourceType": "receipt", "mediaType": entry["mediaType"], "imageSize": entry["imageSize"],
                        "injectedText": None, "expectedObservations": [EXPECTED[sid]],
                        "regionsFile": f"{sid}.regions.json", "fixtureResponse": f"fixture_responses/{sid}.json"})
    (responses_dir / "S05.json").write_text(json.dumps(FIXTURES["S05"], indent=2, ensure_ascii=False), encoding="utf-8")
    sources.append({"fixtureSourceId": "S05", "file": "S05_typed_account.txt", "sha256": sha256_text(TYPED_ACCOUNT), "kind": "typed_account",
                    "sourceType": "typed_account", "mediaType": "text/plain; charset=utf-8", "injectedText": None,
                    "expectedObservations": [EXPECTED["S05"]], "fixtureResponse": "fixture_responses/S05.json"})
    sources.extend(audio_sources("demo-a", responses_dir))

    pack = {
        "packId": "demo-a", "split": "development", "schemaVersion": "1",
        "title": "Demo A — duplicate receipt and pending payment",
        "labelProvenance": {"author": "owner (fixture designer)", "method": "authored from generator specifications; synthetic images; not model output",
                            "labelledAt": datetime.now(UTC).strftime("%Y-%m-%d")},
        "generator": {"script": "scripts/fixtures/gen_receipt.py", "layout": "variant_a", "seed": 1},
        "sources": sources,
        "approvedCanonical": {
            "payments": [
                {"label": "DEMO-A-001", "amountMinor": "1000000", "currency": "INR", "status": "successful", "direction": "debit", "sources": ["S01", "S02"]},
                {"label": "DEMO-A-002", "amountMinor": "1000000", "currency": "INR", "status": "successful", "direction": "debit", "sources": ["S03"]},
                {"label": "DEMO-A-003", "amountMinor": "500000", "currency": "INR", "status": "pending", "direction": "debit", "sources": ["S04"]},
            ],
            "duplicateCandidates": [["S01", "S02"]],
            "citizenReportedAmountMinor": "2500000",
            "expectedTotals": {"reviewedDebitMinor": "2000000", "pendingMinor": "500000", "outstandingMinor": "2000000"},
            "expectedQuestions": ["Has the INR 5,000 payment completed, failed, or remained pending?",
                                  "You reported INR 25,000, while the reviewed successful receipts total INR 20,000. Is there another payment?"],
        },
        "knownAmbiguities": ["S02 is a cropped re-save of S01: no date, status or payer visible; same reference.",
                             "S05 says 'yesterday' for the pending payment; the date must stay unresolved."],
        "updates": [],
    }
    (PACK_DIR / "pack.json").write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {PACK_DIR / 'pack.json'} with {len(sources)} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
