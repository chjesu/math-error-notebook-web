from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

from scripts import migrate_error_notebook


class ErrorNotebookMigrationTests(unittest.TestCase):
    def _source(self, root: Path) -> None:
        database = root / "data" / "math_notebook.db"
        skill = root / ".agents" / "skills" / "math-error-notebook" / "SKILL.md"
        database.parent.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        skill.write_text("skill", encoding="utf-8")
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE knowledge_points(code TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE errors(id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,problem_text TEXT NOT NULL,student_answer TEXT,correct_answer TEXT,correct_solution TEXT,first_wrong_step TEXT,cause_code TEXT NOT NULL,cause_detail TEXT NOT NULL,difficulty REAL NOT NULL,confidence REAL NOT NULL,question_id TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE error_knowledge(error_id TEXT NOT NULL REFERENCES errors(id),knowledge_code TEXT NOT NULL REFERENCES knowledge_points(code));
            CREATE TABLE review_schedule(id INTEGER PRIMARY KEY,error_id TEXT NOT NULL REFERENCES errors(id),cycle INTEGER NOT NULL,stage INTEGER NOT NULL,due_date TEXT NOT NULL,completed_at TEXT,result TEXT,note TEXT);
            CREATE TABLE recommendations(id TEXT PRIMARY KEY,error_id TEXT NOT NULL REFERENCES errors(id),question_id TEXT NOT NULL,rank INTEGER NOT NULL,reason TEXT NOT NULL,assigned_at TEXT NOT NULL,status TEXT NOT NULL);
            INSERT INTO knowledge_points VALUES ('line-circle','直线与圆');
            INSERT INTO errors VALUES ('err-1','2026-08-01T08:00:00+08:00','求圆的方程','x=1','x=2','完整解析','首步移项错误','calculation','移项符号错误',2.0,0.95,'q-1','active','2026-08-01T08:00:00+08:00');
            INSERT INTO error_knowledge VALUES ('err-1','line-circle');
            INSERT INTO review_schedule VALUES (1,'err-1',1,1,'2026-08-02','2026-08-02T09:00:00+08:00','correct',NULL);
            INSERT INTO review_schedule VALUES (2,'err-1',1,2,'2026-08-04',NULL,NULL,NULL);
            INSERT INTO recommendations VALUES ('rec-1','err-1','q-2',1,'同类巩固','2026-08-01T08:00:00+08:00','assigned');
            """
        )
        connection.commit()
        connection.close()

    def test_extract_is_read_only_complete_and_reproducible(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            first = migrate_error_notebook.extract(root)
            second = migrate_error_notebook.extract(root)
        self.assertEqual(first["source_sha256"], second["source_sha256"])
        self.assertEqual(first["counts"], {"errors": 1, "knowledge_links": 1, "completed_reviews": 1, "recommendations": 1})
        self.assertEqual(first["errors"][0]["knowledge_points"], ["直线与圆"])
        self.assertEqual(len(first["errors"][0]["reviews"]), 2)

    def test_ids_are_stable_and_scoped_to_target_user(self) -> None:
        first = migrate_error_notebook._stable_id("error", "user-a", "source-1")
        self.assertEqual(first, migrate_error_notebook._stable_id("error", "user-a", "source-1"))
        self.assertNotEqual(first, migrate_error_notebook._stable_id("error", "user-b", "source-1"))

    def test_diagnosis_preserves_cause_knowledge_and_solution(self) -> None:
        payload = json.loads(migrate_error_notebook._diagnosis({
            "id": "err-1", "cause_code": "calculation", "cause_detail": "符号错误",
            "knowledge_points": ["直线与圆"], "correct_solution": "过程", "correct_answer": "答案",
        }))
        self.assertEqual(payload["schema"], "math-error-diagnosis/v1")
        self.assertEqual(payload["knowledge_points"], ["直线与圆"])
        self.assertEqual((payload["cause_evidence"], payload["correct_solution"], payload["final_answer"]), ("符号错误", "过程", "答案"))

    def test_account_resolution_requires_one_active_last4_match(self) -> None:
        cursor = mock.Mock()
        cursor.fetchall.return_value = [("user-2970",)]
        self.assertEqual(migrate_error_notebook._resolve_user(cursor, "2970"), "user-2970")
        cursor.fetchall.return_value = []
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            migrate_error_notebook._resolve_user(cursor, "2970")
        with self.assertRaisesRegex(ValueError, "four digits"):
            migrate_error_notebook._resolve_user(cursor, "29x0")


if __name__ == "__main__":
    unittest.main()
