"""
Agent 执行过程监控模块

负责把工具调用、子智能体调用、任务结果和会话目录等事件统一包装后落库并广播。
与旧版本的差别：事件不再「直推 WebSocket」，而是

    _emit ──> PostgreSQL ws_events（历史回放）
          └─> Redis pub/sub events:{task_id} ──> API 进程 ──> WebSocket

这样带来两个好处：
1. 前端刷新页面后可以按游标回放完整轨迹；
2. Agent 执行进程（arq worker）与 WebSocket 连接进程（FastAPI）彻底解耦，
   任一进程重启都不会丢事件。
"""

import asyncio
import builtins
import datetime
from typing import Any, Optional

from fastapi import WebSocket

from app.api.context import get_thread_context, get_user_context
from app.services.event_store import record_event_sync

try:  # 用户 ID 是可选上下文，缺失时事件依然要正常上报
    import uuid as _uuid
except ImportError:  # pragma: no cover
    _uuid = None


class ToolMonitor:
    """
    工具和助手调用的统一监控入口

    业务工具只需要导入全局 monitor，并调用 report_tool/report_assistant 等方法。
    事件落库与广播的具体实现在 app/services/event_store.py，本类只负责组装载荷。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None
        return cls._instance

    def set_websocket_manager(self, manager: "ConnectionManager") -> None:
        """保留旧接口：WebSocket 推送已改由 API 进程订阅 Redis 完成。"""
        self.websocket_manager = manager

    # ------------------------------------------------------------------ #
    # 事件出口
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        构造统一监控事件，落库并广播。

        :param event_type: 事件类型，例如 tool_start、assistant_call
        :param message: 面向前端展示的事件说明
        :param data: 附加结构化数据
        """
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }

        task_id = get_thread_context()
        if task_id:
            record_event_sync(task_id, payload, self._current_user_id())
        else:
            # 脚本调试场景没有任务上下文，此时只打印，不去连数据库
            print(f"\n[Monitor:{event_type}] {message} (无任务上下文，仅控制台输出)")
            return

        # DeepAgents 脚本调试时，如果运行时暴露了 stream_writer，也同步写入流式输出
        if hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 控制台保底输出，便于无前端场景下观察执行过程
        print(f"\n[Monitor:{event_type}] {message}")

    @staticmethod
    def _current_user_id():
        """把上下文中的用户 ID 字符串转成 UUID；缺失或非法时返回 None。"""
        raw = get_user_context()
        if not raw or _uuid is None:
            return None
        try:
            return _uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # 具体事件
    # ------------------------------------------------------------------ #
    def report_tool(
        self,
        tool_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告开始执行某个工具"""
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args},
        )

    def report_assistant(
        self,
        assistant_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告正在调用某个子智能体"""
        self._emit(
            "assistant_call",
            f"正在调用助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
        )

    def report_task_result(self, result: str) -> None:
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_task_cancelled(self) -> None:
        """报告任务已被用户取消"""
        self._emit("task_cancelled", "任务已取消")

    def report_session_dir(self, path: str) -> None:
        """报告当前任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})

    def report_status(self, status: str, message: str) -> None:
        """报告任务队列状态变化（排队中/开始执行/重试中/超时）。"""
        self._emit("task_status", message, {"status": status})

    def report_error(self, message: str) -> None:
        """报告执行异常，前端据此把会话标记为失败。"""
        self._emit("error", message)


monitor = ToolMonitor()


class ConnectionManager:
    """
    WebSocket 连接管理器

    active_connections 使用 thread_id 作为 key，保证事件只推送给对应任务的前端连接。
    API 进程在 WS 建立后，会为它启动一个 Redis 订阅协程，把广播转换成
    `send_to_thread` 调用。
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        # WebSocket 发送必须回到创建连接的事件循环，因此启动时需要显式绑定 loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录 FastAPI/WebSocket 所在的事件循环，供后台线程安全投递消息"""
        self.loop = loop

    async def connect(self, websocket: WebSocket, thread_id: str) -> None:
        """接受 WebSocket 连接，并按 thread_id 保存"""
        await websocket.accept()
        self.active_connections[thread_id] = websocket

    def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """移除已经断开的 WebSocket 连接"""
        if self.active_connections.get(thread_id) is websocket:
            del self.active_connections[thread_id]
            print(f"Client disconnected: {thread_id}")
        else:
            print(f"Stale websocket disconnected, current connection kept: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """向指定 WebSocket 发送纯文本消息"""
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict[str, Any], thread_id: str) -> None:
        """向指定 thread_id 对应的前端连接发送 JSON 消息"""
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()
