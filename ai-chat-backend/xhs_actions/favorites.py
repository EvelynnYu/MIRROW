"""Read the active account's own saved notes through the native app."""
import asyncio
import base64
import hashlib
import json
import re
from io import BytesIO

from xhs_interactions.native import InteractionBlocked, as_row
from xhs_interactions.store import digest
from xhs_publish.store import account_config
from .dm_navigation import DirectChatNavigation

_keyboard_key = {}


async def press_search_key(phone, query):
    """Some keyboards accept IME_ENTER but submit only their visible Search key."""
    from PIL import Image
    from llm_client import call_vision_api, official_flash_vision_ready
    def editor():
        return [r for r in phone.rows if r['class']=='android.widget.EditText' and r['text']==query
                and r.get('focused')=='true']
    if len(editor())!=1 or not phone.matching_rows('已选定收藏'):
        return False
    if not await phone.device.xhs_keyboard_visible():
        await phone.tap(editor()[0])
        if not await phone.device.xhs_keyboard_visible():
            return False
    async def crop():
        data = await phone.device.screenshot()
        image = Image.open(BytesIO(data)).convert('RGB')
        w,h=image.size
        region=(int(w*.79),int(h*.87),w,h)
        output=BytesIO()
        image.crop(region).save(output,format='PNG')
        return output.getvalue(), region
    pixels, region = await crop()
    key=hashlib.sha256(pixels).digest()
    point=_keyboard_key.get(key)
    if point is None:
        official_flash_vision_ready()
        result=await call_vision_api(base64.b64encode(pixels).decode(),
            '截图仅含安卓输入法右下角。在确实看见文字为“搜索”的键时，返回它中心在这张裁剪图内的千分比坐标 JSON {"label":"搜索","x":500,"y":500}。其他键或不确定时返回 {}。',
            model='flash-vision',mime_type='image/png',max_tokens=1500,official_only=True)
        raw=str(result.get('content','')).strip().removeprefix('```json').removesuffix('```').strip()
        obj=json.loads(raw)
        if not isinstance(obj,dict) or obj.get('label')!='搜索':
            return False
        point=(obj.get('x'),obj.get('y'))
        if not all(type(v) is int and 0<v<1000 for v in point):
            return False
        _keyboard_key.clear()
        _keyboard_key[key]=point
    await phone.observe()
    if len(editor())!=1 or not phone.matching_rows('已选定收藏') or not await phone.device.xhs_keyboard_visible():
        return False
    live, actual=await crop()
    if live!=pixels or actual!=region:
        return False
    left,top,right,bottom=region
    return await phone.device.tap(left+(right-left)*point[0]//1000, top+(bottom-top)*point[1]//1000)


def saved_cards(root, search=False):
    result = []
    for node in root.iter('node'):
        desc = node.get('content-desc', '')
        pattern = r'笔记\s+(.+) 来自(.+) [^ ]+赞' if search else r'笔记,(.*),来自(.*),[^,]*[，,]?'
        match = re.fullmatch(pattern, desc)
        if match:
            row = as_row(node)
            if row['box'][3] > row['box'][1]:
                result.append({'title': match[1].strip(), 'author': match[2].strip(), 'box': row['box']})
    return result


async def open_collection(phone, query):
    await phone.home(account_config())
    tabs = [r for r in phone.rows if r['content-desc'] in ('收藏','已选定收藏')]
    if len(tabs) != 1:
        raise InteractionBlocked('favorite_tab_missing')
    if tabs[0]['content-desc'] == '收藏':
        await phone.tap(tabs[0])
    if not phone.matching_rows('已选定收藏'):
        raise InteractionBlocked('favorite_tab_unverified')
    if not query:
        return
    await phone.click('搜索', desc=True)
    edits = [r for r in phone.rows if r['class'] == 'android.widget.EditText' and
             r['text'] == '搜索我的笔记/收藏/赞过']
    if len(edits) != 1:
        raise InteractionBlocked('favorite_search_scope_unknown')
    phone.owns_favorite_search = True
    await phone.tap(edits[0])
    if not await phone.device.search_favorites(edits[0]['box'], query):
        raise InteractionBlocked('favorite_search_not_submitted')
    for attempt in range(4):
        await phone.observe()
        if (any(r['class']=='android.widget.EditText' and r['text']==query for r in phone.rows)
                and favorite_result_scope(phone.rows)):
            return
        if attempt==1 and not await press_search_key(phone,query):
            break
        await asyncio.sleep(.4)
    raise InteractionBlocked('favorite_results_unverified')


def favorite_result_scope(rows):
    # The background profile and the foreground search both expose tab strips.
    history = [r for r in rows if r.get('content-desc')=='浏览记录']
    selected = [r for r in rows if r.get('content-desc')=='已选定收藏']
    return any(abs(h['box'][1] - s['box'][1]) < 20 for h in history for s in selected)

async def leave_search(phone):
    if (getattr(phone,'owns_favorite_search',False) and phone.matching_rows('已选定收藏') and phone.matching_rows('取消') and
            any(r['class']=='android.widget.EditText' for r in phone.rows)):
        await phone.click('取消')
        phone.owns_favorite_search = False

async def fetch_saved_post(source, query, excluded):
    """Pass a real saved note through the usual browsing and image-reading pipeline."""
    from wander_manager.xhs_profile_navigation import ProfileNavigator
    phone = DirectChatNavigation()
    with await phone.device.lease_async('xhs_read_favorites', timeout=12):
        serial, status = await source._select_device()
        if not serial:
            return {'status':status}
        try:
            await open_collection(phone, query)
            last = None
            for _ in range(5):
                cards = saved_cards(phone.root, bool(query))
                keys = [digest(['favorite-card',c['title'],c['author']]) for c in cards]
                if keys == last:
                    break
                last = keys
                for card, key in zip(cards, keys):
                    if key in excluded or keys.count(key) != 1:
                        continue
                    await phone.tap(card)
                    result = await ProfileNavigator(source, serial).read_article(card)
                    if result.get('status') == 'success':
                        result.update(source_id=key, query=query, source_identity_kind='favorite_card_title_author')
                        # The navigator returns to the collection after a verified read.
                        # Refresh the actual UI before deciding whether to close the search.
                        try:
                            await phone.observe()
                        except InteractionBlocked:
                            # A new foreground window cannot invalidate material already read.
                            phone.owns_favorite_search = False
                    return result
                if not cards:
                    break
                await phone.device.swipe(540,1770,540,920,450)
                await phone.observe()
            return {'status':'no_new_feed_cards'}
        finally:
            await leave_search(phone)
