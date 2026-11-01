# Fictional fixture dataset

Everything here is synthetic. Names, merchants, VPAs, references (`DEMO-*`, `HO*-*`), amounts and
"official" letters are invented; the issuer is always "Demo Cyber Helpdesk (fictional)". No real
brand marks are used (layouts are called DemoPay, PayLedger and QuickUPI).

## Layout

```
development/   10 packs used for development and the public demo (demo-a, demo-b, dev-03 … dev-10)
holdout/       10 packs kept for the evaluation (ho-01 … ho-10)
audio/         WAV clips (16 kHz mono 16-bit PCM) with authored transcripts; index.json
malformed/     unsafe/invalid uploads for security tests
catalog.json   one line per pack with tags and expected totals
```

Each pack directory holds the original files, `<name>.regions.json` sidecars (pixel boxes of every
field, written by the generator), `fixture_responses/` (what a well-behaved extractor returns, with
declared imperfections), and `pack.json`:

- `sources[]` — file, SHA-256, kind, `expectedObservations` (ground truth), `declaredImperfections`,
  `fixtureResponse` (image/text) or `fixtureTranscript` (audio);
- `approvedCanonical` — distinct payments, refunds, duplicate candidates, exclusions, expected totals;
- `updates[]` — expected recovery / official-status outcomes after later steps;
- `labelProvenance` — how the labels were produced.

## Provenance of labels

Labels are **derived from the generator specifications**: every image is rendered from a spec, so
the truth is known by construction. They are never copied from model output. Fixture responses
imitate a faithful extractor; where a field is deliberately unreadable (blur, crop, truncation) the
response says so (`amountRaw: null`, `issues: ["reference_truncated"]`), which is the behaviour the
app must handle conservatively.

## Coverage (handoff §14)

Varied layouts (3), crops (`bottom_cut`, `amount_ref_only`, `ref_truncated`), degradation (blur,
downscale, JPEG), Devanagari digits, byte-identical duplicates (dev-04), cropped duplicates
(demo-a, ho-02), missing identifiers (dev-07), contradictory statuses (ho-02), amount conflicts
(ho-06), false refund claims (dev-10, ho-04), unrelated credits/debits (dev-10, ho-08), foreign
currency (dev-08, ho-08), prompt-injection text (dev-09 in a receipt, ho-09 in a letter), refund and
later-status updates (ho-03, ho-04, ho-05, ho-09, ho-10 — five holdout packs).

## Audio

- English clips are produced by Windows SAPI text-to-speech from authored scripts (`gen_audio.py`).
- **Hindi and Hinglish clips are pending**: no Hindi voice exists on the development machine.
  `audio/index.json` lists them as `pending_owner_recording` with the script to speak. Record them
  with the in-app recorder (Material page), then add the WAV and transcript here.
- Spoken number words ("twenty five thousand rupees") are kept as raw text; the app leaves the
  amount unreadable for the citizen to correct. This is intended.

## Regenerating

```bash
uv run --project backend --extra fixtures python scripts/fixtures/gen_receipt.py
uv run --project backend --extra fixtures python scripts/fixtures/gen_official_update.py
uv run --project backend --extra fixtures python scripts/fixtures/gen_audio.py
uv run --project backend --extra fixtures python scripts/fixtures/build_packs.py
uv run --project backend --extra fixtures python scripts/fixtures/build_pack_demo_b.py
uv run --project backend --extra fixtures python scripts/fixtures/build_dataset.py
```

Generation is deterministic (seeded, fixed fonts), so regenerated files hash identically on the
same platform. Generated files are committed so that CI and Docker need no fonts.

## Do not

Do not add real screenshots, real names, real references or real complaint numbers. Do not use
held-out packs for prompt tuning; if you do, record it in `docs/evaluation-results.md` and create a
fresh holdout before claiming a result.
