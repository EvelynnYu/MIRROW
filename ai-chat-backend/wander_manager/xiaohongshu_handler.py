"""Single dedicated-device XHS material producer for chat and Wander."""
from __future__ import annotations
import asyncio
import inspect
import json
import re
from datetime import datetime
from typing import Any
from .event_handlers import BaseEventHandler
from .xiaohongshu_adb import AdbXiaohongshuSource
from .xhs_request import compile_task, validate_task

class BrowseXiaohongshuHandler(BaseEventHandler):
    def __init__(self, *, device_source=None, vision_func=None):
        super().__init__(None)
        self._device_source = device_source or AdbXiaohongshuSource()
        self._vision_func = vision_func

    async def handle(self, event):
        payload = await self.fetch_one_post(activity_reason=str(event.details.get('reason') or '首页推荐'))
        event.details.update(payload)
        event.description = '刷小红书'
        event.process_log = '小红书读取：' + str(payload.get('status'))
        return event

    async def fetch_one_post(self, *, exclude_ids=None, activity_reason='',
                             task=None, cursor=None, frames_out=None):
        task = validate_task(task) if task else await compile_task(activity_reason or '浏览首页推荐')
        if task['operation'] == 'continue':
            return {'status': 'resume_requires_runtime'}
        try:
            raw = await self._device_source.fetch_task(task=task, cursor=cursor or {},
                exclude_ids=set(exclude_ids or ()))
        except Exception as exc:
            return {'status': 'device_error', 'error': type(exc).__name__}
        if not isinstance(raw, dict):
            return {'status': 'invalid_device_result'}
        result = dict(raw)
        frames = result.pop('frames', [])
        screenshot = result.pop('screenshot_base64', '')
        result.update(source_provider='xiaohongshu_android_adb',
                      execution_mode='dedicated_device_direct', material_version=1,
                      task_spec=task, retrieved_at=datetime.now().isoformat())
        if result.get('status') != 'success':
            return result
        capture = result.get('gallery_capture') or {}
        if not frames and screenshot and not capture.get('declared_total'):
            frames = [{'label': 'visible_screen', 'screenshot_base64': screenshot}]
        if capture.get('declared_total'):
            frames = [frame for frame in frames if frame.get('page') in capture.get('captured_pages', [])]
        if not frames or not result.get('source_id'):
            return dict(result, status='insufficient_device_evidence')
        if frames_out is not None:
            from .xhs_gallery_review import prepare_pages
            pages = await asyncio.to_thread(prepare_pages, frames)
            frames_out.extend(pages)
            ui_text = str(result.pop('ui_text', '') or '')
            result.update(content_summary=result.get('article_text') or result.get('title', ''),
                evidence_excerpt=re.sub(r"\s+", " ", ui_text).strip()[:240], source_ui_text=ui_text[:3000],
                gallery_labels=[label for label, _ in pages], image_observations=[],
                media_observation='native_screens_for_review')
            result.setdefault('coverage', {'text':'visible_excerpt','images':'visible_screen','video':'not_played'})
            result['coverage'] = {**result['coverage'], 'image_analysis':'pending'}
            result.setdefault('limitations','图片覆盖范围以实际可见的图页为准。')
            return result
        observations, calls = [], []
        for frame in frames:
            vision, error = await self._run_device_vision(frame['screenshot_base64'],
                query=task['query'], ui_text=frame.get('ui_text', ''))
            if error:
                return dict(result, status='vision_unavailable', error=error)
            raw_content = self._vision_content(vision)
            parsed = self._parse_vision_json(raw_content)
            summary = str(parsed.get('summary') or raw_content).strip()
            if not summary:
                return dict(result, status='vision_empty')
            observations.append({'page': frame['label'], 'observation': summary[:4000],
                                 'omitted_chars': max(0, len(summary)-4000)})
            calls.extend(self._vision_audit(vision, raw_content)['calls'])
        ui_text = str(result.pop('ui_text', '') or '')
        result.update(content_summary='\n'.join(x['observation'] for x in observations)[:2000],
            image_observations=observations, evidence_excerpt=self._evidence_excerpt(ui_text),
            source_ui_text=ui_text[:3000], media_observation='android_screens_and_accessibility_text',
            agent_provider='flash-vision', agent_audit={'calls': calls})
        result.setdefault('coverage', {'text': 'visible_excerpt', 'images': 'visible_screen', 'video': 'not_played'})
        result.setdefault('limitations', '仅取得可见屏幕；未证明全文或视频完整读取。')
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
                from llm_client import call_vision_api
                vision_func = call_vision_api
            except Exception:
                return None, "vision_import_failed"

        prompt = (
            "你是只读的小红书屏幕理解器。只能依据这张真实的Android截图和随后给出的"
            "可访问UI文本描述当前帖子。不要补充截图/UI文本没有的信息，不要声称看完视频，"
            "不要执行或建议任何互动。严格输出JSON："
            '{"title":"可见标题","summary":"逐项客观描述当前页图片的文字与内容，尽量完整保留图片文字",'
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

