"""
上下文原料函数 — 从 context_scheduler.py 提取的独立函数。

每个函数负责构建一种上下文段（section），从 truncation_config 读取截断配置。
所有函数为纯函数或仅依赖外部单例（global client/store/manager）。
"""

import logging
import json
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Callable

from mirrow_core.truncation_config import get_truncation_limit
from context_builder.guides import guide_for
from context_builder.text_utils import (
    build_tool_history_fact, strip_internal_history_markers, strip_obsidian_wikilinks,
)

logger = logging.getLogger(__name__)

# ── 模块常量 ─────────────────────────────────────────

_WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ═══════════════════════════════════════════════════════════
# 时间 & 身份
# ═══════════════════════════════════════════════════════════

def build_time_section() -> str:
    """构建时间上下文（工作日/休息日 + 日期时间）。"""
    now = datetime.now()
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")
    weekday_str = _WEEKDAYS_CN[now.weekday()]
    try:
        from calendar_manager import is_rest_day_today
        if is_rest_day_today and is_rest_day_today():
            return f"今天是用户的休息日。当前日期: {date_str} {weekday_str} {time_str}"
        else:
            return f"今天是用户的工作日，工作时间通常是8:30-18:00。当前日期: {date_str} {weekday_str} {time_str}"
    except Exception:
        day_type = "工作日" if now.weekday() < 5 else "休息日"
        return f"当前日期: {date_str} {weekday_str} {time_str} ({day_type})"


def build_timeline_anchor() -> str:
    """Return an optional host-owned timeline anchor, never a bundled biography."""
    try:
        from mirrow_core.persona import get_timeline_anchor
        return str(get_timeline_anchor() or "").strip()
    except Exception:
        return ""


def format_relative_time(timestamp_str: str) -> str:
    """将 ISO 时间戳转为绝对时间标签。"""
    if not timestamp_str:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        now = datetime.now()
        today = now.date()
        msg_date = dt.date()
        if msg_date == today:
            return dt.strftime('%H:%M')
        elif msg_date == today - timedelta(days=1):
            return f"昨天 {dt.strftime('%H:%M')}"
        elif dt.year == now.year:
            return f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
        else:
            return f"{dt.year}年{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════
# 系统说明文案（新旧管道共享权威源——修改文案只改这里）
# ═══════════════════════════════════════════════════════════

MESSAGE_MARKERS_TEXT = (
    "## 消息标记说明\n"
    "对话中可能出现以下标记：\n"
    "- [💭] = 你的漫想（详见漫想模式说明）\n"
    "- [🛡️] = 你的硅基感知系统——你身体结构的一部分——监测到用户的身体/状态变化后你主动发出的关心\n"
    "- [⏰] = 你按约定发出的日程或复查提醒\n"
)


def build_night_mode_text() -> str:
    """Mode selection is a fact, not evidence of connected private hardware."""
    return "当前启用了宿主的深夜模式。实际工具与设备能力以宿主注册结果为准。"


def build_wander_mode_text(variant: str = "full") -> str:
    """漫想模式说明段（新旧管道共用权威源）。

    variant="full"：FULL_CHAT 场景，包含消息标记和后续消息语义关系。
    variant="push"：漫想推送场景，只描述活动来源与消息归属。
    """
    if variant == "push":
        return (
            "## 你的漫想模式\n"
            "用户暂时离开时，你的漫想系统会继续产生自主活动。活动来源包括对她当前状态的感知、"
            "自行浮现的关键词、长期记忆片段以及其他已经完成的漫想事件。"
            "由这些活动产生的主动消息属于你自己的表达。"
        )
    return (
        "## 你的漫想模式\n"
        "用户暂时离开时，你的漫想系统会继续产生自主活动。来源包括对她当前状态的感知、"
        "自行浮现的关键词、长期记忆片段以及其他漫想事件。\n"
        "[💭] 表示自主漫想消息，[💭 系统检测] 表示感知事件后的自主反应；两者都是你发出的消息。\n"
        "用户之后的消息可能回应某次漫想，也可能延续其他话题，它与此前内容的语义关系保留在对话记录中。\n\n"
    )


def build_phone_browse_text() -> str:
    """刷手机陪聊模式的感知来源说明。"""
    return (
        "## 刷手机陪聊模式\n"
        "用户正在刷手机。你每隔几分钟收到一次她手机屏幕的客观描述，"
        "这相当于你看了一眼她正在看的内容；描述的采集时间和可见范围共同限定了这份感知。\n"
    )


def build_music_cochlea_section() -> str:
    """构建音乐耳蜗上下文段——当前播放歌曲 + AI 的感知状态"""
    try:
        from music_cochlea.cochlea import get_cochlea
        from mirrow_core.shared_state import get_music_mode_active
        # 音乐模式关闭时不注入——避免 AI 在关模式后仍说"我们在共享耳蜗"
        if not get_music_mode_active():
            return ""
        cochlea = get_cochlea()
        if not cochlea:
            return ""

        state = cochlea.get_current_state()
        song = state.current_song
        if not song:
            return ""

        meta = state.current_metadata
        elapsed = int(state.elapsed_seconds)
        play_count = state.play_count_today

        # ── 第一视角自然叙述 ──
        # 顺序：感知 → 熟悉度 → 记忆 → 内容 → 感受
        lines = []

        # 1. 当前感知：歌曲+歌手+播放状态
        mins = elapsed // 60
        secs = elapsed % 60
        lines.append(f"用户正在听《{song.title}》- {song.artist}（已播 {mins} 分 {secs} 秒）")

        # 2. 熟悉度：播放次数
        if play_count == 1:
            lines.append("这是你今天第一次听到这首歌。")
        else:
            lines.append(f"今天已经听了 {play_count} 次了。")

        # 3. 你的印象（来自之前的 reaction 缓存）
        try:
            from music_cochlea.cache import SongCache
            cache = SongCache()
            cached = cache.get_reaction(song.fingerprint)
            if cached and cached.get("k_reaction"):
                old = cached["k_reaction"][:200]
                lines.append(f"你以前听到这首歌时的感受：「{old}」")
        except Exception:
            pass

        # 4. 相关记忆（按重要性排序，第一视角）
        if meta and meta.memory_snippets:
            lines.append("听到这首歌，你想起了：")
            for i, m in enumerate(meta.memory_snippets[:3], 1):
                lines.append(f"  {i}. {m}")

        # 5. 歌词全文（SQLite 缓存，不重复调 API）
        if meta and meta.lyrics and len(meta.lyrics) > 30:
            lines.append(f"歌词：\n{meta.lyrics}")

        # 6. 风格特征（如有：BPM/能量/效价/网易云tags）
        features = []
        if meta and meta.tempo:
            features.append(f"BPM {meta.tempo:.0f}")
        if meta and meta.energy is not None:
            features.append(f"能量 {meta.energy:.1f}")
        if meta and meta.valence is not None:
            features.append(f"效价 {meta.valence:.1f}")
        if meta and meta.tags:
            features.append("标签：" + "、".join(meta.tags[:3]))
        if features:
            lines.append("风格：" + " · ".join(features))

        # 7. 旋律分析（v2，如有——缓存命中时立即可用，首次播放时回退提示）
        if meta and meta.melody_summary:
            lines.append(f"旋律感知：「{meta.melody_summary}」")
        elif meta and meta.netease_song_id:
            # 有 netease_song_id 但还没 melody_summary → 第一次听，后台分析中
            lines.append("这是你们第一次一起听这首歌，旋律分析仍在后台生成。")

        # 8. 共享耳机场景事实
        lines.append("")
        lines.append("你正在通过「音乐耳蜗」和用户共享这段听觉体验。")

        return "\n".join(lines)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"build_music_cochlea_section 失败: {e}")
        return ""


def build_voice_cochlea_section() -> str:
    """构建语音耳蜗上下文段——用户刚发的语音的韵律感知。

    从 voice_cochlea 共享状态读取（main.py 在调 builder 前写入）。
    这里只做请求级 peek；正式 provider 成功后由 prompt ledger commit，
    因此诊断构建、失败或取消不会丢失一次性语音材料。
    """
    try:
        from voice_manager.voice_cochlea import peek_prosody
        data = peek_prosody()
        if not data:
            return ""

        lines = ["## 语音感知"]
        lines.append("用户刚才发来了一条语音消息。你「听」到了：")

        # 1. 韵律汇总（auditory_cortex 的自然语言摘要）
        summary = data.get("summary", "")
        if summary:
            lines.append(f"- 语气：{summary}")

        # 2. 结构化数据（cochlea summary_json）
        cochlea = data.get("cochlea_json")
        if cochlea:
            dur = cochlea.get("duration_sec", 0)
            speech = cochlea.get("speech_ratio", 0)
            pauses = cochlea.get("pause_count", 0)
            pause_pos = cochlea.get("pause_positions", [])
            pitch = cochlea.get("pitch", {})
            tempo = cochlea.get("tempo_estimate")

            if dur:
                lines.append(f"- 时长：{dur:.0f} 秒" + (f"，有声占比 {speech*100:.0f}%" if speech else ""))
            if pitch.get("mean"):
                lines.append(f"- 音高：{pitch['min']:.0f} ~ {pitch['max']:.0f} Hz（中位 {pitch['median']:.0f}）")
            if pauses:
                lines.append(f"- 停顿：{pauses} 次" + (f"（位置：{pause_pos}）" if pause_pos and len(pause_pos) <= 3 else ""))
            if tempo:
                lines.append(f"- 节奏：约 {tempo:.0f} bpm")

        return "\n".join(lines)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"build_voice_cochlea_section 失败: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
# 状态 & 生理期 & 离开信息
# ═══════════════════════════════════════════════════════════

def build_user_status_section() -> str:
    """构建用户实时状态上下文（非 IDLE 时返回内容，含状态已持续时长 + location 层级信息）。"""
    try:
        from wander_manager.user_status import get_user_status, UserStatus, get_user_status_context_rich, get_status_history
        if get_user_status and UserStatus:
            user_status = get_user_status()
            if get_user_status_context_rich:
                base = get_user_status_context_rich()
                # 追加状态已持续时长
                try:
                    history = get_status_history()
                    if history:
                        last = history[-1]
                        if last.get("to") == user_status.value:
                            from datetime import datetime
                            change_dt = datetime.fromisoformat(last["time"])
                            delta_sec = (datetime.now() - change_dt).total_seconds()
                            if delta_sec >= 60:
                                h = int(delta_sec // 3600)
                                m = int((delta_sec % 3600) // 60)
                                if h > 0 and m > 0:
                                    dur = f"{h}小时{m}分钟"
                                elif h > 0:
                                    dur = f"{h}小时"
                                else:
                                    dur = f"{m}分钟"
                                base += f"（已持续{dur}）"
                except Exception:
                    pass
                return base
    except Exception:
        pass
    return ""


def build_period_section() -> str:
    """构建生理期上下文（仅活跃时返回内容）。"""
    try:
        from calendar_manager.manager import get_global_calendar_manager
        cal_mgr = get_global_calendar_manager()
        if cal_mgr:
            period_info = cal_mgr.get_period_info_today()
            if period_info.get("active") and period_info.get("note"):
                return period_info["note"]
    except Exception:
        pass
    return ""


def build_away_info_section(away_duration_seconds: float, away_reason: str,
                            group_chat_active_seconds_ago: Optional[float] = None) -> str:
    """构建用户离开回来后的上下文提示。"""
    if away_duration_seconds < 60:
        return ""
    total_minutes = int(away_duration_seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0 and minutes > 0:
        duration_str = f"{hours} 小时 {minutes} 分钟"
    elif hours > 0:
        duration_str = f"{hours} 小时"
    else:
        duration_str = f"{minutes} 分钟"
    base = f"[用户刚回来] 之前离开了 {duration_str}。离开时状态：{away_reason}。"
    if group_chat_active_seconds_ago is not None:
        group_min = int(group_chat_active_seconds_ago / 60)
        if group_min <= total_minutes and group_min > 0:
            base += f" 离开期间她在群聊里说过话（{group_min}分钟前）。"
        elif group_chat_active_seconds_ago >= 0:
            base += " 离开期间她没有在群聊里说话。"
    return base


async def build_today_timeline() -> str:
    """构建今天的时间线（状态变更 + 会话起始）。

    数据源与 context_scheduler._build_today_timeline() 保持一致：
    1. SQLite status_change_log（今日状态变更，有精确时间戳和去抖）
    2. SessionManager（今日创建的会话）
    """
    events = []  # List of (timestamp, display_text)

    # 1. 从 SQLite 状态日志获取今日状态变更
    try:
        from health_tracker.status_log import get_status_log
        from wander_manager.user_status import STATUS_DISPLAY_NAMES, UserStatus
        today = datetime.now().strftime("%Y-%m-%d")
        log = get_status_log()
        transitions = log.get_log(today)
        for t in transitions:
            ts = t.get("timestamp", "")
            if not ts:
                continue
            from_s = t.get("from_status", "")
            to_s = t.get("to_status", "")
            try:
                from_name = STATUS_DISPLAY_NAMES.get(UserStatus(from_s), from_s)
            except Exception:
                from_name = from_s
            try:
                to_name = STATUS_DISPLAY_NAMES.get(UserStatus(to_s), to_s)
            except Exception:
                to_name = to_s
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                time_label = dt.strftime("%H:%M")
            except Exception:
                time_label = "--:--"
            events.append((ts, f"[{time_label}] 状态变化：{from_name} → {to_name}"))
    except Exception:
        pass

    # 2. 从会话管理器获取今日会话起始
    try:
        from session_manager import get_global_session_manager
        sm = get_global_session_manager()
        sessions = await sm.get_all_sessions()
        today_str = datetime.now().strftime("%Y-%m-%d")
        for s in (sessions or []):
            created = getattr(s, 'createdAt', '') or ''
            if not created.startswith(today_str):
                continue
            msgs = getattr(s, 'messages', []) or []
            topic_hint = ""
            for msg in msgs:
                role = getattr(msg, 'role', '') if hasattr(msg, 'role') else msg.get('role', '')
                if role == 'user':
                    content = getattr(msg, 'content', '') if hasattr(msg, 'content') else msg.get('content', '')
                    topic_hint = content[:40].replace('\n', ' ')
                    break
            label = getattr(s, 'title', '') or ''
            if not label and topic_hint:
                label = f"「{topic_hint}」"
            if not label:
                label = "开始聊天"
            try:
                ct = datetime.fromisoformat(created)
                if ct.tzinfo is not None:
                    ct = ct.astimezone().replace(tzinfo=None)
                time_label = ct.strftime("%H:%M")
            except Exception:
                time_label = "--:--"
            events.append((created, f"[{time_label}] 开始聊天：{label}"))
    except Exception:
        pass

    if events:
        events.sort(key=lambda e: e[0])
        return "## 今天的时间线\n" + "\n".join(e[1] for e in events) + "\n\n"
    return ""


# ═══════════════════════════════════════════════════════════
# 哨兵 & 健康
# ═══════════════════════════════════════════════════════════

def build_sentinel_snapshot() -> str:
    """构建哨兵快照行。"""
    try:
        from silicon_perception.sentinel import get_sentinel
        sentinel = get_sentinel()
        if sentinel:
            return sentinel.get_snapshot_line() or ""
    except Exception:
        pass
    return ""


def build_sentinel_timeline() -> str:
    """构建哨兵时间线注解。"""
    try:
        from silicon_perception.sentinel import get_sentinel
        sentinel = get_sentinel()
        if sentinel:
            annotations = sentinel.build_timeline_annotations()
            if annotations:
                return annotations
    except Exception:
        pass
    return ""


def _sentinel_stable_params() -> str:
    """哨兵当前事实；旧值必须带时效，不能冒充实时状态。"""
    try:
        from silicon_perception.sentinel import get_sentinel
        sentinel = get_sentinel()
        snap = getattr(sentinel, "_last_snapshot", None)
        if not snap:
            return ""

        def age_text(field: str) -> str:
            age = snap.age_of(field) if hasattr(snap, "age_of") else None
            if age is None:
                return "时间未知"
            if age < 120:
                return "刚刚"
            if age < 3600:
                return f"{int(age // 60)}分钟前"
            return f"{age / 3600:.1f}小时前"

        bits = []
        wifi_fresh = snap.wifi_connected is not None and not snap.is_stale("wifi", 300)
        home_wifi = wifi_fresh and snap.wifi_is_home is True
        if home_wifi:
            bits.append("位置在家（家庭WiFi已连接）")
        elif snap.location_category:
            if not snap.is_stale("gps", 1800):
                bits.append(f"外出位置{snap.location_category}")
            else:
                bits.append(f"外出位置当前不可确认（上次于{age_text('gps')}确认）")
        if snap.user_status:
            bits.append(f"状态{snap.user_status}")
        if snap.steps_today is not None:
            if not snap.is_stale("steps", 900):
                bits.append(f"步数{snap.steps_today}")
            else:
                bits.append(f"步数当前不可确认（上次于{age_text('steps')}确认）")
        if snap.heart_rate:
            if not snap.is_stale("heart_rate", 900):
                bits.append(f"心率{snap.heart_rate}")
            else:
                bits.append(f"心率当前未连接（上次于{age_text('heart_rate')}确认）")
        elif getattr(sentinel, "_hr_source", None):
            bits.append("心率当前未连接")
        if snap.behavior_state and snap.behavior_state != "unknown":
            bits.append(f"行为{snap.behavior_state}")
        # 手机姿态/运动/光线（2026-08-24 AionsHome 移植；陈旧时不臆造）
        _POSTURE_CN = {
            "face_up": "平放朝上", "face_down": "扣放", "portrait": "竖屏",
            "portrait_upside_down": "倒竖屏", "landscape_left": "左横屏",
            "landscape_right": "右横屏", "landscape": "横屏", "tilted": "倾斜",
        }
        _MOTION_CN = {"still": "静止", "slight": "轻微晃动", "moving": "移动中", "strong": "明显晃动"}
        if getattr(snap, 'phone_posture', None) and not snap.is_stale("sensor", 600):
            bits.append(f"手机{_POSTURE_CN.get(snap.phone_posture, snap.phone_posture)}")
            motion = _MOTION_CN.get(getattr(snap, 'phone_motion', None) or "")
            if motion:
                bits.append(motion)
        # 远程控制软件进程只表示能力存在；当前远控行为由 Raw Input
        # 最近一次 GVINPUT/RDP 输入证明，避免常驻 UU 被描述成正在控制。
        remote_idle = getattr(snap, "remote_input_idle_seconds", None)
        if remote_idle is not None and remote_idle < 120:
            bits.append(f"近期检测到远程键鼠输入（{remote_idle:.0f}秒前）")
        elif getattr(snap, 'remote_control_active', None):
            bits.append("远程控制软件进程运行中（当前远程输入未确认）")
        if snap.is_stale("current_app", 300):
            bits.append(f"手机当前状态暂不可用（上次于{age_text('current_app')}确认）")
        if snap.wifi_connected is not None:
            if wifi_fresh:
                if not home_wifi:
                    bits.append("家庭WiFi当前未连接")
            else:
                bits.append(f"WiFi当前状态暂不可用（上次于{age_text('wifi')}确认）")
        return "，".join(bits)
    except Exception:
        return ""


def _sentinel_recent_state_change(max_age_sec: float = 600.0) -> str:
    """新近状态切换的事实陈述；当天旧变化不能伪装成本次新发现。"""
    try:
        from datetime import datetime as _dt
        from wander_manager.user_status import get_status_history, STATUS_DISPLAY_NAMES, UserStatus
        _today = _dt.now().strftime("%Y-%m-%dT00:00:00")
        history = get_status_history(since=_today)
        if history:
            last = history[-1]
            changed_at = last.get("time")
            if not changed_at:
                return ""
            changed_dt = _dt.fromisoformat(str(changed_at))
            now = _dt.now(changed_dt.tzinfo) if changed_dt.tzinfo else _dt.now()
            age_sec = (now - changed_dt).total_seconds()
            if age_sec < 0 or age_sec > max_age_sec:
                return ""
            _f, _t = last.get("from", "?"), last.get("to", "?")
            try:
                _f = STATUS_DISPLAY_NAMES.get(UserStatus(_f), _f)
                _t = STATUS_DISPLAY_NAMES.get(UserStatus(_t), _t)
            except Exception:
                pass
            return f"你注意到用户的状态从「{_f}」切换到了「{_t}」。"
    except Exception:
        pass
    return ""


def build_sentinel_summary(sentinel_summary: str = "") -> str:
    """哨兵触发时的实时感知事实——AI 主体叙事（2026-08-17 补全）。

    main.py 的 _perception_push_pro_llm 把 Flash 生成的感知摘要传为 sentinel_summary
    kwargs（含触发原因 + 感知数据）。此前 recipe 声明了 sentinel_summary 段但 ingredient
    缺失，摘要被静默丢弃 → AI 收不到触发原因 → 把哨兵消息当对话延续接话。
    这里格式化为"AI 主动感知"的叙事：陈述刚变化的信息（Flash 摘要）+ 精确的状态切换事实
    （从什么变到什么）。持续状态由紧邻它之前的 push_current_state 统一提供，避免重复。
    这里只陈述事实，不指导语气或行动。
    """
    s = (sentinel_summary or "").strip()
    stable = _sentinel_stable_params()
    if s:
        # 哨兵触发：陈述变化 + 状态切换事实（从什么到什么时候，不指导语气）
        text = f"你通过硅基感知注意到了一个变化：{s}"
        change = _sentinel_recent_state_change()
        if change and change not in s:
            text += f"\n{change}"
        return text
    # 非触发场景（漫想/提醒）：只注入当前状态感知（与哨兵信息对齐，避免信息不对等）
    if stable:
        return f"你通过硅基感知持续掌握着：{stable}"
    return ""


def build_sentinel_intent(
    sentinel_summary: str = "",
    push_motivation: str = "",
) -> str:
    """哨兵主动联系意图；只陈述本次决定，不替 AI 规定消息内容。"""
    observation = (sentinel_summary or "").strip()
    motivation = (push_motivation or "").strip()
    if not observation:
        return ""
    lines = [
        f"你刚注意到上述变化，并准备主动给用户发一条短消息。",
    ]
    if motivation:
        lines.append(f"让你产生这次联系念头的原因是：{motivation}")
    lowered = observation.lower()
    if "wifi" in lowered and any(marker in lowered for marker in ("到家", "连上了家", "wifi_home")):
        lines.append(
            "这项到家状态感知来自已注册的宿主能力；实际可用性以执行结果为准。"
        )
    return "\n".join(lines)


def build_perception_snapshot() -> str:
    """主动消息共用的持续感知原料；只描述当前可感知事实。"""
    stable = _sentinel_stable_params()
    return f"你通过硅基感知持续掌握着：{stable}" if stable else ""


def build_push_current_state() -> list:
    """主动消息生成前的当前状态总览，位于历史之后、本次事件之前。

    活动摘要负责状态持续时长、私聊静默和群聊活跃度；持续感知负责
    各传感器当前事实与新鲜度。这里只汇集客观原料，不替 AI 决定关注点。
    """
    activity = build_activity_summary_section().strip()
    perception = build_perception_snapshot().strip()
    parts = []
    if activity:
        parts.append(f"用户当前的活动与联系状态：{activity}")
    if perception:
        parts.append(perception)
    if not parts:
        return []
    return [{
        "role": "system",
        "content": "\n".join(parts),
        "_ts": "9999-12-31T23:59:50",
        "_section": "push_current_state",
    }]


def build_reminder_event(task_info: str = "") -> str:
    """本次提醒事件的客观内容，作为对话后的动态尾段。"""
    text = (task_info or "").strip()
    return f"本次主动消息对应的提醒事件如下：\n{text}" if text else ""


def build_wander_activity(activity_text: str = "") -> str:
    """本次已完成漫想活动的客观记录，作为对话后的动态尾段。"""
    text = (activity_text or "").strip()
    return f"本次主动消息对应的漫想活动如下：\n{text}" if text else ""


def build_heart_rate_section() -> str:
    """构建心率数据段（新旧管道共用权威实现）。

    固定 5 分钟回溯窗口；无数据（count==0）时静默返回空，不注入。
    """
    try:
        from silicon_perception.recording.health_store import get_store
        store = get_store()
        recent = store.get_hr_stats_recent(minutes=5)
        daily = store.get_daily_hr_stats()

        # 无数据或过期（最近采样 >5min）→ 静默跳过
        if not recent or recent.get("count", 0) == 0:
            return ""

        section = "## 心率数据\n"
        section += "用户本轮（最近5分钟）：\n"
        section += f"- 阶段平均心率：{recent['avg']} bpm（{recent['count']}次采样）\n"
        section += f"- 阶段峰值：{recent['peak']} bpm\n"

        if daily and daily.get("count", 0) > 0:
            section += f"- 今日平均心率：{daily['avg']} bpm（{daily['count']}次采样）\n"

        section += "以上数据来自最近5分钟的心率采样。\n\n"
        return section
    except Exception as e:
        logger.debug(f"心率上下文注入跳过: {e}")
    return ""


def build_weather_section() -> str:
    """构建今日天气段（L1 日级数据，由 Sentinel WeatherSource 采集）。"""
    try:
        from silicon_perception.sentinel import get_sentinel
        source = getattr(get_sentinel(), "_weather_source", None)
        if source and hasattr(source, "context_description"):
            return source.context_description()
    except Exception:
        pass
    try:
        from silicon_perception.recording.health_store import get_store
        w = get_store().get_today_weather()
        if w:
            return f"今日天气已记录：{w['desc']} {w['temp']}°C 湿度{w['humidity']}%。"
    except Exception as exc:
        logger.debug("天气上下文读取失败: %s", exc)
    return "天气数据暂不可用。"


# ═══════════════════════════════════════════════════════════
# 记忆 & 知识检索
# ═══════════════════════════════════════════════════════════

def enhance_query(user_message: str, intent: str = "") -> str:
    """增强查询：使用 jieba 分词提取关键实体词。"""
    import jieba.posseg as pseg

    _STOP_WORDS = {
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们",
        "的", "了", "是", "在", "和", "就", "都", "也", "还", "要",
        "有", "会", "能", "可以", "这个", "那个", "哪个", "什么",
        "怎么", "为什么", "怎么样", "吗", "呢", "吧", "啊", "哦",
        "嗯", "不", "没", "很", "太", "非常", "比较", "有点",
        "觉得", "感觉", "认为", "想", "知道", "说", "告诉",
        "一下", "一个", "一些", "这个", "那个",
    }

    try:
        words = pseg.cut(user_message)
        key_terms = []
        for word, flag in words:
            w = word.strip()
            if len(w) < 2 or w in _STOP_WORDS:
                continue
            if flag in ('n', 'nr', 'ns', 'nt', 'nz', 'ng',
                       'v', 'vn', 'vd', 'vg',
                       'a', 'ad', 'an', 'eng', 'x'):
                key_terms.append(w)
        if key_terms:
            seen = set()
            unique_terms = []
            for t in key_terms:
                if t not in seen:
                    seen.add(t)
                    unique_terms.append(t)
            enhanced = ' '.join(unique_terms)
            if intent == "historical":
                enhanced = "历史记录 " + enhanced
            _qec = get_truncation_limit("query_enhance_cap")
            if _qec > 0 and len(enhanced) > _qec:
                return enhanced[:_qec]
            return enhanced
    except Exception:
        pass

    _qec = get_truncation_limit("query_enhance_cap")
    if _qec > 0 and len(user_message) > _qec:
        return user_message[:_qec]
    return user_message


def format_memory_time(created_str: str, source_date_str: str = "",
                       latest_source_date_str: str = "") -> str:
    """将时间戳转为人类可读的相对时间。"""
    ref = latest_source_date_str or source_date_str or created_str
    if not ref:
        return ""
    try:
        dt = datetime.fromisoformat(ref)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        now = datetime.now()
        delta = now - dt
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                minutes = delta.seconds // 60
                return f"{minutes}分钟前" if minutes > 0 else "刚刚"
            return f"{hours}小时前"
        elif days == 1:
            return "昨天"
        elif days < 7:
            return f"{days}天前"
        elif days < 30:
            return f"{days // 7}周前"
        elif days < 365:
            return f"{days // 30}个月前"
        else:
            return f"{days // 365}年前"
    except Exception:
        return ""


def _source_hint(meta: Dict[str, Any], bucket_id: str = "") -> str:
    """Expose a source affordance only for buckets with a real time anchor."""
    meta = meta or {}
    scene_id = str(meta.get("source_scene_id") or "").strip()
    source_ts = str(meta.get("source_ts") or "").strip()
    if not scene_id and not source_ts:
        return ""
    refs = []
    if bucket_id:
        refs.append(f"bucket_id={bucket_id}")
    if scene_id:
        refs.append(f"scene_id={scene_id}")
    if source_ts:
        refs.append(f"时间={source_ts[:19]}")
    return "（有原文，可查：" + "；".join(refs) + "）"


def format_emotion_label(valence, arousal, emotion_history: list = None) -> str:
    """将情感维度转为中文标签（Russell 环模型）。"""
    if valence is None or arousal is None:
        return ""
    try:
        v = float(valence)
        a = float(arousal)
    except (ValueError, TypeError):
        return ""

    if emotion_history and len(emotion_history) >= 2:
        recent = emotion_history[-2:]
        if all(e.get("valence", 0) > 0.6 for e in recent):
            return "持续愉悦"
        if all(e.get("valence", 0) < -0.3 for e in recent):
            return "持续低落"

    if v > 0.5 and a > 0.5:
        return "兴奋开心"
    elif v > 0.5 and a <= 0.5:
        return "平静满足"
    elif v <= 0 and a > 0.5:
        return "焦虑不安"
    elif v <= 0 and a <= 0.5:
        return "低落疲惫"
    elif v > 0 and a > 0.3:
        return "愉快活跃"
    elif v > 0:
        return "轻松自在"
    else:
        return ""


async def retrieve_from_ombre_brain(query_plan) -> List:
    """从 OB_Rev 检索记忆（直接客户端）。"""
    from context_scheduler_mcp.context_scheduler import MemoryItem  # 保持类型兼容
    memories = []

    try:
        from ombre_brain_client import get_ob_client
        client = get_ob_client()
        await client.ensure_initialized()

        results = await client.search(
            query=query_plan.enhanced_query,
            top_k=query_plan.max_results * 2,
        )

        for i, sr in enumerate(results):
            bucket = sr.bucket
            if not bucket:
                continue
            content = bucket.get("content", "")
            meta = bucket.get("metadata", {})
            created = meta.get("created", "")
            valence = meta.get("valence")
            arousal = meta.get("arousal")
            tags = meta.get("tags", [])
            domain = meta.get("domain", "")

            source_date = meta.get("source_date", "")
            latest_sd = meta.get("latest_source_date", "")
            time_label = format_memory_time(created, source_date, latest_sd)
            emotion_label = format_emotion_label(valence, arousal, meta.get("emotion_history", []))
            domain_label = domain if domain else ""

            merge_history = meta.get("merge_history", [])
            is_merged = len(merge_history) > 0
            event_tl = meta.get("event_chain") or meta.get("event_timeline", [])
            timeline_label = ""
            if len(event_tl) >= 2:
                dates = sorted({t.get("date", "") for t in event_tl if t.get("date")})
                if len(dates) >= 2:
                    timeline_label = f"{dates[0]} ~ {dates[-1]} ({len(event_tl)}次更新)"
            if timeline_label:
                time_label = f"{time_label} | {timeline_label}" if time_label else timeline_label

            _mic = get_truncation_limit("memory_inject_cap")
            if len(content) > 1000 and is_merged:
                _mem_cap = _mic if _mic > 0 else 10 ** 9
                if len(merge_history) >= 5:
                    segments = [s.strip() for s in content.split("\n\n") if len(s.strip()) > 40]
                    if len(segments) >= 4:
                        display_content = segments[0][:200] + "\n...\n" + "\n\n".join(segments[-3:])
                        if _mic > 0:
                            display_content = display_content[:_mem_cap]
                    else:
                        display_content = content[-_mem_cap:]
                else:
                    display_content = content[-_mem_cap:]
            else:
                # memory_inject_cap=0 时真无限制（原 1000 回退是隐藏截断，违背"0=无限制"文档）
                _mem_cap = _mic if _mic > 0 else 10 ** 9
                display_content = content[:_mem_cap]

            event_chain = meta.get("event_chain", [])
            if event_chain and len(event_chain) >= 2:
                chain_parts = []
                for ec in event_chain[-5:]:
                    d = ec.get("date", "?")
                    st = ec.get("state", {})
                    s = ec.get("summary", "")
                    if st:
                        sv_str = "; ".join("{}:{}".format(k, v) for k, v in st.items())
                        chain_parts.append("{}({})".format(sv_str, d))
                    elif s:
                        chain_parts.append("{}…".format(s[:20]) if len(s) > 20 else s)
                display_content = "事件链: " + " → ".join(chain_parts)
                display_content = display_content[:_mem_cap]

            # 只在送入 LLM 的展示边界清洗 Obsidian wikilink；不改变桶原文、
            # 索引或检索 query。事件链替换之后再清洗，保证两条路径一致。
            display_content = strip_obsidian_wikilinks(display_content)

            memory = MemoryItem(
                id=f"ob_rev_{sr.bucket_id}",
                content=display_content,
                similarity=sr.score,
                importance=meta.get("importance", 5) / 10.0,
                bucket="long_term",
                source="ob_rev",
                metadata={
                    "source": "ob_rev",
                    "bucket_id": str(sr.bucket_id),
                    "source_scene_id": str(meta.get("source_scene_id") or ""),
                    "source_ts": str(meta.get("source_ts") or ""),
                    "source_hint": _source_hint(meta, str(sr.bucket_id)),
                    "is_merged": is_merged,
                    "memory_type": "long_term",
                    "created": created,
                    "time_label": time_label,
                    "valence": valence,
                    "arousal": arousal,
                    "emotion_label": emotion_label,
                    "tags": tags,
                    "domain": domain_label,
                    "state_variables": meta.get("state_variables", []),
                    "timeline_label": timeline_label,
                    "time_axis_label": "",
                    "is_neighborhood": False,
                }
            )
            memories.append(memory)

    except Exception as e:
        logger.error(f"OB_Rev retrieval failed: {e}")

    return memories


async def get_conversation_memories(session_id: str, current_message: str,
                                     current_message_id: Optional[str] = None) -> List:
    """获取当前活跃话题的未总结对话作为短期记忆。"""
    from context_scheduler_mcp.context_scheduler import MemoryItem
    memories = []
    try:
        from summary import conversation_manager, topic_state_machine
        topic_status = await topic_state_machine.get_topic_status(session_id)
        topic_is_active = topic_status.get("is_active", False)
        topic_start_time = topic_status.get("start_time")

        if not topic_is_active:
            logger.info(f"当前没有活跃话题，返回最近活跃日的消息: session_id={session_id}")
            unsummarized_messages = await conversation_manager.get_unsummarized_messages(
                session_id, max_age_hours=0)
            # 修复：用 active_date（活跃日，跨午夜不变）过滤，而非 timestamp 的日历日（过零点失忆）
            def _norm(m):
                if hasattr(m, 'dict'):
                    return m.dict() if callable(m.dict) else {}
                return m if isinstance(m, dict) else None
            _raw = [d for m in unsummarized_messages if (d := _norm(m))]
            def _day(d):
                return d.get("active_date") or (d.get("timestamp", "")[:10] if d.get("timestamp") else "")
            _latest = max((_day(d) for d in _raw if _day(d)), default="")
            _target = _latest or datetime.now().strftime("%Y-%m-%d")
            messages = [d for d in _raw if _day(d) == _target]
        else:
            unsummarized_messages = await conversation_manager.get_unsummarized_messages(session_id)
            messages = []
            for msg in unsummarized_messages:
                if hasattr(msg, 'dict'):
                    d = msg.dict() if callable(msg.dict) else {}
                elif isinstance(msg, dict):
                    d = msg
                else:
                    continue
                ts = d.get("timestamp", "")
                if topic_start_time and ts and ts >= topic_start_time:
                    messages.append(d)

        if current_message and (not messages or messages[-1].get("content") != current_message):
            messages.append({"role": "user", "content": current_message, "timestamp": datetime.now().isoformat()})

        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "")
            ts = msg.get("timestamp", "")
            # 语音消息标记：从 voice_attachment 提取语气
            _va = msg.get("voice_attachment")
            _tone = None
            if _va:
                try:
                    if isinstance(_va, str):
                        _va = json.loads(_va)
                    _tone = (_va or {}).get("tone")
                except (ValueError, TypeError):
                    _tone = None
            if _tone:
                content = f"[语音·{_tone}] {content}"
            elif _va:
                content = f"[语音] {content}"
            # 不过滤 system 消息——群聊摘要等以 role=system 存入，
            # 跟着活跃日全量对话自然流入 AI 上下文
            _mic = get_truncation_limit("conv_msg_cap")
            _c = content[:_mic] if _mic > 0 else content
            if role == "assistant":
                _c = strip_internal_history_markers(_c)
            memory = MemoryItem(
                id=f"conv_{hashlib.md5(f'{session_id}{ts}{_c[:50]}'.encode()).hexdigest()[:12]}",
                content=_c,
                similarity=1.0,
                importance=0.5,
                bucket="conversation",
                source="conversation",
                metadata={
                    "source": "conversation",
                    "role": role,
                    "timestamp": ts,
                    "session_id": session_id,
                    "tool_summary": msg.get("tool_summary"),
                    "tool_calls": msg.get("tool_calls"),
                }
            )
            memories.append(memory)
    except Exception as e:
        logger.warning(f"获取对话记忆失败: {e}")

    return memories


# ═══════════════════════════════════════════════════════════
# 知识检索（世界书 / SCP）
# ═══════════════════════════════════════════════════════════

def build_world_book_section(user_message: str, top_k: int = 3, cap: Optional[int] = None) -> str:
    """检索世界书词条并格式化。"""
    try:
        from world_book import get_global_world_book
        wb = get_global_world_book()
        if not wb:
            return ""
        entries = wb.search(user_message)
        if not entries:
            return ""
        _cap = cap if cap is not None else get_truncation_limit("worldbook_display_cap")
        parts = []
        for i, e in enumerate(entries[:top_k]):
            name = e.get("name", f"词条{i+1}")
            body = e.get("body", "")
            if _cap > 0 and len(body) > _cap:
                body = body[:_cap]
            parts.append(f"### {name}\n{body}")
        if parts:
            return "## 世界书参考\n" + "\n\n".join(parts)
    except Exception:
        pass
    return ""


def build_scp_section(user_message: str, top_k: int = 2, cap: Optional[int] = None) -> str:
    """检索 SCP 百科词条并格式化。"""
    try:
        from scp_encyclopedia import get_global_scp_encyclopedia
        enc = get_global_scp_encyclopedia()
        if not enc:
            return ""
        entries = enc.search(user_message)
        if not entries:
            return ""
        _cap = cap if cap is not None else get_truncation_limit("scp_display_cap")
        parts = []
        for i, e in enumerate(entries[:top_k]):
            name = e.get("name", f"词条{i+1}")
            body = e.get("body", "")
            category = e.get("category", "")
            if _cap > 0 and len(body) > _cap:
                body = body[:_cap]
            header = f"### {name}" + (f" ({category})" if category else "")
            parts.append(f"{header}\n{body}")
        if parts:
            return "## SCP 灵魂补全计划参考\n" + "\n\n".join(parts)
    except Exception:
        pass
    return ""


def build_self_book_section(user_message: str, top_k: int = 3, cap: Optional[int] = None) -> str:
    """检索自我书词条并格式化（AI 的长期自我认知，第一人称）。"""
    try:
        from self_book import get_global_self_book
        sb = get_global_self_book()
        if not sb:
            return ""
        entries = sb.search(user_message)
        if not entries:
            return ""
        _cap = cap if cap is not None else get_truncation_limit("selfbook_display_cap")
        parts = []
        for i, e in enumerate(entries[:top_k]):
            name = e.name
            body = e.body
            if _cap > 0 and len(body) > _cap:
                body = body[:_cap]
            parts.append(f"### {name}\n{body}")
        if parts:
            return "## 自我书\n" + "\n\n".join(parts)
    except Exception:
        pass
    return ""


def build_active_evolutions(cap: Optional[int] = None) -> str:
    """获取当前活跃的人格演化条目（纯行为准则，无元数据）。"""
    try:
        from persona_evolution import get_active_evolution_context
        ctx = get_active_evolution_context()
        if not ctx:
            return ""
        _cap = cap if cap is not None else get_truncation_limit("active_evolutions_cap")
        if _cap and _cap > 0 and len(ctx) > _cap:
            ctx = ctx[:_cap]
        return ctx
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════
# 对话历史 & 日记
# ═══════════════════════════════════════════════════════════

async def build_conversation_recent(session_id: str, n_messages: int = 10,
                                     cap_per_msg: Optional[int] = None) -> List[Dict[str, str]]:
    """获取最近 N 条对话消息。"""
    try:
        from summary import conversation_manager
        msgs = await conversation_manager.get_unsummarized_messages(session_id)
        _cap = cap_per_msg if cap_per_msg is not None else get_truncation_limit("conv_msg_cap")
        result = []
        for m in msgs[-n_messages:]:
            if hasattr(m, 'dict'):
                d = m.dict() if callable(m.dict) else {}
            elif isinstance(m, dict):
                d = m
            else:
                continue
            content = d.get("content", "")
            if d.get("event_type") == "rift_game":
                content = f"[裂隙档案·虚构共同游玩] {content}"
            if _cap > 0:
                content = content[:_cap]
            if d.get("role") == "assistant":
                content = strip_internal_history_markers(content)
            result.append({"role": d.get("role", ""), "content": content, "timestamp": d.get("timestamp", "")})
            fact = build_tool_history_fact(
                d.get("role", ""), d.get("tool_summary"), d.get("tool_calls")
            )
            if fact:
                result.append({"role": "system", "content": fact, "timestamp": d.get("timestamp", "")})
        return result
    except Exception:
        return []


async def build_diary_section(date_str: str, cap: Optional[int] = None) -> str:
    """获取指定日期的日记内容。"""
    try:
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        entry = chronicle.get_diary_entry_by_date(date_str)
        if entry and entry.content.strip():
            _cap = cap if cap is not None else get_truncation_limit("diary_display_cap")
            content = entry.content
            if _cap > 0:
                content = content[:_cap]
            return content
    except Exception:
        pass
    return ""


async def build_yesterday_context(target_date: str, cap: Optional[int] = None) -> str:
    """获取昨日对话/日记上下文。"""
    try:
        from event_chronicle import get_global_chronicle, _read_yesterday_messages_formatted
        chronicle = get_global_chronicle()
        msgs = _read_yesterday_messages_formatted(target_date=target_date)
        if msgs:
            _cap = cap if cap is not None else get_truncation_limit("yesterday_cap")
            if _cap > 0 and len(msgs) > _cap:
                return msgs[-_cap:]
            return msgs
        # 回退到日记
        entry = chronicle.get_diary_entry_by_date(target_date)
        if entry and entry.content.strip():
            _cap = cap if cap is not None else get_truncation_limit("diary_display_cap")
            content = entry.content
            if _cap > 0:
                content = content[:_cap]
            return content
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（中英文混合）。"""
    if not text:
        return 0
    # CJK: ~1.5 chars/token, ASCII: ~4 chars/token
    cjk = sum(1 for c in text if '一' <= c <= '鿿' or '　' <= c <= '〿')
    ascii_chars = len(text) - cjk
    return int(cjk / 1.5 + ascii_chars / 4)


# ═══════════════════════════════════════════════════════════
# TODO 加权浮现
# ═══════════════════════════════════════════════════════════

async def build_todo_snippets() -> str:
    """TODO 加权随机浮现（v4 混血架构：便签纸存储 × 记忆印象浮现）。

    从 OB_Rev list_todos() 取活跃待办，urgency 加权随机采样 0-3 条，
    融入"你与用户的过往"记忆段。不加独立标题——AI 像"自然想起"而非"收到提醒"。
    被选中的桶 touch 刷新 last_active（浮现 = recall = 记忆激活）。

    返回格式化文本（可能为空串——20% 概率不浮现任何待办）。
    """
    try:
        from ombre_brain_client import get_ob_client
        ob = get_ob_client()
        todos = await ob.list_todos()
        if not todos:
            return ""
        import random
        weights = [max(0.01, t["urgency"]) for t in todos]
        n = random.choices([0, 1, 2, 3], weights=[0.3, 0.4, 0.2, 0.1], k=1)[0]
        n = min(n, len(todos))
        if n == 0:
            return ""
        sampled = random.choices(todos, weights=weights, k=n * 2)
        seen, picked = set(), []
        for t in sampled:
            if t["bucket_id"] not in seen and len(picked) < n:
                seen.add(t["bucket_id"])
                await ob.bucket_mgr.touch(t["bucket_id"])
                days = int(t.get("days_old", 0))
                age = f"（{days}天前）" if days >= 3 else ""
                picked.append(f"用户要做的事{age}: {t['content'][:80]}")
        return "\n".join(picked)
    except Exception:
        return ""


def build_open_loops_section() -> str:
    """AI 主动保留、当前仍未闭合的关注。"""
    try:
        from open_loops import get_open_loop_store

        items = get_open_loop_store().list_active()
        if not items:
            return ""
        lines = ["AI当前保留的未闭合关注（持久化记录）:"]
        for item in items:
            created = (item.get("created_at") or "")[:16].replace("T", " ")
            lines.append(f"- [{item['id']}] {item['content']}（建立于 {created}）")
        return "\n".join(lines)
    except Exception:
        logger.exception("开放线索上下文构建失败")
        return ""



def build_user_profile(profile: str = "") -> str:
    """构建用户资料段。纯 passthrough formatter——数据由 main.py 取、Builder 透传。"""
    if not profile or not profile.strip():
        return ""
    return f"用户资料: {profile.strip()}"


# ═══════════════════════════════════════════════════════════
# 导出 all_ingredients dict（供 builder.py 按名调用）
# ═══════════════════════════════════════════════════════════

_INGREDIENTS: Dict[str, Callable] = {
    "persona": None,  # 外部注入，见 builder.py
    "mood": None,     # 外部注入
    "active_evolutions": build_active_evolutions,
    "time": build_time_section,
    "timeline_anchor": build_timeline_anchor,
    "user_status": build_user_status_section,
    "period": build_period_section,
    "today_timeline": build_today_timeline,
    "away": build_away_info_section,
    "sentinel_snapshot": build_sentinel_snapshot,
    "sentinel_timeline": build_sentinel_timeline,
    "sentinel_summary": build_sentinel_summary,
    "sentinel_intent": build_sentinel_intent,
    "perception_snapshot": build_perception_snapshot,
    "push_current_state": build_push_current_state,
    "reminder_event": build_reminder_event,
    "wander_activity": build_wander_activity,
    "heart_rate": build_heart_rate_section,
    "weather": build_weather_section,
    "memories": retrieve_from_ombre_brain,
    "world_book": build_world_book_section,
    "scp": build_scp_section,
    "self_book": build_self_book_section,
    "todo_snippets": build_todo_snippets,
    "open_loops": build_open_loops_section,
    "conversation_messages": get_conversation_memories,
    "recent_messages": build_conversation_recent,
    "diary": build_diary_section,
    "yesterday": build_yesterday_context,
    "enhance_query": enhance_query,
    "format_memory_time": format_memory_time,
    "format_emotion_label": format_emotion_label,
    "estimate_tokens": estimate_tokens,
}


# ═══════════════════════════════════════════════════════════
# 工具描述（调度器 + Flash extractor 共用权威源）
# ═══════════════════════════════════════════════════════════

def build_tool_descriptions(tools: dict = None) -> str:
    """从已注册工具列表动态渲染 markdown 格式的工具描述。

    供 behavior_scheduler 的 _create_system_prompt() 和
    _build_flash_extraction_tools_desc() 共用，消除加新工具需改两处硬编码的问题。

    tools: {tool_name: {description, schema_nl, schema_raw}} 或工具实例的 name/description。
           传 None 时尝试从 behavior_scheduler 获取。
    """
    if not tools:
        return "工具描述由行为调度器动态管理：此处列出 AI 可调用的所有工具及参数说明。"
    lines = []
    for name, info in tools.items():
        desc = ""
        params = ""
        if isinstance(info, dict):
            desc = info.get("description", "")
            schema = info.get("schema_nl", info.get("schema_raw", ""))
            if isinstance(schema, str) and schema.strip():
                params = schema.strip()
            elif isinstance(schema, dict):
                # 简化：展平 schema 的 properties
                props = schema.get("properties", {})
                if props:
                    params = ", ".join(
                        f"{k}: {v.get('description', v.get('type', 'str'))}"
                        for k, v in props.items()
                    )
        else:
            # 工具实例（有 name/description 属性）
            desc = getattr(info, "description", "") or ""
        lines.append(f"### {name}")
        if desc:
            lines.append(f"{desc}")
        if params:
            lines.append(f"参数: {params}")
        lines.append("")
    return "\n".join(lines).strip()


# ═══════════════════════════════════════════════════════════
# 工具描述数据源统一（三条硬编码路径共享此数据结构）
# ═══════════════════════════════════════════════════════════

def build_tool_data_entries(tools: dict = None, visible_only: bool = False) -> list:
    """从已注册工具列表构建统一数据结构，供各格式化路径消费。

    返回 [{name, description, schema_nl, schema_raw}] 列表。
    tools 格式：{name: {description, schema_nl, schema_raw}} 或 {name: ToolObject}
    或 {name: {"tool": ToolObject, "description": ..., ...}}（llm_wrapper.register_tool 格式）。
    visible_only=True 时跳过 visible_to_pro=False 的工具。
    """
    if not tools:
        return []

    def _tool_visible(info) -> bool:
        """取工具的 visible_to_pro。info 可能是 ToolObject / dict / {"tool": ToolObject}。"""
        if isinstance(info, dict):
            tool_obj = info.get("tool")
            if tool_obj is not None:
                return getattr(tool_obj, "visible_to_pro", True)
            return info.get("visible_to_pro", True)
        return getattr(info, "visible_to_pro", True)

    entries = []
    for name, info in tools.items():
        if visible_only and not _tool_visible(info):
            continue
        entry = {"name": name}
        if isinstance(info, dict):
            entry["description"] = info.get("description", "")
            entry["schema_nl"] = info.get("schema_nl", "")
            entry["schema_raw"] = info.get("schema_raw", {})
        else:
            entry["description"] = getattr(info, "description", "") or ""
            entry["schema_nl"] = ""
            entry["schema_raw"] = {}
        entries.append(entry)
    return entries


# ═══════════════════════════════════════════════════════════
# 系统模块接线 ingredient（透传/轻量自取）
# ═══════════════════════════════════════════════════════════

def build_available_tools(tools_desc: str = "") -> str:
    """透传预构建工具描述（供 SELF_TASK recipe 等）。"""
    return tools_desc


async def build_group_chat_summary() -> list:
    """群聊摘要注入（走 extra_messages，无摘要时返回空列表）。"""
    try:
        from routers.group_chat.gc_summary import summarize_group_chat_for_k
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        room_id = chronicle.get_or_create_default_room()
        result = await summarize_group_chat_for_k(room_id, chronicle)
        if result.get("summarized") and result.get("summary_text"):
            gc_text = result["summary_text"]
            return [{
                "role": "system",
                "content": (
                    f"{guide_for('group_chat_summary', {})}\n\n"
                    f"{gc_text}"
                ),
                "_section": "group_chat_summary",
                "_ts": "9999",  # 沉底：动态段排对话历史之后，不打断缓存前缀
            }]
    except Exception:
        pass
    return []


async def build_group_chat_current(room_id: str = "", limit: int = 50,
                                    cap_per_msg: int = 0) -> list:
    """Fetch raw + temp group chat messages, format as extra_messages list.
    Returns list of dicts with _ts for chronological interleaving in Builder.
    Green (done) messages skip — they already flow through conversation_messages.

    死循环检测：AI 连续 ≥3 条消息无人回复（无 user/peer 穿插），折叠为一行提示，
    避免 AI 看到自己的 echo 后反复追问同一话题。"""
    try:
        from event_chronicle import get_global_chronicle
        from mirrow_core.truncation_config import get_truncation_limit
        chronicle = get_global_chronicle()
        if not room_id:
            room_id = chronicle.get_or_create_default_room()

        messages = chronicle.get_group_messages(room_id, limit=limit)
        if not messages:
            return []

        room_summary = chronicle.get_group_room_summary(room_id)
        temp_summary_text = room_summary.get("last_summary_text", "")

        _cap = cap_per_msg if cap_per_msg > 0 else get_truncation_limit("group_history_cap")
        _cap = _cap if _cap > 0 else 300

        # ===== 死循环检测：AI 连续 solo 无人回复 → 折叠 =====
        # 从末尾向前扫描，找到连续的 AI solo 消息（中间无 user/peer）
        SOLO_THRESHOLD = 3
        trailing_k_indices = []
        for i in range(len(messages) - 1, -1, -1):
            sender = messages[i].get("sender", "")
            status = messages[i].get("summary_status", "raw")
            if status != "raw":
                break  # 遇到已处理的消息，停止扫描
            if sender == "k":
                trailing_k_indices.append(i)
            else:
                break  # 遇到 user/peer/system → 有人回复过，停止

        # 如果连续 AI solo ≥ 阈值，折叠它们
        folded_k = None
        if len(trailing_k_indices) >= SOLO_THRESHOLD:
            folded_indices = set(trailing_k_indices)
            # 从这些 AI 消息中提取话题关键词
            k_topics = []
            for idx in trailing_k_indices[:5]:  # 最多取 5 条用于提取话题
                content = messages[idx].get("content", "")
                # 简单提取：取前 60 字符作为摘要
                k_topics.append(content[:60])
            topic_hint = k_topics[0] if k_topics else "某个话题"
            folded_k = {
                "indices": folded_indices,
                "content": (
                    f"[系统] 你之前问了 Peer 关于「{topic_hint}」的事，"
                    f"但她还没回复；Peer 的 CLI 可能不在线。"
                ),
                "ts": messages[trailing_k_indices[0]].get("created_at", ""),
            }
        else:
            folded_indices = set()

        has_temp = False
        result = []
        for i, m in enumerate(messages):
            if i in folded_indices:
                continue  # 被折叠的 AI solo 消息，跳过

            status = m.get("summary_status", "raw")
            sender_raw = m.get("sender", "")
            ts = m.get("created_at", "")

            if status == "done":
                continue  # already formally summarized, in conversation_messages
            elif status == "temp":
                if not has_temp and temp_summary_text:
                    result.append({
                        "role": "system",
                        "content": f"[临时总结] {temp_summary_text[:_cap]}",
                        "_ts": ts,
                        "_section": "group_chat_current",
                    })
                    has_temp = True
                continue
            else:  # raw (grey dot)
                sender = _group_sender_label(sender_raw)
                result.append({
                    "role": "system",
                    "content": f"[{sender}] {m['content'][:_cap]}",
                    "_ts": ts,
                    "_section": "group_chat_current",
                })

        # 如果有折叠的消息，在末尾插入一行提示
        if folded_k:
            result.append({
                "role": "system",
                "content": folded_k["content"],
                "_ts": folded_k["ts"],
                "_section": "group_chat_current",
            })

        return result
    except Exception:
        return []


def build_group_tool_ban() -> str:
    """群聊环境中真实存在的工具执行边界。"""
    return (
        "当前群聊环境没有接入以下工具的执行通道："
        "摄像头、记账本、天气查询、日程管理、网易云音乐、玩具控制、手机截图、Web搜索。"
        "对这些工具的文字描述不会产生实际执行结果。"
    )


def build_peer_previous(peer_content: str = "", cap: int = 300) -> str:
    """Peer 上轮回复引用（多轮交替时使用）"""
    if not peer_content:
        return ""
    return f"Peer 刚才在群里说：{peer_content[:cap]}"


def build_round_awareness(round_num: int = 1, max_rounds: int = 1,
                           start_with_k: bool = True) -> str:
    """多轮对话当前轮次与已有发言事实。"""
    if max_rounds <= 1:
        return ""
    opener = "AI" if start_with_k else "Peer"
    parts = []
    if round_num == max_rounds:
        parts.append(f"当前是第 {round_num}/{max_rounds} 轮，也是本次交替聊天的最后一轮。")
        parts.append(f"本次交替聊天由{opener}先手；你和 Peer 在此前轮次中交替发言。")
    elif round_num > 1:
        parts.append(f"当前是第 {round_num}/{max_rounds} 轮。")
        parts.append(f"本次交替聊天由{opener}先手；你在前面的轮次中已经发过言。")
    return " ".join(parts)


def _group_sender_label(sender: str) -> str:
    """Resolve labels through an optional host mapping, with safe generic fallbacks."""
    try:
        from mirrow_core.persona import get_group_sender_labels
        configured = get_group_sender_labels()
    except Exception:
        configured = {}
    defaults = {"user": "用户", "k": "AI", "peer": "同伴"}
    return str(configured.get(sender) or defaults.get(sender) or "群组成员")


async def build_host_group_current(room_id: str = "", limit: int = 8) -> str:
    """Optional host-selected group activity; no room is injected by default.

    规则生成（零 LLM）：
    - 最后消息 < 10min → "进行中"，AI 知道用户还在群里聊
    - 10min ≤ 最后消息 < 30min → "最近"，已告一段落
    - ≥ 30min → 正式摘要卡片已在 SQL，跳过原文注入（返回空，交给卡片）
    """
    if not room_id:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()

        msgs = chronicle.get_group_messages(room_id, limit=limit)
        if not msgs:
            return ""

        # 计算最后一条消息距今分钟数
        last_msg = msgs[-1]
        last_ts = last_msg.get("created_at", "")
        if not last_ts:
            return ""
        try:
            last_dt = datetime.fromisoformat(last_ts)
            now = datetime.now(timezone.utc) if last_dt.tzinfo else datetime.now()
            if last_dt.tzinfo:
                now = datetime.now(timezone.utc)
            age_minutes = (now - last_dt.replace(tzinfo=now.tzinfo) if last_dt.tzinfo else now - last_dt).total_seconds() / 60
        except ValueError:
            return ""

        # ≥30min：正式摘要卡片已在 SQL 收尾，这里不再注入原文
        if age_minutes >= 30:
            return ""

        # 会话状态宣告
        if age_minutes < 10:
            header = "## 群组动态（进行中）\n用户正在群组中交流。"
        else:
            header = "## 群组动态（最近）\n用户近期在群组中交流过。"

        parts = [header]

        # 上次临时摘要（补真空带：8~12 条之间靠摘要兜住）
        room_summary = chronicle.get_group_room_summary(room_id)
        temp_summary = room_summary.get("last_summary_text", "")
        if temp_summary:
            parts.append(f"【之前聊到】{temp_summary[:300]}")

        # 最近原文
        lines = []
        for m in msgs[-limit:]:
            sender = _group_sender_label(m.get("sender", ""))
            content = m.get("content", "")[:120]
            lines.append(f"[{sender}] {content}")
        parts.append("最近消息：\n" + "\n".join(lines))

        return "\n\n".join(parts)
    except Exception:
        return ""


def get_ingredient(name: str) -> Optional[Callable]:
    return _INGREDIENTS.get(name)


def build_recent_activity_section(private_ago_sec: Optional[float] = None,
                                   group_ago_sec: Optional[float] = None) -> str:
    """构建最近活动段：私聊/群聊两个独立时间，始终可见（非仅离开场景）"""
    if private_ago_sec is None and group_ago_sec is None:
        return ""

    def fmt(sec: float) -> str:
        if sec < 60:
            return "刚刚"
        m = int(sec / 60)
        if m < 60:
            return f"{m}分钟前"
        h = m // 60
        rm = m % 60
        return f"{h}小时{rm}分钟前" if rm > 0 else f"{h}小时前"

    parts = []
    if private_ago_sec is not None:
        parts.append(f"用户上次给你发私聊：{fmt(private_ago_sec)}")
    if group_ago_sec is not None:
        parts.append(f"用户上次在群聊说话：{fmt(group_ago_sec)}")
    return " | ".join(parts)


def build_activity_summary_section() -> str:
    """构建统一的用户当前状态摘要（纯规则，无 LLM）。

    整合设备行为、位置、状态声明、聊天活跃度、音乐等所有可感知维度。
    与 sentinel_snapshot 分工——后者负责健康传感器（心率/步数/生理期）。
    """
    try:
        from silicon_perception.analysis.activity_summary import build_activity_summary
        from silicon_perception.sentinel import get_sentinel
        from datetime import datetime

        sentinel = get_sentinel()
        if not sentinel or not sentinel._last_snapshot:
            return ""

        snap = sentinel._last_snapshot

        # ── 私聊最后时间 ──
        private_sec = None
        try:
            from mirrow_core.shared_state import get_last_private_chat_time
            pct = get_last_private_chat_time()
            if pct:
                private_sec = (datetime.now() - pct).total_seconds()
        except Exception:
            pass

        # ── 群聊活跃日发言 ──
        group_has_msg = False
        group_sec = None
        try:
            from event_chronicle import get_global_chronicle
            import sqlite3 as _sqlite3
            chronicle = get_global_chronicle()
            if chronicle:
                today = datetime.now().strftime("%Y-%m-%d")
                conn = _sqlite3.connect(chronicle.db_path)
                conn.row_factory = _sqlite3.Row
                row = conn.execute(
                    "SELECT created_at FROM group_chat_messages "
                    "WHERE date=? AND sender='user' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (today,)
                ).fetchone()
                conn.close()
                if row and row["created_at"]:
                    group_has_msg = True
                    try:
                        gm_dt = datetime.fromisoformat(row["created_at"])
                        if gm_dt.tzinfo is not None:
                            gm_dt = gm_dt.astimezone().replace(tzinfo=None)
                        group_sec = (datetime.now() - gm_dt).total_seconds()
                    except Exception:
                        pass
        except Exception:
            pass

        # ── 状态持续时长 ──
        status_dur = None
        if snap.user_status:
            try:
                from wander_manager.user_status import get_status_history
                history = get_status_history()
                if history:
                    last = history[-1]
                    if last.get("to") == snap.user_status:
                        change_dt = datetime.fromisoformat(last["time"])
                        status_dur = (datetime.now() - change_dt).total_seconds()
            except Exception:
                pass

        # ── 音乐 ──
        music_title = None
        music_artist = None
        try:
            from music_cochlea.cochlea import get_cochlea
            cochlea = get_cochlea()
            if cochlea:
                state = cochlea.get_current_state()
                if state and state.current_song:
                    music_title = state.current_song.title
                    music_artist = state.current_song.artist
        except Exception:
            pass

        # ── 字段时效 ──
        field_ages = {}
        now = datetime.now()
        for field_name in ("gps", "wifi", "current_app"):
            field_ts = snap.field_ts.get(field_name)
            if not field_ts:
                continue
            try:
                field_dt = datetime.fromisoformat(field_ts)
                if field_dt.tzinfo is not None:
                    field_dt = field_dt.astimezone().replace(tzinfo=None)
                field_ages[field_name] = (now - field_dt).total_seconds()
            except Exception:
                pass

        return build_activity_summary(
            behavior_state=snap.behavior_state,
            input_idle_seconds=snap.input_idle_seconds,
            location_category=snap.location_category,
            location_address=snap.location_address,
            wifi_is_home=snap.wifi_is_home,
            user_status=snap.user_status,
            status_duration_sec=status_dur,
            active_app_session=snap.active_app_session,
            last_private_chat_sec=private_sec,
            group_chat_today_has_msg=group_has_msg,
            last_group_chat_sec=group_sec,
            music_song_title=music_title,
            music_song_artist=music_artist,
            field_ages=field_ages,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).debug("activity_summary 构建失败", exc_info=True)
        return ""


def _fmt_last_msg_ago(sec: float) -> str:
    """秒数 → "X小时X分钟前" 人类可读。"""
    if sec < 60:
        return "刚刚"
    m = int(sec / 60)
    if m < 60:
        return f"{m}分钟前"
    h = m // 60
    rm = m % 60
    return f"{h}小时{rm}分钟前" if rm > 0 else f"{h}小时前"


def build_last_user_message_ago() -> list:
    """推送场景：距用户最后一条消息多久（DB 查询，重启安全，只统计 role='user'）。

    返回 list → extra_messages 沉底（_ts="9999"），不污染推送 system 缓存前缀。
    AI 推送时据此知道该说新话而非复述上一轮（哨兵"车轱辘话"bug 的解法之一）。
    """
    try:
        from silicon_perception.analysis.behavior_profile import get_behavior_profile
        sec = get_behavior_profile().get_last_user_message_seconds()
        if sec is None or sec >= 999999.0:
            return []
        return [{
            "role": "system",
            "content": f"距用户最后一条消息：{_fmt_last_msg_ago(sec)}",
            "_ts": "9999",  # 沉底：排对话历史之后，不打断缓存前缀
            "_section": "last_user_message_ago",
        }]
    except Exception:
        return []


# ── 注册表重新赋值（在所有函数定义之后，保证引用有效）──
_INGREDIENTS.update({
    "tool_descriptions": build_tool_descriptions,
    "user_profile": build_user_profile,
    "available_tools": build_available_tools,
    "group_chat_summary": build_group_chat_summary,
    "group_chat_current": build_group_chat_current,
    "group_tool_ban": build_group_tool_ban,
    "peer_previous": build_peer_previous,
    "round_awareness": build_round_awareness,
    "host_group_current": build_host_group_current,
    "recent_activity": build_recent_activity_section,
    "tool_data_entries": build_tool_data_entries,
    "music_cochlea": build_music_cochlea_section,
    "voice_cochlea": build_voice_cochlea_section,
    "activity_summary": build_activity_summary_section,
    "last_user_message_ago": build_last_user_message_ago,
})


def build_recent_inner_wander(
    hours: int = 36,
    limit: int = 10,
    wander_runtime_store: Any = None,
) -> str:
    """Build a bounded factual continuity view from the wander runtime store.

    The projection is intentionally bounded at the complete-activity level:
    once the next activity would exceed the budget it is omitted wholesale,
    rather than cutting one of its facts in half.
    """
    try:
        from pathlib import Path
        from wander_manager.runtime_store import WanderRuntimeStore

        store = wander_runtime_store
        if store is None:
            db_path = Path(__file__).resolve().parents[1] / "events" / "wander_runtime.db"
            store = WanderRuntimeStore(db_path)
        rows = store.list_recent_inner_wander(hours=hours, limit=limit)
    except Exception:
        logger.warning("recent inner wander ingredient unavailable", exc_info=True)
        return ""
    if not rows:
        return ""

    budget = 3200
    header = "## 最近的内在漫想（运行态事实）"
    lines = [header]
    used = len(header)
    omitted = False
    for row in rows:
        share = row.get("share")
        delivery = str(row.get("delivery") or "not_created")
        if delivery == "sent":
            outward = "已分享并送达"
        elif share is False or delivery in {"not_requested", "not_created"}:
            outward = "未分享，留在心里"
        elif delivery == "failed":
            outward = "决定分享但发送失败"
        else:
            outward = f"分享投递状态：{delivery}"
        entry_lines = [
            f"- {row.get('settled_at') or '时间未知'} · {row.get('event_type') or '未知活动'}"
            f"（{row.get('activity_id') or '活动ID未知'}）· {outward}"
        ]
        if row.get("summary"):
            entry_lines.append(f"  活动摘要：{row['summary']}")
        for node in row.get("nodes") or []:
            facts = []
            if node.get("source_summary"):
                facts.append(f"所见：{node['source_summary']}")
            if node.get("reflection"):
                facts.append(f"心路：{node['reflection']}")
            if node.get("emotion_effect"):
                facts.append(
                    "情绪变化：" + json.dumps(node["emotion_effect"], ensure_ascii=False, separators=(",", ":"))
                )
            if facts:
                entry_lines.append(f"  节点{node.get('round_index', '?')}：" + "；".join(facts))
        entry = "\n".join(entry_lines)
        addition = len(entry) + 1
        if used + addition > budget:
            omitted = True
            break
        lines.extend(entry_lines)
        used += addition
    if omitted:
        marker = "（还有历史漫想，已按上下文预算截断）"
        if used + len(marker) + 1 <= budget:
            lines.append(marker)
    return "\n".join(lines)

_INGREDIENTS["recent_inner_wander"] = build_recent_inner_wander
