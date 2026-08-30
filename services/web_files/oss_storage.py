"""Fail-closed Alibaba Cloud OSS storage backed by the official Python SDK V2."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import md5, sha256
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit
import re

from .storage import LocalFsStorageAdapter, PresignedStorageRequest, StorageAdapter, validate_object_key


_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_REGION_RE = re.compile(r"[a-z][a-z0-9-]{1,31}[a-z0-9]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTENT_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")
_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchObject", "NotFound"}
_EXISTS_CODES = {"FileAlreadyExists", "ObjectAlreadyExists"}


@dataclass(frozen=True)
class OssStorageConfig:
    region: str
    bucket: str
    endpoint: str
    key_prefix: str = "math-notebook"
    max_read_bytes: int = 64 * 1024 * 1024
    max_presign_seconds: int = 900

    def __post_init__(self) -> None:
        if not _REGION_RE.fullmatch(self.region):
            raise ValueError("invalid OSS region")
        if not _BUCKET_RE.fullmatch(self.bucket):
            raise ValueError("invalid OSS bucket")
        parsed = urlsplit(self.endpoint)
        allowed_hosts = {
            f"oss-{self.region}.aliyuncs.com",
            f"oss-{self.region}-internal.aliyuncs.com",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid OSS endpoint")
        if not self.key_prefix or self.key_prefix.startswith("/") or self.key_prefix.endswith("/"):
            raise ValueError("invalid OSS key prefix")
        validate_object_key(f"{self.key_prefix}/probe")
        if not 1 <= self.max_read_bytes <= 300 * 1024 * 1024:
            raise ValueError("invalid OSS read size limit")
        if not 1 <= self.max_presign_seconds <= 900:
            raise ValueError("invalid OSS presign limit")


class AliyunOssStorageAdapter:
    """Private immutable object storage with bounded reads and short-lived URLs."""

    def __init__(self, config: OssStorageConfig, *, client: Any, sdk: Any) -> None:
        self.config = config
        self.client = client
        self.sdk = sdk
        self._endpoint_host = urlsplit(config.endpoint).hostname

    def _key(self, object_key: str) -> str:
        key = validate_object_key(object_key)
        full_key = f"{self.config.key_prefix}/{key.as_posix()}"
        validate_object_key(full_key)
        return full_key

    @staticmethod
    def _content_type(value: str) -> str:
        if not isinstance(value, str) or len(value) > 80 or not _CONTENT_TYPE_RE.fullmatch(value):
            raise ValueError("invalid content type")
        return value

    @staticmethod
    def _status(error: Exception) -> tuple[int, str]:
        status = getattr(error, "status_code", 0)
        code = getattr(error, "code", "")
        return (status if isinstance(status, int) else 0, code if isinstance(code, str) else "")

    @classmethod
    def _is_not_found(cls, error: Exception) -> bool:
        status, code = cls._status(error)
        return status == 404 or code in _NOT_FOUND_CODES

    @classmethod
    def _is_exists(cls, error: Exception) -> bool:
        status, code = cls._status(error)
        return status == 409 or code in _EXISTS_CODES

    def _head(self, full_key: str) -> Any | None:
        try:
            return self.client.head_object(
                self.sdk.HeadObjectRequest(bucket=self.config.bucket, key=full_key)
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise RuntimeError("object storage unavailable") from exc

    def save_bytes(self, object_key: str, content: bytes, content_type: str) -> bool:
        if not isinstance(content, bytes) or not content:
            raise ValueError("invalid stored object")
        if len(content) > self.config.max_read_bytes:
            raise ValueError("stored object exceeds the size limit")
        media_type = self._content_type(content_type)
        full_key = self._key(object_key)
        digest = sha256(content).hexdigest()
        content_md5 = b64encode(md5(content, usedforsecurity=False).digest()).decode("ascii")
        request = self.sdk.PutObjectRequest(
            bucket=self.config.bucket,
            key=full_key,
            acl="private",
            body=BytesIO(content),
            cache_control="private, no-store",
            content_length=len(content),
            content_md5=content_md5,
            content_type=media_type,
            forbid_overwrite=True,
            metadata={"sha256": digest},
        )
        try:
            self.client.put_object(request)
            return True
        except Exception as exc:
            if not self._is_exists(exc):
                raise RuntimeError("object storage write failed") from exc
        existing = self._head(full_key)
        metadata = getattr(existing, "metadata", {}) if existing is not None else {}
        existing_digest = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        existing_size = getattr(existing, "content_length", None) if existing is not None else None
        if existing_digest == digest and existing_size == len(content):
            return False
        raise RuntimeError("storage object collision")

    def read_bytes(self, object_key: str) -> bytes:
        full_key = self._key(object_key)
        head = self._head(full_key)
        if head is None:
            raise LookupError("file not found")
        size = getattr(head, "content_length", None)
        if not isinstance(size, int) or size < 0 or size > self.config.max_read_bytes:
            raise RuntimeError("stored object exceeds the size limit")
        try:
            result = self.client.get_object(
                self.sdk.GetObjectRequest(bucket=self.config.bucket, key=full_key)
            )
            body = getattr(result, "body", None)
            if body is None or not callable(getattr(body, "iter_bytes", None)):
                raise RuntimeError("object storage returned an invalid body")
            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in body.iter_bytes():
                    if not isinstance(chunk, bytes):
                        raise RuntimeError("object storage returned an invalid body")
                    total += len(chunk)
                    if total > self.config.max_read_bytes:
                        raise RuntimeError("stored object exceeds the size limit")
                    chunks.append(chunk)
            finally:
                body.close()
        except RuntimeError:
            raise
        except Exception as exc:
            if self._is_not_found(exc):
                raise LookupError("file not found") from exc
            raise RuntimeError("object storage read failed") from exc
        content = b"".join(chunks)
        metadata = getattr(head, "metadata", {})
        expected = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if (
            len(content) != size
            or not isinstance(expected, str)
            or not _SHA256_RE.fullmatch(expected)
            or sha256(content).hexdigest() != expected
        ):
            raise RuntimeError("object storage integrity check failed")
        return content

    def delete_path(self, object_key: str) -> None:
        full_key = self._key(object_key)
        try:
            self.client.delete_object(
                self.sdk.DeleteObjectRequest(bucket=self.config.bucket, key=full_key)
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return
            raise RuntimeError("object storage delete failed") from exc

    def _ttl(self, expires_in: int) -> timedelta:
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= self.config.max_presign_seconds
        ):
            raise ValueError("invalid presign expiration")
        return timedelta(seconds=expires_in)

    @staticmethod
    def _download_disposition(name: str) -> str:
        if not isinstance(name, str) or not name or len(name) > 200 or "\r" in name or "\n" in name:
            raise ValueError("invalid download name")
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "download"
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"

    def _presigned(self, result: Any, *, method: str, full_key: str) -> PresignedStorageRequest:
        url = getattr(result, "url", None)
        actual_method = getattr(result, "method", None)
        expiration = getattr(result, "expiration", None)
        headers = getattr(result, "signed_headers", {})
        if not isinstance(url, str) or actual_method != method or not isinstance(expiration, datetime):
            raise RuntimeError("object storage returned an invalid presigned request")
        parsed = urlsplit(url)
        allowed_host = f"{self.config.bucket}.{self._endpoint_host}"
        if (
            parsed.scheme != "https"
            or parsed.hostname != allowed_host
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.query
            or unquote(parsed.path.lstrip("/")) != full_key
        ):
            raise RuntimeError("unsafe presigned URL")
        if not isinstance(headers, Mapping):
            raise RuntimeError("object storage returned invalid signed headers")
        clean_headers: dict[str, str] = {}
        for key, value in headers.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or "\r" in key + value
                or "\n" in key + value
            ):
                raise RuntimeError("object storage returned invalid signed headers")
            clean_headers[key] = value
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        return PresignedStorageRequest(
            method=method,
            url=url,
            headers=MappingProxyType(clean_headers),
            expires_at=expiration,
        )

    def presign_download(
        self,
        object_key: str,
        *,
        expires_in: int = 300,
        download_name: str | None = None,
    ) -> PresignedStorageRequest:
        full_key = self._key(object_key)
        values: dict[str, Any] = {"bucket": self.config.bucket, "key": full_key}
        if download_name is not None:
            values["response_content_disposition"] = self._download_disposition(download_name)
        result = self.client.presign(
            self.sdk.GetObjectRequest(**values), expires=self._ttl(expires_in)
        )
        return self._presigned(result, method="GET", full_key=full_key)

    def presign_upload(
        self,
        object_key: str,
        *,
        content_type: str,
        content_length: int,
        content_sha256: str,
        expires_in: int = 300,
    ) -> PresignedStorageRequest:
        if (
            not isinstance(content_length, int)
            or isinstance(content_length, bool)
            or not 1 <= content_length <= self.config.max_read_bytes
        ):
            raise ValueError("invalid upload size")
        if not isinstance(content_sha256, str) or not _SHA256_RE.fullmatch(content_sha256):
            raise ValueError("invalid upload digest")
        full_key = self._key(object_key)
        request = self.sdk.PutObjectRequest(
            bucket=self.config.bucket,
            key=full_key,
            acl="private",
            body=None,
            cache_control="private, no-store",
            content_length=content_length,
            content_type=self._content_type(content_type),
            forbid_overwrite=True,
            metadata={"sha256": content_sha256},
        )
        result = self.client.presign(request, expires=self._ttl(expires_in))
        return self._presigned(result, method="PUT", full_key=full_key)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for OSS storage")
    return value


def _build_oss_client(sdk: Any, config: OssStorageConfig, environ: Mapping[str, str]) -> Any:
    credential_mode = _required(environ, "OSS_CREDENTIAL_MODE").lower()
    sdk_config = sdk.config.load_default()
    sdk_config.region = config.region
    sdk_config.endpoint = config.endpoint
    if credential_mode == "environment":
        _required(environ, "OSS_ACCESS_KEY_ID")
        _required(environ, "OSS_ACCESS_KEY_SECRET")
        sdk_config.credentials_provider = sdk.credentials.EnvironmentVariableCredentialsProvider()
    elif credential_mode == "ecs_ram_role":
        try:
            from alibabacloud_credentials.client import Client as CredentialClient
            from alibabacloud_credentials.models import Config as CredentialConfig
        except ImportError as exc:
            raise RuntimeError("ECS RAM Role credentials dependency is unavailable") from exc
        role_name = environ.get("OSS_ECS_ROLE_NAME", "").strip()
        credential_values: dict[str, str] = {"type": "ecs_ram_role"}
        if role_name:
            credential_values["role_name"] = role_name
        credential_client = CredentialClient(CredentialConfig(**credential_values))

        def credentials() -> Any:
            value = credential_client.get_credential()
            return sdk.credentials.Credentials(
                access_key_id=value.access_key_id,
                access_key_secret=value.access_key_secret,
                security_token=value.security_token,
            )

        sdk_config.credentials_provider = sdk.credentials.CredentialsProviderFunc(func=credentials)
    else:
        raise RuntimeError("OSS_CREDENTIAL_MODE must be environment or ecs_ram_role")
    return sdk.Client(sdk_config)


def build_storage_adapter(
    local_root: Path,
    *,
    environ: Mapping[str, str],
    oss_client: Any | None = None,
    oss_sdk: Any | None = None,
) -> StorageAdapter:
    provider = environ.get("STORAGE_PROVIDER", "local").strip().lower()
    if provider == "local":
        return LocalFsStorageAdapter(local_root)
    if provider != "oss":
        raise RuntimeError("STORAGE_PROVIDER must be local or oss")
    config = OssStorageConfig(
        region=_required(environ, "OSS_REGION"),
        bucket=_required(environ, "OSS_BUCKET"),
        endpoint=_required(environ, "OSS_ENDPOINT"),
        key_prefix=environ.get("OSS_KEY_PREFIX", "math-notebook").strip(),
    )
    if oss_sdk is None:
        try:
            import alibabacloud_oss_v2 as oss_sdk
        except ImportError as exc:
            raise RuntimeError("Alibaba Cloud OSS SDK V2 is unavailable") from exc
    client = oss_client if oss_client is not None else _build_oss_client(oss_sdk, config, environ)
    return AliyunOssStorageAdapter(config, client=client, sdk=oss_sdk)
