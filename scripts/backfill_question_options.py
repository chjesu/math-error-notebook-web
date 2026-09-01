"""Backfill question_versions.options_json from the canonical desktop question bank.

The canonical desktop bank (data/math_notebook.db) carries options_json for multiple-choice
questions, but the earlier web migration dropped options. This script copies options for
existing web questions by matching the desktop fingerprint (questions.canonical_sha256).
Read-only on the desktop bank; writes only options_json on the web MySQL.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _desktop_options(source_root: Path) -> dict[str, str]:
    database = source_root / "data" / "math_notebook.db"
    if not database.is_file():
        raise RuntimeError(f"desktop bank is missing: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT fingerprint, options_json FROM questions WHERE options_json IS NOT NULL"
        )
        return {str(row[0]): str(row[1]) for row in cursor.fetchall()}
    finally:
        connection.close()


def _connection() -> Any:
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
        raise RuntimeError(f"missing MySQL env: {', '.join(missing)}")
    return pymysql.connect(
        host=os.environ["LZLM_MYSQL_HOST"],
        user=os.environ["LZLM_MYSQL_USER"],
        password=os.environ["LZLM_MYSQL_PASSWORD"],
        database=os.environ["LZLM_MYSQL_DATABASE"],
        charset="utf8mb4",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill question options from the desktop bank")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="report planned updates without writing")
    args = parser.parse_args()

    desktop = _desktop_options(args.source_root)
    if not desktop:
        raise RuntimeError("desktop bank has no options to backfill")

    connection = _connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT v.id, q.canonical_sha256 FROM questions q "
            "JOIN question_versions v ON v.question_id = q.id AND v.version_no = q.current_version_no "
            "WHERE v.options_json IS NULL"
        )
        rows = cursor.fetchall()
        matched: list[tuple[str, str, str]] = []  # version_id, options_json, fingerprint
        for version_id, fingerprint in rows:
            options = desktop.get(str(fingerprint))
            if options is not None:
                matched.append((str(version_id), options, str(fingerprint)))
        print(f"desktop options entries: {len(desktop)}; web questions missing options: {len(rows)}; matchable: {len(matched)}")
        if args.dry_run or not matched:
            return 0
        connection.begin()
        updated = 0
        for version_id, options, _fingerprint in matched:
            cursor.execute(
                "UPDATE question_versions SET options_json=%s WHERE id=%s AND options_json IS NULL",
                (options, version_id),
            )
            updated += cursor.rowcount
        connection.commit()
        print(f"updated question_versions rows with options: {updated}")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
