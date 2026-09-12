"""对象存储抽象与 S3 兼容实现（OSS / COS 通用）。"""

from app.storage.base import Storage, StorageError
from app.storage.s3_storage import S3Storage, get_storage

__all__ = ["Storage", "StorageError", "S3Storage", "get_storage"]
