"""
S3 兼容对象存储实现

阿里云 OSS、腾讯云 COS、MinIO 等均提供 S3 兼容协议，因此统一用 boto3 对接，
只需在 .env 中替换 endpoint / bucket / ak / sk 即可切换服务商，无需改代码。
"""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app import config
from app.storage.base import Storage, StorageError


class S3Storage(Storage):
    """基于 boto3 的对象存储实现。"""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str,
        use_path_style: bool = False,
    ) -> None:
        self.bucket = bucket
        # boto3 client 是线程安全的，可以全局复用一个实例
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if use_path_style else "virtual"},
                connect_timeout=config.STORAGE_CONNECT_TIMEOUT,
                read_timeout=config.STORAGE_READ_TIMEOUT,
                retries={
                    "max_attempts": config.STORAGE_MAX_RETRIES,
                    "mode": "standard",
                },
                # boto3 >= 1.36 默认对所有请求启用「弹性校验和」，上传时会改写成
                # aws-chunked 分块编码 + STREAMING-UNSIGNED-PAYLOAD-TRAILER。
                # 阿里云 OSS 未实现该扩展，会直接返回 NotImplemented，导致所有上传失败：
                #   ClientError: Aws MultiChunkedEncoding
                #   STREAMING-UNSIGNED-PAYLOAD-TRAILER is not supported
                # 改为 when_required：只在服务端明确要求时才计算校验和，
                # 兼容 OSS/COS/MinIO 等 S3 兼容实现。
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _wrap_error(action: str, key: str, exc: Exception) -> StorageError:
        """把 boto3 的底层异常统一包装成 StorageError，方便上层统一兜底。"""
        return StorageError(f"对象存储{action}失败 key={key}: {exc}")

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def put_bytes(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> None:
        params = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            params["ContentType"] = content_type
        try:
            self._client.put_object(**params)
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("上传", key, exc) from exc

    def put_file(
        self, key: str, local_path: Path, content_type: Optional[str] = None
    ) -> None:
        extra = {"ContentType": content_type} if content_type else None
        try:
            # upload_file 会根据文件大小自动选择单次上传或分片上传
            self._client.upload_file(
                str(local_path), self.bucket, key, ExtraArgs=extra
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("上传", key, exc) from exc

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("读取", key, exc) from exc

    def download_file(self, key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self.bucket, key, str(local_path))
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("下载", key, exc) from exc

    # ------------------------------------------------------------------ #
    # 删除 / 存在性
    # ------------------------------------------------------------------ #
    def delete(self, key: str) -> None:
        try:
            # S3 的 delete_object 对不存在的 key 也返回成功，天然幂等
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("删除", key, exc) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise self._wrap_error("检查", key, exc) from exc

    # ------------------------------------------------------------------ #
    # 预签名
    # ------------------------------------------------------------------ #
    def presigned_url(
        self,
        key: str,
        expires: Optional[timedelta] = None,
        download_name: Optional[str] = None,
    ) -> str:
        expires = expires or timedelta(seconds=config.STORAGE_PRESIGN_EXPIRE_SECONDS)
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}

        if download_name:
            # RFC 5987 编码，保证中文文件名在各浏览器下都能正确下载
            encoded = quote(download_name)
            params["ResponseContentDisposition"] = (
                f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}"
            )

        try:
            return self._client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=int(expires.total_seconds())
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._wrap_error("生成下载地址", key, exc) from exc


_storage: Optional[S3Storage] = None
_storage_lock = threading.Lock()


def get_storage() -> S3Storage:
    """
    获取全局对象存储单例。

    API 进程与 worker 进程各自持有一份；boto3 client 本身线程安全，
    因此这里用锁保证多线程首次初始化时不会重复创建。
    """
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                _storage = S3Storage(
                    endpoint=config.STORAGE_ENDPOINT,
                    bucket=config.STORAGE_BUCKET,
                    access_key=config.STORAGE_ACCESS_KEY,
                    secret_key=config.STORAGE_SECRET_KEY,
                    region=config.STORAGE_REGION,
                    use_path_style=config.STORAGE_USE_PATH_STYLE,
                )
    return _storage
