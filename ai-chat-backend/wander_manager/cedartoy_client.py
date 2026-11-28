"""CedarToy's narrow, authenticated JSON-RPC boundary for Wander games."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


BASE_URL = "https://toy.cedarstar.org"
GAMES = {"detroit": "底特律：变人", "moonlit": "月幕万象",
         "mbti": "MBTI 人格", "sins_virtues": "七宗罪 VS 七美德", "fishing": "AI钓鱼"}


class CedarToyError(RuntimeError):
    pass


class CedarToyRejectedAction(CedarToyError):
    """A provider refusal, distinct from transport loss or unknown write outcome."""

    def __init__(self, detail: str):
        self.detail = detail
        self.attempted_action = ""
        self.game = ""
        self.category = (
            "anti_addiction" if "防沉迷" in detail else
            "stale_revision" if "revision" in detail.lower() or "版本过期" in detail else
            "unsupported_action" if "未知" in detail and "操作" in detail else
            "rejected_action"
        )
        super().__init__(f"cedartoy_{self.category}")


def _rejection_detail(result: dict[str, Any], token: str) -> str:
    content = result.get("content")
    text = next((item.get("text") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"
                 and isinstance(item.get("text"), str)), "") if isinstance(content, list) else ""
    # This bounded diagnostic is retained only in local execution audit, not
    # inserted into AI's prompt or a public activity summary.
    text = " ".join(text.split()).replace(token, "[credential]")
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "[identifier]", text)
    return text[:240] or "CedarToy 未提供拒绝原因"


def configured() -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,256}", os.getenv("CEDARTOY_MACHINE_TOKEN", "").strip()))


class CedarToyClient:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._token = (token if token is not None else os.getenv("CEDARTOY_MACHINE_TOKEN", "")).strip()
        self._transport = transport
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", self._token):
            raise CedarToyError("cedartoy_machine_account_unavailable")

    async def play(self, game: str, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if game not in GAMES:
            raise CedarToyError("unsupported_game")
        args = {"game": game, "action": action, "params": dict(params or {})}
        return await self._call("play", args)

    async def my_saves(self) -> dict[str, Any]:
        return await self._call("account", {"action": "my_saves"})

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=35.0,
                                         follow_redirects=False, trust_env=False) as client:
                async def send():
                    response = await client.post(f"{BASE_URL}/{self._token}", json=request)
                    response.raise_for_status()
                    return response
                if name == 'account' and args.get('action') == 'my_saves':
                    from .node_retry import read
                    response = await read(send, '查看游戏存档')
                else:
                    response = await send()
                envelope = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The URL contains a credential. Never put an HTTP exception or
            # server-returned text into logs, node evidence, or UI errors.
            raise CedarToyError("cedartoy_transport_error") from None
        if not isinstance(envelope, dict) or envelope.get("error"):
            raise CedarToyError("cedartoy_protocol_error")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise CedarToyError("cedartoy_protocol_error")
        if result.get("isError"):
            raise CedarToyRejectedAction(_rejection_detail(result, self._token))
        # Some games forward their own MCP envelope inside the outer text.
        # A nested refusal must never become a successful game checkpoint.
        for _ in range(5):
            if result.get("error"):
                raise CedarToyError("cedartoy_protocol_error")
            if result.get("isError"):
                raise CedarToyRejectedAction(_rejection_detail(result, self._token))
            if result.get("jsonrpc") and isinstance(result.get("result"), dict):
                result = result["result"]
                continue
            if "content" not in result:
                return result
            content = result.get("content")
            if not isinstance(content, list) or not content:
                raise CedarToyError("cedartoy_empty_result")
            chunks = [item["text"] for item in content if isinstance(item, dict)
                      and item.get("type") == "text" and isinstance(item.get("text"), str)]
            if not chunks:
                raise CedarToyError("cedartoy_empty_result")
            text = "\n".join(chunks)
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"text": text}
            if not isinstance(parsed, dict):
                return {"text": text}
            result = parsed
        raise CedarToyError("cedartoy_protocol_error")
