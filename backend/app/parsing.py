"""Reading values out of supplied material: money, dates, provenance anchors, observations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from .schemas import ObservationProposal

# ---- from services/money.py ----------------------------------------------------------------------
# Money parsing and formatting. Decimal while parsing, integer paise internally, strings in JSON.
#
# Rules: preserve raw text; accept Indian and Western grouping and Devanagari digits;
# reject ambiguous symbols, invalid grouping, more than two fractional digits, negatives and
# overflow instead of guessing; never infer INR from context; never repair unreadable digits.

# 100 crore rupees, in paise. A documented application bound, not a legal or banking limit.
MAX_AMOUNT_MINOR = 1_000_000_000 * 100

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_CURRENCY_TOKENS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<![A-Za-z])(INR|Rs\.?|Rs|रु\.?|रुपये|रुपए|₹)(?![A-Za-z])", re.IGNORECASE), "INR"),
    (re.compile(r"(?<![A-Za-z])(USD|US\$|\$)(?![A-Za-z])"), "USD"),
    (re.compile(r"(?<![A-Za-z])(EUR|€)(?![A-Za-z])"), "EUR"),
    (re.compile(r"(?<![A-Za-z])(GBP|£)(?![A-Za-z])"), "GBP"),
    (re.compile(r"(?<![A-Za-z])(AED)(?![A-Za-z])"), "AED"),
]

# A numeric token: digits with optional comma grouping and optional fraction.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
# Letters commonly confused with digits when they sit against digits or commas.
_CONFUSABLE = re.compile(r"(?:(?<=[\d,.])[OoIl](?=[\d,.])|(?<=[\d,.])[OoIl]\b|\b[OoIl](?=[\d,.]))")
_INDIAN_GROUPING = re.compile(r"^\d{1,2}(,\d{2})*,\d{3}$")
_WESTERN_GROUPING = re.compile(r"^\d{1,3}(,\d{3})+$")


class MoneyParseError(ValueError):
    """Raised when an amount cannot be parsed safely. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Money:
    raw_text: str
    currency: str | None
    amount_minor: int | None

    def to_json(self) -> dict[str, str | None]:
        return {
            "rawText": self.raw_text,
            "currency": self.currency,
            "amountMinor": None if self.amount_minor is None else str(self.amount_minor),
        }


def detect_currency(raw: str) -> str | None:
    """Return a currency code only when a currency token is present. Never infers INR."""
    found: set[str] = set()
    for pattern, code in _CURRENCY_TOKENS:
        if pattern.search(raw):
            found.add(code)
    if len(found) > 1:
        raise MoneyParseError("ambiguous_currency", f"More than one currency token in {raw!r}.")
    return next(iter(found)) if found else None


def parse_amount(raw: str | None, *, currency_hint: str | None = None) -> Money:
    """Parse a raw amount string. Raises MoneyParseError rather than guessing."""
    if raw is None or not raw.strip():
        raise MoneyParseError("missing", "No amount text was supplied.")
    text = raw.translate(_DEVANAGARI_DIGITS)

    if _CONFUSABLE.search(text):
        raise MoneyParseError(
            "ambiguous_symbol",
            f"{raw!r} contains a letter that may be a digit (O/0, I/l/1). Left unresolved.",
        )

    detected = detect_currency(text)
    currency = detected
    if currency is None and currency_hint:
        currency = currency_hint.upper()
    elif detected and currency_hint and detected != currency_hint.upper():
        raise MoneyParseError(
            "ambiguous_currency", f"Text says {detected} but the source claims {currency_hint}."
        )

    stripped = text
    for pattern, _ in _CURRENCY_TOKENS:
        stripped = pattern.sub(" ", stripped)

    if re.search(r"(^|\s)[-−–]\s*\d|\(\s*[\d,.]+\s*\)", stripped):
        raise MoneyParseError(
            "negative", f"{raw!r} is negative or bracketed; direction is a separate field."
        )

    numbers = _NUMBER.findall(stripped)
    if not numbers:
        raise MoneyParseError("unreadable", f"No digits found in {raw!r}.")
    if len(numbers) > 1:
        raise MoneyParseError("ambiguous_multiple", f"More than one number in {raw!r}.")
    token = numbers[0]

    integer_part, _, fraction = token.partition(".")
    if fraction and len(fraction) > 2:
        raise MoneyParseError(
            "too_many_decimals", f"{raw!r} has more than two fractional digits."
        )
    if "," in integer_part:
        if not (_INDIAN_GROUPING.match(integer_part) or _WESTERN_GROUPING.match(integer_part)):
            raise MoneyParseError("invalid_grouping", f"{raw!r} has inconsistent digit grouping.")
        integer_part = integer_part.replace(",", "")
    if integer_part == "":
        integer_part = "0"

    try:
        value = Decimal(f"{integer_part}.{fraction or '0'}")
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex above
        raise MoneyParseError("unreadable", f"Cannot parse {raw!r}.") from exc

    amount_minor = int((value * 100).to_integral_value())
    if amount_minor > MAX_AMOUNT_MINOR:
        raise MoneyParseError("overflow", f"{raw!r} exceeds the application bound.")
    return Money(raw_text=raw, currency=currency, amount_minor=amount_minor)


def format_inr(amount_minor: int) -> str:
    """Format integer paise as rupees with Indian grouping, e.g. 1000000 -> '₹10,000.00'."""
    sign = "-" if amount_minor < 0 else ""
    rupees, paise = divmod(abs(amount_minor), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}₹{digits}.{paise:02d}"


def minor_to_str(amount_minor: int | None) -> str | None:
    return None if amount_minor is None else str(amount_minor)


def str_to_minor(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if not re.fullmatch(r"-?\d+", value):
        raise MoneyParseError("invalid_minor", f"{value!r} is not an integer paise string.")
    return int(value)


# ---- from services/dates.py ----------------------------------------------------------------------
# Date handling. Preserve what is known; never invent midnight, a timezone, or a reference day.
#
# Kinds:
# - instant: time and offset known -> stored as UTC.
# - local_datetime: time known, offset unknown -> kept naive, displayed as written.
# - date: calendar day only.
# - unresolved: ambiguous (day/month), relative ("yesterday"), impossible, or unreadable.

Kind = Literal["instant", "local_datetime", "date", "unresolved"]

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

_RELATIVE = re.compile(
    r"\b(today|yesterday|tomorrow|tonight|last\s+(week|month|night)|"
    r"\d+\s+(days?|weeks?|months?|hours?)\s+ago|kal|aaj|parso|pichhle\s+hafte)\b",
    re.IGNORECASE,
)
_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_NUMERIC = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})(?:[ ,]+(.*))?$")
_NAMED = re.compile(
    r"^(?:(\d{1,2})\s*([A-Za-z]{3,9})\.?,?\s*(\d{4})|([A-Za-z]{3,9})\.?\s*(\d{1,2}),?\s*(\d{4}))"
    r"(?:[ ,]+(?:at\s+)?(.*))?$"
)
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?::(\d{2}))?\s*(am|pm|AM|PM)?\s*(IST|UTC|GMT)?$")


@dataclass(frozen=True)
class ParsedDate:
    raw_text: str
    kind: Kind
    instant_utc: datetime | None = None
    local_datetime: datetime | None = None
    day: date | None = None
    issue: str | None = None

    @property
    def sort_key(self) -> date | None:
        if self.instant_utc:
            return self.instant_utc.date()
        if self.local_datetime:
            return self.local_datetime.date()
        return self.day

    def to_json(self) -> dict[str, str | None]:
        return {
            "rawText": self.raw_text,
            "kind": self.kind,
            "instantUtc": self.instant_utc.isoformat() if self.instant_utc else None,
            "localDatetime": self.local_datetime.isoformat() if self.local_datetime else None,
            "date": self.day.isoformat() if self.day else None,
            "issue": self.issue,
        }


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_time(text: str | None) -> tuple[int, int, int, str | None] | None:
    if not text:
        return None
    match = _TIME.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    meridiem = (match.group(4) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second, match.group(5)


def _with_time(raw: str, day: date, time_text: str | None) -> ParsedDate:
    if not time_text or not time_text.strip():
        return ParsedDate(raw, "date", day=day)
    parsed = _parse_time(time_text)
    if parsed is None:
        return ParsedDate(raw, "unresolved", day=day, issue="unreadable_time")
    hour, minute, second, zone = parsed
    naive = datetime(day.year, day.month, day.day, hour, minute, second)
    if zone in ("UTC", "GMT"):
        return ParsedDate(raw, "instant", instant_utc=naive.replace(tzinfo=UTC), day=day)
    if zone == "IST":
        from datetime import timedelta, timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        return ParsedDate(
            raw, "instant", instant_utc=naive.replace(tzinfo=ist).astimezone(UTC), day=day
        )
    return ParsedDate(raw, "local_datetime", local_datetime=naive, day=day)


def parse_date_text(raw: str | None) -> ParsedDate:
    if raw is None or not raw.strip():
        return ParsedDate(raw or "", "unresolved", issue="missing")
    text = raw.strip().translate(_DEVANAGARI_DIGITS)

    if _RELATIVE.search(text):
        return ParsedDate(raw, "unresolved", issue="relative")

    iso = _ISO.match(text)
    if iso:
        year, month, day_ = (int(iso.group(i)) for i in (1, 2, 3))
        day = _safe_date(year, month, day_)
        if day is None:
            return ParsedDate(raw, "unresolved", issue="impossible")
        if iso.group(4) is None:
            return ParsedDate(raw, "date", day=day)
        hour, minute = int(iso.group(4)), int(iso.group(5))
        second = int(iso.group(6) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return ParsedDate(raw, "unresolved", issue="impossible")
        naive = datetime(year, month, day_, hour, minute, second)
        offset = iso.group(7)
        if offset is None:
            return ParsedDate(raw, "local_datetime", local_datetime=naive, day=day)
        aware = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
        return ParsedDate(raw, "instant", instant_utc=aware.astimezone(UTC), day=day)

    numeric = _NUMERIC.match(text)
    if numeric:
        first, second_, year_text, rest = numeric.groups()
        a, b = int(first), int(second_)
        year = int(year_text)
        if year < 100:
            year += 2000
        if a > 31 or b > 31 or a == 0 or b == 0:
            return ParsedDate(raw, "unresolved", issue="impossible")
        if a <= 12 and b <= 12 and a != b:
            return ParsedDate(raw, "unresolved", issue="ambiguous_day_month")
        if a > 12:
            day = _safe_date(year, b, a)  # dd/mm
        elif b > 12:
            day = _safe_date(year, a, b)  # mm/dd
        else:
            day = _safe_date(year, a, b)  # a == b, order irrelevant
        if day is None:
            return ParsedDate(raw, "unresolved", issue="impossible")
        return _with_time(raw, day, rest)

    named = _NAMED.match(text)
    if named:
        if named.group(1):
            day_, month_name, year = int(named.group(1)), named.group(2), int(named.group(3))
        else:
            month_name, day_, year = named.group(4), int(named.group(5)), int(named.group(6))
        month = _MONTHS.get(month_name.lower())
        if month is None:
            return ParsedDate(raw, "unresolved", issue="unreadable")
        day = _safe_date(year, month, day_)
        if day is None:
            return ParsedDate(raw, "unresolved", issue="impossible")
        return _with_time(raw, day, named.group(7))

    return ParsedDate(raw, "unresolved", issue="unreadable")


# ---- from services/provenance.py -----------------------------------------------------------------
# Source anchors: every material fact points at a same-case source, or is unresolved.
#
# Pure functions over light records so the rules are testable without a database. The ORM
# layer builds `SourceRef`/`SegmentRef` from rows. A valid anchor proves the source exists and
# (for text sources) that the quote is really in it; it never proves the source is truthful.

@dataclass(frozen=True)
class SourceRef:
    id: str
    case_id: str
    kind: str
    sha256: str
    text_content: str | None = None
    image_size: tuple[int, int] | None = None
    audio_duration_ms: int | None = None


@dataclass(frozen=True)
class SegmentRef:
    id: str
    source_id: str
    text: str | None = None
    audio_range_ms: tuple[int, int] | None = None
    image_region: dict | None = None


@dataclass(frozen=True)
class ValidatedAnchor:
    source_id: str
    segment_id: str | None
    quote: str | None
    quote_verified: bool
    image_region: dict | None
    audio_range_ms: dict | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {
            "sourceId": self.source_id,
            "segmentId": self.segment_id,
            "quote": self.quote,
            "quoteVerified": self.quote_verified,
            "imageRegion": self.image_region,
            "audioRangeMs": self.audio_range_ms,
            "notes": list(self.notes),
        }


class AnchorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def validate_anchor(
    anchor: dict,
    case_id: str,
    sources: dict[str, SourceRef],
    segments: dict[str, SegmentRef] | None = None,
) -> ValidatedAnchor:
    segments = segments or {}
    notes: list[str] = []

    source_id = anchor.get("sourceId")
    if not source_id:
        raise AnchorError("missing_source", "Anchor has no sourceId.")
    source = sources.get(source_id)
    if source is None:
        raise AnchorError("unknown_source", f"Source {source_id} is not known.")
    if source.case_id != case_id:
        raise AnchorError("cross_case_source", "Anchor points at a source from another case.")

    segment_id = anchor.get("segmentId")
    segment: SegmentRef | None = None
    if segment_id:
        segment = segments.get(segment_id)
        if segment is None:
            raise AnchorError("unknown_segment", f"Segment {segment_id} is not known.")
        if segment.source_id != source_id:
            raise AnchorError("segment_source_mismatch", "Segment belongs to a different source.")

    quote = anchor.get("quote")
    quote_verified = False
    if quote:
        haystack = segment.text if segment and segment.text else source.text_content
        if haystack is not None:
            if _normalise(quote) not in _normalise(haystack):
                raise AnchorError("quote_not_found", "Quoted text is not present in the source.")
            quote_verified = True
        else:
            notes.append("model_reported_excerpt")  # image/audio: excerpt cannot be verified

    region = anchor.get("imageRegion")
    valid_region: dict | None = None
    if region is not None:
        valid_region = _validate_region(region, source.image_size)
        if valid_region is None:
            notes.append("region_invalid")  # fall back to opening the full source

    audio_range = anchor.get("audioRangeMs")
    valid_range: dict | None = None
    if audio_range is not None:
        valid_range = _validate_range(audio_range, source.audio_duration_ms)
        if valid_range is None:
            notes.append("range_invalid")

    return ValidatedAnchor(
        source_id=source_id,
        segment_id=segment_id,
        quote=quote,
        quote_verified=quote_verified,
        image_region=valid_region,
        audio_range_ms=valid_range,
        notes=tuple(notes),
    )


def validate_anchors(
    anchors: list[dict],
    case_id: str,
    sources: dict[str, SourceRef],
    segments: dict[str, SegmentRef] | None = None,
) -> list[ValidatedAnchor]:
    """Validate every anchor; any invalid anchor rejects the whole set (no partial provenance)."""
    if not anchors:
        raise AnchorError("missing_source", "A material fact needs at least one anchor.")
    return [validate_anchor(a, case_id, sources, segments) for a in anchors]


def _validate_region(region: dict, image_size: tuple[int, int] | None) -> dict | None:
    try:
        x, y = float(region["x"]), float(region["y"])
        width, height = float(region["width"]), float(region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    if image_size and (x + width > image_size[0] or y + height > image_size[1]):
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _validate_range(audio_range: dict, duration_ms: int | None) -> dict | None:
    try:
        start, end = int(audio_range["start"]), int(audio_range["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    if duration_ms is not None and end > duration_ms:
        return None
    return {"start": start, "end": end}


# ---- from services/observations.py ---------------------------------------------------------------
# Server-side interpretation of model proposals: conservative parsing and status mapping.
#
# The model returns raw strings. This module decides what they mean (and when it cannot), using
# the deterministic parsers. Anything uncertain becomes an issue for human review.

STATUS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "successful": ("success", "successful", "paid", "completed", "complete", "debited", "sent", "received", "credited", "posted", "सफल", "safal"),
    "pending": ("pending", "processing", "in progress", "initiated", "awaiting", "frozen", "on hold", "requested", "under process", "लंबित", "lambit"),
    "failed": ("failed", "failure", "declined", "rejected", "unsuccessful", "विफल", "asafal", "असफल"),
}
DIRECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "credit": ("received from", "received", "credited", "refund", "credit", "reversal", "money received", "got back", "returned", "upi/cr", "/cr/", "you received"),
    "debit": ("paid to", "paid", "sent to", "sent", "debited", "debit", "payment to", "transfer to", "upi/dr", "/dr/", "you paid"),
}
REFERENCE_TYPES: dict[str, tuple[str, ...]] = {
    "upi_ref": ("upi ref", "upi reference", "upi txn", "rrn", "upi_ref"),
    "utr": ("utr",),
    "transaction_id": ("transaction id", "txn id", "transaction_id", "txn", "payment id", "transaction"),
    "order_id": ("order id", "order_id", "order"),
}
TRUNCATION_MARKERS = ("…", "...", "xxxx", "****")


def map_status(raw: str | None) -> str:
    if not raw:
        return "unknown"
    text = raw.casefold()
    hits = [status for status, words in STATUS_KEYWORDS.items() if any(w in text for w in words)]
    # "payment failed, amount debited" style conflicts stay unknown rather than guessing
    if len(set(hits)) == 1:
        return hits[0]
    return "unknown"


def map_direction(raw: str | None, kind: str) -> str:
    if kind == "refund_credit":
        return "credit"
    if not raw:
        return "unknown"
    text = raw.casefold()
    if any(w in text for w in DIRECTION_KEYWORDS["credit"]):
        return "credit"
    if any(w in text for w in DIRECTION_KEYWORDS["debit"]):
        return "debit"
    return "unknown"


def map_reference_type(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.casefold()
    for code, words in REFERENCE_TYPES.items():
        if any(w in text for w in words):
            return code
    return "other"


def reference_is_truncated(reference: str | None, issues: list[str]) -> bool:
    if any("truncat" in issue or "partial" in issue for issue in issues):
        return True
    return bool(reference) and any(reference.endswith(m) or reference.startswith(m) for m in TRUNCATION_MARKERS)


def parse_observation(proposal: ObservationProposal) -> dict:
    """Return the parsed view stored on the observation and used to build unreviewed facts."""
    issues = list(proposal.issues)
    amount_minor: str | None = None
    currency: str | None = None
    amount_issue: str | None = None
    if proposal.amount_raw:
        try:
            money = parse_amount(proposal.amount_raw, currency_hint=proposal.currency)
            amount_minor = str(money.amount_minor) if money.amount_minor is not None else None
            currency = money.currency
        except MoneyParseError as exc:
            amount_issue = exc.code
            try:
                currency = detect_currency(proposal.amount_raw) or (proposal.currency.upper() if proposal.currency else None)
            except MoneyParseError:
                currency = None
    else:
        amount_issue = "missing"
        currency = proposal.currency.upper() if proposal.currency else None
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        currency = None

    reference = proposal.reference_raw.strip() if proposal.reference_raw else None
    truncated = reference_is_truncated(reference, issues)
    date = parse_date_text(proposal.date_raw).to_json() if proposal.date_raw else {"rawText": None, "kind": "unresolved", "issue": "missing"}

    return {
        "amountMinor": amount_minor,
        "amountIssue": amount_issue,
        "currency": currency,
        "currencyIssue": None if currency else ("missing" if not proposal.currency else "unrecognised"),
        "reference": reference,
        "referenceIssue": "missing" if not reference else ("truncated" if truncated else None),
        "referenceTruncated": truncated,
        "referenceType": map_reference_type(proposal.reference_type),
        "accountScope": proposal.account_scope_raw.strip() if proposal.account_scope_raw else None,
        "counterparty": proposal.counterparty_raw.strip() if proposal.counterparty_raw else None,
        "status": map_status(proposal.status_raw),
        "statusIssue": None if map_status(proposal.status_raw) != "unknown" else ("missing" if not proposal.status_raw else "unrecognised"),
        "direction": map_direction(proposal.direction_raw, proposal.kind),
        "directionIssue": None if map_direction(proposal.direction_raw, proposal.kind) != "unknown" else ("missing" if not proposal.direction_raw else "unrecognised"),
        "date": date,
        "issues": issues,
    }


def fact_issue(parse_issue: str | None) -> str | None:
    """Map parser codes onto the four fact issue categories."""
    if parse_issue is None:
        return None
    if parse_issue == "missing":
        return "missing"
    if parse_issue in ("ambiguous_symbol", "ambiguous_currency", "ambiguous_multiple", "ambiguous_day_month", "relative", "truncated"):
        return "ambiguous"
    return "unreadable"
