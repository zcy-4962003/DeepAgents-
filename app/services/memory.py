"""
短期记忆与长期记忆的统一管理

短期记忆（Checkpointer）
    同一 thread_id 内的对话上下文。用 langgraph 的 `AsyncPostgresSaver` 落库，
    替代原来的 InMemorySaver —— 进程重启后同一会话仍能接着聊。

长期记忆（Store）
    跨会话、跨进程复用的用户画像与历史结论。用 `AsyncPostgresStore` + pgvector，
    命名空间固定为 `(user_id,)`：这是隔离边界，A 用户的检索永远碰不到 B 用户的数据。

两种记忆都走 PG_SYNC_DSN（psycopg 驱动），连接池在本模块统一持有，
由 FastAPI lifespan 或 arq worker 的 on_startup 调用 `setup_memory()` 初始化。
"""

import asyncio
import os
import uuid
from typing import Any, Optional

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import config

# 记忆命名空间 = (user_id,)，用户之间物理隔离
MEMORY_FIELDS = ["text"]

_saver: Optional[AsyncPostgresSaver] = None
_store: Optional[AsyncPostgresStore] = None
_pool: Optional[AsyncConnectionPool] = None
_init_lock = asyncio.Lock()


def _build_embeddings() -> Any:
    """
    构造长期记忆用的向量化模型。

    复用 .env 中已有的 dashscope 兼容 embedding 配置，避免再引入一套独立配置。
    tiktoken 预检查在国内环境常因下载失败而报错，这里与本地知识库保持同样的处理。
    """
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_api_base=os.getenv("OPENAI_BASE_URL"),
        tiktoken_enabled=False,
        check_embedding_ctx_length=False,
    )


def namespace_for(user_id: str) -> tuple[str, ...]:
    """长期记忆命名空间；所有读写都必须经过它，保证隔离不被绕过。"""
    return (str(user_id),)


async def setup_memory() -> None:
    """
    初始化记忆后端（幂等）。

    连接池用 autocommit=True + prepare_threshold=0，与 langgraph 官方
    `from_conn_string` 的连接参数保持一致，否则 CONCURRENTLY 建索引会失败。
    """
    global _saver, _store, _pool

    if _saver is not None and _store is not None:
        return

    async with _init_lock:
        if _saver is not None and _store is not None:
            return

        if _pool is None:
            _pool = AsyncConnectionPool(
                conninfo=config.PG_SYNC_DSN,
                min_size=1,
                max_size=8,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            await _pool.open()
            # 启动即校验连通性，避免等到第一次对话才发现数据库配置错误
            await _pool.wait()

        if _saver is None:
            _saver = AsyncPostgresSaver(_pool)
            await _saver.setup()

        if _store is None:
            _store = AsyncPostgresStore(
                _pool,
                index={
                    "dims": config.EMBEDDING_DIMS,
                    "embed": _build_embeddings(),
                    "fields": MEMORY_FIELDS,
                },
            )
            await _store.setup()

        print("[Memory] PostgresSaver / PostgresStore 初始化完成")


async def close_memory() -> None:
    """关闭记忆连接池；进程退出时调用。"""
    global _saver, _store, _pool
    if _pool is not None:
        await _pool.close()
    _saver = None
    _store = None
    _pool = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """取短期记忆后端；未初始化时自动初始化。"""
    if _saver is None:
        await setup_memory()
    assert _saver is not None
    return _saver


async def get_store() -> AsyncPostgresStore:
    """取长期记忆后端；未初始化时自动初始化。"""
    if _store is None:
        await setup_memory()
    assert _store is not None
    return _store


# --------------------------------------------------------------------------- #
# 长期记忆读写
# --------------------------------------------------------------------------- #
async def save_memory(
    user_id: str,
    text: str,
    kind: str = "fact",
    extra: Optional[dict[str, Any]] = None,
    key: Optional[str] = None,
) -> str:
    """
    写入一条长期记忆。

    :param user_id: 记忆归属用户，决定命名空间
    :param text: 记忆正文，也是向量化检索的字段
    :param kind: 记忆类别：fact(事实) / preference(偏好) / conclusion(结论)
    :param extra: 结构化补充信息
    :param key: 指定 key；不传时自动生成，重复写入同一 key 会覆盖
    :return: 实际使用的 key
    """
    store = await get_store()
    memory_key = key or f"{kind}:{uuid.uuid4()}"
    value: dict[str, Any] = {"text": text, "kind": kind, **(extra or {})}
    await store.aput(
        namespace_for(user_id), memory_key, value, index=MEMORY_FIELDS
    )
    return memory_key


async def recall_memories(
    user_id: str,
    query: str,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    语义检索该用户的长期记忆。

    只会在 `(user_id,)` 命名空间内检索，因此天然满足用户隔离要求。
    :return: 命中的记忆列表（含 text 与 kind），没有命中时返回空列表
    """
    store = await get_store()
    try:
        items = await store.asearch(
            namespace_for(user_id),
            query=query,
            limit=limit or config.MEMORY_RECALL_LIMIT,
        )
    except Exception as exc:
        # 记忆检索属于增强能力，失败时降级为空，不能阻断正常对话
        print(f"[Memory] 长期记忆检索失败，本次跳过：{exc}")
        return []

    memories: list[dict[str, Any]] = []
    for item in items:
        value = item.value or {}
        text = str(value.get("text", "")).strip()
        if text:
            memories.append(
                {"text": text, "kind": value.get("kind", "fact"), "key": item.key}
            )
    return memories


async def list_memories(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """列出该用户的全部长期记忆（不分页，用于前端记忆管理页）。"""
    store = await get_store()
    items = await store.asearch(namespace_for(user_id), limit=limit)
    return [
        {
            "key": item.key,
            "text": str((item.value or {}).get("text", "")),
            "kind": str((item.value or {}).get("kind", "fact")),
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }
        for item in items
    ]


async def delete_memory(user_id: str, key: str) -> None:
    """删除一条长期记忆（前端删除按钮 / 用户纠正错误认知时使用）。"""
    store = await get_store()
    await store.adelete(namespace_for(user_id), key)
