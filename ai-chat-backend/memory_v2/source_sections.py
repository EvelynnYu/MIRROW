"""Lossless request-only sections of a local producer's terminal account.

This verifies fixed projection boundaries against a frozen producer receipt,
not natural-language intent or truth.
Canonical message bodies, source digests and source reference coordinates stay
unchanged. Unrecognized formats retain the complete original text.
"""

import re
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .conversation_source import ConversationMessage


# Older open releases used a one-letter role label in this frozen suffix.
# Keep the source offsets by matching the label in place.
_ACCOUNT_HEADER = re.compile(r'\n(?:AI|[A-Z])(?:当时留下的感受（AI原述）：| 当时选择留下的感受：)')


def account_header_end(text):
    header = _ACCOUNT_HEADER.match(text)
    return header.end() if header else 0


def personal_account_start(row, content):
    """Verify the terminal account against its frozen receipt, not prose words."""
    if row.get('role') != 'event' or row.get('event_type') != 'wander_activity':
        return None
    from .source_projection import _calls
    calls = _calls(row.get('tool_calls'))
    extra = calls[0].get('extra_data') if calls else None
    if not isinstance(extra, dict):
        return None
    frozen = extra.get('memory_experience')
    if isinstance(frozen, dict) and frozen.get('version') in (1, 2):
        from .experience_contract import impression_text
        account = impression_text(frozen.get('retained_impression'),
                                  node_ids=frozen.get('retained_node_ids') or ())
        marker = '当时留下的感受（AI原述）：'
    else:
        retained = extra.get('retained_experience')
        account = str(retained.get('impression') or '').strip() if isinstance(retained, dict) else ''
        marker = ' 当时选择留下的感受：'
    suffix = marker + account
    if account and content.endswith(suffix):
        marker_start = len(content) - len(suffix)
        start = content.rfind('\n', 0, marker_start)
        if start > 0 and account_header_end(content[start:marker_start + len(marker)]) == marker_start + len(marker) - start:
            return start
    return None


def encoding_source_content(message: 'ConversationMessage'):
    if message.role != 'event' or message.event_type != 'wander_activity':
        return message.content
    ranges = message.personal_account_ranges
    if ranges:
        parts, cursor = [], 0
        for start, end in ranges:
            if cursor < start:
                parts.append({'kind': 'domain_record', 'text': message.content[cursor:start]})
            parts.append({'kind': 'personal_account', 'speaker': 'k', 'text': message.content[start:end]})
            cursor = end
        if cursor < len(message.content):
            parts.append({'kind': 'domain_record', 'text': message.content[cursor:]})
        return {'source_parts': parts}
    start = message.personal_account_start
    if start is not None and 0 < start < len(message.content):
        return {'source_parts': [
            {'kind': 'domain_record', 'text': message.content[:start]},
            {'kind': 'personal_account', 'speaker': 'k', 'text': message.content[start:]},
        ]}
    return message.content


def personal_account_ranges(row, content):
    """New attributed checkup accounts, verified against the whole frozen view.

    No historical source bytes or digest coordinates change. Unknown/ambiguous
    formats stay whole rather than guessing a boundary from natural language.
    """
    if row.get('role') != 'event' or row.get('event_type') != 'wander_activity':
        return ()
    from .source_projection import _calls
    calls = _calls(row.get('tool_calls'))
    extra = calls[0].get('extra_data') if calls else None
    frozen = extra.get('memory_experience') if isinstance(extra, dict) else None
    if not isinstance(frozen, dict) or frozen.get('account_provenance_version') != 1:
        return ()
    from .experience_contract import project, impression_text
    if project(frozen) != content:
        return ()
    candidates = []
    retained = impression_text(frozen.get('retained_impression'), node_ids=frozen.get('retained_node_ids') or [])
    for item in frozen.get('materials') or []:
        if (item.get('kind') == 'xhs_interactions' and item.get('context_version') == 2
                and item.get('account_source') == 'k_interaction_decision' and item.get('reflection')
                and not item.get('reflection_is_retained')):
            candidates.append('AI当时的心绪（原述）：' + item['reflection'])
        reflection = item.get('recorded_reflection')
        if (item.get('account_source') == 'k_node_review' and reflection
                and not item.get('reflection_is_fact_projection') and not item.get('reflection_is_retained')
                and reflection != retained):
            candidates.append('AI 当时节点感想（原述）：' + reflection)
    if retained:
        candidates.append('AI当时留下的感受（AI原述）：' + retained)
    if frozen.get('settlement_reflection'):
        candidates.append('AI 当时的活动回顾（原述）：' + frozen['settlement_reflection'])
    ranges = []
    for text in candidates:
        if content.count(text) != 1:
            return ()
        start = content.index(text)
        ranges.append((start, start + len(text)))
    ranges.sort()
    if any(a[1] > b[0] for a,b in zip(ranges, ranges[1:])):
        return ()
    return tuple(ranges)
