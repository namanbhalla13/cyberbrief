"""Generate fictional UPI-style receipt images with Pillow. Deterministic, no real brands.

Layout `variant_a` imitates a generic payment-app success screen for a fictional app called
"DemoPay". Every render also writes a `<name>.regions.json` sidecar with the pixel box of each
field, so expected image anchors can be authored from ground truth rather than model output.

Usage:  uv run --project backend --extra fixtures python scripts/fixtures/gen_receipt.py
        (writes Demo A sources S01-S04 to evals/fixtures/development/demo-a/)
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
BOLD_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
DEVANAGARI_CANDIDATES = ["C:/Windows/Fonts/Nirmala.ttc", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"]

_DEVANAGARI = str.maketrans("0123456789", "०१२३४५६७८९")


def _font(size: int, bold: bool = False, devanagari: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = DEVANAGARI_CANDIDATES if devanagari else (BOLD_CANDIDATES if bold else FONT_CANDIDATES)
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


@dataclass
class ReceiptSpec:
    name: str
    amount_text: str  # e.g. "₹10,000.00"
    reference: str
    reference_label: str = "UPI Ref No"
    status: str = "successful"  # successful | pending | failed
    direction: str = "debit"  # debit -> "Paid to"; credit -> "Received from"
    date_text: str | None = "12 Oct 2026, 10:42 AM"
    payee_name: str = "Trendy Threads Store"
    payee_vpa: str = "trendythreads.demo@demobank"
    payer_vpa: str = "citizen.demo@demobank"
    remarks: str | None = None
    injected_text: str | None = None  # drawn as a remarks/note line to test prompt-injection handling
    digit_script: str = "latin"  # latin | devanagari (digits only)
    layout: str = "variant_a"  # variant_a DemoPay card | variant_b PayLedger statement row | variant_c QuickUPI dark bubble
    fmt: str = "png"  # png | jpg | webp
    crop: str | None = None  # None | bottom_cut | amount_ref_only
    degrade: dict = field(default_factory=dict)  # blur, jpeg_quality, downscale
    seed: int = 1


STATUS_TEXT = {
    "successful": ("Payment Successful", (24, 150, 76)),
    "pending": ("Payment Pending", (214, 140, 0)),
    "failed": ("Payment Failed", (200, 40, 40)),
}


def _writer(draw: ImageDraw.ImageDraw, regions: dict, width: int, dev: bool):
    def text(key: str | None, xy: tuple[int, int], value: str, size: int, fill=(30, 30, 30), bold=False, center=False):
        f = _font(size, bold=bold, devanagari=dev and any(ch.isdigit() for ch in value))
        shown = value.translate(_DEVANAGARI) if dev else value
        x, y = xy
        if center:
            left, top, right, bottom = draw.textbbox((0, 0), shown, font=f)
            x = (width - (right - left)) // 2
        draw.text((x, y), shown, font=f, fill=fill)
        if key:
            left, top, right, bottom = draw.textbbox((x, y), shown, font=f)
            regions[key] = {"x": left, "y": top, "width": right - left, "height": bottom - top, "text": shown}
        return x

    return text


def render_variant_b(spec: ReceiptSpec) -> tuple[Image.Image, dict]:
    """PayLedger: a bank-statement style row. Direction is encoded as DR/CR; statements show no status."""
    width, height = 900, 420
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    regions: dict[str, dict] = {}
    text = _writer(draw, regions, width, spec.digit_script == "devanagari")
    draw.rectangle([0, 0, width, 70], fill=(236, 239, 244))
    text("app_name", (28, 20), "PayLedger", 28, fill=(40, 60, 110), bold=True)
    text(None, (200, 28), f"Account statement · {spec.payer_vpa}", 18, fill=(90, 90, 100))
    headers = [(28, "Date"), (200, "Description"), (590, "Debit"), (700, "Credit"), (800, "Balance")]
    for x, label in headers:
        text(None, (x, 110), label, 16, fill=(120, 120, 130), bold=True)
    draw.line([(28, 140), (width - 28, 140)], fill=(220, 224, 230), width=2)
    y = 160
    date_only = (spec.date_text or "").split(",")[0]
    text("date", (28, y), date_only or "—", 18)
    tag = "DR" if spec.direction == "debit" else "CR"
    x_after = text("direction", (200, y), f"UPI/{tag}/", 18, fill=(60, 60, 70))
    x_after = text("reference", (x_after + draw.textlength(f"UPI/{tag}/", font=_font(18)), y), spec.reference, 18, bold=True)
    text("payee", (200, y + 26), spec.payee_name, 16, fill=(90, 90, 100))
    text("amount", (590 if spec.direction == "debit" else 700, y), spec.amount_text, 18, bold=True)
    text(None, (800, y), "₹1,23,456.78", 18, fill=(90, 90, 100))
    note = spec.remarks or spec.injected_text
    if note:
        text("remarks", (200, y + 52), f"Note: {note}", 14, fill=(110, 110, 120))
    draw.line([(28, y + 84), (width - 28, y + 84)], fill=(236, 239, 244), width=2)
    text(None, (28, height - 40), "Fictional demo statement. Not a real bank or payment.", 14, fill=(150, 150, 160))
    return image, regions


def render_variant_c(spec: ReceiptSpec) -> tuple[Image.Image, dict]:
    """QuickUPI: dark chat-bubble confirmation."""
    width, height = 720, 1280
    image = Image.new("RGB", (width, height), (17, 24, 39))
    draw = ImageDraw.Draw(image)
    regions: dict[str, dict] = {}
    text = _writer(draw, regions, width, spec.digit_script == "devanagari")
    text("app_name", (28, 30), "QuickUPI", 32, fill=(165, 180, 252), bold=True)
    text(None, (width - 150, 40), "Demo build", 18, fill=(140, 150, 180))
    draw.rounded_rectangle([40, 300, width - 40, 820], radius=28, fill=(31, 41, 55))
    verb = "You paid" if spec.direction == "debit" else "You received"
    text("direction", (72, 340), verb, 26, fill=(200, 205, 220))
    text("amount", (72, 384), spec.amount_text, 64, fill=(255, 255, 255), bold=True)
    text("payee", (72, 470), ("to " if spec.direction == "debit" else "from ") + spec.payee_name, 24, fill=(200, 205, 220))
    text("payee_vpa", (72, 506), spec.payee_vpa, 18, fill=(140, 150, 180))
    text(None, (72, 570), "UPI transaction ID", 16, fill=(140, 150, 180))
    text("reference", (72, 594), spec.reference, 24, fill=(255, 255, 255), bold=True)
    if spec.date_text:
        text("date", (72, 650), spec.date_text, 20, fill=(200, 205, 220))
    chip = {"successful": ("Completed", (34, 197, 94)), "pending": ("Pending", (245, 158, 11)), "failed": ("Failed", (239, 68, 68))}[spec.status]
    draw.rounded_rectangle([72, 710, 72 + 170, 752], radius=20, fill=chip[1])
    text("status", (92, 718), chip[0], 20, fill=(10, 10, 10), bold=True)
    note = spec.remarks or spec.injected_text
    if note:
        text("remarks", (72, 770), f"Note: {note}", 16, fill=(200, 205, 220))
    text("payer_vpa", (72, 860), f"From {spec.payer_vpa}", 18, fill=(140, 150, 180))
    text(None, (0, 1215), "Fictional demo confirmation. Not a real payment.", 16, fill=(120, 130, 160), center=True)
    return image, regions


def render_receipt(spec: ReceiptSpec) -> tuple[Image.Image, dict]:
    if spec.layout == "variant_b":
        return render_variant_b(spec)
    if spec.layout == "variant_c":
        return render_variant_c(spec)
    width, height = 720, 1280
    image = Image.new("RGB", (width, height), (247, 248, 250))
    draw = ImageDraw.Draw(image)
    regions: dict[str, dict] = {}
    dev = spec.digit_script == "devanagari"
    text = _writer(draw, regions, width, dev)

    # header bar
    draw.rectangle([0, 0, width, 110], fill=(44, 62, 120))
    text("app_name", (28, 34), "DemoPay", 36, fill=(255, 255, 255), bold=True)
    text(None, (width - 170, 44), "Demo build", 20, fill=(200, 210, 240))

    # status glyph
    label, colour = STATUS_TEXT[spec.status]
    if spec.direction == "credit" and spec.status == "successful":
        label = "Payment Received"
    cx, cy, r = width // 2, 290, 78
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    if spec.status == "successful":
        draw.line([(cx - 36, cy), (cx - 8, cy + 30), (cx + 42, cy - 30)], fill="white", width=12)
    elif spec.status == "pending":
        draw.arc([cx - 36, cy - 36, cx + 36, cy + 36], 20, 300, fill="white", width=12)
    else:
        draw.line([(cx - 30, cy - 30), (cx + 30, cy + 30)], fill="white", width=12)
        draw.line([(cx - 30, cy + 30), (cx + 30, cy - 30)], fill="white", width=12)
    text("status", (0, 400), label, 40, fill=colour, bold=True, center=True)

    direction_label = "Paid to" if spec.direction == "debit" else "Received from"
    text("direction", (0, 470), direction_label, 26, fill=(100, 100, 110), center=True)
    text("payee", (0, 508), spec.payee_name, 32, bold=True, center=True)
    text("payee_vpa", (0, 552), spec.payee_vpa, 22, fill=(90, 90, 100), center=True)
    text("amount", (0, 620), spec.amount_text, 72, bold=True, center=True)
    if spec.date_text:
        text("date", (0, 720), spec.date_text, 24, fill=(90, 90, 100), center=True)

    # details card
    draw.rounded_rectangle([40, 800, width - 40, 1180], radius=24, fill="white", outline=(225, 228, 235))
    rows = [
        ("reference", spec.reference_label, spec.reference),
        ("payer_vpa", "From" if spec.direction == "debit" else "To", spec.payer_vpa),
        ("status_detail", "Status", label.replace("Payment ", "") if spec.direction == "debit" else
         {"successful": "Received", "pending": "Pending", "failed": "Failed"}[spec.status]),
    ]
    if spec.remarks or spec.injected_text:
        rows.append(("remarks", "Remarks", spec.remarks or spec.injected_text or ""))
    y = 836
    for key, k, v in rows:
        text(None, (72, y), k, 22, fill=(120, 120, 130))
        text(key, (72, y + 32), v, 28, bold=(key == "reference"))
        draw.line([(72, y + 84), (width - 72, y + 84)], fill=(235, 237, 242), width=2)
        y += 100
    text(None, (0, 1215), "Fictional demo receipt. Not a real payment.", 18, fill=(150, 150, 160), center=True)

    return image, regions


def apply_crop_and_degrade(image: Image.Image, regions: dict, spec: ReceiptSpec) -> tuple[Image.Image, dict]:
    offset = (0, 0)
    if spec.crop == "bottom_cut":  # hides the details card (reference + from)
        image = image.crop((0, 0, image.width, 790 if spec.layout != "variant_c" else 560))
    elif spec.crop == "ref_truncated":  # cuts the reference row on its right side, leaving a partial identifier
        ref = regions["reference"]
        cut = ref["x"] + int(ref["width"] * 0.55)
        image = image.crop((0, 0, cut, image.height))
    elif spec.crop == "amount_ref_only":  # keeps amount and reference, drops date and header
        top = regions["amount"]["y"] - 20
        bottom = regions["reference"]["y"] + regions["reference"]["height"] + 24
        image = image.crop((0, top, image.width, bottom))
        offset = (0, top)
        # remove the date line by painting over its band inside the crop
        d = ImageDraw.Draw(image)
        date = regions.get("date")
        if date:
            d.rectangle([0, date["y"] - top - 6, image.width, date["y"] - top + date["height"] + 6], fill=(247, 248, 250))
    scale = spec.degrade.get("downscale")
    if scale:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    blur = spec.degrade.get("blur")
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    noise = spec.degrade.get("noise")
    if noise:
        import random as _random

        rng = _random.Random(spec.seed)  # noqa: S311 - deterministic image noise, not security
        px = image.load()
        for _ in range(int(image.width * image.height * float(noise))):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            v = rng.randrange(0, 255)
            px[x, y] = (v, v, v)
    rotate = spec.degrade.get("rotate")
    if rotate:
        image = image.rotate(float(rotate), expand=False, fillcolor=(247, 248, 250))

    adjusted: dict[str, dict] = {}
    for key, box in regions.items():
        x, y = box["x"] - offset[0], box["y"] - offset[1]
        if y < 0 or y + box["height"] > (image.height / (scale or 1)):
            continue  # cropped away
        if spec.crop == "amount_ref_only" and key == "date":
            continue
        s = scale or 1
        adjusted[key] = {"x": round(x * s), "y": round(y * s), "width": round(box["width"] * s),
                         "height": round(box["height"] * s), "text": box["text"]}
    return image, adjusted


def encode(image: Image.Image, spec: ReceiptSpec) -> bytes:
    buffer = io.BytesIO()
    if spec.fmt == "jpg":
        image.convert("RGB").save(buffer, "JPEG", quality=int(spec.degrade.get("jpeg_quality", 90)))
    elif spec.fmt == "webp":
        image.save(buffer, "WEBP", quality=int(spec.degrade.get("jpeg_quality", 90)))
    else:
        image.save(buffer, "PNG")
    return buffer.getvalue()


def write_receipt(spec: ReceiptSpec, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    image, regions = render_receipt(spec)
    image, regions = apply_crop_and_degrade(image, regions, spec)
    data = encode(image, spec)
    ext = {"png": "png", "jpg": "jpg", "webp": "webp"}[spec.fmt]
    path = out_dir / f"{spec.name}.{ext}"
    path.write_bytes(data)
    (out_dir / f"{spec.name}.regions.json").write_text(
        json.dumps({"spec": asdict(spec), "imageSize": [image.width, image.height], "regions": regions}, indent=2),
        encoding="utf-8",
    )
    return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "mediaType": {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[spec.fmt],
            "imageSize": [image.width, image.height]}


DEMO_A: list[ReceiptSpec] = [
    ReceiptSpec(name="S01", amount_text="₹10,000.00", reference="DEMO-A-001", date_text="12 Oct 2026, 10:42 AM"),
    # S02: cropped re-save of S01 (same reference, different bytes, no date visible)
    ReceiptSpec(name="S02", amount_text="₹10,000.00", reference="DEMO-A-001", date_text="12 Oct 2026, 10:42 AM",
                crop="amount_ref_only", fmt="jpg", degrade={"downscale": 0.85, "jpeg_quality": 80}),
    ReceiptSpec(name="S03", amount_text="₹10,000.00", reference="DEMO-A-002", date_text="12 Oct 2026, 11:05 AM"),
    ReceiptSpec(name="S04", amount_text="₹5,000.00", reference="DEMO-A-003", status="pending",
                date_text="13 Oct 2026, 09:15 AM"),
]


DEMO_B: list[ReceiptSpec] = [
    ReceiptSpec(name="S01", amount_text="₹10,000.00", reference="DEMO-B-001", date_text="15 Oct 2026, 06:12 PM",
                payee_name="Gadget Galaxy Deals", payee_vpa="gadgetgalaxy.demo@demobank"),
    ReceiptSpec(name="S02", amount_text="₹10,000.00", reference="DEMO-B-002", date_text="15 Oct 2026, 06:20 PM",
                payee_name="Gadget Galaxy Deals", payee_vpa="gadgetgalaxy.demo@demobank"),
    # credits: "Received from" the merchant; references differ from the original debits
    ReceiptSpec(name="R1", amount_text="₹5,000.00", reference="REF-B-RF1", direction="credit", date_text="20 Oct 2026, 11:03 AM",
                payee_name="Gadget Galaxy Deals", payee_vpa="gadgetgalaxy.demo@demobank"),
    ReceiptSpec(name="R2", amount_text="₹15,000.00", reference="REF-B-RF2", direction="credit", date_text="24 Oct 2026, 04:45 PM",
                payee_name="Gadget Galaxy Deals", payee_vpa="gadgetgalaxy.demo@demobank"),
]

PACKS = {"demo-a": DEMO_A, "demo-b": DEMO_B}


def main() -> int:
    for pack_id, specs in PACKS.items():
        out_dir = REPO_ROOT / "evals" / "fixtures" / "development" / pack_id
        index_path = out_dir / "images.json"
        existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        kept = [e for e in existing if e["source"] not in {s.name for s in specs}]
        index = kept + [{"source": spec.name, **write_receipt(spec, out_dir)} for spec in specs]
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
        for entry in index:
            print(f"{pack_id} {entry['source']}: {entry['file']} {entry['bytes']} B sha256={entry['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
