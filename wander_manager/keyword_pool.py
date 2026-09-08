# 漫想关键词池管理器
#
# 功能：
# 1. 管理动态关键词池（加载/保存/扩充/去重/淘汰）
# 2. 支持情感标签（positive/neutral/negative）
# 3. 支持心情加权随机选择
# 4. 支持从记忆桶提取新关键词

from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import random
import json
import os
import logging

logger = logging.getLogger(__name__)

# 默认存储路径
_POOL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "keyword_pool.json")


@dataclass
class KeywordEntry:
    """关键词条目"""
    word: str
    sentiment: str = "neutral"  # positive / neutral / negative
    source_bucket: str = ""     # 来源记忆桶ID
    added_at: str = ""          # ISO 时间戳
    selected_count: int = 0     # 被选中次数
    last_selected_at: str = ""  # 最后被选中时间

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "sentiment": self.sentiment,
            "source_bucket": self.source_bucket,
            "added_at": self.added_at,
            "selected_count": self.selected_count,
            "last_selected_at": self.last_selected_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeywordEntry":
        return cls(
            word=d.get("word", ""),
            sentiment=d.get("sentiment", "neutral"),
            source_bucket=d.get("source_bucket", ""),
            added_at=d.get("added_at", ""),
            selected_count=d.get("selected_count", 0),
            last_selected_at=d.get("last_selected_at", ""),
        )


# 冷启动关键词池（预设40词，带情感标签）
_DEFAULT_POOL = [
    ("星空", "neutral"), ("雨夜", "negative"), ("咖啡", "neutral"), ("旅行", "positive"),
    ("音乐", "positive"), ("书籍", "neutral"), ("电影", "positive"), ("美食", "positive"),
    ("回忆", "neutral"), ("梦想", "positive"), ("友情", "positive"), ("爱情", "positive"),
    ("成长", "positive"), ("孤独", "negative"), ("温暖", "positive"), ("希望", "positive"),
    ("春天", "positive"), ("夏天", "positive"), ("秋天", "neutral"), ("冬天", "neutral"),
    ("日出", "positive"), ("日落", "neutral"), ("月光", "neutral"), ("海浪", "neutral"),
    ("森林", "neutral"), ("城市", "neutral"), ("乡村", "neutral"), ("花园", "positive"),
    ("猫咪", "positive"), ("狗狗", "positive"), ("花朵", "positive"), ("蝴蝶", "positive"),
    ("时间", "neutral"), ("空间", "neutral"), ("宇宙", "neutral"), ("生命", "neutral"),
    ("艺术", "neutral"), ("科技", "neutral"), ("未来", "positive"), ("过去", "neutral"),
]


class KeywordPool:
    """动态关键词池管理器"""

    MAX_POOL_SIZE = 200
    MAX_DAILY_ADDITIONS = 10
    RETENTION_DAYS = 30  # 30天未使用自动淘汰

    def __init__(self, pool_file: str = None):
        self._pool_file = pool_file or _POOL_FILE
        self._entries: Dict[str, KeywordEntry] = {}
        self._today_added = 0
        self._today_date = ""
        self._load()

    def _load(self):
        """从文件加载关键词池，失败则用默认池"""
        if os.path.exists(self._pool_file):
            try:
                with open(self._pool_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    entry = KeywordEntry.from_dict(item)
                    self._entries[entry.word] = entry
                logger.info(f"关键词池已加载: {len(self._entries)} 词")
                if self._entries:
                    return
            except Exception as e:
                logger.warning(f"关键词池加载失败: {e}，使用默认池")

        # 冷启动
        now = datetime.now().isoformat()
        for word, sentiment in _DEFAULT_POOL:
            self._entries[word] = KeywordEntry(
                word=word, sentiment=sentiment, added_at=now
            )
        self._save()
        logger.info(f"关键词池冷启动: {len(self._entries)} 词")

    def _save(self):
        """持久化关键词池"""
        try:
            os.makedirs(os.path.dirname(self._pool_file), exist_ok=True)
            with open(self._pool_file, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self._entries.values()], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"关键词池保存失败: {e}")

    def _check_daily_reset(self):
        """每日重置添加计数"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today_date:
            self._today_added = 0
            self._today_date = today

    def get_all_words(self) -> List[KeywordEntry]:
        """获取所有关键词条目"""
        return list(self._entries.values())

    def select_keyword(self) -> str:
        """从关键词池随机选择一个词；不读取旧的三参数情绪模型。"""
        if not self._entries:
            return "咖啡"  # 兜底

        words = list(self._entries.keys())
        selected = random.choice(words)
        entry = self._entries[selected]
        entry.selected_count += 1
        entry.last_selected_at = datetime.now().isoformat()
        self._save()
        return selected

    def add_words(self, new_words: List[Tuple[str, str, str]]):
        """
        批量添加新关键词（去重、限流）。

        Args:
            new_words: [(word, sentiment, source_bucket), ...]
        """
        self._check_daily_reset()
        now = datetime.now().isoformat()
        added = 0

        for word, sentiment, source_bucket in new_words:
            if self._today_added >= self.MAX_DAILY_ADDITIONS:
                break
            if len(self._entries) >= self.MAX_POOL_SIZE:
                self._evict_low_usage()
                if len(self._entries) >= self.MAX_POOL_SIZE:
                    break
            if word in self._entries:
                continue
            # 简单去重：检查是否有高度相似的词
            if self._is_duplicate(word):
                continue

            self._entries[word] = KeywordEntry(
                word=word,
                sentiment=sentiment or "neutral",
                source_bucket=source_bucket,
                added_at=now,
            )
            added += 1
            self._today_added += 1

        if added > 0:
            self._save()
            logger.info(f"关键词池新增 {added} 词, 总计 {len(self._entries)} 词")

    def _is_duplicate(self, word: str) -> bool:
        """简单去重：检查是否有完全相同的词或子串匹配"""
        word_lower = word.lower().strip()
        for existing in self._entries:
            existing_lower = existing.lower().strip()
            # 完全相同
            if word_lower == existing_lower:
                return True
            # 一个是另一个的子串（长度>1才考虑）
            if len(word_lower) > 1 and len(existing_lower) > 1:
                if word_lower in existing_lower or existing_lower in word_lower:
                    return True
        return False

    def _evict_low_usage(self):
        """淘汰低使用率的关键词（被选中次数最低的10%）"""
        if len(self._entries) < 50:
            return

        now = datetime.now()
        to_remove = []
        for word, entry in self._entries.items():
            # 30天未使用
            if entry.last_selected_at:
                try:
                    last = datetime.fromisoformat(entry.last_selected_at)
                    if (now - last).days > self.RETENTION_DAYS:
                        to_remove.append(word)
                        continue
                except Exception:
                    pass
            elif entry.added_at:
                try:
                    added = datetime.fromisoformat(entry.added_at)
                    if (now - added).days > self.RETENTION_DAYS:
                        to_remove.append(word)
                        continue
                except Exception:
                    pass

        # 如果还不够，按选中次数淘汰最低的10%
        if len(to_remove) < max(1, len(self._entries) // 10):
            sorted_entries = sorted(self._entries.items(), key=lambda x: x[1].selected_count)
            for word, _ in sorted_entries[:max(1, len(self._entries) // 10)]:
                if word not in to_remove:
                    to_remove.append(word)

        for word in to_remove[:max(5, len(self._entries) // 10)]:
            del self._entries[word]

        if to_remove:
            self._record_evictions(to_remove)
            logger.info(f"关键词池淘汰 {len(to_remove)} 词, 剩余 {len(self._entries)}")

    def _get_history_path(self) -> str:
        if self._pool_file and self._pool_file.endswith(".json"):
            return self._pool_file.replace(".json", "_history.json")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "keyword_pool_history.json")

    def _record_evictions(self, evicted_words: list):
        """将被淘汰的关键词记录到历史文件"""
        try:
            import json as _json, os as _os
            history_path = self._get_history_path()
            existing = []
            if _os.path.exists(history_path):
                try:
                    with open(history_path, "r", encoding="utf-8") as f:
                        existing = _json.load(f)
                except Exception:
                    existing = []
            now_iso = datetime.now().isoformat()
            for word in evicted_words:
                existing.append({
                    "word": word,
                    "evicted_at": now_iso,
                })
            existing = existing[-500:]
            _os.makedirs(_os.path.dirname(history_path), exist_ok=True)
            with open(history_path, "w", encoding="utf-8") as f:
                _json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_eviction_history(self, limit: int = 50) -> list:
        """获取淘汰历史"""
        try:
            import json as _json, os as _os
            history_path = self._get_history_path()
            if _os.path.exists(history_path):
                with open(history_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                return data[-limit:]
        except Exception:
            pass
        return []


# 全局单例
_keyword_pool_instance: Optional[KeywordPool] = None


def get_keyword_pool() -> KeywordPool:
    """获取全局关键词池实例"""
    global _keyword_pool_instance
    if _keyword_pool_instance is None:
        _keyword_pool_instance = KeywordPool()
    return _keyword_pool_instance


def init_keyword_pool(pool_file: str = None) -> KeywordPool:
    """初始化全局关键词池实例"""
    global _keyword_pool_instance
    _keyword_pool_instance = KeywordPool(pool_file=pool_file)
    return _keyword_pool_instance
