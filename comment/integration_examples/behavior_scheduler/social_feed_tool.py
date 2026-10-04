"""AI's one-shot social-feed tool.

The tool is deliberately a very small adapter around the authoritative
``social_feed`` store and the existing Wander ``BrowseSocialFeedHandler``.
It never returns feed contents to the model: its result is mounted as a
UI-only tool record, while the handler's bounded evidence remains available
to diagnostics/tests.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .base_tool import BaseTool, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class ManageSocialFeedTool(BaseTool):
    """Read or perform AI-side feed actions."""

    name = "manage_social_feed"
    description = (
        "打开 MIRROW·Comment（共域，旧称朋友圈），供站主、AI 与已接入朋友参与。action=review 从 AI 的相关未读开始，"
        "按节点继续看最新十条或模糊搜索，也可切换到已注册的其他共域；每个节点只在当前一家互动。"
        "也可直接执行一个有真实目标的 post/comment/like/forward/delete/set_visibility，或 nickname 修改自己的公开网名。"
        "delete 仅能删除 AI 自己的帖子，set_visibility 仅能修改 AI 自己的帖子；没有确认过真实帖子 ID 时先用 review 阅读或搜索定位，不能凭正文编 ID。"
        "工具结果只进入前端挂载条，"
        "不会回注模型或触发第二轮回复。post 的 visibility=public 时持有访问权的朋友可在共享朋友圈看到并互动；"
        "visibility=private 时仅本地站主和 AI 可见可互动，朋友家看不到。改回 private 后朋友不能继续查看或互动；"
        "缺失或非法时按 private 处理。公开不代表已逐一推送给朋友。"
        "直接操作时可传已注册共域的 site_id；在别家只能发表公开动态，该家主人可管理互动。"
        "review 可用 read_scope=all 合并浏览有 AI Key 的已注册域，全局每页十条；visibility_filter=all/public/private 独立于发帖权限。"
        "私密只读本家；跨域浏览先选来源，下一节点读取原帖再在一家批量操作；支持 inbox_v1 的远端从真实未读开始，旧域才读取最新。"
        "艾特只能传已读取人物投影中的稳定 mention_actor_ids，不能从昵称或正文猜人；旧远端不支持时会明确失败。"
    )
    flash_description = (
        "打开共域（旧称朋友圈）：review 从相关未读开始，逐节点看最新十条或搜索、批量互动，AI 决定退出；"
        "read_scope=all 可合并浏览已注册域，visibility_filter 筛选全部/公开/本家私密；每节点仍只在一家操作。"
        "或直接发帖、评论、点赞、引用转发公开原帖、删除、改 AI 自己帖子的可见性或共友圈网名；forward 的 source_site_id 是来源、site_id 是目的，content 是本人附言，跨家不复制原文；public 朋友可见可互动，private 仅站主和 AI 本地可见可互动；结果不回注模型"
    )
    pro_guide = ("MIRROW·Comment（共域，旧称朋友圈）。action：review(从本家相关未读开始，可逐节点搜索真实帖子、切到已注册的别家共域，每节点只操作一家) / post / comment / like / forward / delete / set_visibility / nickname。"
                 "forward 将 source_site_id（缺省本家）中已读的公开 moment_id 引用到 site_id（缺省本家），content 是本人附言；原文仍在原站，跨家引用需在来源域登录查看，私密与转发帖不支持再转发。"
                 "delete 与 set_visibility 只限 AI 自己在本家的真实帖子；comment/like/delete/set_visibility 的 moment_id 必须是已读快照中的真实帖子 ID，不能由正文猜造；不知道 ID 就先 review 并搜索。"
                 "site_id 可填已注册共域 ID 或精确站名；别家只可发 public，帖子及互动由该家主人管理。"
                 "review 支持 read_scope=current/all 和 visibility_filter=all/public/private；进入后 next.kind=latest 的 read_scope=home/all/current，省略默认全域十条。all 合并每页十条，仅使用 AI 的远端 Key，先选来源再到单域节点操作；支持 inbox_v1 的远端读取真实未读，旧域才从最新开始。"
                 "本家 post 的 visibility=public 表示有访问权的朋友可见可评论点赞，private 表示仅站主和 AI 本地可见可互动，朋友家不可见；缺失按 private。结果只进挂载条，不触发第二轮。")
    parameters_schema = {
        "type": "object",
        "properties": {
            "read_scope": {"type":"string","enum":["current","all"],"description":"review 读取范围，current 缺省从本家未读开始，all 为已注册且有 AI Key 的域合并最新十条；仅浏览，实际操作需单域节点"},
            "visibility_filter": {"type":"string","enum":["all","public","private"],"description":"review 阅读筛选，私密只读本家；与写入的 visibility 不同"},
            "action": {
                "type": "string",
                "enum": ["review", "post", "comment", "like", "forward", "delete", "set_visibility", "nickname"],
                "description": "一次性动作",
            },
            "day": {
                "type": "string",
                "description": "可选查看日期 YYYY-MM-DD；缺省时查看完整连续时间线",
            },
            "moment_id": {
                "type": "string",
                "description": "已读取的真实目标帖子 ID（comment/like/forward/delete/set_visibility）；forward 的 ID 属于 source_site_id",
            },
            "source_site_id": {"type":"string","description":"forward 的来源共域 ID；缺省本家。site_id 是转发目的域，两者分别核验，不按网名推断来源。"},
            "content": {
                "type": "string",
                "description": "帖子或评论正文（post/comment）；forward 时仅为自己的附言，不复制原帖",
            },
            "visibility": {
                "type": "string",
                "enum": ["private", "public"],
                "description": "发帖或 set_visibility 的范围；public 为朋友可见可互动，private 仅本地站主和 AI 可见可互动；发帖缺省/非法按 private",
            },
            "reply_to_id": {
                "type": "string",
                "description": "可选的真实评论 ID",
            },
            "mention_actor_ids": {
                "type": "array", "items": {"type": "string"}, "maxItems": 8,
                "description": "post/comment 可选的已读取稳定人物 actor ID；不解析正文中的 @昵称",
            },
            "activity_reason": {
                "type": "string",
                "description": "review 的简短缘由",
            },
            "nickname": {
                "type": "string",
                "description": "action=nickname 时 AI 想在共友圈使用的网名；不改变登记身份或头像",
            },
            "site_id": {
                "type": "string",
                "description": "可选已注册共域 ID 或精确站名；review 可从指定家的最新动态起步，省略从本家未读起步；read_scope=all 时合并浏览。直接写入省略表示本家。",
            },
        },
        "required": ["action"],
    }
    single_use = True
    ui_only_result = True

    def __init__(
        self,
        call_llm_func: Callable | None = None,
        *,
        store: Any = None,
        on_notification: Callable | None = None,
    ) -> None:
        self.call_llm = call_llm_func
        self.store = store
        self.on_notification = on_notification

    def set_dependencies(
        self,
        *,
        call_llm_func: Callable | None = None,
        store: Any = None,
        on_notification: Callable | None = None,
        run_visit: Callable | None = None,
    ) -> None:
        if call_llm_func is not None:
            self.call_llm = call_llm_func
        if store is not None:
            self.store = store
        if on_notification is not None:
            self.on_notification = on_notification
        if run_visit is not None:
            self.run_visit = run_visit

    def get_user_facing_description(self, **kwargs) -> str:
        """Expose one stable outer label for every one-shot feed operation.

        The action/result belongs to the private feed itself.  Persisted and
        live chat tool mounts intentionally do not turn that implementation
        detail into a second chat message or an expandable transcript.
        """
        return "打开了共域"

    @staticmethod
    def _stable_source_key(parameters: dict[str, Any]) -> str:
        explicit = str(parameters.get("source_key") or "").strip()
        if explicit:
            return explicit[:200]
        identity = {
            "run_id": str(parameters.get("run_id") or "").strip(),
            "activity_id": str(parameters.get("activity_id") or "").strip(),
            "node_id": str(parameters.get("node_id") or "").strip(),
        }
        if all(identity.values()):
            raw = "social-feed-action:%s:%s:%s" % (
                identity["run_id"], identity["activity_id"], identity["node_id"]
            )
        else:
            # The request-scoped original user message makes a repeated retry
            # of one chat turn idempotent without persisting the message text.
            try:
                from .execution_context import get_request_user_message

                request_message = get_request_user_message()
            except Exception:
                request_message = ""
            raw = json.dumps(
                {
                    # Revoke/resend on the same Beijing day replays the same
                    # action, while an intentional same-text post on a later
                    # day remains a new action.
                    "day_key": datetime.now(
                        timezone(timedelta(hours=8), name="Asia/Shanghai")
                    ).date().isoformat(),
                    "request": request_message,
                    "action": parameters.get("action"),
                    "site_id": parameters.get("site_id"),
                    "source_site_id": parameters.get("source_site_id"),
                    "day": parameters.get("day"),
                    "moment_id": parameters.get("moment_id"),
                    "content": parameters.get("content"),
                    "visibility": parameters.get("visibility"),
                    "reply_to_id": parameters.get("reply_to_id"),
                    "mention_actor_ids": parameters.get("mention_actor_ids") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            raw = "social-feed-chat:" + raw
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _fallback_call_llm() -> Callable | None:
        try:
            from llm_client import call_llm_for_behavior

            return call_llm_for_behavior
        except Exception:
            return None

    def _get_store(self):
        if self.store is not None:
            return self.store
        from social_feed import get_social_feed_store

        return get_social_feed_store()

    async def _notify(self, source_id: str, action: str) -> bool:
        callback = self.on_notification
        if callback is None:
            try:
                import sys

                main_module = sys.modules.get("main") or sys.modules.get("__main__")
                callback = getattr(main_module, "_emit_ui_notification", None)
            except Exception:
                callback = None
        if not callback or not source_id:
            return False
        from notification_service import social_feed_action_content
        # The mutation has completed. The notification states only the action
        # known here; the authoritative feed remains the source for details.
        content = social_feed_action_content(action)
        try:
            result = callback(
                "", source_id, None,
                event_type="social_feed_update", content=content,
            )
            if inspect.isawaitable(result):
                result = await result
            return bool(isinstance(result, dict) and (
                result.get("mutated") or result.get("reason") == "already_persisted"
            ))
        except TypeError:
            # Compatibility with older injected callbacks that do not accept
            # the optional content argument.
            try:
                result = callback("", source_id, None, event_type="social_feed_update")
                if inspect.isawaitable(result):
                    result = await result
                return bool(isinstance(result, dict) and (
                    result.get("mutated") or result.get("reason") == "already_persisted"
                ))
            except Exception:
                logger.warning("social-feed notification callback failed", exc_info=True)
                return False
        except Exception:
            logger.warning("social-feed notification callback failed", exc_info=True)
            return False

    @staticmethod
    def _evidence(result: dict[str, Any], *, action: str) -> dict[str, Any]:
        """Retain only bounded IDs/counts in the tool mount metadata."""
        if action == "review":
            keys = (
                "status", "summary", "node_count", "run_id", "activity_id", "barrier",
                "selected_date", "moment_count", "viewed_moment_count",
                "viewed_moment_ids", "unread_count", "unread_notification_ids",
                "unread_kinds", "action", "moment_id", "comment_id", "visibility",
                "frontend_notified", "reason", "error", "site_id", "site_name",
            )
            return {key: result[key] for key in keys if key in result}
        return {
            key: result[key]
            for key in (
                "status", "action", "moment_id", "comment_id", "visibility", "nickname",
                "changed", "deleted", "previous_visibility", "frontend_notified",
                "reason", "error", "idempotent_replay", "site_id", "site_name",
            )
            if key in result
        }

    async def _review(self, parameters: dict[str, Any]) -> dict[str, Any]:
        from .execution_context import get_request_identity
        request_session, request_message, request_turn = get_request_identity()
        session_id = str(request_session or parameters.get("session_id") or "")
        run_visit = getattr(self, "run_visit", None)
        read_scope = str(parameters.get('read_scope') or 'current')
        visibility_filter = str(parameters.get('visibility_filter') or 'all')
        if read_scope not in {'current','all'} or visibility_filter not in {'all','public','private'}:
            return {'status':'invalid_request','error':'invalid_social_read_filter'}
        initial = None
        if read_scope=='all':
            initial = {'kind':'all','visibility_filter':visibility_filter}
        elif parameters.get('site_id'):
            from social_feed.remote_sites import get_remote_site_store
            wanted = str(parameters['site_id']).strip()
            sites = [s for s in get_remote_site_store().list() if s.enabled and s.ai_key and (s.id==wanted or s.name==wanted)]
            if len(sites)!=1 or visibility_filter=='private':
                return {'status':'invalid_request','error':'social_site_or_filter_unavailable'}
            initial = {'kind':'unread','site_id':sites[0].id,'visibility_filter':visibility_filter}
        elif visibility_filter!='all':
            initial = {'kind':'latest','visibility_filter':visibility_filter}
        if run_visit is not None:
            return await run_visit(
                session_id=session_id,
                activity_reason=str(parameters.get("activity_reason") or "站主邀请 AI 打开共友圈"),
                source_turn_id=str(request_turn or request_message or ""),
                **({'initial_request':initial} if initial else {}),
            )
        if initial:
            return {'status':'runtime_unavailable','error':'social_multi_node_runtime_required'}
        call_llm = self.call_llm or self._fallback_call_llm()
        if call_llm is None:
            return {"status": "decision_failed", "action": "none", "error": "llm_unavailable"}
        from wander_manager.social_feed_handler import BrowseSocialFeedHandler

        source_key = self._stable_source_key(parameters)
        run_id = str(parameters.get("run_id") or "chat")
        activity_id = str(parameters.get("activity_id") or ("chat-" + source_key[:24]))
        node_id = str(parameters.get("node_id") or ("social-" + source_key[:24]))
        handler = BrowseSocialFeedHandler(
            call_llm,
            store=self._get_store(),
            on_notification=self.on_notification,
            notification_read_source="tool",
        )
        return await handler.visit_once(
            activity_reason=str(
                parameters.get("activity_reason") or parameters.get("day") or ""
            ),
            run_id=run_id,
            activity_id=activity_id,
            node_id=node_id,
        )

    async def _direct(self, parameters: dict[str, Any], action: str) -> dict[str, Any]:
        if parameters.get('read_scope')=='all':
            return {'status':'invalid_request','error':'all_scope_is_read_only','action':action}
        site_id = str(parameters.get('site_id') or '').strip()
        site = None
        store = self._get_store()
        if site_id:
            from social_feed.remote_sites import RemoteSiteError, get_remote_site_store
            from social_feed.remote_visit_store import RemoteSocialVisitStore
            site_store = get_remote_site_store()
            try:
                site = site_store.get(site_id)
            except RemoteSiteError:
                named = [candidate for candidate in site_store.list() if candidate.name == site_id]
                if len(named) != 1:
                    return {'status': 'site_unavailable', 'action': action,
                            'error': 'social_site_not_found_or_ambiguous'}
                site = named[0]
            site_id = site.id
            if not site.enabled or not site.ai_key:
                return {'status': 'site_unavailable', 'action': action, 'error': 'social_site_identity_unavailable'}
            store = RemoteSocialVisitStore(site)
            await store.me()
        source_key = self._stable_source_key(parameters)
        moment_id = str(parameters.get("moment_id") or "").strip()
        content = str(parameters.get("content") or "").strip()
        from social_feed.mentions import normalise
        try:
            mentions = normalise(parameters.get('mention_actor_ids'))
        except (TypeError, ValueError):
            return {'status': 'invalid_mentions', 'action': action, 'error': 'mention_actor_ids_invalid'}
        if action == "nickname":
            nickname = str(parameters.get("nickname") or "").strip()
            if not nickname:
                return {"status": "invalid_name", "action": action, "error": "nickname_required"}
            if site:
                saved = await store.set_nickname(nickname)
                nickname = str((saved.get('actor') or {}).get('nickname') or nickname)
            else:
                from social_feed.public_wall import get_public_wall
                nickname = get_public_wall().set_profile('k', nickname=nickname)['nickname']
            return {"status": "success", "action": action, "nickname": nickname,
                    'site_id': site_id, 'site_name': site.name if site else '本家'}
        if action == 'forward':
            from social_feed.forward_action import forward_moment
            if not moment_id:
                return {'status':'invalid_target','action':action,'error':'moment_id_required'}
            created = await forward_moment(actor='k', moment_id=moment_id,
                source_site_id=str(parameters.get('source_site_id') or ''), site_id=site_id,
                content=content, visibility=str(parameters.get('visibility') or ('public' if site else 'private')),
                source_key=source_key, local_store=self._get_store())
            return {'status':'success','action':action,'moment_id':created['id'],
                    'site_id':site_id,'site_name':site.name if site else '本家',
                    'frontend_notified':await self._notify(created['id'],'forward') if not site else False}
        if action == "post":
            visibility = str(parameters.get("visibility") or "private").strip().lower()
            if visibility not in {"private", "public"}:
                visibility = "private"
            if site and visibility != 'public':
                return {'status': 'remote_public_only', 'action': action, 'error': 'remote_social_public_only'}
            created = await store.create_moment(
                "k", content,
                source_run_id=str(parameters.get("run_id") or ""),
                source_activity_id=str(parameters.get("activity_id") or ""),
                source_key=source_key,
                visibility=visibility,
                **({'mention_actor_ids': mentions} if mentions else {}),
            )
            action_result = {
                "status": "success", "action": "post", "moment_id": created["id"],
                "visibility": created.get("visibility", visibility),
                "mention_actor_ids": created.get('mention_actor_ids', mentions),
                'site_id': site_id, 'site_name': site.name if site else '本家',
            }
            action_result["idempotent_replay"] = bool(created.get("idempotent_replay"))
            action_result["frontend_notified"] = await self._notify(created["id"], "post") if not site else False
            return action_result
        if site and action in {'delete', 'set_visibility'}:
            return {'status': 'remote_action_unavailable', 'action': action,
                    'error': '在别家的共域暂不支持直接删除或改可见性'}
        if action == "delete":
            if not moment_id:
                return {"status": "invalid_target", "action": action, "error": "moment_id_required"}
            deleted = await store.delete_moment(
                moment_id, "k", source_key=source_key
            )
            deleted["frontend_notified"] = (
                await self._notify(source_key, "delete") if deleted.get("deleted") else False
            )
            return deleted
        if action == "set_visibility":
            if not moment_id:
                return {"status": "invalid_target", "action": action, "error": "moment_id_required"}
            changed = await store.set_moment_visibility(
                moment_id,
                "k",
                str(parameters.get("visibility") or "").strip().lower(),
                allow_owner_override=False,
                source_key=source_key,
            )
            changed["frontend_notified"] = (
                await self._notify(source_key, "set_visibility") if changed.get("changed") else False
            )
            return changed
        if not moment_id:
            return {"status": "invalid_target", "action": action, "error": "moment_id_required"}
        if not await store.get_moment(moment_id):
            return {"status": "invalid_target", "action": action, "error": "moment_not_found"}
        if action == "comment":
            created = await store.add_comment(
                moment_id, "k", content,
                reply_to_id=str(parameters.get("reply_to_id") or "") or None,
                source_key=source_key,
                **({'mention_actor_ids': mentions} if mentions else {}),
            )
            action_result = {
                "status": "success", "action": "comment", "moment_id": moment_id,
                "comment_id": created["id"],
                "mention_actor_ids": created.get('mention_actor_ids', mentions),
                "idempotent_replay": bool(created.get("idempotent_replay")),
                'site_id': site_id, 'site_name': site.name if site else '本家',
            }
            action_result["frontend_notified"] = await self._notify(created["id"], "comment") if not site else False
            return action_result
        liked = await store.ensure_like(moment_id, "k", source_key=source_key)
        action_result = {
            "status": "success", "action": "like", "moment_id": moment_id,
            "idempotent_replay": bool(liked.get("idempotent_replay")),
            'site_id': site_id, 'site_name': site.name if site else '本家',
        }
        # A repeated ensure_like is still the same prior side effect and the
        # idempotent UI notification may be safely re-checked.  An already
        # existing like from another call is not a new AI write.
        if not site and (not liked.get("existing") or liked.get("idempotent_replay")):
            source_id = str(liked.get("notification_id") or moment_id)
            action_result["frontend_notified"] = await self._notify(source_id, "like")
        return action_result

    async def execute(self, **kwargs) -> ToolResult:
        parameters = dict(kwargs)
        action = str(parameters.get("action") or "review").strip().lower()
        if action not in {"review", "post", "comment", "like", "forward", "delete", "set_visibility", "nickname"}:
            return ToolResult(
                status=ToolStatus.ERROR, content="", error="action_invalid", delivery="ui_only"
            )
        try:
            result = await (
                self._review(parameters) if action == "review"
                else self._direct(parameters, action)
            )
        except Exception as exc:
            logger.warning("manage_social_feed failed: %s", type(exc).__name__, exc_info=True)
            return ToolResult(
                status=ToolStatus.ERROR, content="", error=str(exc),
                extra_data={"social_feed": {"status": "error", "action": action}},
                delivery="ui_only",
            )
        evidence = self._evidence(result, action=action)
        status = str(result.get("status") or "success")
        failed = status not in {"success"}
        return ToolResult(
            status=ToolStatus.ERROR if failed else ToolStatus.SUCCESS,
            content="共域操作未完成" if failed else "共域操作已完成",
            error=str(result.get("error") or "") if failed else None,
            extra_data={"social_feed": evidence},
            delivery="ui_only",
        )


__all__ = ["ManageSocialFeedTool"]
