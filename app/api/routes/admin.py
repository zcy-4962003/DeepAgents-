"""
管理接口（仅 admin 可用）

包含审计日志查询、用户管理、SQL 表白名单维护。这三类操作都会再次写审计，
保证「谁看了审计」「谁改了权限」「谁放了新表」同样可追溯。
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    AllowlistAddRequest,
    AllowlistItem,
    AuditItem,
    AuditListResponse,
    UserItem,
    UserUpdateRequest,
)
from app.auth.deps import CurrentUser, client_ip, require_admin
from app.db.models import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    AuditLog,
    SqlTableAllowlist,
    Task,
    User,
)
from app.db.session import get_session
from app.services.audit import AuditAction, record_audit

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@router.get("/audit", response_model=AuditListResponse, summary="审计日志")
async def list_audit(
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    action: Optional[str] = Query(None, description="按动作过滤"),
    user_id: Optional[str] = Query(None, description="按操作者过滤"),
    resource_type: Optional[str] = Query(None, description="按资源类型过滤"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """分页查询审计日志，支持按动作/操作者/资源类型过滤。"""
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    if user_id:
        try:
            conditions.append(AuditLog.user_id == uuid.UUID(user_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="user_id 格式不正确")

    count_stmt = select(func.count()).select_from(AuditLog)
    list_stmt = select(AuditLog).order_by(AuditLog.id.desc())
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = await session.scalar(count_stmt) or 0
    rows = await session.scalars(list_stmt.limit(limit).offset(offset))

    return AuditListResponse(
        total=total,
        items=[
            AuditItem(
                id=row.id,
                user_id=str(row.user_id) if row.user_id else None,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                detail=row.detail or {},
                ip=row.ip,
                created_at=_iso(row.created_at),
            )
            for row in rows
        ],
    )


@router.get("/users", summary="用户列表")
async def list_users(
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """列出全部内部员工账号。"""
    rows = await session.scalars(select(User).order_by(User.created_at))
    return {
        "items": [
            UserItem(
                id=str(user.id),
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                is_active=user.is_active,
                created_at=_iso(user.created_at),
            )
            for user in rows
        ]
    }


@router.patch("/users/{user_id}", summary="修改用户角色/状态")
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """调整用户角色或停用账号；停用后该用户的旧令牌会立即失效。"""
    try:
        target_id = uuid.UUID(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="用户 ID 格式不正确")

    user = await session.get(User, target_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if user.id == current_user.id and payload.is_active is False:
        # 防止管理员把自己锁在系统外
        raise HTTPException(status_code=400, detail="不能停用当前登录的账号")

    if payload.role is not None:
        if payload.role not in {ROLE_MEMBER, ROLE_ADMIN}:
            raise HTTPException(status_code=400, detail="角色只能是 member 或 admin")
        user.role = payload.role

    if payload.is_active is not None:
        user.is_active = payload.is_active

    await session.commit()

    await record_audit(
        action=AuditAction.ADMIN_USER_UPDATE,
        user_id=current_user.id,
        resource_type="user",
        resource_id=user_id,
        detail={"role": user.role, "is_active": user.is_active},
        ip=client_ip(request),
    )
    return {"status": "ok", "user_id": user_id}


@router.get("/sql-allowlist", summary="SQL 表白名单")
async def list_allowlist(
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """查看当前允许数据库子智能体访问的表。"""
    rows = await session.scalars(
        select(SqlTableAllowlist).order_by(SqlTableAllowlist.table_name)
    )
    return {
        "items": [
            AllowlistItem(table_name=row.table_name, note=row.note) for row in rows
        ]
    }


@router.post("/sql-allowlist", summary="新增白名单表")
async def add_allowlist(
    payload: AllowlistAddRequest,
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    把一张表加入白名单。

    worker 进程内有一层白名单缓存，改动会在 SQL_ALLOWLIST_CACHE_SECONDS 秒内生效。
    """
    table_name = payload.table_name.strip().lower()
    exists = await session.scalar(
        select(SqlTableAllowlist).where(SqlTableAllowlist.table_name == table_name)
    )
    if exists:
        raise HTTPException(status_code=409, detail="该表已在白名单中")

    session.add(SqlTableAllowlist(table_name=table_name, note=payload.note))
    await session.commit()

    await record_audit(
        action=AuditAction.ADMIN_ALLOWLIST_UPDATE,
        user_id=current_user.id,
        resource_type="sql_allowlist",
        resource_id=table_name,
        detail={"operation": "add", "note": payload.note},
        ip=client_ip(request),
    )
    return {"status": "ok", "table_name": table_name}


@router.delete("/sql-allowlist/{table_name}", summary="移除白名单表")
async def remove_allowlist(
    table_name: str,
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """把一张表移出白名单，之后的查询会被 sql_guard 拒绝。"""
    normalized = table_name.strip().lower()
    result = await session.execute(
        delete(SqlTableAllowlist).where(SqlTableAllowlist.table_name == normalized)
    )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="该表不在白名单中")
    await session.commit()

    await record_audit(
        action=AuditAction.ADMIN_ALLOWLIST_UPDATE,
        user_id=current_user.id,
        resource_type="sql_allowlist",
        resource_id=normalized,
        detail={"operation": "remove"},
        ip=client_ip(request),
    )

    # 立刻清空该进程的白名单缓存，让管理动作即时可见
    from app.services import sql_guard

    sql_guard._allowlist_cache = set()
    return {"status": "ok", "table_name": normalized}


@router.get("/stats", summary="系统概览")
async def system_stats(
    current_user: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """系统概览：任务数、用户数、审计条数，用于管理面板首屏。"""
    task_count = await session.scalar(select(func.count()).select_from(Task))
    user_count = await session.scalar(select(func.count()).select_from(User))
    audit_count = await session.scalar(select(func.count()).select_from(AuditLog))

    return {
        "tasks": task_count or 0,
        "users": user_count or 0,
        "audit_logs": audit_count or 0,
    }
