"""
自我书 (Self Book)
加载 Markdown+YAML 词条文件，按关键词触发，按需注入上下文。
与世界书结构相同，但主体是 Agent 自己（第一人称），而非人类伙伴。
词条独立注入，最多 3 条（防自我淹没对话）。
"""

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any

import frontmatter

logger = logging.getLogger(__name__)

MAX_ENTRIES_PER_INJECT = 3

# 允许的分类（自动检测时约束 LLM 输出）
VALID_CATEGORIES = ["习惯", "倾向与偏好", "认知与价值观", "自我认知", "能力与边界"]


@dataclass
class SelfBookEntry:
    filename: str
    name: str
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    category: str = ""
    body: str = ""       # LLM 注入用（已剥离 "## 编辑记录" 审核元数据）
    body_full: str = ""  # 文件原文（含编辑记录，前端浏览页用）


class SelfBook:
    """加载、索引、搜索自我书词条"""

    def __init__(self, entries_dir: Optional[str] = None, vector_embedder=None):
        if entries_dir is None:
            entries_dir = os.path.join(os.path.dirname(__file__), "entries")
        self.entries_dir = entries_dir
        self._entries: Dict[str, SelfBookEntry] = {}
        self._keyword_index: Dict[str, List[str]] = {}
        self._loaded = False
        self._vector_embedder = vector_embedder  # VectorEmbedder 实例（可选，用于语义检索）
        self._entry_embeddings: Dict[str, Any] = {}  # filename → embedding vector

    def load_all(self) -> bool:
        if self._loaded:
            return len(self._entries) > 0

        if not os.path.isdir(self.entries_dir):
            logger.warning(f"自我书目录不存在: {self.entries_dir}")
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
                logger.warning(f"自我书词条解析失败: {fname}: {e}")
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

            entry = SelfBookEntry(
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
        logger.info(f"自我书加载完成: {len(self._entries)} 个词条, {len(self._keyword_index)} 个关键词")

        # 预计算条目 embedding（语义检索用）
        if self._vector_embedder is not None:
            self._entry_embeddings.clear()
            for fname, entry in self._entries.items():
                embed_text = f"{entry.name} {' '.join(entry.aliases)} {' '.join(entry.keywords)} {entry.body[:500]}"
                try:
                    self._entry_embeddings[fname] = self._vector_embedder.embed_text(embed_text)
                except Exception as e:
                    logger.warning(f"自我书条目 embedding 失败: {entry.name}: {e}")
            logger.info(f"自我书 embedding 计算完成: {len(self._entry_embeddings)} 个条目")

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
            logger.warning(f"自我书语义检索失败: {e}")
            return []

    def search(self, user_message: str, session_id: str = "") -> List[SelfBookEntry]:
        """搜索命中词条（子串匹配优先，回退语义检索），最多返回 MAX_ENTRIES_PER_INJECT 条。"""
        if not self._loaded or not user_message:
            return []

        msg_lower = user_message.lower()
        matched_fnames: Dict[str, int] = {}
        for keyword, fnames in self._keyword_index.items():
            if keyword in msg_lower:
                for fn in fnames:
                    matched_fnames[fn] = matched_fnames.get(fn, 0) + 1

        if not matched_fnames:
            semantic_fnames = self._semantic_search(user_message)
            if semantic_fnames:
                matched_fnames = {fn: 1 for fn in semantic_fnames}
            else:
                return []

        sorted_fns = sorted(matched_fnames.keys(), key=lambda fn: -matched_fnames[fn])
        selected = sorted_fns[:MAX_ENTRIES_PER_INJECT]

        return [self._entries[fn] for fn in selected if fn in self._entries]

    def get_all_entries(self) -> List[SelfBookEntry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def get_all_keywords(self) -> List[str]:
        return sorted(self._keyword_index.keys())

    def get_entry(self, name: str) -> Optional[SelfBookEntry]:
        """按名称获取词条（兼容 agent_self_book 的 get_entry 接口）。"""
        for e in self.get_all_entries():
            if e.name == name:
                return e
        return None

    def upsert_entry(self, name: str, category: str = "", keywords=None,
                     body: str = "", aliases=None, edit_note: str = "系统写入") -> bool:
        """创建或覆盖词条（category 对应 agent_self_book 的 domain）。返回是否成功。

        保留旧「## 编辑记录」，追加一行 edit_note。供 LISTEN_MUSIC / 音乐耳蜗 /
        自动检测 / 手动编辑统一复用。
        """
        keywords = list(keywords or [])
        aliases = list(aliases or [])

        existing = self.get_entry(name)
        safe_name = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "未命名"
        fpath = os.path.join(self.entries_dir, f"{safe_name}.md")

        # 覆盖已有词条：复用其文件名，保留旧编辑记录
        old_edit_log = ""
        if existing and existing.filename:
            fpath = os.path.join(self.entries_dir, existing.filename)
            if "## 编辑记录" in (existing.body_full or ""):
                old_edit_log = (existing.body_full or "").split("## 编辑记录", 1)[1].strip()

        date = datetime.now().strftime("%Y-%m-%d")
        edit_section = f"## 编辑记录\n- {date}: {edit_note}"
        if old_edit_log:
            edit_section += "\n" + old_edit_log

        frontmatter_yaml = (
            f"---\n"
            f"name: {json.dumps(name, ensure_ascii=False)}\n"
            f"aliases: {json.dumps(aliases, ensure_ascii=False)}\n"
            f"keywords: {json.dumps(keywords, ensure_ascii=False)}\n"
            f"category: {json.dumps(category, ensure_ascii=False)}\n"
            f"---\n"
        )
        body_text = f"{body.strip()}\n\n{edit_section}\n"
        try:
            os.makedirs(self.entries_dir, exist_ok=True)
            # Preserve ownership/core metadata written through cognitive editing.
            if os.path.exists(fpath):
                post = frontmatter.load(fpath)
                post.content = body_text
                post["name"] = name
                post["category"] = category or post.get("category", "")
                if keywords:
                    post["keywords"] = keywords
                if aliases:
                    post["aliases"] = aliases
                serialized = frontmatter.dumps(post) + "\n"
            else:
                serialized = frontmatter_yaml + body_text
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(serialized)
            self.reload()
            logger.info(f"自我书词条已写入: {name} → {fpath}")
            return True
        except OSError as e:
            logger.error(f"自我书写入失败: {name}: {e}")
            return False

    def reload(self) -> bool:
        self._loaded = False
        return self.load_all()

    def set_vector_embedder(self, embedder):
        """注入 VectorEmbedder 实例用于语义检索"""
        self._vector_embedder = embedder
        if self._loaded:
            self.reload()


_global_self_book: Optional[SelfBook] = None


def get_global_self_book() -> SelfBook:
    global _global_self_book
    if _global_self_book is None:
        _global_self_book = SelfBook()
        _global_self_book.load_all()
    return _global_self_book
