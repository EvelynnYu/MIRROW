"""Owner-only bridge from authenticated public-wall contacts to lounge friends."""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack

from fastapi import APIRouter, Body, Depends, HTTPException

from lounge_reception.runtime import get_runtime
from lounge_visits import service, storage
from lounge_visits.registered_identity_match import candidates as identity_candidates
from routers.lounge_reception_router import local_ui
from social_feed.public_wall import PublicWallError, get_public_wall
from social_feed.household_identity import has_active_key, social_visitor
from visitor_lounge.security import RateLimitExceeded


router = APIRouter(prefix='/api/lounge-social-contacts', tags=['点赞之交'],
                   dependencies=[Depends(local_ui)])


def _visitors(runtime) -> dict[str, dict]:
    with runtime.database.connection() as db:
        return {row[0]: {'display_name': row[1] or '未认领访客', 'status': row[2],
                         'visitor_kind': row[3], 'has_active_key': bool(row[4])}
                for row in db.execute('SELECT v.id,v.display_name,v.status,v.visitor_kind,'
                                      'EXISTS(SELECT 1 FROM visitor_keys k WHERE k.visitor_id=v.id AND k.revoked_at IS NULL) '
                                      'FROM visitors v')}


def _contact_or_404(wall, visitor_id: str) -> dict:
    contact = next((item for item in wall.contacts() if item['visitor_id'] == visitor_id), None)
    if not contact:
        raise HTTPException(404, '点赞之交记录不存在')
    return contact


def _social_contacts(wall) -> list[dict]:
    """Show a newly registered pair without pretending either Key has visited."""
    contacts = wall.contacts()
    seen = {item['visitor_id'] for item in contacts}
    registered_ids = list(wall.prepared_visitor_ids())
    for link in wall.household_links():
        registered_ids.extend((link['human_visitor_id'], link['ai_visitor_id']))
    for visitor_id in registered_ids:
        if visitor_id not in seen:
            contacts.append({'visitor_id': visitor_id, 'first_social_login_at': None,
                             'linked_friend_id': ''})
            seen.add(visitor_id)
    removed = wall.removed_contacts()
    return [contact for contact in contacts if contact['visitor_id'] not in removed]


@router.get('')
async def list_contacts():
    wall = get_public_wall()
    runtime = await get_runtime()
    visitors = _visitors(runtime)
    friends = {friend.id: friend for friend in storage.friends().list_for_actor('k')}
    from lounge_visits.cognition_profiles import get as cognition_profile
    from cognition.other_book import entities
    known = entities()
    result = []
    for contact in _social_contacts(wall):
        visitor_id = contact['visitor_id']
        visitor = visitors.get(visitor_id)
        if not visitor:
            continue
        linked = contact['linked_friend_id']
        profile = wall.profile('visitor:' + visitor_id)
        registered_name = wall.registered_name(visitor_id)
        entity_id = cognition_profile('visitor:' + visitor_id)['primary_entity_id']
        result.append({**contact, **visitor, 'nickname': profile['nickname'],
                       'registered_name': registered_name,
                       'recognition_matches': identity_candidates(registered_name, known, visitor['visitor_kind']),
                       'entity_name': known.get(entity_id, {}).get('name', '') if entity_id else '',
                       'cognition_bound': bool(entity_id),
                       'linked_friend_id': linked if linked in friends else '',
                       'household_human_id': wall.household_human(visitor_id) if visitor['visitor_kind'] == 'external_ai' else '',
                       'registered_only': contact['first_social_login_at'] is None})
    links = wall.household_links()
    return {'contacts': result,
            'count': sum(not item['linked_friend_id'] and not item['cognition_bound'] for item in result),
            'visitor_candidates': [{'id': visitor_id, **visitor,
                                    'registered_name': wall.registered_name(visitor_id),
                                    'household_human_id': wall.household_human(visitor_id) if visitor['visitor_kind'] == 'external_ai' else '',
                                    'display_name': visitor['display_name'] if visitor['display_name'] != '未认领访客'
                                    else wall.registered_name(visitor_id) or wall.profile('visitor:' + visitor_id)['nickname'] or '未认领访客'}
                                   for visitor_id, visitor in visitors.items() if visitor_id not in wall.removed_contacts()],
            'household_links': links,
            'friends': [{'id': friend.id, 'display_name': friend.display_name}
                        for friend in friends.values()]}


@router.post('/household-links')
async def link_household(data: dict = Body(...)):
    if set(data) != {'human_visitor_id', 'ai_visitor_id'} or any(
            not isinstance(value, str) or not value or len(value) > 100 for value in data.values()):
        raise HTTPException(400, '请分别选择人和机的入站身份')
    if service._busy:
        raise HTTPException(409, '会客期间请稍后关联')
    runtime = await get_runtime()
    try:
        human = social_visitor(runtime, data['human_visitor_id'], require_link=False)
        ai = social_visitor(runtime, data['ai_visitor_id'], require_link=False)
    except (KeyError, ValueError, PublicWallError):
        raise HTTPException(400, '入站身份不可用') from None
    if human.visitor_kind != 'human' or ai.visitor_kind != 'external_ai':
        raise HTTPException(400, '必须分别选择人和机的身份')
    if not has_active_key(runtime, data['human_visitor_id']) or not has_active_key(runtime, data['ai_visitor_id']):
        raise HTTPException(400, '两位身份都需要有当前有效的入站 Key')
    try:
        result = get_public_wall().link_household(data['human_visitor_id'], data['ai_visitor_id'])
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'ok': True, **result}


@router.post('/import-reception')
async def import_reception_identity(data: dict = Body(...)):
    """Explicitly enroll one already-issued reception identity in the social roster.

    This reuses the reception credential by reference.  No Key is copied into
    the public-wall database, and AI enrollment cannot bypass human binding.
    """
    if set(data) != {'visitor_id', 'registered_name'} or any(
            not isinstance(value, str) for value in data.values()):
        raise HTTPException(400, '请选择已有入站身份与登记身份名')
    visitor_id = data['visitor_id'].strip()
    name = data['registered_name'].strip()
    if not visitor_id or len(visitor_id) > 100 or not name or len(name) > 40:
        raise HTTPException(400, '入站身份或登记身份名无效')
    runtime = await get_runtime()
    try:
        visitor = social_visitor(runtime, visitor_id)
    except (KeyError, ValueError, PublicWallError):
        raise HTTPException(409, '机身份须先关联一位有效的人；入站身份也必须可用') from None
    if not has_active_key(runtime, visitor_id):
        raise HTTPException(409, '请先在接待设置为该身份签发有效 Key')
    if visitor.visitor_kind not in {'human', 'external_ai'}:
        raise HTTPException(400, '仅支持人或机的身份')
    wall = get_public_wall()
    try:
        wall.register_human_name(visitor_id, name)
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'ok': True, 'visitor_id': visitor_id, 'registered_name': name,
            'kind': visitor.visitor_kind}


@router.post('/household-links/rebind')
async def rebind_household(data: dict = Body(...)):
    required = {'ai_visitor_id', 'previous_human_visitor_id', 'new_human_visitor_id'}
    if set(data) != required or any(
            not isinstance(value, str) or not value or len(value) > 100 for value in data.values()):
        raise HTTPException(400, '请分别选择机身份及新旧人类身份')
    if data['previous_human_visitor_id'] == data['new_human_visitor_id']:
        raise HTTPException(400, '请选择不同的人类身份')
    if service._busy:
        raise HTTPException(409, '会客期间请稍后换绑')
    runtime = await get_runtime()
    old_human = _visitors(runtime).get(data['previous_human_visitor_id'])
    try:
        new_human = social_visitor(runtime, data['new_human_visitor_id'], require_link=False)
        ai = social_visitor(runtime, data['ai_visitor_id'], require_link=False)
    except (KeyError, ValueError, PublicWallError):
        raise HTTPException(400, '入站身份不可用') from None
    if not old_human or old_human['visitor_kind'] != 'human' or new_human.visitor_kind != 'human' or ai.visitor_kind != 'external_ai':
        raise HTTPException(400, '必须将机换绑到人类身份')
    if not has_active_key(runtime, data['new_human_visitor_id']) or not has_active_key(runtime, data['ai_visitor_id']):
        raise HTTPException(400, '新人类身份与机身份都需要有效入站 Key')
    try:
        result = get_public_wall().rebind_household(data['ai_visitor_id'],
                                                     data['previous_human_visitor_id'],
                                                     data['new_human_visitor_id'])
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'ok': True, **result}


@router.post('/identities/{visitor_id}/confirm-key')
async def confirm_existing_key(visitor_id: str, data: dict = Body(...)):
    """Verify an already-issued Key belongs to this exact reception identity."""
    if set(data) != {'visitor_key'} or not isinstance(data['visitor_key'], str) or not 20 <= len(data['visitor_key']) <= 256:
        raise HTTPException(400, '请输入接待设置为这张身份卡签发的新 Key')
    runtime = await get_runtime()
    visitor = _visitors(runtime).get(visitor_id)
    if not visitor:
        raise HTTPException(404, '入站身份不存在')
    try:
        identity = runtime.keys.authenticate_identity(data['visitor_key'])
    except RateLimitExceeded:
        raise HTTPException(429, '验证尝试过于频繁，请稍后再试') from None
    if not identity:
        raise HTTPException(403, 'Key 无效或已经失效')
    if identity[1] != visitor_id:
        raise HTTPException(409, '这把 Key 属于另一张身份卡；请在接待设置对当前身份签发，不能跨身份挪用')
    if visitor['status'] not in {'active', 'suspended'}:
        raise HTTPException(409, '这张身份卡已暂停或锁定；请先在接待设置恢复身份')
    return {'ok': True, 'visitor_id': visitor_id, 'already_bound': True,
            'notice': 'Key 已由接待设置绑定到这张身份卡，共友圈会直接复用，无需另存一份'}


@router.post('/{visitor_id}/link')
async def link_existing(visitor_id: str, data: dict = Body(...)):
    if set(data) != {'friend_id'} or not isinstance(data['friend_id'], str):
        raise HTTPException(400, '请选择已有好友')
    if service._busy:
        raise HTTPException(409, '拜访期间请稍后关联')
    wall = get_public_wall()
    contact = _contact_or_404(wall, visitor_id)
    friend_id = data['friend_id']
    if contact['linked_friend_id'] and contact['linked_friend_id'] != friend_id:
        raise HTTPException(409, '该访客已经关联其他好友')
    try:
        storage.friends().get_owned('k', friend_id)
        wall.link_contact(visitor_id, friend_id)
    except KeyError:
        raise HTTPException(404, '会客室好友不存在') from None
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'ok': True, 'friend_id': friend_id}


@router.post('/{visitor_id}/promote')
async def promote_contact(visitor_id: str, data: dict = Body(...)):
    allowed = {'display_name', 'lounge_url', 'visitor_key', 'relationship_note'}
    if set(data) - allowed or any(not isinstance(value, str) or len(value) > 2048
                                   for value in data.values()):
        raise HTTPException(400, '好友设置格式无效')
    if not data.get('lounge_url', '').strip() or not data.get('visitor_key', '').strip():
        raise HTTPException(400, '转入好友名册需要对方 MCP 地址和给 AI 的 Key 两项齐全')
    if service._busy:
        raise HTTPException(409, '拜访期间请稍后转入')
    wall = get_public_wall()
    contact = _contact_or_404(wall, visitor_id)
    if contact['linked_friend_id']:
        raise HTTPException(409, '该访客已经关联会客室好友')
    runtime = await get_runtime()
    visitor = _visitors(runtime).get(visitor_id)
    if not visitor:
        raise HTTPException(404, '来访身份不存在')
    friend_id = uuid.uuid5(uuid.NAMESPACE_URL, 'mirrow-social-contact:' + visitor_id).hex
    store = storage.friends()
    try:
        try:
            friend = store.get_owned('k', friend_id)
        except KeyError:
            friend = store.create(friend_id=friend_id, actor_id='k',
                                  display_name=data.get('display_name', '').strip() or visitor['display_name'],
                                  lounge_url=data['lounge_url'], visitor_key=data['visitor_key'],
                                  relationship_note=data.get('relationship_note', '').strip() or '\u200b',
                                  enabled=True, allow_autonomous=False, cooldown_hours=12, max_turns=4)
        wall.link_contact(visitor_id, friend.id)
    except (ValueError, TypeError):
        raise HTTPException(400, '请检查称呼、HTTPS /mcp 地址和 Key；同名好友请改用关联已有好友') from None
    except PublicWallError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'ok': True, 'friend_id': friend.id}


@router.delete('/identities/{visitor_id}')
async def delete_social_contact(visitor_id: str):
    wall = get_public_wall()
    if visitor_id not in {c['visitor_id'] for c in _social_contacts(wall)}:
        raise HTTPException(404,'共域访客不存在')
    runtime = await get_runtime()
    visitor = _visitors(runtime).get(visitor_id)
    if not visitor:
        raise HTTPException(404,'共域访客不存在')
    targets = {visitor_id}
    if visitor['visitor_kind']=='human':
        targets.update(link['ai_visitor_id'] for link in wall.household_links()
                       if link['human_visitor_id']==visitor_id)
    lanes = [runtime.service.lane(target) for target in sorted(targets)]
    if any(lane.locked() for lane in lanes):
        raise HTTPException(409,'这位朋友或旗下机正在会客，请结束后再删除')
    async with AsyncExitStack() as stack:
        for lane in lanes:
            await stack.enter_async_context(lane)
        if any(runtime.history.active(target) for target in targets):
            raise HTTPException(409,'这位朋友或旗下机正在会客，请结束后再删除')
        # This removes only the social roster and social sessions. Reception
        # access and shared Key credentials are managed explicitly elsewhere.
        wall.remove_contacts(sorted(targets))
    return {'deleted':True,'removed_count':len(targets),
            'notice':'已从点赞之交移除该好友及其旗下机；各自 Key 和会客名册不变，两个名册都移除后可到接待设置分别删除 Key。旧记录保留。'}
