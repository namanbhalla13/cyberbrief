"""The private file store and the versioned fact log every other layer writes through."""

from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .core import ApiError, Settings
from .models import Case, CaseRevision, Fact, ReviewEvent, utcnow
from .parsing import sha256_bytes

# ---- from services/storage.py --------------------------------------------------------------------
# Private file storage: signature checks, safe decode, byte/pixel caps, generated names.
#
# Originals are written once and never rewritten. Paths are generated from the case id and the
# content hash and always resolved inside STORAGE_ROOT; uploaded filenames are display-only.

ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
THUMBNAIL_MAX = 360


@dataclass(frozen=True)
class StoredImage:
    storage_key: str
    sha256: str
    byte_size: int
    media_type: str
    width: int
    height: int
    thumbnail_key: str | None


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sanitise_filename(name: str | None, fallback: str = "upload") -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", base).strip(" .")
    base = re.sub(r"\s+", " ", base)
    return (base[:120] or fallback)


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.storage_root.resolve()
        self.settings = settings
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- paths ------------------------------------------------------------------------
    def resolve(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        if self.root not in path.parents:
            raise ApiError("not_found", "Stored file not found.", 404)
        return path

    def read(self, storage_key: str) -> bytes:
        path = self.resolve(storage_key)
        if not path.is_file():
            raise ApiError("not_found", "Stored file not found.", 404)
        return path.read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self.resolve(storage_key).is_file()

    def _write_once(self, storage_key: str, data: bytes) -> None:
        path = self.resolve(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)

    # ---- images -----------------------------------------------------------------------
    def store_image(self, case_id: str, data: bytes, declared_type: str | None) -> StoredImage:
        if len(data) == 0:
            raise ApiError("invalid_file", "The upload is empty.", 400)
        if len(data) > self.settings.max_image_bytes:
            raise ApiError(
                "limit_exceeded",
                "The image is larger than this deployment accepts.",
                413,
                f"Images must be at most {self.settings.max_image_bytes // (1024 * 1024)} MB.",
            )
        sniffed = sniff_image_type(data)
        if sniffed is None or sniffed not in ALLOWED_IMAGE_TYPES:
            raise ApiError(
                "invalid_file",
                "Only JPEG, PNG and WebP images are accepted in this release.",
                400,
                "Export the screenshot as PNG or JPEG and try again.",
            )
        if declared_type and declared_type.split(";")[0].strip().lower() not in (sniffed, "image/jpg", "application/octet-stream"):
            # declared type disagrees with the bytes: trust the bytes, but do not accept lies about type
            if declared_type.split(";")[0].strip().lower().startswith("image/"):
                raise ApiError("invalid_file", "The declared image type does not match the file contents.", 400)

        Image.MAX_IMAGE_PIXELS = self.settings.max_image_pixels
        try:
            with Image.open(io.BytesIO(data)) as probe:
                width, height = probe.size
                if width * height > self.settings.max_image_pixels:
                    raise ApiError("limit_exceeded", "The image has too many pixels.", 413)
                probe.load()  # full decode catches truncated or malformed files
        except ApiError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise ApiError(
                "invalid_file", "The image could not be decoded.", 400,
                "Re-save the screenshot and upload it again.",
            ) from exc

        digest = sha256_bytes(data)
        ext = ALLOWED_IMAGE_TYPES[sniffed]
        storage_key = f"{case_id}/originals/{digest}.{ext}"
        self._write_once(storage_key, data)
        thumbnail_key = self._write_thumbnail(case_id, digest, data)
        return StoredImage(storage_key, digest, len(data), sniffed, width, height, thumbnail_key)

    def _write_thumbnail(self, case_id: str, digest: str, data: bytes) -> str | None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image = image.convert("RGB")
                image.thumbnail((THUMBNAIL_MAX, THUMBNAIL_MAX))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=82)
        except OSError:
            return None
        key = f"{case_id}/derived/{digest}_thumb.jpg"
        self._write_once(key, buffer.getvalue())
        return key

    # ---- audio ----------------------------------------------------------------------------
    def store_audio(self, case_id: str, data: bytes) -> tuple[str, str, int]:
        digest = sha256_bytes(data)
        key = f"{case_id}/originals/{digest}.wav"
        self._write_once(key, data)
        return key, digest, len(data)

    # ---- text and exports -----------------------------------------------------------------
    def store_text(self, case_id: str, text: str, kind: str) -> tuple[str, str, int]:
        data = text.encode("utf-8")
        digest = sha256_bytes(data)
        key = f"{case_id}/originals/{digest}.{kind}.txt"
        self._write_once(key, data)
        return key, digest, len(data)

    def store_export(self, case_id: str, export_id: str, fmt: str, data: bytes) -> str:
        key = f"{case_id}/exports/{export_id}.{fmt}"
        self._write_once(key, data)
        return key

    # ---- deletion -------------------------------------------------------------------------
    def purge_case(self, case_id: str) -> None:
        """Remove every original, derivative and export for one case."""
        path = (self.root / case_id).resolve()
        if self.root in path.parents and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


# ---- from services/revisions.py ------------------------------------------------------------------
# Append-only revisions and fact versioning. Every commit of accepted facts bumps the revision.

def check_expected_revision(case: Case, expected: int | None) -> None:
    if expected is not None and expected != case.current_revision:
        raise ApiError(
            "stale_revision",
            f"The case is at revision {case.current_revision}, not {expected}.",
            409,
            "Reload the case and apply your change to the latest revision.",
        )


def bump_revision(
    db: Session,
    case: Case,
    *,
    kind: str,
    summary: str,
    actor_session_id: str | None,
    actor_role: str,
) -> int:
    case.current_revision += 1
    case.updated_at = utcnow()
    db.add(
        CaseRevision(
            case_id=case.id,
            revision=case.current_revision,
            kind=kind,
            summary=summary,
            actor_session_id=actor_session_id,
            actor_role=actor_role,
        )
    )
    db.flush()
    return case.current_revision


def current_fact(db: Session, fact_id: str) -> Fact | None:
    return db.scalar(select(Fact).where(Fact.fact_id == fact_id, Fact.is_current.is_(True)))


def current_facts(db: Session, case_id: str, subject_type: str, subject_id: str) -> dict[str, Fact]:
    rows = db.scalars(
        select(Fact).where(
            Fact.case_id == case_id,
            Fact.subject_type == subject_type,
            Fact.subject_id == subject_id,
            Fact.is_current.is_(True),
        )
    ).all()
    return {row.kind: row for row in rows}


def new_fact(
    db: Session,
    *,
    case_id: str,
    subject_type: str,
    subject_id: str,
    kind: str,
    value: dict | None,
    raw_text: str | None,
    anchors: list[dict],
    revision: int,
    issue: str | None = None,
    review_state: str = "unreviewed",
    source_observation_id: str | None = None,
) -> Fact:
    fact = Fact(
        case_id=case_id,
        subject_type=subject_type,
        subject_id=subject_id,
        kind=kind,
        value=value,
        raw_text=raw_text,
        anchors=anchors,
        issue=issue,
        review_state=review_state,
        created_revision=revision,
        source_observation_id=source_observation_id,
    )
    db.add(fact)
    db.flush()
    return fact


def supersede_fact(
    db: Session,
    previous: Fact,
    *,
    revision: int,
    review_state: str,
    actor_role: str,
    value: dict | None = None,
    raw_text: str | None = None,
    anchors: list[dict] | None = None,
    issue: str | None = None,
    reason: str | None = None,
    keep_value: bool = True,
) -> Fact:
    """Create version n+1 of a fact. The previous version stays readable in history."""
    if not previous.is_current:
        raise ApiError(
            "review_conflict",
            "This fact version has already been superseded.",
            409,
            "Reload the case and review the current version.",
        )
    previous.is_current = False
    nxt = Fact(
        fact_id=previous.fact_id,
        version=previous.version + 1,
        case_id=previous.case_id,
        subject_type=previous.subject_type,
        subject_id=previous.subject_id,
        kind=previous.kind,
        value=previous.value if (keep_value and value is None) else value,
        raw_text=previous.raw_text if (keep_value and raw_text is None) else raw_text,
        anchors=previous.anchors if anchors is None else anchors,
        review_state=review_state,
        issue=issue,
        supersedes_version=previous.version,
        is_current=True,
        reviewed_by=actor_role,
        reviewed_at=utcnow(),
        reason=reason,
        source_observation_id=previous.source_observation_id,
        created_revision=revision,
    )
    db.add(nxt)
    db.flush()
    return nxt


def record_review_event(
    db: Session,
    *,
    case_id: str,
    revision: int,
    actor_session_id: str,
    actor_role: str,
    action: str,
    target_type: str,
    target_id: str,
    payload: dict | None = None,
) -> ReviewEvent:
    event = ReviewEvent(
        case_id=case_id,
        revision=revision,
        actor_session_id=actor_session_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
    )
    db.add(event)
    return event
