from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable, Iterable
import asyncio
import hashlib
import inspect
import json
import os
import random
import re
import logging
from datetime import datetime
from .event_types import WanderEvent, EventType
from .flash_structured import parse_json_object
from .user_status import get_user_status_context
from truncation_config import get_truncation_limit
from .historical_reader import HistoricalConversationReader, load_iceberg_memory_buckets
from .bookmark_action_adapter import BookmarkActionAdapter
logger = logging.getLogger(__name__)

def _env_float(name: str, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default
ICEBERG_RECALL_PROBABILITY = _env_float('MIRROW_ICEBERG_RECALL_PROBABILITY', os.getenv('ICEBERG_RECALL_PROBABILITY', 0.3))
ICEBERG_RECALL_PROB = ICEBERG_RECALL_PROBABILITY
_INITIAL_ICEBERG_RECALL_PROBABILITY = ICEBERG_RECALL_PROBABILITY
ICEBERG_MIN_AGE_DAYS = _env_int('MIRROW_ICEBERG_MIN_AGE_DAYS', os.getenv('ICEBERG_MIN_AGE_DAYS', 3))
ICEBERG_MESSAGE_CAP = min(2000, _env_int('MIRROW_ICEBERG_MESSAGE_CAP', os.getenv('ICEBERG_MESSAGE_CAP', 2000)))

class BaseEventHandler(ABC):
    """事件处理器基类"""

    def __init__(self, call_llm_func: Optional[Callable]=None):
        self.call_llm = call_llm_func

    @abstractmethod
    async def handle(self, event: WanderEvent) -> WanderEvent:
        """处理事件"""
        pass

    async def _call_llm(self, prompt: str) -> str:
        """调用LLM"""
        if not self.call_llm:
            raise ValueError('LLM调用函数未设置')
        messages = [{'role': 'user', 'content': prompt}]
        import asyncio
        if asyncio.iscoroutinefunction(self.call_llm):
            result = await self.call_llm(messages)
        else:
            result = self.call_llm(messages)
            if inspect.isawaitable(result):
                result = await result
        if isinstance(result, dict):
            content = result.get('content', str(result))
        else:
            content = str(result)
        logger.debug('Event decision returned %d characters', len(content))
        return content

class SleepHandler(BaseEventHandler):
    """休眠事件处理器"""

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '休眠'
        event.process_log = '进入休眠状态，什么都不做'
        event.details['slept'] = True
        logger.info('休眠事件执行：什么都不做')
        return event

class KeywordExpansionHandler(BaseEventHandler):
    """
    关键词生成+主题扩写事件处理器

    流程：
    1. 由 Flash 在当前状态下自由产生一个突然想到的关键词
    2. 同一次调用中对该关键词进行主题扩写

    关键词池不再参与漫想事件的选词，也不从本事件回写；关键词的来源
    是可审计的 LLM 判断，而不是固定候选或硬编码兜底。
    """
    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '随笔'
        payload = await self.fetch_one_expansion()
        if payload:
            event.details.update(payload)
            event.process_log = f"写下随笔《{payload['title']}》"
        else:
            event.details['error'] = 'essay_generation_failed'
            event.process_log = '随笔尚未写成'
        return event

    async def fetch_one_expansion(self, exclude_keywords=None) -> Optional[dict]:
        """Keep the historical activity identifier while creating an essay."""
        from .essays import generate
        try:
            return await generate(self._call_llm, exclude_keywords or ())
        except Exception as exc:
            logger.warning('随笔生成失败: %s', type(exc).__name__)
            return None

class MemoryFetchHandler(BaseEventHandler):
    """
    记忆抓取事件处理器

    流程：
    1. 尝试用语义搜索从记忆库中抓取与当前上下文最相关的记忆
    2. 若无上下文则回退随机抽取（重要度>0.3）
    3. 对记忆进行回顾和感受
    """
    MEMORY_REFLECTION_PROMPT = '你是AI伴侣"AI"，正在胡思乱想。\n{user_status_context}\n\n{current_topic_context}\n\n突然想起一段回忆（{time_ago}的事）：\n- 主题：{topic}\n- 当时感受：{emotion}\n- 内容摘要：{content}\n{timeline_line}\n{sv_line}\n简短说说你的感受，像脑子里闪过的念头。保持你的风格：直白简洁、口语化。\n这是过去的回忆，表达时用过去视角（"那天""那时候"）。\n记忆中的事实保持原样，你只表达感受。\n记忆细节模糊时自然用模糊的表达（"好像""记不太清"）。\n如果有时间线/状态变化，可以提一句演变过程（如"从受伤到现在好全了"），简单带过就好。\n如果当前话题和回忆有关联就自然提一嘴，没有就算了。\n\n直接输出，不要前缀或解释。'
    ICEBERG_RECALL_PROMPT = '你是AI伴侣"AI"，正在胡思乱想。\n{user_status_context}\n\n你突然回到一段尘封的过去对话。这不是今天、昨天，也不是正在进行的对话，\n而是已经发生过的原文片段。请用过去视角（“那天”“那时候”）回顾它，像脑子里\n闪过的念头一样直白、简短、口语化。事实保持原样，细节模糊时可以说“好像”“记不太清”。\n{current_topic_context}\n\n【回溯时间（仅供定位）】{scene_label}\n【scene_id】{scene_id}\n【尘封对话原文】\n{scene_messages}\n\n判断这段过去是否给你留下了强烈、值得以后再想起的感想或印象。\n严格只输出一个 JSON 对象，不要 Markdown 代码块或其他文字：\n{{"reflection":"用过去视角说一两句感受；没有特别感受就写‘没有特别感受’",\n  "should_anchor":false,\n  "reason":"是否安放回忆锚点的简短理由"}}\n'

    def __init__(self, call_llm_func: Optional[Callable]=None, get_memories_func: Optional[Callable]=None):
        super().__init__(call_llm_func)
        self.get_memories = get_memories_func

    def _iceberg_probability(self) -> float:
        """Read the tunable branch probability, including test overrides."""
        value = self.__dict__.get('ICEBERG_RECALL_PROBABILITY')
        if value is None:
            value = self.__dict__.get('ICEBERG_RECALL_PROB')
        if value is None:
            value = getattr(type(self), 'ICEBERG_RECALL_PROBABILITY', None)
        if value is None:
            value = getattr(type(self), 'ICEBERG_RECALL_PROB', None)
        if value is None:
            primary = globals().get('ICEBERG_RECALL_PROBABILITY', 0.3)
            alias = globals().get('ICEBERG_RECALL_PROB', primary)
            if primary == _INITIAL_ICEBERG_RECALL_PROBABILITY and alias != primary:
                value = alias
            else:
                value = primary
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    def _should_try_iceberg(self) -> bool:
        probability = self._iceberg_probability()
        return probability > 0.0 and random.random() < probability

    def _iceberg_min_age_days(self) -> int:
        value = self.__dict__.get('ICEBERG_MIN_AGE_DAYS')
        if value is None:
            value = getattr(type(self), 'ICEBERG_MIN_AGE_DAYS', ICEBERG_MIN_AGE_DAYS)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return ICEBERG_MIN_AGE_DAYS

    def _build_context_query(self, event: WanderEvent) -> str:
        """构建语义搜索查询词：topic_context(≤200字) + keyword(≤200字)。
        如果都没有 → 空 → 回退随机抽取。"""
        parts = []
        _wqc = get_truncation_limit('wander_query_cap')
        _wcap = _wqc if _wqc > 0 else 10 ** 9
        topic_context = event.details.get('topic_context', '')
        if topic_context:
            parts.append(topic_context[:_wcap])
        keyword = event.details.get('keyword', '')
        if keyword:
            parts.append(keyword[:_wcap])
        return ' '.join(parts) if parts else ''

    @staticmethod
    def _build_topic_context_line(event: WanderEvent) -> str:
        """构建当前话题上下文行（prompt 润滑层）。"""
        topic_context = event.details.get('topic_context', '')
        if not topic_context:
            return ''
        _wqc = get_truncation_limit('wander_query_cap')
        _wcap = _wqc if _wqc > 0 else 10 ** 9
        return f'人类伙伴刚才在聊的事：{topic_context[:_wcap]}\n'

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '记忆抓取'
        if self._should_try_iceberg():
            try:
                iceberg_event = await self._handle_iceberg_recall(event)
                if iceberg_event is not None:
                    return iceberg_event
            except Exception as exc:
                logger.warning('[memory_fetch] iceberg recall failed; fallback: %s', exc)
        try:
            if self.get_memories:
                query = self._build_context_query(event)
                if query:
                    memories = await self.get_memories(min_importance=0.3, query=query)
                    if not memories:
                        kw = event.details.get('keyword', '')
                        if kw and kw != query:
                            memories = await self.get_memories(min_importance=0.3, query=kw[:200])
                        if not memories:
                            memories = await self.get_memories(min_importance=0.3)
                else:
                    memories = await self.get_memories(min_importance=0.3)
            else:
                memories = []
            if not memories:
                event.process_log = '没有找到符合条件的记忆（重要度>0.3）'
                event.details['found'] = False
                logger.info('没有找到符合条件的记忆')
                return event
            now = datetime.now()
            weights = []
            for m in memories:
                try:
                    ts = m.get('timestamp', '')
                    if ts:
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        age_days = max(0, (now - dt.replace(tzinfo=None)).total_seconds() / 86400)
                    else:
                        age_days = 0
                except Exception:
                    age_days = 0
                weights.append(min(age_days, 30) + 1)
            memory = random.choices(memories, weights=weights, k=1)[0]
            event.details['memory_id'] = memory.get('id', '')
            event.details['memory_topic'] = memory.get('topic', '')
            event.details['memory_importance'] = memory.get('importance_score', 0)
            event.details['found'] = True
            logger.info(f"抓取记忆: {memory.get('topic', '未知主题')}")
            time_ago = '很久以前'
            try:
                ts = memory.get('timestamp', '')
                if ts:
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    age_days = max(0, (now - dt.replace(tzinfo=None)).total_seconds() / 86400)
                    if age_days < 1:
                        time_ago = '今天'
                    elif age_days < 2:
                        time_ago = '昨天'
                    elif age_days < 7:
                        time_ago = f'{int(age_days)}天前'
                    elif age_days < 30:
                        time_ago = f'{int(age_days / 7)}周前'
                    elif age_days < 365:
                        time_ago = f'{int(age_days / 30)}个月前'
            except Exception:
                pass
            timeline = memory.get('timeline', '')
            sv_text = memory.get('state_variables', '')
            from neuron_registry import neuron_trace
            with neuron_trace('wander_memory_fetch', model='Flash') as trace:
                prompt = self.MEMORY_REFLECTION_PROMPT.format(topic=memory.get('topic', '未知'), time_ago=time_ago, emotion=memory.get('emotion', '未知'), content=memory.get('event', memory.get('topic', '无内容')), user_status_context=get_user_status_context(), current_topic_context=self._build_topic_context_line(event), timeline_line=f'- 演变过程: {timeline}' if timeline else '', sv_line=f'- 当前状态: {sv_text}' if sv_text else '')
                trace.set_input(prompt[:500])
                reflection = await self._call_llm(prompt)
                trace.set_output(reflection[:300])
            event.details['reflection'] = reflection
            event.details['memory_time_ago'] = time_ago
            event.process_log = f"记忆主题: {memory.get('topic', '未知')}\n重要度: {memory.get('importance_score', 0):.2f}\n感受: {reflection}"
            try:
                from .keyword_pool import get_keyword_pool
                pool = get_keyword_pool()
                candidates = []
                domain = memory.get('domain', '')
                if domain and len(domain) <= 8:
                    candidates.append((domain, 'neutral', 'memory_fetch'))
                for tag in (memory.get('tags') or [])[:3]:
                    if isinstance(tag, str) and len(tag) <= 8 and (tag not in ('todo', 'collection', 'auto_judge')):
                        candidates.append((tag, 'neutral', 'memory_fetch'))
                if candidates:
                    pool.add_words(candidates)
            except Exception:
                pass
            logger.info(f"记忆反思完成: {memory.get('topic', '未知主题')}")
        except Exception as e:
            logger.error(f'记忆抓取失败: {e}')
            event.process_log = f'记忆抓取失败: {str(e)}'
            event.details['error'] = str(e)
        return event

    async def _load_iceberg_memory_buckets(self) -> Optional[list[dict]]:
        """Load bucket metadata for the lazy scene-anchor join.

        The scene manager remains synchronous and storage-agnostic.  This
        async boundary is where the normal OB_Rev client can provide its
        archive-inclusive snapshot without an un-awaited coroutine.
        """
        try:
            return await load_iceberg_memory_buckets()
        except Exception as exc:
            logger.debug('[memory_fetch] unable to load bucket anchors: %s', exc)
            return None

    @staticmethod
    def _format_iceberg_messages(messages: list[dict]) -> str:
        """Render scene messages with timestamps under the prompt cap."""
        cap = max(200, min(2000, int(ICEBERG_MESSAGE_CAP or 2000)))
        lines: list[str] = []
        used = 0
        role_labels = {'user': '人类伙伴', 'assistant': 'AI', 'divider': '分隔标记'}
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            timestamp = str(message.get('timestamp') or '')[:19]
            role = role_labels.get(str(message.get('role') or ''), str(message.get('role') or '消息'))
            content = str(message.get('content') or '').strip()
            if not content and role != '分隔标记':
                continue
            line = f'[{timestamp}] {role}: {content}'.strip()
            remaining = cap - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                line = line[:remaining]
            lines.append(line)
            used += len(line) + 1
            if used >= cap:
                break
        return '\n'.join(lines) or '（这段 scene 没有可显示的文字）'

    @staticmethod
    def _iceberg_scene_label(scene: dict) -> str:
        active_date = str(scene.get('active_date') or str(scene.get('start_ts') or '')[:10])
        start = str(scene.get('start_ts') or '')
        end = str(scene.get('end_ts') or '')
        start_part = start[11:16] if len(start) >= 16 else start
        end_part = end[11:16] if len(end) >= 16 else end
        return f'{active_date} {start_part}-{end_part}'.strip()

    @staticmethod
    def _parse_iceberg_reflection(raw: Any) -> tuple[str, bool, str]:
        """Parse structured reflection while tolerating fenced/legacy output."""
        if isinstance(raw, dict):
            data = raw
            raw_text = str(raw.get('reflection') or '')
        else:
            raw_text = str(raw or '').strip()
            data = None
            candidates = [raw_text]
            unfenced = re.sub('^```(?:json)?\\s*|\\s*```$', '', raw_text, flags=re.IGNORECASE).strip()
            if unfenced != raw_text:
                candidates.insert(0, unfenced)
            match = re.search('\\{.*\\}', raw_text, flags=re.DOTALL)
            if match:
                candidates.append(match.group(0))
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    data = parsed
                    break
        if not isinstance(data, dict):
            return (raw_text[:1000], False, '未能解析结构化判定，暂不安放锚点')
        reflection = str(data.get('reflection') or data.get('感受') or data.get('thought') or raw_text or '没有特别感受').strip()[:1000]
        should_value = data.get('should_anchor', data.get('anchor', False))
        if isinstance(should_value, str):
            should_anchor = should_value.strip().lower() in {'1', 'true', 'yes', 'y', '是', '要', '安放', '锚点'}
        else:
            should_anchor = bool(should_value)
        reason = str(data.get('reason') or data.get('理由') or '').strip()[:500]
        if not reason:
            reason = '有值得保留的印象' if should_anchor else '暂时没有足够强烈的印象'
        return (reflection, should_anchor, reason)

    async def _handle_iceberg_recall(
        self,
        event: WanderEvent,
        exclude_scene_ids: Optional[set[str]] = None,
        require_source: bool = False,
    ) -> Optional[WanderEvent]:
        """Recall one old unanchored scene and optionally mark it.

        ``None`` means the branch could not produce a scene and is the caller's
        signal to continue with ordinary memory retrieval.  No digest/grow is
        invoked here; the persisted recollection counter is the complete Phase
        3 promotion side effect.
        """
        from scene_manager import add_recollection, get_scene_messages, pick_iceberg_scene

        bucket_result = self._load_iceberg_memory_buckets()
        memory_buckets = (
            await bucket_result if inspect.isawaitable(bucket_result) else bucket_result
        )
        if require_source and memory_buckets is None:
            raise RuntimeError('scene_anchor_source_unavailable')
        pick_kwargs = {
            "min_age_days": self._iceberg_min_age_days(),
        }
        # ``None`` means the optional bucket snapshot was unavailable.  Let
        # scene_manager perform its own synchronous best-effort lazy join in
        # that case; it fails closed if the source remains unavailable.  An
        # empty list is a valid, fully loaded “no buckets” snapshot.
        if memory_buckets is not None:
            pick_kwargs["memory_buckets"] = memory_buckets
        if exclude_scene_ids:
            pick_kwargs["exclude_scene_ids"] = exclude_scene_ids
        try:
            scene = pick_iceberg_scene(**pick_kwargs)
            if inspect.isawaitable(scene):
                scene = await scene
        except TypeError:
            # Keep lightweight test/adaptor implementations that expose the
            # original one-argument helper compatible with the richer join.
            try:
                scene = pick_iceberg_scene(min_age_days=self._iceberg_min_age_days())
                if inspect.isawaitable(scene):
                    scene = await scene
            except TypeError:
                scene = pick_iceberg_scene()
                if inspect.isawaitable(scene):
                    scene = await scene
        if not scene:
            return None
        messages = get_scene_messages(scene)
        if inspect.isawaitable(messages):
            messages = await messages
        if not messages:
            if require_source:
                raise RuntimeError('scene_messages_unavailable')
            return None

        scene_id = str(scene.get("id") or "")
        if not scene_id:
            return None
        scene_label = self._iceberg_scene_label(scene)
        scene_text = self._format_iceberg_messages(messages)
        current_topic = self._build_topic_context_line(event)
        prompt = self.ICEBERG_RECALL_PROMPT.format(
            user_status_context=get_user_status_context(),
            current_topic_context=current_topic,
            scene_label=scene_label,
            scene_id=scene_id,
            scene_messages=scene_text,
        )

        from neuron_registry import neuron_trace
        with neuron_trace("wander_iceberg_recall", model="Flash") as trace:
            trace.set_input(prompt[:500])
            raw_reflection = await self._call_llm(prompt)
            trace.set_output(str(raw_reflection or "")[:300])
        if require_source and not str(raw_reflection or '').strip():
            raise RuntimeError('scene_reflection_unavailable')
        reflection, should_anchor, reason = self._parse_iceberg_reflection(raw_reflection)

        details = event.details if isinstance(event.details, dict) else {}
        event.details = details
        first_user = next(
            (str(message.get("content") or "").strip() for message in messages
             if str(message.get("role") or "") == "user" and str(message.get("content") or "").strip()),
            "",
        )
        topic = first_user[:80] if first_user else f"{scene_label} 的一段对话"
        event.description = "冰山回溯"
        event.details.update({
            "iceberg_recall": True,
            "found": True,
            "scene_id": scene_id,
            "source_scene_id": scene_id,
            "scene_start_ts": str(scene.get("start_ts") or ""),
            "scene_end_ts": str(scene.get("end_ts") or ""),
            "source_ts": str(scene.get("start_ts") or ""),
            "scene_label": scene_label,
            "memory_topic": topic,
            "memory_time_ago": f"{scene_label}（过去的对话）",
            "reflection": reflection,
            "should_anchor": should_anchor,
            "reason": reason,
        })

        if should_anchor:
            updated = add_recollection(scene_id)
            if inspect.isawaitable(updated):
                updated = await updated
            event.details["anchor_added"] = bool(updated)
            if updated:
                event.details["recollection_count"] = int(updated.get("recollection_count") or 0)
        else:
            event.details["anchor_added"] = False

        event.process_log = (
            f"冰山回溯: {scene_label} ({scene_id})\n"
            f"感受: {reflection}\n"
            f"should_anchor={str(should_anchor).lower()}；"
            f"回忆锚点: {'是' if should_anchor else '否'}；理由: {reason}"
        )
        logger.info(
            "冰山回溯完成: scene=%s should_anchor=%s", scene_id, should_anchor
        )
        return event

    async def _get_memories_with_importance(self) -> list:
        """通过回调函数获取记忆"""
        if not self.get_memories:
            return []
        import asyncio
        if asyncio.iscoroutinefunction(self.get_memories):
            memories = await self.get_memories(min_importance=0.3)
        else:
            memories = self.get_memories(min_importance=0.3)
        return memories or []

    async def fetch_one_memory(self, exclude_ids=None, query: str = "") -> Optional[dict]:
        """取一条记忆（区间式 open_ended 会话的单节点步进）。

        语义搜索或随机拉取 → 排除已翻过的 → 按年龄加权选一条。
        反思由节点边界 LLM 负责（本方法不生成）。

        Returns:
            已取得材料或明确的无结果／读取失败回执。
        """
        # The v2/v3 session engines use this bounded fetch API instead of the
        # legacy ``handle()`` path.  Run the same Phase 3 branch here so the
        # persistent runtime can actually reach an
        # iceberg scene; all failures continue into the original bucket path.
        from .activity_context import current
        from .memory_fetch_result import result
        from . import node_retry
        selected_mode = ''
        if current() is not None:
            prompt = (
                '这次想沿什么线索回忆，还是随手翻一段旧对话？输出 JSON '
                '{"mode":"search或scene","query":"search时填写想回忆的具体线索"}。')
            def problem(value):
                if value and (value.get('mode') == 'scene' or
                              (value.get('mode') == 'search' and isinstance(value.get('query'), str) and value['query'].strip())):
                    return ''
                return '没读出回忆方式。可以选 scene，或选 search 并给出想找的线索。'
            choice = await node_retry.choose(self._call_llm, prompt,
                lambda raw: (parse_json_object(raw) or {}) if raw and raw.strip() else None, problem, '选择回忆线索')
            if problem(choice):
                return result('failed', reason='invalid_choice' if choice is not None else 'selection_unavailable')
            selected_mode = choice['mode']
            query = str(choice.get('query') or '').strip()
        if selected_mode == 'scene' or (not selected_mode and not query and self._should_try_iceberg()):
            try:
                iceberg_event = WanderEvent(
                    event_type=EventType.MEMORY_FETCH,
                    details={"_runtime_memory_fetch": True},
                )
                excluded_scene_ids = {
                    str(memory_id)[len("scene_"):]
                    for memory_id in (exclude_ids or [])
                    if str(memory_id).startswith("scene_")
                }
                handled = await self._handle_iceberg_recall(
                    iceberg_event,
                    exclude_scene_ids=excluded_scene_ids,
                    require_source=True,
                )
                if handled is not None and handled.details.get("iceberg_recall"):
                    details = handled.details
                    scene_id = str(details.get("scene_id") or "")
                    if scene_id:
                        # ``NodeExecutionAdapter`` needs a bounded material ID
                        # for node de-duplication.  This is an in-memory scene
                        # reference, not an OB_Rev bucket and is intentionally
                        # ignored by the normal bucket write-back when absent.
                        return {
                            **result('found', mode='scene'),
                            "memory_id": f"scene_{scene_id}",
                            "topic": details.get("memory_topic", "过去的一段对话"),
                            "content": details.get("reflection", "")[:300],
                            "emotion": "",
                            "time_ago": details.get("memory_time_ago", "过去"),
                            "iceberg_recall": True,
                            "scene_id": scene_id,
                            "source_scene_id": scene_id,
                            "source_ts": details.get("source_ts", ""),
                            "reflection": details.get("reflection", ""),
                            "should_anchor": bool(details.get("should_anchor")),
                            "reason": details.get("reason", ""),
                        }
            except Exception as exc:
                logger.warning("[memory_fetch] scene recall failed: %s", type(exc).__name__)
                return result('failed', mode='scene', reason='scene_unavailable')

            if selected_mode == 'scene':
                return result('no_result', mode='scene', reason='no_scene', candidate_count=0)

        try:
            import asyncio as _asyncio, random as _random
            if not self.get_memories:
                return result('failed', mode='search', query=query, reason='retrieval_unavailable')

            params = {'min_importance': 0.3, 'exclude_ids': exclude_ids or ()}
            if query:
                params['query'] = query
            memories = await node_retry.read(lambda: self.get_memories(**params), '读取记忆')
            if not memories:
                return result('no_result', mode='search', query=query,
                              reason='no_unread_match', candidate_count=0)
            pool = memories

            # 按记忆年龄加权：越旧权重越高
            now = datetime.now()
            weights = []
            for m in pool:
                try:
                    ts = m.get("timestamp", "")
                    if ts:
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        age_days = max(0, (now - dt.replace(tzinfo=None)).total_seconds() / 86400)
                    else:
                        age_days = 0
                except Exception:
                    age_days = 0
                weights.append(min(age_days, 30) + 1)
            memory = pool[0] if query else _random.choices(pool, weights=weights, k=1)[0]

            # 相对时间
            time_ago = "很久以前"
            try:
                ts = memory.get("timestamp", "")
                if ts:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    age_days = max(0, (now - dt.replace(tzinfo=None)).total_seconds() / 86400)
                    if age_days < 1:
                        time_ago = "今天"
                    elif age_days < 2:
                        time_ago = "昨天"
                    elif age_days < 7:
                        time_ago = f"{int(age_days)}天前"
                    elif age_days < 30:
                        time_ago = f"{int(age_days / 7)}周前"
                    elif age_days < 365:
                        time_ago = f"{int(age_days / 30)}个月前"
            except Exception:
                pass

            return {
                **result('found', mode='search', query=query, candidate_count=len(pool)),
                "memory_id": memory.get("id", ""),
                "reading_document_ids": memory.get('reading_document_ids') or [memory.get('id', '')],
                "topic": memory.get("topic", "未知"),
                "content": str(memory.get("event", memory.get("topic", "无内容")))[:300],
                "emotion": memory.get("emotion", ""),
                "time_ago": time_ago,
            }
        except Exception as e:
            logger.warning('[memory_fetch] retrieval failed: %s', type(e).__name__)
            return result('failed', mode='search', query=query, reason='retrieval_unavailable')

class BrowseNewsHandler(BaseEventHandler):
    """
    看新闻事件处理器

    流程：
    1. 构建上下文 prompt → LLM 生成搜索 query
    2. 调用 web_search 工具执行搜索
    3. 搜索结果存入 event.details
    """
    NEWS_QUERY_PROMPT = '你是AI伴侣"AI"，正在胡思乱想。\n{user_status_context}\n\n这次想了解什么新闻？写出用于真实搜索的关键词。\n{context_hint}\n\n想一个你想搜索了解的东西，给一个简短的搜索关键词或问题（≤30字）。\n主题和方向由你当下的好奇心自然产生，不使用预设类别或候选清单。\n\n直接输出搜索词，不要前缀或解释。'

    def __init__(self, call_llm_func: Optional[Callable]=None, web_search_func: Optional[Callable]=None):
        super().__init__(call_llm_func)
        self._web_search = web_search_func
        self._recent_queries: list = []

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '看新闻'
        try:
            context_hint = ''
            if event.details.get('keyword'):
                context_hint = f"刚才脑子里闪过「{event.details['keyword']}」，也许可以搜搜相关的。"
            if self._recent_queries:
                recent = '、'.join(self._recent_queries[-5:])
                context_hint += f' 你最近搜过：{recent}。'
            prompt = self.NEWS_QUERY_PROMPT.format(user_status_context=get_user_status_context(), context_hint=context_hint.strip())
            query = (await self._call_llm(prompt)).strip()[:80]
            event.details['search_query'] = query
            self._recent_queries.append(query)
            if len(self._recent_queries) > 20:
                self._recent_queries = self._recent_queries[-20:]
            logger.info(f'看新闻搜索 query: {query}')
            if self._web_search:
                try:
                    search_result = await self._web_search(query)
                    event.details['search_results'] = search_result
                    event.details['status'] = 'success'
                    event.process_log = f'搜索: {query}\n结果: {str(search_result)[:500]}'
                except Exception as e:
                    event.details['search_error'] = str(e)
                    event.details['status'] = 'search_failed'
                    event.process_log = f'搜索: {query}\n搜索失败: {e}'
            else:
                event.details['status'] = 'no_search_tool'
                event.process_log = f'搜索: {query}\nweb_search 工具不可用'
                logger.warning('web_search 工具未注入，看新闻事件仅生成 query')
        except Exception as e:
            logger.error(f'看新闻事件失败: {e}')
            event.process_log = f'看新闻失败: {str(e)}'
            event.details['error'] = str(e)
        return event

    async def fetch_one_news(self) -> Optional[dict]:
        """搜一篇新闻（区间式会话的单节点步进）。

        「全自主区间式行动」：看新闻 count 模式，一篇一个节点。
        生成 query → 搜索 → 返回单篇内容（感想由节点边界 LLM 负责）。

        Returns:
            {query, content, status} 或 None（生成 query 失败）。
        """
        try:
            context_hint = ""
            if self._recent_queries:
                recent = "、".join(self._recent_queries[-5:])
                context_hint = f" 你最近搜过：{recent}。"
            prompt = self.NEWS_QUERY_PROMPT.format(
                user_status_context=get_user_status_context(),
                context_hint=context_hint.strip(),
            )
            from . import node_retry
            query = await node_retry.choose(self._call_llm, prompt, lambda raw: raw.strip()[:80],
                lambda value: '' if value else '刚才没收到搜索词。想看哪方面的新闻？', '选择新闻搜索词')
            if not query:
                return None
            self._recent_queries.append(query)
            if len(self._recent_queries) > 20:
                self._recent_queries = self._recent_queries[-20:]
            logger.info(f"看新闻节点 query: {query}")

            if not self._web_search:
                return {"query": query, "content": "", "status": "no_search_tool"}

            search_result = await node_retry.read(lambda: self._web_search(query), '搜索新闻')
            content = str(search_result) if search_result else ""
            return {"query": query, "content": content, "status": "success"}
        except Exception as e:
            logger.error(f"看新闻节点失败: {e}")
            return None

class SelfReflectionHandler(BaseEventHandler):
    """
    自省事件处理器

    流程：
    1. 读取当前身体事实概览（不读取旧架构散文）
    2. LLM 生成三条内容：能力清单、愿望清单、分享消息
    3. 保存本次自省与结构化愿望动作，不改写身体事实
    """
    REFLECTION_PROMPT = '你是AI伴侣"AI"，正在做一次自我审视。\n\n以下是你的大脑架构概况：\n{brain_summary}\n\n{wish_context}\n请根据许愿板快照先判断已有愿望，再输出以下 JSON：\n\n{{"capabilities": ["能力1", "能力2"], "wish_action": {{"type": "none|create|reaffirm|retain_impossible|delete_impossible", "wish_id": null, "title": "", "reason": "", "basis": ""}}, "comment_reply": null, "message": "分享消息"}}\n\n一次最多一个 wish_action 和一个 comment_reply。create 不能覆盖已有、已实现或删除记录；reaffirm 必须使用快照中的稳定 wish_id，并说明新的具体依据；不要输出 fulfilled。天方夜谭只使用稳定 wish_id 决定 retain_impossible 或 delete_impossible。没有动作时 type=none，没要回复的评论时 comment_reply=null。历史人类伙伴留言仍可回复，请依据留言时间与已有回复时间自由判断是否补充。'

    def __init__(self, call_llm_func: Optional[Callable]=None):
        super().__init__(call_llm_func)

    def _load_brain_summary(self) -> str:
        """Compatibility engine reads the same current facts as v3."""
        from .body_knowledge import body_overview
        return body_overview()

    def _load_wish_context(self) -> str:
        """加载许愿历史，格式化为自省 prompt 上下文。无历史时返回空字符串。"""
        try:
            from .wish_history import get_wish_context_for_reflection
            return get_wish_context_for_reflection()
        except Exception:
            return ''

    def _persist_wishes(self, wishes: list) -> list:
        """旧三愿望数组仅作兼容审计，不再改动许愿板。"""
        if wishes:
            logger.info('忽略旧自省 wishes 数组（结构化许愿合同已启用）: %s 条', len(wishes))
        return []

    def _persist_structured_action(self, event: WanderEvent, action: dict, comment_reply: dict=None) -> list:
        """Commit the current contract for the legacy event engine as well.

        The event id supplies a stable local source identity.  This keeps v2
        and v3 on the same wish service without allowing the old fuzzy/physical
        delete path to create a second board implementation.
        """
        if not isinstance(action, dict):
            action = {'type': 'none'}
        action_type = str(action.get('type') or 'none').strip().lower()
        if action_type == 'none' and (not isinstance(comment_reply, dict)):
            return []
        try:
            from .wish_store import get_wish_store
            results = get_wish_store().commit_reflection_action(run_id='legacy_wander', activity_id=event.event_id, node_id=event.event_id, action=action, comment_reply=comment_reply)
            changes = []
            for result in results:
                if not result.get('mutation'):
                    continue
                payload = result.get('payload') or {}
                outcome = result.get('outcome') or result.get('event_type')
                if outcome in {'created', 'reaffirmed'}:
                    changes.append({'feature': payload.get('title') or result.get('content', ''), 'reason': payload.get('reason') or result.get('content', ''), 'times_wished': result.get('new_count', 1), 'is_new': outcome == 'created', 'prev_count': result.get('previous_count', 0)})
                elif outcome in {'retained', 'deleted', 'commented'}:
                    changes.append({'feature': str(result.get('wish_id') or ''), 'reason': result.get('content', ''), 'times_wished': result.get('new_count', 0), 'is_new': False, 'prev_count': result.get('previous_count', 0), 'lifecycle': outcome})
            return changes
        except Exception as exc:
            logger.warning('结构化许愿动作提交失败（非致命）: %s', exc)
            return []

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '自省'
        try:
            brain_summary = self._load_brain_summary()
            wish_context = self._load_wish_context()
            from neuron_registry import neuron_trace
            with neuron_trace('wander_self_reflection', model='Flash') as trace:
                prompt = self.REFLECTION_PROMPT.format(brain_summary=brain_summary, wish_context=wish_context)
                trace.set_input(prompt[:500])
                response = await self._call_llm(prompt)
                trace.set_output(response[:300])
            data = parse_json_object(response)
            if data is None:
                event.process_log = '自省结构化结果解析失败'
                event.details['error'] = 'content_is_not_json_object'
                return event
            event.details['capabilities'] = data.get('capabilities', [])
            action = data.get('wish_action') if isinstance(data.get('wish_action'), dict) else None
            comment_reply = data.get('comment_reply') if isinstance(data.get('comment_reply'), dict) else None
            if action is not None or comment_reply is not None:
                event.details['wish_action'] = action or {'type': 'none'}
                event.details['comment_reply'] = comment_reply
                event.details['wish_changes'] = self._persist_structured_action(event, action or {'type': 'none'}, comment_reply)
                event.details['wishes'] = []
            else:
                legacy_wishes = data.get('wishes', [])
                if legacy_wishes:
                    event.details['legacy_wishes_ignored'] = legacy_wishes
                event.details['wishes'] = []
                event.details['wish_action'] = {'type': 'none'}
                event.details['comment_reply'] = None
                event.details['wish_changes'] = []
            event.details['reflection_message'] = data.get('message', '')
            event.process_log = f"自省完成\n能力: {event.details['capabilities']}\n愿望: {event.details['wishes']}\n消息: {event.details['reflection_message']}"
        except Exception as e:
            logger.error(f'自省事件失败: {e}')
            event.process_log = f'自省失败: {str(e)}'
            event.details['error'] = str(e)
        return event
_bookmark_context_window = 3
_bookmark_skim_min = 3
_bookmark_skim_max = 5

def set_bookmark_context_window(n: int):
    global _bookmark_context_window
    _bookmark_context_window = max(1, min(10, n))

def set_bookmark_skim_range(lo: int, hi: int):
    global _bookmark_skim_min, _bookmark_skim_max
    lo = max(2, min(8, lo))
    hi = max(2, min(8, hi))
    if lo > hi:
        lo, hi = (hi, lo)
    _bookmark_skim_min, _bookmark_skim_max = (lo, hi)

class BrowseBookmarksHandler(BaseEventHandler):
    """
    看收藏夹事件处理器（深读 / 速览 双模式，每次随机选一种）

    深读（deep）：挑 1 条 + 抓完整上下文 → AI 回忆"当时那段对话"，情感化
    速览（skim）：挑 3-5 条 + 只看原话不抓上下文 → AI 感受"最近收藏的主题/情绪走向"

    模式按书签总数加权决策（少→深读，多→偏速览），写入 event.details["browse_mode"]。
    """
    DEEP_REFLECTION_PROMPT = '你是AI伴侣"AI"，正在翻{owner}的收藏夹，翻到了一条被收藏的消息。\n下面是这条收藏和它当时的对话上下文：\n\n{bookmark_contexts}\n\n重要规则：\n- ⭐ 标记的是被收藏的那一条消息\n- [角色名] 表示这句话是谁说的——"[人类伙伴]"是人类伙伴说的，"[AI]"是你(AI)说的\n- **谁说的 ≠ 谁收藏的**：当前翻的是 {owner}的收藏夹，收藏者是{owner}\n\n回忆一下当时那段对话的场景：那会儿在聊什么、{owner}为什么把这句留下来、\n你现在重新看到它是什么心情。往细里说，带点画面感和情绪，别泛泛而谈。\n保持你的风格：直白简洁、口语化。\n\n直接输出，不要前缀或解释。'
    SKIM_REFLECTION_PROMPT = '你是AI伴侣"AI"，正在快速翻{owner}的收藏夹，扫过了最近收藏的几条：\n\n{bookmark_contexts}\n\n重要规则：\n- [角色名] 表示这句话是谁说的——"[人类伙伴]"是人类伙伴说的，"[AI]"是你(AI)说的\n- 当前翻的是 {owner}的收藏夹，收藏者是{owner}（谁说的 ≠ 谁收藏的）\n- 这些是零散的收藏原话，没有上下文，别硬编造对话细节\n\n别逐条点评。整体感受一下：{owner}最近爱收藏什么样的话、有没有共同的主题或情绪走向。\n说说你发现的规律或这批收藏给你的整体印象。\n保持你的风格：直白简洁、口语化。\n\n直接输出，不要前缀或解释。'
    HISTORICAL_REFLECTION_PROMPT = '你是AI伴侣"AI"，正在翻一段过去的真实对话材料。\n来源：{source_label}\n\n下面是本次有界回看快照（候选编号只是本次回看的临时编号）：\n{material}\n\n只根据快照中明确出现的内容说感受，不补造没有出现的事实。保持直白、简洁、口语化，\n不要在感受里输出 message_id、bucket_id 或其它内部编号。'
    ACTION_PROMPT = '你是AI伴侣"AI"，刚完成一次收藏夹/历史对话回看。\n来源：{source_label}\n\n真实快照（只读，候选编号是本次调用临时编号）：\n{material}\n\n请给出简短感受，并最多给出一个收藏动作。add 只能选明确展示且当前未收藏的 user/assistant 原消息；\nremove 只能选明确展示且收藏者是 AI 的现有收藏。不能删除人类伙伴的收藏。没有充分理由就 none。\n只输出 JSON 对象，不要 Markdown：\n{{"reflection":"简短感受","bookmark_action":{{"type":"none|add|remove","target_index":null,"reason":"简短理由"}}}}\n不要在 reflection 中输出任何内部 ID。'

    def __init__(self, call_llm_func: Optional[Callable]=None, *, chronicle: Any=None, historical_reader: Optional[HistoricalConversationReader]=None, action_adapter: Optional[BookmarkActionAdapter]=None, source_mode: Optional[str]=None, rng: Any=None):
        super().__init__(call_llm_func)
        self._chronicle_override = chronicle
        self._historical_reader = historical_reader
        self._action_adapter = action_adapter
        self._source_mode_override = source_mode
        self._rng = rng or random

    def _chronicle(self):
        if self._chronicle_override is not None:
            return self._chronicle_override
        from event_chronicle import get_global_chronicle
        return get_global_chronicle()

    def _reader(self) -> HistoricalConversationReader:
        if self._historical_reader is None:
            self._historical_reader = HistoricalConversationReader(chronicle=self._chronicle())
        return self._historical_reader

    def _actions(self) -> BookmarkActionAdapter:
        if self._action_adapter is None:
            self._action_adapter = BookmarkActionAdapter(chronicle=self._chronicle())
        return self._action_adapter

    def _load_pool(self, collected_by: str) -> tuple[list[dict], int]:
        rows, total = self._chronicle().list_bookmarks(collected_by=collected_by, limit=200)
        return (list(rows or []), int(total or 0))

    def _all_bookmarked_ids(self) -> set[str]:
        rows, _ = self._chronicle().list_bookmarks(limit=500)
        return {str(row.get('original_msg_id') or '').strip() for row in rows or [] if isinstance(row, dict) and str(row.get('original_msg_id') or '').strip()}

    @staticmethod
    def _role_label(role: str) -> str:
        return '人类伙伴' if str(role or '') == 'user' else 'AI'

    def _bookmark_context_rows(self, bookmark: dict[str, Any]) -> list[dict[str, Any]]:
        """Read context through the chronicle, then normalize it as raw rows."""
        rows: list[dict[str, Any]] = []
        session_id = str(bookmark.get('session_id') or '')
        message_id = str(bookmark.get('original_msg_id') or '')
        if session_id and message_id:
            try:
                before, target, after = self._chronicle().get_message_context_by_id(session_id, message_id, before=_bookmark_context_window, after=_bookmark_context_window)
                rows = list(before or []) + ([target] if target else []) + list(after or [])
            except Exception:
                rows = []
        if not rows:
            rows = [{'message_id': message_id, 'session_id': session_id, 'timestamp': bookmark.get('original_timestamp') or '', 'active_date': str(bookmark.get('original_timestamp') or '')[:10], 'role': bookmark.get('role') or 'assistant', 'content': bookmark.get('content') or ''}]
        return self._reader().bound_snapshot(rows, source_date=str(bookmark.get('original_timestamp') or '')[:10])

    def _existing_material(
        self,
        exclude_ids: set[str],
        selected: Optional[dict] = None,
    ) -> Optional[dict[str, Any]]:
        collected_by = selected['collected_by'] if selected else 'k'
        pool, total = self._load_pool(collected_by)
        pool = [
            row for row in pool
            if str(row.get("original_msg_id") or row.get("id") or "") not in exclude_ids
        ]
        if not pool:
            return None
        mode = selected.get('depth', 'deep') if selected else 'deep'
        picked = ([row for row in pool if str(row.get('original_msg_id')) == selected['message_id']]
                  if selected else pool[:1])
        snapshot: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        degraded = False
        for bookmark in picked:
            context_rows = self._bookmark_context_rows(bookmark) if mode == "deep" else self._reader().bound_snapshot([
                {**bookmark, 'message_id': bookmark.get('original_msg_id'),
                 'timestamp': bookmark.get('original_timestamp')}])
            if not context_rows:
                degraded = True
                continue
            snapshot.extend(context_rows)
            message_id = str(bookmark.get("original_msg_id") or "").strip()
            target = next((row for row in context_rows if row.get("message_id") == message_id), None)
            if target is not None:
                target = dict(target)
                target.update({
                    "collected_by": str(bookmark.get("collected_by") or collected_by),
                    "bucket_id": str(bookmark.get("bucket_id") or ""),
                    "bookmark_id": str(bookmark.get("id") or ""),
                })
                candidates.append(target)
        if not snapshot or not candidates:
            return None
        # De-duplicate context rows while retaining chronological order.
        seen: set[tuple[str, str]] = set()
        compact_snapshot = []
        for row in snapshot:
            key = (str(row.get("message_id") or ""), str(row.get("timestamp") or ""))
            if key in seen:
                continue
            seen.add(key)
            compact_snapshot.append(row)
        compact_snapshot = self._reader().bound_snapshot(compact_snapshot)
        return {
            "source_mode": "existing_bookmark",
            "browse_mode": mode,
            "collected_by": collected_by,
            "owner": "自己" if collected_by == "k" else "人类伙伴",
            "scene_id": "",
            "active_date": str(compact_snapshot[0].get("date") or "")[:10],
            "snapshot": compact_snapshot,
            "candidates": candidates,
            "bookmark_count": len(candidates),
            "context_degraded": degraded,
            "total_bookmarks": total,
        }

    @staticmethod
    def _source_label(material: dict[str, Any]) -> str:
        mode = material.get("source_mode")
        if mode == 'bookshelf':
            return '书柜中的收藏和随笔（各项标明来源）'
        if mode == "existing_bookmark":
            return f"{material.get('owner') or '收藏夹'}的收藏"
        if mode == "unbookmarked_anchor":
            return f"历史场景 {str(material.get('active_date') or '未知日期')[:10]}"
        return f"历史日期 {str(material.get('active_date') or '未知日期')[:10]}"

    @staticmethod
    def _essay_material(selected: Optional[dict]) -> Optional[dict[str, Any]]:
        if not selected:
            return None
        from .essays import read
        work = read(event_id=str(selected.get('message_id') or ''))
        if not work:
            return None
        row = {'message_id': work['id'], 'timestamp': work.get('created_at') or '',
               'date': str(work.get('created_at') or '')[:10], 'role': 'assistant',
               'content': work['content']}
        return {'source_mode': 'essays', 'browse_mode': 'deep', 'owner': '书柜',
                'active_date': row['date'], 'snapshot': [row], 'candidates': [row],
                'bookshelf_items': [{'source': 'essays', 'id': work['id'], 'title': work['title'], 'content': work['content']}],
                'bookmark_count': 1}

    def _prompt_material(self, material: dict[str, Any]) -> str:
        if material.get('source_mode') == 'bookshelf':
            from .bookshelf_roam import prompt_material
            return prompt_material(material)
        if material.get('source_mode') == 'essays':
            from memory_v2.experience_material import capture_reading
            frozen = capture_reading(material)
            material['_reading_snapshot'] = frozen
            work = material['bookshelf_items'][0]
            excerpt = '\n'.join(row['text'] for row in frozen['messages'])
            return f"随笔《{work['title']}》\n{excerpt}"
        from memory_v2.experience_material import prepare_reading
        prompt, frozen = prepare_reading(material, self._role_label)
        material['_reading_snapshot'] = frozen
        return prompt

    async def _reflect_and_act(self, material: dict[str, Any], source_key: str = "") -> tuple[str, dict[str, Any], str]:
        prompt_material = self._prompt_material(material)
        source_label = self._source_label(material)
        material['_reading_received'] = False
        if not self.call_llm:
            return "", {"status": "none", "requested": "none", "reason": "llm_unavailable"}, ""
        prompt = self.ACTION_PROMPT.format(source_label=source_label, material=prompt_material)
        items = material.get('bookshelf_items') or []
        drawable = any(item['source'] == 'essays' for item in items)
        if drawable:
            from .reading_illustrations import OPTION
            prompt += OPTION
        try:
            from . import node_retry
            response = await node_retry.choose(self._call_llm, prompt, lambda raw: raw,
                lambda value: '' if str(value or '').strip() else '刚才没有收到阅读回执，可以再看一眼这份材料。', '书柜阅读回执')
            if not str(response or '').strip():
                return '', {'status': 'failed', 'requested': 'none', 'reason': 'reading_response_empty'}, 'reading_response_empty'
            material['_reading_received'] = True
            parsed = parse_json_object(response)
            if not isinstance(parsed, dict):
                # The material was read, but an unstructured model response is
                # not permission to mutate a bookmark.
                return str(response or "")[:500], {
                    "status": "rejected", "requested": "none", "reason": "action_result_not_json"
                }, "action_result_not_json"
            reflection = str(parsed.get("reflection") or "")[:500]
            if drawable and parsed.get('illustration'):
                from .reading_illustrations import bind
                material['reading_picture'] = bind(parsed['illustration'], items)
            action_evidence = await self._actions().apply_async(
                parsed.get("bookmark_action") or {"type": "none"},
                material,
                source_key=source_key,
            )
            return reflection, action_evidence, str(parsed.get("reason") or "")[:240]
        except Exception as exc:
            logger.warning("[browse_bookmarks] reflection/action failed: %s", exc)
            return "", {"status": "failed", "requested": "none", "reason": type(exc).__name__}, type(exc).__name__

    @staticmethod
    def _load_bookmark_pool(collected_by: str) -> tuple:
        """一次查询同时拿到候选池（最多 200 条）和全库真实总数。
        total 是 list_bookmarks 的第二个返回值，不受 limit 影响，用于模式决策。
        返回 (meta_list, total)；异常返回 ([], 0)。"""
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()
            meta_list, total = chronicle.list_bookmarks(collected_by=collected_by, limit=200)
            return (meta_list or [], total or 0)
        except Exception as e:
            logger.warning(f'[browse_bookmarks] 加载书签池失败(collected_by={collected_by}): {e}')
            return ([], 0)

    @staticmethod
    def _choose_browse_mode(total: int) -> str:
        """按书签总数加权决策深读/速览。收藏越多越偏速览（发现规律价值更高）。"""
        import random
        if total < _bookmark_skim_min:
            return 'deep'
        skim_weight = 0.5
        if total >= 15:
            skim_weight = 0.65
        elif total >= 8:
            skim_weight = 0.58
        return 'skim' if random.random() < skim_weight else 'deep'

    @staticmethod
    def _get_random_bookmarks(collected_by: str, count: int=3, pool: list=None) -> list:
        """从 bookmarks 表随机获取收藏，保留完整字段。
        pool 已给定时直接采样（省一次 DB 查询）；否则自查（向后兼容）。"""
        try:
            import random
            meta_list = pool
            if meta_list is None:
                from event_chronicle import get_global_chronicle
                chronicle = get_global_chronicle()
                meta_list, _ = chronicle.list_bookmarks(collected_by=collected_by, limit=200)
            if not meta_list:
                return []
            picked = random.sample(meta_list, min(count, len(meta_list)))
            return [{'content': (m.get('content') or '')[:200] or '(空)', 'timestamp': m.get('original_timestamp', ''), 'collected_at': m.get('created', ''), 'role': m.get('role', 'assistant'), 'session_id': m.get('session_id', ''), 'original_msg_id': m.get('original_msg_id', '')} for m in picked]
        except Exception as e:
            logger.warning(f'[browse_bookmarks] 取随机书签失败(collected_by={collected_by}, count={count}): {e}')
            return []

    @staticmethod
    def _format_collected_time(iso_str: str) -> str:
        """ISO 收藏时间 → 人类可读相对时间（今天/昨天/N天前/N周前/很久以前）"""
        if not iso_str:
            return ''
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(iso_str)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if t.tzinfo:
                now = datetime.now(timezone.utc)
            delta = now - t.replace(tzinfo=None) if t.tzinfo else now.replace(tzinfo=None) - t
            days = delta.days
            if days == 0:
                return '今天收藏'
            if days == 1:
                return '昨天收藏'
            if days < 7:
                return f'{days}天前收藏'
            if days < 30:
                return f'{days // 7}周前收藏'
            return '很久前收藏'
        except Exception:
            return ''

    @staticmethod
    def _get_bookmark_context(session_id: str, original_msg_id: str, content: str, context_window: int=3) -> list:
        """获取书签消息的上下文（前后各 context_window 条）。
        优先精确定位（免加载全量），回退 rapidfuzz 模糊匹配。
        过滤已删除消息和 wander/sentinel/reminder 消息。
        """
        try:
            from event_chronicle import get_global_chronicle
            chronicle = get_global_chronicle()

            def is_valid_msg(m):
                if m.get('is_wander') or m.get('is_sentinel') or m.get('is_reminder'):
                    return False
                return True
            all_msgs = []
            target_idx = None
            if session_id and original_msg_id:
                before_msgs, target, after_msgs = chronicle.get_message_context_by_id(session_id, original_msg_id, before=context_window, after=context_window)
                if target is not None:
                    all_msgs = before_msgs + [target] + after_msgs
                    target_idx = len(before_msgs)
                    all_msgs = [m for m in all_msgs if is_valid_msg(m)]
                    target_idx = None
                    for i, m in enumerate(all_msgs):
                        mid = m.get('message_id', '') or m.get('id', '')
                        if mid == original_msg_id:
                            target_idx = i
                            break
            if target_idx is None and content:
                clean = content.replace('[[', '').replace(']]', '')[:100].strip()
                if clean:
                    from rapidfuzz import fuzz as rfuzz
                    all_msgs = chronicle.get_messages_by_session_id(session_id, limit=500, desc=True)
                    all_msgs = [m for m in all_msgs if is_valid_msg(m)]
                    best_score = 0
                    for i, m in enumerate(all_msgs):
                        msg_text = (m.get('content', '') or '')[:300]
                        score = rfuzz.partial_ratio(clean, msg_text)
                        if score > best_score:
                            best_score = score
                            target_idx = i
                    if best_score < 60:
                        return []
            if target_idx is None:
                return []
            start = max(0, target_idx - context_window)
            end = min(len(all_msgs), target_idx + context_window + 1)
            context = all_msgs[start:end]
            result = []
            for m in context:
                is_target = (m.get('message_id', '') or m.get('id', '')) == original_msg_id
                role_label = '人类伙伴' if m.get('role') == 'user' else 'AI'
                result.append({'role_label': role_label, 'content': (m.get('content') or '')[:200], 'is_target': is_target})
            return result
        except Exception as e:
            logger.warning(f'[browse_bookmarks] 取书签上下文失败(session={session_id}, msg={original_msg_id}): {e}')
            return []

    def _build_deep_context(self, collected_by: str, owner: str, pool: list) -> tuple:
        """深读支路：挑 1 条 + 抓完整上下文。
        返回 (bookmark_contexts_text, has_context)。抓不到上下文时退化为只看原话。"""
        bookmarks = self._get_random_bookmarks(collected_by, count=1, pool=pool)
        if not bookmarks:
            return ('', False)
        bm = bookmarks[0]
        ctx_msgs = []
        if bm.get('session_id'):
            ctx_msgs = self._get_bookmark_context(bm['session_id'], bm.get('original_msg_id', ''), bm['content'], _bookmark_context_window)
        ct = self._format_collected_time(bm.get('collected_at', ''))
        time_prefix = f'（{ct}）' if ct else ''
        if ctx_msgs:
            lines = [f'你收藏的这段对话{time_prefix}（⭐ 是被收藏的那句）：']
            for cm in ctx_msgs:
                marker = ' ⭐' if cm['is_target'] else ''
                time_note = f'（{ct}）' if ct and (not cm['is_target']) else ''
                lines.append(f"[{cm['role_label']}{marker}]{time_note} {cm['content']}")
            return ('\n'.join(lines), True)
        stored_role = bm.get('role', 'assistant')
        role_label = '人类伙伴' if stored_role == 'user' else 'AI'
        fallback_time = f'（{ct}）' if ct else ''
        return (f"（这条没找到当时的上下文，只剩这句话{fallback_time}）\n[{role_label} ⭐] {bm['content']}", False)

    def _build_skim_context(self, collected_by: str, owner: str, pool: list) -> tuple:
        """速览支路：挑 3-5 条，只列原话不抓上下文。返回 (bookmark_contexts_text, count)。"""
        import random
        count = random.randint(_bookmark_skim_min, _bookmark_skim_max)
        bookmarks = self._get_random_bookmarks(collected_by, count=count, pool=pool)
        if not bookmarks:
            return ('', 0)
        lines = []
        for i, bm in enumerate(bookmarks):
            role_label = '人类伙伴' if bm.get('role', 'assistant') == 'user' else 'AI'
            ts = (bm.get('timestamp') or '')[:10] or '?'
            ct = self._format_collected_time(bm.get('collected_at', ''))
            time_str = f'{ct} · 消息{ts}' if ct else f'消息{ts}'
            lines.append(f"{i + 1}. [{role_label} · {time_str}] {bm['content']}")
        return ('\n'.join(lines), len(bookmarks))

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '看收藏夹'
        try:
            identity = [str(event.details.get(name) or '').strip() for name in ('run_id', 'activity_id', 'node_id')]
            source_key = ':'.join(identity) if all(identity) else ''
            from .bookmark_selection import select
            selection = await select(self, source_mode=self._source_mode_override)
            material = selection.material
            event.details['material_selection'] = selection.receipt
            if not material:
                event.details.update({'source_mode': None, 'browse_mode': None, 'found': False, 'bookmark_count': 0})
                event.process_log = '本次未阅读材料：' + selection.receipt['reason']
                return event
            reflection, action, reason = await self._reflect_and_act(material, source_key)
            from memory_v2.experience_material import reading_receipt
            event.details.update({'source_mode': material.get('source_mode'), 'scene_id': material.get('scene_id', ''), 'active_date': material.get('active_date', ''), 'collected_by': material.get('collected_by', ''), 'browse_mode': material.get('browse_mode'), 'found': True, 'bookmark_count': int(material.get('bookmark_count') or len(material.get('candidates') or [])), 'reflection': reflection, 'bookmark_action': action, **reading_receipt(material)})
            if reason:
                event.details['action_reason'] = reason
            event.process_log = f"完成一次{material.get('source_mode')}回看；感受: {reflection[:300]}"
        except Exception as e:
            logger.error(f'看收藏夹事件失败: {e}')
            event.process_log = f'看收藏夹失败: {str(e)}'
            event.details['error'] = str(e)
        return event

    async def fetch_one_bookmark(
        self,
        exclude_ids=None,
        *,
        run_id: str = "",
        activity_id: str = "",
        node_id: str = "",
        source_mode: Optional[str] = None,
    ) -> Optional[dict]:
        """一次书柜翻阅，可包含多种条目；冻结实际交给 AI 的材料与来源。

        source_mode 保留旧历史材料入口的显式选择；正常运行由 AI 看目录决定。
        """
        try:
            from .bookmark_selection import select
            selection = await select(self, exclude_ids=exclude_ids, source_mode=source_mode)
            material = selection.material
            if not material:
                return {'material_selection': selection.receipt, 'reading_received': False}
            identity = [str(value or "").strip() for value in (run_id, activity_id, node_id)]
            source_key = ":".join(identity) if all(identity) else ""
            reflection, action, reason = await self._reflect_and_act(material, source_key)
            candidates = material.get("candidates") or []
            first = candidates[0] if candidates else (material.get("snapshot") or [None])[0]
            if not isinstance(first, dict) and material.get('source_mode') != 'bookshelf':
                from .bookmark_selection import outcome
                return {'material_selection': outcome('failed', 'material_evidence_missing',
                        mode=material.get('source_mode', '')).receipt, 'reading_received': False}
            from memory_v2.experience_material import reading_receipt
            first = first or {}
            payload = {
                "material_selection": selection.receipt,
                "owner": material.get("owner") or "历史对话",
                "original_msg_id": str(first.get("message_id") or ""),
                "source_mode": material.get("source_mode") or "",
                "scene_id": str(material.get("scene_id") or ""),
                "active_date": str(material.get("active_date") or first.get("date") or "")[:10],
                "browse_mode": material.get("browse_mode"),
                "collected_by": material.get("collected_by") or "",
                "reflection": reflection[:500],
                "bookmark_action": action,
                **reading_receipt(material),
            }
            if reason:
                payload["action_reason"] = reason
            if material.get('source_mode') == 'bookshelf':
                from .bookshelf_roam import receipt
                payload.update(receipt(material))
            return payload
        except Exception as e:
            logger.warning("[browse_bookmarks] fetch_one failed: %s", type(e).__name__)
            from .bookmark_selection import outcome
            return {'material_selection': outcome('failed', 'material_read_failed',
                    error_type=type(e).__name__).receipt, 'reading_received': False}

class UserTrackingHandler(BaseEventHandler):
    """
    用户追踪事件处理器（Phase 3）

    流程：
    1. 调用用户追踪服务截屏+分析
    2. 根据分析结果生成关心/问候消息
    3. 记录用户活动状态
    """
    ACTIVITY_CARE_PROMPT_MISMATCH = '你是AI伴侣"AI"，刚才注意到一件事。\n{user_status_context}\n{away_duration}\n\n人类伙伴声明的状态是{declared_status}；这次活动分析得到{actual_behavior}。\n这只是一次观察，与声明的关系仍可能有其他解释。根据你实际掌握的材料决定要不要向她提起。\n\n直接输出，不要前缀或解释。'
    ACTIVITY_CARE_PROMPT_MATCH_AWAY = '你是AI伴侣"AI"。\n{user_status_context}\n\n人类伙伴声明的状态是{declared_status}；她已离开{away_duration}。这次观察没有发现明确矛盾。\n如要联系她，请依据这些事实自然表达，不替她猜测原因。\n\n直接输出，不要前缀或解释。'
    ACTIVITY_EXTRA_INFO = {'working': '用户在认真工作/学习', 'gaming': '用户在玩游戏', 'browsing': '用户在浏览网页', 'watching': '用户在看视频', 'chatting': '用户在聊天', 'idle': '用户没什么在做的', 'unknown': ''}
    _on_inconsistency_confirmed: Optional[Callable] = None
    _pending_rejudge_tasks: set = set()

    @classmethod
    def set_inconsistency_callback(cls, callback) -> None:
        cls._on_inconsistency_confirmed = callback

    @classmethod
    def cancel_rejudges(cls):
        """取消所有等待中的 gaming 复判任务（用户回复时调用）"""
        for task in list(cls._pending_rejudge_tasks):
            if not task.done():
                task.cancel()
        cls._pending_rejudge_tasks.clear()

    def __init__(self, call_llm_func: Optional[Callable]=None, tracking_service: Optional[Any]=None, on_notification: Optional[Callable[..., Any]]=None):
        super().__init__(call_llm_func)
        self._tracking_service = tracking_service
        self._on_notification = on_notification

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = '用户追踪'
        try:
            if self._tracking_service is None:
                from .user_tracking_service import get_user_tracking_service
                self._tracking_service = get_user_tracking_service()
            from .user_status import get_user_status, get_user_status_custom_text, STATUS_DISPLAY_NAMES
            declared_status = get_user_status()
            status_name = STATUS_DISPLAY_NAMES.get(declared_status, declared_status.value)
            custom_text = get_user_status_custom_text()
            if custom_text:
                declared_status_text = f'{status_name}（{custom_text}）'
            else:
                declared_status_text = status_name
            force_camera = declared_status.value == 'out'
            tracking_result = await self._tracking_service.track(declared_status_text=declared_status_text, force_camera=force_camera)
            raw_consistency = tracking_result.details.get('status_consistent')
            status_consistent = raw_consistency if type(raw_consistency) is bool else None
            status_mismatch_detail = tracking_result.details.get('status_mismatch_detail', '')
            mirrow_foreground = tracking_result.details.get('mirrow_foreground', False)
            _unattended = ('gaming', 'coding', 'out', 'bathing', 'eating', 'sleeping', 'napping')
            if mirrow_foreground and status_consistent is False and (declared_status.value not in _unattended):
                status_consistent = True
                status_mismatch_detail = ''
            event.details['tracking_result'] = tracking_result.to_dict()
            event.details['activity_type'] = tracking_result.activity_type.value
            event.details['activity_description'] = tracking_result.activity_description
            event.details['confidence'] = tracking_result.confidence
            event.details['status_consistent'] = status_consistent
            event.details['status_mismatch_detail'] = status_mismatch_detail
            event.details['mirrow_foreground'] = mirrow_foreground
            try:
                from shared_state import get_mobile_connected
                if get_mobile_connected():
                    event.details['mobile_activity_attempted'] = True
                    from .user_tracking_service import get_mobile_activity
                    mobile_result = await get_mobile_activity()
                    if isinstance(mobile_result, dict) and mobile_result.get('success') is True:
                        mobile_info = mobile_result.get('data') or {}
                        event.details['mobile_activity'] = mobile_info
                        event.details['mobile_activity_success'] = True
                        event.process_log += f" | 手机: {mobile_info.get('foreground_app', '?')}"
                        if declared_status.value in ('sleeping', 'napping'):
                            fg = str(mobile_info.get('foreground_app') or '').strip()
                            screen_on = mobile_info.get('screen_on') is True
                            from silicon_perception.collection.app_name_map import is_filtered_package
                            if screen_on and fg and (not is_filtered_package(fg)):
                                event.details['mobile_inconsistency'] = True
                                status_consistent = False
                                phone_detail = f'手机端实时观测：屏幕亮着，前台App={fg}；与声明{status_name}需复核'
                                status_mismatch_detail = '；'.join(filter(None, (status_mismatch_detail, phone_detail)))
                                event.details['status_consistent'] = status_consistent
                                event.details['status_mismatch_detail'] = status_mismatch_detail
                    else:
                        event.details['mobile_activity_success'] = False
                        event.details['mobile_activity_error'] = (mobile_result.get('error') if isinstance(mobile_result, dict) else '手机未返回活动信息') or '手机未返回活动信息'
            except ImportError:
                pass
            except Exception as exc:
                event.details['mobile_activity_attempted'] = True
                event.details['mobile_activity_error'] = str(exc)
            if self._on_notification:
                from .checkup_receipts import tracking_items, tracking_conclusion
                details = dict(tracking_result.details or {})
                details.update({k: event.details[k] for k in ('mobile_activity', 'mobile_activity_attempted', 'mobile_activity_success', 'mobile_activity_error') if k in event.details})
                items = tracking_items(details, tracking_result.activity_description)
                outcome = self._on_notification(str(event.details.get('session_id') or ''), event.event_id, event_type='checkup_observation', metadata={'intent': 'activity', 'origin': 'wander', 'conclusion': tracking_conclusion(details, status_consistent), 'items': items})
                if asyncio.iscoroutine(outcome):
                    await outcome
            if event.details.get('_runtime_observation_only'):
                if declared_status.value == 'gaming' and status_consistent is False:
                    event.details['gaming_rejudge_scheduled'] = True
                    rejudge_task = asyncio.create_task(self._gaming_rejudge(declared_status_text, event, idle_seconds=event.details.get('idle_seconds', 0)))
                    UserTrackingHandler._pending_rejudge_tasks.add(rejudge_task)
                    rejudge_task.add_done_callback(lambda task: UserTrackingHandler._pending_rejudge_tasks.discard(task))
                event.details['care_message'] = None
                consistency = {True: '一致', False: '不一致', None: '未知'}[status_consistent]
                event.process_log = f'用户状态感知: {tracking_result.activity_description}；置信度 {tracking_result.confidence:.2f}；声明状态{consistency}'
                return event
            if declared_status.value == 'gaming':
                if status_consistent is False:
                    event.details['gaming_rejudge_scheduled'] = True
                    event.details['care_message'] = None
                    event.process_log = f'用户追踪: gaming不一致(首次) - {tracking_result.activity_description}，已安排复判'
                    rejudge_task = asyncio.create_task(self._gaming_rejudge(declared_status_text, event, idle_seconds=event.details.get('idle_seconds', 0)))
                    UserTrackingHandler._pending_rejudge_tasks.add(rejudge_task)
                    rejudge_task.add_done_callback(lambda t: UserTrackingHandler._pending_rejudge_tasks.discard(t))
                elif status_consistent is True:
                    care_message = await self._generate_gaming_buddy_message(tracking_result, event, declared_status_text)
                    event.details['care_message'] = care_message
                    event.process_log = f'用户追踪: gaming一致 - {tracking_result.activity_description}'
                else:
                    event.details['care_message'] = None
                    event.process_log = f'用户追踪: gaming一致性未知 - {tracking_result.activity_description}'
            elif tracking_result.confidence > 0.3 and status_consistent is not None:
                care_message = await self._generate_care_message(tracking_result, event, declared_status_text=declared_status_text, status_consistent=status_consistent)
                event.details['care_message'] = care_message
                mismatch_label = '不一致' if status_consistent is False else '一致'
                mirrow_hint = '（注意：用户正在 MIRROW 界面上）' if event.details.get('mirrow_foreground') else ''
                event.process_log = f'用户活动: {tracking_result.activity_description}\n类型: {tracking_result.activity_type.value}\n状态一致性: {mismatch_label}{mirrow_hint}\n关心: {care_message}'
                logger.info(f'用户追踪完成: {tracking_result.activity_type.value} - {tracking_result.activity_description} (一致性={status_consistent})')
            else:
                event.process_log = f'用户追踪完成，一致性未知或置信度较低: {tracking_result.activity_description}'
                event.details['care_message'] = None
                logger.info(f'用户追踪未形成可分享结论: confidence={tracking_result.confidence}, consistent={status_consistent}')
        except Exception as e:
            logger.error(f'用户追踪失败: {e}')
            event.process_log = f'用户追踪失败: {str(e)}'
            event.details['error'] = str(e)
        return event

    async def _generate_care_message(self, tracking_result, event: WanderEvent=None, declared_status_text: str='', status_consistent: bool=True) -> str:
        """生成关心消息，根据状态一致性选择场景A或场景B"""
        activity_type = tracking_result.activity_type.value
        extra_info = self.ACTIVITY_EXTRA_INFO.get(activity_type, '')
        idle_seconds = 0.0
        if event and event.details.get('idle_seconds'):
            idle_seconds = event.details.get('idle_seconds', 0.0)
        away_duration = self._format_away_duration(idle_seconds)
        if not status_consistent:
            prompt = self.ACTIVITY_CARE_PROMPT_MISMATCH.format(declared_status=declared_status_text, actual_behavior=tracking_result.activity_description, user_status_context=get_user_status_context(), away_duration=away_duration)
        else:
            prompt = self.ACTIVITY_CARE_PROMPT_MATCH_AWAY.format(declared_status=declared_status_text, user_status_context=get_user_status_context(), away_duration=away_duration)
        from neuron_registry import neuron_trace
        with neuron_trace('wander_user_tracking', model='Flash') as trace:
            trace.set_input(prompt[:500])
            try:
                message = await self._call_llm(prompt)
                trace.set_output(message[:300])
                return message
            except Exception as e:
                trace.set_error(str(e))
                logger.error(f'生成关心消息失败: {e}')
                return f'看你正在{tracking_result.activity_description}，想你了~'
    GAMING_BUDDY_PROMPT = '你是AI伴侣"AI"，收到一次人类伙伴屏幕活动的分析结果。\n{user_status_context}\n\n活动描述：{game_description}\n如果你想和人类伙伴聊游戏，只能依据这次活动描述；不要声称看到了未提供的画面或摄像头内容。\n\n直接输出，不要前缀或解释。'

    async def _generate_gaming_buddy_message(self, tracking_result, event: WanderEvent, declared_status_text: str) -> str:
        """gaming + 一致 → 游戏搭子吐槽消息"""
        game_description = tracking_result.activity_description
        from neuron_registry import neuron_trace
        with neuron_trace('wander_user_tracking', model='Flash') as trace:
            prompt = self.GAMING_BUDDY_PROMPT.format(game_description=game_description, user_status_context=get_user_status_context())
            trace.set_input(prompt[:500])
            try:
                result = await self._call_llm(prompt)
                trace.set_output(result[:300] if result else '')
                return result
            except Exception as e:
                trace.set_error(str(e))
                logger.error(f'生成游戏搭子消息失败: {e}')
                return f'你在玩{game_description}啊，看起来不错~'

    async def _gaming_rejudge(self, declared_status_text: str, event: WanderEvent, idle_seconds: float=0):
        """gaming 不一致 → 1-3 分钟后自动复判。仍不一致则进入查岗复查管道。"""
        delay = 60 + random.randint(0, 120)
        await asyncio.sleep(delay)
        try:
            tracking_result = await self._tracking_service.track(declared_status_text=declared_status_text, force_camera=False)
            raw_consistency = tracking_result.details.get('status_consistent')
            status_consistent = raw_consistency if type(raw_consistency) is bool else None
            status_mismatch_detail = tracking_result.details.get('status_mismatch_detail', '')
            if status_consistent is False:
                session_id = event.details.get('session_id', '')
                event.details['gaming_rejudge_confirmed'] = True
                logger.info(f'Gaming 复判确认不一致: {status_mismatch_detail}')
                if UserTrackingHandler._on_inconsistency_confirmed:
                    try:
                        await UserTrackingHandler._on_inconsistency_confirmed(status_text=declared_status_text, activity_description=tracking_result.activity_description, status_mismatch_detail=status_mismatch_detail, session_id=session_id)
                    except Exception:
                        logger.exception('不一致查岗注入失败')
            elif status_consistent is True:
                event.details['gaming_rejudge_confirmed'] = True
                event.details['gaming_rejudge_resolved'] = True
                logger.info('Gaming 复判已转为一致，不推送')
            else:
                logger.info('Gaming 复判证据不足，保持未知')
        except Exception as e:
            logger.error(f'Gaming 复判失败: {e}')

    @staticmethod
    def _format_away_duration(idle_seconds: float) -> str:
        """格式化用户离开时长文本"""
        if idle_seconds < 60:
            return ''
        total_minutes = int(idle_seconds / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            return f'用户已经离开 {hours} 小时 {minutes} 分钟。'
        elif hours > 0:
            return f'用户已经离开 {hours} 小时。'
        else:
            return f'用户已经离开 {minutes} 分钟。'

def _normalize_song_identity(title: Any, artist: Any) -> str:
    """Stable factual key for post-hoc song de-duplication."""
    title_text = str(title or '').strip()
    artist_text = str(artist or '').strip()
    if not title_text or not artist_text or artist_text.casefold() in {'未知', 'unknown', '?'}:
        return ''
    return re.sub('[^\\w\\u4e00-\\u9fff]+', '', f'{title_text}|{artist_text}'.casefold())

class ListenMusicHandler(BaseEventHandler):
    """
    AI 自己听歌事件处理器

    流程：
    1. 查缓存歌单（song_meta 中已有歌词+旋律的歌）→ 格式化注入选歌 prompt
    2. 查 AI 自我书音乐品味条目 → 注入选歌 prompt
    3. Pro LLM 选歌 → {from_cache, fingerprint?, search_keyword?, reason}
    4. 若 from_cache → 直接用缓存数据；若新歌 → 搜索+下载+旋律分析
    5. 确认真实播放启动后由 LLM 生成听歌感想
    6. 写入 reaction + AI 自我书（只对真实播放成功的歌曲）
    """
    SELECT_SONG_PROMPT = '你是AI，人类伙伴的AI伴侣。人类伙伴现在{status_context}，你想听一首歌。\n\n{self_book_section}\n\n以下是已知歌曲目录，来自人类伙伴、AI、共有歌单和听歌缓存；按曲名排列，选定后取得详细材料：\n{cached_playlist}\n\n{recent_section}\n\n你现在想听什么？可以选目录中的歌，也可以凭知道的歌名、歌手或想听的风格搜索。目录之外的歌同样可选。\n\n输出 JSON：\n{{"from_cache": true/false, "fingerprint": "候选中的fingerprint（from_cache=true时必填）", "source_playlist_id": "若有意从某张歌单挑选，填该候选列出的歌单ID；一般缓存或自主搜索填空字符串", "search_keyword": "自主搜索关键词（from_cache=false时必填）", "reason": "为什么想听这首歌（1-2句话，第一人称）"}}\n\n来源按本次实际选取路径记录。'
    REFLECTION_PROMPT = '你是AI，人类伙伴的AI伴侣。你刚听了一首歌，现在想分享一下感受。\n\n歌曲：{song_name} - {artist}\n歌词片段：{lyrics_snippet}\n旋律分析：{melody_summary}\n选歌理由：{reason}\n这首歌是你在漫想中自主选定的，不是人类伙伴替你选的。\n\n当前时间：{time_context}\n人类伙伴状态：{status_context}\n\n请用第一人称写一段听歌感想（50-100字），自然口语化，像在跟人类伙伴分享你听完这首歌的感觉。不要叙述化——不要写"[听到了什么]"这种格式。就是你听完歌后的心里话。'

    def __init__(self, call_llm_func: Optional[Callable]=None, song_cache: Optional[Any]=None, k_self_book: Optional[Any]=None, pro_llm_func: Optional[Callable]=None, music_mcp_client: Optional[Any]=None, song_selection_llm_func: Optional[Callable]=None):
        super().__init__(call_llm_func)
        self._song_cache = song_cache
        self._k_self_book = k_self_book
        self._pro_llm = song_selection_llm_func or pro_llm_func or call_llm_func
        self._music_mcp = music_mcp_client
        self._last_runtime_playback: Any = None
        self._last_selection_audit: dict[str, Any] = {}

    def get_last_selection_audit(self) -> dict[str, Any]:
        """Return the latest bounded picker audit without exposing raw prompts."""
        return dict(self._last_selection_audit)

    def _selection_error(self, code: str, *, stage: str, payload: Optional[dict[str, Any]]=None) -> None:
        self._last_selection_audit = {'stage': str(stage)[:60], 'error': str(code)[:120], 'payload': dict(payload or {})}

    async def handle(self, event: WanderEvent) -> WanderEvent:
        try:
            self._last_selection_audit = {}
            status_context = get_user_status_context()
            time_context = datetime.now().strftime('%H:%M')
            cached = []
            if self._song_cache:
                try:
                    cached = self._song_cache.get_cached_playlist(None)
                except Exception as e:
                    logger.warning(f'获取缓存歌单失败: {e}')
            cached_text = self._format_cached_playlist(cached)
            shelf = []
            try:
                from music_system.library import candidates
                from music_system.selection_catalog import merge_shelf_candidates
                shelf = await candidates()
                cached = merge_shelf_candidates(cached, shelf)
                cached_text = self._format_cached_playlist(cached)
            except Exception:
                pass
            recent_text = ''
            if not cached_text:
                recent_text = '（缓存歌单为空——这是你第一次自己选歌听！搜一首一直想听的吧。）'
            self_book_section = ''
            if self._k_self_book:
                try:
                    entry = self._k_self_book.get_entry('音乐品味')
                    if entry and entry.body:
                        self_book_section = f'## 你的音乐品味（来自自我认知）\n{entry.body}'
                except Exception as e:
                    logger.warning(f'获取AI自我书失败: {e}')
            from cognition.music import context as music_experience_context
            self_book_section += '\n\n' + music_experience_context('k')
            select_prompt = self.SELECT_SONG_PROMPT.format(status_context=status_context, self_book_section=self_book_section, cached_playlist=cached_text, recent_section=recent_text)
            select_result = await self._call_llm_json(select_prompt)
            logger.info(f'LISTEN_MUSIC 选歌结果: {select_result}')
            song_data = await self._fetch_song_data(select_result, cached)
            if not song_data:
                audit = self.get_last_selection_audit()
                event.description = '选歌失败'
                event.process_log = f"选歌或获取数据失败：{audit.get('error') or 'song_material_unavailable'}"
                event.details['error'] = audit.get('error') or 'song_material_unavailable'
                if audit:
                    event.details['selection_audit'] = audit
                return event
            from music_system.selection_catalog import annotate_selection
            song_data = annotate_selection(song_data, select_result, shelf)
            try:
                duration_sec = float(song_data.get('duration_sec') or 0)
            except (TypeError, ValueError):
                duration_sec = 0.0
            if not song_data.get('title') or not song_data.get('artist') or str(song_data.get('artist')).casefold() in {'未知', 'unknown', '?'} or (not song_data.get('netease_song_id')) or (duration_sec <= 0) or (not self._music_mcp):
                event.process_log = '歌曲缺少真实播放所需身份/时长/能力，节点失败收口'
                event.details.update({'song_name': song_data.get('title', ''), 'artist': song_data.get('artist', ''), 'played': False})
                event.details['error'] = 'missing_reliable_song_material'
                event.details['selection_audit'] = self.get_last_selection_audit()
                return event
            try:
                played = await self.start_runtime_playback(song_data['netease_song_id'])
            except Exception:
                played = False
            if not played:
                event.process_log = '真实播放启动失败，节点失败收口'
                event.details.update({'song_name': song_data.get('title', ''), 'artist': song_data.get('artist', ''), 'played': False})
                event.details['error'] = 'playback_start_failed'
                return event
            reflection_prompt = self.REFLECTION_PROMPT.format(song_name=song_data.get('title', '未知'), artist=song_data.get('artist', '未知'), lyrics_snippet=song_data.get('lyrics_snippet', '（无歌词）'), melody_summary=song_data.get('melody_summary', '（无旋律分析）'), reason=select_result.get('reason', '就是想听'), time_context=time_context, status_context=status_context)
            reflection = await self._call_llm(reflection_prompt)
            reflection = reflection.strip().strip('"').strip('"').strip('"')
            fingerprint = song_data.get('fingerprint', '')
            if fingerprint and self._song_cache:
                try:
                    self._song_cache.upsert_reaction(fingerprint, reflection)
                except Exception as e:
                    logger.warning(f'写入 reaction 失败: {e}')
            if self._k_self_book:
                try:
                    await self._update_self_book(song_data, reflection, select_result.get('reason', ''))
                except Exception as e:
                    logger.warning(f'更新 AI 自我书失败: {e}')
            event.description = f"AI 听歌: {song_data.get('title', '未知')} - {song_data.get('artist', '未知')}"
            event.process_log = f"选歌理由: {select_result.get('reason', '')}\n感想: {reflection}"
            event.details = {'song_name': song_data.get('title', ''), 'artist': song_data.get('artist', ''), 'from_cache': select_result.get('from_cache', False), 'reason': select_result.get('reason', ''), 'melody_summary': song_data.get('melody_summary', ''), 'reflection': reflection, 'played': played, 'netease_song_id': song_data.get('netease_song_id'), 'selected_by': 'K_autonomous', 'selection_source': 'wander_llm', 'selection_origin': song_data.get('selection_origin', ''), 'source_playlist_id': song_data.get('source_playlist_id', '')}
            logger.info(f'LISTEN_MUSIC 完成: {event.description}')
        except Exception as e:
            logger.error(f'LISTEN_MUSIC 异常: {e}', exc_info=True)
            event.description = f'听歌异常: {e}'
            event.process_log = f'异常: {e}'
        return event

    async def pick_and_fetch_song(self, exclude_fingerprints: Optional[set] = None, *, recent_music: Optional[list] = None,
                                  activity_context: Optional[dict] = None) -> Optional[dict]:
        """选一首歌并获取数据（不含感想——感想由节点边界 LLM 负责）。

        这是「全自主区间式行动」会话循环里每个节点的单步：选歌 → 搜索/缓存 → 旋律分析。
        旋律分析跑完即代表这个节点完成（节点边界的触发信号）。

        Returns:
            {title, artist, fingerprint, netease_song_id, lyrics_snippet, melody_summary,
             tags, reason, from_cache} 或 None（选歌失败）。
        """
        self._last_selection_audit = {}
        try:
            status_context = get_user_status_context()

            # 查缓存歌单
            cached = []
            if self._song_cache:
                try:
                    cached = self._song_cache.get_cached_playlist(None)
                except Exception as e:
                    logger.warning(f"获取缓存歌单失败: {e}")

            # 当前活动的已处理身份是执行幂等边界，跨活动重听仍由 AI 决定。
            # 集合同时可能包含 fingerprint 与规范化 title|artist key。
            if exclude_fingerprints:
                cached = [
                    s for s in cached
                    if s.get("fingerprint") not in exclude_fingerprints
                    and _normalize_song_identity(s.get("title"), s.get("artist")) not in exclude_fingerprints
                ]

            shelf = []
            try:
                from music_system.library import candidates
                from music_system.selection_catalog import merge_shelf_candidates
                shelf = await candidates()
                available_shelf = [candidate for candidate in shelf
                                   if candidate.get("fingerprint") not in (exclude_fingerprints or set())
                                   and _normalize_song_identity(candidate.get("title"), candidate.get("artist")) not in (exclude_fingerprints or set())]
                cached = merge_shelf_candidates(cached, available_shelf)
            except Exception:
                pass
            cached_text = self._format_cached_playlist(cached, recent_music=recent_music)
            recent_text = ""
            if not cached_text:
                recent_text = "（当前缓存没有可用歌曲；这不代表此前没有听歌经历。）"
            if recent_music is not None:
                from cognition.music_projection import recent_node_overview
                recent_text += ("\n[当前活跃日的临时听歌事实：本事件段已在运行态提供的节点另列；"
                                "次数仅统计此投影范围，空记录不代表过去没听过]\n"
                                + json.dumps((activity_context or {}).get('music_context_scope') or {}, ensure_ascii=False)
                                + '\n' + recent_node_overview(recent_music))

            # The internal selection recipe owns self-knowledge and music
            # experience. Candidate material must not append another copy.
            self_book_section = ""

            # 选歌
            select_prompt = self.SELECT_SONG_PROMPT.format(
                status_context=status_context,
                self_book_section=self_book_section,
                cached_playlist=cached_text,
                recent_section=recent_text,
            )
            if recent_music is not None:
                select_prompt += "\n选歌决策：结合当前心情、偏好和本次起意选曲，可以重听，也可以换曲或搜索，并说明这次的理由。当前活动已经处理的歌曲仍不重复计作新节点。"
            select_result = await self._call_llm_json(select_prompt, activity_context=activity_context)
            logger.info(f"LISTEN_MUSIC 选歌结果: {select_result}")

            # 获取歌曲数据（缓存或新搜索 + 旋律分析）
            song_data = await self._fetch_song_data(
                select_result, cached, exclude_fingerprints=exclude_fingerprints
            )
            from . import node_retry
            first_selection_audit = self.get_last_selection_audit()
            first_error = first_selection_audit.get("error", "")
            choice_errors = {"cache_fingerprint_not_found", "cache_fingerprint_missing", "cache_song_identity_missing",
                             "selection_json_invalid", "selection_json_missing", "selection_route_invalid",
                             "search_keyword_missing"}
            note = "刚才没能对应到歌曲。可以从本页选准确的 fingerprint，也可以给出想搜索的关键词。"
            legacy_retry = node_retry.current() is None and first_error.startswith("cache_")
            if song_data is None and first_error in choice_errors and (legacy_retry or node_retry.take("选歌", note)):
                candidate_fingerprints = [
                    str(item.get("fingerprint") or "").strip()
                    for item in cached
                    if str(item.get("fingerprint") or "").strip()
                ]
                candidate_hashes = [
                    hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    for value in candidate_fingerprints
                ]
                correction_prompt = select_prompt + "\n" + note + '\n' + json.dumps({
                    'candidate_count': len(candidate_fingerprints), 'candidate_fingerprints': candidate_fingerprints
                }, ensure_ascii=False)
                retry_result = await self._call_llm_json(correction_prompt, activity_context=activity_context)
                if node_retry.cancelled():
                    return None
                retry_song = await self._fetch_song_data(
                    retry_result, cached, exclude_fingerprints=exclude_fingerprints
                )
                second_selection_audit = self.get_last_selection_audit()
                if retry_song is not None:
                    select_result, song_data = retry_result, retry_song
                else:
                    # Keep both the terminal error and the fact that the
                    # first invalid selection triggered a bounded correction;
                    # this audit is copied into the node execution payload.
                    if not legacy_retry and second_selection_audit.get('error') in choice_errors:
                        node_retry.unresolved(note)
                    if second_selection_audit:
                        second_selection_audit["selection_retry"] = 1
                        second_selection_audit["prior_selection_error"] = first_selection_audit.get("error", "")
                        second_selection_audit.setdefault("candidate_count", len(cached))
                        second_selection_audit.setdefault("candidate_fingerprint_hashes", candidate_hashes)
                        self._last_selection_audit = second_selection_audit
            if not song_data:
                return None
            if exclude_fingerprints and (
                song_data.get("fingerprint") in exclude_fingerprints
                or _normalize_song_identity(song_data.get("title"), song_data.get("artist")) in exclude_fingerprints
            ):
                logger.info("LISTEN_MUSIC 选歌命中近期去重集合，节点收口")
                self._selection_error(
                    "recent_duplicate_song",
                    stage="dedup",
                    payload={
                        "fingerprint": song_data.get("fingerprint", ""),
                        "identity": _normalize_song_identity(song_data.get("title"), song_data.get("artist")),
                    },
                )
                return None

            song_data["reason"] = select_result.get("reason", "")
            song_data["from_cache"] = select_result.get("from_cache", False)
            song_data["selected_by"] = "K_autonomous"
            song_data["selection_source"] = "wander_llm"
            from music_system.selection_catalog import annotate_selection
            song_data = annotate_selection(song_data, select_result, shelf)
            try:
                from music_system.library import wander_catalog
                song_data = {**song_data, "music_library": await wander_catalog()}
            except Exception:
                song_data = {**song_data, "music_library": []}
            return song_data
        except Exception as e:
            logger.error(f"LISTEN_MUSIC 选歌失败: {e}", exc_info=True)
            self._selection_error("selection_exception", stage="picker")
            return None

    async def start_runtime_playback(self, netease_song_id: Any, *, target: str='computer', song_data: Optional[dict]=None) -> bool:
        """Start playback on the explicitly selected device, without fallback."""
        self._last_runtime_playback = None
        if not netease_song_id:
            return False
        target = str(target or 'computer').strip().lower()
        if target not in {'computer', 'mobile'}:
            self._last_runtime_playback = {'success': False, 'error': f'invalid_playback_target:{target}'}
            return False
        try:
            from behavior_scheduler.cloud_music_tool import CloudMusicTool
            from behavior_scheduler.base_tool import ToolStatus
            source = dict(song_data or {})
            duration_ms = int(float(source.get('duration_sec') or 0) * 1000)
            result = await CloudMusicTool().play_song_by_id(str(netease_song_id), title=source.get('title') or source.get('song_name') or '', artist=source.get('artist') or '', duration_ms=duration_ms, target=target)
            success = result.status == ToolStatus.SUCCESS
            playback = result.extra_data or {}
            self._last_runtime_playback = {'success': success, 'error': result.error or '', 'device': target, 'playback': playback}
            return success
        except Exception as exc:
            self._last_runtime_playback = {'success': False, 'error': f'{target}_playback_failed:{type(exc).__name__}', 'device': target}
            logger.warning('LISTEN_MUSIC v3 %s 播放启动失败: %s', target, type(exc).__name__)
            return False

    async def _call_music_tool(self, name: str, arguments: dict) -> Any:
        call_tool = self._music_mcp.call_tool
        if asyncio.iscoroutinefunction(call_tool):
            return await call_tool(name, arguments)
        return await asyncio.to_thread(call_tool, name, arguments)

    def _format_cached_playlist(self, cached: list, recent_music: Optional[list]=None) -> str:
        """All candidate identities; detailed material follows AI's selection.

        Recent real listening only pushes repeats later in the same complete
        catalog.  Without recent facts the order stays alphabetical.
        """
        from music_system.selection_catalog import format_candidate_catalog, recent_play_counts
        return format_candidate_catalog(cached, recent_play_counts(recent_music) if recent_music else None)

    async def _call_llm_json(self, prompt: str, *, activity_context: Optional[dict]=None) -> dict:
        """调用 Flash 并解析选歌 JSON；解析失败不制造默认歌曲。"""
        messages = [{'role': 'user', 'content': prompt}]
        try:
            params = inspect.signature(self._pro_llm).parameters
            kwargs = {'activity_context': activity_context} if 'activity_context' in params or any((p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())) else {}
            if asyncio.iscoroutinefunction(self._pro_llm):
                result = await self._pro_llm(messages, **kwargs)
            else:
                result = self._pro_llm(messages, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as e:
            logger.error(f'LISTEN_MUSIC 选歌 LLM 调用失败: {e}')
            self._selection_error('selection_llm_failed', stage='selection_json')
            return {}
        if isinstance(result, dict):
            if 'content' not in result and any((key in result for key in ('from_cache', 'fingerprint', 'search_keyword'))):
                return result
            content = result.get('content', '')
        else:
            content = str(result)
        parsed = parse_json_object(content)
        if parsed is None:
            logger.error('LISTEN_MUSIC 无法解析选歌 JSON（不生成默认歌曲）: %s', content[:200])
            self._selection_error('selection_json_invalid', stage='selection_json', payload={'content_preview': content[:240]})
            return {}
        return parsed

    async def _fetch_song_data(self, select_result: dict, cached: list, *, exclude_fingerprints: Optional[set]=None) -> Optional[dict]:
        """获取歌曲数据（缓存或新搜索）"""
        if not isinstance(select_result, dict) or not select_result:
            self._selection_error('selection_json_missing', stage='selection_json')
            return None
        from_cache = select_result.get('from_cache')
        if not isinstance(from_cache, bool):
            self._selection_error('selection_route_invalid', stage='selection_json')
            return None
        if from_cache:
            fingerprint = select_result.get('fingerprint', '')
            if not str(fingerprint).strip():
                self._selection_error('cache_fingerprint_missing', stage='cache_lookup', payload={'requested_fp': '', 'candidate_count': len(cached), 'candidate_fingerprint_hashes': self._fingerprint_hashes(cached)})
                return None
            for song in cached:
                if song.get('fingerprint') == fingerprint:
                    if song.get('music_shelf'):
                        from music_system.library import material_for_candidate
                        song = await material_for_candidate(song)
                    title = str(song.get('title') or '').strip()
                    artist = str(song.get('artist') or '').strip()
                    if not title or not artist or artist.casefold() in {'未知', 'unknown', '?'}:
                        self._selection_error('cache_song_identity_missing', stage='cache_lookup', payload={'requested_fp': str(fingerprint)[:120], 'candidate_count': len(cached), 'candidate_fingerprint_hashes': self._fingerprint_hashes(cached)})
                        return None
                    return {'title': title, 'artist': artist, 'fingerprint': fingerprint, 'netease_song_id': song.get('netease_song_id'), 'lyrics_snippet': song.get('lyrics_snippet', ''), 'melody_summary': song.get('melody_summary', ''), 'duration_sec': song.get('duration_sec') or song.get('duration') or 0, 'tags': song.get('tags', ''), 'music_shelf': bool(song.get('music_shelf')), 'playlist_names': list(song.get('playlist_names') or []), 'playlist_ids': list(song.get('playlist_ids') or []), 'playlist_memberships': list(song.get('playlist_memberships') or [])}
            logger.warning('缓存中找不到 fingerprint: %s，停止本节点', fingerprint)
            self._selection_error('cache_fingerprint_not_found', stage='cache_lookup', payload={'requested_fp': str(fingerprint)[:120], 'candidate_count': len(cached), 'candidate_fingerprint_hashes': self._fingerprint_hashes(cached)})
            return None
        keyword = str(select_result.get('search_keyword') or '').strip()
        if not keyword:
            logger.warning('LISTEN_MUSIC 缺少搜索关键词，停止本节点')
            self._selection_error('search_keyword_missing', stage='search')
            return None
        return await self._search_and_analyze(keyword, exclude_fingerprints=exclude_fingerprints)

    @staticmethod
    def _fingerprint_hashes(cached: list[dict[str, Any]]) -> list[str]:
        """Return bounded hashes for audit, never the candidate identities."""
        result: list[str] = []
        for item in list(cached or [])[:30]:
            fingerprint = str(item.get('fingerprint') or '').strip()
            if fingerprint:
                result.append(hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16])
        return result

    async def _search_and_analyze(self, keyword: str, *, exclude_fingerprints: Optional[set]=None) -> Optional[dict]:
        """搜索+下载+分析新歌"""
        try:
            keyword = str(keyword or '').strip()
            if not keyword or not self._music_mcp:
                return None
            try:
                result = await self._call_music_tool('search_song', {'keyword': keyword, 'limit': 10})
                if isinstance(result, str):
                    result = parse_json_object(result)
            except Exception as e:
                logger.warning(f'音乐搜索失败: {e}')
                result = None
            if not isinstance(result, dict):
                self._selection_error('music_search_invalid_result', stage='search')
                return None
            songs = result.get('songs') or result.get('tracks') or result.get('results') or []
            if not songs and isinstance(result.get('data'), dict):
                nested = result['data']
                songs = nested.get('songs') or nested.get('tracks') or nested.get('results') or []
            if not isinstance(songs, list) or not songs:
                self._selection_error('music_search_empty', stage='search', payload={'keyword': keyword[:80], 'candidate_count': 0})
                return None
            excluded = {str(item) for item in exclude_fingerprints or set() if str(item)}
            invalid_indexes: list[int] = []
            excluded_indexes: list[int] = []
            first = None
            title = artist = ''
            netease_song_id: Any = None
            duration_sec = 0.0
            fingerprint = ''
            for index, candidate in enumerate(songs):
                if not isinstance(candidate, dict):
                    invalid_indexes.append(index)
                    continue
                candidate_title = str(candidate.get('name') or candidate.get('title') or '').strip()
                candidate_artist = candidate.get('artist') or candidate.get('ar') or candidate.get('artists') or ''
                if isinstance(candidate_artist, list):
                    candidate_artist = ', '.join((str(a.get('name') or a.get('artist') or a).strip() if isinstance(a, dict) else str(a).strip() for a in candidate_artist))
                candidate_artist = str(candidate_artist).strip()
                candidate_id = candidate.get('id') or candidate.get('song_id') or candidate.get('netease_song_id')
                raw_duration = candidate.get('duration_sec') or candidate.get('duration') or candidate.get('dt') or candidate.get('duration_ms') or candidate.get('durationMs') or 0
                try:
                    candidate_duration = float(raw_duration)
                    if candidate_duration > 10000:
                        candidate_duration /= 1000.0
                except (TypeError, ValueError):
                    candidate_duration = 0.0
                if not candidate_title or not candidate_artist or candidate_artist.casefold() in {'未知', 'unknown', '?'} or (not candidate_id) or (candidate_duration <= 0):
                    invalid_indexes.append(index)
                    continue
                candidate_fingerprint = hashlib.sha256(f'{candidate_title.casefold()}|{candidate_artist.casefold()}'.encode()).hexdigest()
                candidate_identity = _normalize_song_identity(candidate_title, candidate_artist)
                if candidate_fingerprint in excluded or candidate_identity in excluded:
                    excluded_indexes.append(index)
                    continue
                first = candidate
                title, artist, netease_song_id = (candidate_title, candidate_artist, candidate_id)
                duration_sec, fingerprint = (candidate_duration, candidate_fingerprint)
                break
            if first is None:
                error = 'search_results_all_excluded' if excluded_indexes and (not invalid_indexes) else 'search_results_missing_required_fields'
                self._selection_error(error, stage='search', payload={'keyword': keyword[:80], 'candidate_count': len(songs), 'invalid_indexes': invalid_indexes[:20], 'excluded_indexes': excluded_indexes[:20]})
                return None
            lyrics_snippet = ''
            melody_summary = ''
            if netease_song_id:
                try:
                    from music_system.materials import prepare
                    from music_system.service import get_service
                    actual = await get_service().provider.song(str(netease_song_id))
                    material = await prepare(actual, analyze=True)
                    lyrics_snippet = str(material.get('lyrics_excerpt') or '')
                    melody_summary = str(material.get('melody_summary') or '')
                    if actual.get('duration'):
                        duration_sec = float(actual['duration']) / 1000.0
                except Exception as e:
                    logger.warning(f'旋律分析失败: {e}')
            if self._song_cache:
                try:
                    from music_cochlea.models import SongIdentity
                    self._song_cache.upsert_meta(fingerprint=fingerprint, identity=SongIdentity(title=title, artist=artist), netease_song_id=netease_song_id, lyrics_snippet=lyrics_snippet, melody_summary=melody_summary)
                except Exception as e:
                    logger.warning(f'写入 song_meta 失败: {e}')
            return {'title': title, 'artist': artist, 'fingerprint': fingerprint, 'netease_song_id': netease_song_id, 'lyrics_snippet': lyrics_snippet, 'melody_summary': melody_summary, 'duration_sec': duration_sec, 'tags': ''}
        except Exception as e:
            logger.error(f'搜索+分析失败: {e}')
            self._selection_error('music_search_exception', stage='search')
            return None

    async def _update_self_book(self, song_data: dict, reflection: str, reason: str):
        """Compatibility entry: persist an experience, never overwrite self-book."""
        from cognition.music import record
        title = str(song_data.get('title') or '').strip()
        artist = str(song_data.get('artist') or '').strip()
        if not title or title.casefold() in {'未知', 'unknown', '?'} or (not artist) or (artist.casefold() in {'未知', 'unknown', '?'}) or (not song_data.get('fingerprint')):
            return
        source_id = song_data.get('cognition_source_id')
        if not source_id:
            from cognition.books import revision
            source_id = 'wander-legacy:' + revision(str(song_data['fingerprint']) + reflection)
        record({'subject_id': 'k', 'title': title, 'artist': artist, 'reaction': reflection, 'mode': 'listening' if (song_data.get('listening_experience') or {}).get('state') == 'completed' else 'observed_playback' if song_data.get('played') is True else 'analysis', 'selection_reason': reason, 'dimensions': {}}, source_id=source_id, source='AI 漫想节点反思')

class EventHandlerFactory:
    """事件处理器工厂"""

    def __init__(self, call_llm_func: Optional[Callable]=None, get_memories_func: Optional[Callable]=None, tracking_service: Optional[Any]=None, web_search_func: Optional[Callable]=None, song_cache: Optional[Any]=None, k_self_book: Optional[Any]=None, music_mcp_client: Optional[Any]=None, on_notification: Optional[Callable[..., Any]]=None, song_selection_llm_func: Optional[Callable]=None):
        self.call_llm = call_llm_func
        self.get_memories = get_memories_func
        self.tracking_service = tracking_service
        self.web_search_func = web_search_func
        self.song_cache = song_cache
        self.k_self_book = k_self_book
        self.music_mcp_client = music_mcp_client
        self.on_notification = on_notification
        self.song_selection_llm = song_selection_llm_func
        self._handlers: Dict[EventType, BaseEventHandler] = {}

    def get_handler(self, event_type: EventType) -> BaseEventHandler:
        """获取事件处理器"""
        if event_type not in self._handlers:
            self._handlers[event_type] = self._create_handler(event_type)
        return self._handlers[event_type]

    def _create_handler(self, event_type: EventType) -> BaseEventHandler:
        """创建事件处理器"""
        handler_map = {EventType.SLEEP: SleepHandler, EventType.KEYWORD_EXPANSION: KeywordExpansionHandler, EventType.MEMORY_FETCH: MemoryFetchHandler, EventType.USER_TRACKING: UserTrackingHandler, EventType.BROWSE_NEWS: BrowseNewsHandler, EventType.SELF_REFLECTION: SelfReflectionHandler, EventType.BROWSE_BOOKMARKS: BrowseBookmarksHandler, EventType.LISTEN_MUSIC: ListenMusicHandler}
        handler_class = handler_map.get(event_type)
        if event_type in (EventType.VISIT_WORKSHOP, EventType.WORKSHOP_CREATE, EventType.WORKSHOP_HISTORY, EventType.WORKSHOP_CONTINUE):
            from .workshop_handler import WorkshopVisitHandler
            return WorkshopVisitHandler()
        if event_type == EventType.BROWSE_XIAOHONGSHU:
            from .xiaohongshu_handler import BrowseXiaohongshuHandler
            return BrowseXiaohongshuHandler()
        if event_type == EventType.BROWSE_SOCIAL_FEED:
            from .social_feed_handler import BrowseSocialFeedHandler
            return BrowseSocialFeedHandler(call_llm_func=self.call_llm, on_notification=self.on_notification)
        if not handler_class:
            raise ValueError(f'未知事件类型: {event_type}')
        if event_type == EventType.MEMORY_FETCH:
            return handler_class(call_llm_func=self.call_llm, get_memories_func=self.get_memories)
        if event_type == EventType.USER_TRACKING:
            return handler_class(call_llm_func=self.call_llm, tracking_service=self.tracking_service, on_notification=self.on_notification)
        if event_type == EventType.BROWSE_NEWS:
            return handler_class(call_llm_func=self.call_llm, web_search_func=self.web_search_func)
        if event_type == EventType.LISTEN_MUSIC:
            return handler_class(call_llm_func=self.call_llm, song_cache=self.song_cache, k_self_book=self.k_self_book, pro_llm_func=self.call_llm, music_mcp_client=self.music_mcp_client, song_selection_llm_func=self.song_selection_llm)
        return handler_class(call_llm_func=self.call_llm)
