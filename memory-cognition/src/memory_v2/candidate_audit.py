"""Read-only pre-cutover audit for a frozen Memory V2 candidate database.

The audit deliberately emits identities, counts, and reason codes only.  It may
read private summaries and source messages to validate digests and select a
manual-review sample, but it never copies those bodies into its report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .day_settlement_candidates import is_long_gap_without_back_reference
from .day_settlement_lineage import effective_event_thread_status_sql
from .models import BatchSourceRef, digest_batch_sources
from .schema import SCHEMA_VERSION
from .source_projection import memory_source_content


AUDIT_VERSION = "memory-v2-candidate-audit-v2"
_REQUIRED_MEMORY_TABLES = frozenset(
    {
        "memory_schema",
        "encoding_batches",
        "encoding_batch_sources",
        "events",
        "event_sources",
        "event_threads",
        "event_thread_status_log",
        "event_links",
        "thread_events",
        "day_settlements",
        "day_compact_items",
        "day_compact_item_events",
        "day_thread_judgments",
    }
)
_REQUIRED_CONVERSATION_COLUMNS = frozenset(
    {
        "id",
        "active_date",
        "calendar_date",
        "session_id",
        "timestamp",
        "role",
        "content",
        "message_id",
        "is_wander",
        "is_sentinel",
        "is_reminder",
        "event_type",
    }
)
_ABSENCE_AS_CONCLUSION_RE = re.compile(
    r"没有明确|未找到|没有找到|未提及|没有记录|无法确认|尚无明确|此后没有"
)
_POSSIBLE_ASSUMED_STANCE_RE = re.compile(
    r"人类伙伴(?:默认|同意|原谅|接受|认可|答应|不反对|没有反对|理解了)"
)
_SENSITIVE_OR_NETWORK_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|"
    r"(?:\b\d{1,3}\.){3}\d{1,3}\b|"
    r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}|"
    r"\b[a-fA-F0-9]{24,}\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AuditFinding:
    code: str
    count: int
    sample_ids: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "count": self.count,
            "sample_ids": list(self.sample_ids),
        }


@dataclass
class CandidateAuditReport:
    candidate_name: str
    conversation_name: str
    candidate_sha256: str
    expected_sha256: str = ""
    schema_version: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    blockers: list[AuditFinding] = field(default_factory=list)
    review_flags: list[AuditFinding] = field(default_factory=list)
    manual_event_sample: list[dict[str, Any]] = field(default_factory=list)
    manual_compact_sample: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.blockers:
            return "blocked"
        if self.review_flags:
            return "manual_review_required"
        return "structurally_ready"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "audit_version": AUDIT_VERSION,
            "status": self.status,
            "candidate_name": self.candidate_name,
            "conversation_name": self.conversation_name,
            "candidate_sha256": self.candidate_sha256,
            "expected_sha256": self.expected_sha256,
            "expected_sha256_matches": (
                not self.expected_sha256
                or self.candidate_sha256.lower() == self.expected_sha256.lower()
            ),
            "schema_version": self.schema_version,
            "counts": dict(sorted(self.counts.items())),
            "blockers": [item.safe_dict() for item in self.blockers],
            "review_flags": [item.safe_dict() for item in self.review_flags],
            "manual_event_sample": self.manual_event_sample,
            "manual_compact_sample": self.manual_compact_sample,
            "body_text_included": False,
        }


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_digest(value: object) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _source_kind(row: Mapping[str, Any]) -> str:
    marked = [
        name
        for name, enabled in (
            ("wander", row["is_wander"]),
            ("sentinel", row["is_sentinel"]),
            ("reminder", row["is_reminder"]),
        )
        if bool(enabled)
    ]
    if len(marked) != 1:
        return "chat" if not marked else "invalid"
    return marked[0]


def _sample_ids(values: Iterable[str], limit: int = 12) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))[:limit]


def _add_finding(
    target: list[AuditFinding],
    code: str,
    values: Sequence[str] | int,
) -> None:
    if isinstance(values, int):
        count = values
        sample = ()
    else:
        count = len(values)
        sample = _sample_ids(values)
    if count:
        target.append(AuditFinding(code=code, count=count, sample_ids=sample))


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _stratified(rows: Sequence[sqlite3.Row], count: int) -> list[sqlite3.Row]:
    if not rows or count <= 0:
        return []
    if len(rows) <= count:
        return list(rows)
    return [rows[(index * len(rows)) // count] for index in range(count)]


def _has_cycle(edges: Sequence[tuple[str, str]]) -> bool:
    graph: dict[str, list[str]] = {}
    for start, end in edges:
        graph.setdefault(start, []).append(end)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for following in graph.get(node, ()):
            if visit(following):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in tuple(graph))


def audit_candidate(
    memory_db: str | Path,
    conversation_db: str | Path,
    *,
    expected_sha256: str = "",
) -> CandidateAuditReport:
    """Audit a frozen candidate without opening either SQLite authority writable."""

    memory_path = Path(memory_db).resolve()
    conversation_path = Path(conversation_db).resolve()
    if not memory_path.is_file():
        raise FileNotFoundError(memory_path)
    if not conversation_path.is_file():
        raise FileNotFoundError(conversation_path)
    report = CandidateAuditReport(
        candidate_name=memory_path.name,
        conversation_name=conversation_path.name,
        candidate_sha256=_sha256_file(memory_path),
        expected_sha256=expected_sha256.strip().upper(),
    )
    if report.expected_sha256 and report.candidate_sha256 != report.expected_sha256:
        _add_finding(report.blockers, "candidate_hash_mismatch", 1)

    with closing(_connect_read_only(memory_path)) as memory, closing(
        _connect_read_only(conversation_path)
    ) as conversation:
        integrity = str(memory.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            _add_finding(report.blockers, "integrity_check_failed", 1)
        foreign_keys = memory.execute("PRAGMA foreign_key_check").fetchall()
        _add_finding(report.blockers, "foreign_key_errors", len(foreign_keys))

        tables = _table_names(memory)
        missing_tables = sorted(_REQUIRED_MEMORY_TABLES - tables)
        _add_finding(report.blockers, "missing_memory_tables", missing_tables)
        conversation_tables = _table_names(conversation)
        if "conversation_messages" not in conversation_tables:
            _add_finding(report.blockers, "missing_conversation_messages", 1)
        else:
            missing_columns = sorted(
                _REQUIRED_CONVERSATION_COLUMNS
                - _table_columns(conversation, "conversation_messages")
            )
            _add_finding(
                report.blockers,
                "missing_conversation_columns",
                missing_columns,
            )
        if missing_tables or "conversation_messages" not in conversation_tables:
            return report

        schema_row = memory.execute(
            "SELECT schema_version FROM memory_schema WHERE schema_key='memory_v2'"
        ).fetchone()
        report.schema_version = int(schema_row[0]) if schema_row else 0
        if report.schema_version != SCHEMA_VERSION:
            _add_finding(report.blockers, "unexpected_schema_version", 1)

        for table in (
            "encoding_batches",
            "encoding_batch_sources",
            "events",
            "event_sources",
            "event_threads",
            "event_thread_status_log",
            "event_links",
            "thread_events",
            "day_settlements",
            "day_compact_items",
            "day_compact_item_events",
            "day_thread_judgments",
        ):
            report.counts[table] = int(
                memory.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )

        raw_by_row: dict[int, sqlite3.Row] = {}
        duplicate_message_ids = [
            str(row[0])
            for row in conversation.execute(
                "SELECT message_id FROM conversation_messages "
                "WHERE message_id IS NOT NULL AND trim(message_id)<>'' "
                "GROUP BY message_id HAVING COUNT(*)>1"
            )
        ]
        _add_finding(
            report.review_flags,
            "duplicate_authoritative_message_ids",
            duplicate_message_ids,
        )
        receipt_column = (
            "tool_calls" if "tool_calls" in _table_columns(conversation, "conversation_messages")
            else "NULL AS tool_calls"
        )
        card_column = (
            "long_text_card" if "long_text_card" in _table_columns(conversation, "conversation_messages")
            else "NULL AS long_text_card"
        )
        for row in conversation.execute(
            "SELECT id, active_date, calendar_date, session_id, timestamp, role, "
            "content, message_id, is_wander, is_sentinel, is_reminder, event_type, "
            f"{receipt_column}, {card_column} "
            "FROM conversation_messages"
        ):
            raw_by_row[int(row["id"])] = row

        batch_source_mismatches: list[str] = []
        sources_by_batch: dict[str, list[BatchSourceRef]] = {}
        for source in memory.execute(
            "SELECT batch_id, message_row_id, message_id, session_id, active_date, "
            "calendar_date, source_ts, source_role, source_kind, source_event_type, "
            "content_digest, source_order FROM encoding_batch_sources "
            "ORDER BY batch_id, source_order"
        ):
            batch_id = str(source["batch_id"])
            raw = raw_by_row.get(int(source["message_row_id"]))
            if raw is None or any(
                (
                    str(raw["message_id"] or "") != str(source["message_id"]),
                    str(raw["session_id"]) != str(source["session_id"]),
                    str(raw["active_date"]) != str(source["active_date"]),
                    str(raw["calendar_date"] or "") != str(source["calendar_date"]),
                    str(raw["timestamp"]) != str(source["source_ts"]),
                    str(raw["role"]) != str(source["source_role"]),
                    _source_kind(raw) != str(source["source_kind"]),
                    str(raw["event_type"] or "")
                    != str(source["source_event_type"] or ""),
                    _digest_text(memory_source_content(dict(raw)) or "")
                    != str(source["content_digest"]),
                )
            ):
                batch_source_mismatches.append(
                    f"{batch_id}:{source['message_row_id']}"
                )
            sources_by_batch.setdefault(batch_id, []).append(
                BatchSourceRef(
                    message_row_id=int(source["message_row_id"]),
                    message_id=str(source["message_id"]),
                    session_id=str(source["session_id"]),
                    active_date=str(source["active_date"]),
                    calendar_date=str(source["calendar_date"]),
                    source_ts=str(source["source_ts"]),
                    source_role=str(source["source_role"]),
                    content_digest=str(source["content_digest"]),
                    source_kind=str(source["source_kind"]),
                    source_event_type=str(source["source_event_type"]),
                )
            )
        _add_finding(
            report.blockers,
            "batch_source_authority_mismatch",
            batch_source_mismatches,
        )

        batch_receipt_mismatches: list[str] = []
        for batch in memory.execute(
            "SELECT id, active_date, from_message_row_id, to_message_row_id, "
            "from_message_id, to_message_id, source_count, source_digest, status, "
            "error_code, event_count FROM encoding_batches ORDER BY active_date, id"
        ):
            batch_id = str(batch["id"])
            sources = sources_by_batch.get(batch_id, [])
            event_count = int(
                memory.execute(
                    "SELECT COUNT(*) FROM events WHERE batch_id=?", (batch_id,)
                ).fetchone()[0]
            )
            if (
                str(batch["status"]) != "completed"
                or str(batch["error_code"] or "")
                or not sources
                or len(sources) != int(batch["source_count"])
                or event_count != int(batch["event_count"])
                or sources[0].message_row_id != int(batch["from_message_row_id"])
                or sources[-1].message_row_id != int(batch["to_message_row_id"])
                or sources[0].message_id != str(batch["from_message_id"])
                or sources[-1].message_id != str(batch["to_message_id"])
                or any(item.active_date != str(batch["active_date"]) for item in sources)
                or digest_batch_sources(tuple(sources)) != str(batch["source_digest"])
            ):
                batch_receipt_mismatches.append(batch_id)
        _add_finding(
            report.blockers,
            "encoding_batch_receipt_mismatch",
            batch_receipt_mismatches,
        )

        event_rows = memory.execute(
            "SELECT id, batch_id, subject_id, event_type, summary, occurred_at, "
            "reported_at, active_date, calendar_date, importance, emotional_weight, "
            "confidence, epistemic_status FROM events "
            "ORDER BY active_date, reported_at, id"
        ).fetchall()
        event_by_id = {str(row["id"]): row for row in event_rows}
        missing_event_sources: list[str] = []
        authority_mismatches: list[str] = []
        source_manifest_mismatches: list[str] = []
        subject_evidence_mismatches: list[str] = []
        event_roles: dict[str, set[str]] = {}
        event_kinds: dict[str, set[str]] = {}
        for source in memory.execute(
            "SELECT s.event_id, s.message_row_id, s.message_id, s.session_id, "
            "s.source_ts, s.source_role, s.source_kind, s.source_event_type, "
            "s.span_start, s.span_end, s.span_digest, e.batch_id, e.active_date "
            "FROM event_sources s JOIN events e ON e.id=s.event_id "
            "ORDER BY s.event_id, s.source_order"
        ):
            event_id = str(source["event_id"])
            event_roles.setdefault(event_id, set()).add(str(source["source_role"]))
            event_kinds.setdefault(event_id, set()).add(str(source["source_kind"]))
            raw = raw_by_row.get(int(source["message_row_id"]))
            mismatch = raw is None
            if raw is not None:
                mismatch = any(
                    (
                        str(raw["message_id"] or "") != str(source["message_id"]),
                        str(raw["session_id"]) != str(source["session_id"]),
                        str(raw["timestamp"]) != str(source["source_ts"]),
                        str(raw["role"]) != str(source["source_role"]),
                        str(raw["active_date"]) != str(source["active_date"]),
                        _source_kind(raw) != str(source["source_kind"]),
                        str(raw["event_type"] or "")
                        != str(source["source_event_type"] or ""),
                    )
                )
                start = source["span_start"]
                end = source["span_end"]
                span_digest = str(source["span_digest"] or "")
                if start is None and end is None:
                    mismatch = mismatch or bool(span_digest)
                elif start is None or end is None:
                    mismatch = True
                else:
                    content = memory_source_content(dict(raw)) or ""
                    start_int, end_int = int(start), int(end)
                    mismatch = mismatch or not (0 <= start_int < end_int <= len(content))
                    if not mismatch:
                        mismatch = (
                            _digest_text(content[start_int:end_int]) != span_digest
                        )
            if mismatch:
                authority_mismatches.append(event_id)
            manifest_match = memory.execute(
                "SELECT 1 FROM encoding_batch_sources WHERE batch_id=? "
                "AND message_row_id=? AND message_id=?",
                (
                    str(source["batch_id"]),
                    int(source["message_row_id"]),
                    str(source["message_id"]),
                ),
            ).fetchone()
            if manifest_match is None:
                source_manifest_mismatches.append(event_id)
        for event in event_rows:
            event_id = str(event["id"])
            roles = event_roles.get(event_id, set())
            if not roles:
                missing_event_sources.append(event_id)
            if (
                event["subject_id"] == "human" and "user" not in roles
            ) or (event["subject_id"] == "agent" and not ({"assistant", "notification"} & roles)):
                subject_evidence_mismatches.append(event_id)
        _add_finding(report.blockers, "events_without_sources", missing_event_sources)
        _add_finding(
            report.blockers,
            "event_source_authority_mismatch",
            authority_mismatches,
        )
        _add_finding(
            report.blockers,
            "event_source_outside_batch_manifest",
            source_manifest_mismatches,
        )
        _add_finding(
            report.blockers,
            "subject_evidence_mismatch",
            subject_evidence_mismatches,
        )
        _add_finding(
            report.blockers,
            "model_inference_events",
            [
                str(row["id"])
                for row in event_rows
                if str(row["epistemic_status"]) == "model_inference"
            ],
        )
        _add_finding(
            report.blockers,
            "proactive_only_events",
            [
                str(row["id"])
                for row in event_rows
                if event_kinds.get(str(row["id"]), set())
                and "chat" not in event_kinds[str(row["id"])]
            ],
        )

        summary_over_limit = [
            str(row["id"])
            for row in event_rows
            if not str(row["summary"]).strip() or len(str(row["summary"])) > 500
        ]
        _add_finding(report.blockers, "event_summary_length_invalid", summary_over_limit)
        content_flag_ids: dict[str, list[str]] = {
            "absence_presented_as_conclusion": [],
            "possible_assumed_stance": [],
            "sensitive_or_network_text_in_summary": [],
            "mostly_ascii_event_summary": [],
        }
        for row in event_rows:
            event_id = str(row["id"])
            summary = str(row["summary"])
            if _ABSENCE_AS_CONCLUSION_RE.search(summary):
                content_flag_ids["absence_presented_as_conclusion"].append(event_id)
            if _POSSIBLE_ASSUMED_STANCE_RE.search(summary):
                content_flag_ids["possible_assumed_stance"].append(event_id)
            if _SENSITIVE_OR_NETWORK_RE.search(summary):
                content_flag_ids["sensitive_or_network_text_in_summary"].append(event_id)
            visible = [char for char in summary if not char.isspace()]
            ascii_letters = sum(char.isascii() and char.isalpha() for char in visible)
            if visible and ascii_letters / len(visible) > 0.65:
                content_flag_ids["mostly_ascii_event_summary"].append(event_id)
        for code, ids in content_flag_ids.items():
            target = (
                report.blockers
                if code == "sensitive_or_network_text_in_summary"
                else report.review_flags
            )
            _add_finding(target, code, ids)

        compact_rows = memory.execute(
            "SELECT i.id, i.settlement_id, i.item_order, i.summary, i.content_digest, "
            "d.active_date FROM day_compact_items i "
            "JOIN day_settlements d ON d.id=i.settlement_id "
            "ORDER BY d.active_date, i.item_order"
        ).fetchall()
        compact_digest_mismatches: list[str] = []
        compact_day_mismatches: list[str] = []
        compact_over_limit: list[str] = []
        compact_above_target: list[str] = []
        compact_absence_flags: list[str] = []
        compact_stance_flags: list[str] = []
        compact_sensitive_flags: list[str] = []
        compact_ascii_flags: list[str] = []
        for item in compact_rows:
            event_ids = [
                str(row[0])
                for row in memory.execute(
                    "SELECT event_id FROM day_compact_item_events "
                    "WHERE settlement_id=? AND item_order=? ORDER BY event_order",
                    (str(item["settlement_id"]), int(item["item_order"])),
                )
            ]
            expected = _canonical_digest(
                {
                    "ordinal": int(item["item_order"]),
                    "summary": str(item["summary"]),
                    "event_ids": event_ids,
                }
            )
            if expected != str(item["content_digest"]):
                compact_digest_mismatches.append(str(item["id"]))
            if not event_ids or any(
                event_by_id.get(event_id) is None
                or str(event_by_id[event_id]["active_date"])
                != str(item["active_date"])
                for event_id in event_ids
            ):
                compact_day_mismatches.append(str(item["id"]))
            if not str(item["summary"]).strip() or len(str(item["summary"])) > 1400:
                compact_over_limit.append(str(item["id"]))
            summary = str(item["summary"])
            if len(summary) > 450:
                compact_above_target.append(str(item["id"]))
            if _ABSENCE_AS_CONCLUSION_RE.search(summary):
                compact_absence_flags.append(str(item["id"]))
            if _POSSIBLE_ASSUMED_STANCE_RE.search(summary):
                compact_stance_flags.append(str(item["id"]))
            if _SENSITIVE_OR_NETWORK_RE.search(summary):
                compact_sensitive_flags.append(str(item["id"]))
            visible = [char for char in summary if not char.isspace()]
            ascii_letters = sum(char.isascii() and char.isalpha() for char in visible)
            if visible and ascii_letters / len(visible) > 0.65:
                compact_ascii_flags.append(str(item["id"]))
        _add_finding(
            report.blockers,
            "day_compact_digest_mismatch",
            compact_digest_mismatches,
        )
        _add_finding(
            report.blockers,
            "day_compact_source_day_mismatch",
            compact_day_mismatches,
        )
        _add_finding(
            report.blockers,
            "day_compact_summary_length_invalid",
            compact_over_limit,
        )
        _add_finding(
            report.review_flags,
            "day_compact_above_prompt_target",
            compact_above_target,
        )
        _add_finding(
            report.review_flags,
            "day_compact_absence_presented_as_conclusion",
            compact_absence_flags,
        )
        _add_finding(
            report.review_flags,
            "day_compact_possible_assumed_stance",
            compact_stance_flags,
        )
        _add_finding(
            report.blockers,
            "sensitive_or_network_text_in_day_compact",
            compact_sensitive_flags,
        )
        _add_finding(
            report.review_flags,
            "mostly_ascii_day_compact_summary",
            compact_ascii_flags,
        )

        settlement_mismatches: list[str] = []
        settlement_days: set[str] = set()
        for settlement in memory.execute(
            "SELECT * FROM day_settlements ORDER BY active_date, id"
        ):
            settlement_id = str(settlement["id"])
            active_date = str(settlement["active_date"])
            settlement_days.add(active_date)
            actual_day_events = int(
                memory.execute(
                    "SELECT COUNT(*) FROM events WHERE active_date=?", (active_date,)
                ).fetchone()[0]
            )
            actual_compacts = int(
                memory.execute(
                    "SELECT COUNT(*) FROM day_compact_items WHERE settlement_id=?",
                    (settlement_id,),
                ).fetchone()[0]
            )
            actual_judgments = int(
                memory.execute(
                    "SELECT COUNT(*) FROM day_thread_judgments WHERE settlement_id=?",
                    (settlement_id,),
                ).fetchone()[0]
            )
            actual_accepted = int(
                memory.execute(
                    "SELECT COUNT(*) FROM day_thread_judgments "
                    "WHERE settlement_id=? AND outcome='continue' "
                    "AND accepted_link_id IS NOT NULL",
                    (settlement_id,),
                ).fetchone()[0]
            )
            if (
                actual_day_events != int(settlement["day_event_count"])
                or actual_compacts != int(settlement["compact_item_count"])
                or actual_judgments != int(settlement["judgment_count"])
                or actual_accepted != int(settlement["accepted_continuation_count"])
            ):
                settlement_mismatches.append(settlement_id)
        _add_finding(
            report.blockers,
            "day_settlement_count_mismatch",
            settlement_mismatches,
        )
        active_days = {
            str(row[0])
            for row in memory.execute("SELECT DISTINCT active_date FROM events")
        }
        _add_finding(
            report.blockers,
            "active_days_without_settlement",
            sorted(active_days - settlement_days),
        )
        report.counts["active_days"] = len(active_days)

        thread_status_sql = effective_event_thread_status_sql("t")
        active_threads = memory.execute(
            "SELECT id, subject_id FROM event_threads t "
            f"WHERE {thread_status_sql}='active' ORDER BY id"
        ).fetchall()
        report.counts["active_event_threads"] = len(active_threads)
        report.counts["retracted_or_superseded_event_threads"] = (
            report.counts["event_threads"] - len(active_threads)
        )
        active_continuation_edges: list[tuple[str, str]] = []
        long_gap_continuations: list[str] = []
        thread_order_mismatches: list[str] = []
        thread_event_ids: list[str] = []
        for thread in active_threads:
            thread_id = str(thread["id"])
            nodes = memory.execute(
                "SELECT e.id, e.subject_id, e.reported_at, e.summary "
                "FROM thread_events te "
                "JOIN events e ON e.id=te.event_id WHERE te.thread_id=? "
                "ORDER BY te.sequence_no",
                (thread_id,),
            ).fetchall()
            thread_event_ids.extend(str(row["id"]) for row in nodes)
            if any(str(row["subject_id"]) != str(thread["subject_id"]) for row in nodes):
                thread_order_mismatches.append(thread_id)
            times = [str(row["reported_at"]) for row in nodes]
            if times != sorted(times):
                thread_order_mismatches.append(thread_id)
            for earlier, later in zip(nodes, nodes[1:]):
                link = memory.execute(
                    "SELECT id FROM event_links WHERE from_event_id=? "
                    "AND to_event_id=? AND link_type='continues'",
                    (str(earlier["id"]), str(later["id"])),
                ).fetchone()
                if link is None:
                    continue
                active_continuation_edges.append(
                    (str(earlier["id"]), str(later["id"]))
                )
                if is_long_gap_without_back_reference(
                    str(earlier["reported_at"]),
                    str(later["reported_at"]),
                    str(later["summary"]),
                ):
                    long_gap_continuations.append(thread_id)
        if _has_cycle(active_continuation_edges):
            _add_finding(report.blockers, "continuation_cycle", 1)
        _add_finding(
            report.blockers,
            "thread_order_or_subject_mismatch",
            thread_order_mismatches,
        )
        _add_finding(
            report.blockers,
            "long_gap_continuation_without_back_reference",
            long_gap_continuations,
        )

        sample_reasons: dict[str, set[str]] = {}

        def add_samples(rows: Iterable[sqlite3.Row], reason: str) -> None:
            for row in rows:
                sample_reasons.setdefault(str(row["id"]), set()).add(reason)

        add_samples(_stratified(event_rows, 24), "date_stratified")
        add_samples(
            sorted(event_rows, key=lambda row: abs(float(row["emotional_weight"])), reverse=True)[:8],
            "high_emotional_weight",
        )
        add_samples(
            sorted(event_rows, key=lambda row: float(row["importance"]), reverse=True)[:8],
            "high_importance",
        )
        add_samples(
            sorted(event_rows, key=lambda row: len(str(row["summary"])), reverse=True)[:8],
            "long_summary",
        )
        for event_id in thread_event_ids:
            if event_id in event_by_id:
                sample_reasons.setdefault(event_id, set()).add("thread_node")
        for code, ids in content_flag_ids.items():
            for event_id in ids:
                sample_reasons.setdefault(event_id, set()).add(code)
        report.manual_event_sample = [
            {
                "event_id": event_id,
                "active_date": str(event_by_id[event_id]["active_date"]),
                "reason_codes": sorted(reasons),
            }
            for event_id, reasons in sorted(
                sample_reasons.items(),
                key=lambda item: (
                    str(event_by_id[item[0]]["active_date"]),
                    item[0],
                ),
            )
        ]
        compact_reason_codes: dict[str, set[str]] = {}

        def add_compact_samples(rows: Iterable[sqlite3.Row], reason: str) -> None:
            for row in rows:
                compact_reason_codes.setdefault(str(row["id"]), set()).add(reason)

        add_compact_samples(_stratified(compact_rows, 24), "date_stratified")
        compact_by_id = {str(row["id"]): row for row in compact_rows}
        for reason, ids in (
            ("above_prompt_target", compact_above_target),
            ("absence_presented_as_conclusion", compact_absence_flags),
            ("possible_assumed_stance", compact_stance_flags),
            ("sensitive_or_network_text", compact_sensitive_flags),
            ("mostly_ascii_summary", compact_ascii_flags),
        ):
            for item_id in ids:
                compact_reason_codes.setdefault(item_id, set()).add(reason)
        report.manual_compact_sample = [
            {
                "compact_item_id": item_id,
                "active_date": str(compact_by_id[item_id]["active_date"]),
                "reason_codes": sorted(reasons),
            }
            for item_id, reasons in sorted(
                compact_reason_codes.items(),
                key=lambda item: (
                    str(compact_by_id[item[0]]["active_date"]),
                    item[0],
                ),
            )
        ]
        report.counts["manual_event_sample"] = len(report.manual_event_sample)
        report.counts["manual_compact_sample"] = len(report.manual_compact_sample)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-db", required=True)
    parser.add_argument("--conversation-db", required=True)
    parser.add_argument("--expected-sha256", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = audit_candidate(
        args.memory_db,
        args.conversation_db,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(report.safe_dict(), ensure_ascii=False, indent=2))
    return 2 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
