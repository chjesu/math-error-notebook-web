from __future__ import annotations

import os
from unittest import mock
from urllib.parse import parse_qs
import unittest

from services.web_auth import RuichengSmsSender, SmsProviderError


class RecordingTransport:
    def __init__(self, response: bytes = b"03,receipt-001") -> None:
        self.response = response
        self.calls: list[tuple[str, bytes, float, float]] = []

    def __call__(self, url: str, body: bytes, connect: float, read: float) -> bytes:
        self.calls.append((url, body, connect, read))
        return self.response


class RuichengSmsSenderTests(unittest.TestCase):
    def sender(self, transport: RecordingTransport) -> RuichengSmsSender:
        return RuichengSmsSender(
            username="account-from-secret-store",
            password="password-from-secret-store",
            transport=transport,
        )

    def test_posts_utf8_form_and_accepts_queued_receipt(self) -> None:
        transport = RecordingTransport()
        receipt = self.sender(transport).send_verification("13800138000", "012345", 300)
        self.assertEqual(receipt, "receipt-001")
        url, body, connect_timeout, read_timeout = transport.calls[0]
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True)
        self.assertTrue(url.endswith("/SendMT/SendMessage"))
        self.assertEqual(fields["Mobile"], ["13800138000"])
        self.assertEqual(fields["Subid"], [""])
        self.assertIn("【云派】", fields["Content"][0])
        self.assertIn("012345", fields["Content"][0])
        self.assertLessEqual(len(fields["Content"][0]), 70)
        self.assertEqual((connect_timeout, read_timeout), (3.0, 5.0))

    def test_accepts_both_documented_success_codes(self) -> None:
        for response in (b"00,receipt-a", b"03,receipt-b"):
            transport = RecordingTransport(response)
            self.assertTrue(self.sender(transport).send_verification("13800138000", "012345", 300))

    def test_rejection_does_not_leak_phone_code_or_provider_message(self) -> None:
        sender = self.sender(RecordingTransport("01,用户密码错误".encode("utf-8")))
        with self.assertRaises(SmsProviderError) as raised:
            sender.send_verification("13800138000", "012345", 300)
        message = str(raised.exception)
        self.assertNotIn("13800138000", message)
        self.assertNotIn("012345", message)
        self.assertNotIn("用户密码错误", message)

    def test_unknown_or_missing_receipt_is_rejected(self) -> None:
        for response in (b"03", b"03,", b"not-a-provider-response"):
            with self.assertRaises(SmsProviderError):
                self.sender(RecordingTransport(response)).send_verification(
                    "13800138000", "012345", 300
                )

    def test_template_fails_closed_when_domain_ttl_changes(self) -> None:
        with self.assertRaises(SmsProviderError):
            self.sender(RecordingTransport()).send_verification("13800138000", "012345", 600)

    def test_environment_factory_keeps_credentials_out_of_source_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUICHENG_SMS_USERNAME": "runtime-user",
                "RUICHENG_SMS_PASSWORD": "runtime-password",
            },
            clear=True,
        ):
            sender = RuichengSmsSender.from_environment()
        self.assertEqual(sender._username, "runtime-user")
        self.assertEqual(sender._password, "runtime-password")


if __name__ == "__main__":
    unittest.main()
