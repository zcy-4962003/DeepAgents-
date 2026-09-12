"""
多 Agent 电商研搜系统

包导入时立刻完成 Windows 事件循环适配：langgraph 的记忆后端依赖 psycopg3 异步驱动，
它无法在 Windows 默认的 ProactorEventLoop 上工作。这一步必须发生在任何事件循环被
创建之前，而所有入口（API 进程、arq worker、脚本）都会先 import 本包，因此这里是
最可靠的位置。详见 app/utils/event_loop.py。
"""

from app.utils.event_loop import configure_event_loop

configure_event_loop()
