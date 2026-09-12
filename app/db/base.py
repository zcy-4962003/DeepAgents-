"""
SQLAlchemy 声明式基类

所有 ORM 模型共用同一个 Base，Alembic 的 env.py 也通过这里的 metadata
自动比对表结构，因此模型必须在本模块被导入后才能被 autogenerate 感知。
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """项目统一 ORM 基类。"""


class TimestampMixin:
    """为模型补充创建时间/更新时间两列，避免每个表重复声明。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
