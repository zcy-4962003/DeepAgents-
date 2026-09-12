"""
审计日志服务

覆盖登录、任务、文件、SQL、记忆和管理动作六类关键操作。写入失败不会向上抛异常，
因为审计属于旁路能力，不能因为它写不进去就让主业务失败——但会在控制台留下痕迹。

提供两个入口：
- `record_audit`    async，供 HTTP 接口和 worker 协程调用
- `record_audit_sync` sync，供同步工具函数（如 db_tools）调用
"""

import uuid
from typing import Any, Optional

from app.db.models import AuditLog
from app.db.session import session_scope
from app.utils.async_bridge import run_soon


class AuditAction:
    """审计动作常量，避免各处硬编码字符串导致日志难以检索。"""

    REGISTER = "register"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"

    TASK_CREATE = "task_create"
    TASK_CANCEL = "task_cancel"
    TASK_DELETE = "task_delete"
    TASK_VIEW_ALL = "task_view_all"

    WS_CONNECT = "ws_connect"

    FILE_UPLOAD = "file_upload"
    FILE_DOWNLOAD = "file_download"
    FILE_PREVIEW = "file_preview"
    FILE_EDIT = "file_edit"
    FILE_DELETE = "file_delete"
    FILE_CLEANUP = "file_cleanup"

    SQL_QUERY = "sql_query"

    MEMORY_WRITE = "memory_write"
    MEMORY_RECALL = "memory_recall"

    ADMIN_ALLOWLIST_UPDATE = "admin_allowlist_update"
    ADMIN_USER_UPDATE = "admin_user_update"


async def record_audit(
    action: str,
    user_id: Optional[uuid.UUID] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> None:
    """
    写入一条审计记录。

    :param action: 动作标识，取自 AuditAction
    :param user_id: 操作者；登录失败等场景允许为空
    :param resource_type: 资源类型，如 task / file / sql
    :param resource_id: 资源标识
    :param detail: 结构化补充信息（SQL 全文、文件名、失败原因等）
    :param ip: 客户端 IP
    """
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    user_id=user_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id is not None else None,
                    detail=detail or {},
                    ip=ip,
                )
            )
    except Exception as exc:  # pragma: no cover - 审计失败不应影响主流程
        print(f"[Audit] 写入失败 action={action}: {exc}")


def record_audit_sync(
    action: str,
    user_id: Optional[uuid.UUID] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> None:
    """
    同步上下文的审计入口。

    同步工具（数据库查询、文件读写等）跑在 Agent 的事件循环里，这里优先把写库
    协程挂到当前循环上异步执行，不阻塞模型调用；只有完全脱离循环（脚本调试）时
    才退化为 asyncio.run。
    """
    run_soon(
        record_audit(
            action=action,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            ip=ip,
        )
    )
