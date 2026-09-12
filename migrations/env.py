"""
Alembic 运行环境

两个刻意的设计：

1. DSN 不写在 alembic.ini 里，而是从 app.config 读。这样迁移和运行时用的是
   同一份配置来源，避免「迁移连 A 库、程序连 B 库」这类难查的问题。
2. 使用同步引擎（psycopg3）。迁移是一次性的 DDL 操作，没有并发压力，
   用同步驱动可以少一层 async 包装，出错时的栈也更直观。

注意：target_metadata 只有在 app.db.models 被导入后才是完整的，
因此本文件必须显式 import 它（下方的 noqa 就是为了说明这不是无用导入）。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config

# 必须在读取 Base.metadata 之前导入，否则 autogenerate 会认为「所有表都要删掉」
from app.db import models  # noqa: F401  (导入即注册到 Base.metadata)
from app.db.base import Base

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# autogenerate 的比对基准
target_metadata = Base.metadata


def _database_url() -> str:
    """迁移用的同步 DSN，带 psycopg3 驱动名。"""
    return app_config.PG_MIGRATION_DSN


# 由外部组件自行创建和迁移、不属于本项目模型的表。
# 不排除的话，autogenerate 每次都会认为「这些表该删掉」，生成一堆 DROP TABLE。
#
#   checkpoint* / store*  —— langgraph 的短期记忆与长期记忆（setup() 自动建）
#   vector_migrations     —— pgvector 扩展自带的版本表
_EXTERNAL_TABLE_PREFIXES = ("checkpoint", "store")
_EXTERNAL_TABLE_NAMES = {"vector_migrations"}


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """autogenerate 的过滤器：只比对 models.py 里定义过的表。"""
    if type_ != "table":
        return True
    # reflected=True 表示表来自数据库反射而非本地 metadata
    if not reflected:
        return True
    if compare_to is not None:
        # 本地也存在同名表，说明确实是我们管理的，交给 Alembic 正常比对
        return True

    table_name = str(name)
    if table_name in _EXTERNAL_TABLE_NAMES:
        return False
    return not table_name.startswith(_EXTERNAL_TABLE_PREFIXES)


def run_migrations_offline() -> None:
    """离线模式：只把 SQL 打印出来，不真正连库（用于人工审阅或交付 DBA 执行）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移，这是日常使用的方式。"""
    connectable = engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
