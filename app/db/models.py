"""
PostgreSQL 系统库 ORM 模型

承载账号、任务、WebSocket 事件、文件元数据、审计日志和 SQL 表白名单六类数据。
短期记忆(checkpoint)与长期记忆(store)的表由 langgraph 自己管理，不在这里定义。

隔离模型：系统是单租户（一个公司内部员工），所有私有资源仅按 user_id 隔离，
因此每张业务表都带 user_id 冗余列，便于按用户直接过滤而不用频繁 join。
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


# 任务状态机：排队 -> 执行中 -> 成功 / 失败 / 已取消
TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_TERMINAL_STATUSES = {
    TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
}

ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"

FILE_KIND_UPLOADED = "uploaded"
FILE_KIND_GENERATED = "generated"


class User(Base, TimestampMixin):
    """内部员工账号；role 决定能否查看全员数据与审计日志。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 只存 bcrypt 摘要，永不落明文密码
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MEMBER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Task(Base, TimestampMixin):
    """
    一次研搜任务，同时也是 Agent 的 thread_id。

    这张表既是任务历史，也是权限判断的依据：任何按 thread_id 访问接口的请求，
    都必须先在这里确认该任务属于当前用户（或当前用户是 admin）。
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=TASK_STATUS_QUEUED, index=True, nullable=False
    )
    # 队列重试计数与上限，达到上限后不再重试，直接置 failed
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # arq 的 job id，取消任务时需要用它定位队列中的作业
    arq_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    queued_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("idx_tasks_user_created", "user_id", "created_at"),)


class WsEvent(Base):
    """
    WebSocket 历史事件

    Agent 执行过程中的每一条监控事件都会先落这张表，再通过 Redis 广播给 API 进程。
    前端刷新页面时用 GET /api/task/{id}/events 按 id 游标回放，即可恢复完整进度。
    """

    __tablename__ = "ws_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 user_id：回放事件时可以直接校验归属，无需再查 tasks 表
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_ws_events_task", "task_id", "id"),
        Index("idx_ws_events_task_type", "task_id", "event_type", "id"),
    )


class FileRecord(Base, TimestampMixin):
    """
    文件元数据

    文件实体保存在对象存储（OSS/COS），这里只记录 object_key 和校验信息。
    expires_at 到期后由 worker 的定时任务同时删除对象与元数据行。

    需要 created_at：文件列表与清理都按上传时间排序（见 file_governance），
    因此这里和 Task 一样带上 TimestampMixin。
    """

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # uploaded=用户上传的附件，generated=Agent 生成的交付物
    kind: Mapped[str] = mapped_column(
        String(16), default=FILE_KIND_UPLOADED, nullable=False, index=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index("idx_files_user_kind_created", "user_id", "kind", "created_at"),
    )


class AuditLog(Base):
    """关键操作审计：登录、任务、文件、SQL、记忆、管理动作全部落这里。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class SqlTableAllowlist(Base):
    """
    SQL 表白名单

    数据库查询子智能体只能访问这里的表。空表时会在首次使用时用业务 MySQL
    现有表名自动播种（见 app/services/sql_guard.py），管理员可随时增删。
    """

    __tablename__ = "sql_table_allowlist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("table_name", name="uq_sql_allowlist_table"),)
