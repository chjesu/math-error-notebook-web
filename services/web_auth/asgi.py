"""Small ASGI adapter for the deterministic phone-registration service."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

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
        authority = headers.get("host", "")
        host = self._host(authority)
        if host is None or host not in self.allowed_hosts:
            await self._json(send, 400, {"error": "invalid_host"})
            return
        if self.require_https and scope.get("scheme") != "https":
            await self._json(send, 400, {"error": "https_required"})
            return
        path = scope.get("path")
        method = scope.get("method", "")
        if (
            (path == "/v1/auth/sensitive/otp/request" and method == "POST")
            or (path in {"/v1/session", "/v1/sessions"} and method == "DELETE")
        ) and not self._same_origin(str(scope.get("scheme", "")), authority, headers.get("origin", "")):
            await self._json(send, 403, {"error": "forbidden"})
            return
        if path == "/healthz":
            if method != "GET":
                await self._json(send, 405, {"error": "method_not_allowed"}, [(b"allow", b"GET")])
                return
            await self._json(send, 200, {"status": "ok"})
            return
        if path in {"/v1/session", "/v1/sessions"}:
            await self._session(path, method, headers, send)
            return
        if path not in {
            "/v1/auth/login/otp/request", "/v1/auth/login/otp/verify",
            "/v1/auth/register/otp/request", "/v1/auth/register/complete",
            "/v1/auth/sensitive/otp/request",
        }:
            await self._json(send, 404, {"error": "not_found"})
            return
        if method != "POST":
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
            if path == "/v1/auth/sensitive/otp/request":
                token = self._cookie(headers.get("cookie", ""), self.session_cookie)
                await self._request_sensitive(send, payload, token or "", client_ip, device_id)
            elif path.endswith("/request"):
                await self._request_code(send, payload, client_ip, device_id, "login" if "/login/" in path else "register")
            elif path.endswith("/otp/verify"):
                await self._verify(send, payload, client_ip, device_id, "login")
            else:
                await self._verify(send, payload, client_ip, device_id, "register")
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
        self, send: Send, payload: dict[str, Any], client_ip: str, device_id: str, purpose: str
    ) -> None:
        if set(payload) - {"phone", "captcha_token"}:
            raise ValueError("unsupported request field")
        result = await asyncio.to_thread(
            self.service.request_code,
            purpose=purpose,
            phone=str(payload["phone"]),
            captcha_token=(str(payload["captcha_token"]) if payload.get("captcha_token") else None),
            ip_address=client_ip,
            device_id=device_id,
            tenant_scope=self.tenant_scope,
        )
        if result.status is SendCodeStatus.PHONE_NOT_REGISTERED:
            await self._json(send, 409, {"error": "phone_not_registered"})
            return
        if result.status is SendCodeStatus.PHONE_ALREADY_REGISTERED:
            await self._json(send, 409, {"error": "phone_already_registered"})
            return
        if result.status is SendCodeStatus.CAPTCHA_REQUIRED:
            await self._json(send, 428, {"error": "captcha_required"})
            return
        if result.status is SendCodeStatus.RETRY_LATER:
            await self._rate_limited(send, result.retry_after_seconds)
            return
        if result.status is SendCodeStatus.TEMPORARILY_UNAVAILABLE:
            await self._json(send, 503, {"error": "temporarily_unavailable"})
            return
        if result.status is SendCodeStatus.LOCKED:
            await self._json(send, 409, {"error": "phone_not_registered" if purpose == "login" else "phone_already_registered"})
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
        self, send: Send, payload: dict[str, Any], client_ip: str, device_id: str, purpose: str
    ) -> None:
        allowed = {"challenge_token", "phone", "code", "terms_version", "privacy_version"}
        if purpose == "register":
            allowed.add("password")
        if set(payload) - allowed:
            raise ValueError("unsupported verification field")
        method = self.service.login if purpose == "login" else self.service.complete_registration
        result = await asyncio.to_thread(method, challenge_id=str(payload["challenge_token"]), phone=str(payload["phone"]), code=str(payload["code"]), password=str(payload["password"]) if purpose == "register" and "password" in payload else None, terms_version=str(payload.get("terms_version", "")), privacy_version=str(payload.get("privacy_version", "")), ip_address=client_ip, device_id=device_id, tenant_scope=self.tenant_scope)
        if result.status is not RegistrationStatus.COMPLETE or not result.session_token:
            codes = {
                RegistrationStatus.WEAK_PASSWORD: "weak_password",
                RegistrationStatus.AGREEMENT_REQUIRED: "agreement_required",
                RegistrationStatus.EXPIRED: "code_expired",
                RegistrationStatus.LOCKED: "too_many_attempts",
                RegistrationStatus.INVALID_CODE: "invalid_code",
            }
            await self._json(send, 400, {"error": codes[result.status]})
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

    async def _request_sensitive(self, send: Send, payload: dict[str, Any], session_token: str, client_ip: str, device_id: str) -> None:
        if set(payload) - {"phone", "action", "captcha_token"}:
            raise ValueError("unsupported request field")
        result = await asyncio.to_thread(self.service.request_sensitive_code, session_token=session_token, phone=str(payload["phone"]), action=str(payload["action"]), captcha_token=str(payload["captcha_token"]) if payload.get("captcha_token") else None, ip_address=client_ip, device_id=device_id)
        if result.status is SendCodeStatus.LOCKED:
            await self._json(send, 401, {"error": "authentication_required"})
            return
        if result.status is SendCodeStatus.CAPTCHA_REQUIRED:
            await self._json(send, 428, {"error": "captcha_required"})
            return
        if result.status is SendCodeStatus.RETRY_LATER:
            await self._rate_limited(send, result.retry_after_seconds)
            return
        if result.status is SendCodeStatus.TEMPORARILY_UNAVAILABLE:
            await self._json(send, 503, {"error": "temporarily_unavailable"})
            return
        await self._json(send, 202, {"status": "accepted", "message": result.message, "challenge_token": result.challenge_id, "retry_after_seconds": result.retry_after_seconds, "captcha_required": False})

    async def _rate_limited(self, send: Send, retry_after_seconds: int | None) -> None:
        retry_after = max(1, int(retry_after_seconds or self.service.config.resend_cooldown_seconds or 1))
        await self._json(
            send,
            429,
            {"error": "rate_limited", "retry_after_seconds": retry_after},
            [(b"retry-after", str(retry_after).encode("ascii"))],
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
        user = await asyncio.to_thread(self.service.authenticate_session, token or "")
        if user is None:
            await self._json(send, 401, {"error": "authentication_required"})
            return
        if method == "GET":
            await self._json(send, 200, {"authenticated": True, "account_status": user.status})
            return
        if path == "/v1/sessions":
            await asyncio.to_thread(self.service.logout_all, token or "")
        else:
            await asyncio.to_thread(self.service.logout, token or "")
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

    @staticmethod
    def _host(authority: str) -> str | None:
        try:
            parsed = urlsplit(f"//{authority}")
            parsed.port
        except ValueError:
            return None
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return parsed.hostname.lower()

    @staticmethod
    def _same_origin(scheme: str, authority: str, origin: str) -> bool:
        try:
            parsed = urlsplit(origin)
            parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == scheme
            and parsed.netloc.lower() == authority.lower()
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
        )

    async def _json(
        self,
        send: Send,
        status: int,
        payload: dict[str, Any],
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        if isinstance(payload.get("error"), str):
            code = str(payload["error"])
            payload = {**{key: value for key, value in payload.items() if key != "error"},
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
