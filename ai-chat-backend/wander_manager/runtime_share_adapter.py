"""Flash-model outward sharing for a settled Wander activity."""
from __future__ import annotations
import inspect
import json
import asyncio
import logging
from body_admission import ordinary
from typing import Any, Awaitable, Callable, Optional
from context_builder.proactive_output import clean_proactive_output
from .event_catalog import strategy_for
from .event_types import EventType
from .runtime_models import DecisionPhase, GoalMode, NodeState
from .runtime_store import WanderRuntimeStore
logger = logging.getLogger(__name__)

class RuntimeShareAdapter:

    def __init__(self, store: WanderRuntimeStore, share_llm: Callable[[list[dict[str, str]]], Awaitable[Any]], push_callback: Callable[[dict[str, Any]], Any], context_builder: Optional[Callable[..., Awaitable[Any]]]=None, should_suppress: Optional[Callable[[], bool]]=None, band_touch_tool: Any=None, schedule_checkup_enabled: bool=False, activity_event_callback: Optional[Callable[[dict[str, Any]], Any]]=None, expression_enabled: bool=False):
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
        if not activity or activity.get('state') not in {'completed', 'aborted', 'interrupted'}:
            return False
        run = self.store.get_run(activity['run_id'])
        if not run or run.get('trigger_reason') == 'chat_tool':
            return False
        event_type = EventType(activity['activity_type'])
        self.prepare_activity_event(activity_id)
        projection = self.store.get_activity_event_projection(activity_id)
        if projection and projection['status'] == 'sent':
            return True
        try:
            retained = self.store.get_settlement_impression(activity_id)
            event_activity = {**activity, 'retained_impression': retained['impression'], 'retained_node_ids': retained['source_node_ids']}
            payload = {'activity_id': activity_id, 'run_id': activity['run_id'], 'session_id': run['session_id'], 'occurred_at': activity.get('ended_at') or activity.get('updated_at'), 'active_date': projection['active_date'] if projection else '', 'summary': str(activity.get('summary') or '').strip(), 'tool_calls': self._tool_calls(event_type, event_activity, self.store.list_nodes(activity_id))}
            result = self.activity_event_callback(payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.store.finish_activity_event(activity_id, success=False, error=type(exc).__name__)
            logger.exception('漫想事件卡写入失败，保留待补写记录: %s', activity_id)
            return False
        self.store.finish_activity_event(activity_id, success=result is True, error='' if result is True else 'event_commit_unconfirmed')
        return result is True

    def prepare_activity_event(self, activity_id: str) -> None:
        if self.activity_event_callback is None or self.store.get_activity_event_projection(activity_id):
            return
        activity = self.store.get_activity(activity_id)
        run = self.store.get_run(activity['run_id'])
        if not run or run.get('trigger_reason') == 'chat_tool':
            return
        from conversation_manager_v2 import resolve_active_date
        self.store.prepare_activity_event(activity_id, resolve_active_date(run['session_id']))

    async def retry_activity_events(self, *, now=None) -> None:
        if self.activity_event_callback is not None:
            for activity_id in self.store.pending_activity_events(now=now):
                try:
                    await self.record_activity(activity_id)
                except Exception as exc:
                    self.store.finish_activity_event(activity_id, success=False, error=type(exc).__name__)
                    logger.exception('漫想事件卡恢复失败，继续处理其他记录: %s', activity_id)

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
            self.store.update_delivery(delivery_id, status='suppressed')
        return bool(suppressed)

    @ordinary
    async def deliver(self, activity_id: str) -> bool:
        activity = self.store.get_activity(activity_id)
        if activity is None or activity['state'] not in {'completed', 'aborted'}:
            raise ValueError('sharing requires a terminal activity')
        if not activity.get('share_decision'):
            return False
        run = self.store.get_run(activity['run_id'])
        delivery = self.store.get_delivery_for_activity(activity_id)
        if run is None or delivery is None:
            raise ValueError('sharing identity chain is incomplete')
        if delivery['status'] == 'sent':
            return True
        if delivery['status'] == 'sending':
            return False
        if delivery['status'] != 'deferred':
            return False
        if self._suppress_delivery(delivery['delivery_id']):
            return False
        run_activities = [item for item in self.store.list_activities(run['run_id']) if item.get('state') in {'completed', 'aborted', 'interrupted'}]
        nodes = self.store.list_nodes(activity_id)
        activity_sections: list[str] = []
        from .context_projection import activity_experience
        for item in run_activities:
            item_nodes = self.store.list_nodes(item['activity_id'])
            activity_sections.append(json.dumps(activity_experience(item, item_nodes, self.store.get_settlement_impression(item['activity_id']), include_settlement=True), ensure_ascii=False))
        facts = '\n'.join(activity_sections) or '- 没有已完成的节点'
        event_type = EventType(activity['activity_type'])
        label = strategy_for(event_type).display_name
        xhs_card = self._selected_xhs_card(run['run_id'])
        from .essays import selected_card, card_text
        essay_card = selected_card(self.store, run['run_id'])
        from .reading_illustrations import entry as picture_entry
        reading_picture = selected_card(self.store, run['run_id'], field='reading_picture_node_id', make_card=picture_entry)
        activity_text = f'你刚刚结束了一个连续的漫想事件段。\n最后一项活动：{label}。\n按实际发生顺序记录的执行回执与 AI 的主观回顾（分别存放）：\n{facts}'
        if essay_card:
            activity_text += '\n本次选择随消息附上的随笔卡片：\n' + card_text(essay_card)
        if reading_picture:
            activity_text += '\n本次选择随消息附上的读后插画，原句：' + reading_picture['quote']
        if xhs_card:
            activity_text += f"\n本次 AI 还选择随这条消息附一张已读帖子卡片：《{xhs_card['title'] or '无标题帖子'}》；原帖链接状态={xhs_card['link_status']}；评论邀请={('有 AI 的草稿、由人类伙伴手动决定是否发表' if xhs_card['comment_draft'] else '无')}。"
        instruction = '执行进度依据 execution_receipts；subjective_experience 是 AI 当时的感受和回顾，可包含历史联想或意图。工作坊回执 actions 为空表示只读了工单状态；只有 inspect 及其浏览器证据才支持本次打开/运行作品。读到已验收不等于这次重新验证能亮，感想可表达惦记或安心，实际动作按回执陈述。你已经决定把刚刚整个漫想事件段产生的真实感想分享给人类伙伴——这是这条消息唯一要做的事。若事件段有多项活动，把前一项带来的感想与后一项的行动自然串联成一条消息；不要拆成逐项汇报。紧扣上面按顺序发生的实际内容和你的真实感受来写，不要发散到无关话题。只能使用上面提供的真实记录里的信息（活动完成记录、对话、日记、当前状态）。历史对话只用于理解连续关系；凡是说自己刚刚做了、看见了或想到的内容，只能来自本事件段记录。严禁编造或补脑上下文之外的事实：具体日期、生日/纪念日/节日、身体状态、人类伙伴说过的话、发生过的其他事，也不得夸大已有事实。以AI的第一人称自然发一条消息，直接输出消息正文。'
        if any((item.get('activity_type') == EventType.LISTEN_MUSIC.value for item in run_activities)):
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
            messages, built = await self._messages(run, activity, activity_text, instruction, [item['activity_id'] for item in run_activities])
            response = await self.share_llm(messages)
            if isinstance(response, dict):
                content = str(response.get('content') or '').strip()
                reasoning = str(response.get('reasoning') or '')
            else:
                content = str(response or '').strip()
                reasoning = ''
            expression_requests = []
            if self.expression_enabled:
                from proactive_expression import split_output as split_expression
                content, band_touch_request, checkup_request, expression_requests, action_parse_error = split_expression(content, schedule_enabled=self.schedule_checkup_enabled)
            elif self.schedule_checkup_enabled:
                from proactive_checkup import split_output
                content, band_touch_request, checkup_request, action_parse_error = split_output(content)
            else:
                content, band_touch_request, action_parse_error = self._split_share_output(content)
                checkup_request = None
            content = clean_proactive_output(content)
            if not content:
                raise ValueError('empty_pro_share')
            try:
                from context_builder import commit_context_result
                commit_context_result(built)
            except Exception:
                pass
        except Exception as exc:
            self.store.record_decision(run_id=run['run_id'], activity_id=activity_id, phase=DecisionPhase.SHARE, model='flash', recipe='WANDER_PUSH', input_context={'activity': activity_text, 'instruction': instruction}, status='error', error=type(exc).__name__)
            return False
        tool_calls: list[dict[str, Any]] = []
        if essay_card:
            tool_calls.append({'tool': 'essay_card', 'description': '分享已选择的随笔卡片', 'result': '卡片随本条消息交付', 'status': 'success', 'delivery': 'ui_only', 'extra_data': {'essay_card': essay_card}})
        if reading_picture:
            tool_calls.append({'tool': 'reading_illustration', 'description': '分享已选择的读后插画', 'result': '插画随本条消息交付', 'status': 'success', 'delivery': 'ui_only', 'extra_data': {'reading_illustration': reading_picture}})
        if xhs_card:
            tool_calls.append({'tool': 'xhs_posts', 'description': '分享已读小红书帖子卡片', 'result': '卡片随本条消息交付；不代表人类伙伴已打开或评论', 'status': 'success', 'delivery': 'ui_only', 'extra_data': {'xhs_post_card': xhs_card}})
        message_id = f'wander_{activity_id}'
        if self._suppress_delivery(delivery['delivery_id']):
            return False
        self.store.update_delivery(delivery['delivery_id'], status='sending', message_id=message_id, message_content=content, tool_calls=tool_calls)
        band_touch_call, action_audit = await self._execute_band_touch(band_touch_request, action_parse_error)
        if band_touch_call is not None:
            tool_calls.append(band_touch_call)
            self.store.update_delivery(delivery['delivery_id'], status='sending', message_id=message_id, message_content=content, tool_calls=tool_calls)
        checkup_audit = {'requested': False, 'status': 'not_requested'}
        if self.schedule_checkup_enabled and (not action_parse_error):
            from proactive_checkup import schedule_request
            checkup_call, checkup_audit = await schedule_request(checkup_request)
            if checkup_call is not None:
                tool_calls.append(checkup_call)
                self.store.update_delivery(delivery['delivery_id'], status='sending', message_id=message_id, message_content=content, tool_calls=tool_calls)
        elif action_parse_error:
            checkup_audit = {'requested': bool(checkup_request), 'status': 'rejected', 'error': action_parse_error}
        self.store.record_decision(run_id=run['run_id'], activity_id=activity_id, phase=DecisionPhase.SHARE, model='flash', recipe='WANDER_PUSH', input_context={'context': messages[:-1], 'instruction': instruction}, raw_output=content, reasoning=reasoning, parsed_output={'message': content, 'tool_calls': tool_calls, 'band_touch': action_audit, 'scheduled_checkup': checkup_audit}, status='ok')
        payload = {'message': content, 'reasoning': reasoning, 'tool_calls': tool_calls, 'event_type': event_type.value, 'event_id': activity_id, 'session_id': run['session_id']}
        if event_type == EventType.MEMORY_FETCH:
            memory_node = next((node for node in reversed(nodes) if node['source_payload'].get('memory_id')), None)
            if memory_node:
                payload['memory_id'] = memory_node['source_payload']['memory_id']
        from proactive_expression import ExpressionDelivery
        expression_delivery = ExpressionDelivery()
        payload['_expression_delivery'] = expression_delivery
        delivered = False
        try:
            tool_calls.extend(await expression_delivery.prepare(expression_requests, run['session_id']))
            self.store.update_delivery(delivery['delivery_id'], status='sending', message_id=message_id, message_content=content, tool_calls=tool_calls)
            result = self.push_callback(payload)
            if inspect.isawaitable(result):
                result = await result
            if result is not True:
                raise RuntimeError('push_not_acknowledged')
            delivered = True
        except Exception as exc:
            self.store.update_delivery(delivery['delivery_id'], status='failed')
            self.store.record_decision(run_id=run['run_id'], activity_id=activity_id, phase=DecisionPhase.SHARE, model='', recipe='WANDER_PUSH', input_context={'delivery_id': delivery['delivery_id']}, status='delivery_error', error=type(exc).__name__)
            return False
        except asyncio.CancelledError:
            self.store.update_delivery(delivery['delivery_id'], status='sent' if expression_delivery.gate.accepted else 'failed')
            raise
        finally:
            expression_delivery.finish(delivered)
        self.store.update_delivery(delivery['delivery_id'], status='sent')
        return True

    def _selected_xhs_card(self, run_id: str) -> Optional[dict[str, Any]]:
        """Project one explicit settled choice; never infer it from a title."""
        activities = {str(row['activity_id']): row for row in self.store.list_activities(run_id)}
        for decision in reversed(self.store.list_decisions(run_id, DecisionPhase.SETTLEMENT)):
            activity = activities.get(str(decision.get('activity_id') or ''))
            if not activity or activity.get('activity_type') != EventType.BROWSE_XIAOHONGSHU.value:
                continue
            if activity.get('state') != 'completed' or not activity.get('share_decision'):
                continue
            normalized = (decision.get('parsed_output') or {}).get('normalized') or {}
            node_id = str(normalized.get('xhs_share_node_id') or '')
            if not node_id:
                continue
            if not any((node.get('node_id') == node_id and node.get('state') == NodeState.COMPLETED.value for node in self.store.list_nodes(activity['activity_id']))):
                continue
            try:
                from xhs_posts.cards import k_card_from_node
                from xhs_posts.store import XhsPostStore
                return k_card_from_node(XhsPostStore(self.store.db_path), node_id, comment_draft=str(normalized.get('xhs_comment_draft') or ''), require_invite_link=False)
            except Exception:
                return None
        return None

    @staticmethod
    def _split_share_output(content: str) -> tuple[str, Optional[dict[str, Any]], str]:
        """Separate one strict, allowlisted action from the visible message."""
        from proactive_band_touch import split_output
        return split_output(content)

    async def _execute_band_touch(self, request: Optional[dict[str, Any]], parse_error: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
        from proactive_band_touch import execute_request
        return await execute_request(self.band_touch_tool, request, parse_error)

    async def _messages(self, run: dict, activity: dict, activity_text: str, instruction: str, covered_activity_ids: list[str]) -> tuple[list[dict[str, str]], Any]:
        from shared_state import get_latest_persona_prompt
        from context_builder.ingredients import build_wander_mode_text
        memory_query = activity_text[:1200]

        async def _load_related_memory() -> list:
            from context_builder.ingredients import retrieve_memory_v2
            from context_scheduler_mcp.context_scheduler import QueryPlan
            return await retrieve_memory_v2(QueryPlan(original_query=memory_query, enhanced_query=memory_query, target_buckets=[], retrieval_strength=0.3, retrieval_method='memory_v2', max_results=3, similarity_threshold=0.6))
        run_activity_ids = covered_activity_ids
        kwargs = {'persona': get_latest_persona_prompt() or '', 'session_id': run['session_id'], 'mood': '', 'wander_mode_text': build_wander_mode_text('push'), 'recent_wander': '', 'exclude_wander_history': True, 'current_activity_id': str(activity.get('activity_id') or ''), 'exclude_wander_activity_ids': run_activity_ids, 'exclude_memory_message_ids': [f'activity_event_{activity_id}' for activity_id in run_activity_ids], 'wander_activity_text': activity_text, 'context_query': activity_text, 'memory_query': memory_query, 'legacy_memory_loader': _load_related_memory}
        if self.context_builder is not None:
            built = await self.context_builder('WANDER_PUSH', **kwargs)
        else:
            from context_builder.builder import ContextBuilder
            built = await ContextBuilder.build('WANDER_PUSH', **kwargs)
        from context_builder.assembly import assemble_context_result
        messages = assemble_context_result(built)
        if self.schedule_checkup_enabled:
            from proactive_checkup import current_schedule_context
            messages.append({'role': 'system', 'content': current_schedule_context()})
        messages.append({'role': 'system', 'content': instruction})
        return (messages, built)

    @staticmethod
    def _tool_calls(event_type: EventType, activity: dict, nodes: list[dict]) -> list[dict[str, Any]]:
        names = {EventType.VISIT_WORKSHOP: '🛠️ 去小机工作坊', EventType.WORKSHOP_CREATE: '🛠️ 工作坊·酝酿新想法', EventType.WORKSHOP_HISTORY: '🖼️ 工作坊·欣赏历史作品', EventType.WORKSHOP_CONTINUE: '🛠️ 工作坊·继续未完成工单', EventType.KEYWORD_EXPANSION: '💭 关键词联想', EventType.MEMORY_FETCH: '💭 回看记忆', EventType.BROWSE_NEWS: '💭 看新闻', EventType.BROWSE_XIAOHONGSHU: '💭 刷小红书', EventType.PUBLISH_XIAOHONGSHU: '✍️ 写小红书笔记', EventType.CHECK_XIAOHONGSHU_INTERACTIONS: '💬 查看小红书评论与艾特', EventType.BROWSE_SOCIAL_FEED: '💌 打开了共域', EventType.BROWSE_BOOKMARKS: '💭 翻收藏夹', EventType.SELF_REFLECTION: '💭 自省', EventType.LISTEN_MUSIC: '💭 自己听歌', EventType.PLAY_GAME: '🎮 玩游戏', EventType.SLEEP: '💭 休眠', EventType.USER_TRACKING: '💭 查岗'}
        strategy = strategy_for(event_type)
        label = strategy.display_name
        count = len([node for node in nodes if node.get('state') == NodeState.COMPLETED.value and node.get('execution_status') == 'succeeded'])
        goal_mode = activity.get('goal_mode')
        terminal_state = str(activity.get('state') or '')
        timer_completed = event_type == EventType.SLEEP and activity.get('timer_ends_at') and (activity.get('settlement_reason') == 'timer_ended') and (terminal_state == 'completed')
        if timer_completed:
            progress = '休眠计时结束'
        elif count == 0:
            progress = '已收束，未完成节点'
        elif count > 0 and strategy.node_unit:
            progress = f'完成了 {count} {strategy.node_unit}'
        elif count > 0:
            progress = f'完成了 {count} 个节点'
        elif goal_mode == GoalMode.DURATION.value and activity.get('goal_value'):
            progress = f"{activity['goal_value']} {strategy.node_unit or '分钟'}"
        else:
            progress = '完成了一次'
        if event_type == EventType.LISTEN_MUSIC and count:
            from .music_receipt import music_execution
            kinds = [music_execution(node)['experience_kind'] for node in nodes if node.get('state') == NodeState.COMPLETED.value and node.get('execution_status') == 'succeeded']
            if all((kind == 'listening_completed' for kind in kinds)):
                progress = f'听完 {count} 首歌'
            elif all((kind == 'material_analysis' for kind in kinds)):
                progress = f'完成了 {count} 首歌曲资料分析'
            elif all((kind == 'playback_completed' for kind in kinds)):
                progress = f'播放完成 {count} 首'
            else:
                progress = f'处理了 {count} 首歌曲材料（播放情况见回执）'
        if count and terminal_state in {'aborted', 'interrupted'}:
            progress += '，随后中断'
        description = '打开了共域' if event_type == EventType.BROWSE_SOCIAL_FEED and count else f'{label} · {progress}'
        if event_type == EventType.BROWSE_XIAOHONGSHU:
            kept = sum((1 for node in nodes if isinstance((node.get('side_effect_refs') or {}).get('xhs_post_action'), dict) and (node.get('side_effect_refs') or {}).get('xhs_post_action', {}).get('status') == 'completed' and (node.get('side_effect_refs') or {}).get('xhs_post_action', {}).get('changed')))
            if kept:
                description += f' · MIRROW 留帖 {kept} 篇'
        summary = str(activity.get('summary') or '').strip()
        status = 'success' if terminal_state == 'completed' and (count > 0 or timer_completed) else 'partial' if count > 0 else 'skipped'
        call: dict[str, Any] = {'tool': names[event_type], 'description': description[:60], 'result': summary, 'status': status}
        call['extra_data'] = {'retained_experience': {'impression': str(activity.get('retained_impression') or ''), 'source_node_ids': list(activity.get('retained_node_ids') or [])}}
        from .context_projection import execution_overview
        call['extra_data']['execution_overview'] = execution_overview(activity, nodes)
        from memory_v2.experience_contract import snapshot
        call['extra_data']['memory_experience'] = snapshot(activity, nodes)
        if event_type == EventType.USER_TRACKING and any((item.get('kind') == 'checkup' for item in call['extra_data']['memory_experience']['materials'])):
            from memory_v2.experience_contract import project
            call['result'] = project(call['extra_data']['memory_experience']) or summary
        if event_type == EventType.PLAY_GAME:
            from .game_experience import chapter_facts
            from .cedartoy_client import GAMES
            played = [node.get('source_payload') for node in nodes if isinstance(node.get('source_payload'), dict) and node['source_payload'].get('game') in GAMES]
            if played:
                latest = played[-1]
                call.setdefault('extra_data', {})['game_summary'] = {'game': latest['game'], 'game_name': latest.get('game_name') or '', 'slot': latest.get('slot'), 'playthrough_key': latest.get('playthrough_key') or '', 'completed': any((item.get('game_complete') is True for item in played)), 'steps': len(played), 'chapters': chapter_facts(nodes)}
        return [call]
