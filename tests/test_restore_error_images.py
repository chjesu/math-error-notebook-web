from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.restore_error_images import prepare_manifest
from services.web_files import LocalFsStorageAdapter


class RestoreErrorImagesTests(unittest.TestCase):
    def test_existing_content_addressed_asset_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            content = b"verified-image-bytes"
            digest = hashlib.sha256(content).hexdigest()
            reference = f"bank-assets/{digest}.png"
            storage.save_bytes(reference, content, "image/png")
            user_id, entries = prepare_manifest({
                "user_id": "a" * 32,
                "entries": [{"error_id": "b" * 32, "existing_refs": [reference]}],
            }, storage)
            self.assertEqual(user_id, "a" * 32)
            self.assertEqual(entries[0]["references"], [reference])
            self.assertEqual(entries[0]["assets"], {})

    def test_existing_asset_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            reference = f"bank-assets/{'c' * 64}.jpg"
            storage.save_bytes(reference, b"different", "image/jpeg")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                prepare_manifest({
                    "user_id": "a" * 32,
                    "entries": [{"error_id": "b" * 32, "existing_refs": [reference]}],
                }, storage)

    def test_duplicate_error_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFsStorageAdapter(Path(directory))
            reference = f"bank-assets/{hashlib.sha256(b'x').hexdigest()}.png"
            storage.save_bytes(reference, b"x", "image/png")
            item = {"error_id": "b" * 32, "existing_refs": [reference]}
            with self.assertRaisesRegex(ValueError, "unique"):
                prepare_manifest({"user_id": "a" * 32, "entries": [item, item]}, storage)


if __name__ == "__main__":
    unittest.main()
