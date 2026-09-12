"""
长期记忆巩固与召回

召回（任务开始前）
    用任务问题做语义检索，把该用户过去沉淀的记忆拼成一段提示词注入 system prompt，
    让新会话「记得」用户是谁、关心什么、上次得出过什么结论。

巩固（任务结束后）
    让模型从本次问答中抽取「用户偏好 / 关键事实 / 任务结论」这类可复用信息，
    写入 PostgresStore 的 (user_id,) 命名空间。

命名空间只由 user_id 决定，因此 A 用户的记忆永远检索不到 B 用户的内容。
"""

import json
import re
import uuid
from typing import Any, Optional

from app import config
from app.agent.llm import model
from app.services.audit import AuditAction, record_audit
from app.services.memory import recall_memories, save_memory

_CONSOLIDATE_PROMPT = """你是一个记忆整理助手。请从下面这次「用户提问 + 助手最终答复」中，\
抽取**未来对话可能复用**的信息，最多 {max_items} 条。

只抽取以下三类：
1. preference：用户的稳定偏好、关注领域、表达习惯（例如"关注药品库存周转率"）
2. fact：任务中确认下来的、与具体业务对象绑定的客观事实（例如"药品 A 属于处方药"）
3. conclusion：本次分析得出的、有长期参考价值的结论

不要抽取：一次性的寒暄、与具体问题无关的过程性描述、随时会变的数据快照（如某日销量）。
如果没有任何值得长期记住的信息，返回空数组。

只输出 JSON 数组，不要输出任何解释。格式：
[{{"kind": "preference|fact|conclusion", "text": "一句话描述"}}]

用户提问：
{query}

助手最终答复：
{answer}
"""


def _parse_memories(raw: str, limit: int) -> list[dict[str, str]]:
    """
    从模型输出中解析记忆条目。

    模型有时会把 JSON 包在 ```json ``` 里或加解释文字，这里用正则兜底抽取数组，
    解析失败就当作「没有值得记住的内容」，不影响主流程。
    """
    if not raw:
        return []

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []

    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    memories: list[dict[str, str]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        kind = str(item.get("kind", "fact")).strip() or "fact"
        if kind not in {"preference", "fact", "conclusion"}:
            kind = "fact"
        memories.append({"kind": kind, "text": text})
    return memories


async def build_memory_prompt(
    user_id: uuid.UUID, task_query: str
) -> str:
    """
    召回该用户的长期记忆并格式化成提示词片段。

    :return: 可直接拼进 system_prompt 的文本；没有命中记忆时返回空字符串
    """
    memories = await recall_memories(str(user_id), task_query)
    if not memories:
        return ""

    lines = [f"- （{item['kind']}）{item['text']}" for item in memories]

    await record_audit(
        action=AuditAction.MEMORY_RECALL,
        user_id=user_id,
        resource_type="memory",
        detail={"count": len(memories), "query": task_query[:200]},
    )

    return (
        "\n\n【用户长期记忆】\n"
        "以下是该用户在过去任务中沉淀的信息，请在回答时参考，但不要直接复述：\n"
        + "\n".join(lines)
    )


async def consolidate_task_memory(
    user_id: uuid.UUID,
    task_query: str,
    final_result: str,
) -> list[str]:
    """
    任务结束后抽取长期记忆。

    :return: 本次写入的记忆 key 列表；未开启或没有可用信息时返回空列表
    """
    if not config.MEMORY_CONSOLIDATE_ENABLED:
        return []
    if not final_result or not final_result.strip():
        return []

    prompt = _CONSOLIDATE_PROMPT.format(
        max_items=3,
        query=task_query[:4000],
        answer=final_result[:8000],
    )

    try:
        response = await model.ainvoke(prompt)
    except Exception as exc:
        # 巩固失败只是少沉淀一条记忆，绝不能影响任务本身已经成功的状态
        print(f"[Memory] 记忆巩固调用失败：{exc}")
        return []

    content = getattr(response, "content", "")
    if isinstance(content, list):
        # 部分模型返回结构化 content 列表，这里拼接其中的文本片段
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    memories = _parse_memories(str(content), limit=3)
    if not memories:
        return []

    saved_keys: list[str] = []
    for item in memories:
        key = await save_memory(
            str(user_id),
            item["text"],
            kind=item["kind"],
            extra={"source_query": task_query[:200]},
            key=f"{item['kind']}:{uuid.uuid4()}",
        )
        saved_keys.append(key)

    if saved_keys:
        await record_audit(
            action=AuditAction.MEMORY_WRITE,
            user_id=user_id,
            resource_type="memory",
            detail={"count": len(saved_keys), "items": memories},
        )

    return saved_keys


async def consolidate_safely(
    user_id: uuid.UUID,
    task_query: str,
    final_result: Optional[str],
) -> None:
    """记忆巩固的容错包装：任何异常都只打印，不影响任务状态写回。"""
    try:
        await consolidate_task_memory(user_id, task_query, final_result or "")
    except Exception as exc:  # pragma: no cover - 兜底保护
        print(f"[Memory] 记忆巩固异常：{exc}")
