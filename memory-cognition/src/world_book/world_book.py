"""
世界书 (World Book)
加载 Markdown+YAML 词条文件，按关键词触发，按需注入上下文。
词条独立注入（不分组），最多 3 条。
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

import frontmatter

logger = logging.getLogger(__name__)

MAX_ENTRIES_PER_INJECT = 3


@dataclass
class WorldBookEntry:
    filename: str
    name: str
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    category: str = ""
    body: str = ""       # LLM 注入用（已剥离 "## 编辑记录" 审核元数据）
    body_full: str = ""  # 文件原文（含编辑记录，前端浏览页用）


class WorldBook:
    """加载、索引、搜索世界书词条"""

    def __init__(self, entries_dir: Optional[str] = None, vector_embedder=None):
        if entries_dir is None:
            entries_dir = os.path.join(os.path.dirname(__file__), "entries")
        self.entries_dir = entries_dir
        self._entries: Dict[str, WorldBookEntry] = {}
        self._keyword_index: Dict[str, List[str]] = {}
        self._loaded = False
        self._vector_embedder = vector_embedder  # VectorEmbedder 实例（可选，用于语义检索）
        self._entry_embeddings: Dict[str, Any] = {}  # filename → embedding vector

    def load_all(self) -> bool:
        if self._loaded:
            return len(self._entries) > 0

        if not os.path.isdir(self.entries_dir):
            logger.warning(f"世界书目录不存在: {self.entries_dir}")
            return False

        self._entries.clear()
        self._keyword_index.clear()

        for fname in sorted(os.listdir(self.entries_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(self.entries_dir, fname)
            try:
                post = frontmatter.load(fpath)
            except Exception as e:
                logger.warning(f"世界书词条解析失败: {fname}: {e}")
                continue

            name = post.get("name", "")
            if not name:
                continue

            keywords = post.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            keywords = [str(k).strip() for k in keywords if str(k).strip()]

            aliases = post.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]
            aliases = [str(a).strip() for a in aliases if str(a).strip()]

            # 注入用 body 剥离尾部 "## 编辑记录" 段（审核元数据，不进 LLM 上下文；文件本体不动）
            body_full = post.content.strip() if post.content else ""
            body = body_full
            if "## 编辑记录" in body:
                body = body.rsplit("## 编辑记录", 1)[0].strip()

            entry = WorldBookEntry(
                filename=fname,
                name=name,
                aliases=aliases,
                keywords=keywords,
                category=post.get("category", ""),
                body=body,
                body_full=body_full,
            )
            self._entries[fname] = entry

            trigger_terms = set()
            trigger_terms.add(name.lower())
            for a in aliases:
                trigger_terms.add(a.lower())
            for k in keywords:
                trigger_terms.add(k.lower())

            for term in trigger_terms:
                term = term.strip()
                if not term:
                    continue
                if term not in self._keyword_index:
                    self._keyword_index[term] = []
                if fname not in self._keyword_index[term]:
                    self._keyword_index[term].append(fname)

        self._loaded = True
        logger.info(f"世界书加载完成: {len(self._entries)} 个词条, {len(self._keyword_index)} 个关键词")

        # 预计算条目 embedding（语义检索用）
        if self._vector_embedder is not None:
            self._entry_embeddings.clear()
            for fname, entry in self._entries.items():
                embed_text = f"{entry.name} {' '.join(entry.aliases)} {' '.join(entry.keywords)} {entry.body[:500]}"
                try:
                    self._entry_embeddings[fname] = self._vector_embedder.embed_text(embed_text)
                except Exception as e:
                    logger.warning(f"世界书条目 embedding 失败: {entry.name}: {e}")
            logger.info(f"世界书 embedding 计算完成: {len(self._entry_embeddings)} 个条目")

        return len(self._entries) > 0

    def _semantic_search(self, query: str, top_k: int = 3) -> List[str]:
        """语义检索：返回相似度 > 0.6 的条目文件名列表"""
        if self._vector_embedder is None or not self._entry_embeddings:
            return []
        try:
            import numpy as np
            q_embed = self._vector_embedder.embed_text(query)
            scores = []
            for fname, emb in self._entry_embeddings.items():
                sim = float(np.dot(q_embed, emb) / (np.linalg.norm(q_embed) * np.linalg.norm(emb) + 1e-8))
                if sim > 0.6:
                    scores.append((fname, sim))
            scores.sort(key=lambda x: -x[1])
            return [fname for fname, _ in scores[:top_k]]
        except Exception as e:
            logger.warning(f"世界书语义检索失败: {e}")
            return []

    def search(self, user_message: str, session_id: str = "") -> List[WorldBookEntry]:
        """搜索命中词条（子串匹配优先，回退语义检索），最多返回 MAX_ENTRIES_PER_INJECT 条。
        session_id 参数保留用于 API 兼容，当前不再追踪冷却。"""
        if not self._loaded or not user_message:
            return []
        from cognition.entity_names import expand_query
        user_message = expand_query(user_message)
        msg_lower = user_message.lower()
        matched_fnames: Dict[str, int] = {}
        for keyword, fnames in self._keyword_index.items():
            if keyword in msg_lower:
                for fn in fnames:
                    matched_fnames[fn] = matched_fnames.get(fn, 0) + 1

        if not matched_fnames:
            # 子串匹配未命中 → 回退语义检索
            semantic_fnames = self._semantic_search(user_message)
            if semantic_fnames:
                matched_fnames = {fn: 1 for fn in semantic_fnames}
            else:
                return []

        # 按匹配关键词数排序，取前 N
        sorted_fns = sorted(matched_fnames.keys(), key=lambda fn: -matched_fnames[fn])
        selected = sorted_fns[:MAX_ENTRIES_PER_INJECT]

        return [self._entries[fn] for fn in selected if fn in self._entries]

    def get_all_entries(self) -> List[WorldBookEntry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def get_all_keywords(self) -> List[str]:
        return sorted(self._keyword_index.keys())

    def reload(self) -> bool:
        self._loaded = False
        return self.load_all()

    def set_vector_embedder(self, embedder):
        """注入 VectorEmbedder 实例用于语义检索"""
        self._vector_embedder = embedder
        if self._loaded:
            self.reload()


_global_world_book: Optional[WorldBook] = None


def get_global_world_book() -> WorldBook:
    global _global_world_book
    if _global_world_book is None:
        _global_world_book = WorldBook()
        _global_world_book.load_all()
    return _global_world_book
