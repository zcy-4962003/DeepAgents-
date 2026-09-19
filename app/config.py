"""
项目集中配置模块

所有子系统（PostgreSQL / Redis / 对象存储 / JWT / 文件治理 / SQL 安全）的配置
统一在这里从 .env 读取，其它模块只导入本模块的常量，不直接读环境变量。

设计要点：
1. PostgreSQL 的 DSN 优先读 PG_DSN / PG_SYNC_DSN；未配置时用已有的
   POSTGRES_* 变量拼装，保证 .env 只填一份数据库信息即可。
2. Redis 与对象存储（OSS/COS）由用户自行部署，这里只提供占位默认值，
   项目交付后由用户在 .env 中填入真实参数。
3. 路径类配置统一使用绝对路径，避免不同启动目录下行为不一致。
"""

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import find_dotenv, load_dotenv

# find_dotenv 会从当前文件向上查找 .env，兼容脚本、API、arq worker 三种启动入口
load_dotenv(find_dotenv())

# 当前文件位于 app/config.py，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env(key: str, default: str = "") -> str:
    """读取环境变量并去掉首尾空白，避免 .env 中的多余空格污染配置。"""
    value = os.getenv(key)
    return value.strip() if value and value.strip() else default


def _env_int(key: str, default: int) -> int:
    """读取整型环境变量；解析失败时退回默认值，避免启动期直接崩溃。"""
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """读取浮点环境变量；解析失败时退回默认值。"""
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """读取布尔环境变量，接受 1/true/yes/on 等常见写法。"""
    return _env(key, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_list(key: str, default: str = "") -> list[str]:
    """读取逗号分隔的列表配置，自动去掉空项与首尾空格。"""
    raw = _env(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# PostgreSQL：应用表 + 短期记忆(PostgresSaver) + 长期记忆(PostgresStore)
# --------------------------------------------------------------------------- #
_pg_user = _env("POSTGRES_USER", "postgres")
_pg_password = quote(_env("POSTGRES_PASSWORD"), safe="")
_pg_host = _env("POSTGRES_HOST", "localhost")
_pg_port = _env("POSTGRES_PORT", "5432")
_pg_db = _env("POSTGRES_DB", "deepagents")

# asyncpg 驱动给 SQLAlchemy 异步引擎用；psycopg 同步 DSN 给 langgraph 记忆用
PG_DSN = _env(
    "PG_DSN",
    f"postgresql+asyncpg://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{_pg_db}",
)
PG_SYNC_DSN = _env(
    "PG_SYNC_DSN",
    f"postgresql://{_pg_user}:{_pg_password}@{_pg_host}:{_pg_port}/{_pg_db}",
)


def _with_psycopg_driver(dsn: str) -> str:
    """
    给裸 DSN 补上 SQLAlchemy 需要的驱动名。

    PG_SYNC_DSN 是给 langgraph 的 psycopg.connect 用的（`postgresql://` 即可），
    但 SQLAlchemy 看到没有驱动名会去找 psycopg2，而本项目装的是 psycopg3，
    因此 Alembic 用的这份必须显式写成 `postgresql+psycopg://`。
    """
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dsn.startswith(prefix):
            return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://") :]
    return dsn


# Alembic 迁移专用 DSN（同步驱动 psycopg3）
PG_MIGRATION_DSN = _env("PG_MIGRATION_DSN", _with_psycopg_driver(PG_SYNC_DSN))

# 连接池规模：API 进程和 worker 进程都会用，默认值足够单机内部系统使用
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 10)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 20)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)
DB_ECHO = _env_bool("DB_ECHO", False)


# --------------------------------------------------------------------------- #
# Redis：arq 任务队列 + 事件总线(pub/sub)
# --------------------------------------------------------------------------- #
REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
REDIS_EVENT_CHANNEL_PREFIX = _env("REDIS_EVENT_CHANNEL_PREFIX", "events:")
TASK_CANCEL_KEY_PREFIX = _env("TASK_CANCEL_KEY_PREFIX", "task_cancel:")


# --------------------------------------------------------------------------- #
# 对象存储（OSS / COS，S3 兼容协议，统一走 boto3）
# --------------------------------------------------------------------------- #
STORAGE_ENDPOINT = _env("STORAGE_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
STORAGE_BUCKET = _env("STORAGE_BUCKET", "your-bucket")
STORAGE_ACCESS_KEY = _env("STORAGE_ACCESS_KEY", "your-access-key")
STORAGE_SECRET_KEY = _env("STORAGE_SECRET_KEY", "your-secret-key")
STORAGE_REGION = _env("STORAGE_REGION", "cn-hangzhou")
# 部分云厂商需要 path-style 寻址（如 MinIO、部分私有化 OSS）
STORAGE_USE_PATH_STYLE = _env_bool("STORAGE_USE_PATH_STYLE", False)
# 预签名下载地址有效期（秒），默认 10 分钟
STORAGE_PRESIGN_EXPIRE_SECONDS = _env_int("STORAGE_PRESIGN_EXPIRE_SECONDS", 600)
# 上传/下载连接超时与重试，避免对象存储抖动时长时间挂起
STORAGE_CONNECT_TIMEOUT = _env_int("STORAGE_CONNECT_TIMEOUT", 10)
STORAGE_READ_TIMEOUT = _env_int("STORAGE_READ_TIMEOUT", 60)
STORAGE_MAX_RETRIES = _env_int("STORAGE_MAX_RETRIES", 3)


# --------------------------------------------------------------------------- #
# JWT 鉴权
# --------------------------------------------------------------------------- #
JWT_SECRET = _env("JWT_SECRET", "please-change-me-in-production")
JWT_ALGORITHM = _env("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = _env_int("JWT_EXPIRE_MINUTES", 1440)
# 是否开放自助注册；内部系统默认开放，管理员可关闭后改为手工建号
ALLOW_SELF_REGISTER = _env_bool("ALLOW_SELF_REGISTER", True)
# 允许跨域的前端地址。带 Authorization 头时浏览器禁止 allow_origins=["*"]，
# 因此这里必须列举具体来源；留空表示仅同源部署。
CORS_ORIGINS = _env_list(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
)


# --------------------------------------------------------------------------- #
# 文件治理
# --------------------------------------------------------------------------- #
FILE_MAX_SIZE_MB = _env_int("FILE_MAX_SIZE_MB", 50)
FILE_MAX_SIZE_BYTES = FILE_MAX_SIZE_MB * 1024 * 1024
FILE_ALLOWED_EXTS = _env_list(
    "FILE_ALLOWED_EXTS",
    ".md,.txt,.pdf,.docx,.xlsx,.xls,.csv,.png,.jpg,.jpeg",
)
# 允许通过 MIME 嗅探识别的类型前缀，扩展名白名单之外的兜底校验
FILE_ALLOWED_MIME_PREFIXES = _env_list(
    "FILE_ALLOWED_MIME_PREFIXES",
    "text/,image/,application/pdf,application/msword,"
    "application/vnd.openxmlformats-officedocument,application/vnd.ms-excel",
)
# 生成文件在对象存储中的保留天数，到期由定时任务清理
FILE_RETENTION_DAYS = _env_int("FILE_RETENTION_DAYS", 30)
# 上传文件的保留天数（比生成文件更短，附件通常只在任务期间需要）
UPLOAD_RETENTION_DAYS = _env_int("UPLOAD_RETENTION_DAYS", 7)


# --------------------------------------------------------------------------- #
# SQL 安全（业务 MySQL 只读代理）
# --------------------------------------------------------------------------- #
SQL_QUERY_TIMEOUT_MS = _env_int("SQL_QUERY_TIMEOUT_MS", 5000)
SQL_MAX_ROWS = _env_int("SQL_MAX_ROWS", 500)
# 白名单为空时是否自动用业务库现有表名播种，方便首次部署
SQL_ALLOWLIST_AUTO_SEED = _env_bool("SQL_ALLOWLIST_AUTO_SEED", True)
# 白名单在进程内的缓存时长（秒），避免每次查询都读库
SQL_ALLOWLIST_CACHE_SECONDS = _env_int("SQL_ALLOWLIST_CACHE_SECONDS", 60)


# --------------------------------------------------------------------------- #
# 任务队列（arq）
# --------------------------------------------------------------------------- #
TASK_MAX_ATTEMPTS = _env_int("TASK_MAX_ATTEMPTS", 3)
# 单任务最长执行时间，超时后 arq 会取消任务并触发重试/置失败
TASK_JOB_TIMEOUT_SECONDS = _env_int("TASK_JOB_TIMEOUT_SECONDS", 900)
# worker 并发处理的任务数，限制同时运行的 LLM/DB 压力
TASK_MAX_JOBS = _env_int("TASK_MAX_JOBS", 2)
# 重试退避基数：第 n 次重试等待 retry_base * 2^(n-1) 秒
TASK_RETRY_BASE_SECONDS = _env_int("TASK_RETRY_BASE_SECONDS", 5)
# 取消信号的轮询间隔，决定点“取消”后多久真正中断
TASK_CANCEL_POLL_SECONDS = _env_float("TASK_CANCEL_POLL_SECONDS", 2.0)


# --------------------------------------------------------------------------- #
# 长期记忆
# --------------------------------------------------------------------------- #
# 记忆向量化复用 .env 中已有的 embedding 模型，维度需与模型输出一致
EMBEDDING_MODEL = _env("LLM_QWEN_EMBEDDING", "text-embedding-v3")
EMBEDDING_DIMS = _env_int("EMBEDDING_DIMS", 1024)
# 任务开始前召回的历史记忆条数上限
MEMORY_RECALL_LIMIT = _env_int("MEMORY_RECALL_LIMIT", 5)
# 是否开启任务结束后的记忆巩固（会额外调用一次 LLM，可按需关闭）
MEMORY_CONSOLIDATE_ENABLED = _env_bool("MEMORY_CONSOLIDATE_ENABLED", True)


# --------------------------------------------------------------------------- #
# 运行时目录（会话工作区，仅作 Agent 读写文件的临时空间，非持久存储）
# --------------------------------------------------------------------------- #
OUTPUT_DIR = PROJECT_ROOT / "output"
UPDATED_DIR = PROJECT_ROOT / "updated"


def ensure_runtime_dirs() -> None:
    """确保运行期需要的本地目录存在；对象存储才是文件持久层。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    UPDATED_DIR.mkdir(parents=True, exist_ok=True)
