from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

from scripts import migrate_error_notebook
from tests.image_fixtures import jpeg_bytes


class ErrorNotebookMigrationTests(unittest.TestCase):
    def _source(self, root: Path) -> None:
        database = root / "data" / "math_notebook.db"
        skill = root / ".agents" / "skills" / "math-error-notebook" / "SKILL.md"
        image = root / "source.jpg"
        database.parent.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        skill.write_text("skill", encoding="utf-8")
        image.write_bytes(jpeg_bytes())
        pdf_root = root / "output" / "pdf"
        pdf_root.mkdir(parents=True)
        (pdf_root / "历史练习.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        (pdf_root / "历史练习-别名.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE knowledge_points(code TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE errors(id TEXT PRIMARY KEY,occurred_at TEXT NOT NULL,problem_text TEXT NOT NULL,student_answer TEXT,correct_answer TEXT,correct_solution TEXT,first_wrong_step TEXT,cause_code TEXT NOT NULL,cause_detail TEXT NOT NULL,evidence_json TEXT,difficulty REAL NOT NULL,confidence REAL NOT NULL,image_path TEXT,question_id TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,raw_analysis_json TEXT NOT NULL);
            CREATE TABLE error_knowledge(error_id TEXT NOT NULL REFERENCES errors(id),knowledge_code TEXT NOT NULL REFERENCES knowledge_points(code));
            CREATE TABLE review_schedule(id INTEGER PRIMARY KEY,error_id TEXT NOT NULL REFERENCES errors(id),cycle INTEGER NOT NULL,stage INTEGER NOT NULL,due_date TEXT NOT NULL,completed_at TEXT,result TEXT,note TEXT);
            CREATE TABLE recommendations(id TEXT PRIMARY KEY,error_id TEXT NOT NULL REFERENCES errors(id),question_id TEXT NOT NULL,rank INTEGER NOT NULL,score REAL NOT NULL,reason TEXT NOT NULL,assigned_at TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE attempts(id TEXT PRIMARY KEY,question_id TEXT NOT NULL,error_id TEXT,submitted_answer TEXT,is_correct INTEGER NOT NULL,cause_code TEXT,attempted_at TEXT NOT NULL,note TEXT);
            CREATE TABLE review_packet_items(packet_sha256 TEXT NOT NULL,packet_path TEXT NOT NULL,packet_date TEXT NOT NULL,error_id TEXT NOT NULL,cycle INTEGER NOT NULL,stage INTEGER NOT NULL,result TEXT NOT NULL,review_schedule_id INTEGER,attempt_ids_json TEXT NOT NULL,note TEXT,result_file_sha256 TEXT NOT NULL,finalized_at TEXT NOT NULL);
            INSERT INTO knowledge_points VALUES ('line-circle','直线与圆');
            """
        )
        connection.execute(
            "INSERT INTO errors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "err-1", "2026-08-01T08:00:00+08:00", "求圆的方程", "x=1", "x=2", "完整解析",
                "首步移项错误", "calculation", "移项符号错误", '["列式与照片一致"]', 2.0, 0.95,
                str(image), "q-1", "active", "2026-08-01T08:00:00+08:00",
                '{"prevention_cue":"先检查移项符号","image_path":"local-only.jpg"}',
            ),
        )
        connection.executescript(
            """
            INSERT INTO error_knowledge VALUES ('err-1','line-circle');
            INSERT INTO review_schedule VALUES (1,'err-1',1,1,'2026-08-02','2026-08-02T09:00:00+08:00','correct',NULL);
            INSERT INTO review_schedule VALUES (2,'err-1',1,2,'2026-08-04',NULL,NULL,NULL);
            INSERT INTO recommendations VALUES ('rec-1','err-1','q-2',1,0.9,'同类巩固','2026-08-01T08:00:00+08:00','assigned');
            INSERT INTO attempts VALUES ('attempt-1','q-2','err-1','x=2',1,NULL,'2026-08-02T09:00:00+08:00','复习正确');
            INSERT INTO review_packet_items VALUES ('packet-sha','packet.json','2026-08-02','err-1',1,1,'correct',1,'["attempt-1"]',NULL,'result-sha','2026-08-02T09:00:00+08:00');
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
        self.assertEqual(first["counts"], {"errors": 1, "knowledge_links": 1, "completed_reviews": 1, "recommendations": 1, "images": 1, "unique_images": 1, "attempts": 1, "review_packet_items": 1, "pdfs": 2, "unique_pdfs": 1})
        self.assertEqual(first["errors"][0]["knowledge_points"], ["直线与圆"])
        self.assertEqual(len(first["errors"][0]["reviews"]), 2)
        self.assertEqual(first["errors"][0]["image"]["media_type"], "image/jpeg")
        self.assertEqual(first["errors"][0]["attempts"][0]["id"], "attempt-1")
        self.assertEqual(first["errors"][0]["review_packet_items"][0]["packet_sha256"], "packet-sha")
        self.assertEqual([item["original_name"] for item in first["pdfs"]], ["历史练习-别名.pdf", "历史练习.pdf"])

    def test_ids_are_stable_and_scoped_to_target_user(self) -> None:
        first = migrate_error_notebook._stable_id("error", "user-a", "source-1")
        self.assertEqual(first, migrate_error_notebook._stable_id("error", "user-a", "source-1"))
        self.assertNotEqual(first, migrate_error_notebook._stable_id("error", "user-b", "source-1"))

    def test_diagnosis_preserves_cause_knowledge_and_solution(self) -> None:
        payload = json.loads(migrate_error_notebook._diagnosis({
            "id": "err-1", "cause_code": "calculation", "cause_detail": "符号错误",
            "knowledge_points": ["直线与圆"], "correct_solution": "过程", "correct_answer": "答案",
            "difficulty": 2.0, "evidence_json": "[]", "raw_analysis_json": "{}",
        }))
        self.assertEqual(payload["schema"], "math-error-diagnosis/v1")
        self.assertEqual(payload["knowledge_points"], ["直线与圆"])
        self.assertEqual((payload["cause_evidence"], payload["correct_solution"], payload["final_answer"]), ("符号错误", "过程", "答案"))

    def test_snapshot_preserves_history_without_local_image_path(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            item = migrate_error_notebook.extract(root)["errors"][0]
        payload = json.loads(migrate_error_notebook._source_snapshot(item, migrate_error_notebook._item_digest(item)))
        self.assertEqual(payload["schema"], "desktop-error-snapshot/v2")
        self.assertEqual(len(payload["review_schedule"]), 2)
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertNotIn("local_path", payload["image"])
        self.assertNotIn("image_path", payload["raw_analysis"])

    def test_account_resolution_requires_one_active_last4_match(self) -> None:
        cursor = mock.Mock()
        cursor.fetchall.return_value = [("user-2970",)]
        self.assertEqual(migrate_error_notebook._resolve_user(cursor, "2970"), "user-2970")
        cursor.fetchall.return_value = []
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            migrate_error_notebook._resolve_user(cursor, "2970")
        with self.assertRaisesRegex(ValueError, "four digits"):
            migrate_error_notebook._resolve_user(cursor, "29x0")

    def test_commit_reconciles_and_upserts_a_complete_snapshot(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.executed: list[tuple[str, tuple | None]] = []
                self.rows: list[tuple] = []
                self.rowcount = 1

            def execute(self, sql: str, params: tuple | None = None) -> None:
                self.executed.append((sql, params))
                if sql.startswith("SELECT id FROM web_users"):
                    self.rows = [("a" * 32,)]
                elif sql.startswith("SELECT COUNT(*) FROM error_notebook_entries"):
                    self.rows = [(1,)]
                else:
                    self.rows = []

            def fetchall(self) -> list[tuple]:
                return list(self.rows)

            def fetchone(self) -> tuple | None:
                return self.rows[0] if self.rows else None

            def close(self) -> None:
                pass

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()
                self.committed = False
                self.rolled_back = False

            def cursor(self) -> Cursor:
                return self.cursor_instance

            def begin(self) -> None:
                pass

            def commit(self) -> None:
                self.committed = True

            def rollback(self) -> None:
                self.rolled_back = True

            def close(self) -> None:
                pass

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            plan = migrate_error_notebook.extract(root)
            connection = Connection()
            with mock.patch.object(migrate_error_notebook, "_connection", return_value=connection):
                result = migrate_error_notebook.commit(plan, "2970", root / "target-files")
            stored = list((root / "target-files").rglob("*.jpg"))
            self.assertEqual(len(stored), 1)
            self.assertEqual(len(list((root / "target-files").rglob("*.pdf"))), 1)
        sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertEqual((result["inserted_errors"], result["ready_images"], result["synchronized_completed_reviews"]), (1, 1, 1))
        self.assertEqual((result["ready_pdfs"], result["unique_ready_pdfs"]), (2, 1))
        self.assertIn("DELETE FROM recommendations", sql)
        self.assertIn("'practice_pdf','file'", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("INSERT IGNORE", sql)


if __name__ == "__main__":
    unittest.main()
