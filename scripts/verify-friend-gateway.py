#!/usr/bin/env python3
"""Check the configured Claude V1A endpoint before a candidate build."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request


PRODUCT = "claude"
PROTOCOL = "anthropic-messages"
DEFAULT_CLIENT_VERSION = "friend-agent-launcher/0.1.0"
PREFLIGHT_PATH = "/v1/friend/preflight"
PREFLIGHT_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CATALOG_VERSION_RE = re.compile(r"^v1a-[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")
RESPONSE_FIELDS = frozenset(
    {"request_id", "available", "product", "protocol", "catalog_version", "expires_at", "reason_code"}
)
REQUIRED_RESPONSE_FIELDS = frozenset(
    {"request_id", "available", "product", "protocol", "catalog_version", "expires_at"}
)
SPECIAL_USE_SUFFIXES = (".invalid", ".example", ".test", ".localhost", ".local")
EXAMPLE_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


class GatewayPreflightError(Exception):
    """An expected fail-closed gateway verification failure."""


class GatewayOriginError(GatewayPreflightError):
    """The configured origin or its DNS boundary is not acceptable."""


class GatewayNetworkError(GatewayPreflightError):
    """DNS, TLS, HTTP, redirect, or response transport failure."""


@dataclass(frozen=True)
class GatewayOrigin:
    """A normalized HTTPS origin whose host passed the address boundary."""

    url: str
    hostname: str
    port: int


@dataclass(frozen=True)
class ResolvedAddress:
    """One address returned by the audited resolver and safe to connect to."""

    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]
    address: str


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _normalize_hostname(hostname: str) -> str:
    if not hostname or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in hostname):
        raise GatewayOriginError("invalid hostname")
    if "%" in hostname:
        # Zone-scoped IPv6 literals are local-interface selectors, not public origins.
        raise GatewayOriginError("scoped hostname")
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise GatewayOriginError("invalid hostname") from exc
    if not normalized or len(normalized) > 253:
        raise GatewayOriginError("invalid hostname")
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise GatewayOriginError("invalid hostname")
    return normalized


def _is_example_or_special_use(hostname: str) -> bool:
    return (
        hostname in EXAMPLE_DOMAINS
        or any(hostname.endswith("." + domain) for domain in EXAMPLE_DOMAINS)
        or any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in SPECIAL_USE_SUFFIXES)
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    )


def validate_gateway_origin(raw_url: str) -> GatewayOrigin:
    """Validate only an HTTPS root origin and reject non-public host literals."""

    if not isinstance(raw_url, str) or not raw_url or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_url
    ):
        raise GatewayOriginError("invalid origin")
    if "?" in raw_url or "#" in raw_url:
        raise GatewayOriginError("query or fragment")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise GatewayOriginError("invalid origin") from exc
    if parsed.scheme.lower() != "https":
        raise GatewayOriginError("HTTPS required")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise GatewayOriginError("userinfo is forbidden")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.netloc:
        raise GatewayOriginError("origin must have a root path")
    if port is None:
        port = 443
    if not 1 <= port <= 65535:
        raise GatewayOriginError("invalid port")

    hostname = parsed.hostname
    if hostname is None:
        raise GatewayOriginError("hostname is missing")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise GatewayOriginError("non-global IP literal")
        normalized_hostname = str(literal)
        authority_host = f"[{normalized_hostname}]" if literal.version == 6 else normalized_hostname
    else:
        normalized_hostname = _normalize_hostname(hostname)
        if _is_example_or_special_use(normalized_hostname):
            raise GatewayOriginError("special-use hostname")
        authority_host = normalized_hostname
    authority = authority_host if port == 443 and parsed.port is None else f"{authority_host}:{port}"
    return GatewayOrigin(f"https://{authority}", normalized_hostname, port)


def _resolved_sockaddr(family: int, raw_sockaddr: Any, port: int) -> tuple[Any, ...]:
    if not isinstance(raw_sockaddr, (tuple, list)) or not raw_sockaddr:
        raise ValueError("invalid sockaddr")
    address = raw_sockaddr[0]
    if not isinstance(address, str):
        raise ValueError("invalid address")
    if family == socket.AF_INET:
        if len(raw_sockaddr) < 2:
            raise ValueError("invalid IPv4 sockaddr")
        return (address, port)
    if family == socket.AF_INET6:
        if len(raw_sockaddr) < 4:
            raise ValueError("invalid IPv6 sockaddr")
        if raw_sockaddr[3] != 0:
            raise ValueError("scoped IPv6 address")
        return (address, port, raw_sockaddr[2], 0)
    raise ValueError("unsupported address family")


def _resolve_all_global(origin: GatewayOrigin, resolver: Callable[..., Any]) -> tuple[ResolvedAddress, ...]:
    """Audit every resolver result; callers must use only the returned addresses.

    The standard resolver has no portable per-call timeout. We therefore do not
    fake a DNS timeout with a worker thread whose cleanup could outlive the
    advertised deadline; the ten-second deadline is enforced from the first
    connection attempt through response reading.
    """

    try:
        records = list(resolver(origin.hostname, origin.port, type=socket.SOCK_STREAM))
    except (OSError, TypeError, ValueError, socket.gaierror) as exc:
        raise GatewayNetworkError("DNS resolution failed") from exc
    if not records:
        raise GatewayNetworkError("DNS returned no addresses")

    audited: list[ResolvedAddress] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for record in records:
        try:
            family = record[0]
            socktype = record[1]
            proto = record[2]
            sockaddr = _resolved_sockaddr(family, record[4], origin.port)
            raw_address = sockaddr[0]
            literal = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except (IndexError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GatewayNetworkError("DNS returned an invalid address") from exc
        if literal.version != (4 if family == socket.AF_INET else 6) or not literal.is_global:
            raise GatewayOriginError("DNS returned a non-global address")
        if socktype not in (0, socket.SOCK_STREAM):
            raise GatewayNetworkError("DNS returned a non-stream address")
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        audited.append(ResolvedAddress(family, socket.SOCK_STREAM, proto, sockaddr, str(literal)))
    if not audited:
        raise GatewayNetworkError("DNS returned no usable addresses")
    return tuple(audited)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GatewayNetworkError("gateway preflight timed out")
    return remaining


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP destination is an already audited address."""

    def __init__(
        self,
        origin: GatewayOrigin,
        address: ResolvedAddress,
        context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        super().__init__(
            origin.hostname,
            origin.port,
            timeout=_remaining(deadline),
            context=context,
        )
        self._audited_address = address
        self._deadline = deadline

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise GatewayNetworkError("proxy tunneling is disabled")
        raw_socket: Optional[socket.socket] = None
        try:
            raw_socket = socket.socket(
                self._audited_address.family,
                socket.SOCK_STREAM,
                self._audited_address.proto,
            )
            raw_socket.settimeout(_remaining(self._deadline))
            # Do not use socket.create_connection: it would perform a second
            # resolver lookup and break the DNS-audit/connection binding.
            raw_socket.connect(self._audited_address.sockaddr)
            raw_socket.settimeout(_remaining(self._deadline))
            secure_socket = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            secure_socket.settimeout(_remaining(self._deadline))
            self.sock = secure_socket
            raw_socket = None
        except Exception:
            if raw_socket is not None:
                raw_socket.close()
            raise


def _default_connection_factory(
    origin: GatewayOrigin,
    address: ResolvedAddress,
    context: ssl.SSLContext,
    deadline: float,
) -> _PinnedHTTPSConnection:
    return _PinnedHTTPSConnection(origin, address, context, deadline)


def _header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get(name)
        except (AttributeError, TypeError):
            value = None
        if isinstance(value, str):
            return value
    getter = getattr(response, "getheader", None)
    if callable(getter):
        try:
            value = getter(name)
        except (AttributeError, TypeError):
            value = None
        if isinstance(value, str):
            return value
    return None


def _status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        getter = getattr(response, "getcode", None)
        value = getter() if callable(getter) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayNetworkError("invalid HTTP status")
    return value


def _set_response_timeout(response: Any, timeout: float) -> None:
    file_object = getattr(response, "fp", None)
    raw = getattr(file_object, "raw", None)
    sock = getattr(raw, "_sock", None) or getattr(file_object, "_sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _set_connection_timeout(connection: Any, timeout: float) -> None:
    sock = getattr(connection, "sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _read_response(response: Any, deadline: Optional[float] = None) -> bytes:
    content_length = _header(response, "Content-Length")
    if content_length is not None:
        try:
            length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise GatewayNetworkError("invalid response length") from exc
        if length < 0 or length > MAX_RESPONSE_BYTES:
            raise GatewayNetworkError("response is too large")

    content_type = _header(response, "Content-Type")
    if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise GatewayNetworkError("response content type is invalid")

    try:
        if deadline is None:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        else:
            chunks: list[bytes] = []
            total = 0
            read1 = getattr(response, "read1", None)
            while total <= MAX_RESPONSE_BYTES:
                _set_response_timeout(response, _remaining(deadline))
                limit = MAX_RESPONSE_BYTES + 1 - total
                chunk = read1(limit) if callable(read1) else response.read(limit)
                if not isinstance(chunk, (bytes, bytearray)):
                    raise GatewayNetworkError("response could not be read")
                if not chunk:
                    break
                chunks.append(bytes(chunk))
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
            body = b"".join(chunks)
    except GatewayPreflightError:
        raise
    except (OSError, TypeError, ValueError, TimeoutError) as exc:
        raise GatewayNetworkError("response could not be read") from exc
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
        raise GatewayNetworkError("response is too large")
    return bytes(body)


def _parse_expiry(value: Any, now: dt.datetime) -> None:
    if not isinstance(value, str) or not value or "T" not in value:
        raise GatewayPreflightError("expiry is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayPreflightError("expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise GatewayPreflightError("expiry has no timezone")
    if parsed.astimezone(dt.timezone.utc) <= now.astimezone(dt.timezone.utc):
        raise GatewayPreflightError("expiry is not in the future")


def _validate_response(body: bytes, request_id: str, now: dt.datetime) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GatewayPreflightError("response JSON is invalid") from exc
    if not isinstance(value, dict):
        raise GatewayPreflightError("response JSON is not an object")
    if not REQUIRED_RESPONSE_FIELDS.issubset(value) or not set(value).issubset(RESPONSE_FIELDS):
        raise GatewayPreflightError("response fields are invalid")
    if type(value["available"]) is not bool or value["available"] is not True:
        raise GatewayPreflightError("gateway is unavailable")
    if value["product"] != PRODUCT or value["protocol"] != PROTOCOL:
        raise GatewayPreflightError("product or protocol is invalid")
    if not isinstance(value["request_id"], str) or not REQUEST_ID_RE.fullmatch(value["request_id"]):
        raise GatewayPreflightError("response request id is invalid")
    if value["request_id"] != request_id:
        raise GatewayPreflightError("response request id does not match request")
    if not isinstance(value["catalog_version"], str) or not CATALOG_VERSION_RE.fullmatch(value["catalog_version"]):
        raise GatewayPreflightError("catalog version is invalid")
    _parse_expiry(value["expires_at"], now)
    if "reason_code" in value:
        # The successful build preflight has no failure reason.
        raise GatewayPreflightError("successful response contains a reason")
    return value


def _host_header(origin: GatewayOrigin) -> str:
    host = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
    return host if origin.port == 443 else f"{host}:{origin.port}"


def _build_request(origin: GatewayOrigin, client_version: str, request_id: str) -> Request:
    query = urlencode(
        {"client_version": client_version, "product": PRODUCT, "protocol": PROTOCOL}
    )
    return Request(
        f"{origin.url}{PREFLIGHT_PATH}?{query}",
        method="GET",
        headers={"Accept": "application/json", "X-Request-Id": request_id},
    )


def _verify_with_opener(
    opener: Any,
    request: Request,
    deadline: float,
) -> bytes:
    """Use an explicitly injected opener seam; production uses direct HTTPS below."""

    response = None
    try:
        response = opener.open(request, timeout=min(PREFLIGHT_TIMEOUT_SECONDS, _remaining(deadline)))
        status = _status(response)
        if 300 <= status < 400:
            raise GatewayNetworkError("redirect rejected")
        if status != 200:
            raise GatewayNetworkError("unexpected HTTP status")
        return _read_response(response)
    except GatewayPreflightError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, ssl.SSLError) as exc:
        raise GatewayNetworkError("gateway preflight transport failed") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def verify_gateway(
    raw_url: str,
    client_version: str = DEFAULT_CLIENT_VERSION,
    *,
    resolver: Optional[Callable[..., Any]] = None,
    opener: Any = None,
    connection_factory: Optional[Callable[..., Any]] = None,
    now: Optional[dt.datetime] = None,
    request_id_factory: Optional[Callable[[], str]] = None,
) -> Dict[str, Any]:
    """Resolve, connect, and strictly verify the unauthenticated V1A preflight."""

    if not isinstance(client_version, str) or not 1 <= len(client_version) <= 64:
        raise GatewayPreflightError("client version is invalid")
    origin = validate_gateway_origin(raw_url)
    deadline = time.monotonic() + PREFLIGHT_TIMEOUT_SECONDS
    audited_addresses = _resolve_all_global(origin, resolver or socket.getaddrinfo)
    _remaining(deadline)
    request_id = request_id_factory() if request_id_factory is not None else "preflight-" + secrets.token_hex(16)
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise GatewayPreflightError("request id is invalid")
    request = _build_request(origin, client_version, request_id)
    validation_now = now or dt.datetime.now(dt.timezone.utc)

    if opener is not None:
        body = _verify_with_opener(opener, request, deadline)
        return _validate_response(body, request_id, validation_now)

    context = ssl.create_default_context()
    factory = connection_factory or _default_connection_factory
    target = f"{PREFLIGHT_PATH}?{urlencode({'client_version': client_version, 'product': PRODUCT, 'protocol': PROTOCOL})}"
    headers = {
        "Accept": "application/json",
        "Host": _host_header(origin),
        "X-Request-Id": request_id,
    }
    for address in audited_addresses:
        connection = None
        response = None
        try:
            connection = factory(origin, address, context, deadline)
            _remaining(deadline)
            connection.request("GET", target, headers=headers)
            _set_connection_timeout(connection, _remaining(deadline))
            response = connection.getresponse()
            status = _status(response)
            if 300 <= status < 400:
                raise GatewayNetworkError("redirect rejected")
            if status != 200:
                raise GatewayNetworkError("unexpected HTTP status")
            body = _read_response(response, deadline)
            return _validate_response(body, request_id, validation_now)
        except GatewayPreflightError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
            # A connection failure may try the next audited address; no new DNS
            # lookup or non-audited fallback is permitted.
            del exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            close_connection = getattr(connection, "close", None)
            if callable(close_connection):
                close_connection()
    raise GatewayNetworkError("gateway preflight transport failed")


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--client-version", default=DEFAULT_CLIENT_VERSION)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        verify_gateway(args.url, args.client_version)
    except GatewayNetworkError:
        print(
            "friend gateway preflight: BLOCKED: network/TLS/preflight verification failed",
            file=sys.stderr,
        )
        return 1
    except GatewayPreflightError:
        print(
            "friend gateway preflight: BLOCKED: gateway origin or response is invalid",
            file=sys.stderr,
        )
        return 1
    print(
        "friend gateway preflight: PASS: configured Claude V1A endpoint reachable and contract-matching"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
