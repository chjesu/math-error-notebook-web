from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import hashlib
import tempfile
import unittest

from services.web_files import (
    AliyunOssStorageAdapter,
    LocalFsStorageAdapter,
    OssStorageConfig,
    build_storage_adapter,
)


class FakeServiceError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class FakeRequest:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def iter_bytes(self, **_: object):
        for offset in range(0, len(self.content), 3):
            yield self.content[offset:offset + 3]

    def close(self) -> None:
        self.closed = True


class FakeOssSdk:
    PutObjectRequest = FakeRequest
    GetObjectRequest = FakeRequest
    HeadObjectRequest = FakeRequest
    DeleteObjectRequest = FakeRequest


class FakeOssClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.last_put: FakeRequest | None = None
        self.last_presign: FakeRequest | None = None
        self.presign_host = "student-files.oss-cn-beijing.aliyuncs.com"

    def put_object(self, request: FakeRequest) -> object:
        self.last_put = request
        key = (request.bucket, request.key)
        if key in self.objects and request.forbid_overwrite:
            raise FakeServiceError(409, "FileAlreadyExists")
        body = request.body.read() if hasattr(request.body, "read") else bytes(request.body)
        self.objects[key] = {
            "content": body,
            "content_type": request.content_type,
            "metadata": dict(request.metadata),
        }
        return SimpleNamespace(status_code=200)

    def head_object(self, request: FakeRequest) -> object:
        item = self.objects.get((request.bucket, request.key))
        if item is None:
            raise FakeServiceError(404, "NoSuchKey")
        return SimpleNamespace(
            content_length=len(item["content"]),
            content_type=item["content_type"],
            metadata=dict(item["metadata"]),
        )

    def get_object(self, request: FakeRequest) -> object:
        item = self.objects.get((request.bucket, request.key))
        if item is None:
            raise FakeServiceError(404, "NoSuchKey")
        return SimpleNamespace(body=FakeBody(item["content"]))

    def delete_object(self, request: FakeRequest) -> object:
        self.objects.pop((request.bucket, request.key), None)
        return SimpleNamespace(status_code=204)

    def presign(self, request: FakeRequest, **_: object) -> object:
        self.last_presign = request
        return SimpleNamespace(
            method="PUT" if hasattr(request, "body") else "GET",
            url=f"https://{self.presign_host}/{request.key}?x-oss-signature=test",
            expiration=datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc),
            signed_headers={"Content-Type": request.content_type}
            if getattr(request, "content_type", None)
            else {},
        )


def oss_config(**changes: object) -> OssStorageConfig:
    values: dict[str, object] = {
        "region": "cn-beijing",
        "bucket": "student-files",
        "endpoint": "https://oss-cn-beijing.aliyuncs.com",
        "key_prefix": "math-notebook",
    }
    values.update(changes)
    return OssStorageConfig(**values)


class AliyunOssStorageAdapterTests(unittest.TestCase):
    def test_put_read_delete_is_private_immutable_and_prefixed(self) -> None:
        client = FakeOssClient()
        storage = AliyunOssStorageAdapter(oss_config(), client=client, sdk=FakeOssSdk)
        content = b"practice-pdf"

        self.assertTrue(storage.save_bytes("quarantine/user/file.pdf", content, "application/pdf"))
        request = client.last_put
        assert request is not None
        self.assertEqual(request.key, "math-notebook/quarantine/user/file.pdf")
        self.assertEqual(request.acl, "private")
        self.assertTrue(request.forbid_overwrite)
        self.assertEqual(request.metadata["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(storage.read_bytes("quarantine/user/file.pdf"), content)

        storage.delete_path("quarantine/user/file.pdf")
        with self.assertRaises(LookupError):
            storage.read_bytes("quarantine/user/file.pdf")

    def test_same_object_is_idempotent_but_collision_is_rejected(self) -> None:
        storage = AliyunOssStorageAdapter(oss_config(), client=FakeOssClient(), sdk=FakeOssSdk)

        self.assertTrue(storage.save_bytes("quarantine/user/file.pdf", b"same", "application/pdf"))
        self.assertFalse(storage.save_bytes("quarantine/user/file.pdf", b"same", "application/pdf"))
        with self.assertRaisesRegex(RuntimeError, "collision"):
            storage.save_bytes("quarantine/user/file.pdf", b"different", "application/pdf")

    def test_presigned_requests_are_short_lived_and_host_restricted(self) -> None:
        client = FakeOssClient()
        storage = AliyunOssStorageAdapter(oss_config(), client=client, sdk=FakeOssSdk)

        download = storage.presign_download(
            "quarantine/user/file.pdf", expires_in=300, download_name="练习卷.pdf"
        )
        upload = storage.presign_upload(
            "quarantine/user/new.png",
            content_type="image/png",
            content_length=128,
            content_sha256="a" * 64,
            expires_in=300,
        )

        self.assertEqual((download.method, upload.method), ("GET", "PUT"))
        self.assertTrue(download.url.startswith("https://student-files.oss-cn-beijing.aliyuncs.com/"))
        self.assertEqual(upload.headers["Content-Type"], "image/png")
        with self.assertRaises(ValueError):
            storage.presign_download("quarantine/user/file.pdf", expires_in=901)

        client.presign_host = "metadata.internal"
        with self.assertRaisesRegex(RuntimeError, "unsafe presigned URL"):
            storage.presign_download("quarantine/user/file.pdf")

    def test_rejects_unsafe_endpoint_and_unbounded_read(self) -> None:
        with self.assertRaisesRegex(ValueError, "endpoint"):
            oss_config(endpoint="http://127.0.0.1:9000")

        client = FakeOssClient()
        storage = AliyunOssStorageAdapter(
            oss_config(max_read_bytes=3), client=client, sdk=FakeOssSdk
        )
        client.objects[("student-files", "math-notebook/quarantine/user/file.pdf")] = {
            "content": b"four",
            "content_type": "application/pdf",
            "metadata": {"sha256": hashlib.sha256(b"four").hexdigest()},
        }
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            storage.read_bytes("quarantine/user/file.pdf")

    def test_read_fails_closed_when_required_integrity_metadata_is_missing(self) -> None:
        client = FakeOssClient()
        storage = AliyunOssStorageAdapter(oss_config(), client=client, sdk=FakeOssSdk)
        client.objects[("student-files", "math-notebook/quarantine/user/file.pdf")] = {
            "content": b"content",
            "content_type": "application/pdf",
            "metadata": {},
        }

        with self.assertRaisesRegex(RuntimeError, "integrity"):
            storage.read_bytes("quarantine/user/file.pdf")


class StorageFactoryTests(unittest.TestCase):
    def test_local_is_explicit_and_oss_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = build_storage_adapter(Path(directory), environ={"STORAGE_PROVIDER": "local"})
            self.assertIsInstance(local, LocalFsStorageAdapter)

            with self.assertRaisesRegex(RuntimeError, "OSS_REGION"):
                build_storage_adapter(Path(directory), environ={"STORAGE_PROVIDER": "oss"})
            with self.assertRaisesRegex(RuntimeError, "STORAGE_PROVIDER"):
                build_storage_adapter(Path(directory), environ={"STORAGE_PROVIDER": "unknown"})

    def test_oss_factory_uses_complete_explicit_configuration(self) -> None:
        client = FakeOssClient()
        with tempfile.TemporaryDirectory() as directory:
            storage = build_storage_adapter(
                Path(directory),
                environ={
                    "STORAGE_PROVIDER": "oss",
                    "OSS_REGION": "cn-beijing",
                    "OSS_BUCKET": "student-files",
                    "OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
                    "OSS_KEY_PREFIX": "math-notebook",
                },
                oss_client=client,
                oss_sdk=FakeOssSdk,
            )

        self.assertIsInstance(storage, AliyunOssStorageAdapter)
        self.assertIs(storage.client, client)


if __name__ == "__main__":
    unittest.main()
