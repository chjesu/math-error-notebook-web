"""MySQL 8 persistence adapter for the phone-registration domain."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import secrets
from typing import Any, Protocol
import uuid

from .registration import (
    AuditEvent,
    AuthConfig,
    Challenge,
    RegistrationStatus,
    User,
)


class Cursor(Protocol):
    def execute(self, query: str, args: tuple[Any, ...] = ()) -> int: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def begin(self) -> None: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]


class MySqlRegistrationStore:
    """PyMySQL-compatible adapter; every security mutation is transactional."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _window_start(now: datetime, kind: str) -> datetime:
        utc = now.astimezone(timezone.utc).replace(tzinfo=None)
        if kind == "minute":
            return utc.replace(second=0, microsecond=0)
        if kind == "hour":
            return utc.replace(minute=0, second=0, microsecond=0)
        if kind == "day":
            return utc.replace(hour=0, minute=0, second=0, microsecond=0)
        raise ValueError("unsupported rate window")

    def _bucket_specs(
        self,
        *,
        phone_hash: str,
        ip_hash: str,
        ip_prefix_hash: str,
        device_hash: str,
        tenant_hash: str,
        now: datetime,
        config: AuthConfig,
    ) -> list[tuple[str, str, str, datetime, int]]:
        specs = [
            ("phone", phone_hash, "hour", self._window_start(now, "hour"), config.phone_hour_limit),
            ("phone", phone_hash, "day", self._window_start(now, "day"), config.phone_day_limit),
            ("ip", ip_hash, "minute", self._window_start(now, "minute"), config.ip_minute_limit),
            ("ip", ip_hash, "hour", self._window_start(now, "hour"), config.ip_hour_limit),
            ("ip_prefix", ip_prefix_hash, "hour", self._window_start(now, "hour"), config.ip_prefix_hour_limit),
            ("device", device_hash, "hour", self._window_start(now, "hour"), config.device_hour_limit),
            ("device", device_hash, "day", self._window_start(now, "day"), config.device_day_limit),
            ("tenant", tenant_hash, "hour", self._window_start(now, "hour"), config.tenant_hour_limit),
            ("global", "all", "day", self._window_start(now, "day"), config.global_day_limit),
        ]
        return sorted(specs, key=lambda item: item[:4])

    def count(self, dimension: str, subject_hash: str, since: datetime) -> int:
        columns = {
            "phone": "phone_lookup_hash",
            "ip": "ip_hash",
            "ip_prefix": "ip_prefix_hash",
            "device": "device_hash",
            "tenant": "tenant_scope_hash",
            "global": None,
        }
        if dimension not in columns:
            raise ValueError("unsupported rate dimension")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            if columns[dimension] is None:
                query = "SELECT COUNT(*) FROM auth_sms_send_events WHERE occurred_at >= %s"
                args = (since.astimezone(timezone.utc).replace(tzinfo=None),)
            else:
                query = (
                    f"SELECT COUNT(*) FROM auth_sms_send_events "
                    f"WHERE {columns[dimension]} = %s AND occurred_at >= %s"
                )
                args = (subject_hash, since.astimezone(timezone.utc).replace(tzinfo=None))
            cursor.execute(query, args)
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            cursor.close()
            connection.close()

    def reserve_send(
        self,
        *,
        phone_hash: str,
        ip_hash: str,
        ip_prefix_hash: str,
        device_hash: str,
        tenant_hash: str,
        now: datetime,
        config: AuthConfig,
    ) -> tuple[bool, int]:
        connection = self._connect()
        cursor = connection.cursor()
        utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
        specs = self._bucket_specs(
            phone_hash=phone_hash,
            ip_hash=ip_hash,
            ip_prefix_hash=ip_prefix_hash,
            device_hash=device_hash,
            tenant_hash=tenant_hash,
            now=now,
            config=config,
        )
        try:
            connection.begin()
            for dimension, subject, kind, window, _ in specs:
                cursor.execute(
                    "INSERT INTO auth_rate_limit_buckets "
                    "(dimension, subject_hash, window_kind, window_start, request_count, updated_at) "
                    "VALUES (%s, %s, %s, %s, 0, %s) "
                    "ON DUPLICATE KEY UPDATE updated_at = updated_at",
                    (dimension, subject, kind, window, utc_now),
                )
            counts: list[int] = []
            for dimension, subject, kind, window, maximum in specs:
                cursor.execute(
                    "SELECT request_count FROM auth_rate_limit_buckets "
                    "WHERE dimension=%s AND subject_hash=%s AND window_kind=%s "
                    "AND window_start=%s FOR UPDATE",
                    (dimension, subject, kind, window),
                )
                row = cursor.fetchone()
                count = int(row[0]) if row else 0
                counts.append(count)
                if count >= maximum:
                    connection.rollback()
                    return False, config.resend_cooldown_seconds

            cursor.execute(
                "INSERT INTO auth_send_cooldowns (phone_lookup_hash, next_send_at, updated_at) "
                "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE updated_at=updated_at",
                (phone_hash, utc_now, utc_now),
            )
            cursor.execute(
                "SELECT next_send_at FROM auth_send_cooldowns "
                "WHERE phone_lookup_hash=%s FOR UPDATE",
                (phone_hash,),
            )
            row = cursor.fetchone()
            next_send_at = row[0] if row else utc_now
            if next_send_at > utc_now:
                retry = max(1, int((next_send_at - utc_now).total_seconds()))
                connection.rollback()
                return False, retry

            # Fixed buckets protect budget windows; rolling checks close their
            # boundary bursts. The locked cooldown row serializes a phone, while
            # the fixed IP/device bucket rows serialize the other subjects.
            rolling_checks = (
                ("phone_lookup_hash", phone_hash, timedelta(days=1), config.phone_day_limit),
                ("ip_hash", ip_hash, timedelta(minutes=1), config.ip_minute_limit),
                ("device_hash", device_hash, timedelta(days=1), config.device_day_limit),
            )
            for column, subject, interval, maximum in rolling_checks:
                cursor.execute(
                    f"SELECT COUNT(*) FROM auth_sms_send_events "
                    f"WHERE {column}=%s AND occurred_at >= %s",
                    (subject, utc_now - interval),
                )
                row = cursor.fetchone()
                if row and int(row[0]) >= maximum:
                    connection.rollback()
                    return False, config.resend_cooldown_seconds

            for (dimension, subject, kind, window, _), count in zip(specs, counts):
                cursor.execute(
                    "UPDATE auth_rate_limit_buckets SET request_count=%s, updated_at=%s "
                    "WHERE dimension=%s AND subject_hash=%s AND window_kind=%s AND window_start=%s",
                    (count + 1, utc_now, dimension, subject, kind, window),
                )
            cursor.execute(
                "UPDATE auth_send_cooldowns SET next_send_at=%s, updated_at=%s "
                "WHERE phone_lookup_hash=%s",
                (
                    utc_now + timedelta(seconds=config.resend_cooldown_seconds),
                    utc_now,
                    phone_hash,
                ),
            )
            cursor.execute(
                "INSERT INTO auth_sms_send_events "
                "(phone_lookup_hash, ip_hash, ip_prefix_hash, device_hash, tenant_scope_hash, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (phone_hash, ip_hash, ip_prefix_hash, device_hash, tenant_hash, utc_now),
            )
            connection.commit()
            return True, config.resend_cooldown_seconds
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def add_challenge(self, challenge: Challenge) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "SELECT id FROM auth_sms_challenges WHERE phone_lookup_hash=%s "
                "AND status IN ('pending','sent') FOR UPDATE",
                (challenge.phone_hash,),
            )
            cursor.execute(
                "UPDATE auth_sms_challenges SET status='cancelled' "
                "WHERE phone_lookup_hash=%s AND status IN ('pending','sent')",
                (challenge.phone_hash,),
            )
            cursor.execute(
                "INSERT INTO auth_sms_challenges "
                "(id, phone_lookup_hash, tenant_scope_hash, purpose, code_hash, status, "
                "attempt_count, expires_at, created_at) "
                "VALUES (%s, %s, %s, 'register', %s, 'pending', 0, %s, %s)",
                (
                    challenge.challenge_id,
                    challenge.phone_hash,
                    challenge.tenant_hash,
                    challenge.code_hash,
                    challenge.expires_at.astimezone(timezone.utc).replace(tzinfo=None),
                    challenge.created_at.astimezone(timezone.utc).replace(tzinfo=None),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def mark_delivery(self, challenge_id: str, status: str, receipt: str | None) -> None:
        if status not in {"sent", "delivery_failed"}:
            raise ValueError("invalid delivery status")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            changed = cursor.execute(
                "UPDATE auth_sms_challenges SET status=%s, provider_receipt=%s "
                "WHERE id=%s AND status='pending'",
                (status, receipt, challenge_id),
            )
            if changed != 1:
                raise ValueError("challenge is not pending")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def register(
        self,
        *,
        challenge_id: str,
        phone_hash: str,
        tenant_hash: str,
        phone_last4: str,
        code_hash: str,
        session_hash: str,
        session_expires_at: datetime,
        now: datetime,
        max_attempts: int,
    ) -> tuple[RegistrationStatus, User | None]:
        connection = self._connect()
        cursor = connection.cursor()
        utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
        try:
            connection.begin()
            cursor.execute(
                "SELECT phone_lookup_hash, tenant_scope_hash, code_hash, status, "
                "attempt_count, expires_at FROM auth_sms_challenges WHERE id=%s FOR UPDATE",
                (challenge_id,),
            )
            row = cursor.fetchone()
            if not row or row[0] != phone_hash or row[1] != tenant_hash:
                connection.rollback()
                return RegistrationStatus.INVALID_CODE, None
            stored_hash, status, attempts, expires_at = row[2], str(row[3]), int(row[4]), row[5]
            if status == "locked" or attempts >= max_attempts:
                connection.rollback()
                return RegistrationStatus.LOCKED, None
            if status != "sent":
                connection.rollback()
                return RegistrationStatus.INVALID_CODE, None
            if utc_now >= expires_at:
                cursor.execute(
                    "UPDATE auth_sms_challenges SET status='expired' WHERE id=%s",
                    (challenge_id,),
                )
                connection.commit()
                return RegistrationStatus.EXPIRED, None
            if not secrets.compare_digest(str(stored_hash), code_hash):
                attempts += 1
                next_status = "locked" if attempts >= max_attempts else "sent"
                cursor.execute(
                    "UPDATE auth_sms_challenges SET attempt_count=%s, status=%s WHERE id=%s",
                    (attempts, next_status, challenge_id),
                )
                connection.commit()
                return (
                    RegistrationStatus.LOCKED
                    if next_status == "locked"
                    else RegistrationStatus.INVALID_CODE,
                    None,
                )
            cursor.execute(
                "SELECT id, phone_last4, created_at, status FROM web_users "
                "WHERE phone_lookup_hash=%s FOR UPDATE",
                (phone_hash,),
            )
            user_row = cursor.fetchone()
            if user_row:
                user = User(
                    user_id=str(user_row[0]),
                    phone_hash=phone_hash,
                    phone_last4=str(user_row[1]),
                    created_at=user_row[2].replace(tzinfo=timezone.utc),
                    status=str(user_row[3]),
                )
                if user.status != "active":
                    connection.rollback()
                    return RegistrationStatus.LOCKED, None
            else:
                user = User(
                    user_id=uuid.uuid4().hex,
                    phone_hash=phone_hash,
                    phone_last4=phone_last4,
                    created_at=now,
                )
                cursor.execute(
                    "INSERT INTO web_users "
                    "(id, phone_lookup_hash, phone_last4, status, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'active', %s, %s)",
                    (
                        user.user_id,
                        phone_hash,
                        phone_last4,
                        utc_now,
                        utc_now,
                    ),
                )
            cursor.execute(
                "UPDATE auth_sms_challenges SET status='verified', consumed_at=%s WHERE id=%s",
                (utc_now, challenge_id),
            )
            cursor.execute(
                "INSERT INTO auth_sessions (session_hash, user_id, expires_at, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    session_hash,
                    user.user_id,
                    session_expires_at.astimezone(timezone.utc).replace(tzinfo=None),
                    utc_now,
                ),
            )
            connection.commit()
            return RegistrationStatus.COMPLETE, user
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_session_user(self, session_hash: str, now: datetime) -> User | None:
        connection = self._connect()
        cursor = connection.cursor()
        utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
        try:
            cursor.execute(
                "SELECT u.id, u.phone_lookup_hash, u.phone_last4, u.created_at, u.status "
                "FROM auth_sessions s JOIN web_users u ON u.id=s.user_id "
                "WHERE s.session_hash=%s AND s.revoked_at IS NULL AND s.expires_at>%s "
                "AND u.status='active'",
                (session_hash, utc_now),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return User(
                user_id=str(row[0]),
                phone_hash=str(row[1]),
                phone_last4=str(row[2]),
                created_at=row[3].replace(tzinfo=timezone.utc),
                status=str(row[4]),
            )
        finally:
            cursor.close()
            connection.close()

    def revoke_session(self, session_hash: str, now: datetime) -> bool:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            changed = cursor.execute(
                "UPDATE auth_sessions SET revoked_at=%s "
                "WHERE session_hash=%s AND revoked_at IS NULL",
                (now.astimezone(timezone.utc).replace(tzinfo=None), session_hash),
            )
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def revoke_user_sessions(self, user_id: str, now: datetime) -> int:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            changed = cursor.execute(
                "UPDATE auth_sessions SET revoked_at=%s "
                "WHERE user_id=%s AND revoked_at IS NULL",
                (now.astimezone(timezone.utc).replace(tzinfo=None), user_id),
            )
            connection.commit()
            return int(changed)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def audit(self, event: AuditEvent) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO auth_audit_events "
                "(event_type, outcome, phone_masked, phone_lookup_hash, ip_hash, "
                "ip_prefix_hash, device_hash, tenant_scope_hash, metadata, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.event,
                    event.outcome,
                    event.phone_masked,
                    event.phone_hash,
                    event.ip_hash,
                    event.ip_prefix_hash,
                    event.device_hash,
                    event.tenant_hash,
                    json.dumps(event.metadata, ensure_ascii=False, separators=(",", ":")),
                    event.occurred_at.astimezone(timezone.utc).replace(tzinfo=None),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()
