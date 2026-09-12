"""
PostgreSQL 一次性初始化脚本

新环境部署时按顺序跑一遍即可，已初始化的环境重复执行是安全的（全部幂等）：

    1. 校验连接，打印服务端版本；
    2. 建 pgvector 扩展（长期记忆的向量检索依赖它）；
    3. 执行 Alembic 迁移，建出业务表；
    4. 初始化 langgraph 的短期记忆(checkpoints)与长期记忆(store)表；
    5. 播种 SQL 表白名单（可选）；
    6. 创建初始管理员账号（可选）。

用法：

    python scripts/init_db.py                       # 1-4 步
    python scripts/init_db.py --seed-allowlist      # 额外从业务 MySQL 播种白名单
    python scripts/init_db.py --admin zhang 123456  # 额外创建管理员

注意第 2 步 `CREATE EXTENSION vector` 需要超级用户权限。若当前账号不是超级用户，
脚本会打印一段提示，把那条 SQL 交给 DBA 执行后重跑即可，不影响后续步骤继续做。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 允许以 `python scripts/init_db.py` 直接运行：把项目根目录加进模块搜索路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app import config  # noqa: E402
from app.utils.event_loop import run as run_async  # noqa: E402


def _print_step(index: int, title: str) -> None:
    print(f"\n[{index}] {title}")


def check_connection() -> None:
    """第 1 步：确认能连上库，并打印版本，便于排查「连错库」这类问题。"""
    _print_step(1, "校验 PostgreSQL 连接")
    engine = create_engine(config.PG_MIGRATION_DSN, poolclass=None)
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        database = conn.execute(text("SELECT current_database()")).scalar()
        user = conn.execute(text("SELECT current_user")).scalar()
    engine.dispose()
    print(f"    连接成功：database={database} user={user} server_version={version}")


def ensure_vector_extension() -> None:
    """
    第 2 步：建 pgvector 扩展。

    这一步失败不终止脚本：扩展只影响长期记忆的语义检索，业务表结构不依赖它。
    提前把话说明白，比等到运行期 store.setup() 抛错再回头查要好。
    """
    _print_step(2, "确认 pgvector 扩展")
    engine = create_engine(config.PG_MIGRATION_DSN, poolclass=None, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            installed = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        print(f"    pgvector 就绪，版本 {installed}")
    except Exception as exc:
        print(f"    [!] 创建 vector 扩展失败：{exc}")
        print("        当前账号可能不是超级用户。请用超级用户连到本库执行：")
        print("            CREATE EXTENSION IF NOT EXISTS vector;")
        print("        执行完重新运行本脚本即可。")
    finally:
        engine.dispose()


def run_migrations() -> None:
    """第 3 步：执行 Alembic 迁移到最新版本。"""
    _print_step(3, "执行数据库迁移（alembic upgrade head）")
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(alembic_cfg, "head")
    print("    业务表已就绪：users / tasks / ws_events / files / audit_log / sql_table_allowlist")


async def _run_async_steps(args: argparse.Namespace) -> None:
    """
    把所有异步步骤放在同一个事件循环里顺序执行。

    不能每一步各调一次 run_async：数据库引擎是进程级单例，连接与创建它的循环
    绑定，第二个循环复用上一个循环留下的连接池会直接报 "Event loop is closed"
    （同 app/utils/async_bridge.py 里说明的问题）。
    """
    from app.db.session import dispose_engine

    try:
        if not args.skip_memory:
            await setup_memory_tables()
        if args.seed_allowlist:
            await seed_allowlist()
        if args.admin:
            await create_admin(args.admin, args.password, args.display_name)
    finally:
        # 退出前归还连接，避免留下半开连接
        await dispose_engine()


async def setup_memory_tables() -> None:
    """第 4 步：建 langgraph 的 checkpoint 与 store 表（含向量索引）。"""
    _print_step(4, "初始化短期/长期记忆表")
    from app.services.memory import close_memory, setup_memory

    try:
        await setup_memory()
        print("    checkpoints / store / store_vectors 已就绪")
    finally:
        await close_memory()


async def seed_allowlist() -> None:
    """第 5 步：从业务 MySQL 现有表名播种 SQL 白名单。"""
    _print_step(5, "播种 SQL 表白名单")
    from app.services.sql_guard import refresh_allowlist

    tables = refresh_allowlist(force=True)
    if tables:
        print(f"    白名单共 {len(tables)} 张表：{', '.join(sorted(tables)[:20])}"
              f"{' …' if len(tables) > 20 else ''}")
    else:
        print("    [!] 未取到任何表名。请确认 .env 中的 MYSQL_* 配置可用，"
              "或稍后在管理端手工添加白名单。")


async def create_admin(username: str, password: str, display_name: str | None) -> None:
    """第 6 步：创建（或升级）一个管理员账号。"""
    _print_step(6, "创建管理员账号")
    from sqlalchemy import select

    from app.auth.security import hash_password
    from app.db.models import ROLE_ADMIN, User
    from app.db.session import session_scope

    # 连接池由 _run_async_steps 统一在收尾时释放，这里不做局部清理
    async with session_scope() as session:
        existing = await session.scalar(select(User).where(User.username == username))
        if existing is not None:
            # 已存在则只提权与重置密码，不重复建号
            existing.role = ROLE_ADMIN
            existing.password_hash = hash_password(password)
            existing.is_active = True
            session.add(existing)
            print(f"    账号 {username} 已存在：已重置密码并提升为 admin")
        else:
            session.add(
                User(
                    username=username,
                    display_name=display_name or username,
                    password_hash=hash_password(password),
                    role=ROLE_ADMIN,
                    is_active=True,
                )
            )
            print(f"    管理员 {username} 创建成功")


def main() -> None:
    parser = argparse.ArgumentParser(description="PostgreSQL 初始化脚本")
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        help="跳过 langgraph 记忆表初始化（例如仅想重建业务表）",
    )
    parser.add_argument(
        "--seed-allowlist",
        action="store_true",
        help="从业务 MySQL 播种 SQL 表白名单",
    )
    parser.add_argument("--admin", metavar="USERNAME", help="创建/重置一个管理员账号的用户名")
    parser.add_argument("--password", help="配合 --admin 使用的密码")
    parser.add_argument("--display-name", help="配合 --admin 使用的显示名，默认与用户名相同")
    args = parser.parse_args()

    if args.admin and not args.password:
        parser.error("使用 --admin 时必须同时提供 --password")

    check_connection()
    ensure_vector_extension()
    run_migrations()

    # 统一走 app.utils.event_loop.run：Windows 上必须用 SelectorEventLoop，
    # 否则 psycopg3 异步驱动连不上库（见该模块的说明）。
    # 所有异步步骤合成一次调用，确保全程只有一个事件循环。
    if not args.skip_memory or args.seed_allowlist or args.admin:
        run_async(_run_async_steps(args))

    print("\n初始化完成。接下来：")
    print("    1) 在 .env 中填好 REDIS_URL 与 STORAGE_* 真实配置")
    print("    2) 启动 API：python -m app.api.server")
    print("       （若用 uvicorn 命令行，务必带上 --loop 参数：")
    print("         uvicorn app.api.server:app --loop app.utils.event_loop:selector_loop_factory）")
    print("    3) 另开一个终端启动 worker：arq app.queue.worker.WorkerSettings")


if __name__ == "__main__":
    main()
