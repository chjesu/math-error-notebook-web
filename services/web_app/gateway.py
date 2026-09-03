"""Authenticated loopback reverse proxy for the bundled Harness Web runtime."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from hashlib import sha1
import http.client
import json
import logging
import secrets
from typing import Any, Awaitable, Callable


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
LOGGER = logging.getLogger(__name__)


class HarnessRequestTooLarge(ValueError):
    pass


class WebSocketProtocolError(ValueError):
    pass


class WebSocketInvalidPayload(ValueError):
    pass


class WebSocketMessageTooLarge(ValueError):
    pass


class LoopbackHarnessGateway:
    """Proxy one fixed loopback Harness authority without forwarding credentials."""

    _HOP_HEADERS = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    _PRIVATE_REQUEST_HEADERS = {
        "authorization",
        "cf-connecting-ip",
        "client-ip",
        "cookie",
        "forwarded",
        "proxy-authorization",
        "true-client-ip",
        "via",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-lzlm-harness-internal-token",
        "x-real-ip",
    }
    _WEBSOCKET_PATHS = {"/api/events.mux", "/api/events.host"}
    _WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    _REQUEST_HEADER_ALLOWLIST = {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-encoding",
        "content-type",
        "if-match",
        "if-modified-since",
        "if-none-match",
        "if-range",
        "if-unmodified-since",
        "last-event-id",
        "origin",
        "pragma",
        "range",
        "user-agent",
    }
    _RESPONSE_HEADER_ALLOWLIST = {
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-encoding",
        "content-language",
        "content-length",
        "content-location",
        "content-range",
        "content-security-policy",
        "content-type",
        "cross-origin-embedder-policy",
        "cross-origin-opener-policy",
        "cross-origin-resource-policy",
        "etag",
        "expires",
        "last-modified",
        "link",
        "location",
        "permissions-policy",
        "referrer-policy",
        "retry-after",
        "vary",
        "x-content-type-options",
        "x-frame-options",
    }

    def __init__(
        self,
        host: str,
        port: int,
        *,
        max_request_bytes: int = 300 * 1024 * 1024,
        max_websocket_frame_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("Harness upstream must be a loopback IP literal")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid Harness upstream port")
        self.host = host
        self.port = port
        self.max_request_bytes = max_request_bytes
        self.max_websocket_frame_bytes = max_websocket_frame_bytes

    @property
    def authority(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    async def http(self, scope: dict[str, Any], receive: Receive, send: Send, target_path: str) -> None:
        connection: http.client.HTTPConnection | None = None
        try:
            try:
                body = await self._request_body(scope, receive)
                headers = self._request_headers(scope)
                method = str(scope.get("method", "GET"))
                connection, response = await self._open_response(
                    method,
                    self._target(scope, target_path),
                    body if body else None,
                    headers,
                )
            except HarnessRequestTooLarge:
                await self._payload_too_large(send)
                return
            except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
                LOGGER.debug("Harness HTTP upstream request failed", exc_info=exc)
                await self._unavailable(send)
                return

            response_headers = self._response_headers(
                response.getheaders(),
                public_authority=self._header_map(scope).get("host", ""),
                public_scheme=str(scope.get("scheme", "http")),
            )
            await send({
                "type": "http.response.start",
                "status": response.status,
                "headers": response_headers,
            })
            while True:
                chunk = await asyncio.to_thread(response.read1, 64 * 1024)
                if not chunk:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        finally:
            if connection is not None:
                await asyncio.to_thread(connection.close)

    async def _open_response(
        self,
        method: str,
        target: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        safe_retry = method.upper() in {"GET", "HEAD", "OPTIONS"}
        for attempt in range(2 if safe_retry else 1):
            connection = http.client.HTTPConnection(self.host, self.port, timeout=300)
            try:
                await asyncio.to_thread(connection.request, method, target, body, headers)
                response = await asyncio.to_thread(connection.getresponse)
                return connection, response
            except (ConnectionResetError, http.client.RemoteDisconnected):
                await asyncio.to_thread(connection.close)
                if attempt == 0 and safe_retry:
                    await asyncio.sleep(0.02)
                    continue
                raise
            except asyncio.CancelledError:
                await asyncio.to_thread(connection.close)
                raise
            except Exception:
                await asyncio.to_thread(connection.close)
                raise
        raise RuntimeError("unreachable Harness retry state")

    async def websocket(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        target_path: str,
    ) -> None:
        if target_path not in self._WEBSOCKET_PATHS:
            await send({"type": "websocket.close", "code": 1008, "reason": "unsupported websocket path"})
            return
        writer: asyncio.StreamWriter | None = None
        accepted = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5
            )
            subprotocol = await self._websocket_handshake(scope, reader, writer, target_path)
            accept: dict[str, Any] = {"type": "websocket.accept"}
            if subprotocol:
                accept["subprotocol"] = subprotocol
            await send(accept)
            accepted = True
            browser_disconnected = asyncio.Event()
            upstream = asyncio.create_task(
                self._upstream_frames(reader, writer, send, browser_disconnected)
            )
            downstream = asyncio.create_task(
                self._browser_frames(receive, writer, browser_disconnected)
            )
            tasks = (upstream, downstream)
            try:
                _, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                if downstream.done() and browser_disconnected.is_set() and upstream in pending:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(upstream), timeout=1)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    if browser_disconnected.is_set():
                        continue
                    raise result
        except WebSocketMessageTooLarge:
            await self._websocket_failure(send, writer, accepted, 1009, "message too large")
        except WebSocketInvalidPayload:
            await self._websocket_failure(send, writer, accepted, 1007, "invalid payload")
        except WebSocketProtocolError:
            await self._websocket_failure(send, writer, accepted, 1002, "protocol error")
        except (OSError, TimeoutError, EOFError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await self._websocket_failure(
                send, writer, accepted, 1013, "Harness unavailable"
            )
        except (UnicodeError, ValueError):
            await self._websocket_failure(send, writer, accepted, 1011, "Harness connection failed")
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)

    async def _request_body(self, scope: dict[str, Any], receive: Receive) -> bytes:
        declared = self._header_map(scope).get("content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError as exc:
                raise ValueError("invalid Harness request length") from exc
            if declared_length < 0:
                raise ValueError("invalid Harness request length")
            if declared_length > self.max_request_bytes:
                raise HarnessRequestTooLarge("Harness request too large")
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise OSError("client disconnected")
            if message.get("type") != "http.request":
                raise ValueError("invalid ASGI request body")
            chunk = bytes(message.get("body", b""))
            received += len(chunk)
            if received > self.max_request_bytes:
                raise HarnessRequestTooLarge("Harness request too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    def _request_headers(self, scope: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        connection_tokens = {
            token.strip().lower()
            for raw_name, raw_value in scope.get("headers", ())
            if raw_name.decode("latin-1").lower() == "connection"
            for token in raw_value.decode("latin-1").split(",")
            if token.strip()
        }
        for raw_name, raw_value in scope.get("headers", ()):
            name = raw_name.decode("latin-1").lower()
            if (
                name in self._HOP_HEADERS
                or name in connection_tokens
                or name in self._PRIVATE_REQUEST_HEADERS
                or name.startswith("x-forwarded-")
                or name in {"host", "content-length"}
                or name.startswith("sec-websocket-")
                or name not in self._REQUEST_HEADER_ALLOWLIST
            ):
                continue
            headers[name] = raw_value.decode("latin-1")
        if "origin" in headers:
            headers["origin"] = f"http://{self.authority}"
        return headers

    def _response_headers(
        self,
        values: list[tuple[str, str]],
        *,
        public_authority: str,
        public_scheme: str,
    ) -> list[tuple[bytes, bytes]]:
        headers: list[tuple[bytes, bytes]] = []
        if public_scheme not in {"http", "https"}:
            public_scheme = "http"
        public_websocket_scheme = "wss" if public_scheme == "https" else "ws"
        connection_tokens = {
            token.strip().lower()
            for name, value in values
            if name.lower() == "connection"
            for token in value.split(",")
            if token.strip()
        }
        for name, value in values:
            lowered = name.lower()
            if (
                lowered in self._HOP_HEADERS
                or lowered in connection_tokens
                or lowered in {"set-cookie", "server"}
                or lowered not in self._RESPONSE_HEADER_ALLOWLIST
            ):
                continue
            if lowered == "location":
                for prefix in (
                    f"http://{self.authority}",
                    f"https://{self.authority}",
                ):
                    if value.startswith(prefix):
                        value = value[len(prefix):] or "/"
                        break
            if self.authority in value:
                if not public_authority:
                    continue
                replacements = (
                    (f"http://{self.authority}", f"{public_scheme}://{public_authority}"),
                    (f"https://{self.authority}", f"{public_scheme}://{public_authority}"),
                    (f"ws://{self.authority}", f"{public_websocket_scheme}://{public_authority}"),
                    (f"wss://{self.authority}", f"{public_websocket_scheme}://{public_authority}"),
                )
                for internal, public in replacements:
                    value = value.replace(internal, public)
                value = value.replace(self.authority, public_authority)
            headers.append((lowered.encode("ascii"), value.encode("latin-1")))
        if not any(name == b"x-content-type-options" for name, _ in headers):
            headers.append((b"x-content-type-options", b"nosniff"))
        headers.append((b"referrer-policy", b"same-origin"))
        return headers

    def _target(self, scope: dict[str, Any], target_path: str) -> str:
        query = bytes(scope.get("query_string", b""))
        return target_path if not query else f"{target_path}?{query.decode('ascii')}"

    async def _websocket_handshake(
        self,
        scope: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target_path: str,
    ) -> str | None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        query = bytes(scope.get("query_string", b""))
        target = target_path if not query else f"{target_path}?{query.decode('ascii')}"
        headers = self._header_map(scope)
        request = [
            f"GET {target} HTTP/1.1",
            f"Host: {self.authority}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"Origin: http://{self.authority}",
        ]
        protocols = headers.get("sec-websocket-protocol")
        if protocols:
            request.append(f"Sec-WebSocket-Protocol: {protocols}")
        writer.write(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        if len(raw) > 16 * 1024:
            raise ValueError("oversized websocket handshake")
        lines = raw.decode("latin-1").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101 "):
            raise ValueError("upstream rejected websocket")
        response_headers = {
            name.lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for name, value in (line.split(":", 1),)
        }
        expected = base64.b64encode(sha1((key + self._WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        if not secrets.compare_digest(response_headers.get("sec-websocket-accept", ""), expected):
            raise ValueError("invalid websocket accept")
        if response_headers.get("upgrade", "").lower() != "websocket":
            raise ValueError("invalid websocket upgrade")
        connection_tokens = {
            item.strip().lower()
            for item in response_headers.get("connection", "").split(",")
        }
        if "upgrade" not in connection_tokens:
            raise ValueError("invalid websocket connection header")
        selected = response_headers.get("sec-websocket-protocol")
        if selected and selected not in {item.strip() for item in (protocols or "").split(",") if item.strip()}:
            raise ValueError("invalid websocket subprotocol")
        return selected

    async def _upstream_frames(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send: Send,
        browser_disconnected: asyncio.Event,
    ) -> None:
        fragmented_opcode: int | None = None
        fragments: list[bytes] = []
        while True:
            fin, opcode, payload = await self._read_frame(reader)
            if opcode == 0x8:
                code, reason = self._decode_close_payload(payload)
                with suppress(OSError):
                    await self._write_frame(writer, 0x8, payload)
                if not browser_disconnected.is_set():
                    await send({"type": "websocket.close", "code": code, "reason": reason})
                return
            if opcode == 0x9:
                await self._write_frame(writer, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                if fragmented_opcode is not None:
                    raise WebSocketProtocolError("nested websocket fragment")
                if fin:
                    await self._send_message(send, opcode, payload)
                else:
                    fragmented_opcode, fragments = opcode, [payload]
                continue
            if opcode == 0x0 and fragmented_opcode is not None:
                fragments.append(payload)
                if sum(map(len, fragments)) > self.max_websocket_frame_bytes:
                    raise WebSocketMessageTooLarge("websocket message too large")
                if fin:
                    await self._send_message(send, fragmented_opcode, b"".join(fragments))
                    fragmented_opcode, fragments = None, []
                continue
            raise WebSocketProtocolError("unsupported websocket frame")

    async def _browser_frames(
        self,
        receive: Receive,
        writer: asyncio.StreamWriter,
        browser_disconnected: asyncio.Event,
    ) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                browser_disconnected.set()
                try:
                    code = int(message.get("code", 1000))
                except (TypeError, ValueError):
                    code = 1005
                payload = code.to_bytes(2, "big") if self._valid_close_code(code) else b""
                await self._write_frame(writer, 0x8, payload)
                return
            if message_type != "websocket.receive":
                raise WebSocketProtocolError("invalid ASGI websocket event")
            if message.get("text") is not None:
                payload = str(message["text"]).encode("utf-8")
            elif message.get("bytes") is not None:
                payload = bytes(message["bytes"])
            else:
                raise WebSocketProtocolError("empty ASGI websocket message")
            if len(payload) > self.max_websocket_frame_bytes:
                raise WebSocketMessageTooLarge("websocket message too large")
            await self._write_frame(writer, 0x1 if message.get("text") is not None else 0x2, payload)

    async def _read_frame(self, reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
        first, second = await reader.readexactly(2)
        fin, opcode, masked = bool(first & 0x80), first & 0x0F, bool(second & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError("unexpected websocket extension bits")
        if masked:
            raise WebSocketProtocolError("masked upstream websocket frame")
        if opcode not in {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}:
            raise WebSocketProtocolError("reserved websocket opcode")
        length_code = second & 0x7F
        length = length_code
        if length_code == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
            if length < 126:
                raise WebSocketProtocolError("non-canonical websocket length")
        elif length_code == 127:
            encoded_length = await reader.readexactly(8)
            if encoded_length[0] & 0x80:
                raise WebSocketProtocolError("invalid websocket 64-bit length")
            length = int.from_bytes(encoded_length, "big")
            if length <= 0xFFFF:
                raise WebSocketProtocolError("non-canonical websocket length")
        if opcode >= 0x8 and (not fin or length > 125):
            raise WebSocketProtocolError("invalid websocket control frame")
        if opcode == 0x8 and length == 1:
            raise WebSocketProtocolError("invalid websocket close frame")
        if length > self.max_websocket_frame_bytes:
            raise WebSocketMessageTooLarge("websocket frame too large")
        return fin, opcode, await reader.readexactly(length)

    @staticmethod
    async def _send_message(send: Send, opcode: int, payload: bytes) -> None:
        if opcode == 0x1:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WebSocketInvalidPayload("invalid websocket text") from exc
            await send({"type": "websocket.send", "text": text})
        else:
            await send({"type": "websocket.send", "bytes": payload})

    @classmethod
    def _decode_close_payload(cls, payload: bytes) -> tuple[int, str]:
        if not payload:
            return 1000, ""
        if len(payload) == 1:
            raise WebSocketProtocolError("invalid websocket close frame")
        code = int.from_bytes(payload[:2], "big")
        if not cls._valid_close_code(code):
            raise WebSocketProtocolError("invalid websocket close code")
        try:
            reason = payload[2:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebSocketInvalidPayload("invalid websocket close reason") from exc
        return code, reason

    @staticmethod
    def _valid_close_code(code: int) -> bool:
        return (1000 <= code <= 1014 and code not in {1004, 1005, 1006}) or 3000 <= code <= 4999

    async def _websocket_failure(
        self,
        send: Send,
        writer: asyncio.StreamWriter | None,
        accepted: bool,
        code: int,
        reason: str,
    ) -> None:
        if not accepted:
            await send({"type": "websocket.close", "code": 1013, "reason": "Harness unavailable"})
            return
        if writer is not None:
            with suppress(OSError):
                await self._write_frame(writer, 0x8, code.to_bytes(2, "big") + reason.encode("utf-8"))
        await send({"type": "websocket.close", "code": code, "reason": reason})

    @staticmethod
    async def _write_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        writer.write(header + mask + masked)
        await writer.drain()

    @staticmethod
    def _header_map(scope: dict[str, Any]) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }

    @staticmethod
    async def _unavailable(send: Send) -> None:
        body = json.dumps({
            "error": {
                "code": "harness_unavailable",
                "message": "harness_unavailable",
                "retryable": True,
                "request_id": secrets.token_hex(8),
            }
        }, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _payload_too_large(send: Send) -> None:
        body = b'{"error":{"code":"payload_too_large","message":"payload_too_large","retryable":false}}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
