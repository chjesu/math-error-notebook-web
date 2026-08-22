from __future__ import annotations

import asyncio
import json
import threading
import unittest

from services.web_auth import (
    AuthAsgiApp,
    InMemoryCaptchaVerifier,
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
            secret_pepper=b"p" * 32,
        )
        self.app = AuthAsgiApp(self.service, allowed_hosts={"example.test"})

    def call(
        self,
        path: str,
        payload: dict | None = None,
        *,
        method: str = "POST",
        cookie: str | None = None,
        client_ip: str = "203.0.113.7",
        extra_headers: list[tuple[bytes, bytes]] | None = None,
        scheme: str = "https",
        host: str = "example.test",
    ) -> tuple[int, dict[str, str], dict | None]:
        body = json.dumps(payload or {}).encode("utf-8")
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        output: list[dict] = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            output.append(message)

        headers = [(b"host", host.encode("ascii")), (b"content-type", b"application/json"), (b"x-device-id", b"browser-device-001")]
        if cookie:
            headers.append((b"cookie", cookie.encode("ascii")))
        headers.extend(extra_headers or [])
        scope = {"type": "http", "method": method, "path": path, "scheme": scheme, "client": (client_ip, 12345), "headers": headers}
        asyncio.run(self.app(scope, receive, send))
        started, finished = output
        response_headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
        parsed = json.loads(finished["body"]) if finished["body"] else None
        return started["status"], response_headers, parsed

    def login(self, phone: str = "13800138000") -> str:
        requested = self.call("/v1/auth/otp/request", {"phone": phone})
        status, headers, payload = self.call(
            "/v1/auth/otp/verify",
            {"challenge_token": requested[2]["challenge_token"], "phone": phone, "code": self.sender.deliveries[-1][1]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "authenticated", "account_status": "active", "next_action": "workbench"})
        return headers["set-cookie"].split(";", 1)[0]

    def test_request_endpoint_keeps_accepted_and_limited_shapes_equal(self) -> None:
        first = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        second = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        self.assertEqual((first[0], second[0]), (202, 202))
        self.assertEqual(set(first[2]), set(second[2]))
        self.assertEqual(first[2]["message"], second[2]["message"])

    def test_registration_work_runs_outside_the_event_loop_thread(self) -> None:
        caller_thread = threading.get_ident()
        worker_threads: list[int] = []
        original = self.service.request_code

        def wrapped(**kwargs):
            worker_threads.append(threading.get_ident())
            return original(**kwargs)

        self.service.request_code = wrapped
        try:
            self.assertEqual(self.call("/v1/auth/otp/request", {"phone": "13800138000"})[0], 202)
        finally:
            self.service.request_code = original
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], caller_thread)

    def test_provider_failure_has_same_public_shape(self) -> None:
        accepted = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        self.sender.fail = True
        failed = self.call("/v1/auth/otp/request", {"phone": "13900139000"})
        self.assertEqual((accepted[0], failed[0]), (202, 202))
        self.assertEqual(set(accepted[2]), set(failed[2]))

    def test_success_needs_no_profile_and_uses_secure_cookie(self) -> None:
        cookie = self.login()
        self.assertIn("__Host-lzlm_session=", cookie)
        requested = self.call("/v1/auth/otp/request", {"phone": "13900139000"})
        status, headers, _ = self.call(
            "/v1/auth/otp/verify",
            {"challenge_token": requested[2]["challenge_token"], "phone": "13900139000", "code": self.sender.deliveries[-1][1]},
        )
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("Secure", headers["set-cookie"])
        self.assertIn("SameSite=Lax", headers["set-cookie"])

    def test_session_query_current_logout_and_all_logout(self) -> None:
        cookie = self.login()
        status, _, payload = self.call("/v1/session", method="GET", cookie=cookie)
        self.assertEqual((status, payload), (200, {"authenticated": True, "account_status": "active"}))
        status, headers, payload = self.call("/v1/session", method="DELETE", cookie=cookie)
        self.assertEqual((status, payload), (204, None))
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assertEqual(self.call("/v1/session", method="GET", cookie=cookie)[0], 401)

        cookie = self.login("13700137000")
        self.assertEqual(self.call("/v1/sessions", method="DELETE", cookie=cookie)[0], 204)
        self.assertEqual(self.call("/v1/session", method="GET", cookie=cookie)[0], 401)

    def test_invalid_code_has_stable_error_envelope_and_no_cookie(self) -> None:
        requested = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        status, headers, payload = self.call(
            "/v1/auth/otp/verify",
            {"challenge_token": requested[2]["challenge_token"], "phone": "13800138000", "code": "000000"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_or_expired_code")
        self.assertFalse(payload["error"]["retryable"])
        self.assertNotIn("set-cookie", headers)

    def test_legacy_profile_fields_are_rejected_not_silently_stored(self) -> None:
        requested = self.call("/v1/auth/otp/request", {"phone": "13800138000"})
        status, _, payload = self.call(
            "/v1/auth/otp/verify",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": "13800138000",
                "code": self.sender.deliveries[0][1],
                "display_name": "不应接收",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertFalse(self.store.users_by_phone)

    def test_forwarded_for_header_is_not_trusted(self) -> None:
        self.call("/v1/auth/otp/request", {"phone": "13800138000"}, extra_headers=[(b"x-forwarded-for", b"198.51.100.8")])
        result = self.call("/v1/auth/otp/request", {"phone": "13800138000"}, extra_headers=[(b"x-forwarded-for", b"192.0.2.99")])
        self.assertEqual(result[0], 202)
        self.assertEqual(len(self.sender.deliveries), 1)

    def test_host_https_health_and_body_boundaries(self) -> None:
        payload = {"phone": "13800138000"}
        self.assertEqual(self.call("/v1/auth/otp/request", payload, host="evil.test")[0], 400)
        self.assertEqual(self.call("/v1/auth/otp/request", payload, scheme="http")[0], 400)
        self.assertEqual(self.call("/healthz", method="GET")[0], 200)
        original = self.app
        self.app = AuthAsgiApp(self.service, allowed_hosts={"example.test"}, max_body_bytes=8)
        try:
            status, _, response = self.call("/v1/auth/otp/request", payload)
        finally:
            self.app = original
        self.assertEqual(status, 413)
        self.assertEqual(response["error"]["code"], "request_too_large")


if __name__ == "__main__":
    unittest.main()
