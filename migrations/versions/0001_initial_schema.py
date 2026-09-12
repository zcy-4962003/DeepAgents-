"""initial schema: users / tasks / ws_events / files / audit_log / sql_table_allowlist

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-11

说明：
- 索引名与 SQLAlchemy 自动生成的命名保持一致（`ix_<表>_<列>`），
  否则每次 autogenerate 都会出现「删除旧索引、新建同名索引」的假差异。
- langgraph 的 checkpoint/store 系列表由它自己的 setup() 建立，不在本迁移内。
- vector 扩展需要超级用户权限，因此放在 scripts/init_db.py 里单独执行，
  本迁移只负责业务表，保证用普通账号也能跑通。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 模型的 unique=True + index=True 只产出这一个唯一索引，不再额外建 UNIQUE 约束，
    # 否则 autogenerate 会一直报「多出一个 users_username_key」
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # ------------------------------------------------------------------ tasks
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("arq_job_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"], unique=False)
    op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)
    op.create_index("idx_tasks_user_created", "tasks", ["user_id", "created_at"], unique=False)

    # -------------------------------------------------------------- ws_events
    op.create_table(
        "ws_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ws_events_task", "ws_events", ["task_id", "id"], unique=False)
    op.create_index(
        "idx_ws_events_task_type", "ws_events", ["task_id", "event_type", "id"], unique=False
    )

    # ------------------------------------------------------------------ files
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_files_task_id", "files", ["task_id"], unique=False)
    op.create_index("ix_files_user_id", "files", ["user_id"], unique=False)
    op.create_index("ix_files_kind", "files", ["kind"], unique=False)
    op.create_index("ix_files_expires_at", "files", ["expires_at"], unique=False)
    op.create_index(
        "idx_files_user_kind_created", "files", ["user_id", "kind", "created_at"], unique=False
    )

    # -------------------------------------------------------------- audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"], unique=False)
    op.create_index("ix_audit_log_action", "audit_log", ["action"], unique=False)
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"], unique=False)

    # ----------------------------------------------------- sql_table_allowlist
    op.create_table(
        "sql_table_allowlist",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_name", name="uq_sql_allowlist_table"),
    )


def downgrade() -> None:
    # 按依赖关系倒序删除：子表在前，被引用的 users 在最后
    op.drop_table("sql_table_allowlist")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("idx_files_user_kind_created", table_name="files")
    op.drop_index("ix_files_expires_at", table_name="files")
    op.drop_index("ix_files_kind", table_name="files")
    op.drop_index("ix_files_user_id", table_name="files")
    op.drop_index("ix_files_task_id", table_name="files")
    op.drop_table("files")

    op.drop_index("idx_ws_events_task_type", table_name="ws_events")
    op.drop_index("idx_ws_events_task", table_name="ws_events")
    op.drop_table("ws_events")

    op.drop_index("idx_tasks_user_created", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_user_id", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
