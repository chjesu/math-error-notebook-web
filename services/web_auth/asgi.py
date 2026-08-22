"""Small ASGI adapter for the deterministic phone-registration service."""

from __future__ import annotations

import json
import secrets
from typing import Any, Awaitable, Callable

from .registration import (
    RegistrationService,
    RegistrationStatus,
    SendCodeStatus,
)


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class AuthAsgiApp:
    """Expose two JSON endpoints without coupling the domain to a framework."""

    def __init__(
        self,
        service: RegistrationService,
        *,
        allowed_hosts: set[str],
        tenant_scope: str = "public-registration",
        max_body_bytes: int = 16_384,
        require_https: bool = True,
        session_cookie: str = "__Host-lzlm_session",
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        self.service = service
        self.allowed_hosts = {item.lower() for item in allowed_hosts}
        self.tenant_scope = tenant_scope
        self.max_body_bytes = max_body_bytes
        self.require_https = require_https
        self.session_cookie = session_cookie

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        host = headers.get("host", "").split(":", 1)[0].lower()
        if host not in self.allowed_hosts:
            await self._json(send, 400, {"error": "invalid_host"})
            return
        if self.require_https and scope.get("scheme") != "https":
            await self._json(send, 400, {"error": "https_required"})
            return
        path = scope.get("path")
        if path == "/healthz":
            if scope.get("method") != "GET":
                await self._json(send, 405, {"error": "method_not_allowed"}, [(b"allow", b"GET")])
                return
            await self._json(send, 200, {"status": "ok"})
            return
        if path in {"/v1/session", "/v1/sessions"}:
            await self._session(path, scope.get("method", ""), headers, send)
            return
        if path not in {"/v1/auth/otp/request", "/v1/auth/otp/verify"}:
            await self._json(send, 404, {"error": "not_found"})
            return
        if scope.get("method") != "POST":
            await self._json(send, 405, {"error": "method_not_allowed"}, [(b"allow", b"POST")])
            return
        if headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            await self._json(send, 415, {"error": "application_json_required"})
            return
        try:
            payload = await self._read_json(receive)
            device_id = headers["x-device-id"]
            client = scope.get("client")
            if not client or not client[0]:
                raise ValueError("client address is required")
            # Deliberately ignore X-Forwarded-For. A trusted proxy middleware must
            # set scope['client']; accepting this header here enables IP spoofing.
            client_ip = str(client[0])
            if path.endswith("/request"):
                await self._request_code(send, payload, client_ip, device_id)
            else:
                await self._verify(send, payload, client_ip, device_id)
        except BodyTooLarge:
            await self._json(send, 413, {"error": "request_too_large"})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._json(send, 400, {"error": "invalid_request"})

    async def _read_json(self, receive: Receive) -> dict[str, Any]:
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                raise ValueError("invalid ASGI message")
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                raise BodyTooLarge
            if not message.get("more_body", False):
                break
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    async def _request_code(
        self, send: Send, payload: dict[str, Any], client_ip: str, device_id: str
    ) -> None:
        if set(payload) - {"phone", "captcha_token"}:
            raise ValueError("unsupported request field")
        result = self.service.request_code(
            phone=str(payload["phone"]),
            captcha_token=(str(payload["captcha_token"]) if payload.get("captcha_token") else None),
            ip_address=client_ip,
            device_id=device_id,
            tenant_scope=self.tenant_scope,
        )
        if result.status is SendCodeStatus.CAPTCHA_REQUIRED:
            await self._json(
                send,
                202,
                {
                    "status": "accepted",
                    "message": self.service.GENERIC_SEND_MESSAGE,
                    "challenge_token": None,
                    "retry_after_seconds": None,
                    "captcha_required": True,
                },
            )
            return
        await self._json(
            send,
            202,
            {
                "status": "accepted",
                "message": result.message,
                "challenge_token": result.challenge_id,
                "retry_after_seconds": result.retry_after_seconds,
                "captcha_required": False,
            },
        )

    async def _verify(
        self, send: Send, payload: dict[str, Any], client_ip: str, device_id: str
    ) -> None:
        if set(payload) - {"challenge_token", "phone", "code"}:
            raise ValueError("unsupported verification field")
        result = self.service.register(
            challenge_id=str(payload["challenge_token"]),
            phone=str(payload["phone"]),
            code=str(payload["code"]),
            ip_address=client_ip,
            device_id=device_id,
            tenant_scope=self.tenant_scope,
        )
        if result.status is not RegistrationStatus.COMPLETE or not result.session_token:
            await self._json(send, 400, {"error": "invalid_or_expired_code"})
            return
        max_age = self.service.config.session_ttl_days * 86_400
        cookie = (
            f"{self.session_cookie}={result.session_token}; Path=/; Max-Age={max_age}; "
            "HttpOnly; Secure; SameSite=Lax"
        ).encode("ascii")
        await self._json(
            send,
            200,
            {
                "status": "authenticated",
                "account_status": result.account_status,
                "next_action": "workbench",
            },
            [(b"set-cookie", cookie)],
        )

    async def _session(
        self,
        path: str,
        method: str,
        headers: dict[str, str],
        send: Send,
    ) -> None:
        allowed = "GET, DELETE" if path == "/v1/session" else "DELETE"
        if method not in ({"GET", "DELETE"} if path == "/v1/session" else {"DELETE"}):
            await self._json(send, 405, {"error": "method_not_allowed"}, [(b"allow", allowed.encode("ascii"))])
            return
        token = self._cookie(headers.get("cookie", ""), self.session_cookie)
        user = self.service.authenticate_session(token or "")
        if user is None:
            await self._json(send, 401, {"error": "authentication_required"})
            return
        if method == "GET":
            await self._json(send, 200, {"authenticated": True, "account_status": user.status})
            return
        if path == "/v1/sessions":
            self.service.logout_all(token or "")
        else:
            self.service.logout(token or "")
        expired = (
            f"{self.session_cookie}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        ).encode("ascii")
        await send({"type": "http.response.start", "status": 204, "headers": [(b"set-cookie", expired), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    def _cookie(header: str, name: str) -> str | None:
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value
        return None

    async def _json(
        self,
        send: Send,
        status: int,
        payload: dict[str, Any],
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        if isinstance(payload.get("error"), str):
            code = str(payload["error"])
            payload = {
                "error": {
                    "code": code,
                    "message": code,
                    "retryable": code in {"rate_limited", "temporarily_unavailable", "failed_retryable"},
                    "request_id": secrets.token_hex(8),
                }
            }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"content-security-policy", b"default-src 'none'"),
            (b"x-content-type-options", b"nosniff"),
        ]
        headers.extend(extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class BodyTooLarge(ValueError):
    pass
