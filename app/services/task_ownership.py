"""
任务归属校验

thread_id 现在等同于 tasks.id，由服务端登记，前端不能再自己编造。所有按
thread_id 访问的接口（事件回放、文件列表、取消、结果、WebSocket）都必须先
过这里，保证员工之间互相看不到对方的任务。

规则：任务所有者本人可访问；admin 可访问全员任务；其余一律 403。
"""

import uuid
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.db.models import Task


def can_access_task(task: Task, current_user: CurrentUser) -> bool:
    """判断当前用户是否有权访问该任务：本人或管理员。"""
    return current_user.is_admin or task.user_id == current_user.id


def _parse_task_id(task_id: str) -> uuid.UUID:
    """把前端传来的 thread_id 转成 UUID；格式非法时按「任务不存在」处理。"""
    try:
        return uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在"
        )


async def get_task_checked(
    session: AsyncSession,
    task_id: str,
    current_user: CurrentUser,
) -> Task:
    """
    取出任务并校验归属。

    :raises HTTPException: 404 任务不存在；403 任务属于其他用户
    """
    task = await session.get(Task, _parse_task_id(task_id))
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在"
        )
    if not can_access_task(task, current_user):
        # 刻意返回 403 而不是 404：内部系统里提示「无权访问」比隐藏资源更好排查
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务"
        )
    return task


async def find_task_by_id(
    session: AsyncSession, task_id: str
) -> Optional[Task]:
    """不做权限校验地取任务，仅供 worker 内部使用。"""
    try:
        return await session.get(Task, uuid.UUID(str(task_id)))
    except (ValueError, TypeError):
        return None


def user_filter(current_user: CurrentUser):
    """
    构造列表查询的归属过滤条件。

    member 只能看自己的数据（返回 user_id == 自己），admin 不过滤（返回 None）。
    供任务列表、文件列表等接口统一复用。
    """
    if current_user.is_admin:
        return None
    return current_user.id


def apply_user_scope(statement, column, current_user: CurrentUser):
    """把归属过滤条件套到查询语句上，admin 原样返回。"""
    scope_user_id = user_filter(current_user)
    if scope_user_id is None:
        return statement
    return statement.where(column == scope_user_id)


async def list_accessible_task_ids(
    session: AsyncSession, current_user: CurrentUser
) -> Optional[list[uuid.UUID]]:
    """列出当前用户可访问的任务 id；admin 返回 None 表示「不做限制」。"""
    if current_user.is_admin:
        return None
    rows = await session.scalars(
        select(Task.id).where(Task.user_id == current_user.id)
    )
    return list(rows)
