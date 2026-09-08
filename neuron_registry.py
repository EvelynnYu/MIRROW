"""
神经元注册表 — MIRROW 所有子 Agent 的 SINGLE SOURCE OF TRUTH。

每个"神经元"是一个自主决策/分析节点：接收上下文 → 思考 → 产出结果。
本模块提供：
  - 31 个神经元的静态声明（_ALL_NEURONS）
  - neuron_trace() context manager（LLM 神经元埋点）
  - record_neuron_execution() 函数（规则引擎/感知型埋点）
  - get_all_neuron_status() / get_neuron_history()（API 数据构建）

用法：
    from neuron_registry import neuron_trace, record_neuron_execution

    # LLM 神经元
    with neuron_trace("sentinel_flash_summarizer", recipe="SENTINEL_PUSH", model="Flash") as trace:
        result = await call_flash(prompt)
        trace.set_input(prompt)
        trace.set_output(result)
        trace.set_thinking(result.get("reasoning", ""))

    # 规则引擎
    record_neuron_execution("trigger_detector", status="ok", duration_ms=5.2,
                            output_preview="触发: hr_surge")
"""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ═══════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════

@dataclass
class NeuronDefinition:
    """神经元静态声明（类似 Recipe 定义）"""
    neuron_id: str                              # 唯一标识
    name: str                                   # 中文名
    category: str                               # "cognitive" | "reflex" | "perception"
    subsystem: str                              # 所属子系统
    consumer: str = ""                          # 对应 Recipe consumer
    trigger_desc: str = ""                      # 触发条件描述
    recipe: str = ""                            # 使用的 Recipe key；硬构建则为 ""
    is_llm: bool = False                        # 是否调 LLM
    llm_model: str = ""                         # 用哪个模型
    description: str = ""                       # 一句话职责说明
    source_file: str = ""                       # 源码位置
    emoji: str = "🧩"                           # 展示图标


@dataclass
class NeuronTrace:
    """运行时执行记录"""
    trace_id: str = ""
    neuron_id: str = ""
    timestamp: str = ""
    status: str = "ok"                          # "ok" | "error" | "skipped"
    duration_ms: float = 0.0
    input_preview: str = ""                     # 截断 300 字符
    output_preview: str = ""                    # 截断 300 字符
    thinking_preview: str = ""                  # 截断 300 字符
    input_full: str = ""
    output_full: str = ""
    thinking_full: str = ""
    error_message: str = ""
    recipe_used: str = ""
    model_used: str = ""


# ═══════════════════════════════════════════════════════════
# 31 个神经元声明
# ═══════════════════════════════════════════════════════════

_ALL_NEURONS: Dict[str, NeuronDefinition] = {}


def _declare_all():
    """模块加载时注册全部 31 个神经元。"""

    def _reg(nid, **kw):
        _ALL_NEURONS[nid] = NeuronDefinition(neuron_id=nid, **kw)

    # ── 认知型 (cognitive) — LLM 驱动 ──

    _reg("sentinel_flash_summarizer",
         name="哨兵感知摘要", category="cognitive", subsystem="silicon_perception",
         consumer="AI哨兵", recipe="SENTINEL_PUSH", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="L1/L2/L3 触发器命中 → tick → Flash",
         description="用 Flash 将传感器数据消化为自然语言内部感知笔记，决定是否 [PUSH]",
         source_file="silicon_perception/analysis/sentinel_summarizer.py",
         emoji="🛡️")

    _reg("wander_message_generator",
         name="漫想消息生成", category="cognitive", subsystem="wander_manager",
         consumer="AI漫想", recipe="WANDER_PUSH", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想事件 + 扰动判断通过 → Flash",
         description="根据漫想事件生成推送到聊天的自然语言消息",
         source_file="wander_manager/message_generator.py",
         emoji="💭")

    _reg("wander_keyword_expansion",
         name="漫想关键词扩展", category="cognitive", subsystem="wander_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想 KEYWORD_EXPANSION 事件 → Flash",
         description="将随机关键词扩展为内省式思考文本",
         source_file="wander_manager/event_handlers.py",
         emoji="💭")

    _reg("wander_memory_fetch",
         name="漫想记忆检索", category="cognitive", subsystem="wander_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想 MEMORY_FETCH 事件 → Flash",
         description="对随机检索到的记忆进行反思并生成推送文本",
         source_file="wander_manager/event_handlers.py",
         emoji="💭")

    _reg("wander_self_reflection",
         name="漫想自我审视", category="cognitive", subsystem="wander_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想 SELF_REFLECTION 事件 → Flash",
         description="自我审计能力+愿望，输出 JSON 结构化结果",
         source_file="wander_manager/event_handlers.py",
         emoji="💭")

    _reg("wander_browse_bookmarks",
         name="漫想收藏翻阅", category="cognitive", subsystem="wander_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想 BROWSE_BOOKMARKS 事件 → Flash",
         description="翻阅收藏夹并生成反思（深读/速览两种模式）",
         source_file="wander_manager/event_handlers.py",
         emoji="💭")

    _reg("wander_user_tracking",
         name="漫想用户追踪", category="cognitive", subsystem="wander_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="漫想 USER_TRACKING 事件 → Flash",
         description="根据屏幕分析生成关心/担忧/游戏搭子消息",
         source_file="wander_manager/event_handlers.py",
         emoji="💭")

    _reg("reminder_recheck_generator",
         name="提醒复查生成", category="cognitive", subsystem="task_scheduler",
         consumer="AI提醒", recipe="REMINDER_PUSH", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="pending reminder 5min/10min 超时无回复 → Flash",
         description="生成提醒复查消息（温和提醒→记账语气）",
         source_file="main.py",
         emoji="⏰")

    _reg("self_task_executor",
         name="自调度执行", category="cognitive", subsystem="task_scheduler",
         consumer="自调度", recipe="SELF_TASK", is_llm=True, llm_model="deepseek-v4-pro",
         trigger_desc="cron 触发 → Pro",
         description="执行自调度任务（含 thinking）",
         source_file="task_scheduler/self_task_executor.py",
         emoji="🤖")

    _reg("diary_generator",
         name="日记生成", category="cognitive", subsystem="event_chronicle",
         consumer="日记", recipe="DIARY", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 + ≥18:00 + ≥10条消息 → Flash",
         description="从全天对话生成日记条目，含 TODO 参考+人设注入",
         source_file="event_chronicle.py",
         emoji="📔")

    _reg("memory_digest",
         name="记忆消化", category="cognitive", subsystem="OB_Rev",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 → grow() → digest() → Flash",
         description="将完整对话拆分为记忆条目（domain/valence/arousal/state_variables）",
         source_file="OB_Rev/dehydrator.py",
         emoji="🧠")

    _reg("memory_analyzer",
         name="记忆分析", category="cognitive", subsystem="OB_Rev",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="hold_with_merge（非 skip_analyze 路径） → Flash",
         description="分析单条记忆条目的 domain/valence/arousal",
         source_file="OB_Rev/dehydrator.py",
         emoji="🔬")

    _reg("topic_boundary_detector",
         name="话题边界检测", category="cognitive", subsystem="OB_Rev",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="digest 输入 >15000 字符 → Flash",
         description="两轮消化第一轮：识别话题边界，分段 digest",
         source_file="OB_Rev/dehydrator.py",
         emoji="✂️")

    _reg("post_topic_todo",
         name="话题分析-TODO", category="cognitive", subsystem="main",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 → 统一分析（3路并行）→ Flash",
         description="从对话中提取新 TODO/画像更新/纪念日+完成检测",
         source_file="main.py",
         emoji="✅")

    _reg("post_topic_bookmark",
         name="话题分析-收藏", category="cognitive", subsystem="main",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 → 统一分析（3路并行）→ Flash",
         description="检测值得收藏的对话片段并自动创建书签",
         source_file="main.py",
         emoji="⭐")

    _reg("post_topic_worldbook",
         name="话题分析-世界书", category="cognitive", subsystem="main",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 → 统一分析（3路并行）→ Flash",
         description="检测新世界书事实并生成建议",
         source_file="main.py",
         emoji="📖")

    _reg("persona_evolution",
         name="人格演化", category="cognitive", subsystem="main",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="话题结束 → Flash",
         description="分析对话检测人格演化建议",
         source_file="persona_evolution.py",
         emoji="🧬")

    _reg("world_book_detector",
         name="世界书检测", category="cognitive", subsystem="world_book",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="半日节流（AM/PM） + ≥5条消息 → Flash",
         description="独立世界书检测：从当日消息中提取新事实建议",
         source_file="world_book/suggestions.py",
         emoji="📖")

    _reg("phone_browse_vision",
         name="刷手机视觉分析", category="cognitive", subsystem="phone_browser",
         is_llm=True, llm_model="glm-4v-flash",
         trigger_desc="summon/capture → GLM-4V（回退 DeepSeek）",
         description="分析手机截屏内容，生成屏幕描述",
         source_file="phone_browser.py",
         emoji="👁️")

    _reg("phone_browse_msg_gen",
         name="刷手机陪聊消息", category="cognitive", subsystem="phone_browser",
         consumer="AI 刷手机", recipe="PHONE_BROWSE", is_llm=True, llm_model="deepseek-v4-pro",
         trigger_desc="视觉分析完成 → Pro（回退 Flash）",
         description="根据屏幕分析生成陪伴式聊天消息",
         source_file="phone_browser.py",
         emoji="📱")

    _reg("music_analysis_summarizer",
         name="音乐分析摘要", category="cognitive", subsystem="voice_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="音乐上传 + basic-pitch 转录完成 → Flash",
         description="将 MIDI 音符数据转为文学性音乐描述",
         source_file="voice_manager/music_analyzer.py",
         emoji="🎵")

    _reg("health_capture_analyzer",
         name="健康截图分析", category="cognitive", subsystem="health_tracker",
         is_llm=True, llm_model="glm-4v-flash",
         trigger_desc="out/sleeping 状态变更 → 手机截屏 → GLM-4V",
         description="分析健康 App 截图提取步数/睡眠/血氧数据",
         source_file="health_tracker/capture.py",
         emoji="💓")

    _reg("flash_intent_extractor",
         name="Flash意图提取", category="cognitive", subsystem="behavior_scheduler",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="每轮 Pro 无 TOOL_CALL + 关键词预筛命中 → Flash",
         description="从 Pro NL 中提取工具调用意图和参数",
         source_file="behavior_scheduler/scheduler.py",
         emoji="⚡")

    _reg("calendar_memorial_msg",
         name="纪念日消息", category="cognitive", subsystem="calendar_manager",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="每日日历扫描（midnight cron）→ Flash",
         description="生成纪念日/生理期个性化提醒消息",
         source_file="calendar_manager/manager.py",
         emoji="📅")

    _reg("group_chat_summarizer",
         name="群聊摘要", category="cognitive", subsystem="group_chat",
         consumer="AI群聊", recipe="GROUP_CHAT_K", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="群聊消息数达标 → Flash",
         description="为 AI 总结群聊上下文（临时摘要+正式摘要）",
         source_file="group_chat/gc_summary.py",
         emoji="👥")

    _reg("peer_moment_capture",
         name="Peer情感快照", category="cognitive", subsystem="group_chat",
         consumer="Peer群聊", recipe="PEER_GROUP", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="每 20+ 条群聊消息 → Flash",
         description="从群聊中捕获 1-3 个 Peer 情感瞬间",
         source_file="group_chat/moments.py",
         emoji="💫")

    _reg("parallel_timeline_compressor",
         name="平行时空压缩", category="cognitive", subsystem="parallel_timeline",
         consumer="平行时空", recipe="PARALLEL_TIMELINE", is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="每 N 轮 → Flash（三层压缩引擎）",
         description="原文→近期摘要→远古大纲 三层压缩",
         source_file="parallel_timeline/compression.py",
         emoji="🌌")

    _reg("music_taste_analyzer",
         name="音乐品味分析", category="cognitive", subsystem="music_router",
         is_llm=True, llm_model="deepseek-v4-flash",
         trigger_desc="POST /api/music/analyze → Flash",
         description="分析网易云听歌历史提取音乐品味",
         source_file="music_router.py",
         emoji="🎶")

    # ── 反射型 (reflex) — 规则引擎 ──

    _reg("sentinel_anomaly_detector",
         name="哨兵异常检测", category="reflex", subsystem="silicon_perception",
         trigger_desc="每 tick（60s） → 规则引擎评估",
         description="6条规则（C1-C2+B1-B2+L1-L2）对比快照与基线",
         source_file="silicon_perception/analysis/anomaly_detector.py",
         emoji="🔔")

    _reg("behavior_state_inferrer",
         name="行为状态推断", category="reflex", subsystem="silicon_perception",
         trigger_desc="每 tick（60s） → 融合键鼠+PC前台+手机前台",
         description="纯规则引擎：合并多源数据推断行为状态枚举",
         source_file="silicon_perception/analysis/behavior_state.py",
         emoji="🎯")

    _reg("trigger_detector",
         name="触发器检测", category="reflex", subsystem="silicon_perception",
         trigger_desc="每 tick（60s） → L1快照对比+L2救命规则+L3定时",
         description="三级触发体系：10条L1+2条L2+1条L3，决定是否唤醒Flash",
         source_file="silicon_perception/analysis/trigger_detector.py",
         emoji="⚙️")

    _reg("disturb_judgment",
         name="扰动判断", category="reflex", subsystem="wander_manager",
         trigger_desc="漫想周期 → 一致性检查",
         description="判断用户状态与手机数据是否一致，连续2次不一致→复查管道",
         source_file="wander_manager/disturb_judgment.py",
         emoji="🚦")

    _reg("decay_engine",
         name="衰减引擎", category="reflex", subsystem="OB_Rev",
         trigger_desc="每日 cron → 艾宾浩斯遗忘曲线",
         description="按时间规则衰减记忆桶：active→dormant→archived",
         source_file="OB_Rev/decay_engine.py",
         emoji="📉")

    _reg("daily_aggregator",
         name="每日聚合", category="reflex", subsystem="silicon_perception",
         trigger_desc="日终 → SQL 聚合查询",
         description="从 health_snapshots 聚合每日健康摘要，唯一写入入口",
         source_file="silicon_perception/analysis/daily_aggregator.py",
         emoji="📊")

    # ── 感知型 (perception) — 数据采集 ──

    _reg("heart_rate_collector",
         name="心率采集", category="perception", subsystem="silicon_perception",
         trigger_desc="每 tick（60s） → PC BLE / 手机 relay 兜底",
         description="从 PC BLE 心率带或手机 BLE 采集实时心率",
         source_file="silicon_perception/collection/heart_rate.py",
         emoji="💗")

    _reg("step_count_collector",
         name="步数采集", category="perception", subsystem="silicon_perception",
         trigger_desc="每 5min → 手机 relay",
         description="通过手机传感器采集当日步数",
         source_file="silicon_perception/collection/step_count.py",
         emoji="🚶")

    _reg("gps_collector",
         name="GPS采集", category="perception", subsystem="silicon_perception",
         trigger_desc="每 10min → 手机 relay → 高德逆地理编码",
         description="采集手机 GPS 坐标并逆地理编码为地址+分类",
         source_file="silicon_perception/collection/gps.py",
         emoji="📍")

    _reg("screen_time_collector",
         name="屏幕时间采集", category="perception", subsystem="silicon_perception",
         trigger_desc="每 30min → 手机 relay",
         description="采集手机屏幕使用时间和前台应用分类",
         source_file="silicon_perception/collection/screen_time.py",
         emoji="📲")


_declare_all()


# ═══════════════════════════════════════════════════════════
# 运行时追踪存储
# ═══════════════════════════════════════════════════════════

_MAX_TRACES = 50                              # 每神经元最多保留 50 条 trace
_trace_buffers: Dict[str, deque] = {}         # neuron_id → deque(maxlen=50)
_stats: Dict[str, dict] = {}                  # neuron_id → 累积统计
_exec_counter: Dict[str, int] = {}            # neuron_id → 全局递增计数器


def _ensure_neuron(neuron_id: str):
    """确保某神经元的 buffer 和 stats 已初始化。"""
    if neuron_id not in _trace_buffers:
        _trace_buffers[neuron_id] = deque(maxlen=_MAX_TRACES)
    if neuron_id not in _stats:
        _stats[neuron_id] = {
            "execution_count": 0,
            "success_count": 0,
            "last_status": "never_executed",
            "last_duration_ms": 0.0,
            "last_timestamp": "",
            "last_ok_at": None,
        }
    if neuron_id not in _exec_counter:
        _exec_counter[neuron_id] = 0


# ═══════════════════════════════════════════════════════════
# 埋点 API
# ═══════════════════════════════════════════════════════════

_TRUNCATE = 300  # 预览截断长度


def _truncate(text: str, max_len: int = _TRUNCATE) -> str:
    """截断文本用于预览。"""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


class NeuronTraceContext:
    """context manager 返回的追踪上下文对象。"""

    def __init__(self, neuron_id: str, recipe: str = "", model: str = ""):
        self._neuron_id = neuron_id
        self._recipe = recipe
        self._model = model
        self._t0 = time.perf_counter()
        self._input_full = ""
        self._output_full = ""
        self._thinking_full = ""
        self._error = ""
        self._status = "ok"
        _ensure_neuron(neuron_id)

    def set_input(self, text: str):
        self._input_full = text

    def set_output(self, text: str):
        self._output_full = text

    def set_thinking(self, text: str):
        self._thinking_full = text

    def set_error(self, error: str):
        self._error = error
        self._status = "error"

    def _finalize(self, exc_type=None, exc_val=None, exc_tb=None):
        """由 __exit__ 调用，写入 buffer 和 stats。"""
        duration_ms = (time.perf_counter() - self._t0) * 1000

        if exc_type is not None:
            self._status = "error"
            if not self._error:
                self._error = f"{exc_type.__name__}: {exc_val}"

        trace = NeuronTrace(
            trace_id=uuid.uuid4().hex[:12],
            neuron_id=self._neuron_id,
            timestamp=datetime.now().isoformat(),
            status=self._status,
            duration_ms=round(duration_ms, 1),
            input_preview=_truncate(self._input_full),
            output_preview=_truncate(self._output_full),
            thinking_preview=_truncate(self._thinking_full),
            input_full=self._input_full,
            output_full=self._output_full,
            thinking_full=self._thinking_full,
            error_message=self._error,
            recipe_used=self._recipe,
            model_used=self._model,
        )

        _ensure_neuron(self._neuron_id)
        _trace_buffers[self._neuron_id].append(trace)
        _exec_counter[self._neuron_id] += 1

        s = _stats[self._neuron_id]
        s["execution_count"] = _exec_counter[self._neuron_id]
        s["last_status"] = trace.status
        s["last_duration_ms"] = trace.duration_ms
        s["last_timestamp"] = trace.timestamp
        s["last_input_preview"] = trace.input_preview
        s["last_output_preview"] = trace.output_preview
        s["last_thinking_preview"] = trace.thinking_preview
        s["last_error_message"] = trace.error_message

        if self._status == "ok":
            s["success_count"] += 1
            s["last_ok_at"] = trace.timestamp

        return False  # 不抑制异常


class neuron_trace:
    """LLM 神经元的 context manager 埋点。

    Usage:
        with neuron_trace("sentinel_flash_summarizer", recipe="SENTINEL_PUSH", model="Flash") as trace:
            result = await call_flash(prompt)
            trace.set_input(prompt)
            trace.set_output(str(result))
            trace.set_thinking(result.get("reasoning", ""))
    """

    def __init__(self, neuron_id: str, recipe: str = "", model: str = ""):
        self._ctx = NeuronTraceContext(neuron_id, recipe=recipe, model=model)

    def __enter__(self):
        return self._ctx

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._ctx._finalize(exc_type, exc_val, exc_tb)


def record_neuron_execution(neuron_id: str, *,
                            status: str = "ok",
                            duration_ms: float = 0.0,
                            input_preview: str = "",
                            output_preview: str = "",
                            input_full: str = "",
                            output_full: str = "",
                            error_message: str = ""):
    """规则引擎/感知型神经元的直接埋点函数。

    Usage:
        record_neuron_execution("trigger_detector", status="ok", duration_ms=5.2,
                                output_preview="触发: hr_surge, steps_stagnant")
    """
    trace = NeuronTrace(
        trace_id=uuid.uuid4().hex[:12],
        neuron_id=neuron_id,
        timestamp=datetime.now().isoformat(),
        status=status,
        duration_ms=round(duration_ms, 1),
        input_preview=_truncate(input_preview) or _truncate(input_full),
        output_preview=_truncate(output_preview) or _truncate(output_full),
        thinking_preview="",
        input_full=input_full or input_preview,
        output_full=output_full or output_preview,
        thinking_full="",
        error_message=error_message,
        recipe_used="",
        model_used="",
    )

    _ensure_neuron(neuron_id)
    _trace_buffers[neuron_id].append(trace)
    _exec_counter[neuron_id] += 1

    s = _stats[neuron_id]
    s["execution_count"] = _exec_counter[neuron_id]
    s["last_status"] = trace.status
    s["last_duration_ms"] = trace.duration_ms
    s["last_timestamp"] = trace.timestamp
    s["last_input_preview"] = trace.input_preview
    s["last_output_preview"] = trace.output_preview
    s["last_thinking_preview"] = ""
    s["last_error_message"] = trace.error_message

    if status == "ok":
        s["success_count"] += 1
        s["last_ok_at"] = trace.timestamp


# ═══════════════════════════════════════════════════════════
# API 数据构建
# ═══════════════════════════════════════════════════════════

def _trace_to_dict(t: NeuronTrace) -> dict:
    return {
        "trace_id": t.trace_id,
        "neuron_id": t.neuron_id,
        "timestamp": t.timestamp,
        "status": t.status,
        "duration_ms": t.duration_ms,
        "input_preview": t.input_preview,
        "output_preview": t.output_preview,
        "thinking_preview": t.thinking_preview,
        "input_full": t.input_full,
        "output_full": t.output_full,
        "thinking_full": t.thinking_full,
        "error_message": t.error_message,
        "recipe_used": t.recipe_used,
        "model_used": t.model_used,
    }


def get_neuron_definition(neuron_id: str) -> Optional[NeuronDefinition]:
    """获取神经元静态声明。"""
    return _ALL_NEURONS.get(neuron_id)


def list_neuron_ids() -> List[str]:
    """返回所有神经元 ID 列表。"""
    return list(_ALL_NEURONS.keys())


def get_all_neuron_status() -> dict:
    """返回所有神经元的聚合状态（供 GET /api/system/neurons）。"""
    neurons = []
    for nid, ndef in _ALL_NEURONS.items():
        _ensure_neuron(nid)
        s = _stats[nid]
        last_trace = None
        buf = _trace_buffers.get(nid)
        if buf and len(buf) > 0:
            last_trace = _trace_to_dict(buf[-1])

        neurons.append({
            "neuron_id": ndef.neuron_id,
            "name": ndef.name,
            "category": ndef.category,
            "subsystem": ndef.subsystem,
            "consumer": ndef.consumer,
            "trigger_desc": ndef.trigger_desc,
            "recipe": ndef.recipe,
            "is_llm": ndef.is_llm,
            "llm_model": ndef.llm_model,
            "description": ndef.description,
            "emoji": ndef.emoji,
            "last_trace": last_trace,
            "execution_count": s["execution_count"],
            "success_count": s["success_count"],
            "last_ok_at": s.get("last_ok_at"),
        })

    # 统计
    total = len(neurons)
    cognitive = sum(1 for n in neurons if n["category"] == "cognitive")
    reflex = sum(1 for n in neurons if n["category"] == "reflex")
    perception = sum(1 for n in neurons if n["category"] == "perception")
    with_recipe = sum(1 for n in neurons if n["recipe"])
    never = sum(1 for n in neurons if n["execution_count"] == 0)

    stats = {
        "total": total,
        "cognitive": cognitive,
        "reflex": reflex,
        "perception": perception,
        "with_recipe": with_recipe,
        "hard_built": total - with_recipe,
        "migration_pct": round(with_recipe / total * 100, 1) if total > 0 else 0,
        "never_executed": never,
    }

    return {"neurons": neurons, "stats": stats}


def get_neuron_history(neuron_id: str, limit: int = 20) -> dict:
    """返回指定神经元的历史 trace（供 GET /api/system/neurons/{id}/history）。"""
    _ensure_neuron(neuron_id)
    buf = _trace_buffers.get(neuron_id, deque())
    traces = [_trace_to_dict(t) for t in buf]
    # 最新的在前
    traces.reverse()
    if limit > 0:
        traces = traces[:limit]
    return {"neuron_id": neuron_id, "traces": traces}


def get_neuron_summary() -> dict:
    """返回神经元摘要（供 /monitor 端点，不含完整 trace）。"""
    result = get_all_neuron_status()
    # 去掉 last_trace 中的 full 字段，减小 payload
    for n in result["neurons"]:
        if n["last_trace"]:
            for k in ("input_full", "output_full", "thinking_full"):
                n["last_trace"].pop(k, None)
    return result
