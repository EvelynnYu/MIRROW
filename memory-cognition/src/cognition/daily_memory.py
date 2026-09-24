"""Bounded, read-only history lookup. Recalled summaries are not primary facts."""
import asyncio
from pathlib import Path
from . import books


async def recall(messages):
    if books.ROOT != Path(__file__).resolve().parents[1]:
        return []
    from ombre_brain_client import get_ob_client
    client = get_ob_client()
    query = '\n'.join(m['text'] for m in messages[-8:])
    if not query:
        return []
    async def search():
        await client.ensure_initialized()
        result = []
        for hit in await client.search(query, top_k=8, include_dormant=False):
            bucket = hit.bucket or await client.bucket_mgr.get(hit.bucket_id)
            if not bucket:
                continue
            meta = bucket.get('metadata', {})
            if meta.get('status') in {'archived', 'dormant'}:
                continue
            result.append({'id': str(hit.bucket_id), 'text': str(bucket.get('content', '')),
                           'date': str(meta.get('updated_at') or meta.get('created_at') or ''),
                           'source': 'recalled_summary_not_primary_statement'})
        return result
    return await asyncio.wait_for(search(), timeout=25)
