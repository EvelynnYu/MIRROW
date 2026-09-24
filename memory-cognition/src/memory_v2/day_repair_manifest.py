"""Content-addressed manifest for one isolated Memory V2 day repair stage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .day_repair import DayRepairPlan
from .schema import SCHEMA_VERSION
from .store import MemoryV2Store


class DayRepairManifestError(RuntimeError):
    """The staging database does not exactly represent its repair plan."""


@dataclass(frozen=True)
class DayRepairStageManifest:
    result_digest: str
    batch_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    event_status_ids: tuple[str, ...]
    replacement_thread_ids: tuple[str, ...]
    thread_status_ids: tuple[str, ...]
    event_replacements: tuple[tuple[str, str, str], ...]
    thread_replacements: tuple[tuple[str, str, str], ...]
    thread_memberships: tuple[tuple[str, tuple[str, ...]], ...]


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def _placeholders(values: Sequence[object]) -> str:
    if not values:
        raise ValueError("cannot build placeholders for an empty sequence")
    return ",".join("?" for _value in values)


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_day_repair_stage_manifest(
    db_path: str | Path,
    plan: DayRepairPlan,
) -> DayRepairStageManifest:
    """Validate and hash every row introduced by one repair identity."""

    path = Path(db_path).resolve()
    if not path.is_file():
        raise DayRepairManifestError("day repair stage database is missing")
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        schema_row = connection.execute(
            "SELECT schema_version FROM memory_schema WHERE schema_key='memory_v2'"
        ).fetchone()
        if schema_row is None or int(schema_row["schema_version"]) != SCHEMA_VERSION:
            raise DayRepairManifestError("day repair stage schema is not current")

        expected_batch_ids = tuple(
            MemoryV2Store.resolve_batch_id(batch.batch)
            for batch in plan.replay_day.batches
        )
        if expected_batch_ids:
            batch_rows = _rows(
                connection,
                "SELECT * FROM encoding_batches WHERE id IN ("
                + _placeholders(expected_batch_ids)
                + ") ORDER BY id",
                expected_batch_ids,
            )
        else:
            batch_rows = []
        batch_ids = tuple(str(row["id"]) for row in batch_rows)
        if set(batch_ids) != set(expected_batch_ids) or len(batch_ids) != len(
            expected_batch_ids
        ):
            raise DayRepairManifestError("day repair stage batches are incomplete")
        if any(
            str(row["source_namespace"]) != plan.source_namespace
            or str(row["active_date"]) != plan.active_date
            or str(row["encoder_version"]) != plan.repair_encoder_version
            or str(row["status"]) != "completed"
            for row in batch_rows
        ):
            raise DayRepairManifestError("day repair stage batch identity is invalid")

        if batch_ids:
            batch_parameters = tuple(batch_ids)
            batch_source_rows = _rows(
                connection,
                "SELECT * FROM encoding_batch_sources WHERE batch_id IN ("
                + _placeholders(batch_parameters)
                + ") ORDER BY batch_id, source_order",
                batch_parameters,
            )
            event_rows = _rows(
                connection,
                "SELECT * FROM events WHERE batch_id IN ("
                + _placeholders(batch_parameters)
                + ") ORDER BY id",
                batch_parameters,
            )
        else:
            batch_source_rows = []
            event_rows = []
        event_ids = tuple(str(row["id"]) for row in event_rows)
        if any(str(row["encoder_version"]) != plan.repair_encoder_version for row in event_rows):
            raise DayRepairManifestError("day repair event encoder identity is invalid")

        if event_ids:
            event_parameters = tuple(event_ids)
            event_source_rows = _rows(
                connection,
                "SELECT * FROM event_sources WHERE event_id IN ("
                + _placeholders(event_parameters)
                + ") ORDER BY event_id, source_order",
                event_parameters,
            )
            participant_rows = _rows(
                connection,
                "SELECT * FROM event_participants WHERE event_id IN ("
                + _placeholders(event_parameters)
                + ") ORDER BY event_id, participant_order",
                event_parameters,
            )
        else:
            event_source_rows = []
            participant_rows = []

        event_status_rows = _rows(
            connection,
            "SELECT * FROM event_status_log WHERE source_revision=? ORDER BY id",
            (plan.source_revision,),
        )
        event_status_ids = tuple(str(row["id"]) for row in event_status_rows)
        if len(event_status_rows) != len(plan.previous_event_ids) or {
            str(row["event_id"]) for row in event_status_rows
        } != set(plan.previous_event_ids):
            raise DayRepairManifestError("day repair event disposition is incomplete")
        event_replacements = tuple(
            sorted(
                (
                    str(row["event_id"]),
                    str(row["status"]),
                    str(row["replacement_event_id"] or ""),
                )
                for row in event_status_rows
            )
        )
        for previous_event_id, status, replacement_event_id in event_replacements:
            if status == "superseded":
                if replacement_event_id not in set(event_ids):
                    raise DayRepairManifestError(
                        "superseded event does not target this repair generation"
                    )
            elif status == "retracted":
                if replacement_event_id:
                    raise DayRepairManifestError(
                        "retracted event unexpectedly has a replacement"
                    )
            else:
                raise DayRepairManifestError(
                    f"unsupported day repair event disposition: {status}"
                )
            latest = connection.execute(
                "SELECT id FROM event_status_log WHERE event_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (previous_event_id,),
            ).fetchone()
            if latest is None or str(latest["id"]) not in set(event_status_ids):
                raise DayRepairManifestError(
                    "day repair event disposition is not the latest state"
                )

        thread_status_rows = _rows(
            connection,
            "SELECT * FROM event_thread_status_log WHERE source_key=? ORDER BY id",
            (plan.source_revision,),
        )
        thread_status_ids = tuple(str(row["id"]) for row in thread_status_rows)
        thread_replacements = tuple(
            sorted(
                (
                    str(row["thread_id"]),
                    str(row["status"]),
                    str(row["replacement_thread_id"] or ""),
                )
                for row in thread_status_rows
            )
        )
        for previous_thread_id, status, replacement_thread_id in thread_replacements:
            if status == "superseded" and not replacement_thread_id:
                raise DayRepairManifestError(
                    "superseded thread does not identify its replacement"
                )
            if status == "retracted" and replacement_thread_id:
                raise DayRepairManifestError(
                    "retracted thread unexpectedly has a replacement"
                )
            if status not in {"superseded", "retracted"}:
                raise DayRepairManifestError(
                    f"unsupported day repair thread disposition: {status}"
                )
            latest = connection.execute(
                "SELECT id FROM event_thread_status_log WHERE thread_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (previous_thread_id,),
            ).fetchone()
            if latest is None or str(latest["id"]) not in set(thread_status_ids):
                raise DayRepairManifestError(
                    "day repair thread disposition is not the latest state"
                )

        replacement_thread_ids = tuple(
            sorted(
                replacement_thread_id
                for _previous_thread_id, status, replacement_thread_id
                in thread_replacements
                if status == "superseded"
            )
        )
        if replacement_thread_ids:
            replacement_parameters = tuple(replacement_thread_ids)
            thread_rows = _rows(
                connection,
                "SELECT * FROM event_threads WHERE id IN ("
                + _placeholders(replacement_parameters)
                + ") ORDER BY id",
                replacement_parameters,
            )
            membership_rows = _rows(
                connection,
                "SELECT * FROM thread_events WHERE thread_id IN ("
                + _placeholders(replacement_parameters)
                + ") ORDER BY thread_id, sequence_no",
                replacement_parameters,
            )
        else:
            thread_rows = []
            membership_rows = []
        if {str(row["id"]) for row in thread_rows} != set(replacement_thread_ids):
            raise DayRepairManifestError("day repair replacement thread is missing")
        memberships_by_thread: dict[str, list[str]] = {
            thread_id: [] for thread_id in replacement_thread_ids
        }
        for row in membership_rows:
            memberships_by_thread[str(row["thread_id"])].append(str(row["event_id"]))
        if any(len(event_membership) < 2 for event_membership in memberships_by_thread.values()):
            raise DayRepairManifestError(
                "day repair replacement thread is too short"
            )
        thread_memberships = tuple(
            (thread_id, tuple(memberships_by_thread[thread_id]))
            for thread_id in replacement_thread_ids
        )

        payload = {
            "repair_id": plan.repair_id,
            "authority_digest": plan.authority_digest,
            "baseline_digest": plan.baseline_digest,
            "tables": {
                "encoding_batches": batch_rows,
                "encoding_batch_sources": batch_source_rows,
                "events": event_rows,
                "event_sources": event_source_rows,
                "event_participants": participant_rows,
                "event_status_log": event_status_rows,
                "event_threads": thread_rows,
                "thread_events": membership_rows,
                "event_thread_status_log": thread_status_rows,
            },
        }
        return DayRepairStageManifest(
            result_digest=_digest(payload),
            batch_ids=batch_ids,
            event_ids=event_ids,
            event_status_ids=event_status_ids,
            replacement_thread_ids=replacement_thread_ids,
            thread_status_ids=thread_status_ids,
            event_replacements=event_replacements,
            thread_replacements=thread_replacements,
            thread_memberships=thread_memberships,
        )
    except sqlite3.Error as exc:
        raise DayRepairManifestError(
            f"cannot inspect day repair stage: {type(exc).__name__}"
        ) from exc
    finally:
        connection.close()


__all__ = [
    "DayRepairManifestError",
    "DayRepairStageManifest",
    "build_day_repair_stage_manifest",
]
