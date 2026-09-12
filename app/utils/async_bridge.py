"""
同步/异步桥接工具

Agent 的工具函数是同步的（LangChain `@tool`），但它们需要触发的副作用
（写审计、落事件、存记忆）全是异步的。如果直接在同步函数里 `asyncio.run`，
会因为「事件循环已在运行」而报错；如果新开线程跑一个新的循环，又会和
主循环里创建的连接池（psycopg / asyncpg）冲突。

正确做法只有一个：把协程投递回**当前正在运行的那个事件循环**，让它异步执行。
副作用失败不影响主流程，因此不需要等待结果。

脚本模式为什么要复用同一个循环
--------------------------------
asyncpg / psycopg 的连接与「创建它的那个事件循环」绑定，而数据库引擎是进程级
单例（见 app/db/session.py 的 `_engine`）。如果每次调用都 `asyncio.run()` 新建
循环，第一次调用会把连接池绑在 loop-1 上，loop-1 关闭后，第二次调用在 loop-2
里复用这些连接就会报：

    got Future attached to a different loop / Event loop is closed

结果就是审计、事件这类副作用从第二次调用起全部静默丢失。所以脚本模式下改用
一个进程内常驻的循环反复 `run_until_complete`，让连接池始终只属于一个循环。

为什么必须登记「进程主循环」
--------------------------------
上面那条规则在 worker 里同样致命，而且更容易踩中：LangChain 把同步 `@tool`
函数丢进线程池执行，那些线程里 `asyncio.get_running_loop()` 会抛 RuntimeError。
此时进程**明明有主循环，只是不在当前线程**——若照脚本模式再开一个常驻循环，
两个循环就会抢同一个连接池：

    [EventStore] 事件落库失败 task=...: got Future ... attached to a different loop
    [Worker] 产物归档失败（不影响任务成功状态）：... attached to a different loop

后果比脚本模式更隐蔽：事件与产物归档都在 `except Exception` 里被吞掉，
任务状态仍是 success，但**用户拿不到产物、刷新后看不到历史事件**。
因此 API / worker 启动时必须用 `set_main_loop()` 登记主循环，之后线程里的调用
一律用 `run_coroutine_threadsafe` 投回去，绝不另起循环。
"""

import asyncio
import atexit
from typing import Coroutine, Optional

# 脚本模式（进程内没有运行中的循环）下复用的常驻循环
_script_loop: Optional[asyncio.AbstractEventLoop] = None

# 应用进程（API / worker）的主循环，启动时由 set_main_loop() 登记
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """
    登记应用主循环，供工作线程把协程投递回来。

    必须在 API / worker 启动时调用一次（此时正处于主循环内）。不调用的话，
    工作线程只能退回脚本模式的常驻循环，就会重新引入「两个循环抢连接池」。
    """
    global _main_loop
    _main_loop = loop or asyncio.get_running_loop()


def _get_script_loop() -> asyncio.AbstractEventLoop:
    """取得脚本模式的常驻循环，必要时创建。"""
    global _script_loop
    if _script_loop is None or _script_loop.is_closed():
        _script_loop = asyncio.new_event_loop()
        # 一并设为当前循环，兼容第三方库里调用 get_event_loop() 的代码
        asyncio.set_event_loop(_script_loop)
    return _script_loop


@atexit.register
def _cleanup_script_loop() -> None:
    """
    进程退出前归还连接池并关闭常驻循环。

    不先 dispose 引擎就直接 close 循环，会让 asyncpg 在 GC 时报
    「连接从未关闭」的噪音；顺序反了又会用到已关闭的循环。
    """
    global _script_loop
    if _script_loop is None or _script_loop.is_closed():
        _script_loop = None
        return

    try:
        # 延迟导入：避免 utils 层在导入期依赖 db 层
        from app.db.session import dispose_engine

        _script_loop.run_until_complete(dispose_engine())
    except Exception:
        # 退出阶段的清理失败不值得打扰调用方
        pass
    finally:
        _script_loop.close()
        _script_loop = None


def run_soon(coroutine: Coroutine) -> None:
    """
    把协程调度到**拥有连接池的那个事件循环**上执行。

    三种情形，按优先级判断：
    1. 当前线程就在跑循环（API 请求 / worker 任务体 / 脚本）：`create_task`，调用方立即返回；
    2. 当前线程没有循环，但进程主循环已登记（同步工具跑在线程池里）：用
       `run_coroutine_threadsafe` 投回主循环，同样是「不等结果」；
    3. 都没有（纯脚本、单测）：退到常驻脚本循环同步跑完。

    为什么情形 2 不能落进情形 3：见模块文档「为什么必须登记进程主循环」。
    一旦另起循环，连接池会被绑到第二个循环上，症状是副作用静默丢失。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        loop.create_task(coroutine)
        return

    if _main_loop is not None and not _main_loop.is_closed() and _main_loop.is_running():
        # 跨线程投递：返回的 concurrent.futures.Future 不等待，副作用异常由
        # 各调用方（record_event / write_audit 等）自行捕获打印，这里不重复处理。
        asyncio.run_coroutine_threadsafe(coroutine, _main_loop)
        return

    _get_script_loop().run_until_complete(coroutine)
