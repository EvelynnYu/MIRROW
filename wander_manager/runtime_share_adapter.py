"""Flash-model outward sharing for a settled Wander activity."""

from __future__ import annotations

import inspect
from .host_hooks import ordinary
from typing import Any, Awaitable, Callable, Optional

from context_builder.proactive_output import clean_proactive_output

from .event_catalog import strategy_for
from .event_types import EventType
from .runtime_models import DecisionPhase, GoalMode, NodeState
from .runtime_store import WanderRuntimeStore


class RuntimeShareAdapter:
    def __init__(
        self,
        store: WanderRuntimeStore,
        share_llm: Callable[[list[dict[str, str]]], Awaitable[Any]],
        push_callback: Callable[[dict[str, Any]], Any],
        context_builder: Optional[Callable[..., Awaitable[Any]]] = None,
        should_suppress: Optional[Callable[[], bool]] = None,
    ):
        self.store = store
        self.share_llm = share_llm
        self.push_callback = push_callback
        self.context_builder = context_builder
        self.should_suppress = should_suppress

    def _suppress_delivery(self, delivery_id: str) -> bool:
        if self.should_suppress is None:
            from mirrow_core.shared_state import should_suppress_proactive
            suppressed = should_suppress_proactive()
        else:
            suppressed = self.should_suppress()
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
            if item.get("state") in {"completed", "aborted"}
            and self.store.list_nodes(str(item.get("activity_id") or ""))
        ]
        nodes = self.store.list_nodes(activity_id)
        if activity['activity_type'] in {EventType.BROWSE_TAOBAO.value, EventType.VISIT_LOUNGE.value}:
            # The transplanted outing already delivers its verified product card.
            receipt = next((n.get('source_payload', {}).get('notification_id') for n in nodes
                            if n.get('source_payload', {}).get('notification_id')), '')
            if receipt:
                self.store.update_delivery(delivery['delivery_id'], status='sent', message_id=receipt)
                return True
        activity_sections: list[str] = []
        for item in run_activities:
            item_nodes = self.store.list_nodes(item["activity_id"])
            item_label = strategy_for(EventType(item["activity_type"])).display_name
            item_facts = "\n".join(
                f"  - {node['source_summary']}；感想：{node.get('reflection') or '没有感想'}"
                for node in item_nodes if node["state"] == "completed"
            ) or "  - 没有已完成的节点"
            activity_sections.append(
                f"- {item_label}：{item.get('summary') or '没有额外总结'}\n{item_facts}"
            )
        facts = "\n".join(activity_sections) or "- 没有已完成的节点"
        event_type = EventType(activity["activity_type"])
        label = strategy_for(event_type).display_name
        activity_text = (
            "你刚刚结束了一个连续的漫想事件段。\n"
            f"最后一项活动：{label}。\n"
            f"按实际发生顺序记录的活动、综合感受与节点感想：\n{facts}"
        )
        instruction = (
            "你已经决定把刚刚整个漫想事件段产生的真实感想分享给用户——这是这条消息唯一要做的事。"
            "若事件段有多项活动，把前一项带来的感想与后一项的行动自然串联成一条消息；不要拆成逐项汇报。"
            "紧扣上面按顺序发生的实际内容和你的真实感受来写，不要发散到无关话题。"
            "只能使用上面提供的真实记录里的信息（活动完成记录、对话、日记、当前状态）。"
            "历史对话只用于理解连续关系；凡是说自己刚刚做了、看见了或想到的内容，只能来自本事件段记录。"
            "严禁编造或补脑上下文之外的事实：具体日期、生日/纪念日/节日、身体状态、"
            "用户说过的话、发生过的其他事，也不得夸大已有事实。"
            "以AI的第一人称自然发一条消息，直接输出消息正文。"
        )
        try:
            messages, built = await self._messages(run, activity, activity_text, instruction)
            response = await self.share_llm(messages)
            if isinstance(response, dict):
                content = str(response.get("content") or "").strip()
                reasoning = str(response.get("reasoning") or "")
            else:
                content = str(response or "").strip()
                reasoning = ""
            content = clean_proactive_output(content)
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

        tool_calls: list[dict[str, str]] = []
        for item in run_activities or [activity]:
            item_event_type = EventType(item["activity_type"])
            tool_calls.extend(self._tool_calls(
                item_event_type, item, self.store.list_nodes(item["activity_id"]),
            ))
        self.store.record_decision(
            run_id=run["run_id"], activity_id=activity_id,
            phase=DecisionPhase.SHARE, model="flash", recipe="WANDER_PUSH",
            input_context={"context": messages[:-1], "instruction": instruction},
            raw_output=content, reasoning=reasoning,
            parsed_output={"message": content, "tool_calls": tool_calls}, status="ok",
        )
        message_id = f"wander_{activity_id}"
        if self._suppress_delivery(delivery["delivery_id"]):
            return False
        self.store.update_delivery(
            delivery["delivery_id"], status="sending", message_id=message_id,
            message_content=content, tool_calls=tool_calls,
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
        try:
            result = self.push_callback(payload)
            if inspect.isawaitable(result):
                result = await result
            # A host acknowledges its authoritative message commit explicitly.
            # Returning None/False is not evidence that anything was saved.
            if result is not True:
                raise RuntimeError("push_not_acknowledged")
        except Exception as exc:
            self.store.update_delivery(delivery["delivery_id"], status="failed")
            self.store.record_decision(
                run_id=run["run_id"], activity_id=activity_id,
                phase=DecisionPhase.SHARE, model="", recipe="WANDER_PUSH",
                input_context={"delivery_id": delivery["delivery_id"]},
                status="delivery_error", error=type(exc).__name__,
            )
            return False
        self.store.update_delivery(delivery["delivery_id"], status="sent")
        return True

    async def _messages(
        self, run: dict, activity: dict, activity_text: str, instruction: str,
    ) -> tuple[list[dict[str, str]], Any]:
        from mirrow_core.shared_state import get_latest_persona_prompt
        from context_builder.ingredients import build_wander_mode_text

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
            "wander_activity_text": activity_text,
            "context_query": activity_text,
        }
        if self.context_builder is not None:
            built = await self.context_builder("WANDER_PUSH", **kwargs)
        else:
            from context_builder.builder import ContextBuilder
            built = await ContextBuilder.build("WANDER_PUSH", **kwargs)
        messages = list(getattr(built, "formatted_messages", []) or [])
        if not messages and getattr(built, "system_content", ""):
            messages.append({"role": "system", "content": built.system_content})
        messages.append({"role": "system", "content": instruction})
        return messages, built

    @staticmethod
    def _tool_calls(event_type: EventType, activity: dict, nodes: list[dict]) -> list[dict[str, str]]:
        names = {
            EventType.VISIT_LOUNGE: "💭 好友串门",
            EventType.BROWSE_TAOBAO: "🛍️ 逛淘宝",
            EventType.KEYWORD_EXPANSION: "💭 关键词联想",
            EventType.MEMORY_FETCH: "💭 回看记忆",
            EventType.BROWSE_NEWS: "💭 看新闻",
            EventType.BROWSE_XIAOHONGSHU: "💭 刷小红书",
            EventType.BROWSE_SOCIAL_FEED: "💌 打开了朋友圈",
            EventType.BROWSE_BOOKMARKS: "💭 翻收藏夹",
            EventType.SELF_REFLECTION: "💭 自省",
            EventType.LISTEN_MUSIC: "💭 自己听歌",
            EventType.SLEEP: "💭 休眠",
            EventType.USER_TRACKING: "💭 查岗",
            EventType.HOST_GROUP_ACTIVITY: "💭 群组活动",
        }
        strategy = strategy_for(event_type)
        label = strategy.display_name
        count = len([node for node in nodes if node.get("state") == NodeState.COMPLETED.value])
        goal_mode = activity.get("goal_mode")
        if count > 0 and strategy.node_unit:
            progress = f"完成了 {count} {strategy.node_unit}"
        elif count > 0:
            progress = f"完成了 {count} 个节点"
        elif goal_mode == GoalMode.DURATION.value and activity.get("goal_value"):
            progress = f"{activity['goal_value']} {strategy.node_unit or '分钟'}"
        else:
            progress = "完成了一次"
        # Do not surface the bounded visit's internal action (review/post/
        # comment/like) in the outer share mount.  It remains in the private
        # feed evidence and is not a second chat message.
        description = "打开了朋友圈" if event_type == EventType.BROWSE_SOCIAL_FEED else f"{label} · {progress}"
        summary = "" if event_type == EventType.BROWSE_SOCIAL_FEED else str(activity.get("summary") or "").strip()
        return [{"tool": names[event_type], "description": description[:60], "result": summary}]
