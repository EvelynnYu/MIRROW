"""One-shot orchestration for a sealed Memory V2 day repair."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .conversation_source import ConversationSource
from .day_repair import DayRepairPlanner
from .day_repair_promotion import promote_day_repair
from .day_repair_staging import BatchEncoder, execute_day_repair_staging


@dataclass(frozen=True)
class DayRepairRunResult:
    status: str
    active_date: str
    repair_id: str = ""
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    event_count: int = 0
    stage_result_digest: str = ""
    cleanup_status: str = "not_created"
    error_code: str = ""

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "active_date": self.active_date,
            "repair_id": self.repair_id,
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "event_count": self.event_count,
            "stage_result_digest": self.stage_result_digest,
            "cleanup_status": self.cleanup_status,
            "error_code": self.error_code,
        }


def _cleanup_stage(stage_db_path: str, stage_root: Path) -> str:
    if not stage_db_path:
        return "not_created"
    stage_path = Path(stage_db_path).resolve()
    root = stage_root.resolve()
    stage_dir = stage_path.parent
    if (
        stage_path.name != "memory_v2.db"
        or stage_dir.parent != root
        or not stage_dir.is_dir()
    ):
        return "path_rejected"
    try:
        shutil.rmtree(stage_dir)
    except OSError as exc:
        return f"error:{type(exc).__name__}"
    return "removed"


async def run_day_repair(
    source: ConversationSource,
    *,
    memory_db_path: str | Path,
    stage_root: str | Path,
    active_date: str,
    completed_before_active_date: str,
    max_requests_per_batch: int = 2,
    batch_encoder: BatchEncoder | None = None,
) -> DayRepairRunResult:
    """Stage, promote, and clean one repair without refreshing read models."""

    memory_path = Path(memory_db_path).resolve()
    root = Path(stage_root).resolve()
    planner = DayRepairPlanner(source, memory_path)
    plan = planner.plan(
        active_date=active_date,
        completed_before_active_date=completed_before_active_date,
    )
    if not plan.ready:
        return DayRepairRunResult(
            status=plan.status,
            active_date=plan.active_date,
            repair_id=plan.repair_id,
        )
    staging_kwargs: dict[str, Any] = {}
    if batch_encoder is not None:
        staging_kwargs["batch_encoder"] = batch_encoder
    staged = await execute_day_repair_staging(
        plan,
        source,
        memory_db_path=memory_path,
        stage_root=root,
        max_requests_per_batch=max_requests_per_batch,
        **staging_kwargs,
    )
    if not staged.staged:
        return DayRepairRunResult(
            status=staged.status,
            active_date=plan.active_date,
            repair_id=plan.repair_id,
            request_count=staged.request_count,
            prompt_tokens=staged.prompt_tokens,
            completion_tokens=staged.completion_tokens,
            error_code=staged.error_code,
        )
    promoted = promote_day_repair(
        plan,
        staged,
        source,
        memory_db_path=memory_path,
    )
    cleanup_status = _cleanup_stage(staged.stage_db_path, root)
    return DayRepairRunResult(
        status="ok" if promoted.promoted else promoted.status,
        active_date=plan.active_date,
        repair_id=plan.repair_id,
        request_count=staged.request_count,
        prompt_tokens=staged.prompt_tokens,
        completion_tokens=staged.completion_tokens,
        event_count=len(staged.new_event_ids),
        stage_result_digest=staged.stage_result_digest,
        cleanup_status=cleanup_status,
        error_code=promoted.error_code,
    )


__all__ = ["DayRepairRunResult", "run_day_repair"]
