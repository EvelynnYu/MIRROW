"""Isolated source-bound retention, not a new memory or persona authority.

Reuse the existing summary-parts protocol. Only terminal accounts verified
against frozen domain receipts require an exact excerpt once their source is
admitted to an episode. Occurrence admission and all other validation stay
with the existing encoder.
"""
from copy import deepcopy
import json

from .encoding import EncodingContractError, MAX_SUMMARY_CHARS
from . import summary_parts_candidate as parts

VERSION = 'experience-retained-account-v1'
FLAT_VERSION = 'experience-retained-account-v2'
VERSIONS = (VERSION, FLAT_VERSION)
RENDER_VERSION = 'summary-retained-account-v1'
NARRATIVE_RENDER_VERSION = 'summary-retained-account-narrative-v2'
ERROR_CODES = frozenset({
    'retained_account_missing', 'retained_account_header_only',
    'retained_account_rendered_length',
})

_OLD_ADMISSION = 'mainline中只有主动AI消息、没有人类伙伴参与的片段按non_event_source_refs记账；'
_ADMISSION = (
    'mainline中只有主动assistant消息支持的AI片段按non_event_source_refs记账；'
    'E领域记录中的实际经历可在没有U回复时入事件，随后的AI主动分享可补充这件经历。'
    '玩、读、创作作品是实际行动者的活动，角色情节仍归作品。'
)
_RETENTION = (
    ' 本轮保留已入事件的领域活动及AI自己选择留下的记述。'
    'retained_account_part_refs由冻结回执边界核验，不按措辞推断。'
    '这些段所在的s-ref入事件时，在支持其具体经过的episode里引用account段，'
    '选出一两段具体理由、态度或感想的连续原话作为evidence.quote。'
    '程序直接呈现这些原话及AI归属，text的泛泛转述不能替代它们。'
    '其他段仍用自然简短叙述，具体细节来自该段所引来源，'
    '优先此次的内容、选择和已知结果，日常叮嘱在必要时才占篇幅。'
    '每件经历的最终叙述及引文合计500字符内；少量精确引文服务于经历，不是复制整张卡片。'
    '该要求只适用于已经入事件的来源，非事件与技术状态仍按原合同记账。'
)


def retained_accounts(rows):
    inventory = parts.source_inventory(rows)
    return {ref: part for ref, part in inventory.items()
            if ref.endswith('.account') and part['kind'] == 'personal_account'
            and part['speaker'] == 'k'}


def _account_body(part):
    text = part['text']
    from .source_sections import account_header_end
    return text[account_header_end(text):]


def build_messages(base_messages, rows, *, version=VERSION):
    if version not in VERSIONS:
        raise ValueError('unsupported_retained_account_version')
    result = parts.build_messages(base_messages, rows, version=parts.COMPACT_VERSION)
    if result[0]['content'].count(_OLD_ADMISSION) != 1:
        raise ValueError('retained_account_admission_contract_changed')
    result[0]['content'] = result[0]['content'].replace(_OLD_ADMISSION, _ADMISSION) + _RETENTION
    prefix, separator, body = result[1]['content'].partition('Authoritative source records:\n')
    result[1]['content'] = (prefix + 'retained_account_part_refs=' +
                           json.dumps(list(retained_accounts(rows)), separators=(',', ':')) +
                           '\n' + separator + body)
    return result


def build_schema(rows, *, version=VERSION):
    if version not in VERSIONS:
        raise ValueError('unsupported_retained_account_version')
    schema = parts.build_schema(rows, version=parts.COMPACT_VERSION)
    field = schema['properties']['episodes']['items']['properties']['summary_parts']
    field['description'] = (
        '按连续经历归纳的少量段；引用带冻结account的领域来源时保留其具体本人原话。'
        '最终文本含归属及原话共500字符内。')
    required_refs = set(retained_accounts(rows))
    for branch in field['items']['anyOf']:
        evidence = branch['properties']['evidence']['items']['properties']
        if required_refs.intersection(evidence['part_ref']['enum']):
            evidence['quote']['description'] = (
                '本段所引来源的连续逐字原文；来自retained_account段时选具体理由、态度或感想。'
                '这段原话会直接保留，不用泛泛text转述取代。')
    if version == FLAT_VERSION:
        # Some production relay/native-schema paths do not reliably choose a
        # later anyOf branch. Ownership is still strictly checked locally;
        # flatten only the transport's citation grammar, not that validator.
        field['items'] = parts.build_schema(rows, version=parts.NARRATIVE_VERSION)[
            'properties']['episodes']['items']['properties']['summary_parts']['items']
        field['items']['properties']['evidence']['items']['properties']['quote']['description'] = (
            '该part_ref正文的连续逐字原文；受保护account选择具体理由、态度或感想。'
            '其原话直接保留，每段仍保持同类、同说话者，最终由本地核验。')
    return schema


def canonical_output(raw, rows, *, include_account_narrative=False):
    # Reuse strict ownership, quote membership, episode-source and size checks.
    # This renderer has its own final length gate, not the legacy audit layout.
    canonical = parts.canonical_output(raw, rows, validate_rendered_length=False)
    value = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    inventory = parts.source_inventory(rows)
    accounts = retained_accounts(rows)
    source_order = {ref: position for position, ref in enumerate(inventory)}
    admitted_refs = {ref for episode in value['episodes'] for ref in episode['source_refs']}
    required = {ref for ref, account in accounts.items() if account['source_ref'] in admitted_refs}
    seen = set()
    for original, episode in zip(value['episodes'], canonical['episodes'], strict=True):
        rendered = []
        for paragraph in original['summary_parts']:
            evidence = paragraph['evidence']
            retained = [entry for entry in evidence if entry['part_ref'] in accounts]
            if retained:
                # Other same-owner prose is not silently upgraded to an exact quote.
                # Every evidence excerpt is kept separately with its actual owner.
                if include_account_narrative:
                    # Keep object/plot context as AI's attributed account, not
                    # an independently verified execution result.
                    rendered.append('AI记述这次经历时说：' + paragraph['text'].strip())
                excerpts = set()
                for entry in evidence:
                    ref, quote = entry['part_ref'], entry['quote']
                    if ref in accounts:
                        if quote not in _account_body(accounts[ref]):
                            raise EncodingContractError('retained_account_header_only')
                        seen.add(ref)
                    excerpts.add((ref, inventory[ref]['text'].index(quote), quote))
                ordered = sorted(excerpts, key=lambda item: (source_order[item[0]], item[1]))
                by_source = {}
                for ref, _, quote in ordered:
                    by_source.setdefault(ref, []).append(quote)
                for ref, quotes in by_source.items():
                    prefix = 'AI当时写下：' if ref in accounts else 'AI当时说：'
                    rendered.append(prefix + ' '.join('“' + quote + '”' for quote in quotes))
            else:
                source = inventory[evidence[0]['part_ref']]
                text = paragraph['text'].strip()
                if source['kind'] == 'personal_account':
                    owner = {'k': 'AI', 'human': '人类伙伴'}[source['speaker']]
                    rendered.append(owner + '当时说起：' + text)
                else:
                    rendered.append(text)
        summary = ' '.join(rendered)
        if not summary or len(summary) > MAX_SUMMARY_CHARS:
            raise EncodingContractError('retained_account_rendered_length')
        episode['summary'] = summary
    if required - seen:
        raise EncodingContractError('retained_account_missing')
    return canonical
