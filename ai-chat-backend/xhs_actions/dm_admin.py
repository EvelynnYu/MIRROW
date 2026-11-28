"""Owner-only admission and identity management for XHS private messages."""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request

from management_origin import management_origin_allowed
from .direct_messages import DirectMessageStore

router = APIRouter(prefix='/api/xhs-direct-messages', tags=['小红书私信'])


async def local_ui(request: Request):
    peer = request.client.host if request.client else ''
    try:
        is_local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_local = False
    from network_access import NetworkAccessPolicy
    from shared_state import resolve_allowed_ips
    policy = NetworkAccessPolicy.from_environment(resolve_allowed_ips())
    if not is_local and (not (policy.exact_ips or policy.mobile_lan_mac) or not policy.allows(peer)):
        raise HTTPException(403, '仅允许本机或已登记个人设备管理小红书私信')
    if request.headers.get('x-mirrow-xhs-admin') != '1':
        raise HTTPException(403, '请从 MIRROW 的小红书私信页面操作')
    origin = request.headers.get('origin')
    if origin and not management_origin_allowed(origin, str(request.url)):
        raise HTTPException(403, '小红书私信管理请求来源不匹配')


def readable_error(code):
    return {
        'dm_batch_changed': '这组来信已有更新，请重新核对后再决定。',
        'dm_preview_required': '请先核对这组固定来信内容。',
        'dm_profile_identity_unverified': '该账号尚未经过真机身份核验，暂不能绑定。',
        'dm_contact_blocked': '这个账号已在本地拉黑。',
        'dm_pending_not_found': '这组待处理私信已不存在。',
    }.get(code, '私信管理操作未完成。')


def safe(action):
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(400, readable_error(str(exc))) from None


@router.get('/inbox', dependencies=[Depends(local_ui)])
def inbox():
    store = DirectMessageStore()
    return {'review': store.admin_pending(), 'contacts': store.contacts()}


@router.post('/inbox/{session}/{thread}/preview', dependencies=[Depends(local_ui)])
async def preview(session: str, thread: str, request: Request):
    data = await request.json()
    token = str(data.get('batch_token') or '')
    try:
        from .direct_messages import dm_request
        value = await dm_request(session, 'inspect', thread=thread, batch_token=token)
        return {'preview': {'batch_token':token, 'source':value['source'], 'profile_id':value['thread'],
                            'name':value['name'], 'messages':value.get('messages',[]),
                            'truncated':value.get('truncated',False), 'unread_remaining':value.get('unread_remaining',0)}}
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(409, readable_error(str(exc))) from None


@router.post('/inbox/{session}/{thread}/approve', dependencies=[Depends(local_ui)])
async def approve(session: str, thread: str, request: Request):
    data = await request.json()
    return safe(lambda: {'pending': DirectMessageStore().approve_batch(session, thread, str(data.get('batch_token') or ''))})


@router.post('/inbox/{session}/{thread}/reject', dependencies=[Depends(local_ui)])
async def reject(session: str, thread: str, request: Request):
    data = await request.json()
    token = str(data.get('batch_token') or '')
    return safe(lambda: {'pending': DirectMessageStore().reject_batch(session, thread, token)})


@router.get('/entities', dependencies=[Depends(local_ui)])
def entities():
    from cognition.other_book import entities as registered
    return {'entities': [{'id': key, 'name': row.get('name',''), 'type': row.get('type',''),
                          'aliases': row.get('aliases',[])} for key, row in registered().items() if key not in {'human','k'}]}


@router.put('/contacts/{profile_id}/binding', dependencies=[Depends(local_ui)])
async def bind(profile_id: str, request: Request):
    data = await request.json()
    entity_id = str(data.get('entity_id') or '')
    from cognition.other_book import entities as registered
    known = registered()
    from xhs_interactions.native import config
    if profile_id == config().get('owner_account_id') or entity_id in {'human','k'}:
        raise HTTPException(400, '人类伙伴与 AI 使用已配置的平台账号识别。')
    if entity_id and entity_id not in known:
        raise HTTPException(400, '请选择他者书中已有的人物')
    return safe(lambda: {'contact': DirectMessageStore().bind_contact(profile_id, entity_id=entity_id,
                                                                       name=str(data.get('name') or ''))})


@router.post('/contacts/{profile_id}/block', dependencies=[Depends(local_ui)])
async def block(profile_id: str, request: Request):
    data = await request.json()
    store = DirectMessageStore()
    contact = safe(lambda: store.block_contact(profile_id, name=str(data.get('name') or '')))
    # The platform adapter owns UI writes and returns only durable status.
    try:
        from .platform_block import sync_block
        status = await sync_block(profile_id, contact.get('name',''))
        contact = safe(lambda: store.set_block_sync(profile_id, status))
    except Exception:
        contact = safe(lambda: store.set_block_sync(profile_id, 'pending'))
    return {'contact': contact}


@router.post('/contacts/{profile_id}/block/retry', dependencies=[Depends(local_ui)])
async def retry_block(profile_id: str):
    store = DirectMessageStore(); contact = store.contact(profile_id)
    if not contact or contact.get('state') != 'blocked':
        raise HTTPException(400, '这个账号当前未在本地拉黑')
    try:
        from .platform_block import sync_block
        status = await sync_block(profile_id, contact.get('name',''))
        contact = safe(lambda: store.set_block_sync(profile_id, status))
    except Exception:
        contact = safe(lambda: store.set_block_sync(profile_id, 'pending'))
    return {'contact': contact}
