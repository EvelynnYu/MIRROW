"""Verify historical quotations by source identity, never by topic keywords."""
from copy import deepcopy


def validate_history_evidence(claims, index):
    accepted, errors = [], []
    if not isinstance(claims, list):
        return [], ['history_evidence_not_list']
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append('history_evidence_invalid_record')
            continue
        ref = claim.get('source_ref')
        source = index.get(ref) if isinstance(ref, str) else None
        if not source:
            errors.append('history_evidence_unknown_source')
            continue
        speaker = '人类伙伴' if source['role'] == 'user' else 'AI' if source['role'] == 'assistant' else ''
        if not speaker or claim.get('speaker') != speaker:
            errors.append('history_evidence_speaker_mismatch')
            continue
        quote = claim.get('quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in source['content']:
            errors.append('history_evidence_quote_mismatch')
            continue
        accepted.append({**deepcopy(source), 'source_ref': ref, 'speaker': speaker, 'quote': quote})
    return accepted, errors


def render_history_evidence(evidence):
    if not evidence:
        return '本次没有提交经过来源核对的聊天引文。'
    return '\n'.join(f"[{item['timestamp']}｜{item['speaker']} 的历史通信原话] {item['quote']}"
                     for item in evidence)
