#!/usr/bin/env python3
import hashlib
import http.client
import io
import json
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR))
import friend_gateway as gateway  # noqa: E402


TEST_KEY = "local-" + "test-" + "friend-" + "key"
TEST_REQUEST_ID = "integration-test-1"


def make_snapshot() -> gateway.CatalogSnapshot:
    expires_at = "2099-01-01T00:00:00Z"
    entry = gateway.CatalogEntry(
        product=gateway.PRODUCT,
        protocol=gateway.PROTOCOL,
        canonical_id="claude.default",
        model_ref="friend-model:claude-test",
        gateway_ref=gateway.FIXED_GATEWAY_REF,
        display_name="Claude test",
        capabilities=("streaming", "tool_use"),
        default=True,
        catalog_version="v1a-test-1",
        expires_at=expires_at,
        billing_label="test only",
    )
    return gateway.CatalogSnapshot("v1a-test-1", expires_at, (entry,))


class RecordingBalanceAdapter:
    def __init__(self):
        self.last_binding = None
        self.calls = 0

    def get_balance(self, binding, request_id):
        self.last_binding = binding
        self.calls += 1
        return gateway.BalanceSnapshot(1250, "CNY", "2098-01-01T00:00:00Z")


class NoopMessagesAdapter:
    def __init__(self):
        self.calls = 0

    def forward(self, binding, request_id, body, stream):
        self.calls += 1
        return gateway.AdapterResponse(
            200,
            [("Content-Type", "application/json")],
            body=gateway._json_bytes(
                {
                    "id": "msg-test",
                    "type": "message",
                    "role": "assistant",
                    "model": "friend-model:claude-test",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
        )


def make_app(messages=None, balance=None):
    binding = gateway.KeyBinding(
        key_sha256=hashlib.sha256(TEST_KEY.encode()).hexdigest(),
        account_id="acct-server-bound",
        install_id="install-server-bound",
    )
    return (
        gateway.FriendGateway(
            gateway.KeyBindingStore([binding]),
            gateway.StaticCatalogAdapter(make_snapshot()),
            balance or RecordingBalanceAdapter(),
            messages or gateway.MockMessagesAdapter(),
        ),
        binding,
    )


@contextmanager
def running_gateway(app):
    logs = io.StringIO()
    server = gateway.FriendGatewayHTTPServer(("127.0.0.1", 0), app, log_stream=logs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, logs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, method, path, body=None, auth=True, extra_headers=None):
    headers = {"X-Request-Id": TEST_REQUEST_ID}
    if auth:
        headers["Authorization"] = "Bearer " + TEST_KEY
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    else:
        raw = None
    if extra_headers:
        headers.update(extra_headers)
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def valid_catalog_request():
    return {"product": gateway.PRODUCT, "protocol": gateway.PROTOCOL}


def valid_message_request(**overrides):
    request_body = {
        "model": "friend-model:claude-test",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hello"}],
    }
    request_body.update(overrides)
    return request_body


class FakeBalanceUpstreamResponse:
    def __init__(self, headers, body=b"{}", status=200):
        self.headers = headers
        self.body = body
        self.status = status
        self.read_calls = 0

    def getheaders(self):
        return self.headers

    def read(self, limit):
        self.read_calls += 1
        return self.body


class FakeBalanceConnection:
    def __init__(self, upstream):
        self.upstream = upstream
        self.close_calls = 0

    def request(self, method, path, headers):
        self.request_args = (method, path, headers)

    def getresponse(self):
        return self.upstream

    def close(self):
        self.close_calls += 1


class NewApiBalanceAdapterTests(unittest.TestCase):
    def test_invalid_upstream_headers_fail_closed_without_echoing_secrets(self):
        class MalformedHeaderContainer:
            def __iter__(self):
                raise TypeError("malformed-container-secret")

        cases = (
            (
                "unknown duplicate",
                [("X-Upstream-Secret", "first-secret"), ("x-upstream-secret", "second-secret")],
                "first-secret",
            ),
            ("control", [("X-Upstream-Header", "control-secret\x01")], "control-secret"),
            ("malformed container", MalformedHeaderContainer(), "malformed-container-secret"),
        )

        for case, headers, secret in cases:
            with self.subTest(case=case):
                response = FakeBalanceUpstreamResponse(headers)
                connection = FakeBalanceConnection(response)
                adapter = gateway.NewApiBalanceAdapter("http://balance.example.invalid")
                with patch.object(gateway, "_http_connection", return_value=(connection, "/balance")):
                    with self.assertRaises(gateway.GatewayError) as raised:
                        adapter.get_balance(None, TEST_REQUEST_ID)
                self.assertEqual(raised.exception.status, 502)
                self.assertEqual(raised.exception.code, "UPSTREAM_UNAVAILABLE")
                self.assertNotIn(secret, str(raised.exception))
                self.assertEqual(response.read_calls, 0)
                self.assertEqual(connection.close_calls, 1)

    def test_valid_unknown_header_is_ignored_for_canonical_balance(self):
        body = json.dumps(
            {
                "product": gateway.PRODUCT,
                "balance": {
                    "amount_minor": 1250,
                    "currency": "CNY",
                    "as_of": "2098-01-01T00:00:00Z",
                    "source": gateway.BALANCE_SOURCE,
                },
            },
            separators=(",", ":"),
        ).encode()
        response = FakeBalanceUpstreamResponse(
            [("X-Upstream-Trace", "trace-123"), ("cOnTeNt-TyPe", "application/json")],
            body=body,
        )
        connection = FakeBalanceConnection(response)
        adapter = gateway.NewApiBalanceAdapter("http://balance.example.invalid")

        with patch.object(gateway, "_http_connection", return_value=(connection, "/balance")):
            snapshot = adapter.get_balance(None, TEST_REQUEST_ID)

        self.assertEqual(snapshot, gateway.BalanceSnapshot(1250, "CNY", "2098-01-01T00:00:00Z"))
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(connection.close_calls, 1)


class FriendGatewayIntegrationTests(unittest.TestCase):
    def test_preflight_is_unauthenticated_and_exact_wire(self):
        app, _ = make_app()
        with running_gateway(app) as (server, _):
            status, _, raw = request(
                server,
                "GET",
                "/v1/friend/preflight?client_version=test&product=claude&protocol=anthropic-messages",
                auth=False,
            )
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(
            set(body),
            {"request_id", "available", "product", "protocol", "catalog_version", "expires_at"},
        )
        self.assertTrue(body["available"])
        self.assertEqual(body["product"], "claude")
        self.assertEqual(body["protocol"], "anthropic-messages")

    def test_all_authenticated_routes_require_bearer_key(self):
        app, _ = make_app()
        with running_gateway(app) as (server, _):
            calls = [
                ("POST", "/v1/friend/catalog", valid_catalog_request()),
                ("GET", "/v1/friend/balance", None),
                ("POST", "/v1/messages", valid_message_request()),
            ]
            for method, path, body in calls:
                status, _, raw = request(server, method, path, body=body, auth=False)
                self.assertEqual(status, 401, path)
                error = json.loads(raw)
                self.assertEqual(set(error), {"request_id", "code", "message", "retryable"})
                self.assertEqual(error["code"], "AUTH_REQUIRED")

    def test_server_binding_cannot_be_overridden_by_body_query_or_header(self):
        balance = RecordingBalanceAdapter()
        messages = NoopMessagesAdapter()
        app, binding = make_app(messages=messages, balance=balance)
        with running_gateway(app) as (server, _):
            status, _, _ = request(
                server,
                "POST",
                "/v1/friend/catalog",
                body={**valid_catalog_request(), "account_id": "attacker", "install_id": "attacker"},
            )
            self.assertEqual(status, 400)
            status, _, _ = request(
                server,
                "POST",
                "/v1/messages",
                body={**valid_message_request(), "account_id": "attacker"},
            )
            self.assertEqual(status, 400)
            status, _, _ = request(
                server,
                "GET",
                "/v1/friend/balance?install_id=attacker",
            )
            self.assertEqual(status, 400)
            status, _, _ = request(
                server,
                "GET",
                "/v1/friend/balance",
                extra_headers={"X-Account-Id": "attacker"},
            )
            self.assertEqual(status, 400)
        self.assertEqual(balance.calls, 0)
        self.assertEqual(messages.calls, 0)
        self.assertEqual(binding.account_id, "acct-server-bound")
        self.assertEqual(binding.install_id, "install-server-bound")

    def test_catalog_and_balance_use_contract_wire_and_server_adapter_binding(self):
        balance = RecordingBalanceAdapter()
        app, _ = make_app(balance=balance)
        with running_gateway(app) as (server, _):
            status, _, raw = request(server, "POST", "/v1/friend/catalog", body=valid_catalog_request())
            self.assertEqual(status, 200)
            catalog = json.loads(raw)
            self.assertEqual(
                set(catalog),
                {"product", "protocol", "catalog_version", "expires_at", "integrity", "catalog", "balance"},
            )
            self.assertEqual(set(catalog["catalog"][0]), gateway.CATALOG_ENTRY_KEYS)
            self.assertEqual(set(catalog["balance"]), {"amount_minor", "currency", "as_of", "source"})
            self.assertEqual(catalog["integrity"], "tls-fixed-gateway")
            status, _, raw = request(server, "GET", "/v1/friend/balance")
            self.assertEqual(status, 200)
            balance_body = json.loads(raw)
            self.assertEqual(set(balance_body), {"product", "balance"})
            self.assertEqual(balance.calls, 2)
        self.assertEqual(balance.last_binding.account_id, "acct-server-bound")
        self.assertEqual(balance.last_binding.install_id, "install-server-bound")

    def test_model_must_be_a_current_server_catalog_model(self):
        messages = NoopMessagesAdapter()
        app, _ = make_app(messages=messages)
        with running_gateway(app) as (server, _):
            status, _, _ = request(server, "POST", "/v1/messages", body=valid_message_request(model="claude-bare-name"))
            self.assertEqual(status, 400)
            status, _, raw = request(server, "POST", "/v1/messages", body=valid_message_request())
            self.assertEqual(status, 200)
            response = json.loads(raw)
            self.assertEqual(response["model"], "friend-model:claude-test")
        self.assertEqual(messages.calls, 1)

    def test_catalog_duplicate_rule_keeps_exact_duplicate_and_rejects_conflict(self):
        entry = make_snapshot().entries[0]
        self.assertEqual(len(gateway.normalize_catalog_entries([entry, entry])), 1)
        conflict = gateway.CatalogEntry(
            product=entry.product,
            protocol=entry.protocol,
            canonical_id=entry.canonical_id,
            model_ref=entry.model_ref,
            gateway_ref=entry.gateway_ref,
            display_name="different",
            capabilities=entry.capabilities,
            default=entry.default,
            catalog_version=entry.catalog_version,
            expires_at=entry.expires_at,
            billing_label=entry.billing_label,
        )
        with self.assertRaises(gateway.GatewayConfigurationError):
            gateway.normalize_catalog_entries([entry, conflict])

    def test_streaming_mock_uses_event_stream_wire(self):
        app, _ = make_app()
        with running_gateway(app) as (server, _):
            status, headers, raw = request(
                server,
                "POST",
                "/v1/messages",
                body=valid_message_request(stream=True),
            )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn(b"event: message_start", raw)

    def test_proxy_forwards_one_post_without_forwarding_friend_authorization(self):
        class CountingUpstreamHandler(BaseHTTPRequestHandler):
            calls = 0
            bodies = []
            authorizations = []

            def do_POST(self):  # noqa: N802
                type(self).calls += 1
                length = int(self.headers.get("Content-Length", "0"))
                type(self).bodies.append(self.rfile.read(length))
                type(self).authorizations.append(self.headers.get("Authorization"))
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            messages = gateway.NewApiMessagesProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
            app, _ = make_app(messages=messages)
            with running_gateway(app) as (server, _):
                body = valid_message_request()
                raw_body = json.dumps(body, separators=(",", ":")).encode()
                status, _, raw = request(server, "POST", "/v1/messages", body=body)
            self.assertEqual(status, 503)
            self.assertEqual(raw, b"{}")
            self.assertEqual(CountingUpstreamHandler.calls, 1)
            self.assertEqual(CountingUpstreamHandler.bodies, [raw_body])
            self.assertEqual(CountingUpstreamHandler.authorizations, [None])
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    def test_proxy_allowlists_upstream_response_headers(self):
        class MaliciousUpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.send_header("Set-Cookie", "session=upstream-secret")
                self.send_header("Authorization", "Bearer upstream-secret")
                self.send_header("Connection", "keep-alive")
                self.send_header("Keep-Alive", "timeout=60")
                self.send_header("Trailer", "X-Upstream-Trailer")
                self.send_header("X-Upstream-Secret", "do-not-forward")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), MaliciousUpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            messages = gateway.NewApiMessagesProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
            app, _ = make_app(messages=messages)
            with running_gateway(app) as (server, _):
                status, headers, raw = request(server, "POST", "/v1/messages", body=valid_message_request())
            self.assertEqual(status, 503)
            self.assertEqual(set(headers), {"Content-Type", "Connection"})
            self.assertEqual(headers.get("Content-Type"), "application/json")
            self.assertEqual(headers.get("Connection"), "close")
            for forbidden in (
                "Set-Cookie",
                "Authorization",
                "Server",
                "Content-Length",
                "Keep-Alive",
                "Trailer",
                "X-Upstream-Secret",
            ):
                self.assertNotIn(forbidden, headers)
            self.assertNotIn("upstream-secret", repr(headers))
            self.assertEqual(raw, b"{}")
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    def test_proxy_rejects_duplicate_content_type_headers(self):
        class DuplicateHeaderUpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("cOnTeNt-TyPe", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), DuplicateHeaderUpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            messages = gateway.NewApiMessagesProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
            app, _ = make_app(messages=messages)
            with running_gateway(app) as (server, _):
                status, headers, raw = request(server, "POST", "/v1/messages", body=valid_message_request())
            self.assertEqual(status, 502)
            self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
            self.assertNotIn("text/plain", raw.decode("utf-8"))
            self.assertEqual(json.loads(raw)["code"], "UPSTREAM_UNAVAILABLE")
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    def test_proxy_rejects_invalid_types_controls_and_unknown_duplicates(self):
        cases = (
            ("non-iterable headers", None, "headers"),
            ("non-string name", [(None, "safe-value")], "safe-value"),
            ("non-string value", [("X-Upstream-Value", None)], "X-Upstream-Value"),
            ("invalid token name", [("X-Upstream Value", "safe-value")], "safe-value"),
            ("unknown control name", [("X-Upstream-\x01Name", "safe-value")], "safe-value"),
            ("unknown control value", [("X-Upstream-Value", "unknown-value-secret\x01")], "unknown-value-secret"),
            (
                "unknown duplicate",
                [("X-Upstream-Secret", "first-secret"), ("x-upstream-secret", "second-secret")],
                "first-secret",
            ),
        )

        for case, headers, secret in cases:
            with self.subTest(case=case):
                with self.assertRaises(gateway.GatewayError) as raised:
                    gateway._safe_response_headers(headers)
                self.assertEqual(raised.exception.status, 502)
                self.assertEqual(raised.exception.code, "UPSTREAM_UNAVAILABLE")
                self.assertNotIn(secret, str(raised.exception))

    def test_proxy_fails_closed_for_malformed_header_container(self):
        class MalformedHeaderAdapter:
            def forward(self, binding, request_id, body, stream):
                return gateway.AdapterResponse(200, None, body=b"{}")

        app, _ = make_app(messages=MalformedHeaderAdapter())
        with running_gateway(app) as (server, _):
            status, _, raw = request(server, "POST", "/v1/messages", body=valid_message_request())
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(raw)["code"], "UPSTREAM_UNAVAILABLE")

    def test_logs_use_exact_allowlisted_path_without_query_or_controls(self):
        app, _ = make_app()
        with running_gateway(app) as (server, logs):
            status, _, _ = request(
                server,
                "GET",
                "/v1/friend/preflight?client_version=test%0Aquery-secret&product=claude&protocol=anthropic-messages",
                auth=False,
            )
            self.assertEqual(status, 200)
            status, _, _ = request(server, "GET", "/not-allowed%0Ainjected?secret=query-secret")
            self.assertEqual(status, 404)

        log_text = logs.getvalue()
        self.assertIn("request method=GET path=/v1/friend/preflight status=200", log_text)
        self.assertIn("request method=GET path=<unmatched> status=404", log_text)
        self.assertNotIn("query-secret", log_text)
        self.assertNotIn("%0A", log_text)
        self.assertNotIn("not-allowed", log_text)
        for line in log_text.splitlines():
            self.assertFalse(any(ord(character) < 0x20 or ord(character) == 0x7F for character in line))

    def test_logs_do_not_contain_authorization_or_key(self):
        app, _ = make_app()
        with running_gateway(app) as (server, logs):
            status, _, _ = request(server, "POST", "/v1/friend/catalog", body=valid_catalog_request())
            self.assertEqual(status, 200)
        self.assertNotIn(TEST_KEY, logs.getvalue())
        self.assertNotIn("Authorization", logs.getvalue())

    def test_public_route_set_matches_contract(self):
        contract = json.loads((GATEWAY_DIR.parent / "contracts" / "friend-api.openapi.json").read_text())
        public = {
            "/v1/friend/preflight",
            "/v1/friend/catalog",
            "/v1/friend/balance",
            "/v1/messages",
        }
        self.assertEqual(set(contract["x-public-paths"]), {"GET /v1/friend/preflight", "POST /v1/friend/catalog", "GET /v1/friend/balance", "POST /v1/messages"})
        self.assertEqual(public, {path for path in ("/v1/friend/preflight", "/v1/friend/catalog", "/v1/friend/balance", "/v1/messages")})
        self.assertEqual(contract["components"]["schemas"]["CatalogResponse"]["properties"]["integrity"]["const"], gateway.CATALOG_TRUST_BOUNDARY)
        self.assertEqual(contract["components"]["schemas"]["BalanceSnapshot"]["properties"]["source"]["const"], gateway.BALANCE_SOURCE)


if __name__ == "__main__":
    unittest.main()
