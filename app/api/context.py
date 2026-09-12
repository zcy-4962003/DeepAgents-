"""
请求上下文管理模块

负责在异步请求链路中保存当前任务的 进程ID（thread_id）、对话目录（session_dir）
与所属用户（user_id）。工具、智能体和监控模块可以在深层调用中读取这些值，
而不需要层层传参。

注意：ContextVar 只在同一个 asyncio 任务链内可见。arq worker 在启动任务协程前
会显式设置这些值，因此 Agent 内部任意深度的同步/异步调用都能读到。
"""

from contextvars import ContextVar, Token
from typing import Optional

_session_dir_ctx: ContextVar[Optional[str]] = ContextVar(
    "session_dir",
    default=None,
)
_thread_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "thread_id",
    default=None,
)
# 当前任务所属用户，事件落库、审计写入、长期记忆命名空间都依赖它
_user_id_ctx: ContextVar[Optional[str]] = ContextVar(
    "user_id",
    default=None,
)


def set_session_context(path: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的会话目录

    :param path: 当前任务的工作目录
    :return: reset 时需要使用的上下文 token
    """
    return _session_dir_ctx.set(path)


def get_session_context() -> Optional[str]:
    """
    获取当前请求链路的会话目录

    :return: 当前任务工作目录；未设置时返回 None
    """
    return _session_dir_ctx.get()


def set_thread_context(thread_id: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的线程 ID

    :param thread_id: 前端连接和 Agent 执行共用的任务 ID
    :return: reset 时需要使用的上下文 token
    """
    return _thread_id_ctx.set(thread_id)


def get_thread_context() -> Optional[str]:
    """
    获取当前请求链路的线程 ID

    :return: 当前任务 ID；未设置时返回 None
    """
    return _thread_id_ctx.get()


def set_user_context(user_id: str) -> Token[Optional[str]]:
    """
    设置当前请求链路的用户 ID

    :param user_id: 任务所属用户的 UUID 字符串
    :return: reset 时需要使用的上下文 token
    """
    return _user_id_ctx.set(user_id)


def get_user_context() -> Optional[str]:
    """
    获取当前请求链路的用户 ID

    :return: 当前用户 UUID 字符串；未设置时返回 None
    """
    return _user_id_ctx.get()


def reset_session_context(
    session_token: Token[Optional[str]],
    thread_token: Optional[Token[Optional[str]]] = None,
    user_token: Optional[Token[Optional[str]]] = None,
) -> None:
    """
    恢复请求上下文，避免本次任务信息残留到后续请求

    :param session_token: set_session_context 返回的 token
    :param thread_token: set_thread_context 返回的 token
    :param user_token: set_user_context 返回的 token
    """
    _session_dir_ctx.reset(session_token)
    if thread_token is not None:
        _thread_id_ctx.reset(thread_token)
    if user_token is not None:
        _user_id_ctx.reset(user_token)
