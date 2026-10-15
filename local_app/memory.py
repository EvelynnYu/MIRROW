"""Source-validated Memory V2 encoding and recall, with injected transport."""
import asyncio
import json
from datetime import date
from cognition import books
from memory_v2.conversation_source import ConversationSource
from memory_v2.replay import ReplayPlanner
from memory_v2.store import MemoryV2Store
from memory_v2.encoding import build_encoding_messages, parse_encoding_output, assess_encoding_quality
from memory_v2.overflow import encode_complete_tree
from memory_v2.day_settlement_orchestrator import settle_completed_day
from memory_v2.recall import EventRecallIndex, RecallQuery
from memory_v2.recall_service import run_shadow_recall
from mirrow_core.model_provenance import day_model_evidence, PROVENANCE_GUIDE


class Memory:
    def __init__(self, config, store, provider):
        self.provider = provider
        self.store = store
        self.source = ConversationSource(store.path, canonical_sessions_only=True)
        self.events = MemoryV2Store(config.data_dir / 'memory_v2.db')
        self.lock = asyncio.Lock()
        self.days = None
        books.ROOT = config.data_dir / 'cognition'

    @staticmethod
    def core_context():
        return books.core_context()

    async def search(self, query):
        if not query.strip():
            return []
        execution = run_shadow_recall(EventRecallIndex(self.events.db_path), self.source,
            RecallQuery(text=query), reference_date=date.today(), limit=5, force_source_detail=True)
        evidence = {group.rank: group for group in execution.evidence.groups}
        result = []
        for rank, hit in enumerate(execution.result.hits, 1):
            group = evidence.get(rank)
            result.append({'id': hit.document.document_id, 'summary': '\n'.join(hit.document.summaries),
                'event_ids': list(hit.document.event_ids), 'source_message_ids': list(hit.document.source_message_ids),
                'source_missing': group.missing_anchor_count if group else None,
                'sources': [{key: getattr(message, key) for key in ('message_id', 'timestamp', 'role',
                    'content', 'content_truncated', 'span_selected', 'span_fallback')}
                    for message in group.messages] if group else []})
        return result

    async def encode(self, active_date):
        day = date.fromisoformat(active_date)
        closed = self.days.get(active_date) if self.days else None
        if closed and closed['state'] == 'closed':
            self.days.verify(active_date)
        elif day >= date.today() or (closed and closed['state'] == 'open'):
            raise ValueError('only_completed_calendar_days')
        async with self.lock:
            frozen_rows = [r for r in self.store.messages() if r['active_date'] == active_date]
            day_provenance = day_model_evidence(frozen_rows)
            def verify_day():
                if closed and closed['state'] == 'closed':
                    self.days.verify(active_date)

            def with_provenance(messages, evidence):
                return [*messages, {'role': 'system', 'content': PROVENANCE_GUIDE},
                        {'role': 'user', 'content': '【模型来源与运行事实；不是可引用的聊天原话】\n' + json.dumps(evidence, ensure_ascii=False)}]

            plan = ReplayPlanner(self.source).plan_active_date(active_date)
            reports = []
            for batch in plan.batches:
                cached = self.events.load_completed_events(batch.batch, batch_sources=batch.batch_sources,
                    batch_source_validator=self.source.validate_batch_sources)
                if cached is not None:
                    reports.append({'status': 'cached'})
                    continue

                async def encode_shard(shard, messages):
                    verify_day()
                    ids = {message.message_id for message in messages}
                    evidence = day_model_evidence([r for r in frozen_rows if r['message_id'] in ids or r['event_type'] == 'model_switch'])
                    # Batch source refs bind the sidecar; original text/span offsets stay intact.
                    refs = {message.message_id: f's{i + 1}' for i, message in enumerate(messages)}
                    for item in evidence['message_models']:
                        item['source_ref'] = refs[item['message_id']]
                    response = await self.provider.complete(with_provenance(build_encoding_messages(shard, messages), evidence),
                        internal=True, temperature=0, max_tokens=16384)
                    verify_day()
                    return parse_encoding_output(response['content'], shard, messages)

                parsed, _ = await encode_complete_tree(self.source, batch, encode_shard, max_shards=8)
                quality = assess_encoding_quality(parsed, batch)
                if quality.review_required:
                    reports.append({'status': 'review_required', 'quality': quality.safe_dict()})
                    continue
                verify_day()
                ids = self.events.commit_encoding(batch.batch, parsed.events, batch_sources=batch.batch_sources,
                    source_validator=self.source.validate_refs, batch_source_validator=self.source.validate_batch_sources)
                reports.append({'status': 'committed', 'event_ids': ids})
            if any(item['status'] == 'review_required' for item in reports):
                return {'status': 'review_required', 'batches': reports, 'settlement': None}
            if not plan.batches:
                return {'status': 'no_source_messages', 'batches': [], 'settlement': None}

            async def model_call(*, messages, schema_name, schema, max_tokens):
                verify_day()
                response = await self.provider.complete(with_provenance(messages, day_provenance), internal=True, temperature=0,
                    max_tokens=max_tokens)
                verify_day()
                return response['content']

            settled = await settle_completed_day(self.events, active_date=active_date,
                completed_before_active_date=(day.fromordinal(day.toordinal() + 1).isoformat() if closed else date.today().isoformat()), model_call=model_call, source_namespace='mainline')
            verify_day()
            return {'status': settled.status, 'batches': reports, 'settlement': settled.safe_dict()}
