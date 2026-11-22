"""Read-only game results from provider receipts, separate from AI's feelings."""
from __future__ import annotations

from typing import Any


def completed(payload: dict[str, Any]) -> bool:
    from .game_handler import _completion
    return payload.get('game_complete') is True or any(
        _completion(str(payload.get('game') or ''), value)
        for key in ('checkpoint', 'outcome')
        if isinstance(value := payload.get(key), dict))


def _endings(state):
    values = state.get('all_endings') or [state.get('ending')]
    result = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, str) and value.strip():
            item = {'title': value.strip(), 'narrative': ''}
        elif isinstance(value, dict):
            item = {key: value.get(key) if isinstance(value.get(key), str) else ''
                    for key in ('title', 'narrative')}
        else:
            continue
        if any(item.values()) and item not in result:
            result.append(item)
    return result


def result_fact(payload: dict[str, Any]) -> dict[str, Any] | None:
    game = payload.get('game')
    checkpoint = payload.get('checkpoint') or {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    base = {key: payload.get(key) for key in ('game', 'game_name', 'slot', 'playthrough_key')}
    complete = completed(payload)
    if game in {'mbti', 'sins_virtues'}:
        if not complete or payload.get('test_restart_pending') is True:
            return None
        text = checkpoint.get('text')
        if isinstance(text, str) and text.strip():
            return {**base, 'kind': 'test_result', 'complete': True,
                    'text': text, 'source_field': 'checkpoint'}
    elif game == 'detroit':
        # A completed campaign's scene intentionally clears its ending. Its
        # terminal chapter record retains every protagonist's ending.
        card = checkpoint.get('record_card') or {}
        chapters = card.get('chapters') if isinstance(card, dict) else None
        if complete and isinstance(chapters, list) and chapters:
            last = chapters[-1]
            if isinstance(last, dict) and (endings := _endings(last)):
                return {**base, 'kind': 'campaign_ending', 'complete': True,
                        'chapter': last.get('chapter_number'), 'endings': endings,
                        'source_field': 'checkpoint.record_card.chapters[-1]'}
        sources = ('outcome', 'checkpoint') if payload.get('action') == 'start_next_chapter' else ('outcome', 'checkpoint', 'before')
        for source in sources:
            state = payload.get(source)
            if isinstance(state, dict) and (endings := _endings(state)):
                chapter = state.get('chapter') or {}
                return {**base, 'kind': 'campaign_ending' if complete else 'chapter_ending',
                        'complete': complete, 'chapter': chapter.get('number') if isinstance(chapter, dict) else None,
                        'endings': endings, 'source_field': source}
    elif game == 'moonlit' and complete:
        from .game_handler import _moonlit_state
        outcome = payload.get('outcome') or {}
        text = outcome.get('text') if isinstance(outcome, dict) else ''
        text = text or checkpoint.get('text') or ''
        return {**base, 'kind': 'run_result', 'complete': True,
                'text': str(text).split('[STATE]', 1)[0].strip(),
                'phase': _moonlit_state(checkpoint).get('phase') or '', 'source_field': 'outcome/checkpoint'}
    elif game == 'fishing' and payload.get('action') == 'cmd':
        outcome = payload.get('outcome') or {}
        text = outcome.get('text') if isinstance(outcome, dict) else ''
        if isinstance(text, str) and text.strip():
            return {**base, 'kind': 'fishing_result', 'complete': False,
                    'text': text, 'source_field': 'outcome'}
    return None


def result_facts(nodes):
    results = []
    for node in nodes:
        payload = node.get('source_payload') or {}
        if node.get('execution_status') != 'succeeded' or not isinstance(payload, dict):
            continue
        fact = result_fact(payload)
        if fact:
            if any(all(previous.get(key) == fact.get(key) for key in (
                    'game', 'playthrough_key', 'kind', 'chapter', 'text', 'endings')) for previous in results):
                continue
            results.append({**fact, 'source_node_id': str(node.get('node_id') or ''),
                            'at': str(node.get('completed_at') or '')})
    return results


def result_text(fact, *, titles_only=False):
    if not fact:
        return ''
    labels = {'test_result': '测试结果', 'campaign_ending': '本周目终章结局',
              'chapter_ending': '章节结局', 'run_result': '本局结果', 'fishing_result': '钓鱼行动结果'}
    label = labels.get(fact.get('kind'), '游戏结果')
    text = str(fact.get('text') or '')
    if fact.get('endings'):
        text = '\n'.join('：'.join(part for part in (
            ending.get('title'), '' if titles_only else ending.get('narrative')) if part)
            for ending in fact['endings'])
    if fact.get('phase'):
        text = str(fact['phase']) + '\n' + text
    return f"{fact.get('game_name') or fact.get('game') or '游戏'} · {label}：\n{text}".strip()


def review_material(payload):
    # The record card repeats earlier chapters. Current screen, action,
    # checkpoint and the result remain complete; history has its own source.
    view = dict(payload)
    for key in ('before', 'outcome', 'checkpoint'):
        if isinstance(view.get(key), dict):
            view[key] = {field: value for field, value in view[key].items() if field != 'record_card'}
    if fact := result_fact(payload):
        view['confirmed_result'] = fact
    return view
