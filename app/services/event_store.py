"""
事件持久化与实时广播

Agent 执行过程中产生的每条监控事件走同一条管道：

    monitor._emit
        ├─ 写入 PostgreSQL ws_events   ← 前端刷新后按游标回放，恢复完整进度
        └─ publish Redis events:{task_id} ← API 进程订阅后转发给 WebSocket

worker 进程只负责写库和广播，不直接持有 WebSocket；API 进程负责把广播转成
WebSocket 消息。这样 WebSocket 断线、进程重启都不会丢事件。
"""

import uuid
from typing import Any, Optional

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db.models import WsEvent
from app.db.session import session_scope
from app.utils.async_bridge import run_soon

_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """
    获取全局异步 Redis 客户端（懒加载单例）。

    arq 队列本身也用这个连接串，但 arq 有自己的连接池，这里只用于事件总线。
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            config.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接；进程退出时调用。"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def event_channel(task_id: str) -> str:
    """任务事件对应的 Redis pub/sub 频道名。"""
    return f"{config.REDIS_EVENT_CHANNEL_PREFIX}{task_id}"


def cancel_key(task_id: str) -> str:
    """任务取消标志的 Redis key。"""
    return f"{config.TASK_CANCEL_KEY_PREFIX}{task_id}"


async def persist_event(
    task_id: str,
    payload: dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
) -> Optional[int]:
    """
    把一条监控事件写入 ws_events。

    :return: 新事件的数据库自增 id（前端用作回放游标）；写入失败返回 None
    """
    try:
        async with session_scope() as session:
            row = WsEvent(
                task_id=uuid.UUID(str(task_id)),
                user_id=user_id,
                event_type=str(payload.get("event", "unknown")),
                message=str(payload.get("message", "")),
                # data 列保存完整 payload，回放时可以原样吐给前端，不做有损裁剪
                data=payload,
            )
            session.add(row)
            await session.flush()
            return int(row.id)
    except Exception as exc:
        print(f"[EventStore] 事件落库失败 task={task_id}: {exc}")
        return None


async def publish_event(task_id: str, payload: dict[str, Any]) -> None:
    """把事件广播到 Redis 频道，API 进程订阅后转发给对应 WebSocket。"""
    try:
        await get_redis().publish(
            event_channel(task_id), json_dumps(payload)
        )
    except Exception as exc:
        print(f"[EventStore] 事件广播失败 task={task_id}: {exc}")


def json_dumps(payload: dict[str, Any]) -> str:
    """
    统一的事件序列化入口。

    - ensure_ascii=False：中文不转义成 \\uXXXX，直接读 Redis 也能看懂；
    - default=str：事件里可能带 UUID / datetime，统一降级成字符串而不是抛异常。
    """
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


async def record_event(
    task_id: str,
    payload: dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
) -> None:
    """落库 + 广播，两个动作互不阻塞，任一失败都不影响 Agent 继续执行。"""
    await persist_event(task_id, payload, user_id)
    await publish_event(task_id, payload)


def record_event_sync(
    task_id: str,
    payload: dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
) -> None:
    """
    同步上下文的入口。

    monitor._emit 是同步方法，这里把落库+广播的协程挂到当前事件循环上异步执行，
    避免在 Agent 的同步工具调用点阻塞模型流。极端情况下（无运行中的循环）退化为
    asyncio.run，保证脚本调试时事件同样不丢。
    """
    run_soon(record_event(task_id, payload, user_id))


async def fetch_events(
    session: AsyncSession,
    task_id: str,
    event_type: Optional[str] = None,
    after_id: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    按游标增量拉取某个任务的历史事件。

    :param event_type: 传入时只返回该类型事件（前端筛选器使用）
    :param after_id: 只返回 id 大于该值的事件，前端传已收到的最大 id 即可续播
    :param limit: 单次返回条数上限，防止一次拉爆前端
    """
    statement = (
        select(WsEvent)
        .where(WsEvent.task_id == uuid.UUID(str(task_id)))
        .where(WsEvent.id > after_id)
        .order_by(WsEvent.id)
        .limit(limit)
    )
    if event_type:
        statement = statement.where(WsEvent.event_type == event_type)

    rows = (await session.scalars(statement)).all()
    return [_row_to_payload(row) for row in rows]


def _row_to_payload(row: WsEvent) -> dict[str, Any]:
    """把数据库行还原成前端认识的 monitor_event 结构，并附带事件 id 作为游标。"""
    payload = dict(row.data or {})
    payload.setdefault("type", "monitor_event")
    payload["id"] = int(row.id)
    payload.setdefault("timestamp", row.created_at.isoformat() if row.created_at else "")
    return payload
