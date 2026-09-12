"""
Windows 事件循环适配

问题背景
--------
langgraph 的 AsyncPostgresSaver / AsyncPostgresStore 底层用 psycopg3 的异步驱动，
而 psycopg3 的异步模式依赖 `loop.add_reader()` 这类接口，Windows 默认的
ProactorEventLoop 并不提供：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

这个限制会同时命中三个入口——API 进程、arq worker、初始化脚本，所以必须在
进程启动的最早期统一换掉事件循环，而不是在各个调用点打补丁。

本模块提供两个互补的入口
------------------------
1. `configure_event_loop()`：设置全局事件循环策略。`asyncio.run()` 在未显式指定
   循环时会采用它，因此对 arq worker 和各类脚本生效。
2. `selector_loop_factory()`：一个显式的循环工厂。uvicorn 0.36+ 是用
   `asyncio_run(..., loop_factory=...)` 直接构造循环的，**会绕过全局策略**，
   所以必须把本函数传给它的 `loop` 参数（见 app/api/server.py 的 `__main__`）。

为什么切到 Selector 是安全的
----------------------------
Windows 上 SelectorEventLoop 不支持 `asyncio.create_subprocess_exec`。本项目全程
没有使用它：deepagents 的文件后端用的是阻塞式 `subprocess.run`（在
backends/filesystem.py），属于普通同步调用，与事件循环类型无关。
uvicorn 自己在 `--reload`/`--workers` 模式下也会给 Windows 选 SelectorEventLoop，
所以这条路径是 uvicorn 官方支持并测试过的。
"""

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any, Optional

IS_WINDOWS = sys.platform == "win32"


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """
    创建一个基于 select 的事件循环。

    Windows 上显式指定 `SelectSelector`，与 psycopg 报错信息中给出的建议一致，
    避免依赖 `DefaultSelector` 在未来的实现变化。

    用法（uvicorn 命令行需要传点号路径）：
        uvicorn app.api.server:app --loop app.utils.event_loop:selector_loop_factory
    """
    if IS_WINDOWS:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.SelectorEventLoop()


def configure_event_loop() -> None:
    """
    把全局事件循环策略切成 Selector（仅 Windows 生效），幂等。

    由 app/__init__.py 在包被导入时调用，因此任何 `import app.*` 的入口
    （脚本、API、worker）都会在创建循环之前完成设置。
    """
    if not IS_WINDOWS:
        return

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:
        # 非 Windows 实现或未来版本移除了该策略：此时只能依赖显式的循环工厂
        return

    if isinstance(asyncio.get_event_loop_policy(), policy_cls):
        return

    asyncio.set_event_loop_policy(policy_cls())


def run(coro: Coroutine[Any, Any, Any], *, debug: Optional[bool] = None) -> Any:
    """
    脚本入口统一使用的 `asyncio.run` 包装。

    与直接调用 `asyncio.run` 的区别是显式传入循环工厂，不依赖全局策略是否曾被
    其它库改写——脚本往往还会 import 各种第三方库，这一点值得防。
    """
    if IS_WINDOWS:
        return asyncio.run(coro, debug=debug, loop_factory=selector_loop_factory)
    return asyncio.run(coro, debug=debug)
