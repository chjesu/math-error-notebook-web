from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from services.web_auth import AuthConfig, InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService, normalize_cn_mobile
from services.web_auth.registration import RegistrationStatus, SendCodeStatus, User


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)
PHONE = "13800138000"
PROTOCOL = "2026-08-23"


class RegistrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.service = RegistrationService(store=self.store, sms_sender=self.sender, captcha_verifier=InMemoryCaptchaVerifier(), secret_pepper=b"p" * 32, config=AuthConfig(captcha_after_phone_day=99, captcha_after_ip_hour=99, scrypt_n=2**10))

    def request(self, purpose: str, phone: str = PHONE, now: datetime = NOW):
        return self.service.request_code(purpose=purpose, phone=phone, ip_address="203.0.113.7", device_id="browser-device-001", now=now)

    def register(self, *, phone: str = PHONE, now: datetime = NOW + timedelta(seconds=1)):
        sent = self.request("register", phone, now - timedelta(seconds=1))
        return self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=phone, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=now)

    def test_register_creates_password_and_agreement_in_one_completion(self) -> None:
        result = self.register()
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        self.assertIn(result.user_id, self.store.password_credentials)
        self.assertIn(result.user_id, self.store.agreements)
        self.assertNotIn("safe123", repr(self.store.password_credentials))

    def test_password_pepper_is_domain_separated_and_versioned(self) -> None:
        with patch("services.web_auth.registration.hashlib.scrypt", return_value=b"h" * 64) as scrypt:
            result = self.register()
        salt, password_hash, parameters = self.store.password_credentials[result.user_id or ""]
        derived_pepper = hmac.new(
            b"p" * 32, b"password-pepper-v1", hashlib.sha256
        ).digest()
        self.assertEqual(scrypt.call_args.kwargs["salt"], derived_pepper + salt)
        self.assertEqual(password_hash, b"h" * 64)
        self.assertIn("pepper=v1", parameters)
        self.assertNotIn(b"p" * 32, (salt, password_hash))
        self.assertNotIn(derived_pepper, (salt, password_hash))

    def test_login_only_existing_active_and_never_creates_user(self) -> None:
        self.assertEqual(self.request("login", "13700137000").status, SendCodeStatus.PHONE_NOT_REGISTERED)
        registered = self.register()
        sent = self.request("login", now=NOW + timedelta(seconds=62))
        login = self.service.login(challenge_id=sent.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=63))
        self.assertEqual((login.status, login.user_id), (RegistrationStatus.COMPLETE, registered.user_id))

    def test_register_refuses_existing_phone_and_cross_purpose_replay(self) -> None:
        self.register()
        self.assertEqual(self.request("register", now=NOW + timedelta(seconds=62)).status, SendCodeStatus.PHONE_ALREADY_REGISTERED)
        login = self.request("login", now=NOW + timedelta(seconds=123))
        cross = self.service.complete_registration(challenge_id=login.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=124))
        self.assertEqual(cross.status, RegistrationStatus.INVALID_CODE)

    def test_password_agreement_and_replay_fail_closed(self) -> None:
        sent = self.request("register")
        self.assertEqual(self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code="000000", password="123", terms_version="", privacy_version="", ip_address="203.0.113.7", device_id="browser-device-001").status, RegistrationStatus.AGREEMENT_REQUIRED)
        result = self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=1))
        self.assertEqual(result.status, RegistrationStatus.COMPLETE)
        replay = self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=2))
        self.assertEqual(replay.status, RegistrationStatus.INVALID_CODE)

    def test_protocol_version_is_fixed_strict_and_length_limited(self) -> None:
        with self.assertRaises(ValueError):
            AuthConfig(protocol_version="2026-08-22")
        sent = self.request("register")
        common = dict(
            challenge_id=sent.challenge_id or "",
            phone=PHONE,
            code=self.sender.deliveries[-1][1],
            password="safe123",
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=1),
        )
        for terms, privacy in (
            ("2026-08-22", PROTOCOL),
            (f"{PROTOCOL}x", PROTOCOL),
            ("x" * 33, PROTOCOL),
        ):
            result = self.service.complete_registration(
                terms_version=terms, privacy_version=privacy, **common
            )
            self.assertEqual(result.status, RegistrationStatus.AGREEMENT_REQUIRED)
        self.assertEqual(
            self.service.complete_registration(
                terms_version=PROTOCOL, privacy_version=PROTOCOL, **common
            ).status,
            RegistrationStatus.COMPLETE,
        )

    def test_random_and_wrong_codes_never_run_scrypt_but_still_lock(self) -> None:
        sent = self.request("register")
        wrong = "000001" if self.sender.deliveries[-1][1] == "000000" else "000000"
        common = dict(
            phone=PHONE,
            password="safe123",
            terms_version=PROTOCOL,
            privacy_version=PROTOCOL,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=1),
        )
        with patch("services.web_auth.registration.hashlib.scrypt") as scrypt:
            random_result = self.service.complete_registration(
                challenge_id="f" * 32, code=wrong, **common
            )
            self.assertEqual(random_result.status, RegistrationStatus.INVALID_CODE)
            for expected in (
                RegistrationStatus.INVALID_CODE,
                RegistrationStatus.INVALID_CODE,
                RegistrationStatus.INVALID_CODE,
                RegistrationStatus.INVALID_CODE,
                RegistrationStatus.LOCKED,
            ):
                result = self.service.complete_registration(
                    challenge_id=sent.challenge_id or "", code=wrong, **common
                )
                self.assertEqual(result.status, expected)
            scrypt.assert_not_called()

    def test_concurrent_correct_registration_consumes_challenge_once(self) -> None:
        sent = self.request("register")
        barrier = threading.Barrier(2)

        def fake_scrypt(*args, **kwargs):
            del args, kwargs
            barrier.wait(timeout=5)
            return b"h" * 64

        def complete():
            return self.service.complete_registration(
                challenge_id=sent.challenge_id or "",
                phone=PHONE,
                code=self.sender.deliveries[-1][1],
                password="safe123",
                terms_version=PROTOCOL,
                privacy_version=PROTOCOL,
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW + timedelta(seconds=1),
            ).status

        with patch("services.web_auth.registration.hashlib.scrypt", side_effect=fake_scrypt) as scrypt:
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(lambda _: complete(), range(2)))
        self.assertEqual(statuses.count(RegistrationStatus.COMPLETE), 1)
        self.assertEqual(statuses.count(RegistrationStatus.INVALID_CODE), 1)
        self.assertEqual(scrypt.call_count, 2)
        self.assertEqual(len(self.store.users_by_phone), 1)

    def test_phone_budget_is_shared_between_purposes_and_is_ten_per_day(self) -> None:
        self.register()
        for index in range(1, 10):
            self.assertEqual(self.request("login", now=NOW + timedelta(seconds=3661 * index)).status, SendCodeStatus.ACCEPTED)
        self.assertEqual(self.request("login", now=NOW + timedelta(seconds=3661 * 10)).status, SendCodeStatus.RETRY_LATER)

    def test_existence_branches_consume_shared_enumeration_budget_without_sms(self) -> None:
        phones = [f"1390013900{index}" for index in range(4)]
        config = AuthConfig(
            resend_cooldown_seconds=0,
            ip_minute_limit=3,
            ip_hour_limit=3,
            device_hour_limit=99,
            device_day_limit=99,
            captcha_after_phone_day=99,
            captcha_after_ip_hour=99,
        )
        for purpose, existing, visible in (
            ("login", False, SendCodeStatus.PHONE_NOT_REGISTERED),
            ("register", True, SendCodeStatus.PHONE_ALREADY_REGISTERED),
        ):
            store = InMemoryRegistrationStore()
            sender = RecordingSmsSender()
            service = RegistrationService(
                store=store,
                sms_sender=sender,
                captcha_verifier=InMemoryCaptchaVerifier(),
                secret_pepper=b"p" * 32,
                config=config,
            )
            if existing:
                for index, phone in enumerate(phones):
                    phone_hash = service._hash("phone", phone)
                    store.users_by_phone[phone_hash] = User(
                        f"user-{index}", phone_hash, phone[-4:], NOW
                    )
            statuses = [
                service.request_code(
                    purpose=purpose,
                    phone=phone,
                    ip_address="203.0.113.7",
                    device_id="enumeration-device",
                    now=NOW + timedelta(seconds=index),
                ).status
                for index, phone in enumerate(phones)
            ]
            self.assertEqual(statuses[:3], [visible] * 3)
            self.assertEqual(statuses[3], SendCodeStatus.RETRY_LATER)
            self.assertEqual(len(sender.deliveries), 0)

    def test_cooldown_is_shared_across_login_register_export_and_delete(self) -> None:
        registered = self.register()
        self.assertEqual(
            self.request("login", now=NOW + timedelta(seconds=10)).status,
            SendCodeStatus.RETRY_LATER,
        )
        login_request = self.request("login", now=NOW + timedelta(seconds=61))
        self.assertEqual(login_request.status, SendCodeStatus.ACCEPTED)
        login = self.service.login(
            challenge_id=login_request.challenge_id or "",
            phone=PHONE,
            code=self.sender.deliveries[-1][1],
            terms_version=PROTOCOL,
            privacy_version=PROTOCOL,
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=62),
        )
        self.assertEqual(
            self.service.request_sensitive_code(
                session_token=login.session_token or "",
                phone=PHONE,
                action="export",
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW + timedelta(seconds=70),
            ).status,
            SendCodeStatus.RETRY_LATER,
        )
        export = self.service.request_sensitive_code(
            session_token=registered.session_token or "",
            phone=PHONE,
            action="export",
            ip_address="203.0.113.7",
            device_id="browser-device-001",
            now=NOW + timedelta(seconds=122),
        )
        self.assertEqual(export.status, SendCodeStatus.ACCEPTED)
        self.assertEqual(
            self.service.request_sensitive_code(
                session_token=registered.session_token or "",
                phone=PHONE,
                action="delete",
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW + timedelta(seconds=130),
            ).status,
            SendCodeStatus.RETRY_LATER,
        )

    def test_sensitive_action_is_bound_to_session_phone_and_action(self) -> None:
        registered = self.register()
        export = self.service.request_sensitive_code(session_token=registered.session_token or "", phone=PHONE, action="export", ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=62))
        self.assertEqual(export.status, SendCodeStatus.ACCEPTED)
        self.assertIsNone(self.service.verify_sensitive(registered.session_token or "", PHONE, export.challenge_id or "", self.sender.deliveries[-1][1], "delete", ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=63)))
        self.assertIsNotNone(self.service.verify_sensitive(registered.session_token or "", PHONE, export.challenge_id or "", self.sender.deliveries[-1][1], "export", ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=63)))

    def test_deactivate_revokes_sessions_and_blocks_login_send(self) -> None:
        registered = self.register()
        self.assertTrue(self.service.deactivate_account(registered.user_id or "", now=NOW + timedelta(seconds=2)))
        self.assertTrue(self.service.deactivate_account(registered.user_id or "", now=NOW + timedelta(seconds=3)))
        self.assertIsNone(self.service.authenticate_session(registered.session_token or "", now=NOW + timedelta(seconds=3)))
        self.assertEqual(self.request("login", now=NOW + timedelta(seconds=62)).status, SendCodeStatus.LOCKED)

    def test_normalizes_supported_cn_mobile_forms(self) -> None:
        self.assertEqual(normalize_cn_mobile("+86 138-0013-8000"), PHONE)
        self.assertEqual(normalize_cn_mobile("8613800138000"), PHONE)
        with self.assertRaises(ValueError):
            normalize_cn_mobile("12345")

    def test_plaintext_code_phone_and_password_are_not_persisted_or_audited(self) -> None:
        result = self.request("register")
        code = self.sender.deliveries[-1][1]
        completed = self.service.complete_registration(challenge_id=result.challenge_id or "", phone=PHONE, code=code, password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=1))
        persisted = json.dumps({"challenge": vars(self.store.challenges[result.challenge_id or ""]), "audit": [vars(item) for item in self.store.audit_events], "credentials": repr(self.store.password_credentials)}, default=str)
        self.assertEqual(completed.status, RegistrationStatus.COMPLETE)
        self.assertNotIn(code, persisted)
        self.assertNotIn(PHONE, persisted)
        self.assertNotIn("safe123", persisted)

    def test_password_length_unicode_whitespace_and_control_boundaries(self) -> None:
        cases = (
            ("a" * 5, RegistrationStatus.WEAK_PASSWORD),
            ("a" * 6, RegistrationStatus.COMPLETE),
            ("a" * 20, RegistrationStatus.COMPLETE),
            ("a" * 21, RegistrationStatus.WEAK_PASSWORD),
            ("数学密码安全", RegistrationStatus.COMPLETE),
            ("safe\x00x", RegistrationStatus.WEAK_PASSWORD),
            ("safe 123", RegistrationStatus.WEAK_PASSWORD),
        )
        for offset, (password, expected) in enumerate(cases):
            phone = f"13900139{offset:03d}"
            sent = self.request("register", phone=phone, now=NOW + timedelta(seconds=61 * offset))
            result = self.service.complete_registration(
                challenge_id=sent.challenge_id or "",
                phone=phone,
                code=self.sender.deliveries[-1][1],
                password=password,
                terms_version=PROTOCOL,
                privacy_version=PROTOCOL,
                ip_address="203.0.113.7",
                device_id="browser-device-001",
                now=NOW + timedelta(seconds=61 * offset + 1),
            )
            self.assertEqual(result.status, expected)

    def test_resend_invalidates_only_same_purpose_challenge(self) -> None:
        first = self.request("register")
        old_code = self.sender.deliveries[-1][1]
        second = self.request("register", now=NOW + timedelta(seconds=61))
        self.assertEqual(second.status, SendCodeStatus.ACCEPTED)
        self.assertEqual(self.store.challenges[first.challenge_id or ""].status, "cancelled")
        result = self.service.complete_registration(challenge_id=first.challenge_id or "", phone=PHONE, code=old_code, password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=62))
        self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)

    def test_invalid_attempts_lock_and_expired_code_fails_closed(self) -> None:
        sent = self.request("register")
        wrong = "000001" if self.sender.deliveries[-1][1] == "000000" else "000000"
        for _ in range(4):
            result = self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code=wrong, password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=1))
            self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)
        locked = self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code=wrong, password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=2))
        self.assertEqual(locked.status, RegistrationStatus.LOCKED)
        expired_phone = "13900139000"
        expired = self.request("register", expired_phone, NOW + timedelta(seconds=61))
        result = self.service.complete_registration(challenge_id=expired.challenge_id or "", phone=expired_phone, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(minutes=7))
        self.assertEqual(result.status, RegistrationStatus.EXPIRED)

    def test_session_logout_current_and_all(self) -> None:
        first = self.register()
        sent = self.request("login", now=NOW + timedelta(seconds=61))
        second = self.service.login(challenge_id=sent.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", now=NOW + timedelta(seconds=62))
        self.assertTrue(self.service.logout(second.session_token or "", now=NOW + timedelta(seconds=63)))
        self.assertIsNone(self.service.authenticate_session(second.session_token or "", now=NOW + timedelta(seconds=64)))
        self.assertTrue(self.service.logout_all(first.session_token or "", now=NOW + timedelta(seconds=65)))
        self.assertFalse(self.store.sessions)

    def test_cooldown_captcha_provider_failure_and_concurrency(self) -> None:
        captcha_service = RegistrationService(store=InMemoryRegistrationStore(), sms_sender=RecordingSmsSender(), captcha_verifier=InMemoryCaptchaVerifier({"captcha-once"}), secret_pepper=b"p" * 32, config=AuthConfig(captcha_after_phone_day=2, captcha_after_ip_hour=99))
        request = lambda now, captcha=None: captcha_service.request_code(purpose="register", phone=PHONE, ip_address="203.0.113.7", device_id="browser-device-001", captcha_token=captcha, now=now)
        self.assertEqual(request(NOW).status, SendCodeStatus.ACCEPTED)
        self.assertEqual(request(NOW + timedelta(seconds=10)).status, SendCodeStatus.RETRY_LATER)
        self.assertEqual(request(NOW + timedelta(seconds=61)).status, SendCodeStatus.ACCEPTED)
        self.assertEqual(request(NOW + timedelta(seconds=122)).status, SendCodeStatus.CAPTCHA_REQUIRED)
        self.assertEqual(request(NOW + timedelta(seconds=122), "captcha-once").status, SendCodeStatus.ACCEPTED)
        failing = RegistrationService(store=InMemoryRegistrationStore(), sms_sender=RecordingSmsSender(fail=True), captcha_verifier=InMemoryCaptchaVerifier(), secret_pepper=b"p" * 32)
        failed = failing.request_code(purpose="register", phone=PHONE, ip_address="203.0.113.7", device_id="test-device", now=NOW)
        self.assertEqual(failed.status, SendCodeStatus.TEMPORARILY_UNAVAILABLE)
        self.assertTrue(all(item.status not in {"pending", "sent"} for item in failing.store.challenges.values()))
        concurrent = RegistrationService(store=InMemoryRegistrationStore(), sms_sender=RecordingSmsSender(), captcha_verifier=InMemoryCaptchaVerifier(), secret_pepper=b"p" * 32, config=AuthConfig(captcha_after_phone_day=99, captcha_after_ip_hour=99))
        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(lambda _: concurrent.request_code(purpose="register", phone=PHONE, ip_address="203.0.113.7", device_id="test-device", now=NOW).status, range(50)))
        self.assertEqual(statuses.count(SendCodeStatus.ACCEPTED), 1)
        self.assertEqual(len(concurrent.sms_sender.deliveries), 1)

    def test_challenge_cannot_cross_server_scope_and_user_remains_minimal(self) -> None:
        sent = self.request("register")
        result = self.service.complete_registration(challenge_id=sent.challenge_id or "", phone=PHONE, code=self.sender.deliveries[-1][1], password="safe123", terms_version=PROTOCOL, privacy_version=PROTOCOL, ip_address="203.0.113.7", device_id="browser-device-001", tenant_scope="other-scope", now=NOW + timedelta(seconds=1))
        self.assertEqual(result.status, RegistrationStatus.INVALID_CODE)
        completed = self.register(phone="13900139000", now=NOW + timedelta(seconds=62))
        user = next(user for user in self.store.users_by_phone.values() if user.user_id == completed.user_id)
        self.assertEqual(set(vars(user)), {"user_id", "phone_hash", "phone_last4", "created_at", "status"})

    def test_forward_migrations_keep_old_files_immutable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sql = (root / "services/web_auth/migrations/0003_account_simplification.sql").read_text(encoding="utf-8").lower()
        v040 = (root / "services/web_auth/migrations/0005_auth_v040.sql").read_text(encoding="utf-8").lower()
        self.assertIn("drop column display_name", sql)
        self.assertIn("drop table if exists guardian_consents", sql)
        self.assertIn("auth_password_credentials", v040)
        self.assertIn("auth_agreement_acceptances", v040)


if __name__ == "__main__":
    unittest.main()
