"""The bank connector: one small interface a bank integration implements, so a bank can be added without touching the officer interface.

What this module keeps:
- A provider answers one question: is there a record for this reference, and what does it say. It returns only the fields the bank
  itself returned. Nothing is inferred, filled in or carried over from the case.
- Only an authenticated answer from a provider whose mode is "live" can ever count as bank confirmation. The sandbox provider answers
  from a ledger of fictional entries, is marked simulated on every answer, and is not even built when the sandbox is switched off.
- Credentials are read here, from the environment, by the names `workflow_config.json` declares. They never leave this module: not
  into a log line, an error message, a stored row or a model prompt.
- No endpoint is built in. A live provider is configured with a full HTTPS URL and a token; with either missing it is "not configured".
  The request/response contract below is CyberBrief's adapter contract, not any bank's API. A bank-side gateway has to implement it.

Adapter contract (live provider): POST JSON `{"reference", "amountMinor", "currency", "date"}`, only the fields the provider lists in
`send`; answer 200 with `{"found": bool, "responseReference": str, "transaction": {"amountMinor", "currency", "date", "direction",
"status", "recipientName"}}` where `transaction` carries only what the bank returned. 401/403 is an authentication failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx

from .core import Settings

log = logging.getLogger("cyberbrief.bankconnector")
_ASSETS = Path(__file__).parent / "assets"

RETURNABLE = ("amountMinor", "currency", "date", "direction", "status", "recipientName")   # the only fields a bank answer may carry
SANDBOX_FAULTS = {"SANDBOX-TIMEOUT": "timeout", "SANDBOX-AUTH": "auth_failed", "SANDBOX-UNAVAILABLE": "unavailable", "SANDBOX-INCOMPLETE": "incomplete"}


_VPA = re.compile(r"[A-Za-z0-9._\-]+@[A-Za-z0-9\-]+")


def mask_value(value: Any, keep_start: int = 2, keep_end: int = 3) -> str:
    """A financial identifier for logs, audit rows and prompts: enough to tell two apart, never the whole of it."""
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ""
    if len(text) <= keep_start + keep_end + 2:
        return text[:1] + "•" * max(len(text) - 1, 1)
    return text[:keep_start] + "•" * (len(text) - keep_start - keep_end) + text[len(text) - keep_end:]


def mask_handle(value: Any) -> str:
    """A UPI address, or a name with one in brackets: the first letters before the @ and the handle after it. A plain name is left readable."""
    text = str(value or "").strip()

    def one(m: re.Match[str]) -> str:
        local, _, handle = m.group(0).rpartition("@")
        return mask_value(local, 2, 0) + "@" + handle

    masked = _VPA.sub(one, text)
    if masked != text:
        return masked
    return text if " " in text else mask_value(text, 2, 1)


class ProviderError(Exception):
    """A provider could not give an answer. `kind` is one of auth_failed, timeout, unavailable, incomplete; the message is safe to show and never holds a secret."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class VerifyQuery:
    reference: str
    amount_minor: int | None = None
    currency: str | None = "INR"
    date: str | None = None

    def payload(self, send: tuple[str, ...]) -> dict[str, Any]:
        """Only the fields the configured integration needs, and only those that are known. An unknown value is left out, never guessed."""
        values = {"reference": self.reference, "amountMinor": self.amount_minor, "currency": self.currency, "date": self.date}
        return {k: v for k, v in values.items() if k in send and v not in (None, "")}


@dataclass
class ProviderAnswer:
    found: bool
    response_reference: str
    returned: dict[str, Any] = field(default_factory=dict)   # only RETURNABLE keys, only what the bank returned


class BankProvider(Protocol):
    id: str
    name: str
    mode: str   # "live" | "sandbox"
    simulated: bool
    send: tuple[str, ...]

    def verify(self, query: VerifyQuery) -> ProviderAnswer: ...


def _clean_returned(raw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key in RETURNABLE:
        value = raw.get(key)
        if value in (None, "") or isinstance(value, (dict, list)):
            continue
        out[key] = int(value) if key == "amountMinor" and str(value).isdigit() else str(value)
    return out


@lru_cache
def _ledger() -> dict[str, dict[str, Any]]:
    data = json.loads((_ASSETS / "mock_bank_ledger.json").read_text(encoding="utf-8"))
    return {re.sub(r"\s+", "", e["reference"]).upper(): e for e in data["entries"]}


class SandboxProvider:
    """Answers from a ledger of fictional entries. Every answer is simulated; there is no network call and no credential.

    A reference of the form SANDBOX-TIMEOUT, SANDBOX-AUTH, SANDBOX-UNAVAILABLE or SANDBOX-INCOMPLETE makes it fail in that way, so failure handling
    can be shown without a real bank. These references mean nothing to a live provider."""

    mode = "sandbox"
    simulated = True
    send = ("reference", "amountMinor", "currency", "date")

    def __init__(self, bank_id: str, name: str) -> None:
        self.id, self.name = bank_id, name

    def verify(self, query: VerifyQuery) -> ProviderAnswer:
        key = re.sub(r"\s+", "", query.reference).upper()
        fault = SANDBOX_FAULTS.get(key)
        if fault == "timeout":
            raise ProviderError("timeout", "The simulated bank did not answer in time.")
        if fault == "auth_failed":
            raise ProviderError("auth_failed", "The simulated bank refused the credentials.")
        if fault == "unavailable":
            raise ProviderError("unavailable", "The simulated bank is not available.")
        if fault == "incomplete":
            raise ProviderError("incomplete", "The simulated bank's answer was missing required parts.")
        entry = _ledger().get(key)
        ref = "SIM-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper()
        if entry is None:
            return ProviderAnswer(False, ref)
        return ProviderAnswer(True, ref, _clean_returned({"amountMinor": entry["amountMinor"], "currency": entry["currency"], "date": entry["date"],
                                                          "direction": entry["direction"], "status": entry["status"], "recipientName": entry["counterparty"]}))


class HttpProvider:
    """A live provider behind CyberBrief's adapter contract. Configured only by environment; certificate verification is always on."""

    mode = "live"
    simulated = False

    def __init__(self, bank_id: str, name: str, url: str, token: str, send: tuple[str, ...], timeout: float,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.id, self.name, self._url, self._token, self.send, self._timeout, self._transport = bank_id, name, url, token, send, timeout, transport

    def verify(self, query: VerifyQuery) -> ProviderAnswer:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport, verify=True) as client:
                res = client.post(self._url, json=query.payload(self.send), headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"})
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"{self.name} did not answer in time.") from exc
        except httpx.HTTPError as exc:   # never include the exception text: it can carry the URL
            log.warning("provider %s unreachable (%s)", self.id, type(exc).__name__)
            raise ProviderError("unavailable", f"{self.name} could not be reached.") from exc
        if res.status_code in (401, 403):
            raise ProviderError("auth_failed", f"{self.name} did not accept CyberBrief's credentials.")
        if res.status_code >= 500 or res.status_code == 429:
            raise ProviderError("unavailable", f"{self.name} is not available right now.")
        if res.status_code != 200:
            raise ProviderError("incomplete", f"{self.name} answered with an unexpected status.")
        try:
            body = res.json()
        except ValueError as exc:
            raise ProviderError("incomplete", f"{self.name}'s answer could not be read.") from exc
        if not isinstance(body, dict) or not isinstance(body.get("found"), bool):
            raise ProviderError("incomplete", f"{self.name}'s answer did not say whether a record was found.")
        ref = body.get("responseReference")
        if not isinstance(ref, str) or not ref.strip():
            raise ProviderError("incomplete", f"{self.name}'s answer carried no response reference.")
        if not body["found"]:
            return ProviderAnswer(False, ref.strip()[:80])
        returned = _clean_returned(body.get("transaction"))
        if not returned:
            raise ProviderError("incomplete", f"{self.name} reported a record but returned none of its details.")
        return ProviderAnswer(True, ref.strip()[:80], returned)


# ---- the registry ------------------------------------------------------------------------------------------------------

@dataclass
class Registry:
    providers: dict[str, BankProvider]
    banks: dict[str, dict[str, Any]]
    not_configured: dict[str, str]   # bank id -> why a declared live integration is unusable (names no secret)

    def for_bank(self, bank_id: str | None) -> BankProvider | None:
        return self.providers.get(bank_id or "")


def build_registry(settings: Settings, banks: list[dict[str, Any]], transport: httpx.BaseTransport | None = None) -> Registry:
    providers: dict[str, BankProvider] = {}
    not_configured: dict[str, str] = {}
    for bank in banks:
        spec = bank.get("integration") or {}
        kind = spec.get("type")
        if kind == "sandbox":
            if settings.bank_sandbox_enabled:
                providers[bank["id"]] = SandboxProvider(bank["id"], bank["name"])
            else:
                not_configured[bank["id"]] = "The sandbox adapter is switched off in this deployment."
        elif kind == "http":
            url, token = os.environ.get(spec.get("urlEnv", ""), "").strip(), os.environ.get(spec.get("tokenEnv", ""), "").strip()
            if not url or not token:
                not_configured[bank["id"]] = "No verification integration is configured for this bank."
            elif not (url.startswith("https://") or url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
                not_configured[bank["id"]] = "The configured verification address is not HTTPS."
            else:
                send = tuple(k for k in spec.get("send", ["reference", "amountMinor", "date"]) if k in ("reference", "amountMinor", "currency", "date"))
                providers[bank["id"]] = HttpProvider(bank["id"], bank["name"], url, token, send or ("reference",), settings.bank_provider_timeout_seconds, transport)
        else:
            not_configured[bank["id"]] = "No verification integration is configured for this bank."
    return Registry(providers, {b["id"]: b for b in banks}, not_configured)


_TEST_REGISTRY: Registry | None = None


def set_registry_for_tests(registry: Registry | None) -> None:
    global _TEST_REGISTRY
    _TEST_REGISTRY = registry


def get_registry(settings: Settings, banks: list[dict[str, Any]]) -> Registry:
    return _TEST_REGISTRY if _TEST_REGISTRY is not None else build_registry(settings, banks)
