from __future__ import annotations

import json
from urllib.parse import parse_qs
import unittest

from services.web_auth import TurnstileCaptchaVerifier


class Transport:
    def __init__(self, result: object) -> None:
        self.payload = json.dumps(result).encode("utf-8")
        self.calls: list[tuple[bytes, float]] = []

    def __call__(self, body: bytes, timeout: float) -> bytes:
        self.calls.append((body, timeout))
        return self.payload


class TurnstileCaptchaVerifierTests(unittest.TestCase):
    def verifier(self, result: object) -> tuple[TurnstileCaptchaVerifier, Transport]:
        transport = Transport(result)
        return (
            TurnstileCaptchaVerifier(
                secret="secret-from-server-store",
                allowed_hostnames={"app.example.cn"},
                expected_action="otp_request",
                transport=transport,
            ),
            transport,
        )

    def test_requires_success_matching_hostname_and_action(self) -> None:
        verifier, transport = self.verifier(
            {"success": True, "hostname": "app.example.cn", "action": "otp_request"}
        )
        self.assertTrue(verifier.verify("browser-token", ip_hash="i", phone_hash="p"))
        fields = parse_qs(transport.calls[0][0].decode("ascii"))
        self.assertEqual(fields["response"], ["browser-token"])
        self.assertEqual(fields["secret"], ["secret-from-server-store"])
        self.assertEqual(transport.calls[0][1], 5.0)

    def test_fails_closed_on_hostname_action_or_provider_failure(self) -> None:
        cases = [
            {"success": False},
            {"success": True, "hostname": "evil.example", "action": "otp_request"},
            {"success": True, "hostname": "app.example.cn", "action": "other"},
        ]
        for result in cases:
            verifier, _ = self.verifier(result)
            self.assertFalse(verifier.verify("browser-token", ip_hash="i", phone_hash="p"))

    def test_rejects_oversized_token_without_network_call(self) -> None:
        verifier, transport = self.verifier({"success": True})
        self.assertFalse(verifier.verify("x" * 2049, ip_hash="i", phone_hash="p"))
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
