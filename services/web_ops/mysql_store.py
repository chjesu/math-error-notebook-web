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
            if "users" in sections:
                cursor.execute(
                    "SELECT u.phone_last4,u.status,u.created_at,"
                    "GREATEST(u.updated_at,"
                    "COALESCE((SELECT MAX(s.created_at) FROM auth_sessions s WHERE s.user_id=u.id),u.created_at),"
                    "COALESCE((SELECT MAX(a.occurred_at) FROM domain_audit_events a WHERE a.user_id=u.id),u.created_at)) last_active_at,"
                    "(SELECT COUNT(*) FROM auth_sessions s2 WHERE s2.user_id=u.id AND s2.revoked_at IS NULL AND s2.expires_at>UTC_TIMESTAMP()),"
                    "(SELECT COUNT(*) FROM error_notebook_entries e WHERE e.user_id=u.id AND e.status<>'removed'),"
                    "(SELECT COUNT(*) FROM review_attempts r WHERE r.user_id=u.id),"
                    "(SELECT COUNT(*) FROM web_jobs j WHERE j.user_id=u.id AND j.job_type='practice_pdf' AND j.status='completed'),"
                    "COALESCE((SELECT SUM(m.uncached_input_tokens+m.output_tokens+m.cache_read_tokens+m.cache_write_tokens) "
                    "FROM model_usage_sessions m WHERE m.user_id=u.id),0) "
                    "FROM web_users u ORDER BY last_active_at DESC,u.id DESC LIMIT %s",
                    (limit,),
                )
                result["users"] = [
                    {
                        "user_ref": f"用户 ····{row[0]}", "status": str(row[1]),
                        "created_at": _iso(row[2]), "last_active_at": _iso(row[3]),
                        "active_sessions": int(row[4] or 0), "error_count": int(row[5] or 0),
                        "review_count": int(row[6] or 0), "pdf_count": int(row[7] or 0),
                        "total_tokens": int(row[8] or 0),
                    }
                    for row in cursor.fetchall()
                ]
            if "behavior" in sections:
                cursor.execute(
                    "SELECT DATE(event_at),COUNT(DISTINCT user_id),"
                    "SUM(kind='register'),SUM(kind='upload'),SUM(kind='intake'),SUM(kind='grade'),"
                    "SUM(kind='error'),SUM(kind='review'),SUM(kind='pdf') FROM ("
                    "SELECT created_at event_at,id user_id,'register' kind FROM web_users WHERE created_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT created_at,user_id,'upload' FROM web_files WHERE created_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT created_at,user_id,'intake' FROM intake_items WHERE created_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT created_at,user_id,'grade' FROM grade_candidates WHERE created_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT created_at,user_id,'error' FROM error_notebook_entries WHERE created_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT completed_at,user_id,'review' FROM review_attempts WHERE completed_at>=UTC_DATE()-INTERVAL 6 DAY UNION ALL "
                    "SELECT updated_at,user_id,'pdf' FROM web_jobs WHERE job_type='practice_pdf' AND status='completed' AND updated_at>=UTC_DATE()-INTERVAL 6 DAY"
                    ") events GROUP BY DATE(event_at) ORDER BY DATE(event_at)"
                )
                daily = [
                    {
                        "date": str(row[0]), "active_users": int(row[1] or 0),
                        "registrations": int(row[2] or 0), "uploads": int(row[3] or 0),
                        "intakes": int(row[4] or 0), "grades": int(row[5] or 0),
                        "errors_added": int(row[6] or 0), "reviews_completed": int(row[7] or 0),
                        "pdfs_generated": int(row[8] or 0),
                    }
                    for row in cursor.fetchall()
                ]
                result["behavior"] = {
                    "range_days": 7,
                    "totals": {
                        key: sum(item[key] for item in daily)
                        for key in ("registrations", "uploads", "intakes", "grades", "errors_added", "reviews_completed", "pdfs_generated")
                    },
                    "daily": daily,
                }
            if "usage" in sections:
                cursor.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT user_id),SUM(uncached_input_tokens),SUM(output_tokens),"
                    "SUM(cache_read_tokens),SUM(cache_write_tokens),MAX(updated_at) FROM model_usage_sessions"
                )
                usage = cursor.fetchone() or (0, 0, 0, 0, 0, 0, None)
                cursor.execute(
                    "SELECT u.phone_last4,COUNT(*),SUM(m.uncached_input_tokens),SUM(m.output_tokens),"
                    "SUM(m.cache_read_tokens),SUM(m.cache_write_tokens),MAX(m.updated_at) "
                    "FROM model_usage_sessions m JOIN web_users u ON u.id=m.user_id "
                    "GROUP BY m.user_id,u.phone_last4 ORDER BY "
                    "SUM(m.uncached_input_tokens+m.output_tokens+m.cache_read_tokens+m.cache_write_tokens) DESC LIMIT %s",
                    (limit,),
                )
                result["usage"] = {
                    "summary": {
                        "session_count": int(usage[0] or 0), "user_count": int(usage[1] or 0),
                        "uncached_input_tokens": int(usage[2] or 0), "output_tokens": int(usage[3] or 0),
                        "cache_read_tokens": int(usage[4] or 0), "cache_write_tokens": int(usage[5] or 0),
                        "total_tokens": sum(int(value or 0) for value in usage[2:6]), "updated_at": _iso(usage[6]),
                    },
                    "users": [
                        {
                            "user_ref": f"用户 ····{row[0]}", "session_count": int(row[1] or 0),
                            "uncached_input_tokens": int(row[2] or 0), "output_tokens": int(row[3] or 0),
                            "cache_read_tokens": int(row[4] or 0), "cache_write_tokens": int(row[5] or 0),
                            "total_tokens": sum(int(value or 0) for value in row[2:6]), "updated_at": _iso(row[6]),
                        }
                        for row in cursor.fetchall()
                    ],
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
