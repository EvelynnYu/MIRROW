"""Shared extraction contract; never injected as a Agent context ingredient."""
WORLD_SCOPE = """
领域归属：世界书保存外部生活事实。Agent 对自己的价值、感受、长期倾向属于自我书。
先识别真实实体和别名。宠物、猫女儿等分类或关系称谓不表示新实体；已有同一实体时使用精确词条名作为 target_entry。无法确定同一主体时保留歧义，不自动合并。
说话者、被描述者和个人理解分开；转述不是说话者自己的偏好，Agent 的推测不是人类伙伴明确陈述的事实。
"""


def format_entries(entries):
    return "\n\n".join(f"词条：{e.name}\n别名：{e.aliases}\n关键词：{e.keywords}\n已有正文：{e.body}" for e in entries)
