"""Offline two-home transfer tests; never open the production wall database."""

import json

import pytest
import httpx

from social_feed import migration
from social_feed.public_wall import PublicWall, PublicWallError
from social_feed.remote_sites import RemoteSite
from social_feed.transfer_runner import move_hosted_post


OLD = 'https://friend.example.com'
NEW = 'https://social.example.com'


def walls(tmp_path):
    old = PublicWall(tmp_path / 'old.db')
    new = PublicWall(tmp_path / 'new.db')
    for wall in (old, new):
        wall.initialize()
        wall.migrate_transfers()
    return old, new


def hosted_post(old):
    post = old.create_moment('visitor:human-1', '寄存的原帖')
    first = old.add_comment('visitor:human-2', post['id'], '第一条评论')
    old.add_comment('visitor:human-1', post['id'], '回复第一条', first['id'])
    old.set_like('visitor:human-2', post['id'], True)
    old.set_profile('visitor:human-2', '人甲')
    return post


def test_staged_transfer_never_exposes_a_second_post_and_preserves_replies(tmp_path):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    prepared = migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
    with pytest.raises(PublicWallError, match='moment_transfer_pending'):
        old.add_comment('visitor:human-2', post['id'], '冻结后不能评论')
    with pytest.raises(PublicWallError, match='moment_transfer_pending'):
        old.set_like('visitor:human-2', post['id'], False)
    assert new.list_moments()['items'] == []
    staged = migration.stage(new, transfer_id=prepared['transfer_id'], source_origin=OLD,
                             source_actor='visitor:human-1', target_actor='aning',
                             snapshot=prepared['snapshot'], expected_digest=prepared['digest'])
    assert new.list_moments()['items'] == []
    proof = migration.prove(new, prepared['transfer_id'], staged['proof'], prepared['digest'],
                            staged['destination_moment_id'])
    assert proof['state'] == 'staged'
    with pytest.raises(PublicWallError, match='transfer_proof_rejected'):
        migration.prove(new, prepared['transfer_id'], 'incorrect-proof', prepared['digest'],
                        staged['destination_moment_id'])
    committed = migration.commit(old, 'visitor:human-1', prepared['transfer_id'],
                                 staged['destination_moment_id'])
    old_item = old.get_moment(post['id'])
    assert old_item['migrated'] is True
    assert old_item['content'] == '该动态已迁移至私人服务器'
    assert old_item['comments'] == [] and old_item['reactions'] == []
    with old._connect() as db:
        assert db.execute('SELECT content FROM wall_moments WHERE id=?', (post['id'],)).fetchone()[0] == ''
        assert db.execute('SELECT COUNT(*) FROM wall_comments WHERE moment_id=?', (post['id'],)).fetchone()[0] == 0
    assert old.removal_status('visitor:human-1', post['id'])['reason'] == 'migrated'
    assert new.list_moments()['items'] == []
    activated = migration.activate(new, prepared['transfer_id'], committed)
    assert migration.activate(new, prepared['transfer_id'], committed) == activated
    item = new.get_moment(activated['id'])
    assert item['author'] == 'aning' and item['content'] == '寄存的原帖'
    assert item['arrived_from_other_home'] is True
    assert [comment['content'] for comment in item['comments']] == ['第一条评论', '回复第一条']
    assert item['comments'][1]['reply_to_id'] == item['comments'][0]['id']
    assert len(item['reactions']) == 1
    assert new.transfer_person(item['comments'][0]['author']) == '人甲'
    with pytest.raises(PublicWallError, match='moment_not_found'):
        old.add_comment('visitor:human-2', post['id'], '迁移后不能评论')


def test_transfer_requires_original_author_and_does_not_commit_without_destination(tmp_path):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    with pytest.raises(PublicWallError, match='forbidden'):
        migration.prepare(old, 'visitor:human-2', post['id'], NEW, 'aning')
    prepared = migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
    migration.stage(new, transfer_id=prepared['transfer_id'], source_origin=OLD,
                    source_actor='visitor:human-1', target_actor='aning',
                    snapshot=prepared['snapshot'], expected_digest=prepared['digest'])
    with pytest.raises(PublicWallError, match='transfer_source_not_committed'):
        migration.activate(new, prepared['transfer_id'], {'state': 'prepared'})
    assert old.get_moment(post['id'])['content'] == '寄存的原帖'
    assert new.list_moments()['items'] == []


def test_migration_schema_is_explicit(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db')
    wall.initialize()
    post = wall.create_moment('visitor:author', '尚未迁移')
    with pytest.raises(PublicWallError, match='transfer_schema_not_ready'):
        migration.prepare(wall, 'visitor:author', post['id'], NEW, 'aning')
    assert wall.get_moment(post['id'])['content'] == '尚未迁移'


def test_cancel_unfreezes_original_and_can_prepare_again(tmp_path):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    first = migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
    staged = migration.stage(new, transfer_id=first['transfer_id'], source_origin=OLD,
                             source_actor='visitor:human-1', target_actor='aning',
                             snapshot=first['snapshot'], expected_digest=first['digest'])
    assert staged['state'] == 'staged'
    assert migration.cancel(old, 'visitor:human-1', first['transfer_id'])['state'] == 'cancelled'
    assert migration.cancel(old, 'visitor:human-1', first['transfer_id'])['state'] == 'cancelled'
    migration.discard_staged(new, first['transfer_id'])
    old.add_comment('visitor:human-2', post['id'], '已解冻')
    second = migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
    assert second['transfer_id'] != first['transfer_id']
    assert new.list_moments()['items'] == []


@pytest.mark.asyncio
async def test_runner_resumes_after_lost_commit_response(tmp_path):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    site = RemoteSite(id='friend', name='好友家', origin=OLD, human_key='test-key', ai_key='',
                      enabled=True, created_at=0, updated_at=0)

    class Client:
        lose_once = True

        async def request(self, _site, actor, method, path, *, payload=None, params=None):
            assert actor == 'human'
            if path == '/social/v1/me':
                return {'actor': {'actor_id': 'visitor:human-1'}, 'can_manage_avatar': True}
            if path.endswith('/transfer/prepare'):
                return migration.prepare(old, 'visitor:human-1', post['id'],
                                         payload['destination_origin'], payload['target_actor'])
            if path.endswith('/status'):
                transfer_id = path.split('/')[-2]
                return migration.outbound_status(old, 'visitor:human-1', transfer_id)
            if path.endswith('/commit'):
                transfer_id = path.split('/')[-2]
                staged = migration.inbound_by_id(new, transfer_id)
                migration.prove(new, transfer_id, payload['proof'], staged['digest'],
                                payload['destination_moment_id'])
                result = migration.commit(old, 'visitor:human-1', transfer_id,
                                          payload['destination_moment_id'])
                if self.lose_once:
                    self.lose_once = False
                    from social_feed.remote_sites import RemoteSiteError
                    raise RemoteSiteError('simulated_lost_response')
                return result
            raise AssertionError(path)

    client = Client()
    result = await move_hosted_post(new, site, 'human', post['id'], NEW, client)
    assert result['state'] == 'complete'
    again = await move_hosted_post(new, site, 'human', post['id'], NEW, client)
    assert again == result
    assert len(new.list_moments()['items']) == 1


@pytest.mark.asyncio
async def test_runner_keeps_stage_invisible_when_old_home_has_not_committed(tmp_path):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    site = RemoteSite(id='friend', name='好友家', origin=OLD, human_key='test-key', ai_key='',
                      enabled=True, created_at=0, updated_at=0)

    class Client:
        async def request(self, _site, _actor, _method, path, *, payload=None, params=None):
            if path == '/social/v1/me':
                return {'actor': {'actor_id': 'visitor:human-1'}, 'can_manage_avatar': True}
            if path.endswith('/transfer/prepare'):
                return migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
            if path.endswith('/commit'):
                from social_feed.remote_sites import RemoteSiteError
                raise RemoteSiteError('network_offline')
            if path.endswith('/status'):
                return migration.outbound_status(old, 'visitor:human-1', path.split('/')[-2])
            raise AssertionError(path)

    result = await move_hosted_post(new, site, 'human', post['id'], NEW, Client())
    assert result['state'] == 'pending'
    assert len(migration.inbound_pending(new)) == 1
    assert new.list_moments()['items'] == []
    assert old.get_moment(post['id'])['content'] == '寄存的原帖'


@pytest.mark.asyncio
async def test_old_home_verifies_new_stage_over_pinned_public_destination(tmp_path, monkeypatch):
    old, new = walls(tmp_path)
    post = hosted_post(old)
    prepared = migration.prepare(old, 'visitor:human-1', post['id'], NEW, 'aning')
    staged = migration.stage(new, transfer_id=prepared['transfer_id'], source_origin=OLD,
                             source_actor='visitor:human-1', target_actor='aning',
                             snapshot=prepared['snapshot'], expected_digest=prepared['digest'])
    checks = []

    async def destination(url):
        checks.append(('dns', url))
        return '203.0.113.20'

    def pin(_request, _expected, _address):
        checks.append(('pin', _address))

    def response(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=migration.prove(new, prepared['transfer_id'],
            payload['proof'], payload['digest'], payload['destination_moment_id']))

    monkeypatch.setattr(migration, 'public_destination', destination)
    monkeypatch.setattr(migration, 'pin_request', pin)
    await migration.verify_remote_stage(NEW, prepared['transfer_id'], staged['proof'], prepared['digest'],
                                        staged['destination_moment_id'], transport=httpx.MockTransport(response))
    assert any(kind == 'dns' for kind, _ in checks)
    assert any(kind == 'pin' for kind, _ in checks)
    with pytest.raises(PublicWallError, match='transfer_destination_unavailable'):
        await migration.verify_remote_stage(NEW, prepared['transfer_id'], 'wrong', prepared['digest'],
                                            staged['destination_moment_id'], transport=httpx.MockTransport(response))
    assert old.get_moment(post['id'])['content'] == '寄存的原帖'
