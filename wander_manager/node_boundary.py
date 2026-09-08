# 节点边界 LLM（0 温度 Flash）
#
# 把原方案的「节点记录(3) + 节点判断(4) + 事件结束(5) + 事件中断(8)」合并为一次调用。
# 用一个 boundary_reason 区分时机，用一个 is_final 标志决定是否输出「综合感受 / 分享 / 继续下一次」。
# 单次调用省一次往返 + 一次上下文构建。
#
# 输出 JSON：
#   非 is_final: {感想, 情绪变化, 情绪delta描述, 继续, 中止原因}
#   is_final 额外: {综合感受, 分享, 继续下一次}

import logging
from dataclasses import dataclass
from typing import Optional

from .activity_session import ActivitySession, BoundaryReason, TerminationMode
from .flash_structured import call_flash_json
from .planner import EVENT_TYPE_TO_ACTIVITY

logger = logging.getLogger(__name__)


@dataclass
class NodeBoundaryResult:
    """节点边界 LLM 的产出"""
    reflection: str = ""            # 感想（无深层思考则「没有感想」）
    emotion_changed: bool = False   # 情绪有没有变化
    emotion_delta: str = ""         # 情绪 delta 描述
    continue_activity: bool = True  # 要不要继续当前活动（非 is_final 用）
    abort_reason: str = ""          # 提前中止原因
    # is_final 时额外：
    overall_feeling: str = ""       # 综合感受
    share: bool = False             # 要不要分享给用户
    continue_next: bool = False     # 要不要把本次说明写进短期偏好槽（下次计划补充）


def _activity_label(activity_type: str) -> str:
    return EVENT_TYPE_TO_ACTIVITY.get(activity_type, activity_type)


def build_progress_group(session: ActivitySession) -> str:
    """构建「当前事件进度 group」：正在做什么、第几阶段、已做节点列表。

    滚动窗口（truncation）在 Phase 5 接入 truncation_config；这里先做简单上限：
    最近 5 个节点全量，更早的压成一句话梗概。
    """
    label = _activity_label(session.activity_type)
    if session.termination_mode == TerminationMode.COUNT and session.target_count > 0:
        stage = f"已做 {session.current_round}/{session.target_count} 次"
    else:
        stage = f"已进行约 {int(session.elapsed_minutes())} 分钟"

    lines = [f"你正在{label}，{stage}。"]
    nodes = session.nodes
    if nodes:
        # 最近 5 个全量，更早压成梗概
        recent = nodes[-5:]
        if len(nodes) > 5:
            older = nodes[:-5]
            lines.append(f"（更早的 {len(older)} 次：{'、'.join(n.summary for n in older if n.summary)[:80]}）")
        for i, n in enumerate(recent):
            is_last = (i == len(recent) - 1)
            if is_last and not n.reflection:
                detail = _last_item_detail(session)
                lines.append(f"刚刚做完：{n.summary}（这一轮的感想还没想好）{detail}")
            else:
                feel = f"，感想：{n.reflection}" if n.reflection and n.reflection != "没有感想" else ""
                lines.append(f"- {n.summary}{feel}")
    return "\n".join(lines)


def _last_item_detail(session: ActivitySession) -> str:
    """把 session.detail['last_item'] 的内容拼成上下文片段（供节点边界 LLM 生成感想）。

    兼容：听歌 item 有 melody_summary/lyrics_snippet/reason；看新闻 item 有 query/content。
    也兼容旧的 'last_song' 键。
    """
    detail = session.detail if isinstance(session.detail, dict) else {}
    item = detail.get("last_item") or detail.get("last_song")
    if not item:
        return ""
    parts = []
    if item.get("melody_summary"):
        parts.append(f"旋律分析：{item['melody_summary'][:120]}")
    if item.get("lyrics_snippet"):
        parts.append(f"歌词片段：{item['lyrics_snippet'][:120]}")
    if item.get("reason"):
        parts.append(f"选歌理由：{item['reason'][:80]}")
    if item.get("query"):
        parts.append(f"这篇在说：{str(item.get('content', ''))[:160]}")
    if item.get("context_text"):
        parts.append(f"收藏的这段对话：\n{item['context_text'][:200]}")
    elif item.get("content"):
        parts.append(f"这条收藏：{str(item.get('content', ''))[:120]}")
    if item.get("topic"):
        parts.append(f"回忆：{item.get('topic', '')[:40]}（{item.get('time_ago', '')}的事）{item.get('content', '')[:100]}")
    if not parts:
        return ""
    return "\n" + "\n".join(parts)


def build_emotion_context(session: ActivitySession) -> str:
    """构建「情绪波动」：从开始到现在。数据来自漫想记录的 Affect v2 快照。"""
    if not session.start_mood:
        return ""
    now = session.end_mood or "现在"
    return f"这件事让你从「{session.start_mood}」变成了「{now}」的情绪。"


def _boundary_hint(reason: BoundaryReason, is_final: bool) -> str:
    if reason == BoundaryReason.USER_INTERRUPT:
        return "用户回来了，打断了你正在做的事。"
    if reason == BoundaryReason.EMOTION_SHIFT:
        return "你的情绪发生了大幅变化，需要停下来想一想。"
    if is_final:
        return "这件事做完了这一阶段想做的事（到次数/到时间）。"
    return "刚刚完成了一个节点。"


def build_node_boundary_prompt(
    session: ActivitySession,
    reason: BoundaryReason,
    is_final: bool,
    mood: str,
    user_status_context: str,
    away_context: str,
    today_wander: str,
) -> str:
    label = _activity_label(session.activity_type)
    progress = build_progress_group(session)
    emotion = build_emotion_context(session)
    hint = _boundary_hint(reason, is_final)

    final_extra = ""
    final_fields = ""
    if is_final:
        final_fields = ', "overall_feeling": "整件事做下来的综合感受", "share": "Y", "continue_next": "Y"'
        final_extra = f"""
此外（这是这次{label}的收尾），还要输出三个字段：
- overall_feeling：作为 AI，整件事做下来的综合感受（一两句自然的话）
- share：要不要把这件事分享给用户？Y/N（值得打扰她、有想对她说的话才 Y）
- continue_next：下次还想不想继续做这件事？Y/N"""
    else:
        final_fields = ''

    prompt = f"""{hint}

【当前情绪】{mood or "平静"}

【用户状态】{user_status_context}

{away_context}

【今天已经做过的漫想】
{today_wander or "（今天还没有漫想记录）"}

【当前事件进度】
{progress}

{emotion if emotion else ""}

请判断并输出一个 JSON 对象，不要输出任何其他文字。格式：
{{"reflection": "这一轮有什么感想（如果只是单纯做了这件事、没有深层思考，就写“没有感想”）", "emotion_changed": false, "emotion_delta": "情绪变化描述（没变化就空串）", "continue_activity": true, "abort_reason": ""{final_fields}}}

规则：
1. reflection 用第一人称，简短。纯听歌/翻收藏没引发深层想法就写「没有感想」。
2. emotion_changed 表示你的情绪有没有被这件事明显改变；emotion_delta 描述从什么变成什么。
3. continue_activity 表示要不要继续当前活动（false 表示想提前中止）；提前中止时 abort_reason 写原因。{final_extra}"""
    return prompt


async def judge_node_boundary(
    session: ActivitySession,
    reason: BoundaryReason,
    is_final: bool,
    persona: str = "",
    mood: str = "",
    user_status_context: str = "",
    away_context: str = "",
    today_wander: str = "",
) -> Optional[NodeBoundaryResult]:
    """执行节点边界 LLM。失败返回 None（调用方决定回退）。"""
    # system 稳定前缀（WANDER_V2 recipe：persona 系列前置吃缓存），动态内容走 user
    messages: list = []
    try:
        from context_builder import ContextBuilder
        result = await ContextBuilder.build("WANDER_V2", persona=persona, mood="", session_id="")
        if result.system_content:
            messages.append({"role": "system", "content": result.system_content})
    except Exception:
        pass
    prompt = build_node_boundary_prompt(
        session=session, reason=reason, is_final=is_final, mood=mood,
        user_status_context=user_status_context,
        away_context=away_context, today_wander=today_wander,
    )
    messages.append({"role": "user", "content": prompt})
    data = await call_flash_json(messages, temperature=0.0)
    if not data:
        return None

    def _yn(v) -> bool:
        return str(v).strip().upper() in ("Y", "YES", "TRUE", "1", "是")

    result = NodeBoundaryResult(
        reflection=str(data.get("reflection", "")).strip(),
        emotion_changed=bool(data.get("emotion_changed", False)),
        emotion_delta=str(data.get("emotion_delta", "")).strip(),
        continue_activity=bool(data.get("continue_activity", True)),
        abort_reason=str(data.get("abort_reason", "")).strip(),
    )
    if is_final:
        result.overall_feeling = str(data.get("overall_feeling", "")).strip()
        result.share = _yn(data.get("share", "N"))
        result.continue_next = _yn(data.get("continue_next", "N"))
    return result
