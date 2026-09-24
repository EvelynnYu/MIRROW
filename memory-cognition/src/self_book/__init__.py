"""
自我书 (Self Book) — Agent 的长期自我认知（习惯/倾向/认知/价值观）。

与世界书并列：
- 世界书存「人类伙伴」的生活事实（外部知识，关键词触发，人审核）
- 自我书存「Agent」自己的倾向（身份层，自动生长 + 人类伙伴手动编辑，无审核队列）

注入位置在 context_builder 的 A身份 层（与 persona/active_evolutions 同簇）。
"""

from .self_book import SelfBook, SelfBookEntry, get_global_self_book

__all__ = ["SelfBook", "SelfBookEntry", "get_global_self_book"]
