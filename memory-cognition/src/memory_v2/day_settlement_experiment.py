"""Flash transport adapter for isolated completed-day settlement runs."""

from __future__ import annotations

from typing import Any


class DaySettlementProviderError(RuntimeError):
    """The experimental provider transport did not return usable content."""

    def __init__(self, status: str, error: str = "") -> None:
        self.safe_error_code = (
            f"provider_{status}:{error or 'unknown'}"
        )
        super().__init__(self.safe_error_code)


class FlashDaySettlementModelAdapter:
    """Keep provider content in-memory and expose only bounded usage diagnostics."""

    def __init__(self) -> None:
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    async def __call__(self, **kwargs: Any) -> str:
        from .experiment import _call_flash_once

        provider = await _call_flash_once(
            kwargs["messages"],
            max_tokens=int(kwargs["max_tokens"]),
            schema_name=str(kwargs["schema_name"]),
            schema=dict(kwargs["schema"]),
            usage_tag="memory_v2_day_settlement_experiment",
        )
        self.call_count += 1
        self.prompt_tokens += int(provider.usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(provider.usage.get("completion_tokens") or 0)
        self.total_tokens += int(provider.usage.get("total_tokens") or 0)
        if provider.status != "ok":
            raise DaySettlementProviderError(provider.status, provider.error)
        return provider.raw_content

    def safe_usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


__all__ = [
    "DaySettlementProviderError",
    "FlashDaySettlementModelAdapter",
]
