"""
FastAPI 接口层与项目闭环入口

本进程只做「轻量调度 + 鉴权 + 事件转发」，不再直接执行 Agent：

    前端 ──HTTP──> 本进程 ──enqueue──> Redis ──> arq worker（真正跑 DeepAgents）
    前端 <─WS──── 本进程 <──subscribe── Redis events:{task_id} <── worker

这样做的三个收益：
1. HTTP 请求立刻返回，长耗时任务由 worker 排队执行，支持并发上限/重试/超时；
2. Agent 进程崩溃或重启不影响已建立的 WebSocket；
3. 事件同时落库 ws_events，前端刷新后可完整回放。
"""

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.api.monitor import manager
from app.api.routes import admin, files, memory, tasks
from app.auth.deps import get_user_from_websocket
from app.auth.routes import router as auth_router
from app.db.session import dispose_engine, get_session_factory
from app.queue.enqueue import close_arq_pool
from app.services.audit import AuditAction, record_audit
from app.services.event_store import (
    close_redis,
    event_channel,
    fetch_events,
    get_redis,
)
from app.services.memory import close_memory, setup_memory
from app.services.task_ownership import get_task_checked
from app.utils.async_bridge import set_main_loop

# WebSocket 自定义关闭码（4000-4999 为应用保留区间），前端据此区分错误原因
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_NOT_FOUND = 4404
WS_CLOSE_SERVER_ERROR = 4500


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动：建运行目录、初始化记忆后端（PostgresSaver / PostgresStore）。
    关闭：按「后建先关」的顺序释放 Redis、arq 连接池、记忆连接池与数据库引擎，
    避免进程退出时留下半开连接。
    """
    config.ensure_runtime_dirs()

    # 事件改为经 Redis 转发，这里只需保留 manager 的登记能力，不再绑定 loop 直推
    manager.set_loop(asyncio.get_running_loop())
    # 登记主循环：同步工具跑在线程池里时，副作用协程要投回这个循环上执行，
    # 否则会另起循环抢连接池（详见 app/utils/async_bridge.py）
    set_main_loop()
    print("[Server] 启动中……")

    try:
        await setup_memory()
    except Exception as exc:
        # 记忆后端不可用不应阻断整个接口层：任务仍可执行，只是没有长期记忆
        print(f"[Server] 记忆后端初始化失败，长期记忆将不可用：{exc}")

    yield

    await close_arq_pool()
    await close_redis()
    await close_memory()
    await dispose_engine()
    print("[Server] 已关闭")


app = FastAPI(
    title="DeepAgents 多 Agent 电商研搜系统",
    description="单租户内部系统：多用户隔离 + 任务队列 + 事件回放 + 长短记忆",
    lifespan=lifespan,
)

# 浏览器在带 Authorization 头时禁止 allow_origins=["*"]，因此列举具体来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(tasks.router)
app.include_router(files.router)
app.include_router(memory.router)
app.include_router(admin.router)


@app.get("/api/health", tags=["system"], summary="健康检查")
async def health():
    """探活接口：只反映接口层自身状态，不探测下游依赖，避免探针误杀。"""
    return {"status": "ok"}


async def _replay_history(websocket: WebSocket, thread_id: str, after: int) -> None:
    """
    回放历史事件。

    前端重连时带上 `after=已收到的最大 id` 即可只补增量；不带则从头回放，
    覆盖「刷新页面后恢复完整执行轨迹」的场景。
    """
    factory = get_session_factory()
    async with factory() as session:
        # 一次回放可能超过单页上限，循环拉到没有为止
        while True:
            items = await fetch_events(session, thread_id, after_id=after, limit=500)
            if not items:
                return
            for item in items:
                await websocket.send_json(item)
                after = max(after, int(item.get("id", 0)))
            if len(items) < 500:
                return


async def _forward_published_events(websocket: WebSocket, thread_id: str) -> None:
    """
    订阅 Redis 频道并转发给前端。

    注意订阅必须发生在回放之前（由调用方保证顺序）：Redis 客户端会把订阅期间
    到达的消息缓存起来，等这里开始 listen 时再消费，因此回放与实时流之间不会
    出现事件空洞。前端可按事件 id 去重。
    """
    pubsub = get_redis().pubsub()
    try:
        await pubsub.subscribe(event_channel(thread_id))
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            # worker 侧已序列化为 JSON 文本，直接透传即可
            await websocket.send_text(message["data"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # 连接断开或 Redis 抖动都会走到这里；静默收尾由外层统一处理
        print(f"[WebSocket] 事件转发终止 task={thread_id}: {exc}")
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            pass


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    thread_id: str,
    token: str = Query(None, description="JWT，浏览器无法在 WS 握手设置请求头"),
    after: int = Query(0, ge=0, description="历史回放游标，传已收到的最大事件 id"),
):
    """
    WebSocket 实时通讯接口。

    握手流程：鉴权 -> 归属校验 -> 订阅 Redis -> 回放历史 -> 双向循环。
    先 accept 再校验，是为了能用自定义关闭码告诉前端到底是被拒了还是网络断了；
    否则浏览器只能看到一个没有原因的 1006。

    循环里同时维护两件事：redis 事件转发（后台协程）和前端心跳（主循环）。
    """
    await websocket.accept()

    # 1) 鉴权：令牌来自查询参数，校验规则与 HTTP 接口完全一致
    #
    # 这里刻意只捕获 HTTPException：鉴权/归属失败是预期路径，按语义回 4401/4404；
    # 但数据库连不上之类的意外错误必须打出来，否则会被伪装成「任务不存在」，
    # 排查时完全看不出真相。
    factory = get_session_factory()
    async with factory() as session:
        try:
            user = await get_user_from_websocket(token, session)
        except HTTPException as exc:
            await websocket.close(
                code=(
                    WS_CLOSE_FORBIDDEN
                    if exc.status_code == status.HTTP_403_FORBIDDEN
                    else WS_CLOSE_UNAUTHORIZED
                ),
                reason="未登录或登录已过期",
            )
            return
        except Exception as exc:
            print(f"[WebSocket] 鉴权异常 task={thread_id}: {type(exc).__name__}: {exc}")
            await websocket.close(code=WS_CLOSE_SERVER_ERROR, reason="服务内部错误")
            return

        # 2) 归属校验：别人的任务连握手都不允许，避免探测任务是否存在
        try:
            task = await get_task_checked(session, thread_id, user)
        except HTTPException as exc:
            close_code = (
                WS_CLOSE_FORBIDDEN
                if exc.status_code == status.HTTP_403_FORBIDDEN
                else WS_CLOSE_NOT_FOUND
            )
            await websocket.close(code=close_code, reason="任务不存在或无权限")
            return
        except Exception as exc:
            print(
                f"[WebSocket] 归属校验异常 task={thread_id}: {type(exc).__name__}: {exc}"
            )
            await websocket.close(code=WS_CLOSE_SERVER_ERROR, reason="服务内部错误")
            return

        user_id = user.id
        username = user.username
        task_status = task.status

    try:
        # 3) 先订阅再回放，保证「回放结束」到「实时推送开始」之间不漏事件
        await get_redis().ping()
    except Exception as exc:
        print(f"[WebSocket] Redis 不可用，仅能回放历史 task={thread_id}: {exc}")

    forward_task = asyncio.create_task(
        _forward_published_events(websocket, thread_id)
    )
    # 给订阅留出一个调度周期，确保回放期间到达的事件确实进入了缓存
    await asyncio.sleep(0)

    try:
        await _replay_history(websocket, thread_id, after)
    except Exception as exc:
        print(f"[WebSocket] 历史回放失败 task={thread_id}: {exc}")

    # 通知前端当前任务状态，避免页面在任务已结束时仍显示「执行中」
    try:
        await websocket.send_json(
            {
                "type": "monitor_event",
                "event": "task_status",
                "message": f"已连接，当前任务状态：{task_status}",
                "data": {"status": task_status, "replay_after": after},
            }
        )
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[WebSocket] 连接异常 task={thread_id}: {exc}")
    finally:
        forward_task.cancel()
        try:
            await forward_task
        except (asyncio.CancelledError, Exception):
            pass
        manager.disconnect(websocket, thread_id)
        print(f"[WebSocket] 连接已关闭 task={thread_id} user={username}")

        try:
            await record_audit(
                action=AuditAction.WS_CONNECT,
                user_id=user_id,
                resource_type="task",
                resource_id=thread_id,
                detail={"event": "disconnect", "replay_after": after},
            )
        except Exception:
            pass


if __name__ == "__main__":
    # loop 必须显式指定：uvicorn 0.36+ 用 loop_factory 直接构造事件循环，
    # 会绕过 app/__init__.py 里设置的全局策略；而 Windows 默认的 ProactorEventLoop
    # 无法承载 langgraph 记忆后端的 psycopg3 异步驱动。
    uvicorn.run(
        "app.api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        loop="app.utils.event_loop:selector_loop_factory",
    )
