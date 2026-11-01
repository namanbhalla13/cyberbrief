"""Generate a fictional 'official update' image: a plain helpdesk letter with status wording.

No real agency names, logos or seals. The issuer is "Demo Cyber Helpdesk (fictional)".
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_receipt import _font  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class UpdateSpec:
    name: str
    reference: str
    date_text: str
    status_text: str
    issuer: str = "Demo Cyber Helpdesk (fictional)"
    addressee: str = "Dear complainant,"
    seed: int = 1


def render_update(spec: UpdateSpec) -> tuple[Image.Image, dict]:
    width, height = 900, 700
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    regions: dict[str, dict] = {}

    def text(key: str | None, xy, value: str, size: int, bold=False, fill=(20, 20, 20)):
        f = _font(size, bold=bold)
        draw.text(xy, value, font=f, fill=fill)
        if key:
            left, top, right, bottom = draw.textbbox(xy, value, font=f)
            regions[key] = {"x": left, "y": top, "width": right - left, "height": bottom - top, "text": value}

    draw.rectangle([0, 0, width, 90], fill=(235, 238, 245))
    text("issuer", (40, 28), spec.issuer, 30, bold=True, fill=(44, 62, 120))
    text(None, (40, 110), "Complaint acknowledgement", 24, bold=True)
    text("reference", (40, 160), f"Complaint reference: {spec.reference}", 22)
    text("date", (40, 196), f"Date: {spec.date_text}", 22)
    text(None, (40, 250), spec.addressee, 22)

    # wrap the status text at ~70 characters
    words, lines, current = spec.status_text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > 68:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    y = 296
    first_top = y
    for line in lines:
        text(None, (40, y), line, 22)
        y += 34
    f = _font(22)
    regions["status"] = {"x": 40, "y": first_top, "width": max(draw.textlength(line, font=f) for line in lines), "height": y - first_top, "text": spec.status_text}

    text(None, (40, y + 30), "This communication is fictional demo material for testing software. It is not from any real authority.", 16, fill=(120, 120, 130))
    text(None, (40, height - 60), "Demo Cyber Helpdesk (fictional) · demo-helpdesk@example.invalid", 16, fill=(120, 120, 130))
    return image, regions


def write_update(spec: UpdateSpec, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    image, regions = render_update(spec)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = buffer.getvalue()
    (out_dir / f"{spec.name}.png").write_bytes(data)
    (out_dir / f"{spec.name}.regions.json").write_text(json.dumps({"spec": asdict(spec), "imageSize": [image.width, image.height], "regions": regions}, indent=2), encoding="utf-8")
    return {"source": spec.name, "file": f"{spec.name}.png", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "mediaType": "image/png", "imageSize": [image.width, image.height]}


DEMO_B_UPDATES = [
    UpdateSpec(name="O1", reference="DEMO-B-ACK-7781", date_text="20 Oct 2026",
               status_text="Your complaint has been acknowledged and forwarded to the concerned bank for necessary action. You will be informed of further developments."),
]


def main() -> int:
    out_dir = REPO_ROOT / "evals" / "fixtures" / "development" / "demo-b"
    index_path = out_dir / "images.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [e for e in index if not e["source"].startswith("O")]
    for spec in DEMO_B_UPDATES:
        entry = write_update(spec, out_dir)
        index.append(entry)
        print(f"{entry['source']}: {entry['file']} {entry['bytes']} B sha256={entry['sha256'][:12]}")
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
