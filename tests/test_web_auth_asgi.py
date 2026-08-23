from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from services.web_auth import AuthAsgiApp, AuthConfig, InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService


PROTOCOL = "2026-08-23"


class AuthAsgiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.service = RegistrationService(store=self.store, sms_sender=self.sender, captcha_verifier=InMemoryCaptchaVerifier(), secret_pepper=b"p" * 32, config=AuthConfig(resend_cooldown_seconds=0, captcha_after_phone_day=99, captcha_after_ip_hour=99))
        self.app = AuthAsgiApp(self.service, allowed_hosts={"example.test"})

    def call(
        self,
        path: str,
        payload: dict | None = None,
        *,
        cookie: str | None = None,
        method: str = "POST",
        scheme: str = "https",
        host: str = "example.test",
        origin: str | None = None,
        client_ip: str = "203.0.113.7",
        extra_headers: list[tuple[bytes, bytes]] | None = None,
        body: bytes | None = None,
    ):
        messages = [{"type": "http.request", "body": body if body is not None else json.dumps(payload or {}).encode(), "more_body": False}]
        output: list[dict] = []
        async def receive(): return messages.pop(0)
        async def send(message): output.append(message)
        headers = [(b"host", host.encode()), (b"content-type", b"application/json"), (b"x-device-id", b"browser-device-001")]
        if cookie: headers.append((b"cookie", cookie.encode()))
        if origin: headers.append((b"origin", origin.encode()))
        headers.extend(extra_headers or [])
        asyncio.run(self.app({"type":"http", "method":method, "path":path, "scheme":scheme, "client":(client_ip, 1), "headers":headers}, receive, send))
        start, body = output
        return start["status"], {k.decode():v.decode() for k,v in start["headers"]}, json.loads(body["body"]) if body["body"] else None

    def register(self) -> str:
        request = self.call("/v1/auth/register/otp/request", {"phone":"13800138000"})
        status, headers, value = self.call("/v1/auth/register/complete", {"challenge_token":request[2]["challenge_token"], "phone":"13800138000", "code":self.sender.deliveries[-1][1], "password":"safe123", "terms_version":PROTOCOL, "privacy_version":PROTOCOL})
        self.assertEqual(status, 200)
        self.assertEqual(value["next_action"], "workbench")
        return headers["set-cookie"].split(";", 1)[0]

    def test_target_endpoints_enforce_account_state_and_agreement(self) -> None:
        self.assertEqual(self.call("/v1/auth/login/otp/request", {"phone":"13800138000"})[2]["error"]["code"], "phone_not_registered")
        request = self.call("/v1/auth/register/otp/request", {"phone":"13800138000"})
        missing = self.call("/v1/auth/register/complete", {"challenge_token":request[2]["challenge_token"], "phone":"13800138000", "code":self.sender.deliveries[-1][1], "password":"safe123"})
        self.assertEqual(missing[2]["error"]["code"], "agreement_required")
        self.assertEqual(self.call("/v1/auth/register/complete", {"challenge_token":request[2]["challenge_token"], "phone":"13800138000", "code":self.sender.deliveries[-1][1], "password":"safe123", "terms_version":PROTOCOL, "privacy_version":PROTOCOL})[0], 200)
        self.assertEqual(self.call("/v1/auth/register/otp/request", {"phone":"13800138000"})[2]["error"]["code"], "phone_already_registered")

    def test_login_and_sensitive_request_are_session_bound(self) -> None:
        cookie = self.register()
        request = self.call("/v1/auth/login/otp/request", {"phone":"13800138000"})
        login = self.call("/v1/auth/login/otp/verify", {"challenge_token":request[2]["challenge_token"], "phone":"13800138000", "code":self.sender.deliveries[-1][1], "terms_version":PROTOCOL, "privacy_version":PROTOCOL})
        self.assertEqual(login[0], 200)
        self.assertEqual(self.call("/v1/auth/sensitive/otp/request", {"phone":"13800138000", "action":"export"}, origin="https://example.test")[0], 401)
        sensitive = self.call("/v1/auth/sensitive/otp/request", {"phone":"13800138000", "action":"export"}, cookie=cookie, origin="https://example.test")
        self.assertEqual((sensitive[0], self.store.challenges[sensitive[2]["challenge_token"]].purpose), (202, "sensitive_export"))

    def test_legacy_endpoints_are_not_product_contract(self) -> None:
        self.assertEqual(self.call("/v1/auth/otp/request", {"phone":"13800138000"})[0], 404)

    def test_host_https_body_limit_and_forwarded_for_boundary(self) -> None:
        self.assertEqual(self.call("/healthz", method="GET", host="evil.test")[0], 400)
        self.assertEqual(self.call("/healthz", method="GET", host="example.test:443@evil.test")[0], 400)
        self.assertEqual(self.call("/healthz", method="GET", host="example.test:bad")[0], 400)
        self.assertEqual(self.call("/healthz", method="GET", scheme="http")[0], 400)
        self.app.max_body_bytes = 8
        self.assertEqual(
            self.call("/v1/auth/register/otp/request", body=b'{"phone":"13800138000"}')[0],
            413,
        )
        self.app.max_body_bytes = 16_384
        status, _, value = self.call(
            "/v1/auth/register/otp/request",
            {"phone": "13800138000"},
            extra_headers=[(b"x-forwarded-for", b"198.51.100.99")],
        )
        self.assertEqual(status, 202)
        challenge = self.store.challenges[value["challenge_token"]]
        self.assertEqual(challenge.phone_hash, self.service._hash("phone", "13800138000"))
        self.assertEqual(
            self.store.audit_events[-1].ip_hash,
            self.service._hash("ip", "203.0.113.7"),
        )

    def test_blocking_service_work_is_offloaded(self) -> None:
        calls = []

        async def run_in_thread(function, *args, **kwargs):
            calls.append(function)
            return function(*args, **kwargs)

        with patch("services.web_auth.asgi.asyncio.to_thread", side_effect=run_in_thread):
            self.call("/v1/auth/login/otp/request", {"phone": "13800138000"})
        self.assertEqual(calls, [self.service.request_code])

    def test_provider_failure_unknown_fields_and_invalid_code_fail_closed(self) -> None:
        self.sender.fail = True
        status, _, payload = self.call(
            "/v1/auth/register/otp/request", {"phone": "13800138000"}
        )
        self.assertEqual((status, payload["error"]["code"]), (503, "temporarily_unavailable"))
        self.sender.fail = False
        self.assertEqual(
            self.call(
                "/v1/auth/register/otp/request",
                {"phone": "13900139000", "unexpected": True},
            )[2]["error"]["code"],
            "invalid_request",
        )
        requested = self.call(
            "/v1/auth/register/otp/request", {"phone": "13900139000"}
        )
        wrong = "000001" if self.sender.deliveries[-1][1] == "000000" else "000000"
        invalid = self.call(
            "/v1/auth/register/complete",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": "13900139000",
                "code": wrong,
                "password": "safe123",
                "terms_version": PROTOCOL,
                "privacy_version": PROTOCOL,
            },
        )
        self.assertEqual((invalid[0], invalid[2]["error"]["code"]), (400, "invalid_code"))

    def test_rate_limits_include_retry_after_header_and_json(self) -> None:
        self.service.config = AuthConfig(
            resend_cooldown_seconds=0,
            ip_minute_limit=1,
            captcha_after_phone_day=99,
            captcha_after_ip_hour=99,
        )
        self.call("/v1/auth/login/otp/request", {"phone": "13800138000"})
        status, headers, payload = self.call(
            "/v1/auth/login/otp/request", {"phone": "13900139000"}
        )
        self.assertEqual(status, 429)
        self.assertGreaterEqual(payload["retry_after_seconds"], 1)
        self.assertEqual(headers["retry-after"], str(payload["retry_after_seconds"]))

    def test_sensitive_rate_limit_has_retry_metadata_too(self) -> None:
        cookie = self.register()
        self.service.config = AuthConfig(
            resend_cooldown_seconds=0,
            ip_minute_limit=1,
            captcha_after_phone_day=99,
            captcha_after_ip_hour=99,
        )
        status, headers, payload = self.call(
            "/v1/auth/sensitive/otp/request",
            {"phone": "13800138000", "action": "export"},
            cookie=cookie,
            origin="https://example.test",
        )
        self.assertEqual(status, 429)
        self.assertEqual(headers["retry-after"], str(payload["retry_after_seconds"]))

    def test_sensitive_and_session_mutations_require_strict_same_origin(self) -> None:
        cookie = self.register()
        sensitive_payload = {"phone": "13800138000", "action": "export"}
        self.assertEqual(
            self.call("/v1/auth/sensitive/otp/request", sensitive_payload, cookie=cookie)[0],
            403,
        )
        self.assertEqual(
            self.call(
                "/v1/auth/sensitive/otp/request",
                sensitive_payload,
                cookie=cookie,
                origin="https://evil.test",
            )[0],
            403,
        )
        self.assertEqual(
            self.call(
                "/v1/auth/sensitive/otp/request",
                sensitive_payload,
                cookie=cookie,
                origin="https://example.test",
            )[0],
            202,
        )
        self.assertEqual(self.call("/v1/session", cookie=cookie, method="GET")[0], 200)
        self.assertEqual(self.call("/v1/session", cookie=cookie, method="DELETE")[0], 403)
        self.assertEqual(
            self.call(
                "/v1/session",
                cookie=cookie,
                method="DELETE",
                origin="https://evil.test",
            )[0],
            403,
        )
        self.assertEqual(self.call("/v1/sessions", cookie=cookie, method="DELETE")[0], 403)
        self.assertEqual(
            self.call(
                "/v1/sessions",
                cookie=cookie,
                method="DELETE",
                origin="https://example.test",
            )[0],
            204,
        )

    def test_secure_cookie_and_session_logout(self) -> None:
        request = self.call("/v1/auth/register/otp/request", {"phone":"13800138000"})
        status, headers, _ = self.call(
            "/v1/auth/register/complete",
            {
                "challenge_token": request[2]["challenge_token"],
                "phone": "13800138000",
                "code": self.sender.deliveries[-1][1],
                "password": "safe123",
                "terms_version": PROTOCOL,
                "privacy_version": PROTOCOL,
            },
        )
        self.assertEqual(status, 200)
        cookie_header = headers["set-cookie"]
        for attribute in ("Path=/", "HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(attribute, cookie_header)
        self.assertNotIn("Domain=", cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        logout = self.call(
            "/v1/session",
            cookie=cookie,
            method="DELETE",
            origin="https://example.test",
        )
        self.assertEqual(logout[0], 204)
        self.assertIn("Max-Age=0", logout[1]["set-cookie"])
        self.assertEqual(self.call("/v1/session", cookie=cookie, method="GET")[0], 401)


if __name__ == "__main__":
    unittest.main()
