"""Flash adapter for period-view experiments over isolated Memory V2 stores."""

from __future__ import annotations

from typing import Any

from .period_runner import PeriodGenerationPlan, PeriodRunResult, run_period_generation
from .store import MemoryV2Store


class PeriodProviderError(RuntimeError):
    """The provider failed before a period candidate could be committed."""

    def __init__(self, message: str, *, usage: dict[str, int]):
        super().__init__(message)
        self.usage = dict(usage)


class FlashPeriodModelAdapter:
    """Collect content-free usage while keeping provider output in-memory only."""

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
            usage_tag="memory_v2_period_experiment",
        )
        self.call_count += 1
        self.prompt_tokens += int(provider.usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(provider.usage.get("completion_tokens") or 0)
        self.total_tokens += int(provider.usage.get("total_tokens") or 0)
        if provider.status != "ok":
            raise PeriodProviderError(
                f"period_provider_{provider.status}:{provider.error or 'unknown'}",
                usage=self.safe_usage(),
            )
        return provider.raw_content

    def safe_usage(self) -> dict[str, int]:
        return {
            "request_count": self.call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


async def run_flash_period_experiment(
    store: MemoryV2Store,
    plan: PeriodGenerationPlan,
    *,
    max_output_tokens: int = 6_000,
) -> tuple[PeriodRunResult, dict[str, int]]:
    adapter = FlashPeriodModelAdapter()
    result = await run_period_generation(
        store,
        plan,
        model_call=adapter,
        max_output_tokens=max_output_tokens,
    )
    return result, adapter.safe_usage()


__all__ = [
    "FlashPeriodModelAdapter",
    "PeriodProviderError",
    "run_flash_period_experiment",
]
