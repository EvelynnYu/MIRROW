"""Objective projections only. No behavior instructions or read side effects."""
from .books import person_projection, SUBJECTS


def people_context(subjects=None, user_message="", context_query=""):
    lines = []
    for subject in subjects or ["human"]:
        entries = person_projection(subject)
        query = (context_query or user_message).casefold()
        entries.sort(key=lambda e: (-sum(bool(t) and str(t).casefold() in query for t in [e['name'], *e['aliases'], *e['keywords']]), e['name']))
        for entry in entries[:3]:
            lines.append(f"关于{SUBJECTS[subject]}的已标注记录：{entry['name']}（性质：{entry['kind']}；观点归属：{SUBJECTS.get(entry['knower_id'], '未标注')}；来源：{entry['source']}）\n{entry['body']}")
        if len(entries) > 3:
            lines.append(f"另有 {len(entries) - 3} 条已标注人物记录未在本轮选入。")
    return "\n\n".join(lines)
