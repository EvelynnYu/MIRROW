"""Read-only Xiaohongshu browsing through a dedicated ADB phone.

The legacy public-card path remains available when a ``public_source`` is
explicitly injected (for deterministic tests and a controlled compatibility
fallback).  Production construction uses :class:`AdbXiaohongshuSource` first,
so a missing/disconnected phone is reported as a real skipped/failed node and
never silently represented as a successful browse.
"""

from __future__ import annotations

import re
import json
import inspect
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .xiaohongshu_adb import AdbXiaohongshuSource
from .dots_client import DotsClient, DotsJsonResult
from .event_handlers import BaseEventHandler
from .event_types import WanderEvent
from .xiaohongshu_public_source import PublicXiaohongshuSource
from .xhs_comment_delivery import prepare_comment_delivery


_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


class BrowseXiaohongshuHandler(BaseEventHandler):
    """One node means reading one evidence-backed real-screen result."""

    def __init__(self, web_search_func: Optional[Callable[[str], Awaitable[Any]]] = None,
                 dots_client: Optional[DotsClient] = None,
                 public_source: Optional[PublicXiaohongshuSource] = None,
                 device_source: Optional[AdbXiaohongshuSource] = None,
                 vision_func: Optional[Callable[..., Any]] = None,
                 attachment_saver: Optional[Callable[..., Any]] = None):
        super().__init__(None)
        self._web_search = web_search_func
        self._dots = dots_client or DotsClient()
        # Explicit public_source injection preserves the old deterministic
        # boundary.  With no source supplied, the dedicated ADB phone is the
        # production source; it is never replaced by a browser on failure.
        self._use_device = public_source is None
        self._device_source = device_source or AdbXiaohongshuSource()
        self._public_source = public_source
        self._vision_func = vision_func
        self._attachment_saver = attachment_saver
        self._recent_queries: list[str] = []

    async def handle(self, event: WanderEvent) -> WanderEvent:
        """Legacy-compatible boundary; v3 normally calls ``fetch_one_post``."""
        event.description = "刷小红书"
        payload = await self.fetch_one_post(
            activity_reason=str(event.details.get("reason") or ""),
        )
        event.details.update(payload)
        event.process_log = (
            f"浏览小红书：{payload.get('title') or payload.get('query')}"
            if payload.get("status") == "success"
            else f"小红书浏览未完成：{payload.get('status') or 'unknown'}"
        )
        return event

    async def fetch_one_post(self, *, exclude_ids: set[str] | None = None,
                             activity_reason: str = "",
                             comment_draft: str = "") -> dict[str, Any]:
        if self._use_device:
            result = await self._fetch_one_device_post(
                exclude_ids=exclude_ids,
                activity_reason=activity_reason,
                comment_draft=comment_draft,
            )
            # This label is deliberately explicit in runtime evidence.  It
            # tells the log that the autonomous node used AI's dedicated phone
            # directly, while leaving all device/login/risk gates truthful.
            result.setdefault("execution_mode", "dedicated_device_direct")
            return result

        if not self._dots.available:
            return {"status": "no_dots_key"}
        planned = await self._dots.complete_json(
            system=("你是只读的小红书浏览外包 Agent。你只决定搜索方向，不得声称已经看过帖子。"
                    "查询必须具体到主题或场景，禁止使用最近热门、生活分享、趣事等泛词。"
                    "只输出 JSON，不要输出解释。"),
            user=("为一次自主浏览选择一个简短、具体、适合小红书公开搜索的中文查询词。\n"
                  f"当前活动目标：{activity_reason or '没有特定目标，随便看看有趣的生活内容'}\n"
                  f"最近搜过：{'、'.join(self._recent_queries[-6:]) or '无'}\n"
                  '输出：{"query":"不超过30字","mode":"goal|wander","reason":"一句话"}'),
            max_tokens=512,
        )
        query = self._clean_query((planned.parsed or {}).get("query"))
        if planned.status != "ok" or not query:
            return self._failure("dots_plan_failed", planned=planned)

        mode = str((planned.parsed or {}).get("mode") or "wander")
        source_query = query if mode == "goal" and activity_reason else ""
        source = await self._public_source.fetch_cards(query=source_query, limit=16)
        candidates = [card for card in source.cards
                      if card.source_id not in (exclude_ids or set())]
        if not candidates:
            # Keyword search is more likely to require login than the public feed.
            # Falling back keeps idle browsing useful without inventing goal evidence.
            if source_query:
                source = await self._public_source.fetch_cards(limit=16)
                candidates = [card for card in source.cards
                              if card.source_id not in (exclude_ids or set())]
            if not candidates:
                return self._failure(source.status or "no_public_cards", planned=planned,
                                     error=source.error)

        card = candidates[0]
        evidence = card.text
        read = await self._dots.complete_json(
            system=("你是只读的小红书内容阅读外包 Agent。只能依据给出的真实公开帖子卡片文字"
                    "和封面图观察内容；证据没有的信息必须写未知，禁止把视频封面说成看完视频，"
                    "禁止凭训练记忆补全。只输出 JSON。"),
            user=(f"浏览目标：{activity_reason or '无特定目标，随便看看'}\n"
                  f"帖子卡片文字：\n{evidence[:3000]}\n\n"
                  "请整理你实际看见的内容。输出："
                  '{"title":"卡片标题","summary":"仅基于文字和封面的摘要",'
                  '"reflection":"看完后的简短感想或没有感想",'
                  '"evidence_excerpt":"来自卡片文字的短摘录",'
                  '"media_kind":"text|image_cover|video_cover",'
                  '"limitations":"没有读到什么","evidence_sufficient":true或false}。'),
            max_tokens=1536,
            image_urls=[card.cover_url] if card.cover_url else None,
        )
        data = read.parsed or {}
        summary = str(data.get("summary") or "").strip()
        if read.status != "ok" or not summary:
            return self._failure("dots_read_failed", planned=planned, read=read)
        excerpt = str(data.get("evidence_excerpt") or "").strip()
        title = next((line.strip() for line in card.text.splitlines() if line.strip()), "")
        sufficient = data.get("evidence_sufficient") is True
        normalized_evidence = re.sub(r"\s+", " ", card.text).strip()
        normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip()
        if (not sufficient or not normalized_excerpt or not title
                or normalized_excerpt not in normalized_evidence):
            return self._failure("insufficient_evidence", planned=planned, read=read)

        self._recent_queries.append(query)
        self._recent_queries = self._recent_queries[-20:]
        media_kind = str(data.get("media_kind") or "unknown").lower()
        allowed_media = {"text", "image_cover", "video_cover"}
        if media_kind not in allowed_media:
            media_kind = "video_cover" if card.has_video_cover else (
                "image_cover" if card.cover_url else "text")
        observation = "text_only"
        if card.cover_url:
            observation = "video_cover_and_text" if card.has_video_cover else "image_cover_and_text"
        return {
            "status": "success", "source_id": card.source_id, "query": query,
            "browse_mode": mode,
            "source_url": card.url,
            "title": title[:200],
            "content_summary": summary[:2000],
            "reflection": str(data.get("reflection") or "没有感想").strip()[:1000],
            "evidence_excerpt": excerpt[:1000],
            "source_card_text": card.text[:2000],
            "cover_url": card.cover_url,
            "media_kind": media_kind, "media_observation": observation,
            "limitations": str(data.get("limitations") or "仅浏览公开卡片，未进入原帖正文").strip()[:500],
            "retrieved_at": datetime.now().isoformat(),
            "source_provider": "xiaohongshu_public_web", "agent_provider": "dots",
            "agent_audit": self._audit(planned, read),
        }

    async def _fetch_one_device_post(
        self,
        *,
        exclude_ids: set[str] | None = None,
        activity_reason: str = "",
        comment_draft: str = "",
    ) -> dict[str, Any]:
        """Acquire one post from the real phone and summarize its screenshot.

        The adapter owns all device actions.  This method only turns the
        returned, bounded evidence into the event material contract.  The raw
        screenshot is removed before the result is persisted in the runtime
        node; only a hash and capture metadata remain.
        """

        query = self._explicit_target_query(activity_reason)
        try:
            raw = await self._device_source.fetch_one(
                query=query,
                exclude_ids=set(exclude_ids or set()),
                capture_home_screenshot=bool(str(comment_draft or "").strip()),
            )
        except Exception as exc:
            # Keep the node truthful and avoid leaking command details or a
            # device serial through exception text.
            return {
                "status": "device_error",
                "error": type(exc).__name__,
                "source_provider": "xiaohongshu_android_adb",
                "execution_mode": "dedicated_device_direct",
                "agent_audit": {"calls": []},
            }
        if not isinstance(raw, dict):
            return {
                "status": "device_error",
                "error": "invalid_device_result",
                "source_provider": "xiaohongshu_android_adb",
                "execution_mode": "dedicated_device_direct",
                "agent_audit": {"calls": []},
            }

        result = dict(raw)
        status = str(result.get("status") or "device_error")
        # Every gate/failure from the phone is returned as-is.  In particular,
        # battery_low, consent_required, login_required, and device disconnect
        # are not converted into a public-web success.
        if status != "success":
            result.pop("screenshot_base64", None)
            result["source_provider"] = "xiaohongshu_android_adb"
            result["execution_mode"] = "dedicated_device_direct"
            result.setdefault("agent_audit", {"calls": []})
            return result

        screenshot = str(result.pop("screenshot_base64", "") or "")
        home_screenshot = str(result.pop("home_screenshot_base64", "") or "")
        ui_text = re.sub(r"\s+", " ", str(result.get("ui_text") or "")).strip()
        source_id = str(result.get("source_id") or "").strip()
        if not screenshot or not ui_text or not source_id:
            result["status"] = "insufficient_device_evidence"
            result["error"] = "screenshot_ui_or_source_id_missing"
            result["source_provider"] = "xiaohongshu_android_adb"
            result["execution_mode"] = "dedicated_device_direct"
            result["agent_audit"] = {"calls": []}
            return result

        vision, vision_error = await self._run_device_vision(
            screenshot,
            query=query,
            ui_text=ui_text,
        )
        if vision_error:
            result["status"] = "vision_unavailable"
            result["error"] = vision_error
            result["source_provider"] = "xiaohongshu_android_adb"
            result["execution_mode"] = "dedicated_device_direct"
            result["agent_audit"] = {"calls": []}
            return result

        raw_content = self._vision_content(vision)
        parsed = self._parse_vision_json(raw_content)
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            # A plain-text answer is accepted only as the visual model's
            # bounded description.  Empty output is not material.
            summary = raw_content.strip()
        if not summary:
            result["status"] = "vision_empty"
            result["error"] = "vision_returned_no_summary"
            result["source_provider"] = "xiaohongshu_android_adb"
            result["execution_mode"] = "dedicated_device_direct"
            result["agent_audit"] = {"calls": []}
            return result

        # Evidence excerpt is deterministic screen text, not a model-created
        # quote.  This guarantees a future reviewer can find it in the saved
        # UI material even if the model returns malformed JSON.
        excerpt = self._evidence_excerpt(ui_text)
        title = str(result.get("title") or parsed.get("title") or excerpt[:100]).strip()
        result.update({
            "status": "success",
            "query": query,
            "title": title[:200],
            "content_summary": summary[:2000],
            "reflection": str(parsed.get("reflection") or "没有特别感想").strip()[:1000],
            "evidence_excerpt": excerpt[:1000],
            "source_ui_text": ui_text[:3000],
            "media_kind": "screen",
            "media_observation": "android_screenshot_and_ui_text",
            "limitations": (
                "只读取小红书当前可见真实截图和UI文本；未读取Cookie或App私有数据，"
                "未播放视频完整内容，也未执行点赞、收藏、关注、评论或发帖。"
            ),
            "source_provider": "xiaohongshu_android_adb",
            "agent_provider": "flash-vision",
            "execution_mode": "dedicated_device_direct",
            "retrieved_at": datetime.now().isoformat(),
            "agent_audit": self._vision_audit(vision, raw_content),
        })
        if str(comment_draft or "").strip():
            bounds = result.get("details", {}).get("selected_bounds") if isinstance(result.get("details"), dict) else None
            if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                bounds = tuple(int(value) for value in bounds)
            else:
                bounds = None
            result["comment_delivery"] = await prepare_comment_delivery(
                post_id=source_id, comment_draft=comment_draft,
                home_screenshot_base64=home_screenshot, crop_bounds=bounds,
                attachment_saver=self._attachment_saver,
            )
        self._recent_queries.append(query)
        self._recent_queries = self._recent_queries[-20:]
        return result

    async def _run_device_vision(
        self,
        screenshot_base64: str,
        *,
        query: str,
        ui_text: str,
    ) -> tuple[Any, str]:
        vision_func = self._vision_func
        if vision_func is None:
            try:
                from mirrow_core.llm_runtime import call_vision_api
                vision_func = call_vision_api
            except Exception:
                return None, "vision_import_failed"

        prompt = (
            "你是只读的小红书屏幕理解器。只能依据这张真实的Android截图和随后给出的"
            "可访问UI文本描述当前帖子。不要补充截图/UI文本没有的信息，不要声称看完视频，"
            "不要执行或建议任何互动。严格输出JSON："
            '{"title":"可见标题","summary":"仅基于当前画面的简短摘要",'
            '"reflection":"一句简短感想，没有则写没有特别感想",'
            '"limitations":"当前画面没有覆盖的内容"}。\n'
            f"导航目标：{query or '首页推荐流'}\n"
            f"可访问UI文本：{ui_text[:5000]}"
        )
        try:
            value = vision_func(
                screenshot_base64,
                prompt,
                model="flash-vision",
                max_tokens=1024,
            )
            if inspect.isawaitable(value):
                value = await value
            return value, ""
        except TypeError as exc:
            # Support a narrow two-argument test double/legacy adapter, but do
            # not retry a real vision failure.
            message = str(exc).lower()
            if "unexpected keyword" not in message and "positional argument" not in message:
                return None, type(exc).__name__
            try:
                value = vision_func(screenshot_base64, prompt)
                if inspect.isawaitable(value):
                    value = await value
                return value, ""
            except Exception as second_exc:
                return None, type(second_exc).__name__
        except Exception as exc:
            return None, type(exc).__name__

    @staticmethod
    def _vision_content(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("content") or value.get("text") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _parse_vision_json(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        # Models occasionally add one sentence around the JSON object.  Parse
        # only the first balanced-looking object; never infer fields from prose.
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start:end + 1])
                return value if isinstance(value, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
        return {}

    @staticmethod
    def _evidence_excerpt(ui_text: str) -> str:
        compact = re.sub(r"\s+", " ", str(ui_text or "")).strip()
        return compact[:240]

    @staticmethod
    def _vision_audit(value: Any, raw_content: str) -> dict[str, Any]:
        if isinstance(value, dict):
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            model = str(value.get("model") or "flash-vision")
            reasoning = value.get("reasoning") or value.get("reasoning_content") or ""
        else:
            usage, model, reasoning = {}, "flash-vision", ""
        return {"calls": [{
            "model": model,
            "status": "ok",
            "error": "",
            "finish_reason": "",
            "duration_ms": 0,
            "usage": usage,
            "raw_content": str(raw_content or "")[:4000],
            "reasoning": reasoning,
        }]}

    @staticmethod
    def _explicit_target_query(activity_reason: str) -> str:
        value = re.sub(r"\s+", " ", str(activity_reason or "")).strip()
        if not value:
            return ""
        generic = {
            "看看", "随便看看", "随便看看有趣的生活内容", "刷小红书", "看小红书",
            "首页推荐", "没有特定目标，随便看看有趣的生活内容",
        }
        if value in generic:
            return ""
        value = re.sub(r"^(?:去|想|先)?(?:看看|找找|搜索|搜一下|浏览|阅读)\s*", "", value)
        value = value.strip(" ：:，,。！!~～")
        return "" if value in generic or len(value) < 2 else value[:60]

    @staticmethod
    def _clean_query(value: Any) -> str:
        query = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
        return re.sub(r"\s+", " ", query)[:30]

    @staticmethod
    def _xiaohongshu_urls(text: str) -> list[str]:
        urls: list[str] = []
        for raw in _URL_RE.findall(text):
            url = raw.rstrip(").,，。；;]}")
            try:
                host = (urlparse(url).hostname or "").lower()
            except ValueError:
                continue
            if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
                if url not in urls:
                    urls.append(url)
        return urls

    @classmethod
    def _failure(cls, status: str, *, planned: DotsJsonResult | None = None,
                 read: DotsJsonResult | None = None, error: str = "") -> dict[str, Any]:
        return {"status": status, "error": error or status,
                "agent_audit": cls._audit(planned, read)}

    @staticmethod
    def _audit(*calls: DotsJsonResult | None) -> dict[str, Any]:
        items = []
        for call in calls:
            if call is not None:
                items.append({
                    "model": call.model, "status": call.status, "error": call.error,
                    "finish_reason": call.finish_reason, "duration_ms": call.duration_ms,
                    "usage": call.usage, "raw_content": call.raw_content,
                    "reasoning": call.reasoning,
                })
        return {"calls": items}
