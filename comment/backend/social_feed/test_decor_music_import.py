"""No real account, network or production store in import acceptance tests."""
import io
import shutil
from types import SimpleNamespace
import wave

from fastapi import FastAPI
import httpx
import pytest

from social_feed import decor_music_import as module
from social_feed.decor_store import DecorError, DecorStore


@pytest.mark.parametrize('source', ['123', 'https://music.163.com/song?id=123',
    'https://music.163.com/#/song?id=123', 'https://y.music.163.com/song?id=123'])
def test_only_standard_song_references(source):
    assert module.song_id(source) == '123'


@pytest.mark.parametrize('source', ['https://localhost/song?id=123', 'https://music.163.com/playlist?id=123',
    'https://music.163.com:8005/song?id=123', 'https://user:secret@music.163.com/song?id=123',
    'https://music.163.com/song?id=123&id=456', 'file:///secret', '1'*21])
def test_invalid_references_never_become_fetch_targets(source):
    with pytest.raises(DecorError, match='invalid_import_song'):
        module.song_id(source)


@pytest.mark.parametrize('url', ['https://127.0.0.1/file', 'https://evil.example/file',
    'https://m.music.126.net.evil.example/file', 'https://m.music.126.net:8005/file',
    'https://secret@m.music.126.net/file', 'file:///music.mp3'])
def test_cdn_allowlist(url):
    with pytest.raises(DecorError):
        module.media_url(url)


class Provider:
    def __init__(self, **audio):
        self.audio = dict(id=123, code=200, url='http://m.music.126.net/audio', time=10000, size=20000,
                          freeTrialInfo=None) | audio
    async def _call(self, function):
        return ({'songs': [{'id':123, 'dt':10000, 'name':'测试歌', 'ar':[{'name':'歌手'}]}]},
                {'code':200, 'data':[self.audio]})


@pytest.mark.asyncio
@pytest.mark.parametrize('fields,reason', [({'freeTrialInfo':{'start':0,'end':5000}},'preview_only'),
    ({'time':5000},'preview_only'), ({'url':None},'unavailable'),
    ({'size':module.MAX_UPLOAD+1},'media_too_large')])
async def test_provider_preview_and_unavailable_fail_before_download(fields, reason):
    with pytest.raises(DecorError, match=reason):
        await module.track_source('123', Provider(**fields))


@pytest.mark.asyncio
async def test_provider_session_is_scoped_and_url_is_tls():
    result = await module.track_source('123', Provider())
    assert result['url']=='https://m.music.126.net/audio'
    assert result['title']=='测试歌' and result['duration_ms']==10000


@pytest.mark.asyncio
async def test_download_pins_host_without_account_headers():
    requests=[]
    async def resolve(url): return '203.0.113.12'
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b'candidate')
    def client(**kwargs): return httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    assert await module.download_audio('https://m.music.126.net/audio',client_factory=client,resolve=resolve)==b'candidate'
    request=requests[0]
    assert request.url.host=='203.0.113.12' and request.headers['Host']=='m.music.126.net'
    assert request.extensions['sni_hostname']=='m.music.126.net'
    assert 'cookie' not in request.headers and 'authorization' not in request.headers


@pytest.mark.asyncio
async def test_redirect_must_pass_cdn_allowlist_again():
    visited=[]
    async def resolve(url): visited.append(url); return '203.0.113.12'
    def client(**kwargs): return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={'location':'https://localhost/secret'})), **kwargs)
    with pytest.raises(DecorError):
        await module.download_audio('https://m.music.126.net/audio',client_factory=client,resolve=resolve)
    assert len(visited)==1


@pytest.mark.asyncio
async def test_relative_redirect_keeps_verified_origin_and_repins():
    visited=[]
    async def resolve(url): visited.append(url); return '203.0.113.12'
    def respond(request):
        if request.url.path=='/audio':
            return httpx.Response(302,headers={'location':'/complete.mp3'})
        assert request.headers['Host']=='m.music.126.net'
        return httpx.Response(200,content=b'complete')
    def client(**kwargs): return httpx.AsyncClient(transport=httpx.MockTransport(respond),**kwargs)
    result=await module.download_audio('https://m.music.126.net/audio',client_factory=client,resolve=resolve)
    assert result==b'complete'
    assert visited==['https://m.music.126.net/audio','https://m.music.126.net/complete.mp3']


@pytest.mark.asyncio
async def test_dns_and_declared_size_rejected():
    async def private(url): raise ValueError('private destination')
    with pytest.raises(DecorError, match='unavailable'):
        await module.download_audio('https://m.music.126.net/audio',resolve=private)
    async def resolve(url): return '203.0.113.12'
    def client(**kwargs): return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200,headers={'content-length':str(module.MAX_UPLOAD+1)})),**kwargs)
    with pytest.raises(DecorError, match='media_too_large'):
        await module.download_audio('https://m.music.126.net/audio',client_factory=client,resolve=resolve)


@pytest.mark.skipif(not shutil.which('ffprobe') or not shutil.which('ffmpeg'),reason='optional audio tools absent')
def test_real_audio_duration_before_and_after_transcode():
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0\0'*8000)
    raw=buffer.getvalue()
    assert abs(module.verify_duration(raw,1000)-1)<0.01
    with pytest.raises(DecorError,match='incomplete'):
        module.verify_duration(raw,60000)
    converted,_,_=module.audio_bytes(raw)
    assert abs(module.verify_duration(converted,1000)-1)<0.2


@pytest.mark.asyncio
async def test_import_returns_candidate_without_saving_home(tmp_path,monkeypatch):
    store=DecorStore(tmp_path/'decor.db'); before=store.home()
    async def prepare(source): return module.ImportedMusic(b'fixture',{'id':'123','title':'测试歌','artist':'歌手'},10)
    monkeypatch.setattr(module,'prepare_music',prepare)
    result=await module.import_music(store,'123')
    assert result['mime']=='audio/mpeg' and result['track']['id']=='123'
    assert store.home()==before and (store.media_dir/result['id']).read_bytes()==b'fixture'
    async def failed(source): raise DecorError('music_import_preview_only')
    monkeypatch.setattr(module,'prepare_music',failed)
    with pytest.raises(DecorError): await module.import_music(store,'123')
    assert store.home()==before


@pytest.mark.asyncio
async def test_busy_import_is_not_queued(monkeypatch):
    import asyncio
    lock=asyncio.Semaphore(1);await lock.acquire();monkeypatch.setattr(module,'_IMPORT_LOCK',lock)
    with pytest.raises(DecorError,match='music_import_busy'):
        await module.import_music(None,'123')


@pytest.mark.asyncio
async def test_route_is_local_owner_only_and_requires_rights_ack(monkeypatch):
    from social_feed.decor_router import create_decor_router, install_errors
    from routers.lounge_reception_router import local_ui
    called=[]
    async def fake(store,source): called.append(source);return {'id':'candidate','track':{'id':'123','title':'测试歌'}}
    monkeypatch.setattr(module,'import_music',fake)
    monkeypatch.setattr('social_feed.decor_router.get_decor_store',lambda:object())
    app=FastAPI();app.include_router(create_decor_router(local=True));install_errors(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
        body={'source':'123','sharing_rights_confirmed':True}
        assert (await client.post('/api/social-decor/music-import',json=body)).status_code==403
        app.dependency_overrides[local_ui]=lambda:None
        assert (await client.post('/api/social-decor/music-import',json={'source':'123'})).status_code==422
        assert (await client.post('/api/social-decor/music-import',json=body|{'sharing_rights_confirmed':False})).status_code==422
        assert (await client.post('/api/social-decor/music-import',json=body)).status_code==200
    public=FastAPI();public.include_router(create_decor_router(runtime=SimpleNamespace()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='https://public.example') as client:
        assert (await client.post('/social/v1/decor/music-import',json=body)).status_code==404
    assert called==['123']


@pytest.mark.asyncio
async def test_shared_music_editor_static_route_pair():
    from social_feed.decor_router import create_decor_router,ui_router
    from routers.lounge_reception_router import local_ui
    app=FastAPI();app.include_router(ui_router);app.include_router(create_decor_router(runtime=SimpleNamespace()))
    app.dependency_overrides[local_ui]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
        local=await client.get('/api/social-decor-ui/music-editor')
        public=await client.get('/social/v1/decor/music-editor.js')
        assert local.status_code==public.status_code==200
        assert local.content==public.content and b'MirrowDecorMusic' in local.content
        assert 'text/javascript' in public.headers['content-type']
