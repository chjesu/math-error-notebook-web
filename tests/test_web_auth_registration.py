from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from services.web_auth import (
    AuthConfig,
    InMemoryCaptchaVerifier,
    InMemoryRegistrationStore,
    RecordingSmsSender,
    RegistrationService,
    normalize_cn_mobile,
)
from services.web_auth.registration import RegistrationStatus, SendCodeStatus, User


NOW = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)


class RegistrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.service = RegistrationService(
            store=self.store,
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier({"captcha-once"}),
            secret_pepper=b"p" * 32,
            config=AuthConfig(captcha_after_phone_day=2, captcha_after_ip_hour=4),
        )

    def request(self, *, now: datetime = NOW, captcha: str | None = None):
        return self.service.request_code(
            phone="+86 138-0013-8000",
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            captcha_token=captcha,
            now=now,
        )

    def register(self, challenge_id: str, code: str, *, now: datetime | None = None):
        return self.service.register(
            challenge_id=challenge_id,
            phone="13800138000",
            code=code,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=now or NOW + timedelta(seconds=30),
        )

    def test_normalizes_supported_cn_mobile_forms(self) -> None:
        self.assertEqual(normalize_cn_mobile("+86 138-0013-8000"), "13800138000")
        self.assertEqual(normalize_cn_mobile("8613800138000"), "13800138000")
        with self.assertRaises(ValueError):
            normalize_cn_mobile("12345")

    def test_plaintext_code_and_phone_are_not_persisted_or_audited(self) -> None:
        result = self.request()
        code = self.sender.deliveries[0][1]
        persisted = json.dumps(
            {"challenge": vars(self.store.challenges[result.challenge_id]), "audit": [vars(item) for item in self.store.audit_events]},
            default=str,
        )
        self.assertNotIn(code, persisted)
        self.assertNotIn("13800138000", persisted)
        self.assertIn("138****8000", persisted)

    def test_verification_creates_minimal_account_without_profile_fields(self) -> None:
        sent = self.request()
        result = self.register(sent.challenge_id, self.sender.deliveries[0][1])
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        user = next(iter(self.store.users_by_phone.values()))
        self.assertEqual(user.status, "active")
        self.assertEqual(set(vars(user)), {"user_id", "phone_hash", "phone_last4", "created_at", "status"})

    def test_verification_is_single_use_and_existing_phone_reuses_account(self) -> None:
        first = self.request()
        code = self.sender.deliveries[0][1]
        complete = self.register(first.challenge_id, code)
        replay = self.register(first.challenge_id, code)
        self.assertEqual(replay.status, RegistrationStatus.INVALID_CODE)

        second = self.request(now=NOW + timedelta(seconds=61))
        login = self.register(second.challenge_id, self.sender.deliveries[-1][1], now=NOW + timedelta(seconds=91))
        self.assertEqual(login.status, RegistrationStatus.COMPLETE)
        self.assertEqual(login.user_id, complete.user_id)

    def test_locked_account_cannot_create_session(self) -> None:
        sent = self.request()
        phone_hash = self.store.challenges[sent.challenge_id].phone_hash
        self.store.users_by_phone[phone_hash] = User("locked-user", phone_hash, "8000", NOW, "locked")
        result = self.register(sent.challenge_id, self.sender.deliveries[0][1])
        self.assertEqual(result.status, RegistrationStatus.LOCKED)
        self.assertFalse(self.store.sessions)

    def test_resend_invalidates_previous_code(self) -> None:
        first = self.request()
        old_code = self.sender.deliveries[-1][1]
        self.request(now=NOW + timedelta(seconds=61))
        self.assertEqual(self.store.challenges[first.challenge_id].status, "cancelled")
        self.assertEqual(self.register(first.challenge_id, old_code).status, RegistrationStatus.INVALID_CODE)

    def test_invalid_attempts_lock_challenge(self) -> None:
        sent = self.request()
        for _ in range(4):
            self.assertEqual(self.register(sent.challenge_id, "000000").status, RegistrationStatus.INVALID_CODE)
        self.assertEqual(self.register(sent.challenge_id, "000000").status, RegistrationStatus.LOCKED)
        self.assertEqual(self.register(sent.challenge_id, self.sender.deliveries[0][1]).status, RegistrationStatus.LOCKED)

    def test_expired_code_cannot_create_session(self) -> None:
        sent = self.request()
        result = self.register(sent.challenge_id, self.sender.deliveries[0][1], now=NOW + timedelta(minutes=6))
        self.assertEqual(result.status, RegistrationStatus.EXPIRED)
        self.assertFalse(self.store.sessions)

    def test_session_query_current_logout_and_all_logout(self) -> None:
        first = self.request()
        logged_in = self.register(first.challenge_id, self.sender.deliveries[0][1])
        self.assertIsNotNone(self.service.authenticate_session(logged_in.session_token or "", now=NOW + timedelta(minutes=1)))

        second = self.request(now=NOW + timedelta(seconds=61))
        another = self.register(second.challenge_id, self.sender.deliveries[-1][1], now=NOW + timedelta(seconds=91))
        self.assertTrue(self.service.logout(another.session_token or "", now=NOW + timedelta(minutes=2)))
        self.assertIsNone(self.service.authenticate_session(another.session_token or "", now=NOW + timedelta(minutes=2)))
        self.assertTrue(self.service.logout_all(logged_in.session_token or "", now=NOW + timedelta(minutes=2)))
        self.assertFalse(self.store.sessions)

    def test_cooldown_captcha_and_day_limit(self) -> None:
        self.assertEqual(self.request().status, SendCodeStatus.ACCEPTED)
        self.assertEqual(self.request(now=NOW + timedelta(seconds=10)).status, SendCodeStatus.RETRY_LATER)
        self.request(now=NOW + timedelta(seconds=61))
        self.assertEqual(self.request(now=NOW + timedelta(seconds=122)).status, SendCodeStatus.CAPTCHA_REQUIRED)
        self.assertEqual(self.request(now=NOW + timedelta(seconds=122), captcha="captcha-once").status, SendCodeStatus.ACCEPTED)

    def test_provider_failure_does_not_leave_active_challenge(self) -> None:
        self.sender.fail = True
        result = self.request()
        self.assertEqual(result.status, SendCodeStatus.TEMPORARILY_UNAVAILABLE)
        self.assertTrue(all(item.status not in {"pending", "sent"} for item in self.store.challenges.values()))

    def test_concurrent_requests_reserve_only_one_sms(self) -> None:
        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(lambda _: self.request().status, range(50)))
        self.assertEqual(statuses.count(SendCodeStatus.ACCEPTED), 1)
        self.assertEqual(len(self.sender.deliveries), 1)

    def test_challenge_cannot_cross_server_scope(self) -> None:
        sent = self.request()
        result = self.service.register(
            challenge_id=sent.challenge_id,
            phone="13800138000",
            code=self.sender.deliveries[0][1],
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            tenant_scope="another-registration-scope",
            now=NOW + timedelta(seconds=30),
        )
        self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)

    def test_forward_migration_removes_profile_and_guardian_schema(self) -> None:
        sql = (Path(__file__).resolve().parents[1] / "services" / "web_auth" / "migrations" / "0003_account_simplification.sql").read_text(encoding="utf-8").lower()
        self.assertIn("drop column display_name", sql)
        self.assertIn("drop column birth_date", sql)
        self.assertIn("drop table if exists guardian_consents", sql)


if __name__ == "__main__":
    unittest.main()
