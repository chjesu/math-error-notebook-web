from __future__ import annotations

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

from scripts import migrate_question_bank


QUESTION = {
    "id": "desktop-q-1",
    "stem": "若 x+1=2，求 x。",
    "options": None,
    "answer": "x=1",
    "solution": "移项得 x=1。",
    "grade": 10,
    "difficulty": 1.0,
    "source_name": "用户授权试卷",
    "source_url": "local://exam",
    "license": "user-owned",
    "verified": 1,
    "fingerprint": "f" * 64,
}


class QuestionBankMigrationTests(unittest.TestCase):
    def test_current_project_skill_layout_is_supported(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / ".agents" / "skills" / "math-error-notebook" / "scripts" / "notebook.py"
            database = root / "data" / "math_notebook.db"
            script.parent.mkdir(parents=True)
            database.parent.mkdir(parents=True)
            script.touch()
            database.touch()
            result = subprocess.CompletedProcess([], 0, stdout='{"status":"ok"}', stderr="")
            with mock.patch.object(migrate_question_bank.subprocess, "run", return_value=result) as run:
                self.assertEqual(migrate_question_bank._run_notebook(root, "bank-info"), {"status": "ok"})
            command = run.call_args.args[0]
            self.assertEqual((Path(command[4]), Path(command[6])), (script, database))

    def test_mapping_is_stable_and_preserves_verified_only_with_rights(self) -> None:
        authorized = migrate_question_bank.map_question(QUESTION, {"rights_confirmed": 1})
        repeated = migrate_question_bank.map_question(dict(QUESTION), {"rights_confirmed": 1})
        self.assertEqual(authorized, repeated)
        self.assertEqual((authorized["license_status"], authorized["status"]), ("user_authorized", "verified"))
        restricted = migrate_question_bank.map_question(QUESTION, {"rights_confirmed": 0})
        self.assertEqual((restricted["license_status"], restricted["status"]), ("restricted", "candidate"))
        self.assertIsNone(restricted["verification_sha256"])

    def test_extract_uses_notebook_cli_and_is_reproducible(self) -> None:
        responses = {
            "bank-info": {"status": "ok", "integrity": True, "canonical_path": "desktop.db"},
            "sources": [{"name": "用户授权试卷", "rights_confirmed": 1}],
            "search": [QUESTION],
        }

        def fake_run(source_root, command, *args):
            del source_root, args
            return responses[command]

        with mock.patch.object(migrate_question_bank, "_run_notebook", side_effect=fake_run):
            first = migrate_question_bank.extract(Path("source"))
            second = migrate_question_bank.extract(Path("source"))
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((first["count"], first["verified"]), (1, 1))

    def test_missing_provenance_fails_closed(self) -> None:
        broken = dict(QUESTION)
        broken["license"] = ""
        with self.assertRaisesRegex(ValueError, "required"):
            migrate_question_bank.map_question(broken, {})

    def test_commit_refreshes_existing_rows_and_uses_actual_version_id(self) -> None:
        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, args):
                self.calls.append((sql, args))

            def fetchone(self):
                return ("existing-version-id",)

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

            def begin(self):
                pass

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        connection = Connection()
        item = migrate_question_bank.map_question(QUESTION, {"rights_confirmed": 1})
        with mock.patch.object(migrate_question_bank, "_connection", return_value=connection):
            migrate_question_bank.commit({"questions": [item]})
        statements = [sql for sql, _ in connection.cursor_instance.calls]
        self.assertTrue(all("ON DUPLICATE KEY UPDATE id=id" not in sql for sql in statements))
        verification = next(args for sql, args in connection.cursor_instance.calls if sql.startswith("INSERT INTO question_verifications"))
        self.assertEqual(verification[1], "existing-version-id")

    def test_connection_falls_back_to_local_environment_without_exporting_secrets(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("scripts.local_env._connection_factory", return_value=lambda: "local-connection"):
            self.assertEqual(migrate_question_bank._connection(), "local-connection")

    def test_grade_candidate_schema_has_unclear_and_first_error_gates(self) -> None:
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "grade-candidate.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["verdict"]["enum"]), {"correct", "partial", "incorrect", "unclear"})
        self.assertEqual(len(schema["allOf"]), 2)


if __name__ == "__main__":
    unittest.main()
