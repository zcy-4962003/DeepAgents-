"""
数据库引擎与会话管理

提供两种使用方式：
1. FastAPI 依赖注入：`session: AsyncSession = Depends(get_session)`
2. 独立进程/后台协程：`async with session_scope() as session:`

引擎是模块级懒加载单例，API 进程和 arq worker 进程各自持有一份连接池。
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import config

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """
    获取（惰性创建）全局异步引擎。

    第一次调用时才真正建连接池，这样导入模块本身不会去连数据库，
    便于脚本在数据库未就绪时也能被安全导入。
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            config.PG_DSN,
            echo=config.DB_ECHO,
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_recycle=config.DB_POOL_RECYCLE,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取（惰性创建）会话工厂，所有会话都关闭自动提交、由代码显式 commit。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    FastAPI 依赖：每个请求一个会话，请求结束自动关闭。

    注意这里不做自动 commit，写操作由各接口/服务显式提交，
    避免只读请求意外开启事务。
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    独立协程使用的会话上下文：正常结束自动提交，异常自动回滚。

    适合事件落库、审计写入这类「一次性写操作」，调用方无需关心事务边界。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """关闭连接池；进程退出（FastAPI shutdown / worker on_shutdown）时调用。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
