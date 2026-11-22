"""
上下文 Recipe 定义 — 所有场景的上下文配方的 SINGLE SOURCE OF TRUTH。

每个 Recipe 声明自己的 Section 列表 + 参数覆盖。
消费者按名取 Recipe，改配方只需改此文件。
"""
from dataclasses import dataclass, field
from copy import deepcopy
from typing import List, Callable, Optional
from context_builder.assembly import VALID_ZONES, ZONE_CONTEXT, ZONE_HISTORY, ZONE_PREFIX, ZONE_TURN

@dataclass
class RecipeSection:
    """Recipe 段定义"""
    name: str
    ingredient: str
    params: dict = field(default_factory=dict)
    condition: Optional[str] = None
    group: str = ''
    guide: Optional[str] = None
    placement: Optional[str] = None

    def __post_init__(self) -> None:
        if self.placement is not None and self.placement not in VALID_ZONES:
            raise ValueError(f'invalid context placement: {self.placement}')

@dataclass
class Recipe:
    name: str
    description: str
    sections: List[RecipeSection]
    consumer: str = ''
    pipeline: str = 'builder'
_PUSH_BASE = [RecipeSection('身份', 'persona', group='A身份', placement=ZONE_PREFIX), RecipeSection('人设锚点', 'persona_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('行为调整', 'active_evolutions', group='A身份', placement=ZONE_PREFIX), RecipeSection('时间锚点', 'timeline_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('标记说明', 'message_markers', group='E系统', placement=ZONE_PREFIX), RecipeSection('前天日记', 'day_before_diary', group='D历史', placement=ZONE_PREFIX), RecipeSection('昨日固定背景', 'yesterday', group='D历史', placement=ZONE_PREFIX), RecipeSection('昨日相关原文', 'yesterday_related', group='C知识', placement=ZONE_CONTEXT), RecipeSection('间隙日记', 'gap_diaries', group='D历史', placement=ZONE_PREFIX), RecipeSection('自我书', 'self_book', group='A身份', placement=ZONE_CONTEXT), RecipeSection('情绪本体', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索', 'open_loops', group='B情境', placement=ZONE_CONTEXT), RecipeSection('时间', 'time', group='B情境', placement=ZONE_CONTEXT), RecipeSection('今日时间线', 'today_timeline', group='B情境', placement=ZONE_CONTEXT), RecipeSection('天气', 'weather', group='B情境', placement=ZONE_CONTEXT), RecipeSection('音乐耳蜗', 'music_cochlea', condition='music_cochlea', group='B情境', placement=ZONE_CONTEXT), RecipeSection('语音耳蜗', 'voice_cochlea', group='B情境', placement=ZONE_CONTEXT), RecipeSection('今日对话记录', 'push_conversation_history', group='对话注入', placement=ZONE_HISTORY), RecipeSection('当前状态总览', 'push_current_state', group='对话注入', placement=ZONE_CONTEXT)]
FULL_CHAT = Recipe('主聊天（完整）', '正常聊天的完整上下文，按 A-E 层组织', consumer='AI私聊', sections=[RecipeSection('身份', 'persona', group='A身份', placement=ZONE_PREFIX), RecipeSection('人设锚点', 'persona_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('行为调整', 'active_evolutions', group='A身份', placement=ZONE_PREFIX), RecipeSection('自我书', 'self_book', group='A身份', placement=ZONE_CONTEXT), RecipeSection('时间锚点', 'timeline_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('标记说明', 'message_markers', group='E系统', placement=ZONE_PREFIX), RecipeSection('情绪本体', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索', 'open_loops', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索 v2', 'open_loops_v2', group='B情境', placement=ZONE_CONTEXT), RecipeSection('用户资料', 'user_profile', group='B情境', placement=ZONE_PREFIX), RecipeSection('记忆检索', 'memories', params={'top_k': 10}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('相关经历', 'experience_recall', group='C知识', placement=ZONE_CONTEXT), RecipeSection('世界书', 'world_book', group='C知识', placement=ZONE_CONTEXT), RecipeSection('前天日记', 'day_before_diary', condition='inject_yesterday', group='D历史', placement=ZONE_PREFIX), RecipeSection('昨日对话', 'yesterday', condition='inject_yesterday', group='D历史', placement=ZONE_PREFIX), RecipeSection('间隙日记', 'gap_diaries', condition='inject_yesterday', group='D历史', placement=ZONE_PREFIX), RecipeSection('引用日记', 'referenced_diary', condition='date_referenced', group='D历史', placement=ZONE_CONTEXT), RecipeSection('重要事件', 'important_events', condition='inject_yesterday', group='D历史', placement=ZONE_PREFIX), RecipeSection('时间', 'time', group='B情境', placement=ZONE_CONTEXT), RecipeSection('活动摘要', 'activity_summary', group='B情境', placement=ZONE_CONTEXT), RecipeSection('生理期', 'period', condition='period_active', group='B情境', placement=ZONE_CONTEXT), RecipeSection('离开信息', 'away', condition='away', group='B情境', placement=ZONE_CONTEXT), RecipeSection('哨兵快照', 'sentinel_snapshot', group='B情境', placement=ZONE_CONTEXT), RecipeSection('天气', 'weather', group='B情境', placement=ZONE_CONTEXT), RecipeSection('深夜模式', 'night_mode', condition='night_mode', group='B情境', placement=ZONE_CONTEXT), RecipeSection('刷手机模式', 'phone_browse', condition='phone_browse', group='B情境', placement=ZONE_CONTEXT), RecipeSection('音乐耳蜗', 'music_cochlea', condition='music_cochlea', group='B情境', placement=ZONE_CONTEXT), RecipeSection('语音耳蜗', 'voice_cochlea', group='B情境', placement=ZONE_CONTEXT), RecipeSection('最近内在漫想', 'recent_inner_wander', params={'hours': 36, 'limit': 10, 'skip_projected_active_day': True}, group='D历史', placement=ZONE_CONTEXT), RecipeSection('裂隙档案连续性', 'rift_game_context', condition='rift_game_context', group='B情境', placement=ZONE_CONTEXT), RecipeSection('对话历史', 'conversation_messages', group='对话注入', placement=ZONE_HISTORY), RecipeSection('时间感知', 'time_awareness', condition='time_hint', group='对话注入', placement=ZONE_HISTORY), RecipeSection('状态变更履历', 'status_changes', group='对话注入', placement=ZONE_CONTEXT), RecipeSection('哨兵注解', 'sentinel_timeline', group='对话注入', placement=ZONE_CONTEXT), RecipeSection('特别关心提醒', 'social_feed_reminder', group='E系统', placement=ZONE_TURN)])
RIFT_PLAY = Recipe('裂隙档案共同游玩', 'AI 保留完整人格骨架、聚焦当前虚构案件的主聊天上下文', consumer='AI私聊·裂隙档案', sections=[RecipeSection('身份', 'persona', group='A身份', placement=ZONE_PREFIX), RecipeSection('人设锚点', 'persona_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('行为调整', 'active_evolutions', group='A身份', placement=ZONE_PREFIX), RecipeSection('自我书', 'self_book', group='A身份', placement=ZONE_CONTEXT), RecipeSection('时间锚点', 'timeline_anchor', group='A身份', placement=ZONE_PREFIX), RecipeSection('标记说明', 'message_markers', group='E系统', placement=ZONE_PREFIX), RecipeSection('情绪本体', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索 v2', 'open_loops_v2', group='B情境', placement=ZONE_CONTEXT), RecipeSection('用户资料', 'user_profile', group='B情境', placement=ZONE_PREFIX), RecipeSection('相关记忆', 'memories', params={'top_k': 3}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('时间', 'time', group='B情境', placement=ZONE_CONTEXT), RecipeSection('裂隙档案运行态', 'rift_game_context', group='B情境', placement=ZONE_CONTEXT), RecipeSection('对话历史', 'conversation_messages', group='对话注入', placement=ZONE_HISTORY), RecipeSection('时间感知', 'time_awareness', condition='time_hint', group='对话注入', placement=ZONE_HISTORY)])
SENTINEL_PUSH = Recipe('哨兵-推送', '哨兵触发时 AI 主动发消息的 Flash 上下文', consumer='AI哨兵', sections=[*_PUSH_BASE, RecipeSection('本次感知事件', 'sentinel_event', group='对话注入', placement=ZONE_TURN), RecipeSection('本次主动联系意图', 'sentinel_intent', group='对话注入', placement=ZONE_TURN)])
MUSIC_COCHLEA_PUSH = Recipe('音乐耳蜗-推送', '音乐耳蜗触发时 AI 主动分享听歌感受的 Flash 上下文（精简，不含哨兵健康/基线段）', consumer='AI音乐耳蜗', sections=[RecipeSection('身份', 'persona', group='A身份'), RecipeSection('人设锚点', 'persona_anchor', group='A身份'), RecipeSection('时间锚点', 'timeline_anchor', group='A身份'), RecipeSection('情绪本体', 'emotion_self', group='B情境'), RecipeSection('情绪共振', 'emotion_resonance', group='B情境'), RecipeSection('时间', 'time', group='B情境'), RecipeSection('活动摘要', 'activity_summary', group='B情境'), RecipeSection('天气', 'weather', group='B情境'), RecipeSection('音乐耳蜗', 'music_cochlea', group='B情境'), RecipeSection('最近对话', 'recent_messages', params={'n': 6}, group='对话注入')])
PHONE_BROWSE = Recipe('刷手机陪聊', '刷手机模式极简上下文（仅 4 段）', consumer='AI 刷手机', sections=[RecipeSection('身份', 'persona', group='A身份'), RecipeSection('情绪本体', 'emotion_self', group='B情境'), RecipeSection('情绪共振', 'emotion_resonance', group='B情境'), RecipeSection('时间', 'time', group='A身份'), RecipeSection('最近对话', 'recent_messages', params={'n': 6}, group='对话注入')])
SELF_TASK = Recipe('自调度任务', '定时自唤醒任务的执行上下文（与哨兵/漫想/提醒共享 _PUSH_BASE，AI 醒来时不是失忆的）', consumer='自调度', sections=[*_PUSH_BASE, RecipeSection('任务信息', 'task_info', group='E系统'), RecipeSection('可用工具', 'available_tools', group='E系统')])
DIARY = Recipe('日记生成', '每日日记生成的 Flash prompt 上下文', consumer='日记', pipeline='legacy_manual', sections=[RecipeSection('日记规则', 'diary_rules'), RecipeSection('昨日参考', 'yesterday_diary'), RecipeSection('对话全文', 'full_conversation')])
WANDER_PUSH = Recipe('漫想-推送', '漫想触发时 AI 主动发消息的 Flash 上下文', consumer='AI漫想', sections=[*_PUSH_BASE, RecipeSection('相关历史记忆', 'memories', params={'top_k': 3}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('漫想说明', 'wander_mode', group='E系统', placement=ZONE_CONTEXT), RecipeSection('本次漫想活动', 'wander_activity', group='对话注入', placement=ZONE_TURN)])
WANDER_V2 = Recipe('漫想v2-计划', '漫想计划 LLM 的连续性上下文', consumer='AI漫想v2', sections=[RecipeSection('相关历史事实', 'memories', params={'top_k': 3}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('最近已结束活跃日', 'yesterday', group='D历史', placement=ZONE_CONTEXT), RecipeSection('身份', 'persona', group='A身份'), RecipeSection('人设锚点', 'persona_anchor', group='A身份'), RecipeSection('行为调整', 'active_evolutions', group='A身份'), RecipeSection('时间锚点', 'timeline_anchor', group='A身份'), RecipeSection('开放线索', 'open_loops', group='B情境'), RecipeSection('最近内在漫想', 'recent_inner_wander', params={'hours': 36, 'limit': 10, 'skip_projected_active_day': True}, group='B情境'), RecipeSection('兴趣倾向与相关自我书', 'self_book', params={'include_interests': True}, group='C知识'), RecipeSection('活跃日经历', 'wander_day_history', group='B情境'), RecipeSection('AI自身情绪', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('人类伙伴情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索 v2', 'open_loops_v2', group='B情境', placement=ZONE_CONTEXT), RecipeSection('漫想运行态', 'wander_runtime', group='E系统', placement=ZONE_CONTEXT)])
WANDER_ACTIVITY = Recipe('漫想-活动判断', '漫想节点复核、自省与结算的当前事件段上下文', consumer='AI漫想v3', sections=[RecipeSection('相关历史事实', 'memories', params={'top_k': 3}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('最近已结束活跃日', 'yesterday', group='D历史', placement=ZONE_CONTEXT), RecipeSection('身份', 'persona', group='A身份'), RecipeSection('人设锚点', 'persona_anchor', group='A身份'), RecipeSection('行为调整', 'active_evolutions', group='A身份'), RecipeSection('时间锚点', 'timeline_anchor', group='A身份'), RecipeSection('开放线索', 'open_loops', group='B情境'), RecipeSection('兴趣倾向与相关自我书', 'self_book', params={'include_interests': True}, group='C知识'), RecipeSection('活跃日经历', 'wander_day_history', group='B情境'), RecipeSection('本段先前经历', 'wander_predecessors', group='B情境'), RecipeSection('AI自身情绪', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('人类伙伴情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索 v2', 'open_loops_v2', group='B情境', placement=ZONE_CONTEXT), RecipeSection('漫想运行态', 'wander_runtime', group='E系统', placement=ZONE_CONTEXT)])
WANDER_MUSIC_SELECTION = Recipe('漫想-选歌', '已决定听歌后的内部选择；经历沿共享时间线读取，不重复铺入跨日音乐感想', consumer='AI自主选歌', sections=deepcopy(WANDER_ACTIVITY.sections))
WORKSHOP_K = Recipe('工作坊·AI', '同一个 AI 在工作坊讨论、派工与审阅的客观背景', consumer='AI工作坊', sections=[RecipeSection('相关历史事实', 'memories', params={'top_k': 3}, group='C知识', placement=ZONE_CONTEXT), RecipeSection('最近已结束活跃日', 'yesterday', group='D历史', placement=ZONE_CONTEXT), RecipeSection('身份', 'persona', group='A身份'), RecipeSection('人设锚点', 'persona_anchor', group='A身份'), RecipeSection('时间锚点', 'timeline_anchor', group='A身份'), RecipeSection('相关自我书', 'self_book', group='C知识'), RecipeSection('活跃日经历', 'wander_day_history', group='B情境'), RecipeSection('AI自身情绪', 'emotion_self', group='B情境', placement=ZONE_CONTEXT), RecipeSection('人类伙伴情绪共振', 'emotion_resonance', group='B情境', placement=ZONE_CONTEXT), RecipeSection('开放线索 v2', 'open_loops_v2', group='B情境', placement=ZONE_CONTEXT), RecipeSection('工作坊当前事实', 'wander_runtime', group='E系统')])
REMINDER_PUSH = Recipe('提醒-推送', '日程/查岗提醒时的 Flash 上下文', consumer='AI提醒', sections=[*_PUSH_BASE, RecipeSection('本次提醒事件', 'reminder_event', group='对话注入', placement=ZONE_TURN)])
for _domain_recipe in (WORKSHOP_K,):
    _domain_recipe.sections.append(RecipeSection('当前漫想交接', 'wander_handoff', group='E系统', guide='', placement=ZONE_CONTEXT))
_ALL_RECIPES: dict = {'FULL_CHAT': FULL_CHAT, 'RIFT_PLAY': RIFT_PLAY, 'SENTINEL_PUSH': SENTINEL_PUSH, 'MUSIC_COCHLEA_PUSH': MUSIC_COCHLEA_PUSH, 'PHONE_BROWSE': PHONE_BROWSE, 'SELF_TASK': SELF_TASK, 'DIARY': DIARY, 'WANDER_PUSH': WANDER_PUSH, 'WANDER_V2': WANDER_V2, 'WANDER_ACTIVITY': WANDER_ACTIVITY, 'WANDER_MUSIC_SELECTION': WANDER_MUSIC_SELECTION, 'WORKSHOP_K': WORKSHOP_K, 'REMINDER_PUSH': REMINDER_PUSH}
_PROFILE_TASKS = {'FULL_CHAT': ('daily', None), 'SENTINEL_PUSH': ('life_rhythm', None), 'REMINDER_PUSH': ('life_rhythm', None), 'SELF_TASK': ('life_rhythm', None), 'PHONE_BROWSE': ('daily', None), 'RIFT_PLAY': ('gaming', None), 'MUSIC_COCHLEA_PUSH': ('music', None), 'WANDER_V2': ('wander', None), 'WANDER_ACTIVITY': ('wander', None), 'WANDER_MUSIC_SELECTION': ('music', None), 'WANDER_PUSH': ('general', None), 'WORKSHOP_K': ('general', ['k'])}
for _key, _recipe in _ALL_RECIPES.items():
    if _key in _PROFILE_TASKS:
        _task, _subjects = _PROFILE_TASKS[_key]
        _position = next((i for i, section in enumerate(_recipe.sections) if section.placement == ZONE_TURN or section.ingredient in {'wander_runtime', 'sentinel_event', 'sentinel_intent'}), len(_recipe.sections))
        _recipe.sections.insert(_position, RecipeSection('认知画像', 'cognition_brief', params={'task': _task, 'subject_ids': _subjects}, group='C知识', placement=ZONE_CONTEXT))
    if _recipe.pipeline == 'builder':
        for _section in _recipe.sections:
            if _section.ingredient == 'music_cochlea':
                _section.condition = None
        if _key in {'WANDER_V2', 'WANDER_ACTIVITY', 'WANDER_MUSIC_SELECTION', 'WANDER_PUSH', 'REMINDER_PUSH', 'SENTINEL_PUSH'} and (not any((s.ingredient == 'music_cochlea' for s in _recipe.sections))):
            _position = next((i for i, s in enumerate(_recipe.sections) if s.ingredient == 'wander_runtime'), len(_recipe.sections))
            _recipe.sections.insert(_position, RecipeSection('音乐会话观察', 'music_cochlea', group='B情境'))
    if _recipe.pipeline == 'builder':
        _had_profile = any((s.ingredient == 'user_profile' for s in _recipe.sections))
        _recipe.sections[:] = [s for s in _recipe.sections if s.ingredient not in {'user_profile', 'active_evolutions', 'people_knowledge'}]
        _recipe.sections.insert(1, RecipeSection('主体与核心自我', 'cognitive_core', group='A身份', placement=ZONE_PREFIX))
        _recipe.sections.insert(2, RecipeSection('他者书核心与关系', 'other_core', params={'default_interlocutors': ['human'], 'include_profile': _had_profile}, group='A身份', placement=ZONE_PREFIX))
    if _key == 'MUSIC_COCHLEA_PUSH':
        _position = next((i for i, s in enumerate(_recipe.sections) if s.group == '对话注入' or s.ingredient == 'wander_runtime'), len(_recipe.sections))
        _recipe.sections.insert(_position, RecipeSection('近期音乐体验', 'music_experiences', params={'subject': 'all'}, group='C知识'))
_CACHE_IDENTITY = {'persona', 'cognitive_core', 'other_core', 'persona_anchor', 'timeline_anchor'}
_CACHE_HISTORY = {'wander_day_history', 'recent_messages'}
for _key in {'WANDER_V2', 'WANDER_ACTIVITY', 'WANDER_MUSIC_SELECTION', 'WORKSHOP_K', 'PHONE_BROWSE', 'MUSIC_COCHLEA_PUSH'}:
    for _section in _ALL_RECIPES[_key].sections:
        _section.placement = ZONE_PREFIX if _section.ingredient in _CACHE_IDENTITY else ZONE_HISTORY if _section.ingredient in _CACHE_HISTORY else ZONE_TURN if _section.ingredient in {'wander_runtime'} else ZONE_CONTEXT

def get_recipe(name: str) -> Optional[Recipe]:
    """按名获取 Recipe。"""
    return _ALL_RECIPES.get(name)

def list_recipes() -> List[str]:
    """列出所有可用 Recipe 名。"""
    return list(_ALL_RECIPES.keys())
