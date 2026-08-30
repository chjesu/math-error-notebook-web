from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from PIL import Image

from services.web_files import FileIntake, LocalFsStorageAdapter
from services.web_domain import InMemoryNotebookStore, NotebookService
from tests.image_fixtures import png_bytes


class FalsyLocalFsStorageAdapter(LocalFsStorageAdapter):
    def __bool__(self) -> bool:
        return False


class LocalFsStorageAdapterTests(unittest.TestCase):
    def test_saves_reads_and_deletes_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            storage.save_bytes("quarantine/user/object.png", b"content", "image/png")

            self.assertEqual(storage.read_bytes("quarantine/user/object.png"), b"content")
            self.assertTrue(storage.resolve("quarantine/user/object.png").is_file())

            storage.delete_path("quarantine/user/object.png")
            with self.assertRaises(LookupError):
                storage.read_bytes("quarantine/user/object.png")

    def test_rejects_unsafe_object_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            for object_key in (
                "../outside", "/absolute", "folder\\escape", "folder/../escape", "C:/escape",
                "quarantine/user/NUL", "quarantine/user/con.txt", "quarantine/user/COM1.log",
                "quarantine/user/trailing.",
            ):
                with self.subTest(object_key=object_key), self.assertRaises(ValueError):
                    storage.save_bytes(object_key, b"content", "application/octet-stream")

    def test_same_key_is_idempotent_but_different_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            self.assertTrue(storage.save_bytes("quarantine/user/object.png", b"content", "image/png"))
            self.assertFalse(storage.save_bytes("quarantine/user/object.png", b"content", "image/png"))

            with self.assertRaisesRegex(RuntimeError, "collision"):
                storage.save_bytes("quarantine/user/object.png", b"replacement", "image/png")


class FileIntakeTests(unittest.TestCase):
    def test_quarantine_reencodes_jpeg_orientation_and_removes_metadata(self) -> None:
        stream = BytesIO()
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[0x010F] = "test-camera"
        Image.new("RGB", (20, 10), "white").save(stream, format="JPEG", exif=exif)
        original = stream.getvalue()

        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            candidate = intake.quarantine(
                user_id="a" * 32, original_name="q.jpg", content=original
            )
            stored = intake.read(candidate.object_key)

            self.assertNotEqual(stored, original)
            self.assertEqual(candidate.content_sha256, hashlib.sha256(stored).hexdigest())
            self.assertEqual(candidate.byte_size, len(stored))
            with Image.open(BytesIO(stored)) as image:
                self.assertEqual((image.format, image.size), ("JPEG", (10, 20)))
                self.assertEqual(len(image.getexif()), 0)
                self.assertNotIn("icc_profile", image.info)

    def test_rejects_magic_only_png_that_cannot_be_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            with self.assertRaisesRegex(ValueError, "invalid image"):
                intake.quarantine(
                    user_id="a" * 32,
                    original_name="q.png",
                    content=b"\x89PNG\r\n\x1a\nimage",
                )

    def test_can_store_validated_files_in_a_separate_local_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as objects:
            storage = LocalFsStorageAdapter(Path(objects))
            intake = FileIntake(Path(scratch), storage=storage)
            content = png_bytes()

            candidate = intake.quarantine(user_id="a" * 32, original_name="q.png", content=content)

            stored = storage.read_bytes(candidate.object_key)
            self.assertEqual(intake.read(candidate.object_key), stored)
            self.assertEqual(candidate.content_sha256, hashlib.sha256(stored).hexdigest())
            self.assertTrue(candidate.local_path.is_relative_to(Path(objects)))

    def test_notebook_service_uses_injected_storage_for_upload_and_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as objects:
            storage = FalsyLocalFsStorageAdapter(Path(objects))
            store = InMemoryNotebookStore()
            notebook = NotebookService(store, Path(scratch), storage=storage)
            user_id = "a" * 32

            record = notebook.upload(
                user_id=user_id,
                purpose="question_image",
                original_name="q.png",
                content=png_bytes(),
            )

            self.assertTrue(storage.resolve(record.object_key).is_relative_to(Path(objects)))
            notebook.prepare_user_deletion(user_id=user_id)
            notebook.complete_user_deletion(user_id=user_id)
            with self.assertRaises(LookupError):
                storage.read_bytes(record.object_key)

    def test_metadata_failure_does_not_delete_an_idempotently_existing_object(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as objects:
            storage = LocalFsStorageAdapter(Path(objects))
            store = InMemoryNotebookStore()
            notebook = NotebookService(store, Path(scratch), storage=storage)
            user_id, job_id, content = "a" * 32, "b" * 32, b"{}"
            namespace = hashlib.sha256(user_id.encode("ascii")).hexdigest()[:16]
            object_key = f"quarantine/{namespace}/export-{job_id}.json"
            storage.save_bytes(object_key, content, "application/json")

            with mock.patch.object(store, "create_file", side_effect=OSError("metadata unavailable")):
                with self.assertRaisesRegex(OSError, "metadata unavailable"):
                    notebook._store_export(user_id=user_id, job_id=job_id, content=content)

            self.assertEqual(storage.read_bytes(object_key), content)

    def test_valid_file_is_user_scoped_and_object_key_is_unpredictable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            content = png_bytes()
            first = intake.quarantine(user_id="a" * 32, original_name="q.png", content=content)
            second = intake.quarantine(user_id="b" * 32, original_name="q.png", content=content)
            self.assertEqual(first.content_sha256, second.content_sha256)
            self.assertNotEqual(first.object_key, second.object_key)
            self.assertNotIn(first.content_sha256, first.object_key)
            self.assertTrue(first.local_path.is_file())
            self.assertEqual(intake.resolve(first.object_key), first.local_path)
            self.assertEqual(
                hashlib.sha256(intake.read(first.object_key)).hexdigest(),
                first.content_sha256,
            )

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

    def test_model_preview_rechecks_hash_and_removes_image_metadata(self) -> None:
        stream = BytesIO()
        exif = Image.Exif()
        exif[0x010F] = "test-camera"
        Image.new("RGB", (20, 10), "white").save(stream, format="JPEG", exif=exif)
        with tempfile.TemporaryDirectory() as directory:
            intake = FileIntake(Path(directory))
            candidate = intake.quarantine(user_id="a" * 32, original_name="q.jpg", content=stream.getvalue())
            with intake.model_preview(candidate.object_key, candidate.content_sha256) as preview:
                self.assertNotEqual(preview, candidate.local_path)
                self.assertEqual(preview.suffix, ".png")
                with Image.open(preview) as image:
                    self.assertEqual((image.format, image.size, len(image.getexif())), ("PNG", (20, 10), 0))
            self.assertFalse(preview.exists())

            candidate.local_path.write_bytes(b"replaced")
            with self.assertRaisesRegex(RuntimeError, "file_integrity_failed"):
                with intake.model_preview(candidate.object_key, candidate.content_sha256):
                    pass


if __name__ == "__main__":
    unittest.main()
