"""当前可验证能力目录。

这个目录给自省和大脑架构生成器提供事实，不承担决策、语气或执行合同。
字段只描述能力的来源、状态、时效和边界；事件的可执行语义继续以
``event_catalog.EVENT_CATALOG`` 为唯一来源，聊天工具清单继续以
``behavior_scheduler.tools`` 的注册表为唯一来源。

公开版只列出本仓库实际提供或可由宿主注入的能力；不声明未提供的功能。
"""

from __future__ import annotations

import copy
import json
from typing import Any


# 这些是跨运行时稳定、且无需初始化设备即可确认的事实。易变的漫想事件
# 由 _event_records() 从 EVENT_CATALOG 读取，避免这里再维护一份事件清单。
_BASE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "domain": "漫想运行态",
        "capability": "生产漫想区间自主规划",
        "state": "available",
        "details": "默认运行时按 run→activity→node→decision/delivery 组织计划、节点执行、复核、结算与分享；未完成节点重启后进入恢复路径，用户回复会形成中断结算。",
        "source": "wander_manager/runtime_runner.py + runtime_store.py",
        "freshness": "runtime state",
        "limit": "实际节点能力和对外分享取决于对应适配器、模型和客户端状态。",
    },
    {
        "domain": "漫想事件",
        "capability": "当前生产事件目录与目标语义",
        "state": "available",
        "details": "事件名称、允许的目标模式、默认目标、上下限和节点单位来自统一事件目录。",
        "source": "wander_manager/event_catalog.py",
        "freshness": "source-defined",
        "limit": "不在目录中的事件不会进入生产运行时计划白名单。",
    },
    {
        "domain": "自省",
        "capability": "基于真实证据产生 pending 自我书候选",
        "state": "available",
        "details": "自省结果可以包含能力观察和带证据的自我书候选；候选与已写入的长期自我认知是不同状态。",
        "source": "wander_manager/self_reflection_adapter.py",
        "freshness": "per reflection",
        "limit": "一次自省不会直接覆盖长期自我认知。",
    },
    {
        "domain": "自省",
        "capability": "许愿板结构化生命周期",
        "state": "available",
        "details": "自省可以提交单一结构化 wish_action 或 no-op，并可附带评论回复；愿望状态和评论线程由独立服务保存。",
        "source": "wander_manager/wish_board_service.py",
        "freshness": "persisted state",
        "limit": "愿望动作需要已有稳定身份和可核验的当前状态。",
    },
    {
        "domain": "小红书",
        "capability": "Android 真机只读浏览",
        "state": "available_when_dedicated_device_ready",
        "details": "宿主配置的 Android 设备可通过官方 App 读取推荐或搜索结果，并用视觉理解整理帖子；本仓库提供漫想节点与读取组件，普通聊天的能力授权接线由宿主实现。",
        "source": "wander_manager/xiaohongshu_adb.py + node_execution_adapter.py",
        "freshness": "per node",
        "limit": "登录、权限、设备连接、电量、风控提示、弹窗或证据不足会使节点跳过或失败；不执行点赞、收藏、关注、评论或发布。",
    },
    {
        "domain": "小红书",
        "capability": "评论人工交接",
        "state": "available_when_requested_and_attachment_boundary_ready",
        "details": "已有评论草稿时可以交付帖子首页裁剪图和草稿给用户手动处理。",
        "source": "wander_manager/xhs_comment_delivery.py + chat attachment boundary",
        "freshness": "per requested handoff",
        "limit": "系统不会替用户点击评论或发布。",
    },
    {
        "domain": "记忆理解",
        "capability": "当前对话、长期记忆与结构化档案检索",
        "state": "available",
        "details": "上下文层按当前场景组织真实对话、长期记忆、场景锚点和结构化档案；需要细节时可追溯到被锚定的历史原文。",
        "source": "context_builder + memory_provider",
        "freshness": "per request or persisted snapshot",
        "limit": "检索结果受索引、锚点和原始数据时效影响。",
    },
    {
        "domain": "具身感知",
        "capability": "用户状态、屏幕/输入活动、位置和设备健康数据",
        "state": "available_when_source_fresh",
        "details": "感知事实带有观测时间、来源和可用性；家庭 WiFi 是在家/外出的自动位置依据，外出时 GPS 可补充位置描述。",
        "source": "silicon_perception + mobile relay",
        "freshness": "latest source report",
        "limit": "手机权限、后台连接、传感器和网络状态会影响可用性；过期数据不会被当作当前事实。",
    },
    {
        "domain": "天气感知",
        "capability": "天气作为感知上下文注入",
        "state": "available_when_weather_source_fresh",
        "details": "天气由感知源进入对话、哨兵和漫想上下文。",
        "source": "silicon_perception weather source + context builder",
        "freshness": "source-defined snapshot",
        "limit": "天气工具是否可用以宿主的实际注册表及数据源配置为准。",
    },
    {
        "domain": "行动",
        "capability": "已注册的聊天工具与本地设备行动",
        "state": "available_for_registered_tools",
        "details": "实际聊天工具目录由行为调度器注册表产生；工具结果区分模型可消费结果和只供界面记录的结果。",
        "source": "behavior_scheduler/tools.py",
        "freshness": "per invocation",
        "limit": "每项能力仍受自身设备、权限、服务和真实执行结果约束。",
    },
    {
        "domain": "后台连续性",
        "capability": "本地服务、WebSocket、手机中继和恢复运行态",
        "state": "available_when_services_running",
        "details": "消息、主动运行态和设备请求由本地服务持久化并可在受支持的重启路径恢复。",
        "source": "本地服务 + mobile relay + wander runtime store",
        "freshness": "latest health/runtime state",
        "limit": "系统省电、权限、网络或进程状态可能造成暂时不可用。",
    },
)


def _event_records() -> list[dict[str, Any]]:
    """从当前事件目录生成自省事实；导入失败时不伪造事件列表。"""

    try:
        from .event_catalog import EVENT_CATALOG
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    for event_type, strategy in EVENT_CATALOG.items():
        events.append(
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
    return events


def get_capability_records() -> tuple[dict[str, Any], ...]:
    """Return a fresh factual snapshot for self-reflection and documentation."""

    records = [copy.deepcopy(record) for record in _BASE_CATALOG]
    events = _event_records()
    if events:
        for record in records:
            if record.get("capability") == "当前生产事件目录与目标语义":
                record["events"] = events
                record["event_count"] = len(events)
                break
    return tuple(records)


def get_capability_catalog() -> str:
    """Serialize the current factual snapshot without instructions or secrets."""

    return json.dumps(
        list(get_capability_records()),
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = ["get_capability_catalog", "get_capability_records"]
