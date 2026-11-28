"""Observed UI state machine for the dedicated XHS phone (no model coordinates)."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import re
import unicodedata
from urllib.parse import quote

from .xiaohongshu_adb import _parse_bounds, _verified_note, XIAOHONGSHU_PACKAGE


def normalized(value):
    return unicodedata.normalize('NFKC', str(value)).strip()


def elements(snapshot):
    return list(snapshot.root.iter('node')) if snapshot.available else []


def profile_cards(snapshot, author):
    cards = []
    for node in elements(snapshot):
        desc = node.get('content-desc', '')
        match = re.fullmatch(r'笔记,(.*),来自(.*),[^,]*[，,]?', desc)
        bounds = _parse_bounds(node.get('bounds', ''))
        if match and bounds and normalized(match[2]) == normalized(author):
            # Title/author is an observed listing key, never a platform note ID.
            cards.append({'title': match[1], 'author': match[2], 'bounds': bounds,
                          'key': hashlib.sha256(normalized(match[2] + '\n' + match[1]).encode()).hexdigest()})
    return cards


def identity(snapshot):
    match = re.search(r'小红书号[：:]\s*([A-Za-z0-9_-]+)', snapshot.text)
    return match[1] if match else ''


def gallery_position(snapshot):
    from .xhs_gallery import position
    return position(snapshot.root) if snapshot.available else None


class ProfileNavigator:
    def __init__(self, source, serial):
        self.source, self.serial = source, serial

    async def snapshot(self):
        snap = await self.source._dump_ui(self.serial)
        guard = self.source._guard_snapshot(snap)
        if guard or not snap.available:
            raise ValueError(guard or 'ui_unavailable')
        return snap

    async def command(self, *args):
        result = await self.source._run_device(self.serial, *args)
        if result is None or result.returncode:
            raise ValueError('navigation_command_failed')
        await self.source._sleeper(self.source.post_wait_seconds)

    async def tap(self, node):
        bounds = _parse_bounds(node.get('bounds', '')) if hasattr(node, 'get') else None
        if not bounds:
            raise ValueError('target_bounds_missing')
        x1, y1, x2, y2 = bounds
        await self.command('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))

    async def enter_profile(self, query, cursor):
        uri = 'xhsdiscover://search/result?keyword=' + quote(query, safe='')
        await self.command('shell', 'am', 'start', '-W', '-a', 'android.intent.action.VIEW',
                           '-d', uri, '-p', XIAOHONGSHU_PACKAGE)
        snap = await self.snapshot()
        tabs = [node for node in elements(snap) if node.get('text') == '用户']
        if not tabs:
            raise ValueError('user_tab_not_found')
        await self.tap(tabs[0])
        snap = await self.snapshot()
        matches = [node for node in elements(snap)
                   if normalized(node.get('text', '')) == normalized(query)
                   and (_parse_bounds(node.get('bounds', '')) or (0, 0, 0, 0))[1] > 380]
        if len(matches) != 1:
            raise ValueError('profile_candidate_ambiguous' if matches else 'profile_not_found')
        await self.tap(matches[0])
        snap = await self.snapshot()
        profile_id = identity(snap)
        if not profile_id:
            raise ValueError('profile_identity_missing')
        if not any(normalized(node.get('text', '')) == normalized(query) for node in elements(snap)):
            raise ValueError('profile_name_unconfirmed')
        if cursor.get('profile_id') and cursor['profile_id'] != profile_id:
            raise ValueError('profile_identity_changed')
        # The exact name was selected in the user results. Profile ID is frozen for resume.
        return snap, profile_id

    async def fetch(self, task, cursor):
        cursor = deepcopy(cursor or {})
        if task.get('profile_target') == 'self':
            from xhs_publish.store import account_config
            from .xiaohongshu_adb import XIAOHONGSHU_LAUNCH_ACTIVITY
            from .xhs_feed_navigation import enter_tab
            account = account_config()
            await self.command('shell', 'am', 'start', '-W', '-a', 'android.intent.action.MAIN',
                               '-c', 'android.intent.category.LAUNCHER', '-n',
                               f'{XIAOHONGSHU_PACKAGE}/{XIAOHONGSHU_LAUNCH_ACTIVITY}')
            snap = await enter_tab(self, await self.snapshot(), '我')
            profile_id = identity(snap)
            if (profile_id != account['account_id'] or not any(
                    normalized(n.get('text', '')) == normalized(account['account_name']) for n in elements(snap))):
                raise ValueError('interaction_account_mismatch')
            if cursor.get('profile_id') and cursor['profile_id'] != profile_id:
                raise ValueError('profile_identity_changed')
            task = {**task, 'query': account['account_name']}
        else:
            snap, profile_id = await self.enter_profile(task['query'], cursor)
        confirmed_path = cursor.get('listing_path', [])
        next_index = int(cursor.get('read_count', 0))
        if len(confirmed_path) != len(set(confirmed_path)) or next_index != len(confirmed_path):
            return {'status': 'profile_cursor_ambiguous', 'coverage': {'listing': 'partial'}}
        discovered = []
        previous = None
        stalled = 0
        for page in range(30):
            cards = profile_cards(snap, task['query'])
            keys = [card['key'] for card in cards]
            if len(keys) != len(set(keys)):
                return {'status': 'profile_listing_ambiguous', 'coverage': {'listing': 'partial'}}
            if previous is not None and any(key in discovered and key not in previous for key in keys):
                return {'status': 'profile_listing_ambiguous', 'coverage': {'listing': 'partial'}}
            # A masonry grid can retain non-contiguous cards after scrolling.
            # Suffix/prefix overlap invents new ordinals for those old cards.
            for key in keys:
                if key not in discovered:
                    discovered.append(key)
            # Image loading changes masonry heights and first-visible order.
            # The cursor records confirmed reads, not a reconstructed global rank.
            available = [card for card in cards if card['key'] not in confirmed_path]
            # Do not tap a clipped card whose center falls in the fixed navigation bar.
            available = [card for card in available if card['bounds'][1] < snap.height - 180
                         and (card['bounds'][1]+card['bounds'][3])//2 < snap.height - 130]
            if available:
                card = available[0]
                x1, y1, x2, y2 = card['bounds']
                await self.command('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))
                result = await self.read_article(card, profile_id)
                if result.get('status') == 'success':
                    result['cursor_after'] = {'version': 1, 'profile_id': profile_id,
                        'listing_path': confirmed_path+[card['key']], 'last_listing_key': card['key'],
                        'listing': 'partial', 'read_count': next_index+1}
                    result['cursor_before'] = cursor
                return result
            if (not any(key not in confirmed_path for key in discovered) and
                    any(marker in snap.text for marker in ('没有更多了', '没有更多笔记', '到底了'))):
                return {'status': 'profile_end_observed', 'coverage': {'listing': 'explicit_end'},
                        'cursor_before': cursor, 'profile_id': profile_id}
            stalled = stalled + 1 if keys == previous else 0
            if stalled >= 2:
                return {'status': 'profile_listing_stalled', 'coverage': {'listing': 'unproven_end'},
                        'profile_id': profile_id, 'observed_count': next_index}
            previous = keys
            await self.command('shell', 'input', 'swipe', '540', '1900', '540', '700', '280')
            snap = await self.snapshot()
        return {'status': 'profile_page_budget', 'coverage': {'listing': 'partial'}}

    async def read_article(self, card, profile_id=''):
        snap = await self.snapshot()
        authors = [node.get('text', '') for node in elements(snap)
                   if node.get('resource-id', '').endswith('/nickNameTV')]
        if not authors or normalized(authors[0]) != normalized(card['author']):
            return {'status': 'article_author_unconfirmed'}
        title = card['title']
        frames = []
        gallery_stop = ''
        seen_screens = set()
        last_captured = snap

        async def capture(current, label):
            nonlocal gallery_stop, last_captured
            current_position = gallery_position(current)
            if current_position:
                current_authors = [node.get('text', '') for node in elements(current)
                                   if node.get('resource-id', '').endswith('/nickNameTV')]
                if current_authors != [card['author']]:
                    gallery_stop = 'gallery_author_changed'
                    return False
                observed = _verified_note(current)
                if observed and normalized(observed['note_title']) != normalized(title):
                    gallery_stop = 'gallery_note_changed'
                    return False
            image = await self.source._screenshot(self.serial)
            if not image:
                gallery_stop = 'gallery_screenshot_failed'
                return False
            try:
                raw_image = base64.b64decode(image, validate=True)
            except (ValueError, TypeError):
                gallery_stop = 'gallery_screenshot_failed'
                return False
            digest = hashlib.sha256(raw_image).hexdigest()
            if current_position and digest in seen_screens:
                gallery_stop = 'gallery_duplicate_screen'
                return False
            if current_position:
                seen_screens.add(digest)
            frame = {'label': label, 'screenshot_base64': image, 'ui_text': current.text[:20000]}
            if current_position:
                frame.update(page=current_position[0], total=current_position[1], screenshot_sha256=digest,
                             screenshot_bytes=len(raw_image))
            frames.append(frame)
            last_captured = current
            return True

        from .xhs_gallery import collect
        video = any('video' in node.get('resource-id', '').lower() for node in elements(snap))
        initial_gallery = gallery_position(snap)
        async def retreat_gallery():
            await self.command('shell', 'input', 'swipe', '180', '900', '900', '900', '280')
        gallery_complete = False
        try:
            snap, gallery_complete = await collect(snap, get_position=gallery_position, capture=capture,
                advance=lambda: self.command('shell', 'input', 'swipe', '900', '900', '180', '900', '280'),
                refresh=self.snapshot, retreat=retreat_gallery, require_first=True,
                limit=initial_gallery[1] if initial_gallery else 1)
        except ValueError as exc:
            gallery_stop = str(exc)
        if not frames and initial_gallery:
            current = gallery_position(snap)
            if (gallery_stop or not current or current[1] != initial_gallery[1] or
                    not await capture(snap, f'image_{current[0]}_of_{current[1]}')):
                return {'status':'gallery_capture_failed','error':gallery_stop or 'first_page_unconfirmed'}
        if not frames:
            return {'status': 'screenshot_failed'}
        if initial_gallery and not gallery_complete and not gallery_position(snap) and not gallery_stop:
            gallery_stop = 'gallery_position_unconfirmed'
        if gallery_stop:
            snap = last_captured
        # Accessibility may expose body text outside the clipped visual bounds.
        # An observed date alone does not prove that a folded article was complete.
        body, title_seen = '', False
        title_seen_step = None
        title_key = ' '.join(normalized(title).split())
        for step in range(5):
            current_nodes = elements(snap)
            title_index = next((i for i, node in enumerate(current_nodes)
                                if ' '.join(normalized(node.get('text', '')).split()) == title_key), None)
            if title_index is not None:
                title_seen = True
                if title_seen_step is None:
                    title_seen_step = step
                # The installed app obfuscates body IDs. Bind the text after the
                # verified title before comments; title may begin below a tall image.
                for node in current_nodes[title_index+1:]:
                    if '评论' in node.get('content-desc', ''):
                        break
                    value = node.get('text', '').strip()
                    if value and value not in {'展开全文', '展开正文'}:
                        if not body or (value.startswith(body[:40]) and len(value) > len(body)):
                            body = value
                        break
            if gallery_stop:
                break
            expand = [node for node in elements(snap) if node.get('text') in {'展开全文', '展开正文'}]
            if len(expand) == 1:
                await self.tap(expand[0])
                snap = await self.snapshot()
                # Rebind using the title on the next inspection. A comment
                # quoting the same prefix is not evidence of a longer body.
                if step < 4:
                    continue
            if body and not profile_id and _verified_note(snap):
                # A collection card has no author ID. This is the native detail
                # snapshot on which the original comment/share locator can be verified.
                break
            if body and title_seen_step is not None and step > title_seen_step:
                break
            if step < 4:
                # A fast list fling can jump over a short caption under a tall
                # image. Use one fixed slow reading gesture, then inspect again.
                await self.command('shell', 'input', 'swipe', '540', '1800', '540', '1200', '700')
                snap = await self.snapshot()
        if not title_seen:
            if not gallery_stop:
                await self.command('shell', 'input', 'keyevent', 'KEYCODE_BACK')
            return {'status': 'article_title_unconfirmed'}
        text = body
        article = text[:20000]
        locator = None
        if profile_id:
            locator = {'version': 1, 'profile_id': profile_id, 'note_title': title,
                       'note_author': card['author'], 'note_body': article}
        elif not gallery_stop:
            from xhs_interactions.post_navigation import capture_locator
            locator = await capture_locator(self.source, self.serial, snap)
            if locator and (normalized(locator['note_title']) != normalized(title)
                            or normalized(locator['note_author']) != normalized(card['author'])):
                locator = None
        digest = hashlib.sha256((profile_id+'\n'+title+'\n'+article).encode()).hexdigest()
        if not gallery_stop:
            await self.command('shell', 'input', 'keyevent', 'KEYCODE_BACK')
        return {'status': 'success', 'source_id': 'xhs-observed:'+digest,
                'source_identity_kind': 'profile_title_body_digest_not_platform_id',
                'source_url': '', 'profile_id': profile_id, 'title': title,
                'comment_locator': locator,
                'ui_text': frames[0]['ui_text'], 'article_text': article,
                'frames': frames, 'coverage': {'text': 'partial',
                    'text_omitted_chars': max(0, len(text)-len(article)),
                    'images': 'all_captured' if gallery_complete else 'partial_or_unknown',
                    'video': 'not_played' if video else 'not_confirmed', 'listing': 'partial'},
                'gallery_capture': {'status': 'complete' if gallery_complete else (gallery_stop or 'partial'),
                    'declared_total': initial_gallery[1] if initial_gallery else 0,
                    'captured_pages': [frame['page'] for frame in frames if 'page' in frame],
                    'page_limit': initial_gallery[1] if initial_gallery else 1, 'capture_complete': gallery_complete},
                'limitations': '正文来自标题后绑定的可访问文本节点；全文边界尚未证明。列表按已确认读取的卡片键续读，未取得平台笔记ID；视频未完整播放。'}


async def fetch_profile(source, task, cursor):
    lease = await source.lease_manager.acquire_async('xhs_profile', timeout=0)
    try:
        serial, state = await source._select_device()
        if not serial:
            return {'status': state}
        battery = await source._read_battery(serial)
        if not battery or battery.level is None:
            return {'status': 'battery_unavailable'}
        if battery.level < 20 and not battery.charging:
            return {'status': 'battery_low'}
        wake = await source._wake_and_unlock(serial)
        if wake != 'ok':
            return {'status': wake}
        return await ProfileNavigator(source, serial).fetch(task, cursor)
    except ValueError as exc:
        # All ValueErrors above are fixed local contract codes, no device routing data.
        return {'status': str(exc)}
    finally:
        lease.release()
