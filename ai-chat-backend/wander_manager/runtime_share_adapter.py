"""Flash-model outward sharing for a settled Wander activity."""

from __future__ import annotations

import inspect
import json
import asyncio
import logging
from body_admission import ordinary
from typing import Any, Awaitable, Callable, Optional

from context_builder.proactive_output import clean_proactive_output as clean_visible_reply

from .event_catalog import strategy_for
from .event_types import EventType
from .runtime_models import DecisionPhase, GoalMode, NodeState
from .runtime_store import WanderRuntimeStore

logger = logging.getLogger(__name__)


class RuntimeShareAdapter:
    def __init__(
        self,
        store: WanderRuntimeStore,
        share_llm: Callable[[list[dict[str, str]]], Awaitable[Any]],
        push_callback: Callable[[dict[str, Any]], Any],
        context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
        should_suppress: Optional[Callable[[], bool]] = None,
        band_touch_tool: Any = None,
        schedule_checkup_enabled: bool = False,
        activity_event_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
        expression_enabled: bool = False,
    ):
        self.store = store
        self.share_llm = share_llm
        self.push_callback = push_callback
        self.context_builder = context_builder
        self.should_suppress = should_suppress
        self.band_touch_tool = band_touch_tool
        self.schedule_checkup_enabled = schedule_checkup_enabled
        self.activity_event_callback = activity_event_callback
        self.expression_enabled = expression_enabled
        self._activity_event_lock = asyncio.Lock()

    async def record_activity(self, activity_id: str) -> bool:
        """Project a settled activity into the conversation timeline, shared or not."""
        async with self._activity_event_lock:
            return await self._record_activity(activity_id)

    async def _record_activity(self, activity_id: str) -> bool:
        if self.activity_event_callback is None:
            return False
        activity = self.store.get_activity(activity_id)
        if not activity or activity.get("state") not in {"completed", "aborted", "interrupted"}:
            return False
        run = self.store.get_run(activity["run_id"])
        if not run or run.get("trigger_reason") == "chat_tool":
            return False
        event_type = EventType(activity["activity_type"])
        self.prepare_activity_event(activity_id)
        projection = self.store.get_activity_event_projection(activity_id)
        if projection and projection["status"] == "sent":
            return True
        try:
            retained = self.store.get_settlement_impression(activity_id)
            event_activity = {**activity, "retained_impression": retained["impression"],
                              "retained_node_ids": retained["source_node_ids"],
                              "closeout_version": retained.get('closeout_version'),
                              "purpose_outcome": retained.get('purpose_outcome')}
            payload = {
                "activity_id": activity_id,
                "run_id": activity["run_id"],
                "session_id": run["session_id"],
                "occurred_at": activity.get("ended_at") or activity.get("updated_at"),
                "active_date": projection["active_date"] if projection else "",
                "summary": str(activity.get("summary") or "").strip(),
                "tool_calls": self._tool_calls(event_type, event_activity, self.store.list_nodes(activity_id)),
            }
            essay_index = (payload['tool_calls'][0].get('extra_data') or {}).get('essay_index')
            if essay_index is not None:
                from .essays import index_text
                payload['summary'] = index_text(essay_index) or '本次随笔尚未写成。'
            result = self.activity_event_callback(payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.store.finish_activity_event(activity_id, success=False, error=type(exc).__name__)
            logger.exception("漫想事件卡写入失败，保留待补写记录: %s", activity_id)
            return False
        self.store.finish_activity_event(activity_id, success=result is True,
                                         error="" if result is True else "event_commit_unconfirmed")
        return result is True

    def prepare_activity_event(self, activity_id: str) -> None:
        if self.activity_event_callback is None or self.store.get_activity_event_projection(activity_id):
            return
        activity = self.store.get_activity(activity_id)
        if not activity:
            return
        run = self.store.get_run(activity["run_id"])
        if not run or run.get("trigger_reason") == "chat_tool":
            return
        from conversation_manager_v2 import resolve_active_date
        self.store.prepare_activity_event(activity_id, resolve_active_date(run["session_id"]))

    async def retry_activity_events(self, *, now=None) -> None:
        if self.activity_event_callback is not None:
            for activity_id in self.store.pending_activity_events(now=now):
                try:
                    await self.record_activity(activity_id)
                except Exception as exc:
                    # One malformed activity must not starve later local cards.
                    self.store.finish_activity_event(activity_id, success=False, error=type(exc).__name__)
                    logger.exception("漫想事件卡恢复失败，继续处理其他记录: %s", activity_id)

    def _suppress_delivery(self, delivery_id: str) -> bool:
        from music_system.context import quiet_active
        music_quiet = quiet_active()
        if self.should_suppress is None:
            from shared_state import should_suppress_proactive
            suppressed = should_suppress_proactive()
        else:
            suppressed = self.should_suppress()
        suppressed = suppressed or music_quiet
        if suppressed:
            # Keep the activity's real sharing intent without queuing stale
            # speech for the moment DND is turned off.
            self.store.update_delivery(delivery_id, status="suppressed")
        return bool(suppressed)

    @ordinary
    async def deliver(self, activity_id: str) -> bool:
        activity = self.store.get_activity(activity_id)
        if activity is None or activity["state"] not in {"completed", "aborted"}:
            raise ValueError("sharing requires a terminal activity")
        if not activity.get("share_decision"):
            return False
        run = self.store.get_run(activity["run_id"])
        delivery = self.store.get_delivery_for_activity(activity_id)
        if run is None or delivery is None:
            raise ValueError("sharing identity chain is incomplete")
        if delivery["status"] == "sent":
            return True
        if delivery["status"] == "sending":
            return False
        if delivery["status"] != "deferred":
            return False
        if self._suppress_delivery(delivery["delivery_id"]):
            return False

        run_activities = [
            item for item in self.store.list_activities(run["run_id"])
            if item.get("state") in {"completed", "aborted", "interrupted"}
        ]
        nodes = self.store.list_nodes(activity_id)
        activity_sections: list[str] = []
        from .context_projection import activity_experience
        for item in run_activities:
            item_nodes = self.store.list_nodes(item["activity_id"])
            activity_sections.append(json.dumps(activity_experience(
                item, item_nodes, self.store.get_settlement_impression(item["activity_id"]),
                include_settlement=True,
            ), ensure_ascii=False))
        facts = "\n".join(activity_sections) or "- 没有已完成的节点"
        event_type = EventType(activity["activity_type"])
        label = strategy_for(event_type).display_name
        xhs_card = self._selected_xhs_card(run["run_id"])
        from .essays import selected_card, card_text
        essay_card = selected_card(self.store, run['run_id'])
        from .reading_illustrations import entry as picture_entry, fact as picture_fact
        picture = selected_card(self.store, run['run_id'], field='reading_picture_node_id', make_card=picture_entry)
        activity_text = (
            "你刚刚结束了一个连续的漫想事件段。\n"
            f"最后一项活动：{label}。\n"
            f"按实际发生顺序记录的执行回执与 AI 的主观回顾（分别存放）：\n{facts}"
        )
        if essay_card:
            activity_text += '\n本次选择随消息附上的随笔卡片：\n' + card_text(essay_card)
        if picture:
            activity_text += '\n这条消息已附上你选的' + picture_fact(picture)
        if xhs_card:
            activity_text += (f"\n本次 AI 还选择随这条消息附一张已读帖子卡片：《{xhs_card['title'] or '无标题帖子'}》；"
                              f"原帖链接状态={xhs_card['link_status']}；"
                              f"评论邀请={'有 AI 的草稿、由人类伙伴手动决定是否发表' if xhs_card['comment_draft'] else '无'}。")
        instruction = (
            "主要和人类伙伴分享这段漫想留下的感受，自然说说此刻想说的话。"
            "几项活动可以顺着经历串起来，同类经历可以合写，不必逐项汇报。"
            "刚发生的行动依据 execution_receipts；subjective_experience 是当时的感受、联想与意图，"
            "历史聊天和较早经历保留原来的时间与说话人身份。"
            "以 AI 的第一人称写一条消息，直接输出正文。"
        )
        if essay_card:
            instruction += '随笔原文已经附在小卡片里，消息聊聊此刻想说的话就好。'
        if any(item.get('activity_type') in {'visit_workshop', 'workshop_create', 'workshop_history', 'workshop_continue'} for item in run_activities):
            from .decision_contracts import WORKSHOP_FACTS
            instruction += WORKSHOP_FACTS
        if any(item.get("activity_type") == EventType.LISTEN_MUSIC.value for item in run_activities):
            from .music_receipt import MUSIC_EXPRESSION_CONTRACT
            instruction += MUSIC_EXPRESSION_CONTRACT
        if self.band_touch_tool is not None:
            from proactive_band_touch import output_instruction
            instruction += output_instruction(additional_action=self.schedule_checkup_enabled or self.expression_enabled)
        if self.schedule_checkup_enabled:
            from proactive_checkup import output_instruction as checkup_instruction
            instruction += checkup_instruction()
        try:
            if self.expression_enabled:
                from proactive_expression import registered_tools, output_instruction as expression_instruction
                instruction += expression_instruction(await registered_tools())
            messages, built = await self._messages(run, activity, activity_text, instruction,
                                                  [item["activity_id"] for item in run_activities])
            response = await self.share_llm(messages)
            if isinstance(response, dict):
                content = str(response.get("content") or "").strip()
                reasoning = str(response.get("reasoning") or "")
            else:
                content = str(response or "").strip()
                reasoning = ""
            expression_requests = []
            if self.expression_enabled:
                from proactive_expression import split_output as split_expression
                content, band_touch_request, checkup_request, expression_requests, action_parse_error = split_expression(
                    content, schedule_enabled=self.schedule_checkup_enabled,
                )
            elif self.schedule_checkup_enabled:
                from proactive_checkup import split_output
                content, band_touch_request, checkup_request, action_parse_error = split_output(content)
            else:
                content, band_touch_request, action_parse_error = self._split_share_output(content)
                checkup_request = None
            content = clean_visible_reply(content)
            if not content:
                raise ValueError("empty_pro_share")
            try:
                from context_builder import commit_context_result
                commit_context_result(built)
            except Exception:
                pass
        except Exception as exc:
            self.store.record_decision(
                run_id=run["run_id"], activity_id=activity_id,
                phase=DecisionPhase.SHARE, model="flash", recipe="WANDER_PUSH",
                input_context={"activity": activity_text, "instruction": instruction}, status="error", error=type(exc).__name__,
            )
            return False

        # Activity cards are independent timeline events.  This message only
        # carries actions actually taken while composing the optional share.
        tool_calls: list[dict[str, Any]] = []
        if picture:
            tool_calls.append({'tool': 'reading_illustration', 'status': 'success', 'delivery': 'ui_only',
                'description': '分享读后插画', 'result': picture_fact(picture),
                'extra_data': {'wander_projection_version': 2, 'reading_illustration': picture}})
        if essay_card:
            tool_calls.append({'tool': 'essay_card', 'status': 'success', 'delivery': 'ui_only',
                'description': '分享随笔《' + essay_card['title'] + '》',
                'result': '随本条消息交付，不代表人类伙伴已读。',
                'extra_data': {'wander_projection_version': 2, 'essay_card': essay_card}})
        if xhs_card:
            tool_calls.append({
                "tool": "xhs_posts", "description": "分享已读小红书帖子卡片",
                "result": "卡片随本条消息交付；不代表人类伙伴已打开或评论",
                "status": "success", "delivery": "ui_only",
                "extra_data": {"xhs_post_card": xhs_card},
            })
        message_id = f"wander_{activity_id}"
        if self._suppress_delivery(delivery["delivery_id"]):
            return False
        # Claim the one-shot delivery before touching the band.  A crash after
        # the BLE request must not leave a deferred row that can replay it.
        self.store.update_delivery(
            delivery["delivery_id"], status="sending", message_id=message_id,
            message_content=content, tool_calls=tool_calls,
        )
        band_touch_call, action_audit = await self._execute_band_touch(
            band_touch_request, action_parse_error,
        )
        if band_touch_call is not None:
            tool_calls.append(band_touch_call)
            self.store.update_delivery(
                delivery["delivery_id"], status="sending", message_id=message_id,
                message_content=content, tool_calls=tool_calls,
            )
        checkup_audit = {"requested": False, "status": "not_requested"}
        if self.schedule_checkup_enabled and not action_parse_error:
            from proactive_checkup import schedule_request
            checkup_call, checkup_audit = await schedule_request(checkup_request, session_id=run['session_id'])
            if checkup_call is not None:
                tool_calls.append(checkup_call)
                self.store.update_delivery(
                    delivery["delivery_id"], status="sending", message_id=message_id,
                    message_content=content, tool_calls=tool_calls,
                )
        elif action_parse_error:
            checkup_audit = {"requested": bool(checkup_request), "status": "rejected", "error": action_parse_error}
        self.store.record_decision(
            run_id=run["run_id"], activity_id=activity_id,
            phase=DecisionPhase.SHARE, model="flash", recipe="WANDER_PUSH",
            input_context={"context": messages[:-1], "instruction": instruction},
            raw_output=content, reasoning=reasoning,
            parsed_output={"message": content, "tool_calls": tool_calls,
                           "band_touch": action_audit,
                           "scheduled_checkup": checkup_audit}, status="ok",
        )
        payload = {
            "message": content,
            "reasoning": reasoning,
            "tool_calls": tool_calls,
            "event_type": event_type.value,
            "event_id": activity_id,
            "session_id": run["session_id"],
        }
        if event_type == EventType.MEMORY_FETCH:
            memory_node = next((node for node in reversed(nodes) if node["source_payload"].get("memory_id")), None)
            if memory_node:
                payload["memory_id"] = memory_node["source_payload"]["memory_id"]
        from proactive_expression import ExpressionDelivery
        expression_delivery = ExpressionDelivery()
        payload["_expression_delivery"] = expression_delivery
        delivered = False
        try:
            tool_calls.extend(await expression_delivery.prepare(expression_requests, run["session_id"]))
            self.store.update_delivery(
                delivery["delivery_id"], status="sending", message_id=message_id,
                message_content=content, tool_calls=tool_calls,
            )
            result = self.push_callback(payload)
            if inspect.isawaitable(result):
                result = await result
            # A host acknowledges its authoritative message commit explicitly.
            # Returning None/False is not evidence that anything was saved.
            if result is not True:
                raise RuntimeError("push_not_acknowledged")
            delivered = True
        except Exception as exc:
            self.store.update_delivery(delivery["delivery_id"], status="failed")
            self.store.record_decision(
                run_id=run["run_id"], activity_id=activity_id,
                phase=DecisionPhase.SHARE, model="", recipe="WANDER_PUSH",
                input_context={"delivery_id": delivery["delivery_id"]},
                status="delivery_error", error=type(exc).__name__,
            )
            return False
        except asyncio.CancelledError:
            self.store.update_delivery(delivery["delivery_id"],
                                       status="sent" if expression_delivery.gate.accepted else "failed")
            raise
        finally:
            expression_delivery.finish(delivered)
        self.store.update_delivery(delivery["delivery_id"], status="sent")
        return True

    def _selected_xhs_card(self, run_id: str) -> Optional[dict[str, Any]]:
        """Project one explicit settled choice; never infer it from a title."""
        activities = {str(row["activity_id"]): row for row in self.store.list_activities(run_id)}
        for decision in reversed(self.store.list_decisions(run_id, DecisionPhase.SETTLEMENT)):
            activity = activities.get(str(decision.get("activity_id") or ""))
            if not activity or activity.get("activity_type") != EventType.BROWSE_XIAOHONGSHU.value:
                continue
            if activity.get("state") != "completed" or not activity.get("share_decision"):
                continue
            normalized = (decision.get("parsed_output") or {}).get("normalized") or {}
            node_id = str(normalized.get("xhs_share_node_id") or "")
            if not node_id:
                continue
            if not any(node.get("node_id") == node_id and node.get("state") == NodeState.COMPLETED.value
                       for node in self.store.list_nodes(activity["activity_id"])):
                continue
            try:
                from xhs_posts.cards import k_card_from_node
                from xhs_posts.store import XhsPostStore
                return k_card_from_node(
                    XhsPostStore(self.store.db_path), node_id,
                    comment_draft=str(normalized.get("xhs_comment_draft") or ""),
                    require_invite_link=False,
                )
            except Exception:
                return None
        return None

    @staticmethod
    def _split_share_output(content: str) -> tuple[str, Optional[dict[str, Any]], str]:
        """Separate one strict, allowlisted action from the visible message."""
        from proactive_band_touch import split_output
        return split_output(content)

    async def _execute_band_touch(
        self,
        request: Optional[dict[str, Any]],
        parse_error: str,
    ) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
        from proactive_band_touch import execute_request
        return await execute_request(self.band_touch_tool, request, parse_error)

    async def _messages(
        self, run: dict, activity: dict, activity_text: str, instruction: str,
        covered_activity_ids: list[str],
    ) -> tuple[list[dict[str, str]], Any]:
        from shared_state import get_latest_persona_prompt
        from context_builder.ingredients import build_wander_mode_text

        memory_query = activity_text[:1200]

        async def _load_related_memory() -> list:
            from context_builder.ingredients import retrieve_memory_v2
            from context_scheduler_mcp.context_scheduler import QueryPlan

            return await retrieve_memory_v2(QueryPlan(
                original_query=memory_query,
                enhanced_query=memory_query,
                target_buckets=[],
                retrieval_strength=0.3,
                retrieval_method="memory_v2",
                max_results=3,
                similarity_threshold=0.60,
            ))

        run_activity_ids = covered_activity_ids
        kwargs = {
            "persona": get_latest_persona_prompt() or "",
            "session_id": run["session_id"],
            "mood": "",
            "wander_mode_text": build_wander_mode_text("push"),
            # ``wander_activity`` is the current activity.  Passing its
            # summary as ``recent_wander`` made Builder repeat the same event
            # in the anti-duplication section; let Builder query prior rows
            # and exclude this stable activity id instead.
            "recent_wander": "",
            "exclude_wander_history": True,
            "current_activity_id": str(activity.get("activity_id") or ""),
            "exclude_wander_activity_ids": run_activity_ids,
            "exclude_memory_message_ids": [f"activity_event_{activity_id}" for activity_id in run_activity_ids],
            "wander_activity_text": activity_text,
            "context_query": activity_text,
            "memory_query": memory_query,
            "legacy_memory_loader": _load_related_memory,
        }
        if self.context_builder is not None:
            built = await self.context_builder("WANDER_PUSH", **kwargs)
        else:
            from context_builder.builder import ContextBuilder
            built = await ContextBuilder.build("WANDER_PUSH", **kwargs)
        from context_builder.assembly import assemble_context_result
        messages = assemble_context_result(built)
        messages.append({"role": "system", "content": instruction})
        return messages, built

    @staticmethod
    def _tool_calls(event_type: EventType, activity: dict, nodes: list[dict]) -> list[dict[str, Any]]:
        names = {
            EventType.VISIT_WORKSHOP: "🛠️ 去小机工作坊",
            EventType.WORKSHOP_CREATE: "🛠️ 工作坊·酝酿新想法",
            EventType.WORKSHOP_HISTORY: "🖼️ 工作坊·欣赏历史作品",
            EventType.WORKSHOP_CONTINUE: "🛠️ 工作坊·继续未完成工单",
            EventType.KEYWORD_EXPANSION: "✍️ 随笔",
            EventType.MEMORY_FETCH: "💭 回看记忆",
            EventType.BROWSE_NEWS: "💭 看新闻",
            EventType.BROWSE_XIAOHONGSHU: "💭 刷小红书",
            EventType.PUBLISH_XIAOHONGSHU: "✍️ 写小红书笔记",
            EventType.XHS_DIRECT_MESSAGES: "💬 查看小红书私信",
            EventType.CHECK_XIAOHONGSHU_INTERACTIONS: "💬 查看小红书评论与艾特",
            EventType.BROWSE_SOCIAL_FEED: "💌 打开了共域",
            EventType.BROWSE_BOOKMARKS: "📚 逛书柜",
            EventType.SELF_REFLECTION: "💭 自省",
            EventType.LISTEN_MUSIC: "💭 自己听歌",
            EventType.PLAY_GAME: "🎮 玩游戏",
            EventType.SLEEP: "💭 休息",
            EventType.USER_TRACKING: "💭 查岗",
        }
        strategy = strategy_for(event_type)
        label = strategy.display_name
        count = len([node for node in nodes if node.get("state") == NodeState.COMPLETED.value
                     and node.get("execution_status") == "succeeded"])
        goal_mode = activity.get("goal_mode")
        terminal_state = str(activity.get("state") or "")
        timer_completed = (event_type == EventType.SLEEP and activity.get('timer_ends_at')
                           and activity.get('settlement_reason') == 'timer_ended' and terminal_state == 'completed')
        no_new_interactions = (event_type == EventType.CHECK_XIAOHONGSHU_INTERACTIONS and bool(nodes)
            and all(node.get('execution_status') == 'skipped'
                    and (node.get('source_payload') or {}).get('status') == 'no_pending_interactions'
                    for node in nodes))
        failed = any(node.get('execution_status') == 'failed' for node in nodes)
        if timer_completed:
            progress = '休息计时结束'
        elif no_new_interactions:
            progress = '没有新评论或艾特'
        elif count == 0:
            progress = ('这次没能完成' if failed else '中途停止'
                        if terminal_state == 'interrupted' else '这次已结束，没有完成的操作')
        elif count > 0 and strategy.node_unit:
            progress = f"完成了 {count} {strategy.node_unit}"
        elif count > 0:
            progress = f"完成了 {count} 步"
        elif goal_mode == GoalMode.DURATION.value and activity.get("goal_value"):
            progress = f"{activity['goal_value']} {strategy.node_unit or '分钟'}"
        else:
            progress = "完成了一次"
        if event_type == EventType.LISTEN_MUSIC and count:
            from .music_receipt import music_execution
            kinds = [music_execution(node)["experience_kind"] for node in nodes
                     if node.get("state") == NodeState.COMPLETED.value and node.get("execution_status") == "succeeded"]
            if all(kind == 'listening_completed' for kind in kinds):
                progress = f"听完 {count} 首歌"
            elif all(kind == "material_analysis" for kind in kinds):
                progress = f"完成了 {count} 首歌曲资料分析"
            elif all(kind == "playback_completed" for kind in kinds):
                progress = f"播放完成 {count} 首"
            else:
                progress = f"处理了 {count} 首歌曲材料（播放情况见回执）"
        # Do not surface the bounded visit's internal action (review/post/
        # comment/like) in the outer share mount.  It remains in the private
        # feed evidence and is not a second chat message.
        if count and terminal_state in {"aborted", "interrupted"}:
            progress += "，随后中断"
        description = (
            "打开了共域" if event_type == EventType.BROWSE_SOCIAL_FEED and count
            else f"{label} · {progress}"
        )
        if event_type == EventType.BROWSE_XIAOHONGSHU:
            kept = sum(1 for node in nodes if
                       isinstance((node.get("side_effect_refs") or {}).get("xhs_post_action"), dict)
                       and (node.get("side_effect_refs") or {}).get("xhs_post_action", {}).get("status") == "completed"
                       and (node.get("side_effect_refs") or {}).get("xhs_post_action", {}).get("changed"))
            if kept:
                description += f" · MIRROW 留帖 {kept} 篇"
        summary = str(activity.get("summary") or "").strip()
        if no_new_interactions:
            summary = '没有新评论或艾特，本次查看已结束。'
        if event_type == EventType.XHS_DIRECT_MESSAGES:
            reports = [str((node.get('source_payload') or {}).get('summary') or '') for node in nodes
                       if node.get('execution_status') == 'succeeded']
            if any(reports):
                summary = '\n'.join(dict.fromkeys(report for report in reports if report))
        status = ("success" if terminal_state == "completed" and (count > 0 or timer_completed)
                  else "partial" if count > 0 else "failure" if failed else "skipped")
        call: dict[str, Any] = {"tool": names[event_type], "description": description[:60],
                                "result": summary, "status": status}
        call["extra_data"] = {"retained_experience": {
            "impression": str(activity.get("retained_impression") or ""),
            "source_node_ids": list(activity.get("retained_node_ids") or []),
        }}
        from .context_projection import execution_overview
        call["extra_data"]["wander_projection_version"] = 2
        call["extra_data"]["execution_overview"] = execution_overview(activity, nodes)
        if terminal_state == 'interrupted':
            call['extra_data']['interrupted_review_version'] = 1
        effect = activity.get("emotion_effect")
        if isinstance(effect, str):
            try:
                effect = json.loads(effect)
            except (TypeError, ValueError):
                effect = {}
        if isinstance(effect, dict) and effect:
            call["extra_data"]["emotion_effect"] = effect
        from memory_v2.experience_contract import snapshot
        call["extra_data"]["memory_experience"] = snapshot(activity, nodes)
        if 'essay_index' in call['extra_data']['memory_experience']:
            from .essays import index_text
            items = call['extra_data']['memory_experience']['essay_index']
            call['extra_data']['essay_index'] = items
            call['extra_data']['retained_experience'] = {'impression': '', 'source_node_ids': []}
            call['result'] = index_text(items) or '本次随笔尚未写成。'
        if event_type == EventType.USER_TRACKING and any(
                item.get('kind') == 'checkup' for item in call['extra_data']['memory_experience']['materials']):
            from memory_v2.experience_contract import project
            # The existing expandable activity UI renders this complete text;
            # no separate notification card or frontend data fetch is needed.
            call['result'] = project(call['extra_data']['memory_experience']) or summary
        if event_type == EventType.PLAY_GAME:
            from .game_experience import chapter_facts
            from .game_results import completed, result_facts
            from .cedartoy_client import GAMES

            played = [node.get("source_payload") for node in nodes
                      if isinstance(node.get("source_payload"), dict)
                      and node["source_payload"].get("game") in GAMES]
            if played:
                latest = played[-1]
                call.setdefault("extra_data", {})["game_summary"] = {
                    "game": latest["game"], "game_name": latest.get("game_name") or "",
                    "slot": latest.get("slot"),
                    "playthrough_key": latest.get("playthrough_key") or "",
                    "completed": any(completed(item) for item in played),
                    "steps": len(played),
                    "chapters": chapter_facts(nodes),
                    "results": result_facts(nodes),
                }
        return [call]
