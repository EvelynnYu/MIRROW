"""Versioned, immutable Wander experience snapshots, not lifecycle summaries."""
from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Mapping

from context_builder.text_utils import _redact_tool_fact

VERSION = 2
_OPAQUE_ID = re.compile(r"(?:[0-9a-f]{32,64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.I)


def impression_text(value, *, node_ids=()):
    """Accept prose only; a returned node ID is not a felt experience."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text in node_ids or _OPAQUE_ID.fullmatch(text):
        return ""
    return _redact_tool_fact(text)


def snapshot(activity, nodes):
    from wander_manager.stop_reason import stop_reason_text
    reason = stop_reason_text(activity, nodes)
    stop = {'stop_reason': reason} if reason else {}
    from wander_manager.memory_fetch_result import description
    attempts = [description(node['source_payload']) for node in nodes
                if (node.get('source_payload') or {}).get('memory_fetch_version') == 1
                and node['source_payload'].get('outcome') != 'found']
    if attempts:
        stop['memory_lookup_attempts'] = attempts
    if activity.get('activity_type') == 'keyword_expansion' and any(
            (node.get('source_payload') or {}).get('essay_version') == 1 for node in nodes):
        from wander_manager.essays import entry, index_text
        items = [item for node in nodes if (item := entry(node, activity.get('activity_id', '')))]
        return {'version': VERSION, **stop, 'activity_type': 'keyword_expansion', 'essay_index': [
            {key: item[key] for key in ('id', 'activity_id', 'title', 'keyword', 'created_at')} for item in items],
            'materials': [{'kind': 'material', 'description': index_text(items)}] if items else [],
            'retained_impression': '', 'retained_node_ids': [], 'music_occurrences': [],
            'operational_only': not items}
    materials, music = [], []
    from .experience_material import game_material
    game = game_material(nodes)
    game_node_ids = {node_id for chapter in game['chapters'] for node_id in chapter['source_node_ids']} if game else set()
    if game:
        game_node_ids.update(fact['source_node_id'] for fact in game.get('results') or [])
    completed_ids = {str(n.get("node_id") or "") for n in nodes
                     if n.get("state") == "completed" and n.get("execution_status") == "succeeded"}
    retained_ids = list(activity.get("retained_node_ids") or [])
    retained = impression_text(activity.get("retained_impression"),
                               node_ids={str(n.get("node_id") or "") for n in nodes})
    if not completed_ids or not set(retained_ids) <= completed_ids:
        retained = ""
        retained_ids = []
    for node in nodes:
        payload = node.get("source_payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if payload.get('checkup_version') == 1:
            from wander_manager.checkup_experience import material
            observed = material(node)
            observed['reflection_is_retained'] = bool(retained and observed['recorded_reflection'] == retained)
            materials.append(observed)
        elif ((payload.get('listening_experience') or {}).get('version') == 1
              and (payload.get('listening_experience') or {}).get('started_at')
              and not (node.get('execution_status') == 'succeeded'
                       and node.get('completion_signal') == 'listening_finished')):
            from wander_manager.music_receipt import music_execution
            # Only the already visible identity and clock cross this boundary.
            # Prepared melody/lyrics in the private checkpoint stay private.
            materials.append({'kind': 'music_listening_attempt',
                'title': _redact_tool_fact(payload.get('title') or ''),
                'artist': _redact_tool_fact(payload.get('artist') or ''),
                'music_execution': music_execution(node)})
        elif node.get("execution_status") == "succeeded" and payload.get("netease_song_id"):
            from wander_manager.music_receipt import music_execution
            title = _redact_tool_fact(payload.get("title") or "")
            artist = _redact_tool_fact(payload.get("artist") or "")
            materials.append({"kind": "music", "title": title or "曲名未标注的歌曲", "artist": artist,
                              "source_node_id": str(node.get("node_id") or ""),
                              "completion_signal": str(node.get("completion_signal") or ""),
                              "music_execution": music_execution(node)})
            if node.get("state") == "completed" and node.get("node_id") and activity.get("activity_id"):
                music.append({"subject_id": "k", "pattern_key": "music:" + str(payload["netease_song_id"]),
                              "occurrence_key": str(activity["activity_id"]) + ":" + str(node["node_id"]),
                              "source_node_id": str(node["node_id"]),
                              "temporal_status": "occurred", "completion_signal": str(node.get("completion_signal") or "")})
        elif node.get("execution_status") in {"succeeded", "failed"}:
            if str(node.get('node_id') or '') in game_node_ids:
                continue  # The same confirmed choices appear once as a chapter.
            from .experience_material import node_material
            rich = node_material(payload, execution_status=node.get('execution_status'),
                                 k_reflection=(node.get('reflection') or '') if (
                                     (node.get('side_effect_refs') or {}).get('xhs_review_succeeded') is True) else '',
                                 k_received=(node.get('side_effect_refs') or {}).get('xhs_material_received') is True)
            if rich:
                if rich.get('kind') == 'bookshelf':
                    from wander_manager.bookshelf_actions import with_finished_picture
                    rich['description'] = with_finished_picture(rich['description'], node)
                if rich.get('kind') == 'xhs_interactions' and rich.get('context_version') == 2:
                    rich['reflection_is_retained'] = bool(retained and rich.get('reflection') == retained)
                materials.append({**deepcopy(rich), "execution_status": node.get("execution_status"),
                                  "source_node_id": str(node.get("node_id") or "")})
                continue
            # A failed attempt can be an experience if it has actual material;
            # a status/error receipt alone cannot establish one.
            material = payload.get('memory_material')
            if material is not None:
                # New failures require a handler-authored identity and actual
                # payload evidence. Existing unmarked nodes keep their frozen
                # historical contract; old snapshots are never reinterpreted.
                if (not isinstance(material, Mapping) or material.get('version') != 1
                        or material.get('kind') != 'observed_material'):
                    continue
                evidence = material.get('evidence_refs')
                if (not isinstance(evidence, list) or not evidence
                        or any(not isinstance(key, str) or not payload.get(key) for key in evidence)):
                    continue
            description = _redact_tool_fact(node.get("source_summary") or "")
            if description:
                materials.append({"kind": "material", "description": description,
                                  "execution_status": node.get("execution_status")})
    for node in nodes:
        comment=(node.get('side_effect_refs') or {}).get('xhs_comment_result')
        if isinstance(comment, Mapping):
            from xhs_interactions.context import material
            materials.append(material(comment.get('context') or comment))
    if game:
        materials.append(game)
    from wander_manager.xhs_request import activity_progress
    phone_task = activity_progress(activity, nodes)
    checkup = any(item.get('kind') == 'checkup' for item in materials)
    if checkup:
        from wander_manager.checkup_experience import independent_settlement
    from wander_manager.activity_purpose import normalize_outcome
    purpose = normalize_outcome(activity.get('purpose_outcome'))
    # A subjective outcome cannot manufacture an otherwise absent experience.
    if not materials and not retained and not (
            activity.get('activity_type') == 'sleep' and activity.get('timer_ends_at')
            and activity.get('settlement_reason') == 'timer_ended'):
        purpose = None
    return {"version": VERSION, **stop, "activity_type": str(activity.get("activity_type") or ""),
            **({'account_provenance_version': 1} if purpose is not None or any(
                item.get('account_source') in {'k_node_review', 'k_interaction_decision'} for item in materials) else {}),
            "materials": materials, "retained_impression": retained,
            "retained_node_ids": retained_ids, "music_occurrences": music,
            **({'phone_task': phone_task} if phone_task else {}),
            **({'settlement_reflection': independent_settlement(activity, nodes)} if checkup else {}),
            **({'settlement_reflection': str(activity.get('summary') or '')}
               if not checkup and activity.get('closeout_version') == 1
               and (materials or retained or purpose is not None) else {}),
            **({'purpose_outcome': purpose} if purpose is not None else {}),
            "operational_only": not materials and not retained and purpose is None}


def project(value, *, music_spans=None):
    """None means no independent experience; the original UI row stays intact."""
    if not isinstance(value, Mapping) or value.get("version") not in {1, VERSION}:
        raise ValueError("unsupported_experience_snapshot")
    parts = []
    if value.get('stop_reason'):
        parts.append(_redact_tool_fact(str(value['stop_reason'])))
    parts.extend(_redact_tool_fact(str(item)) for item in value.get('memory_lookup_attempts') or [])
    phone_task = value.get('phone_task')
    if isinstance(phone_task, Mapping) and phone_task.get('task_id'):
        parts.append('手机阅读任务事实：task_id=' + str(phone_task['task_id']) +
                     '；目标=' + str(phone_task.get('query') or '') +
                     '；累计已取得=' + str(phone_task.get('acquired_count', 0)) +
                     '；范围=' + str(phone_task.get('scope') or '') +
                     '；列表覆盖=' + str(phone_task.get('listing_coverage') or 'partial') + '。')
    for item in value.get("materials", []):
        if item.get('kind') == 'checkup' and item.get('version') == 1:
            from wander_manager.checkup_experience import project as checkup_project
            parts.append(checkup_project(item, retained_impression=value.get('retained_impression') or ''))
        elif item.get("kind") == "music" and item.get("title"):
            title, artist = item["title"], item.get("artist") or ""
            # Analysis completion is not a device playback receipt.
            verb = ("听完了歌曲" if item.get('completion_signal') == 'listening_finished' else
                    "完成了歌曲材料分析" if item.get("completion_signal") == "analysis_complete" else "处理了歌曲材料")
            start = sum(len(p) for p in parts) + len(parts)
            parts.append(f"AI{verb}：《{title}》" + (f"（{artist}）" if artist else "") + "。")
            if music_spans is not None and item.get('source_node_id'):
                music_spans.append({'source_node_id': item['source_node_id'],
                                    'span_start': start, 'span_end': start + len(parts[-1])})
            # Additive frozen facts: old snapshots are not reinterpreted.
            receipt = item.get("music_execution")
            if isinstance(receipt, Mapping) and receipt.get("fact"):
                parts.append("音乐执行事实：" + str(receipt["fact"]))
        elif item.get('kind') == 'music_listening_attempt':
            receipt = item.get('music_execution') or {}
            parts.append('AI 开始听《' + str(item.get('title') or '曲名未记录') + '》'
                         + ('（' + str(item['artist']) + '）' if item.get('artist') else '')
                         + '；' + str(receipt.get('fact') or '完成状态未知'))
            parts.append('；'.join(label + '=' + str(receipt[key]) for key,label in (
                ('started_at','开始'),('completes_at','原定听完到点'),('duration_sec','曲长秒')) if receipt.get(key) is not None))
        elif item.get('kind') == 'xhs_interactions' and item.get('context_version') == 2:
            from .experience_material import project_material
            parts.append(project_material(item))
        elif value.get('version') == 2 and item.get('kind') in {'reading', 'bookshelf', 'xhs', 'workshop', 'shopping', 'game'}:
            from .experience_material import project_material
            parts.append(project_material(item) +
                         ('\n材料已取得，但该步骤执行失败。' if item.get('execution_status') == 'failed' else ''))
        elif item.get("description"):
            parts.append("AI的活动材料：" + item["description"] +
                         ("（该步骤执行失败）" if item.get("execution_status") == "failed" else ""))
    retained = impression_text(value.get("retained_impression"), node_ids=value.get("retained_node_ids", []))
    if retained:
        parts.append("AI当时留下的感受（AI原述）：" + retained)
    if value.get('settlement_reflection'):
        parts.append('AI 当时的活动回顾（原述）：' + _redact_tool_fact(value['settlement_reflection']))
    from wander_manager.activity_purpose import outcome_text
    purpose = outcome_text(value.get('purpose_outcome'))
    if purpose:
        parts.append(purpose)
    return "\n".join(parts) or None


def project_legacy_public(value):
    """None means no independent experience; the original UI row stays intact."""
    if not isinstance(value, Mapping) or value.get("version") not in {1, VERSION}:
        raise ValueError("unsupported_experience_snapshot")
    parts = []
    phone_task = value.get('phone_task')
    if isinstance(phone_task, Mapping) and phone_task.get('task_id'):
        parts.append('手机阅读任务事实：task_id=' + str(phone_task['task_id']) +
                     '；目标=' + str(phone_task.get('query') or '') +
                     '；累计已取得=' + str(phone_task.get('acquired_count', 0)) +
                     '；范围=' + str(phone_task.get('scope') or '') +
                     '；列表覆盖=' + str(phone_task.get('listing_coverage') or 'partial') + '。')
    for item in value.get("materials", []):
        if item.get('kind') == 'checkup' and item.get('version') == 1:
            from wander_manager.checkup_experience import project as checkup_project
            parts.append(checkup_project(item, retained_impression=value.get('retained_impression') or ''))
        elif item.get("kind") == "music" and item.get("title"):
            title, artist = item["title"], item.get("artist") or ""
            # Analysis completion is not a device playback receipt.
            verb = ("听完了歌曲" if item.get('completion_signal') == 'listening_finished' else
                    "完成了歌曲材料分析" if item.get("completion_signal") == "analysis_complete" else "处理了歌曲材料")
            parts.append(f"AI{verb}：《{title}》" + (f"（{artist}）" if artist else "") + "。")
            # Additive frozen facts: old snapshots are not reinterpreted.
            receipt = item.get("music_execution")
            if isinstance(receipt, Mapping) and receipt.get("fact"):
                parts.append("音乐执行事实：" + str(receipt["fact"]))
        elif item.get('kind') == 'music_listening_attempt':
            receipt = item.get('music_execution') or {}
            parts.append('AI 开始听《' + str(item.get('title') or '曲名未记录') + '》'
                         + ('（' + str(item['artist']) + '）' if item.get('artist') else '')
                         + '；' + str(receipt.get('fact') or '完成状态未知'))
            parts.append('；'.join(label + '=' + str(receipt[key]) for key,label in (
                ('started_at','开始'),('completes_at','原定听完到点'),('duration_sec','曲长秒')) if receipt.get(key) is not None))
        elif item.get('kind') == 'xhs_interactions' and item.get('context_version') == 2:
            from .experience_material import project_material
            parts.append(project_material(item))
        elif value.get('version') == 2 and item.get('kind') in {'reading', 'xhs', 'workshop', 'shopping', 'game'}:
            from .experience_material import project_material
            parts.append(project_material(item) +
                         ('\n材料已取得，但该步骤执行失败。' if item.get('execution_status') == 'failed' else ''))
        elif item.get("description"):
            parts.append("AI的活动材料：" + item["description"] +
                         ("（该步骤执行失败）" if item.get("execution_status") == "failed" else ""))
    retained = impression_text(value.get("retained_impression"), node_ids=value.get("retained_node_ids", []))
    if retained:
        parts.append("AI当时留下的感受（AI原述）：" + retained)
    if value.get('settlement_reflection'):
        parts.append('AI 当时的活动回顾（原述）：' + _redact_tool_fact(value['settlement_reflection']))
    return "\n".join(parts) or None
