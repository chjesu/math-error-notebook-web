"""Safe file intake primitives for local and object-storage adapters."""

from .intake import FileCandidate, FileIntake
from .oss_storage import AliyunOssStorageAdapter, OssStorageConfig, build_storage_adapter
from .storage import LocalFsStorageAdapter, PresignedStorageRequest, StorageAdapter

__all__ = [
    "AliyunOssStorageAdapter",
    "FileCandidate",
    "FileIntake",
    "LocalFsStorageAdapter",
    "OssStorageConfig",
    "PresignedStorageRequest",
    "StorageAdapter",
    "build_storage_adapter",
]
