"""Owner/AI-only merged reads. No replication, visits, gifts or unread writes."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time

from .store import _encode_feed_cursor


class TimelineError(ValueError):
    pass


def _decode(cursor, actor, visibility):
    try:
        if not cursor or len(cursor) > 16000:
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
        if data['v'] != 1 or data['actor'] != actor or data['visibility'] != visibility:
            raise ValueError()
        if not math.isfinite(data['at']) or data['at'] < 0:
            raise ValueError()
        positions = data['positions']
        if not isinstance(positions, dict) or len(positions) > 33:
            raise ValueError()
        for key, point in positions.items():
            if not isinstance(key, str) or len(key) > 100:
                raise ValueError()
            if point is not None and (not isinstance(point, list) or len(point) != 2
                    or not isinstance(point[0], (int, float)) or not math.isfinite(point[0])
                    or point[0] < 0 or point[0] > data['at'] or not isinstance(point[1], str)
                    or len(point[1]) > 200):
                raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise TimelineError('invalid_timeline_cursor') from None


class SocialTimeline:
    def __init__(self, local, sites, *, actor='aning', remote_read=None):
        if actor not in {'aning', 'k'}:
            raise TimelineError('invalid_timeline_actor')
        self.local, self.sites, self.actor = local, sites, actor
        self.remote_read = remote_read or self._remote

    async def _remote(self, site, params):
        from .remote_client import RemoteSocialClient
        client = RemoteSocialClient()
        kind = 'human' if self.actor == 'aning' else 'ai'
        me = await client.request(site, kind, 'GET', '/social/v1/me')
        if (not (me.get('actor') or {}).get('actor_id')
                or me.get('can_manage_avatar') is not (kind == 'human')):
            raise TimelineError('timeline_identity_unavailable')
        page = await client.request(site, kind, 'GET', '/social/v1/moments', params=params)
        return {**page, 'viewer_actor': me['actor']['actor_id']}

    async def read(self, *, limit=30, visibility='all', cursor=None):
        # AI/tool callers keep the complete, authoritative page contract.
        async for event in self.events(limit=limit, visibility=visibility, cursor=cursor):
            if event['type'] == 'complete':
                return event['page']

    async def events(self, *, limit=30, visibility='all', cursor=None):
        """Ephemeral owner-UI progress; only the final event has a paging cursor."""
        if visibility not in {'all', 'private', 'public'}:
            raise TimelineError('invalid_visibility_filter')
        limit = max(1, min(30, int(limit)))
        data = _decode(cursor, self.actor, visibility) if cursor else {
            'v': 1, 'actor': self.actor, 'visibility': visibility,
            'at': time.time(), 'positions': {}}
        eligible = [s for s in self.sites if s.enabled and
                    (s.human_key if self.actor == 'aning' else s.ai_key)] if visibility != 'private' else []
        omitted = max(0, len(eligible) - 32)
        eligible = eligible[:32]
        sources = [('', None), *[(s.id, s) for s in eligible]]
        gate = asyncio.Semaphore(4)

        async def source_page(identifier, site):
            if identifier in data['positions'] and data['positions'][identifier] is None:
                return identifier, [], False, None
            point = data['positions'].get(identifier) or [data['at'], '~']
            try:
                # The deadline includes queueing: many offline homes must not
                # turn one page into several consecutive 18-second batches.
                async with asyncio.timeout(18):
                    async with gate:
                        if site is None:
                            page = await self.local.list_moments(limit=limit,
                                visibility=None if visibility == 'all' else visibility,
                                cursor=_encode_feed_cursor(*point))
                        else:
                            page = await self.remote_read(site, {'limit': limit,
                                'before_time': point[0], 'before_id': point[1]})
                rows = page.get('items') or []
                if not isinstance(rows, list) or len(rows) > 100:
                    raise TimelineError('invalid_source_page')
                valid = []
                for item in rows:
                    stamp, ident = float(item['created_at']), str(item['id'])
                    if not math.isfinite(stamp) or stamp < 0 or not ident or len(ident) > 200:
                        raise TimelineError('invalid_source_page')
                    if (stamp, ident) >= tuple(point):
                        raise TimelineError('source_cursor_not_honored')
                    # A foreign server never receives local private material.
                    # Reject (rather than relabel) any explicit remote private row.
                    if site and item.get('visibility', 'public') != 'public':
                        raise TimelineError('remote_private_row_rejected')
                    mode = item.get('hosting_mode') or ('hosted' if str(item.get('author','')).startswith('visitor:') else 'home')
                    valid.append({**item, 'id': ident, 'created_at': stamp,
                        'visibility': item.get('visibility', 'public'),
                        'source': {'site_id': identifier, 'site_name': site.name if site else '本家',
                                   'origin': site.origin if site else '', 'hosting_mode': mode},
                        'viewer_actor': page.get('viewer_actor') or self.actor})
                valid.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
                return identifier, valid, bool(page.get('has_more')), None
            except Exception:
                # Remote exceptions and URLs may contain credentials. Expose
                # only an opaque source ID and a fixed, safe availability code.
                return identifier, [], False, {'site_id': identifier,
                    'site_name': site.name if site else '本家', 'status': 'unavailable'}

        yield {'type': 'begin', 'total_sources': len(sources), 'omitted_sources': omitted}
        tasks = [asyncio.create_task(source_page(*source)) for source in sources]
        pages = []
        try:
            for task in asyncio.as_completed(tasks):
                page = await task
                pages.append(page)
                identifier, rows, _, failure = page
                yield {'type': 'source', 'site_id': identifier, 'items': rows,
                       'unavailable': failure, 'finished_sources': len(pages), 'total_sources': len(sources)}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        combined = sorted([r for _, rows, _, _ in pages for r in rows],
            key=lambda r: (r['created_at'], r['id'], r['source']['site_id']), reverse=True)
        # Identical IDs/text in distinct homes are distinct hosted posts.
        unique, known = [], set()
        for row in combined:
            ref = (row['source']['site_id'], row['id'])
            if ref not in known:
                unique.append(row)
                known.add(ref)
        items = unique[:limit]
        more = False
        for identifier, rows, source_more, failure in pages:
            if failure:
                continue
            consumed = [r for r in items if r['source']['site_id'] == identifier]
            if consumed:
                last = consumed[-1]
                data['positions'][identifier] = [last['created_at'], last['id']]
            remaining = len(rows) > len(consumed)
            if not source_more and not remaining:
                data['positions'][identifier] = None
            more |= source_more or remaining
        # Never include stale/deleted registrations in a cursor or response.
        allowed = {key for key, _ in sources}
        data['positions'] = {k:v for k,v in data['positions'].items() if k in allowed}
        encoded = base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode()
        yield {'type': 'complete', 'page': {'items': items, 'has_more': bool(more), 'next_cursor': encoded if more else None,
                'sources': [{'site_id': key, 'site_name': site.name if site else '本家'} for key,site in sources],
                'unavailable': [p[3] for p in pages if p[3]], 'omitted_sources': omitted}}
