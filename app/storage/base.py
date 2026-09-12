"""
对象存储抽象接口

文件实体一律存放在云对象存储（OSS/COS），本地只保留 Agent 运行期的临时工作目录。
这里定义统一接口，方便后续替换成其它兼容 S3 的服务而不影响业务代码。

接口同时提供同步版本（boto3 原生）和 `a` 前缀的异步版本（内部用 to_thread 包装），
供 FastAPI 接口和 worker 按需选择。
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path
from typing import Optional


class StorageError(RuntimeError):
    """对象存储操作失败；调用方据此返回 5xx 而不是把 traceback 抛给前端。"""


class Storage(ABC):
    """对象存储统一接口。"""

    # ------------------------------------------------------------------ #
    # 同步接口
    # ------------------------------------------------------------------ #
    @abstractmethod
    def put_bytes(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> None:
        """写入一段字节内容到指定 key。"""

    @abstractmethod
    def put_file(
        self, key: str, local_path: Path, content_type: Optional[str] = None
    ) -> None:
        """把一个本地文件上传到指定 key（大文件自动走分片上传）。"""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """读取指定 key 的全部内容到内存，仅用于小文件预览。"""

    @abstractmethod
    def download_file(self, key: str, local_path: Path) -> None:
        """把对象下载到本地路径，父目录会自动创建。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象；对象不存在时不报错（幂等）。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """判断对象是否存在。"""

    @abstractmethod
    def presigned_url(
        self,
        key: str,
        expires: Optional[timedelta] = None,
        download_name: Optional[str] = None,
    ) -> str:
        """
        生成带签名的临时访问地址。

        :param key: 对象 key
        :param expires: 有效期，默认取 STORAGE_PRESIGN_EXPIRE_SECONDS
        :param download_name: 传入时强制浏览器按该文件名下载，否则内联预览
        """

    # ------------------------------------------------------------------ #
    # 异步包装：boto3 是同步库，直接调用会阻塞事件循环
    # ------------------------------------------------------------------ #
    async def aput_file(
        self, key: str, local_path: Path, content_type: Optional[str] = None
    ) -> None:
        await asyncio.to_thread(self.put_file, key, local_path, content_type)

    async def aput_bytes(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> None:
        await asyncio.to_thread(self.put_bytes, key, data, content_type)

    async def aget_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self.get_bytes, key)

    async def adownload_file(self, key: str, local_path: Path) -> None:
        await asyncio.to_thread(self.download_file, key, local_path)

    async def adelete(self, key: str) -> None:
        await asyncio.to_thread(self.delete, key)

    async def aexists(self, key: str) -> bool:
        return await asyncio.to_thread(self.exists, key)

    async def apresigned_url(
        self,
        key: str,
        expires: Optional[timedelta] = None,
        download_name: Optional[str] = None,
    ) -> str:
        return await asyncio.to_thread(self.presigned_url, key, expires, download_name)
