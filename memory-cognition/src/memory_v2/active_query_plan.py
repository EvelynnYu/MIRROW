"""Validate search stages explicitly supplied through Agent's bookshelf action.

The existing tool-action compiler formats Agent's action as tool parameters.  No
additional planner model is called on this memory path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_MAX_BRANCH_CHARS = 120
_MAX_STAGES = 4


@dataclass(frozen=True)
class ActiveQueryPlan:
    anchor_query: str
    followup_queries: tuple[str, ...]

    @property
    def queries(self) -> tuple[str, ...]:
        return (self.anchor_query, *self.followup_queries)


def plan_from_stage_queries(value: Any) -> ActiveQueryPlan | None:
    """Accept only a bounded explicit sequence; absence means ordinary search."""

    if not isinstance(value, list) or not 2 <= len(value) <= _MAX_STAGES:
        return None
    if any(
        not isinstance(item, str) or not 2 <= len(item.strip()) <= _MAX_BRANCH_CHARS
        for item in value
    ):
        return None
    stages = tuple(item.strip() for item in value)
    if len({stage.casefold() for stage in stages}) != len(stages):
        return None
    return ActiveQueryPlan(stages[0], stages[1:])


__all__ = ["ActiveQueryPlan", "plan_from_stage_queries"]
