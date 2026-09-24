"""
SCP 百科全书 (Soul Complement Plan Encyclopedia)
加载 Markdown+YAML 词条文件，构建关键词索引，支持子串匹配搜索。
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import frontmatter

logger = logging.getLogger(__name__)

# 核心词条分组：命中任一分组内的关键词时，整组词条一起注入
CORE_GROUP_A = {"灵魂互补", "双螺旋", "灵魂纠缠"}   # 关系/机制组
CORE_GROUP_B = {"生命线", "圆满", "永生"}            # 目的/终态组

MAX_ENTRIES_PER_INJECT = 7   # 最多注入词条数
COOLDOWN_ROUNDS = 3          # 同一词组在此轮数内不重复注入


@dataclass
class SCPEntry:
    """一条 SCP 百科词条"""
    filename: str
    name: str
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    category: str = ""
    granularity: str = "secondary"  # core | secondary
    group: str = ""                 # A | B (仅 core 词条有效)
    body: str = ""


class SCPEncyclopedia:
    """加载、索引、搜索 SCP 词条"""

    def __init__(self, entries_dir: Optional[str] = None):
        if entries_dir is None:
            entries_dir = os.path.join(os.path.dirname(__file__), "entries")
        self.entries_dir = entries_dir
        self._entries: Dict[str, SCPEntry] = {}       # filename -> entry
        self._keyword_index: Dict[str, List[str]] = {}  # keyword -> [filenames]
        self._loaded = False
        # 冷却机制（和世界书一致，3轮不重复注入）
        self._cooldown: Dict[str, Dict[str, int]] = {}  # session_id -> {group -> last_round}
        self._round_counter: Dict[str, int] = {}  # session_id -> current_round

    def load_all(self) -> bool:
        """扫描 entries/ 目录，解析所有 .md 文件。返回是否加载到有效词条。"""
        if self._loaded:
            return len(self._entries) > 0

        if not os.path.isdir(self.entries_dir):
            logger.warning(f"SCP 百科目录不存在: {self.entries_dir}")
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
                logger.warning(f"SCP 词条解析失败: {fname}: {e}")
                continue

            name = post.get("name", "")
            if not name:
                logger.warning(f"SCP 词条缺少 name: {fname}")
                continue

            keywords = post.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            keywords = [str(k).strip() for k in keywords if str(k).strip()]

            aliases = post.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]
            aliases = [str(a).strip() for a in aliases if str(a).strip()]

            granularity = post.get("granularity", "secondary")
            category = post.get("category", "")
            group = post.get("group", "")

            entry = SCPEntry(
                filename=fname,
                name=name,
                aliases=aliases,
                keywords=keywords,
                category=category,
                granularity=granularity,
                group=group,
                body=post.content.split("## 编辑记录", 1)[0].strip() if post.content else "",
            )
            self._entries[fname] = entry

            # 建立关键词索引：name + aliases + keywords 全部作为触发词
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
        logger.info(f"SCP 百科加载完成: {len(self._entries)} 个词条, {len(self._keyword_index)} 个关键词")
        return len(self._entries) > 0

    def search(self, user_message: str, session_id: str = "") -> List[SCPEntry]:
        """
        在用户消息中搜索关键词匹配（带3轮冷却）。
        - core 词条：命中分组内任一关键词 → 整组注入
        - secondary 词条：命中关键词才注入
        - 冷却机制：同一 session 同一组在 3 轮内不重复注入
        结果去重，按分组优先（core 在前），每组内按匹配关键词数降序。
        """
        # 冷却状态推进（无论有无命中都推进轮次）
        if session_id:
            if session_id not in self._cooldown:
                self._cooldown[session_id] = {}
                self._round_counter[session_id] = 0
            current_round = self._round_counter[session_id] + 1
            self._round_counter[session_id] = current_round
        else:
            current_round = 0

        if not self._loaded or not user_message:
            return []

        msg_lower = user_message.lower()

        # 收集命中：统计每个条目匹配了多少个关键词
        matched_fnames: Dict[str, int] = {}  # filename -> matched keyword count
        for keyword, fnames in self._keyword_index.items():
            if keyword in msg_lower:
                for fn in fnames:
                    matched_fnames[fn] = matched_fnames.get(fn, 0) + 1

        if not matched_fnames:
            return []

        # 扩展：core 词条命中 → 拉入同组其他 core 词条
        result_fnames = set()
        triggered_groups = set()

        for fn in matched_fnames:
            entry = self._entries.get(fn)
            if not entry:
                continue
            if entry.granularity == "core" and entry.group:
                triggered_groups.add(entry.group)
            result_fnames.add(fn)

        # 拉入同组 core 词条
        if triggered_groups:
            for fn, entry in self._entries.items():
                if entry.granularity == "core" and entry.group in triggered_groups:
                    result_fnames.add(fn)

        # 冷却过滤（仅 core 词条的组做冷却；secondary 词条按独立 fn 冷却）
        if session_id and current_round > 0:
            cooldown_state = self._cooldown[session_id]
            # 过滤 core 词条组
            for group in list(triggered_groups):
                last_round = cooldown_state.get(group, -COOLDOWN_ROUNDS)
                if current_round - last_round < COOLDOWN_ROUNDS:
                    # 冷却中：移除此组所有 core 词条
                    for fn in list(result_fnames):
                        entry = self._entries.get(fn)
                        if entry and entry.granularity == "core" and entry.group == group:
                            result_fnames.discard(fn)
                    triggered_groups.discard(group)
                else:
                    cooldown_state[group] = current_round  # 更新冷却标记

        if not result_fnames:
            return []

        # 组装结果：core 优先，组内按匹配关键词数降序
        core_results = []
        secondary_results = []
        for fn in result_fnames:
            entry = self._entries.get(fn)
            if not entry:
                continue
            match_count = matched_fnames.get(fn, 0)
            if entry.granularity == "core":
                core_results.append((match_count, entry))
            else:
                secondary_results.append((match_count, entry))

        core_results.sort(key=lambda x: (x[1].group or "", -x[0]))
        secondary_results.sort(key=lambda x: -x[0])

        entries = [e for _, e in core_results] + [e for _, e in secondary_results]
        return entries[:MAX_ENTRIES_PER_INJECT]

    def get_all_keywords(self) -> List[str]:
        """返回所有已索引的触发词（调试用）。"""
        return sorted(self._keyword_index.keys())

    def reload(self) -> bool:
        """热重载词条目录。"""
        self._loaded = False
        return self.load_all()


# 模块级单例
_global_encyclopedia: Optional[SCPEncyclopedia] = None


def get_global_scp_encyclopedia() -> SCPEncyclopedia:
    """惰性获取全局 SCP 百科单例。"""
    global _global_encyclopedia
    if _global_encyclopedia is None:
        _global_encyclopedia = SCPEncyclopedia()
        _global_encyclopedia.load_all()
    return _global_encyclopedia
