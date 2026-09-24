"""Overflow sharding and all-or-nothing assembly for event encoding."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Sequence

from .conversation_source import ConversationMessage, ConversationSource, digest_messages
from .encoding import ParsedEncoding
from .models import (
    BatchSourceRef,
    EncodingBatch,
    EventDraft,
    SourceRef,
    digest_batch_sources,
)
from .replay import (
    DEFAULT_MAX_ESTIMATED_TOKENS,
    ReplayBatchPlan,
    estimate_tokens,
)


class OverflowAssemblyError(ValueError):
    """Overflow shards cannot be proven complete and consistent."""


@dataclass(frozen=True)
class EncodingShardResult:
    plan: ReplayBatchPlan
    encoding: ParsedEncoding


ShardEncoder = Callable[
    [ReplayBatchPlan, Sequence[ConversationMessage]],
    Awaitable[ParsedEncoding],
]


def _shard_id(
    *,
    root_batch_id: str,
    parent_batch_id: str,
    shard_depth: int,
    source_digest: str,
) -> str:
    identity = "\0".join(
        (root_batch_id, parent_batch_id, str(shard_depth), source_digest)
    )
    return "shard_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def build_overflow_followup(
    source: ConversationSource,
    parent: EncodingShardResult,
) -> ReplayBatchPlan:
    """Create a deterministic child plan from one validated overflow response."""

    _validate_node_sources(parent)
    if not parent.encoding.overflow or not parent.encoding.overflow_source_refs:
        raise OverflowAssemblyError("parent encoding has no overflow to follow up")
    source_by_ref = {
        f"s{index + 1}": batch_source
        for index, batch_source in enumerate(parent.plan.batch_sources)
    }
    try:
        selected_sources = tuple(
            source_by_ref[source_ref]
            for source_ref in parent.encoding.overflow_source_refs
        )
    except KeyError as exc:
        raise OverflowAssemblyError("overflow source ref is outside its parent") from exc
    if len(selected_sources) >= len(parent.plan.batch_sources):
        raise OverflowAssemblyError("overflow follow-up does not narrow the source range")
    messages = source.read_batch(selected_sources)
    if len(messages) != len(selected_sources) or any(
        message.as_batch_source_ref() != batch_source
        for message, batch_source in zip(messages, selected_sources)
    ):
        raise OverflowAssemblyError("overflow source rows cannot be reconstructed")

    first = messages[0]
    last = messages[-1]
    source_digest = digest_messages(messages)
    root_batch_id = parent.plan.root_batch_id or parent.plan.batch.batch_id
    shard_depth = parent.plan.shard_depth + 1
    batch = EncodingBatch(
        source_namespace=parent.plan.batch.source_namespace,
        session_id=first.session_id,
        active_date=first.active_date,
        from_message_row_id=first.row_id,
        to_message_row_id=last.row_id,
        from_message_id=first.message_id,
        to_message_id=last.message_id,
        source_count=len(messages),
        source_digest=source_digest,
        encoder_version=parent.plan.batch.encoder_version,
        prompt_version=parent.plan.batch.prompt_version,
        batch_id=_shard_id(
            root_batch_id=root_batch_id,
            parent_batch_id=parent.plan.batch.batch_id,
            shard_depth=shard_depth,
            source_digest=source_digest,
        ),
    )
    tokens = sum(estimate_tokens(message.content) for message in messages)
    return ReplayBatchPlan(
        batch=batch,
        batch_sources=selected_sources,
        estimated_tokens=tokens,
        content_chars=sum(len(message.content) for message in messages),
        boundary_reason="overflow_followup",
        oversized_single_message=(
            len(messages) == 1 and tokens > DEFAULT_MAX_ESTIMATED_TOKENS
        ),
        root_batch_id=root_batch_id,
        parent_batch_id=parent.plan.batch.batch_id,
        shard_depth=shard_depth,
    )


def _event_signature(event: EventDraft) -> str:
    value = asdict(event)
    value.pop("ordinal", None)
    value.pop("event_id", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_node_sources(node: EncodingShardResult) -> None:
    batch = node.plan.batch
    manifest = node.plan.batch_sources
    if not manifest:
        raise OverflowAssemblyError("overflow shard manifest cannot be empty")
    if len({source.message_id for source in manifest}) != len(manifest):
        raise OverflowAssemblyError("overflow shard manifest contains duplicate sources")
    if (
        batch.source_count != len(manifest)
        or batch.source_digest != digest_batch_sources(manifest)
        or batch.from_message_row_id != manifest[0].message_row_id
        or batch.to_message_row_id != manifest[-1].message_row_id
        or batch.from_message_id != manifest[0].message_id
        or batch.to_message_id != manifest[-1].message_id
        or any(source.session_id != batch.session_id for source in manifest)
        or any(source.active_date != batch.active_date for source in manifest)
    ):
        raise OverflowAssemblyError("overflow shard batch disagrees with its manifest")

    allowed = {source.message_id: source for source in manifest}
    emitted: set[str] = set()
    for event in node.encoding.events:
        for source in event.sources:
            expected = allowed.get(source.message_id)
            if expected is None or not _source_matches_manifest(source, expected):
                raise OverflowAssemblyError(
                    "shard event source does not match its manifest"
                )
            emitted.add(source.message_id)
    local_to_id = {
        f"s{index + 1}": source.message_id
        for index, source in enumerate(node.plan.batch_sources)
    }
    try:
        overflow = {
            local_to_id[source_ref]
            for source_ref in node.encoding.overflow_source_refs
        }
        non_event = {
            local_to_id[source_ref]
            for source_ref in node.encoding.non_event_source_refs
        }
    except KeyError as exc:
        raise OverflowAssemblyError(
            "shard accounting ref is outside its manifest"
        ) from exc
    if emitted & overflow:
        raise OverflowAssemblyError("emitted and overflow sources must be disjoint")
    if non_event & (emitted | overflow):
        raise OverflowAssemblyError(
            "non-event, emitted, and overflow sources must be disjoint"
        )


def _source_matches_manifest(
    source: SourceRef,
    expected: BatchSourceRef,
) -> bool:
    return (
        source.message_row_id == expected.message_row_id
        and source.message_id == expected.message_id
        and source.session_id == expected.session_id
        and source.source_ts == expected.source_ts
        and source.source_role == expected.source_role
    )


def assemble_complete_encoding(
    root_plan: ReplayBatchPlan,
    shard_results: Sequence[EncodingShardResult],
) -> ParsedEncoding:
    """Merge a complete overflow tree for one root batch.

    Any missing, extra, overlapping, or still-overflowing leaf rejects the
    whole assembly.  The caller may commit the returned events to the root
    batch only after this succeeds.
    """

    if not shard_results:
        raise OverflowAssemblyError("encoding assembly requires a root result")
    by_id: dict[str, EncodingShardResult] = {}
    for node in shard_results:
        batch_id = node.plan.batch.batch_id
        if batch_id in by_id:
            raise OverflowAssemblyError(f"duplicate shard result: {batch_id}")
        _validate_node_sources(node)
        by_id[batch_id] = node

    root_id = root_plan.batch.batch_id
    root = by_id.get(root_id)
    if root is None or root.plan != root_plan:
        raise OverflowAssemblyError("root encoding result does not match the root plan")
    if root.plan.root_batch_id not in {"", root_id} or root.plan.shard_depth != 0:
        raise OverflowAssemblyError("root shard identity is inconsistent")

    root_order = {
        source.message_id: index
        for index, source in enumerate(root_plan.batch_sources)
    }

    children: dict[str, list[EncodingShardResult]] = {}
    for node in shard_results:
        if node.plan.batch.batch_id == root_id:
            if node.plan.parent_batch_id:
                raise OverflowAssemblyError("root shard cannot have a parent")
            continue
        if node.plan.root_batch_id != root_id:
            raise OverflowAssemblyError("shard belongs to a different root batch")
        if not node.plan.parent_batch_id:
            raise OverflowAssemblyError("non-root shard is missing its parent")
        children.setdefault(node.plan.parent_batch_id, []).append(node)

    visited: set[str] = set()
    ordered_nodes: list[EncodingShardResult] = []

    def visit(node: EncodingShardResult) -> None:
        node_id = node.plan.batch.batch_id
        if node_id in visited:
            raise OverflowAssemblyError("overflow shard tree contains a cycle")
        visited.add(node_id)
        ordered_nodes.append(node)
        direct_children = children.get(node_id, [])
        if not node.encoding.overflow:
            if direct_children:
                raise OverflowAssemblyError("complete shard unexpectedly has children")
            return
        if not direct_children:
            raise OverflowAssemblyError("overflow shard is missing a follow-up result")

        local_to_id = {
            f"s{index + 1}": source.message_id
            for index, source in enumerate(node.plan.batch_sources)
        }
        expected = {
            local_to_id[source_ref]
            for source_ref in node.encoding.overflow_source_refs
        }
        actual: set[str] = set()
        for child in direct_children:
            if child.plan.shard_depth != node.plan.shard_depth + 1:
                raise OverflowAssemblyError("overflow shard depth is inconsistent")
            child_ids = {source.message_id for source in child.plan.batch_sources}
            if actual & child_ids:
                raise OverflowAssemblyError("overflow child manifests overlap")
            actual.update(child_ids)
        if actual != expected:
            raise OverflowAssemblyError("overflow child manifests do not cover parent refs")
        for child in sorted(
            direct_children,
            key=lambda item: min(
                root_order[source.message_id]
                for source in item.plan.batch_sources
            ),
        ):
            visit(child)

    visit(root)
    if visited != set(by_id):
        raise OverflowAssemblyError("assembly contains unreachable shard results")

    unique_events: list[tuple[int, int, EventDraft]] = []
    seen_signatures: set[str] = set()
    discovery_order = 0
    for node in ordered_nodes:
        for event in node.encoding.events:
            signature = _event_signature(event)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            try:
                source_order = min(
                    root_order[source.message_id] for source in event.sources
                )
            except KeyError as exc:
                raise OverflowAssemblyError(
                    "assembled event source is outside the root manifest"
                ) from exc
            unique_events.append((source_order, discovery_order, event))
            discovery_order += 1
    unique_events.sort(key=lambda item: (item[0], item[1]))
    events = tuple(
        replace(event, ordinal=ordinal, event_id="")
        for ordinal, (_, _, event) in enumerate(unique_events)
    )
    root_ref_by_id = {
        source.message_id: f"s{index + 1}"
        for index, source in enumerate(root_plan.batch_sources)
    }
    non_event_ids: set[str] = set()
    for node in ordered_nodes:
        local_to_id = {
            f"s{index + 1}": source.message_id
            for index, source in enumerate(node.plan.batch_sources)
        }
        non_event_ids.update(
            local_to_id[source_ref]
            for source_ref in node.encoding.non_event_source_refs
        )
    non_event_source_refs = tuple(
        root_ref_by_id[message_id]
        for message_id in sorted(non_event_ids, key=root_order.__getitem__)
    )
    ignored_episode_fields = tuple(
        sorted(
            {
                field
                for node in ordered_nodes
                for field in node.encoding.ignored_episode_fields
            }
        )
    )
    projected_episode_fields = tuple(
        sorted(
            {
                field
                for node in ordered_nodes
                for field in node.encoding.projected_episode_fields
            }
        )
    )
    provider_episode_count = sum(
        node.encoding.provider_episode_count for node in ordered_nodes
    )
    backend_overflow_applied = any(
        node.encoding.backend_overflow_applied for node in ordered_nodes
    )
    backend_deferred_source_count = sum(
        node.encoding.backend_deferred_source_count for node in ordered_nodes
    )
    backend_time_projection_count = sum(
        node.encoding.backend_time_projection_count for node in ordered_nodes
    )
    return ParsedEncoding(
        events=events,
        overflow=False,
        overflow_source_refs=(),
        non_event_source_refs=non_event_source_refs,
        ignored_episode_fields=ignored_episode_fields,
        projected_episode_fields=projected_episode_fields,
        provider_episode_count=provider_episode_count,
        backend_overflow_applied=backend_overflow_applied,
        backend_deferred_source_count=backend_deferred_source_count,
        backend_time_projection_count=backend_time_projection_count,
    )


async def encode_complete_tree(
    source: ConversationSource,
    root_plan: ReplayBatchPlan,
    encode_shard: ShardEncoder,
    *,
    max_shards: int,
) -> tuple[ParsedEncoding, tuple[EncodingShardResult, ...]]:
    """Encode one root and all overflow follow-ups without persistent writes."""

    if max_shards <= 0:
        raise ValueError("max_shards must be positive")
    results: list[EncodingShardResult] = []
    plan = root_plan
    while True:
        if len(results) >= max_shards:
            raise OverflowAssemblyError("overflow shard request limit reached")
        messages = source.read_batch(plan.batch_sources)
        if len(messages) != len(plan.batch_sources) or any(
            message.as_batch_source_ref() != batch_source
            for message, batch_source in zip(messages, plan.batch_sources)
        ):
            raise OverflowAssemblyError("shard source rows cannot be reconstructed")
        encoding = await encode_shard(plan, messages)
        node = EncodingShardResult(plan=plan, encoding=encoding)
        results.append(node)
        if not encoding.overflow:
            break
        plan = build_overflow_followup(source, node)

    merged = assemble_complete_encoding(root_plan, results)
    return merged, tuple(results)
