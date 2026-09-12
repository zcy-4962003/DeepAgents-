"""
本地知识库混合检索引擎

从项目根目录下的 knowledge_base/ 目录递归加载 PDF 报告，抽文本 -> 按页切块 ->
构建 BM25 关键词索引与向量索引。检索时先用 BM25 粗召回，再用向量相似度精排，
返回带来源（文件 + 页码）的 Top-K 片段。

向量索引使用本地向量数据库 ChromaDB 持久化：分块的文本、来源元数据和向量都存到
knowledge_base/.kb_cache/ 下的 Chroma 集合中，首次构建后直接复用；knowledge_base
里的文件发生变化时（名称、大小或修改时间不一致）会自动重建。BM25 关键词索引体量小，
每次启动时基于分块文本在内存中即时构建，不落盘。
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
from dotenv import find_dotenv, load_dotenv
from jieba import cut
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from rank_bm25 import BM25Okapi

# 当前文件位于 app/knowledge/local_retriever.py，parents[2] 即项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_BASE_DIR = _PROJECT_ROOT / "knowledge_base"
CACHE_DIR = KNOWLEDGE_BASE_DIR / ".kb_cache"

_COLLECTION_NAME = "knowledge_base"

_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 120
_EMBED_BATCH_SIZE = 10


def _tokenize(text: str) -> list[str]:
    """用 jieba 把中文文本切成词，作为 BM25 的检索单元。"""
    return [tok for tok in cut(text) if tok.strip()]


def _cosine_similarity(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """计算查询向量与一组向量的余弦相似度（矩阵按行存放向量）。"""
    query_vec = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
    return matrix @ query_vec


class LocalKnowledgeRetriever:
    """本地知识库的加载、索引与混合检索。"""

    def __init__(self) -> None:
        self._chunks: list[dict[str, Any]] = []
        self._bm25: Optional[BM25Okapi] = None
        self._embedding_model: Any = None
        self._client: Any = None
        self._collection: Any = None
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #
    def search(self, query: str, k: int = 5, recall_k: int = 30) -> str:
        """
        混合检索：BM25 粗召回 recall_k 个候选，再用向量相似度精排出 Top-K。

        :param query: 用户/子智能体的问题
        :param k: 最终返回的片段数量
        :param recall_k: BM25 粗召回的候选数量
        :return: 拼接好的检索结果文本（含来源与页码），知识库为空时返回提示
        """
        self._ensure_loaded()

        if not self._chunks:
            return "本地知识库为空，没有可检索的文档。"

        tokens = _tokenize(query)
        bm25_scores = self._bm25.get_scores(tokens) if self._bm25 else np.zeros(len(self._chunks))

        # 粗召回：只保留 BM25 得分最高的 recall_k 个候选做精排
        recall_size = min(recall_k, len(self._chunks))
        recall_indices = np.argsort(bm25_scores)[::-1][:recall_size]

        # 精排：向量可用时按余弦相似度重排；否则退化为纯 BM25 结果
        try:
            top_indices = self._rerank_by_vector(query, recall_indices, k)
        except Exception as exc:
            print(f"[KnowledgeBase] 向量精排失败，退化为 BM25：{exc}")
            top_indices = recall_indices[:k]

        return self._format_results(top_indices)

    # ------------------------------------------------------------------ #
    # 索引加载 / 构建
    # ------------------------------------------------------------------ #
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        self._init_client()

        if self._load_cache():
            self._build_bm25()
            self._loaded = True
            print(f"[KnowledgeBase] 已从 ChromaDB 加载 {len(self._chunks)} 个分块")
            return

        print("[KnowledgeBase] 首次构建索引（抽取 PDF + 向量化），可能需要一些时间...")
        self._build_index()
        self._save_manifest()
        self._loaded = True
        print(f"[KnowledgeBase] 索引构建完成，共 {len(self._chunks)} 个分块")

    def _init_client(self) -> None:
        """初始化 ChromaDB 持久化客户端与集合（复用已有，否则新建）。"""
        import chromadb

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(CACHE_DIR))

        # 先尝试读取已存在的集合，避免 create 时因 metadata 不一致而告警
        try:
            self._collection = self._client.get_collection(_COLLECTION_NAME)
        except Exception:
            self._collection = self._client.create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )

    def _get_embedding_model(self) -> Any:
        """懒加载 .env 配置的 embedding 模型，构建索引和精排共用同一实例。"""
        if self._embedding_model is None:
            load_dotenv(find_dotenv())
            from langchain_openai import OpenAIEmbeddings

            # 关闭 tiktoken 预检查：国内环境下载 cl100k_base 编码常失败，且非必需
            self._embedding_model = OpenAIEmbeddings(
                model=os.getenv("LLM_QWEN_EMBEDDING", "text-embedding-v3"),
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                openai_api_base=os.getenv("OPENAI_BASE_URL"),
                tiktoken_enabled=False,
                check_embedding_ctx_length=False,
            )
        return self._embedding_model

    def _build_index(self) -> None:
        """从零构建：抽 PDF 文本 -> 切块 -> BM25 + 向量写入 ChromaDB。"""
        self._chunks = self._extract_chunks()
        self._build_bm25()

        self._reset_collection()

        if not self._chunks:
            return

        vectors = self._embed_chunks([c["text"] for c in self._chunks])
        self._collection.add(
            ids=[str(i) for i in range(len(self._chunks))],
            documents=[c["text"] for c in self._chunks],
            metadatas=[
                {"source": c["source"], "page": c["page"]} for c in self._chunks
            ],
            embeddings=vectors.tolist(),
        )

    def _build_bm25(self) -> None:
        tokenized = [_tokenize(c["text"]) for c in self._chunks]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    def _reset_collection(self) -> None:
        """清空旧集合，保证重建后集合内容与当前 PDF 完全一致。"""
        try:
            self._client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
        self._collection = self._client.create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def _extract_chunks(self) -> list[dict[str, Any]]:
        """遍历 knowledge_base 下所有 PDF，逐页抽取文本并切块。"""
        documents: list[Document] = []
        for pdf_path in sorted(KNOWLEDGE_BASE_DIR.rglob("*.pdf")):
            reader = PdfReader(str(pdf_path))
            # 统一成正斜杠，避免 Windows 反斜杠在前后端展示时不一致
            rel_path = str(pdf_path.relative_to(KNOWLEDGE_BASE_DIR)).replace("\\", "/")
            for page_idx, page in enumerate(reader.pages):
                text = (page.extract_text() or "").strip()
                if not text:
                    continue
                # 一页对应一个 Document，切块后 metadata 能保留来源与页码
                documents.append(
                    Document(
                        page_content=text,
                        metadata={"source": rel_path, "page": page_idx + 1},
                    )
                )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=_CHUNK_SIZE,
            chunk_overlap=_CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
        )
        chunks = []
        for doc in splitter.split_documents(documents):
            text = doc.page_content.strip()
            # 过滤掉封面、页眉之类几乎无信息的分块
            if len(text) < 20:
                continue
            chunks.append(
                {
                    "text": text,
                    "source": doc.metadata["source"],
                    "page": doc.metadata["page"],
                }
            )
        return chunks

    def _embed_chunks(self, texts: list[str]) -> np.ndarray:
        """用 .env 配置的 embedding 模型批量向量化所有分块。"""
        model = self._get_embedding_model()

        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            vectors.extend(model.embed_documents(batch))
            print(f"[KnowledgeBase] 已向量化 {min(start + _EMBED_BATCH_SIZE, len(texts))}/{len(texts)}")
        return np.array(vectors, dtype=np.float32)

    # ------------------------------------------------------------------ #
    # 检索排序 / 结果格式化
    # ------------------------------------------------------------------ #
    def _rerank_by_vector(
        self, query: str, recall_indices: np.ndarray, k: int
    ) -> np.ndarray:
        if self._collection is None or not len(recall_indices):
            return recall_indices[:k]

        cand = np.asarray(recall_indices, dtype=int)
        ids = [str(int(i)) for i in cand]

        # 从 ChromaDB 按 id 取回候选分块的向量，重建候选顺序后算余弦相似度
        data = self._collection.get(ids=ids, include=["embeddings"])
        embeddings = data.get("embeddings")
        result_ids = data.get("ids") or []
        if embeddings is None or len(result_ids) == 0:
            return cand[:k]
        id2vec = {
            cid: np.asarray(vec, dtype=np.float32)
            for cid, vec in zip(result_ids, embeddings)
        }

        matrix = np.stack([id2vec[str(int(i))] for i in cand])
        query_vec = np.array(
            self._get_embedding_model().embed_query(query), dtype=np.float32
        )
        sims = _cosine_similarity(query_vec, matrix)
        order = np.argsort(sims)[::-1][:k]
        return cand[order]

    def _format_results(self, indices: np.ndarray) -> str:
        blocks = []
        for rank, idx in enumerate(indices, start=1):
            chunk = self._chunks[int(idx)]
            blocks.append(
                f"【片段{rank}】来源：{chunk['source']}（第{chunk['page']}页）\n{chunk['text']}"
            )
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ #
    # 缓存读写（manifest 仅用于失效判断；正文与向量由 ChromaDB 持久化）
    # ------------------------------------------------------------------ #
    def _manifest(self) -> list[dict[str, Any]]:
        """当前 knowledge_base 下所有 PDF 的指纹，用于判断缓存是否失效。"""
        manifest = []
        for pdf_path in sorted(KNOWLEDGE_BASE_DIR.rglob("*.pdf")):
            stat = pdf_path.stat()
            manifest.append(
                {
                    "source": str(pdf_path.relative_to(KNOWLEDGE_BASE_DIR)).replace("\\", "/"),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime_ns,
                }
            )
        return manifest

    def _load_cache(self) -> bool:
        """manifest 未变化且 ChromaDB 集合非空时，从集合恢复分块文本。"""
        manifest_path = CACHE_DIR / "manifest.json"
        if not manifest_path.exists():
            return False

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                cached_manifest = json.load(f)
            if cached_manifest != self._manifest():
                return False

            data = self._collection.get(include=["documents", "metadatas"])
            ids = data.get("ids")
            if ids is None or len(ids) == 0:
                return False

            # ChromaDB 返回无序，按 id（即分块序号）排序恢复原始顺序
            items = sorted(
                zip(ids, data["documents"], data["metadatas"]),
                key=lambda t: int(t[0]),
            )
            self._chunks = [
                {"text": doc, "source": meta["source"], "page": meta["page"]}
                for _, doc, meta in items
            ]
            return True
        except Exception as exc:
            print(f"[KnowledgeBase] 缓存加载失败，将重建索引：{exc}")
            return False

    def _save_manifest(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(CACHE_DIR / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(self._manifest(), f, ensure_ascii=False)
        except Exception as exc:
            print(f"[KnowledgeBase] manifest 写入失败（不影响本次检索）：{exc}")


# 模块级单例，子智能体工具和后续调用共享同一份索引
retriever = LocalKnowledgeRetriever()


if __name__ == "__main__":
    # 本地调试入口：直接运行可验证 PDF 抽取、向量化和混合检索整条链路
    print(retriever.search("2026 年电商行业 AI 应用有哪些趋势？", k=3))
