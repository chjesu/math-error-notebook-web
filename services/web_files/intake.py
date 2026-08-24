"""Validate and quarantine uploaded study files without trusting names or MIME headers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import re
import secrets
from typing import Iterator
import zipfile

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class FileCandidate:
    user_id: str
    original_name: str
    media_type: str
    byte_size: int
    content_sha256: str
    object_key: str
    local_path: Path


class FileIntake:
    def __init__(self, root: Path, max_bytes: int = 25 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes

    @staticmethod
    def _name(value: str) -> str:
        if (
            not value
            or len(value) > 200
            or Path(value).name != value
            or any(ord(character) < 32 for character in value)
            or not re.fullmatch(r"[^/\\]+", value)
        ):
            raise ValueError("unsafe file name")
        return value

    @staticmethod
    def _kind(name: str, content: bytes) -> tuple[str, str]:
        extension = Path(name).suffix.lower()
        if content.startswith(b"%PDF-") and extension == ".pdf":
            return "application/pdf", "pdf"
        if content.startswith(b"\x89PNG\r\n\x1a\n") and extension == ".png":
            return "image/png", "png"
        if content.startswith(b"\xff\xd8\xff") and extension in {".jpg", ".jpeg"}:
            return "image/jpeg", "jpg"
        if extension == ".docx" and content.startswith(b"PK"):
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    entries = archive.infolist()
                    names = {entry.filename for entry in entries}
                    if len(entries) > 5_000 or sum(entry.file_size for entry in entries) > 100 * 1024 * 1024:
                        raise ValueError("DOCX archive is too large")
                    if "[Content_Types].xml" in names and "word/document.xml" in names:
                        return (
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "docx",
                        )
            except zipfile.BadZipFile as exc:
                raise ValueError("invalid DOCX archive") from exc
        raise ValueError("file content does not match an allowed PDF, PNG, JPEG or DOCX type")

    def quarantine(self, *, user_id: str, original_name: str, content: bytes) -> FileCandidate:
        if not re.fullmatch(r"[0-9a-f]{32}", user_id):
            raise ValueError("invalid user_id")
        name = self._name(original_name)
        if not content or len(content) > self.max_bytes:
            raise ValueError("file is empty or exceeds the size limit")
        media_type, suffix = self._kind(name, content)
        digest = hashlib.sha256(content).hexdigest()
        user_namespace = hashlib.sha256(user_id.encode("ascii")).hexdigest()[:16]
        object_key = f"quarantine/{user_namespace}/{secrets.token_hex(16)}.{suffix}"
        target = (self.root / object_key).resolve()
        if self.root not in target.parents:
            raise ValueError("unsafe quarantine path")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("quarantine hash collision")
        return FileCandidate(user_id, name, media_type, len(content), digest, object_key, target)

    def read(self, object_key: str) -> bytes:
        return self.resolve(object_key).read_bytes()

    def resolve(self, object_key: str) -> Path:
        target = (self.root / object_key).resolve()
        if self.root not in target.parents or not target.is_file():
            raise LookupError("file not found")
        return target

    @contextmanager
    def model_preview(self, object_key: str, expected_sha256: str) -> Iterator[Path]:
        """Yield a bounded, metadata-free image made from the verified uploaded bytes."""
        raw = self.read(object_key)
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise RuntimeError("file_integrity_failed")

        preview_root = (self.root / "model-previews").resolve()
        if self.root not in preview_root.parents:
            raise ValueError("unsafe preview path")
        preview_root.mkdir(parents=True, exist_ok=True)
        preview = (preview_root / f"{secrets.token_hex(16)}.png").resolve()
        try:
            with Image.open(BytesIO(raw)) as source:
                width, height = source.size
                if width < 1 or height < 1 or width > 10_000 or height > 10_000 or width * height > 40_000_000:
                    raise ValueError("image dimensions exceed the model preview limit")
                source.load()
                rendered = ImageOps.exif_transpose(source).convert("RGB")
                rendered.thumbnail((4096, 4096))
                rendered.save(preview, format="PNG", optimize=True)
            yield preview
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise ValueError("invalid image for model processing") from exc
        finally:
            preview.unlink(missing_ok=True)
