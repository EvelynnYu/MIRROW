"""Explicit synthetic scenarios for the isolated UI server only."""
import json
from . import maintenance


async def run_fixture():
    messages = [
        {"id": "fixture-a1", "role": "user", "content": "测试小猫喜欢在纸箱里睡觉。", "timestamp": "2026-09-06T09:00:00"},
        {"id": "fixture-a2", "role": "user", "content": "测试小猫现在不再喜欢靠窗睡觉。", "timestamp": "2026-09-06T09:01:00"},
        {"id": "fixture-agent1", "role": "assistant", "content": "我今天发现自己偏爱安静。", "timestamp": "2026-09-06T09:02:00"},
        {"id": "fixture-agent2", "role": "assistant", "content": "我提议把这个理论称为测试回响。", "timestamp": "2026-09-06T09:03:00"},
    ]
    base = {"operation": "append", "subject_id": "unknown", "knower_id": "human", "kind": "fact", "keywords": ["测试宠物"], "reason": "隔离模拟材料，不来自真实对话"}
    specs = [
        {**base, "domain": "world", "name": "测试小猫", "target_entry": "测试小猫", "body": "测试小猫喜欢纸箱。", "evidence": [{"message_id": "fixture-a1", "quote": messages[0]["content"]}]},
        {**base, "domain": "world", "name": "测试宠物习惯更正", "target_entry": "测试小猫", "operation": "replace", "body": messages[1]["content"], "evidence": [{"message_id": "fixture-a2", "quote": messages[1]["content"]}]},
        {**base, "domain": "self", "name": "测试安静偏好", "target_entry": "", "subject_id": "agent", "knower_id": "agent", "kind": "self_reflection", "body": "我今天偏爱安静。", "evidence": [{"message_id": "fixture-agent1", "quote": messages[2]["content"]}]},
        {**base, "domain": "scp", "name": "测试回响", "target_entry": "", "subject_id": "shared", "knower_id": "agent", "kind": "theory", "body": "测试回响是尚未取得双方一致的理论提案。", "evidence": [{"message_id": "fixture-agent2", "quote": messages[3]["content"]}]},
    ]
    responses = iter([{"proposals": specs}, {"reviews": [{"index": i, "supported": True, "attribution_clear": True, "no_conflict": i != 1, "entity_match": True, "novel": True, "stable": False, "agreed": False, "reason": "隔离模拟审核，不是模型实测"} for i in range(4)]}])
    async def fake_llm(prompt):
        return json.dumps(next(responses), ensure_ascii=False)
    result = await maintenance.run(messages, fake_llm, source_date="2026-09-06", session_id="isolated-fixture")
    return {"status": result["status"], "items_count": len(result.get("items", [])), "fixture": True}
