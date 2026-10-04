import base64
import io
import json
import pytest
import httpx
from PIL import Image
from .decor_presets import catalog, import_preset
from .decor_gif_api import decode_request
from .decor_store import DecorError
from .test_decor import gateway


def test_catalog_builtin_and_path_admission(tmp_path):
    assert len(catalog()['themes'])==11 and len(catalog()['frames'])==11
    assert len(catalog()['assets'])==6
    (tmp_path/'catalog.json').write_text(json.dumps({'assets':[{'id':'bad','name':'bad','category':'frame','file':'../private.png'}]}))
    with pytest.raises(DecorError,match='preset_catalog_invalid'):catalog(tmp_path)


def test_invalid_gif_wire_input_is_bounded():
    for value in ({'frames':['x','y']},{'frames':[]},{'frames':['YQ==','Yg=='],'smooth':'yes'}, {'frames':['YQ==','Yg=='],'path':'private'}):
        with pytest.raises(DecorError):decode_request(json.dumps(value).encode())


@pytest.mark.asyncio
async def test_gif_and_preset_admission_uses_existing_human_authority(gateway):
    app,store=gateway
    def frame(color):
        raw=io.BytesIO();Image.new('RGBA',(16,16),color).save(raw,format='PNG')
        return base64.b64encode(raw.getvalue()).decode()
    body={'frames':[frame('red'),frame('blue')],'smooth':False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        result=await client.post('/social/v1/decor/gif/visitor:a',json=body)
        assert result.status_code==200 and result.json()['mode']=='frames'
        asset=store.asset(result.json()['id']);assert asset['owner']=='visitor:a' and asset['mime']=='image/gif'
        with Image.open(store.media_dir/asset['id']) as image:assert image.n_frames==2
        assert (await client.post('/social/v1/decor/gif/visitor:a',json=body,headers={'x-test-actor':'visitor:a'})).status_code==403
        assert (await client.get('/social/v1/decor/presets')).status_code==200
        assert (await client.post('/social/v1/decor/presets/visitor:a',json={'id':'missing'})).status_code==400


@pytest.mark.asyncio
async def test_bundled_fonts_presets_and_new_tokens(gateway):
    from .decor_models import Theme, PersonalDesign
    from .decor_presets import font_path
    from pydantic import ValidationError
    assert font_path('calligraphy').read_bytes()[:4] == b'\x00\x01\x00\x00'
    with pytest.raises(DecorError): font_path('../private')
    with pytest.raises(ValidationError): Theme(font='url(private)')
    for name in ('calligraphy','flower','neon','outline','gold'):
        assert Theme(font=name).font==name and PersonalDesign(font=name).font==name
    app,store=gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://social.example.invalid') as client:
        font=await client.get('/social/v1/decor/fonts/flower')
        assert font.status_code==200 and font.headers['content-type']=='font/ttf'
        assert font.headers['access-control-allow-origin']=='*'
        assert (await client.get('/social/v1/decor/fonts/missing')).status_code==404
        result=await client.post('/social/v1/decor/presets/visitor:a',json={'id':'kenney-frame-000'})
        assert result.status_code==200 and store.asset(result.json()['id'])['owner']=='visitor:a'
