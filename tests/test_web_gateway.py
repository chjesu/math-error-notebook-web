from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from hashlib import sha1
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest import mock

from services.web_app import NotebookAsgiApp
from services.web_app.gateway import (
    LoopbackHarnessGateway,
    WebSocketInvalidPayload,
    WebSocketProtocolError,
)
from services.web_auth import (
    AuthConfig,
    InMemoryCaptchaVerifier,
    InMemoryRegistrationStore,
    RecordingSmsSender,
    RegistrationService,
)
from services.web_domain import InMemoryNotebookStore, NotebookService


class RecordingHarnessHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def _record(self, body: bytes = b"") -> None:
        self.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        })

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._record()
        if self.path == "/":
            body, media_type = b"<html>Harness through gateway</html>", "text/html; charset=utf-8"
        elif self.path == "/assets/app.js":
            body, media_type = b"globalThis.harnessLoaded=true", "text/javascript; charset=utf-8"
        elif self.path == "/plugins/@deepseek-ai/dsh-client-modules/client.js":
            body, media_type = b"globalThis.harnessPluginLoaded=true", "text/javascript; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", media_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("set-cookie", "upstream_session=must-not-escape")
        self.send_header("connection", "x-upstream-hop")
        self.send_header("x-upstream-hop", "must-not-escape")
        self.send_header("x-auth-request-user", "forged-upstream-user")
        internal = f"127.0.0.1:{self.server.server_port}"
        self.send_header("content-location", f"http://{internal}/assets/current")
        self.send_header("link", f"<ws://{internal}/api/events.mux>; rel=preconnect")
        self.send_header("content-security-policy", f"connect-src 'self' ws://{internal}")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


class UnifiedGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.auth_store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.auth_service = RegistrationService(
            store=self.auth_store,
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier(),
            secret_pepper=b"p" * 32,
            config=AuthConfig(resend_cooldown_seconds=0),
        )
        self.notebook = NotebookService(InMemoryNotebookStore(), Path(self.temp.name))
        RecordingHarnessHandler.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHarnessHandler)
        self.thread = Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.app = NotebookAsgiApp(
            self.auth_service,
            self.notebook,
            allowed_hosts={"127.0.0.1"},
            require_https=False,
            harness_upstream=("127.0.0.1", self.upstream.server_port),
        )

    def tearDown(self) -> None:
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        cookie: str | None = None,
        origin: str | None = "http://127.0.0.1:8000",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        route_path, _, query = path.partition("?")
        body = json.dumps(payload or {}).encode("utf-8")
        requests = [{"type": "http.request", "body": body, "more_body": False}]
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return requests.pop(0)

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        headers = [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"x-device-id", b"gateway-test-device"),
        ]
        if origin is not None:
            headers.append((b"origin", origin.encode("ascii")))
        if cookie:
            headers.append((b"cookie", cookie.encode("ascii")))
        for name, value in (extra_headers or {}).items():
            headers.append((name.encode("ascii"), value.encode("latin-1")))
        scope = {
            "type": "http",
            "method": method,
            "path": route_path,
            "query_string": query.encode("ascii"),
            "scheme": "http",
            "client": ("127.0.0.1", 53000),
            "headers": headers,
        }
        asyncio.run(self.app(scope, receive, send))
        started = responses[0]
        response_headers = {
            key.decode("ascii"): value.decode("latin-1")
            for key, value in started["headers"]  # type: ignore[index]
        }
        response_body = b"".join(
            message.get("body", b"") for message in responses[1:]  # type: ignore[arg-type]
        )
        return int(started["status"]), response_headers, response_body

    def login(self, phone: str = "13800138000") -> str:
        requested = self.call("/v1/auth/register/otp/request", method="POST", payload={"phone": phone})
        token = json.loads(requested[2])["challenge_token"]
        completed = self.call(
            "/v1/auth/register/complete",
            method="POST",
            payload={
                "phone": phone,
                "challenge_token": token,
                "code": self.sender.deliveries[-1][1],
                "password": "safe1234",
                "terms_version": "2026-08-23",
                "privacy_version": "2026-08-23",
            },
        )
        self.assertEqual(completed[0], 200)
        return completed[1]["set-cookie"].split(";", 1)[0]

    def test_root_requires_session_before_contacting_harness(self) -> None:
        response = self.call("/")
        plugin = self.call("/plugins/@deepseek-ai/dsh-client-modules/client.js")
        manifest = self.call("/manifest.webmanifest")

        self.assertEqual((response[0], response[1]["location"]), (303, "/login"))
        self.assertEqual((plugin[0], plugin[1]["location"]), (303, "/login"))
        self.assertEqual(manifest[0], 200)
        self.assertEqual(manifest[1]["content-type"], "application/manifest+json")
        self.assertEqual(json.loads(manifest[2])["name"], "李兆霖数学错题本")
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_authenticated_root_and_assets_are_served_through_one_origin(self) -> None:
        cookie = self.login()

        root = self.call("/", cookie=cookie)
        asset = self.call("/assets/app.js", cookie=cookie)
        plugin = self.call("/plugins/@deepseek-ai/dsh-client-modules/client.js", cookie=cookie)

        self.assertEqual((root[0], asset[0], plugin[0]), (200, 200, 200))
        self.assertIn(b"Harness through gateway", root[2])
        self.assertEqual(asset[2], b"globalThis.harnessLoaded=true")
        self.assertEqual(plugin[2], b"globalThis.harnessPluginLoaded=true")
        requested_paths = [item["path"] for item in RecordingHarnessHandler.requests]
        self.assertEqual(set(requested_paths), {
            "/", "/assets/app.js", "/plugins/@deepseek-ai/dsh-client-modules/client.js",
        })
        self.assertTrue(all(requested_paths.count(path) <= 2 for path in set(requested_paths)))
        self.assertNotIn("set-cookie", root[1])
        self.assertNotIn("x-upstream-hop", root[1])
        self.assertNotIn("x-auth-request-user", root[1])
        internal_authority = f"127.0.0.1:{self.upstream.server_port}"
        for name in ("content-location", "link", "content-security-policy"):
            self.assertNotIn(internal_authority, root[1][name])
            self.assertIn("127.0.0.1:8000", root[1][name])

    def test_manifest_is_gateway_owned_for_every_method(self) -> None:
        for method in ("HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
            response = self.call("/manifest.webmanifest", method=method)
            self.assertEqual(response[0], 405, method)
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_product_runtime_assets_require_authentication(self) -> None:
        for route in (
            "/web/app.js",
            "/web/nav-icons.svg",
            "/web/vendor/katex/katex.min.js",
            "/web/vendor/katex/auto-render.min.js",
        ):
            response = self.call(route)
            self.assertEqual((response[0], response[1]["location"]), (303, "/login"), route)
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_api_proxy_rewrites_upstream_authority_and_strips_credentials(self) -> None:
        cookie = self.login()

        response = self.call(
            "/api/echo",
            method="POST",
            payload={"value": 7},
            cookie=cookie,
            extra_headers={
                "authorization": "Bearer browser-secret",
                "connection": "x-secret-hop",
                "forwarded": "for=203.0.113.1",
                "proxy-connection": "keep-alive",
                "x-auth-request-user": "forged-browser-user",
                "x-forwarded-port": "443",
                "x-lzlm-harness-internal-token": "browser-secret",
                "x-real-ip": "203.0.113.1",
                "x-secret-hop": "must-not-reach-upstream",
            },
        )

        self.assertEqual((response[0], json.loads(response[2])), (200, {"value": 7}))
        upstream = RecordingHarnessHandler.requests[-1]
        headers = upstream["headers"]
        self.assertEqual(headers["host"], f"127.0.0.1:{self.upstream.server_port}")
        self.assertEqual(headers["origin"], f"http://127.0.0.1:{self.upstream.server_port}")
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        for name in (
            "forwarded", "proxy-connection", "x-auth-request-user", "x-device-id",
            "x-forwarded-port", "x-lzlm-harness-internal-token", "x-real-ip", "x-secret-hop",
        ):
            self.assertNotIn(name, headers)

    def test_product_pages_are_no_longer_public(self) -> None:
        self.assertEqual(self.call("/errors")[0], 303)
        self.assertEqual(self.call("/errors", cookie=self.login())[0], 200)

    def test_same_origin_session_binding_rejects_cross_site_post(self) -> None:
        response = self.call(
            "/v1/harness/sessions/bind",
            method="POST",
            payload={"session_id": "session-cross-site"},
            cookie=self.login(),
            origin="http://attacker.invalid",
        )

        self.assertEqual(response[0], 403)

    def test_cross_site_safe_request_is_rejected_before_harness(self) -> None:
        response = self.call(
            "/assets/app.js",
            cookie=self.login(),
            origin="http://attacker.invalid",
        )

        self.assertEqual(response[0], 403)
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_unknown_http_method_is_rejected_before_harness(self) -> None:
        response = self.call(
            "/api/echo",
            method="PROPFIND",
            origin=None,
        )

        self.assertEqual(response[0], 405)
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_auth_routes_use_the_same_origin_policy(self) -> None:
        cross_site_login = self.call(
            "/v1/auth/login",
            method="POST",
            payload={"phone": "13800138000", "password": "safe1234"},
            origin="http://attacker.invalid",
        )
        cross_site_session = self.call(
            "/v1/session",
            cookie=self.login(),
            origin="http://attacker.invalid",
        )

        self.assertEqual((cross_site_login[0], cross_site_session[0]), (403, 403))

    def test_proxy_bounds_request_body_before_contacting_upstream(self) -> None:
        self.app.harness_gateway = LoopbackHarnessGateway(
            "127.0.0.1", self.upstream.server_port, max_request_bytes=4
        )

        response = self.call("/api/echo", method="POST", payload={"value": 7}, cookie=self.login())

        self.assertEqual(response[0], 413)
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_proxy_target_must_be_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            NotebookAsgiApp(
                self.auth_service,
                self.notebook,
                allowed_hosts={"127.0.0.1"},
                require_https=False,
                harness_upstream=("example.com", 443),
            )

    def test_http_proxy_fails_closed_when_harness_is_unavailable(self) -> None:
        cookie = self.login()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(timeout=2)

        response = self.call("/", cookie=cookie)

        self.assertEqual(response[0], 503)
        self.assertEqual(json.loads(response[2])["error"]["code"], "harness_unavailable")

    def test_safe_proxy_read_retries_one_transient_connection_reset(self) -> None:
        first_connection = mock.Mock()
        first_connection.getresponse.side_effect = ConnectionResetError(
            "simulated reset before response"
        )
        second_connection = mock.Mock()
        expected_response = mock.Mock(spec=http.client.HTTPResponse)
        second_connection.getresponse.return_value = expected_response

        with mock.patch(
            "services.web_app.gateway.http.client.HTTPConnection",
            side_effect=[first_connection, second_connection],
        ) as connection_factory:
            connection, response = asyncio.run(
                self.app.harness_gateway._open_response("GET", "/", None, {})
            )

        self.assertIs(connection, second_connection)
        self.assertIs(response, expected_response)
        self.assertEqual(connection_factory.call_count, 2)
        first_connection.close.assert_called_once_with()

    def test_proxy_mutation_is_not_retried_after_connection_reset(self) -> None:
        cookie = self.login()
        attempts = 0

        def reset(connection: http.client.HTTPConnection) -> http.client.HTTPResponse:
            nonlocal attempts
            attempts += 1
            raise ConnectionResetError("simulated reset after mutation request")

        with mock.patch.object(http.client.HTTPConnection, "getresponse", reset):
            response = self.call("/api/echo", method="POST", payload={"value": 7}, cookie=cookie)

        self.assertEqual(response[0], 503)
        self.assertEqual(attempts, 1)

    def test_safe_proxy_read_does_not_retry_connection_refusal(self) -> None:
        cookie = self.login()

        with mock.patch.object(
            http.client.HTTPConnection,
            "connect",
            side_effect=ConnectionRefusedError("simulated refusal"),
        ) as connect:
            response = self.call("/", cookie=cookie)

        self.assertEqual(response[0], 503)
        self.assertEqual(connect.call_count, 1)

    def test_websocket_events_are_bridged_after_session_validation(self) -> None:
        cookie = self.login()

        async def scenario() -> tuple[list[dict[str, object]], dict[str, str]]:
            request_headers: dict[str, str] = {}

            async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("latin-1").split("\r\n")
                request_headers.update(
                    line.split(":", 1) for line in lines[1:] if ":" in line
                )
                request_headers.update({key.lower(): value.strip() for key, value in list(request_headers.items())})
                key = request_headers["sec-websocket-key"]
                accept = base64.b64encode(
                    sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                writer.write(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                payload = b'{"type":"turn_started"}'
                writer.write(bytes((0x81, len(payload))) + payload + b"\x88\x02\x03\xe8")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(upstream, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            app = NotebookAsgiApp(
                self.auth_service,
                self.notebook,
                allowed_hosts={"127.0.0.1"},
                require_https=False,
                harness_upstream=("127.0.0.1", port),
            )
            incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            responses: list[dict[str, object]] = []

            async def receive() -> dict[str, object]:
                return await incoming.get()

            async def send(message: dict[str, object]) -> None:
                responses.append(message)

            scope = {
                "type": "websocket",
                "path": "/api/events.mux",
                "query_string": b"",
                "scheme": "ws",
                "client": ("127.0.0.1", 53001),
                "headers": [
                    (b"host", b"127.0.0.1:8000"),
                    (b"origin", b"http://127.0.0.1:8000"),
                    (b"cookie", cookie.encode("ascii")),
                ],
            }
            async with server:
                await app(scope, receive, send)
            return responses, request_headers

        responses, upstream_headers = asyncio.run(scenario())

        self.assertEqual(responses[0]["type"], "websocket.accept")
        self.assertEqual(responses[1], {"type": "websocket.send", "text": '{"type":"turn_started"}'})
        self.assertEqual(responses[-1]["type"], "websocket.close")
        self.assertEqual(upstream_headers["origin"], upstream_headers["host"].join(("http://", "")))
        self.assertNotIn("cookie", upstream_headers)

    def test_websocket_rejects_missing_session_before_contacting_upstream(self) -> None:
        async def scenario() -> list[dict[str, object]]:
            incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            responses: list[dict[str, object]] = []

            async def receive() -> dict[str, object]:
                return await incoming.get()

            async def send(message: dict[str, object]) -> None:
                responses.append(message)

            await self.app(
                {
                    "type": "websocket",
                    "path": "/api/events.mux",
                    "query_string": b"",
                    "scheme": "ws",
                    "client": ("127.0.0.1", 53002),
                    "headers": [
                        (b"host", b"127.0.0.1:8000"),
                        (b"origin", b"http://127.0.0.1:8000"),
                    ],
                },
                receive,
                send,
            )
            return responses

        responses = asyncio.run(scenario())

        self.assertEqual(responses, [{
            "type": "websocket.close",
            "code": 4401,
            "reason": "authentication required",
        }])
        self.assertEqual(RecordingHarnessHandler.requests, [])

    def test_websocket_upstream_eof_after_upgrade_is_retryable_unavailable(self) -> None:
        cookie = self.login()

        async def scenario() -> list[dict[str, object]]:
            async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                raw = await reader.readuntil(b"\r\n\r\n")
                headers = {
                    line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                    for line in raw.decode("latin-1").split("\r\n")[1:]
                    if ":" in line
                }
                accept = base64.b64encode(
                    sha1((headers["sec-websocket-key"] + LoopbackHarnessGateway._WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                writer.write((
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii"))
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(upstream, "127.0.0.1", 0)
            app = NotebookAsgiApp(
                self.auth_service,
                self.notebook,
                allowed_hosts={"127.0.0.1"},
                require_https=False,
                harness_upstream=("127.0.0.1", server.sockets[0].getsockname()[1]),
            )
            incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            responses: list[dict[str, object]] = []

            async def receive() -> dict[str, object]:
                return await incoming.get()

            async def send(message: dict[str, object]) -> None:
                responses.append(message)

            async with server:
                await asyncio.wait_for(app(self._websocket_scope(cookie), receive, send), timeout=3)
            return responses

        responses = asyncio.run(scenario())

        self.assertEqual(responses[0]["type"], "websocket.accept")
        self.assertEqual(responses[-1], {
            "type": "websocket.close",
            "code": 1013,
            "reason": "Harness unavailable",
        })

    def test_browser_disconnect_cancels_and_collects_upstream_reader(self) -> None:
        cookie = self.login()

        async def scenario() -> tuple[list[dict[str, object]], bool]:
            upstream_closed = asyncio.Event()

            async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                raw = await reader.readuntil(b"\r\n\r\n")
                headers = {
                    line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                    for line in raw.decode("latin-1").split("\r\n")[1:]
                    if ":" in line
                }
                accept = base64.b64encode(
                    sha1((headers["sec-websocket-key"] + LoopbackHarnessGateway._WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                writer.write((
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii"))
                await writer.drain()
                try:
                    await reader.read()
                finally:
                    upstream_closed.set()
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(upstream, "127.0.0.1", 0)
            app = NotebookAsgiApp(
                self.auth_service,
                self.notebook,
                allowed_hosts={"127.0.0.1"},
                require_https=False,
                harness_upstream=("127.0.0.1", server.sockets[0].getsockname()[1]),
            )
            incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            responses: list[dict[str, object]] = []

            async def receive() -> dict[str, object]:
                return await incoming.get()

            async def send(message: dict[str, object]) -> None:
                responses.append(message)

            async with server:
                await asyncio.wait_for(app(self._websocket_scope(cookie), receive, send), timeout=3)
                await asyncio.wait_for(upstream_closed.wait(), timeout=1)
            return responses, upstream_closed.is_set()

        responses, upstream_closed = asyncio.run(scenario())

        self.assertEqual(responses, [{"type": "websocket.accept"}])
        self.assertTrue(upstream_closed)

    @staticmethod
    def _websocket_scope(cookie: str) -> dict[str, object]:
        return {
            "type": "websocket",
            "path": "/api/events.mux",
            "query_string": b"",
            "scheme": "ws",
            "client": ("127.0.0.1", 53003),
            "headers": [
                (b"host", b"127.0.0.1:8000"),
                (b"origin", b"http://127.0.0.1:8000"),
                (b"cookie", cookie.encode("ascii")),
            ],
        }

    def test_websocket_close_payload_rejects_reserved_code_and_invalid_utf8(self) -> None:
        with self.assertRaises(WebSocketProtocolError):
            LoopbackHarnessGateway._decode_close_payload((1005).to_bytes(2, "big"))
        with self.assertRaises(WebSocketInvalidPayload):
            LoopbackHarnessGateway._decode_close_payload((1000).to_bytes(2, "big") + b"\xff")

    def test_websocket_rejects_noncanonical_and_high_bit_lengths(self) -> None:
        async def read(frame: bytes) -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(frame)
            reader.feed_eof()
            await self.app.harness_gateway._read_frame(reader)

        with self.assertRaises(WebSocketProtocolError):
            asyncio.run(read(b"\x82\x7e\x00\x7d"))
        with self.assertRaises(WebSocketProtocolError):
            asyncio.run(read(b"\x82\x7f\x80\x00\x00\x00\x00\x00\x00\x00"))


if __name__ == "__main__":
    unittest.main()
