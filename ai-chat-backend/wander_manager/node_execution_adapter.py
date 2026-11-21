"""Safe one-node execution boundary for the new Wander runtime.

This adapter executes only handlers whose current API represents one bounded
piece of real work. It never advances the runtime state machine and it never
retries after a crash.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging
import re
import inspect
import json
from typing import Any, Callable
from .event_types import EventType, WanderEvent
from .cedartoy_client import CedarToyError, CedarToyRejectedAction
from .runtime_models import DecisionPhase, NodeState, WanderNode
from .runtime_store import WanderRuntimeStore
from .goal_policy import is_autonomous_run
logger = logging.getLogger(__name__)

def _safe_handler_error(event_type: EventType, exc: BaseException) -> str:
    """Map handler exceptions to a short public error.

    Handler/provider exceptions may contain credentials, URLs, local paths or
    device details.  Each domain can own a domain-level public mapper; use it
    when available, but deliberately collapse its configuration hint to the
    UI-safe wording (the environment variable name is not public evidence).
    Other domains expose only a stable generic message while the full
    traceback remains in the server logger.
    """
    return 'RuntimeError:节点执行异常'

class NodeExecutionStatus(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    WAITING_EXTERNAL = 'waiting_external'

@dataclass
class NodeExecutionResult:
    status: NodeExecutionStatus
    completion_signal: str = ''
    summary: str = ''
    payload: dict[str, Any] = field(default_factory=dict)
    side_effect_refs: dict[str, Any] = field(default_factory=dict)
    error: str = ''
    retry_safe: bool = False
    audit_model: str = 'legacy_handler'
    audit_input: dict[str, Any] = field(default_factory=dict)
    audit_raw_output: Any = ''
    audit_reasoning: Any = ''
    audit_usage: dict[str, Any] = field(default_factory=dict)
    memory_material: dict[str, Any] | None = None
_SPECIALIZED_EVENTS = {EventType.SELF_REFLECTION}
_XHS_SKIP_STATUSES = frozenset({'adb_unavailable', 'adb_unauthorized', 'ambiguous_devices', 'device_disconnected', 'battery_low', 'battery_unavailable', 'consent_required', 'login_required', 'verification_required', 'risk_detected', 'popup_detected', 'vision_unavailable', 'profile_end_observed', 'profile_listing_stalled', 'profile_listing_changed', 'profile_listing_ambiguous', 'profile_cursor_ambiguous', 'profile_page_budget', 'no_feed_cards', 'no_new_feed_cards', 'no_public_cards'})

def _song_identity_key(payload: dict[str, Any] | None) -> str:
    """Build the same normalized title/artist key used by the music handler."""
    if not isinstance(payload, dict):
        return ''
    title = str(payload.get('title') or payload.get('song_name') or '').strip()
    artist = str(payload.get('artist') or '').strip()
    if not title or not artist or artist.casefold() in {'未知', 'unknown', '?'}:
        return ''
    return re.sub('[^\\w\\u4e00-\\u9fff]+', '', f'{title}|{artist}'.casefold())

class NodeExecutionAdapter:
    """Execute one already-claimed RUNNING node and persist its evidence."""

    def __init__(self, store: WanderRuntimeStore, handler_factory: Any, clock: Callable[[], datetime]=datetime.now, music_playback_enabled: Callable[[], bool] | None=None, get_idle_seconds: Callable[[], float] | None=None, get_setting: Callable[[str], Any] | None=None, music_playback_receipt: Callable[[str, str], Any] | None=None, game_context_provider: Callable[[], Any] | None=None, checkup_executor: Any=None):
        self.store = store
        self.handler_factory = handler_factory
        self.clock = clock
        self.music_playback_enabled = music_playback_enabled
        self.get_idle_seconds = get_idle_seconds
        self.get_setting = get_setting
        self.music_playback_receipt = music_playback_receipt
        self.game_context_provider = game_context_provider
        self.checkup_executor = checkup_executor

    async def execute(self, node_id: str, *, decision_context: Any=None) -> NodeExecutionResult:
        """Execute one claimed node; local post sharing belongs to xhs_posts."""
        node = self._required_node(node_id)
        if node.state != NodeState.RUNNING:
            raise ValueError('node must be running')
        if node.execution_status != NodeExecutionStatus.PENDING.value:
            raise ValueError('node execution was already claimed')
        activity = self._required_activity(node.activity_id)
        run = self._required_run(activity['run_id'])
        event_type = EventType(activity['activity_type'])
        if activity['interrupt_requested']:
            return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='节点开始前已收到中断', error='interrupt_requested'))
        node.execution_status = NodeExecutionStatus.RUNNING.value
        self.store.save_node(node)
        if event_type == EventType.SLEEP:
            return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='睡眠由真实计时器管理，不生成普通节点', error='sleep_has_no_nodes'))
        if event_type in _SPECIALIZED_EVENTS:
            return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='该事件需要专用执行器', error='unsupported_specialized_adapter'))
        seen = self.store.list_nodes(activity['activity_id'])
        try:
            if event_type == EventType.CHECK_XIAOHONGSHU_INTERACTIONS:
                from .xhs_interaction_runtime import execute_node
                result = await execute_node(self.store, activity, node.node_id, decision_context)
                return self._finish(run, activity, node, result)
            if event_type == EventType.USER_TRACKING:
                from .checkup_execution import WanderCheckupExecutor
                if decision_context is None:
                    raise ValueError('checkup_context_required')
                executor = self.checkup_executor or WanderCheckupExecutor(self.store)
                result = await executor.observe(activity, node.to_dict(), decision_context)
                return self._finish(run, activity, node, result)
            if event_type == EventType.PUBLISH_XIAOHONGSHU:
                from xhs_publish.autonomy import compose
                from xhs_publish.service import experience
                payload = await compose(self.store, activity, decision_context, node_id=node.node_id)
                publication = payload.get('xhs_publication') or {}
                good = publication.get('status') in {'published', 'previewed'} or payload.get('status') == 'composition_finished'
                result = NodeExecutionResult(NodeExecutionStatus.SUCCEEDED if good else NodeExecutionStatus.FAILED, completion_signal=publication.get('status') or payload.get('status', 'publication_incomplete'), summary=experience(publication) or '本次小红书创作结束，未发布。', payload=payload, retry_safe=False, side_effect_refs={'publication_task_id': publication.get('task_id', '')}, error='' if good else publication.get('error') or payload.get('status', 'publication_incomplete'), audit_model='xhs_publication')
                return self._finish(run, activity, node, result)
            if event_type == EventType.PLAY_GAME:
                if self.game_context_provider is None:
                    raise RuntimeError('game_context_provider_unavailable')
                from .game_handler import GameNodeHandler
                bundle = await self.game_context_provider()
                receipt = await GameNodeHandler(self.store).play_once(activity, seen, bundle.decision, node_id=node.node_id)
                from .game_experience import game_step_summary
                result = NodeExecutionResult(NodeExecutionStatus.SUCCEEDED, completion_signal='game_complete' if receipt.get('game_complete') else 'game_step_complete', summary=game_step_summary(receipt), payload=receipt, side_effect_refs={'cedartoy_game': receipt['game'], 'slot': receipt['slot'], 'playthrough_key': receipt['playthrough_key']}, retry_safe=False, audit_model='cedartoy_verified_action', audit_input={'event_type': event_type.value, 'game': receipt['game'], 'slot': receipt['slot'], 'action': receipt['action']}, audit_raw_output={'game_complete': receipt.get('game_complete'), 'playthrough_key': receipt['playthrough_key']})
                return self._finish(run, activity, node, result)
            handler = self.handler_factory.get_handler(event_type)
            result = await self._execute_supported(event_type, handler, seen, activity_id=activity['activity_id'], node_id=node.node_id, decision_context=decision_context)
        except CedarToyRejectedAction as exc:
            resting = exc.category == 'anti_addiction'
            result = NodeExecutionResult(NodeExecutionStatus.SKIPPED if resting else NodeExecutionStatus.FAILED, summary='CedarToy 防沉迷要求休息，本次游玩停在已确认的存档。' if resting else f'CedarToy 拒绝了这一步游戏动作（{exc.category}），结果未确认。', error=f'cedartoy_{exc.category}', audit_model='cedartoy_provider_refusal', audit_input={'event_type': event_type.value, 'failure_stage': 'provider_refusal', 'game': exc.game, 'attempted_action': exc.attempted_action}, audit_raw_output={'category': exc.category, 'provider_detail': exc.detail})
            return self._finish(run, activity, node, result)
        except CedarToyError as exc:
            code = str(exc)
            code = code if re.fullmatch('[a-z][a-z0-9_]{1,80}', code) else 'cedartoy_game_error'
            result = NodeExecutionResult(NodeExecutionStatus.FAILED, summary=f'游戏动作未完成（{code}），存档结果未确认。', error=code, audit_model='cedartoy_local_contract', audit_input={'event_type': event_type.value, 'failure_stage': 'game_contract'}, audit_raw_output={'error_code': code})
            return self._finish(run, activity, node, result)
        except Exception as exc:
            logger.exception('wander node handler raised: event_type=%s', event_type.value)
            public_error = _safe_handler_error(event_type, exc)
            result = NodeExecutionResult(NodeExecutionStatus.FAILED, summary=f'节点执行异常：{public_error}', error=public_error, audit_input={'event_type': event_type.value, 'failure_stage': 'unknown', 'public_error': public_error}, audit_raw_output={'status': 'failed', 'failure_stage': 'unknown', 'public_error': public_error})
        refreshed = self._required_activity(node.activity_id)
        if refreshed['interrupt_requested']:
            result.side_effect_refs['interrupted_after_execution'] = True
        if result.status == NodeExecutionStatus.SUCCEEDED and event_type in {EventType.KEYWORD_EXPANSION, EventType.BROWSE_BOOKMARKS}:
            key, source = ('essay_illustration', {}) if event_type == EventType.KEYWORD_EXPANSION else ('reading_illustration', result.payload.pop('reading_picture', {}) or {})
            request = result.payload.pop('illustration_request', None) if event_type == EventType.KEYWORD_EXPANSION else source.pop('scene', None)
            if request:
                result.side_effect_refs[key] = {**source, 'status': 'interrupted' if refreshed['interrupt_requested'] else 'requested'}
                self._finish(run, refreshed, node, result)
                if not refreshed['interrupt_requested']:
                    from .essay_illustration import attach
                    result.side_effect_refs = await attach(self.store, node.node_id, request, key, source)
                return result
        return self._finish(run, refreshed, node, result)

    async def _execute_supported(self, event_type, handler, seen_nodes, *, activity_id, node_id='', decision_context=None) -> NodeExecutionResult:
        from .activity_context import bind, snapshot
        activity = self._required_activity(activity_id)
        with bind(snapshot(self.store, activity, seen_nodes)):
            return await self._execute_scoped(event_type, handler, seen_nodes, activity_id=activity_id, node_id=node_id, decision_context=decision_context)

    async def _execute_scoped(self, event_type: EventType, handler: Any, seen_nodes: list[dict[str, Any]], *, activity_id: str, node_id: str='', decision_context: Any=None) -> NodeExecutionResult:
        if event_type == EventType.KEYWORD_EXPANSION:
            excluded = {item['source_payload'].get('keyword') for item in seen_nodes if item['source_payload'].get('keyword')}
            payload = await handler.fetch_one_expansion(exclude_keywords=excluded)
            valid = bool(payload and payload.get('keyword') and payload.get('expansion'))
            return self._material_result(valid, payload, (f"写下随笔《{payload['title']}》" if payload and payload.get('essay_version') == 1 else f"关键词「{payload.get('keyword')}」的联想") if valid else '随笔尚未写成')
        if event_type == EventType.MEMORY_FETCH:
            excluded = {item['source_payload'].get('memory_id') for item in seen_nodes if item['source_payload'].get('memory_id')}
            payload = await handler.fetch_one_memory(exclude_ids=excluded)
            valid = bool(payload and payload.get('memory_id'))
            return self._material_result(valid, payload, f"关于「{payload.get('topic', '未命名')}」的回忆" if valid else '未取得新记忆')
        if event_type == EventType.BROWSE_NEWS:
            payload = await handler.fetch_one_news()
            if payload and payload.get('status') == 'no_search_tool':
                return NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='没有可用的新闻搜索工具', payload=payload, error='no_search_tool')
            valid = bool(payload and payload.get('status') == 'success' and payload.get('query') and payload.get('content'))
            return self._material_result(valid, payload, f"阅读「{payload.get('query')}」的搜索结果" if valid else '新闻搜索未取得有效内容')
        if event_type == EventType.BROWSE_XIAOHONGSHU:
            activity = self._required_activity(activity_id)
            excluded = {item['source_payload'].get('source_id') for item in seen_nodes if item['source_payload'].get('source_id')}
            fetch_kwargs = {'exclude_ids': excluded, 'activity_reason': str(activity.get('reason') or '')}
            from .xhs_request import compile_task, latest_cursor, resolve_profile_resume
            from .runtime_models import WanderRun, RunState
            run_record = self.store.get_run(activity['run_id'])
            snapshot = dict(run_record.get('context_snapshot') or {})
            tasks = dict(snapshot.get('phone_tasks') or {})
            task_record = tasks.get(activity_id)
            if task_record is None:
                spec = await compile_task(str(activity.get('reason') or '浏览首页推荐'))
                initial_cursor, parent_run = ({}, None)
                if spec['operation'] == 'continue':
                    try:
                        parent_run, parent_activity, spec, initial_cursor = resolve_profile_resume(self.store, run_record['session_id'], spec['task_id'])
                        if parent_run['state'] not in {'completed', 'aborted', 'interrupted'}:
                            raise ValueError('task_in_progress')
                    except ValueError as exc:
                        return NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary=f'小红书续读未执行：{exc}', payload={'status': str(exc)}, error=str(exc))
                task_record = {'version': 1, 'spec': spec, 'initial_cursor': initial_cursor, **({'resumed_from': parent_run['run_id']} if parent_run else {})}
                tasks[activity_id] = task_record
                snapshot['phone_tasks'] = tasks
                frozen_run = WanderRun(**{**run_record, 'state': RunState(run_record['state']), 'context_snapshot': snapshot})
                self.store.save_run(frozen_run)
                if parent_run:
                    parent_run['context_snapshot'].setdefault('phone_task_successors', {})[parent_activity['activity_id']] = activity_id
                    self.store.save_run(WanderRun(**{**parent_run, 'state': RunState(parent_run['state'])}))
            fetch_kwargs['task'] = task_record['spec']
            fetch_kwargs['cursor'] = latest_cursor(seen_nodes, task_record.get('initial_cursor'))
            if task_record.get('resumed_from') and task_record['spec']['scope'] == 'count' and (fetch_kwargs['cursor'].get('read_count', 0) >= task_record['spec']['goal_count']):
                return NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='主页任务已达到累计目标', payload={'status': 'profile_goal_reached'}, error='profile_goal_reached')
            payload = await handler.fetch_one_post(**fetch_kwargs)
            status = str(payload.get('status') or '') if payload else ''
            audit = payload.pop('agent_audit', {}) if isinstance(payload, dict) else {}
            calls = audit.get('calls') or [] if isinstance(audit, dict) else []
            if status in _XHS_SKIP_STATUSES:
                execution_mode = str((payload or {}).get('execution_mode') or 'dedicated_device_direct')
                return NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary=f"小红书真机节点跳过：{status or '能力不可用'}", payload=payload or {}, error=status, audit_model=calls[-1].get('model') if calls else 'adb', audit_input={'activity_reason': activity.get('reason') or '', 'event_type': event_type.value, 'execution_mode': execution_mode}, audit_raw_output=calls or [])
            source_id = str(payload.get('source_id') or '') if payload else ''
            if status == 'success' and source_id and (source_id in excluded):
                return NodeExecutionResult(NodeExecutionStatus.FAILED, summary='小红书浏览命中了已读帖子', payload=payload or {}, error='duplicate_source_id', retry_safe=False, audit_model=calls[-1].get('model') if calls else 'adb', audit_input={'activity_reason': activity.get('reason') or '', 'event_type': event_type.value}, audit_raw_output=calls)
            valid = bool(payload and status == 'success' and payload.get('source_id') and payload.get('content_summary'))
            title = payload.get('title') if payload else ''
            return NodeExecutionResult(NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED, completion_signal='node_material_ready' if valid else '', summary=f"浏览小红书内容「{title or payload.get('query', '未知')}」" if valid else '小红书浏览未取得可靠内容', payload=payload or {}, error='' if valid else status or 'missing_required_material', retry_safe=False, audit_model=calls[-1].get('model') if calls else 'adb', audit_input={'activity_reason': activity.get('reason') or '', 'event_type': event_type.value, 'execution_mode': str(payload.get('execution_mode') or 'dedicated_device_direct')}, audit_raw_output=calls, audit_reasoning=[item.get('reasoning', '') for item in calls], audit_usage={'calls': [item.get('usage', {}) for item in calls]})
        if event_type in (EventType.VISIT_WORKSHOP, EventType.WORKSHOP_CREATE, EventType.WORKSHOP_HISTORY, EventType.WORKSHOP_CONTINUE):
            activity = self._required_activity(activity_id)
            if event_type == EventType.VISIT_WORKSHOP:
                payload = await handler.visit_once(activity_id, purpose=activity.get('reason') or '')
            else:
                payload = await handler.visit_once(activity_id, purpose=activity.get('reason') or '', entry=event_type.value)
            valid = payload.get('status') == 'success'
            waiting = payload.get('status') == 'waiting' and bool(payload.get('collaboration_id'))
            return NodeExecutionResult(NodeExecutionStatus.WAITING_EXTERNAL if waiting else NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.SKIPPED, completion_signal='workshop_visit_saved' if valid else '', summary=payload.get('message', ''), payload=payload, side_effect_refs={'job_id': payload.get('job_id', ''), 'workshop_collaboration_id': payload.get('collaboration_id', ''), **({'expected_completion_at': self.clock().isoformat()} if waiting else {})}, retry_safe=False)
        if event_type == EventType.BROWSE_SOCIAL_FEED:
            activity = self._required_activity(activity_id)
            run = self._required_run(activity['run_id'])
            from .social_circle_visit import SocialCircleVisit
            prior = [row for row in seen_nodes if row.get('node_id') != node_id and row.get('execution_status') == NodeExecutionStatus.SUCCEEDED.value]
            request = (prior[-1].get('source_payload') or {}).get('next_request') if prior else None
            if not prior:
                request = ((self.store.get_node(node_id) or {}).get('source_payload') or {}).get('social_initial_request')
                if request is None:
                    request = {'kind': 'all', 'visibility_filter': 'all', 'opening': True}
            payload = await SocialCircleVisit(handler.call_llm, store=handler.store, on_notification=handler.on_notification, home_observation=self.store.latest_home_social_observation_at).visit_step(request=request, previous_nodes=prior, activity_reason=str(activity.get('reason') or ''), run_id=str(activity.get('run_id') or ''), activity_id=activity_id, node_id=node_id, persona=str(getattr(decision_context, 'persona', '') or ''), session_id=str(getattr(decision_context, 'session_id', '') or run.get('session_id') or ''))
            status = str(payload.get('status') or '') if payload else ''
            valid = status == 'success'
            actions = [str(row.get('action') or '') for row in payload.get('action_results') or [] if row.get('status') == 'success'] if payload else []
            return NodeExecutionResult(NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED, completion_signal=('social_feed_visit_complete' if payload.get('exit') else 'social_feed_node_complete') if valid else '', summary=f"读了共友圈 {len(payload.get('viewed_moment_ids') or [])} 条动态；执行 {len(actions)} 项互动；{str(payload.get('reflection') or '')[:140]}" if valid else '朋友圈浏览没有完成可靠决策', payload=payload or {}, side_effect_refs={'action_refs': [dict(row) for row in (payload.get('action_results') or [])[:5]] if payload else []}, error='' if valid else status or 'social_feed_visit_failed', retry_safe=False, audit_model='wander_flash', audit_input={'event_type': event_type.value, 'activity_reason': activity.get('reason') or ''}, audit_raw_output={'status': status, 'actions': actions})
        if event_type == EventType.BROWSE_BOOKMARKS:
            excluded = {item['source_payload'].get('original_msg_id') for item in seen_nodes if item['source_payload'].get('original_msg_id')}
            activity = self._required_activity(activity_id)
            fetch = handler.fetch_one_bookmark
            fetch_kwargs = {'exclude_ids': excluded}
            try:
                parameters = inspect.signature(fetch).parameters
                accepts_kwargs = any((parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()))
                for name, value in (('run_id', activity.get('run_id', '')), ('activity_id', activity_id), ('node_id', node_id)):
                    if accepts_kwargs or name in parameters:
                        fetch_kwargs[name] = value
            except (TypeError, ValueError):
                pass
            payload = await fetch(**fetch_kwargs)
            selection = (payload or {}).get('material_selection') or {}
            if selection and selection.get('status') != 'ready':
                from .bookmark_selection import describe
                reason = str(selection.get('reason') or 'material_read_failed')
                summary = describe(reason)
                return NodeExecutionResult(NodeExecutionStatus.SKIPPED if selection.get('status') in {'empty', 'stopped'} else NodeExecutionStatus.FAILED, summary=summary, payload={**payload, 'public_error': summary}, error=reason, retry_safe=False)
            valid = bool(payload and payload.get('original_msg_id') and (payload.get('content') or payload.get('displayed_message_count')) and payload.get('reading_received', True))
            owner = payload.get('owner', '未知') if payload else '未知'
            action = payload.get('bookmark_action') if isinstance(payload, dict) else None
            action_status = str(action.get('status') or '') if isinstance(action, dict) else ''
            if selection and (not valid):
                reason = 'reading_call_failed' if not payload.get('reading_received') else 'material_evidence_missing'
                from .bookmark_selection import describe
                return NodeExecutionResult(NodeExecutionStatus.FAILED, summary=describe(reason), payload={**payload, 'public_error': describe(reason)}, error=reason, retry_safe=False)
            if valid and action_status == 'failed':
                return NodeExecutionResult(NodeExecutionStatus.FAILED, summary='收藏夹材料已读取，但收藏动作失败', payload=payload, error=str(action.get('reason') or 'bookmark_action_failed')[:200], retry_safe=False)
            return self._material_result(valid, payload, f"回看{payload.get('active_date')}的历史对话" if valid and payload.get('source_mode') in {'unbookmarked_anchor', 'random_day'} else '逛书柜读了一篇随笔' if valid and payload.get('source_mode') == 'essays' else f'翻看{owner}收藏夹中的一条收藏' if valid else '未取得新收藏')
        if event_type == EventType.LISTEN_MUSIC:
            excluded = {item['source_payload'].get('fingerprint') for item in seen_nodes if item['source_payload'].get('fingerprint')}
            excluded |= {_song_identity_key(item.get('source_payload')) for item in seen_nodes if _song_identity_key(item.get('source_payload'))}
            activity = self._required_activity(activity_id)
            autonomous = is_autonomous_run(self._required_run(activity['run_id']))
            pick_kwargs = {'exclude_fingerprints': excluded}
            from .context_projection import activity_history_scope, activity_experience
            scope = activity_history_scope(self.store, activity)
            params = inspect.signature(handler.pick_and_fetch_song).parameters
            if 'activity_context' in params or any((p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())):
                pick_kwargs['activity_context'] = {'activity_id': activity_id, 'activity_reason': activity.get('reason') or '', 'activity_created_at': activity.get('created_at') or '', 'earlier_experiences': scope['wander_predecessor_text'], 'exclude_wander_activity_ids': scope['exclude_wander_activity_ids'], 'provided_node_reflections': scope['provided_wander_node_reflections'], 'current_experience': activity_experience(activity, seen_nodes, include_node_reflections=True)}
            if autonomous:
                if 'recent_music' in params or any((p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())):
                    from .music_context_scope import current_scope
                    music_scope = current_scope(self._required_run(activity['run_id'])['session_id'])
                    if 'activity_context' in pick_kwargs:
                        pick_kwargs['activity_context']['music_context_scope'] = music_scope.facts()
                    pick_kwargs['recent_music'] = self.store.list_music_context_facts(session_id=music_scope.session_id, active_date=music_scope.active_date, completed_since=music_scope.completed_since, exclude_activity_ids=scope['exclude_wander_activity_ids'])
            else:
                try:
                    excluded |= self.store.list_recent_listen_music_fingerprints(days=3)
                    excluded |= self.store.list_recent_listen_music_identities(days=3)
                except Exception:
                    pass
            payload = await handler.pick_and_fetch_song(**pick_kwargs)
            picker_audit = {}
            try:
                picker_audit = handler.get_last_selection_audit()
            except Exception:
                picker_audit = {}
            if payload and (payload.get('fingerprint') in excluded or _song_identity_key(payload) in excluded):
                return NodeExecutionResult(NodeExecutionStatus.FAILED, summary='选定歌曲命中本轮或近期去重记录', payload={**payload, 'selection_audit': picker_audit} if picker_audit else payload, error='duplicate_recent_song', retry_safe=False)
            valid = bool(payload and payload.get('fingerprint') and payload.get('title') and payload.get('artist') and (str(payload.get('artist')).casefold() not in {'未知', 'unknown', '?'}) and payload.get('netease_song_id'))
            if not valid:
                audit_error = str(picker_audit.get('error') or '').strip()
                audit_payload = picker_audit.get('payload') if isinstance(picker_audit, dict) else {}
                failed_payload = dict(payload or {})
                if picker_audit:
                    failed_payload['selection_audit'] = picker_audit
                return NodeExecutionResult(NodeExecutionStatus.FAILED, summary=f'听歌材料失败：{audit_error}' if audit_error else '未取得可真实播放的歌曲材料', payload=failed_payload, error=audit_error or 'missing_reliable_song_material', retry_safe=False)
            payload.setdefault('selected_by', 'K_autonomous')
            payload.setdefault('selection_source', 'wander_llm')
            from .music_listening import prepare
            activity = self._required_activity(activity_id)
            run = self._required_run(activity['run_id'])
            return await prepare(payload, handler=handler, clock=self.clock, checkpoint=lambda result: self._finish(run, self._required_activity(activity_id), self._required_node(node_id), result), interrupted=lambda: bool(self._required_activity(activity_id)['interrupt_requested']), playback_enabled=self._music_playback_is_enabled(), playback_target=self._music_playback_target())
        return NodeExecutionResult(NodeExecutionStatus.SKIPPED, summary='当前没有该事件的安全单节点执行器', error='unsupported_event')

    def _music_playback_is_enabled(self) -> bool:
        """Read the live ritual setting without caching it between nodes."""
        if self.music_playback_enabled is not None:
            try:
                return bool(self.music_playback_enabled())
            except Exception:
                return False
        return bool(self._read_setting('listen_music_playback', False))

    def _music_playback_target(self) -> str:
        value = self._read_setting('listen_music_playback_device', 'computer')
        return str(value or 'computer').strip().lower()

    def _read_setting(self, key: str, default: Any=None) -> Any:
        if self.get_setting is not None:
            try:
                value = self.get_setting(key)
                return default if value is None else value
            except Exception:
                return default
        try:
            from settings_manager import get_setting
            value = get_setting(key)
            return default if value is None else value
        except Exception:
            return default

    async def confirm_external_completion(self, node_id: str, signal: str) -> NodeExecutionResult:
        if signal != 'playback_finished':
            raise ValueError('unsupported completion signal')
        node = self._required_node(node_id)
        activity = self._required_activity(node.activity_id)
        if EventType(activity['activity_type']) != EventType.LISTEN_MUSIC:
            raise ValueError('external completion is only supported for music nodes')
        if node.state != NodeState.RUNNING:
            raise ValueError('node must still be running')
        if node.execution_status != NodeExecutionStatus.WAITING_EXTERNAL.value:
            raise ValueError('node is not waiting for external completion')
        from .music_listening import is_listening
        if is_listening({'source_payload': node.source_payload}):
            raise ValueError('listening completes at its persisted song-length deadline')
        run = self._required_run(activity['run_id'])
        title = node.source_payload.get('title', '未命名')
        return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.SUCCEEDED, completion_signal=signal, summary=f'《{title}》播放已完成', payload=node.source_payload, side_effect_refs=node.side_effect_refs, retry_safe=False))

    async def poll_external_completion(self, node_id: str) -> NodeExecutionResult:
        """Resolve a waiting music node from the music authority's receipt.

        Estimated duration only controls when polling starts.  It can never
        turn into completion evidence by itself.
        """
        node = self._required_node(node_id)
        activity = self._required_activity(node.activity_id)
        if activity['activity_type'] in {EventType.VISIT_WORKSHOP.value, EventType.WORKSHOP_CREATE.value, EventType.WORKSHOP_HISTORY.value, EventType.WORKSHOP_CONTINUE.value}:
            if node.state != NodeState.RUNNING or node.execution_status != NodeExecutionStatus.WAITING_EXTERNAL.value:
                raise ValueError('workshop node must still be waiting')
            from .workshop_waiting import poll
            result = poll(node)
            if result.status == NodeExecutionStatus.WAITING_EXTERNAL:
                return result
            return self._finish(self._required_run(activity['run_id']), activity, node, result)
        if EventType(activity['activity_type']) != EventType.LISTEN_MUSIC:
            raise ValueError('external completion is only supported for music nodes')
        if node.state != NodeState.RUNNING:
            raise ValueError('node must still be running')
        if node.execution_status != NodeExecutionStatus.WAITING_EXTERNAL.value:
            raise ValueError('node is not waiting for external completion')
        from .music_listening import is_listening, poll
        if is_listening({'source_payload': node.source_payload}):
            result = poll(self.store.get_node(node_id), now=self.clock(), interrupted=bool(activity['interrupt_requested']))
            if result.status == NodeExecutionStatus.WAITING_EXTERNAL:
                return result
            return self._finish(self._required_run(activity['run_id']), activity, node, result)
        session_id = str(node.side_effect_refs.get('music_session_id') or '').strip()
        song_id = str(node.side_effect_refs.get('netease_song_id') or '').strip()
        receipt: dict[str, Any]
        try:
            if not session_id:
                receipt = {'state': 'missing'}
            elif self.music_playback_receipt is not None:
                resolved = self.music_playback_receipt(session_id, song_id)
                if inspect.isawaitable(resolved):
                    resolved = await resolved
                receipt = resolved if isinstance(resolved, dict) else {'state': 'unavailable'}
            else:
                from music_system.service import get_service
                receipt = get_service().playback_receipt(session_id, song_id)
        except Exception:
            logger.warning('漫想听歌完成回执暂不可读', exc_info=True)
            receipt = {'state': 'unavailable'}
        state = str(receipt.get('state') or 'unavailable').strip().lower()
        if state == 'finished':
            return await self.confirm_external_completion(node_id, 'playback_finished')
        deadline = str(node.side_effect_refs.get('completion_deadline_at') or '')
        deadline_elapsed = False
        if deadline:
            try:
                deadline_elapsed = self.clock() >= datetime.fromisoformat(deadline)
            except (TypeError, ValueError):
                deadline_elapsed = True
        terminal_states = {'ended', 'external', 'failed', 'superseded', 'paused_timeout', 'missing', 'song_mismatch'}
        if state not in terminal_states and (not deadline_elapsed):
            return NodeExecutionResult(NodeExecutionStatus.WAITING_EXTERNAL, completion_signal=f'playback_{state}', summary='仍在等待设备确认这首歌真正播放完毕', payload=node.source_payload, side_effect_refs=node.side_effect_refs, retry_safe=False)
        run = self._required_run(activity['run_id'])
        payload = dict(node.source_payload)
        payload['playback_receipt_state'] = state
        error = f'playback_interrupted:{state}' if state in terminal_states else 'playback_completion_unconfirmed'
        return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.FAILED, summary='共同播放已经结束，但没有取得这首歌完整播完的设备收据', payload=payload, side_effect_refs=node.side_effect_refs, error=error, retry_safe=False, memory_material={'version': 1, 'kind': 'observed_material', 'evidence_refs': ['netease_song_id', 'title', 'artist']}))

    @staticmethod
    def _material_result(valid: bool, payload: Any, summary: str) -> NodeExecutionResult:
        return NodeExecutionResult(NodeExecutionStatus.SUCCEEDED if valid else NodeExecutionStatus.FAILED, completion_signal='node_material_ready' if valid else '', summary=summary, payload=payload if isinstance(payload, dict) else {}, error='' if valid else 'missing_required_material', retry_safe=False)

    def _finish(self, run: dict[str, Any], activity: dict[str, Any], node: WanderNode, result: NodeExecutionResult) -> NodeExecutionResult:
        node.execution_status = result.status.value
        node.completion_signal = result.completion_signal
        node.source_summary = result.summary
        node.source_payload = dict(result.payload)
        if result.memory_material is not None:
            node.source_payload['memory_material'] = result.memory_material
        elif result.status == NodeExecutionStatus.FAILED:
            node.source_payload['memory_material'] = {'version': 1, 'kind': 'none', 'origin': 'execution_receipt'}
        node.side_effect_refs = result.side_effect_refs
        node.execution_error = result.error
        node.retry_safe = result.retry_safe
        self.store.save_node(node)
        self.store.record_decision(run_id=run['run_id'], activity_id=activity['activity_id'], node_id=node.node_id, phase=DecisionPhase.NODE_EXECUTION, model=result.audit_model, recipe='node_execution_adapter', input_context=result.audit_input or {'event_type': activity['activity_type'], 'audit_gap': 'legacy handler internals do not expose raw LLM metadata'}, raw_output=result.audit_raw_output, reasoning=result.audit_reasoning, parsed_output={'status': result.status.value, 'completion_signal': result.completion_signal, 'summary': result.summary, 'payload': result.payload, 'side_effect_refs': result.side_effect_refs, 'error': result.error, 'failure_stage': result.payload.get('failure_stage', '') if isinstance(result.payload, dict) else '', 'public_error': result.payload.get('public_error', '') if isinstance(result.payload, dict) else ''}, status=result.status.value, error=result.error, prompt_tokens=self._usage_total(result.audit_usage, 'prompt_tokens', 'input_tokens'), completion_tokens=self._usage_total(result.audit_usage, 'completion_tokens', 'output_tokens'), cache_tokens=self._usage_total(result.audit_usage, 'cached_tokens', 'cache_read_input_tokens'))
        return result

    @staticmethod
    def _usage_total(usage: dict[str, Any], *names: str) -> int:
        groups = usage.get('calls') if isinstance(usage, dict) else None
        groups = groups if isinstance(groups, list) else [usage]
        total = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            for name in names:
                if name in group:
                    try:
                        total += int(group[name] or 0)
                    except (TypeError, ValueError):
                        pass
                    break
        return total

    def recover_publication(self, node_id: str) -> NodeExecutionResult:
        """Recover already committed facts only, without phone or model actions."""
        import hashlib
        from xhs_publish.store import PublishStore, receipt
        from xhs_publish.service import experience
        node = self._required_node(node_id)
        activity = self._required_activity(node.activity_id)
        run = self._required_run(activity['run_id'])
        task = node.side_effect_refs.get('publication_task_id') or hashlib.sha256((run['session_id'] + '\nwander:' + activity['activity_id']).encode()).hexdigest()
        saved = PublishStore().get(task, run['session_id'])
        value = receipt(saved) if saved else None
        good = bool(value and value['status'] in {'published', 'previewed'})
        return self._finish(run, activity, node, NodeExecutionResult(NodeExecutionStatus.SUCCEEDED if good else NodeExecutionStatus.FAILED, summary=experience(value) if value else '小红书创作中断，尚无可恢复的发布回执。', payload={'xhs_publication': value} if value else {'status': 'composition_interrupted'}, side_effect_refs={'publication_task_id': value['task_id']} if value else {}, completion_signal=value['status'] if value else 'composition_interrupted', error='' if good else 'publication_recovered_without_replay', retry_safe=False))

    def _required_node(self, node_id: str) -> WanderNode:
        data = self.store.get_node(node_id)
        if data is None:
            raise ValueError('node not found')
        data['state'] = NodeState(data['state'])
        return WanderNode(**data)

    def _required_activity(self, activity_id: str) -> dict[str, Any]:
        activity = self.store.get_activity(activity_id)
        if activity is None:
            raise ValueError('activity not found')
        return activity

    def _required_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError('run not found')
        return run
