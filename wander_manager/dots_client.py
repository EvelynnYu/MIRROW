"""Minimal, secret-safe client for the Dots multimodal model API."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .flash_structured import parse_json_object


DEFAULT_DOTS_BASE_URL = "https://note3-prev-api.askdiandian.com/v1"
DEFAULT_DOTS_MODEL = "dots3-note-prev"


@dataclass(frozen=True)
class DotsJsonResult:
    parsed: Optional[dict[str, Any]] = None
    raw_content: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = DEFAULT_DOTS_MODEL
    finish_reason: str = ""
    duration_ms: int = 0
    status: str = "error"
    error: str = ""


class DotsClient:
    """Call Dots without ever returning credentials or request headers."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout_seconds: float = 90.0):
        self._api_key = (api_key if api_key is not None else os.getenv("DOTS_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("DOTS_API_BASE_URL") or DEFAULT_DOTS_BASE_URL).rstrip("/")
        self.model = (model or os.getenv("DOTS_MODEL") or DEFAULT_DOTS_MODEL).strip()
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def complete_json(self, *, system: str, user: str,
                            max_tokens: int = 2048,
                            image_urls: list[str] | None = None,
                            video_urls: list[str] | None = None) -> DotsJsonResult:
        started = time.perf_counter()
        if not self._api_key:
            return DotsJsonResult(model=self.model, status="configuration_error",
                                  error="api_key_not_configured")
        user_content: str | list[dict[str, Any]] = user
        media_parts: list[dict[str, Any]] = []
        for url in (image_urls or [])[:4]:
            if str(url).startswith(("http://", "https://")):
                media_parts.append({"type": "image_url", "image_url": {"url": str(url)}})
        for url in (video_urls or [])[:1]:
            if str(url).startswith(("http://", "https://")):
                media_parts.append({"type": "video_url", "video_url": {"url": str(url)}})
        if media_parts:
            user_content = [{"type": "text", "text": user}, *media_parts]

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": max(128, min(int(max_tokens), 8192)),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, proxy=None,
                                         trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"api-key": self._api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            return self._error_result(started, f"http_{exc.response.status_code}")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return self._error_result(started, type(exc).__name__)
        except Exception as exc:
            return self._error_result(started, type(exc).__name__)

        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices or not isinstance(choices[0], dict):
            return self._error_result(started, "response_has_no_choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        raw = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        parsed = parse_json_object(raw)
        status = "ok" if parsed is not None else "json_parse_error"
        usage = body.get("usage") if isinstance(body, dict) else None
        return DotsJsonResult(
            parsed=parsed, raw_content=raw, reasoning=reasoning,
            usage=usage if isinstance(usage, dict) else {},
            model=str(body.get("model") or self.model),
            finish_reason=str(choice.get("finish_reason") or ""),
            duration_ms=int((time.perf_counter() - started) * 1000), status=status,
            error="" if status == "ok" else "content_is_not_json_object",
        )

    def _error_result(self, started: float, error: str) -> DotsJsonResult:
        return DotsJsonResult(model=self.model,
                              duration_ms=int((time.perf_counter() - started) * 1000),
                              status="error", error=error)
