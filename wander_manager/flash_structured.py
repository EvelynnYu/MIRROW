# 0 温度结构化 Flash 调用辅助。
# 详细结果刻意不携带 API key、URL 或 headers；这些只存在于 HTTP 调用局部变量。

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class FlashJsonResult:
    """可审计的结构化 Flash 结果，永不包含连接凭据。"""
    parsed: Optional[Dict[str, Any]] = None
    raw_content: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 16384
    duration_ms: int = 0
    finish_reason: str = ""
    status: str = "error"
    error: str = ""
    http_status: int = 0
    request_messages: list[dict[str, str]] = field(default_factory=list)


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出文本里解析一个 JSON 对象，失败返回 ``None``。

    除了普通对象和 Markdown fenced JSON，一些 OpenAI-compatible 网关会
    把模型的 JSON 再编码成一个 JSON string（例如
    ``"{\\"has_topic\\": true}"``）。这种返回仍然是合法的结构化结果，
    因此只在解析出 *字符串* 时再解包一次；其它字符串、数组和坏 JSON
    仍然明确视为失败。
    """
    import re

    if not isinstance(text, str) or not text.strip():
        return None

    def _without_fence(value: str) -> str:
        value = value.strip()
        if "```json" in value:
            return value.split("```json", 1)[1].split("```", 1)[0].strip()
        if "```" in value:
            parts = value.split("```")
            if len(parts) >= 2:
                # fenced blocks can start with a language name such as json
                block = parts[1].strip()
                if block.lower().startswith("json"):
                    block = block[4:].lstrip()
                return block
        return value

    def _parse(value: str, depth: int = 0) -> Optional[Dict[str, Any]]:
        if depth > 2:
            return None
        value = _without_fence(value)

        # Try the complete value first. This is the important path for a
        # double-encoded JSON object: json.loads() returns the inner object as
        # a string, which is then parsed recursively below.
        try:
            decoded = json.loads(value)
            decoded_ok = True
        except (TypeError, json.JSONDecodeError):
            decoded = None
            decoded_ok = False
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, str):
            nested = _parse(decoded, depth + 1)
            if nested is not None:
                return nested
        if decoded_ok:
            # A valid top-level array/primitive is not an object. Do not
            # salvage an object nested inside an otherwise valid array.
            return None

        # Preserve the existing tolerance for a short explanation around an
        # object (and for harmless trailing commas/smart quotes).
        start, end = value.find("{"), value.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = value[start:end + 1]
        candidate = re.sub(r',\s*}', '}', candidate)
        candidate = re.sub(r',\s*]', ']', candidate)
        candidate = candidate.replace('“', '"').replace('”', '"')
        candidate = candidate.replace('‘', "'").replace('’', "'")
        if "\n" in candidate:
            candidate = ' '.join(line.strip() for line in candidate.split('\n'))
        try:
            decoded = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, str):
            return _parse(decoded, depth + 1)
        return None

    return _parse(text)


async def call_flash_json_detailed(
    messages: List[Dict[str, str]], temperature: float = 0.0, max_tokens: int = 16384,
    api_key: str = "", api_url: str = "", model_name: str = "",
    json_mode: bool = True,
) -> FlashJsonResult:
    """调用 Flash 并保留模型实际返回的审计信息，维持两次重试。"""
    started = time.perf_counter()
    safe_messages = [{"role": str(item.get("role", "")), "content": str(item.get("content", ""))} for item in messages]
    result = FlashJsonResult(model=model_name, temperature=temperature, max_tokens=max_tokens, request_messages=safe_messages)
    try:
        from mirrow_core.llm_runtime import (DEEPSEEK_FLASH_API_KEY, DEEPSEEK_FLASH_API_URL, DEEPSEEK_FLASH_MODEL,
            DEEPSEEK_API_KEY, DEEPSEEK_API_URL, http_client, llm_semaphore, LLM_TIMEOUT, LLM_LIMITS)
    except Exception as exc:
        result.status, result.error = "configuration_error", type(exc).__name__
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    _api_key = api_key or DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY
    _api_url = api_url or DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL
    _model = model_name or DEEPSEEK_FLASH_MODEL
    result.model = _model
    if not _api_key:
        logger.error("flash_structured: API key not configured")
        result.status, result.error = "configuration_error", "api_key_not_configured"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    headers = {"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"}
    payload = {"model": _model, "messages": safe_messages, "stream": False, "temperature": temperature, "max_tokens": max_tokens}
    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=LLM_TIMEOUT, limits=LLM_LIMITS, proxy=None, trust_env=False)
    fresh_client = None
    try:
        async with llm_semaphore:
            for attempt in range(2):
                retry_client = fresh_client or client
                try:
                    response = await retry_client.post(_api_url, json=payload, headers=headers)
                    result.http_status = response.status_code
                    response.raise_for_status()
                    body = response.json()
                    usage = body.get("usage") if isinstance(body, dict) else None
                    result.usage = usage if isinstance(usage, dict) else {}
                    if usage is not None:
                        try:
                            from mirrow_core.token_tracker import log_token_usage
                            asyncio.create_task(log_token_usage("flash_structured", _model, usage))
                        except Exception:
                            pass
                    choices = body.get("choices") if isinstance(body, dict) else None
                    if not choices:
                        result.status, result.error = "empty_choice", "response_has_no_choices"
                        return result
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    message = choice.get("message") or {}
                    result.raw_content = str(message.get("content") or "")
                    result.reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
                    result.finish_reason = str(choice.get("finish_reason") or "")
                    if not result.raw_content.strip():
                        result.status, result.error = "empty_content", "model_returned_no_content"
                        # Empty content is a structured-output failure, but
                        # it is safe to ask once more.  The second response is
                        # the terminal result; transport failures retain
                        # their existing retry semantics below.
                        if attempt == 0:
                            continue
                        return result
                    if not json_mode:
                        result.status = 'ok'
                        return result
                    result.parsed = parse_json_object(result.raw_content)
                    if result.parsed is None:
                        result.status, result.error = "json_parse_error", "content_is_not_json_object"
                        # A malformed first response gets one fresh request.
                        # Do not loop beyond the documented two total calls.
                        if attempt == 0:
                            continue
                    else:
                        result.status = "ok"
                    return result
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    result.status, result.error = "connection_error", type(exc).__name__
                    if attempt == 1:
                        return result
                    fresh_client = httpx.AsyncClient(timeout=LLM_TIMEOUT, limits=LLM_LIMITS, proxy=None, trust_env=False)
                    await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                except Exception as exc:
                    result.status, result.error = "error", type(exc).__name__
                    if attempt == 1:
                        return result
                    await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
    finally:
        if fresh_client is not None:
            await fresh_client.aclose()
        if owned_client:
            await client.aclose()
        result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


async def call_flash_json(
    messages: List[Dict[str, str]], temperature: float = 0.0, max_tokens: int = 16384,
    api_key: str = "", api_url: str = "", model_name: str = "",
) -> Optional[Dict[str, Any]]:
    """旧接口兼容层：仍只返回解析后的 dict 或 None。"""
    result = await call_flash_json_detailed(messages, temperature, max_tokens, api_key, api_url, model_name)
    return result.parsed if result.status == "ok" else None
