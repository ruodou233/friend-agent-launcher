#!/usr/bin/env python3
"""Small Friend V1A gateway reference implementation.

This service is deliberately dependency-free and reference/mock only.  The
public surface is limited to the four routes in contracts/friend-api.openapi.json.
Production New API behavior is an explicit adapter boundary; this file does
not infer vendor endpoints or silently fall back to mock mode.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
from urllib.parse import parse_qs, urlsplit


PRODUCT = "claude"
PROTOCOL = "anthropic-messages"
FIXED_GATEWAY_REF = "friend-fixed-gateway"
CATALOG_TRUST_BOUNDARY = "tls-fixed-gateway"
BALANCE_SOURCE = "new-api"
MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_UPSTREAM_BODY_BYTES = 4 * 1024 * 1024

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
MODEL_REF_RE = re.compile(r"^friend-model:[A-Za-z0-9][A-Za-z0-9._:-]{0,110}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

CATALOG_ENTRY_KEYS = {
    "product",
    "protocol",
    "canonical_id",
    "model_ref",
    "gateway_ref",
    "display_name",
    "capabilities",
    "default",
    "catalog_version",
    "expires_at",
    "billing_label",
}
CATALOG_FILE_KEYS = {"catalog_version", "expires_at", "catalog"}
MESSAGE_KEYS = {
    "model",
    "max_tokens",
    "messages",
    "system",
    "tools",
    "stream",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "metadata",
}
FORBIDDEN_IDENTITY_HEADERS = {
    "x-account-id",
    "x-install-id",
    "x-friend-account-id",
    "x-friend-install-id",
}
SAFE_UPSTREAM_RESPONSE_HEADERS = frozenset({"content-type"})
ALLOWED_LOG_PATHS = frozenset(
    {
        "/healthz",
        "/v1/friend/preflight",
        "/v1/friend/catalog",
        "/v1/friend/balance",
        "/v1/messages",
    }
)
UNKNOWN_LOG_PATH = "<unmatched>"


class GatewayError(Exception):
    """An expected request or adapter failure with a safe public response."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class GatewayConfigurationError(RuntimeError):
    """Configuration is invalid; fail before serving requests."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GatewayConfigurationError("date-time must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GatewayConfigurationError("date-time is invalid") from error
    if parsed.tzinfo is None:
        raise GatewayConfigurationError("date-time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        raise GatewayError(400, "INVALID_REQUEST", "X-Request-Id 格式无效")
    return value


def _new_request_id() -> str:
    return "gw-" + uuid.uuid4().hex


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GatewayError(400, "INVALID_REQUEST", "JSON 请求格式无效") from error
    if not isinstance(value, dict):
        raise GatewayError(400, "INVALID_REQUEST", "JSON 请求必须是对象")
    return value


def _require_string(value: Any, field: str, minimum: int = 1, maximum: int | None = None) -> str:
    if not isinstance(value, str) or len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise GatewayError(400, "INVALID_REQUEST", f"字段 {field} 格式无效")
    return value


def _validate_opaque(value: Any, field: str, maximum: int) -> str:
    text = _require_string(value, field, 1, maximum)
    if not OPAQUE_RE.fullmatch(text):
        raise GatewayError(400, "INVALID_REQUEST", f"字段 {field} 格式无效")
    return text


def _validate_identity(value: Any, field: str) -> str:
    text = _require_string(value, field, 1, 128)
    if not IDENTITY_RE.fullmatch(text):
        raise GatewayConfigurationError(f"{field} is invalid")
    return text


@dataclass(frozen=True)
class KeyBinding:
    key_sha256: str
    account_id: str
    install_id: str
    status: str = "active"
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not SHA256_RE.fullmatch(self.key_sha256):
            raise GatewayConfigurationError("key_sha256 must be lowercase SHA-256")
        _validate_identity(self.account_id, "account_id")
        _validate_identity(self.install_id, "install_id")
        if self.status not in {"active", "revoked", "expired"}:
            raise GatewayConfigurationError("key status is invalid")
        if self.expires_at is not None:
            _parse_datetime(self.expires_at)


def sha256_key(key: str) -> str:
    if not isinstance(key, str) or not key or any(char in key for char in "\r\n\0"):
        raise ValueError("key is invalid")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyBindingStore:
    """Server-side key hash to account/install binding.

    Plaintext bearer keys are never stored in this class or read from a
    binding file.  A caller-controlled account/install value is not consulted.
    """

    def __init__(self, bindings: Iterable[KeyBinding]):
        self._bindings: dict[str, KeyBinding] = {}
        for binding in bindings:
            if binding.key_sha256 in self._bindings:
                raise GatewayConfigurationError("duplicate key binding")
            self._bindings[binding.key_sha256] = binding

    @classmethod
    def from_file(cls, path: str | Path) -> "KeyBindingStore":
        file_path = Path(path)
        if file_path.is_symlink():
            raise GatewayConfigurationError("key binding file must not be a symlink")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayConfigurationError("key binding file cannot be read") from error
        if isinstance(payload, dict):
            if set(payload) != {"bindings"} or not isinstance(payload["bindings"], list):
                raise GatewayConfigurationError("key binding file shape is invalid")
            payload = payload["bindings"]
        if not isinstance(payload, list):
            raise GatewayConfigurationError("key binding file must contain a list")
        bindings: list[KeyBinding] = []
        for item in payload:
            if not isinstance(item, dict):
                raise GatewayConfigurationError("key binding record is invalid")
            allowed = {"key_sha256", "account_id", "install_id", "status", "expires_at"}
            if not set(item).issubset(allowed) or not {"key_sha256", "account_id", "install_id"}.issubset(item):
                raise GatewayConfigurationError("key binding record fields are invalid")
            bindings.append(KeyBinding(**item))
        return cls(bindings)

    @classmethod
    def for_mock_key(cls, key: str = "local-mock-friend-key") -> "KeyBindingStore":
        return cls(
            [
                KeyBinding(
                    key_sha256=sha256_key(key),
                    account_id="acct-mock",
                    install_id="install-mock",
                )
            ]
        )

    def resolve(self, authorization: str | None) -> KeyBinding:
        if not isinstance(authorization, str):
            raise GatewayError(401, "AUTH_REQUIRED", "需要 Bearer Friend Key")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            raise GatewayError(401, "AUTH_REQUIRED", "需要 Bearer Friend Key")
        token = parts[1]
        if any(char in token for char in "\r\n\0"):
            raise GatewayError(401, "AUTH_REQUIRED", "需要 Bearer Friend Key")
        binding = self._bindings.get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if binding is None:
            raise GatewayError(401, "AUTH_REQUIRED", "Friend Key 无效")
        if binding.status == "revoked":
            raise GatewayError(403, "KEY_REVOKED", "Friend Key 已撤销")
        if binding.status == "expired":
            raise GatewayError(403, "KEY_EXPIRED", "Friend Key 已过期")
        if binding.expires_at is not None and _parse_datetime(binding.expires_at) <= _utc_now():
            raise GatewayError(403, "KEY_EXPIRED", "Friend Key 已过期")
        return binding


@dataclass(frozen=True)
class BalanceSnapshot:
    amount_minor: int
    currency: str
    as_of: str
    source: str = BALANCE_SOURCE

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int) or self.amount_minor < 0:
            raise GatewayConfigurationError("balance amount_minor is invalid")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise GatewayConfigurationError("balance currency is invalid")
        if self.source != BALANCE_SOURCE:
            raise GatewayConfigurationError("balance source must be new-api")
        _parse_datetime(self.as_of)

    def wire(self) -> dict[str, Any]:
        return {
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "as_of": self.as_of,
            "source": self.source,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "BalanceSnapshot":
        if not isinstance(value, dict) or set(value) != {"amount_minor", "currency", "as_of", "source"}:
            raise GatewayConfigurationError("balance wire shape is invalid")
        return cls(**value)


@dataclass(frozen=True)
class CatalogEntry:
    product: str
    protocol: str
    canonical_id: str
    model_ref: str
    gateway_ref: str
    display_name: str
    capabilities: tuple[str, ...]
    default: bool
    catalog_version: str
    expires_at: str
    billing_label: str

    def __post_init__(self) -> None:
        if self.product != PRODUCT or self.protocol != PROTOCOL:
            raise GatewayConfigurationError("catalog product/protocol is invalid")
        if not re.fullmatch(r"v1a-[A-Za-z0-9][A-Za-z0-9._:-]{0,59}", self.catalog_version):
            raise GatewayConfigurationError("catalog_version is invalid")
        if self.gateway_ref != FIXED_GATEWAY_REF:
            raise GatewayConfigurationError("gateway_ref is invalid")
        _validate_opaque(self.canonical_id, "canonical_id", 128)
        if not MODEL_REF_RE.fullmatch(self.model_ref) or len(self.model_ref) > 128:
            raise GatewayConfigurationError("model_ref is invalid")
        if not isinstance(self.display_name, str) or not 1 <= len(self.display_name) <= 128:
            raise GatewayConfigurationError("display_name is invalid")
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise GatewayConfigurationError("catalog capabilities are invalid")
        if set(self.capabilities) - {"streaming", "tool_use"} or "streaming" not in self.capabilities:
            raise GatewayConfigurationError("catalog capabilities are invalid")
        if not isinstance(self.default, bool):
            raise GatewayConfigurationError("catalog default is invalid")
        if not isinstance(self.billing_label, str) or not 1 <= len(self.billing_label) <= 128:
            raise GatewayConfigurationError("billing_label is invalid")
        _parse_datetime(self.expires_at)

    def wire(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "protocol": self.protocol,
            "canonical_id": self.canonical_id,
            "model_ref": self.model_ref,
            "gateway_ref": self.gateway_ref,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "default": self.default,
            "catalog_version": self.catalog_version,
            "expires_at": self.expires_at,
            "billing_label": self.billing_label,
        }

    @classmethod
    def from_wire(cls, value: Any) -> "CatalogEntry":
        if not isinstance(value, dict) or set(value) != CATALOG_ENTRY_KEYS:
            raise GatewayConfigurationError("catalog entry wire shape is invalid")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise GatewayConfigurationError("catalog capabilities are invalid")
        return cls(
            product=value["product"],
            protocol=value["protocol"],
            canonical_id=value["canonical_id"],
            model_ref=value["model_ref"],
            gateway_ref=value["gateway_ref"],
            display_name=value["display_name"],
            capabilities=tuple(capabilities),
            default=value["default"],
            catalog_version=value["catalog_version"],
            expires_at=value["expires_at"],
            billing_label=value["billing_label"],
        )


def normalize_catalog_entries(entries: Iterable[CatalogEntry]) -> tuple[CatalogEntry, ...]:
    """Keep exact duplicates once; conflicting names fail closed.

    A duplicate canonical_id or model_ref is considered the same name.  An
    exact duplicate is dropped after the first occurrence.  If any field
    differs, selecting one would be ambiguous, so the server rejects the
    catalog instead of silently choosing a model.
    """

    result: list[CatalogEntry] = []
    by_canonical: dict[str, CatalogEntry] = {}
    by_model_ref: dict[str, CatalogEntry] = {}
    defaults = 0
    for entry in entries:
        existing = by_canonical.get(entry.canonical_id)
        if existing is not None:
            if existing.wire() == entry.wire():
                continue
            raise GatewayConfigurationError("catalog has conflicting duplicate canonical_id")
        existing = by_model_ref.get(entry.model_ref)
        if existing is not None:
            if existing.wire() == entry.wire():
                continue
            raise GatewayConfigurationError("catalog has conflicting duplicate model_ref")
        by_canonical[entry.canonical_id] = entry
        by_model_ref[entry.model_ref] = entry
        result.append(entry)
        if entry.default:
            defaults += 1
            if defaults > 1:
                raise GatewayConfigurationError("catalog may have only one default entry")
    if not result:
        raise GatewayConfigurationError("catalog must not be empty")
    return tuple(result)


@dataclass(frozen=True)
class CatalogSnapshot:
    catalog_version: str
    expires_at: str
    entries: tuple[CatalogEntry, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"v1a-[A-Za-z0-9][A-Za-z0-9._:-]{0,59}", self.catalog_version):
            raise GatewayConfigurationError("catalog_version is invalid")
        _parse_datetime(self.expires_at)
        if any(entry.catalog_version != self.catalog_version or entry.expires_at != self.expires_at for entry in self.entries):
            raise GatewayConfigurationError("catalog entry metadata does not match snapshot")
        normalized = normalize_catalog_entries(self.entries)
        if normalized != self.entries:
            raise GatewayConfigurationError("catalog entries must already be normalized")

    @classmethod
    def from_file(cls, path: str | Path) -> "CatalogSnapshot":
        file_path = Path(path)
        if file_path.is_symlink():
            raise GatewayConfigurationError("catalog file must not be a symlink")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayConfigurationError("catalog file cannot be read") from error
        if not isinstance(payload, dict) or set(payload) != CATALOG_FILE_KEYS or not isinstance(payload["catalog"], list):
            raise GatewayConfigurationError("catalog file shape is invalid")
        entries = tuple(CatalogEntry.from_wire(item) for item in payload["catalog"])
        normalized = normalize_catalog_entries(entries)
        return cls(payload["catalog_version"], payload["expires_at"], normalized)

    def wire_entries(self) -> list[dict[str, Any]]:
        return [entry.wire() for entry in self.entries]


class CatalogAdapter(Protocol):
    """Explicit server-side catalog source boundary."""

    def get_catalog(self, binding: KeyBinding | None) -> CatalogSnapshot:
        ...


class BalanceAdapter(Protocol):
    """Explicit New API balance source boundary."""

    def get_balance(self, binding: KeyBinding, request_id: str) -> BalanceSnapshot:
        ...


class MessagesAdapter(Protocol):
    """Explicit single-route generation forwarding boundary."""

    def forward(
        self,
        binding: KeyBinding,
        request_id: str,
        body: bytes,
        stream: bool,
    ) -> "AdapterResponse":
        ...


class StaticCatalogAdapter:
    def __init__(self, snapshot: CatalogSnapshot):
        self.snapshot = snapshot

    def get_catalog(self, binding: KeyBinding | None) -> CatalogSnapshot:
        return self.snapshot


class MockBalanceAdapter:
    def __init__(self, amount_minor: int = 10000):
        self.snapshot = BalanceSnapshot(amount_minor, "CNY", _rfc3339(_utc_now()))
        self.last_binding: KeyBinding | None = None

    def get_balance(self, binding: KeyBinding, request_id: str) -> BalanceSnapshot:
        self.last_binding = binding
        return self.snapshot


@dataclass
class AdapterResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes | None = None
    reader: Callable[[int], bytes] | None = None
    closer: Callable[[], None] | None = None

    def iter_body(self) -> Iterator[bytes]:
        if self.body is not None:
            if self.body:
                yield self.body
            return
        if self.reader is None:
            return
        while True:
            chunk = self.reader(64 * 1024)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        if self.closer is not None:
            self.closer()


class MockMessagesAdapter:
    def __init__(self):
        self.last_binding: KeyBinding | None = None
        self.calls = 0

    def forward(
        self,
        binding: KeyBinding,
        request_id: str,
        body: bytes,
        stream: bool,
    ) -> AdapterResponse:
        self.last_binding = binding
        self.calls += 1
        request = _parse_json_object(body)
        response = {
            "id": "msg_mock_" + request_id,
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": [{"type": "text", "text": "Friend gateway mock response"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": len(body), "output_tokens": 5},
        }
        if not stream:
            encoded = _json_bytes(response)
            return AdapterResponse(200, [("Content-Type", "application/json; charset=utf-8")], body=encoded)
        events = [
            "event: message_start\n" + "data: " + json.dumps({"type": "message_start", "message": response}, ensure_ascii=False) + "\n\n",
            "event: message_stop\n" + "data: {\"type\":\"message_stop\"}\n\n",
        ]
        return AdapterResponse(200, [("Content-Type", "text/event-stream")], body="".join(events).encode("utf-8"))


def _validated_http_url(raw: str, field: str) -> tuple[str, str, int | None, str]:
    if not isinstance(raw, str) or not raw:
        raise GatewayConfigurationError(f"{field} is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GatewayConfigurationError(f"{field} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GatewayConfigurationError(f"{field} must not contain credentials or query parameters")
    path = parsed.path.rstrip("/")
    return parsed.scheme, parsed.hostname, parsed.port, path


def _http_connection(raw: str, field: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    scheme, hostname, port, path = _validated_http_url(raw, field)
    connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    connection = connection_type(hostname, port=port, timeout=timeout)
    return connection, path


def _contains_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _safe_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate every upstream header before applying the forwarding allowlist."""

    validated: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        iterator = iter(headers)
    except TypeError as error:
        raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 响应头无效", False) from error
    for header in iterator:
        if not isinstance(header, (tuple, list)) or len(header) != 2:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 响应头无效", False)
        name, value = header
        if not isinstance(name, str) or not isinstance(value, str):
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 响应头无效", False)
        if _contains_control(name) or _contains_control(value) or HEADER_NAME_RE.fullmatch(name) is None:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 响应头无效", False)
        normalized_name = name.casefold()
        if normalized_name in seen:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 响应头重复", False)
        seen.add(normalized_name)
        validated.append((normalized_name, value))

    return [
        ("Content-Type", value)
        for normalized_name, value in validated
        if normalized_name in SAFE_UPSTREAM_RESPONSE_HEADERS
    ]


class NewApiMessagesProxy:
    """Reference adapter: one POST with an explicit response-header boundary."""

    def __init__(self, base_url: str, auth_token: str | None = None, timeout: float = 30.0):
        self.base_url = base_url
        self.auth_token = auth_token
        self.timeout = timeout

    def forward(
        self,
        binding: KeyBinding,
        request_id: str,
        body: bytes,
        stream: bool,
    ) -> AdapterResponse:
        connection, base_path = _http_connection(self.base_url, "FRIEND_GATEWAY_UPSTREAM_BASE_URL", self.timeout)
        path = (base_path or "") + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Accept": "text/event-stream" if stream else "application/json",
            "X-Request-Id": request_id,
        }
        if self.auth_token:
            headers["Authorization"] = "Bearer " + self.auth_token
        try:
            # Exactly one request call.  There is intentionally no retry loop.
            connection.request("POST", path, body=body, headers=headers)
            upstream = connection.getresponse()
            response_headers = _safe_response_headers(upstream.getheaders())
        except GatewayError:
            connection.close()
            raise
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            connection.close()
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 不可达", False) from error
        return AdapterResponse(
            status=upstream.status,
            headers=response_headers,
            reader=upstream.read,
            closer=connection.close,
        )


class NewApiBalanceAdapter:
    """Reference adapter for a reviewed endpoint returning the canonical balance wire."""

    def __init__(self, adapter_url: str, auth_token: str | None = None, timeout: float = 15.0):
        self.adapter_url = adapter_url
        self.auth_token = auth_token
        self.timeout = timeout

    def get_balance(self, binding: KeyBinding, request_id: str) -> BalanceSnapshot:
        connection, path = _http_connection(self.adapter_url, "FRIEND_GATEWAY_BALANCE_ADAPTER_URL", self.timeout)
        headers = {"Accept": "application/json", "X-Request-Id": request_id}
        if self.auth_token:
            headers["Authorization"] = "Bearer " + self.auth_token
        try:
            connection.request("GET", path or "/", headers=headers)
            upstream = connection.getresponse()
            _safe_response_headers(upstream.getheaders())
            raw = upstream.read(MAX_UPSTREAM_BODY_BYTES + 1)
        except GatewayError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 余额不可达", False) from error
        finally:
            connection.close()
        if upstream.status < 200 or upstream.status >= 300 or len(raw) > MAX_UPSTREAM_BODY_BYTES:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 余额不可用", False)
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
            if not isinstance(payload, dict) or set(payload) != {"product", "balance"} or payload["product"] != PRODUCT:
                raise ValueError("unexpected balance wire")
            return BalanceSnapshot.from_wire(payload["balance"])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, GatewayConfigurationError) as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API 余额格式不可用", False) from error


def _default_catalog() -> CatalogSnapshot:
    expires_at = "2099-01-01T00:00:00Z"
    entry = CatalogEntry(
        product=PRODUCT,
        protocol=PROTOCOL,
        canonical_id="claude.default",
        model_ref="friend-model:claude-mock",
        gateway_ref=FIXED_GATEWAY_REF,
        display_name="Claude mock",
        capabilities=("streaming", "tool_use"),
        default=True,
        catalog_version="v1a-mock-1",
        expires_at=expires_at,
        billing_label="mock only",
    )
    return CatalogSnapshot("v1a-mock-1", expires_at, (entry,))


class FriendGateway:
    """Application logic separated from the HTTP server for integration tests."""

    def __init__(
        self,
        key_store: KeyBindingStore,
        catalog_adapter: CatalogAdapter,
        balance_adapter: BalanceAdapter,
        messages_adapter: MessagesAdapter,
    ):
        self.key_store = key_store
        self.catalog_adapter = catalog_adapter
        self.balance_adapter = balance_adapter
        self.messages_adapter = messages_adapter

    @staticmethod
    def _request_id(headers: Mapping[str, str]) -> str:
        return _validate_request_id(headers.get("X-Request-Id"))

    @staticmethod
    def _reject_identity_headers(headers: Mapping[str, str]) -> None:
        for name in FORBIDDEN_IDENTITY_HEADERS:
            if headers.get(name) is not None:
                raise GatewayError(400, "INVALID_REQUEST", "account_id/install_id 只能从服务端绑定派生")

    @staticmethod
    def _require_no_query(query: Mapping[str, list[str]]) -> None:
        if query:
            raise GatewayError(400, "INVALID_REQUEST", "该路径不接受查询参数")

    @staticmethod
    def _catalog_is_current(snapshot: CatalogSnapshot) -> None:
        if _parse_datetime(snapshot.expires_at) <= _utc_now():
            raise GatewayError(502, "CATALOG_EXPIRED", "服务端目录已过期")

    def preflight(self, headers: Mapping[str, str], query: Mapping[str, list[str]]) -> tuple[int, dict[str, Any]]:
        request_id = self._request_id(headers)
        self._reject_identity_headers(headers)
        if headers.get("Authorization") is not None:
            raise GatewayError(400, "INVALID_REQUEST", "preflight 不接受 Friend Key")
        if set(query) != {"client_version", "product", "protocol"}:
            raise GatewayError(400, "INVALID_REQUEST", "preflight 参数不完整或包含额外字段")
        for name in query:
            if len(query[name]) != 1:
                raise GatewayError(400, "INVALID_REQUEST", "preflight 参数重复")
        _require_string(query["client_version"][0], "client_version", 1, 64)
        if query["product"][0] != PRODUCT or query["protocol"][0] != PROTOCOL:
            raise GatewayError(400, "PRODUCT_PROTOCOL_MISMATCH", "产品或协议不是固定 V1A")
        try:
            snapshot = self.catalog_adapter.get_catalog(None)
            available = _parse_datetime(snapshot.expires_at) > _utc_now()
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(500, "RECOVERY_REQUIRED", "Friend 网关目录不可用") from error
        response: dict[str, Any] = {
            "request_id": request_id,
            "available": available,
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "catalog_version": snapshot.catalog_version,
            "expires_at": snapshot.expires_at,
        }
        if not available:
            response["reason_code"] = "CATALOG_EXPIRED"
        return 200, response

    def catalog(self, headers: Mapping[str, str], query: Mapping[str, list[str]], body: bytes) -> tuple[int, dict[str, Any]]:
        request_id = self._request_id(headers)
        self._require_no_query(query)
        self._reject_identity_headers(headers)
        binding = self.key_store.resolve(headers.get("Authorization"))
        request = _parse_json_object(body)
        if set(request) != {"product", "protocol"}:
            raise GatewayError(400, "INVALID_REQUEST", "catalog 请求只接受 product 和 protocol")
        if request["product"] != PRODUCT or request["protocol"] != PROTOCOL:
            raise GatewayError(400, "PRODUCT_PROTOCOL_MISMATCH", "产品或协议不是固定 V1A")
        try:
            snapshot = self.catalog_adapter.get_catalog(binding)
            self._catalog_is_current(snapshot)
            balance = self.balance_adapter.get_balance(binding, request_id)
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "目录或余额 adapter 不可用") from error
        return 200, {
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "catalog_version": snapshot.catalog_version,
            "expires_at": snapshot.expires_at,
            "integrity": CATALOG_TRUST_BOUNDARY,
            "catalog": snapshot.wire_entries(),
            "balance": balance.wire(),
        }

    def balance(self, headers: Mapping[str, str], query: Mapping[str, list[str]]) -> tuple[int, dict[str, Any]]:
        request_id = self._request_id(headers)
        self._require_no_query(query)
        self._reject_identity_headers(headers)
        binding = self.key_store.resolve(headers.get("Authorization"))
        try:
            balance = self.balance_adapter.get_balance(binding, request_id)
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "余额 adapter 不可用") from error
        return 200, {"product": PRODUCT, "balance": balance.wire()}

    def messages(self, headers: Mapping[str, str], query: Mapping[str, list[str]], body: bytes) -> AdapterResponse:
        request_id = self._request_id(headers)
        self._require_no_query(query)
        self._reject_identity_headers(headers)
        binding = self.key_store.resolve(headers.get("Authorization"))
        request = _parse_json_object(body)
        stream = self._validate_messages_request(request, binding)
        try:
            # The original validated bytes are forwarded unchanged by the
            # proxy adapter.  It receives binding metadata only from the key
            # store, never from the public request.
            return self.messages_adapter.forward(binding, request_id, body, stream)
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "New API messages adapter 不可用") from error

    def _validate_messages_request(self, request: dict[str, Any], binding: KeyBinding) -> bool:
        if not set(request).issubset(MESSAGE_KEYS) or not {"model", "max_tokens", "messages"}.issubset(request):
            raise GatewayError(400, "INVALID_REQUEST", "messages 请求字段不符合 V1A 合同")
        model = request["model"]
        if not isinstance(model, str) or not MODEL_REF_RE.fullmatch(model) or len(model) > 128:
            raise GatewayError(400, "INVALID_REQUEST", "model 必须是服务端目录中的 friend-model 引用")
        max_tokens = request["max_tokens"]
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 200000:
            raise GatewayError(400, "INVALID_REQUEST", "max_tokens 格式无效")
        messages = request["messages"]
        if not isinstance(messages, list) or not 1 <= len(messages) <= 10000:
            raise GatewayError(400, "INVALID_REQUEST", "messages 格式无效")
        for item in messages:
            self._validate_message_input(item)
        if "system" in request:
            self._validate_system(request["system"])
        if "tools" in request:
            self._validate_tools(request["tools"])
        if "stream" in request and not isinstance(request["stream"], bool):
            raise GatewayError(400, "INVALID_REQUEST", "stream 格式无效")
        if "temperature" in request:
            self._validate_number(request["temperature"], "temperature", 0, 1, False)
        if "top_p" in request:
            self._validate_number(request["top_p"], "top_p", 0, 1, True)
        if "top_k" in request:
            value = request["top_k"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GatewayError(400, "INVALID_REQUEST", "top_k 格式无效")
        if "stop_sequences" in request:
            values = request["stop_sequences"]
            if not isinstance(values, list) or len(values) > 4 or any(not isinstance(item, str) or len(item) > 1000 for item in values):
                raise GatewayError(400, "INVALID_REQUEST", "stop_sequences 格式无效")
        if "metadata" in request:
            metadata = request["metadata"]
            if not isinstance(metadata, dict) or not set(metadata).issubset({"user_id", "trace_id"}):
                raise GatewayError(400, "INVALID_REQUEST", "metadata 只能包含 user_id/trace_id")
            for name, value in metadata.items():
                if not isinstance(value, str) or len(value) > 128:
                    raise GatewayError(400, "INVALID_REQUEST", f"metadata.{name} 格式无效")
        try:
            snapshot = self.catalog_adapter.get_catalog(binding)
            self._catalog_is_current(snapshot)
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(502, "UPSTREAM_UNAVAILABLE", "目录 adapter 不可用") from error
        if not any(entry.model_ref == model for entry in snapshot.entries):
            raise GatewayError(400, "INVALID_REQUEST", "model 不是当前服务端目录中的模型")
        return bool(request.get("stream", False))

    @staticmethod
    def _validate_number(value: Any, field: str, minimum: float, maximum: float, exclusive_minimum: bool) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GatewayError(400, "INVALID_REQUEST", f"{field} 格式无效")
        if (value <= minimum if exclusive_minimum else value < minimum) or value > maximum:
            raise GatewayError(400, "INVALID_REQUEST", f"{field} 超出范围")

    @classmethod
    def _validate_message_input(cls, item: Any) -> None:
        if not isinstance(item, dict) or set(item) != {"role", "content"} or item["role"] not in {"user", "assistant"}:
            raise GatewayError(400, "INVALID_REQUEST", "message 条目格式无效")
        cls._validate_content(item["content"])

    @classmethod
    def _validate_system(cls, value: Any) -> None:
        if isinstance(value, str):
            if len(value) > 1_000_000:
                raise GatewayError(400, "INVALID_REQUEST", "system 过长")
            return
        if not isinstance(value, list):
            raise GatewayError(400, "INVALID_REQUEST", "system 格式无效")
        for item in value:
            if not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "text" or not isinstance(item["text"], str) or len(item["text"]) > 1_000_000:
                raise GatewayError(400, "INVALID_REQUEST", "system 内容块格式无效")

    @classmethod
    def _validate_content(cls, value: Any) -> None:
        if isinstance(value, str):
            if len(value) > 1_000_000:
                raise GatewayError(400, "INVALID_REQUEST", "message content 过长")
            return
        if not isinstance(value, list):
            raise GatewayError(400, "INVALID_REQUEST", "message content 格式无效")
        for item in value:
            if not isinstance(item, dict) or item.get("type") not in {"text", "tool_use", "tool_result"}:
                raise GatewayError(400, "INVALID_REQUEST", "message 内容块类型无效")
            block_type = item["type"]
            if block_type == "text":
                if set(item) != {"type", "text"} or not isinstance(item["text"], str) or len(item["text"]) > 1_000_000:
                    raise GatewayError(400, "INVALID_REQUEST", "text 内容块格式无效")
            elif block_type == "tool_use":
                if set(item) != {"type", "id", "name", "input"} or not all(isinstance(item[name], str) and 1 <= len(item[name]) <= 128 for name in ("id", "name")) or not isinstance(item["input"], dict):
                    raise GatewayError(400, "INVALID_REQUEST", "tool_use 内容块格式无效")
            else:
                allowed = {"type", "tool_use_id", "content", "is_error"}
                if not set(item).issubset(allowed) or not {"type", "tool_use_id", "content"}.issubset(item) or not isinstance(item["tool_use_id"], str) or not 1 <= len(item["tool_use_id"]) <= 128 or not isinstance(item["content"], str) or len(item["content"]) > 1_000_000 or ("is_error" in item and not isinstance(item["is_error"], bool)):
                    raise GatewayError(400, "INVALID_REQUEST", "tool_result 内容块格式无效")

    @staticmethod
    def _validate_tools(value: Any) -> None:
        if not isinstance(value, list) or len(value) > 256:
            raise GatewayError(400, "INVALID_REQUEST", "tools 格式无效")
        for item in value:
            if not isinstance(item, dict) or not set(item).issubset({"name", "description", "input_schema"}) or not {"name", "input_schema"}.issubset(item):
                raise GatewayError(400, "INVALID_REQUEST", "tool 定义格式无效")
            if not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 128 or not isinstance(item["input_schema"], dict):
                raise GatewayError(400, "INVALID_REQUEST", "tool 定义格式无效")
            if "description" in item and (not isinstance(item["description"], str) or len(item["description"]) > 10000):
                raise GatewayError(400, "INVALID_REQUEST", "tool description 格式无效")


def build_from_environment() -> FriendGateway:
    mode = os.getenv("FRIEND_GATEWAY_MODE", "mock").strip().lower()
    catalog_file = os.getenv("FRIEND_GATEWAY_CATALOG_FILE", "").strip()
    binding_file = os.getenv("FRIEND_GATEWAY_KEY_BINDINGS_FILE", "").strip()
    if catalog_file:
        catalog_adapter: CatalogAdapter = StaticCatalogAdapter(CatalogSnapshot.from_file(catalog_file))
    elif mode == "mock":
        catalog_adapter = StaticCatalogAdapter(_default_catalog())
    else:
        raise GatewayConfigurationError("proxy mode requires FRIEND_GATEWAY_CATALOG_FILE")
    if binding_file:
        key_store = KeyBindingStore.from_file(binding_file)
    elif mode == "mock":
        key_store = KeyBindingStore.for_mock_key()
    else:
        raise GatewayConfigurationError("proxy mode requires FRIEND_GATEWAY_KEY_BINDINGS_FILE")
    if mode == "mock":
        try:
            amount_minor = int(os.getenv("FRIEND_GATEWAY_MOCK_BALANCE_MINOR", "10000"))
        except ValueError as error:
            raise GatewayConfigurationError("FRIEND_GATEWAY_MOCK_BALANCE_MINOR is invalid") from error
        return FriendGateway(key_store, catalog_adapter, MockBalanceAdapter(amount_minor), MockMessagesAdapter())
    if mode != "proxy":
        raise GatewayConfigurationError("FRIEND_GATEWAY_MODE must be mock or proxy")
    upstream_base = os.getenv("FRIEND_GATEWAY_UPSTREAM_BASE_URL", "").strip()
    balance_url = os.getenv("FRIEND_GATEWAY_BALANCE_ADAPTER_URL", "").strip()
    auth_token = os.getenv("FRIEND_GATEWAY_UPSTREAM_AUTH_TOKEN")
    if not upstream_base or not balance_url:
        raise GatewayConfigurationError("proxy mode requires explicit New API URLs")
    return FriendGateway(
        key_store,
        catalog_adapter,
        NewApiBalanceAdapter(balance_url, auth_token),
        NewApiMessagesProxy(upstream_base, auth_token),
    )


class FriendGatewayHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: FriendGateway, log_stream: Any = None):
        super().__init__(address, FriendGatewayRequestHandler)
        self.app = app
        self.log_stream = log_stream or sys.stderr


class FriendGatewayRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FriendGatewayReference/1"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        self.close_connection = True
        self._log_path = UNKNOWN_LOG_PATH
        parsed = urlsplit(self.path)
        route = parsed.path
        self._log_path = route if route in ALLOWED_LOG_PATHS else UNKNOWN_LOG_PATH
        status = 500
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
            if route == "/healthz" and self.command == "GET":
                status = 200
                self._send_json(status, {"status": "ok"})
                return
            body = b""
            if self.command == "POST":
                body = self._read_body()
            if route == "/v1/friend/preflight" and self.command == "GET":
                status, response = self.server.app.preflight(self.headers, query)
                self._send_json(status, response)
            elif route == "/v1/friend/catalog" and self.command == "POST":
                status, response = self.server.app.catalog(self.headers, query, body)
                self._send_json(status, response)
            elif route == "/v1/friend/balance" and self.command == "GET":
                status, response = self.server.app.balance(self.headers, query)
                self._send_json(status, response)
            elif route == "/v1/messages" and self.command == "POST":
                adapter_response = self.server.app.messages(self.headers, query, body)
                status = adapter_response.status
                self._send_adapter_response(adapter_response)
            else:
                raise GatewayError(404, "NOT_FOUND", "路径不存在")
        except GatewayError as error:
            status = error.status
            request_id = self._safe_request_id()
            self._send_json(
                status,
                {
                    "request_id": request_id,
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
            )
        except Exception:
            status = 500
            self._send_json(
                status,
                {
                    "request_id": self._safe_request_id(),
                    "code": "RECOVERY_REQUIRED",
                    "message": "Friend 网关安全失败关闭",
                    "retryable": False,
                },
            )
        finally:
            self._last_status = status

    def _safe_request_id(self) -> str:
        raw = self.headers.get("X-Request-Id")
        return raw if isinstance(raw, str) and REQUEST_ID_RE.fullmatch(raw) else _new_request_id()

    def _read_body(self) -> bytes:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise GatewayError(400, "INVALID_REQUEST", "Content-Type 必须是 application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as error:
            raise GatewayError(400, "INVALID_REQUEST", "Content-Length 无效") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise GatewayError(400, "INVALID_REQUEST", "请求体大小无效")
        body = self.rfile.read(length)
        if len(body) != length:
            raise GatewayError(400, "INVALID_REQUEST", "请求体不完整")
        return body

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = _json_bytes(value)
        self._last_status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_adapter_response(self, response: AdapterResponse) -> None:
        try:
            safe_headers = _safe_response_headers(response.headers)
            self._last_status = response.status
            self.log_request(response.status)
            self.send_response_only(response.status)
            for name, value in safe_headers:
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in response.iter_body():
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            response.close()

    def log_message(self, format: str, *args: Any) -> None:
        # Only fixed values reach the log: requestline, query, headers, and
        # unknown paths can all contain caller-controlled secrets or controls.
        method = self.command if self.command in {"GET", "POST"} else "<unknown>"
        route = getattr(self, "_log_path", UNKNOWN_LOG_PATH)
        status = getattr(self, "_last_status", "-")
        self.server.log_stream.write(f"request method={method} path={route} status={status}\n")
        self.server.log_stream.flush()


def main() -> None:
    try:
        app = build_from_environment()
        host = os.getenv("FRIEND_GATEWAY_LISTEN_ADDR", "127.0.0.1").strip()
        port = int(os.getenv("FRIEND_GATEWAY_PORT", "3000"))
        if not 1 <= port <= 65535:
            raise GatewayConfigurationError("FRIEND_GATEWAY_PORT is out of range")
    except (GatewayConfigurationError, ValueError) as error:
        print(f"friend-gateway configuration failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    server = FriendGatewayHTTPServer((host, port), app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
