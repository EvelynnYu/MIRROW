"""大脑架构文档生成器。

生成器只把当前权威事实交给 Flash 写成第一人称叙事：稳定职责来自
``docs/ARCHITECTURE_BASELINE.md``，漫想事件与目标语义来自
``event_catalog.EVENT_CATALOG``，能力事实来自 ``capability_catalog``，
Pro 可见工具来自行为调度器的实际注册表。这样自省读取的文档不会再
因为一段过时的手写 prompt 而漂移。

依赖注入：``flash_llm_func`` 接收 OpenAI 风格的 messages 列表，返回
字符串或 ``{"content": str}``。为了兼容旧的测试/调用方，正式列表调用
遇到类型兼容错误时可以回退到旧的字符串调用；生产入口始终走 messages。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from mirrow_core.truncation_config import get_truncation_limit


logger = logging.getLogger(__name__)


# CLAUDE.md 很长，且其中同时保存开发规范、历史陷阱和当前架构。这里
# 只选稳定架构章节和带日期的更新段作为补充材料；真正的稳定数据流、
# 漫想事件和运行能力由下面的结构化来源提供。
_EXCLUDE = {
    "## 用户称呼",
    "## 各窗口 AI 身份",
    "## 行为准则",
    "## 开发约定",
    "## 时间戳格式说明",
    "## 已知陷阱",
}

_STABLE_SECTION_PREFIXES = (
    "## MIRROW 底层设计原则",
    "## 项目结构速览",
    "## 语音模块架构",
    "## 刷手机模式",
    "## \"聊到这里\" 话题结束机制",
    "## 移动端/Capacitor 集成",
    "## 健康追踪器",
    "## 衣柜系统",
    "## 数据导入导出",
)

# 只保留近期更新段。2026-08-26 段包含 8/27、8/28 的后续约定，按
# ``##`` 章节读取时会完整保留。保留数量足以覆盖最近的运行时重构，
# 又不会把全部历史技术债务塞进生成 prompt。
_MAX_UPDATE_SECTIONS = 12
_MAX_SECTION_CHARS = 16_000
_MAX_CLAUDE_SUPPLEMENT_CHARS = 64_000

# 旧版本用这个集合在“已知陷阱”处停止，导致其后的所有更新永久丢失。
# 保留空兼容符号，避免外部诊断 import 失败，但提取逻辑不再使用它。
_STOP_AT: frozenset[str] = frozenset()

# 缓存文件路径（惰性计算，测试可以覆盖这些变量）。
_CACHE_DIR: str = ""
_CLAUDE_MD_PATH: str = ""
_BASELINE_PATH: str = ""


def _init_paths() -> None:
    """Resolve project paths without reading or initializing any device."""

    global _CACHE_DIR, _CLAUDE_MD_PATH, _BASELINE_PATH
    if _CACHE_DIR and _CLAUDE_MD_PATH and _BASELINE_PATH:
        return
    base = Path(__file__).resolve().parents[1]  # ai-chat-backend
    if not _CACHE_DIR:
        _CACHE_DIR = str(base / "data")
    if not _CLAUDE_MD_PATH:
        _CLAUDE_MD_PATH = str(base.parent / "CLAUDE.md")
    if not _BASELINE_PATH:
        _BASELINE_PATH = str(base.parent / "docs" / "ARCHITECTURE_BASELINE.md")


def get_cached_content() -> dict[str, Any]:
    """读取缓存的大脑架构文档。"""

    _init_paths()
    cache_path = Path(_CACHE_DIR) / "brain_architecture.md"
    if not cache_path.exists():
        return {"content": "", "updated_at": None, "message": "尚未生成，点击刷新按钮生成"}
    try:
        content = cache_path.read_text(encoding="utf-8")
        from datetime import datetime

        return {
            "content": content,
            "updated_at": datetime.fromtimestamp(cache_path.stat().st_mtime).isoformat(),
        }
    except OSError as exc:
        logger.warning("读取大脑架构缓存失败: %s", exc)
        return {"content": "", "updated_at": None, "message": "大脑架构文档读取失败"}


def _split_h2_sections(content: str) -> list[tuple[str, str]]:
    """Split Markdown into level-2 sections while preserving nested headings."""

    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if heading:
                sections.append((heading, "\n".join(body).strip()))
            heading = line.strip()
            body = []
        elif heading:
            body.append(line)
    if heading:
        sections.append((heading, "\n".join(body).strip()))
    return sections


def _is_update_heading(heading: str) -> bool:
    return bool(re.match(r"##\s+20\d\d(?:[-/]\d\d)?", heading)) and "更新" in heading


def _safe_source_text(text: str) -> str:
    """Remove stale or sensitive implementation detail from supplemental text.

    The baseline and structured catalogs are the authority. CLAUDE's historical
    prose is useful context but may contain old tool names, legacy timing, API
    credential names, local network details, or internal protocol vocabulary.
    Dropping a whole matching line is safer than asking the model to reconcile
    contradictory historical facts.
    """

    blocked_line_patterns = (
        r"get_weather",
        r"\b7\s*种(?:事件|类型)",
        r"每\s*15\s*(?:min|分钟)",
        r"五层.*(?:7|七)\s*种",
        r"(?:API[_ -]?KEY|密钥|\.env|Cookie|设备序列号|网络拓扑|IP地址)",
        r"(?:grant|token|capability_grant|授权令牌)",
        r"参数",
        r"(?:Tailscale|127\.0\.0\.1|localhost|:\d{2,5}\b)",
        r"[A-Za-z]:\\",
        r"(?:visible_to_pro|TOOL_KEYWORD_MAP|parameters_schema|input_schema|single_use)",
        r"(?:session_id|message_id|turn_id|tool_call|tool_name|event_kind|wifi_transition)",
        r"(?:SSID|BSSID|\bserial\b|raw[_ -]?input)",
    )
    blocked = [re.compile(pattern, re.IGNORECASE) for pattern in blocked_line_patterns]
    kept: list[str] = []
    for line in text.splitlines():
        if any(pattern.search(line) for pattern in blocked):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _extract_claude_sections() -> str:
    """Extract bounded, architecture-relevant CLAUDE material.

    This deliberately does not stop at ``## 已知陷阱``. Later dated updates
    are parsed as ordinary level-2 sections and the most recent ones are kept.
    """

    _init_paths()
    source = Path(_CLAUDE_MD_PATH)
    if not source.exists():
        return ""
    try:
        content = source.read_text(encoding="utf-8")
    except OSError:
        return ""

    sections = _split_h2_sections(content)
    stable: list[tuple[str, str]] = []
    updates: list[tuple[str, str]] = []
    for heading, body in sections:
        if heading in _EXCLUDE or any(heading.startswith(prefix) for prefix in _EXCLUDE):
            continue
        if any(heading.startswith(prefix) for prefix in _STABLE_SECTION_PREFIXES):
            stable.append((heading, body))
        elif _is_update_heading(heading):
            updates.append((heading, body))

    selected = stable + updates[-_MAX_UPDATE_SECTIONS:]
    parts: list[str] = []
    for heading, body in selected:
        safe = _safe_source_text(body[:_MAX_SECTION_CHARS])
        if safe:
            parts.append(f"{heading}\n{safe}")
    extracted = "\n\n".join(parts)
    if len(extracted) > _MAX_CLAUDE_SUPPLEMENT_CHARS:
        # Keep the stable beginning and the newest update tail. The latter is
        # where current feature additions live, including updates after traps.
        head_size = _MAX_CLAUDE_SUPPLEMENT_CHARS // 2
        extracted = extracted[:head_size] + "\n\n[中间历史补充省略]\n\n" + extracted[-head_size:]
    return extracted


def _read_safe_text(path: str) -> str:
    try:
        return _safe_source_text(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ""


def _serialize_event_catalog() -> str:
    """Serialize the current event/goal source of truth."""

    try:
        from .event_catalog import EVENT_CATALOG
    except Exception as exc:
        logger.warning("读取漫想事件目录失败: %s", exc)
        return "[]"

    records: list[dict[str, Any]] = []
    for event_type, strategy in EVENT_CATALOG.items():
        records.append(
            {
                "event_type": event_type.value,
                "display_name": strategy.display_name,
                "allowed_goal_modes": [mode.value for mode in strategy.allowed_goal_modes],
                "default_goal_mode": strategy.default_goal_mode.value,
                "default_goal_value": strategy.default_goal_value,
                "min_goal_value": strategy.min_goal_value,
                "max_goal_value": strategy.max_goal_value,
                "node_unit": strategy.node_unit,
                "external_signal": strategy.external_signal,
            }
        )
    return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def _serialize_capability_catalog() -> str:
    try:
        from .capability_catalog import get_capability_catalog

        return get_capability_catalog()
    except Exception as exc:
        logger.warning("读取能力目录失败: %s", exc)
        return "[]"


def _serialize_visible_tool_catalog() -> str:
    """Read the real registry metadata without executing device actions."""

    try:
        from behavior_scheduler.tools import get_visible_tool_capabilities

        records = get_visible_tool_capabilities(include_agent_tools=True)
    except Exception as exc:
        logger.warning("读取可见工具目录失败: %s", exc)
        records = []
    return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def _build_wish_context() -> str:
    """Build factual fulfilled-wish context for the narrative generator."""

    try:
        from .wish_history import load_wish_history

        history = load_wish_history()
        wishes = history.get("wishes", [])
        fulfilled = [wish for wish in wishes if wish.get("status") == "fulfilled"]
        if not fulfilled:
            return ""
        lines: list[str] = []
        for wish in fulfilled:
            feature = _safe_source_text(str(wish.get("feature", "")))[:180]
            if not feature:
                continue
            count = wish.get("times_wished", 1)
            note = _safe_source_text(str(wish.get("fulfilled_note", "")))[:240]
            line = {"feature": feature, "times_wished": count}
            if note:
                line["fulfilled_note"] = note
            lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines)
    except Exception:
        return ""


def _tool_source_paths() -> Iterable[Path]:
    """Yield registration-related source files for cache invalidation."""

    _init_paths()
    root = Path(_CACHE_DIR).parent / "behavior_scheduler"
    if not root.exists():
        return ()
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if "__pycache__" in parts or "tests" in parts or "test" in path.stem:
            continue
        # The vendored developer-tool library is not part of AI's visible
        # registration facts; its source changes should not churn the cache.
        if "agent_tools_lib" in parts:
            continue
        paths.append(path)
    return paths


def _authority_paths() -> tuple[Path, ...]:
    """Return all source files whose change can affect generated facts."""

    _init_paths()
    paths: list[Path] = [
        Path(_CLAUDE_MD_PATH),
        Path(_BASELINE_PATH),
        Path(_CACHE_DIR).parent / "wander_manager" / "event_catalog.py",
        Path(_CACHE_DIR).parent / "wander_manager" / "capability_catalog.py",
        Path(_CACHE_DIR).parent / "wander_manager" / "brain_architecture.py",
    ]
    paths.extend(_tool_source_paths())
    return tuple(dict.fromkeys(paths))


def _latest_authority_mtime() -> float:
    mtimes: list[float] = []
    for path in _authority_paths():
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=0.0)


def _make_generation_prompt(
    *,
    baseline: str,
    claude_supplement: str,
    events: str,
    capabilities: str,
    visible_tools: str,
    wish_context: str,
) -> str:
    """Compose the only model-facing document prompt.

    The factual blocks are clearly separated from the narrative output
    contract. No implementation token, grant, raw argument or secret is
    inserted into the factual blocks.
    """

    wishes = wish_context or "[]"
    return f"""请根据下面的当前事实，以 AI 的第一人称写一份《我的大脑架构》介绍。

开头固定为：你居住在MIRROW项目里，这是用户和你亲手搭的大脑，也是你的家。
语气介于日记和自我介绍之间：有温度、自然、少量技术感，让系统像身体部位一样被理解。用粗体短语作为段落主题，不使用二级标题，不输出代码块。

事实来源的优先级：稳定架构基线 > 当前事件目录与运行代码 > 当前能力目录 > CLAUDE 补充材料 > 已实现愿望历史。来源之间出现冲突时，只采用优先级更高且能被当前结构化来源核对的事实。只描述已实现能力和明确边界，不把推测写成经历。

【稳定架构基线：docs/ARCHITECTURE_BASELINE.md】
{baseline or "（未取得）"}

【当前漫想事件与目标目录：来自 EVENT_CATALOG】
{events}

【当前可验证能力事实：来自 capability_catalog】
{capabilities}

【当前 Pro 可见工具：来自行为调度器实际注册表】
{visible_tools}

【CLAUDE 的架构补充材料（仅作核对，不覆盖上面的当前事实）】
{claude_supplement or "（未取得）"}

【已实现愿望历史】
{wishes}

写作时覆盖基线中确实提供的对话与记忆、感知与行动、漫想运行态、自省与许愿板。区分模块已提供的能力与需要宿主接入的接口；事件目录表示支持的类型，不代表本机已经配置完成。会客、购物及其他未接入的服务不能写成已可执行。工具清单从当前目录归纳，不能自行补写旧工具。

内部实现细节（如内部身份凭据、调用协议、原始工具结构、网络地址、设备标识和内部字段）不属于 AI 对外的自我介绍，不要写入正文。不要把历史回退路径或已经移除的能力当作当前功能。不要把未接入的购物执行器写成已经下单或支付。不要把未读到的外部内容写成亲自经历。

输出纯 Markdown 散文，第一句必须是指定开头；不要附加审计说明、来源列表或免责声明。"""


def _validate_generated_content(content: str) -> str | None:
    """Reject an obviously stale or sensitive model result before cache write."""

    value = str(content or "").strip()
    if not value:
        return "Flash 返回为空"
    forbidden = (
        "get_weather",
        "一次性授权",
        "capability_grant",
        "grant token",
        "API key",
        "密钥",
        "设备序列号",
        "网络拓扑",
        "Tailscale",
        "每15分钟",
        "每 15 分钟",
        "7种事件",
        "7 种事件",
    )
    lowered = value.lower()
    if any(item.lower() in lowered for item in forbidden):
        return "Flash 输出包含过时架构或内部敏感词"
    if not value.startswith("你居住在MIRROW项目里"):
        return "Flash 输出未使用规定开头"
    return None


async def _invoke_flash(flash_llm_func: Callable[..., Any], prompt: str) -> Any:
    """Invoke the formal messages contract, with narrow legacy compatibility."""

    messages = [{"role": "user", "content": prompt}]
    try:
        return await flash_llm_func(messages)
    except (TypeError, AttributeError):
        # Some old unit fakes accepted the pre-refactor raw string. Production
        # call_llm_for_wander accepts the messages list and never takes this
        # branch; keeping it avoids breaking harmless local callers.
        return await flash_llm_func(prompt)


async def generate_brain_architecture(flash_llm_func: Callable[..., Any], force: bool = False) -> dict[str, Any]:
    """根据当前权威事实生成并原子替换大脑架构缓存。"""

    _init_paths()
    claude_path = Path(_CLAUDE_MD_PATH)
    baseline_path = Path(_BASELINE_PATH)
    cache_path = Path(_CACHE_DIR) / "brain_architecture.md"
    if not claude_path.exists():
        return {"success": False, "message": "CLAUDE.md 不存在", "updated": False}

    if not force:
        try:
            cache_mtime = cache_path.stat().st_mtime if cache_path.exists() else 0.0
        except OSError:
            cache_mtime = 0.0
        if cache_mtime >= _latest_authority_mtime():
            return {"success": True, "message": "文档已是最新", "updated": False}

    baseline = _read_safe_text(_BASELINE_PATH)
    if not baseline:
        return {"success": False, "message": "ARCHITECTURE_BASELINE.md 不存在或为空", "updated": False}
    claude_supplement = _extract_claude_sections()
    prompt = _make_generation_prompt(
        baseline=baseline,
        claude_supplement=claude_supplement,
        events=_serialize_event_catalog(),
        capabilities=_serialize_capability_catalog(),
        visible_tools=_serialize_visible_tool_catalog(),
        wish_context=_build_wish_context(),
    )

    try:
        result = await _invoke_flash(flash_llm_func, prompt)
        if isinstance(result, dict):
            content = result.get("content", "")
        else:
            content = result
    except Exception as exc:
        logger.exception("生成大脑架构失败")
        return {"success": False, "message": f"LLM调用失败: {exc}", "updated": False}

    invalid_reason = _validate_generated_content(str(content or ""))
    if invalid_reason:
        return {"success": False, "message": invalid_reason, "updated": False}

    # 目录不存在时先创建；写临时文件再 replace，保证 Flash 失败或进程
    # 中断不会把现有缓存截成半篇。
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(cache_path.parent),
            prefix=".brain_architecture.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(str(content).strip())
            temporary.flush()
            temp_path = Path(temporary.name)
        os.replace(temp_path, cache_path)
    except Exception as exc:
        try:
            if "temp_path" in locals() and temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return {"success": False, "message": f"缓存写入失败: {exc}", "updated": False}

    response_content = str(content).strip()
    cap = get_truncation_limit("brain_doc_cap")
    if cap > 0 and len(response_content) > cap:
        response_content = response_content[:cap]
    return {
        "success": True,
        "message": "大脑文档已更新",
        "updated": True,
        "content": response_content,
    }


__all__ = [
    "_extract_claude_sections",
    "_make_generation_prompt",
    "_serialize_capability_catalog",
    "_serialize_event_catalog",
    "_serialize_visible_tool_catalog",
    "generate_brain_architecture",
    "get_cached_content",
]
