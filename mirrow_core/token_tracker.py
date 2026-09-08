"""
Token 消耗追踪模块

非阻塞记录每次 LLM 调用的 token 消耗到日志文件。
所有写入通过 asyncio.create_task() 触发，不影响主流程。
"""
import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)

_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_log_path = os.path.join(_log_dir, "token_usage.log")
_write_lock = asyncio.Lock()


async def log_token_usage(caller: str, model: str, usage: Any = None):
    """异步写入 token 消耗记录。

    调用方应使用 asyncio.create_task() 触发，不阻塞响应。

    Args:
        caller: 调用方标识 (e.g. "chat_final_reply", "agent_tools", "wander_message")
        model: 模型名称
        usage: token 用量数据。支持 dict (httpx) 或 openai.types.CompletionUsage 对象。
               为 None 时仍记录一行（标记为 unavailable）。
    """
    try:
        # 提取 token 数值
        if usage is None:
            prompt = completion = total = -1
        elif isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", -1)
            completion = usage.get("completion_tokens", -1)
            total = usage.get("total_tokens", -1)
        else:
            # OpenAI SDK CompletionUsage 对象
            prompt = getattr(usage, "prompt_tokens", -1)
            completion = getattr(usage, "completion_tokens", -1)
            total = getattr(usage, "total_tokens", -1)

        record = {
            "ts": datetime.now().isoformat(),
            "caller": caller,
            "model": model,
            "prompt": prompt,
            "completion": completion,
            "total": total,
        }

        line = json.dumps(record, ensure_ascii=False) + "\n"

        async with _write_lock:
            os.makedirs(_log_dir, exist_ok=True)
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        logger.warning(f"Token usage logging failed: {e}")
