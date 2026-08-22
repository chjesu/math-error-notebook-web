from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import unittest

from services.web_auth import (
    AuthConfig,
    InMemoryCaptchaVerifier,
    InMemoryGuardianConsentVerifier,
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
        self.captcha = InMemoryCaptchaVerifier({"captcha-once"})
        self.guardian = InMemoryGuardianConsentVerifier({"guardian-consent-verified-001"})
        self.service = RegistrationService(
            store=self.store,
            sms_sender=self.sender,
            captcha_verifier=self.captcha,
            guardian_consent_verifier=self.guardian,
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

    def test_normalizes_supported_cn_mobile_forms(self) -> None:
        self.assertEqual(normalize_cn_mobile("+86 138-0013-8000"), "13800138000")
        self.assertEqual(normalize_cn_mobile("8613800138000"), "13800138000")
        with self.assertRaises(ValueError):
            normalize_cn_mobile("12345")

    def test_plaintext_code_is_not_persisted_or_audited(self) -> None:
        result = self.request()
        code = self.sender.deliveries[0][1]
        persisted = json.dumps(
            {
                "challenge": vars(self.store.challenges[result.challenge_id]),
                "audit": [vars(item) for item in self.store.audit_events],
            },
            default=str,
        )
        self.assertNotIn(code, persisted)
        self.assertNotIn("13800138000", persisted)
        self.assertIn("138****8000", persisted)

    def test_resend_invalidates_previous_code(self) -> None:
        first = self.request()
        old_code = self.sender.deliveries[-1][1]
        second = self.request(now=NOW + timedelta(seconds=61))
        self.assertEqual(self.store.challenges[first.challenge_id].status, "cancelled")
        attempt = self.service.register(
            challenge_id=first.challenge_id,
            phone="13800138000",
            code=old_code,
            display_name="测试学生",
            birth_date=date(2000, 1, 1),
            guardian_consent_receipt=None,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=62),
        )
        self.assertEqual(attempt.status, RegistrationStatus.INVALID_CODE)
        self.assertIsNotNone(second.challenge_id)

    def test_cooldown_and_captcha_escalation(self) -> None:
        accepted = self.request()
        limited = self.request(now=NOW + timedelta(seconds=10))
        self.assertEqual(accepted.status, SendCodeStatus.ACCEPTED)
        self.assertEqual(limited.status, SendCodeStatus.RETRY_LATER)
        self.assertIsNotNone(limited.challenge_id)
        self.assertGreaterEqual(limited.retry_after_seconds, 50)
        self.request(now=NOW + timedelta(seconds=61))
        captcha = self.request(now=NOW + timedelta(seconds=122))
        self.assertEqual(captcha.status, SendCodeStatus.CAPTCHA_REQUIRED)
        passed = self.request(now=NOW + timedelta(seconds=122), captcha="captcha-once")
        self.assertEqual(passed.status, SendCodeStatus.ACCEPTED)

    def test_default_phone_day_limit_stops_sixth_provider_send(self) -> None:
        service = RegistrationService(
            store=InMemoryRegistrationStore(),
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier(),
            guardian_consent_verifier=self.guardian,
            secret_pepper=b"p" * 32,
            config=AuthConfig(
                phone_hour_limit=99,
                captcha_after_phone_day=99,
                captcha_after_ip_hour=99,
            ),
        )
        results = [
            service.request_code(
                phone="13800138000",
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW + timedelta(hours=offset),
            )
            for offset in range(0, 12, 2)
        ]
        self.assertEqual([item.status for item in results[:5]], [SendCodeStatus.ACCEPTED] * 5)
        self.assertEqual(results[5].status, SendCodeStatus.RETRY_LATER)
        self.assertEqual(len(self.sender.deliveries), 5)

    def test_ip_minute_and_device_day_limits_are_atomic(self) -> None:
        def reserve(store, config, index, *, ip_hash, device_hash, now):
            return store.reserve_send(
                phone_hash=f"phone-{index}",
                ip_hash=ip_hash,
                ip_prefix_hash=f"prefix-{index}",
                device_hash=device_hash,
                tenant_hash="tenant",
                now=now,
                config=config,
            )[0]

        ip_store = InMemoryRegistrationStore()
        ip_config = AuthConfig(
            phone_hour_limit=99,
            phone_day_limit=99,
            ip_minute_limit=2,
            ip_hour_limit=99,
            ip_prefix_hour_limit=99,
            device_hour_limit=99,
            device_day_limit=99,
            tenant_hour_limit=99,
        )
        self.assertEqual(
            [
                reserve(
                    ip_store,
                    ip_config,
                    index,
                    ip_hash="shared-ip",
                    device_hash=f"device-{index}",
                    now=NOW + timedelta(seconds=index),
                )
                for index in range(3)
            ],
            [True, True, False],
        )

        device_store = InMemoryRegistrationStore()
        device_config = AuthConfig(
            phone_hour_limit=99,
            phone_day_limit=99,
            ip_minute_limit=99,
            ip_hour_limit=99,
            ip_prefix_hour_limit=99,
            device_hour_limit=99,
            device_day_limit=2,
            tenant_hour_limit=99,
        )
        self.assertEqual(
            [
                reserve(
                    device_store,
                    device_config,
                    index,
                    ip_hash=f"ip-{index}",
                    device_hash="shared-device",
                    now=NOW + timedelta(hours=index),
                )
                for index in range(3)
            ],
            [True, True, False],
        )

    def test_invalid_attempts_lock_challenge(self) -> None:
        sent = self.request()
        for _ in range(4):
            result = self._register(sent.challenge_id, "000000", date(2000, 1, 1))
            self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)
        fifth = self._register(sent.challenge_id, "000000", date(2000, 1, 1))
        self.assertEqual(fifth.status, RegistrationStatus.LOCKED)
        correct = self._register(sent.challenge_id, self.sender.deliveries[0][1], date(2000, 1, 1))
        self.assertEqual(correct.status, RegistrationStatus.LOCKED)

    def test_minor_requires_guardian_then_registration_is_single_use(self) -> None:
        sent = self.request()
        code = self.sender.deliveries[0][1]
        blocked = self._register(sent.challenge_id, code, date(2012, 1, 1))
        self.assertEqual(blocked.status, RegistrationStatus.GUARDIAN_CONSENT_REQUIRED)
        forged = self._register(
            sent.challenge_id,
            code,
            date(2012, 1, 1),
            guardian_consent_receipt="client-invented-receipt",
        )
        self.assertEqual(forged.status, RegistrationStatus.GUARDIAN_CONSENT_REQUIRED)
        complete = self._register(
            sent.challenge_id,
            code,
            date(2012, 1, 1),
            guardian_consent_receipt="guardian-consent-verified-001",
        )
        self.assertEqual(complete.status, RegistrationStatus.COMPLETE)
        self.assertTrue(complete.session_token)
        reused = self._register(
            sent.challenge_id,
            code,
            date(2012, 1, 1),
            guardian_consent_receipt="guardian-consent-verified-001",
        )
        self.assertEqual(reused.status, RegistrationStatus.INVALID_CODE)

    def test_guardian_receipt_validity_is_not_revealed_before_phone_verification(self) -> None:
        sent = self.request()
        result = self._register(
            sent.challenge_id,
            "000000",
            date(2012, 1, 1),
            guardian_consent_receipt="guardian-consent-verified-001",
        )
        self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)

    def test_existing_restricted_user_cannot_change_status_with_claimed_adult_birthdate(self) -> None:
        sent = self.request()
        challenge = self.store.challenges[sent.challenge_id]
        self.store.users_by_phone[challenge.phone_hash] = User(
            user_id="restricted-user",
            phone_hash=challenge.phone_hash,
            phone_last4="8000",
            display_name="受限学生",
            birth_date=date(2012, 1, 1),
            guardian_consent_receipt=None,
            created_at=NOW,
            status="restricted",
        )
        result = self._register(
            sent.challenge_id,
            self.sender.deliveries[0][1],
            date(2000, 1, 1),
        )
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        self.assertEqual(result.account_status, "restricted")

    def test_existing_user_login_does_not_require_guardian_receipt_again(self) -> None:
        sent = self.request()
        challenge = self.store.challenges[sent.challenge_id]
        self.store.users_by_phone[challenge.phone_hash] = User(
            user_id="existing-student",
            phone_hash=challenge.phone_hash,
            phone_last4="8000",
            display_name="已有学生",
            birth_date=date(2012, 1, 1),
            guardian_consent_receipt="existing-consent",
            created_at=NOW,
            status="active",
        )
        result = self._register(
            sent.challenge_id,
            self.sender.deliveries[0][1],
            date(2012, 1, 1),
        )
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        self.assertEqual(result.account_status, "active")

    def test_provider_failure_does_not_leave_active_challenge(self) -> None:
        self.sender.fail = True
        result = self.request()
        self.assertEqual(result.status, SendCodeStatus.TEMPORARILY_UNAVAILABLE)
        self.assertTrue(all(item.status not in {"pending", "sent"} for item in self.store.challenges.values()))

    def test_expired_code_cannot_create_session(self) -> None:
        sent = self.request()
        result = self.service.register(
            challenge_id=sent.challenge_id,
            phone="13800138000",
            code=self.sender.deliveries[0][1],
            display_name="测试学生",
            birth_date=date(2000, 1, 1),
            guardian_consent_receipt=None,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(minutes=6),
        )
        self.assertEqual(result.status, RegistrationStatus.EXPIRED)
        self.assertFalse(self.store.sessions)

    def test_session_plaintext_is_returned_once_but_only_hash_is_stored(self) -> None:
        sent = self.request()
        result = self._register(sent.challenge_id, self.sender.deliveries[0][1], date(2000, 1, 1))
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        self.assertNotIn(result.session_token, self.store.sessions)
        self.assertEqual(len(self.store.sessions), 1)

    def test_challenge_cannot_cross_server_resolved_tenant_scope(self) -> None:
        sent = self.request()
        result = self.service.register(
            challenge_id=sent.challenge_id,
            phone="13800138000",
            code=self.sender.deliveries[0][1],
            display_name="测试学生",
            birth_date=date(2000, 1, 1),
            guardian_consent_receipt=None,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            tenant_scope="another-tenant",
            now=NOW + timedelta(seconds=30),
        )
        self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)
        self.assertFalse(self.store.sessions)

    def test_concurrent_requests_reserve_only_one_sms(self) -> None:
        def request_once(_: int):
            return self.service.request_code(
                phone="13800138000",
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW,
            ).status

        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(request_once, range(50)))
        self.assertEqual(statuses.count(SendCodeStatus.ACCEPTED), 1)
        self.assertEqual(len(self.sender.deliveries), 1)

    def test_mysql_migration_has_no_plaintext_otp_column(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "web_auth"
            / "migrations"
            / "0001_phone_registration.sql"
        ).read_text(encoding="utf-8")
        lowered = sql.lower()
        self.assertIn("code_hash", lowered)
        self.assertIn("session_hash", lowered)
        self.assertNotIn("plaintext", lowered.replace("plaintext is never stored", ""))
        self.assertNotRegex(lowered, r"\b(code|otp|session_token)\s+(varchar|char)")

    def _register(
        self,
        challenge_id: str,
        code: str,
        birth_date: date,
        guardian_consent_receipt: str | None = None,
    ):
        return self.service.register(
            challenge_id=challenge_id,
            phone="13800138000",
            code=code,
            display_name="测试学生",
            birth_date=birth_date,
            guardian_consent_receipt=guardian_consent_receipt,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=30),
        )


if __name__ == "__main__":
    unittest.main()
