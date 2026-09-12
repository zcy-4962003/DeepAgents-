"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为队列 worker 调用的统一入口。

与旧版本的关键差别：
1. 短期记忆从 InMemorySaver 换成 AsyncPostgresSaver —— 进程重启后同一会话仍可续聊；
2. 挂载 PostgresStore 作为长期记忆后端，命名空间固定为 (user_id,) 实现用户隔离；
3. 执行前召回该用户的长期记忆注入提示词，执行后抽取新记忆回写；
4. 附件从对象存储拉取，产物在任务结束后回传对象存储。
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage

from app import config
from app.agent.llm import model
from app.agent.memory_consolidator import build_memory_prompt
from app.agent.prompts import main_agent_content
from app.agent.subagent.database_query_agent import database_query_agent
from app.agent.subagent.local_knowledge_agent import local_knowledge_agent
from app.agent.subagent.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
    set_user_context,
)
from app.api.monitor import monitor

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.memory_tools import remember_about_user
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tools import read_file_content

# 会话工作目录在 config.OUTPUT_DIR（<项目根>/output）之下，因此算相对路径必须以
# 项目根为基准。这里不能写 Path(__file__).parents[1]：本文件在 app/agent/ 下，
# 那拿到的是 app 目录，session_dir.relative_to(app) 会直接抛 ValueError
# （'...\output\session_xxx' is not in the subpath of '...\app'），任务必然失败。
project_root_path = config.PROJECT_ROOT

# Agent 是重量级对象：构造时需要 checkpointer / store 实例，而这两者依赖事件循环，
# 因此不能在导入期创建，改为首次执行任务时惰性构建并复用。
_agent: Any = None
_agent_lock: Optional[asyncio.Lock] = None


async def get_main_agent() -> Any:
    """
    获取（惰性创建）主智能体单例。

    进程内复用同一个 compiled graph，避免每个任务重复构建 Agent 与中间件。
    """
    global _agent, _agent_lock

    if _agent is not None:
        return _agent

    if _agent_lock is None:
        _agent_lock = asyncio.Lock()

    async with _agent_lock:
        if _agent is not None:
            return _agent

        # 记忆后端的连接池和建表都在这里完成（幂等）
        from app.services.memory import get_checkpointer, get_store

        checkpointer = await get_checkpointer()
        store = await get_store()

        # 主智能体是调度中心：
        # 1. tools 只放最终交付相关的文件工具与显式记忆工具
        # 2. subagents 放网络、数据库、本地知识库三类信息获取助手
        # 3. checkpointer 保存短期记忆，store 提供跨会话的长期记忆
        _agent = create_deep_agent(
            model=model,
            system_prompt=main_agent_content["system_prompt"],
            tools=[
                generate_markdown,
                convert_md_to_pdf,
                read_file_content,
                remember_about_user,
            ],
            checkpointer=checkpointer,
            store=store,
            subagents=[
                database_query_agent,
                network_search_agent,
                local_knowledge_agent,
            ],
        )
        return _agent


async def prepare_session_dir(
    session_id: str, user_id: uuid.UUID
) -> tuple[Path, list[str]]:
    """
    准备本次任务的本地工作目录，并把上传附件从对象存储拉回来。

    本地目录只是 Agent 运行期的临时空间：附件用完即弃，产物在任务结束后上传到
    对象存储。这样即使换台机器跑 worker，任务依然可复现。
    """
    from app.db.session import session_scope
    from app.services.file_governance import download_task_uploads

    session_dir = config.OUTPUT_DIR / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    landed: list[str] = []
    try:
        async with session_scope() as session:
            landed = await download_task_uploads(
                session, uuid.UUID(str(session_id)), session_dir
            )
    except Exception as exc:
        # 附件下载失败不致命：任务仍然可以基于网络/数据库/知识库完成
        print(f"[MainAgent] 附件下载失败，本次任务按无附件处理：{exc}")

    return session_dir, landed


def _build_path_instruction(
    relative_session_dir: str, uploaded_files: list[str]
) -> str:
    """拼装工作环境指令，把工作目录和附件清单明确告诉模型。"""
    uploaded_info = ""
    if uploaded_files:
        uploaded_info = (
            "\n    [已上传文件] 已加载到工作目录:\n"
            + "\n".join(f"    - {name}" for name in uploaded_files)
            + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
        )

    return f"""
    【工作环境指令】
    工作目录: {relative_session_dir}
    {uploaded_info}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """


def _extract_final_result(chunks: dict[str, Any], node_name: str) -> Optional[str]:
    """
    从流式片段中判断本轮是否产生了可直接反馈给用户的最终文本。

    模型没有继续调用工具时，最新一条 AI 消息的 content 就是本轮结果。
    """
    state = chunks.get(node_name)
    if not state or "messages" not in state:
        return None

    messages = state["messages"]
    if not messages or not isinstance(messages, list):
        return None

    last_msg = messages[-1]
    if not isinstance(last_msg, AIMessage):
        return None
    if last_msg.tool_calls:
        return None
    content = last_msg.content
    if not content:
        return None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


async def run_deep_agent(
    task_query: str,
    session_id: str,
    user_id: uuid.UUID,
) -> str:
    """
    异步流式执行主智能体。

    :param task_query: 用户提交的原始任务问题
    :param session_id: 任务 ID，同时用作 thread_id（短期记忆的隔离键）
    :param user_id: 任务所属用户，用于长期记忆命名空间与事件归属
    :return: 本轮最终答复文本（供 worker 写入 tasks.result）
    """
    print(f"[MainAgent] 开始执行会话，session_id={session_id}, user_id={user_id}")

    session_dir, uploaded_files = await prepare_session_dir(session_id, user_id)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(
        session_dir.relative_to(project_root_path)
    ).replace("\\", "/")

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录、任务 ID 和用户 ID
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(session_id)
    user_id_token = set_user_context(str(user_id))

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    final_result = ""

    try:
        agent = await get_main_agent()

        # 长期记忆召回：用户级命名空间，(user_id,) 保证跨用户互不可见
        memory_prompt = ""
        try:
            memory_prompt = await build_memory_prompt(user_id, task_query)
        except Exception as exc:
            print(f"[MainAgent] 长期记忆召回失败，本次不带记忆执行：{exc}")

        path_instruction = _build_path_instruction(
            relative_session_dir_str, uploaded_files
        )

        # 短期记忆按 thread_id 隔离；同一 session_id 多次执行会复用同一条对话上下文
        config_dict = {"configurable": {"thread_id": session_id}}

        user_message = f"{task_query}\n{path_instruction}{memory_prompt}"

        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": user_message}]},
            config=config_dict,
        ):
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
                    last_msg = messages[-1]
                    if node_name == "model" and isinstance(last_msg, AIMessage):
                        if last_msg.tool_calls:
                            # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                            for tool_call in last_msg.tool_calls:
                                if tool_call["name"] == "task":
                                    monitor.report_assistant(
                                        tool_call["args"].get("subagent_type", "未知"),
                                        {
                                            "description": tool_call["args"].get(
                                                "description", ""
                                            )
                                        },
                                    )
                        else:
                            # 模型不再调用工具，说明本轮已产出可直接反馈给用户的文本
                            text = _extract_final_result(chunk, node_name)
                            if text:
                                final_result = text
                                print(f"[MainAgent] 本轮结果：{text[:100]}")
                                monitor.report_task_result(text)

    except asyncio.CancelledError:
        monitor.report_task_cancelled()
        raise
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件
        monitor.report_error(f"执行主智能体发生异常：{str(e)}")
        raise
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或用户信息
        reset_session_context(session_dir_token, session_id_token, user_id_token)

    return final_result


if __name__ == "__main__":
    # 本地调试入口：直接运行需先启动 Redis/PostgreSQL，或改用无需记忆的脚本测试
    asyncio.run(
        run_deep_agent(
            "从网络查询机器人信息，并生成Markdown文件",
            "test_session_001",
            uuid.uuid4(),
        )
    )
