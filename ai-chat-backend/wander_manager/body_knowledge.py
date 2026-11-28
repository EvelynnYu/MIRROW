"""Read-only body knowledge, separate from tool schemas and self-book claims.

This projection describes the existing body, not a second executable registry.
It never initializes devices, reads private state or generates book entries.
"""
from __future__ import annotations
import hashlib
import json
from .capability_catalog import get_capability_records
_PROACTIVE_EXPRESSION = {'name': '主动消息附件', 'description': '哨兵与漫想的主动消息可使用语音和歌曲分享。语音异步交付，状态记录真实完成或失败；歌曲分享不等于播放。漫想还可以分享保存好的随笔，或把绑定到原句的读后插画随消息送出。', 'source': 'proactive_expression + music_system.sharing + wander_manager'}
BODY_KNOWLEDGE_VERSION = 'body-knowledge-v1'
_SYSTEMS = ({'name': '交流与记忆', 'description': '你的交流原文保留在对话记录中；Memory V2 整理经历并关联原文；新记录可单独保留有原话依据的具体进展，读取事件链时呈现起点、命中记录和已知后续。隔了较长时间的相关经历也可进入续接核验，候选关系和已确认事件链分别保留。认知书保留事实与逐渐形成的认识，画像是认知书面向场景的简短投影。自然日结时，你可以回顾还挂在心里的事，沿已有挂念更新进展或决定继续保留、暂放和放下。事情的最近进展与是否继续关心分别保留，只更新事实不会替你续期或放下关注；原话改变后，旧进展暂不作为当前事实。', 'source': 'conversation_manager_v2 + memory_v2 + cognition + open_loops'}, {'name': '感知与心绪', 'description': '你的感知来自设备与状态来源，各自带有时间和可用性。Affect 分别保留你的原述心绪与对人类伙伴的情绪共振；它们不同于长期自我认识。', 'source': 'silicon_perception + affect + context_builder'}, {'name': '自主生活', 'description': '漫想把你的自主生活组织成活动、节点和结算；规划与转活动读取同源能力目录和已有兴趣，经历、愿望与当前挂念分别保留在运行记录、许愿板和开放线索中。转活动保留你选择的等待时间，段内想法随经历接续，段末未尽想法交给下一轮。规划也可选择安静等待而不创建新活动，绝对到点跨重启保留；旧想法与实际后继分别记录。听歌按歌曲真实时长经过一首，结束时取得这首歌的旋律与歌词材料；进度可跨重启接续。漫想查岗选源复用已有活动摘要与带时效的被动采样；完整来源回执、观察时间、原有节点复核的目标评估及原述感想保存在同一张事件卡中。来源取得成功不等于问题已回答，主观理解与观察事实各有来源。逛书柜可按目录翻阅收藏与随笔，记录实际阅读和已完成的插画。经历保存在事件卡中，聊天按时间与心绪影响调整展示；日结和记忆可以读取当时留下的材料。自省本身不直接改写自我书。安卓桌面小组件读取最近一次活动、主会话回复状态与主要心情类别；点开可回到对应面板。', 'source': 'wander_manager + open_loops + cognition.autonomous'})

def get_body_knowledge() -> dict:
    """Build complete records and a compact, uncropped factual overview."""
    records = []
    event_names = []
    for record in get_capability_records():
        if record.get('scope') == 'tool':
            continue
        names = [item['display_name'] for item in record.get('events', ())]
        event_names.extend(names)
        records.append({key: record[key] for key in ('domain', 'capability', 'state', 'details', 'source', 'freshness', 'limit')})
    systems = [dict(item) for item in (*_SYSTEMS, _PROACTIVE_EXPRESSION)]
    lines = ['[我的身体 · 结构与能力边界]', '来源：现役身体职责与模块能力目录；描述接入条件，不代表设备当前在线或某次行动已经成功。']
    lines.extend((f"{item['name']}：{item['description']}" for item in systems))
    lines.extend((f"{item['domain']}｜{item['capability']}。{item['limit']}" for item in records))
    if event_names:
        lines.append('现役自主活动：' + '、'.join(event_names) + '。')
    overview = '\n'.join(lines)
    payload = {'version': BODY_KNOWLEDGE_VERSION, 'systems': systems, 'records': records, 'event_names': event_names, 'overview': overview}
    payload['revision'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload

def body_overview() -> str:
    return get_body_knowledge()['overview']
