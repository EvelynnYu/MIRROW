"""Observed native text-card publication with exact text checks and a submit claim."""
import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from .device import PublishDevice
from .store import receipt

class PublicationBlocked(RuntimeError):
    pass

def nodes(xml):
    try:
        result=[]
        for n in ET.fromstring(xml).iter('node'):
            a=dict(n.attrib)
            b=list(map(int,re.findall(r'\d+',a.get('bounds',''))))
            if len(b)==4 and b[2]>b[0] and b[3]>b[1]:
                a['box']=b;result.append(a)
        return result
    except (ET.ParseError,TypeError):
        return []

def labels(rows):
    return [a.get(k,'') for a in rows for k in ('text','content-desc') if a.get(k)]

class NativePublisher:
    def __init__(self,device=None):
        self.device=device or PublishDevice(command_timeout_seconds=30)
        self.rows=[]
        self.timings=[]
    async def observe(self):
        start=time.monotonic()
        self.rows=nodes(await self.device.ui_dump())
        self.timings.append({'step':'observe','seconds':round(time.monotonic()-start,3)})
        if not self.rows: raise PublicationBlocked('ui_unavailable')
        from xhs_interactions.unread import observe_labels
        observe_labels(row.get('content-desc','') for row in self.rows if row.get('package')=='com.xingin.xhs')
        ls=labels(self.rows)
        if any(x in ls for x in ('拖动滑块完成拼图','请完成安全验证','请输入验证码','手机号登录')):
            raise PublicationBlocked('authentication_or_verification_required')
        return self.rows
    def find(self,label,*,desc=False):
        key='content-desc' if desc else 'text'
        candidates=[a for a in self.rows if a.get(key)==label]
        if not candidates: raise PublicationBlocked('control_missing')
        # Prefer enabled/clickable accessible controls; repeated labels must
        # represent the same expected navigation affordance.
        return next((a for a in candidates if a.get('clickable')=='true'),candidates[0])
    async def tap(self,node):
        x1,y1,x2,y2=node['box']
        if not await self.device.tap((x1+x2)//2,(y1+y2)//2):
            raise PublicationBlocked('tap_failed')
        await asyncio.sleep(.35)
        await self.observe()
    async def click(self,label,*,desc=False):
        try:
            target=self.find(label,desc=desc)
        except PublicationBlocked:
            if label not in ('我','用户','消息','评论和@','查看历史消息','发私信','私信好友','搜索'):
                raise
            if not await self.recover_read_navigation():
                raise
            target=self.find(label,desc=desc)
        await self.tap(target)
    async def recover_read_navigation(self, *, allowed=('返回', '关闭', '取消', '以后再说', '知道了', '我知道了')):
        """Dismiss one observed, ordinary XHS navigation obstruction.

        This is deliberately separate from ``click``.  Publishing, messaging,
        collection and comment controls must always be found by their exact
        native contracts.  The visual chooser can only select among the safe
        controls supplied by the caller.
        """
        from xhs_actions.navigation_vision import select_read_navigation, candidates, is_blocked
        if (getattr(self,'_navigation_vision_used',False) or not self.rows or
                any(row.get('package')!='com.xingin.xhs' for row in self.rows) or
                is_blocked(self.rows) or not candidates(self.rows,allowed=allowed)):
            return False
        self._navigation_vision_used=True
        try:
            frame = await self.device.screenshot()
        except Exception:
            return False
        candidate = await select_read_navigation(self.rows, frame, allowed=allowed)
        if candidate is None:
            return False
        try:
            await self.observe()
            if is_blocked(self.rows) or candidate not in candidates(self.rows, allowed=allowed):
                return False
            await self.tap({'box':candidate['bounds']})
        except Exception:
            return False
        return True
    async def enter(self,node,value):
        if not await self.device.set_text(node['box'],value):
            raise PublicationBlocked('unicode_input_failed')
        await self.observe()
        if not any(a.get('class')=='android.widget.EditText' and a.get('text')==value for a in self.rows):
            raise PublicationBlocked('input_readback_mismatch')
    def own_note_cards(self,title,account):
        # Native profile cards expose one combined accessibility label rather
        # than a standalone title on the observed Android 13 XHS build.
        prefix='笔记,'+title+',来自'+account['account_name']+','
        found={}
        for row in self.rows:
            if row.get('content-desc','').startswith(prefix):
                found[tuple(row['box'])]=row
        return list(found.values())
    async def home(self,account):
        if await self.device.get_state()!='device':
            raise PublicationBlocked('device_disconnected')
        b=await self.device.battery()
        if b.get('level') is None or (b['level']<20 and not b.get('charging')):
            raise PublicationBlocked('battery_unavailable_or_low')
        await self.device.wake()
        await self.device.launch('com.xingin.xhs',component='.index.v2.IndexActivityV2')
        await self.observe()
        for _ in range(3):
            if any(a.get('content-desc')=='我' for a in self.rows): break
            if (any(a.get('class')=='android.widget.EditText' for a in self.rows)
                    or any(x in labels(self.rows) for x in ('预览','下一步','发笔记','存草稿'))):
                raise PublicationBlocked('existing_editor_requires_attention')
            if not any(a.get('content-desc')=='返回' for a in self.rows):
                raise PublicationBlocked('home_navigation_unrecognized')
            await self.device.back();await self.observe()
        # An existing editor may contain someone else's draft. Never discard it.
        if any(a.get('class')=='android.widget.EditText' for a in self.rows):
            raise PublicationBlocked('existing_editor_requires_attention')
        await self.click('我',desc=True)
        texts=labels(self.rows)
        if not any(re.sub(r'\s','',x)=='小红书号：'+account['account_id'] for x in texts):
            raise PublicationBlocked('publishing_account_mismatch')
        if account['account_name'] not in texts:
            raise PublicationBlocked('publishing_account_name_changed')
    async def select_profile_notes(self):
        # The native profile remembers its tab, including when reopened via 我.
        tabs = [r for r in self.rows if r.get('content-desc') == '笔记']
        if '已选定笔记' in labels(self.rows):
            return
        if len(tabs) == 1:
            await self.tap(tabs[0])
            if '已选定笔记' in labels(self.rows):
                return
            raise PublicationBlocked('profile_notes_tab_unverified')
        if tabs or any(r.get('content-desc', '').startswith('已选定') for r in self.rows):
            raise PublicationBlocked('profile_notes_tab_unverified')
        # Earlier native layouts expose the account's note cards without tabs.

    async def prepare(self,record):
        d=record['draft']
        await self.home(record['account'])
        await self.select_profile_notes()
        if d['title'] in labels(self.rows) or self.own_note_cards(d['title'],record['account']):
            raise PublicationBlocked('same_title_already_visible')
        await self.click('发布',desc=True)  # Opens composer, never the submit button.
        await self.click('写文字')
        await self.click('写想法')
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        if len(edits)!=1: raise PublicationBlocked('cover_editor_ambiguous')
        await self.enter(edits[0],d['cover_text'])
        await self.click('下一步')
        if '申请获取您的相册内容位置信息' in labels(self.rows):
            await self.click('取消')
        # The native text-card editor precedes the title/body publishing form.
        if not any(a.get('class')=='android.widget.EditText' for a in self.rows):
            if not any(a.get('content-desc')==d['cover_text'] for a in self.rows):
                raise PublicationBlocked('rendered_cover_readback_mismatch')
            await self.click('下一步')
        if '申请获取您的相册内容位置信息' in labels(self.rows):
            await self.click('取消')
        await self.fill_form(d)
        return {'account_verified':True,'text_verified':True,'cover_text_verified':True,
                'media_kind':'native_text_card','timings':self.timings}
    async def fill_form(self,draft):
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        title=next((a for a in edits if '标题' in a.get('text','')),None)
        body=next((a for a in edits if a.get('text') in {'展开说说','添加正文','添加描述'}),None)
        if not title or not body: raise PublicationBlocked('publish_form_unrecognized')
        if '公开可见' not in labels(self.rows): raise PublicationBlocked('visibility_not_public')
        await self.enter(title,draft['title'])
        edits=[a for a in self.rows if a.get('class')=='android.widget.EditText']
        body=next((a for a in edits if a.get('text')!=draft['title']),None)
        if not body: raise PublicationBlocked('body_editor_missing')
        await self.enter(body,draft['body'])
        texts=labels(self.rows)
        if draft['title'] not in texts or draft['body'] not in texts:
            raise PublicationBlocked('final_text_readback_mismatch')
        self.find('发笔记')
    async def submit(self):
        # Caller MUST commit submitting before entering this function.
        await self.click('发笔记')
    async def leave_preview(self,record):
        # Only discard the editor that this invocation just constructed and checked.
        texts=labels(self.rows)
        if record['draft']['title'] not in texts or record['draft']['body'] not in texts:
            raise PublicationBlocked('preview_cleanup_identity_changed')
        for _ in range(10):
            if any(a.get('content-desc')=='我' for a in self.rows): return
            if '不保存返回' in labels(self.rows):
                await self.click('不保存返回',desc=True)
            else:
                await self.device.back();await self.observe()
            texts=labels(self.rows)
            known=any(x in texts for x in ('不保存返回','预览','写想法','发笔记','我'))
            if not known: raise PublicationBlocked('preview_cleanup_unrecognized')
        raise PublicationBlocked('preview_cleanup_incomplete')
    async def verify(self,record):
        # Only a new, matching note in the verified own profile establishes publication.
        await asyncio.sleep(2)
        await self.observe()
        for _ in range(3):
            if any(a.get('content-desc')=='我' for a in self.rows): break
            if any(a.get('class')=='android.widget.EditText' for a in self.rows):
                return None
            await self.device.back();await self.observe()
        await self.click('我',desc=True)
        if not any('小红书号：'+record['account']['account_id']==re.sub(r'\s','',x) for x in labels(self.rows)):
            return None
        await self.select_profile_notes()
        title=record['draft']['title']
        cards=self.own_note_cards(title,record['account'])
        if cards:
            if len(cards)!=1: return None
            await self.tap(cards[0])
        else:
            titles=[a for a in self.rows if a.get('text')==title]
            if len(titles)!=1: return None
            await self.tap(titles[0])
        texts=labels(self.rows)
        body = record['draft']['body']
        # The published detail TextView renders each blank paragraph separator
        # as newline-tab-newline. Compare this exact rendering only on readback.
        if title not in texts or not (body in texts or body.replace('\n\n', '\n\t\n') in texts):
            return None
        if record['account']['account_name'] not in texts: return None
        return {'verified':True,'basis':'own_profile_new_title_and_exact_detail',
            'content_sha256':hashlib.sha256((title+'\n'+record['draft']['body']).encode()).hexdigest(),
            'platform_note_id':'','timings':self.timings}

async def execute(store,record,*,preview=False,publisher=None):
    p=publisher or NativePublisher()
    task,sid=record['task_id'],record['session_id']
    if record['state'] in {'published','submitting','submitted_unverified','preparing'}:
        return receipt(record)  # Unknown work is never automatically replayed.
    if record['state'] not in {'draft','blocked','previewed'}:
        return receipt(record)
    with await p.device.lease_async('xhs_publication'):
        record=store.transition(task,sid,{record['state']},'preparing',error='')
        try:
            evidence=await p.prepare(record)
            record=store.transition(task,sid,{'preparing'},'previewed',receipt=evidence)
            if preview:
                await p.leave_preview(record)
                return receipt(record)
            record=store.transition(task,sid,{'previewed'},'submitting')
            await p.submit()
            record=store.transition(task,sid,{'submitting'},'submitted_unverified')
            verified=await p.verify(record)
            if verified:
                record=store.transition(task,sid,{'submitted_unverified'},'published',receipt=verified)
        except Exception as exc:
            current=store.get(task,sid)
            code=str(exc) if isinstance(exc,PublicationBlocked) else 'device_execution_error'
            state='submitted_unverified' if current['state'] in {'submitting','submitted_unverified'} else 'blocked'
            record=store.transition(task,sid,{current['state']},state,error=code)
    return receipt(record)
