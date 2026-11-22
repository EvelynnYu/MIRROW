"""
上下文调度器核心模块
负责触发判断、查询规划、记忆检索、结果筛选和上下文构建
"""
import asyncio
import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict, replace
from time_utils import parse_ts
import logging
import yaml
from pathlib import Path
import httpx
import numpy as np
from truncation_config import get_truncation_limit
from context_builder.guides import guide_for
from context_builder.text_utils import build_tool_history_fact, strip_internal_history_markers, strip_obsidian_wikilinks
from message_roles import is_ui_only_role
from context_builder.notification_projection import project_notification_message

def _resolve_relative_weekday(weekday_cn: str) -> Optional[str]:
    """将中文星期几转为上周对应日期(YYYY-MM-DD)。无法解析返回 None。"""
    WEEKDAY_MAP = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
    target_wd = WEEKDAY_MAP.get(weekday_cn)
    if target_wd is None:
        return None
    today = datetime.now()
    days_since = (today.weekday() - target_wd) % 7
    if days_since == 0:
        days_since = 7
    if days_since <= today.weekday():
        days_since += 7
    target = today - timedelta(days=days_since)
    return target.strftime('%Y-%m-%d')
import jieba
logger = logging.getLogger(__name__)
_DEDUP_STOP_WORDS = {'我', '你', '他', '她', '它', '我们', '你们', '他们', '她们', '的', '了', '是', '在', '和', '就', '都', '也', '还', '要', '有', '会', '能', '可以', '这个', '那个', '哪个', '什么', '怎么', '为什么', '吗', '呢', '吧', '啊', '哦', '嗯', '不', '没', '很', '太', '非常', '比较', '有点', '真的', '觉得', '感觉', '认为', '想', '知道', '说', '告诉', '一下', '一个', '一些', '但是', '因为', '所以', '如果', '人类伙伴', '人类伙伴说', '她说', '我说', 'AI', '然后', '就是'}

def _tokenize_for_dedup(text: str) -> set:
    """将文本分词为实义词集合，用于 Jaccard 去重。"""
    if not text:
        return set()
    text = text.replace('[[', '').replace(']]', '').replace('**', '')
    words = set()
    try:
        for w in jieba.cut(text):
            w = w.strip()
            if len(w) >= 2 and w not in _DEDUP_STOP_WORDS:
                words.add(w)
    except Exception:
        for i in range(len(text) - 1):
            w = text[i:i + 2]
            if w not in _DEDUP_STOP_WORDS:
                words.add(w)
    return words
_WEEKDAYS_CN = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
import threading
import time as _time
_pipeline_health: dict = {}
_pipeline_health_lock = threading.Lock()
_last_pipeline_run: str = ''

def _record_checkpoint(key: str, status: str, duration_ms: float, summary: str='', error: str=''):
    with _pipeline_health_lock:
        _pipeline_health[key] = {'status': status, 'duration_ms': round(duration_ms, 1), 'summary': summary, 'error': error}

def get_pipeline_health() -> dict:
    with _pipeline_health_lock:
        return {'last_run': _last_pipeline_run, 'steps': dict(_pipeline_health)}

def _estimate_tokens(text: str) -> int:
    """估算中英混合文本的 token 数。CJK: ~1.5字符/token, 其他: ~4字符/token"""
    if not text:
        return 0
    cjk = 0
    for c in text:
        cp = ord(c)
        if 19968 <= cp <= 40959 or 13312 <= cp <= 19903 or 63744 <= cp <= 64255 or (131072 <= cp <= 173791):
            cjk += 1
    return max(1, round(cjk / 1.5 + (len(text) - cjk) / 4))

def _build_ledger_reminder() -> str:
    """生成未销旧账的客观状态（≥7天未 touch 的活跃条目）。"""
    try:
        from ledger_manager.manager import get_global_ledger_manager
        mgr = get_global_ledger_manager()
        old = mgr.get_old_active_entries(min_days=7)
        if not old:
            return ''
        type_label = {'promise_broken': '失信', 'hidden_truth': '隐瞒', 'serious_flirt': '情趣', 'wander_check': '查岗'}
        lines = [f'## 未销旧账（共{len(old)}笔，超过一周未提及）']
        for entry in old[:5]:
            label = type_label.get(entry['debt_type'], entry['debt_type'])
            lines.append(f"- [{label}] {entry['content']}")
        lines.append('这些条目仍处于活跃状态，且超过一周没有新的相关记录。')
        return '\n'.join(lines)
    except Exception:
        return ''
_last_context_snapshot: dict = {}
_last_context_snapshot_lock = threading.Lock()

def get_context_snapshot() -> dict:
    with _last_context_snapshot_lock:
        return dict(_last_context_snapshot)
try:
    from calendar_manager.database import is_rest_day_today
except ImportError:
    is_rest_day_today = None
try:
    from calendar_manager.manager import get_global_calendar_manager
except ImportError:
    get_global_calendar_manager = None
try:
    from wander_manager.user_status import get_user_status_context, get_user_status, UserStatus
except ImportError:
    get_user_status_context = None
    get_user_status = None
    UserStatus = None
from context_scheduler_log import write_log as _log_to_file

@dataclass
class TriggerResult:
    """触发判断结果"""
    triggered: bool
    confidence: float
    reason: str
    keywords_found: List[str] = field(default_factory=list)
    semantic_similarity: float = 0.0
LEGACY_RETRIEVAL_PARAMETERS = ('target_buckets', 'retrieval_strength', 'similarity_threshold')

@dataclass
class QueryPlan:
    """查询规划结果"""
    original_query: str
    enhanced_query: str
    target_buckets: List[str]
    retrieval_strength: float
    retrieval_method: str
    max_results: int
    similarity_threshold: float
    intent: str = 'general_query'
    reasoning: str = ''
    time_travel_dates: List[str] = field(default_factory=list)
    time_travel_scene_ids: List[str] = field(default_factory=list)
    time_travel_window: Optional[Tuple[str, str]] = None

    def retrieval_request(self) -> dict:
        """The exact arguments forwarded to the shared V2 ranker."""
        dates = sorted(self.time_travel_dates or [])
        return {'query': self.enhanced_query or self.original_query, 'limit': self.max_results, 'date_from': dates[0] if dates else '', 'date_to': dates[-1] if dates else ''}

    def public_dict(self) -> dict:
        """Retain legacy fields while identifying the parameters actually used."""
        return {**asdict(self), 'retrieval_contract': {'backend': 'memory_v2', 'request': self.retrieval_request(), 'ignored_legacy_parameters': list(LEGACY_RETRIEVAL_PARAMETERS)}}

@dataclass
class MemoryItem:
    """记忆项"""
    id: str
    content: str
    similarity: float
    importance: float
    bucket: str
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ContextResult:
    """上下文构建结果"""
    user_message: str
    persona: str
    user_profile: str
    memories: List[MemoryItem]
    formatted_context: str
    formatted_messages: List[Dict[str, str]]
    stats: Dict[str, Any]

class ContextScheduler:
    """上下文调度器主类"""

    def __init__(self, config_path: Optional[str]=None):
        """
        初始化上下文调度器

        Args:
            config_path: 配置文件路径，如果为None则使用默认配置
        """
        self.config = self._load_config(config_path)
        self.embedding_model = None
        self.http_client = None
        self.initialized = False
        self.trigger_keywords = self.config.get('trigger', {}).get('keywords', ['之前', '上次', '那件事', '还记得吗', '以前', '曾经', '过去', '记忆', '记得', '昨天', '前天', '上周', '上个月', '去年', '前几天', '前阵子', '前一阵', '最近', '那天', '有一次', '某天', '回溯', '那时候', '当时', '回想', '记得那天', '翻一下那天'])
        jieba.initialize()
        logger.info('上下文调度器初始化完成')

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """加载配置文件"""
        default_config = {'trigger': {'keywords': ['之前', '上次', '那件事', '还记得吗', '以前', '曾经', '过去', '记忆', '记得', '昨天', '前天', '上周', '上个月', '去年', '前几天', '前阵子', '前一阵', '最近', '那天', '有一次', '某天', '回溯', '那时候', '当时', '回想', '记得那天', '翻一下那天'], 'similarity_threshold': 0.55, 'enable_semantic_detection': True}, 'query_planning': {'max_memories_to_retrieve': 10}, 'context_building': {'max_memories_in_context': 5, 'persona_format': '系统角色: {persona}\n用户资料: {profile}\n', 'memory_format': '相关记忆 {index}: {memory}', 'include_original_query': True}, 'services': {'ob_rev': {'direct_client': True}}}
        if config_path and Path(config_path).exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = yaml.safe_load(f)
                self._deep_merge(default_config, user_config)
                logger.info(f'从 {config_path} 加载配置')
            except Exception as e:
                logger.error(f'加载配置文件失败: {e}')
        return default_config

    def _deep_merge(self, base: Dict, update: Dict) -> Dict:
        """深度合并字典"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    async def initialize(self):
        """初始化资源（异步）"""
        if self.initialized:
            return
        try:
            from vector_embedder import get_global_embedder
            global_embedder = get_global_embedder()
            global_embedder._ensure_model_loaded()
            self.embedding_model = global_embedder.model
            self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0), limits=httpx.Limits(max_connections=100), proxy=None, trust_env=False)
            self.initialized = True
            logger.info('上下文调度器资源初始化完成')
        except Exception as e:
            logger.error(f'初始化失败: {e}')
            raise

    async def detect_trigger(self, user_message: str) -> TriggerResult:
        """
        触发判断：检测用户输入是否需要记忆检索

        Args:
            user_message: 用户消息

        Returns:
            TriggerResult: 触发判断结果
        """
        keywords_found = []
        for keyword in self.trigger_keywords:
            if keyword in user_message:
                keywords_found.append(keyword)
        for signal in self._TIME_TRAVEL_SIGNALS:
            if signal in user_message and signal not in keywords_found:
                keywords_found.append(signal)
        keyword_triggered = len(keywords_found) > 0
        keyword_confidence = min(1.0, len(keywords_found) * 0.3)
        semantic_similarity = 0.0
        if self.config['trigger']['enable_semantic_detection'] and self.embedding_model:
            try:
                semantic_similarity = await self._calculate_semantic_similarity(user_message)
            except Exception as e:
                logger.warning(f'语义相似度计算失败: {e}')
        semantic_triggered = semantic_similarity > self.config['trigger']['similarity_threshold']
        semantic_confidence = semantic_similarity
        triggered = keyword_triggered or semantic_triggered
        confidence = max(keyword_confidence, semantic_confidence)
        reason_parts = []
        if keyword_triggered:
            reason_parts.append(f"找到关键词: {', '.join(keywords_found)}")
        if semantic_triggered:
            reason_parts.append(f'语义相似度: {semantic_similarity:.2f}')
        reason = ' | '.join(reason_parts) if reason_parts else '未触发'
        return TriggerResult(triggered=triggered, confidence=confidence, reason=reason, keywords_found=keywords_found, semantic_similarity=semantic_similarity)

    async def _calculate_semantic_similarity(self, user_message: str) -> float:
        """计算用户消息与触发模式的语义相似度"""
        trigger_patterns = ['询问之前的事情', '提到过去的经历', '回忆以前的对话', '关于历史记忆的问题', '之前我们讨论过', '提到某个具体的日期或时间', '询问过去某一天发生的事情', '谈论最近几天的经历', '说起上周或上个月的事', '提到前几天发生的事', '好像之前有件什么事', '我记得你说过什么', '那天我们做了什么来着']
        if not self.embedding_model:
            return 0.0
        all_texts = [user_message] + trigger_patterns
        embeddings = self.embedding_model.encode(all_texts, convert_to_numpy=True)
        query_embedding = embeddings[0]
        pattern_embeddings = embeddings[1:]
        similarities = []
        for pattern_embedding in pattern_embeddings:
            similarity = np.dot(query_embedding, pattern_embedding) / (np.linalg.norm(query_embedding) * np.linalg.norm(pattern_embedding))
            similarities.append(similarity)
        return max(similarities) if similarities else 0.0
    _TIME_TRAVEL_SIGNALS = ('回溯', '那时候', '那天', '当时', '回想', '记得那天', '翻一下那天')
    _SEMANTIC_DATE_SIGNALS = ('大前天', '前几天', '几天前', '前阵子', '早些时候', '上周', '上星期', '上礼拜', '周末', '月初', '月中', '月末', '上个月', '这个月', '本月', '去年', '前年', '今年', '那年', '清明', '五一', '端午', '中秋', '国庆', '元旦', '春节', '除夕', '情人节', '儿童节', '生日', '纪念日', '那天', '当天', '某天')

    @classmethod
    def _is_time_travel_request(cls, user_message: str) -> bool:
        """Detect an explicit temporal look-back request (not generic recall)."""
        text = str(user_message or '')
        return any((signal in text for signal in cls._TIME_TRAVEL_SIGNALS))

    @classmethod
    def _may_contain_semantic_date_reference(cls, user_message: str) -> bool:
        """Cheap, conservative gate for the Flash semantic-date fallback.

        Explicit numeric and simple relative dates are handled by
        ``_extract_calendar_reference`` first.  This gate only avoids an LLM
        call for ordinary messages with no plausible temporal expression.
        """
        text = str(user_message or '').strip()
        if not text:
            return False
        if any((signal in text for signal in cls._SEMANTIC_DATE_SIGNALS)):
            return True
        return bool(re.search('(?:上|下|这|本)(?:周|星期|礼拜|月|季度|半年|年)|(?:周|星期|礼拜)[一二三四五六日天末]|(?:春|夏|秋|冬)(?:天|季|季节)?|(?:两|三|四|五|六|七|八|九|十|\\d+)(?:天|周|个月|月|年)(?:前|后)', text))

    @staticmethod
    def _extract_calendar_reference(user_message: str, chronicle=None) -> Tuple[Optional[str], Optional[str]]:
        """Reuse the canonical calendar-reference rules for every date path.

        The numeric/relative patterns originally lived in ``schedule_context``
        (the existing diary reference path).  Keeping them here gives
        ``time_travel`` the same interpretation without introducing a second
        date parser; non-numeric expressions still go through
        ``_extract_semantic_date``.
        """
        text = str(user_message or '')
        date_patterns = [('(\\d{4})年(\\d{1,2})月(\\d{1,2})[号日]', lambda match: f'{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'), ('(\\d{1,2})月(\\d{1,2})[号日]', lambda match: f'{datetime.now().year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}'), ('前天', lambda _match: chronicle.get_nth_previous_active_date(2) if chronicle else (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')), ('昨天', lambda _match: chronicle.get_last_active_date() if chronicle else (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')), ('上周三', lambda _match: _resolve_relative_weekday('周三')), ('上周二', lambda _match: _resolve_relative_weekday('周二')), ('上周一', lambda _match: _resolve_relative_weekday('周一')), ('上周四', lambda _match: _resolve_relative_weekday('周四')), ('上周五', lambda _match: _resolve_relative_weekday('周五')), ('上周六', lambda _match: _resolve_relative_weekday('周六')), ('上周日', lambda _match: _resolve_relative_weekday('周日'))]
        for pattern, resolver in date_patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                resolved = resolver(match)
            except (TypeError, ValueError, AttributeError):
                resolved = None
            if resolved:
                return (str(resolved)[:10], match.group(0))
        return (None, None)

    async def _time_travel_dates(self, user_message: str) -> List[str]:
        """Resolve a time-travel request to one date or a recent three-day span."""
        explicit, _description = self._extract_calendar_reference(user_message)
        if explicit:
            return [explicit]
        try:
            semantic = await self._extract_semantic_date(user_message)
            semantic_date = str((semantic or {}).get('date') or '')[:10]
            if re.fullmatch('\\d{4}-\\d{2}-\\d{2}', semantic_date):
                return [semantic_date]
        except Exception as exc:
            logger.debug('time-travel semantic date skipped: %s', exc)
        today = datetime.now().date()
        return [(today - timedelta(days=offset)).strftime('%Y-%m-%d') for offset in range(3)]

    @staticmethod
    def _time_travel_scene_plan(dates: List[str]) -> Tuple[List[str], Optional[Tuple[str, str]]]:
        """Read scene ids and the enclosing source window for target dates."""
        if not dates:
            return ([], None)
        try:
            from scene_manager.scene import get_scenes_for_day
            scenes = []
            for date in dates:
                scenes.extend(get_scenes_for_day(date))
            scenes = [scene for scene in scenes if scene]
            scene_ids = [str(scene.get('id') or '') for scene in scenes if scene.get('id')]
            starts = [str(scene.get('start_ts') or '') for scene in scenes if scene.get('start_ts')]
            ends = [str(scene.get('end_ts') or '') for scene in scenes if scene.get('end_ts')]
            if starts and ends:
                return (scene_ids, (min(starts), max(ends)))
            return (scene_ids, None)
        except Exception as exc:
            logger.debug('time-travel scene planning skipped: %s', exc)
            return ([], None)

    async def plan_query(self, user_message: str, trigger_result: TriggerResult) -> QueryPlan:
        """
        查询规划：分析用户意图，生成检索策略

        Args:
            user_message: 用户消息
            trigger_result: 触发判断结果

        Returns:
            QueryPlan: 查询规划结果
        """
        config = self.config['query_planning']
        intent = 'time_travel' if self._is_time_travel_request(user_message) else self._detect_intent(user_message)
        time_travel_dates: List[str] = []
        time_travel_scene_ids: List[str] = []
        time_travel_window: Optional[Tuple[str, str]] = None
        if intent == 'time_travel':
            time_travel_dates = await self._time_travel_dates(user_message)
            time_travel_scene_ids, time_travel_window = self._time_travel_scene_plan(time_travel_dates)
        enhanced_query = self._enhance_query(user_message, intent)
        reasoning = f'意图: {intent}, 检索后端: memory_v2'
        if time_travel_dates:
            reasoning += f", 时间范围: {','.join(time_travel_dates)}"
        return QueryPlan(original_query=user_message, enhanced_query=enhanced_query, target_buckets=[], retrieval_strength=0.0, retrieval_method='time_travel' if intent == 'time_travel' else 'memory_v2', max_results=config['max_memories_to_retrieve'], similarity_threshold=0.0, intent=intent, reasoning=reasoning, time_travel_dates=time_travel_dates, time_travel_scene_ids=time_travel_scene_ids, time_travel_window=time_travel_window)

    def _detect_intent(self, user_message: str) -> str:
        """检测用户意图（简化版）"""
        user_message_lower = user_message.lower()
        if self._is_time_travel_request(user_message):
            return 'time_travel'
        if any((word in user_message_lower for word in ['如何', '怎样', '怎么', '方法', '步骤'])):
            return 'how_to'
        elif any((word in user_message_lower for word in ['为什么', '为何', '原因', '理由'])):
            return 'why'
        elif any((word in user_message_lower for word in ['什么', '哪些', '是什么'])):
            return 'what'
        elif any((word in user_message_lower for word in ['谁', '何人', '哪个'])):
            return 'who'
        elif any((word in user_message_lower for word in ['时间', '时候', '日期', '何时'])):
            return 'when'
        elif any((word in user_message_lower for word in ['地方', '地点', '哪里', '何处'])):
            return 'where'
        elif any((word in user_message_lower for word in ['之前', '上次', '以前', '过去'])):
            return 'historical'
        else:
            return 'general_query'

    @staticmethod
    def _enhance_query(user_message: str, intent: str) -> str:
        """增强查询：使用 jieba 分词提取关键实体词作为检索 query。

        比旧版前缀拼接更精准——提取名词/动词/专有名词等实义词，
        组成语义搜索 query，过滤掉"我/你/的/了"等停用词。
        """
        import jieba.posseg as pseg
        _STOP_WORDS = {'我', '你', '他', '她', '它', '我们', '你们', '他们', '她们', '的', '了', '是', '在', '和', '就', '都', '也', '还', '要', '有', '会', '能', '可以', '这个', '那个', '哪个', '什么', '怎么', '为什么', '怎么样', '吗', '呢', '吧', '啊', '哦', '嗯', '不', '没', '很', '太', '非常', '比较', '有点', '觉得', '感觉', '认为', '想', '知道', '说', '告诉', '一下', '一个', '一些', '这个', '那个'}
        try:
            words = pseg.cut(user_message)
            key_terms = []
            for word, flag in words:
                w = word.strip()
                if len(w) < 2:
                    continue
                if w in _STOP_WORDS:
                    continue
                if flag in ('n', 'nr', 'ns', 'nt', 'nz', 'ng', 'v', 'vn', 'vd', 'vg', 'a', 'ad', 'an', 'eng', 'x'):
                    key_terms.append(w)
            if key_terms:
                seen = set()
                unique_terms = []
                for t in key_terms:
                    if t not in seen:
                        seen.add(t)
                        unique_terms.append(t)
                enhanced = ' '.join(unique_terms)
                if intent == 'historical':
                    enhanced = '历史记录 ' + enhanced
                _qec = get_truncation_limit('query_enhance_cap')
                if _qec > 0 and len(enhanced) > _qec:
                    return enhanced[:_qec]
                return enhanced
        except Exception:
            pass
        _qec = get_truncation_limit('query_enhance_cap')
        if _qec > 0 and len(user_message) > _qec:
            return user_message[:_qec]
        return user_message

    async def retrieve_memories(self, query_plan: QueryPlan, session_id: str='') -> List[MemoryItem]:
        """Retrieve through V2's shared ranker; no old activation or neighbor chain."""
        from memory_v2.retrieval import search_memory_items
        return await search_memory_items(**query_plan.retrieval_request())

    async def _extract_semantic_date(self, user_message: str) -> Optional[dict]:
        """使用 Flash 提取用户消息中的隐含日期引用（"清明那天""上周末""五一"等）。

        仅在正则匹配失败后调用，作为兜底。
        返回 {"date": "YYYY-MM-DD", "description": "用户原文表述"} 或 None。
        """
        import os
        api_key = os.getenv('DEEPSEEK_FLASH_API_KEY') or os.getenv('DEEPSEEK_API_KEY', '')
        if not api_key or not self.http_client:
            return None
        api_url = os.getenv('DEEPSEEK_FLASH_API_URL', 'https://api.deepseek.com/v1')
        flash_model = os.getenv('DEEPSEEK_FLASH_MODEL', 'deepseek-v4-flash')
        prompt = '从用户消息中提取隐含的日期引用（非明确数字日期，如"清明那天""上周末""五一""大前天"等）。\n已知节假日：清明=4月4日前后, 五一=5月1日, 端午=农历五月初五(2026年6月19日), 中秋=农历八月十五(2026年10月4日), 国庆=10月1日, 元旦=1月1日, 春节=农历正月初一(2026年2月17日)。\n相对日期：大前天=3天前, 上周六/日=上周末。\n只返回确定可解析的日期。太模糊的（如"前几天"）不要猜，返回null。\n仅输出JSON，格式: {"date": "YYYY-MM-DD", "description": "用户原文表述"} 或 null。'
        from context_builder.channel_projection import prepare, backend_turn
        date_messages, _ = prepare([{'role': 'system', 'content': prompt}, backend_turn('今天是' + datetime.now().strftime('%Y年%m月%d日') + '。\n[待解析的人类伙伴原话]\n' + user_message)])
        try:
            from api_sources.legacy_transport import tracked_post
            from uuid import uuid4
            resp = await tracked_post(self.http_client, f'{api_url}/chat/completions', task_id=uuid4().hex, category='intent', node='semantic_date_parse', ordinal=1, json={'model': flash_model, 'messages': date_messages, 'temperature': 0, 'max_tokens': 100}, headers={'Authorization': f'Bearer {api_key}'})
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            if not content or content.lower() == 'null':
                return None
            import json as _json
            result = _json.loads(content)
            if result and result.get('date'):
                return result
        except Exception:
            pass
        return None

    async def _get_conversation_memories(self, session_id: str, current_message: str, current_message_id: Optional[str]=None) -> List[MemoryItem]:
        """获取当前活跃话题的未总结对话作为短期记忆

        只获取当前活跃话题的消息，已总结的话题不再构建到上下文中。
        通过话题状态机的 is_topic_active 和 topic_start_time 确定话题边界。
        """
        memories = []
        try:
            topic_is_active = False
            topic_start_time = None
            try:
                from summary import conversation_manager, topic_state_machine
                topic_status = await topic_state_machine.get_topic_status(session_id)
                topic_is_active = topic_status.get('is_active', False)
                topic_start_time = topic_status.get('start_time')
                logger.debug(f'话题状态: is_active={topic_is_active}, start_time={topic_start_time}')
                active_day = ''
                if topic_start_time:
                    active_day = str(topic_start_time)[:10]
                if not active_day:
                    active_day = datetime.now().strftime('%Y-%m-%d')
                import asyncio as _asyncio_conv
                from event_chronicle import get_global_chronicle as _gc_conv
                chronicle = _gc_conv()
                all_day = await _asyncio_conv.to_thread(chronicle.get_messages_by_date, active_day)
                messages = []
                for msg in all_day:
                    if msg.get('session_id') and msg.get('session_id') != session_id:
                        continue
                    messages.append(msg)
            except ImportError:
                logger.warning('无法导入summary模块，回退到 SQLite')
                import asyncio
                from event_chronicle import get_global_chronicle
                chronicle = get_global_chronicle()
                all_messages = await asyncio.to_thread(chronicle.get_messages_by_session_id, session_id, 5000)
                messages = all_messages
                logger.debug(f'从 SQLite 获取了 {len(messages)} 条消息')
                logger.warning('无法判断话题状态，跳过对话记忆')
                return []
            if current_message_id:
                filtered_messages = [msg for msg in messages if msg.get('message_id') != current_message_id]
            else:
                filtered_messages = [msg for msg in messages if msg.get('content') != current_message]
            from context_builder.event_history_adapter import adapt_history_for_context
            filtered_messages = adapt_history_for_context(filtered_messages, for_provider=True, wander_level='daily')
            for i, msg in enumerate(filtered_messages):
                similarity = 0.5
                reasoning_content = msg.get('reasoning_content')
                msg_id = msg.get('message_id') or msg.get('id') or ''
                _va_raw = msg.get('voice_attachment')
                _va_meta = None
                if _va_raw:
                    try:
                        _va_meta = json.loads(_va_raw) if isinstance(_va_raw, str) else _va_raw
                    except Exception:
                        _va_meta = None
                _content = str(msg.get('content', ''))
                if msg.get('role') != 'user' and (not msg.get('notification_projection')):
                    from tool_action_protocol import strip_tool_protocol_text
                    _content = strip_tool_protocol_text(_content)
                if msg.get('role') == 'user' and msg.get('long_text_card'):
                    try:
                        from long_text_cards import card_for_history
                        from settings_manager import get_setting
                        _card = msg.get('long_text_card')
                        if isinstance(_card, str):
                            _card = json.loads(_card)
                        _card_text = card_for_history(_card, bool(get_setting('longTextCardFullContext')))
                        if _card_text:
                            _content = f'{_content}\n{_card_text}' if _content else _card_text
                    except Exception as _card_exc:
                        logger.warning('长文卡片历史注入跳过: %s', type(_card_exc).__name__)
                if msg.get('event_type') == 'rift_game':
                    _content = f'[裂隙档案·虚构共同游玩] {_content}'
                memory = MemoryItem(id=f"conversation_{session_id}_{i}_{hashlib.md5(str(msg.get('content', '')).encode()).hexdigest()[:8]}", content=f"{msg.get('role', 'unknown')}: {_content}", similarity=similarity, importance=0.6, bucket='short_term', source='conversation', metadata={'role': msg.get('role'), 'timestamp': msg.get('timestamp'), 'session_id': session_id, 'message_id': msg_id, 'is_active_topic': True, 'reasoning_content': reasoning_content, 'tool_summary': msg.get('tool_summary'), 'tool_calls': msg.get('tool_calls'), 'is_wander': bool(msg.get('is_wander')) or str(msg_id).startswith('wander_'), 'is_sentinel': bool(msg.get('is_sentinel')) or str(msg_id).startswith('sentinel_'), 'is_reminder': bool(msg.get('is_reminder')) or str(msg_id).startswith('reminder_'), 'event_type': str(msg.get('event_type') or ''), 'notification_projection': bool(msg.get('notification_projection')), 'wander_identity': msg.get('_wander_identity'), 'provider_internal': bool(msg.get('_provider_internal')), 'voice_attachment': _va_meta})
                memories.append(memory)
            logger.info(f'获取了 {len(memories)} 条当前活跃话题消息（短期记忆），会话ID: {session_id}')
        except ImportError as e:
            logger.warning(f'无法导入对话管理器: {e}')
        except Exception:
            logger.exception('获取对话记忆失败')
            return memories
        return memories

    @staticmethod
    def _build_time_section() -> str:
        """与 Builder 共用日历事实，不预设个人作息。"""
        from context_builder.ingredients import build_time_section
        return build_time_section()

    @staticmethod
    def _build_timeline_anchor() -> str:
        """构建时间基准锚点；与 Builder 共用权威原料。"""
        from context_builder.ingredients import build_timeline_anchor
        return build_timeline_anchor()

    @staticmethod
    def _format_relative_time(timestamp_str: str) -> str:
        """将 ISO 时间戳转为绝对时间标签。
        当天内: [HH:MM]  昨天: [昨天 HH:MM]  更早: [M月D日 HH:MM]  [YYYY年M月D日 HH:MM]
        与状态变更时间格式保持一致，日期锚点由系统消息中的 time_section 提供。
        """
        if not timestamp_str:
            return ''
        try:
            dt = datetime.fromisoformat(timestamp_str)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            now = datetime.now()
            today = now.date()
            msg_date = dt.date()
            if msg_date == today:
                return dt.strftime('%H:%M')
            elif msg_date == today - timedelta(days=1):
                return f"昨天 {dt.strftime('%H:%M')}"
            elif dt.year == now.year:
                return f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
            else:
                return f"{dt.year}年{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
        except Exception:
            return ''

    @staticmethod
    def _build_period_section() -> str:
        """构建生理期上下文（仅活跃时返回内容）。"""
        try:
            if get_global_calendar_manager:
                cal_mgr = get_global_calendar_manager()
                if cal_mgr:
                    period_info = cal_mgr.get_period_info_today()
                    if period_info.get('active') and period_info.get('note'):
                        return period_info['note']
        except Exception:
            pass
        return ''

    @staticmethod
    def _build_user_status_section() -> str:
        """构建用户实时状态上下文（非 IDLE 时返回内容，含状态已持续时长 + location 层级信息）。

        统一使用 get_user_status_context_rich()（与 ingredients.py 共用同一入口）。
        """
        try:
            if get_user_status and UserStatus:
                user_status = get_user_status()
                try:
                    from wander_manager.user_status import get_user_status_context_rich
                    base = get_user_status_context_rich() if get_user_status_context_rich else get_user_status_context()
                except Exception:
                    base = get_user_status_context() if get_user_status_context else ''
                if base:
                    try:
                        from wander_manager.user_status import get_status_history
                        history = get_status_history(transitions_only=True)
                        if history:
                            last = history[-1]
                            if last.get('to') == user_status.value:
                                from datetime import datetime
                                change_dt = datetime.fromisoformat(last['time'])
                                delta_sec = (datetime.now() - change_dt).total_seconds()
                                if delta_sec >= 60:
                                    h = int(delta_sec // 3600)
                                    m = int(delta_sec % 3600 // 60)
                                    if h > 0 and m > 0:
                                        dur = f'{h}小时{m}分钟'
                                    elif h > 0:
                                        dur = f'{h}小时'
                                    else:
                                        dur = f'{m}分钟'
                                    base += f'（已持续{dur}）'
                    except Exception:
                        pass
                    return base
        except Exception:
            pass
        return ''

    @staticmethod
    def _build_away_info_section(away_duration_seconds: float, away_reason: str, group_chat_active_seconds_ago: Optional[float]=None) -> str:
        """构建用户离开回来后的上下文提示（仅首条消息注入）"""
        if away_duration_seconds < 60:
            return ''
        total_minutes = int(away_duration_seconds / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            duration_str = f'{hours} 小时 {minutes} 分钟'
        elif hours > 0:
            duration_str = f'{hours} 小时'
        else:
            duration_str = f'{minutes} 分钟'
        base = f'用户刚回来，之前离开了 {duration_str}。离开时状态：{away_reason}。'
        return base

    @staticmethod
    def _build_heart_rate_section() -> str:
        """心率数据段（权威实现在 context_builder.ingredients，新旧管道共用）。"""
        try:
            from context_builder.ingredients import build_heart_rate_section
            return build_heart_rate_section()
        except Exception as e:
            logger.debug(f'心率上下文注入跳过: {e}')
            return ''

    @staticmethod
    def _build_sentinel_snapshot() -> str:
        """获取哨兵快照（双行：结构化数据行 + Flash 感知行，与新管道一致）。"""
        try:
            from silicon_perception.sentinel import get_sentinel
            s = get_sentinel()
            if s:
                return s.get_snapshot_line()
        except Exception:
            pass
        return ''

    @staticmethod
    def _build_sentinel_timeline() -> list:
        """查询今日哨兵事件，返回时间线注解列表。"""
        try:
            from silicon_perception.sentinel import get_sentinel
            s = get_sentinel()
            if s and s._annotator:
                return s._annotator.peek_timeline_annotations()
        except Exception:
            pass
        return []

    async def _build_today_timeline(self) -> str:
        """构建今天的时间线：合并状态变更 + 会话起始，按时间顺序排列。

        纯读操作，不写任何存储。仅在 LLM 上下文注入时使用。
        数据源：
        1. SQLite status_change_log（今日状态变更，有精确时间戳和去抖）
        2. SessionManager（今日创建的会话，取首条用户消息作为话题锚点）
        """
        events = []
        try:
            from health_tracker.status_log import get_status_log
            from wander_manager.user_status import STATUS_DISPLAY_NAMES, UserStatus
            today = datetime.now().strftime('%Y-%m-%d')
            log = get_status_log()
            transitions = log.get_log(today)
            for t in transitions:
                ts = t.get('timestamp', '')
                if not ts:
                    continue
                from_s = t.get('from_status', '')
                to_s = t.get('to_status', '')
                try:
                    from_name = STATUS_DISPLAY_NAMES.get(UserStatus(from_s), from_s)
                except Exception:
                    from_name = from_s
                try:
                    to_name = STATUS_DISPLAY_NAMES.get(UserStatus(to_s), to_s)
                except Exception:
                    to_name = to_s
                events.append((ts, 'status', f'状态变化：{from_name} → {to_name}'))
        except Exception as e:
            logger.debug(f'今日时间线-状态日志查询跳过: {e}')
        try:
            from session_manager import get_global_session_manager
            sm = get_global_session_manager()
            sessions = await sm.get_all_sessions()
            today = datetime.now().strftime('%Y-%m-%d')
            for s in sessions or []:
                created = getattr(s, 'createdAt', '') or ''
                if not created.startswith(today):
                    continue
                msgs = getattr(s, 'messages', []) or []
                topic_hint = ''
                for msg in msgs:
                    role = getattr(msg, 'role', '') if hasattr(msg, 'role') else msg.get('role', '')
                    if role == 'user':
                        content = getattr(msg, 'content', '') if hasattr(msg, 'content') else msg.get('content', '')
                        topic_hint = content[:40].replace('\n', ' ')
                        break
                label = getattr(s, 'title', '') or ''
                if not label and topic_hint:
                    label = f'「{topic_hint}」'
                if not label:
                    label = '开始聊天'
                events.append((created, 'topic', f'开始聊天：{label}'))
        except Exception as e:
            logger.debug(f'今日时间线-会话查询跳过: {e}')
        if not events:
            return ''
        events.sort(key=lambda e: e[0])
        lines = ['## 今天的时间线']
        for ts, kind, text in events:
            try:
                if 'T' in ts:
                    time_str = ts[11:16]
                elif ' ' in ts:
                    time_str = ts[11:16] if len(ts) >= 16 else ts[:5]
                else:
                    time_str = ts[:5]
            except Exception:
                time_str = ts[:5] if len(ts) >= 5 else ts
            lines.append(f'- {time_str} {text}')
        return '\n'.join(lines) + '\n\n'

    async def build_context(self, user_message: str, memories: List[MemoryItem], persona: str='', user_profile: str='', yesterday_compact: Optional[str]=None, yesterday_conversation: Optional[str]=None, yesterday_diary_fallback: Optional[str]=None, day_before_diary: Optional[str]=None, gap_diaries_text: Optional[str]=None, yesterday_date: str='', day_before_date: str='', important_events_section: str='', referenced_diary: Optional[str]=None, referenced_diary_date: str='', away_duration_seconds: Optional[float]=None, away_reason: Optional[str]=None, group_chat_active_seconds_ago: Optional[float]=None, night_mode: bool=False, phone_browse_mode: bool=False, music_cochlea: bool=False, world_book_entries: Optional[List]=None, self_book_entries: Optional[List]=None, time_awareness_hint: Optional[str]=None, is_wander_active: bool=False, conversation_memories: Optional[List[MemoryItem]]=None, session_id: str='') -> ContextResult:
        """
        上下文构建：组合人设、记忆、日记和用户输入

        Args:
            user_message: 用户消息
            memories: 检索到的记忆
            persona: 人设提示词
            user_profile: 用户资料
            yesterday_compact: Memory V2 对上一活跃日的渐退投影
            yesterday_conversation: 旧管道的昨日完整对话记录
            yesterday_diary_fallback: 昨日日记回退（无昨日消息时注入，含"一天没找我"信号）
            day_before_diary: 前天日记（新话题时注入）
            important_events_section: 近期重要事件（新话题时注入）
            referenced_diary: 用户消息中引用的特定日期日记

        Returns:
            ContextResult: 完整的上下文构建结果
        """
        config = self.config['context_building']
        if not persona:
            logger.warning('build_context: persona 为空，AI 将失去人设身份！请检查 _latest_persona_prompt 和 system_prompt 提取链路。')
        persona_section = config['persona_format'].format(persona=persona or '', profile=user_profile or '')
        time_section = self._build_time_section()
        timeline_anchor = self._build_timeline_anchor()
        yesterday_section = ''
        if (yesterday_compact or yesterday_conversation or yesterday_diary_fallback) and yesterday_date:
            if yesterday_compact:
                yesterday_section = yesterday_compact
            elif yesterday_conversation:
                yesterday_section = guide_for('yesterday', {'yesterday_conversation': True, 'yesterday_date': yesterday_date}) + '\n' + yesterday_conversation
            elif yesterday_diary_fallback:
                yesterday_section = guide_for('yesterday', {'yesterday_conversation': False, 'yesterday_date': yesterday_date}) + '\n' + yesterday_diary_fallback
        day_before_section = ''
        if day_before_diary and day_before_date:
            day_before_section = guide_for('day_before_diary', {'day_before_date': day_before_date}) + '\n' + day_before_diary
        gap_section = ''
        if gap_diaries_text:
            gap_section = guide_for('gap_diaries', {}) + '\n' + gap_diaries_text
        memory_sections = []
        neighborhood_memories = [m for m in memories if m.metadata.get('is_neighborhood')]
        main_memories = [m for m in memories if not m.metadata.get('is_neighborhood')]
        self_memories = [m for m in main_memories if m.metadata.get('subject') == 'AI']
        memories = [m for m in main_memories if m.metadata.get('subject', '人类伙伴') != 'AI']
        memory_fused = False
        max_memories = min(config['max_memories_in_context'], len(memories))
        time_axis_memories = any((m.metadata.get('time_axis_label') for m in memories))
        if time_axis_memories:
            memory_sections.append('## 时间轴回忆')
        try:
            from context_scheduler_mcp.memory_fusion import fuse_memories
            fused_text = '' if time_axis_memories else fuse_memories(memories, world_book_entries or [], yesterday_diary_fallback or '')
            if fused_text:
                memory_sections = [fused_text]
                memory_section = '\n'.join(memory_sections)
                memory_fused = True
        except Exception as e:
            logger.debug(f'记忆融合跳过: {e}')
        if not memory_fused:
            for i, memory in enumerate(memories[:max_memories], 1):
                if memory.source == 'conversation':
                    continue
                meta_parts = []
                time_label = memory.metadata.get('time_label', '')
                time_axis_label = memory.metadata.get('time_axis_label', '')
                emotion_label = memory.metadata.get('emotion_label', '')
                domain_label = memory.metadata.get('domain', '')
                if time_axis_label:
                    meta_parts.append(time_axis_label)
                elif time_label:
                    meta_parts.append(time_label)
                if emotion_label:
                    meta_parts.append(emotion_label)
                if domain_label:
                    meta_parts.append(domain_label)
                if meta_parts:
                    meta_str = ' · '.join(meta_parts)
                    source_hint = memory.metadata.get('source_hint', '')
                    memory_text = f'相关记忆 {i}: ({meta_str}) {memory.content}{source_hint}'
                else:
                    memory_text = config['memory_format'].format(index=i, memory=memory.content)
                    memory_text += memory.metadata.get('source_hint', '')
                memory_sections.append(memory_text)
        if memory_fused:
            source_hints = [m.metadata.get('source_hint', '') for m in memories if m.metadata.get('source_hint')]
            if source_hints:
                memory_sections.append('可查原文：' + '、'.join(dict.fromkeys(source_hints)))
        if neighborhood_memories:
            neighborhood_bits = []
            for neighbor in neighborhood_memories[:2]:
                label = neighbor.metadata.get('neighborhood_scene_label', '')
                text = strip_obsidian_wikilinks(neighbor.content or '').replace('\n', ' ').strip()[:100]
                if text:
                    neighborhood_bits.append(f'{label}: {text}' if label else text)
            if neighborhood_bits:
                memory_sections.append('（记忆邻域·背景折叠：' + '；'.join(neighborhood_bits) + '）')
        memory_section = '\n'.join(memory_sections) if memory_sections else ''
        self_memory_section = ''
        if self_memories:
            _self_lines = []
            for i, m in enumerate(self_memories[:5], 1):
                _sp = [p for p in (m.metadata.get('time_axis_label') or m.metadata.get('time_label', ''), m.metadata.get('domain', '')) if p]
                _smeta = ' · '.join(_sp)
                _source_hint = m.metadata.get('source_hint', '')
                _self_lines.append(f'相关记忆 {i}: ({_smeta}) {m.content}{_source_hint}' if _smeta else f'相关记忆 {i}: {m.content}{_source_hint}')
            self_memory_section = '\n'.join(_self_lines)
        context_parts = []
        if persona or user_profile:
            context_parts.append(persona_section)
        if memory_sections:
            context_parts.append(guide_for('memories', {}))
            context_parts.append(memory_section)
        if self_memory_section:
            context_parts.append(guide_for('self_memories', {}))
            context_parts.append(self_memory_section)
        if config['include_original_query']:
            context_parts.append('## 当前查询')
            context_parts.append(user_message)
        formatted_context = '\n\n'.join(context_parts)
        formatted_messages = []
        system_content = ''
        _tracked = {'wander_mode': '', 'world_book': '', 'self_book': '', 'night_mode': '', 'phone_browse': '', 'away': '', 'hr': '', 'anchor': '', 'sentinel': ''}
        if persona_section:
            system_content += persona_section + '\n\n'
        if persona and len(persona) > 0:
            anchor = persona.split('。')[0].strip()
            _pac = get_truncation_limit('persona_anchor_cap')
            if _pac > 0 and len(anchor) > _pac:
                anchor = anchor[:_pac]
            if anchor and anchor not in persona_section:
                _tracked['anchor'] = f'【记住】{anchor}。\n\n'
                system_content += _tracked['anchor']
        try:
            from persona_evolution import get_active_evolution_context
            evo_ctx = get_active_evolution_context()
            if evo_ctx:
                _evo_cap = get_truncation_limit('active_evolutions_cap')
                if _evo_cap > 0 and len(evo_ctx) > _evo_cap:
                    evo_ctx = evo_ctx[:_evo_cap]
                system_content += evo_ctx + '\n\n'
        except Exception:
            pass
        if self_book_entries:
            sb_lines = ['## 自我书']
            for entry in self_book_entries:
                if isinstance(entry, dict):
                    sb_lines.append(f"### {entry.get('name', '')}")
                    body = entry.get('body', '') or ''
                else:
                    sb_lines.append(f'### {entry.name}')
                    body = entry.body if entry.body else ''
                sb_lines.append(body)
                sb_lines.append('')
            _tracked['self_book'] = '\n'.join(sb_lines).strip()
            system_content += _tracked['self_book'] + '\n\n'
        system_content += timeline_anchor + '\n\n'
        system_content += time_section + '\n\n'
        status_section = self._build_user_status_section()
        if status_section:
            system_content += f'{status_section}\n\n'
        period_section = self._build_period_section()
        if period_section:
            system_content += period_section + '\n\n'
        try:
            from context_builder.ingredients import build_recent_activity_section
            _recent = build_recent_activity_section()
            if _recent:
                _tracked['recent_activity'] = _recent
                system_content += _recent + '\n\n'
        except Exception:
            pass
        if away_duration_seconds is not None and away_reason:
            _tracked['away'] = self._build_away_info_section(away_duration_seconds, away_reason, group_chat_active_seconds_ago) or ''
            if _tracked['away']:
                system_content += _tracked['away'] + '\n\n'
        try:
            snapshot_line = self._build_sentinel_snapshot()
            if snapshot_line:
                _tracked['sentinel'] = snapshot_line
                system_content += _tracked['sentinel'] + '\n\n'
        except Exception:
            pass
        _tracked['hr'] = self._build_heart_rate_section() or ''
        if _tracked['hr']:
            system_content += _tracked['hr']
        if night_mode:
            from context_builder.ingredients import build_night_mode_text
            _tracked['night_mode'] = build_night_mode_text()
            system_content += _tracked['night_mode']
        if phone_browse_mode:
            from context_builder.ingredients import build_phone_browse_text
            _tracked['phone_browse'] = build_phone_browse_text()
            system_content += _tracked['phone_browse']
        if music_cochlea:
            from context_builder.ingredients import build_music_cochlea_section
            section = build_music_cochlea_section()
            if section:
                _tracked['music_cochlea'] = section
                system_content += section
        if memory_sections:
            system_content += guide_for('memories', {}) + '\n' + memory_section + '\n\n'
        if self_memory_section:
            system_content += guide_for('self_memories', {}) + '\n' + self_memory_section + '\n\n'
        if world_book_entries:
            wb_lines = ['## 世界书参考']
            for entry in world_book_entries:
                if isinstance(entry, dict):
                    wb_lines.append(f"### {entry.get('name', '')}")
                    body = entry.get('body', '') or ''
                else:
                    wb_lines.append(f'### {entry.name}')
                    body = entry.body if entry.body else ''
                wb_lines.append(body)
                wb_lines.append('')
            _tracked['world_book'] = '\n'.join(wb_lines).strip()
            system_content += _tracked['world_book'] + '\n\n'
        if day_before_section:
            system_content += day_before_section + '\n\n'
        if yesterday_section:
            system_content += yesterday_section + '\n\n'
        if gap_section:
            system_content += gap_section + '\n\n'
        if referenced_diary:
            system_content += guide_for('referenced_diary', {'referenced_diary_date': referenced_diary_date}) + '\n' + referenced_diary + '\n\n'
        if important_events_section:
            system_content += important_events_section + '\n\n'
        ledger_reminder = _build_ledger_reminder()
        if ledger_reminder:
            system_content += ledger_reminder + '\n\n'
        if is_wander_active:
            from context_builder.ingredients import build_wander_mode_text
            _tracked['wander_mode'] = build_wander_mode_text('full')
            system_content += _tracked['wander_mode']
        from context_builder.ingredients import MESSAGE_MARKERS_TEXT
        _tracked['message_markers'] = MESSAGE_MARKERS_TEXT
        system_content += _tracked['message_markers']
        if system_content:
            formatted_messages.append({'role': 'system', 'content': system_content.strip()})
        if conversation_memories is None:
            conversation_memories = [m for m in memories if m.source == 'conversation']
        conversation_memories.sort(key=lambda m: parse_ts(m.metadata.get('timestamp', '')))
        if time_awareness_hint:
            formatted_messages.append({'role': 'system', 'content': time_awareness_hint})
        _status_entries = []
        try:
            from wander_manager.user_status import get_status_history
            from context_builder.status_history import context_history_start, project_status_history, history_sort_key
            from conversation_manager_v2 import resolve_active_date
            _raw_history = get_status_history(since=context_history_start(conversation_memories, active_date=resolve_active_date(session_id)))
            _status_entries = sorted(project_status_history(_raw_history), key=history_sort_key)
        except Exception:
            pass
        _status_idx = 0
        from api_sources.model_context import labels_for_ids
        generation_models = labels_for_ids((m.metadata.get('message_id') or getattr(m, 'id', '') for m in conversation_memories))
        for memory in conversation_memories:
            mem_ts = memory.metadata.get('timestamp', '')
            while _status_idx < len(_status_entries):
                _se = _status_entries[_status_idx]
                if history_sort_key(_se) < history_sort_key({'_ts': mem_ts}):
                    formatted_messages.append({key: value for key, value in _se.items() if not key.startswith('_')})
                    _status_idx += 1
                else:
                    break
            role = memory.metadata.get('role', 'user')
            if is_ui_only_role(role):
                continue
            content = memory.content
            if ': ' in content:
                content = content.split(': ', 1)[1]
            if role == 'assistant':
                content = strip_internal_history_markers(content)
                from tool_action_protocol import strip_tool_protocol_text
                content = strip_tool_protocol_text(content)
            time_label = self._format_relative_time(memory.metadata.get('timestamp', ''))
            is_wander = memory.metadata.get('is_wander')
            is_sentinel = memory.metadata.get('is_sentinel')
            is_reminder = memory.metadata.get('is_reminder')
            if is_sentinel:
                label = f'[🛡️ {time_label}] ' if time_label else '[🛡️] '
            elif is_reminder:
                label = f'[⏰ {time_label}] ' if time_label else '[⏰] '
            elif is_wander:
                label = f'[💭 {time_label}] ' if time_label else '[💭] '
            elif time_label:
                label = f'[{time_label}] '
            else:
                label = ''
            if role == 'user':
                _va = memory.metadata.get('voice_attachment')
                if _va and isinstance(_va, dict):
                    _tone = _va.get('tone')
                    label += f'（语音·{_tone}）' if _tone else '（语音）'
            generation_model = generation_models.get(str(memory.metadata.get('message_id') or getattr(memory, 'id', '')))
            model_header = f'【生成模型：{generation_model}】\n' if role == 'assistant' and generation_model else ''
            msg_dict = {'role': role, 'content': model_header + label + content}
            formatted_messages.append(msg_dict)
            fact = build_tool_history_fact(role, memory.metadata.get('tool_summary'), memory.metadata.get('tool_calls'), memory.metadata.get('event_type', ''))
            if fact:
                formatted_messages.append({'role': 'system', 'content': fact})
        while _status_idx < len(_status_entries):
            _se = _status_entries[_status_idx]
            formatted_messages.append({key: value for key, value in _se.items() if not key.startswith('_')})
            _status_idx += 1
        try:
            sentinel_annotations = self._build_sentinel_timeline()
            for sa in sentinel_annotations:
                formatted_messages.append({'role': 'system', 'content': sa['content']})
        except Exception:
            pass
        if not conversation_memories or conversation_memories[-1].metadata.get('role') != 'user':
            formatted_messages.append({'role': 'user', 'content': user_message})
        stats = {'total_memories_retrieved': len(memories), 'memories_in_context': len(memories[:max_memories]), 'persona_provided': bool(persona), 'profile_provided': bool(user_profile), 'context_length': len(formatted_context), 'average_memory_similarity': np.mean([m.similarity for m in memories]) if memories else 0.0, 'average_memory_importance': np.mean([m.importance for m in memories]) if memories else 0.0}
        try:
            logger.info(f'Snapshot: entering capture block')
            sections = {}
            _map = [('persona', persona_section), ('persona_anchor', _tracked['anchor']), ('self_book', _tracked['self_book']), ('timeline_anchor', timeline_anchor), ('time', time_section), ('status', status_section), ('period', period_section), ('away', _tracked['away']), ('night_mode', _tracked['night_mode']), ('hr', _tracked['hr']), ('sentinel', _tracked['sentinel']), ('phone_browse', _tracked['phone_browse']), ('memory', guide_for('memories', {}) + '\n' + memory_section if memory_sections else ''), ('world_book', _tracked['world_book']), ('day_before', day_before_section), ('yesterday', yesterday_section), ('gap', gap_section), ('referenced_diary', guide_for('referenced_diary', {'referenced_diary_date': referenced_diary_date}) + '\n' + referenced_diary if referenced_diary else ''), ('important_events', important_events_section), ('wander_mode', _tracked['wander_mode']), ('message_markers', _tracked.get('message_markers', ''))]
            for name, text in _map:
                if text and text.strip():
                    sections[name] = {'text': text, 'tokens': _estimate_tokens(text), 'chars': len(text)}
            if time_awareness_hint:
                sections['time_awareness'] = {'text': time_awareness_hint, 'tokens': _estimate_tokens(time_awareness_hint), 'chars': len(time_awareness_hint)}
            msgs = []
            conversation_tokens = 0
            system_tokens_est = _estimate_tokens(system_content)
            for i, msg in enumerate(formatted_messages):
                content = msg.get('content', '') or ''
                role = msg.get('role', 'unknown')
                tok = _estimate_tokens(content)
                if role == 'system':
                    pass
                else:
                    conversation_tokens += tok
                msgs.append({'index': i, 'role': role, 'tokens': tok, 'chars': len(content), 'content': content})
            total_tokens = system_tokens_est + conversation_tokens
            section_groups = [{'group': 'A', 'label': '身份层', 'desc': 'AI 是谁，首因效应窗口', 'keys': ['persona', 'persona_anchor', 'self_book', 'timeline_anchor', 'time']}, {'group': 'B', 'label': '当前情境感知', 'desc': '此刻的状态', 'keys': ['status', 'period', 'away', 'night_mode', 'hr', 'sentinel', 'phone_browse']}, {'group': 'D', 'label': '历史参考', 'desc': '过去发生的事，已明确标记', 'keys': ['day_before', 'yesterday', 'gap', 'referenced_diary', 'important_events']}, {'group': 'E', 'label': '系统功能说明', 'desc': '工具/模式等系统指令，仅条件注入', 'keys': ['wander_mode', 'message_markers']}, {'group': 'F', 'label': '消息间注入', 'desc': '在 system prompt 之外、对话消息之间注入的上下文', 'keys': ['time_awareness']}]
            for g in section_groups:
                g['sections'] = {k: sections[k] for k in g['keys'] if k in sections}
                g['group_tokens'] = sum((s.get('tokens', 0) for s in g['sections'].values()))
            with _last_context_snapshot_lock:
                _last_context_snapshot.clear()
                _last_context_snapshot.update({'timestamp': datetime.now().isoformat(), 'total_tokens': total_tokens, 'total_chars': sum((m['chars'] for m in msgs)), 'token_breakdown': {'system_tokens': system_tokens_est, 'conversation_tokens': conversation_tokens, 'total_tokens': total_tokens}, 'messages_count': len(msgs), 'messages': msgs, 'system_sections': sections, 'section_groups': section_groups, 'system_tokens': system_tokens_est, 'system_chars': len(system_content) if system_content else 0})
        except Exception as e:
            logger.warning(f'Snapshot capture failed: {e}', exc_info=True)
            pass
        return ContextResult(user_message=user_message, persona=persona, user_profile=user_profile, memories=memories[:max_memories], formatted_context=formatted_context, formatted_messages=formatted_messages, stats=stats)

    async def schedule_context(self, user_message: str, persona: str='', user_profile: str='', force_retrieve: bool=False, session_id: str='', is_new_topic: bool=False, inject_yesterday: bool=False, away_duration_seconds: Optional[float]=None, away_reason: Optional[str]=None, group_chat_active_seconds_ago: Optional[float]=None, night_mode: bool=False, phone_browse_mode: bool=False, music_cochlea: bool=False, time_awareness_hint: Optional[str]=None, current_message_id: Optional[str]=None, is_wander_active: bool=False) -> ContextResult:
        """
        一站式上下文调度：执行完整的调度流程

        Args:
            user_message: 用户消息
            persona: 人设提示词
            user_profile: 用户资料
            force_retrieve: 是否强制检索（忽略触发判断）
            session_id: 会话ID，用于检索当前对话上下文
            is_new_topic: 是否为新话题首轮（用于注入gap日记/重要事件等一次性内容）
            inject_yesterday: 是否注入昨日完整对话+前天日记（每轮，同一活跃日首话题内）

        Returns:
            ContextResult: 完整的上下文调度结果
        """
        global _last_pipeline_run
        pipeline_start = _time.time()
        _last_pipeline_run = datetime.now().isoformat()
        _log_to_file(f'开始上下文调度 | 用户消息: {user_message[:100]}... | session_id: {session_id} | force_retrieve: {force_retrieve} | is_new_topic: {is_new_topic}')
        t0 = _time.time()
        try:
            await self.initialize()
            _record_checkpoint('initialize', 'ok', (_time.time() - t0) * 1000, '模型已加载' if self.initialized else '跳过')
        except Exception as e:
            _record_checkpoint('initialize', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
        t0 = _time.time()
        trigger_result = await self.detect_trigger(user_message)
        _record_checkpoint('trigger_detect', 'ok', (_time.time() - t0) * 1000, f'触发={trigger_result.triggered} conf={trigger_result.confidence:.1f}' if trigger_result.triggered else '未触发')
        _log_to_file(f'触发判断 | triggered: {trigger_result.triggered} | confidence: {trigger_result.confidence:.3f} | reason: {trigger_result.reason}')
        t0 = _time.time()
        query_plan = await self.plan_query(user_message, trigger_result)
        from cognition.entity_names import expand_query
        query_plan.enhanced_query = expand_query(query_plan.enhanced_query or user_message)
        _record_checkpoint('query_plan', 'ok', (_time.time() - t0) * 1000, f'方法={query_plan.retrieval_method}')
        _log_to_file(f'查询规划 | enhanced_query: {query_plan.enhanced_query[:100]}... | method: {query_plan.retrieval_method} | backend: memory_v2 | limit: {query_plan.max_results}')
        memories = []
        _legacy_memory_loaded = False
        _use_new_context_builder = os.environ.get('MIRROW_USE_CONTEXT_BUILDER') == '1'
        try:
            from memory_v2.context_shadow import memory_v2_context_mode
            _memory_v2_enabled_for_recall = memory_v2_context_mode() == 'enabled'
        except Exception:
            _memory_v2_enabled_for_recall = False

        async def _load_legacy_memories():
            nonlocal memories, _legacy_memory_loaded
            if _legacy_memory_loaded:
                return memories
            _legacy_memory_loaded = True
            started = _time.time()
            if trigger_result.triggered or force_retrieve:
                try:
                    memories = await self.retrieve_memories(query_plan, session_id)
                    _record_checkpoint('memory_retrieval', 'ok', (_time.time() - started) * 1000, f'旧链回退·全量 {len(memories)}条')
                    _log_to_file(f'记忆检索(旧链回退·全量) | 检索到 {len(memories)} 条记忆')
                except Exception as e:
                    _record_checkpoint('memory_retrieval', 'fail', (_time.time() - started) * 1000, '', str(e)[:100])
                    _log_to_file(f'记忆检索(旧链回退·全量)失败: {e}')
            else:
                try:
                    light_plan = replace(query_plan, max_results=3, reasoning='轻量检索(无触发词)')
                    memories = await self.retrieve_memories(light_plan, session_id)
                    _record_checkpoint('memory_retrieval', 'ok', (_time.time() - started) * 1000, f'旧链回退·轻量 {len(memories)}条 (top-3)')
                    _log_to_file(f'记忆检索(旧链回退·轻量) | 检索到 {len(memories)} 条记忆')
                except Exception as e:
                    _record_checkpoint('memory_retrieval', 'fail', (_time.time() - started) * 1000, '', str(e)[:100])
                    _log_to_file(f'记忆检索(旧链回退·轻量)失败: {e}')
            return memories
        _defer_legacy_memory = _use_new_context_builder and _memory_v2_enabled_for_recall
        if _defer_legacy_memory:
            _record_checkpoint('memory_retrieval', 'skip', 0, 'Memory V2 enabled，旧链仅故障回退')
            _log_to_file('记忆检索(旧链) | Memory V2 enabled，延迟到故障回退')
        else:
            await _load_legacy_memories()
        conversation_memories = []
        if session_id:
            t0 = _time.time()
            try:
                conversation_memories = await self._get_conversation_memories(session_id, user_message, current_message_id)
                _record_checkpoint('conv_memories', 'ok', (_time.time() - t0) * 1000, f'{len(conversation_memories)}条消息')
            except Exception as e:
                _record_checkpoint('conv_memories', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
                conversation_memories = []
            _log_to_file(f'对话记忆 | 获取了 {len(conversation_memories)} 条对话消息（分池注入）')
        else:
            _record_checkpoint('conv_memories', 'skip', 0, '无session_id')
        yesterday_conversation = None
        yesterday_compact = None
        yesterday_diary_fallback = None
        day_before_diary = None
        gap_diaries_text = None
        important_events_section = ''
        yesterday = ''
        day_before = ''
        t0 = _time.time()
        if is_new_topic or inject_yesterday:
            try:
                from event_chronicle import get_global_chronicle, _read_yesterday_messages_formatted
                chronicle = get_global_chronicle()
                now = datetime.now()
                try:
                    from memory_v2.context_shadow import memory_v2_context_mode
                    _memory_v2_enabled = memory_v2_context_mode() == 'enabled'
                except Exception:
                    _memory_v2_enabled = False
                last_active = chronicle.get_last_active_date()
                if last_active and session_id:
                    try:
                        from summary import topic_state_machine
                        from time_utils import to_beijing_time
                        topic_status = await topic_state_machine.get_topic_status(session_id)
                        if topic_status.get('is_active') and topic_status.get('start_time'):
                            start_ts = topic_status['start_time']
                            start_beijing = to_beijing_time(start_ts)
                            topic_start_date = start_beijing[:10]
                            if last_active >= topic_start_date:
                                topic_dt = datetime.strptime(topic_start_date, '%Y-%m-%d')
                                last_active = (topic_dt - timedelta(days=1)).strftime('%Y-%m-%d')
                                _log_to_file(f'活跃话题覆盖: 话题始于{topic_start_date}(北京), 昨日注入回溯至{last_active}')
                    except Exception:
                        pass
                yesterday = last_active if last_active else (now - timedelta(days=1)).strftime('%Y-%m-%d')
                day_before = chronicle.get_last_active_date(before_date=last_active) if last_active else None
                if yesterday:
                    if _memory_v2_enabled:
                        from context_builder.cross_day_memory import build_cross_day_memory_context
                        yesterday_compact = build_cross_day_memory_context(chronicle, yesterday, now=now, session_id=session_id)
                        if yesterday_compact:
                            _log_to_file(f'Memory V2 跨日渐退投影 ({len(yesterday_compact)} 字符)')
                    else:
                        yesterday_conversation = _read_yesterday_messages_formatted(target_date=yesterday)
                        if yesterday_conversation:
                            _log_to_file(f'注入活跃日对话记录 ({len(yesterday_conversation)} 字符)')
                        else:
                            entry = chronicle.get_diary_entry_by_date(yesterday)
                            if entry:
                                yesterday_diary_fallback = entry.content
                                _log_to_file(f'无活跃日消息，回退注入日记: {entry.date} len={len(entry.content)}')
                                logger.warning(f'context_scheduler: {yesterday} 无对话记录，回退注入日记 (entry_date={entry.date} entry_len={len(entry.content)})')
                            else:
                                _log_to_file(f'无活跃日消息，且无日记回退: {yesterday}')
                                logger.warning(f'context_scheduler: {yesterday} 无对话记录且无日记回退')
                if day_before and (not _memory_v2_enabled):
                    entry = chronicle.get_diary_entry_by_date(day_before)
                    if entry:
                        day_before_diary = entry.content
                        _log_to_file(f'注入前活跃日日记: {entry.date} -> {entry.content[:60]}...')
                nearby = None
                lines = []
                if inject_yesterday:
                    if yesterday:
                        calendar_yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                        if yesterday < calendar_yesterday:
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
                                gap_diaries_text = '\n'.join(gap_parts)
                                _log_to_file(f'新话题 | 注入Gap期日记 {len(gap_parts)} 天: {gap_parts[0][:60]}...')
                    nearby = chronicle.get_nearby_important_events(now.strftime('%Y-%m-%d'), days=1)
                    if nearby:
                        lines = []
                        for ev in nearby[:8]:
                            d_str = ev.date_string
                            if ev.date_precision == 'full':
                                if d_str == now.strftime('%Y-%m-%d'):
                                    d_str = '今天'
                                elif yesterday and d_str == yesterday:
                                    d_str = '昨天'
                            elif ev.date_precision == 'year_month':
                                d_str = d_str.replace('-00', '月')
                            elif ev.date_precision == 'year_only':
                                d_str = d_str.replace('-00-00', '年')
                            em = f'（{ev.emotion}）' if ev.emotion else ''
                            lines.append(f'- {d_str} {ev.event_text}{em}')
                        important_events_section = '## 近期重要事件\n' + '\n'.join(lines)
                        _log_to_file(f'新话题 | 注入重要事件 {len(lines)} 条')
                    else:
                        _log_to_file('新话题 | 暂无近期重要事件')
                _summary = f"昨天={yesterday} 投影={('有' if yesterday_compact else '对话' if yesterday_conversation else '日记回退' if yesterday_diary_fallback else '无')} 前天日记={('有' if day_before_diary else '无')} gap={(len(gap_diaries_text) if gap_diaries_text else 0)} 事件={(len(nearby) if nearby else 0)}"
                _record_checkpoint('diary_inject', 'ok', (_time.time() - t0) * 1000, _summary)
            except Exception as e:
                _record_checkpoint('diary_inject', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
                logger.warning(f'获取日记/重要事件失败: {e}')
                _log_to_file(f'获取日记/重要事件失败: {e}')
        else:
            _record_checkpoint('diary_inject', 'skip', (_time.time() - t0) * 1000, '非新话题')
        referenced_diary = None
        referenced_diary_date = ''
        t0 = _time.time()
        try:
            try:
                from event_chronicle import get_global_chronicle as _gc_date_ref
                _chr_date = _gc_date_ref()
            except Exception:
                _chr_date = None
            target_date, date_description = self._extract_calendar_reference(user_message, chronicle=_chr_date)
            if target_date:
                boundary = day_before if day_before else (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
                if target_date < boundary:
                    from event_chronicle import get_global_chronicle
                    chronicle = get_global_chronicle()
                    entry = chronicle.get_diary_entry_by_date(target_date)
                    if entry:
                        date_label = date_description if date_description else target_date
                        month_str = target_date[5:7].lstrip('0')
                        day_str = target_date[8:10].lstrip('0')
                        date_label_fallback = f'{month_str}月{day_str}日'
                        display_date = date_description or date_label_fallback
                        referenced_diary = entry.content
                        referenced_diary_date = display_date
                        _log_to_file(f'日期检索 | {target_date} -> {entry.content[:60]}...')
                else:
                    _log_to_file(f'日期检索 | {target_date} 在守卫范围内（≥{boundary}），跳过避免与昨天/前天注入重复')
            if not target_date and self._may_contain_semantic_date_reference(user_message):
                try:
                    semantic_result = await self._extract_semantic_date(user_message)
                    if semantic_result:
                        target_date = semantic_result.get('date')
                        date_description = semantic_result.get('description')
                        if target_date:
                            boundary = day_before if day_before else (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
                            if target_date < boundary:
                                from event_chronicle import get_global_chronicle
                                chronicle = get_global_chronicle()
                                entry = chronicle.get_diary_entry_by_date(target_date)
                                if entry:
                                    display_date = date_description or target_date
                                    referenced_diary = entry.content
                                    referenced_diary_date = display_date
                                    _log_to_file(f'语义日期检索 | {target_date} ({date_description}) -> {entry.content[:60]}...')
                            else:
                                _log_to_file(f'语义日期检索 | {target_date} 在守卫范围内（≥{boundary}），跳过')
                except Exception as e:
                    logger.debug(f'语义日期匹配失败（非关键路径）: {e}')
            elif not target_date:
                _log_to_file('语义日期检索 | 无日期线索，跳过 Flash')
            _record_checkpoint('date_reference', 'ok', (_time.time() - t0) * 1000, f'命中={target_date}' if target_date else '未命中')
        except Exception as e:
            _record_checkpoint('date_reference', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
            logger.warning(f'日期检索日记失败: {e}')
        t0 = _time.time()
        world_book_entries = []
        t0 = _time.time()
        try:
            from world_book import get_global_world_book
            wb = get_global_world_book()
            world_book_entries = wb.search(user_message, session_id=session_id)
            _record_checkpoint('worldbook_search', 'ok', (_time.time() - t0) * 1000, f'{len(world_book_entries)}条' if world_book_entries else '无命中')
            if world_book_entries:
                _log_to_file(f'世界书检索 | 命中 {len(world_book_entries)} 个词条: {[e.name for e in world_book_entries]}')
        except ImportError:
            _record_checkpoint('worldbook_search', 'skip', (_time.time() - t0) * 1000, '模块未加载')
        except Exception as e:
            _record_checkpoint('worldbook_search', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
            logger.warning(f'世界书检索失败: {e}')
        self_book_entries = []
        t0 = _time.time()
        try:
            from self_book import get_global_self_book
            sb = get_global_self_book()
            self_book_entries = sb.search(user_message, session_id=session_id)
            _record_checkpoint('selfbook_search', 'ok', (_time.time() - t0) * 1000, f'{len(self_book_entries)}条' if self_book_entries else '无命中')
            if self_book_entries:
                _log_to_file(f'自我书检索 | 命中 {len(self_book_entries)} 个词条: {[e.name for e in self_book_entries]}')
        except ImportError:
            _record_checkpoint('selfbook_search', 'skip', (_time.time() - t0) * 1000, '模块未加载')
        except Exception as e:
            _record_checkpoint('selfbook_search', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
            logger.warning(f'自我书检索失败: {e}')
        if query_plan.intent != 'time_travel' and memories and yesterday_conversation and yesterday:
            t0 = _time.time()
            original_count = len(memories)
            filtered = []
            for mem in memories:
                if mem.source == 'conversation':
                    filtered.append(mem)
                    continue
                created = mem.metadata.get('created', '')
                mem_date = created[:10] if created else ''
                is_merged = mem.metadata.get('is_merged', False)
                has_timeline = bool(mem.metadata.get('time_label', '')) and '|' in mem.metadata.get('time_label', '')
                has_state_vars = bool(mem.metadata.get('state_variables', []))
                if mem_date != yesterday:
                    filtered.append(mem)
                    continue
                if is_merged or has_timeline or has_state_vars:
                    filtered.append(mem)
                    continue
                mem_words = _tokenize_for_dedup(mem.content[:800])
                if len(mem_words) < 4:
                    filtered.append(mem)
                    continue
                yst_words = _tokenize_for_dedup((yesterday_conversation or '')[:3000])
                if len(yst_words) < 4:
                    filtered.append(mem)
                    continue
                intersection = len(mem_words & yst_words)
                union = len(mem_words | yst_words)
                jaccard = intersection / union if union > 0 else 0
                if jaccard > 0.55:
                    _log_to_file(f"记忆去重: 过滤 '{mem.content[:60]}...' (Jaccard={jaccard:.0%} 与昨日对话重叠)")
                    continue
                filtered.append(mem)
            memories = filtered
            _record_checkpoint('dedup_memories', 'ok', (_time.time() - t0) * 1000, f'{original_count}→{len(memories)}条 (过滤{original_count - len(memories)}条昨日冗余)')
            if original_count != len(memories):
                _log_to_file(f'记忆去重 | {original_count}→{len(memories)} 条 (过滤 {original_count - len(memories)} 条昨日冗余)')
        elif not yesterday_conversation:
            _record_checkpoint('dedup_memories', 'skip', 0, '无昨日对话，跳过')
        t0 = _time.time()
        try:

            async def _legacy_build():
                if not _legacy_memory_loaded:
                    await _load_legacy_memories()
                return await self.build_context(user_message=user_message, memories=memories, persona=persona, user_profile=user_profile, yesterday_compact=yesterday_compact, yesterday_conversation=yesterday_conversation, yesterday_diary_fallback=yesterday_diary_fallback, day_before_diary=day_before_diary, gap_diaries_text=gap_diaries_text, yesterday_date=yesterday if yesterday else '', day_before_date=day_before if day_before else '', referenced_diary=referenced_diary, referenced_diary_date=referenced_diary_date, important_events_section=important_events_section, away_duration_seconds=away_duration_seconds, away_reason=away_reason, night_mode=night_mode, phone_browse_mode=phone_browse_mode, music_cochlea=music_cochlea, world_book_entries=world_book_entries if world_book_entries else None, self_book_entries=self_book_entries if self_book_entries else None, time_awareness_hint=time_awareness_hint, is_wander_active=is_wander_active, conversation_memories=conversation_memories, session_id=session_id)
            _use_new = _use_new_context_builder
            logger.info(f"Snapshot: MIRROW_USE_CONTEXT_BUILDER={os.environ.get('MIRROW_USE_CONTEXT_BUILDER', 'unset')} use_new={_use_new}")
            if _use_new:
                from context_builder import ContextBuilder, context_diff_enabled, maybe_diff_contexts
                from context_builder.ingredients import build_night_mode_text, build_phone_browse_text, build_wander_mode_text
                _night_text = build_night_mode_text() if night_mode else ''
                _phone_text = build_phone_browse_text() if phone_browse_mode else ''
                _wander_text = build_wander_mode_text('full') if is_wander_active else ''
                _away_info = self._build_away_info_section(away_duration_seconds, away_reason, group_chat_active_seconds_ago) if away_duration_seconds and away_duration_seconds >= 60 and away_reason else ''
                _raw_status = []
                try:
                    from wander_manager.user_status import get_status_history
                    from context_builder.status_history import context_history_start
                    from conversation_manager_v2 import resolve_active_date
                    _raw_status = get_status_history(since=context_history_start(conversation_memories, active_date=resolve_active_date(session_id)))
                except Exception:
                    pass
                _recent_activity = ''
                try:
                    _priv_sec = None
                    _grp_sec = group_chat_active_seconds_ago
                    from shared_state import get_last_private_chat_time as _gpc
                    _pl = _gpc()
                    if _pl:
                        _priv_sec = (datetime.now() - _pl).total_seconds()
                    else:
                        try:
                            from event_chronicle import get_global_chronicle as _gc_recent
                            from time_utils import parse_ts as _pts_recent
                            _recent_msgs = _gc_recent().get_messages_by_session_id(session_id, 100, desc=True) if session_id else []
                            _recent_msgs = [m for m in _recent_msgs if m.get('role') == 'user' and m.get('event_type') not in {'ui_action:visit_lounge', 'ui_action:play_game', 'ui_action:visit_workshop'} and (not is_ui_only_role(m.get('role'))) and (not any((m.get(flag) for flag in ('is_wander', 'is_sentinel', 'is_reminder')))) and str(m.get('content') or '').strip()]
                            if _recent_msgs:
                                _rts = (_recent_msgs[-1] or {}).get('timestamp', '')
                                if _rts:
                                    _priv_sec = (datetime.now() - _pts_recent(_rts).replace(tzinfo=None)).total_seconds()
                        except Exception:
                            pass
                    from context_builder.ingredients import build_recent_activity_section
                    _recent_activity = build_recent_activity_section(_priv_sec, _grp_sec)
                except Exception:
                    pass
                _rift_recipe = 'FULL_CHAT'
                _rift_context = ''
                try:
                    from rift_archive import get_rift_archive_service
                    _rift_service = get_rift_archive_service()
                    _rift_playing = _rift_service.observe_user_message(user_message)
                    if _rift_service.should_inject(user_message):
                        _rift_context = _rift_service.build_context()
                    if _rift_playing:
                        _rift_recipe = 'RIFT_PLAY'
                except Exception as _rift_exc:
                    logger.warning('Rift context unavailable: %s', type(_rift_exc).__name__)
                _fading_claimed = False
                if _rift_recipe == 'FULL_CHAT' and current_message_id:
                    try:
                        from affect import get_affect_service
                        from open_loops import get_open_loop_store
                        _affect_mode_for_claim = str(get_affect_service().settings().get('engine_mode') or get_affect_service().settings().get('context_mode') or 'shadow')
                        if _affect_mode_for_claim == 'enabled':
                            _fading_claimed = bool(get_open_loop_store().claim_fading_for_turn(str(current_message_id)))
                    except Exception:
                        logger.debug('open loop fading claim skipped', exc_info=True)
                _bk = dict(user_message=user_message, persona=persona, user_profile=user_profile, memories=memories, world_book_entries=world_book_entries, self_book_entries=self_book_entries, legacy_memory_loader=_load_legacy_memories, yesterday_compact=yesterday_compact, yesterday_conversation=yesterday_conversation, yesterday_diary_fallback=yesterday_diary_fallback, day_before_diary=day_before_diary, gap_diaries=gap_diaries_text, yesterday_date=yesterday if yesterday else '', day_before_date=day_before if day_before else '', referenced_diary=referenced_diary, referenced_diary_date=referenced_diary_date, important_events=important_events_section, away_duration_seconds=away_duration_seconds, away_reason=away_reason, night_mode=night_mode, phone_browse=phone_browse_mode, music_cochlea=music_cochlea, time_awareness_hint=time_awareness_hint, is_wander_active=is_wander_active, is_new_topic=is_new_topic, inject_yesterday=inject_yesterday, session_id=session_id, night_mode_text=_night_text, phone_browse_text=_phone_text, wander_mode_text=_wander_text, mood='', away_info=_away_info, away=bool(_away_info), recent_activity_text=_recent_activity, date_referenced=bool(referenced_diary), conversation_messages=conversation_memories, current_message_id=current_message_id or '', status_changes='', raw_status_history=_raw_status, rift_game_context_text=_rift_context)
                new_result = None
                try:
                    new_result = await ContextBuilder.build(_rift_recipe, **_bk)
                except Exception:
                    import traceback
                    logger.error(f'{_rift_recipe} 新 Builder 构建失败，回退旧 build_context: {traceback.format_exc()}')
                    if _fading_claimed and current_message_id:
                        try:
                            from open_loops import get_open_loop_store
                            get_open_loop_store().cancel_fading_offer(str(current_message_id))
                        except Exception:
                            logger.debug('open loop fading claim cancel skipped', exc_info=True)
                    try:
                        from context_builder import record_recipe_call
                        record_recipe_call('AI 私聊（回退）', 'FULL_CHAT_FALLBACK', sections={'reason': '新 Builder 构建异常，已回退旧管道'})
                    except Exception:
                        pass
                if new_result is not None:
                    if context_diff_enabled() and _rift_recipe == 'FULL_CHAT':
                        try:
                            old_result = await _legacy_build()
                            await maybe_diff_contexts('FULL_CHAT', old_result, new_result)
                        except Exception as _diff_e:
                            logger.warning(f'[CONTEXT_DIFF] 对比失败（跳过，不影响主链路）: {_diff_e}')
                    context_result = new_result
                else:
                    try:
                        context_result = await _legacy_build()
                    except Exception as _legacy_e:
                        logger.error(f'FULL_CHAT Builder+旧管道双失败，使用最小上下文: {_legacy_e}')
                        from context_builder.builder import ContextResult
                        _min = ContextResult()
                        _min.system_content = (persona or '').strip()
                        _min.dynamic_content = ''
                        _min.formatted_messages = [{'role': 'system', 'content': _min.system_content}] if _min.system_content else []
                        _min.memories = list(memories) if memories else []
                        context_result = _min
            else:
                context_result = await _legacy_build()
                try:
                    from context_builder import record_recipe_call
                    sc = context_result.system_content or ''
                    record_recipe_call('AI 私聊 (Legacy)', 'FULL_CHAT', sections={'system': {'text': sc[:200], 'chars': len(sc)}}, total_chars=len(sc), total_tokens=0)
                except Exception:
                    pass
            _context_message_count = len(getattr(context_result, 'context_messages', None) or [])
            _record_checkpoint('context_build', 'ok', (_time.time() - t0) * 1000, f'消息{len(context_result.formatted_messages) + _context_message_count}条 记忆{len(context_result.memories)}条')
        except Exception as e:
            _record_checkpoint('context_build', 'fail', (_time.time() - t0) * 1000, '', str(e)[:100])
            raise
        _context_message_count = len(getattr(context_result, 'context_messages', None) or [])
        _log_to_file(f'上下文构建完成 | 消息数: {len(context_result.formatted_messages) + _context_message_count} | 记忆数: {len(context_result.memories)}')
        _actual_prompt = '\n'.join((str(m.get('content', '')) for m in context_result.formatted_messages))
        _actual_prompt += '\n' + str(getattr(context_result, 'dynamic_content', '') or '')
        _actual_prompt += '\n' + '\n'.join((str(m.get('content', '')) for m in getattr(context_result, 'context_messages', None) or []))
        _actual_prompt += '\n' + '\n'.join((str(m.get('content', '')) for m in getattr(context_result, 'tail_messages', None) or []))
        context_result.stats.update({'triggered': trigger_result.triggered, 'trigger_confidence': trigger_result.confidence, 'retrieval_method': query_plan.retrieval_method, 'retrieval_backend': 'memory_v2', 'ignored_retrieval_parameters': list(LEGACY_RETRIEVAL_PARAMETERS), 'session_id_provided': bool(session_id), 'conversation_memories_added': len(conversation_memories), 'world_book_entries_injected': len(world_book_entries) if world_book_entries else 0})
        overall_ms = (_time.time() - pipeline_start) * 1000
        _log_to_file(f'上下文调度完成 | 总记忆: {len(memories)} | 触发: {trigger_result.triggered} | WB: {(len(world_book_entries) if world_book_entries else 0)}词条')
        _record_checkpoint('overall', 'ok', overall_ms, f'总耗时{overall_ms:.0f}ms')
        return context_result

    async def close(self):
        """清理资源"""
        if self.http_client:
            await self.http_client.aclose()
        self.initialized = False
        logger.info('上下文调度器资源已清理')
        _log_to_file('上下文调度器已关闭')
        from context_scheduler_log import close_log
        close_log()
_global_scheduler = None

async def get_global_scheduler(config_path: Optional[str]=None) -> ContextScheduler:
    """获取全局上下文调度器实例"""
    global _global_scheduler
    if _global_scheduler is None:
        _global_scheduler = ContextScheduler(config_path)
        await _global_scheduler.initialize()
    return _global_scheduler
