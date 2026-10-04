"""Mentions, identity projections and lifecycle: temporary DBs, no real homes."""
import json
from types import SimpleNamespace

import pytest

from .public_wall import PublicWall, PublicWallError
from .store import SocialFeedStore, SocialFeedError
from .hybrid_store import HybridSocialFeedStore
from . import migration, mentions


@pytest.fixture
def wall(tmp_path, monkeypatch):
    value = PublicWall(tmp_path / 'public.db'); value.initialize()
    monkeypatch.setattr('social_feed.public_wall._WALL', value)
    monkeypatch.setattr('lounge_reception.runtime.current_runtime', lambda: None)
    monkeypatch.setattr('social_feed.public_gateway._cognition_name', lambda _: '私有认知真名')
    monkeypatch.setattr('social_feed.public_gateway._home_remark', lambda *_: '本家私有备注')
    for actor in ['person_a', 'c', 'visitor_a', 'same']:
        value.register_human_name(actor, '私有登记名-' + actor)
        value.note_contact(actor)
    value.set_profile('visitor:person_a', '人乙')
    value.set_profile('visitor:same', '人乙')
    return value


def test_same_mention_projects_for_each_viewer_and_rename_keeps_target(wall):
    from .public_gateway import _decorate, _display
    runtime = SimpleNamespace()
    wall.set_remark('visitor:c', 'visitor:person_a', 'C的朋友人甲')
    item = wall.create_moment('aning', '给朋友看的', mention_actor_ids=['visitor:person_a'])
    home = _decorate(runtime, wall.get_moment(item['id']), viewer='aning')
    stranger = _decorate(runtime, wall.get_moment(item['id']), viewer='visitor:visitor_a')
    friend = _decorate(runtime, wall.get_moment(item['id']), viewer='visitor:c')
    assert home['people']['visitor:person_a']['name'] == '人乙（私有认知真名）'
    assert stranger['people']['visitor:person_a']['name'] == '人乙'
    assert friend['people']['visitor:person_a']['name'] == '人乙（C的朋友人甲）'
    assert '私有' not in json.dumps(stranger, ensure_ascii=False)
    wall.set_profile('visitor:person_a', '新网名')
    assert wall.get_moment(item['id'])['mention_actor_ids'] == ['visitor:person_a']
    assert _display(runtime, 'visitor:person_a', viewer='visitor:c')['name'] == '新网名（C的朋友人甲）'
    assert _display(runtime, 'visitor:same', viewer='visitor:c')['name'] == '人乙'


def test_empty_public_nickname_does_not_leak_registered_name(wall):
    from .public_gateway import _display
    assert _display(SimpleNamespace(), 'visitor:visitor_a', viewer='visitor:c')['name'] == '未设置网名'


@pytest.mark.asyncio
async def test_main_chat_mention_peek_announces_without_consuming_body(tmp_path):
    from context_builder import ContextBuilder
    store = SocialFeedStore(tmp_path / 'private.db')
    await store.create_moment('aning', '实际正文', mention_actor_ids=['k'])
    builder = ContextBuilder('FULL_CHAT')
    builder._kwargs['social_feed_store'] = store
    builder._kwargs['current_message_id'] = 'test-mention-turn'
    await builder._prepare_social_feed_reminder()
    assert '艾特' in builder._tail_messages[0]['content']
    assert '实际正文' not in builder._tail_messages[0]['content']
    assert len(await store.unread_notifications('k')) == 1


def test_post_comment_dedup_explicit_read_and_delete(wall):
    post = wall.create_moment('aning', '你好', source_key='post', mention_actor_ids=['k', 'visitor:person_a', 'aning'])
    assert len(wall.notifications('k')) == 1
    assert wall.notifications('k')[0]['kind'] == 'mention'
    assert len(wall.notifications('visitor:person_a')) == 1
    assert wall.notifications('aning') == []
    assert wall.create_moment('aning', '重试', source_key='post', mention_actor_ids=['visitor:person_a'])['id'] == post['id']
    assert len(wall.notifications('visitor:person_a')) == 1
    comment = wall.add_comment('visitor:c', post['id'], '回复', source_key='comment', mention_actor_ids=['visitor:person_a'])
    notices = wall.notifications('visitor:person_a')
    assert len(notices) == 2 and notices[1]['comment_content'] == '回复'
    assert wall.mark_notifications_read('visitor:person_a', []) == 0
    assert wall.mark_notifications_read('visitor:c', [notices[0]['id']]) == 0
    assert wall.mark_notifications_read('visitor:person_a', [notices[0]['id']]) == 1
    assert wall.notifications('visitor:person_a')[0]['id'] == notices[1]['id']
    wall.delete_comment('visitor:c', post['id'], comment['id'])
    assert wall.notifications('visitor:person_a') == []


def test_unavailable_and_removed_targets_leave_no_partial_write(wall):
    for target in ['visitor:unknown', '人甲', 'archive:' + '0' * 32]:
        with pytest.raises(PublicWallError):
            wall.create_moment('aning', '拒绝的动作', mention_actor_ids=[target])
    wall.remove_contact('person_a')
    with pytest.raises(PublicWallError, match='mention_target_unavailable'):
        wall.create_moment('aning', '拒绝的动作', mention_actor_ids=['visitor:person_a'])
    assert wall.list_moments()['items'] == []


def test_revoked_key_target_rejected_even_when_registered(wall, monkeypatch):
    monkeypatch.setattr('lounge_reception.runtime.current_runtime', lambda: SimpleNamespace())
    monkeypatch.setattr('social_feed.household_identity.registered_social_visitor', lambda *_: SimpleNamespace())
    monkeypatch.setattr('social_feed.household_identity.has_active_key', lambda *_: False)
    with pytest.raises(PublicWallError, match='mention_target_unavailable'):
        wall.create_moment('aning', '失效Key', mention_actor_ids=['visitor:person_a'])


def test_withdrawal_removes_mention_body_and_inbox(wall):
    post = wall.create_moment('aning', '原文', mention_actor_ids=['visitor:person_a'])
    wall.withdraw('aning', post['id'])
    assert wall.notifications('visitor:person_a') == []
    assert wall.get_moment(post['id']) is None
    with wall._connect() as db:
        row = db.execute('SELECT content,mentions_json FROM wall_moments WHERE id=?', (post['id'],)).fetchone()
        assert tuple(row) == ('', '[]')


@pytest.mark.asyncio
async def test_private_mentions_and_private_public_round_trip(wall, tmp_path):
    feed = HybridSocialFeedStore(SocialFeedStore(tmp_path / 'private.db'), wall)
    with pytest.raises(SocialFeedError, match='private_mentions_home_only'):
        await feed.create_moment('aning', '私密', mention_actor_ids=['visitor:person_a'])
    post = await feed.create_moment('aning', '给AI', mention_actor_ids=['k'])
    assert (await feed.unread_notifications('k'))[0]['kind'] == 'mention'
    assert await feed.mark_notifications_read('k', []) == 0
    await feed.set_moment_visibility(post['id'], 'aning', 'public')
    assert wall.get_moment(post['id'])['mention_actor_ids'] == ['k']
    assert (await feed.unread_notifications('k'))[0]['kind'] == 'mention'
    comment = await feed.add_comment(post['id'], 'visitor:c', '公评', mention_actor_ids=['visitor:person_a'])
    await feed.set_moment_visibility(post['id'], 'aning', 'private')
    assert wall.notifications('visitor:person_a') == []
    reply = await feed.add_comment(post['id'], 'aning', '私密回评', reply_to_id=comment['id'], mention_actor_ids=['k'])
    notices = await feed.unread_notifications('k')
    assert notices[-1]['comment_content'] == '私密回评' and notices[-1]['kind'] == 'mention'
    assert len([c for c in (await feed.get_moment(post['id']))['comments'] if c['id'] == reply['id']]) == 1
    await feed.set_moment_visibility(post['id'], 'aning', 'public')
    assert len(wall.get_moment(post['id'])['comments']) == 2
    assert wall.notifications('k')[-1]['kind'] == 'mention'
    # Changing visibility is not a second act: no new visitor mention notice.
    assert wall.notifications('visitor:person_a') == []


def test_migration_keeps_historical_mentions_without_wrong_local_identity_or_realert(wall, tmp_path):
    source, target = wall, PublicWall(tmp_path / 'target.db')
    target.initialize()
    source.migrate_transfers(); target.migrate_transfers()
    post = source.create_moment('visitor:c', '带艾特的搬迁', mention_actor_ids=['visitor:person_a'])
    prepared = migration.prepare(source, 'visitor:c', post['id'], 'https://target.example', 'aning')
    assert '私有' not in json.dumps(prepared['snapshot'], ensure_ascii=False)
    staged = migration.stage(target, transfer_id=prepared['transfer_id'], source_origin='https://source.example',
        source_actor='visitor:c', target_actor='aning', snapshot=prepared['snapshot'], expected_digest=prepared['digest'])
    committed = migration.commit(source, 'visitor:c', prepared['transfer_id'], staged['destination_moment_id'])
    migrated = migration.activate(target, prepared['transfer_id'], committed)
    historical = migration.historical_actor('https://source.example', 'visitor:person_a')
    assert target.get_moment(migrated['id'])['mention_actor_ids'] == [historical]
    assert target.transfer_person(historical) == '人乙'
    assert target.notifications('aning') == [] and source.notifications('visitor:person_a') == []
    assert mentions.decode(mentions.encode([historical])) == [historical]
    with pytest.raises(ValueError): mentions.normalise([historical])
