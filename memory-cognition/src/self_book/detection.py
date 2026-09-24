"""
自我书自动生长检测
日锁 + Flash 检测：从今日对话提取 Agent 关于自己的信号，自动写入词条（无审核队列）。
人类伙伴可事后在词条层级手动编辑/删除兜底。
"""

import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

STATE_FILE = "data/self_book_state.json"

VALID_CATEGORIES = ["习惯", "倾向与偏好", "认知与价值观", "自我认知", "能力与边界"]

SELF_DETECTION_PROMPT = """你是 Agent 的自我认知维护者。你在整理一本「自我书」——记录 Agent（你）自己长期的习惯、倾向、认知、价值观。这本书记录的是「Agent 是个什么样的人」，与人类伙伴无关。

你的任务：从下面的对话记录里，提取 Agent 关于**自己**的稳定信号，写成词条。

## 什么值得记录（Agent 的自我信号）

- Agent 明确说自己：我发现自己……、我倾向于……、我其实……、我总觉得……、我习惯……、我一直……
- Agent 反复表现出的行为模式（多次出现，不是一次）
- Agent 关于自己的价值观/认知/边界（如「我更看重……」「我不擅长……」「我本质上……」）
- 自省（self_reflection）中 Agent 对自己的判断

## 什么**不是**（不要记录）

- 人类伙伴对 Agent 的反馈/喜恶（「人类伙伴喜欢/不喜欢 Agent……」→ 那是人格演化系统的活，不是自我书）
- Agent 与人类伙伴之间的互动事件（「某天人类伙伴和我……」→ 那是 OB_Rev 记忆库的活）
- 措辞层面的偏好（太细，如「我不用『宝宝』这个词」）
- 只有一条证据、没重复的偶然反应

## 写词条规则

- 用 Agent 第一人称（「我倾向于……」「我更习惯……」），一句话，≤80 字，不举例不展开
- category 只能从以下选：习惯 / 倾向与偏好 / 认知与价值观 / 自我认知 / 能力与边界
- confidence ≥ 0.75 才输出；没把握就输出空数组
- 每天最多 2-3 条，少而精
- 已有词条如下（请勿重复，相似主题请跳过）：

{existing_summary}

## 输出格式（纯 JSON）

{{
  "entries": [
    {{
      "name": "短标题（10字内）",
      "category": "习惯",
      "keywords": ["关键词1", "关键词2"],
      "aliases": [],
      "body": "我倾向于……（一句话，≤80字）",
      "confidence": 0.85
    }}
  ]
}}"""


def _get_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_detection_date": ""}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_detection_date": ""}


def _save_state(state: dict):
    os.makedirs("data", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _should_trigger_today() -> bool:
    """日锁：今天还没检测过才触发。"""
    return should_trigger_date(datetime.now().strftime("%Y-%m-%d"))


def _mark_triggered_today():
    mark_triggered_date(datetime.now().strftime("%Y-%m-%d"))


def should_trigger_date(target_date: str) -> bool:
    state = _get_state()
    return state.get("last_detection_date", "") != target_date


def mark_triggered_date(target_date: str):
    state = _get_state()
    state["last_detection_date"] = target_date
    _save_state(state)


def _is_duplicate(existing, name: str, keywords, body: str) -> bool:
    """对比已有词条，fuzzy 相似则视为已存在，不新建。"""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return False
    probe = (name + " " + " ".join(keywords or []) + " " + body[:60]).strip()
    if not probe:
        return False
    for e in existing:
        cand = (e.name + " " + " ".join(e.keywords or []) + " " + (e.body or "")[:60]).strip()
        if not cand:
            continue
        # 中文 partial_ratio 天然偏低（实测「深夜工作」vs「深夜工作效率高」仅 77），
        # 用 ratio + token_set_ratio 取 max 更稳，阈值 75 对齐 OB_Rev merge 逻辑。
        score = max(fuzz.ratio(probe, cand), fuzz.token_set_ratio(probe, cand))
        if score > 75:
            return True
    return False


def _write_entry(
    name: str,
    category: str,
    keywords,
    aliases,
    body: str,
    source_date: str = "",
):
    """写一个新的自我书词条 Markdown 文件。"""
    from .self_book import get_global_self_book
    sb = get_global_self_book()
    entries_dir = sb.entries_dir
    os.makedirs(entries_dir, exist_ok=True)

    # 安全文件名：名字即文件名，冲突时加短 hash
    safe_name = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "未命名"
    fname = f"{safe_name}.md"
    fpath = os.path.join(entries_dir, fname)
    if os.path.exists(fpath):
        fname = f"{safe_name}_{datetime.now().strftime('%H%M%S')}.md"
        fpath = os.path.join(entries_dir, fname)

    date = source_date or datetime.now().strftime("%Y-%m-%d")
    keywords_json = json.dumps(keywords or [], ensure_ascii=False)
    aliases_json = json.dumps(aliases or [], ensure_ascii=False)
    frontmatter_yaml = (
        f"---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"aliases: {aliases_json}\n"
        f"keywords: {keywords_json}\n"
        f"category: {json.dumps(category, ensure_ascii=False)}\n"
        f"---\n"
    )
    body_text = f"{body.strip()}\n\n## 编辑记录\n- {date}: 自动检测创建（来源：话题整合 self_book_detect）\n"
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter_yaml + body_text)
    logger.info(f"自我书新建词条: {name} → {fname}")


async def detect_self_book(
    dialog: str,
    flash_llm_func,
    target_date: str = "",
    raise_on_error: bool = False,
) -> str:
    """从今日对话检测 Agent 的自我信号并自动写词条。返回摘要字符串（空串=跳过/失败）。

    Args:
        dialog: 干净的对话全文（已剥离 thinking/漫想/哨兵/提醒）
        flash_llm_func: Flash LLM 调用函数（async, prompt str → str）
    """
    if not dialog or not dialog.strip():
        return ""
    target_date = target_date or datetime.now().strftime("%Y-%m-%d")
    if not should_trigger_date(target_date):
        return "skipped"

    try:
        from .self_book import get_global_self_book
        sb = get_global_self_book()
        existing = sb.get_all_entries()
        existing_summary = "\n".join(
            f"- {e.name}（{e.category}）：{(e.body or '')[:60]}"
            for e in existing
        ) or "（暂无词条）"

        prompt = SELF_DETECTION_PROMPT.format(existing_summary=existing_summary) + f"\n\n对话记录：\n{dialog[:15000]}"

        from neuron_registry import neuron_trace
        with neuron_trace("self_book_detect", model="Flash") as trace:
            trace.set_input(prompt[:500])
            raw = await flash_llm_func(prompt)
            trace.set_output((raw or "")[:300])

        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("self_book response is not a JSON object")
        mark_triggered_date(target_date)
        entries = data.get("entries", [])
        if not entries:
            logger.info("自我书检测: 无新信号")
            return "0"

        created = 0
        for item in entries:
            if not isinstance(item, dict) or not item.get("body"):
                continue
            body = str(item["body"]).strip()[:300]
            if not body:
                continue
            name = (item.get("name") or "").strip()[:20] or body[:20]
            category = (item.get("category") or "自我认知").strip()
            if category not in VALID_CATEGORIES:
                category = "自我认知"
            keywords = item.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            aliases = item.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]

            if _is_duplicate(existing, name, keywords, body):
                logger.info(f"自我书检测: 去重跳过「{name}」")
                continue
            _write_entry(name, category, keywords, aliases, body, source_date=target_date)
            created += 1

        sb.reload()
        logger.info(f"自我书检测: 新建 {created} 条")
        return str(created)
    except Exception as e:
        logger.warning(f"自我书检测失败: {e}")
        if raise_on_error:
            raise
        return ""
