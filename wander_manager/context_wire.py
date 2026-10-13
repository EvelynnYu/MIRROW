"""Validate authoritative runtime tail without flattening four-zone history."""
from copy import deepcopy


def decision_messages(built, runtime_block, instruction):
    sections = getattr(built, 'sections', {}) or {}
    if sections.get('wander_runtime', {}).get('text') != runtime_block:
        raise ValueError('runtime_block_not_final_or_missing')
    wire = getattr(built, 'decision_messages', None)
    if wire is not None:
        if not wire or wire[-1].get('content', '').rstrip() != runtime_block.rstrip():
            raise ValueError('runtime_block_not_final_or_missing')
        messages = deepcopy(wire)
    else:
        system = getattr(built, 'system_content', '')
        if not system or not system.rstrip().endswith(runtime_block.rstrip()):
            raise ValueError('runtime_block_not_final_or_missing')
        messages = [{'role': 'system', 'content': system}]
    return messages + [{'role': 'user', 'content': instruction}]
