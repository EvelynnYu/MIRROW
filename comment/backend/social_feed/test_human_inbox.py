import asyncio
from types import SimpleNamespace

import pytest
from social_feed.public_wall import PublicWall, PublicWallError
from social_feed.notification_schema import migrate, visitor_inbox_ready
from social_feed.human_inbox import collect


def test_visitor_interactions_self_skip_unlike_and_deleted_comment(tmp_path):
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    post=wall.create_moment('visitor:owner','A post')
    own=wall.add_comment('visitor:owner',post['id'],'own')
    assert wall.notifications('visitor:owner')==[]
    reply=wall.add_comment('visitor:friend',post['id'],'reply',own['id'])
    assert any(n['comment_id']==reply['id'] for n in wall.notifications('visitor:owner'))
    wall.set_like('visitor:friend',post['id'],True)
    assert any(n['kind']=='like' for n in wall.notifications('visitor:owner'))
    wall.set_like('visitor:friend',post['id'],False)
    assert not any(n['kind']=='like' for n in wall.notifications('visitor:owner'))
    wall.delete_comment('visitor:friend',post['id'],reply['id'])
    assert wall.notifications('visitor:owner')==[]


def test_explicit_migration_preserves_receipts_and_requires_backup(tmp_path):
    wall=PublicWall(tmp_path/'wall.db');wall.initialize()
    post=wall.create_moment('k','hello')
    notice=wall.notifications('aning')[0]
    wall.mark_notifications_read('aning',[notice['id']],'fixture')
    with wall._connect() as db:
        before=dict(db.execute('SELECT * FROM wall_notifications WHERE id=?',(notice['id'],)).fetchone())
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='wall_notifications'").fetchone()[0]
        sql=sql.replace('wall_notifications','old_receipts',1).replace(" OR recipient GLOB 'visitor:*'",'')
        db.execute(sql);db.execute('INSERT INTO old_receipts SELECT * FROM wall_notifications')
        db.execute('DROP TABLE wall_notifications');db.execute('ALTER TABLE old_receipts RENAME TO wall_notifications')
        assert not visitor_inbox_ready(db)
    with pytest.raises(PublicWallError,match='backup_required'):migrate(wall)
    migrate(wall,backup_ready=True);migrate(wall,backup_ready=True)
    with wall._connect() as db:
        assert visitor_inbox_ready(db)
        assert dict(db.execute('SELECT * FROM wall_notifications WHERE id=?',(notice['id'],)).fetchone())==before
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert wall.get_moment(post['id'])['content']=='hello'


class Local:
    async def unread_notifications(self,actor,*,limit):
        return [{'id':'same','actor':'k','kind':'comment','moment_id':'local','created_at':2}]
    async def list_moments(self,**kwargs):return {'items':[]}


def site(id):return SimpleNamespace(id=id,name=id,enabled=True,human_key='fixture-human',ai_key='fixture-ai')


@pytest.mark.asyncio
async def test_inbox_source_identity_partial_failure_read_only_and_k_independent():
    calls=[]
    class Client:
        async def request(self,s,role,method,path,**kwargs):
            calls.append((s.id,role,method,path))
            if s.id=='offline':raise RuntimeError('offline')
            if path.endswith('/me'):
                assert kwargs['params']=={'arrival':'false'}
                return {'actor':{'actor_id':'visitor:self'},'can_manage_avatar':role=='human','capabilities':['inbox_v1','visitor_interactions_v1']}
            if path.endswith('/moments'):return {'items':[]}
            return {'items':[{'id':'same','actor':'visitor:friend','kind':'like','moment_id':'foreign','created_at':3}]}
    result=await collect(Local(),[site('friend'),site('offline')],client=Client())
    assert {(n['site_id'],n['id']) for n in result['items']}=={('','same'),('friend','same')}
    assert result['items'][0]['priority']=='quiet'
    assert result['unavailable']==[{'site_id':'offline','site_name':'offline','status':'unavailable'}]
    assert all(call[2]=='GET' for call in calls)
    calls.clear()
    await collect(Local(),[site('friend')],actor='k',client=Client(),with_heads=False)
    assert all(call[1]=='ai' for call in calls)
    assert not any(call[3].endswith('/moments') for call in calls)


@pytest.mark.asyncio
async def test_profile_refresh_is_background_and_coalesced(tmp_path,monkeypatch):
    from social_feed import profile_transport
    gate=asyncio.Event();called=[]
    async def slow(*args):called.append(True);await gate.wait()
    monkeypatch.setattr(profile_transport,'refresh_due',slow)
    wall=SimpleNamespace(path=tmp_path/'wall.db')
    profile_transport.schedule_refresh(wall,'https://example.test')
    profile_transport.schedule_refresh(wall,'https://example.test')
    await asyncio.sleep(0)
    assert called==[True]
    gate.set();await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_k_opening_reads_complete_page_and_unread_index_without_consuming(monkeypatch):
    from wander_manager.social_circle_visit import SocialCircleVisit
    from social_feed import timeline,remote_sites,human_inbox
    monkeypatch.setattr(remote_sites,'get_remote_site_store',lambda:SimpleNamespace(list=lambda:[]))
    read=[]
    async def page(self,**kwargs):
        read.append(kwargs);return {'items':[{'id':'post'}],'next_cursor':None,'unavailable':[],'omitted_sources':0}
    async def inbox(*args,**kwargs):
        assert kwargs['actor']=='k' and kwargs['with_heads'] is False
        return {'items':[{'id':'notice','site_id':'other','moment_id':'another','comment_content':'must not masquerade as body'}]}
    monkeypatch.setattr(timeline.SocialTimeline,'read',page)
    monkeypatch.setattr(human_inbox,'collect',inbox)
    visit=SocialCircleVisit(None,store=Local())
    items,consumed,facts=await visit._read({'kind':'all','opening':True})
    assert items==[{'id':'post'}] and consumed==[]
    assert read==[{'limit':10,'visibility':'all','cursor':None}]
    assert facts['read_only'] and facts['related_unread'][0]['site_id']=='other'
    assert 'comment_content' not in facts['related_unread'][0]
