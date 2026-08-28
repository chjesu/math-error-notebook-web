"""Import desktop error-notebook records into one Web account, idempotently."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


def _stable_id(kind: str, user_id: str, source_id: str) -> str:
    return hashlib.sha256(f"desktop-error:{kind}:{user_id}:{source_id}".encode("utf-8")).hexdigest()[:32]


def _question_id(source_id: str | None) -> str | None:
    return hashlib.sha256(("question:" + source_id).encode("utf-8")).hexdigest()[:32] if source_id else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_database(source_root: Path) -> Path:
    database = source_root / "data" / "math_notebook.db"
    skill = source_root / ".agents" / "skills" / "math-error-notebook" / "SKILL.md"
    if not database.is_file() or not skill.is_file():
        raise RuntimeError("source root must contain the canonical notebook database and Skill")
    return database


def extract(source_root: Path) -> dict[str, Any]:
    database = _source_database(source_root)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("desktop notebook integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("desktop notebook foreign key check failed")
        knowledge: dict[str, list[str]] = defaultdict(list)
        for row in connection.execute(
            "SELECT ek.error_id,k.name FROM error_knowledge ek JOIN knowledge_points k ON k.code=ek.knowledge_code ORDER BY ek.error_id,k.code"
        ):
            knowledge[str(row[0])].append(str(row[1]))
        reviews: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id,error_id,cycle,stage,due_date,completed_at,result,note FROM review_schedule ORDER BY error_id,cycle,stage,id"
        ):
            reviews[str(row[1])].append(dict(row))
        recommendations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id,error_id,question_id,reason,assigned_at,status FROM recommendations ORDER BY error_id,rank,id"
        ):
            recommendations[str(row[1])].append(dict(row))
        errors = []
        for row in connection.execute(
            "SELECT id,occurred_at,problem_text,student_answer,correct_answer,correct_solution,first_wrong_step,cause_code,cause_detail,difficulty,confidence,question_id,status,created_at FROM errors ORDER BY occurred_at,id"
        ):
            item = dict(row)
            source_id = str(item["id"])
            item["knowledge_points"] = knowledge[source_id]
            item["reviews"] = reviews[source_id]
            item["recommendations"] = recommendations[source_id]
            errors.append(item)
    finally:
        connection.close()
    digest = hashlib.sha256(_canonical(errors).encode("utf-8")).hexdigest()
    return {
        "source": str(database.resolve()),
        "source_sha256": digest,
        "errors": errors,
        "counts": {
            "errors": len(errors),
            "knowledge_links": sum(len(item["knowledge_points"]) for item in errors),
            "completed_reviews": sum(1 for item in errors for review in item["reviews"] if review["completed_at"]),
            "recommendations": sum(len(item["recommendations"]) for item in errors),
        },
    }


def _connection():
    import pymysql

    required = ["LZLM_MYSQL_HOST", "LZLM_MYSQL_USER", "LZLM_MYSQL_PASSWORD", "LZLM_MYSQL_DATABASE"]
    missing = [name for name in required if not os.environ.get(name)]
    if len(missing) == len(required):
        try:
            from scripts.local_env import _connection_factory
        except ModuleNotFoundError:
            from local_env import _connection_factory

        return _connection_factory()()
    if missing:
        raise RuntimeError("missing MySQL environment: " + ", ".join(missing))
    return pymysql.connect(
        host=os.environ["LZLM_MYSQL_HOST"],
        port=int(os.environ.get("LZLM_MYSQL_PORT", "3306")),
        user=os.environ["LZLM_MYSQL_USER"],
        password=os.environ["LZLM_MYSQL_PASSWORD"],
        database=os.environ["LZLM_MYSQL_DATABASE"],
        charset="utf8mb4",
        autocommit=False,
    )


def _resolve_user(cursor: Any, phone_last4: str) -> str:
    if len(phone_last4) != 4 or not phone_last4.isdigit():
        raise ValueError("phone_last4 must contain exactly four digits")
    cursor.execute("SELECT id FROM web_users WHERE phone_last4=%s AND status='active' ORDER BY id", (phone_last4,))
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one active account ending in {phone_last4}, found {len(rows)}")
    return str(rows[0][0])


def _as_datetime(value: str | None) -> datetime:
    if not value:
        raise ValueError("source timestamp is required")
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _due_datetime(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value[:10]), datetime.min.time())


def _diagnosis(item: dict[str, Any]) -> str:
    payload = {
        "schema": "math-error-diagnosis/v1",
        "cause_code": item["cause_code"],
        "cause_evidence": item["cause_detail"],
        "knowledge_points": item["knowledge_points"],
        "correct_solution": item["correct_solution"],
        "final_answer": item["correct_answer"],
        "prevention_cue": None,
        "source_migration": {"kind": "desktop_error_notebook", "source_error_id": item["id"]},
    }
    return _canonical(payload)


def inspect_target(plan: dict[str, Any], phone_last4: str) -> dict[str, Any]:
    connection = _connection()
    cursor = connection.cursor()
    try:
        user_id = _resolve_user(cursor, phone_last4)
        cursor.execute("SELECT COUNT(*) FROM error_notebook_entries WHERE user_id=%s", (user_id,))
        existing = int(cursor.fetchone()[0])
        mapped = {_question_id(item["question_id"]) for item in plan["errors"] if item["question_id"]}
        if mapped:
            placeholders = ",".join(["%s"] * len(mapped))
            cursor.execute(f"SELECT COUNT(*) FROM questions WHERE id IN ({placeholders})", tuple(sorted(mapped)))
            linked = int(cursor.fetchone()[0])
        else:
            linked = 0
        return {"account_last4": phone_last4, "existing_errors": existing, "mapped_questions": linked}
    finally:
        cursor.close()
        connection.close()


def commit(plan: dict[str, Any], phone_last4: str) -> dict[str, Any]:
    connection = _connection()
    cursor = connection.cursor()
    inserted = 0
    imported_reviews = 0
    imported_recommendations = 0
    skipped_recommendations = 0
    try:
        connection.begin()
        user_id = _resolve_user(cursor, phone_last4)
        source_question_ids = {
            _question_id(source_id)
            for item in plan["errors"]
            for source_id in ([item["question_id"]] + [recommendation["question_id"] for recommendation in item["recommendations"]])
            if source_id
        }
        question_rows: dict[str, tuple[str, str, int]] = {}
        if source_question_ids:
            placeholders = ",".join(["%s"] * len(source_question_ids))
            cursor.execute(
                "SELECT q.id,q.status,s.license_status,EXISTS(SELECT 1 FROM question_versions v JOIN question_verifications x ON x.question_version_id=v.id AND x.verdict='verified' WHERE v.question_id=q.id AND v.version_no=q.current_version_no) "
                f"FROM questions q JOIN question_sources s ON s.id=q.source_id WHERE q.id IN ({placeholders})",
                tuple(sorted(source_question_ids)),
            )
            question_rows = {str(row[0]): (str(row[1]), str(row[2]), int(row[3])) for row in cursor.fetchall()}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for item in plan["errors"]:
            source_id = str(item["id"])
            created_at = _as_datetime(item.get("created_at") or item["occurred_at"])
            row_hash = hashlib.sha256(_canonical(item).encode("utf-8")).hexdigest()
            file_id = _stable_id("file", user_id, source_id)
            intake_id = _stable_id("intake", user_id, source_id)
            attempt_id = _stable_id("attempt", user_id, source_id)
            candidate_id = _stable_id("candidate", user_id, source_id)
            error_id = _stable_id("error", user_id, source_id)
            diagnosis = _diagnosis(item)
            question_id = _question_id(item["question_id"])
            if question_id not in question_rows:
                question_id = None
            cursor.execute(
                "INSERT IGNORE INTO web_files (id,user_id,purpose,original_name,object_key,content_sha256,media_type,byte_size,status,created_at,updated_at) VALUES (%s,%s,'question_image',%s,%s,%s,'application/json',%s,'deleted',%s,%s)",
                (file_id, user_id, f"desktop-error-{source_id}.json"[:255], f"migration/desktop-error/{user_id}/{source_id}.json", row_hash, len(_canonical(item).encode("utf-8")), created_at, created_at),
            )
            cursor.execute(
                "INSERT IGNORE INTO intake_items (id,user_id,file_id,item_no,input_version,status,question_text,answer_text,evidence_json,created_at,updated_at) VALUES (%s,%s,%s,1,1,'confirmed',%s,%s,%s,%s,%s)",
                (intake_id, user_id, file_id, item["problem_text"], item["student_answer"] or "", _canonical({"source": "desktop_error_notebook", "source_error_id": source_id, "source_sha256": row_hash}), created_at, created_at),
            )
            cursor.execute(
                "INSERT IGNORE INTO attempts (id,user_id,intake_id,question_id,input_version,idempotency_key,question_text,answer_text,status,created_at,updated_at) VALUES (%s,%s,%s,%s,1,%s,%s,%s,'committed',%s,%s)",
                (attempt_id, user_id, intake_id, question_id, "desktop-" + _stable_id("key", user_id, source_id), item["problem_text"], item["student_answer"] or "", created_at, created_at),
            )
            result_hash = hashlib.sha256(_canonical([1, "incorrect", item["first_wrong_step"], diagnosis]).encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT IGNORE INTO grade_candidates (id,user_id,attempt_id,input_version,verdict,first_error,evidence_text,confidence,result_sha256,status,created_at) VALUES (%s,%s,%s,1,'incorrect',%s,%s,%s,%s,'committed',%s)",
                (candidate_id, user_id, attempt_id, item["first_wrong_step"], diagnosis, item["confidence"], result_hash, created_at),
            )
            status = "mastered" if item["status"] == "mastered" else "open"
            cursor.execute(
                "INSERT IGNORE INTO error_notebook_entries (id,user_id,attempt_id,grade_candidate_id,question_id,question_text,answer_text,first_error,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (error_id, user_id, attempt_id, candidate_id, question_id, item["problem_text"], item["student_answer"] or "", item["first_wrong_step"], status, created_at, created_at),
            )
            is_new = cursor.rowcount == 1
            inserted += int(is_new)
            completed = [review for review in item["reviews"] if review["completed_at"] and review["result"] in {"correct", "partial", "wrong"}]
            current = next((review for review in item["reviews"] if not review["completed_at"]), None) if status != "mastered" else None
            tasks: dict[int, dict[str, Any]] = {}
            for review in completed:
                tasks[int(review["stage"])] = review | {"target_status": "completed"}
            if current:
                tasks[int(current["stage"])] = current | {"target_status": "ready" if _due_datetime(current["due_date"]) <= now else "pending"}
            for stage, review in tasks.items():
                task_id = _stable_id("review-task", user_id, f"{source_id}:{stage}")
                cursor.execute(
                    "INSERT IGNORE INTO review_tasks (id,user_id,error_id,stage,due_at,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (task_id, user_id, error_id, stage, _due_datetime(review["due_date"]), review["target_status"], created_at),
                )
            for review in completed:
                stage = int(review["stage"])
                task_id = _stable_id("review-task", user_id, f"{source_id}:{stage}")
                review_id = _stable_id("review-attempt", user_id, str(review["id"]))
                cursor.execute(
                    "INSERT IGNORE INTO review_attempts (id,user_id,review_task_id,error_id,stage,result,idempotency_key,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (review_id, user_id, task_id, error_id, stage, review["result"], "desktop-review-" + review_id, _as_datetime(review["completed_at"])),
                )
                imported_reviews += int(cursor.rowcount == 1)
            for recommendation in item["recommendations"]:
                target_question_id = _question_id(recommendation["question_id"])
                target = question_rows.get(target_question_id or "")
                if target_question_id is None or target is None or target != ("verified", "open", 1) and target != ("verified", "user_authorized", 1):
                    skipped_recommendations += 1
                    continue
                recommendation_id = _stable_id("recommendation", user_id, str(recommendation["id"]))
                recommendation_status = "completed" if recommendation["status"] == "correct" else "assigned"
                cursor.execute(
                    "INSERT IGNORE INTO recommendations (id,user_id,error_id,question_id,reason,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (recommendation_id, user_id, error_id, target_question_id, recommendation["reason"], recommendation_status, _as_datetime(recommendation["assigned_at"])),
                )
                imported_recommendations += int(cursor.rowcount == 1)
            if is_new:
                cursor.execute(
                    "INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'error.migrated','error',%s,%s,%s)",
                    (user_id, error_id, _canonical({"source": "desktop_error_notebook", "source_error_id": source_id, "source_sha256": plan["source_sha256"]}), now),
                )
        cursor.execute("SELECT COUNT(*) FROM error_notebook_entries WHERE user_id=%s", (user_id,))
        total = int(cursor.fetchone()[0])
        connection.commit()
        return {
            "account_last4": phone_last4,
            "inserted_errors": inserted,
            "total_errors": total,
            "inserted_completed_reviews": imported_reviews,
            "inserted_recommendations": imported_recommendations,
            "skipped_recommendations": skipped_recommendations,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate desktop error-notebook records into one Web account")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--phone-last4", required=True)
    parser.add_argument("--source-sha256")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        plan = extract(args.source_root.resolve())
        if args.source_sha256 and args.source_sha256 != plan["source_sha256"]:
            raise RuntimeError("source data changed after dry-run")
        if args.commit and not args.source_sha256:
            raise RuntimeError("--commit requires --source-sha256 from a dry-run")
        target = commit(plan, args.phone_last4) if args.commit else inspect_target(plan, args.phone_last4)
        print(_canonical({"mode": "commit" if args.commit else "dry-run", "source": plan["source"], "source_sha256": plan["source_sha256"], "source_counts": plan["counts"]} | target))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(_canonical({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
