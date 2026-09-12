"""
长期记忆写入工具

主智能体在对话中察觉到「用户明确表达了需要长期记住的偏好或事实」时，可以直接
调用本工具落库，不必等任务结束后的自动巩固。

写入目标仍是 `(user_id,)` 命名空间，因此不会跨用户泄漏。
"""

import uuid

from langchain_core.tools import tool

from app.api.context import get_user_context
from app.api.monitor import monitor
from app.services.audit import AuditAction, record_audit_sync
from app.services.memory import save_memory
from app.utils.async_bridge import run_soon

_VALID_KINDS = {"preference", "fact", "conclusion"}


@tool
def remember_about_user(text: str, kind: str = "preference") -> str:
    """
    记住一条关于当前用户的长期信息

    适用场景：用户明确要求「记住…」，或表达了稳定的偏好、习惯、长期约束
    （例如「以后报告都用表格呈现」「我只关心华东区的数据」）。

    不适用场景：一次性的提问内容、随时间变化的数据快照、与用户本人无关的通用知识。

    :param text: 要记住的一句话描述，尽量具体、自包含
    :param kind: 记忆类别，preference(偏好) / fact(事实) / conclusion(结论)
    :return: 写入结果说明
    """
    monitor.report_tool(
        tool_name="长期记忆写入工具：remember_about_user",
        args={"text": text, "kind": kind},
    )

    raw_user_id = get_user_context()
    if not raw_user_id:
        return "当前没有用户上下文，无法写入长期记忆。"

    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError):
        return "当前用户标识无效，无法写入长期记忆。"

    clean_text = (text or "").strip()
    if not clean_text:
        return "记忆内容为空，未写入。"

    safe_kind = kind if kind in _VALID_KINDS else "preference"
    key = f"{safe_kind}:{uuid.uuid4()}"

    # 写库是异步的；这里把协程调度回当前事件循环，不在同步工具里阻塞模型流
    run_soon(save_memory(str(user_id), clean_text, kind=safe_kind, key=key))

    record_audit_sync(
        action=AuditAction.MEMORY_WRITE,
        user_id=user_id,
        resource_type="memory",
        resource_id=key,
        detail={"kind": safe_kind, "text": clean_text, "source": "tool"},
    )

    return f"已记住：{clean_text}"
