"""Read-only recall projection for current Memory V2 period views."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .recall import (
    EventRecallIndex,
    RecallDocument,
    RecallSourceAnchor,
    SemanticScorer,
)


class PeriodRecallIndex(EventRecallIndex):
    """Expose latest source-backed period items without making them authoritative."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        namespace: str = "mainline",
        date_basis: str = "active_date",
        period_kinds: Sequence[str] = ("day", "week", "month"),
        semantic_scorer: SemanticScorer | None = None,
    ):
        clean_kinds = tuple(dict.fromkeys(str(kind) for kind in period_kinds))
        if not namespace.strip():
            raise ValueError("period recall namespace is required")
        if date_basis not in {"active_date", "calendar_date"}:
            raise ValueError("period recall date_basis is invalid")
        if not clean_kinds or any(
            kind not in {"day", "week", "month"} for kind in clean_kinds
        ):
            raise ValueError("period recall kinds must be day, week, or month")
        super().__init__(db_path, semantic_scorer=semantic_scorer)
        self.namespace = namespace
        self.date_basis = date_basis
        self.period_kinds = clean_kinds

    @staticmethod
    def _event_ids_for_item(connection, item_id: str) -> tuple[str, ...]:
        pending = [item_id]
        visited: set[str] = set()
        event_ids: list[str] = []
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            if len(visited) > 10_000:
                raise RuntimeError("period item lineage exceeds the safety bound")
            for row in connection.execute(
                "SELECT event_id FROM period_item_event_sources "
                "WHERE period_item_id=? ORDER BY source_order",
                (current,),
            ).fetchall():
                event_id = str(row["event_id"])
                if event_id not in event_ids:
                    event_ids.append(event_id)
            pending.extend(
                str(row["parent_item_id"])
                for row in connection.execute(
                    "SELECT parent_item_id FROM period_item_parent_sources "
                    "WHERE period_item_id=? ORDER BY source_order",
                    (current,),
                ).fetchall()
                if str(row["parent_item_id"]) not in visited
            )
        return tuple(event_ids)

    def _load_documents(self) -> list[RecallDocument]:
        connection = self._connect()
        try:
            placeholders = ",".join("?" for _ in self.period_kinds)
            rows = connection.execute(
                "SELECT v.period_kind, v.date_from AS period_date_from, "
                "v.date_to AS period_date_to, i.id AS item_id, i.item_kind, "
                "i.summary, i.importance, i.confidence "
                "FROM period_summary_versions v "
                "JOIN period_summary_items i ON i.period_summary_id=v.id "
                "JOIN ("
                "  SELECT namespace, period_kind, date_basis, period_key, "
                "         MAX(revision) AS revision "
                "  FROM period_summary_versions "
                f"  WHERE namespace=? AND date_basis=? AND period_kind IN ({placeholders}) "
                "  GROUP BY namespace, period_kind, date_basis, period_key"
                ") latest ON latest.namespace=v.namespace "
                "AND latest.period_kind=v.period_kind "
                "AND latest.date_basis=v.date_basis "
                "AND latest.period_key=v.period_key "
                "AND latest.revision=v.revision "
                "ORDER BY v.date_from, v.date_to, v.period_kind, i.item_order",
                (self.namespace, self.date_basis, *self.period_kinds),
            ).fetchall()
            documents: list[RecallDocument] = []
            for row in rows:
                event_ids = self._event_ids_for_item(connection, str(row["item_id"]))
                if not event_ids:
                    continue
                event_placeholders = ",".join("?" for _ in event_ids)
                events = connection.execute(
                    "SELECT id, subject_id, event_type, active_date, calendar_date "
                    f"FROM events WHERE id IN ({event_placeholders})",
                    event_ids,
                ).fetchall()
                by_event = {str(event["id"]): event for event in events}
                ordered_events = [
                    by_event[event_id]
                    for event_id in event_ids
                    if event_id in by_event
                ]
                if not ordered_events:
                    continue
                participant_rows = connection.execute(
                    "SELECT event_id, participant_id FROM event_participants "
                    f"WHERE event_id IN ({event_placeholders}) "
                    "ORDER BY event_id, participant_order",
                    event_ids,
                ).fetchall()
                source_rows = connection.execute(
                    "SELECT event_id, message_id, source_kind, span_start, span_end, "
                    "span_digest FROM event_sources "
                    f"WHERE event_id IN ({event_placeholders}) "
                    "ORDER BY event_id, source_order",
                    event_ids,
                ).fetchall()
                sources_by_event: dict[str, list] = {}
                for source in source_rows:
                    sources_by_event.setdefault(str(source["event_id"]), []).append(source)
                anchors: dict[str, RecallSourceAnchor] = {}
                source_kinds: list[str] = []
                for event_id in event_ids:
                    for source in sources_by_event.get(event_id, ()):
                        message_id = str(source["message_id"])
                        anchors.setdefault(
                            message_id,
                            RecallSourceAnchor(
                                message_id=message_id,
                                span_start=(
                                    int(source["span_start"])
                                    if source["span_start"] is not None
                                    else None
                                ),
                                span_end=(
                                    int(source["span_end"])
                                    if source["span_end"] is not None
                                    else None
                                ),
                                span_digest=str(source["span_digest"] or ""),
                            ),
                        )
                        kind = str(source["source_kind"])
                        if kind not in source_kinds:
                            source_kinds.append(kind)
                calendar_dates = sorted(
                    str(event["calendar_date"])
                    for event in ordered_events
                    if event["calendar_date"]
                )
                active_dates = sorted(
                    str(event["active_date"])
                    for event in ordered_events
                    if event["active_date"]
                )
                documents.append(
                    RecallDocument(
                        document_id=f"period:{row['item_id']}",
                        event_ids=event_ids,
                        source_message_ids=tuple(anchors),
                        summaries=(str(row["summary"]),),
                        subject_ids=tuple(
                            dict.fromkeys(
                                str(event["subject_id"]) for event in ordered_events
                            )
                        ),
                        participant_ids=tuple(
                            dict.fromkeys(
                                str(item["participant_id"])
                                for item in participant_rows
                            )
                        ),
                        event_types=tuple(
                            dict.fromkeys(
                                str(event["event_type"])
                                for event in ordered_events
                            )
                        ),
                        facets=(str(row["period_kind"]), str(row["item_kind"])),
                        source_kinds=tuple(source_kinds),
                        active_date_from=(
                            active_dates[0]
                            if active_dates
                            else str(row["period_date_from"])
                        ),
                        active_date_to=(
                            active_dates[-1]
                            if active_dates
                            else str(row["period_date_to"])
                        ),
                        date_from=(
                            calendar_dates[0]
                            if calendar_dates
                            else str(row["period_date_from"])
                        ),
                        date_to=(
                            calendar_dates[-1]
                            if calendar_dates
                            else str(row["period_date_to"])
                        ),
                        importance=float(row["importance"]),
                        confidence=float(row["confidence"]),
                        source_anchors=tuple(anchors.values()),
                        source_layer="period",
                        display_text=str(row["summary"]),
                    )
                )
            return documents
        finally:
            connection.close()


__all__ = ["PeriodRecallIndex"]
