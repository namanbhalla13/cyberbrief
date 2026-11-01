"""Runtime configuration, the error contract, logging and the database session factory."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---- from config.py ------------------------------------------------------------------------------
# Runtime configuration.
#
# All values come from the environment (optionally via `Cyberbrief/.env`, which is gitignored).
# Secrets are `SecretStr` so they never appear in reprs, logs, or error messages.

# backend/app/config.py -> Cyberbrief/
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"),),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    demo_mode: bool = True
    public_fixtures_only: bool = False

    # Meta Model API (server side only)
    model_api_key: SecretStr = SecretStr("")
    meta_base_url: str = "https://api.meta.ai/v1"
    meta_text_model: str = "muse-spark-1.3"
    meta_speech_model: str = "muse-voice-transcribe-1.0"
    ai_mode: Literal["live", "fixture"] = "live"

    # persistence
    database_url: str = "sqlite:///./data/cyberbrief.db"
    storage_root: Path = Path("./data/private")
    checkpoint_db: Path = Path("./data/checkpoints.db")

    # application limits (proposed app limits, not provider limits)
    max_images_per_case: int = 10
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_audio_seconds_per_update: int = 120
    max_audio_recordings_per_case: int = 3
    max_audio_seconds_per_case: int = 360
    max_typed_chars: int = 10_000
    max_case_updates: int = 20
    max_observations_per_case: int = 50
    model_concurrency: int = 2
    model_timeout_seconds: float = 90
    speech_timeout_seconds: float = 120
    model_max_retries: int = 2
    case_model_call_budget: int = 40
    session_case_quota: int = 10
    demo_retention_hours: int = 24
    retention_check_minutes: int = 60  # 0 disables the periodic purge (startup purge still runs)
    max_request_bytes: int = 12 * 1024 * 1024

    audio_converter: Literal["none", "ffmpeg"] = "none"

    # jobs and fixtures
    job_runner: Literal["thread", "inline"] = "thread"
    fixture_roots: Annotated[list[Path], NoDecode] = [
        REPO_ROOT / "evals" / "fixtures" / "development",
        REPO_ROOT / "evals" / "fixtures" / "holdout",
    ]

    # web
    allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    cookie_secure: bool = False
    session_ttl_hours: int = 24

    # sign-in. The one-time code is a demo login for CyberBrief itself: no SMS is sent, and it
    # is never a code from a bank or from the official cybercrime portal.
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_seconds: int = 60
    otp_hourly_limit: int = 5
    #: show the generated code in the response so the demo can be completed without an SMS
    #: provider. `validate_runtime` forces this off whenever `demo_mode` is false.
    demo_otp_visible: bool = True

    # bank verification and the officer's email to a bank. No value here is a credential except the SMTP password, which is read from the
    # environment only. Bank endpoints and tokens are named in workflow_config.json and read by bankconnector; nothing is built in.
    #: the sandbox adapter answers from fictional records. `validate_runtime` forces it off whenever `demo_mode` is false.
    bank_sandbox_enabled: bool = True
    bank_provider_timeout_seconds: float = 10
    bank_provider_retries: int = 1   # extra attempts after a timeout or an unavailable provider, within one click
    bank_check_lease_seconds: int = 120   # a check still "checking" after this long is treated as abandoned
    email_mode: Literal["off", "smtp"] = "off"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 15
    email_from_address: str = ""
    # the officer's official contact details as they appear in a request to a bank; empty means "not configured" and a live send is refused
    official_department: str = ""
    official_contact_email: str = ""
    official_contact_phone: str = ""

    # citizen chat assistant. It works on the citizen's own CyberBrief complaint only, proposes
    # buttons rather than acting, and never points to an outside service.
    assistant_max_messages_per_conversation: int = 80
    assistant_daily_model_calls_per_account: int = 300
    assistant_unlinked_retention_hours: int = 24  # chats that never became a complaint
    assistant_max_completion_tokens: int = 6000
    #: sent only when set; not yet verified against this API's Chat Completions endpoint
    assistant_reasoning_effort: str | None = None
    assistant_fixture_dir: Path = REPO_ROOT / "evals" / "fixtures" / "assistant"

    # optional tracing, off by default
    langsmith_tracing: bool = False
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    langsmith_api_key: SecretStr = SecretStr("")

    @field_validator("allowed_origins", "fixture_roots", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _finalise(self) -> Settings:
        if not self.storage_root.is_absolute():
            self.storage_root = (REPO_ROOT / self.storage_root).resolve()
        if not self.checkpoint_db.is_absolute():
            self.checkpoint_db = (REPO_ROOT / self.checkpoint_db).resolve()
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            raw = Path(self.database_url[len(prefix) :])
            if not raw.is_absolute():
                self.database_url = prefix + (REPO_ROOT / raw).resolve().as_posix()

        return self

    def validate_runtime(self) -> Settings:
        """Startup checks that must fail loudly.

        Raised outside pydantic validation on purpose: a pydantic ValidationError echoes the
        input values, which would include the API key.
        """
        if self.ai_mode == "live" and not self.model_api_key.get_secret_value().strip():
            raise ConfigurationError(
                "AI_MODE=live but MODEL_API_KEY is empty. Set it in Cyberbrief/.env, or run with "
                "AI_MODE=fixture (fixture mode is labelled and never counts as a live demonstration)."
            )
        for model_id in (self.meta_text_model, self.meta_speech_model):
            if "contributor" in model_id.lower():
                raise ConfigurationError(
                    f"Model '{model_id}' is a Contributor model. "
                    "CyberBrief permits Standard models only."
                )
        # A one-time code shown on screen is a demo affordance. The switch is `demo_mode`, not
        # `app_env`: the compose demo runs with APP_ENV=production to disable reload and dev
        # CORS while still being a demo. A real deployment sets DEMO_MODE=false, which turns
        # this off and makes /otp/request fail loudly until an SMS provider exists.
        if not self.demo_mode:
            self.demo_otp_visible = False
            self.bank_sandbox_enabled = False   # fictional bank answers are never available outside a demo
        return self

    @property
    def is_live(self) -> bool:
        return self.ai_mode == "live"


class ConfigurationError(RuntimeError):
    """Invalid runtime configuration. Messages never contain secret values."""


@lru_cache
def get_settings() -> Settings:
    return Settings().validate_runtime()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


# ---- from errors.py ------------------------------------------------------------------------------
# Uniform API error shape: {error, message, nextStep, details}. Never echoes provider payloads.

class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        next_step: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.next_step = next_step
        self.details = details or {}


def error_body(
    code: str, message: str, next_step: str | None = None, details: dict | None = None
) -> dict[str, Any]:
    return {"error": code, "message": message, "nextStep": next_step, "details": details or {}}


_STATUS_CODES = {
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "stale_revision",
    413: "limit_exceeded",
    429: "rate_limited",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.next_step, exc.details), status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Deliberately drop "input": request bodies may contain evidence text.
        fields = [
            {
                "loc": [str(part) for part in err.get("loc", [])],
                "type": err.get("type"),
                "msg": err.get("msg"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body(
                "invalid_request",
                "The request did not match the API contract.",
                "Check the listed fields and try again.",
                {"fields": fields},
            ),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(error_body(code, detail), status_code=exc.status_code, headers=exc.headers)


# ---- from logging_config.py ----------------------------------------------------------------------
# Metadata-only logging. Secret values are redacted defensively; bodies are never logged.

class RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[redacted]")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    redact = RedactSecrets(
        [settings.model_api_key.get_secret_value(), settings.langsmith_api_key.get_secret_value()]
    )
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        logger = logging.getLogger(name)
        logger.addFilter(redact)
        for handler in logger.handlers:
            handler.addFilter(redact)


# ---- from db.py ----------------------------------------------------------------------------------
# SQLAlchemy engine and session helpers. SQLite with WAL and foreign keys enforced.

class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        is_sqlite = settings.database_url.startswith("sqlite")
        connect_args = {"check_same_thread": False, "timeout": 5} if is_sqlite else {}
        _engine = create_engine(settings.database_url, connect_args=connect_args)
        if is_sqlite:

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def make_session() -> Session:
    get_engine()
    assert _session_factory is not None
    return _session_factory()


def get_db() -> Iterator[Session]:
    db = make_session()
    try:
        yield db
    finally:
        db.close()


def reset_engine() -> None:
    """Test helper: drop the cached engine so new settings take effect."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
