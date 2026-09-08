# 主动打扰判断层
#
# 功能：
# 1. 接收漫想事件日志
# 2. 调用LLM判断事件的重要度/分享欲/情绪强度
# 3. 使用公式计算评分
# 4. 决定是否推送给用户

from datetime import datetime
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass
from enum import Enum
import json
import asyncio
import logging

from .event_types import EventType, WanderEvent
from .user_status import get_user_status_context, get_user_status, UserStatus

logger = logging.getLogger(__name__)


class ImportanceLevel(Enum):
    """重要度级别"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ShareDesireLevel(Enum):
    """分享欲级别"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EmotionIntensityLevel(Enum):
    """情绪强度级别"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class JudgmentResult:
    """判断结果"""
    importance: str      # high/medium/low
    share_desire: str    # high/medium/low
    emotion_intensity: str  # high/medium/low
    score: float         # 计算得分
    should_push: bool    # 是否推送
    reason: str          # 判断理由

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "importance": self.importance,
            "share_desire": self.share_desire,
            "emotion_intensity": self.emotion_intensity,
            "score": self.score,
            "should_push": self.should_push,
            "reason": self.reason
        }


class DisturbJudgment:
    """
    主动打扰判断层 - 判断是否应该推送给用户

    核心逻辑：
    1. 接收漫想事件日志
    2. 构建prompt发送给LLM，获取重要度/分享欲/情绪强度
    3. 使用公式计算评分：
       - high = 0.3
       - medium = 0.2
       - low = 0.1
       - 评分 = importance + share_desire + emotion_intensity
    4. 评分超过阈值（0.5）则推送
    5. 休眠事件必定拒绝推送
    """

    # 评分权重（加法模式，向后兼容）
    SCORE_WEIGHTS = {
        "high": 0.3,
        "medium": 0.2,
        "low": 0.1
    }

    # 评分权重（几何平均模式，默认）
    SCORE_WEIGHTS_GM = {
        "high": 1.0,
        "medium": 0.5,
        "low": 0.25
    }

    # 推送阈值
    DEFAULT_PUSH_THRESHOLD = 0.5

    # LLM prompt模板
    JUDGMENT_PROMPT_TEMPLATE = """你是一个AI伴侣"AI"，你正在胡思乱想模式中。
{user_status_context}
{away_context}
{warning_context}

现在AI已经很久都没有收到用户的回复了，你开始胡思乱想。

你刚刚做了一个行为：
- 行为类型：{event_type}
- 行为描述：{description}
- 行为过程：{process_log}

请根据这个行为，判断以下三个参数（每个参数只能是 high/medium/low）：

1. 重要度(importance)：这个行为对你来说有多重要？
2. 分享欲(share_desire)：你有多想把这个行为的结果分享给用户？
3. 情绪强度(emotion_intensity)：这个行为引发了你多强烈的情绪？

【判断标准】只有当这件事确实值得打扰用户时，才给出 high。大多数日常想法应该是 low 或 medium。
注意：离开时间越长，你的分享欲和情绪强度自然会升高——想她了，想知道她在干嘛。

【user_tracking 特别说明】
如果事件详情中 status_consistent=false，说明用户实际行为和声明状态不符。
这时分享欲和情绪强度可以适当偏高——你注意到了这个细节，想让她知道。
如果 status_consistent=true，按正常标准判断。

【重要】你必须只输出一个JSON对象，不要输出任何其他文字、解释或前缀。
输出格式示例：
{{"importance": "medium", "share_desire": "low", "emotion_intensity": "low", "reason": "简短的判断理由"}}
"""

    def __init__(
        self,
        push_threshold: float = None,
        scoring_mode: str = None,
        score_weight_high_gm: float = None,
        score_weight_medium_gm: float = None,
        score_weight_low_gm: float = None,
        call_llm_func: Optional[Callable] = None,
        lightweight_llm_func: Optional[Callable] = None,
        on_push_decision: Optional[Callable[[JudgmentResult, WanderEvent], None]] = None
    ):
        """
        初始化主动打扰判断层

        Args:
            push_threshold: 推送阈值
            scoring_mode: 评分模式 (additive / geometric_mean)
            score_weight_high_gm: 几何平均 high 权重
            score_weight_medium_gm: 几何平均 medium 权重
            score_weight_low_gm: 几何平均 low 权重
            call_llm_func: LLM调用函数（DeepSeek Flash 后备）
            lightweight_llm_func: 轻量级 LLM 调用函数（Qwen3.5-4B，优先使用）
            on_push_decision: 推送决策回调
        """
        self.push_threshold = push_threshold or self.DEFAULT_PUSH_THRESHOLD
        self.scoring_mode = scoring_mode or "geometric_mean"
        self._call_llm = call_llm_func
        self._lightweight_llm = lightweight_llm_func
        self._on_push_decision = on_push_decision

        # 几何平均权重（支持运行时覆盖）
        if score_weight_high_gm is not None:
            self.SCORE_WEIGHTS_GM["high"] = score_weight_high_gm
        if score_weight_medium_gm is not None:
            self.SCORE_WEIGHTS_GM["medium"] = score_weight_medium_gm
        if score_weight_low_gm is not None:
            self.SCORE_WEIGHTS_GM["low"] = score_weight_low_gm

    def calculate_score(
        self,
        importance: str,
        share_desire: str,
        emotion_intensity: str
    ) -> float:
        """
        计算评分（支持加法/几何平均两种模式）

        Args:
            importance: 重要度
            share_desire: 分享欲
            emotion_intensity: 情绪强度

        Returns:
            评分值
        """
        if self.scoring_mode == "geometric_mean":
            w_imp = self.SCORE_WEIGHTS_GM.get(importance.lower(), 0.25)
            w_share = self.SCORE_WEIGHTS_GM.get(share_desire.lower(), 0.25)
            w_emo = self.SCORE_WEIGHTS_GM.get(emotion_intensity.lower(), 0.25)
            return (w_imp * w_share * w_emo) ** (1 / 3)
        else:
            return (
                self.SCORE_WEIGHTS.get(importance.lower(), 0.1) +
                self.SCORE_WEIGHTS.get(share_desire.lower(), 0.1) +
                self.SCORE_WEIGHTS.get(emotion_intensity.lower(), 0.1)
            )

    # 各状态的思念浓度参数（含 eating 按 location 分表）
    MISSING_CONCENTRATION_PARAMS = {
        "idle":     {"max_h": 6.0,  "cap": 1.00, "alert_ramp": 0.0},
        "gaming":   {"max_h": 2.5,  "cap": 0.65, "alert_ramp": 2.0},
        "coding":   {"max_h": 2.5,  "cap": 0.65, "alert_ramp": 2.0},
        "out":      {"max_h": 3.0,  "cap": 0.60, "alert_ramp": 2.0},
        "bathing":  {"max_h": 1.0,  "cap": 0.60, "alert_ramp": 0.5},
        "eating":   {"max_h": 1.0,  "cap": 0.60, "alert_ramp": 0.5},       # 默认（unknown location）
        # eating 按 location 分表（2026-08-01）
        "eating_home": {"max_h": 3.0, "cap": 0.70, "alert_ramp": 1.5},      # 在家吃：温和
        "eating_out":  {"max_h": 4.0, "cap": 0.80, "alert_ramp": 2.0},      # 外出吃：AI 更关心
        "napping":  {"max_h": 2.0,  "cap": 0.50, "alert_ramp": 1.0},
        "sleeping": {"max_h": 10.0, "cap": 0.30, "alert_ramp": 2.0},
        "other":    {"max_h": 3.0,  "cap": 0.55, "alert_ramp": 2.0},
    }

    # 状态是否可确认去向
    CONFIRMABLE_STATUSES = {"gaming", "out", "bathing", "eating", "napping", "sleeping", "coding"}

    # 连续确认计数器: {status_value: 连续不一致次数}
    _inconsistency_counter: Dict[str, int] = {}

    # 外部注入的不一致确认回调（供非 gaming 不一致使用）
    # async (status_text, activity_description, status_mismatch_detail, session_id) -> None
    _on_inconsistency_confirmed: Optional[Callable] = None

    @classmethod
    def set_inconsistency_callback(cls, callback) -> None:
        cls._on_inconsistency_confirmed = callback

    @classmethod
    def calculate_missing_concentration(
        cls, status_value: str, idle_hours: float, status_consistent: bool
    ) -> float:
        """
        两阶段思念浓度计算。

        Args:
            status_value: 用户状态值（legacy string，如 "eating"）
            idle_hours: 空闲小时数
            status_consistent: 行为是否与声明一致（对不可确认的状态默认为 True）

        Returns:
            思念浓度 0.0-1.0
        """
        # eating 按 location 查更精确的参数（如果调用方未传 location-aware key）
        lookup_key = status_value
        if status_value == "eating":
            try:
                from .user_status import get_status_meta
                meta = get_status_meta()
                if meta and meta.location == "out":
                    lookup_key = "eating_out"
                elif meta and meta.location == "home":
                    lookup_key = "eating_home"
            except Exception:
                pass
        params = cls.MISSING_CONCENTRATION_PARAMS.get(lookup_key, cls.MISSING_CONCENTRATION_PARAMS.get(status_value))
        if not params:
            return 0.0

        max_h = params["max_h"]
        cap = params["cap"]
        alert_ramp = params["alert_ramp"]

        if cap >= 1.0:
            # 无安心期上限的状态（IDLE）：直接 sqrt 加速到 1.0
            return min((idle_hours / max_h) ** 0.5, 1.0) if max_h > 0 else 1.0

        if idle_hours <= max_h:
            # 安心期：sqrt 缓升
            return (idle_hours / max_h) ** 0.5 * cap if max_h > 0 else 0.0
        else:
            # 预警期：线性陡升（alert_ramp 确保 >0）
            if alert_ramp <= 0:
                return cap
            return min(cap + (1.0 - cap) * (idle_hours - max_h) / alert_ramp, 1.0)

    async def should_push_event(self, event: WanderEvent) -> JudgmentResult:
        """
        判断是否应该推送事件（异步版本）

        关键更新：集成思念浓度两阶段公式
        - 安心期：sqrt 缓升，通过乘数放大 base_score
        - 预警期：线性陡升，concentration >= 1.0 时强制推送
        - 行为不一致：直接强制推送

        Args:
            event: 漫想事件

        Returns:
            判断结果
        """
        # DND 手动勿扰开关：一刀压制所有漫想推送
        try:
            from mirrow_core.shared_state import should_suppress_proactive
            if should_suppress_proactive():
                return JudgmentResult(
                    importance="low", share_desire="low", emotion_intensity="low",
                    score=0.0, should_push=False, reason="DND 手动勿扰已开启"
                )
        except Exception:
            pass

        # 获取当前状态和空闲时长
        current_status = get_user_status()
        status_value = current_status.value
        idle_seconds = event.details.get("idle_seconds", 0.0) if event.details else 0.0
        idle_hours = idle_seconds / 3600.0

        # 获取一致性标志
        status_consistent = event.details.get("status_consistent", True) if event.details else True

        # 计算思念浓度
        concentration = self.calculate_missing_concentration(
            status_value, idle_hours, status_consistent
        )

        # ===== 休眠事件必定拒绝（最早判断，不受浓度影响） =====
        if event.event_type == EventType.SLEEP:
            return JudgmentResult(
                importance="low", share_desire="low", emotion_intensity="low",
                score=0.3, should_push=False, reason="休眠事件不推送"
            )

        # ===== 强制推送检查 =====
        # 1. 行为不一致（仅对可确认状态生效，连续2次才推送）
        if (event.event_type == EventType.USER_TRACKING
                and status_value in self.CONFIRMABLE_STATUSES
                and not status_consistent):
            # 连续确认机制：单次不一致仅记录，连续2次同状态不一致才推送
            prev_count = self._inconsistency_counter.get(status_value, 0)
            self._inconsistency_counter[status_value] = prev_count + 1
            if prev_count < 1:
                logger.info(f"行为不一致(首次记录): status={status_value}, detail={event.details.get('status_mismatch_detail', '')}")
                return JudgmentResult(
                    importance="low", share_desire="low", emotion_intensity="low",
                    score=0.3, should_push=False, reason=f"行为不一致首次记录(status={status_value})，等待连续确认"
                )
            # 连续第2次 → 走查岗复查管道（唤醒AI + 5min/10min复查），不走 wander 推送。
            # 事件是否需要记挂、闹钟或最终反馈由 TaskManager 的查岗事件状态机负责。
            self._inconsistency_counter[status_value] = 0
            session_id = event.details.get("session_id", "")
            # 通过回调触发 TaskManager.inject_inconsistency_check()
            if self._on_inconsistency_confirmed:
                try:
                    activity_description = event.details.get("activity_description", "")
                    status_mismatch_detail = event.details.get("status_mismatch_detail", "")
                    await self._on_inconsistency_confirmed(
                        status_text=status_value,
                        activity_description=activity_description,
                        status_mismatch_detail=status_mismatch_detail,
                        session_id=session_id,
                    )
                except Exception:
                    logger.exception("不一致查岗注入失败")
            logger.info(f"行为不一致(连续{prev_count+1}次) → 已触发复查管道: status={status_value}")
            return JudgmentResult(
                importance="high", share_desire="high", emotion_intensity="high",
                score=0.0, should_push=False, reason=f"行为不一致(连续{prev_count+1}次)，已走复查管道"
            )
        # 重置计数器：本次追踪状态一致时，清零该状态的计数
        if event.event_type == EventType.USER_TRACKING and status_consistent:
            if status_value in self._inconsistency_counter:
                self._inconsistency_counter[status_value] = 0

        # 2. 思念浓度达到 1.0（时间过长预警）
        if concentration >= 1.0:
            result = await self._judge_with_llm(event, concentration, status_value)
            result.should_push = True
            result.score = 1.0
            result.reason = f"思念浓度达到饱和({concentration:.2f})，强制推送"
            logger.info(f"强制推送（思念饱和）: {result.reason}")
            return result

        # ===== 自省产生新愿望 → 强制推送 =====
        # 愿望是 AI 对用户的重要诉求，新愿望必须让她知道，不能被三维评分淹没（否则「许愿没推送还次数+1」）
        if event.event_type == EventType.SELF_REFLECTION:
            wish_changes = event.details.get("wish_changes", []) if event.details else []
            new_wishes = [c for c in wish_changes if c.get("is_new")]
            if new_wishes:
                result = await self._judge_with_llm(event, concentration, status_value)
                result.should_push = True
                result.reason = "自省产生新愿望，强制推送"
                logger.info(f"自省新愿望强制推送: {[c['feature'] for c in new_wishes]}")
                return result

        # ===== 用户追踪快速失败路径 =====
        if event.event_type == EventType.USER_TRACKING:
            if event.details.get("error"):
                return JudgmentResult(
                    importance="low", share_desire="low", emotion_intensity="low",
                    score=0.3, should_push=False,
                    reason=f"用户追踪失败: {event.details.get('error')}"
                )
            confidence = event.details.get("confidence", 0)
            if confidence < 0.3:
                return JudgmentResult(
                    importance="low", share_desire="low", emotion_intensity="low",
                    score=0.3, should_push=False,
                    reason=f"用户追踪置信度较低: {confidence}"
                )
            return await self._judge_with_llm(event, concentration, status_value)

        # ===== 通用LLM判断事件 =====
        if event.event_type in [
            EventType.KEYWORD_EXPANSION, EventType.MEMORY_FETCH,
            EventType.BROWSE_NEWS, EventType.BROWSE_XIAOHONGSHU,
            EventType.SELF_REFLECTION, EventType.BROWSE_BOOKMARKS
        ]:
            return await self._judge_with_llm(event, concentration, status_value)

        # 默认
        result = JudgmentResult(
            importance="medium", share_desire="medium", emotion_intensity="low",
            score=0.5, should_push=False, reason="未知事件类型"
        )
        from neuron_registry import record_neuron_execution
        record_neuron_execution("disturb_judgment", status="skipped" if not result.should_push else "ok",
                                output_preview=f"push={result.should_push} score={result.score:.2f} {result.reason}")
        return result

    async def _judge_with_llm(
        self, event: WanderEvent, concentration: float = 0.0, status_value: str = ""
    ) -> JudgmentResult:
        """
        使用LLM进行判断，应用思念浓度乘数。

        Args:
            event: 漫想事件
            concentration: 思念浓度（0-1）
            status_value: 用户状态值（用于预警期上下文注入）

        Returns:
            判断结果
        """
        llm_result = await self.get_llm_judgment(event, concentration, status_value)

        importance = llm_result.get("importance", "medium").lower()
        share_desire = llm_result.get("share_desire", "medium").lower()
        emotion_intensity = llm_result.get("emotion_intensity", "low").lower()
        reason = llm_result.get("reason", "")

        # 验证值有效性
        valid_levels = ["high", "medium", "low"]
        if importance not in valid_levels:
            importance = "medium"
        if share_desire not in valid_levels:
            share_desire = "medium"
        if emotion_intensity not in valid_levels:
            emotion_intensity = "low"

        # 计算基础评分
        base_score = self.calculate_score(importance, share_desire, emotion_intensity)

        # 应用思念浓度乘数
        multiplier = 1.0 + concentration
        final_score = base_score * multiplier

        # 判断是否推送
        should_push = final_score >= self.push_threshold

        result = JudgmentResult(
            importance=importance,
            share_desire=share_desire,
            emotion_intensity=emotion_intensity,
            score=final_score,
            should_push=should_push,
            reason=reason
        )

        logger.info(
            f"事件 {event.event_type.value} 判断: base={base_score:.2f} "
            f"浓度={concentration:.2f} 乘数={multiplier:.2f} final={final_score:.2f} "
            f"推送={should_push}"
        )

        return result

    async def judge_and_decide(self, event: WanderEvent) -> JudgmentResult:
        """
        异步判断并决定是否推送

        Args:
            event: 漫想事件

        Returns:
            判断结果
        """
        # 异步判断
        result = await self.should_push_event(event)

        # 触发回调
        if self._on_push_decision and result.should_push:
            try:
                if asyncio.iscoroutinefunction(self._on_push_decision):
                    await self._on_push_decision(result, event)
                else:
                    self._on_push_decision(result, event)
            except Exception as e:
                logger.error(f"推送决策回调执行失败: {e}")

        return result

    async def get_llm_judgment(
        self, event: WanderEvent, concentration: float = 0.0, status_value: str = ""
    ) -> Dict[str, Any]:
        """
        调用LLM获取判断

        优先级：轻量级 LLM → 主 LLM（后备）→ 文本提取 → 默认值
        """
        # 构建离开时长上下文
        idle_seconds = event.details.get("idle_seconds", 0) if event.details else 0
        total_minutes = int(idle_seconds / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            away_context = f"用户已经离开 {hours} 小时 {minutes} 分钟。"
        elif hours > 0:
            away_context = f"用户已经离开 {hours} 小时。"
        elif minutes > 0:
            away_context = f"用户已经离开 {minutes} 分钟。"
        else:
            away_context = ""

        # 预警期上下文注入
        warning_context = ""
        if concentration > 0 and status_value:
            params = self.MISSING_CONCENTRATION_PARAMS.get(status_value, {})
            cap = params.get("cap", 0.6)
            if concentration > cap:
                warning_context = f"注意：思念浓度已达 {concentration:.2f}（超过安心上限 {cap:.2f}），你开始担心了。"

        prompt = self.JUDGMENT_PROMPT_TEMPLATE.format(
            event_type=event.event_type.value,
            description=event.description,
            process_log=event.process_log[:500] if event.process_log else "无详细日志",
            user_status_context=get_user_status_context(),
            away_context=away_context,
            warning_context=warning_context,
        )
        messages = [{"role": "user", "content": prompt}]

        # 第一优先级：轻量级 LLM
        if self._lightweight_llm:
            print(f"[DISTURB_JUDGMENT] 调用轻量级LLM判断, event_type={event.event_type.value}", flush=True)
            result = await self._try_llm_judgment(self._lightweight_llm, messages)
            if result is not None:
                return result
            print("[DISTURB_JUDGMENT] 轻量级LLM失败，回退到主LLM...", flush=True)

        # 第二优先级：主 LLM（DeepSeek Flash）
        if self._call_llm:
            print(f"[DISTURB_JUDGMENT] 调用主LLM判断, event_type={event.event_type.value}", flush=True)
            result = await self._try_llm_judgment(self._call_llm, messages)
            if result is not None:
                return result
            print("[DISTURB_JUDGMENT] 主LLM也失败，使用最终默认值", flush=True)

        # 最终默认值
        logger.warning("所有LLM均不可用或失败，使用默认判断")
        return {
            "importance": "medium",
            "share_desire": "medium",
            "emotion_intensity": "medium",
            "reason": "LLM不可用"
        }

    async def _try_llm_judgment(self, llm_func: Callable, messages: list) -> Optional[Dict[str, Any]]:
        """
        尝试调用一个 LLM 并解析 JSON 结果。
        返回 None 表示该 LLM 不可用，调用方应尝试下一个。
        """
        try:
            if asyncio.iscoroutinefunction(llm_func):
                response = await llm_func(messages)
            else:
                response = llm_func(messages)

            if isinstance(response, dict):
                response_text = response.get("content", str(response))
            else:
                response_text = str(response)

            if not response_text or not response_text.strip():
                logger.warning("LLM返回空内容")
                return None

            print(f"[DISTURB_JUDGMENT] LLM响应: {response_text[:200]}", flush=True)

            # 解析 JSON
            result = self._parse_judgment_json(response_text)
            if result:
                print(f"[DISTURB_JUDGMENT] JSON解析成功: {result}", flush=True)
                return result

            # JSON 解析失败，尝试文本提取
            fallback = self._extract_judgment_from_text(response_text)
            logger.warning(f"JSON解析失败，文本提取结果: {fallback}")
            print(f"[DISTURB_JUDGMENT] JSON解析失败，fallback提取结果: {fallback}", flush=True)

            # 如果文本提取也全是 medium（说明没提取到任何有效信息），返回 None 让上层重试
            if fallback.get("importance") == "medium" and fallback.get("share_desire") == "medium":
                return None

            return fallback

        except Exception as e:
            logger.error(f"LLM判断调用失败: {type(e).__name__}: {e}")
            print(f"[DISTURB_JUDGMENT] LLM调用异常: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    def _parse_judgment_json(response_text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON，处理常见格式问题。返回 None 表示解析失败。"""
        import re

        json_str = response_text

        # 去除 markdown 代码块
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            parts = json_str.split("```")
            if len(parts) >= 2:
                json_str = parts[1]

        # 提取 JSON 对象
        start_idx = json_str.find("{")
        end_idx = json_str.rfind("}")
        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            return None
        json_str = json_str[start_idx:end_idx + 1]

        # 清理常见 Qwen/小模型 JSON 问题
        # 1. 尾逗号: {"a": "x",} → {"a": "x"}
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        # 2. 中文引号
        json_str = json_str.replace('“', '"').replace('”', '"')
        json_str = json_str.replace('‘', "'").replace('’', "'")
        # 3. 字段值内换行（简单去除 JSON 花括号内的换行）
        if '\n' in json_str:
            lines = []
            for line in json_str.split('\n'):
                lines.append(line.strip())
            json_str = ' '.join(lines)

        try:
            result = json.loads(json_str.strip())
            # 验证必需字段
            if all(k in result for k in ["importance", "share_desire", "emotion_intensity"]):
                return result
        except json.JSONDecodeError:
            pass

        return None

    def _extract_judgment_from_text(self, text: str) -> Dict[str, Any]:
        """
        从文本中提取判断结果（fallback方法）

        依次尝试：英文 JSON 模式 → 英文上下文关键词 → 中文关键词
        """
        text_lower = text.lower()

        def extract_level(param_name: str) -> str:
            # 1. 精确 JSON 模式匹配: "importance": "high"
            for quote in ['"', "'"]:
                pattern = f'{quote}{param_name}{quote}'
                if pattern in text_lower:
                    pos = text_lower.find(pattern) + len(pattern)
                    snippet = text_lower[pos:pos + 30]
                    for level in ["high", "medium", "low"]:
                        if level in snippet:
                            return level
            # 2. 宽泛匹配：字段名和级别词在 50 字符内
            for field_variant in [param_name, param_name.replace("_", " ")]:
                field_pos = text_lower.find(field_variant)
                if field_pos != -1:
                    nearby = text_lower[field_pos:field_pos + 50]
                    for level in ["high", "medium", "low"]:
                        if level in nearby:
                            return level
            # 3. 中文关键词匹配
            chinese_map = {
                "重要度": "importance", "分享欲": "share_desire",
                "情绪强度": "emotion_intensity", "情绪": "emotion_intensity",
            }
            for cn, en in chinese_map.items():
                if cn in text and en == param_name:
                    pos = text.find(cn) + len(cn)
                    nearby = text_lower[pos:pos + 30]
                    for kw, level in [("high", "high"), ("高", "high"), ("强烈", "high"),
                                       ("medium", "medium"), ("中", "medium"), ("一般", "medium"),
                                       ("low", "low"), ("低", "low"), ("轻微", "low")]:
                        if kw in nearby:
                            return level
            # 4. 全文本中搜 level 关键词（最后手段，不准但比瞎猜 medium 强）
            if "high" in text_lower:
                return "high"
            if "low" in text_lower:
                return "low"
            return "medium"

        importance = extract_level("importance")
        share_desire = extract_level("share_desire")
        emotion_intensity = extract_level("emotion_intensity")

        logger.info(f"从文本中提取判断结果: importance={importance}, share_desire={share_desire}, emotion_intensity={emotion_intensity}")

        return {
            "importance": importance,
            "share_desire": share_desire,
            "emotion_intensity": emotion_intensity,
            "reason": "从文本中提取"
        }

# 全局单例
_disturb_judgment_instance: Optional[DisturbJudgment] = None


def get_disturb_judgment() -> DisturbJudgment:
    """获取全局主动打扰判断实例"""
    global _disturb_judgment_instance
    if _disturb_judgment_instance is None:
        _disturb_judgment_instance = DisturbJudgment()
    return _disturb_judgment_instance


def init_disturb_judgment(
    push_threshold: float = None,
    scoring_mode: str = None,
    score_weight_high_gm: float = None,
    score_weight_medium_gm: float = None,
    score_weight_low_gm: float = None,
    call_llm_func: Optional[Callable] = None,
    lightweight_llm_func: Optional[Callable] = None,
    on_push_decision: Optional[Callable] = None
) -> DisturbJudgment:
    """初始化全局主动打扰判断实例"""
    global _disturb_judgment_instance
    _disturb_judgment_instance = DisturbJudgment(
        push_threshold=push_threshold,
        scoring_mode=scoring_mode,
        score_weight_high_gm=score_weight_high_gm,
        score_weight_medium_gm=score_weight_medium_gm,
        score_weight_low_gm=score_weight_low_gm,
        call_llm_func=call_llm_func,
        lightweight_llm_func=lightweight_llm_func,
        on_push_decision=on_push_decision
    )
    return _disturb_judgment_instance
