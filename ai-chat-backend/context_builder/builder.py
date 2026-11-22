"""
ContextBuilder — 按 Recipe 组装上下文。

用法：
    # 按 Recipe 名（90% 场景）
    result = await ContextBuilder.build("FULL_CHAT", session_id=sid, user_message=msg, ...)

    # 方法链（10% 定制场景）
    builder = ContextBuilder(session_id)
    builder.identity(persona=persona, affect_self=mood)
    builder.situational(status=True, away=True)
    result = await builder.build()

灰度控制：
    MIRROW_USE_CONTEXT_BUILDER=1 → 使用 Builder
    MIRROW_CONTEXT_DIFF=1 → 并行运行新旧，差异日志
"""
import os
import logging
import asyncio
import time
from copy import deepcopy
from datetime import date, datetime
from typing import Dict, Any, Optional, List
from context_builder.ingredients import get_ingredient, estimate_tokens
from context_builder.ingredients import (
    build_timeline_anchor, build_time_section, build_user_status_section,
    build_period_section, build_sentinel_snapshot, build_heart_rate_section,
)
from context_builder.recipes import get_recipe, Recipe, RecipeSection
from context_builder.assembly import ZONE_CONTEXT, ZONE_HISTORY, ZONE_PREFIX, ZONE_TURN
from context_builder.guides import guide_for, SELF_GUIDED
from context_builder.text_utils import build_tool_history_fact, strip_internal_history_markers
from message_roles import is_ui_only_role
from context_builder.notification_projection import project_notification_message
logger = logging.getLogger(__name__)

class ContextResult:
    """构建结果——兼容现有 ContextResult 接口"""

    def __init__(self):
        self.system_content: str = ''
        self.dynamic_content: str = ''
        self.dynamic_messages: list = []
        self.tail_messages: list = []
        self.context_messages: list = []
        self.formatted_messages: list = []
        self.memories: list = []
        self.stats: dict = {}
        self.sections: dict = {}
        self.section_groups: list = []
        self.call_id: str = ''
        self.one_shot_claims: list = []
        self.memory_trace_id: str = ''
_STABLE_SECTIONS = {'other_core', 'cognitive_core', 'persona', 'persona_anchor', 'active_evolutions', 'timeline_anchor', 'message_markers', 'tool_descriptions', 'user_profile', 'day_before_diary', 'yesterday', 'gap_diaries', 'important_events'}

def dynamic_last_enabled() -> bool:
    """灰度开关：MIRROW_CONTEXT_DYNAMIC_LAST=1 时把动态块移到对话历史之后。"""
    return os.environ.get('MIRROW_CONTEXT_DYNAMIC_LAST') == '1'

def _period_active_today() -> bool:
    """读取日历权威字段，供 recipe 条件门控使用。

    ``period_active`` 是一个条件 flag，不应在未传入时默认恒真：否则
    带有 ``condition="period_active"`` 的 recipe 会在非生理期也尝试挂载
    生理期段。具体文案仍由 ``build_period_section`` 这个 ingredient 生成。
    """
    try:
        from calendar_manager.database import get_period_info_today
        info = get_period_info_today()
        return bool(info and info.get('active'))
    except Exception:
        return False
_DYNAMIC_LAST_RECIPES = {'FULL_CHAT', 'RIFT_PLAY'}
_AFFECT_SECTIONS = {'emotion_self', 'emotion_resonance', 'open_loops_v2'}

def resolve_section_placement(section: RecipeSection) -> str:
    """Resolve one section's zone while recipes migrate to explicit placement."""
    if section.placement:
        return section.placement
    if section.ingredient in _STABLE_SECTIONS:
        return ZONE_PREFIX
    if section.group == '对话注入':
        return ZONE_HISTORY
    if section.group == '输出合同':
        return ZONE_TURN
    return ZONE_CONTEXT

def _affect_context_mode(kwargs: dict) -> str:
    explicit = kwargs.get('affect_context_mode')
    if explicit in {'shadow', 'enabled', 'disabled'}:
        return explicit
    try:
        from affect import get_affect_service
        settings = get_affect_service().settings()
        mode = settings.get('engine_mode', settings.get('context_mode', 'shadow'))
        return mode if mode in {'shadow', 'enabled', 'disabled'} else 'shadow'
    except Exception:
        return 'shadow'

class ContextBuilder:
    """上下文构建器"""

    def __init__(self, session_id: str=''):
        self.session_id = session_id
        self._recipe_name: str = ''
        self._recipe_key: str = ''
        self._scenario_override: str = ''
        self._system_content: str = ''
        self._dynamic_content: str = ''
        self._dynamic_messages: list = []
        self._tail_messages: list = []
        self._context_messages: list = []
        self._extra_messages: list = []
        self._sections: dict = {}
        self._section_status: dict = {}
        self._kwargs: dict = {}
        self._affect_mode: str = 'shadow'
        self._affect_context_projection = None
        self._flags: dict = {}
        self._conversation_empty_today = False
        self._push_diary_kwargs = None
        self._one_shot_claims: list = []
        self._inspection_recipe = None
        self._ingredient_diagnostics = {}
        self._self_book_coverage = ()
        self._history_source_errors = set()
        self._memory_v2_attempted = False
        self._memory_v2_authoritative = False

    @classmethod
    async def build(cls, recipe_name: str, **kwargs) -> ContextResult:
        """按 Recipe 名构建上下文。"""
        recipe = get_recipe(recipe_name)
        if not recipe:
            raise ValueError(f'Unknown recipe: {recipe_name}')
        if recipe.pipeline != 'builder':
            raise RuntimeError(f"Recipe '{recipe_name}' ({recipe.name}) is marked pipeline={recipe.pipeline}; it is maintained by its legacy manual consumer and cannot be built by ContextBuilder.")
        builder = cls(kwargs.get('session_id', ''))
        if any((section.ingredient == 'wander_handoff' for section in recipe.sections)):
            from wander_manager.activity_context import current, recipe_kwargs
            if current() is not None:
                handoff = recipe_kwargs(current())
                for key in ('exclude_wander_activity_ids', 'provided_wander_node_reflections'):
                    kwargs.setdefault(key, handoff[key])
        builder._recipe_key = recipe_name
        return await builder._build_from_recipe(recipe, **kwargs)

    def identity(self, persona: str='', mood: str='', anchor_cap: int=0, affect_self: str='') -> 'ContextBuilder':
        """注入身份层。"""
        if persona:
            self._add('persona', persona)
        self._add('persona_anchor', '')
        self._add('timeline_anchor', build_timeline_anchor())
        self._add('time', build_time_section())
        return self

    def situational(self, include: List[str]=None, exclude: List[str]=None) -> 'ContextBuilder':
        """注入情境层（状态/生理期/离开/哨兵/心率等）。"""
        all_items = {'user_status': build_user_status_section, 'period': build_period_section, 'away': lambda: None, 'sentinel_snapshot': build_sentinel_snapshot, 'heart_rate': build_heart_rate_section}
        for name, fn in all_items.items():
            if include and name not in include:
                continue
            if exclude and name in exclude:
                continue
            result = fn() if callable(fn) else None
            if result:
                self._add(name, result)
        return self

    def knowledge(self, memory_top_k: int=10, memory_cap: int=0, world_book: bool=True) -> 'ContextBuilder':
        """注入知识层（记忆/世界书）。"""
        self._kwargs['memory_top_k'] = memory_top_k
        self._kwargs['memory_cap'] = memory_cap
        self._flags['world_book'] = world_book
        return self

    def history(self, **kwargs) -> 'ContextBuilder':
        self._kwargs.update(kwargs)
        self._flags['inject_history'] = True
        return self

    def conversation(self) -> 'ContextBuilder':
        self._flags['include_conversation'] = True
        return self

    def custom_section(self, name: str, content: str) -> 'ContextBuilder':
        self._add(name, content)
        return self

    async def _build_from_recipe(self, recipe: Recipe, **kwargs) -> ContextResult:
        """遍历 Recipe.sections，逐个调用 ingredients。"""
        build_started = time.perf_counter()
        self._affect_context_projection = None
        self._recipe_name = recipe.name
        self._inspection_recipe = recipe
        self._scenario_override = kwargs.pop('_scenario', '') or ''
        _time_hint_val = kwargs.pop('time_awareness_hint', None)
        _period_active = kwargs.pop('period_active', None)
        self._flags = {'user_status_not_idle': kwargs.pop('user_status_not_idle', True), 'period_active': _period_active if _period_active is not None else _period_active_today(), 'away': kwargs.pop('away', False), 'night_mode': kwargs.pop('night_mode', False), 'phone_browse': kwargs.pop('phone_browse', False), 'music_cochlea': kwargs.pop('music_cochlea', False), 'wander_active': kwargs.pop('is_wander_active', False), 'is_new_topic': kwargs.pop('is_new_topic', False), 'inject_yesterday': kwargs.pop('inject_yesterday', False), 'date_referenced': kwargs.pop('date_referenced', False), 'time_hint': _time_hint_val is not None, 'has_temp_summary': kwargs.pop('has_temp_summary', False), 'rift_game_context': bool(kwargs.get('rift_game_context_text'))}
        self._kwargs = kwargs
        self._affect_mode = _affect_context_mode(kwargs)
        if _time_hint_val:
            self._kwargs['time_awareness_hint'] = _time_hint_val
        for section in recipe.sections:
            if section.condition and (not self._flags.get(section.condition)):
                self._sections[section.ingredient] = ''
                self._section_status[section.ingredient] = {'status': 'gated', 'condition': section.condition, 'placement': resolve_section_placement(section)}
                continue
            ingredient_started = time.perf_counter()
            try:
                content = await self._call_ingredient(section)
                if section.ingredient == 'self_book':
                    self._self_book_coverage = getattr(content, 'sources', ())
                if section.ingredient in _AFFECT_SECTIONS and self._affect_mode != 'enabled':
                    text = str(content).strip() if content else ''
                    self._sections[section.ingredient] = text
                    self._section_status[section.ingredient] = {'status': 'shadow_preview' if self._affect_mode == 'shadow' else 'disabled', 'injected': False, 'placement': resolve_section_placement(section)}
                    continue
                if content and isinstance(content, list):
                    placement = resolve_section_placement(section)
                    self._add(section.ingredient, content, placement=placement)
                    self._sections[section.ingredient] = ''
                    self._section_status[section.ingredient] = {'status': 'interleaved', 'count': len(content), 'placement': placement}
                    if section.ingredient == 'status_changes':
                        history_count = sum((bool(item.get('_status_frozen')) for item in content))
                        self._section_status[section.ingredient].update(history_count=history_count, context_count=len(content) - history_count, placement=ZONE_HISTORY if history_count == len(content) else ZONE_CONTEXT)
                elif content and str(content).strip():
                    text = str(content).strip()
                    guide = ''
                    if section.guide is not None:
                        guide = section.guide
                    elif section.ingredient not in SELF_GUIDED:
                        guide = guide_for(section.ingredient, self._kwargs)
                    if guide:
                        text = f'{guide}\n{text}'
                    placement = resolve_section_placement(section)
                    use_context_zone = placement == ZONE_CONTEXT and (section.placement is not None or self._recipe_key not in _DYNAMIC_LAST_RECIPES or (dynamic_last_enabled() and self._recipe_key in _DYNAMIC_LAST_RECIPES))
                    if use_context_zone:
                        self._dynamic_content += text + '\n\n'
                        from .provider_presentation import SECTION_LABELS, snapshot_message
                        self._dynamic_messages.append(snapshot_message(section.ingredient, text) if section.ingredient in SECTION_LABELS else {'role': 'system', 'content': text, '_section': section.ingredient})
                        self._sections[section.ingredient] = text
                        self._section_status[section.ingredient] = {'status': 'dynamic', 'placement': placement}
                    else:
                        self._add(section.ingredient, text, placement=placement if section.placement is not None else None)
                        self._section_status[section.ingredient] = {'status': 'ok', 'placement': placement}
                else:
                    self._sections[section.ingredient] = str(content) if content else ''
                    self._section_status[section.ingredient] = {'status': 'empty', 'placement': resolve_section_placement(section)}
                    if self._ingredient_diagnostics.get(section.ingredient, {}).get('error_type'):
                        self._section_status[section.ingredient] = {'status': 'error', 'placement': resolve_section_placement(section)}
            except Exception:
                logger.warning(f"Ingredient '{section.ingredient}' failed, skipping")
                self._sections[section.ingredient] = ''
                self._section_status[section.ingredient] = {'status': 'error', 'placement': resolve_section_placement(section)}
            finally:
                self._section_status.setdefault(section.ingredient, {})['duration_ms'] = round((time.perf_counter() - ingredient_started) * 1000, 1)
        if self._recipe_key == 'FULL_CHAT' and (not self._memory_v2_attempted):
            await self._observe_memory_v2_shadow()
        if self._recipe_key == 'FULL_CHAT':
            await self._prepare_social_feed_reminder()
        result = self._finalize()
        result.stats['build_duration_ms'] = round((time.perf_counter() - build_started) * 1000, 1)
        return result

    async def _observe_memory_v2_shadow(self) -> None:
        name = 'memory_v2_shadow'
        try:
            from memory_v2.context_shadow import observe_context_shadow_from_environment
            from time_utils import now_date
            observation = await asyncio.to_thread(observe_context_shadow_from_environment, str(self._kwargs.get('user_message') or ''), reference_date=date.fromisoformat(now_date()), current_message_id=str(self._kwargs.get('current_message_id') or ''), session_id=self.session_id)
            status = str(observation.get('status') or 'error')
            self._sections[name] = ''
            self._section_status[name] = {**observation, 'status': 'shadow_observed' if status == 'observed' else status, 'injected': False}
            self._ingredient_diagnostics[name] = {'source': 'memory_v2.context_shadow.observe_context_shadow_from_environment'}
            self._memory_trace_id = str(observation.get('trace_id') or '')
        except Exception as exc:
            self._sections[name] = ''
            self._section_status[name] = {'status': 'error', 'injected': False, 'error_type': type(exc).__name__}

    async def _recall_memory_v2_enabled(self) -> Optional[str]:
        """Return a V2 replacement, or None when legacy memory must remain."""
        try:
            from memory_v2.context_shadow import memory_v2_context_mode, recall_context_from_environment
            if memory_v2_context_mode() != 'enabled':
                return None
            self._memory_v2_attempted = True
            from time_utils import now_date
            recall_kwargs = {'reference_date': date.fromisoformat(now_date()), 'current_message_id': str(self._kwargs.get('current_message_id') or ''), 'session_id': self.session_id}
            excluded_memory_ids = tuple(self._kwargs.get('exclude_memory_message_ids') or ())
            if excluded_memory_ids:
                recall_kwargs['exclude_message_ids'] = excluded_memory_ids
            observation, text = await asyncio.to_thread(recall_context_from_environment, str(self._kwargs.get('memory_query') or self._kwargs.get('user_message') or self._kwargs.get('context_query') or ''), **recall_kwargs)
            status = str(observation.get('status') or 'error')
            self._sections['memory_v2'] = ''
            self._section_status['memory_v2'] = dict(observation)
            self._ingredient_diagnostics['memory_v2'] = {'source': 'memory_v2.context_shadow.recall_context_from_environment'}
            self._memory_trace_id = str(observation.get('trace_id') or '')
            if status == 'recalled':
                self._memory_v2_authoritative = True
                return text
            return None
        except Exception as exc:
            self._memory_v2_attempted = True
            self._sections['memory_v2'] = ''
            self._section_status['memory_v2'] = {'status': 'error', 'injected': False, 'error_type': type(exc).__name__}
            return None

    async def _prepare_social_feed_reminder(self) -> None:
        """Peek AI's unread Human feed notices for a FULL_CHAT tail message.

        Feed notifications are deliberately kept out of the normal recipe
        sections: they are a final, independently ordered system message and
        are not eligible for GROUP_CHAT, PUSH, or other recipes.
        """
        try:
            store = self._kwargs.get('social_feed_store')
            if store is None:
                from social_feed import get_social_feed_store
                store = get_social_feed_store()
            notices = await store.unannounced_notifications('k', limit=100)
            notices = [item for item in notices if str(item.get('actor') or '').strip().lower() == 'human' and str(item.get('kind') or '').strip().lower() in {'moment', 'comment', 'like', 'mention'} and str(item.get('id') or '').strip()][:100]
            if not notices:
                return
            labels = {'moment': '发帖', 'comment': '评论', 'like': '点赞', 'mention': '艾特'}
            kinds = []
            for kind in ('moment', 'comment', 'like', 'mention'):
                count = sum((1 for item in notices if item.get('kind') == kind))
                if count:
                    kinds.append(labels[kind] if count == 1 else f'{labels[kind]}×{count}')
            kind_text = '、'.join(kinds) or '新动态'
            count_text = f'（共{len(notices)}条）' if len(notices) > 1 else ''
            event_times = []
            for item in notices:
                try:
                    event_times.append(datetime.fromtimestamp(float(item.get('created_at'))).astimezone())
                except (TypeError, ValueError, OSError, OverflowError):
                    continue
            if event_times:
                first_time = min(event_times).strftime('%Y-%m-%d %H:%M %z')
                last_time = max(event_times).strftime('%Y-%m-%d %H:%M %z')
                time_text = first_time if first_time == last_time else f'{first_time} 至 {last_time}'
                time_fact = f'发生时间：{time_text}。'
            else:
                time_fact = '发生时间未取得。'
            content = f'[尚未告知的朋友圈更新]{time_fact}类型：{kind_text}{count_text}。这是尚未向你呈现的更新。'
            self._tail_messages.append({'role': 'system', 'content': content, '_section': 'social_feed_reminder', '_ts': '9999-12-31T23:59:59'})
            notification_ids = [str(item['id']) for item in notices]
            current_message_id = str(self._kwargs.get('current_message_id') or '').strip()
            if current_message_id:
                self._one_shot_claims.append({'kind': 'social_feed_announcements', 'stable_id': current_message_id, 'ids': notification_ids, 'store': store, 'recipient': 'k', 'announce_source': 'main_chat', 'announce_source_id': current_message_id})
            self._sections['social_feed_reminder'] = content
            self._section_status['social_feed_reminder'] = {'status': 'peeked', 'count': len(notification_ids), 'notification_ids': notification_ids}
        except Exception as exc:
            logger.debug('social feed reminder peek skipped: %s', type(exc).__name__)

    def _record_history_read_failure(self, ingredient: str, source: str, error_type: str) -> None:
        safe_type = error_type if error_type in {'RuntimeError', 'TypeError', 'ImportError', 'OSError', 'TimeoutError', 'OperationalError', 'DatabaseError'} else 'Exception'
        self._ingredient_diagnostics.setdefault(ingredient, {}).update({'error_type': safe_type, 'error_source': source})
        self._history_source_errors.add(source)
        logger.warning('History source read failed error_type=%s source=%s', safe_type, source)

    async def _call_ingredient(self, section: RecipeSection) -> Optional[str]:
        """调用单个 ingredient 函数。"""
        name = section.ingredient
        params = dict(section.params)
        if name == 'persona':
            persona = self._kwargs.get('persona', '')
            cap = int(params.get('cap', 0) or 0)
            if cap > 0 and len(persona) > cap:
                cut = persona[:cap].rfind('。')
                if cut < cap // 2:
                    cut = persona[:cap].rfind('\n')
                if cut > 0:
                    persona = persona[:cut + 1]
                else:
                    persona = persona[:cap]
            return persona
        if name == 'persona_anchor':
            persona = self._kwargs.get('persona', '')
            if persona:
                from truncation_config import get_truncation_limit
                cap = get_truncation_limit('persona_anchor_cap')
                anchor = persona.split('。')[0][:cap] if cap > 0 else persona.split('。')[0]
                from .provider_presentation import covered_persona_anchor
                return '' if covered_persona_anchor(self._sections.get('persona', ''), anchor) else anchor
            return ''
        if name == 'open_loops' and self._affect_mode == 'enabled' and (self._inspection_recipe is not None) and any((s.ingredient == 'open_loops_v2' for s in self._inspection_recipe.sections)):
            return ''
        if name in {'emotion_self', 'emotion_resonance'}:
            try:
                from affect import get_affect_service
                service = get_affect_service()
                if self._affect_context_projection is None:
                    self._affect_context_projection = await asyncio.to_thread(service.get_v2_projection, session_id=self.session_id)
                renderer = 'renderer_self' if name == 'emotion_self' else 'renderer_resonance'
                return self._affect_context_projection[renderer][:700]
            except Exception as exc:
                logger.debug('affect emotion section unavailable: %s', type(exc).__name__)
                return ''
        if name == 'open_loops_v2':
            try:
                from affect import get_affect_service
                from affect.open_loop import select_open_loops
                service = get_affect_service()
                settings = service.settings()
                selected = select_open_loops(recipe=self._recipe_key or self._recipe_name, user_message=str(self._kwargs.get('user_message') or self._kwargs.get('context_query') or ''), mode=self._affect_mode, char_budget=int(settings.get('open_loop_char_budget', 3200)), foreground_limit=int(settings.get('open_loop_foreground_limit', 6)), related_limit=int(settings.get('open_loop_related_limit', 3)), due_limit=int(settings.get('open_loop_due_limit', 2)), trace_id=str(self._kwargs.get('trace_id') or ''), turn_id=str(self._kwargs.get('current_message_id') or ''))
                self._kwargs['affect_open_loop_selection'] = selected
                return selected.get('preview_text', '')
            except Exception as exc:
                logger.debug('affect open loop section unavailable: %s', type(exc).__name__)
                return ''
        if name == 'night_mode':
            return self._kwargs.get('night_mode_text', '')
        if name == 'phone_browse':
            return self._kwargs.get('phone_browse_text', '')
        if name == 'rift_game_context':
            return self._kwargs.get('rift_game_context_text', '')
        if name == 'music_cochlea':
            fn = get_ingredient('music_cochlea')
            if fn:
                return fn()
            return ''
        if name == 'voice_cochlea':
            fn = get_ingredient('voice_cochlea')
            if not fn:
                return ''
            result = fn()
            try:
                from voice_manager.voice_cochlea import get_current_prosody_id
                prosody_id = get_current_prosody_id()
                if prosody_id and result:
                    self._one_shot_claims.append({'kind': 'prosody', 'stable_id': prosody_id})
            except Exception:
                pass
            return result
        if name == 'wander_mode':
            return self._kwargs.get('wander_mode_text', '')
        if name == 'wander_handoff':
            from wander_manager.activity_context import handoff_text
            return handoff_text()
        if name == 'wander_runtime':
            return self._kwargs.get('wander_runtime_text', '')
        if name == 'wander_predecessors':
            return self._kwargs.get('wander_predecessor_text', '')
        if name == 'push_current_state':
            frozen = self._kwargs.get('push_current_state_text', '')
            if frozen:
                return [{'role': 'system', 'content': str(frozen).strip(), '_ts': '9999-12-31T23:59:50', '_section': 'push_current_state'}]
        if name == 'sentinel_intent':
            fn = get_ingredient('sentinel_intent')
            text = fn(self._kwargs.get('sentinel_summary', ''), self._kwargs.get('sentinel_push_motivation', ''), self._kwargs.get('sentinel_history_evidence', [])) if fn else ''
            if not text:
                return []
            return [{'role': 'system', 'content': text, '_ts': '9999-12-31T23:59:59', '_section': 'sentinel_intent'}]
        if name == 'message_markers':
            from context_builder.ingredients import MESSAGE_MARKERS_TEXT
            return MESSAGE_MARKERS_TEXT
        if name == 'tool_descriptions':
            prebuilt = self._kwargs.get('tools_desc', '')
            if prebuilt:
                return prebuilt
            tools = self._kwargs.get('tools', {})
            fn = get_ingredient('tool_descriptions')
            if fn and tools:
                return fn(tools=tools)
            if fn:
                return fn()
        if name == 'user_profile':
            profile = self._kwargs.get('user_profile', '')
            if profile and profile.strip():
                return f'用户资料: {profile.strip()}'
            return ''
        if name == 'available_tools':
            return self._kwargs.get('tools_desc', '')
        if name == 'group_tool_ban':
            fn = get_ingredient('group_tool_ban')
            return fn() if fn else ''
        if name == 'round_awareness':
            fn = get_ingredient('round_awareness')
            rn = self._kwargs.get('round_num', 1)
            mr = self._kwargs.get('max_rounds', 1)
            sk = self._kwargs.get('start_with_k', True)
            return fn(rn, mr, sk) if fn else ''
        if name == 'day_before_diary':
            if 'day_before_diary' in self._kwargs:
                return self._kwargs.get('day_before_diary', '')
            dk = await self._resolve_push_diary()
            self._kwargs['day_before_date'] = dk.get('day_before', '')
            return dk.get('day_before_diary', '')
        if name == 'gap_diaries':
            if 'gap_diaries' in self._kwargs:
                return self._kwargs.get('gap_diaries', '')
            dk = await self._resolve_push_diary()
            return dk.get('gap_diaries_text', '')
        if name == 'referenced_diary':
            return self._kwargs.get('referenced_diary', '')
        if name == 'important_events':
            return self._kwargs.get('important_events', '')
        if name == 'recent_messages':
            if 'recent_messages' in self._kwargs:
                error_type = self._kwargs.get('recent_messages_error_type')
                if error_type:
                    self._record_history_read_failure(name, 'recent_history', str(error_type))
                return self._kwargs.get('recent_messages') or ''
            history = await self._call_ingredient(RecipeSection('对话历史', 'conversation_messages', params=params))
            if self._ingredient_diagnostics.get('conversation_messages', {}).get('error_type'):
                self._ingredient_diagnostics[name] = dict(self._ingredient_diagnostics['conversation_messages'])
            return history
        if name == 'wander_event':
            return self._kwargs.get('wander_event', '')
        if name == 'user_status_context':
            return self._kwargs.get('user_status_context', '')
        if name == 'away_duration':
            return self._kwargs.get('away_duration', '')
        if name == 'task_info':
            return self._kwargs.get('task_info', '')
        if name == 'recent_wander':
            val = self._kwargs.get('recent_wander', '')
            current_id = str(self._kwargs.get('current_activity_id') or '').strip()
            current_text = str(self._kwargs.get('wander_activity_text') or '').strip()
            if val and current_text and (str(val).strip() == current_text):
                val = ''
            if val:
                return val
            try:
                from event_chronicle import get_global_chronicle
                from datetime import datetime as _dt
                chronicle = get_global_chronicle()
                today = _dt.now().strftime('%Y-%m-%d')
                msgs = chronicle.get_messages_by_date(today) or []
                n = int(params.get('n', 5))
                wanders = []
                for m in msgs:
                    if not m.get('is_wander'):
                        continue
                    mid = str(m.get('activity_id') or m.get('message_id') or m.get('id') or '').strip()
                    if current_id and mid == current_id:
                        continue
                    mtext = str(m.get('content') or '').strip()
                    if current_text and mtext and (mtext == current_text):
                        continue
                    wanders.append(m)
                wanders = wanders[-n:]
                if wanders:
                    lines = ['## 你今天已发过的漫想']
                    for m in wanders:
                        lines.append(f"- {(m.get('content') or '')[:150]}")
                    return '\n'.join(lines)
            except Exception:
                pass
            return ''
        if name == 'world_book_summaries':
            return self._kwargs.get('world_book_summaries', '')
        if name == 'memories':
            memory_v2_text = await self._recall_memory_v2_enabled()
            if memory_v2_text is not None:
                return memory_v2_text
            mems = self._kwargs.get('memories', '')
            legacy_loader = self._kwargs.get('legacy_memory_loader')
            if not mems and callable(legacy_loader):
                mems = await legacy_loader()
                self._kwargs['memories'] = mems
            if isinstance(mems, list) and mems:
                top_k = int(params.get('top_k', 10))
                anh_parts = []
                self_parts = []
                neighborhood_parts = []
                anh_i = self_i = 0
                for mem in mems[:top_k]:
                    if hasattr(mem, 'source') and mem.source == 'conversation':
                        continue
                    md = getattr(mem, 'metadata', {}) or {}
                    content = getattr(mem, 'content', '') if hasattr(mem, 'content') else str(mem)
                    if md.get('is_neighborhood'):
                        label = md.get('neighborhood_scene_label', '')
                        summary = str(content).replace('\n', ' ').strip()[:100]
                        if summary:
                            neighborhood_parts.append(f'{label}: {summary}' if label else summary)
                        continue
                    subject = md.get('subject', '人类伙伴')
                    meta_parts = []
                    for key in ('time_label', 'emotion_label', 'domain'):
                        v = md.get('time_axis_label') or md.get(key, '') if key == 'time_label' else md.get(key, '')
                        if v:
                            meta_parts.append(', '.join(v) if isinstance(v, list) else str(v))
                    meta_str = ' · '.join(meta_parts)
                    content += str(md.get('source_hint') or '')
                    if subject == 'AI':
                        self_i += 1
                        self_parts.append(f'相关记忆 {self_i}: ({meta_str}) {content}' if meta_str else f'相关记忆 {self_i}: {content}')
                    else:
                        anh_i += 1
                        anh_parts.append(f'相关记忆 {anh_i}: ({meta_str}) {content}' if meta_str else f'相关记忆 {anh_i}: {content}')
                if neighborhood_parts:
                    anh_parts.append('（记忆邻域·背景折叠：' + '；'.join(neighborhood_parts[:2]) + '）')
                blocks = []
                if anh_parts:
                    anh_header = guide_for('memories', self._kwargs)
                    if any((getattr(mem, 'metadata', {}).get('time_axis_label') for mem in mems[:top_k])):
                        anh_header += '\n## 时间轴回忆'
                    blocks.append(anh_header + '\n' + '\n'.join(anh_parts))
                if self_parts:
                    blocks.append(guide_for('self_memories', self._kwargs) + '\n' + '\n'.join(self_parts))
                return '\n\n'.join(blocks) if blocks else ''
            return str(mems) if mems else ''
        if name == 'yesterday':
            if any((key in self._kwargs for key in ('yesterday_compact', 'yesterday_conversation', 'yesterday_diary_fallback'))):
                return self._kwargs.get('yesterday_compact', '') or self._kwargs.get('yesterday_conversation', '') or self._kwargs.get('yesterday_diary_fallback', '')
            dk = await self._resolve_push_diary()
            self._kwargs['yesterday_conversation'] = dk.get('yesterday_conversation', '')
            self._kwargs['yesterday_diary_fallback'] = dk.get('yesterday_diary_fallback', '')
            self._kwargs['yesterday_date'] = dk.get('yesterday', '')
            self._kwargs['yesterday_compact'] = dk.get('yesterday_compact', '')
            return dk.get('yesterday_compact', '') or dk.get('yesterday_conversation', '') or dk.get('yesterday_diary_fallback', '')
        if name == 'yesterday_related':
            if 'yesterday_related' in self._kwargs:
                return self._kwargs.get('yesterday_related', '')
            dk = await self._resolve_push_diary()
            return dk.get('yesterday_related', '')
        if name == 'recent_activity':
            return self._kwargs.get('recent_activity_text', '')
        if name == 'away':
            return self._kwargs.get('away_info', '')
        if name == 'world_book':
            entries = self._kwargs.get('world_book_entries', '')
            if isinstance(entries, list) and entries:
                top_k = int(params.get('top_k', 3))
                lines = ['## 世界书参考']
                for entry in entries[:top_k]:
                    name = entry.get('name', '') if isinstance(entry, dict) else getattr(entry, 'name', '')
                    body = entry.get('body', '') or '' if isinstance(entry, dict) else getattr(entry, 'body', '') or ''
                    lines.append(f'### {name}')
                    lines.append(str(body))
                return '\n'.join(lines)
            return str(entries) if entries else ''
        if name == 'self_book':
            include_interests = bool(params.get('include_interests'))
            entries = self._kwargs.get('self_book_entries', '')
            if isinstance(entries, list) and entries:
                from .book_projection import render_self_book
                if include_interests:
                    from .book_projection import with_interest_overview
                    entries = with_interest_overview(entries, entries[:int(params.get('top_k', 3))])
                return render_self_book(entries, top_k=len(entries) if include_interests else int(params.get('top_k', 3)), separator='\n')
            if entries:
                return str(entries)
            query = self._kwargs.get('context_query', '') or self._kwargs.get('user_message', '')
            fn = get_ingredient('self_book')
            if fn and (query or include_interests):
                extra = {'include_interests': True} if include_interests else {}
                return fn(query, top_k=int(params.get('top_k', 3)), **extra)
            return ''
        if name == 'music_experiences':
            from cognition.music import context
            return context(params.get('subject', 'k'), provided_node_reflections=self._kwargs.get('provided_wander_node_reflections'))
        if name == 'experience_recall':
            from experience_recall import auto_context
            query = str(self._kwargs.get('user_message') or '')
            return await asyncio.to_thread(auto_context, query) if query else ''
        if name == 'conversation_messages':
            mems = self._kwargs.get('conversation_messages', [])
            if 'conversation_messages' not in self._kwargs:
                try:
                    from event_chronicle import get_global_chronicle
                    from datetime import datetime as _dt
                    chronicle = get_global_chronicle()
                    _active_day = ''
                    if self.session_id:
                        try:
                            from summary import topic_state_machine
                            _ts = await topic_state_machine.get_topic_status(self.session_id)
                            _start = _ts.get('start_time') if _ts else ''
                            if _start:
                                _active_day = str(_start)[:10]
                        except Exception:
                            pass
                    if not _active_day:
                        _active_day = _dt.now().strftime('%Y-%m-%d')
                    raw = chronicle.get_messages_by_date(_active_day)
                    if not isinstance(raw, list):
                        raise TypeError('Chronicle history must be a list')
                    from context_builder.event_history_adapter import adapt_history_for_context
                    _sid = self.session_id or ''
                    _current_id = self._kwargs.get('current_message_id')
                    _day_msgs = adapt_history_for_context([r for r in raw if not (_sid and r.get('session_id') and (r.get('session_id') != _sid)) and (not (_current_id and r.get('message_id') == _current_id))], for_provider=True, compact_linked_sources=not bool(params.get('n', 0)), wander_level='daily' if self._recipe_key == 'FULL_CHAT' else 'experience')
                    if _day_msgs:
                        mems = []
                        for r in _day_msgs:

                            class _Msg:
                                pass
                            m = _Msg()
                            m.content = r.get('content', '')
                            _va_raw = r.get('voice_attachment')
                            _va = None
                            if _va_raw:
                                try:
                                    import json as _jv
                                    _va = _jv.loads(_va_raw) if isinstance(_va_raw, str) else _va_raw
                                except Exception:
                                    _va = None
                            m.metadata = {'role': r.get('role', 'user'), 'session_id': r.get('session_id') or _sid, 'timestamp': r.get('timestamp', ''), 'message_id': r.get('message_id', ''), 'is_wander': r.get('is_wander'), 'is_sentinel': r.get('is_sentinel'), 'is_reminder': r.get('is_reminder'), 'event_type': r.get('event_type'), 'notification_projection': r.get('notification_projection'), 'wander_identity': r.get('_wander_identity'), 'provider_internal': r.get('_provider_internal'), 'tool_summary': r.get('tool_summary'), 'tool_calls': r.get('tool_calls'), 'voice_attachment': _va}
                            mems.append(m)
                    try:
                        _today = _dt.now().strftime('%Y-%m-%d')
                        _today_msgs = raw if _active_day == _today else chronicle.get_messages_by_date(_today)
                        if not isinstance(_today_msgs, list):
                            raise TypeError('Chronicle history must be a list')
                        _has_user_today = any((r.get('role') == 'user' and r.get('event_type') not in {'ui_action:play_game', 'ui_action:visit_workshop'} and (not _sid or not r.get('session_id') or r.get('session_id') == _sid) for r in _today_msgs))
                        if not _has_user_today:
                            self._conversation_empty_today = True
                    except Exception as exc:
                        self._record_history_read_failure(name, 'today_presence', type(exc).__name__)
                except Exception as exc:
                    self._record_history_read_failure(name, 'recent_history', type(exc).__name__)
            try:
                _recent_n = int(params.get('n', 0) or 0)
            except (TypeError, ValueError):
                _recent_n = 0
            if _recent_n > 0 and isinstance(mems, list):
                start = max(0, len(mems) - _recent_n)
                if start > 0:
                    first_meta = getattr(mems[start], 'metadata', {}) if hasattr(mems[start], 'metadata') else {}
                    parent_meta = getattr(mems[start - 1], 'metadata', {}) if hasattr(mems[start - 1], 'metadata') else {}
                    if first_meta.get('event_type') == 'ambient_listening_reply' and parent_meta.get('event_type') == 'ambient_listening_observation':
                        start -= 1
                mems = mems[start:]
            if not mems:
                return ''
            from context_builder.ingredients import format_relative_time
            result = []
            excluded_activities = {str(activity_id).strip() for activity_id in self._kwargs.get('exclude_wander_activity_ids', ()) if str(activity_id).strip()}
            current_activity_id = str(self._kwargs.get('current_activity_id') or '').strip()
            if self._kwargs.get('exclude_wander_history') and current_activity_id:
                excluded_activities.add(current_activity_id)
            excluded_event_ids = {f'activity_event_{activity_id}' for activity_id in excluded_activities}
            from .conversation_history import render_history
            return render_history(mems, self._recipe_key, excluded_event_ids)
        if name == 'status_changes':
            raw = self._kwargs.get('raw_status_history', [])
            if raw:
                from .status_history import project_status_history
                return project_status_history(raw)
            return self._kwargs.get('status_changes', '')
        if name == 'sentinel_timeline':
            annotations = self._kwargs.get('sentinel_timeline_annotations', None)
            if annotations is None:
                try:
                    from silicon_perception.sentinel import get_sentinel
                    sentinel = get_sentinel()
                    annotations = sentinel.peek_timeline_annotations() if sentinel else []
                except Exception:
                    annotations = []
                if not annotations:
                    fn = get_ingredient('sentinel_timeline')
                    if fn:
                        try:
                            text = await fn() if asyncio.iscoroutinefunction(fn) else fn()
                            if text and text.strip():
                                return [{'role': 'system', 'content': text, '_section': 'sentinel_timeline'}]
                        except Exception:
                            pass
                    return ''
            if isinstance(annotations, list) and annotations:
                event_ids = [a.get('_event_id') for a in annotations if isinstance(a, dict) and a.get('_event_id') not in (None, '')]
                if event_ids:
                    self._one_shot_claims.append({'kind': 'sentinel_events', 'stable_id': str(event_ids[0]), 'ids': event_ids})
                return [{'role': 'system', 'content': a.get('content', str(a)), '_section': 'sentinel_timeline'} for a in annotations]
            return ''
        if name == 'time_awareness':
            hint = self._kwargs.get('time_awareness_hint', '')
            if hint and hint.strip():
                return [{'role': 'system', 'content': hint.strip(), '_section': 'time_awareness', '_ts': '9999'}]
            return ''
        if name == 'social_feed_reminder':
            return ''
        fn = get_ingredient(name)
        if fn:
            self._ingredient_diagnostics.setdefault(name, {})['source'] = fn.__module__ + '.' + fn.__name__
            try:
                call_kwargs = self._build_fn_kwargs(name, params, fn)
                if name == 'other_core':
                    call_kwargs['inspection'] = self._ingredient_diagnostics.setdefault(name, {})
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**call_kwargs)
                else:
                    result = fn(**call_kwargs)
                return result
            except Exception as e:
                self._ingredient_diagnostics.setdefault(name, {})['error_type'] = type(e).__name__
                logger.warning(f"Ingredient '{name}' failed: {e}")
        else:
            self._ingredient_diagnostics.setdefault(name, {})['error_type'] = 'IngredientNotRegistered'
        return None

    def _build_fn_kwargs(self, name: str, params: dict, fn=None) -> dict:
        """构建 ingredient 函数的有效参数——只传函数实际接受的参数。"""
        merged = dict(self._kwargs)
        merged.update(params)
        if name == 'cognition_brief':
            merged.pop('covered_self_book', None)
            if self._recipe_key in {'WANDER_V2', 'WANDER_ACTIVITY', 'WANDER_MUSIC_SELECTION'} and self._section_status.get('self_book', {}).get('status') in {'ok', 'dynamic'}:
                displayed = self._sections.get('self_book', '')
                merged['covered_self_book'] = tuple((source for source in self._self_book_coverage if source['body'] in displayed))
        if fn is None:
            return merged
        try:
            import inspect
            sig = inspect.signature(fn)
            accepted = set()
            for p in sig.parameters.values():
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                    accepted.add(p.name)
                elif p.kind == p.VAR_KEYWORD:
                    return merged
            return {k: v for k, v in merged.items() if k in accepted}
        except Exception:
            return merged

    async def _resolve_push_diary(self) -> dict:
        """推送场景自取昨日背景（与 schedule_context.py:2377-2486 活跃话题覆盖检测逻辑对齐）。

        返回 {yesterday, yesterday_conversation, yesterday_diary_fallback,
              day_before, day_before_diary, gap_diaries_text}。
        活跃话题跨午夜（活跃日=昨天）时对话流已含活跃日消息，"昨天"回溯到话题开始日前一天，
        避免与对话流重复；无活跃话题时昨天=日历昨天。结果缓存（一天内不变）。
        """
        if self._push_diary_kwargs is not None:
            return self._push_diary_kwargs
        result = {'yesterday': '', 'yesterday_conversation': '', 'yesterday_diary_fallback': '', 'yesterday_compact': '', 'yesterday_compact_source_ids': [], 'yesterday_related': '', 'day_before': '', 'day_before_diary': '', 'gap_diaries_text': ''}
        try:
            from event_chronicle import get_global_chronicle, _read_yesterday_messages_formatted
            from datetime import datetime, timedelta
            chronicle = get_global_chronicle()
            now = datetime.now()
            try:
                from memory_v2.context_shadow import memory_v2_context_mode
                memory_v2_enabled = memory_v2_context_mode() == 'enabled'
            except Exception:
                memory_v2_enabled = False
            last_active = chronicle.get_last_active_date()
            if last_active and self.session_id:
                try:
                    from summary import topic_state_machine
                    from time_utils import to_beijing_time
                    ts = await topic_state_machine.get_topic_status(self.session_id)
                    if ts.get('is_active') and ts.get('start_time'):
                        start_beijing = to_beijing_time(ts['start_time'])
                        topic_start_date = start_beijing[:10] if start_beijing else ''
                        if topic_start_date and last_active >= topic_start_date:
                            _dt = datetime.strptime(topic_start_date, '%Y-%m-%d')
                            last_active = (_dt - timedelta(days=1)).strftime('%Y-%m-%d')
                except Exception:
                    pass
            yesterday = last_active if last_active else (now - timedelta(days=1)).strftime('%Y-%m-%d')
            result['yesterday'] = yesterday
            if yesterday:
                compact = None
                if memory_v2_enabled:
                    try:
                        from context_builder.cross_day_memory import build_cross_day_memory_context
                        represented_ids = set()
                        compact_text = build_cross_day_memory_context(chronicle, yesterday, now=now, session_id=self.session_id, included_ids=represented_ids)
                        if compact_text:
                            compact = {'text': compact_text, 'source_ids': sorted(represented_ids)}
                            self._kwargs['represented_wander_activity_ids'] = [mid.removeprefix('activity_event_') for mid in represented_ids if mid.startswith('activity_event_')]
                    except Exception:
                        compact = None
                elif self._recipe_key in {'WANDER_PUSH', 'SENTINEL_PUSH', 'REMINDER_PUSH'}:
                    try:
                        from context_builder.proactive_history import build_compact_yesterday_context
                        compact = build_compact_yesterday_context(chronicle, yesterday, context_query=self._kwargs.get('context_query', ''), messages=chronicle.get_messages_by_date(yesterday) or [])
                    except Exception:
                        compact = None
                if compact and compact.get('text'):
                    result['yesterday_compact'] = compact['text']
                    result['yesterday_compact_source_ids'] = compact.get('source_ids', [])
                    result['yesterday_related'] = compact.get('related_text', '')
                elif not memory_v2_enabled:
                    yc = _read_yesterday_messages_formatted(target_date=yesterday)
                    if yc:
                        result['yesterday_conversation'] = yc
                    else:
                        entry = chronicle.get_diary_entry_by_date(yesterday)
                        if entry:
                            result['yesterday_diary_fallback'] = entry.content
            day_before = chronicle.get_last_active_date(before_date=last_active) if last_active else None
            if not day_before:
                try:
                    day_before = (datetime.strptime(yesterday, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
                except Exception:
                    day_before = None
            result['day_before'] = day_before
            if day_before and (not memory_v2_enabled):
                entry = chronicle.get_diary_entry_by_date(day_before)
                if entry:
                    result['day_before_diary'] = entry.content
            try:
                calendar_yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
                if yesterday and yesterday < calendar_yesterday:
                    gap_start = datetime.strptime(yesterday, '%Y-%m-%d') + timedelta(days=1)
                    gap_end = datetime.strptime(calendar_yesterday, '%Y-%m-%d')
                    cursor = gap_start
                    gap_parts = []
                    while cursor <= gap_end:
                        date_str = cursor.strftime('%Y-%m-%d')
                        entry = chronicle.get_diary_entry_by_date(date_str)
                        if entry and entry.content.strip():
                            gap_parts.append(f'{date_str}: {entry.content}')
                        cursor += timedelta(days=1)
                    if gap_parts:
                        result['gap_diaries_text'] = '\n'.join(gap_parts)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f'_resolve_push_diary 失败: {e}')
        self._push_diary_kwargs = result
        return result

    def _add(self, name: str, content, *, placement: Optional[str]=None):
        """添加一段内容到系统提示或额外消息列表。
        - str → 默认追加到 system_content；显式 history/turn 保留对应区
        - list[dict]（每个 dict 有 'role' 键）→ 追加到 extra_messages
        """
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and 'role' in item:
                    owned = deepcopy(item)
                    owned.setdefault('_section', name)
                    if name == 'status_changes' and owned.get('_status_frozen'):
                        self._extra_messages.append(owned)
                    elif placement == ZONE_CONTEXT:
                        self._context_messages.append(owned)
                    elif placement == ZONE_TURN:
                        self._tail_messages.append(owned)
                    else:
                        self._extra_messages.append(owned)
            return
        if content and str(content).strip():
            self._sections[name] = str(content).strip()
            if placement in {ZONE_HISTORY, ZONE_TURN}:
                self._add(name, [{'role': 'system', 'content': str(content).strip()}], placement=placement)
                return
            self._system_content += str(content).strip() + '\n\n'

    def _finalize(self) -> ContextResult:
        result = ContextResult()
        result.system_content = self._system_content.strip()
        result.dynamic_content = self._dynamic_content.strip()
        result.dynamic_messages = [{k: v for k, v in m.items() if not k.startswith('_')} for m in self._dynamic_messages]
        result.tail_messages = [{key: value for key, value in message.items() if not key.startswith('_')} for message in self._tail_messages]
        result.context_messages = [{key: value for key, value in message.items() if not key.startswith('_')} for message in self._context_messages]
        _MISSING_MAP = {'memories': '与当前内容相关的长期记忆本轮没有浮现。', 'yesterday': '昨天的对话或日记材料本轮没有取得。'}
        _missing_msgs = [text for sec_name, text in _MISSING_MAP.items() if self._section_status.get(sec_name, {}).get('status') == 'empty']
        _history_failure_facts = {'recent_history': '近期对话来源本轮读取失败，聊天记录情况未知。', 'today_presence': '今日消息状态本轮读取失败，今天是否收到人类伙伴消息未知。'}
        _missing_msgs.extend((text for source, text in _history_failure_facts.items() if source in self._history_source_errors))
        if _missing_msgs:
            _self_text = ' '.join(_missing_msgs)
            if self._dynamic_content:
                self._dynamic_content += '\n\n'
            self._dynamic_content += _self_text
            self._dynamic_messages.append({'role': 'assistant', 'name': 'mirrow_inner_snapshot', 'content': '[回忆·本轮来源状态]\n' + _self_text})
            self._section_status['self_awareness'] = {'status': 'dynamic', 'placement': ZONE_CONTEXT}
            self._sections['self_awareness'] = _self_text
        result.dynamic_content = self._dynamic_content.strip()
        result.dynamic_messages = [{k: v for k, v in m.items() if not k.startswith('_')} for m in self._dynamic_messages]
        if self._extra_messages:
            from .status_history import history_sort_key
            self._extra_messages.sort(key=history_sort_key)
            from .wander_narration import narrate
            self._extra_messages = narrate(self._extra_messages)
        if result.system_content:
            result.formatted_messages.append({'role': 'system', 'content': result.system_content})
        result.formatted_messages.extend(self._extra_messages)
        result.sections = {}
        for sec_name, content in self._sections.items():
            entry = {'text': content, 'tokens': estimate_tokens(content), 'chars': len(content)}
            entry.update(self._section_status.get(sec_name, {}))
            result.sections[sec_name] = entry
        for sec_name, status in self._section_status.items():
            if sec_name not in result.sections:
                entry = {'text': '', 'tokens': 0, 'chars': 0}
                entry.update(status)
                result.sections[sec_name] = entry
        result.stats = {'total_system_chars': len(result.system_content), 'total_system_tokens': estimate_tokens(result.system_content), 'total_dynamic_chars': len(result.dynamic_content), 'total_dynamic_tokens': estimate_tokens(result.dynamic_content), 'messages_count': len(result.formatted_messages), 'context_messages_count': len(result.context_messages), 'tail_messages_count': len(result.tail_messages), 'section_count': len(self._sections), 'section_names': list(self._sections.keys())}
        result.memory_trace_id = str(getattr(self, '_memory_trace_id', '') or '')
        if result.memory_trace_id:
            try:
                from memory_v2.trace_store import collect_memory_sections, get_default_trace_store
                memory_sections, memory_text = collect_memory_sections(result.sections)
                get_default_trace_store().finalize_trace(result.memory_trace_id, actual_sections=memory_sections, actual_injected_text=memory_text)
                result.stats['memory_trace_id'] = result.memory_trace_id
            except Exception as exc:
                logger.debug('memory recall trace finalization skipped: %s', type(exc).__name__)
        try:
            from prompt_ledger import register_prompt
            ledger_messages = []
            for index, message in enumerate(result.formatted_messages):
                item = dict(message)
                if index == 0 and item.get('role') == 'system':
                    item['layer'] = 'stable'
                elif item.get('_section') in {'conversation_messages', 'push_conversation_history'} or item.get('_status_frozen'):
                    item['layer'] = 'interleaved'
                elif item.get('_section'):
                    item['layer'] = 'dynamic'
                ledger_messages.append(item)
            if result.dynamic_messages:
                ledger_messages.extend(({**m, 'layer': 'dynamic'} for m in result.dynamic_messages))
            elif result.dynamic_content:
                ledger_messages.append({'role': 'system', 'content': result.dynamic_content, 'layer': 'dynamic'})
            for message in self._context_messages:
                item = dict(message)
                item['layer'] = 'context'
                ledger_messages.append(item)
            for tail in self._tail_messages:
                item = dict(tail)
                item['layer'] = 'tail'
                ledger_messages.append(item)
            result.call_id = register_prompt(ledger_messages, recipe=self._recipe_key or self._recipe_name, scenario=self._scenario_override or self._recipe_name, activity_id=self._kwargs.get('activity_id') or self._kwargs.get('current_activity_id', ''), session_id=self.session_id, one_shot_claims=self._one_shot_claims, sections=result.sections)
            result.one_shot_claims = list(self._one_shot_claims)
            result.stats['prompt_call_id'] = result.call_id
        except Exception as exc:
            logger.debug(f'prompt ledger registration skipped: {exc}')
        try:
            from context_builder.inspection import record_builder_inspection
            record_builder_inspection(self, result)
        except Exception:
            pass
        if self._recipe_name:
            try:
                from context_builder import record_recipe_call
                msg_order = []
                for i, m in enumerate(result.formatted_messages):
                    role = m.get('role', '?')
                    content = m.get('content', '')
                    section = m.get('_section', '') or ('' if role != 'system' else 'system_content')
                    msg_order.append({'idx': i + 1, 'role': role, 'section': section, 'preview': content, 'chars': len(content)})
                record_recipe_call(scenario=self._scenario_override or self._recipe_name, recipe_name=self._recipe_name, sections=result.sections, total_chars=result.stats['total_system_chars'], total_tokens=result.stats['total_system_tokens'], extra={'session_id': self.session_id}, via_builder=True, msg_order=msg_order)
            except Exception:
                pass
        for m in self._extra_messages:
            m.pop('_ts', None)
            m.pop('_section', None)
            m.pop('_status_frozen', None)
        for m in self._context_messages:
            m.pop('_ts', None)
            m.pop('_section', None)
        return result

def commit_context_result(result: ContextResult, *, reply_persisted: bool=False, assistant_message_id: str='') -> bool:
    """Commit one-shot context material after the provider accepts a call.

    Kept as a tiny public hook for HTTP/WS callers that already know the
    builder result.  Provider usage instrumentation normally commits sooner;
    this remains idempotent and harmless when called a second time.
    """
    if reply_persisted and assistant_message_id:
        trace_id = str(getattr(result, 'memory_trace_id', '') or '')
        if trace_id:
            try:
                from memory_v2.trace_store import get_default_trace_store
                get_default_trace_store().bind_assistant_message(trace_id, assistant_message_id)
            except Exception:
                logging.getLogger(__name__).exception('记忆召回诊断绑定失败')
    call_id = getattr(result, 'call_id', '') if result is not None else ''
    if not call_id:
        return False
    try:
        from prompt_ledger import commit_prompt_call
        return commit_prompt_call(call_id)
    except Exception:
        return False

def use_context_builder() -> bool:
    """灰度开关：环境变量 MIRROW_USE_CONTEXT_BUILDER=1 时启用 Builder。"""
    return os.environ.get('MIRROW_USE_CONTEXT_BUILDER') == '1'

def context_diff_enabled() -> bool:
    """输出对比模式：环境变量 MIRROW_CONTEXT_DIFF=1 时并行运行新旧对比。"""
    return os.environ.get('MIRROW_CONTEXT_DIFF') == '1'

async def maybe_diff_contexts(recipe_name: str, old_result, new_result, **kwargs):
    """并行运行新旧上下文构建，差异写入日志。

    比较两层：① system_content（系统提示）② formatted_messages（对话层——
    穿插消息/角色前缀/时间感知位置等漂移只在这层可见）。
    """
    if not context_diff_enabled():
        return
    try:
        old_content = getattr(old_result, 'system_content', None) or getattr(old_result, 'formatted_context', None) or str(old_result)
        new_content = new_result.system_content
        if old_content.strip() != new_content.strip():
            logger.warning(f'[CONTEXT_DIFF] Recipe={recipe_name} | old_chars={len(old_content)} new_chars={len(new_content)} | old_preview={old_content[:200]}... | new_preview={new_content[:200]}...')
        else:
            logger.info(f'[CONTEXT_DIFF] Recipe={recipe_name} MATCH ({len(old_content)} chars)')
        old_msgs = getattr(old_result, 'formatted_messages', None)
        new_msgs = getattr(new_result, 'formatted_messages', None)
        if old_msgs is not None and new_msgs is not None:
            old_tail = old_msgs[1:] if old_msgs and old_msgs[0].get('role') == 'system' else list(old_msgs)
            new_tail = new_msgs[1:] if new_msgs and new_msgs[0].get('role') == 'system' else list(new_msgs)
            old_sig = [(m.get('role', ''), (m.get('content') or '')[:50]) for m in old_tail]
            new_sig = [(m.get('role', ''), (m.get('content') or '')[:50]) for m in new_tail]
            if old_sig != new_sig:
                first_diff = ''
                for i in range(max(len(old_sig), len(new_sig))):
                    o = old_sig[i] if i < len(old_sig) else ('<缺失>', '')
                    n = new_sig[i] if i < len(new_sig) else ('<缺失>', '')
                    if o != n:
                        first_diff = f'idx={i} old={o} new={n}'
                        break
                logger.warning(f'[CONTEXT_DIFF] Recipe={recipe_name} MSGS_DIFF | old_count={len(old_tail)} new_count={len(new_tail)} | {first_diff}')
            else:
                logger.info(f'[CONTEXT_DIFF] Recipe={recipe_name} MSGS_MATCH ({len(new_tail)} msgs)')
    except Exception as e:
        logger.warning(f'[CONTEXT_DIFF] comparison failed: {e}')
