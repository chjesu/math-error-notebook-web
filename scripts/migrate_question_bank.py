"""Read the canonical desktop bank through notebook.py and import it idempotently."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def _run_notebook(source_root: Path, *arguments: str) -> Any:
    database = source_root / "data" / "math_notebook.db"
    scripts = (
        source_root / "scripts" / "notebook.py",
        source_root / ".agents" / "skills" / "math-error-notebook" / "scripts" / "notebook.py",
    )
    script = next((candidate for candidate in scripts if candidate.is_file()), None)
    if script is None or not database.is_file():
        raise RuntimeError("source root must contain the canonical notebook skill and data/math_notebook.db")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(script), "--db", str(database), *arguments, "--json"],
        cwd=source_root,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("desktop notebook read failed: " + " ".join(result.stderr.splitlines()[-2:]))
    return json.loads(result.stdout)


def extract(source_root: Path) -> dict[str, Any]:
    health = _run_notebook(source_root, "bank-info")
    if health.get("status") not in {None, "ok"} or health.get("integrity") is False:
        raise RuntimeError("desktop bank integrity check failed")
    sources = _run_notebook(source_root, "sources")
    questions = _run_notebook(source_root, "search", "--limit", "1000000", "--full")
    if not isinstance(sources, list) or not isinstance(questions, list):
        raise RuntimeError("unexpected desktop notebook response")
    source_map = {str(item.get("name", "")): item for item in sources}
    rows = [map_question(item, source_map.get(str(item.get("source_name", "")), {})) for item in questions]
    ids = [item["source_question_id"] for item in rows]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate source question ids")
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"source": health.get("canonical_path", str(source_root / "data" / "math_notebook.db")), "questions": rows, "count": len(rows), "verified": sum(item["status"] == "verified" for item in rows), "sha256": digest}


def map_question(question: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    required = ("id", "stem", "answer", "source_name", "license", "fingerprint")
    if any(question.get(name) in {None, ""} for name in required):
        raise ValueError("question is missing a required migration field")
    source_name = str(question["source_name"])
    license_name = str(question["license"])
    rights = bool(source.get("rights_confirmed", False))
    license_lower = license_name.lower()
    license_status = "user_authorized" if rights else ("open" if any(token in license_lower for token in ("public", "open", "cc by", "creative commons")) else "restricted")
    payload = {
        "stem": str(question["stem"]),
        "options": question.get("options"),
        "answer": str(question["answer"]),
        "solution": question.get("solution"),
    }
    content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    source_key = json.dumps([source_name, question.get("source_url"), license_name], ensure_ascii=False, separators=(",", ":"))
    source_id = hashlib.sha256(("source:" + source_key).encode("utf-8")).hexdigest()[:32]
    question_id = hashlib.sha256(("question:" + str(question["id"])).encode("utf-8")).hexdigest()[:32]
    version_id = hashlib.sha256(("version:" + question_id + ":" + content_hash).encode("ascii")).hexdigest()[:32]
    verified = bool(question.get("verified")) and license_status != "restricted"
    return {
        "source_question_id": str(question["id"]),
        "source_id": source_id,
        "source_title": source_name,
        "source_uri": question.get("source_url"),
        "license_status": license_status,
        "source_sha256": hashlib.sha256(source_key.encode("utf-8")).hexdigest(),
        "question_id": question_id,
        "version_id": version_id,
        "content_sha256": content_hash,
        "canonical_sha256": str(question["fingerprint"]),
        "stem_text": payload["stem"],
        "answer_text": payload["answer"],
        "solution_text": payload["solution"],
        "grade": question.get("grade"),
        "difficulty": question.get("difficulty"),
        "status": "verified" if verified else "candidate",
        "verification_sha256": hashlib.sha256(("desktop-quality-gate:" + str(question["id"]) + ":" + str(question["fingerprint"])).encode("utf-8")).hexdigest() if verified else None,
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


def commit(plan: dict[str, Any]) -> None:
    connection = _connection()
    cursor = connection.cursor()
    try:
        connection.begin()
        for item in plan["questions"]:
            cursor.execute("INSERT INTO question_sources (id,title,source_uri,license_status,content_sha256,created_at) VALUES (%s,%s,%s,%s,%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE title=VALUES(title),source_uri=VALUES(source_uri),license_status=VALUES(license_status),content_sha256=VALUES(content_sha256)", (item["source_id"], item["source_title"], item["source_uri"], item["license_status"], item["source_sha256"]))
            cursor.execute("INSERT INTO questions (id,source_id,canonical_sha256,grade,difficulty,status,current_version_no,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE source_id=VALUES(source_id),canonical_sha256=VALUES(canonical_sha256),grade=VALUES(grade),difficulty=VALUES(difficulty),status=VALUES(status),current_version_no=1,updated_at=UTC_TIMESTAMP(6)", (item["question_id"], item["source_id"], item["canonical_sha256"], item["grade"], item["difficulty"], item["status"]))
            cursor.execute("INSERT INTO question_versions (id,question_id,version_no,stem_text,answer_text,solution_text,content_sha256,created_at) VALUES (%s,%s,1,%s,%s,%s,%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE stem_text=VALUES(stem_text),answer_text=VALUES(answer_text),solution_text=VALUES(solution_text),content_sha256=VALUES(content_sha256)", (item["version_id"], item["question_id"], item["stem_text"], item["answer_text"], item["solution_text"], item["content_sha256"]))
            cursor.execute("SELECT id FROM question_versions WHERE question_id=%s AND version_no=1", (item["question_id"],))
            version_id = cursor.fetchone()[0]
            if item["verification_sha256"]:
                verification_id = hashlib.sha256(("verification:" + version_id + ":" + item["verification_sha256"]).encode("ascii")).hexdigest()[:32]
                cursor.execute("INSERT INTO question_verifications (id,question_version_id,verdict,method,evidence_sha256,verified_at) VALUES (%s,%s,'verified','independent',%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE verdict='verified',method='independent',evidence_sha256=VALUES(evidence_sha256),verified_at=UTC_TIMESTAMP(6)", (verification_id, version_id, item["verification_sha256"]))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the canonical desktop question bank to Web MySQL")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--rights-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        plan = extract(args.source_root.resolve())
        if args.commit:
            if not args.rights_confirmed:
                raise RuntimeError("--commit requires --rights-confirmed")
            commit(plan)
        print(json.dumps({key: plan[key] for key in ("source", "count", "verified", "sha256")} | {"mode": "commit" if args.commit else "dry-run"}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
