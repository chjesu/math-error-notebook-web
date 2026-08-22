from __future__ import annotations

import asyncio
from datetime import date
import json
import unittest

from services.web_auth import (
    AuthAsgiApp,
    InMemoryCaptchaVerifier,
    InMemoryGuardianConsentVerifier,
    InMemoryRegistrationStore,
    RecordingSmsSender,
    RegistrationService,
)


class AuthAsgiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.service = RegistrationService(
            store=self.store,
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier(),
            guardian_consent_verifier=InMemoryGuardianConsentVerifier(),
            secret_pepper=b"p" * 32,
        )
        self.app = AuthAsgiApp(self.service, allowed_hosts={"example.test"})

    def call(
        self,
        path: str,
        payload: dict,
        *,
        client_ip: str = "203.0.113.7",
        extra_headers: list[tuple[bytes, bytes]] | None = None,
        scheme: str = "https",
        host: str = "example.test",
        method: str = "POST",
    ) -> tuple[int, dict[str, str], dict]:
        body = json.dumps(payload).encode("utf-8")
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        output: list[dict] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            output.append(message)

        headers = [
            (b"host", host.encode("ascii")),
            (b"content-type", b"application/json"),
            (b"x-device-id", b"browser-device-001"),
        ]
        headers.extend(extra_headers or [])
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": scheme,
            "client": (client_ip, 12345),
            "headers": headers,
        }
        asyncio.run(self.app(scope, receive, send))
        started, finished = output
        response_headers = {
            key.decode("ascii"): value.decode("ascii")
            for key, value in started["headers"]
        }
        return started["status"], response_headers, json.loads(finished["body"])

    def test_request_endpoint_keeps_accepted_and_limited_shapes_equal(self) -> None:
        payload = {"phone": "13800138000"}
        first = self.call("/v1/auth/otp/request", payload)
        second = self.call("/v1/auth/otp/request", payload)
        self.assertEqual((first[0], second[0]), (202, 202))
        self.assertEqual(set(first[2]), set(second[2]))
        self.assertEqual(first[2]["status"], second[2]["status"])
        self.assertEqual(first[2]["message"], second[2]["message"])

    def test_provider_failure_has_the_same_public_shape(self) -> None:
        accepted = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        self.sender.fail = True
        failed = self.call("/v1/auth/otp/request", {"phone": "13900139000"})
        self.assertEqual((accepted[0], failed[0]), (202, 202))
        self.assertEqual(set(accepted[2]), set(failed[2]))
        self.assertEqual(accepted[2]["status"], failed[2]["status"])
        self.assertEqual(accepted[2]["message"], failed[2]["message"])
        self.assertTrue(failed[2]["challenge_token"])

    def test_forwarded_for_header_is_not_trusted(self) -> None:
        payload = {"phone": "13800138000"}
        self.call(
            "/v1/auth/otp/request",
            payload,
            extra_headers=[(b"x-forwarded-for", b"198.51.100.8")],
        )
        result = self.call(
            "/v1/auth/otp/request",
            payload,
            extra_headers=[(b"x-forwarded-for", b"192.0.2.99")],
        )
        self.assertEqual(result[0], 202)
        self.assertEqual(len(self.sender.deliveries), 1)

    def test_success_uses_secure_cookie_and_never_returns_session_token(self) -> None:
        requested = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        status, headers, payload = self.call(
            "/v1/auth/otp/verify",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": "13800138000",
                "code": self.sender.deliveries[0][1],
                "display_name": "测试学生",
                "birth_date": "2000-01-01",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["next_action"], "create_or_join_family")
        self.assertEqual(payload["account_status"], "active")
        self.assertNotIn("session", payload)
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_invalid_code_has_uniform_error_and_no_cookie(self) -> None:
        requested = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        status, headers, payload = self.call(
            "/v1/auth/otp/verify",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": "13800138000",
                "code": "000000",
                "display_name": "测试学生",
                "birth_date": "2000-01-01",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "invalid_or_expired_code"})
        self.assertNotIn("set-cookie", headers)

    def test_host_https_and_content_type_are_enforced(self) -> None:
        payload = {"phone": "13800138000"}
        self.assertEqual(self.call("/v1/auth/otp/request", payload, host="evil.test")[0], 400)
        self.assertEqual(self.call("/v1/auth/otp/request", payload, scheme="http")[0], 400)

    def test_health_endpoint_obeys_host_and_https_boundary(self) -> None:
        status, _, payload = self.call("/healthz", {}, method="GET")
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        self.assertEqual(self.call("/healthz", {}, method="GET", host="evil.test")[0], 400)
        self.assertEqual(self.call("/healthz", {}, method="GET", scheme="http")[0], 400)

    def test_oversized_body_is_rejected_before_domain_call(self) -> None:
        small_app = AuthAsgiApp(
            self.service, allowed_hosts={"example.test"}, max_body_bytes=8
        )
        original = self.app
        self.app = small_app
        try:
            status, _, payload = self.call(
                "/v1/auth/otp/request", {"phone": "13800138000"}
            )
        finally:
            self.app = original
        self.assertEqual(status, 413)
        self.assertEqual(payload, {"error": "request_too_large"})
        self.assertFalse(self.sender.deliveries)


if __name__ == "__main__":
    unittest.main()
