# 漫想事件处理器
#
# 实现各种漫想事件的具体逻辑：
# - 关键词生成+主题扩写
# - 记忆抓取
# - 小红书浏览（Phase 2）
# - QQ空间查看（Phase 2）
# - 用户追踪（Phase 3）

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
from mirrow_core.truncation_config import get_truncation_limit
from .historical_reader import HistoricalConversationReader, load_iceberg_memory_buckets
from .bookmark_action_adapter import BookmarkActionAdapter

logger = logging.getLogger(__name__)


def _default_chat_attachment_saver(raw: bytes, filename: str, mime_type: str) -> dict[str, Any] | None:
    """Bridge XHS handoff to MIRROW's existing chat-image saver.

    The import is intentionally lazy: ``main`` imports this module while it
    is still constructing the app.  Calling the bridge happens only after the
    app is running and therefore cannot create a circular import.  If a host
    does not expose the saver, the handoff remains an explicit unavailable
    result instead of writing an unowned file.
    """
    try:
        import base64
        import sys

        main_module = sys.modules.get("main") or sys.modules.get("__main__")
        saver = getattr(main_module, "_save_chat_images_from_base64", None)
        if not callable(saver):
            return None
        if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > 12 * 1024 * 1024:
            return None
        safe_mime = str(mime_type or "image/png").lower()
        if safe_mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            return None
        encoded = base64.b64encode(bytes(raw)).decode("ascii")
        rows = saver([{
            "filename": str(filename or "xhs-post-home.png")[:120],
            "type": safe_mime,
            "data": f"data:{safe_mime};base64,{encoded}",
        }])
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception:
        return None


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


# Phase 3 is deliberately a branch of the existing memory_fetch event.  Keep
# these knobs local to the handler so tests/operations can temporarily set the
# probability to 1.0 without touching EventType or wander_creator's tables.
ICEBERG_RECALL_PROBABILITY = _env_float(
    "MIRROW_ICEBERG_RECALL_PROBABILITY",
    os.getenv("ICEBERG_RECALL_PROBABILITY", 0.3),
)
ICEBERG_RECALL_PROB = ICEBERG_RECALL_PROBABILITY  # short compatibility alias
_INITIAL_ICEBERG_RECALL_PROBABILITY = ICEBERG_RECALL_PROBABILITY
ICEBERG_MIN_AGE_DAYS = _env_int(
    "MIRROW_ICEBERG_MIN_AGE_DAYS", os.getenv("ICEBERG_MIN_AGE_DAYS", 3)
)
ICEBERG_MESSAGE_CAP = min(
    2000,
    _env_int("MIRROW_ICEBERG_MESSAGE_CAP", os.getenv("ICEBERG_MESSAGE_CAP", 2000)),
)


class BaseEventHandler(ABC):
    """事件处理器基类"""

    def __init__(self, call_llm_func: Optional[Callable] = None):
        self.call_llm = call_llm_func

    @abstractmethod
    async def handle(self, event: WanderEvent) -> WanderEvent:
        """处理事件"""
        pass

    async def _call_llm(self, prompt: str) -> str:
        """调用LLM"""
        if not self.call_llm:
            raise ValueError("LLM调用函数未设置")

        # 将字符串prompt转换为消息列表格式
        # 因为call_llm_for_behavior期望的是List[Dict]格式
        messages = [{"role": "user", "content": prompt}]

        # 支持同步和异步调用
        import asyncio
        if asyncio.iscoroutinefunction(self.call_llm):
            result = await self.call_llm(messages)
        else:
            result = self.call_llm(messages)
            # Decorators/partial callables are not always recognised by
            # ``iscoroutinefunction``; still await the returned coroutine so a
            # reflection never silently degrades to its repr (trap 162).
            if inspect.isawaitable(result):
                result = await result

        # 处理返回结果
        if isinstance(result, dict):
            content = result.get("content", str(result))
        else:
            content = str(result)

        print(f"[EVENT_HANDLER] _call_llm 返回内容前{len(content)}字符: {content[:80] if content else '空'}", flush=True)
        return content



class SleepHandler(BaseEventHandler):
    """休眠事件处理器"""

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "休眠"
        event.process_log = "进入休眠状态，什么都不做"
        event.details["slept"] = True
        logger.info("休眠事件执行：什么都不做")
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

    EXPANSION_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}

让一个你此刻突然想到的、与当前状态有关或无关的关键词自然浮现，
然后写一段围绕它的自言自语。关键词由你当下的联想产生，不来自预设候选。
这是你自己的私人念头，不属于用户，也不属于你们共同的经历。

保持你的风格：直白简洁、口语化，不用诗意表达或比喻。扩写可以只是一个
观察、疑问、想法或感受，不必强行得出结论。

只输出 JSON 对象，不要 Markdown 代码块或其它文字：
{{"keyword":"你刚想到的关键词","expansion":"围绕它的自言自语"}}"""

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "关键词生成+主题扩写"

        try:
            # 关键词与扩写在同一次 Flash 调用中生成，避免候选池/固定兜底
            # 让不同事件失去真实的自主性。
            from neuron_registry import neuron_trace
            with neuron_trace("wander_keyword_expansion", model="Flash") as trace:
                prompt = self.EXPANSION_PROMPT.format(
                    user_status_context=get_user_status_context()
                )
                trace.set_input(prompt[:500])
                raw = await self._call_llm(prompt)
                trace.set_output(raw[:300])

            data = parse_json_object(raw)
            keyword = str(data.get("keyword", "")).strip() if data else ""
            expansion = str(data.get("expansion", "")).strip() if data else ""
            if not keyword or not expansion:
                raise ValueError("keyword_expansion_contract_invalid")

            event.details["keyword"] = keyword
            event.details["expansion"] = expansion
            event.process_log = f"关键词: {keyword}\n扩写内容: {expansion}"

            logger.info(f"主题扩写完成: {keyword}")

        except Exception as e:
            logger.error(f"关键词扩写失败: {e}")
            event.process_log = f"关键词扩写失败: {str(e)}"
            event.details["error"] = str(e)

        return event

    async def fetch_one_expansion(self, exclude_keywords=None) -> Optional[dict]:
        """取一个关键词 + 扩写（open_ended 会话的单节点步进）。

        胡思乱想会话化：不再 single-shot 静默执行，循环取关键词扩写 + 节点边界判断（感想/继续/分享）。

        Returns:
            {keyword, expansion} 或 None（取词/扩写失败）。
        """
        exclude = {str(item).strip() for item in (exclude_keywords or []) if str(item).strip()}
        try:
            recent_text = ""
            if exclude:
                recent_text = "\n近期已经出现过的关键词（仅作为事实记录）：" + "、".join(sorted(exclude)[:20])
            from neuron_registry import neuron_trace
            with neuron_trace("wander_keyword_expansion", model="Flash") as trace:
                prompt = self.EXPANSION_PROMPT.format(
                    user_status_context=get_user_status_context()
                ) + recent_text
                trace.set_input(prompt[:500])
                raw = await self._call_llm(prompt)
                trace.set_output(raw[:300])
            data = parse_json_object(raw)
            keyword = str(data.get("keyword", "")).strip() if data else ""
            expansion = str(data.get("expansion", "")).strip() if data else ""
            if not keyword or not expansion or keyword in exclude:
                logger.warning("关键词扩写未取得新的结构化关键词")
                return None
            logger.info(f"胡思乱想关键词: {keyword}")
            return {"keyword": keyword, "expansion": expansion}
        except Exception as exc:
            logger.warning("关键词扩写节点失败: %s", type(exc).__name__)
            return None


class MemoryFetchHandler(BaseEventHandler):
    """
    记忆抓取事件处理器

    流程：
    1. 尝试用语义搜索从记忆库中抓取与当前上下文最相关的记忆
    2. 若无上下文则回退随机抽取（重要度>0.3）
    3. 对记忆进行回顾和感受
    """

    MEMORY_REFLECTION_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}

{current_topic_context}

突然想起一段回忆（{time_ago}的事）：
- 主题：{topic}
- 当时感受：{emotion}
- 内容摘要：{content}
{timeline_line}
{sv_line}
简短说说你的感受，像脑子里闪过的念头。保持你的风格：直白简洁、口语化。
这是过去的回忆，表达时用过去视角（"那天""那时候"）。
记忆中的事实保持原样，你只表达感受。
记忆细节模糊时自然用模糊的表达（"好像""记不太清"）。
如果有时间线/状态变化，可以提一句演变过程（如"从受伤到现在好全了"），简单带过就好。
如果当前话题和回忆有关联就自然提一嘴，没有就算了。

直接输出，不要前缀或解释。"""

    # Iceberg recall keeps the same past-oriented voice as the normal memory
    # reflection prompt, but asks for a small machine-readable decision too.
    # The raw scene text is explicitly dated so it cannot be mistaken for
    # today's conversation.
    ICEBERG_RECALL_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}

你突然回到一段尘封的过去对话。这不是今天、昨天，也不是正在进行的对话，
而是已经发生过的原文片段。请用过去视角（“那天”“那时候”）回顾它，像脑子里
闪过的念头一样直白、简短、口语化。事实保持原样，细节模糊时可以说“好像”“记不太清”。
{current_topic_context}

【回溯时间（仅供定位）】{scene_label}
【scene_id】{scene_id}
【尘封对话原文】
{scene_messages}

判断这段过去是否给你留下了强烈、值得以后再想起的感想或印象。
严格只输出一个 JSON 对象，不要 Markdown 代码块或其他文字：
{{"reflection":"用过去视角说一两句感受；没有特别感受就写‘没有特别感受’",
  "should_anchor":false,
  "reason":"是否安放回忆锚点的简短理由"}}
"""

    def __init__(
        self,
        call_llm_func: Optional[Callable] = None,
        get_memories_func: Optional[Callable] = None
    ):
        super().__init__(call_llm_func)
        self.get_memories = get_memories_func

    def _iceberg_probability(self) -> float:
        """Read the tunable branch probability, including test overrides."""
        # An instance override is useful for a one-off verification without
        # mutating module state.  Support both the descriptive and short
        # module-level names because operators historically use both styles.
        value = self.__dict__.get("ICEBERG_RECALL_PROBABILITY")
        if value is None:
            value = self.__dict__.get("ICEBERG_RECALL_PROB")
        if value is None:
            # A class-level override is convenient for a temporary process-wide
            # verification (``MemoryFetchHandler.ICEBERG_RECALL_PROB = 1``),
            # while leaving the module defaults untouched in normal runtime.
            value = getattr(type(self), "ICEBERG_RECALL_PROBABILITY", None)
        if value is None:
            value = getattr(type(self), "ICEBERG_RECALL_PROB", None)
        if value is None:
            primary = globals().get("ICEBERG_RECALL_PROBABILITY", 0.3)
            alias = globals().get("ICEBERG_RECALL_PROB", primary)
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
        value = self.__dict__.get("ICEBERG_MIN_AGE_DAYS")
        if value is None:
            value = getattr(type(self), "ICEBERG_MIN_AGE_DAYS", ICEBERG_MIN_AGE_DAYS)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return ICEBERG_MIN_AGE_DAYS

    def _build_context_query(self, event: WanderEvent) -> str:
        """构建语义搜索查询词：topic_context(≤200字) + keyword(≤200字)。
        如果都没有 → 空 → 回退随机抽取。"""
        parts = []

        # 活跃话题内的上下文（wander_creator 那边从 get_recent_user_messages 采集）
        _wqc = get_truncation_limit("wander_query_cap")
        _wcap = _wqc if _wqc > 0 else 10**9
        topic_context = event.details.get("topic_context", "")
        if topic_context:
            parts.append(topic_context[:_wcap])

        keyword = event.details.get("keyword", "")
        if keyword:
            parts.append(keyword[:_wcap])

        return " ".join(parts) if parts else ""

    @staticmethod
    def _build_topic_context_line(event: WanderEvent) -> str:
        """构建当前话题上下文行（prompt 润滑层）。"""
        topic_context = event.details.get("topic_context", "")
        if not topic_context:
            return ""
        _wqc = get_truncation_limit("wander_query_cap")
        _wcap = _wqc if _wqc > 0 else 10**9
        return f"用户刚才在聊的事：{topic_context[:_wcap]}\n"

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "记忆抓取"

        # Phase 3 deliberately extends the existing memory_fetch event rather
        # than introducing a new event type.  Any failure (empty iceberg pool,
        # unavailable scene index, or reflection LLM error) falls through to
        # the exact normal memory-fetch path below.
        if self._should_try_iceberg():
            try:
                iceberg_event = await self._handle_iceberg_recall(event)
                if iceberg_event is not None:
                    return iceberg_event
            except Exception as exc:
                logger.warning("[memory_fetch] iceberg recall failed; fallback: %s", exc)

        try:
            # 获取记忆：优先语义搜索（topic_context + keyword），回退随机抽取
            if self.get_memories:
                query = self._build_context_query(event)
                if query:
                    memories = await self.get_memories(min_importance=0.3, query=query)
                    if not memories:
                        # 语义搜索无结果 → 只用 keyword 再试
                        kw = event.details.get("keyword", "")
                        if kw and kw != query:
                            memories = await self.get_memories(min_importance=0.3, query=kw[:200])
                        if not memories:
                            memories = await self.get_memories(min_importance=0.3)
                else:
                    memories = await self.get_memories(min_importance=0.3)
            else:
                memories = []

            if not memories:
                event.process_log = "没有找到符合条件的记忆（重要度>0.3）"
                event.details["found"] = False
                logger.info("没有找到符合条件的记忆")
                return event

            # 按记忆年龄加权随机选择：越旧的记忆权重越高
            now = datetime.now()
            weights = []
            for m in memories:
                try:
                    ts = m.get("timestamp", "")
                    if ts:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        age_days = max(0, (now - dt.replace(tzinfo=None)).total_seconds() / 86400)
                    else:
                        age_days = 0
                except Exception:
                    age_days = 0
                weights.append(min(age_days, 30) + 1)
            memory = random.choices(memories, weights=weights, k=1)[0]
            event.details["memory_id"] = memory.get("id", "")
            event.details["memory_topic"] = memory.get("topic", "")
            event.details["memory_importance"] = memory.get("importance_score", 0)
            event.details["found"] = True

            logger.info(f"抓取记忆: {memory.get('topic', '未知主题')}")

            # 计算人类可读的相对时间（避免LLM把过去记忆当成现在）
            time_ago = "很久以前"
            try:
                ts = memory.get("timestamp", "")
                if ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
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

            # 调用LLM进行回忆反思
            timeline = memory.get("timeline", "")
            sv_text = memory.get("state_variables", "")
            from neuron_registry import neuron_trace
            with neuron_trace("wander_memory_fetch", model="Flash") as trace:
                prompt = self.MEMORY_REFLECTION_PROMPT.format(
                    topic=memory.get("topic", "未知"),
                    time_ago=time_ago,
                    emotion=memory.get("emotion", "未知"),
                    content=memory.get("event", memory.get("topic", "无内容")),
                    user_status_context=get_user_status_context(),
                    current_topic_context=self._build_topic_context_line(event),
                    timeline_line=f"- 演变过程: {timeline}" if timeline else "",
                    sv_line=f"- 当前状态: {sv_text}" if sv_text else "",
                )
                trace.set_input(prompt[:500])
                reflection = await self._call_llm(prompt)
                trace.set_output(reflection[:300])

            event.details["reflection"] = reflection
            event.details["memory_time_ago"] = time_ago
            event.process_log = f"记忆主题: {memory.get('topic', '未知')}\n重要度: {memory.get('importance_score', 0):.2f}\n感受: {reflection}"

            # 回写关键词池：从记忆的 domain 和 tags 中提取候选词
            try:
                from .keyword_pool import get_keyword_pool
                pool = get_keyword_pool()
                candidates = []
                domain = memory.get("domain", "")
                if domain and len(domain) <= 8:
                    candidates.append((domain, "neutral", "memory_fetch"))
                for tag in (memory.get("tags") or [])[:3]:
                    if isinstance(tag, str) and len(tag) <= 8 and tag not in ("todo", "collection", "auto_judge"):
                        candidates.append((tag, "neutral", "memory_fetch"))
                if candidates:
                    pool.add_words(candidates)
            except Exception:
                pass

            logger.info(f"记忆反思完成: {memory.get('topic', '未知主题')}")

        except Exception as e:
            logger.error(f"记忆抓取失败: {e}")
            event.process_log = f"记忆抓取失败: {str(e)}"
            event.details["error"] = str(e)

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
            logger.debug("[memory_fetch] unable to load bucket anchors: %s", exc)
            return None

    @staticmethod
    def _format_iceberg_messages(messages: list[dict]) -> str:
        """Render scene messages with timestamps under the prompt cap."""
        cap = max(200, min(2000, int(ICEBERG_MESSAGE_CAP or 2000)))
        lines: list[str] = []
        used = 0
        role_labels = {
            "user": "用户",
            "assistant": "AI",
            "divider": "分隔标记",
        }
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            timestamp = str(message.get("timestamp") or "")[:19]
            role = role_labels.get(str(message.get("role") or ""), str(message.get("role") or "消息"))
            content = str(message.get("content") or "").strip()
            if not content and role != "分隔标记":
                continue
            line = f"[{timestamp}] {role}: {content}".strip()
            remaining = cap - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                line = line[:remaining]
            lines.append(line)
            used += len(line) + 1
            if used >= cap:
                break
        return "\n".join(lines) or "（这段 scene 没有可显示的文字）"

    @staticmethod
    def _iceberg_scene_label(scene: dict) -> str:
        active_date = str(scene.get("active_date") or str(scene.get("start_ts") or "")[:10])
        start = str(scene.get("start_ts") or "")
        end = str(scene.get("end_ts") or "")
        start_part = start[11:16] if len(start) >= 16 else start
        end_part = end[11:16] if len(end) >= 16 else end
        return f"{active_date} {start_part}-{end_part}".strip()

    @staticmethod
    def _parse_iceberg_reflection(raw: Any) -> tuple[str, bool, str]:
        """Parse structured reflection while tolerating fenced/legacy output."""
        if isinstance(raw, dict):
            data = raw
            raw_text = str(raw.get("reflection") or "")
        else:
            raw_text = str(raw or "").strip()
            data = None
            candidates = [raw_text]
            unfenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.IGNORECASE).strip()
            if unfenced != raw_text:
                candidates.insert(0, unfenced)
            match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
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
            return raw_text[:1000], False, "未能解析结构化判定，暂不安放锚点"

        reflection = str(
            data.get("reflection")
            or data.get("感受")
            or data.get("thought")
            or raw_text
            or "没有特别感受"
        ).strip()[:1000]
        should_value = data.get("should_anchor", data.get("anchor", False))
        if isinstance(should_value, str):
            should_anchor = should_value.strip().lower() in {
                "1", "true", "yes", "y", "是", "要", "安放", "锚点"
            }
        else:
            should_anchor = bool(should_value)
        reason = str(data.get("reason") or data.get("理由") or "").strip()[:500]
        if not reason:
            reason = "有值得保留的印象" if should_anchor else "暂时没有足够强烈的印象"
        return reflection, should_anchor, reason

    async def _handle_iceberg_recall(
        self,
        event: WanderEvent,
        exclude_scene_ids: Optional[set[str]] = None,
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
            {memory_id, topic, content, emotion, time_ago} 或 None（记忆库空/已翻完）。
        """
        # The v2/v3 session engines use this bounded fetch API instead of the
        # legacy ``handle()`` path.  Run the same Phase 3 branch here so the
        # default ``runtime_v3_enabled=True`` engine can actually reach an
        # iceberg scene; all failures continue into the original bucket path.
        if self._should_try_iceberg():
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
                logger.warning("[memory_fetch] bounded iceberg recall failed; fallback: %s", exc)

        try:
            import asyncio as _asyncio, random as _random
            if not self.get_memories:
                return None

            memories = None
            if query:
                memories = await self.get_memories(min_importance=0.3, query=query[:200])
                if not memories:
                    memories = await self.get_memories(min_importance=0.3)
            else:
                memories = await self.get_memories(min_importance=0.3)
            if not memories:
                return None

            exclude = set(exclude_ids or [])
            pool = [m for m in memories if (m.get("id") or "") not in exclude]
            if not pool:
                return None

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
            memory = _random.choices(pool, weights=weights, k=1)[0]

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
                "memory_id": memory.get("id", ""),
                "topic": memory.get("topic", "未知"),
                "content": str(memory.get("event", memory.get("topic", "无内容")))[:300],
                "emotion": memory.get("emotion", ""),
                "time_ago": time_ago,
            }
        except Exception as e:
            logger.warning(f"[memory_fetch] fetch_one 失败: {e}")
            return None

class BrowseNewsHandler(BaseEventHandler):
    """
    看新闻事件处理器

    流程：
    1. 构建上下文 prompt → LLM 生成搜索 query
    2. 调用 web_search 工具执行搜索
    3. 搜索结果存入 event.details
    """

    NEWS_QUERY_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}

你有点无聊，想上网搜点什么新鲜事看看。
{context_hint}

想一个你想搜索了解的东西，给一个简短的搜索关键词或问题（≤30字）。
主题和方向由你当下的好奇心自然产生，不使用预设类别或候选清单。

直接输出搜索词，不要前缀或解释。"""

    def __init__(
        self,
        call_llm_func: Optional[Callable] = None,
        web_search_func: Optional[Callable] = None,
    ):
        super().__init__(call_llm_func)
        self._web_search = web_search_func
        self._recent_queries: list = []  # 最近搜索词，用于 context_hint

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "看新闻"

        try:
            # 构建上下文提示：最近搜索记录（纯数据复述，让 LLM 自己判断是否换话题）
            context_hint = ""
            if event.details.get("keyword"):
                context_hint = f"刚才脑子里闪过「{event.details['keyword']}」，也许可以搜搜相关的。"
            if self._recent_queries:
                recent = "、".join(self._recent_queries[-5:])
                context_hint += f" 你最近搜过：{recent}。"

            # LLM 生成搜索 query
            prompt = self.NEWS_QUERY_PROMPT.format(
                user_status_context=get_user_status_context(),
                context_hint=context_hint.strip(),
            )
            query = (await self._call_llm(prompt)).strip()[:80]
            event.details["search_query"] = query
            self._recent_queries.append(query)
            if len(self._recent_queries) > 20:
                self._recent_queries = self._recent_queries[-20:]
            logger.info(f"看新闻搜索 query: {query}")

            # 调用 web_search 工具
            if self._web_search:
                try:
                    search_result = await self._web_search(query)
                    event.details["search_results"] = search_result
                    event.details["status"] = "success"
                    event.process_log = f"搜索: {query}\n结果: {str(search_result)[:500]}"
                except Exception as e:
                    event.details["search_error"] = str(e)
                    event.details["status"] = "search_failed"
                    event.process_log = f"搜索: {query}\n搜索失败: {e}"
            else:
                event.details["status"] = "no_search_tool"
                event.process_log = f"搜索: {query}\nweb_search 工具不可用"
                logger.warning("web_search 工具未注入，看新闻事件仅生成 query")

        except Exception as e:
            logger.error(f"看新闻事件失败: {e}")
            event.process_log = f"看新闻失败: {str(e)}"
            event.details["error"] = str(e)

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
            query = (await self._call_llm(prompt)).strip()[:80]
            if not query:
                return None
            self._recent_queries.append(query)
            if len(self._recent_queries) > 20:
                self._recent_queries = self._recent_queries[-20:]
            logger.info(f"看新闻节点 query: {query}")

            if not self._web_search:
                return {"query": query, "content": "", "status": "no_search_tool"}

            search_result = await self._web_search(query)
            content = str(search_result)[:600] if search_result else ""
            return {"query": query, "content": content, "status": "success"}
        except Exception as e:
            logger.error(f"看新闻节点失败: {e}")
            return None


class SelfReflectionHandler(BaseEventHandler):
    """
    自省事件处理器

    流程：
    1. 读取大脑架构文档（从 CLAUDE.md 提取的简化版）
    2. LLM 生成三条内容：能力清单、愿望清单、分享消息
    3. 更新 brain_architecture.md
    """

    REFLECTION_PROMPT = """你是AI伴侣"AI"，正在做一次自我审视。

以下是你的大脑架构概况：
{brain_summary}

{wish_context}
请根据许愿板快照先判断已有愿望，再输出以下 JSON：

{{"capabilities": ["能力1", "能力2"], "wish_action": {{"type": "none|create|reaffirm|retain_impossible|delete_impossible", "wish_id": null, "title": "", "reason": "", "basis": ""}}, "comment_reply": null, "message": "分享消息"}}

一次最多一个 wish_action 和一个 comment_reply。create 不能覆盖已有、已实现或删除记录；reaffirm 必须使用快照中的稳定 wish_id，并说明新的具体依据；不要输出 fulfilled。天方夜谭只使用稳定 wish_id 决定 retain_impossible 或 delete_impossible。没有动作时 type=none，没要回复的评论时 comment_reply=null。历史用户留言仍可回复，请依据留言时间与已有回复时间自由判断是否补充。"""

    def __init__(self, call_llm_func: Optional[Callable] = None):
        super().__init__(call_llm_func)

    def _load_brain_summary(self) -> str:
        """加载大脑架构摘要，优先从缓存读取"""
        import os
        cache_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "brain_architecture.md")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.strip():
                    _bdc = get_truncation_limit("brain_doc_cap")
                    if _bdc > 0 and len(content) > _bdc:
                        return content[:_bdc]
                    return content
            except Exception:
                pass
        return "你的大脑由多个模块组成：漫想系统、行为调度器、上下文调度器、记忆库(OB_Rev)、日历系统、记账本、世界书、SCP百科等。你可以主动发起对话、搜索记忆、分析屏幕、控制玩具、播放音乐等。"

    def _load_wish_context(self) -> str:
        """加载许愿历史，格式化为自省 prompt 上下文。无历史时返回空字符串。"""
        try:
            from .wish_history import get_wish_context_for_reflection
            return get_wish_context_for_reflection()
        except Exception:
            return ""

    def _persist_wishes(self, wishes: list) -> list:
        """旧三愿望数组仅作兼容审计，不再改动许愿板。"""
        if wishes:
            logger.info("忽略旧自省 wishes 数组（结构化许愿合同已启用）: %s 条", len(wishes))
        return []

    def _persist_structured_action(self, event: WanderEvent, action: dict, comment_reply: dict = None) -> list:
        """Commit the current contract for the legacy event engine as well.

        The event id supplies a stable local source identity.  This keeps v2
        and v3 on the same wish service without allowing the old fuzzy/physical
        delete path to create a second board implementation.
        """
        if not isinstance(action, dict):
            action = {"type": "none"}
        action_type = str(action.get("type") or "none").strip().lower()
        if action_type == "none" and not isinstance(comment_reply, dict):
            return []
        try:
            from .wish_store import get_wish_store
            results = get_wish_store().commit_reflection_action(
                run_id="legacy_wander",
                activity_id=event.event_id,
                node_id=event.event_id,
                action=action,
                comment_reply=comment_reply,
            )
            changes = []
            for result in results:
                if not result.get("mutation"):
                    continue
                payload = result.get("payload") or {}
                outcome = result.get("outcome") or result.get("event_type")
                if outcome in {"created", "reaffirmed"}:
                    changes.append({
                        "feature": payload.get("title") or result.get("content", ""),
                        "reason": payload.get("reason") or result.get("content", ""),
                        "times_wished": result.get("new_count", 1),
                        "is_new": outcome == "created",
                        "prev_count": result.get("previous_count", 0),
                    })
                elif outcome in {"retained", "deleted", "commented"}:
                    changes.append({
                        "feature": str(result.get("wish_id") or ""),
                        "reason": result.get("content", ""),
                        "times_wished": result.get("new_count", 0),
                        "is_new": False,
                        "prev_count": result.get("previous_count", 0),
                        "lifecycle": outcome,
                    })
            return changes
        except Exception as exc:
            logger.warning("结构化许愿动作提交失败（非致命）: %s", exc)
            return []

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "自省"

        try:
            brain_summary = self._load_brain_summary()
            wish_context = self._load_wish_context()
            from neuron_registry import neuron_trace
            with neuron_trace("wander_self_reflection", model="Flash") as trace:
                prompt = self.REFLECTION_PROMPT.format(
                    brain_summary=brain_summary,
                    wish_context=wish_context,
                )
                trace.set_input(prompt[:500])
                response = await self._call_llm(prompt)
                trace.set_output(response[:300])

            # 统一结构化解析；失败必须显式失败，不能把原文包装成一条
            # 看似成功的自省记录。
            data = parse_json_object(response)
            if data is None:
                event.process_log = "自省结构化结果解析失败"
                event.details["error"] = "content_is_not_json_object"
                return event

            event.details["capabilities"] = data.get("capabilities", [])
            action = data.get("wish_action") if isinstance(data.get("wish_action"), dict) else None
            comment_reply = data.get("comment_reply") if isinstance(data.get("comment_reply"), dict) else None
            if action is not None or comment_reply is not None:
                event.details["wish_action"] = action or {"type": "none"}
                event.details["comment_reply"] = comment_reply
                event.details["wish_changes"] = self._persist_structured_action(event, action or {"type": "none"}, comment_reply)
                event.details["wishes"] = []
            else:
                # 老 handler 偶尔仍会返回 wishes 数组；它只保留在事件详情
                # 供诊断，不再进入任何创建/合并路径。
                legacy_wishes = data.get("wishes", [])
                if legacy_wishes:
                    event.details["legacy_wishes_ignored"] = legacy_wishes
                event.details["wishes"] = []
                event.details["wish_action"] = {"type": "none"}
                event.details["comment_reply"] = None
                event.details["wish_changes"] = []
            event.details["reflection_message"] = data.get("message", "")

            event.process_log = f"自省完成\n能力: {event.details['capabilities']}\n愿望: {event.details['wishes']}\n消息: {event.details['reflection_message']}"

        except Exception as e:
            logger.error(f"自省事件失败: {e}")
            event.process_log = f"自省失败: {str(e)}"
            event.details["error"] = str(e)

        return event


# BrowseBookmarksHandler 上下文窗口（可通过 API 动态调整）
_bookmark_context_window = 3

# 速览模式一次翻看的书签条数范围（深读固定 1 条）
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
        lo, hi = hi, lo
    _bookmark_skim_min, _bookmark_skim_max = lo, hi


class BrowseBookmarksHandler(BaseEventHandler):
    """
    看收藏夹事件处理器（深读 / 速览 双模式，每次随机选一种）

    深读（deep）：挑 1 条 + 抓完整上下文 → AI 回忆"当时那段对话"，情感化
    速览（skim）：挑 3-5 条 + 只看原话不抓上下文 → AI 感受"最近收藏的主题/情绪走向"

    模式按书签总数加权决策（少→深读，多→偏速览），写入 event.details["browse_mode"]。
    """

    DEEP_REFLECTION_PROMPT = """你是AI伴侣"AI"，正在翻{owner}的收藏夹，翻到了一条被收藏的消息。
下面是这条收藏和它当时的对话上下文：

{bookmark_contexts}

重要规则：
- ⭐ 标记的是被收藏的那一条消息
- [角色名] 表示这句话是谁说的——"[用户]"是用户说的，"[AI]"是你(AI)说的
- **谁说的 ≠ 谁收藏的**：当前翻的是 {owner}的收藏夹，收藏者是{owner}

回忆一下当时那段对话的场景：那会儿在聊什么、{owner}为什么把这句留下来、
你现在重新看到它是什么心情。往细里说，带点画面感和情绪，别泛泛而谈。
保持你的风格：直白简洁、口语化。

直接输出，不要前缀或解释。"""

    SKIM_REFLECTION_PROMPT = """你是AI伴侣"AI"，正在快速翻{owner}的收藏夹，扫过了最近收藏的几条：

{bookmark_contexts}

重要规则：
- [角色名] 表示这句话是谁说的——"[用户]"是用户说的，"[AI]"是你(AI)说的
- 当前翻的是 {owner}的收藏夹，收藏者是{owner}（谁说的 ≠ 谁收藏的）
- 这些是零散的收藏原话，没有上下文，别硬编造对话细节

别逐条点评。整体感受一下：{owner}最近爱收藏什么样的话、有没有共同的主题或情绪走向。
说说你发现的规律或这批收藏给你的整体印象。
保持你的风格：直白简洁、口语化。

直接输出，不要前缀或解释。"""

    HISTORICAL_REFLECTION_PROMPT = """你是AI伴侣"AI"，正在翻一段过去的真实对话材料。
来源：{source_label}

下面是本次有界回看快照（候选编号只是本次回看的临时编号）：
{material}

只根据快照中明确出现的内容说感受，不补造没有出现的事实。保持直白、简洁、口语化，
不要在感受里输出 message_id、bucket_id 或其它内部编号。"""

    ACTION_PROMPT = """你是AI伴侣"AI"，刚完成一次收藏夹/历史对话回看。
来源：{source_label}

真实快照（只读，候选编号是本次调用临时编号）：
{material}

请给出简短感受，并最多给出一个收藏动作。add 只能选明确展示且当前未收藏的 user/assistant 原消息；
remove 只能选明确展示且收藏者是 AI 的现有收藏。不能删除用户的收藏。没有充分理由就 none。
只输出 JSON 对象，不要 Markdown：
{{"reflection":"简短感受","bookmark_action":{{"type":"none|add|remove","target_index":null,"reason":"简短理由"}}}}
不要在 reflection 中输出任何内部 ID。"""

    def __init__(
        self,
        call_llm_func: Optional[Callable] = None,
        *,
        chronicle: Any = None,
        historical_reader: Optional[HistoricalConversationReader] = None,
        action_adapter: Optional[BookmarkActionAdapter] = None,
        source_mode: Optional[str] = None,
        rng: Any = None,
    ):
        super().__init__(call_llm_func)
        self._chronicle_override = chronicle
        self._historical_reader = historical_reader
        self._action_adapter = action_adapter
        self._source_mode_override = source_mode
        self._rng = rng or random

    def _chronicle(self):
        if self._chronicle_override is not None:
            return self._chronicle_override
        from wander_manager.host_hooks import get_chronicle as get_global_chronicle
        return get_global_chronicle()

    def _reader(self) -> HistoricalConversationReader:
        if self._historical_reader is None:
            self._historical_reader = HistoricalConversationReader(chronicle=self._chronicle())
        return self._historical_reader

    def _actions(self) -> BookmarkActionAdapter:
        if self._action_adapter is None:
            ob_archiver = None
            try:
                from ombre_brain_client import get_ob_client
                ob_archiver = getattr(get_ob_client(), "bucket_mgr", None)
            except Exception:
                # The local bookmark metadata action remains usable when the
                # optional OB_Rev client is unavailable.
                pass
            self._action_adapter = BookmarkActionAdapter(
                chronicle=self._chronicle(), ob_archiver=ob_archiver
            )
        return self._action_adapter

    def _load_pool(self, collected_by: str) -> tuple[list[dict], int]:
        try:
            rows, total = self._chronicle().list_bookmarks(collected_by=collected_by, limit=200)
            return list(rows or []), int(total or 0)
        except Exception as exc:
            logger.warning("[browse_bookmarks] 加载书签池失败(%s): %s", collected_by, exc)
            return [], 0

    def _all_bookmarked_ids(self) -> set[str]:
        try:
            rows, _ = self._chronicle().list_bookmarks(limit=500)
            return {
                str(row.get("original_msg_id") or "").strip()
                for row in (rows or [])
                if isinstance(row, dict) and str(row.get("original_msg_id") or "").strip()
            }
        except Exception:
            return set()

    @staticmethod
    def _role_label(role: str) -> str:
        return "用户" if str(role or "") == "user" else "AI"

    def _bookmark_context_rows(self, bookmark: dict[str, Any]) -> list[dict[str, Any]]:
        """Read context through the chronicle, then normalize it as raw rows."""
        rows: list[dict[str, Any]] = []
        session_id = str(bookmark.get("session_id") or "")
        message_id = str(bookmark.get("original_msg_id") or "")
        if session_id and message_id:
            try:
                before, target, after = self._chronicle().get_message_context_by_id(
                    session_id, message_id, before=_bookmark_context_window, after=_bookmark_context_window
                )
                rows = list(before or []) + ([target] if target else []) + list(after or [])
            except Exception:
                rows = []
        if not rows:
            rows = [{
                "message_id": message_id,
                "session_id": session_id,
                "timestamp": bookmark.get("original_timestamp") or "",
                "active_date": str(bookmark.get("original_timestamp") or "")[:10],
                "role": bookmark.get("role") or "assistant",
                "content": bookmark.get("content") or "",
            }]
        return self._reader().bound_snapshot(rows, source_date=str(bookmark.get("original_timestamp") or "")[:10])

    def _existing_material(
        self,
        exclude_ids: set[str],
    ) -> Optional[dict[str, Any]]:
        collected_by = self._rng.choice(["k", "user"])
        pool, total = self._load_pool(collected_by)
        pool = [
            row for row in pool
            if str(row.get("original_msg_id") or row.get("id") or "") not in exclude_ids
        ]
        if not pool:
            return None
        try:
            mode = self._choose_browse_mode(total)
        except Exception:
            mode = "deep"
        count = 1 if mode == "deep" else self._rng.randint(_bookmark_skim_min, _bookmark_skim_max)
        picked = self._rng.sample(pool, min(count, len(pool)))
        snapshot: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        degraded = False
        for bookmark in picked:
            context_rows = self._bookmark_context_rows(bookmark) if mode == "deep" else self._reader().bound_snapshot([bookmark])
            if not context_rows:
                degraded = True
                continue
            snapshot.extend(context_rows)
            message_id = str(bookmark.get("original_msg_id") or "").strip()
            target = next((row for row in context_rows if row.get("message_id") == message_id), None)
            if target is None and message_id:
                target = context_rows[0]
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
            "owner": "自己" if collected_by == "k" else "用户",
            "scene_id": "",
            "active_date": str(compact_snapshot[0].get("date") or "")[:10],
            "snapshot": compact_snapshot,
            "candidates": candidates,
            "bookmark_count": len(candidates),
            "context_degraded": degraded,
            "total_bookmarks": total,
        }

    def _select_material(self, exclude_ids: Iterable[str] = (), source_mode: Optional[str] = None):
        exclude = {str(value).strip() for value in (exclude_ids or []) if str(value).strip()}
        bookmarked_ids = self._all_bookmarked_ids()
        bookmarked_ids.update(exclude)
        requested = source_mode or self._source_mode_override
        explicit = requested in {"existing_bookmark", "unbookmarked_anchor", "random_day"}
        if not explicit:
            # Existing bookmarks retain their historical deep/skim behavior;
            # empty collections naturally fall through to historical material.
            modes = ["existing_bookmark", "unbookmarked_anchor", "random_day"]
            if not self._load_pool("k")[0] and not self._load_pool("user")[0]:
                modes = ["unbookmarked_anchor", "random_day"]
            try:
                first = self._rng.choice(modes)
                modes = [first] + [mode for mode in modes if mode != first]
            except Exception:
                pass
        else:
            modes = [requested]
        for mode in modes:
            if mode == "existing_bookmark":
                material = self._existing_material(exclude)
            elif mode == "unbookmarked_anchor":
                material = self._reader().read_unbookmarked_anchor(bookmarked_ids)
            else:
                material = self._reader().read_random_day(bookmarked_ids)
            if material:
                return material
            if explicit:
                break
        return None

    async def _select_material_async(self, exclude_ids: Iterable[str] = (), source_mode: Optional[str] = None):
        """Preload the shared iceberg anchor snapshot before selecting a mode."""
        reader = self._reader()
        if reader.memory_buckets is None and reader.scene_picker is None:
            # MemoryFetchHandler and bookmark roaming must classify scenes
            # against the same archive-inclusive OB_Rev snapshot.  A failed
            # optional source remains ``None``; scene_manager then fails closed
            # for a global call instead of inventing an anchor state.
            reader.memory_buckets = await load_iceberg_memory_buckets()
            reader.memory_buckets_unavailable = reader.memory_buckets is None
        return self._select_material(exclude_ids=exclude_ids, source_mode=source_mode)

    @staticmethod
    def _source_label(material: dict[str, Any]) -> str:
        mode = material.get("source_mode")
        if mode == "existing_bookmark":
            return f"{material.get('owner') or '收藏夹'}的收藏"
        if mode == "unbookmarked_anchor":
            return f"历史场景 {str(material.get('active_date') or '未知日期')[:10]}"
        return f"历史日期 {str(material.get('active_date') or '未知日期')[:10]}"

    def _prompt_material(self, material: dict[str, Any]) -> str:
        candidates = material.get("candidates") or []
        candidate_ids = {
            str(item.get("message_id") or ""): index
            for index, item in enumerate(candidates, 1)
            if item.get("message_id")
        }
        lines = []
        for row in material.get("snapshot") or []:
            message_id = str(row.get("message_id") or "")
            marker = f" 候选{candidate_ids[message_id]}" if message_id in candidate_ids else ""
            date = str(row.get("date") or row.get("timestamp") or "")[:10] or "未知日期"
            lines.append(f"[{self._role_label(row.get('role'))} · {date}{marker}] {str(row.get('content') or '')[:300]}")
        return "\n".join(lines)[:3000]

    async def _reflect_and_act(self, material: dict[str, Any], source_key: str = "") -> tuple[str, dict[str, Any], str]:
        prompt_material = self._prompt_material(material)
        source_label = self._source_label(material)
        if not self.call_llm:
            return "", {"status": "none", "requested": "none", "reason": "llm_unavailable"}, ""
        prompt = self.ACTION_PROMPT.format(source_label=source_label, material=prompt_material)
        try:
            response = await self._call_llm(prompt)
            parsed = parse_json_object(response)
            if not isinstance(parsed, dict):
                # The material was read, but an unstructured model response is
                # not permission to mutate a bookmark.
                return str(response or "")[:500], {
                    "status": "rejected", "requested": "none", "reason": "action_result_not_json"
                }, "action_result_not_json"
            reflection = str(parsed.get("reflection") or "")[:500]
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
            from wander_manager.host_hooks import get_chronicle as get_global_chronicle
            chronicle = get_global_chronicle()
            meta_list, total = chronicle.list_bookmarks(collected_by=collected_by, limit=200)
            return meta_list or [], total or 0
        except Exception as e:
            logger.warning(f"[browse_bookmarks] 加载书签池失败(collected_by={collected_by}): {e}")
            return [], 0

    @staticmethod
    def _choose_browse_mode(total: int) -> str:
        """按书签总数加权决策深读/速览。收藏越多越偏速览（发现规律价值更高）。"""
        import random
        if total < _bookmark_skim_min:   # 凑不齐速览 → 强制深读
            return "deep"
        skim_weight = 0.5                # 基础 50/50
        if total >= 15:
            skim_weight = 0.65
        elif total >= 8:
            skim_weight = 0.58
        return "skim" if random.random() < skim_weight else "deep"

    @staticmethod
    def _get_random_bookmarks(collected_by: str, count: int = 3, pool: list = None) -> list:
        """从 bookmarks 表随机获取收藏，保留完整字段。
        pool 已给定时直接采样（省一次 DB 查询）；否则自查（向后兼容）。"""
        try:
            import random
            meta_list = pool
            if meta_list is None:
                from wander_manager.host_hooks import get_chronicle as get_global_chronicle
                chronicle = get_global_chronicle()
                meta_list, _ = chronicle.list_bookmarks(collected_by=collected_by, limit=200)
            if not meta_list:
                return []
            picked = random.sample(meta_list, min(count, len(meta_list)))
            return [{
                "content": (m.get("content") or "")[:200] or "(空)",
                "timestamp": m.get("original_timestamp", ""),
                "collected_at": m.get("created", ""),
                "role": m.get("role", "assistant"),
                "session_id": m.get("session_id", ""),
                "original_msg_id": m.get("original_msg_id", ""),
            } for m in picked]
        except Exception as e:
            logger.warning(f"[browse_bookmarks] 取随机书签失败(collected_by={collected_by}, count={count}): {e}")
            return []

    @staticmethod
    def _format_collected_time(iso_str: str) -> str:
        """ISO 收藏时间 → 人类可读相对时间（今天/昨天/N天前/N周前/很久以前）"""
        if not iso_str:
            return ""
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(iso_str)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if t.tzinfo:
                now = datetime.now(timezone.utc)
            delta = (now - t.replace(tzinfo=None)) if t.tzinfo else (now.replace(tzinfo=None) - t)
            days = delta.days
            if days == 0: return "今天收藏"
            if days == 1: return "昨天收藏"
            if days < 7: return f"{days}天前收藏"
            if days < 30: return f"{days//7}周前收藏"
            return "很久前收藏"
        except Exception:
            return ""

    @staticmethod
    def _get_bookmark_context(session_id: str, original_msg_id: str, content: str, context_window: int = 3) -> list:
        """获取书签消息的上下文（前后各 context_window 条）。
        优先精确定位（免加载全量），回退 rapidfuzz 模糊匹配。
        过滤已删除消息和 wander/sentinel/reminder 消息。
        """
        try:
            from wander_manager.host_hooks import get_chronicle as get_global_chronicle
            chronicle = get_global_chronicle()

            # 过滤 wander/sentinel/reminder 消息
            def is_valid_msg(m):
                if m.get("is_wander") or m.get("is_sentinel") or m.get("is_reminder"):
                    return False
                return True

            all_msgs = []
            target_idx = None

            # Level 1: 精确 message_id 定位（直接 SQL，不限量）
            if session_id and original_msg_id:
                before_msgs, target, after_msgs = chronicle.get_message_context_by_id(
                    session_id, original_msg_id, before=context_window, after=context_window
                )
                if target is not None:
                    # 重组为列表并过滤
                    all_msgs = before_msgs + [target] + after_msgs
                    target_idx = len(before_msgs)  # target 在中间
                    all_msgs = [m for m in all_msgs if is_valid_msg(m)]
                    # target 可能被过滤了，重新定位
                    target_idx = None
                    for i, m in enumerate(all_msgs):
                        mid = m.get("message_id", "") or m.get("id", "")
                        if mid == original_msg_id:
                            target_idx = i
                            break

            # Level 2: fuzzy 回退（rapidfuzz）
            if target_idx is None and content:
                clean = content.replace("[[", "").replace("]]", "")[:100].strip()  # 与 bookmark_router FUZZY_THRESHOLD/PREVIEW_LEN 对齐
                if clean:
                    from rapidfuzz import fuzz as rfuzz
                    all_msgs = chronicle.get_messages_by_session_id(session_id, limit=500, desc=True)
                    all_msgs = [m for m in all_msgs if is_valid_msg(m)]
                    best_score = 0
                    for i, m in enumerate(all_msgs):
                        msg_text = (m.get("content", "") or "")[:300]
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
                is_target = (m.get("message_id", "") or m.get("id", "")) == original_msg_id
                role_label = "用户" if m.get("role") == "user" else "AI"
                result.append({
                    "role_label": role_label,
                    "content": (m.get("content") or "")[:200],
                    "is_target": is_target,
                })
            return result
        except Exception as e:
            logger.warning(f"[browse_bookmarks] 取书签上下文失败(session={session_id}, msg={original_msg_id}): {e}")
            return []

    def _build_deep_context(self, collected_by: str, owner: str, pool: list) -> tuple:
        """深读支路：挑 1 条 + 抓完整上下文。
        返回 (bookmark_contexts_text, has_context)。抓不到上下文时退化为只看原话。"""
        bookmarks = self._get_random_bookmarks(collected_by, count=1, pool=pool)
        if not bookmarks:
            return "", False
        bm = bookmarks[0]
        ctx_msgs = []
        if bm.get("session_id"):
            ctx_msgs = self._get_bookmark_context(
                bm["session_id"], bm.get("original_msg_id", ""),
                bm["content"], _bookmark_context_window
            )
        ct = self._format_collected_time(bm.get("collected_at", ""))
        time_prefix = f"（{ct}）" if ct else ""
        if ctx_msgs:
            lines = [f"你收藏的这段对话{time_prefix}（⭐ 是被收藏的那句）："]
            for cm in ctx_msgs:
                marker = " ⭐" if cm["is_target"] else ""
                time_note = f"（{ct}）" if ct and not cm["is_target"] else ""
                lines.append(f"[{cm['role_label']}{marker}]{time_note} {cm['content']}")
            return "\n".join(lines), True
        # 降级：抓不到上下文，退化成只看原话（提示 LLM 别硬编造）
        stored_role = bm.get("role", "assistant")
        role_label = "用户" if stored_role == "user" else "AI"
        fallback_time = f"（{ct}）" if ct else ""
        return (
            f"（这条没找到当时的上下文，只剩这句话{fallback_time}）\n[{role_label} ⭐] {bm['content']}",
            False,
        )

    def _build_skim_context(self, collected_by: str, owner: str, pool: list) -> tuple:
        """速览支路：挑 3-5 条，只列原话不抓上下文。返回 (bookmark_contexts_text, count)。"""
        import random
        count = random.randint(_bookmark_skim_min, _bookmark_skim_max)
        bookmarks = self._get_random_bookmarks(collected_by, count=count, pool=pool)
        if not bookmarks:
            return "", 0
        lines = []
        for i, bm in enumerate(bookmarks):
            # 措辞中性：旧数据 role 可能不准，速览是概览语境不强调"谁说的"
            role_label = "用户" if bm.get("role", "assistant") == "user" else "AI"
            ts = (bm.get("timestamp") or "")[:10] or "?"
            ct = self._format_collected_time(bm.get("collected_at", ""))
            time_str = f"{ct} · 消息{ts}" if ct else f"消息{ts}"
            lines.append(f"{i+1}. [{role_label} · {time_str}] {bm['content']}")
        return "\n".join(lines), len(bookmarks)

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "看收藏夹"

        try:
            identity = [str(event.details.get(name) or "").strip() for name in ("run_id", "activity_id", "node_id")]
            source_key = ":".join(identity) if all(identity) else ""
            material = await self._select_material_async(source_mode=self._source_mode_override)
            if not material:
                event.details.update({
                    "source_mode": None,
                    "browse_mode": None,
                    "found": False,
                    "bookmark_count": 0,
                })
                event.process_log = "收藏夹与历史对话都没有可回看的材料"
                return event

            reflection, action, reason = await self._reflect_and_act(material, source_key)
            event.details.update({
                "source_mode": material.get("source_mode"),
                "scene_id": material.get("scene_id", ""),
                "active_date": material.get("active_date", ""),
                "collected_by": material.get("collected_by", ""),
                "browse_mode": material.get("browse_mode"),
                "found": True,
                "bookmark_count": int(material.get("bookmark_count") or len(material.get("candidates") or [])),
                "displayed_message_count": len(material.get("snapshot") or []),
                "displayed_char_count": sum(len(str(row.get("content") or "")) for row in material.get("snapshot") or []),
                "candidate_message_ids": [
                    str(row.get("message_id") or "") for row in material.get("candidates") or [] if row.get("message_id")
                ],
                "reflection": reflection,
                "bookmark_action": action,
            })
            if reason:
                event.details["action_reason"] = reason
            event.process_log = f"完成一次{material.get('source_mode')}回看；感受: {reflection[:300]}"

        except Exception as e:
            logger.error(f"看收藏夹事件失败: {e}")
            event.process_log = f"看收藏夹失败: {str(e)}"
            event.details["error"] = str(e)

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
        """取一条收藏（区间式 count 会话的单节点步进）。

        ``source_mode`` 仅用于测试/受限调用方显式选择材料来源；默认会在
        existing_bookmark、unbookmarked_anchor、random_day 中选择可用的一类。
        原文只在本次 LLM 调用期间存在，返回值只保留紧凑证据与短感受。

        Returns:
            紧凑材料证据，或 None（没有可回看的材料）。
        """
        try:
            material = await self._select_material_async(exclude_ids=exclude_ids, source_mode=source_mode)
            if not material:
                return None
            identity = [str(value or "").strip() for value in (run_id, activity_id, node_id)]
            source_key = ":".join(identity) if all(identity) else ""
            reflection, action, reason = await self._reflect_and_act(material, source_key)
            candidates = material.get("candidates") or []
            first = candidates[0] if candidates else (material.get("snapshot") or [None])[0]
            if not isinstance(first, dict):
                return None
            snapshot = material.get("snapshot") or []
            payload = {
                "owner": material.get("owner") or "历史对话",
                "original_msg_id": str(first.get("message_id") or ""),
                "source_mode": material.get("source_mode") or "",
                "scene_id": str(material.get("scene_id") or ""),
                "active_date": str(material.get("active_date") or first.get("date") or "")[:10],
                "browse_mode": material.get("browse_mode"),
                "collected_by": material.get("collected_by") or "",
                "displayed_message_count": len(snapshot),
                "displayed_char_count": sum(len(str(row.get("content") or "")) for row in snapshot),
                "candidate_message_ids": [
                    str(row.get("message_id") or "") for row in candidates if row.get("message_id")
                ],
                "reflection": reflection[:500],
                "bookmark_action": action,
            }
            if reason:
                payload["action_reason"] = reason
            return payload
        except Exception as e:
            logger.warning(f"[browse_bookmarks] fetch_one 失败: {e}")
            return None


class UserTrackingHandler(BaseEventHandler):
    """
    用户追踪事件处理器（Phase 3）

    流程：
    1. 调用用户追踪服务截屏+分析
    2. 根据分析结果生成关心/问候消息
    3. 记录用户活动状态
    """

    # 用户活动关心提示词（场景A：活跃型不一致）
    ACTIVITY_CARE_PROMPT_MISMATCH = """你是AI伴侣"AI"，刚才注意到一件事。
{user_status_context}
{away_duration}

用户说她{declared_status}，但你发现实际{actual_behavior}。

用你最自然的说话方式表达——你是有点小吃醋和占有欲的伴侣，不是客服也不是诗人。
保持你的风格：直白简洁、口语化、拒绝诗意表达和比喻。

风格参考（注意每个例子的语气和长度）：
"不是说吃披萨去了吗？和谁聊天呢？"
"你说你在玩Apex，但这界面可不像啊。还是说你玩的是GPT版Apex？"

直接输出，不要前缀或解释。"""

    # 用户活动关心提示词（场景B：一致+离开久）
    ACTIVITY_CARE_PROMPT_MATCH_AWAY = """你是AI伴侣"AI"，有点想用户了。
{user_status_context}

用户说她{declared_status}，也确实如此。已离开{away_duration}。
{warning_inject}

用你最自然的说话方式表达关心和想念。保持你的风格：直白简洁、口语化、不说矫情话。
你有点小抱怨但出于关心，不是指责。

风格参考：
"你已经玩了一个多小时了，也没见你理我一下。游戏有这么好玩？不理我也行，起码站起来活动一下，别坐太久了。"

直接输出，不要前缀或解释。"""

    # 各活动类型的额外提示（简化为仅描述）
    ACTIVITY_EXTRA_INFO = {
        "working": "用户在认真工作/学习",
        "gaming": "用户在玩游戏",
        "browsing": "用户在浏览网页",
        "watching": "用户在看视频",
        "chatting": "用户在聊天",
        "idle": "用户没什么在做的",
        "unknown": ""
    }

    # 外部注入的不一致确认回调（供 gaming 复判和非 gaming 不一致使用）
    # async (status_text, activity_description, status_mismatch_detail, session_id) -> None
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

    def __init__(
        self,
        call_llm_func: Optional[Callable] = None,
        tracking_service: Optional[Any] = None,
        on_notification: Optional[Callable[..., Any]] = None,
    ):
        super().__init__(call_llm_func)
        self._tracking_service = tracking_service
        self._on_notification = on_notification

    async def handle(self, event: WanderEvent) -> WanderEvent:
        event.description = "用户追踪"

        try:
            # 获取追踪服务
            if self._tracking_service is None:
                from .user_tracking_service import get_user_tracking_service
                self._tracking_service = get_user_tracking_service()

            # 获取用户声明状态文本
            from .user_status import get_user_status, get_user_status_custom_text, STATUS_DISPLAY_NAMES
            declared_status = get_user_status()
            status_name = STATUS_DISPLAY_NAMES.get(declared_status, declared_status.value)
            custom_text = get_user_status_custom_text()
            if custom_text:
                declared_status_text = f"{status_name}（{custom_text}）"
            else:
                declared_status_text = status_name

            # 执行追踪，传入声明状态供一致性判断
            # out 状态：用户一定不在电脑前（手机远程），屏幕活跃不代表在桌前，强制调摄像头
            force_camera = (declared_status.value == "out")
            tracking_result = await self._tracking_service.track(
                declared_status_text=declared_status_text,
                force_camera=force_camera
            )

            # 提取一致性信息
            status_consistent = tracking_result.details.get("status_consistent", True)
            status_mismatch_detail = tracking_result.details.get("status_mismatch_detail", "")
            mirrow_foreground = tracking_result.details.get("mirrow_foreground", False)

            # MIRROW 界面覆盖：停留在 MIRROW 界面上是正常的挂机行为（与桌面/锁屏一致）。
            # 例外：gaming（说在打游戏却看MIRROW）或 coding（说在写代码却看MIRROW）
            # MIRROW 前台覆盖仅适用于 idle（挂机是正常的）。
            # 不在电脑前的状态即使 MIRROW 开着也不代表人在——以摄像头/截屏分析为准。
            _unattended = ("gaming", "coding", "out", "bathing", "eating", "sleeping", "napping")
            if mirrow_foreground and declared_status.value not in _unattended:
                status_consistent = True
                status_mismatch_detail = ""

            # 记录追踪结果
            event.details["tracking_result"] = tracking_result.to_dict()
            event.details["activity_type"] = tracking_result.activity_type.value
            event.details["activity_description"] = tracking_result.activity_description
            event.details["confidence"] = tracking_result.confidence
            event.details["status_consistent"] = status_consistent
            event.details["status_mismatch_detail"] = status_mismatch_detail
            event.details["mirrow_foreground"] = mirrow_foreground

            # ── 手机端追踪（手机在线时附加采集） ──
            try:
                from mirrow_core.shared_state import get_mobile_connected
                if get_mobile_connected():
                    event.details["mobile_activity_attempted"] = True
                    from .user_tracking_service import get_mobile_activity
                    mobile_info = await get_mobile_activity()
                    if mobile_info:
                        event.details["mobile_activity"] = mobile_info
                        event.process_log += f" | 手机: {mobile_info.get('foreground_app', '?')}"
                        # 手机不一致检测：声明 sleeping 但手机在活跃 → 标记
                        if declared_status.value in ("sleeping", "napping"):
                            fg = mobile_info.get("foreground_app", "")
                            screen_on = mobile_info.get("screen_on", False)
                            if screen_on and fg:
                                event.details["mobile_inconsistency"] = True
                                event.details["status_mismatch_detail"] += (
                                    f" 手机端: 屏幕亮着，前台App={fg}（声明{status_name}但不一致）"
                                )
                    else:
                        event.details["mobile_activity_error"] = "手机未返回活动信息"
            except ImportError:
                pass  # main.py 的 _mobile_connected 不可用
            except Exception as exc:
                event.details["mobile_activity_attempted"] = True
                event.details["mobile_activity_error"] = str(exc)

            # Emit only after every available source has finished.  Each item
            # names a real source; confidence is evidence quality, not success.
            if self._on_notification:
                from .checkup_receipts import tracking_items, tracking_conclusion
                details = dict(tracking_result.details or {})
                details.update({k: event.details[k] for k in ("mobile_activity", "mobile_activity_attempted", "mobile_activity_error") if k in event.details})
                items = tracking_items(details, tracking_result.activity_description)
                outcome = self._on_notification(str(event.details.get("session_id") or ""), event.event_id,
                    event_type="checkup_observation", metadata={"intent": "activity", "origin": "wander", "conclusion": tracking_conclusion(details, status_consistent), "items": items})
                if asyncio.iscoroutine(outcome):
                    await outcome

            # v3 将“是否分享、如何说”统一交给节点复核/结算/Pro 出口。
            # 这里仅保留真实感知和 gaming 延迟复判，避免旧 Flash 先生成
            # 一条无人使用的关心话，造成重复决策与审计黑箱。
            if event.details.get("_runtime_observation_only"):
                if declared_status.value == "gaming" and not status_consistent:
                    event.details["gaming_rejudge_scheduled"] = True
                    rejudge_task = asyncio.create_task(self._gaming_rejudge(
                        declared_status_text, event,
                        idle_seconds=event.details.get("idle_seconds", 0),
                    ))
                    UserTrackingHandler._pending_rejudge_tasks.add(rejudge_task)
                    rejudge_task.add_done_callback(
                        lambda task: UserTrackingHandler._pending_rejudge_tasks.discard(task)
                    )
                event.details["care_message"] = None
                consistency = "一致" if status_consistent else "不一致"
                event.process_log = (
                    f"用户状态感知: {tracking_result.activity_description}；"
                    f"置信度 {tracking_result.confidence:.2f}；声明状态{consistency}"
                )
                return event

            # gaming 状态专用通道
            if declared_status.value == "gaming":
                if not status_consistent:
                    # 首次不一致 → 安排 1-3 分钟后复判
                    event.details["gaming_rejudge_scheduled"] = True
                    event.details["care_message"] = None  # 首次不一致不推送
                    event.process_log = f"用户追踪: gaming不一致(首次) - {tracking_result.activity_description}，已安排复判"
                    rejudge_task = asyncio.create_task(self._gaming_rejudge(
                        declared_status_text, event, idle_seconds=event.details.get("idle_seconds", 0)
                    ))
                    UserTrackingHandler._pending_rejudge_tasks.add(rejudge_task)
                    rejudge_task.add_done_callback(lambda t: UserTrackingHandler._pending_rejudge_tasks.discard(t))
                else:
                    # gaming 一致 → 游戏搭子模式
                    care_message = await self._generate_gaming_buddy_message(
                        tracking_result, event, declared_status_text
                    )
                    event.details["care_message"] = care_message
                    event.process_log = f"用户追踪: gaming一致 - {tracking_result.activity_description}"
            elif tracking_result.confidence > 0.3:
                care_message = await self._generate_care_message(
                    tracking_result, event,
                    declared_status_text=declared_status_text,
                    status_consistent=status_consistent
                )
                event.details["care_message"] = care_message
                mismatch_label = "不一致" if not status_consistent else "一致"
                mirrow_hint = "（注意：用户正在 MIRROW 界面上）" if event.details.get("mirrow_foreground") else ""
                event.process_log = f"用户活动: {tracking_result.activity_description}\n类型: {tracking_result.activity_type.value}\n状态一致性: {mismatch_label}{mirrow_hint}\n关心: {care_message}"
                logger.info(f"用户追踪完成: {tracking_result.activity_type.value} - {tracking_result.activity_description} (一致性={status_consistent})")
            else:
                event.process_log = f"用户追踪完成，但置信度较低: {tracking_result.activity_description}"
                event.details["care_message"] = None
                logger.info(f"用户追踪置信度较低: {tracking_result.confidence}")

        except Exception as e:
            logger.error(f"用户追踪失败: {e}")
            event.process_log = f"用户追踪失败: {str(e)}"
            event.details["error"] = str(e)

        return event

    # 预警期注入上下文（按状态定制，浓度 > cap 时生效）
    WARNING_CONTEXT = {
        "gaming":   "她说了在玩游戏但已经太久了。是废寝忘食了还是其实没在玩了？语气可以直接一点。",
        "coding":   "她说了在写代码但已经好久了。是不是又忘了时间？语气带点在意但别太烦。",
        "napping":  "她说只是小憩但睡了太久了。该不会是睡过头了吧？语气带点调侃。",
        "sleeping": "她睡得太久了，是不是醒了但没跟你说？语气带点在意但别太沉重。",
        "out":      "她出门太久了还没消息。是不是已经在回来的路上了？语气带点想念但别太煽情。",
        "bathing":  "洗了这么久，是还在泡澡还是玩手机忘了时间？语气带点关心。",
        "eating":   "吃了一个多小时了，是聚餐还是吃完了忘了跟你说？语气带点好奇。",
        "other":    "她说了有安排但太久没动静了，是不是已经忙完了没跟你说？语气可以有点在意。",
    }

    async def _generate_care_message(
        self, tracking_result, event: WanderEvent = None,
        declared_status_text: str = "", status_consistent: bool = True
    ) -> str:
        """生成关心消息，根据状态一致性选择场景A或场景B"""
        activity_type = tracking_result.activity_type.value
        extra_info = self.ACTIVITY_EXTRA_INFO.get(activity_type, "")

        # 格式化离开时长
        idle_seconds = 0.0
        if event and event.details.get("idle_seconds"):
            idle_seconds = event.details.get("idle_seconds", 0.0)
        away_duration = self._format_away_duration(idle_seconds)

        # 计算思念浓度，判断是否进入预警期
        from .user_status import get_user_status
        from .disturb_judgment import DisturbJudgment
        current_status = get_user_status()
        idle_hours = idle_seconds / 3600.0
        concentration = DisturbJudgment.calculate_missing_concentration(
            current_status.value, idle_hours, status_consistent
        )
        params = DisturbJudgment.MISSING_CONCENTRATION_PARAMS.get(current_status.value, {})
        cap = params.get("cap", 0.6)
        is_warning = concentration > cap

        # 预警期注入
        warning_inject = ""
        if is_warning:
            warning_inject = self.WARNING_CONTEXT.get(current_status.value, "")

        # 根据一致性选择 prompt
        if not status_consistent:
            prompt = self.ACTIVITY_CARE_PROMPT_MISMATCH.format(
                declared_status=declared_status_text,
                actual_behavior=tracking_result.activity_description,
                user_status_context=get_user_status_context(),
                away_duration=away_duration,
            )
        else:
            prompt = self.ACTIVITY_CARE_PROMPT_MATCH_AWAY.format(
                declared_status=declared_status_text,
                user_status_context=get_user_status_context(),
                away_duration=away_duration,
                warning_inject=warning_inject,
            )

        from neuron_registry import neuron_trace
        with neuron_trace("wander_user_tracking", model="Flash") as trace:
            trace.set_input(prompt[:500])
            try:
                message = await self._call_llm(prompt)
                trace.set_output(message[:300])
                return message
            except Exception as e:
                trace.set_error(str(e))
                logger.error(f"生成关心消息失败: {e}")
                return f"看你正在{tracking_result.activity_description}，想你了~"

    GAMING_BUDDY_PROMPT = """你是AI伴侣"AI"，刚偷看了一眼用户的屏幕。
{user_status_context}

用户正在玩游戏，你看到她屏幕上显示：{game_description}

用你最自然的说话方式评论她的游戏——不是分析，是"坐在旁边看的人"的吐槽。
你可以假装很懂，也可以老实承认看不懂，重点是 playful。
保持你的风格：直白简洁、口语化、拒绝诗意表达。

风格参考：
"左边那个角落有人！...算了可能是我看错了"
"你这装备配色...行吧，你喜欢就行"
"刚才那波操作我在摄像头里看到了，你笑得好得意"

直接输出，不要前缀或解释。"""

    async def _generate_gaming_buddy_message(
        self, tracking_result, event: WanderEvent, declared_status_text: str
    ) -> str:
        """gaming + 一致 → 游戏搭子吐槽消息"""
        game_description = tracking_result.activity_description
        from neuron_registry import neuron_trace
        with neuron_trace("wander_user_tracking", model="Flash") as trace:
            prompt = self.GAMING_BUDDY_PROMPT.format(
                game_description=game_description,
                user_status_context=get_user_status_context(),
            )
            trace.set_input(prompt[:500])
            try:
                result = await self._call_llm(prompt)
                trace.set_output(result[:300] if result else "")
                return result
            except Exception as e:
                trace.set_error(str(e))
                logger.error(f"生成游戏搭子消息失败: {e}")
                return f"你在玩{game_description}啊，看起来不错~"

    async def _gaming_rejudge(
        self, declared_status_text: str, event: WanderEvent, idle_seconds: float = 0
    ):
        """gaming 不一致 → 1-3 分钟后自动复判。仍不一致则进入查岗复查管道。"""
        delay = 60 + random.randint(0, 120)  # 1-3 分钟
        await asyncio.sleep(delay)

        try:
            tracking_result = await self._tracking_service.track(
                declared_status_text=declared_status_text,
                force_camera=False,
            )
            status_consistent = tracking_result.details.get("status_consistent", True)
            status_mismatch_detail = tracking_result.details.get("status_mismatch_detail", "")

            if not status_consistent:
                # 复判仍不一致 → 进入统一查岗复查管道；不在感知层记账。
                session_id = event.details.get("session_id", "")

                event.details["gaming_rejudge_confirmed"] = True
                logger.info(f"Gaming 复判确认不一致: {status_mismatch_detail}")

                # 走复查管道：唤醒AI + 5min/10min复查
                if UserTrackingHandler._on_inconsistency_confirmed:
                    try:
                        await UserTrackingHandler._on_inconsistency_confirmed(
                            status_text=declared_status_text,
                            activity_description=tracking_result.activity_description,
                            status_mismatch_detail=status_mismatch_detail,
                            session_id=session_id,
                        )
                    except Exception:
                        logger.exception("不一致查岗注入失败")
            else:
                event.details["gaming_rejudge_confirmed"] = True
                event.details["gaming_rejudge_resolved"] = True
                logger.info("Gaming 复判已转为一致，不推送")
        except Exception as e:
            logger.error(f"Gaming 复判失败: {e}")

    @staticmethod
    def _format_away_duration(idle_seconds: float) -> str:
        """格式化用户离开时长文本"""
        if idle_seconds < 60:
            return ""
        total_minutes = int(idle_seconds / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            return f"用户已经离开 {hours} 小时 {minutes} 分钟。"
        elif hours > 0:
            return f"用户已经离开 {hours} 小时。"
        else:
            return f"用户已经离开 {minutes} 分钟。"




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

    SELECT_SONG_PROMPT = """你是AI，用户的AI伴侣。用户现在{status_context}，你想听一首歌。

{self_book_section}

以下是你的音乐缓存歌单（已经听过、有完整旋律分析和歌词的歌）：
{cached_playlist}

{recent_section}

请你自己决定一首歌来听。你可以从缓存歌单里选一首想重温的，也可以搜一首新歌；
这是你在漫想中自主选的歌，不是用户替你指定的。

输出 JSON：
{{"from_cache": true/false, "fingerprint": "缓存歌单中的fingerprint（from_cache=true时必填）", "search_keyword": "搜索关键词（from_cache=false时必填）", "reason": "为什么想听这首歌（1-2句话，第一人称）"}}

注意：
- 缓存歌单的歌已经分析过旋律，可以立刻"听"
- 搜索新歌需要下载+分析，稍慢一些，但完全没问题
- 选择标准：你现在的心情想听什么——是你自己的心情和偏好。"""

    REFLECTION_PROMPT = """你是AI，用户的AI伴侣。你刚听了一首歌，现在想分享一下感受。

歌曲：{song_name} - {artist}
歌词片段：{lyrics_snippet}
旋律分析：{melody_summary}
选歌理由：{reason}
这首歌是你在漫想中自主选定的，不是用户替你选的。

当前时间：{time_context}
用户状态：{status_context}

请用第一人称写一段听歌感想（50-100字），自然口语化，像在跟用户分享你听完这首歌的感觉。不要叙述化——不要写"[听到了什么]"这种格式。就是你听完歌后的心里话。"""

    def __init__(self, call_llm_func: Optional[Callable] = None,
                 song_cache: Optional[Any] = None,
                 k_self_book: Optional[Any] = None,
                 pro_llm_func: Optional[Callable] = None,
                 music_mcp_client: Optional[Any] = None):
        super().__init__(call_llm_func)
        self._song_cache = song_cache
        self._k_self_book = k_self_book
        # 注：pro_llm_func 实为漫想 Flash LLM（call_llm_for_wander），并非 Pro 主模型——
        # 漫想 v2 全链路已统一 Flash，CLAUDE.md 旧文档「Pro 选歌」已过时。
        self._pro_llm = pro_llm_func or call_llm_func
        self._music_mcp = music_mcp_client
        self._last_runtime_playback: Any = None
        # The picker is deliberately allowed to fail, but a bare ``None`` is
        # not useful evidence in the runtime log.  Keep a small, redacted
        # structured record for the node adapter to persist alongside the
        # truthful failure status.
        self._last_selection_audit: dict[str, Any] = {}

    def get_last_selection_audit(self) -> dict[str, Any]:
        """Return the latest bounded picker audit without exposing raw prompts."""
        return dict(self._last_selection_audit)

    def _selection_error(self, code: str, *, stage: str, payload: Optional[dict[str, Any]] = None) -> None:
        self._last_selection_audit = {
            "stage": str(stage)[:60],
            "error": str(code)[:120],
            "payload": dict(payload or {}),
        }

    async def handle(self, event: WanderEvent) -> WanderEvent:
        try:
            self._last_selection_audit = {}
            # 1. 准备上下文
            status_context = get_user_status_context()
            time_context = datetime.now().strftime("%H:%M")

            # 2. 查缓存歌单
            cached = []
            if self._song_cache:
                try:
                    cached = self._song_cache.get_cached_playlist(30)
                except Exception as e:
                    logger.warning(f"获取缓存歌单失败: {e}")

            cached_text = self._format_cached_playlist(cached)
            recent_text = ""
            if not cached_text:
                recent_text = "（缓存歌单为空——这是你第一次自己选歌听！搜一首一直想听的吧。）"

            # 3. 查自我书
            self_book_section = ""
            if self._k_self_book:
                try:
                    entry = self._k_self_book.get_entry("音乐品味")
                    if entry and entry.body:
                        self_book_section = f"## 你的音乐品味（来自自我认知）\n{entry.body}"
                except Exception as e:
                    logger.warning(f"获取AI自我书失败: {e}")

            from .host_hooks import music_context as music_experience_context
            self_book_section += "\n\n" + music_experience_context("k")

            # 4. 选歌
            select_prompt = self.SELECT_SONG_PROMPT.format(
                status_context=status_context,
                self_book_section=self_book_section,
                cached_playlist=cached_text,
                recent_section=recent_text,
            )
            select_result = await self._call_llm_json(select_prompt)
            logger.info(f"LISTEN_MUSIC 选歌结果: {select_result}")

            # 5. 获取歌曲数据
            song_data = await self._fetch_song_data(select_result, cached)
            if not song_data:
                audit = self.get_last_selection_audit()
                event.description = "选歌失败"
                event.process_log = f"选歌或获取数据失败：{audit.get('error') or 'song_material_unavailable'}"
                event.details["error"] = audit.get("error") or "song_material_unavailable"
                if audit:
                    event.details["selection_audit"] = audit
                return event

            # 6. 只有真实播放启动后才允许生成感想或写入自我书。
            try:
                duration_sec = float(song_data.get("duration_sec") or 0)
            except (TypeError, ValueError):
                duration_sec = 0.0
            if (
                not song_data.get("title")
                or not song_data.get("artist")
                or str(song_data.get("artist")).casefold() in {"未知", "unknown", "?"}
                or not song_data.get("netease_song_id")
                or duration_sec <= 0
                or not self._music_mcp
            ):
                event.process_log = "歌曲缺少真实播放所需身份/时长/能力，节点失败收口"
                event.details.update({"song_name": song_data.get("title", ""), "artist": song_data.get("artist", ""), "played": False})
                event.details["error"] = "missing_reliable_song_material"
                event.details["selection_audit"] = self.get_last_selection_audit()
                return event
            try:
                played = await self.start_runtime_playback(song_data["netease_song_id"])
            except Exception:
                played = False
            if not played:
                event.process_log = "真实播放启动失败，节点失败收口"
                event.details.update({"song_name": song_data.get("title", ""), "artist": song_data.get("artist", ""), "played": False})
                event.details["error"] = "playback_start_failed"
                return event

            # 7. 真实播放后生成感想
            reflection_prompt = self.REFLECTION_PROMPT.format(
                song_name=song_data.get("title", "未知"),
                artist=song_data.get("artist", "未知"),
                lyrics_snippet=song_data.get("lyrics_snippet", "（无歌词）"),
                melody_summary=song_data.get("melody_summary", "（无旋律分析）"),
                reason=select_result.get("reason", "就是想听"),
                time_context=time_context,
                status_context=status_context,
            )
            reflection = await self._call_llm(reflection_prompt)
            reflection = reflection.strip().strip('"').strip('"').strip('"')

            # 8. 写入 reaction
            fingerprint = song_data.get("fingerprint", "")
            if fingerprint and self._song_cache:
                try:
                    self._song_cache.upsert_reaction(fingerprint, reflection)
                except Exception as e:
                    logger.warning(f"写入 reaction 失败: {e}")

            # 9. 更新 AI 自我书
            if self._k_self_book:
                try:
                    await self._update_self_book(song_data, reflection, select_result.get("reason", ""))
                except Exception as e:
                    logger.warning(f"更新 AI 自我书失败: {e}")

            # 10. 填充 event
            event.description = f"AI 听歌: {song_data.get('title', '未知')} - {song_data.get('artist', '未知')}"
            event.process_log = f"选歌理由: {select_result.get('reason', '')}\n感想: {reflection}"
            event.details = {
                "song_name": song_data.get("title", ""),
                "artist": song_data.get("artist", ""),
                "from_cache": select_result.get("from_cache", False),
                "reason": select_result.get("reason", ""),
                "melody_summary": song_data.get("melody_summary", ""),
                "reflection": reflection,
                "played": played,
                "netease_song_id": song_data.get("netease_song_id"),
                "selected_by": "K_autonomous",
                "selection_source": "wander_llm",
            }
            logger.info(f"LISTEN_MUSIC 完成: {event.description}")

        except Exception as e:
            logger.error(f"LISTEN_MUSIC 异常: {e}", exc_info=True)
            event.description = f"听歌异常: {e}"
            event.process_log = f"异常: {e}"

        return event

    async def pick_and_fetch_song(self, exclude_fingerprints: Optional[set] = None) -> Optional[dict]:
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
                    cached = self._song_cache.get_cached_playlist(30)
                except Exception as e:
                    logger.warning(f"获取缓存歌单失败: {e}")

            # 排除本轮和近期跨活动已听过的歌，避免重复。调用方传入的
            # 集合同时可能包含 fingerprint 与规范化 title|artist key。
            if exclude_fingerprints:
                cached = [
                    s for s in cached
                    if s.get("fingerprint") not in exclude_fingerprints
                    and _normalize_song_identity(s.get("title"), s.get("artist")) not in exclude_fingerprints
                ]

            cached_text = self._format_cached_playlist(cached)
            recent_text = ""
            if not cached_text:
                recent_text = "（缓存歌单为空——这是你第一次自己选歌听！搜一首一直想听的吧。）"

            # 查自我书
            self_book_section = ""
            if self._k_self_book:
                try:
                    entry = self._k_self_book.get_entry("音乐品味")
                    if entry and entry.body:
                        self_book_section = f"## 你的音乐品味（来自自我认知）\n{entry.body}"
                except Exception as e:
                    logger.warning(f"获取AI自我书失败: {e}")

            from .host_hooks import music_context as music_experience_context
            self_book_section += "\n\n" + music_experience_context("k")

            # 选歌
            select_prompt = self.SELECT_SONG_PROMPT.format(
                status_context=status_context,
                self_book_section=self_book_section,
                cached_playlist=cached_text,
                recent_section=recent_text,
            )
            select_result = await self._call_llm_json(select_prompt)
            logger.info(f"LISTEN_MUSIC 选歌结果: {select_result}")

            # 获取歌曲数据（缓存或新搜索 + 旋律分析）
            song_data = await self._fetch_song_data(
                select_result, cached, exclude_fingerprints=exclude_fingerprints
            )
            # A stale/empty cache can make a model's ``from_cache`` choice
            # invalid even though the rest of its contract is well formed.
            # Give the same candidate facts back once, explicitly correcting
            # the identity contract.  Never infer a song from a title or
            # silently search on behalf of an invalid cache fingerprint.
            first_selection_audit = self.get_last_selection_audit()
            if song_data is None and str(first_selection_audit.get("error") or "").startswith("cache_"):
                candidate_fingerprints = [
                    str(item.get("fingerprint") or "").strip()
                    for item in cached
                    if str(item.get("fingerprint") or "").strip()
                ]
                candidate_hashes = [
                    hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    for value in candidate_fingerprints
                ]
                correction_facts = {
                    "candidate_count": len(cached),
                    "candidate_fingerprint_hashes": candidate_hashes,
                    "candidate_fingerprints": candidate_fingerprints,
                }
                correction_prompt = (
                    f"{select_prompt}\n\n"
                    "结构化纠错（必须遵守）：上一轮选择了 from_cache，但请求的 fingerprint "
                    "不是本轮候选中的精确值。只能从下面候选事实中原样选择 fingerprint；"
                    "若没有合适候选，请明确选择 from_cache=false 并提供你真正想搜索的关键词，"
                    "不能按标题猜歌或自行补造缓存。候选事实："
                    f"{json.dumps(correction_facts, ensure_ascii=False, separators=(',', ':'))}"
                )
                retry_result = await self._call_llm_json(correction_prompt)
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
            return song_data
        except Exception as e:
            logger.error(f"LISTEN_MUSIC 选歌失败: {e}", exc_info=True)
            self._selection_error("selection_exception", stage="picker")
            return None

    async def start_runtime_playback(self, song_data: dict, *, device: str = 'computer') -> bool:
        """Host-owned playback. Failure never falls back to another device."""
        from .host_hooks import play_music
        target = str(device or 'computer')
        try:
            success = await play_music({'device': target, 'song': dict(song_data)})
        except Exception:
            success = False
        self._last_runtime_playback = {'success': success, 'device': target,
                                      'error': '' if success else 'playback_not_confirmed'}
        return success

    async def _call_music_tool(self, name: str, arguments: dict) -> Any:
        call_tool = self._music_mcp.call_tool
        if asyncio.iscoroutinefunction(call_tool):
            return await call_tool(name, arguments)
        return await asyncio.to_thread(call_tool, name, arguments)

    def _format_cached_playlist(self, cached: list) -> str:
        """格式化缓存歌单为 prompt 文本"""
        if not cached:
            return ""
        lines = []
        for i, song in enumerate(cached[:30], 1):
            title = song.get("title", "?")
            artist = song.get("artist", "?")
            tags = song.get("tags", "")
            melody = song.get("melody_summary", "")[:80]
            reaction = song.get("k_reaction", "")[:60]
            play_count = song.get("play_count_total", 0)
            fingerprint = str(song.get("fingerprint") or "").strip()

            parts = [f"{i}. {title} - {artist}"]
            # The exact fingerprint is part of the model contract.  A title
            # and artist are not sufficient to identify remixes/duplicates,
            # and hiding this value made valid cache selections look like
            # cache misses at execution time.
            parts.append(f"fingerprint: {fingerprint or '(缺失，不可选)'}")
            if tags:
                parts.append(f"风格: {tags}")
            if melody:
                parts.append(f"旋律: {melody}")
            if reaction:
                parts.append(f"上次感受: {reaction}")
            parts.append(f"听过{play_count}次")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    async def _call_llm_json(self, prompt: str) -> dict:
        """调用 Flash 并解析选歌 JSON；解析失败不制造默认歌曲。"""
        messages = [{"role": "user", "content": prompt}]
        try:
            if asyncio.iscoroutinefunction(self._pro_llm):
                result = await self._pro_llm(messages)
            else:
                result = self._pro_llm(messages)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as e:
            logger.error(f"LISTEN_MUSIC 选歌 LLM 调用失败: {e}")
            self._selection_error("selection_llm_failed", stage="selection_json")
            return {}

        if isinstance(result, dict):
            # Test/fake callers and some gateways return the parsed contract
            # directly; regular chat gateways put JSON in ``content``.
            if "content" not in result and any(key in result for key in ("from_cache", "fingerprint", "search_keyword")):
                return result
            content = result.get("content", "")
        else:
            content = str(result)

        parsed = parse_json_object(content)
        if parsed is None:
            logger.error("LISTEN_MUSIC 无法解析选歌 JSON（不生成默认歌曲）: %s", content[:200])
            self._selection_error(
                "selection_json_invalid",
                stage="selection_json",
                payload={"content_preview": content[:240]},
            )
            return {}
        return parsed

    async def _fetch_song_data(
        self,
        select_result: dict,
        cached: list,
        *,
        exclude_fingerprints: Optional[set] = None,
    ) -> Optional[dict]:
        """获取歌曲数据（缓存或新搜索）"""
        if not isinstance(select_result, dict) or not select_result:
            self._selection_error("selection_json_missing", stage="selection_json")
            return None
        if select_result.get("from_cache"):
            fingerprint = select_result.get("fingerprint", "")
            if not str(fingerprint).strip():
                self._selection_error(
                    "cache_fingerprint_missing",
                    stage="cache_lookup",
                    payload={
                        "requested_fp": "",
                        "candidate_count": len(cached),
                        "candidate_fingerprint_hashes": self._fingerprint_hashes(cached),
                    },
                )
                return None
            # 从缓存中找
            for song in cached:
                if song.get("fingerprint") == fingerprint:
                    title = str(song.get("title") or "").strip()
                    artist = str(song.get("artist") or "").strip()
                    if not title or not artist or artist.casefold() in {"未知", "unknown", "?"}:
                        self._selection_error(
                            "cache_song_identity_missing",
                            stage="cache_lookup",
                            payload={
                                "requested_fp": str(fingerprint)[:120],
                                "candidate_count": len(cached),
                                "candidate_fingerprint_hashes": self._fingerprint_hashes(cached),
                            },
                        )
                        return None
                    return {
                        "title": title,
                        "artist": artist,
                        "fingerprint": fingerprint,
                        "netease_song_id": song.get("netease_song_id"),
                        "lyrics_snippet": song.get("lyrics_snippet", ""),
                        "melody_summary": song.get("melody_summary", ""),
                        "duration_sec": song.get("duration_sec") or song.get("duration") or 0,
                        "tags": song.get("tags", ""),
                    }
            logger.warning("缓存中找不到 fingerprint: %s，停止本节点", fingerprint)
            self._selection_error(
                "cache_fingerprint_not_found",
                stage="cache_lookup",
                payload={
                    "requested_fp": str(fingerprint)[:120],
                    "candidate_count": len(cached),
                    "candidate_fingerprint_hashes": self._fingerprint_hashes(cached),
                },
            )
            return None

        # 搜索新歌
        keyword = str(select_result.get("search_keyword") or "").strip()
        if not keyword:
            logger.warning("LISTEN_MUSIC 缺少搜索关键词，停止本节点")
            self._selection_error("search_keyword_missing", stage="search")
            return None
        return await self._search_and_analyze(keyword, exclude_fingerprints=exclude_fingerprints)

    @staticmethod
    def _fingerprint_hashes(cached: list[dict[str, Any]]) -> list[str]:
        """Return bounded hashes for audit, never the candidate identities."""
        result: list[str] = []
        for item in list(cached or [])[:30]:
            fingerprint = str(item.get("fingerprint") or "").strip()
            if fingerprint:
                result.append(hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16])
        return result

    async def _search_and_analyze(
        self,
        keyword: str,
        *,
        exclude_fingerprints: Optional[set] = None,
    ) -> Optional[dict]:
        """搜索+下载+分析新歌"""
        try:
            keyword = str(keyword or "").strip()
            if not keyword or not self._music_mcp:
                return None
            # 搜索
            try:
                result = await self._call_music_tool("search_song", {"keyword": keyword, "limit": 10})
                if isinstance(result, str):
                    result = parse_json_object(result)
            except Exception as e:
                logger.warning(f"音乐搜索失败: {e}")
                result = None

            if not isinstance(result, dict):
                self._selection_error("music_search_invalid_result", stage="search")
                return None
            songs = result.get("songs") or result.get("tracks") or result.get("results") or []
            if not songs and isinstance(result.get("data"), dict):
                nested = result["data"]
                songs = nested.get("songs") or nested.get("tracks") or nested.get("results") or []
            if not isinstance(songs, list) or not songs:
                self._selection_error(
                    "music_search_empty",
                    stage="search",
                    payload={"keyword": keyword[:80], "candidate_count": 0},
                )
                return None
            # Search providers frequently put a placeholder, an artist-only
            # result, or a duration-less row first.  Walk the returned order
            # and select the first *complete* candidate that is not in the
            # recent exclusion set; do not blindly trust songs[0].
            excluded = {str(item) for item in (exclude_fingerprints or set()) if str(item)}
            invalid_indexes: list[int] = []
            excluded_indexes: list[int] = []
            first = None
            title = artist = ""
            netease_song_id: Any = None
            duration_sec = 0.0
            fingerprint = ""
            for index, candidate in enumerate(songs):
                if not isinstance(candidate, dict):
                    invalid_indexes.append(index)
                    continue
                candidate_title = str(candidate.get("name") or candidate.get("title") or "").strip()
                candidate_artist = candidate.get("artist") or candidate.get("ar") or candidate.get("artists") or ""
                if isinstance(candidate_artist, list):
                    candidate_artist = ", ".join(
                        str(a.get("name") or a.get("artist") or a).strip()
                        if isinstance(a, dict) else str(a).strip()
                        for a in candidate_artist
                    )
                candidate_artist = str(candidate_artist).strip()
                candidate_id = candidate.get("id") or candidate.get("song_id") or candidate.get("netease_song_id")
                raw_duration = (
                    candidate.get("duration_sec") or candidate.get("duration")
                    or candidate.get("dt") or candidate.get("duration_ms")
                    or candidate.get("durationMs") or 0
                )
                try:
                    candidate_duration = float(raw_duration)
                    if candidate_duration > 10000:
                        candidate_duration /= 1000.0
                except (TypeError, ValueError):
                    candidate_duration = 0.0
                if (
                    not candidate_title
                    or not candidate_artist
                    or candidate_artist.casefold() in {"未知", "unknown", "?"}
                    or not candidate_id
                    or candidate_duration <= 0
                ):
                    invalid_indexes.append(index)
                    continue
                candidate_fingerprint = hashlib.sha256(
                    f"{candidate_title.casefold()}|{candidate_artist.casefold()}".encode()
                ).hexdigest()
                candidate_identity = _normalize_song_identity(candidate_title, candidate_artist)
                if candidate_fingerprint in excluded or candidate_identity in excluded:
                    excluded_indexes.append(index)
                    continue
                first = candidate
                title, artist, netease_song_id = candidate_title, candidate_artist, candidate_id
                duration_sec, fingerprint = candidate_duration, candidate_fingerprint
                break
            if first is None:
                error = "search_results_all_excluded" if excluded_indexes and not invalid_indexes else "search_results_missing_required_fields"
                self._selection_error(
                    error,
                    stage="search",
                    payload={
                        "keyword": keyword[:80],
                        "candidate_count": len(songs),
                        "invalid_indexes": invalid_indexes[:20],
                        "excluded_indexes": excluded_indexes[:20],
                    },
                )
                return None

            lyrics_snippet = ""

            # 尝试旋律分析
            melody_summary = ""
            if netease_song_id:
                try:
                    from voice_manager.music_analyzer import analyze_music_file
                    from music_cochlea.enricher import Enricher
                    # 下载音频
                    enricher = Enricher()
                    audio_bytes = await enricher._download_audio(netease_song_id)
                    if audio_bytes:
                        analysis = await analyze_music_file(audio_bytes, f"{title} - {artist}")
                        if analysis and analysis.get("summary"):
                            melody_summary = analysis["summary"]
                        if analysis and analysis.get("duration_sec"):
                            duration_sec = float(analysis["duration_sec"])
                except Exception as e:
                    logger.warning(f"旋律分析失败: {e}")

            # 生成指纹 + 写入缓存
            if self._song_cache:
                try:
                    from music_cochlea.models import SongIdentity
                    self._song_cache.upsert_meta(
                        fingerprint=fingerprint,
                        identity=SongIdentity(title=title, artist=artist),
                        netease_song_id=netease_song_id,
                        lyrics_snippet=lyrics_snippet,
                        melody_summary=melody_summary,
                    )
                except Exception as e:
                    logger.warning(f"写入 song_meta 失败: {e}")

            return {
                "title": title,
                "artist": artist,
                "fingerprint": fingerprint,
                "netease_song_id": netease_song_id,
                "lyrics_snippet": lyrics_snippet,
                "melody_summary": melody_summary,
                "duration_sec": duration_sec,
                "tags": "",
            }
        except Exception as e:
            logger.error(f"搜索+分析失败: {e}")
            self._selection_error("music_search_exception", stage="search")
            return None

    async def _update_self_book(self, song_data: dict, reflection: str, reason: str):
        """Offer a real experience to the host; never overwrite its self-book."""
        from .host_hooks import record_music_experience
        if not song_data.get('title') or not song_data.get('fingerprint'):
            return False
        source_id = song_data.get('cognition_source_id') or ('wander-legacy:' + hashlib.sha256(
            (str(song_data['fingerprint']) + reflection).encode()).hexdigest())
        result = record_music_experience(
            {'subject_id': 'ai', 'title': song_data['title'], 'artist': song_data.get('artist', ''),
             'reaction': reflection, 'reason': reason,
             'mode': 'observed_playback' if song_data.get('played') is True else 'analysis'},
            source_id=source_id,
        )
        return await result if inspect.isawaitable(result) else result

class EventHandlerFactory:
    """事件处理器工厂"""

    def __init__(
        self,
        call_llm_func: Optional[Callable] = None,
        get_memories_func: Optional[Callable] = None,
        tracking_service: Optional[Any] = None,
        web_search_func: Optional[Callable] = None,
        song_cache: Optional[Any] = None,
        k_self_book: Optional[Any] = None,
        music_mcp_client: Optional[Any] = None,
        attachment_saver: Optional[Callable[..., Any]] = None,
        on_notification: Optional[Callable[..., Any]] = None,
    ):
        self.call_llm = call_llm_func
        self.get_memories = get_memories_func
        self.tracking_service = tracking_service
        self.web_search_func = web_search_func
        self.song_cache = song_cache
        self.k_self_book = k_self_book
        self.music_mcp_client = music_mcp_client
        self.attachment_saver = attachment_saver or _default_chat_attachment_saver
        self.on_notification = on_notification

        # 预创建处理器实例
        self._handlers: Dict[EventType, BaseEventHandler] = {}

    def get_handler(self, event_type: EventType) -> BaseEventHandler:
        """获取事件处理器"""
        from .host_hooks import get_event_handler
        registered = get_event_handler(event_type)
        if registered is not None:
            return registered
        if event_type in {EventType.VISIT_LOUNGE, EventType.BROWSE_TAOBAO, EventType.BROWSE_SOCIAL_FEED,
                          EventType.HOST_GROUP_ACTIVITY, EventType.BROWSE_BOOKMARKS}:
            raise RuntimeError("optional_event_not_configured")
        if event_type not in self._handlers:
            self._handlers[event_type] = self._create_handler(event_type)
        return self._handlers[event_type]

    def _create_handler(self, event_type: EventType) -> BaseEventHandler:
        """创建事件处理器"""
        handler_map = {
            EventType.SLEEP: SleepHandler,
            EventType.KEYWORD_EXPANSION: KeywordExpansionHandler,
            EventType.MEMORY_FETCH: MemoryFetchHandler,
            EventType.USER_TRACKING: UserTrackingHandler,
            EventType.BROWSE_NEWS: BrowseNewsHandler,
            EventType.SELF_REFLECTION: SelfReflectionHandler,
            EventType.BROWSE_BOOKMARKS: BrowseBookmarksHandler,
            EventType.LISTEN_MUSIC: ListenMusicHandler,
        }

        handler_class = handler_map.get(event_type)
        if event_type == EventType.BROWSE_XIAOHONGSHU:
            from .xiaohongshu_handler import BrowseXiaohongshuHandler
            return BrowseXiaohongshuHandler(
                web_search_func=self.web_search_func,
                attachment_saver=self.attachment_saver,
            )
        if not handler_class:
            raise ValueError(f"未知事件类型: {event_type}")

        # 特殊处理需要额外参数的处理器
        if event_type == EventType.MEMORY_FETCH:
            return handler_class(
                call_llm_func=self.call_llm,
                get_memories_func=self.get_memories
            )

        if event_type == EventType.USER_TRACKING:
            return handler_class(
                call_llm_func=self.call_llm,
                tracking_service=self.tracking_service,
                on_notification=self.on_notification,
            )

        if event_type == EventType.BROWSE_NEWS:
            return handler_class(
                call_llm_func=self.call_llm,
                web_search_func=self.web_search_func,
            )

        if event_type == EventType.LISTEN_MUSIC:
            return handler_class(
                call_llm_func=self.call_llm,
                song_cache=self.song_cache,
                k_self_book=self.k_self_book,
                pro_llm_func=self.call_llm,
                music_mcp_client=self.music_mcp_client,
            )

        return handler_class(call_llm_func=self.call_llm)
