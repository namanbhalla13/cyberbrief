"""Generate and normalise fixture audio clips (16 kHz mono 16-bit PCM WAV).

English clips come from Windows SAPI text-to-speech (pyttsx3). Hindi and Hinglish clips cannot be
synthesised on the development machine (only English voices are installed), so this script writes a
clearly labelled placeholder entry and expects the owner to record them with the app's recorder.
Transcripts in fixture responses are the authored scripts, never model output.

Usage: uv run --project backend --extra fixtures python scripts/fixtures/gen_audio.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIO_DIR = REPO_ROOT / "evals" / "fixtures" / "audio"

CLIPS = {
    # pack, source id, language, script (authored transcript)
    "demo-a": [("A05", "en", "I ordered clothes from Trendy Threads Store on the twelfth of October. I paid them twenty five thousand rupees "
                              "in total through U P I. Nothing was delivered and they stopped replying. The last payment was yesterday and still shows as pending.")],
    "demo-b": [("B05", "en", "I ordered a phone from Gadget Galaxy Deals on the fifteenth of October and paid twenty thousand rupees in two payments "
                              "of ten thousand each. The phone never arrived and the seller blocked me.")],
}
PENDING_OWNER_RECORDINGS = [
    ("demo-a", "A06", "hi", "मैंने ट्रेंडी थ्रेड्स स्टोर को कुल पच्चीस हज़ार रुपये यूपीआई से दिए। सामान नहीं आया और उन्होंने जवाब देना बंद कर दिया।"),
    ("demo-b", "B06", "hinglish", "Maine Gadget Galaxy Deals ko bees hazaar rupaye do payments mein diye, phone aaya hi nahi aur seller ne block kar diya."),
]


def normalise_to_16k(path_in: Path, path_out: Path) -> int:
    data, rate = sf.read(str(path_in), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate != 16000:
        from math import gcd

        g = gcd(16000, rate)
        mono = resample_poly(mono, 16000 // g, rate // g).astype("float32")
    mono = np.clip(mono, -1.0, 1.0)
    sf.write(str(path_out), mono, 16000, subtype="PCM_16")
    return round(len(mono) * 1000 / 16000)


def synthesise(text: str, out_wav: Path) -> int:
    import pyttsx3

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.wav"
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.save_to_file(text, str(raw))
        engine.runAndWait()
        if not raw.exists() or raw.stat().st_size < 1000:
            raise RuntimeError("TTS produced no audio")
        return normalise_to_16k(raw, out_wav)


def main() -> int:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    for pack_id, clips in CLIPS.items():
        for source_id, language, script in clips:
            out = AUDIO_DIR / f"{pack_id}-{source_id}.wav"
            duration_ms = synthesise(script, out)
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            transcript = {"transcript": script, "audioDurationMs": duration_ms,
                          "turns": [{"turnId": 0, "startMs": 0, "endMs": duration_ms, "transcript": script}],
                          "labelProvenance": "authored script spoken by Windows SAPI TTS (English voice); not model output"}
            (AUDIO_DIR / f"{pack_id}-{source_id}.transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
            index.append({"pack": pack_id, "source": source_id, "language": language, "file": out.name, "sha256": digest,
                          "durationMs": duration_ms, "provenance": "sapi_tts_english", "transcriptFile": f"{pack_id}-{source_id}.transcript.json"})
            print(f"{pack_id} {source_id} ({language}): {out.name} {duration_ms} ms sha256={digest[:12]}")
    for pack_id, source_id, language, script in PENDING_OWNER_RECORDINGS:
        index.append({"pack": pack_id, "source": source_id, "language": language, "file": None, "sha256": None, "durationMs": None,
                      "provenance": "pending_owner_recording", "script": script,
                      "note": "No Hindi TTS voice on the dev machine; record with the in-app recorder, then rerun this script with --import"})
        print(f"{pack_id} {source_id} ({language}): PENDING owner recording")
    (AUDIO_DIR / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
