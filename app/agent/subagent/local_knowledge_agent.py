"""
本地知识库检索子智能体配置模块

将 app/prompt/prompts.yml 中的 local_kb 配置与本地检索工具组装成
DeepAgents 可识别的字典式子智能体。主智能体后续会根据 description
决定是否把企业内部非结构化文档查询任务分派给它。

本助手替代原先外接 RAGFlow 服务的 knowledge_base_agent，检索对象改为
项目本地 knowledge_base/ 目录下的 PDF 报告，无需部署 RAGFlow 服务。
"""

from app.agent.prompts import sub_agents_content
from app.tools.local_kb_tools import search_local_knowledge

# 本地知识库助手处理内部非结构化文档，与网络搜索助手、数据库查询助手形成互补
# 检索直接落在项目 knowledge_base/ 目录，结果由混合检索（BM25 + 向量）保证召回与语义
local_knowledge_agent = {
    "name": sub_agents_content["local_kb"]["name"],
    "description": sub_agents_content["local_kb"]["description"],
    "system_prompt": sub_agents_content["local_kb"]["system_prompt"],
    "tools": [search_local_knowledge],
}
