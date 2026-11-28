"""Ordered, finite model attempts before any business side effect is committed."""
from __future__ import annotations

import asyncio
import copy
import re
import time
import uuid

import httpx

from . import audit
from .adapters import build_request, extract_response
from .catalog import group_for
from .config import ConfigError, resolve_chain
from .scope import routing_scope


def envelope_error(route: str, value: dict) -> str | None:
    """Only hard output contracts; no inference about AI's decisions or feelings."""
    if route == 'wander_plan' and value.get('action') == 'defer':
        from wander_manager.timing_policy import deferred_wait
        return None if deferred_wait(value) is not None else 'invalid_plan_envelope'
    if route == "wander_plan":
        if not isinstance(value.get("activities"), list) or not value["activities"]:
            return "invalid_plan_envelope"
    elif route == "wander_node_review":
        if not any(key in value for key in ("continue_activity", "reflection")):
            return "invalid_review_envelope"
    elif route == "wander_settlement":
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            return "invalid_settlement_envelope"
    elif route == "wander_timing_review":
        raw_continue = value.get("continue_activity")
        if not isinstance(raw_continue, (bool, str, int, float)):
            return "invalid_timing_envelope"
        from wander_manager.runtime_decision_adapters import _as_bool
        if _as_bool(raw_continue):
            minutes = value.get("extend_minutes")
            if isinstance(minutes, bool):
                return "invalid_extension"
            try:
                if int(minutes) <= 0:
                    return "invalid_extension"
            except (TypeError, ValueError, OverflowError):
                return "invalid_extension"
    elif route == "sentinel_first_layer":
        if not isinstance(value.get("observation"), str) or not value["observation"].strip():
            return "invalid_observation"
        if "should_push" in value and not isinstance(value["should_push"], (bool, str)):
            return "invalid_push_decision"
    elif route == "wander_game_move":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            return "invalid_move_reason"
    elif route == "habit_radar_shadow":
        if not isinstance(value.get("signals"), list) or len(value["signals"]) > 80:
            return "invalid_signals_envelope"
    elif route == "behavior_tool_compiler":
        if "tool_calls" not in value:
            return "invalid_tool_calls"
        calls = value.get("tool_calls")
        if calls is not None and (not isinstance(calls, list) or any(not isinstance(item, dict) for item in calls)):
            return "invalid_tool_calls"
    elif route == "ambient_listening":
        if not isinstance(value.get("participate"), bool):
            return "invalid_participation"
        if value["participate"] and any(not isinstance(value.get(key), str) or not value[key].strip() for key in ("summary", "reason")):
            return "invalid_participation_detail"
    return None


async def run_structured_once(messages: list, *, temperature: float, max_tokens: int, route: str,
                         enable_thinking: bool | None, request_timeout=None, result_validator=None,
                         sources: list | None = None, timeout_seconds: int | None = None,
                         audit_path=None, json_mode: bool = True, task_id: str | None = None,
                         response_format: dict | None = None, request_projector=None, inspect_provider=False,
                         stop_on_output_error=False, image_batch=None):
    from wander_manager.flash_structured import FlashJsonResult, parse_json_object
    from llm_client import http_client, llm_semaphore, LLM_LIMITS

    task_id = task_id or uuid.uuid4().hex
    started = time.perf_counter()
    result = FlashJsonResult(route=route, request_messages=copy.deepcopy(messages), routing_managed=True, task_id=task_id)
    result.request_dispatched = False
    result.status, result.error = 'configuration_error', 'no_available_source'
    try:
        if sources is None:
            sources, configured_timeout = await asyncio.to_thread(resolve_chain, route)
        else:
            configured_timeout = timeout_seconds or 90
    except ConfigError:
        result.status, result.error = "configuration_error", "invalid_routing_config"
        return result
    if not sources:
        result.status, result.error = "configuration_error", "no_enabled_source"
        return result
    timeout = request_timeout or httpx.Timeout(timeout_seconds or configured_timeout, connect=10)
    if not isinstance(timeout, httpx.Timeout):
        timeout = httpx.Timeout(timeout)
    owned = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout, limits=LLM_LIMITS, proxy=None, trust_env=False, follow_redirects=False)
    # Per-source read budgets plus a finite overall chain budget. Never repeat a
    # source here: one configured position means one actual request.
    per_source = max(timeout_seconds or configured_timeout,
                     sum(value or 0 for value in (timeout.connect, timeout.read, timeout.write, timeout.pool)))
    budget = per_source * len(sources) + 10
    overall_timeout = asyncio.timeout(budget)
    request_dispatched = False
    try:
        async with llm_semaphore, overall_timeout:
            for source in sources:
                from .availability import readiness
                if not readiness(source, terminal=source.get('stable', False))['eligible']:
                    continue
                station, model = source["station"], source["model"]
                result = FlashJsonResult(route=route, request_messages=copy.deepcopy(messages), routing_managed=True,
                                         task_id=task_id, provider=station["id"], model=model["model"],
                                         temperature=temperature, max_tokens=max_tokens)
                metadata = {"station_id": station["id"], "station_name": station["name"], "source_id": model["id"], "model": model["model"]}
                handle = None
                inspection_handle = None
                projection = None
                payload = None
                extracted = None
                provider_started_at = audit.now_iso()
                status, error, http_status, trace, usage = "configuration_error", "api_key_not_configured", 0, "", {}
                error_code = ''
                try:
                    if not source["api_key"]:
                        result.status, result.error = status, error
                        # Missing/disabled credentials are visible in settings;
                        # no request was made, so no API call is counted.
                        continue
                    from .model_context import with_current_model
                    call_messages = with_current_model(messages, model['model'])
                    from .structured_output import prompt_schema_messages
                    call_messages = prompt_schema_messages(source, call_messages,
                        json_mode=json_mode, response_format=response_format)
                    result.request_messages = copy.deepcopy(call_messages)
                    if request_projector:
                        wire_messages = request_projector(source, copy.deepcopy(call_messages))
                    else:
                        from context_builder.channel_projection import prepare
                        wire_messages = prepare(call_messages)
                    if isinstance(wire_messages,tuple):
                        wire_messages,projection=wire_messages
                    url, payload, headers = build_request(source, wire_messages, temperature=temperature, max_tokens=max_tokens,
                                                          json_mode=json_mode, enable_thinking=enable_thinking, response_format=response_format)
                    if projection and str(projection.get('variant', '')).startswith('channel_'):
                        from context_builder.channel_projection import prepare_wire
                        from .observability import wire_messages as actual_wire
                        proven, projection = prepare_wire(call_messages, {
                            'protocol': station['protocol'], 'profile': model['profile'],
                            'system_role': model['system_role'], 'json_mode': json_mode})
                        if proven != actual_wire(payload, []):
                            raise ValueError('channel_wire_projection_mismatch')
                    from .image_batch import attach
                    dispatch_payload = attach(payload, station["protocol"], image_batch) if image_batch else payload
                    headers["X-Trace-Id"] = uuid.uuid4().hex
                    handle = await asyncio.to_thread(audit.begin, task_id, "test" if route == "connection_test" else group_for(route), route, metadata, source["ordinal"], **({"path": audit_path} if audit_path else {}))
                    if inspect_provider:
                        from . import observability
                        inspection_handle=observability.begin(payload,call_messages,model=model["model"],route=route,request_id=handle["id"],projection=projection)
                    request_dispatched = True
                    response = await client.post(url, json=dispatch_payload, headers=headers, timeout=timeout, follow_redirects=False)
                    http_status = response.status_code
                    trace = response.headers.get("x-siliconcloud-trace-id") or response.headers.get("x-request-id") or headers["X-Trace-Id"]
                    if response.status_code >= 400:
                        try:
                            body_error = response.json().get("error") or {}
                            error_code = body_error.get("code") if isinstance(body_error, dict) else ""
                        except Exception:
                            error_code = ""
                        if error_code in {"content_filter", "content_policy_violation", "safety_violation"}:
                            status, error = "refusal", "provider_refusal"
                            result.status, result.error = status, error
                            return result
                    response.raise_for_status()
                    extracted = extract_response(response.json(), station["protocol"])
                    usage = extracted.get("usage") if isinstance(extracted.get("usage"), dict) else {}
                    result.usage = usage
                    result.raw_content = extracted.get("content") or ""
                    result.reasoning = extracted.get("reasoning") or ""
                    result.finish_reason = extracted.get("finish_reason") or ""
                    result.model_source = {"station_id": station["id"], "station_name": station["name"], "source_id": model["id"],
                                           "model": model["model"], "model_label": model["label"], "ordinal": source["ordinal"]}
                    if extracted.get("refusal"):
                        status, error = "refusal", "model_refusal"
                    elif not result.raw_content.strip():
                        status, error = "empty_content", "model_returned_no_content"
                    elif result.finish_reason in {"length", "MAX_TOKENS", "incomplete"}:
                        status, error = "validation_error", "output_incomplete"
                    else:
                        result.parsed = parse_json_object(result.raw_content) if json_mode else None
                        if json_mode and result.parsed is None:
                            status, error = "json_parse_error", "content_is_not_json_object"
                        else:
                            status, error = "ok", ""
                            result.status = "ok"
                            error = (envelope_error(route, result.parsed) or "") if json_mode else ""
                            if not error and result_validator:
                                try:
                                    error = result_validator(result) or ""
                                    if not isinstance(error, str):
                                        error = "node_validation_failed"
                                except Exception:
                                    error = "node_validation_failed"
                            if error:
                                status = "validation_error"
                    result.status, result.error = status, error
                    if status in {"ok", "refusal"}:
                        return result
                    if stop_on_output_error and status in {'json_parse_error', 'validation_error', 'empty_content'}:
                        return result
                except asyncio.CancelledError:
                    status, error = ("timeout", "chain_timeout") if overall_timeout.expired() else ("cancelled", "request_cancelled")
                    raise
                except httpx.TimeoutException:
                    status, error = "timeout", "request_timeout"
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    status, error = "http_error", "model_not_found" if error_code in {'model_not_found', 'invalid_model', 'model_not_available'} else "rate_limited" if code == 429 else "authentication_failed" if code in {401, 403} else "request_format_error" if code in {400, 422} else "model_not_found" if code == 404 else "upstream_http_error"
                except httpx.RequestError:
                    status, error = "connection_error", "connection_failed"
                except ValueError as exc:
                    status, error = "validation_error", str(exc) if re.fullmatch(r"[a-z_]{1,80}", str(exc)) else "invalid_response"
                except Exception:
                    status, error = "error", "unexpected_response"
                finally:
                    from .availability import record_outcome
                    record_outcome(source, status, http_status=404 if error == 'model_not_found' else http_status)
                    result.status, result.error, result.http_status = status, error, http_status
                    result.duration_ms = round((time.perf_counter()-started)*1000)
                    if handle:
                        from .response_diagnostics import counters
                        await asyncio.to_thread(audit.finish, handle, status, error_code=error, http_status=http_status, trace_id=trace, usage=usage,
                                                response_metadata=counters(payload or {}, extracted))
                    if inspect_provider:
                        from . import observability
                        observability.finish(inspection_handle,status)
                        if status == "ok" and handle and payload:
                            observability.usage(payload,messages,usage,model=model["model"],route=route,
                                billing_provider="siliconflow" if station["id"] == "siliconflow" else "deepseek" if station["id"] in {"deepseek_direct","deepseek_expression"} else "unknown",
                                duration_ms=result.duration_ms,request_id=handle["id"],started_at=provider_started_at,projection=projection)
                    if usage:
                        try:
                            from token_tracker import log_token_usage
                            await log_token_usage("flash_structured:" + route, model["model"], usage,
                                                  provider="siliconflow" if station["id"] == "siliconflow" else "deepseek" if station["id"] in {"deepseek_direct", "deepseek_expression", "deepseek_main"} else "unknown")
                        except Exception:
                            pass
    except TimeoutError:
        result.status, result.error = "timeout", "chain_timeout"
    finally:
        result.request_dispatched = request_dispatched
        result.duration_ms = round((time.perf_counter()-started)*1000)
        if owned:
            await client.aclose()
    return result


@routing_scope
async def run_structured(messages: list, **kwargs):
    from .recovery import run_recoverable
    return await run_recoverable(messages, **kwargs)
