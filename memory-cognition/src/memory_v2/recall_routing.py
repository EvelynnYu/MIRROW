"""Deterministic query routing for Memory V2 recall.

The bounded date and ordering grammar is adapted from AionsHome's active
memory-search routing (MIT).  MIRROW deliberately requires a caller-supplied
reference date instead of inventing a clock or logical-day boundary here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .recall import RecallQuery, RecallSortMode


_EXPLICIT_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<year>\d{4})[年./-])?"
    r"(?P<month>\d{1,2})[月./-](?P<day>\d{1,2})日?(?!\d)"
)
_VERSION_PREFIX_RE = re.compile(r"(?:v(?:er(?:sion)?)?|版本)\s*$", re.I)
_RELATIVE_DAY_RE = re.compile(r"大前天|前天|昨天|昨日|今天|今日")
_DATE_RANGE_SEPARATOR_RE = re.compile(r"(?:到|至|~|～|—|–|-)")
_LATEST_RE = re.compile(r"最近(?:的)?一次|上一次|上次|最后一次|最新(?:的)?一次")
_EARLIEST_RE = re.compile(r"第一次|最早(?:的)?一次|最开始(?:那次)?")
_HISTORY_RE = re.compile(
    r"之前|以前|过去|当时|那次|哪次|上次|曾经|还记得|想起来|"
    r"昨天|昨日|前天|大前天|上周|本周|这周|上个月|本月|这个月|"
    r"第一次|最早|最近一次|最后一次"
)
_DETAIL_RE = re.compile(
    r"原话|原文|怎么说|说了什么|具体(?:是|发生|过程|原因)?|"
    r"前因后果|来龙去脉|经过|细节|为什么|因为什么|哪件事|是哪件事"
)


@dataclass(frozen=True)
class RecallRoutingHints:
    """Content-free routing facts derived only from the current query."""

    needs_history: bool
    needs_source_detail: bool
    sort_mode: RecallSortMode = "relevance"
    date_from: str = ""
    date_to: str = ""
    time_expressions: tuple[str, ...] = ()

    def safe_observation(self) -> dict[str, object]:
        return {
            "needs_history": self.needs_history,
            "needs_source_detail": self.needs_source_detail,
            "sort_mode": self.sort_mode,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "time_expression_count": len(self.time_expressions),
        }


def _week_window(reference_date: date, *, previous: bool) -> tuple[date, date]:
    start = reference_date - timedelta(days=reference_date.weekday())
    if previous:
        start -= timedelta(days=7)
    return start, start + timedelta(days=6)


def _month_window(reference_date: date, *, previous: bool) -> tuple[date, date]:
    if previous:
        end = reference_date.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    start = reference_date.replace(day=1)
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def _extract_window(
    text: str,
    reference_date: date,
) -> tuple[str, str, tuple[str, ...]]:
    dated: list[tuple[int, int, date, str]] = []
    relative_offsets = {
        "今天": 0,
        "今日": 0,
        "昨天": -1,
        "昨日": -1,
        "前天": -2,
        "大前天": -3,
    }
    for match in _RELATIVE_DAY_RE.finditer(text):
        expression = match.group(0)
        dated.append(
            (
                match.start(),
                match.end(),
                reference_date + timedelta(days=relative_offsets[expression]),
                expression,
            )
        )
    for match in _EXPLICIT_DATE_RE.finditer(text):
        matched_text = match.group(0)
        # Dotted version numbers are not dates.  Preserve ordinary Chinese,
        # slash, ISO and bare dotted dates such as 9.5, while rejecting v3.2,
        # 版本 3.2 and dotted triples such as 3.2.1.
        if "." in matched_text:
            prefix = text[max(0, match.start() - 12) : match.start()]
            following = text[match.end() : match.end() + 1]
            preceding = text[match.start() - 1 : match.start()]
            if (
                _VERSION_PREFIX_RE.search(prefix)
                or preceding == "."
                or following == "."
            ):
                continue
        year = int(match.group("year") or reference_date.year)
        try:
            parsed = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        dated.append((match.start(), match.end(), parsed, match.group(0)))

    dated.sort(key=lambda item: (item[0], item[1]))
    if dated:
        if len(dated) >= 2:
            between = text[dated[0][1] : dated[1][0]]
            if _DATE_RANGE_SEPARATOR_RE.search(between):
                start, end = sorted((dated[0][2], dated[1][2]))
                return (
                    start.isoformat(),
                    end.isoformat(),
                    (dated[0][3], dated[1][3]),
                )
        selected = dated[0]
        value = selected[2].isoformat()
        return value, value, (selected[3],)

    calendar_windows = (
        ("上周", lambda: _week_window(reference_date, previous=True)),
        ("本周", lambda: _week_window(reference_date, previous=False)),
        ("这周", lambda: _week_window(reference_date, previous=False)),
        ("上个月", lambda: _month_window(reference_date, previous=True)),
        ("本月", lambda: _month_window(reference_date, previous=False)),
        ("这个月", lambda: _month_window(reference_date, previous=False)),
    )
    for expression, resolver in calendar_windows:
        if expression in text:
            start, end = resolver()
            return start.isoformat(), end.isoformat(), (expression,)
    return "", "", ()


def route_recall_query(text: str, *, reference_date: date) -> RecallRoutingHints:
    """Extract only deterministic time, ordering, and evidence-depth signals."""

    clean = str(text or "").strip()
    if not clean:
        raise ValueError("recall routing text must not be empty")
    date_from, date_to, expressions = _extract_window(clean, reference_date)
    latest = bool(_LATEST_RE.search(clean))
    earliest = bool(_EARLIEST_RE.search(clean))
    sort_mode: RecallSortMode = "relevance"
    if latest and not earliest:
        sort_mode = "latest"
    elif earliest and not latest:
        sort_mode = "earliest"
    return RecallRoutingHints(
        needs_history=bool(
            expressions or latest or earliest or _HISTORY_RE.search(clean)
        ),
        needs_source_detail=bool(_DETAIL_RE.search(clean)),
        sort_mode=sort_mode,
        date_from=date_from,
        date_to=date_to,
        time_expressions=expressions,
    )


def apply_recall_routing_hints(
    query: RecallQuery,
    hints: RecallRoutingHints,
) -> RecallQuery:
    """Fill unset deterministic fields without weakening explicit caller filters."""

    caller_has_date_filter = bool(query.date_from or query.date_to)
    return RecallQuery(
        text=query.text,
        variants=query.variants,
        entity_terms=query.entity_terms,
        subject_ids=query.subject_ids,
        event_types=query.event_types,
        source_kinds=query.source_kinds,
        date_from=(query.date_from if caller_has_date_filter else hints.date_from),
        date_to=(query.date_to if caller_has_date_filter else hints.date_to),
        date_basis=query.date_basis,
        sort_mode=(
            query.sort_mode
            if query.sort_mode != "relevance"
            else hints.sort_mode
        ),
        exclude_message_ids=query.exclude_message_ids,
        exclude_active_dates=query.exclude_active_dates,
    )


__all__ = [
    "RecallRoutingHints",
    "apply_recall_routing_hints",
    "route_recall_query",
]
