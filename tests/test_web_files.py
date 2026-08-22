from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import zipfile

from services.web_files import FileIntake


class FileIntakeTests(unittest.TestCase):
    def test_valid_file_is_user_scoped_and_object_key_is_unpredictable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            content = b"\x89PNG\r\n\x1a\nimage"
            first = intake.quarantine(user_id="a" * 32, original_name="q.png", content=content)
            second = intake.quarantine(user_id="b" * 32, original_name="q.png", content=content)
            self.assertEqual(first.content_sha256, second.content_sha256)
            self.assertNotEqual(first.object_key, second.object_key)
            self.assertNotIn(first.content_sha256, first.object_key)
            self.assertTrue(first.local_path.is_file())

    def test_rejects_path_traversal_spoofed_extension_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory), max_bytes=20)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                intake.quarantine(user_id="a" * 32, original_name="../q.png", content=b"\x89PNG\r\n\x1a\n")
            with self.assertRaisesRegex(ValueError, "does not match"):
                intake.quarantine(user_id="a" * 32, original_name="q.pdf", content=b"\x89PNG\r\n\x1a\n")
            with self.assertRaisesRegex(ValueError, "size limit"):
                intake.quarantine(user_id="a" * 32, original_name="q.png", content=b"\x89PNG\r\n\x1a\n" + b"x" * 20)

    def test_rejects_broken_docx_and_invalid_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            with self.assertRaisesRegex(ValueError, "invalid DOCX"):
                intake.quarantine(user_id="a" * 32, original_name="q.docx", content=b"PKbroken")
            with self.assertRaisesRegex(ValueError, "user_id"):
                intake.quarantine(user_id="client-choice", original_name="q.png", content=b"\x89PNG\r\n\x1a\n")

    def test_accepts_minimal_docx_without_extracting_entries(self) -> None:
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        with tempfile.TemporaryDirectory() as directory:
            result = FileIntake(Path(directory)).quarantine(user_id="a" * 32, original_name="q.docx", content=stream.getvalue())
            self.assertEqual(result.media_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


if __name__ == "__main__":
    unittest.main()
