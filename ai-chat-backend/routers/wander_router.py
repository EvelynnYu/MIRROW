"""漫想模式 API 路由 — 从 main.py 提取"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# call_llm_for_wander 定义在 main.py，通过 sys.modules 访问

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wander", tags=["wander"])


# ═══════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════

class WanderStatusResponse(BaseModel):
    mode: str
    mode_display: str
    is_wander_mode_active: bool
    last_user_reply_time: Optional[str] = None
    idle_seconds: float = 0.0
    idle_threshold: float = 0.0
    current_event_type: Optional[str] = None
    creator_running: bool = False
    runtime_state: str = "idle"
    active_run_id: str = ""
    active_run_state: str = ""
    blocked_reason: str = ""
    next_plan_at: str = ""
    wake_reason: str = ""
    schedule_source_run_id: str = ""
    schedule_failure_streak: int = 0
    user_status: str = "idle"
    user_status_display: str = "空闲"
    user_status_custom_text: str = ""
    user_status_presets: dict = {}


class WanderReplyRequest(BaseModel):
    session_id: Optional[str] = None


class WanderUserStatusRequest(BaseModel):
    """用户状态更新请求"""
    status: str = "idle"  # gaming/working/out/idle/other
    custom_text: str = ""  # 其他行程的自定义文本（最多20字）


class WishStatusRequest(BaseModel):
    status: str
    note: str = ""


class WishCommentRequest(BaseModel):
    content: str = ""
    reply_to_comment_id: Optional[int] = None


class WishLifecycleRequest(BaseModel):
    action: str
    reason: str = ""


class GameInvitationRequest(BaseModel):
    game: str


# ═══════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════

@router.get("/status", response_model=WanderStatusResponse)
async def get_wander_status():
    """获取漫想模式状态"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return WanderStatusResponse(
            mode="offline",
            mode_display="随时陪伴你",
            is_wander_mode_active=False
        )

    await wander_manager.ensure_consistent_state()
    status = wander_manager.get_status()
    return WanderStatusResponse(
        mode=status.get("mode", "offline"),
        mode_display=status.get("mode_display", "随时陪伴你"),
        last_user_reply_time=status.get("last_user_reply_time"),
        idle_seconds=status.get("idle_seconds", 0),
        idle_threshold=status.get("idle_threshold", 0),
        is_wander_mode_active=status.get("is_wander_mode_active", False),
        current_event_type=status.get("current_event_type"),
        creator_running=status.get("creator_running", False),
        runtime_state=status.get("runtime_state", "idle"),
        active_run_id=status.get("active_run_id", ""),
        active_run_state=status.get("active_run_state", ""),
        blocked_reason=status.get("blocked_reason", ""),
        next_plan_at=status.get("next_plan_at", ""),
        wake_reason=status.get("wake_reason", ""),
        schedule_source_run_id=status.get("schedule_source_run_id", ""),
        schedule_failure_streak=status.get("schedule_failure_streak", 0),
        user_status=status.get("user_status", "idle"),
        user_status_display=status.get("user_status_display", "空闲"),
        user_status_custom_text=status.get("user_status_custom_text", ""),
        user_status_presets=status.get("user_status_presets", {}),
    )


@router.post("/reply")
async def notify_user_reply(request: WanderReplyRequest):
    """通知用户回复，重置漫想状态"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return {"success": False, "message": "漫想管理器不可用"}

    wander_manager.on_user_reply()
    task_scheduler_notified = False
    try:
        from task_scheduler.manager import get_initialized_task_manager
        task_manager = get_initialized_task_manager()
        if task_manager is not None:
            await task_manager.on_user_replied()
            task_scheduler_notified = True
    except Exception:
        logger.exception("task scheduler reply notification failed")
    return {"success": True, "message": "漫想已收到用户回复",
            "task_scheduler_notified": task_scheduler_notified}


@router.post("/start")
async def start_wander_mode():
    """手动启动漫想模式"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return {"success": False, "message": "漫想管理器不可用"}

    await wander_manager.activate_wander_mode()
    return {"success": True, "message": "漫想模式已启动"}


@router.post("/stop")
async def stop_wander_mode():
    """手动停止漫想模式"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return {"success": False, "message": "漫想管理器不可用"}

    await wander_manager.deactivate_wander_mode()
    return {"success": True, "message": "漫想模式已停止"}


@router.get("/games")
async def get_game_life():
    from shared_state import get_wander_manager
    from wander_manager.game_invitation import dashboard
    manager = get_wander_manager()
    if manager is None:
        raise HTTPException(503, "漫想管理器尚未就绪")
    return await dashboard(manager._wander_creator)


@router.post("/games/invite", status_code=202)
async def invite_k_to_game(request: GameInvitationRequest):
    from shared_state import get_wander_manager
    from wander_manager.game_invitation import invite
    manager = get_wander_manager()
    if manager is None:
        raise HTTPException(503, "漫想管理器尚未就绪")
    try:
        return await invite(manager._wander_creator, request.game)
    except ValueError:
        raise HTTPException(400, "游戏未接入或 AI 的小机账号未配置") from None
    except RuntimeError as exc:
        from wander_manager.game_chat_gate import BUSY_MESSAGE
        detail = ("AI 正在进行另一段漫想，请等这段结束" if str(exc) == "wander_activity_busy"
                  else BUSY_MESSAGE if str(exc) == BUSY_MESSAGE
                  else "AI 的会话或邀请消息通道尚未就绪")
        raise HTTPException(409, detail) from None


@router.get("/games/chat-focus")
async def get_game_chat_focus():
    from wander_manager.game_chat_gate import status
    return status()


@router.get("/games/playthrough")
async def get_game_playthrough(key: str):
    from shared_state import get_wander_manager
    from wander_manager.game_chronicle import read_chronicle
    manager = get_wander_manager()
    if manager is None:
        raise HTTPException(503, "漫想管理器尚未就绪")
    detail = read_chronicle(manager._wander_creator, key)
    if detail is None:
        raise HTTPException(404, "没有找到这份已确认的游玩记录")
    return detail


@router.get("/games/activity-chapters")
async def get_game_activity_chapters(activity_id: str):
    """Read a settled event's own confirmed chapters for its chat card."""
    from wander_manager.game_experience import read_activity_chapter_facts

    chapters = read_activity_chapter_facts(activity_id)
    if not chapters:
        raise HTTPException(404, "这次活动没有可展示的已确认章节")
    return {"chapters": chapters}


@router.post("/games/stop", status_code=202)
async def stop_invited_game():
    from shared_state import get_wander_manager
    from wander_manager.game_invitation import stop_invited_game as stop_game
    manager = get_wander_manager()
    if manager is None:
        raise HTTPException(503, "漫想管理器尚未就绪")
    try:
        return stop_game(manager._wander_creator)
    except RuntimeError:
        raise HTTPException(409, "当前没有人类伙伴邀请的游戏可结束") from None


@router.get("/logs")
async def get_wander_logs(limit: int = 50, pushed: Optional[bool] = None,
                          hours: Optional[int] = None, date: str = "", last_viewed_at: str = ""):
    """获取漫想日志。

    ``limit=0`` 表示在日期/时段过滤后的全量。未提供任何日期或时段时，
    默认保留最近 24 小时边界，避免一次 UI 请求扫描整个历史；调用方若
    明确传 ``hours=0`` 仍可请求无时段边界的全量结果。
    """
    filter_hours = 24 if hours is None and not date else (hours or 0)
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        # 启动期管理器尚未挂入 shared_state 时，日志仍以 runtime SQLite 为权威。
        # 直接读持久化库，避免前端先收到空数组后误以为历史被清空。
        from wander_manager.runtime_store import WanderRuntimeStore
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "events", "wander_runtime.db")
        store = WanderRuntimeStore(db_path)
        store.initialize()
        logs = store.list_activity_logs(limit=limit, pushed=pushed, hours=filter_hours, date=date)
        stats = store.activity_log_stats(
            pushed=pushed, hours=filter_hours, date=date, last_viewed_at=last_viewed_at,
        )
        return {"logs": logs, "stats": stats, "schedule": store.get_scheduler_wake()}

    runtime_logs = wander_manager.get_runtime_logs(
        limit=limit, pushed=pushed, hours=filter_hours, date=date
    )
    runtime_stats = wander_manager.get_runtime_log_stats(
        pushed=pushed,
        hours=filter_hours,
        date=date,
        last_viewed_at=last_viewed_at,
    )
    return {
        "logs": runtime_logs or [],
        "stats": runtime_stats or {},
        "schedule": wander_manager.get_runtime_schedule(),
    }


@router.get("/brain-architecture")
async def get_brain_architecture():
    """获取AI的大脑架构文档（缓存内容，手动刷新触发生成）"""
    from wander_manager.brain_architecture import get_cached_content
    return get_cached_content()


@router.get("/body-knowledge")
async def get_body_knowledge():
    """Shared read-only body view; never writes a book or calls a model."""
    from wander_manager.body_knowledge import get_body_knowledge as current_body
    from wander_manager.brain_architecture import get_cached_content
    return {**current_body(), "narrative": get_cached_content()}


@router.post("/brain-architecture/refresh")
async def refresh_brain_architecture(force: bool = False):
    """手动刷新架构散文：核对来源内容收据；过期或 force=True 时调用模型。"""
    import sys
    call_llm_for_wander = (sys.modules.get("main") or sys.modules.get("__main__")).call_llm_for_wander
    from wander_manager.brain_architecture import generate_brain_architecture
    return await generate_brain_architecture(call_llm_for_wander, force)


@router.get("/wish-history")
async def get_wish_history():
    """获取AI的许愿历史。"""
    from wander_manager.wish_history import load_wish_history
    return load_wish_history()


@router.post("/wish-history/fulfill")
async def mark_wish_fulfilled(request: Request):
    """标记某个愿望为已实现。Body: {"feature": "功能名", "note": "备注(可选)"}"""
    from wander_manager.wish_history import mark_fulfilled
    try:
        body = await request.json()
    except Exception:
        return {"success": False, "message": "请求体不是有效的 JSON"}
    feature = body.get("feature", "")
    note = body.get("note", "")
    try:
        success = mark_fulfilled(feature, note)
    except RuntimeError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": success, "message": "已标记为已实现" if success else f"未找到待实现的愿望: {feature}"}


@router.get("/wishes")
async def list_wishes():
    """获取许愿板快照、评论线程与追加历史，供全局弹窗渲染。"""
    from wander_manager.wish_store import get_wish_store
    return get_wish_store().get_board()


@router.put("/wishes/{wish_id}/status")
async def set_wish_status(wish_id: int, request: WishStatusRequest):
    """人类伙伴用三个状态按钮更新愿望；每次更新都留下 wish_events 历史。"""
    from wander_manager.wish_store import get_wish_store
    try:
        result = get_wish_store().set_status(
            wish_id, request.status, actor="human", note=request.note
        )
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}
    return {
        "success": bool(result.get("wish")),
        "mutated": bool(result.get("mutated")),
        "wish": result.get("wish"),
        "event": result.get("event"),
    }


@router.post("/wishes/{wish_id}/comments")
async def add_wish_comment(wish_id: int, request: WishCommentRequest):
    """追加一条平铺评论；不覆盖旧评论，也不凭关键词改变状态。"""
    from wander_manager.wish_store import get_wish_store
    content = (request.content or "").strip()
    if not content:
        return {"success": False, "message": "评论不能为空"}
    try:
        result = get_wish_store().add_comment(
            wish_id,
            content,
            author="human",
            reply_to_comment_id=request.reply_to_comment_id,
        )
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}
    return {
        "success": bool(result.get("comment")),
        "mutated": bool(result.get("mutated")),
        "comment": result.get("comment"),
        "event": result.get("event"),
    }


@router.put("/wishes/comments/{comment_id}")
async def edit_wish_comment(comment_id: int, request: WishCommentRequest):
    """编辑人类伙伴自己写下的一条评论，并保留编辑事件。"""
    from wander_manager.wish_store import get_wish_store
    try:
        result = get_wish_store().edit_comment(comment_id, request.content, actor="human")
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}
    message = {"not_found": "评论不存在", "not_owner": "只能编辑人类伙伴自己的评论"}.get(result.get("error"), "")
    return {
        "success": bool(result.get("comment")),
        "mutated": bool(result.get("mutated")),
        "comment": result.get("comment"),
        "event": result.get("event"),
        "message": message,
    }


@router.delete("/wishes/comments/{comment_id}")
async def delete_wish_comment(comment_id: int):
    """删除人类伙伴自己写下的一条评论，愿望历史保留删除记录。"""
    from wander_manager.wish_store import get_wish_store
    try:
        result = get_wish_store().delete_comment(comment_id, actor="human")
    except RuntimeError as exc:
        return {"success": False, "message": str(exc)}
    message = {"not_found": "评论不存在", "not_owner": "只能删除人类伙伴自己的评论"}.get(result.get("error"), "")
    return {
        "success": bool(result.get("deleted")),
        "mutated": bool(result.get("mutated")),
        "deleted": result.get("deleted"),
        "event": result.get("event"),
        "message": message,
    }


@router.post("/wishes/{wish_id}/lifecycle")
async def wish_lifecycle(wish_id: int, request: WishLifecycleRequest):
    """AI 对人类伙伴标记为天方夜谭的愿望作保留/删除决定。"""
    from wander_manager.wish_store import get_wish_store
    action = (request.action or "").strip().lower()
    target = {"retain_impossible": "impossible_kept", "delete_impossible": "deleted"}.get(action)
    if target is None:
        return {"success": False, "message": "action 必须是 retain_impossible 或 delete_impossible"}
    try:
        result = get_wish_store().set_status(
            wish_id, target, actor="k", note=request.reason
        )
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}
    return {
        "success": bool(result.get("wish")),
        "mutated": bool(result.get("mutated")),
        "wish": result.get("wish"),
        "event": result.get("event"),
    }


@router.put("/wishes/{wish_id}/comment")
async def set_wish_comment(wish_id: int, request: Request):
    """人类伙伴对某愿望的反馈评论。Body: {"comment": "..."}

    这是旧客户端兼容端点；新客户端使用 ``/comments`` 追加评论。
    """
    from wander_manager.wish_store import get_wish_store
    try:
        body = await request.json()
    except Exception:
        return {"success": False, "message": "请求体不是有效的 JSON"}
    comment = (body.get("comment", "") or "").strip()
    store = get_wish_store()
    try:
        ok = store.set_comment(wish_id, comment)
    except RuntimeError as exc:
        return {"success": False, "message": str(exc), "auto_fulfilled": False}

    return {
        "success": ok,
        "message": "已保存反馈" if ok else f"未找到愿望 id={wish_id}",
        "auto_fulfilled": False,
    }


@router.delete("/wishes/{wish_id}")
async def delete_wish(wish_id: int):
    """兼容旧客户端的软删除；愿望行和删除事件都会保留。"""
    from wander_manager.wish_store import get_wish_store
    try:
        ok = get_wish_store().delete(wish_id)
    except RuntimeError as exc:
        return {"success": False, "message": str(exc)}
    return {"success": ok, "message": "已删除" if ok else f"未找到愿望 id={wish_id}"}


@router.post("/user-status")
async def set_wander_user_status(request: WanderUserStatusRequest):
    """设置用户状态（前端选择），供漫想模式LLM感知用户当前活动"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return {"success": False, "message": "漫想管理器不可用"}

    from wander_manager.user_status import UserStatus
    valid_statuses = [s.value for s in UserStatus]
    if request.status not in valid_statuses:
        return {"success": False, "message": f"无效状态: {request.status}，有效值: {valid_statuses}"}

    wander_manager.set_user_status(request.status, request.custom_text)
    logger.info(f"用户状态已设置: {request.status}, custom_text={request.custom_text}")
    return {"success": True, "message": f"用户状态已设置为: {request.status}"}


@router.post("/test-push")
async def test_wander_push():
    """测试推送：跳过整个漫想链路，直接向WebSocket推送一条测试消息"""
    from shared_state import get_wander_manager
    wander_manager = get_wander_manager()
    if not wander_manager:
        return {"success": False, "message": "漫想管理器不可用"}

    test_message = "[测试] 这是一条漫想模式测试消息 - 如果你看到这条消息，说明WebSocket推送链路是通的！"
    print(f"[TEST_PUSH] 发送测试消息: {test_message}", flush=True)

    import sys
    _main = sys.modules.get("main") or sys.modules.get("__main__")
    wander_connections = getattr(_main, 'wander_connections', set())
    before_count = len(wander_connections)
    print(f"[TEST_PUSH] 当前WebSocket连接数: {before_count}", flush=True)

    _push_wander_message = _main._push_wander_message
    await _push_wander_message({"message": test_message, "tool_calls": [], "event_id": f"test_{datetime.now().timestamp()}"})

    wander_connections = getattr(_main, 'wander_connections', set())
    after_count = len(wander_connections)
    print(f"[TEST_PUSH] 发送后连接数: {after_count}", flush=True)

    return {
        "success": True,
        "message": "测试消息已发送",
        "connections_before": before_count,
        "connections_after": after_count
    }
