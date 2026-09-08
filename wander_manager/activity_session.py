# 漫想活动会话数据模型
#
# 「全自主区间式行动」的核心数据结构：把旧的「原子事件」升级为「持续活动会话」。
# 一次活动 = 一个 termination_mode + 目标时长/次数 + 一串节点 + 情绪快照。
#
# 这是 v2 重写的地基之一（与 node_boundary.py / planner.py / wander_journal.py 配套）。

from datetime import datetime
from enum import Enum
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
import uuid


class TerminationMode(Enum):
    """终止模式 —— 决定一个活动怎么「结束」"""
    DURATION = "duration"        # 定时器（睡眠/游戏/阅读）
    COUNT = "count"              # wander_round 计数（听歌 N 首/看新闻 N 篇/翻收藏 N 条）
    OPEN_ENDED = "open_ended"    # 无硬定时，节点判断 LLM 说「想完了」+ 软上限兜底（胡思乱想/自省）


class BoundaryReason(Enum):
    """节点边界 LLM 的触发时机 —— 区分「为什么此刻结算」"""
    NODE_COMPLETE = "node_complete"      # 一个节点自然完成（一首歌听完）
    COUNT_REACHED = "count_reached"      # 计数到达目标（is_final）
    TIMER_END = "timer_end"              # 定时器到时（is_final）
    USER_INTERRUPT = "user_interrupt"    # 用户发消息打断
    EMOTION_SHIFT = "emotion_shift"      # 大幅跨越式情绪变化


@dataclass
class NodeRecord:
    """活动内部的一个节点（一首歌 / 一篇新闻 / 一条收藏）"""
    round_index: int                      # 第几个节点（从 1 开始）
    summary: str = ""                     # 节点摘要（歌曲名 / 新闻标题）
    reflection: str = ""                  # 感想（无深层思考则留空，由节点边界 LLM 决定）
    emotion_delta: str = ""               # 本节点引发的情绪 delta 描述
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "round_index": self.round_index,
            "summary": self.summary,
            "reflection": self.reflection,
            "emotion_delta": self.emotion_delta,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ActivitySession:
    """一次持续活动会话。

    由计划 LLM 决定「做什么 + 做多久/几次」，由节点边界 LLM 决定「继续 / 中止 / 结算」。
    """
    activity_type: str                                   # "listen_music" / "browse_news" 等
    termination_mode: TerminationMode = TerminationMode.COUNT
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    target_count: int = 0                                 # count 模式：目标次数
    target_duration_min: int = 0                          # duration 模式：目标时长（分钟）
    soft_ceiling_min: int = 30                            # open_ended 软上限（分钟）
    current_round: int = 0
    nodes: List[NodeRecord] = field(default_factory=list)
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    start_mood: str = ""                                  # 开始时 Affect v2 自我状态自然语言快照
    end_mood: str = ""                                    # 结束时 Affect v2 自我状态快照
    abort_reason: str = ""                                # 提前中止原因
    status: str = "active"                                # active / completed / aborted / interrupted
    detail: Dict[str, Any] = field(default_factory=dict)  # 事件详情（歌曲列表等，handler 专属）

    # ── 计数 / 终止判断 ──

    def is_count_reached(self) -> bool:
        """count 模式：是否已达目标次数"""
        return self.termination_mode == TerminationMode.COUNT and \
            self.target_count > 0 and self.current_round >= self.target_count

    def add_node(self, summary: str = "", reflection: str = "", emotion_delta: str = "") -> NodeRecord:
        """追加一个节点并推进 current_round。返回新节点。"""
        self.current_round += 1
        node = NodeRecord(
            round_index=self.current_round,
            summary=summary,
            reflection=reflection,
            emotion_delta=emotion_delta,
        )
        self.nodes.append(node)
        return node

    def mark_ended(self, status: str, end_mood: str = "", abort_reason: str = ""):
        """标记会话终态。"""
        self.status = status
        self.end_time = datetime.now()
        self.end_mood = end_mood
        self.abort_reason = abort_reason

    def elapsed_minutes(self) -> float:
        """已进行分钟数（用于 duration 模式到点判断 / open_ended 软上限）"""
        end = self.end_time or datetime.now()
        return max(0.0, (end - self.start_time).total_seconds() / 60.0)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "activity_type": self.activity_type,
            "termination_mode": self.termination_mode.value,
            "target_count": self.target_count,
            "target_duration_min": self.target_duration_min,
            "soft_ceiling_min": self.soft_ceiling_min,
            "current_round": self.current_round,
            "nodes": [n.to_dict() for n in self.nodes],
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "start_mood": self.start_mood,
            "end_mood": self.end_mood,
            "abort_reason": self.abort_reason,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActivitySession":
        session = cls(
            activity_type=data.get("activity_type", ""),
            termination_mode=TerminationMode(data.get("termination_mode", "count")),
            session_id=data.get("session_id", uuid.uuid4().hex),
            target_count=int(data.get("target_count", 0)),
            target_duration_min=int(data.get("target_duration_min", 0)),
            soft_ceiling_min=int(data.get("soft_ceiling_min", 30)),
            current_round=int(data.get("current_round", 0)),
            start_mood=data.get("start_mood", ""),
            end_mood=data.get("end_mood", ""),
            abort_reason=data.get("abort_reason", ""),
            status=data.get("status", "active"),
            detail=data.get("detail", {}),
        )
        try:
            session.start_time = datetime.fromisoformat(data["start_time"])
        except Exception:
            session.start_time = datetime.now()
        if data.get("end_time"):
            try:
                session.end_time = datetime.fromisoformat(data["end_time"])
            except Exception:
                session.end_time = None
        session.nodes = [
            NodeRecord(
                round_index=n.get("round_index", i + 1),
                summary=n.get("summary", ""),
                reflection=n.get("reflection", ""),
                emotion_delta=n.get("emotion_delta", ""),
                timestamp=datetime.fromisoformat(n["timestamp"]) if n.get("timestamp") else datetime.now(),
            )
            for i, n in enumerate(data.get("nodes", []))
        ]
        return session
