from __future__ import annotations

import json
from pathlib import Path
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

    def test_grade_candidate_schema_has_unclear_and_first_error_gates(self) -> None:
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "grade-candidate.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["verdict"]["enum"]), {"correct", "partial", "incorrect", "unclear"})
        self.assertEqual(len(schema["allOf"]), 2)


if __name__ == "__main__":
    unittest.main()
