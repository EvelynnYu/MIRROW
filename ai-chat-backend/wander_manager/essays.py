"""Essay creation and read-only books: successful runtime nodes own the text."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

ARCHIVE_PATH = Path(__file__).resolve().parents[1] / 'events' / 'wander_runtime.db'
_WORKS = """ FROM wander_nodes n JOIN wander_activities a ON a.activity_id=n.activity_id
    WHERE a.activity_type='keyword_expansion' AND n.execution_status='succeeded'
    AND json_extract(n.source_payload,'$.essay_version')=1"""
_DATE = 'COALESCE(n.completed_at,n.started_at,n.created_at)'
SEED_PROMPT = '让一个词自然浮现，顺手记下此刻想写什么。只输出 JSON：{"keyword":"词","idea":"一句想法"}。'
WRITING_PROMPT = '''随手写一篇此刻想写的随笔，心得、感想、短篇小说或幻想都可以，题材与文体自由，500 字以内为宜。浮现的词只是一点灵感，也可以写别的。
只输出 JSON，title 写题目，expansion 写完整正文。写完若有想画的一幕，可在 illustration 里用一句话描述画面和入画的人物。
'''
REVIEW_INSTRUCTION = ('\n当前材料是你刚写的作品，故事人物和情节属于创作；写作感受另行简记。'
    '收尾 summary 简记写了哪篇，正文保存在书柜。想把一篇原文卡片分享给对话伙伴时，'
    '在 closeout 填 essay_share_node_id 为该篇 node_id，同时 share=true；不分享则留空。')


def _attach_continuations(db, items):
    """Derive direct children from their stored parent IDs, including works outside this page."""
    by_id = {item['id']: item for item in items}
    if not by_id:
        return
    parent = "json_extract(n.source_payload,'$.continued_from.id')"
    rows = db.execute(f"""SELECT {parent}, n.node_id, json_extract(n.source_payload,'$.title'), {_DATE}
                          {_WORKS} AND {parent} IS NOT NULL AND {parent} != n.node_id
                          AND json_extract(n.source_payload,'$.expansion') != ''
                          ORDER BY {_DATE},n.node_id""")
    for parent_id, node_id, title, date in rows:
        if parent_id in by_id and title:
            by_id[parent_id].setdefault('continuations', []).append(
                {'id': node_id, 'title': title, 'created_at': date})


def related_works(item):
    if parent := item.get('continued_from'):
        yield '接续', parent
    for child in item.get('continuations', ()):
        yield '后续作品', child


def _writing_shelf(path):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        items = [dict(row) for row in db.execute(f"""SELECT n.node_id AS id, {_DATE} AS date,
            json_extract(n.source_payload,'$.title') AS title,
            json_extract(n.source_payload,'$.continued_from') AS continued_from,
            json_extract(n.source_payload,'$.expansion') AS body,
            json_extract(n.source_payload,'$.keyword') AS keyword {_WORKS}
            ORDER BY {_DATE} DESC,n.node_id""") if row['title'] and row['body']]
        for row in items:
            if row['continued_from']:
                row['continued_from'] = json.loads(row['continued_from'])
        _attach_continuations(db, items)
        return items


def _shelf_label(row, numbers=None):
    label = f"{row['date'][:10]}《{row['title']}》"
    for kind, ref in related_works(row):
        label += f"（{kind} {ref['created_at'][:10]}《{ref['title']}》）"
        if numbers and ref['id'] in numbers:
            label += f"[{numbers[ref['id']]}]"
    return label


def assign_shelf_title(conn, node):
    """Reserve an independent work's shelf title in the same transaction as its first save."""
    payload = node.source_payload
    title = payload['title']
    titles = "SELECT json_extract(n.source_payload,'$.title'), json_extract(n.source_payload,'$.authored_title')"
    existing = conn.execute(titles + _WORKS + ' AND n.node_id=?', (node.node_id,)).fetchone()
    if existing:
        payload['title'] = existing[0]
        if existing[1]:
            payload['authored_title'] = existing[1]
    else:
        used = {row[0] for row in conn.execute("SELECT json_extract(n.source_payload,'$.title')" + _WORKS)}
        if title in used:
            stem, _, suffix = title.rpartition('（')
            base = stem if stem in used and suffix.endswith('）') and suffix[:-1].isdigit() else title
            number = 2
            while f'{base}（{number}）' in used:
                number += 1
            payload['authored_title'] = title
            payload['title'] = f'{base}（{number}）'
    node.source_summary = f"写下随笔《{payload['title']}》"


@lru_cache(maxsize=4)
def _essay_search(path):
    from behavior_scheduler.bookshelf_search import BookshelfSearchService
    from memory_v2.semantic_profile import local_model
    try:
        profile, embedder = local_model()
    except FileNotFoundError:
        return BookshelfSearchService(str(path), cache_path=str(path.with_name('essay_search_vectors.npz')))
    version = sha256(profile.model_id.encode()).hexdigest()[:12]
    return BookshelfSearchService(str(path), cache_path=str(path.with_name(f'essay_search_{version}.npz')),
                                  embedder=embedder, query_instruction=profile.query_instruction)


async def _related(shelf, query, path):
    from behavior_scheduler.bookshelf_search import SearchDocument
    docs = [SearchDocument(id=row['id'], source='essays', date=row['date'],
                           text=f"{row['title']}\n{row['keyword']}\n{row['body']}") for row in shelf]
    service = _essay_search(path)
    # These matches are optional reading suggestions, not evidence for a factual answer.
    result = await service.search(query, sources=set(), extra_documents=docs, limit=3, min_score=40)
    return [] if result.get('used_weak_fallback') else result['hits']


async def generate(call, *, db_path=None):
    from . import node_retry
    from .flash_structured import parse_json_object
    from behavior_scheduler.bookshelf_search import focused_archive_excerpt
    path = Path(db_path) if db_path is not None else ARCHIVE_PATH
    shelf = await asyncio.to_thread(_writing_shelf, path)
    prompt = SEED_PROMPT
    if shelf:
        prompt += '\n最近写过：\n' + '\n'.join(_shelf_label(r) for r in shelf[:6])
    seed = await node_retry.choose(call, prompt, parse_json_object,
        lambda value: '' if value and isinstance(value.get('keyword'), str) and value['keyword'].strip()
        else '没读出这次想到的词，请填在 keyword 里。', '随笔起意') or {}
    keyword = seed.get('keyword')
    if not isinstance(keyword, str) or not keyword.strip():
        return None
    keyword = keyword.strip()
    idea = seed.get('idea') if isinstance(seed.get('idea'), str) else ''
    hits = await _related(shelf, (keyword + ' ' + idea).strip(), path) if shelf else []
    works = {r['id']: r for r in shelf}
    ids = dict.fromkeys(h['record_id'] for h in hits)
    for hit in hits:
        ids.update((ref['id'], None) for _, ref in related_works(works[hit['record_id']]) if ref['id'] in works)
    numbers = {node_id: str(i) for i, node_id in enumerate(ids, 1)}
    candidates = {number: works[node_id] for node_id, number in numbers.items()}
    prompt = WRITING_PROMPT
    if candidates:
        prompt += '\n书柜里相关的旧作：\n'
        for i, hit in enumerate(hits, 1):
            row = candidates[str(i)]
            excerpt = focused_archive_excerpt(hit['content'], hit['matched_terms'], limit=160)
            prompt += f"{i}. {_shelf_label(row, numbers)} · 节选：{excerpt}\n"
        prompt += '可以另写一篇，也可以接着某篇写，续篇用 continues 填原篇编号。'
    tail = (f'\n刚才想写的是：{idea}' if idea else '') + '\n这一次脑子里突然冒出来一个词：' + keyword
    read_option = '想先翻全文，可以只回 {"read":编号}。' if candidates else ''
    def draft_problem(data):
        if data and all(isinstance(data.get(key), str) and data[key].strip() for key in ('title', 'expansion')):
            return ''
        return '没读到完整的标题和正文，请分别填在 title 和 expansion 里。'

    data = await node_retry.choose(call, prompt + read_option + tail, parse_json_object,
        lambda data: '' if data and str(data.get('read')) in candidates else draft_problem(data), '随笔写作') or {}
    reread = candidates.get(str(data.get('read')))
    if reread and not data.get('expansion'):
        prompt += f"\n翻开的原篇：{_shelf_label(reread)}\n{reread['body']}\n〔原篇结束〕"
        data = await node_retry.choose(call, prompt + tail, parse_json_object, draft_problem, '随笔写作') or {}
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ('title', 'expansion')):
        return None
    work = {'essay_version': 1, 'keyword': keyword, 'title': data['title'].strip(),
            'expansion': data['expansion'].strip(), 'source_kind': 'creative_work'}
    parent = candidates.get(str(data.get('continues')))
    if parent:
        work['continued_from'] = {'id': parent['id'], 'title': parent['title'], 'created_at': parent['date']}
    picture = data.get('illustration')
    if isinstance(picture, str) and picture.strip():
        work['illustration_request'] = picture.strip()
    return work


def entry(node, activity_id=''):
    payload = node.get('source_payload') or {}
    if (payload.get('essay_version') != 1 or node.get('execution_status') != 'succeeded'
            or not payload.get('title') or not payload.get('expansion')):
        return None
    picture = (node.get('side_effect_refs') or {}).get('essay_illustration')
    return {'id': node['node_id'], 'activity_id': activity_id or node.get('activity_id', ''),
            'title': payload['title'], 'keyword': payload.get('keyword', ''),
            'created_at': node.get('completed_at') or node.get('started_at') or node.get('created_at', ''),
            'content': payload['expansion'], **({'illustration': picture} if picture else {}),
            **({'continued_from': payload['continued_from']} if payload.get('continued_from') else {})}


def index_text(items):
    return '\n'.join(f'写下随笔《{item["title"]}》。书柜索引：source=essays，event_id={item["id"]}；关键词：{item["keyword"]}。'
                     for item in items)


def card_text(card):
    picture = card.get('illustration') or {}
    caption = f'\n[随笔附图，创作时选的画面：{picture["scene"]}]' if picture.get('status') == 'ready' else ''
    return (f'[AI 分享的随笔作品｜{card.get("created_at", "")}｜《{card.get("title", "")}》]\n'
            f'{card.get("content", "")}{caption}\n[作品结束；故事情节不等于现实经历。书柜 source=essays，event_id={card.get("id", "")}]')


def shared_work_fact(call):
    if not isinstance(call, dict) or call.get('status') != 'success':
        return ''
    extra = call.get('extra_data')
    if not isinstance(extra, dict):
        return ''
    if call.get('tool') == 'essay_card':
        work = extra.get('essay_card')
        if isinstance(work, dict) and isinstance(work.get('content'), str) and work.get('title'):
            return card_text(work)
    if call.get('tool') == 'reading_illustration':
        picture = extra.get('reading_illustration')
        if isinstance(picture, dict) and all(isinstance(picture.get(key), str)
                                             for key in ('title', 'quote', 'scene')):
            from .reading_illustrations import fact
            return 'AI 分享了' + fact(picture)
    return ''


def read(*, event_id='', query='', date='', limit=30, offset=0, db_path=None):
    """No initialization, migrations or model calls on either read surface."""
    path = Path(db_path) if db_path else ARCHIVE_PATH
    if not path.is_file():
        return {'items': [], 'total': 0} if not event_id else None
    clauses = []
    params = []
    if event_id:
        clauses.append('n.node_id=?'); params.append(event_id)
    if date:
        clauses.append('substr(COALESCE(n.completed_at,n.started_at,n.created_at),1,10)=?'); params.append(date)
    if query:
        clauses.append("(instr(json_extract(n.source_payload,'$.title'),?)>0 OR instr(json_extract(n.source_payload,'$.expansion'),?)>0 OR instr(json_extract(n.source_payload,'$.keyword'),?)>0)")
        params.extend([query] * 3)
    base = _WORKS + ''.join(' AND ' + clause for clause in clauses)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        total = conn.execute('SELECT count(*)' + base, params).fetchone()[0]
        rows = conn.execute('SELECT n.*' + base + ' ORDER BY COALESCE(n.completed_at,n.started_at,n.created_at) DESC,n.node_id LIMIT ? OFFSET ?',
                            [*params, 1 if event_id else max(1, min(100, int(limit))), max(0, int(offset))]).fetchall()
        items = []
        for row in rows:
            node = dict(row); node['source_payload'] = json.loads(node['source_payload'])
            node['side_effect_refs'] = json.loads(node['side_effect_refs'] or '{}')
            item = entry(node)
            if item:
                if not event_id:
                    item.pop('content')
                    item.pop('illustration', None)
                items.append(item)
        _attach_continuations(conn, items)
    return (items[0] if items else None) if event_id else {'items': items, 'total': total}


def selected_card(store, run_id, *, field='essay_share_node_id', make_card=entry):
    from .runtime_models import DecisionPhase
    activities = {row['activity_id']: row for row in store.list_activities(run_id)}
    settled = set()
    for decision in reversed(store.list_decisions(run_id, DecisionPhase.SETTLEMENT)):
        activity_id = decision.get('activity_id')
        if activity_id in settled:
            continue
        settled.add(activity_id)
        activity = activities.get(decision.get('activity_id'))
        if not activity or not activity.get('share_decision'):
            continue
        node_id = ((decision.get('parsed_output') or {}).get('normalized') or {}).get(field)
        if node_id:
            for node in store.list_nodes(activity['activity_id']):
                if node['node_id'] == node_id:
                    card = make_card(node)
                    if card:
                        return card
    return None
