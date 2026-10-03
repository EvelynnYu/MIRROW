"""Capture actual React UI with fictitious fixtures, plus deterministic cover typography."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]

async def main():
    target=ROOT/'media';target.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True)
        page=await browser.new_page(viewport={'width':1080,'height':1440},device_scale_factor=1)
        await page.goto('http://127.0.0.1:5187/cover.html')
        await page.screenshot(path=str(target/'xhs-cover.png'))
        await page.close()
        for name,view in [('demo-mobile-player','player'),('demo-mobile-chat','chat')]:
            page=await browser.new_page(viewport={'width':390,'height':844},device_scale_factor=3,is_mobile=True,has_touch=True)
            page.set_default_timeout(8000)
            errors=[];requests=[]
            page.on('pageerror',lambda e: errors.append(str(e)))
            page.on('request',lambda r:requests.append(r.url))
            await page.goto('http://127.0.0.1:5187/?demo=1&view='+view)
            await page.get_by_text('晚风写了一封信',exact=True).first.wait_for()
            if view=='player': await page.get_by_role('dialog',name='音乐中枢').wait_for()
            await page.screenshot(path=str(target/(name+'.png')))
            if view=='chat':
                print('Checking fictitious card composer / send / delete.',flush=True)
                await page.get_by_role('button',name='＋ 歌曲卡片',exact=True).click()
                dialog=page.get_by_role('dialog',name='分享歌曲')
                await dialog.get_by_placeholder('粘贴歌曲链接或分享文字').fill('https://music.163.com/song?id=900001')
                await dialog.get_by_role('button',name='识别',exact=True).click()
                await dialog.get_by_role('button',name='添加到输入框',exact=True).click()
                await page.get_by_role('button',name='取消添加',exact=True).wait_for()
                await page.get_by_placeholder('说点什么，或分享一首歌…').fill('这是一条虚构交互验收消息。')
                await page.get_by_role('button',name='发送 ↑',exact=True).click()
                await page.get_by_text('这是一条虚构交互验收消息。',exact=True).wait_for()
                await page.locator('.message').last.get_by_role('button',name='删除',exact=True).click()
                await page.get_by_text('这是一条虚构交互验收消息。',exact=True).wait_for(state='detached')
            assert not errors,errors
            assert all(url.startswith('http://127.0.0.1:5187/') or url.startswith('data:') for url in requests)
            await page.close()
        await browser.close()
    print('Captured three clean PNGs using actual UI and fictitious fixtures.')

if __name__=='__main__':asyncio.run(main())
