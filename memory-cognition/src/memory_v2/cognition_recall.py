"""Read-only recall projection for current cognition-book entries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from behavior_scheduler.bookshelf_search import sanitize_archive_text

from .recall import (
    EventRecallIndex,
    RecallDocument,
    RecallSourceAnchor,
    SemanticScorer,
)


CognitionEntryLoader = Callable[[], Sequence[Mapping[str, Any]]]
CognitionMessageResolver = Callable[[Sequence[str]], Sequence[str]]
COGNITION_DOMAINS = ("self", "other", "world", "scp")


def load_current_cognition_entries() -> list[Mapping[str, Any]]:
    """Read Markdown authorities without migrating or mutating them."""

    from cognition import books

    return [
        entry
        for domain in COGNITION_DOMAINS
        for entry in books.catalog(domain)
    ]


def _iso_day(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return datetime.fromisoformat(clean).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(clean, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return ""


def _effective(entry: Mapping[str, Any], now: datetime) -> bool:
    if str(entry.get("state") or "active") != "active":
        return False
    expires_at = str(entry.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    reference = now
    if expires.tzinfo is not None:
        reference = now.astimezone(expires.tzinfo) if now.tzinfo else datetime.now(
            expires.tzinfo
        )
    elif now.tzinfo is not None:
        reference = now.replace(tzinfo=None)
    return reference < expires


def _latest_provenance(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    links = entry.get("maintenance_links")
    if not isinstance(links, list):
        return {}
    return next((item for item in reversed(links) if isinstance(item, dict)), {})


class CognitionRecallIndex(EventRecallIndex):
    """Project current cognition as attributed beliefs, not event facts."""

    def __init__(
        self,
        authority_db_path: str | Path,
        *,
        entry_loader: CognitionEntryLoader = load_current_cognition_entries,
        message_id_resolver: CognitionMessageResolver | None = None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        semantic_scorer: SemanticScorer | None = None,
    ):
        super().__init__(authority_db_path, semantic_scorer=semantic_scorer)
        self.entry_loader = entry_loader
        self.message_id_resolver = message_id_resolver
        self.now = now

    def _resolve_message_ids(self, message_ids: Sequence[str]) -> set[str]:
        if not message_ids:
            return set()
        if self.message_id_resolver is not None:
            return {
                str(message_id)
                for message_id in self.message_id_resolver(message_ids)
            }
        try:
            from .conversation_source import ConversationSource

            return {
                message.message_id
                for message in ConversationSource(self.db_path).read_message_ids(
                    message_ids
                )
            }
        except Exception:
            return set()

    def _load_documents(self) -> list[RecallDocument]:
        current = self.now()
        entries = [
            entry
            for entry in self.entry_loader()
            if isinstance(entry, Mapping) and _effective(entry, current)
        ]
        requested_ids = tuple(
            dict.fromkeys(
                str(message_id).strip()
                for entry in entries
                for message_id in (
                    _latest_provenance(entry).get("evidence_ids")
                    if isinstance(
                        _latest_provenance(entry).get("evidence_ids"), list
                    )
                    else ()
                )
                if str(message_id).strip()
            )
        )
        resolvable_ids = self._resolve_message_ids(requested_ids)
        documents: list[RecallDocument] = []
        for entry in entries:
            domain = str(entry.get("domain") or "").strip()
            entry_id = str(entry.get("entry_id") or "").strip()
            revision = str(entry.get("revision") or "").strip()
            name = sanitize_archive_text(str(entry.get("name") or ""), limit=300)
            body = sanitize_archive_text(str(entry.get("body") or ""), limit=12_000)
            if domain not in COGNITION_DOMAINS or not entry_id or not revision:
                continue
            if not name or not body:
                continue
            provenance = _latest_provenance(entry)
            raw_evidence = provenance.get("evidence_ids")
            evidence_ids = tuple(
                dict.fromkeys(
                    str(message_id).strip()
                    for message_id in (
                        raw_evidence if isinstance(raw_evidence, list) else ()
                    )
                    if str(message_id).strip()
                    and str(message_id).strip() in resolvable_ids
                )
            )
            source_day = _iso_day(provenance.get("source_date"))
            updated_day = _iso_day(entry.get("updated_at"))
            effective_day = source_day or updated_day
            subject_id = str(entry.get("subject_id") or "unknown")
            knower_id = str(entry.get("knower_id") or "unknown")
            kind = str(entry.get("kind") or "").strip()
            aliases = (
                entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
            )
            keywords = (
                entry.get("keywords") if isinstance(entry.get("keywords"), list) else []
            )
            labels = tuple(
                sanitize_archive_text(str(value), limit=200)
                for value in (*aliases, *keywords)
                if str(value).strip()
            )
            basis = str(entry.get("basis") or "").strip()
            if basis in {"self_report", "mutual_agreement"}:
                confidence = 0.95
            elif basis == "observed":
                confidence = 0.85
            elif basis == "inferred" or domain in {"self", "other"}:
                confidence = 0.7
            elif evidence_ids:
                confidence = 0.85
            else:
                confidence = 0.55
            importance = 0.9 if entry.get("core") is True else {
                "self": 0.78,
                "other": 0.72,
                "world": 0.68,
                "scp": 0.62,
            }[domain]
            facets = tuple(
                value
                for value in (
                    domain,
                    kind,
                    str(entry.get("category") or "").strip(),
                    str(entry.get("scope") or "").strip(),
                    basis,
                )
                if value
            )
            documents.append(
                RecallDocument(
                    document_id=f"cognition:{domain}:{entry_id}:{revision[:12]}",
                    event_ids=(),
                    source_message_ids=evidence_ids,
                    summaries=(name, *labels, body),
                    subject_ids=(subject_id,),
                    participant_ids=tuple(
                        value
                        for value in dict.fromkeys((subject_id, knower_id))
                        if value and value != "unknown"
                    ),
                    event_types=(),
                    facets=facets,
                    source_kinds=(),
                    active_date_from=effective_day,
                    active_date_to=effective_day,
                    date_from=effective_day,
                    date_to=effective_day,
                    importance=importance,
                    confidence=confidence,
                    source_anchors=tuple(
                        RecallSourceAnchor(message_id=message_id)
                        for message_id in evidence_ids
                    ),
                    source_layer="cognition",
                    display_text=f"{name}：{body}",
                    knower_ids=(knower_id,),
                )
            )
        return documents


__all__ = [
    "COGNITION_DOMAINS",
    "CognitionEntryLoader",
    "CognitionMessageResolver",
    "CognitionRecallIndex",
    "load_current_cognition_entries",
]
