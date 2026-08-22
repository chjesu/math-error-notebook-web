from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_review_packet", ROOT / "scripts/prepare_review_packet.py"
)
packet = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(packet)


class PrepareReviewPacketTests(unittest.TestCase):
    def test_builds_hashed_explicit_packet(self) -> None:
        value = packet.build("web-requirements", [Path("PROJECT_ARCHITECTURE.md")])
        self.assertEqual(value["schema"], "web-review-packet/v1")
        self.assertEqual(value["files"][0]["path"], "PROJECT_ARCHITECTURE.md")
        self.assertEqual(len(value["files"][0]["sha256"]), 64)

    def test_rejects_file_outside_project(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".md") as external:
            with self.assertRaisesRegex(ValueError, "outside"):
                packet.build("web-requirements", [Path(external.name)])


if __name__ == "__main__":
    unittest.main()
