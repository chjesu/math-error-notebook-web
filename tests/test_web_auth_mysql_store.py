from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

from services.web_auth import AuthConfig, MySqlRegistrationStore
from services.web_auth.registration import RegistrationStatus


NOW = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, fetches=()) -> None:
        self.fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []
        self.closed = False

    def execute(self, query: str, args: tuple = ()) -> int:
        self.executed.append((" ".join(query.split()), args))
        return 1

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, fetches=()) -> None:
        self.cursor_instance = FakeCursor(fetches)
        self.begun = self.committed = self.rolled_back = self.closed = 0

    def begin(self) -> None:
        self.begun += 1

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


class MySqlRegistrationStoreTests(unittest.TestCase):
    def store(self, connection: FakeConnection) -> MySqlRegistrationStore:
        return MySqlRegistrationStore(lambda: connection)

    def reserve(self, store: MySqlRegistrationStore):
        return store.reserve_send(
            phone_hash="p" * 64,
            ip_hash="i" * 64,
            ip_prefix_hash="n" * 64,
            device_hash="d" * 64,
            tenant_hash="t" * 64,
            now=NOW,
            config=AuthConfig(),
        )

    def test_atomic_reservation_locks_every_bucket_in_stable_order(self) -> None:
        connection = FakeConnection([(0,)] * 7 + [(NOW.replace(tzinfo=None),), (0,)])
        allowed, _ = self.reserve(self.store(connection))
        self.assertTrue(allowed)
        self.assertEqual((connection.begun, connection.committed, connection.rolled_back), (1, 1, 0))
        locks = [
            args
            for query, args in connection.cursor_instance.executed
            if query.startswith("SELECT request_count")
        ]
        self.assertEqual(len(locks), 7)
        self.assertEqual(locks, sorted(locks))
        self.assertTrue(
            any("INSERT INTO auth_sms_send_events" in query for query, _ in connection.cursor_instance.executed)
        )
        self.assertTrue(
            any(
                "SELECT COUNT(*) FROM auth_sms_send_events" in query
                for query, _ in connection.cursor_instance.executed
            )
        )

    def test_rolling_phone_day_limit_cannot_reset_at_bucket_boundary(self) -> None:
        connection = FakeConnection(
            [(0,)] * 7 + [(NOW.replace(tzinfo=None),), (AuthConfig().phone_day_limit,)]
        )
        allowed, _ = self.reserve(self.store(connection))
        self.assertFalse(allowed)
        self.assertEqual((connection.committed, connection.rolled_back), (0, 1))
        self.assertFalse(
            any("INSERT INTO auth_sms_send_events" in query for query, _ in connection.cursor_instance.executed)
        )

    def test_limit_hit_rolls_back_without_send_event(self) -> None:
        connection = FakeConnection([(10,)])  # alphabetically first bucket is device/hour
        allowed, _ = self.reserve(self.store(connection))
        self.assertFalse(allowed)
        self.assertEqual((connection.committed, connection.rolled_back), (0, 1))
        self.assertFalse(
            any("INSERT INTO auth_sms_send_events" in query for query, _ in connection.cursor_instance.executed)
        )

    def test_wrong_code_updates_attempt_inside_transaction(self) -> None:
        challenge = (
            "p" * 64,
            "t" * 64,
            "correct-hash",
            "sent",
            0,
            (NOW + timedelta(minutes=5)).replace(tzinfo=None),
        )
        connection = FakeConnection([challenge])
        status, user = self.store(connection).register(
            challenge_id="c" * 32,
            phone_hash="p" * 64,
            tenant_hash="t" * 64,
            phone_last4="8000",
            code_hash="wrong-hash",
            display_name="测试学生",
            birth_date=date(2000, 1, 1),
            guardian_consent_receipt=None,
            block_for_guardian=False,
            session_hash="s" * 64,
            session_expires_at=NOW + timedelta(days=30),
            now=NOW,
            max_attempts=5,
        )
        self.assertEqual(status, RegistrationStatus.INVALID_CODE)
        self.assertIsNone(user)
        self.assertEqual((connection.committed, connection.rolled_back), (1, 0))
        self.assertTrue(
            any("SET attempt_count=%s" in query for query, _ in connection.cursor_instance.executed)
        )

    def test_migration_contains_persistent_limits_events_and_audit_dimensions(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "services/web_auth/migrations/0001_phone_registration.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE auth_rate_limit_buckets", sql)
        self.assertIn("CREATE TABLE auth_sms_send_events", sql)
        self.assertIn("ip_prefix_hash", sql)
        self.assertIn("tenant_scope_hash", sql)


if __name__ == "__main__":
    unittest.main()
