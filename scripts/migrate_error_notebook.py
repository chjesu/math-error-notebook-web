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
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE_ROOT = ROOT / "data" / "runtime" / "quarantine"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024


def _stable_id(kind: str, user_id: str, source_id: str) -> str:
    return hashlib.sha256(f"desktop-error:{kind}:{user_id}:{source_id}".encode("utf-8")).hexdigest()[:32]


def _question_id(source_id: str | None) -> str | None:
    return hashlib.sha256(("question:" + source_id).encode("utf-8")).hexdigest()[:32] if source_id else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _image_metadata(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    path = Path(value).resolve()
    if not path.is_file():
        raise RuntimeError(f"source image is missing: {path.name}")
    content = path.read_bytes()
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"source image is empty or too large: {path.name}")
    suffix = path.suffix.lower()
    if content.startswith(b"\xff\xd8\xff") and suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n") and suffix == ".png":
        media_type = "image/png"
    else:
        raise RuntimeError(f"unsupported source image: {path.name}")
    return {
        "local_path": str(path),
        "original_name": path.name,
        "media_type": media_type,
        "byte_size": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def _portable_item(item: dict[str, Any]) -> dict[str, Any]:
    portable = dict(item)
    image = portable.get("image")
    if image:
        portable["image"] = {key: value for key, value in image.items() if key != "local_path"}
    return portable


def _pdf_metadata(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    if not content or len(content) > MAX_PDF_BYTES or path.suffix.lower() != ".pdf" or not content.startswith(b"%PDF-"):
        raise RuntimeError(f"invalid source PDF: {path.name}")
    return {
        "local_path": str(path.resolve()),
        "original_name": path.name,
        "byte_size": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def _portable_pdf(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "local_path"}


def _item_digest(item: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_portable_item(item)).encode("utf-8")).hexdigest()


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
        knowledge: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT ek.error_id,k.code,k.name FROM error_knowledge ek JOIN knowledge_points k ON k.code=ek.knowledge_code ORDER BY ek.error_id,k.code"
        ):
            knowledge[str(row[0])].append({"code": str(row[1]), "name": str(row[2])})
        reviews: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id,error_id,cycle,stage,due_date,completed_at,result,note FROM review_schedule ORDER BY error_id,cycle,stage,id"
        ):
            reviews[str(row[1])].append(dict(row))
        recommendations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id,error_id,question_id,rank,score,reason,assigned_at,status FROM recommendations ORDER BY error_id,rank,id"
        ):
            recommendations[str(row[1])].append(dict(row))
        attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id,question_id,error_id,submitted_answer,is_correct,cause_code,attempted_at,note FROM attempts WHERE error_id IS NOT NULL ORDER BY error_id,attempted_at,id"
        ):
            attempts[str(row[2])].append(dict(row))
        packet_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT packet_sha256,packet_path,packet_date,error_id,cycle,stage,result,review_schedule_id,attempt_ids_json,note,result_file_sha256,finalized_at FROM review_packet_items ORDER BY error_id,cycle,stage,finalized_at"
        ):
            packet_items[str(row[3])].append(dict(row))
        errors = []
        for row in connection.execute(
            "SELECT id,occurred_at,problem_text,student_answer,correct_answer,correct_solution,first_wrong_step,cause_code,cause_detail,evidence_json,difficulty,confidence,image_path,question_id,status,created_at,raw_analysis_json FROM errors ORDER BY occurred_at,id"
        ):
            item = dict(row)
            source_id = str(item["id"])
            item["knowledge"] = knowledge[source_id]
            item["knowledge_points"] = [record["name"] for record in knowledge[source_id]]
            item["reviews"] = reviews[source_id]
            item["recommendations"] = recommendations[source_id]
            item["attempts"] = attempts[source_id]
            item["review_packet_items"] = packet_items[source_id]
            item["image"] = _image_metadata(item.pop("image_path"))
            errors.append(item)
    finally:
        connection.close()
    pdf_root = source_root / "output" / "pdf"
    pdfs = [_pdf_metadata(path) for path in sorted(pdf_root.glob("*.pdf"), key=lambda value: value.name)] if pdf_root.is_dir() else []
    digest = hashlib.sha256(_canonical({
        "errors": [_portable_item(item) for item in errors],
        "pdfs": [_portable_pdf(item) for item in pdfs],
    }).encode("utf-8")).hexdigest()
    return {
        "source": str(database.resolve()),
        "source_sha256": digest,
        "errors": errors,
        "pdfs": pdfs,
        "counts": {
            "errors": len(errors),
            "knowledge_links": sum(len(item["knowledge_points"]) for item in errors),
            "completed_reviews": sum(1 for item in errors for review in item["reviews"] if review["completed_at"]),
            "recommendations": sum(len(item["recommendations"]) for item in errors),
            "images": sum(1 for item in errors if item["image"]),
            "unique_images": len({item["image"]["content_sha256"] for item in errors if item["image"]}),
            "attempts": sum(len(item["attempts"]) for item in errors),
            "review_packet_items": sum(len(item["review_packet_items"]) for item in errors),
            "pdfs": len(pdfs),
            "unique_pdfs": len({item["content_sha256"] for item in pdfs}),
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
    raw = _json_value(item.get("raw_analysis_json"))
    prevention_cue = raw.get("prevention_cue") if isinstance(raw, dict) else None
    payload = {
        "schema": "math-error-diagnosis/v1",
        "cause_code": item["cause_code"],
        "cause_evidence": item["cause_detail"],
        "knowledge_points": item["knowledge_points"],
        "correct_solution": item["correct_solution"],
        "final_answer": item["correct_answer"],
        "prevention_cue": prevention_cue,
        "evidence": _json_value(item.get("evidence_json")),
        "difficulty": item["difficulty"],
        "source_migration": {"kind": "desktop_error_notebook", "source_error_id": item["id"]},
    }
    return _canonical(payload)


def _source_snapshot(item: dict[str, Any], row_hash: str) -> str:
    image = item.get("image")
    payload = {
        "schema": "desktop-error-snapshot/v2",
        "source": "desktop_error_notebook",
        "source_error_id": item["id"],
        "source_sha256": row_hash,
        "occurred_at": item["occurred_at"],
        "difficulty": item["difficulty"],
        "confidence": item["confidence"],
        "evidence": _json_value(item.get("evidence_json")),
        "raw_analysis": _json_value(item.get("raw_analysis_json")),
        "knowledge": item["knowledge"],
        "review_schedule": item["reviews"],
        "recommendations": item["recommendations"],
        "attempts": item["attempts"],
        "review_packet_items": item["review_packet_items"],
        "image": ({key: value for key, value in image.items() if key != "local_path"} if image else None),
    }
    if isinstance(payload["raw_analysis"], dict):
        payload["raw_analysis"].pop("image_path", None)
    return _canonical(payload)


def _item_numbers(items: list[dict[str, Any]]) -> dict[str, int]:
    used: dict[str, set[int]] = defaultdict(set)
    numbers: dict[str, int] = {}
    for item in items:
        image = item.get("image")
        image_key = image["content_sha256"] if image else "missing"
        candidate = int(hashlib.sha256(str(item["id"]).encode("utf-8")).hexdigest()[:8], 16) or 1
        while candidate in used[image_key]:
            candidate = (candidate + 1) & 0xFFFFFFFF or 1
        used[image_key].add(candidate)
        numbers[str(item["id"])] = candidate
    return numbers


def _placeholders(values: list[str]) -> str:
    return ",".join(["%s"] * len(values))


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
        cursor.execute("SELECT COUNT(*) FROM web_jobs WHERE user_id=%s AND job_type='practice_pdf' AND status='completed'", (user_id,))
        existing_pdfs = int(cursor.fetchone()[0])
        pdf_job_ids = [_stable_id("pdf-job", user_id, item["original_name"]) for item in plan["pdfs"]]
        if pdf_job_ids:
            cursor.execute(
                f"SELECT COUNT(*) FROM web_jobs WHERE user_id=%s AND id IN ({_placeholders(pdf_job_ids)})",
                (user_id, *pdf_job_ids),
            )
            matched_pdfs = int(cursor.fetchone()[0])
        else:
            matched_pdfs = 0
        return {"account_last4": phone_last4, "existing_errors": existing, "mapped_questions": linked, "existing_pdfs": existing_pdfs, "matched_pdfs": matched_pdfs}
    finally:
        cursor.close()
        connection.close()


def commit(plan: dict[str, Any], phone_last4: str, file_root: Path = DEFAULT_FILE_ROOT) -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services.web_files import FileIntake

    connection = _connection()
    cursor = connection.cursor()
    files = FileIntake(file_root)
    staged_paths: set[Path] = set()
    retired_keys: set[str] = set()
    inserted = 0
    updated = 0
    imported_reviews = 0
    imported_recommendations = 0
    skipped_recommendations = 0
    pdf_records: dict[str, str] = {}
    try:
        connection.begin()
        user_id = _resolve_user(cursor, phone_last4)
        error_ids = [_stable_id("error", user_id, str(item["id"])) for item in plan["errors"]]
        legacy_file_ids = [_stable_id("file", user_id, str(item["id"])) for item in plan["errors"]]
        existing_errors: set[str] = set()
        if error_ids:
            cursor.execute(
                f"SELECT id FROM error_notebook_entries WHERE user_id=%s AND id IN ({_placeholders(error_ids)})",
                (user_id, *error_ids),
            )
            existing_errors = {str(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                f"SELECT object_key FROM web_files WHERE user_id=%s AND id IN ({_placeholders(legacy_file_ids)})",
                (user_id, *legacy_file_ids),
            )
            retired_keys.update(str(row[0]) for row in cursor.fetchall())
            cursor.execute(
                f"DELETE FROM review_attempts WHERE user_id=%s AND error_id IN ({_placeholders(error_ids)})",
                (user_id, *error_ids),
            )
            cursor.execute(
                f"DELETE FROM recommendations WHERE user_id=%s AND error_id IN ({_placeholders(error_ids)})",
                (user_id, *error_ids),
            )
            cursor.execute(
                f"DELETE FROM review_tasks WHERE user_id=%s AND error_id IN ({_placeholders(error_ids)})",
                (user_id, *error_ids),
            )
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
        item_numbers = _item_numbers(plan["errors"])
        image_records: dict[str, str] = {}
        for item in plan["errors"]:
            source_id = str(item["id"])
            created_at = _as_datetime(item.get("created_at") or item["occurred_at"])
            row_hash = _item_digest(item)
            image = item.get("image")
            if not image:
                raise RuntimeError(f"source error has no image: {source_id}")
            image_sha256 = str(image["content_sha256"])
            file_id = image_records.get(image_sha256)
            if not file_id:
                source_image = Path(str(image["local_path"]))
                content = source_image.read_bytes()
                if len(content) != int(image["byte_size"]) or hashlib.sha256(content).hexdigest() != image_sha256:
                    raise RuntimeError(f"source image changed after dry-run: {source_image.name}")
                cursor.execute(
                    "SELECT id,object_key,content_sha256,media_type,byte_size,status,original_name FROM web_files WHERE user_id=%s AND purpose='question_image' AND content_sha256=%s FOR UPDATE",
                    (user_id, image_sha256),
                )
                existing_file = cursor.fetchone()
                stored = False
                if existing_file and str(existing_file[5]) == "ready":
                    try:
                        stored_content = files.read(str(existing_file[1]))
                        stored = hashlib.sha256(stored_content).hexdigest() == image_sha256
                    except (LookupError, OSError):
                        stored = False
                if stored:
                    file_id = str(existing_file[0])
                else:
                    candidate = files.quarantine(
                        user_id=user_id,
                        original_name=str(image["original_name"]),
                        content=content,
                    )
                    staged_paths.add(candidate.local_path)
                    if existing_file:
                        file_id = str(existing_file[0])
                        retired_keys.add(str(existing_file[1]))
                        cursor.execute(
                            "UPDATE web_files SET original_name=%s,object_key=%s,content_sha256=%s,media_type=%s,byte_size=%s,status='ready',updated_at=%s WHERE id=%s AND user_id=%s",
                            (candidate.original_name, candidate.object_key, candidate.content_sha256, candidate.media_type, candidate.byte_size, now, file_id, user_id),
                        )
                    else:
                        file_id = _stable_id("image", user_id, image_sha256)
                        cursor.execute(
                            "INSERT INTO web_files (id,user_id,purpose,original_name,object_key,content_sha256,media_type,byte_size,status,created_at,updated_at) VALUES (%s,%s,'question_image',%s,%s,%s,%s,%s,'ready',%s,%s)",
                            (file_id, user_id, candidate.original_name, candidate.object_key, candidate.content_sha256, candidate.media_type, candidate.byte_size, created_at, now),
                        )
                image_records[image_sha256] = file_id
            intake_id = _stable_id("intake", user_id, source_id)
            attempt_id = _stable_id("attempt", user_id, source_id)
            candidate_id = _stable_id("candidate", user_id, source_id)
            error_id = _stable_id("error", user_id, source_id)
            diagnosis = _diagnosis(item)
            question_id = _question_id(item["question_id"])
            if question_id not in question_rows:
                question_id = None
            cursor.execute(
                "INSERT INTO intake_items (id,user_id,file_id,item_no,input_version,status,question_text,answer_text,evidence_json,created_at,updated_at) VALUES (%s,%s,%s,%s,1,'confirmed',%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE file_id=VALUES(file_id),item_no=VALUES(item_no),status='confirmed',question_text=VALUES(question_text),answer_text=VALUES(answer_text),evidence_json=VALUES(evidence_json),updated_at=VALUES(updated_at)",
                (intake_id, user_id, file_id, item_numbers[source_id], item["problem_text"], item["student_answer"] or "", _source_snapshot(item, row_hash), created_at, now),
            )
            cursor.execute(
                "INSERT INTO attempts (id,user_id,intake_id,question_id,input_version,idempotency_key,question_text,answer_text,status,created_at,updated_at) VALUES (%s,%s,%s,%s,1,%s,%s,%s,'committed',%s,%s) ON DUPLICATE KEY UPDATE intake_id=VALUES(intake_id),question_id=VALUES(question_id),question_text=VALUES(question_text),answer_text=VALUES(answer_text),status='committed',updated_at=VALUES(updated_at)",
                (attempt_id, user_id, intake_id, question_id, "desktop-" + _stable_id("key", user_id, source_id), item["problem_text"], item["student_answer"] or "", created_at, now),
            )
            result_hash = hashlib.sha256(_canonical([1, "incorrect", item["first_wrong_step"], diagnosis]).encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT INTO grade_candidates (id,user_id,attempt_id,input_version,verdict,first_error,evidence_text,confidence,result_sha256,status,created_at) VALUES (%s,%s,%s,1,'incorrect',%s,%s,%s,%s,'committed',%s) ON DUPLICATE KEY UPDATE verdict='incorrect',first_error=VALUES(first_error),evidence_text=VALUES(evidence_text),confidence=VALUES(confidence),result_sha256=VALUES(result_sha256),status='committed'",
                (candidate_id, user_id, attempt_id, item["first_wrong_step"], diagnosis, item["confidence"], result_hash, created_at),
            )
            status = "mastered" if item["status"] == "mastered" else "open"
            cursor.execute(
                "INSERT INTO error_notebook_entries (id,user_id,attempt_id,grade_candidate_id,question_id,question_text,answer_text,first_error,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE grade_candidate_id=VALUES(grade_candidate_id),question_id=VALUES(question_id),question_text=VALUES(question_text),answer_text=VALUES(answer_text),first_error=VALUES(first_error),status=VALUES(status),updated_at=VALUES(updated_at)",
                (error_id, user_id, attempt_id, candidate_id, question_id, item["problem_text"], item["student_answer"] or "", item["first_wrong_step"], status, created_at, now),
            )
            is_new = error_id not in existing_errors
            inserted += int(is_new)
            updated += int(not is_new)
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
                    "INSERT INTO review_tasks (id,user_id,error_id,stage,due_at,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (task_id, user_id, error_id, stage, _due_datetime(review["due_date"]), review["target_status"], created_at),
                )
            for review in completed:
                stage = int(review["stage"])
                task_id = _stable_id("review-task", user_id, f"{source_id}:{stage}")
                review_id = _stable_id("review-attempt", user_id, str(review["id"]))
                cursor.execute(
                    "INSERT INTO review_attempts (id,user_id,review_task_id,error_id,stage,result,idempotency_key,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (review_id, user_id, task_id, error_id, stage, review["result"], "desktop-review-" + review_id, _as_datetime(review["completed_at"])),
                )
                imported_reviews += 1
            for recommendation in item["recommendations"]:
                target_question_id = _question_id(recommendation["question_id"])
                target = question_rows.get(target_question_id or "")
                if target_question_id is None or target not in {("verified", "open", 1), ("verified", "user_authorized", 1)}:
                    skipped_recommendations += 1
                    continue
                recommendation_id = _stable_id("recommendation", user_id, str(recommendation["id"]))
                recommendation_status = "completed" if recommendation["status"] == "correct" else "assigned"
                cursor.execute(
                    "INSERT INTO recommendations (id,user_id,error_id,question_id,reason,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (recommendation_id, user_id, error_id, target_question_id, recommendation["reason"], recommendation_status, _as_datetime(recommendation["assigned_at"])),
                )
                imported_recommendations += 1
        for pdf in plan["pdfs"]:
            source_pdf = Path(str(pdf["local_path"]))
            content = source_pdf.read_bytes()
            pdf_sha256 = str(pdf["content_sha256"])
            if len(content) != int(pdf["byte_size"]) or hashlib.sha256(content).hexdigest() != pdf_sha256:
                raise RuntimeError(f"source PDF changed after dry-run: {source_pdf.name}")
            file_id = pdf_records.get(pdf_sha256)
            if not file_id:
                cursor.execute(
                    "SELECT id,object_key,status FROM web_files WHERE user_id=%s AND purpose='practice_pdf' AND content_sha256=%s FOR UPDATE",
                    (user_id, pdf_sha256),
                )
                existing_file = cursor.fetchone()
                stored = False
                if existing_file and str(existing_file[2]) == "ready":
                    try:
                        stored = hashlib.sha256(files.read(str(existing_file[1]))).hexdigest() == pdf_sha256
                    except (LookupError, OSError):
                        stored = False
                if stored:
                    file_id = str(existing_file[0])
                else:
                    candidate = files.quarantine(user_id=user_id, original_name=str(pdf["original_name"]), content=content)
                    staged_paths.add(candidate.local_path)
                    modified_at = _as_datetime(str(pdf["modified_at"]))
                    if existing_file:
                        file_id = str(existing_file[0])
                        retired_keys.add(str(existing_file[1]))
                        cursor.execute(
                            "UPDATE web_files SET original_name=%s,object_key=%s,media_type='application/pdf',byte_size=%s,status='ready',updated_at=%s WHERE id=%s AND user_id=%s",
                            (candidate.original_name, candidate.object_key, candidate.byte_size, now, file_id, user_id),
                        )
                    else:
                        file_id = _stable_id("pdf-file", user_id, pdf_sha256)
                        cursor.execute(
                            "INSERT INTO web_files (id,user_id,purpose,original_name,object_key,content_sha256,media_type,byte_size,status,created_at,updated_at) VALUES (%s,%s,'practice_pdf',%s,%s,%s,'application/pdf',%s,'ready',%s,%s)",
                            (file_id, user_id, candidate.original_name, candidate.object_key, pdf_sha256, candidate.byte_size, modified_at, now),
                        )
                pdf_records[pdf_sha256] = file_id
            job_id = _stable_id("pdf-job", user_id, str(pdf["original_name"]))
            idempotency_key = "desktop-pdf-" + _stable_id("pdf-key", user_id, str(pdf["original_name"]))
            checkpoint = _canonical({
                "file_id": file_id,
                "question_count": 0,
                "recommendation_gap_count": 0,
                "include_answers": False,
                "source": "desktop_skill",
                "filename": pdf["original_name"],
            })
            modified_at = _as_datetime(str(pdf["modified_at"]))
            cursor.execute(
                "INSERT INTO web_jobs (id,user_id,job_type,resource_type,resource_id,idempotency_key,input_sha256,status,checkpoint_json,result_json,created_at,updated_at) "
                "VALUES (%s,%s,'practice_pdf','file',%s,%s,%s,'completed',%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE resource_type='file',resource_id=VALUES(resource_id),input_sha256=VALUES(input_sha256),status='completed',checkpoint_json=VALUES(checkpoint_json),result_json=VALUES(result_json),last_error_code=NULL,updated_at=VALUES(updated_at)",
                (job_id, user_id, file_id, idempotency_key, pdf_sha256, checkpoint, checkpoint, modified_at, modified_at),
            )
        if legacy_file_ids:
            cursor.execute(
                f"DELETE FROM web_files WHERE user_id=%s AND id IN ({_placeholders(legacy_file_ids)})",
                (user_id, *legacy_file_ids),
            )
        cursor.execute("SELECT COUNT(*) FROM error_notebook_entries WHERE user_id=%s", (user_id,))
        total = int(cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'error.sync.completed','account',%s,%s,%s)",
            (user_id, user_id, _canonical({"source": "desktop_error_notebook", "source_sha256": plan["source_sha256"], "counts": plan["counts"]}), now),
        )
        connection.commit()
        for object_key in retired_keys:
            try:
                files.resolve(object_key).unlink()
            except (LookupError, OSError):
                pass
        return {
            "account_last4": phone_last4,
            "inserted_errors": inserted,
            "updated_errors": updated,
            "total_errors": total,
            "synchronized_completed_reviews": imported_reviews,
            "synchronized_recommendations": imported_recommendations,
            "skipped_recommendations": skipped_recommendations,
            "ready_images": len(image_records),
            "ready_pdfs": len(plan["pdfs"]),
            "unique_ready_pdfs": len(pdf_records),
        }
    except Exception:
        connection.rollback()
        for path in staged_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        cursor.close()
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate desktop error-notebook records into one Web account")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--phone-last4", required=True)
    parser.add_argument("--source-sha256")
    parser.add_argument("--file-root", type=Path, default=DEFAULT_FILE_ROOT)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        plan = extract(args.source_root.resolve())
        if args.source_sha256 and args.source_sha256 != plan["source_sha256"]:
            raise RuntimeError("source data changed after dry-run")
        if args.commit and not args.source_sha256:
            raise RuntimeError("--commit requires --source-sha256 from a dry-run")
        target = commit(plan, args.phone_last4, args.file_root.resolve()) if args.commit else inspect_target(plan, args.phone_last4)
        print(_canonical({"mode": "commit" if args.commit else "dry-run", "source": plan["source"], "source_sha256": plan["source_sha256"], "source_counts": plan["counts"]} | target))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(_canonical({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
