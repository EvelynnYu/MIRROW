"""哨兵摘要引擎 — 用 Flash 将原始数据消化为自然语言内部感知笔记

每 tick (60s) 调用一次 Flash，缓存结果供上下文构建时注入。
Flash 不可用时回退到模板快照行。
"""

import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT = """你是AI的哨兵感知系统。将以下数据消化为一段内部感知笔记。

⚠️ 这是AI的内部感知，不是发给用户的消息。用"用户..."陈述事实，不要用"你..."的第二人称（那是消息口吻）。

{current_time}
{status_timeline}
用户最后一条消息在{last_msg}前（{chat_phase}）。

身体数据：{data_line}
{baseline_context}
今日事件：{events_line}

近期：{trends_line}

规则：
- 一切正常时一句话带过（20-40字）
- 传感器未连接或数据标注了时间（如"2小时前"）是正常状态，不要过度解读
- 只有明确异常且是近期发生的才需要描述
- 只写感知，不写行动建议（让AI自己判断）
- 第三人称视角，用"用户"而非"你"
- 语气冷静客观，不煽情

触发原因：{triggers}

如果你作为AI的潜意识，觉得用户现在需要被关心，加 [PUSH]。
已经解除的情况、过时数据不要推。参考今日推送历史避免重复。

输出20-60字，一行。末尾可含 [PUSH]。"""


class SentinelSummarizer:
    """Flash 哨兵摘要引擎"""

    def __init__(self, flash_llm_func=None):
        self._call_flash = flash_llm_func
        self._cached_summary: str = ""
        self._fallback_line: str = ""

    async def update(
        self,
        snapshot,           # DataSnapshot
        events_today: List[Dict[str, Any]],
        trends: Dict[str, Any],
        last_message_seconds: float,
        triggers: List[str] = None,
        source_freshness: Dict[str, str] = None,
        current_time: str = "",
        status_timeline: str = "",
        schedule_text: str = "",
        foreground_app: str = "",
        baseline_context: str = "",
    ):
        """每 tick 调用。用 Flash 生成摘要，更新缓存。"""
        source_freshness = source_freshness or {}
        # 构建结构化输入（含时效标注）
        data_parts = []
        if snapshot.heart_rate:
            age = source_freshness.get("heart_rate", "")
            data_parts.append(f"心率{snapshot.heart_rate}{age}")
        else:
            # 断连但有上次值
            last = source_freshness.get("heart_rate_disconnected", "")
            data_parts.append(f"心率:无数据{last}")
        if snapshot.steps_today is not None:
            stagnant = ""
            if snapshot.steps_stagnant_minutes and snapshot.steps_stagnant_minutes >= 10:
                stagnant = f"(停滞{snapshot.steps_stagnant_minutes}min)"
            age = source_freshness.get("steps", "")
            data_parts.append(f"步数{snapshot.steps_today}{stagnant}{age}")
        else:
            last = source_freshness.get("steps_disconnected", "")
            data_parts.append(f"步数:无数据{last}")
        if snapshot.location_category:
            age = source_freshness.get("gps", "")
            data_parts.append(f"GPS:{'家' if snapshot.location_category=='home' else '公司' if snapshot.location_category=='work' else snapshot.location_address or snapshot.location_category}{age}")
        else:
            data_parts.append("GPS:无数据")
        idle = snapshot.input_idle_seconds
        if idle is not None:
            data_parts.append("键鼠活跃" if idle < 120 else f"键鼠空闲{idle/60:.0f}min")
        # 屏幕使用时长（带时效）
        if snapshot.screen_time_minutes is not None:
            age = source_freshness.get("screen_time", "")
            data_parts.append(f"手机屏幕{snapshot.screen_time_minutes}min{age}")
        # 天气（日级 L1，仅当天有数据时）
        try:
            from silicon_perception.recording.health_store import get_store
            weather = get_store().get_today_weather()
            if weather:
                data_parts.append(f"天气:{weather['desc']} {weather['temp']}°C 湿度{weather['humidity']}%")
        except Exception:
            pass
        # 睡眠（日级 L1，仅当天有数据时）
        try:
            sleep = get_store().get_sleep_detail()
            if sleep:
                sp = [f"总{sleep['sleep_min']}min"]
                if sleep.get('deep_sleep_min'):
                    sp.append(f"深睡{sleep['deep_sleep_min']}min")
                if sleep.get('rem_sleep_min'):
                    sp.append(f"REM{sleep['rem_sleep_min']}min")
                if sleep.get('sleep_score'):
                    sp.append(f"评分{sleep['sleep_score']}")
                data_parts.append("睡眠:" + "/".join(sp))
        except Exception:
            pass
        # 行为状态（PC进程 + 手机App）
        if snapshot.behavior_state:
            data_parts.append(f"状态:{snapshot.behavior_state}")
        if snapshot.is_period:
            data_parts.append(f"生理期第{snapshot.period_day or '?'}天")
        else:
            data_parts.append("非生理期")
        data_line = "、".join(data_parts)

        # 今日事件
        if events_today:
            lines = []
            for e in events_today[:5]:
                ts = e.get("timestamp", "")[11:16] if e.get("timestamp") else ""
                lines.append(f"- {ts} {e.get('message') or e.get('rule_id')}")
            events_line = "\n".join(lines)
        else:
            events_line = "无异常"

        # 趋势
        trend_parts = []
        steps_delta = trends.get("steps_delta", 0)
        if steps_delta:
            arrow = "↑" if steps_delta > 0 else "↓"
            trend_parts.append(f"本周步数{arrow}{abs(steps_delta)}%")
        sleep_delta = trends.get("sleep_delta", 0)
        if sleep_delta:
            arrow = "↑" if sleep_delta > 0 else "↓"
            trend_parts.append(f"睡眠{arrow}{abs(sleep_delta)}%")
        trends_line = "、".join(trend_parts) if trend_parts else "无趋势数据"

        # 背景
        if last_message_seconds > 86400:
            chat_phase = "沉默中"
            last_msg = "超过24小时"
        elif last_message_seconds < 300:
            chat_phase = "对话中"
            last_msg = f"{last_message_seconds:.0f}秒"
        elif last_message_seconds < 3600:
            chat_phase = "刚离开"
            last_msg = f"{last_message_seconds/60:.0f}分钟"
        else:
            chat_phase = "沉默中"
            last_msg = f"{last_message_seconds/3600:.1f}小时"

        trigger_str = ", ".join(triggers) if triggers else "定期检查"

        # 构建上下文行（时间 + 作息 + 前台）
        context_parts = []
        if current_time:
            context_parts.append(current_time)
        # PC 前台进程仅键鼠活跃时注入——空闲时前台窗口无意义（挂着 VS Code ≠ 在写代码）
        if foreground_app and (snapshot.input_idle_seconds is None or snapshot.input_idle_seconds < 120):
            context_parts.append(f"前台: {foreground_app}")
        if schedule_text:
            context_parts.append(schedule_text)
        context_line = "。".join(context_parts) + "。" if context_parts else ""

        # 状态时间线
        timeline_line = f"今天状态: {status_timeline}" if status_timeline else ""

        baseline_section = f"基线偏离：{baseline_context}" if baseline_context else ""
        prompt = _SUMMARIZE_PROMPT.format(
            last_msg=last_msg, chat_phase=chat_phase,
            data_line=data_line, events_line=events_line, trends_line=trends_line,
            triggers=trigger_str,
            current_time=context_line,
            status_timeline=timeline_line,
            baseline_context=baseline_section,
        )

        # 保存模板回退
        self._fallback_line = f"[哨兵] {data_line}"
        if events_today:
            self._fallback_line += f" | 事件:{len(events_today)}条"

        # 调 Flash
        if self._call_flash:
            try:
                result = await self._call_flash(prompt)
                if result and len(result.strip()) >= 10:
                    self._cached_summary = result.strip()
                    logger.info(f"SentinelSummarizer: {self._cached_summary[:80]}")
                    return
            except Exception as e:
                logger.warning(f"SentinelSummarizer Flash 调用失败: {e}")

        # Flash 不可用——用回退
        self._cached_summary = ""

    def get_summary(self) -> str:
        """获取当前缓存摘要。Flash 失败时返回模板快照。"""
        return self._cached_summary if self._cached_summary else self._fallback_line
