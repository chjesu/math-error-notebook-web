"""MySQL adapter for sanitized operations queries."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Protocol


class Cursor(Protocol):
    def execute(self, query: str, args: tuple[Any, ...] = ()) -> int: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def begin(self) -> None: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]


class MySqlOperationsStore:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def get_operator_role(self, *, user_id: str) -> str | None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT role FROM admin_operators WHERE user_id=%s AND status='active'",
                (user_id,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None
        finally:
            cursor.close()
            connection.close()

    def dashboard(self, *, sections: tuple[str, ...], limit: int) -> dict[str, Any]:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            result: dict[str, Any] = {}
            if "overview" in sections:
                cursor.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM web_users WHERE status='active'),"
                    "(SELECT COUNT(*) FROM web_jobs WHERE status IN ('failed_retryable','failed_final','waiting_confirmation')) ,"
                    "(SELECT COUNT(*) FROM questions WHERE status='candidate'),"
                    "(SELECT COUNT(*) FROM account_deletions WHERE status='pending')"
                )
                row = cursor.fetchone() or (0, 0, 0, 0)
                result["overview"] = {
                    "active_users": int(row[0] or 0),
                    "attention_tasks": int(row[1] or 0),
                    "candidate_questions": int(row[2] or 0),
                    "pending_privacy_cases": int(row[3] or 0),
                }
            if "tasks" in sections:
                cursor.execute(
                    "SELECT j.id,u.phone_last4,j.job_type,j.status,"
                    "COALESCE(j.last_error_code,''),j.updated_at FROM web_jobs j "
                    "JOIN web_users u ON u.id=j.user_id "
                    "WHERE j.status IN ('failed_retryable','failed_final','waiting_confirmation') "
                    "ORDER BY j.updated_at DESC,j.id DESC LIMIT %s",
                    (limit,),
                )
                result["tasks"] = [
                    {"task_id": str(row[0]), "user_ref": f"用户 ····{row[1]}", "type": str(row[2]), "status": str(row[3]), "error_code": str(row[4]), "updated_at": _iso(row[5])}
                    for row in cursor.fetchall()
                ]
            if "content" in sections:
                cursor.execute(
                    "SELECT q.id,q.status,q.current_version_no,s.license_status,q.updated_at,"
                    "COALESCE((SELECT v.verdict FROM question_verifications v "
                    "JOIN question_versions qv ON qv.id=v.question_version_id "
                    "WHERE qv.question_id=q.id ORDER BY v.verified_at DESC LIMIT 1),'unreviewed') "
                    "FROM questions q JOIN question_sources s ON s.id=q.source_id "
                    "WHERE q.status='candidate' OR EXISTS (SELECT 1 FROM question_verifications v2 "
                    "JOIN question_versions qv2 ON qv2.id=v2.question_version_id "
                    "WHERE qv2.question_id=q.id AND v2.verdict='needs_review') "
                    "ORDER BY q.updated_at DESC,q.id DESC LIMIT %s",
                    (limit,),
                )
                result["content"] = [
                    {"question_id": str(row[0]), "status": str(row[1]), "version": int(row[2]), "license": str(row[3]), "updated_at": _iso(row[4]), "verification": str(row[5])}
                    for row in cursor.fetchall()
                ]
            if "risk" in sections:
                cursor.execute(
                    "SELECT COUNT(*),SUM(status='sent'),SUM(status='delivery_failed') "
                    "FROM auth_sms_challenges WHERE created_at>=UTC_DATE()"
                )
                sms = cursor.fetchone() or (0, 0, 0)
                cursor.execute(
                    "SELECT COUNT(*) FROM auth_audit_events "
                    "WHERE occurred_at>=UTC_DATE() AND outcome='rate_limited'"
                )
                limited = cursor.fetchone() or (0,)
                result["risk"] = {
                    "sms_requested_today": int(sms[0] or 0),
                    "sms_sent_today": int(sms[1] or 0),
                    "sms_failed_today": int(sms[2] or 0),
                    "rate_limited_today": int(limited[0] or 0),
                }
            if "privacy" in sections:
                cursor.execute(
                    "SELECT u.phone_last4,d.status,d.requested_at,d.updated_at,"
                    "COALESCE(d.last_error_code,'') FROM account_deletions d "
                    "JOIN web_users u ON u.id=d.user_id "
                    "ORDER BY d.updated_at DESC,d.user_id DESC LIMIT %s",
                    (limit,),
                )
                result["privacy"] = [
                    {"user_ref": f"用户 ····{row[0]}", "status": str(row[1]), "requested_at": _iso(row[2]), "updated_at": _iso(row[3]), "error_code": str(row[4])}
                    for row in cursor.fetchall()
                ]
            if "audit" in sections:
                cursor.execute(
                    "SELECT RIGHT(a.operator_user_id,8),o.role,a.event_type,"
                    "a.occurred_at FROM operations_audit_events a "
                    "JOIN admin_operators o ON o.user_id=a.operator_user_id "
                    "ORDER BY a.occurred_at DESC,a.id DESC LIMIT %s",
                    (limit,),
                )
                result["audit"] = [
                    {"operator_ref": f"运营账号 ····{row[0]}", "role": str(row[1]), "event": str(row[2]), "occurred_at": _iso(row[3])}
                    for row in cursor.fetchall()
                ]
            return result
        finally:
            cursor.close()
            connection.close()

    def record_access(self, *, user_id: str, role: str, sections: tuple[str, ...], occurred_at: datetime) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute(
                "INSERT INTO operations_audit_events "
                "(operator_user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) "
                "VALUES (%s,'dashboard.viewed','dashboard','operations',%s,%s)",
                (
                    user_id,
                    json.dumps({"role": role, "sections": list(sections)}, ensure_ascii=False, separators=(",", ":")),
                    occurred_at.astimezone(timezone.utc).replace(tzinfo=None),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat()
    return str(value or "")
