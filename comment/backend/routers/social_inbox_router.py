"""Local human inbox; outbound identities stay server-side, every ACK names its source."""
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from routers.lounge_reception_router import local_ui
from social_feed import get_social_feed_store
from social_feed.remote_client import RemoteSocialClient
from social_feed.remote_sites import get_remote_site_store, RemoteSiteError
from social_feed.human_inbox import collect

router=APIRouter(prefix='/inbox',dependencies=[Depends(local_ui)])


class ReadInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    site_id:str=Field(default='',max_length=100)
    notification_ids:list[str]=Field(default_factory=list,max_length=100)


async def remote(site_id,method,path,*,payload=None):
    try:
        site=get_remote_site_store().get(site_id)
        if not site.enabled:raise RemoteSiteError('social_site_disabled')
        client=RemoteSocialClient()
        me=await client.request(site,'human','GET','/social/v1/me',params={'arrival':'false'})
        if me.get('can_manage_avatar') is not True or not (me.get('actor') or {}).get('actor_id'):
            raise RemoteSiteError('social_site_key_kind_mismatch')
        result=await client.request(site,'human',method,path,payload=payload)
        return result,site,me
    except RemoteSiteError as exc:
        raise HTTPException(502,str(exc)) from None


@router.get('')
async def inbox():
    result=await collect(get_social_feed_store(),get_remote_site_store().list())
    from lounge_reception.runtime import current_runtime
    from social_feed.public_gateway import _display
    runtime=current_runtime()
    if runtime:
        for row in result['items']:
            if not row['site_id']:
                row['actor_name']=_display(runtime,row['actor'],viewer='human')['name']
    return result


@router.post('/read')
async def acknowledge(body:ReadInput):
    if not body.notification_ids:return {'marked':0}
    if any(not value or len(value)>100 for value in body.notification_ids):
        raise HTTPException(400,'invalid_notification_claim')
    if body.site_id:
        result,_,_=await remote(body.site_id,'POST','/social/v1/notifications/read',
                               payload={'notification_ids':body.notification_ids})
        return {'marked':result.get('marked',0)}
    count=await get_social_feed_store().mark_notifications_read('human',body.notification_ids,
                                                              read_source='human_inbox')
    return {'marked':count}


@router.get('/moment')
async def open_moment(moment_id:str,site_id:str=''):
    if not moment_id or len(moment_id)>200:raise HTTPException(400,'invalid_moment')
    if site_id:
        item,site,me=await remote(site_id,'GET','/social/v1/moments/'+quote(moment_id,safe=''))
        if item.get('migrated'):raise HTTPException(410,'动态已迁移，请到原域查看')
        if item.get('id')!=moment_id or item.get('visibility','public')!='public':
            raise HTTPException(502,'动态来源未确认，原通知保留')
        item['source']={'site_id':site.id,'site_name':site.name,'origin':site.origin,
                        'hosting_mode':item.get('hosting_mode','hosted')}
        item['viewer_actor']=me['actor']['actor_id']
        item['visibility']='public'
    else:
        item=await get_social_feed_store().get_moment(moment_id)
        if not item:raise HTTPException(404,'动态已删除或无法读取')
        from lounge_reception.runtime import current_runtime
        from social_feed.public_gateway import _decorate
        from routers.social_feed_router import _local_avatar
        runtime=current_runtime()
        if runtime:
            item=_decorate(runtime,item,viewer='human')
            for person in item.get('people',{}).values():_local_avatar(person)
        item['source']={'site_id':'','site_name':'本家','origin':'','hosting_mode':'home'}
        item['viewer_actor']='human'
    return item
