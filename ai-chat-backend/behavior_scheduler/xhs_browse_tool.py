"""Main-chat access to the same dedicated-device XHS runtime as Wander."""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable, Mapping, Optional

from .base_tool import BaseTool, ToolResult, ToolStatus
from .execution_context import get_request_identity


class XhsBrowseTool(BaseTool):
    name = "browse_xiaohongshu"
    description = '用AI的专用Android手机只读浏览小红书。instruction自然表达首页、主题搜索、搜索账号并读主页、或按回执task_id续读；可要求全部主页，每批最多5篇，返回进度和未覆盖范围。图文发布由publish_xiaohongshu执行；评论、下单和指定链接直读不属于本工具。xhs_posts负责本地留帖和分享。'
    flash_description = '专用手机小红书只读行动；自然语言目标、搜索账号主页、持久进度续读'
    pro_guide = '自然表达你要在专用手机上看的内容；读主页可指定账号和范围，续读带上上次回执task_id。回执包含实际正文、逐页图像观察及覆盖范围。'
    parameters_schema = {'type': 'object', 'properties': {
        'instruction': {'type': 'string', 'description': '自然语言行动目标；续读附回执task_id'}},
        'required': ['instruction'], 'additionalProperties': False}
    single_use = False

    def __init__(self, *, runner: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None):
        self.runner = runner or _run_v3_chat_event

    def get_user_facing_description(self, **kwargs) -> str:
        return "正在用专用手机浏览小红书"

    async def execute(self, instruction: str = '', **kwargs) -> ToolResult:
        session_id, _, turn_id = get_request_identity()
        if not session_id:
            return ToolResult(ToolStatus.FAILED, '本次浏览缺少会话身份，未执行。', error='session_id_required')
        if kwargs or not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
            return ToolResult(ToolStatus.FAILED, '需要具体的只读行动目标。', error='invalid_request')
        try:
            result = self.runner(session_id=session_id, instruction=instruction, source_turn_id=turn_id, source="chat_tool")
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return ToolResult(ToolStatus.ERROR, "只读设备能力执行异常。", error=type(exc).__name__)
        payload = result if isinstance(result, Mapping) else {"status": "invalid_result"}
        content = _compact_result(payload)
        if payload.get("status") == "success" or (payload.get('status') == 'partial_success' and payload.get('can_continue_now')):
            return ToolResult(ToolStatus.SUCCESS, content)
        return ToolResult(ToolStatus.FAILED, content, error=str(payload.get("error") or payload.get("status") or "failed"))


def _safe_material(value):
    from wander_manager.xhs_request import material_for_ai
    return material_for_ai(value)


def _compact_result(payload: Mapping[str, Any]) -> str:
    """Return bounded evidence without raw screenshots or device data.

    The runner's aggregate is already evidence-only, but this boundary also
    sanitizes injected/test results so a future handler cannot accidentally
    put raw ADB/UI payloads or model audit text into AI's follow-up prompt.
    """

    def _source(value: Any, *, fallback_id: str = "") -> dict[str, str]:
        if not isinstance(value, Mapping):
            value = {}
        provider = str(value.get("provider") or value.get("source_provider") or "")[:120]
        url = str(value.get("url") or value.get("source_url") or "")[:500]
        source_id = str(value.get("source_id") or fallback_id or "")[:200]
        return {"provider": provider, "url": url, "source_id": source_id}

    def _post(value: Any, index: int = 0) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        source_id = str(value.get("source_id") or "")[:200]
        reliable = _source(value.get("reliable_source") or value.get("source"), fallback_id=source_id)
        if not reliable.get("provider") and not reliable.get("url"):
            reliable = _source(value, fallback_id=source_id)
        result: dict[str, Any] = {
            "index": int(value.get("index") or index or 1),
            "node_id": str(value.get("node_id") or "")[:80],
            "source_id": source_id,
            "title": str(value.get("title") or "未命名帖子").strip()[:240],
            "summary": str(value.get("summary") or value.get("content_summary") or "未取得文字摘要").strip()[:900],
            "evidence_excerpt": str(value.get("evidence_excerpt") or "").strip()[:500],
            "reliable_source": reliable,
            **({'material': _safe_material(value['material'])} if isinstance(value.get('material'), Mapping) else {}),
        }
        return result

    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, Mapping):
        aggregate = {}
    raw_posts = payload.get("posts")
    if not isinstance(raw_posts, list):
        raw_posts = aggregate.get("posts") if isinstance(aggregate.get("posts"), list) else []
    posts = [_post(item, index) for index, item in enumerate(raw_posts, 1)]
    posts = [item for item in posts if item]
    target_count = payload.get("target_count", aggregate.get("target_count"))
    success_count = payload.get("success_count", aggregate.get("success_count"))
    cross_items = [
        {
            "title": item["title"],
            "summary": item["summary"],
            "evidence_excerpt": item["evidence_excerpt"],
            "reliable_source": item["reliable_source"],
        }
        for item in posts
    ]
    safe: dict[str, Any] = {}
    for key in (
        "status", "barrier", "error", "run_id", "activity_id", "node_id",
        "node_ids", "source", "delivery_status", "source_id", "source_url",
        "query", "title", "content_summary", "evidence_excerpt", "limitations",
        "target_count", "success_count", "successful_count", "post_count",
        "task_id", "progress", "coverage", "resumable", "can_continue_now", "instruction",
    ):
        value = payload.get(key)
        if key == "node_ids" and isinstance(value, list):
            value = [str(item)[:80] for item in value]
        elif key in {"source_url"}:
            value = str(value or "")[:500]
        elif key in {"content_summary", "limitations"}:
            value = str(value or "")[:1200]
        elif key in {"evidence_excerpt"}:
            value = str(value or "")[:500]
        if value not in (None, "", [], {}):
            safe[key] = value

    if posts:
        safe["posts"] = posts
        if aggregate.get("summary"):
            safe["aggregate_summary"] = str(aggregate.get("summary"))[:2000]
        safe["cross_post_summary_material"] = {
            "target_count": target_count,
            "success_count": success_count,
            "items": cross_items,
            "comparison_basis": str(
                aggregate.get("cross_post_summary_material", {}).get("comparison_basis")
                if isinstance(aggregate.get("cross_post_summary_material"), Mapping)
                else aggregate.get("comparison_basis")
                or "横向材料仅由各篇独立的专用手机屏幕证据组成；未把未读内容补入总结。"
            )[:300],
        }
    elif isinstance(aggregate.get("summary"), str) and aggregate.get("summary"):
        safe["aggregate_summary"] = str(aggregate["summary"])[:2000]

    status = str(payload.get("status") or "")
    barrier = str(payload.get("barrier") or payload.get("error") or "")
    try:
        target_number = int(target_count or 0)
        success_number = int(success_count or 0)
    except (TypeError, ValueError):
        target_number = success_number = 0
    if barrier == "no_feed_cards":
        safe["feedback"] = "页面没有识别到可读帖子，暂时没有完成本轮浏览。"
    elif barrier == 'profile_listing_stalled':
        safe['feedback'] = '主页列表停滞，尚未观察到明确末尾；已经读取的材料已保存。'
    elif barrier == "no_new_feed_cards":
        safe["feedback"] = "页面上的可读帖子都已经读过，本轮没有取得新的帖子。"
    elif status == "partial_success" or (target_number and 0 < success_number < target_number):
        safe["feedback"] = f"本轮取得{success_number}/{target_number}篇；未取得的部分不作推断。"
    elif status and status not in {"success"} and not posts:
        safe["feedback"] = "本轮只读浏览未完成；未取得可核实的帖子材料。"

    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))


async def _run_v3_chat_event(**kwargs) -> dict[str, Any]:
    """Resolve the already-initialized Wander manager without creating one."""

    try:
        from shared_state import get_wander_manager
        manager = get_wander_manager()
    except Exception:
        manager = None
    if manager is None or not hasattr(manager, "browse_xhs_from_chat"):
        return {"status": "awaiting_runtime", "barrier": "wander_runtime_unavailable"}
    return await manager.browse_xhs_from_chat(**kwargs)
