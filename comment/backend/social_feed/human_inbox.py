"""Source-bound owner inbox projection. Reading this list never acknowledges anything.

Bodies and read receipts remain in each source wall. Heads are change indicators,
not another feed or evidence that the human/AI actually read the posts.
"""
import asyncio
import time

from .remote_client import RemoteSocialClient


def project(row, site_id, site_name):
    return {**{key:row.get(key) for key in ('id','actor','actor_name','kind','moment_id','comment_id','created_at')},
            'site_id':site_id,'site_name':site_name,
            'preview':str(row.get('comment_content') or row.get('moment_content') or '')[:160]}


async def collect(local, sites, *, actor='human', client=None, limit=30, with_heads=True):
    if actor not in {'human','k'}:raise ValueError('invalid_inbox_actor')
    client=client or RemoteSocialClient()
    role='human' if actor=='human' else 'ai'
    gate=asyncio.Semaphore(4)
    eligible=[s for s in sites if s.enabled and (s.human_key if role=='human' else s.ai_key)][:32]

    async def home():
        try:
            rows=await local.unread_notifications(actor,limit=limit)
            page=await local.list_moments(limit=1) if with_heads else {}
            return '', '本家', rows, page.get('items') or [], None
        except Exception:
            return '', '本家', [], [], 'unavailable'

    async def remote(site):
        try:
            async with asyncio.timeout(18):
                async with gate:
                    if with_heads:
                        me,page=await asyncio.gather(client.request(site,role,'GET','/social/v1/me',params={'arrival':'false'}),
                            client.request(site,role,'GET','/social/v1/moments',params={'limit':1}))
                    else:
                        me=await client.request(site,role,'GET','/social/v1/me',params={'arrival':'false'});page={}
                    if (not (me.get('actor') or {}).get('actor_id') or
                            me.get('can_manage_avatar') is not (role=='human')):
                        raise ValueError('inbox_identity_mismatch')
                    supported='inbox_v1' in me.get('capabilities',[])
                    inbox=await client.request(site,role,'GET','/social/v1/notifications',params={'limit':limit}) if supported else {}
            problem=None if 'visitor_interactions_v1' in me.get('capabilities',[]) else 'limited' if supported else 'unsupported'
            return site.id,site.name,inbox.get('items') or [],page.get('items') or [],problem
        except Exception:
            return site.id,site.name,[],[],'unavailable'

    results=await asyncio.gather(home(),*(remote(site) for site in eligible))
    items, heads, unavailable=[],[],[]
    for site_id,name,rows,posts,problem in results:
        items.extend(project(row,site_id,name) for row in rows if row.get('id') and row.get('moment_id'))
        if posts:
            head=posts[0]
            heads.append({'site_id':site_id,'site_name':name,'id':head['id'],'created_at':head['created_at']})
        if problem:unavailable.append({'site_id':site_id,'site_name':name,'status':problem})
    # Existing foreign-home new-post notices remain available, but are not intrusive alerts.
    for item in items:
        item['priority']='quiet' if item['kind']=='like' or (item['kind']=='moment' and item['actor']!='k') else 'normal'
    items.sort(key=lambda row:(float(row.get('created_at') or 0),str(row['id'])),reverse=True)
    return {'items':items,'heads':heads,'unavailable':unavailable,'observed_at':time.time()}
