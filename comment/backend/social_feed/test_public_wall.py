import asyncio
import sqlite3
from pathlib import Path

import pytest

from social_feed.hybrid_store import HybridSocialFeedStore
from social_feed.public_wall import PublicWall, PublicWallError
from social_feed.store import SocialFeedStore


@pytest.fixture
def feed(tmp_path: Path):
    return HybridSocialFeedStore(SocialFeedStore(tmp_path / 'private.db'), PublicWall(tmp_path / 'public.db'))


def run(coro):
    return asyncio.run(coro)


def test_social_contact_records_first_use_and_owner_link_only(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db')
    wall.initialize()
    assert wall.contacts() == []  # Existing keys are never backfilled merely by migration.
    assert wall.note_contact('visitor-1') is True
    assert wall.note_contact('visitor-1') is False
    assert wall.note_contact('visitor-2') is True
    wall.link_contact('visitor-1', 'friend-1')
    assert wall.linked_friend('visitor-1') == 'friend-1'
    assert wall.linked_visitor('friend-1') == 'visitor-1'
    with pytest.raises(PublicWallError, match='friend_already_linked'):
        wall.link_contact('visitor-2', 'friend-1')


def test_remarks_are_private_per_viewer_and_home_is_shared(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    wall.note_contact('v1')
    wall.note_contact('v2')
    assert wall.remark('aning', 'visitor:v1', home_default='人丙') == '人丙'
    assert wall.remark('k', 'visitor:v1', home_default='人丙') == '人丙'
    assert wall.remark('visitor:v2', 'visitor:v1', home_default='人丙') == ''
    wall.set_remark('visitor:v2', 'visitor:v1', '朋友自己写的备注')
    wall.set_remark('aning', 'visitor:v1', '站主的备注')
    assert wall.remark('visitor:v2', 'visitor:v1') == '朋友自己写的备注'
    assert wall.remark('k', 'visitor:v1') == '站主的备注'
    assert wall.remark('visitor:v1', 'visitor:v2') == ''
    wall.set_remark('aning', 'visitor:v1', '')
    assert wall.remark('k', 'visitor:v1', home_default='人丙') == ''
    with pytest.raises(PublicWallError, match='invalid_remark_target'):
        wall.set_remark('visitor:v1', 'visitor:v1', '不能给自己改备注')


def test_comment_author_or_owner_can_delete_without_erasing_replies(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    moment = wall.create_moment('aning', '公开动态')
    typo = wall.add_comment('visitor:friend1', moment['id'], '错字', source_key='first-comment')
    reply = wall.add_comment('visitor:friend2', moment['id'], '我也回复了', typo['id'])
    with pytest.raises(PublicWallError, match='forbidden'):
        wall.delete_comment('visitor:friend2', moment['id'], typo['id'])
    assert wall.delete_comment('visitor:friend1', moment['id'], typo['id'])['status'] == 'deleted'
    remaining = wall.get_moment(moment['id'])['comments']
    assert len(remaining) == 1 and remaining[0]['id'] == reply['id']
    assert remaining[0]['reply_to_id'] is None
    assert wall.add_comment('visitor:friend1', moment['id'], '错字', source_key='first-comment')['status'] == 'withdrawn'
    with pytest.raises(PublicWallError, match='comment_not_found'):
        wall.delete_comment('visitor:friend1', moment['id'], typo['id'])
    wall.delete_comment('aning', moment['id'], reply['id'])
    assert wall.get_moment(moment['id'])['comments'] == []
    assert wall.changes()['items'][-1]['moment']['comments'] == []


def test_guardian_comment_delete_uses_current_household(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    post = wall.create_moment('aning', '临时测试动态')
    ai = wall.add_comment('visitor:ai', post['id'], '机的错字')
    other = wall.add_comment('visitor:other', post['id'], '别家的评论')
    assert not wall.can_delete_comment('visitor:human', 'visitor:ai')
    with pytest.raises(PublicWallError, match='forbidden'):
        wall.delete_comment('visitor:human', post['id'], ai['id'])
    wall.link_household('human', 'ai')
    assert wall.can_delete_comment('visitor:human', 'visitor:ai')
    assert not wall.can_delete_comment('visitor:ai', 'visitor:human')
    assert not wall.can_delete_comment('visitor:human', 'visitor:other')
    with pytest.raises(PublicWallError, match='forbidden'):
        wall.delete_comment('visitor:human', post['id'], other['id'])
    assert wall.delete_comment('visitor:human', post['id'], ai['id'])['status'] == 'deleted'
    wall.delete_comment('aning', post['id'], other['id'])


def test_local_comment_delete_routes_public_and_archived_interactions(feed):
    post = run(feed.create_moment('aning', '公开动态', visibility='public'))
    visitor = run(feed.add_comment(post['id'], 'visitor:friend1', '访客评论'))
    reply = run(feed.add_comment(post['id'], 'aning', '站主回复', reply_to_id=visitor['id']))
    assert run(feed.delete_comment(post['id'], visitor['id'], 'aning'))['status'] == 'deleted'
    assert feed.public.get_moment(post['id'])['comments'][0]['reply_to_id'] is None

    another = run(feed.add_comment(post['id'], 'visitor:friend1', '转私密前的评论'))
    run(feed.set_moment_visibility(post['id'], 'aning', 'private'))
    assert run(feed.delete_comment(post['id'], another['id'], 'aning'))['status'] == 'deleted'
    remaining = run(feed.get_moment(post['id']))['comments']
    assert [row['id'] for row in remaining] == [reply['id']]


def test_public_and_private_have_one_body_authority(feed):
    private = run(feed.create_moment('aning', '只给 AI 看'))
    public = run(feed.create_moment('k', '大家都能看', visibility='public'))
    assert feed.public.get_moment(private['id']) is None
    assert run(feed.private.get_moment(public['id'])) is None
    assert [item['id'] for item in run(feed.list_moments())['items']] == [public['id'], private['id']]
    assert feed.public.get_moment(public['id'])['content'] == '大家都能看'


def test_private_to_public_moves_existing_interactions_and_back(feed):
    moment = run(feed.create_moment('aning', '原本私密'))
    run(feed.add_comment(moment['id'], 'k', '原有评论'))
    run(feed.toggle_like(moment['id'], 'k'))
    result = run(feed.set_moment_visibility(moment['id'], 'aning', 'public'))
    assert result['changed']
    assert any(row['moment_id'] == moment['id'] for row in run(feed.unread_notifications('k')))
    assert run(feed.private.get_moment(moment['id'])) is None
    assert [comment['content'] for comment in feed.public.get_moment(moment['id'])['comments']] == ['原有评论']
    guest = feed.public.add_comment('visitor:friend1', moment['id'], '访客评论')
    assert any(row['actor'] == 'visitor:friend1' for row in run(feed.unread_notifications('aning')))
    feed.public.toggle_like('visitor:friend1', moment['id'])
    run(feed.set_moment_visibility(moment['id'], 'aning', 'private'))
    assert feed.public.get_moment(moment['id']) is None
    with sqlite3.connect(feed.public.path) as db:
        assert db.execute('SELECT content FROM wall_moments WHERE id=?', (moment['id'],)).fetchone()[0] == ''
        assert db.execute('SELECT count(*) FROM wall_comments WHERE moment_id=?', (moment['id'],)).fetchone()[0] == 0
    changes = feed.public.changes()['items']
    assert changes[-1]['status'] == 'withdrawn' and changes[-1]['moment'] is None
    restored = run(feed.get_moment(moment['id']))
    assert restored['visibility'] == 'private'
    assert {c['content'] for c in restored['comments']} == {'原有评论', '访客评论'}
    assert guest['id'] in {c['id'] for c in restored['comments']}
    assert {r['author'] for r in restored['reactions']} == {'k', 'visitor:friend1'}
    run(feed.set_moment_visibility(moment['id'], 'aning', 'public'))
    assert len(feed.public.get_moment(moment['id'])['comments']) == 2


def test_guest_may_edit_own_but_not_others_and_withdrawal_is_content_free(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    post = wall.create_moment('visitor:friend1', '公开原文')
    with pytest.raises(PublicWallError, match='forbidden'):
        wall.edit_moment('visitor:friend2', post['id'], '篡改', 1)
    edited = wall.edit_moment('visitor:friend1', post['id'], '新原文', 1)
    with pytest.raises(PublicWallError, match='revision_conflict'):
        wall.edit_moment('visitor:friend1', post['id'], '过期修改', 1)
    assert edited['revision'] == 2
    wall.withdraw('visitor:friend1', post['id'])
    assert wall.get_moment(post['id']) is None
    assert wall.list_moments()['items'] == []
    assert wall.changes()['items'][-1]['moment'] is None
    with pytest.raises(PublicWallError, match='moment_not_found'):
        wall.add_comment('visitor:friend2', post['id'], '不该成功')


def test_human_guardian_can_manage_only_own_ai_posts(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    ai_post = wall.create_moment('visitor:ai', '原文')
    unrelated = wall.create_moment('visitor:other-ai', '别人家的动态')
    assert not wall.can_manage_moment('visitor:human', 'visitor:ai')
    wall.link_household('human', 'ai')
    assert wall.household_human('ai') == 'human'
    assert wall.can_manage_moment('visitor:human', 'visitor:ai')
    assert not wall.can_manage_moment('visitor:human', 'visitor:other-ai')
    assert wall.edit_moment('visitor:human', ai_post['id'], '改好', 1)['content'] == '改好'
    with pytest.raises(PublicWallError, match='forbidden'):
        wall.withdraw('visitor:human', unrelated['id'])
    with pytest.raises(PublicWallError, match='ai_already_linked_to_other_human'):
        wall.link_household('another-human', 'ai')
    wall.withdraw('visitor:human', ai_post['id'])
    assert wall.get_moment(ai_post['id']) is None


def test_one_human_can_link_multiple_ais_atomically(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    result = wall.link_households('human', [('ai-one', '一号'), ('ai-two', '二号')], human_name='人甲')
    assert len(result['linked']) == 2
    assert wall.household_human('ai-one') == 'human'
    assert wall.household_human('ai-two') == 'human'
    assert wall.registered_name('human') == '人甲'
    assert wall.registered_name('ai-one') == '一号'
    assert wall.profile('visitor:human')['nickname'] == ''
    wall.set_profile('visitor:human', '星际旅人', '')
    assert wall.registered_name('human') == '人甲'
    assert wall.link_households('human', [('ai-one', '')])['linked'][0]['already_linked'] is True
    wall.link_household('other-human', 'taken-ai')
    with pytest.raises(PublicWallError, match='ai_already_linked_to_other_human'):
        wall.link_households('human', [('new-ai', '新机'), ('taken-ai', '不应转家')])
    assert wall.household_human('new-ai') == ''
    with pytest.raises(PublicWallError, match='invalid_household_link'):
        wall.link_households('human', [('ai-one', ''), ('ai-one', '')])
    with pytest.raises(PublicWallError, match='registered_name_conflict'):
        wall.register_human_name('human', '另一个名字')


def test_rebind_household_changes_guardian_without_changing_ai_identity(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    post = wall.create_moment('visitor:ai', '仍是同一位机发布')
    wall.link_household('human-one', 'ai')
    assert wall.can_manage_moment('visitor:human-one', 'visitor:ai')
    result = wall.rebind_household('ai', 'human-one', 'human-two')
    assert result['human_visitor_id'] == 'human-two'
    assert wall.household_human('ai') == 'human-two'
    assert not wall.can_manage_moment('visitor:human-one', 'visitor:ai')
    assert wall.can_manage_moment('visitor:human-two', 'visitor:ai')
    assert wall.get_moment(post['id'])['author'] == 'visitor:ai'
    with pytest.raises(PublicWallError, match='invalid_household_link'):
        wall.rebind_household('ai', 'human-two', 'human-two')
    with pytest.raises(PublicWallError, match='household_link_changed'):
        wall.rebind_household('ai', 'human-one', 'human-three')
    assert wall.household_human('ai') == 'human-two'


def test_partial_profile_update_preserves_other_field_and_registered_name(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    wall.register_human_name('human', '人甲')
    wall.set_profile('visitor:human', '旧网名', 'https://example.test/avatar.png')
    wall.set_profile('visitor:human', nickname='新网名')
    assert wall.profile('visitor:human') == {
        'actor_id': 'visitor:human', 'nickname': '新网名', 'avatar': 'https://example.test/avatar.png'}
    wall.set_profile('visitor:human', avatar='https://example.test/new.png')
    assert wall.profile('visitor:human')['nickname'] == '新网名'
    assert wall.registered_name('human') == '人甲'
    with pytest.raises(PublicWallError, match='invalid_profile_update'):
        wall.set_profile('visitor:human')


def test_cursor_and_source_receipt(tmp_path):
    wall = PublicWall(tmp_path / 'wall.db'); wall.initialize()
    first = wall.create_moment('k', '一', created_at=1, source_key='run-1')
    replay = wall.create_moment('k', '不同内容', created_at=1, source_key='run-1')
    assert replay['id'] == first['id'] and replay['idempotent_replay']
    second = wall.create_moment('aning', '二', created_at=2)
    page = wall.list_moments(limit=1)
    assert page['items'][0]['id'] == second['id']
    older = wall.list_moments(limit=1, before=tuple(page['next_cursor']))
    assert older['items'][0]['id'] == first['id']
    with sqlite3.connect(wall.path) as db:
        receipts = ''.join(row[0] for row in db.execute('SELECT result_json FROM wall_receipts'))
        assert '不同内容' not in receipts and '一' not in receipts


def test_public_post_keeps_local_notification_and_read_receipt(feed):
    moment = run(feed.create_moment('aning', '给大家看', visibility='public'))
    notices = run(feed.unannounced_notifications('k'))
    assert len(notices) == 1 and notices[0]['moment_id'] == moment['id']
    assert feed.mark_notifications_announced_sync('k', [notices[0]['id']],
        announce_source_id='turn-1') == 1
    assert run(feed.unannounced_notifications('k')) == []
    assert run(feed.restore_main_chat_announcements('turn-1')) == 1
    assert len(run(feed.unannounced_notifications('k'))) == 1
    assert run(feed.mark_notifications_read('k', [notices[0]['id']])) == 1
    assert run(feed.unread_notifications('k')) == []


def test_legacy_public_post_is_moved_on_initialize(tmp_path):
    private = SocialFeedStore(tmp_path / 'private.db')
    original = run(private.create_moment('k', '旧版本已公开', visibility='public'))
    feed = HybridSocialFeedStore(private, PublicWall(tmp_path / 'public.db'))
    run(feed.initialize())
    assert run(private.get_moment(original['id'])) is None
    assert feed.public.get_moment(original['id'])['content'] == '旧版本已公开'
    assert any(row['moment_id'] == original['id'] for row in run(feed.unread_notifications('aning')))


def test_owner_can_edit_private_and_public_without_crossing_identity(feed):
    private = run(feed.create_moment('aning', '旧私密'))
    assert run(feed.edit_moment(private['id'], 'aning', '新私密'))['content'] == '新私密'
    public = run(feed.create_moment('aning', '旧公开', visibility='public'))
    updated = run(feed.edit_moment(public['id'], 'aning', '新公开', public['revision']))
    assert updated['content'] == '新公开' and updated['revision'] == public['revision'] + 1
    with pytest.raises(Exception, match='forbidden'):
        run(feed.edit_moment(public['id'], 'k', '篡改', updated['revision']))


def test_daily_decision_public_post_retains_idempotence_and_notice(feed):
    first = run(feed.commit_daily_summary_decision('2026-09-28', 'post', '晚安大家', 'public'))
    assert feed.public.get_moment(first['moment_id'])['content'] == '晚安大家'
    assert run(feed.private.get_moment(first['moment_id'])) is None
    second = run(feed.commit_daily_summary_decision('2026-09-28', 'post', '不应再发', 'public'))
    assert second['idempotent_replay'] and second['moment_id'] == first['moment_id']
    assert any(row['id'] == first['notification_id'] for row in run(feed.unread_notifications('aning')))
