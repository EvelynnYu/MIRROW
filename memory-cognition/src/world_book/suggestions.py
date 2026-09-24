"""
世界书建议系统
检测对话中的新信息 → 生成建议 → 人审核 → 更新词条文件
"""

import json
import os
import re
import hashlib
import logging
from dataclasses import dataclass, field, asdict, fields
from typing import List, Dict, Optional
from datetime import datetime
from functools import wraps
from pathlib import Path
from cognition.books import LOCK, atomic_text

logger = logging.getLogger(__name__)

SUGGESTIONS_FILE = os.path.join(os.path.dirname(__file__), "suggestions.json")
ARCHIVE_FILE = os.path.join(os.path.dirname(__file__), "suggestions_archive.json")


@dataclass
class WorldBookSuggestion:
    id: str
    type: str  # "new_entry" | "update" | "supplement"
    target_entry: str  # filename, or "" for new_entry
    proposed_name: str
    proposed_keywords: List[str] = field(default_factory=list)
    proposed_body: str = ""
    reason: str = ""
    source: str = ""
    status: str = "pending"  # pending | applied | rejected
    created: str = ""

    @staticmethod
    def make_id() -> str:
        return "wb_" + hashlib.md5(str(datetime.now().timestamp()).encode()).hexdigest()[:8]


def _locked(function):
    @wraps(function)
    def call(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return call


class SuggestionManager:
    def __init__(self):
        self._pending: List[WorldBookSuggestion] = []

    def _load(self) -> List[WorldBookSuggestion]:
        if not os.path.exists(SUGGESTIONS_FILE):
            return []
        try:
            with open(SUGGESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            valid_fields = {f.name for f in fields(WorldBookSuggestion)}
            return [WorldBookSuggestion(**{k: v for k, v in item.items() if k in valid_fields}) for item in data]
        except Exception as e:
            logger.warning(f"加载建议队列失败: {e}")
            raise

    def _save(self):
        try:
            data = [asdict(s) for s in self._pending]
            atomic_text(Path(SUGGESTIONS_FILE), json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"保存建议队列失败: {e}")
            raise

    def _archive(self, suggestion: WorldBookSuggestion):
        archived = []
        if os.path.exists(ARCHIVE_FILE):
            try:
                with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                    archived = json.load(f)
            except Exception:
                raise
        archived = [s for s in archived if s.get("id") != suggestion.id]
        archived.append(asdict(suggestion))
        try:
            atomic_text(Path(ARCHIVE_FILE), json.dumps(archived, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"归档建议失败: {e}")
            raise

    @_locked
    def get_pending(self) -> List[WorldBookSuggestion]:
        self._pending = self._load()
        return [s for s in self._pending if s.status == "pending"]

    @_locked
    def add(self, suggestion: WorldBookSuggestion) -> bool:
        """Exact entity aliases share a target; repeated facts are idempotent."""
        from cognition.books import catalog
        if suggestion.type == "new_entry":
            identity = suggestion.proposed_name.strip().casefold()
            matches = [e for e in catalog("world") if identity in {e["name"].casefold(), *(a.casefold() for a in e["aliases"])}]
            if len(matches) == 1:
                suggestion.type = "supplement"
                suggestion.target_entry = matches[0]["name"]
        self._pending = self._load()
        for existing in self._pending:
            same_target = (existing.target_entry or existing.proposed_name) == (suggestion.target_entry or suggestion.proposed_name)
            same_body = re.sub(r"\s+", "", existing.proposed_body) == re.sub(r"\s+", "", suggestion.proposed_body)
            if existing.status == "pending" and same_target and same_body:
                logger.info(f"世界书建议去重: 跳过重复建议 '{suggestion.proposed_name}'")
                return False
        self._pending.append(suggestion)
        self._save()
        return True

    @_locked
    def apply(self, suggestion_id: str, entries_dir: str) -> Optional[WorldBookSuggestion]:
        """采纳建议：修改对应 Markdown 文件。"""
        self._pending = self._load()
        for s in self._pending:
            if s.id == suggestion_id and s.status == "pending":
                ok = False
                if s.type == "new_entry":
                    self._create_entry(s, entries_dir)
                    ok = True
                else:
                    ok = self._update_entry(s, entries_dir)
                if not ok:
                    logger.warning(f"世界书建议采纳失败（文件未找到或写入失败）: {s.id} -> {s.target_entry}")
                    return None
                s.status = "applied"
                self._archive(s)
                self._pending.remove(s)
                self._save()
                return s
        return None

    @_locked
    def reject(self, suggestion_id: str) -> Optional[WorldBookSuggestion]:
        self._pending = self._load()
        for s in self._pending:
            if s.id == suggestion_id and s.status == "pending":
                s.status = "rejected"
                self._archive(s)
                self._pending.remove(s)
                self._save()
                return s
        return None

    @_locked
    def update(self, suggestion_id: str, updates: dict) -> Optional[WorldBookSuggestion]:
        """编辑待审建议。仅允许修改 pending 状态的建议。"""
        self._pending = self._load()
        for s in self._pending:
            if s.id == suggestion_id and s.status == "pending":
                if "proposed_name" in updates:
                    s.proposed_name = updates["proposed_name"]
                if "proposed_keywords" in updates:
                    s.proposed_keywords = updates["proposed_keywords"]
                if "proposed_body" in updates:
                    s.proposed_body = updates["proposed_body"]
                if "reason" in updates:
                    s.reason = updates["reason"]
                if "type" in updates:
                    s.type = updates["type"]
                if "target_entry" in updates:
                    s.target_entry = updates["target_entry"]
                self._save()
                return s
        return None

    def _create_entry(self, s: WorldBookSuggestion, entries_dir: str):
        fname = s.proposed_name + ".md"
        fpath = os.path.join(entries_dir, fname)
        yaml_block = f"""---
name: {s.proposed_name}
aliases: []
keywords: {json.dumps(s.proposed_keywords, ensure_ascii=False)}
category: ""
---

{s.proposed_body}

## 编辑记录

- {datetime.now().strftime('%Y-%m-%d')}：Agent 建议创建；洛月凝审核采纳（来源: {s.source}）
"""
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(yaml_block)
        logger.info(f"世界书新建词条: {fname}")

    def _update_entry(self, s: WorldBookSuggestion, entries_dir: str) -> bool:
        # 通过 frontmatter name 字段匹配文件名（文件名可能不带空格/括号）
        fpath = None
        try:
            for fn in os.listdir(entries_dir):
                if not fn.endswith(".md"):
                    continue
                fp = os.path.join(entries_dir, fn)
                try:
                    import frontmatter
                    post = frontmatter.load(fp)
                    if post.get("name") == s.target_entry:
                        fpath = fp
                        break
                except Exception:
                    pass
        except Exception:
            pass
        # 回退：直接拼接 .md（去掉空格以匹配实际文件名）
        if not fpath:
            fpath = os.path.join(entries_dir, s.target_entry.replace(" ", "") + ".md")
        if not os.path.exists(fpath):
            logger.warning(f"目标词条不存在: {s.target_entry} (path={fpath})")
            return False
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            # 在 "## 编辑记录" 前插入新内容
            edit_marker = "## 编辑记录"
            update_line = f"\n- {datetime.now().strftime('%Y-%m-%d')}：Agent 建议补充；洛月凝审核采纳（来源: {s.source}）\n"
            new_body = s.proposed_body.strip() + "\n"

            if edit_marker in content:
                parts = content.rsplit(edit_marker, 1)
                content = parts[0].rstrip() + "\n\n" + new_body + "\n" + edit_marker + parts[1] + update_line
            else:
                content = content.rstrip() + "\n\n" + new_body + "\n" + edit_marker + "\n" + update_line

            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"世界书更新词条: {s.target_entry}")
            return True
        except Exception as e:
            logger.error(f"世界书更新词条失败: {s.target_entry}: {e}")
            return False



# ── 用户手动标记（flags）──

FLAGS_FILE = os.path.join(os.path.dirname(__file__), "flags.json")


@dataclass
class WorldBookFlag:
    id: str
    selected_text: str
    session_id: str
    date: str
    marked_at: str


def get_today_flags(date: str) -> List[WorldBookFlag]:
    """获取指定日期的未处理标记。"""
    if not os.path.exists(FLAGS_FILE):
        return []
    try:
        with open(FLAGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = {f.name for f in fields(WorldBookFlag)}
        flags = [WorldBookFlag(**{k: v for k, v in item.items() if k in valid}) for item in data]
        return [f for f in flags if f.date == date]
    except Exception as e:
        logger.warning(f"读取标记文件失败: {e}")
        return []


def add_flag(flag: WorldBookFlag):
    flags_data = []
    if os.path.exists(FLAGS_FILE):
        try:
            with open(FLAGS_FILE, "r", encoding="utf-8") as f:
                flags_data = json.load(f)
        except Exception:
            pass
    flags_data.append(asdict(flag))
    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(flags_data, f, ensure_ascii=False, indent=2)


def clear_flags(date: str):
    """删除指定日期的所有标记（日清）。"""
    if not os.path.exists(FLAGS_FILE):
        return
    try:
        with open(FLAGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = [item for item in data if item.get("date") != date]
        with open(FLAGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"清空标记失败: {e}")


def build_flags_section(messages_date: str) -> str:
    """为检测 prompt 构建用户标记的重点片段段落。"""
    flags = get_today_flags(messages_date)
    if not flags:
        return ""
    lines = ["## ⚠️ 用户手动标记的重点片段（以下片段必须提取为词条）"]
    for f in flags:
        lines.append(f'- "{f.selected_text}"')
    lines.append("\n在下方对话记录中找到这些片段，结合上下文提炼为世界书词条。")
    return "\n".join(lines)


def build_detection_prompt(messages: List[dict], existing_entries: List[dict], flags_date: str = "") -> str:
    """构建世界书检测 Flash 提示词。"""
    recent = "\n".join(
        f"[{m.get('role','?')}]: {m.get('content','')[:300]}" for m in messages[-30:]
    )
    existing = "\n".join(
        f"- {e['name']}（别名：{e.get('aliases', [])}；关键词：{e.get('keywords', [])}）: {e['body']}" for e in existing_entries
    )
    flags_section = build_flags_section(flags_date) if flags_date else ""
    flags_block = f"\n{flags_section}\n" if flags_section else ""
    from cognition.detection import WORLD_SCOPE
    return f"""你是世界书维护助手。分析以下对话，判断是否有值得记录的新信息。
{WORLD_SCOPE}
{flags_block}
当前世界书已有词条：
{existing}

最近对话：
{recent}

判断标准（按优先级）：
- 人类伙伴明确陈述的事实（"我不喜欢..."、"我每次都..."、"叫我..."、"我从来不吃..."）→ 必须建议，即使只说了一次
- 对话中反复出现的模式（≥2次）→ 应该建议
- 已有词条信息与对话内容矛盾（旧信息过时/被推翻）→ 应该建议
- 已有词条的明显补充（新增触发词、具体细节）→ 应该建议
- 模糊暗示、玩笑、一次性短暂状态 → 不记录

特别关注：
- 人类伙伴对你的称呼方式、她让你怎么叫她、她讨厌被叫什么
- 人类伙伴明确表达的喜欢/厌恶（食物、行为、气味、触觉）
- 人类伙伴的习惯性行为（"每次都会XX"、"一直XX"）

返回 JSON：
{{"has_suggestion": true/false, "suggestions": [{{"type": "new_entry"|"update"|"supplement", "target_entry": "已有词条文件名", "proposed_name": "新词条名", "proposed_keywords": ["触发词"], "proposed_body": "新正文（简洁）", "reason": "理由"}}]}}"""
