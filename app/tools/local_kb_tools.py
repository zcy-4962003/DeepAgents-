"""
本地知识库检索工具

封装给「本地知识库检索助手」使用的 LangChain 工具，底层调用
app.knowledge.local_retriever 的混合检索（BM25 粗召回 + 向量精排），
检索对象是项目 knowledge_base/ 目录下的 PDF 报告。
"""

from langchain_core.tools import tool

from app.api.monitor import monitor
from app.knowledge.local_retriever import retriever


@tool
def search_local_knowledge(query: str, k: int = 5) -> str:
    """
    在本地知识库（knowledge_base 目录下的 PDF 报告）中检索与问题相关的片段

    作用：从企业内部私有文档（研报、白皮书、政策报告等）中检索语义相关的原文，
    作为回答问题的依据。返回结果会带上来源文件名和页码。

    使用建议：
    1. 问题应聚焦用户原始需求，一次查询一个明确主题，不要一次混入多个无关问题。
    2. 复杂问题可从多个角度分别检索（每次一个角度），合并多段结果后再综合。
    3. 检索结果只是原文片段，需要你结合上下文判断是否真正回答了问题。

    :param query: 要检索的问题或主题
    :param k: 返回的片段数量，默认 5，最多不超过 10
    :return: 带来源与页码的检索片段；知识库为空或没有匹配时返回中文提示
    """
    monitor.report_tool(
        tool_name="本地知识库检索工具：search_local_knowledge",
        args={"query": query, "k": k},
    )

    k = max(1, min(int(k), 10))
    return retriever.search(query, k=k)
