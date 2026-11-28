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

# 单次调用允许的尝试次数（含首次）。空内容 / JSON 解析失败 / 传输异常三者
# 共用同一个上限，保证"最多打几次"是一处可审计的值。
# 两次总调用是既有契约（由 test_two_bad_json_responses_are_terminal_after_two_requests
# 锁定），本次不放宽次数，只补上此前缺失的退避：空内容原本是"立即重试一次"，
# 紧接着打同一个瞬时条件，等于没重试。
_MAX_ATTEMPTS = 2


@dataclass
class FlashJsonResult:
    """可审计的结构化 Flash 结果，永不包含连接凭据。"""
    parsed: Optional[Dict[str, Any]] = None
    raw_content: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 16384
    duration_ms: int = 0
    finish_reason: str = ""
    status: str = "error"
    error: str = ""
    http_status: int = 0
    route: str = ""
    request_messages: list[dict[str, str]] = field(default_factory=list)
    routing_managed: bool = False
    task_id: str = ""


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
    enable_thinking: bool | None = None,
    request_timeout: httpx.Timeout | None = None,
    route_label: str = "",
    result_validator=None,
    image_inputs: list[str] | None = None,
    gallery_images: list[str] | None = None,
) -> FlashJsonResult:
    """调用 Flash 并保留模型实际返回的审计信息，最多尝试 _MAX_ATTEMPTS 次。"""
    started = time.perf_counter()
    from context_builder.channel_projection import copy_messages, prepare
    safe_messages = copy_messages(messages)
    route = str(route_label or "structured_unspecified").strip()[:80]
    managed_route = route
    gallery_kw = {}
    if gallery_images:
        if route not in {'wander_node_review', 'wander_xhs_interactions'} or image_inputs or any((api_key, api_url, model_name)):
            raise ValueError('invalid_gallery_route')
        gallery_kw['image_batch'] = gallery_images
    if not image_inputs:
        from api_sources.managed import call_managed
        managed = await call_managed(managed_route, messages, temperature=temperature, max_tokens=max_tokens,
            enable_thinking=enable_thinking, request_timeout=request_timeout,
            result_validator=result_validator, json_mode=json_mode, **gallery_kw)
        if managed is not None:
            return managed
    from api_sources.catalog import NODE_LABELS, LEGACY_UNMANAGED_NODES
    if not image_inputs and not any((api_key, api_url, model_name)) and route in NODE_LABELS and route not in LEGACY_UNMANAGED_NODES and route != "structured_unspecified":
        from api_sources.runner import run_structured
        return await run_structured(messages, temperature=temperature, max_tokens=max_tokens,
            route=route, enable_thinking=enable_thinking, request_timeout=request_timeout,
            result_validator=result_validator, json_mode=json_mode, **gallery_kw)
    if gallery_images:
        raise ValueError('gallery_route_unavailable')
    result = FlashJsonResult(model=model_name, temperature=temperature, max_tokens=max_tokens,
        route=route, request_messages=safe_messages)
    try:
        from llm_client import (DEEPSEEK_FLASH_API_KEY, DEEPSEEK_FLASH_API_URL, DEEPSEEK_FLASH_MODEL,
            DEEPSEEK_API_KEY, DEEPSEEK_API_URL,
            SILICONFLOW_STRUCTURED_API_KEY, SILICONFLOW_STRUCTURED_API_URL,
            SILICONFLOW_STRUCTURED_MODEL,
            http_client, llm_semaphore, LLM_TIMEOUT, LLM_LIMITS)
    except Exception as exc:
        result.status, result.error = "configuration_error", type(exc).__name__
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    # Explicit provider triples remain an escape hatch for endpoint-owning
    # callers. Ordinary structured and lounge-internal calls use the dedicated
    # SiliconFlow lane when configured.
    explicit_route = any((api_key, api_url, model_name))
    if explicit_route:
        _api_key = api_key or DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY
        _api_url = api_url or DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL
        _model = model_name or DEEPSEEK_FLASH_MODEL
    elif SILICONFLOW_STRUCTURED_API_KEY:
        _api_key = SILICONFLOW_STRUCTURED_API_KEY
        _api_url = SILICONFLOW_STRUCTURED_API_URL
        _model = SILICONFLOW_STRUCTURED_MODEL
    else:
        _api_key = DEEPSEEK_FLASH_API_KEY or DEEPSEEK_API_KEY
        _api_url = DEEPSEEK_FLASH_API_URL or DEEPSEEK_API_URL
        _model = DEEPSEEK_FLASH_MODEL
    _provider = "siliconflow" if "siliconflow" in (_api_url or "").lower() else "deepseek"
    result.provider = _provider
    result.model = _model
    if not _api_key:
        logger.error("flash_structured: API key not configured")
        result.status, result.error = "configuration_error", "api_key_not_configured"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    headers = {"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"}
    payload = {"model": _model, "messages": safe_messages, "stream": False, "temperature": temperature, "max_tokens": max_tokens}
    if image_inputs:
        # Explicit workshop review material, separate from the stable identity
        # prefix and text-only audit. Never stringify pixels as chat text.
        if not route.startswith('workshop_') or len(image_inputs) > 4 or any(
                not value.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')) or len(value) > 8000000
                for value in image_inputs):
            raise ValueError('invalid_workshop_image_input')
        payload['messages'] = [*safe_messages, {'role': 'user', 'content': [
            {'type': 'text', 'text': '【工作坊当前版本的实际视觉附件；不是人类伙伴的新发言】'},
            *[{'type': 'image_url', 'image_url': {'url': value}} for value in image_inputs]]}]
    from api_sources.model_context import with_current_model
    payload["messages"], _ = prepare(with_current_model(payload["messages"], _model))
    if json_mode and _provider == "siliconflow":
        # SiliconFlow's JSON mode makes the structured lane deterministic at
        # the transport boundary; the local parser/validator remains the
        # second line of defence for gateways or models that violate it.
        payload["response_format"] = {"type": "json_object"}
    if enable_thinking is not None:
        if _provider == "siliconflow":
            payload["enable_thinking"] = bool(enable_thinking)
        else:
            payload["thinking"] = {"type": "enabled" if enable_thinking else "disabled"}
    elif _provider == "siliconflow" and "qwen3.6-35b-a3b" in _model.lower():
        # Qwen3.6 defaults to thinking.  The shared structured lane is mostly
        # short JSON decisions, so keep it in fast non-thinking mode unless a
        # specialist caller (for example lounge summary) explicitly opts in.
        payload["enable_thinking"] = False
    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=LLM_TIMEOUT, limits=LLM_LIMITS, proxy=None, trust_env=False)
    fresh_client = None
    from api_sources import audit as api_audit
    import uuid
    audit_task_id = uuid.uuid4().hex
    audit_source = await asyncio.to_thread(api_audit.identify_source, _api_url, _model, _api_key)
    audit_enabled = route.startswith(("workshop_",))
    try:
        async with llm_semaphore:
            for attempt in range(_MAX_ATTEMPTS):
                retry_client = fresh_client or client
                audit_handle = None
                try:
                    if audit_enabled:
                        audit_handle = await asyncio.to_thread(api_audit.begin, audit_task_id, "expression", route, audit_source, attempt + 1)
                    response = await retry_client.post(_api_url, json=payload, headers=headers,
                        **({'timeout': request_timeout} if request_timeout is not None else {}))
                    result.http_status = response.status_code
                    response.raise_for_status()
                    body = response.json()
                    usage = body.get("usage") if isinstance(body, dict) else None
                    result.usage = usage if isinstance(usage, dict) else {}
                    if usage is not None:
                        try:
                            from token_tracker import log_token_usage
                            asyncio.create_task(log_token_usage(
                                f"flash_structured:{route}", _model, usage, provider=_provider,
                            ))
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
                    if message.get("refusal") or result.finish_reason in {"content_filter", "refusal"}:
                        result.status, result.error = "refusal", "model_refusal"
                        return result
                    if not result.raw_content.strip():
                        result.status, result.error = "empty_content", "model_returned_no_content"
                        # 空内容多为瞬时状态。此前是"立即重试一次"、没有退避：
                        # 紧接着打同一个瞬时条件，等于没重试（会客室曾两次都空）。
                        # 这里与传输异常对齐，退避后再试，并且给到三次机会。
                        if attempt < _MAX_ATTEMPTS - 1:
                            await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                            continue
                        return result
                    if not json_mode:
                        result.status = 'ok'
                        return result
                    result.parsed = parse_json_object(result.raw_content)
                    if result.parsed is None:
                        result.status, result.error = "json_parse_error", "content_is_not_json_object"
                        # A malformed response gets a fresh request, with the
                        # same bounded backoff as the other transient failures.
                        if attempt < _MAX_ATTEMPTS - 1:
                            await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                            continue
                    else:
                        result.status = "ok"
                        if result_validator:
                            try:
                                validation_error = result_validator(result)
                            except Exception:
                                validation_error = "node_validation_failed"
                            if validation_error:
                                result.status, result.error = "validation_error", validation_error
                    return result
                except asyncio.CancelledError:
                    result.status, result.error = "cancelled", "request_cancelled"
                    raise
                except (httpx.ConnectError, httpx.TimeoutException) as exc:
                    result.status, result.error = "connection_error", type(exc).__name__
                    if attempt >= _MAX_ATTEMPTS - 1:
                        return result
                    fresh_client = httpx.AsyncClient(timeout=LLM_TIMEOUT, limits=LLM_LIMITS, proxy=None, trust_env=False)
                    await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                except Exception as exc:
                    result.status, result.error = "error", type(exc).__name__
                    if attempt >= _MAX_ATTEMPTS - 1:
                        return result
                    await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                finally:
                    if audit_handle:
                        await asyncio.to_thread(api_audit.finish, audit_handle, result.status,
                            error_code=result.error, http_status=result.http_status, usage=result.usage)
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
    route_label: str = "structured_unspecified",
    result_validator=None,
) -> Optional[Dict[str, Any]]:
    """旧接口兼容层：仍只返回解析后的 dict 或 None。"""
    result = await call_flash_json_detailed(messages, temperature, max_tokens, api_key, api_url, model_name,
        route_label=route_label, result_validator=result_validator)
    return result.parsed if result.status == "ok" else None
