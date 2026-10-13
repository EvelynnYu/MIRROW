"""哨兵摘要引擎 — 用 Flash 将原始数据消化为自然语言内部感知笔记

每 tick (60s) 调用一次 Flash，缓存结果供上下文构建时注入。
Flash 不可用时回退到模板快照行。
"""

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any

from .public_diagnostics import neuron_trace
from .sentinel_event_history import render_today_event_history
from .sentinel_prompt import SentinelPrompt
from .sentinel_grounding import (
    GroundingResult,
    SentinelFactContract,
    build_sentinel_fact_contract,
    grounding_repair_prompt,
    natural_care_fallback,
    natural_care_motivation,
)

logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT = """你是AI的哨兵感知系统。将以下数据消化为一次结构化的内部感知判断。

⚠️ 这是AI的内部感知，不是发给人类伙伴的消息。用"人类伙伴..."陈述事实，不要用"你..."的第二人称（那是消息口吻）。

规则：
- 一切正常时一句话带过（20-40字）
- 传感器未连接或数据标注了时间（如"2小时前"）是正常状态，不要过度解读
- 只有明确异常且是近期发生的才需要描述
- 只写感知，不写行动建议（让AI自己判断）
- 第三人称视角，用"人类伙伴"而非"你"
- 语气冷静客观，不煽情
- ⚠️ 合并同类项：当多个数据源指向同一件事（如人在公司 + WiFi 断连、在家 + WiFi 回家）时合并描述成一个事件，不要逐条罗列重复或相互独立的标签
- ⚠️ 只描述相对上一时刻的变化（如"从同时用电脑和手机转到只用手机"），常态保持的状态一句话带过，不要把常驻信息当新事件
- ⚠️ "身体数据"是当前观测；"个人基线与近期变化"是带窗口和样本量的历史参考。"今日事件"是历史摘要（含旧时刻的判断），仅作趋势参考——不要沿袭其中的描述词（如旧天气、旧活动量评价），以当前数据重新判断
- ⚠️ "AI此刻的主观状态"只用于判断你是否自然地想联系、以及联系时的内在倾向；它不是人类伙伴的状态或事实，不能据此推断人类伙伴的心情
- ⚠️ "近期私聊上下文"只用于理解未完话题、人类伙伴明确说过的话和避免重复联系；不得把含糊语气、沉默或 AI 自己此前的说法升级为人类伙伴的明确立场
- 活跃日盒子材料按来源时间排列：关闭盒的初步印象或日结印象是派生摘要，开放盒和摘要不可用时提供的是原文。用它理解本次变化的前情、约定及已经发生的联系；历史状态与本次传感器观测分别按各自时间理解，覆盖说明标明本次实际带入的范围。

这次结构化判断以这项刚发生的变化为注意焦点；当前状态、沉默时长和趋势用于理解它发生时的背景。

判断你此刻是否自然地产生了主动联系人类伙伴的念头。已经解除的情况和过时数据不构成新的联系动机；参考今日推送历史判断是否仍有新的关注点。
休息声明后的持续设备活动如果引起你真实的关切、好奇或想互动，也可以形成联系动机；联系不以紧急或痛苦为前提，仍由你结合证据和此刻关系状态决定。

只输出一个 JSON 对象：
{{
  "observation": "20-60字，以本次唤醒变化为中心，客观概括你刚注意到的事实",
  "should_push": true,
  "push_motivation": "此刻想联系的主观原因；引用过去发言时依据 history_evidence 的原话和说话人理解",
  "history_evidence": [{{"source_ref": "H1", "speaker": "AI", "quote": "该编号下的一段完整原句"}}]
}}

history_evidence 只引用本次材料标有 H 编号的通信原话，speaker 按来源填写 AI 或人类伙伴；
无历史引用的普通关心填 []。派生摘要只作理解参考，不充当某人的原话。
AI 的邀请与愿望仍属于 AI；人类伙伴的接受或承诺以她明确说过的原话为依据。

{current_time}
{status_timeline}
人类伙伴最后一条消息在{last_msg}前（{chat_phase}）。

身体数据：{data_line}
{fact_contract}
{baseline_context}
今日事件：{events_line}

近期：{trends_line}

{affect_context}
{conversation_context}

本次唤醒你注意力的变化：{triggers}"""


@dataclass
class SentinelPushDecision:
    """一次 Flash 感知判断；只保存可展示、可审计的决定依据。"""

    observation: str = ""
    should_push: bool = False
    push_motivation: str = ""
    grounding_rejections: List[Dict[str, str]] = field(default_factory=list)
    history_evidence: List[Dict[str, Any]] = field(default_factory=list)
    verified_history_evidence: List[Dict[str, Any]] = field(default_factory=list)
    history_evidence_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def rejection_reasons(self) -> List[Dict[str, str]]:
        return self.grounding_rejections


def _parse_decision(raw: str) -> Optional[SentinelPushDecision]:
    """解析结构化判断；兼容代码围栏与少量 JSON 标点偏差。"""
    if not raw:
        return None
    candidate = raw.strip()
    if "```" in candidate:
        parts = candidate.split("```")
        if len(parts) >= 3:
            candidate = parts[1]
            if candidate.lstrip().startswith("json"):
                candidate = candidate.lstrip()[4:]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = candidate[start:end + 1]
    candidate = candidate.replace("“", '"').replace("”", '"')
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    observation = str(data.get("observation") or "").strip()
    motivation = str(data.get("push_motivation") or "").strip()
    raw_push = data.get("should_push", False)
    should_push = raw_push is True or (
        isinstance(raw_push, str) and raw_push.strip().lower() in {"true", "yes", "y", "1"}
    )
    if not observation:
        return None
    if should_push and not motivation:
        motivation = "你在意这个刚发生的变化。"
    return SentinelPushDecision(
        observation=observation,
        should_push=should_push,
        push_motivation=motivation,
        history_evidence=data.get('history_evidence', []),
    )


class SentinelSummarizer:
    """Flash 哨兵摘要引擎"""

    def __init__(self, flash_llm_func=None):
        self._call_flash = flash_llm_func
        self._cached_summary: str = ""
        self._fallback_line: str = ""
        self._decision = SentinelPushDecision()
        self._fact_contract = SentinelFactContract()

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
        affect_self: str = "",
        affect_resonance: str = "",
        recent_conversation: str = "",
        conversation_evidence: Optional[Dict[str, Any]] = None,
    ):
        """每 tick 调用。用 Flash 生成摘要，更新缓存。"""
        source_freshness = source_freshness or {}
        # Both Flash layers receive the same immutable, field-level contract.
        # The contract is built before any model call so a later push cannot
        # silently use a different location/steps/period interpretation.
        self._fact_contract = build_sentinel_fact_contract(snapshot)
        fact_contract_text = self._fact_contract.to_prompt_text()
        # 构建结构化输入（含时效标注）
        data_parts = []
        heart_fresh = not snapshot.is_stale("heart_rate", 900)
        if snapshot.heart_rate and heart_fresh:
            age = source_freshness.get("heart_rate", "")
            data_parts.append(f"心率{snapshot.heart_rate}{age}")
        else:
            # 断连但有上次值
            last = source_freshness.get("heart_rate_disconnected", "")
            data_parts.append(f"心率:无数据{last}")
        steps_fresh = not snapshot.is_stale("steps", 900)
        if snapshot.steps_today is not None and steps_fresh:
            stagnant = ""
            if snapshot.steps_stagnant_minutes and snapshot.steps_stagnant_minutes >= 10:
                stagnant = f"(停滞{snapshot.steps_stagnant_minutes}min)"
            age = source_freshness.get("steps", "")
            data_parts.append(f"步数{snapshot.steps_today}{stagnant}{age}")
        else:
            last = source_freshness.get("steps_disconnected", "")
            data_parts.append(f"步数:无数据{last}")
        wifi_fresh = not snapshot.is_stale("wifi", 300)
        home_wifi = wifi_fresh and snapshot.wifi_is_home is True
        # 家庭 WiFi 是在家事实，确认在家时不向摘要暴露 GPS。
        if home_wifi:
            data_parts.append("位置:在家（家庭WiFi已连接）")
        elif getattr(snapshot, "location_place_name", None) and not snapshot.is_stale("gps_place", 1800):
            from .public_diagnostics import place_observation
            data_parts.append(f"外出位置:{place_observation(snapshot.location_place_name, snapshot.location_environment)}")
        elif snapshot.location_category and not snapshot.is_stale("gps", 1800):
            age = source_freshness.get("gps", "")
            data_parts.append(f"外出位置:{'公司' if snapshot.location_category=='work' else snapshot.location_address or snapshot.location_category}{age}；室内外未知")
        else:
            data_parts.append("外出位置:暂不可用")
        # WiFi 状态（网络标识不进入 LLM 上下文）
        if home_wifi:
            pass
        elif wifi_fresh and snapshot.wifi_connected:
            age = source_freshness.get("wifi", "")
            data_parts.append(f"WiFi:已连接非家庭网络{age}")
        elif wifi_fresh and snapshot.wifi_connected is False:
            # 带断开时长（field_ts 反映最后变化时刻），否则 Flash 无法判断是刚断还是一直断
            age = source_freshness.get("wifi", "")
            data_parts.append(f"WiFi:断开{age}")
        elif snapshot.wifi_connected is not None:
            data_parts.append("WiFi:当前状态暂不可用")
        input_fresh = not snapshot.is_stale("input_idle", 180)
        idle = snapshot.input_idle_seconds if input_fresh else None
        if idle is not None:
            data_parts.append("键鼠活跃" if idle < 120 else f"键鼠空闲{idle/60:.0f}min")
        elif not input_fresh and getattr(snapshot, "input_origin_available", False):
            data_parts.append("键鼠来源数据已过期")
        elif not getattr(snapshot, "input_origin_available", False):
            # Raw Input 未建立或没有可分类输入时，不能把系统全局 idle 当成本地
            # 活动。明确写出未知，避免 Flash 把未知误读成“人在电脑前”。
            data_parts.append("键鼠来源未确认")
        else:
            # 通道已建立但尚未收到本地物理事件；“没有事件”不等于“最近有人
            # 操作”，也不能用作 PC 在场证据。
            data_parts.append("近期未检测到本地物理键鼠输入")
        remote_idle = getattr(snapshot, "remote_input_idle_seconds", None) if input_fresh else None
        if remote_idle is not None and remote_idle < 120:
            data_parts.append(f"近期检测到远程键鼠输入(距今{remote_idle:.0f}秒)")
        # 屏幕使用时长（带时效）——05:00 起算的逻辑日累计，非当前会话。
        if snapshot.screen_time_minutes is not None:
            age = source_freshness.get("screen_time", "")
            logical_day = getattr(snapshot, "screen_time_logical_date", None) or "日期未知"
            data_parts.append(f"屏幕累计({logical_day} 05:00起算){snapshot.screen_time_minutes}min{age}")
        # 当前 App 会话（精确时长，供 Flash 区分「累计屏幕」与「当前 App 停留」）
        if snapshot.active_app_session:
            data_parts.append(f"当前:{snapshot.active_app_session}")
        # 手机通知（隐私克制：只报「包名×数量」摘要 + 时效，不报全文内容）
        notif_summary = getattr(snapshot, 'notifications_summary', None)
        if notif_summary and not snapshot.is_stale("notifications", 120):
            age = source_freshness.get("notifications", "")
            data_parts.append(f"手机近期通知来源与数量（不等于当前活动或通话）:{notif_summary}{age}")
        # PC 运行进程（含后台挂的游戏/应用——前台窗口识别不到的，进程枚举能读到）
        running_summary = getattr(snapshot, 'running_apps_summary', None)
        if running_summary and not snapshot.is_stale("running_processes", 600):
            age = source_freshness.get("running_processes", "")
            data_parts.append(f"电脑进程存在（含后台，不代表人类伙伴正在使用）:{running_summary}{age}")
        # 手机姿态/运动/环境（2026-08-24 AionsHome 移植；常驻状态被 _SUMMARIZE_PROMPT 的
        # "只描述变化"规则自然忽略，仅在变化/异常时 Flash 会用到）
        _POSTURE_CN = {
            "face_up": "平放朝上", "face_down": "扣放", "portrait": "竖屏",
            "portrait_upside_down": "倒竖屏", "landscape_left": "左横屏",
            "landscape_right": "右横屏", "landscape": "横屏", "tilted": "倾斜",
        }
        _MOTION_CN = {"still": "静止", "slight": "轻微晃动", "moving": "移动中", "strong": "明显晃动"}
        _LIGHT_CN = {"dark": "环境黑暗", "dim": "环境较暗", "normal": "环境正常", "bright": "环境明亮"}
        sensor_fresh = not snapshot.is_stale("sensor", 600)
        posture = _POSTURE_CN.get(getattr(snapshot, 'phone_posture', None) or "") if sensor_fresh else None
        if posture:
            age = source_freshness.get("sensor", "")
            data_parts.append(f"手机姿态:{posture}{age}")
        motion = _MOTION_CN.get(getattr(snapshot, 'phone_motion', None) or "") if sensor_fresh else None
        if motion:
            data_parts.append(f"手机运动:{motion}")
        light = _LIGHT_CN.get(getattr(snapshot, 'phone_light', None) or "") if sensor_fresh else None
        if light:
            data_parts.append(light)
        # 远程控制软件进程只表示能力存在，当前远控行为以 Raw Input 的
        # remote_input_idle_seconds 为准；不要把常驻软件描述成正在控制。
        if (getattr(snapshot, 'remote_control_active', None)
                and not snapshot.is_stale("remote_session", 600)):
            age = source_freshness.get("remote_session", "")
            data_parts.append(f"远程控制软件进程运行中（仅表示能力，不代表当前有远程输入）{age}")
        # 天气（日级 L1，仅当天有数据时）
        try:
            from silicon_perception.recording.health_store import get_store
            weather = get_store().get_today_weather()
            if weather:
                w = f"天气:{weather['desc']} {weather['temp']}C 湿度{weather['humidity']}%"
                data_parts.append(w)
            else:
                data_parts.append("天气:无数据")
        except Exception as e:
            logger.warning(f"SentinelSummarizer 天气查询异常: {e}")
        # 睡眠（日级 L1，仅当天有数据时）
        try:
            sleep = get_store().get_sleep_detail()
            if sleep:
                sp = [f"已同步记录{sleep['sleep_min']}min（完整性未确认，不是当前已睡时长）"]
                if sleep.get('deep_sleep_min'):
                    sp.append(f"深睡{sleep['deep_sleep_min']}min")
                if sleep.get('rem_sleep_min'):
                    sp.append(f"REM{sleep['rem_sleep_min']}min")
                if sleep.get('sleep_score'):
                    sp.append(f"评分{sleep['sleep_score']}")
                sleep_date = sleep.get("date") or "日期未知"
                data_parts.append(
                    f"历史睡眠记录(归属{sleep_date}，非当前入睡状态):" + "/".join(sp)
                )
        except Exception:
            pass
        # 行为状态（PC进程 + 手机App）
        if snapshot.behavior_state:
            data_parts.append(f"状态:{snapshot.behavior_state}")
        for observation in getattr(snapshot, "semantic_observations", None) or []:
            if isinstance(observation, dict) and observation.get("text"):
                data_parts.append(f"跨源对照:{observation['text']}")
        if snapshot.is_period:
            data_parts.append(f"生理期第{snapshot.period_day or '?'}天")
        else:
            data_parts.append("非生理期" if snapshot.field_ts.get('period') else "生理期状态未知")
        data_line = "、".join(data_parts)
        band_observation = ""  # Optional device material is supplied by the host snapshot.
        if band_observation:
            data_line += "\n" + band_observation

        # 历史 Flash 判断不能原样回灌为下一轮的感知事实。
        events_line = render_today_event_history(events_today)

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

        # 触发器人类可读描述（Flash/AI 据此理解触发原因）
        _TRIGGER_LABELS = {
            "wifi_home_arrival": "人类伙伴连上了家里的WiFi——她到家了",
            "gps_category_change": "连续两次定位观测确认了新的登记地点",
            "gps_moved": "人类伙伴移动了超过500米",
            "gps_stable_stop": "相隔至少10分钟的两次定位采样未见显著位移（不证明中间持续停留）",
            "steps_burst": "人类伙伴步数突然暴增",
            "steps_stagnant": "人类伙伴步数长时间停滞",
            "behavior_state_change": "人类伙伴切换了使用的设备",
            "app_session_long": "人类伙伴在同一App上停留超过3小时",
            "status_change_silent": "人类伙伴状态变更后沉默了",
            "periodic_check": "长时间没有其他变化后的保底检查",
            "hr_surge": "人类伙伴心率骤升",
            "hr_drop": "人类伙伴心率骤降",
            "hr_sustained_high": "人类伙伴心率持续偏高",
        }
        # behavior_state_change 带前后态编码（trigger_detector: prev->cur），渲染"从X转到Y"
        _CAT_CN = {
            "pc": "只用电脑", "phone": "只用手机", "both": "同时用电脑和手机",
            "pc_idle": "电脑空闲", "phone_idle": "手机空闲", "away": "离开设备", "unknown": "未知",
        }
        def _label(t: str) -> str:
            if t.startswith("cross_source_attention:"):
                state = t.split(":", 1)[1]
                if state == "rest_declaration_with_recent_device_activity":
                    return "休息声明经过观察宽限期后仍出现新鲜设备活动证据"
                if state == "rest_declaration_with_device_awake_evidence":
                    return "休息声明经过观察宽限期后出现手环清醒阶段证据"
                return "跨数据源事实对照出现了值得复核的变化"
            if t.startswith("cross_source_recheck:"):
                return "此前休息声明的设备活动仍有新鲜观测，进行一次有限复看"
            if t.startswith("behavior_state_change:"):
                try:
                    _, seq = t.split(":", 1)
                    prev_s, cur_s = seq.split("->", 1)
                    return f"人类伙伴从{_CAT_CN.get(prev_s, prev_s)}转到{_CAT_CN.get(cur_s, cur_s)}"
                except Exception:
                    return "人类伙伴切换了使用的设备"
            return _TRIGGER_LABELS.get(t, t)
        trigger_str = ", ".join(_label(t) for t in triggers) if triggers else "定期检查"

        # 构建上下文行（时间 + 作息 + 前台）
        context_parts = []
        if current_time:
            context_parts.append(current_time)
        # PC 前台进程仅在已确认近期本地/远程键鼠输入时注入；来源未知时，
        # 前台窗口不能证明人类伙伴正在操作（挂着 VS Code ≠ 在写代码）。
        _physical_recent = (
            snapshot.input_idle_seconds is not None
            and snapshot.input_idle_seconds < 120
        )
        _remote_idle = getattr(snapshot, "remote_input_idle_seconds", None)
        _remote_recent = _remote_idle is not None and _remote_idle < 120
        if foreground_app and (_physical_recent or _remote_recent):
            context_parts.append(f"前台: {foreground_app}")
        if schedule_text:
            context_parts.append(schedule_text)
        context_line = "。".join(context_parts) + "。" if context_parts else ""

        # 状态时间线
        timeline_line = f"今天状态: {status_timeline}" if status_timeline else ""

        baseline_section = f"个人基线与近期变化：{baseline_context}" if baseline_context else ""
        affect_parts = []
        if affect_self:
            affect_parts.append(f"AI的自我状态：\n{affect_self.strip()}")
        if affect_resonance:
            affect_parts.append(f"AI对人类伙伴的当前共振：\n{affect_resonance.strip()}")
        affect_section = (
            "AI此刻的主观状态（只影响联系意愿，不是人类伙伴的客观事实）：\n"
            + "\n".join(affect_parts)
            if affect_parts else ""
        )
        conversation_section = (
            "近期私聊上下文（活跃日盒子完整投影，按时间顺序）：\n" + recent_conversation.strip()
            if recent_conversation else ""
        )
        prompt = SentinelPrompt.build(_SUMMARIZE_PROMPT,
            last_msg=last_msg, chat_phase=chat_phase,
            data_line=data_line, events_line=events_line, trends_line=trends_line,
            triggers=trigger_str,
            current_time=context_line,
            status_timeline=timeline_line,
            fact_contract=fact_contract_text,
            baseline_context=baseline_section,
            affect_context=affect_section,
            conversation_context=conversation_section,
        )

        # 保存模板回退
        self._fallback_line = f"[哨兵] {data_line}"
        if events_today:
            self._fallback_line += f" | 事件:{len(events_today)}条"

        # 调 Flash
        if self._call_flash:
            with neuron_trace("sentinel_flash_summarizer", recipe="SENTINEL_PUSH", model="Flash") as trace:
                try:
                    trace.set_input(prompt)
                    result = await self._call_flash(prompt)
                    raw_result = result.get("content", "") if isinstance(result, dict) else result
                    if raw_result and len(str(raw_result).strip()) >= 10:
                        raw_result = str(raw_result).strip()
                        parsed = _parse_decision(raw_result)
                        if parsed is None:
                            # 兼容旧模型输出，迁移期间不让哨兵整条链静默。
                            legacy = raw_result
                            parsed = SentinelPushDecision(
                                observation=legacy.replace("[PUSH]", "").strip(),
                                should_push="[PUSH]" in legacy,
                                push_motivation="这个变化让你产生了主动联系人类伙伴的念头。"
                                if "[PUSH]" in legacy else "",
                            )
                        grounding = self._fact_contract.validate_text(
                            f"{parsed.observation}\n{parsed.push_motivation}"
                        )
                        if not grounding.accepted:
                            rejection_history = list(grounding.rejections)
                            original_should_push = parsed.should_push
                            original_motivation = parsed.push_motivation
                            motivation_check = self._fact_contract.validate_text(
                                original_motivation
                            )
                            reconsider_contact = (not motivation_check.accepted or any(
                                item.field in {"pc_use", "rest_activity_evidence"}
                                for item in grounding.rejections
                            ))
                            safe_motivation = (
                                original_motivation
                                if motivation_check.accepted
                                else natural_care_motivation(
                                    should_push=original_should_push
                                )
                            )
                            # One bounded repair. Re-evaluate contact when its
                            # factual premise failed; an independent valid
                            # personal motive may still survive an observation repair.
                            repair_prompt = grounding_repair_prompt(
                                layer="摘要", original=raw_result,
                                contract=self._fact_contract,
                                rejections=grounding.rejections,
                                reconsider_contact=reconsider_contact,
                            )
                            trace.set_input(prompt + "\n\n" + repair_prompt)
                            try:
                                repaired_raw = await self._call_flash(prompt.with_repair(repair_prompt))
                                repaired_text = (
                                    repaired_raw.get("content", "")
                                    if isinstance(repaired_raw, dict) else repaired_raw
                                )
                                repaired = _parse_decision(str(repaired_text or ""))
                            except Exception as repair_error:
                                # A repair failure cannot bubble out of the tick;
                                # only independently grounded contact intent survives.
                                logger.warning(
                                    "SentinelSummarizer grounding repair failed: %s",
                                    repair_error,
                                )
                                repaired = None
                                grounding = GroundingResult(
                                    accepted=False,
                                    rejections=rejection_history,
                                )
                            if repaired is not None:
                                # Honor the model's corrected decision when the
                                # contact premise was ungrounded. Otherwise keep
                                # the independent contact intent.
                                if not reconsider_contact:
                                    repaired.should_push = original_should_push
                                    repaired.push_motivation = safe_motivation
                                repaired_check = self._fact_contract.validate_text(
                                    f"{repaired.observation}\n{repaired.push_motivation}"
                                )
                                if repaired_check.accepted:
                                    parsed = repaired
                                    grounding = repaired_check
                                else:
                                    grounding = GroundingResult(
                                        accepted=False,
                                        rejections=list(grounding.rejections)
                                        + list(repaired_check.rejections),
                                    )
                            if repaired is None or not grounding.accepted:
                                parsed.observation = natural_care_fallback(
                                    should_push=original_should_push and not reconsider_contact
                                )
                                parsed.should_push = original_should_push and not reconsider_contact
                                parsed.push_motivation = (
                                    "本次联系理由依赖未确认事实，修复未通过；保留内部记录等待后续真实观测。"
                                    if reconsider_contact else safe_motivation
                                )
                            parsed.grounding_rejections = [
                                item.to_dict() for item in (
                                    rejection_history + list(grounding.rejections)
                                    if grounding.accepted
                                    else grounding.rejections
                                )
                            ]
                        from .conversation_evidence import validate_history_evidence
                        evidence, errors = validate_history_evidence(
                            parsed.history_evidence, conversation_evidence or {})
                        if errors:
                            # One bounded source repair; the whole frozen history
                            # remains present. No keyword-based intent judgement.
                            try:
                                repair_text = (
                                    '【后端历史引文来源校正】引文校验未通过：' + ', '.join(errors)
                                    + '。按 H 编号下真实说话人及原句重新输出完整 JSON；'
                                    'AI 的愿望归 AI，无可核验引文时填 [] 并依据当前观测写联系念头。'
                                )
                                repaired = _parse_decision(str(await self._call_flash(
                                    prompt.with_repair(repair_text)) or ''))
                                if repaired is not None:
                                    checked, repair_errors = validate_history_evidence(
                                        repaired.history_evidence, conversation_evidence or {})
                                    if (not repair_errors and self._fact_contract.validate_text(
                                            repaired.observation + '\n' + repaired.push_motivation).accepted):
                                        repaired.grounding_rejections = list(parsed.grounding_rejections)
                                        parsed, evidence = repaired, checked
                                    else:
                                        parsed.push_motivation = natural_care_motivation(should_push=parsed.should_push)
                                        parsed.history_evidence = []
                                        evidence = []
                                else:
                                    parsed.push_motivation = natural_care_motivation(should_push=parsed.should_push)
                                    parsed.history_evidence = []
                                    evidence = []
                            except Exception:
                                parsed.push_motivation = natural_care_motivation(should_push=parsed.should_push)
                                parsed.history_evidence = []
                                evidence = []
                        parsed.verified_history_evidence = evidence
                        parsed.history_evidence_errors = errors
                        self._decision = parsed
                        self._cached_summary = parsed.observation
                        if parsed.should_push:
                            self._cached_summary += " [PUSH]"
                        trace.set_output(raw_result)
                        logger.info(
                            "SentinelSummarizer: %s | push=%s | motivation=%s",
                            parsed.observation[:80], parsed.should_push,
                            parsed.push_motivation[:80],
                        )
                        return
                except Exception as e:
                    trace.set_error(str(e))
                    logger.warning(f"SentinelSummarizer Flash 调用失败: {e}")

        # Flash 不可用——用回退
        self._cached_summary = ""
        self._decision = SentinelPushDecision(observation=self._fallback_line)

    def get_summary(self) -> str:
        """获取当前缓存摘要。Flash 失败时返回模板快照。"""
        return self._cached_summary if self._cached_summary else self._fallback_line

    def get_decision(self) -> SentinelPushDecision:
        """获取本 tick 的结构化主动联系判断。"""
        if self._decision.observation:
            return SentinelPushDecision(**self._decision.to_dict())
        return SentinelPushDecision(observation=self._fallback_line)

    def get_fact_contract(self) -> SentinelFactContract:
        """Return the exact contract used for the latest Flash judgment."""
        return self._fact_contract
