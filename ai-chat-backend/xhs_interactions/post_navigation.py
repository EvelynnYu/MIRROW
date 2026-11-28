"""Native identity and navigation shared by browsing and top-level comments."""
import hashlib
import re
from urllib.parse import quote

from .service import InteractionBlocked
from .post_identity import matches_locator, target_identity


def note_material(root, rows):
    from .native import box, text
    candidates=[]
    for n in root.iter('node'):
        if n.get('class')!='android.widget.LinearLayout' or len(n)!=2:continue
        a,b=list(n);ba,bb=box(a),box(b)
        if (a.get('class')==b.get('class')=='android.widget.TextView' and text(a).strip() and text(b).strip()
                and ba[0]==bb[0] and ba[2]==bb[2] and ba[0]<100 and ba[2]>900 and ba[1]>250):
            candidates.append((text(a),text(b)))
    names=[a.get('text') for a in rows if a.get('class')=='android.widget.TextView'
           and 100<=a['box'][1]<250 and 200<=a['box'][0]<650 and a.get('text')]
    if len(candidates)!=1 or len(names)!=1:return None
    images=next((int(m[1]) for a in rows if (m:=re.search(r'^图片,第\d+张,共(\d+)张',a.get('content-desc','')))),0)
    return {'note_title':candidates[0][0],'note_body':candidates[0][1],'note_author':names[0],
            'note_body_scope':'native_accessibility_text_only;image_contents_not_read','image_count':images}


async def seek_note_material(native):
    """Return from a comment to the note header, including long expanded posts.

    A clipped body can expose all its text with identical bounds across several
    screens. Repeated XML therefore does not prove scrolling has stopped.
    Keep the read bounded, and require the existing title/body/author evidence.
    """
    for attempt in range(21):
        note = native.note_material()
        if note:
            return note
        if attempt == 20:
            break
        if not await native.device.swipe(540,700,540,1800):
            raise InteractionBlocked('note_navigation_failed')
        await native.observe()
    raise InteractionBlocked('original_note_material_unavailable')


async def capture_locator(source, serial, snapshot):
    """Read author profile while the browsing lease still owns this exact note."""
    from .native import as_row, box
    from wander_manager.xhs_profile_navigation import identity
    if not snapshot.available:return None
    note=note_material(snapshot.root,[as_row(n) for n in snapshot.root.iter('node')])
    if not note:return None
    authors=[n for n in snapshot.root.iter('node') if n.get('text')==note['note_author']
             and 100<=box(n)[1]<250 and 200<=box(n)[0]<650]
    if len(authors)!=1:return None
    x1,y1,x2,y2=box(authors[0])
    result=await source._run_device(serial,'shell','input','tap',str((x1+x2)//2),str((y1+y2)//2))
    if result is None or result.returncode:return None
    profile_id=''
    try:
        await source._sleeper(source.post_wait_seconds)
        profile=await source._dump_ui(serial)
        if profile.available and not source._guard_snapshot(profile):
            profile_id=identity(profile)
    finally:
        await source._run_device(serial,'shell','input','keyevent','KEYCODE_BACK')
        await source._sleeper(source.post_wait_seconds)
    after=await source._dump_ui(serial)
    if not after.available or source._guard_snapshot(after):return None
    current=note_material(after.root,[as_row(n) for n in after.root.iter('node')])
    if not profile_id or current!=note:return None
    return {'version':1,'profile_id':profile_id,**note}


async def open_post(native, item):
    from .native import as_row, box, labels
    from .store import digest
    from wander_manager.xhs_profile_navigation import normalized
    locator=item['incoming']['post_locator']
    await native.home(item['account'])
    uri='xhsdiscover://search/result?keyword='+quote(locator['note_author'],safe='')
    if not await native.device.launch('com.xingin.xhs',uri=uri):raise InteractionBlocked('post_search_unavailable')
    await native.observe();await native.click('用户')
    authors=[n for n in native.root.iter('node') if normalized(n.get('text',''))==normalized(locator['note_author']) and box(n)[1]>380]
    if len(authors)!=1:raise InteractionBlocked('post_author_ambiguous')
    await native.tap(as_row(authors[0]))
    ids=[m[1] for t in labels(native.rows) if (m:=re.fullmatch(r'小红书号[：:]\s*([A-Za-z0-9_-]+)',t))]
    if ids!=[locator['profile_id']]:raise InteractionBlocked('post_author_changed')
    from wander_manager.xhs_profile_navigation import profile_cards
    from types import SimpleNamespace
    for _ in range(30):
        cards=profile_cards(SimpleNamespace(root=native.root,available=True),locator['note_author'])
        matches=[c for c in cards if c['title']==locator['note_title']]
        if len(matches)>1:raise InteractionBlocked('post_title_ambiguous')
        if matches:
            await native.tap({'box':matches[0]['bounds']});break
        if any(x in labels(native.rows) for x in ('没有更多了','没有更多笔记','到底了')):
            raise InteractionBlocked('post_not_visible')
        await native.device.swipe(540,1800,540,650);await native.observe()
    else:raise InteractionBlocked('post_navigation_budget')
    note=native.note_material()
    for _ in range(5):
        if note:break
        await native.device.swipe(540,1800,540,1200,700);await native.observe();note=native.note_material()
    if not matches_locator(note,locator):
        raise InteractionBlocked('post_material_changed')
    target={**note,'target_kind':'post','profile_id':locator['profile_id'],
            'source_ref':item['source_ref'],'source_verified':True,
            'identity_basis':'verified_profile_title_body','actor_name':note['note_author'],
            'comment_text':'','thread':[]}
    target['identity']=target_identity({**note,'profile_id':locator['profile_id']})
    return target


def post_item(item):
    return {'account':item['account'],'source_ref':item['target']['source_ref'],
            'incoming':{'kind':'post','post_locator':item['target']}}


async def prepare_post(native,item):
    from .native import comment_rows, reply_text_matches
    target=await open_post(native,post_item(item))
    if any(r['actor_name']==item['account']['account_name'] and reply_text_matches(r['text'],item['text'])
           for r in comment_rows(native.root)):
        raise InteractionBlocked('matching_comment_already_visible')
    entries=[a for a in native.rows if a.get('text') in {'说点什么...','说点什么…','说点什么吧...','说点什么吧…'}
             or a.get('content-desc')=='评论框']
    if len(entries)!=1:raise InteractionBlocked('post_comment_entry_ambiguous')
    await native.tap(entries[0])
    edits=[a for a in native.rows if a.get('class')=='android.widget.EditText']
    if len(edits)!=1 or not any(a.get('text')=='发送' and a.get('enabled')=='false' for a in native.rows):
        raise InteractionBlocked('post_comment_editor_not_empty')
    # XHS rotates the hint text. Android's real hint flag, not its wording,
    # proves an empty editor; an existing draft is never overwritten.
    if not await native.device.set_text(edits[0]['box'],item['text'],require_empty=True):
        raise InteractionBlocked('post_empty_editor_or_input_unverified')
    await native.observe()
    if not any(a.get('class')=='android.widget.EditText' and a.get('text')==item['text'] for a in native.rows):
        raise InteractionBlocked('post_input_readback_mismatch')
    if len([a for a in native.rows if a.get('text')=='发送' and a.get('clickable')=='true'])!=1:
        raise InteractionBlocked('reply_submit_control_ambiguous')
    return {'target':target,'account':item['account'],'text_verified':True,'existing_reply':False,
            'text_sha256':hashlib.sha256(item['text'].encode()).hexdigest()}


async def verify_post(native,item):
    from .native import comment_rows,reply_text_matches,box,as_row,labels
    from .service import same_target
    await native.observe()
    if any(a.get('class')=='android.widget.EditText' for a in native.rows):return None
    target=await open_post(native,post_item(item))
    if not same_target(target,item['target']):return None
    for _ in range(5):
        rows=comment_rows(native.root)
        matches=[r for r in rows if r['actor_name']==item['account']['account_name']
                 and reply_text_matches(r['text'],item['text']) and box(r['row'])[0]==0]
        if len(matches)>1:return None
        if matches:
            authors=[n for n in matches[0]['row'].iter('node') if n.get('text')==item['account']['account_name']
                     and n.get('clickable')=='true' and box(n)[1]<box(matches[0]['node'])[1]]
            if len(authors)!=1:return None
            await native.tap(as_row(authors[0]))
            verified='小红书号：'+item['account']['account_id'] in [re.sub(r'\s','',t) for t in labels(native.rows)]
            await native.device.back();await native.observe()
            if verified:return {'verified':True,'account_id':item['account']['account_id'],
                'target_identity':target['identity'],'text_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),
                'basis':'native_verified_note_top_level_comment_and_author_profile'}
            return None
        await native.device.swipe(540,1800,540,650);await native.observe()
    return None
