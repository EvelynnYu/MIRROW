"""Observed XHS inbox, original-comment binding and native reply execution."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, time as day_time
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from xhs_publish.publisher import NativePublisher, nodes, labels, PublicationBlocked
from xhs_publish.store import ROOT, account_config, now
from .service import InteractionBlocked
from .store import digest

CONFIG_PATH=ROOT/'events'/'xhs_interactions_config.json'
UI_CONTRACT='xhs-native-interactions-v1'
ZONE=ZoneInfo('Asia/Shanghai')
ACTIONS={'评论了你的笔记':'comment','在评论中@了你':'mention','回复了你的评论':'comment'}

def config():
    try:
        value=json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        if not isinstance(value,dict): return {}
        if value.get('account')!=account_config() or value.get('ui_contract')!=UI_CONTRACT:
            return {}
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,TypeError):
        return {}

def available():
    return config().get('ready') is True

def box(node):
    values=list(map(int,re.findall(r'\d+',node.get('bounds',''))))
    return values if len(values)==4 else [0,0,0,0]

def as_row(node):
    value=dict(node.attrib);value['box']=box(node);return value

def text(node):
    return node.get('text','')

def source_day(label,observed):
    """Only assign a calendar identity when the native display proves its day."""
    label=label.strip()
    if label=='刚刚':
        lower,upper=observed-timedelta(minutes=1),observed
    elif m:=re.fullmatch(r'(\d+)(分钟|小时|秒)前',label):
        step={'分钟':timedelta(minutes=1),'小时':timedelta(hours=1),'秒':timedelta(seconds=1)}[m[2]]
        lower,upper=observed-step*(int(m[1])+1),observed-step*int(m[1])
    elif m:=re.fullmatch(r'(今天|昨天|前天)\s*(\d{1,2}):(\d{2})',label):
        day=observed.date()-timedelta(days={'今天':0,'昨天':1,'前天':2}[m[1]])
        lower=upper=datetime.combine(day,day_time(int(m[2]),int(m[3])),ZONE)
    elif m:=re.fullmatch(r'(\d{1,2}):(\d{2})',label):
        lower=upper=datetime.combine(observed.date(),day_time(int(m[1]),int(m[2])),ZONE)
    elif m:=re.fullmatch(r'(\d{2})-(\d{2})(?:\s+(\d{1,2}):(\d{2}))?',label):
        year=observed.year
        day=datetime(year,int(m[1]),int(m[2]),tzinfo=ZONE).date()
        if day>observed.date(): day=day.replace(year=year-1)
        lower=datetime.combine(day,day_time(int(m[3] or 0),int(m[4] or 0)),ZONE)
        upper=lower if m[3] else datetime.combine(day,day_time.max,ZONE)
    else:
        return None
    if lower.date()!=upper.date(): return None
    return lower.date().isoformat(),lower,upper

def inbox_rows(root,observed):
    parents={c:p for p in root.iter() for c in p}
    result=[]
    for action in root.iter('node'):
        if text(action) not in ACTIONS: continue
        row=action
        while row in parents and row.get('clickable')!='true': row=parents[row]
        if row.get('class')!='android.widget.LinearLayout': continue
        children=[n for n in row.iter('node') if text(n)]
        clicked=[n for n in children if n.get('clickable')=='true' and n.get('class')=='android.widget.TextView']
        author=next((text(n).strip() for n in clicked if box(n)[1]<box(action)[1]),'')
        line=parents.get(action)
        time_nodes=[n for n in line.iter('node') if n is not action and text(n)] if line is not None else []
        stamp=next((source_day(text(n),observed) for n in time_nodes if source_day(text(n),observed)),None)
        body_nodes=[n for n in children if box(n)[1]>=box(action)[3]
                    and box(n)[0]==box(action)[0]]
        body=next((text(n).strip() for n in body_nodes if text(n).strip()),'')
        kind=ACTIONS[text(action)]
        day=stamp[0] if stamp else 'unresolved_date'
        supported=bool(author and stamp and body and not body.endswith(('…','...')))
        ref=digest(['xhs-ui-notification-v1',author,text(action),body,day])
        result.append({'source_ref':ref,'kind':kind,'actor_name':author,'notification_text':body,
                       'native_action':text(action),'source_day':day,'source_time':stamp[1].isoformat() if stamp else '','native_time_label':text(time_nodes[0]) if time_nodes else '',
                       'observed_at':observed.isoformat(),'identity_basis':'visible_notification_fields_and_day',
                       '_node':row,'_body':next((n for n in body_nodes if text(n).strip()==body),None),
                       '_lower':stamp[1] if stamp else None,'_upper':stamp[2] if stamp else None,
                       '_supported':supported})
    counts=Counter(x['source_ref'] for x in result)
    return result,{key for key,count in counts.items() if count>1}

def comment_rows(root):
    parents={c:p for p in root.iter() for c in p}
    found={}
    for body in root.iter('node'):
        if body.get('class')!='android.widget.TextView' or body.get('long-clickable')!='true' or not text(body).strip():continue
        row=body
        while row in parents and row.get('clickable')!='true': row=parents[row]
        if row.get('class')!='android.widget.LinearLayout': continue
        times=[n for n in row.iter('node') if text(n).strip().endswith('回复') and n is not body]
        authors=[n for n in row.iter('node') if n.get('class')=='android.widget.TextView'
                 and n.get('clickable')=='true' and text(n).strip() and box(n)[1]<=box(body)[1]]
        if not times or not authors:continue
        author=next((n for n in authors if box(n)[0]==box(body)[0]),authors[0])
        key=tuple(box(row))
        found[key]={'actor_name':text(author).strip(),'text':text(body),'node':body,'row':row}
    return list(found.values())

def matches_notification_comment(body, incoming, account_name):
    actual = body.strip()
    expected = incoming['notification_text'].strip()
    if actual == expected:
        return True
    # The notification renders a single space after the tagged account. Its
    # expanded comment TextView also inserts a space at the end of the tag span.
    # Preserve the rest of the authored text and the exact tagged identity.
    tag = '@' + account_name + ' '
    return bool(incoming['kind'] == 'mention' and account_name and expected.startswith(tag)
                and actual == tag + ' ' + expected[len(tag):])


def reply_text_matches(actual, expected):
    # Observed native comment TextViews append one presentation separator and
    # may insert extra whitespace between rendered spans (for example an
    # entity/topic-looking phrase).  Compare the authored characters with
    # whitespace ignored entirely; non-whitespace characters must still match
    # in order and in full.
    if actual == expected or actual == expected + ' ':
        return True
    if actual.endswith(' '):
        actual = actual[:-1]
    if not actual or not expected or actual[-1].isspace() != expected[-1].isspace():
        return False
    # Preserve authored line breaks. Native styled spans may add horizontal
    # spacing inside a line, but two presentation suffixes are ambiguous.
    return [line.replace(' ', '') for line in actual.split('\n')] == [
        line.replace(' ', '') for line in expected.split('\n')]


def in_reply_thread(root, parent, child):
    if child['row'] in list(parent['row'].iter()):
        return child['row'] is not parent['row']
    # This XHS build renders indented replies as adjacent RecyclerView items,
    # rather than descendants of the clickable parent-comment item.
    parents={c:p for p in root.iter() for c in p}
    container=parents.get(parent['row'])
    if (container is None or parents.get(child['row']) is not container
            or container.get('class')!='androidx.recyclerview.widget.RecyclerView'):
        return False
    siblings=list(container);start=siblings.index(parent['row']);end=siblings.index(child['row'])
    if end<=start:return False
    known={r['row']:r for r in comment_rows(root)}
    original=box(parent['row']);body=box(parent['node']);previous=original[3]
    for row in siblings[start+1:end+1]:
        candidate=known.get(row)
        if candidate is None:return False
        bounds=box(row)
        if (bounds[0]<=original[0] or bounds[2]!=original[2] or bounds[1]<previous
                or box(candidate['node'])[0]<=body[0]):return False
        previous=bounds[3]
    return True

class NativeInbox(NativePublisher):
    def __init__(self,device=None,*,since=None):
        super().__init__(device)
        self.root=None
        self.since_override=since
        self.last_target=None
        self.window = None
        self.badge_report = None
        self.visible_dm_unread = 0
        self.dm_candidates_observed = False
    async def observe(self):
        raw=await self.device.ui_dump()
        try:self.root=ET.fromstring(raw)
        except (ET.ParseError,TypeError):raise InteractionBlocked('ui_unavailable') from None
        self.rows=nodes(raw)
        from .unread import observe_labels
        observe_labels(row.get('content-desc', '') for row in self.rows)
        if not self.rows or not any(n.get('package')=='com.xingin.xhs' for n in self.root.iter('node')):
            raise InteractionBlocked('xhs_window_unavailable')
        if any(x in labels(self.rows) for x in ('拖动滑块完成拼图','请完成安全验证','请输入验证码','手机号登录')):
            raise InteractionBlocked('authentication_or_verification_required')
        return self.rows
    async def home(self,account):
        if await self.device.get_state()!='device':raise InteractionBlocked('device_disconnected')
        battery=await self.device.battery()
        if battery.get('level') is None or (battery['level']<20 and not battery.get('charging')):
            raise InteractionBlocked('battery_unavailable_or_low')
        await self.device.wake()
        await self.device.launch('com.xingin.xhs',component='.index.v2.IndexActivityV2')
        # The app restores its last screen.  Leave a settle window, then return
        # to the main tabs; a back press without waiting dumps the old screen
        # and makes every retry observe the same non-home page.
        await asyncio.sleep(1.0)
        await self.observe()
        for _ in range(8):
            edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
            if (len(edits)==1 and edits[0].get('text') in ('发消息…','发消息...') and
                    edits[0]['box'][1]>600 and
                    any(a.get('class')=='androidx.recyclerview.widget.RecyclerView' and a['box'][1]<500 for a in self.rows)):
                await self.device.back()
                await asyncio.sleep(.45)
                await self.observe()
                continue
            if edits:
                raise InteractionBlocked('existing_editor_requires_attention')
            if any(a.get('content-desc')=='我' and a['box'][1] > max(r['box'][3] for r in self.rows)*.8 for a in self.rows):
                break
            if any(x in labels(self.rows) for x in ('预览','下一步','发笔记','存草稿')):
                raise InteractionBlocked('existing_editor_requires_attention')
            await self.device.back()
            await asyncio.sleep(.7)
            await self.observe()
        else:
            raise InteractionBlocked('home_navigation_failed')
        try:
            await self.click('我',desc=True)
        except PublicationBlocked as exc:
            raise InteractionBlocked('home_navigation_failed') from exc
        if (not any('小红书号' in label for label in labels(self.rows)) and
                any(row['content-desc']=='我' and row.get('selected')=='true' for row in self.rows)):
            await self.device.swipe(520,590,520,1850,500)
            await asyncio.sleep(.4)
            await self.observe()
        if account['account_name'] not in labels(self.rows) or not any(
            re.sub(r'\s','',x)=='小红书号：'+account['account_id'] for x in labels(self.rows)):
            raise InteractionBlocked('interaction_account_mismatch')
    def bind_inbox(self, store, session, account):
        from .unread import InboxWindow
        self.window = InboxWindow(store, session, account)

    async def inbox(self,account,*,history=False):
        await self.home(account)
        bottom=max(a['box'][3] for a in self.rows)
        buttons=[a for a in self.rows if re.fullmatch(r'消息(?:[，,](?:\d+\+?条)?未读)?',a.get('content-desc',''))
                 and a['box'][1]>bottom*.8]
        if len(buttons)!=1:raise InteractionBlocked('message_entry_ambiguous')
        await self.tap(buttons[0])
        if self.window is not None:
            from .unread import comment_badge
            badge = comment_badge(self.root)
            from xhs_actions.direct_messages import DirectMessageStore, record_visible_unread
            dm_store = DirectMessageStore()
            self.dm_candidates_observed = bool(record_visible_unread(self.root, self.window.session, account, store=dm_store))
            self.visible_dm_unread = len(dm_store.interactive_pending(self.window.session))
            window = self.window.before_entry(badge)
            if not history and not window:
                self.badge_report = ('badge_clear' if badge is not None else 'badge_unavailable', [])
                return
            if badge is None and not window:
                raise InteractionBlocked('comment_badge_unavailable')
        await self.click('评论和@')
        if '收到的评论和@' not in labels(self.rows):raise InteractionBlocked('interaction_inbox_unrecognized')
        if self.window is not None and window:
            self.window.entered(window)
            self.badge_report = await self.collect_window(window)
            if history:
                # A collection pass may have scrolled. Re-enter at the top before locating.
                await self.device.back()
                await asyncio.sleep(.4)
                await self.observe()
                next_window = self.window.before_entry(comment_badge(self.root))
                await self.click('评论和@')
                if next_window:
                    self.window.entered(next_window)
                if '收到的评论和@' not in labels(self.rows):
                    raise InteractionBlocked('interaction_inbox_unrecognized')
        if history and '查看历史消息' in labels(self.rows):await self.click('查看历史消息')

    async def collect_window(self, window):
        stored = self.window.store.known_sources(self.window.session, self.window.account['account_id'])
        frontier = stored - set(window['refs'])
        collected = []
        previous = None
        # Once the app clears a badge, some builds place the captured rows behind
        # “查看历史消息”. Only extend a resumed window through a saved overlap.
        resume_refs = set(window['refs'])
        history_opened = False
        need_anchor = False
        cutoff = datetime.fromisoformat(self.since_override or config()['since'])
        # On the next visit, cross already recorded pages to reach new material.
        for page in range(1, window['pages'] + 7):
            parsed, ambiguous = inbox_rows(self.root, datetime.now(ZONE))
            page_key = tuple(item['source_ref'] for item in parsed)
            history_edge = '查看历史消息' in labels(self.rows)
            if not parsed and history_edge and resume_refs and not history_opened:
                await self.click('查看历史消息')
                history_opened = need_anchor = True
                continue
            if not parsed or page_key == previous:
                break
            previous = page_key
            saved = set(window['refs'])
            found = []
            boundary = False
            uncertain = False
            for item in parsed:
                ref = item['source_ref']
                if need_anchor:
                    if ref not in resume_refs:
                        continue
                    need_anchor = False
                if not item['_supported'] or ref in ambiguous:
                    uncertain = True
                    break
                if ref in frontier or item['_lower'] < cutoff:
                    boundary = True
                    break
                if ref in saved:
                    continue
                if len(saved) >= window['expected'] and not window['lower_bound']:
                    break
                found.append({k:v for k,v in item.items() if not k.startswith('_')})
                saved.add(ref)
            self.window.record_page(window, found, page)
            collected.extend(found)
            at_end = any(label in labels(self.rows) for label in
                         ('查看历史消息', '没有更多了', '没有更多消息了', '- 到底了 -'))
            if (not uncertain and len(saved) >= window['expected'] and
                    (not window['lower_bound'] or boundary or at_end)):
                self.window.finish()
                return 'new_notifications', collected
            if not uncertain and not boundary and history_edge and resume_refs and not history_opened:
                await self.click('查看历史消息')
                history_opened = need_anchor = True
                previous = None
                continue
            if uncertain or boundary or at_end:
                break
            await self.device.swipe(540,1800,540,650)
            await self.observe()
        return 'unread_gap', collected
    async def scan(self,account,*,known_sources=(),scope='new'):
        value=self.since_override or config().get('since')
        if not value:raise InteractionBlocked('inbox_device_validation_required')
        cutoff=datetime.fromisoformat(value)
        if cutoff.tzinfo is None:raise InteractionBlocked('interaction_cutoff_unavailable')
        if scope not in {'new','history'}:raise InteractionBlocked('invalid_inbox_scope')
        await self.inbox(account,history=scope=='history')
        if scope == 'new' and self.badge_report is not None:
            coverage, items = self.badge_report
            return {'account':account, 'account_verified':True, 'observed_at':now(),
                    'coverage':coverage, 'visible_dm_unread': self.visible_dm_unread, 'items':items}
        known=set(known_sources)
        items={};unresolved=set();coverage='bounded_recent_window'
        for page in range(5):
            parsed,ambiguous=inbox_rows(self.root,datetime.now(ZONE));unresolved.update(ambiguous)
            older=False
            for item in parsed:
                if item['_upper'] is not None and item['_upper']<cutoff:older=True;continue
                if not item['_supported']:
                    unresolved.add(item['source_ref']);continue
                if item['_lower']<cutoff<=item['_upper'] and item['_lower'].date()==cutoff.date():
                    unresolved.add(item['source_ref']);continue
                if item['source_ref'] not in ambiguous and (scope=='history' or item['source_ref'] not in known):
                    items[item['source_ref']]={k:v for k,v in item.items() if not k.startswith('_')}
            # Stop at the app's explicit old-message boundary, or a complete
            # already-observed page. A mixed/new or ambiguous page is not a frontier.
            if scope=='new' and ('查看历史消息' in labels(self.rows) or
                    (parsed and all(x['_supported'] and x['source_ref'] in known for x in parsed))):
                coverage='new_notifications' if not unresolved else 'partial_identity'
                break
            if older or any(x in labels(self.rows) for x in ('没有更多了','没有更多消息了','- 到底了 -')):
                coverage='since_cutoff' if not unresolved else 'partial_identity';break
            if not parsed:
                if any(x in labels(self.rows) for x in ('暂时没有评论和@','暂时没有收到评论和@','还没有消息')):
                    coverage='since_cutoff'
                break
            await self.device.swipe(540,1800,540,650);await self.observe()
        return {'account':account,'account_verified':True,'observed_at':now(),'coverage':coverage,
                'unresolved_count':len(unresolved),'items':[x for key,x in items.items() if key not in unresolved]}
    async def admitted_dm_count(self, session, account):
        """Resolve fresh badges privately, after releasing the comment inbox lease."""
        if self.dm_candidates_observed:
            from xhs_actions.direct_messages import DirectMessagesPhone, DirectMessageStore, resolve_pending_identities
            store = DirectMessageStore()
            phone = DirectMessagesPhone(self.device)
            with await self.device.lease_async('xhs_dm_identity'):
                await resolve_pending_identities(session, phone=phone, account=account, store=store)
            self.visible_dm_unread = len(store.interactive_pending(session))
        return self.visible_dm_unread

    async def locate(self,item):
        await self.inbox(item['account'],history=True)
        visited = set()
        while True:
            parsed,ambiguous=inbox_rows(self.root,datetime.now(ZONE))
            found=[x for x in parsed if x['source_ref']==item['source_ref']]
            if item['source_ref'] in ambiguous:raise InteractionBlocked('notification_identity_ambiguous')
            if len(found)==1:
                await self.tap(as_row(found[0]['_body']))
                return
            current = tuple(row['source_ref'] for row in parsed)
            if not current or current in visited or any(label in labels(self.rows) for label in ('- 到底了 -','没有更多了','没有更多消息了')):
                break
            visited.add(current)
            await self.device.swipe(540,1800,540,650);await self.observe()
        raise InteractionBlocked('notification_source_not_visible')
    def note_material(self):
        from .post_navigation import note_material
        return note_material(self.root,self.rows)
    async def open(self,item):
        if item['incoming'].get('kind')=='post':
            from .post_navigation import open_post
            return await open_post(self,item)
        await self.locate(item)
        snapshot_comments=comment_rows(self.root)
        found=[x for x in snapshot_comments if x['actor_name']==item['incoming']['actor_name']
               and matches_notification_comment(x['text'], item['incoming'], item['account']['account_name'])]
        if len(found)!=1:raise InteractionBlocked('original_comment_not_unique')
        comment=found[0]['text'];thread=[{'actor_name':r['actor_name'],'text':r['text']} for r in snapshot_comments]
        author_nodes=[n for n in found[0]['row'].iter('node') if n.get('class')=='android.widget.TextView'
                      and n.get('clickable')=='true' and text(n).strip()==item['incoming']['actor_name']
                      and box(n)[1]<box(found[0]['node'])[1]]
        if len(author_nodes)!=1:raise InteractionBlocked('comment_author_control_ambiguous')
        await self.tap(as_row(author_nodes[0]))
        ids=[m[1] for value in labels(self.rows) if (m:=re.fullmatch(r'小红书号[：:]\s*([A-Za-z0-9_-]+)',value))]
        if len(set(ids))!=1 or item['incoming']['actor_name'] not in labels(self.rows):
            raise InteractionBlocked('comment_author_account_unverified')
        actor_account_id=ids[0]
        await self.device.back();await self.observe()
        from .post_navigation import seek_note_material
        note=await seek_note_material(self)
        target={**note,'source_ref':item['source_ref'],'source_verified':True,
                'identity_basis':'visible_note_and_exact_comment','actor_name':item['incoming']['actor_name'],
                'comment_text':comment,'thread':thread,'comment_scope':'native_visible_comments_only',
                'actor_account_id':actor_account_id,'actor_identity_basis':'native_profile_account_id',
                'actor_relationship':'human_verified' if config().get('owner_account_id')==actor_account_id else 'external_account'}
        target['identity']=digest(['xhs-native-comment-v1',item['account']['account_id'],item['source_ref'],
                                   note['note_author'],note['note_title'],actor_account_id,target['actor_name'],comment])
        self.last_target=target
        return target

    async def capture_images(self, target):
        """Called only by an explicit material read, never reply preparation."""
        from wander_manager.xhs_gallery import collect, position
        frames=[]
        # Returning from a comment can leave the image partly scrolled away.
        # A fixed downward scroll brings the original gallery back into view.
        for _ in range(3):
            galleries=[n for n in self.root.iter('node') if re.search(
                r'图片,第\d+张,共\d+张', n.get('content-desc',''))]
            if len(galleries)==1 and box(galleries[0])[1]>=250:break
            await self.device.swipe(540,700,540,1800);await self.observe()
        pos=position(self.root)
        if not pos or pos[1]!=target.get('image_count'):
            return {'status':'gallery_unavailable','frames':[], 'capture_complete':False}
        # XHS can remember the last viewed page when reopening the same note.
        for _ in range(min(pos[0]-1,12)):
            if not await self.device.swipe(180,900,900,900):break
            await asyncio.sleep(.35);await self.observe()
            previous=position(self.root)
            if previous!=(pos[0]-1,pos[1]):break
            pos=previous

        async def refresh():
            await self.observe()
            return self.root

        async def advance():
            if not await self.device.swipe(900,900,180,900):
                raise InteractionBlocked('gallery_swipe_failed')
            await asyncio.sleep(.35)

        async def capture(root,label):
            current=position(root)
            if not current:return
            # Header author stays visible even when a tall image hides the title.
            names=[a.get('text') for a in self.rows if a.get('class')=='android.widget.TextView'
                   and 100<=a['box'][1]<250 and 200<=a['box'][0]<650 and a.get('text')]
            if names!=[target['note_author']]:
                raise InteractionBlocked('gallery_author_changed')
            visible_note=self.note_material()
            if visible_note and any(visible_note[k]!=target[k] for k in ('note_title','note_body','note_author')):
                raise InteractionBlocked('gallery_note_changed')
            gallery=[n for n in root.iter('node') if re.search(r'图片,第\d+张,共\d+张',n.get('content-desc',''))]
            if len(gallery)!=1:raise InteractionBlocked('gallery_ambiguous')
            bounds=box(gallery[0])
            pixels=await self.device.screenshot()
            if not pixels:raise InteractionBlocked('gallery_screenshot_failed')
            # Send only the visible gallery, improving small text resolution and
            # keeping unrelated status/notification chrome out of the request.
            from io import BytesIO
            from PIL import Image
            try:
                source=Image.open(BytesIO(pixels))
                crop=(max(0,bounds[0]),max(250,bounds[1]),
                      min(source.width,bounds[2]),min(source.height-130,bounds[3]))
                if crop[2]<=crop[0] or crop[3]<=crop[1]:raise ValueError()
                buffer=BytesIO();source.crop(crop).save(buffer,format='PNG')
                pixels=buffer.getvalue()
            except Exception:
                raise InteractionBlocked('gallery_crop_failed') from None
            frames.append({'page':current[0],'total':current[1],'pixels':pixels,
                'captured_at':now(),'visible_bounds':list(crop),
                'screen_scope':'visible_gallery_crop;gallery_may_be_clipped;no_zoom_or_vertical_image_pan'})

        _,complete=await collect(self.root,get_position=position,capture=capture,
                                 advance=advance,refresh=refresh)
        return {'status':'captured','frames':frames,'capture_complete':complete}

    async def target_comment(self,item):
        target=await self.open({'account':item['account'],'source_ref':item['target']['source_ref'],
                               'incoming':{'actor_name':item['target']['actor_name'],
                                           'notification_text':item['target']['comment_text']}})
        from .service import same_target
        if not same_target(target,item['target']):raise InteractionBlocked('reply_target_changed')
        for _ in range(4):
            matches=[r for r in comment_rows(self.root) if r['actor_name']==target['actor_name']
                     and r['text']==target['comment_text']]
            if len(matches)==1:return target,matches[0]
            if len(matches)>1:raise InteractionBlocked('original_comment_not_unique')
            buttons=[a for a in self.rows if re.fullmatch(r'评论 \d+',a.get('content-desc',''))]
            if len(buttons)==1:await self.tap(buttons[0])
            else:await self.device.swipe(540,1800,540,650);await self.observe()
        raise InteractionBlocked('original_comment_not_visible')
    async def prepare_reply(self,item):
        if item['target'].get('target_kind')=='post':
            from .post_navigation import prepare_post
            return await prepare_post(self,item)
        target,comment=await self.target_comment(item)
        if any(r['actor_name']==item['account']['account_name'] and reply_text_matches(r['text'],item['text'])
               for r in comment_rows(self.root)):
            raise InteractionBlocked('matching_reply_already_visible')
        await self.tap(as_row(comment['node']))
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        expected='回复 @'+target['actor_name']+'：'
        if len(edits)!=1 or edits[0].get('text')!=expected:raise InteractionBlocked('reply_recipient_unverified')
        await self.enter(edits[0],item['text'])
        if len([a for a in self.rows if a.get('text')=='发送' and a.get('clickable')=='true'])!=1:
            raise InteractionBlocked('reply_submit_control_ambiguous')
        return {'target':target,'account':item['account'],'text_verified':True,
                'text_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),'existing_reply':False}
    async def leave_reply_preview(self,item):
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        if len(edits)!=1 or edits[0].get('text')!=item['text']:raise InteractionBlocked('reply_preview_identity_changed')
        if not await self.device.clear_text(edits[0]['box']):raise InteractionBlocked('reply_preview_clear_failed')
        for _ in range(3):
            await self.device.back();await self.observe()
            if not any(a.get('class')=='android.widget.EditText' for a in self.rows):return
        raise InteractionBlocked('reply_preview_cleanup_incomplete')
    async def submit_reply(self,item):
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        if len(edits)!=1 or edits[0].get('text')!=item['text']:raise InteractionBlocked('reply_submit_text_changed')
        await self.click('发送')
    async def verify_reply(self,item):
        if item['target'].get('target_kind')=='post':
            from .post_navigation import verify_post
            return await verify_post(self,item)
        await self.observe()
        if any(a.get('class')=='android.widget.EditText' for a in self.rows):return None
        _,parent=await self.target_comment(item)
        rows=comment_rows(self.root)
        matches=[r for r in rows if r['actor_name']==item['account']['account_name']
                 and reply_text_matches(r['text'],item['text'])]
        if len(matches)!=1:return None
        if not in_reply_thread(self.root,parent,matches[0]):return None
        author_nodes=[n for n in matches[0]['row'].iter('node') if n.get('class')=='android.widget.TextView'
                      and n.get('clickable')=='true' and text(n).strip()==item['account']['account_name']
                      and box(n)[1]<box(matches[0]['node'])[1]]
        if len(author_nodes)!=1:return None
        await self.tap(as_row(author_nodes[0]))
        expected_id='小红书号：'+item['account']['account_id']
        verified=expected_id in [re.sub(r'\s','',x) for x in labels(self.rows)]
        await self.device.back();await self.observe()
        if not verified:return None
        return {'verified':True,'account_id':item['account']['account_id'],
                'target_identity':item['target']['identity'],'text_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),
                'basis':'native_original_comment_thread_exact_reply_and_author_profile'}
