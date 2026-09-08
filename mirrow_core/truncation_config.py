"""
截断参数配置中心

所有 LLM 上下文/存储/输出相关的截断上限统一管理。
通过 settings_manager 持久化，前端可通过 /api/settings 读写。

值 = 0 表示无限制（跳过截断逻辑）。
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

_TRUNCATION_META: Dict[str, Dict[str, Any]] = {
    # === A 组: 记忆与日记 - 默认 0 (无限制) ===
    "topic_memory_cap": {
        "default": 0, "recommended": 60000, "min": 1000, "max": 300000, "step": 5000,
        "unit": "char", "group": "A",
        "display_name": "话题记忆整合",
        "description": "话题结束后将全天对话送 LLM 生成长期记忆的字符上限。直接决定记忆库完整度。",
    },
    "diary_msg_cap": {
        "default": 0, "recommended": 2000, "min": 100, "max": 10000, "step": 100,
        "unit": "char", "group": "A",
        "display_name": "日记-单条消息",
        "description": "生成每日日记时单条消息截断。峰值日 332 条消息，设太低日记只截到前半句。",
    },
    "diary_total_cap": {
        "default": 0, "recommended": 120000, "min": 5000, "max": 300000, "step": 5000,
        "unit": "char", "group": "A",
        "display_name": "日记-总长度",
        "description": "日记生成的全文字符帽。峰值日全量约 166K 字符，当前硬编码 60K 丢掉近 2/3 对话内容。",
    },
    "yesterday_cap": {
        "default": 0, "recommended": 60000, "min": 5000, "max": 300000, "step": 5000,
        "unit": "char", "group": "A",
        "display_name": "昨日对话注入",
        "description": "新话题开始时注入昨日完整对话的上限。直接影响 AI 对昨天和用户聊了什么的感知。",
    },
    "memory_inject_cap": {
        "default": 0, "recommended": 2000, "min": 100, "max": 10000, "step": 100,
        "unit": "char", "group": "A",
        "display_name": "记忆碎片注入",
        "description": "聊天时从记忆库检索到并注入上下文时，单条记忆碎片的字符上限。合并桶积累数月的旧内容被截断会让 AI 的回忆残缺不全。",
    },
    "ob_merge_cap": {
        "default": 0, "recommended": 6000, "min": 500, "max": 30000, "step": 500,
        "unit": "char", "group": "A",
        "display_name": "记忆合并更新",
        "description": "同一主题记忆桶更新时合并后内容的字符上限。与记忆碎片注入形成双重截断链。",
    },
    "brain_doc_cap": {
        "default": 0, "recommended": 15000, "min": 1000, "max": 100000, "step": 1000,
        "unit": "char", "group": "A",
        "display_name": "大脑文档生成",
        "description": "自动生成项目架构文档时提取源文本的字符上限。影响 AI 对自身运行环境的认知完整度。",
    },

    # === B 组: 上下文注入 ===
    "conv_msg_cap": {
        "default": 300, "recommended": 500, "min": 50, "max": 5000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "历史对话回顾",
        "description": "上下文注入时，单条近期对话消息的最大保留字符数。用户消息均长 37 字符，AI 均长 130 字符。",
    },
    "flash_user_cap": {
        "default": 300, "recommended": 300, "min": 50, "max": 5000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "工具触发-用户消息",
        "description": "判断是否需要调用工具时，传给 Flash 模型的用户消息截断长度。影响工具调用的触发准确度。",
    },
    "flash_ai_cap": {
        "default": 400, "recommended": 400, "min": 50, "max": 5000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "工具触发-AI回复",
        "description": "判断是否需要调用工具时，传给 Flash 模型的 AI 回复截断长度。Pro 的长回复被截断可能导致 Flash 漏判工具意图。",
    },
    "flash_reasoning_cap": {
        "default": 200, "recommended": 200, "min": 50, "max": 5000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "工具触发-思考过程",
        "description": "Flash 工具提取时，Pro 模型的 thinking/reasoning 截断长度。思考链过长时可能包含重要工具调用意图。",
    },
    "flash_tools_cap": {
        "default": 3000, "recommended": 3000, "min": 500, "max": 30000, "step": 500,
        "unit": "char", "group": "B",
        "display_name": "工具触发-工具描述",
        "description": "传给 Flash 模型的工具列表描述总字符上限。当前已触发截断警告，新增工具后末尾工具从 Flash 视野中消失。",
    },

    # === B2 组: 上下文注入（新增） ===
    "persona_anchor_cap": {
        "default": 200, "recommended": 200, "min": 50, "max": 1000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "人设锚点",
        "description": "注入 LLM 上下文时的身份锚点截断长度。取人设第一句，控制在首因效应窗口内。",
    },
    "persona_simplified_cap": {
        "default": 200, "recommended": 200, "min": 50, "max": 1000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "人设简化",
        "description": "生成日记时注入的简化版人设截断长度。",
    },
    "query_enhance_cap": {
        "default": 200, "recommended": 200, "min": 50, "max": 1000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "搜索查询增强",
        "description": "记忆检索前增强搜索查询时，查询文本的截断长度。影响检索精度。",
    },
    "wander_query_cap": {
        "default": 200, "recommended": 200, "min": 50, "max": 1000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "漫想搜索查询",
        "description": "漫想模式下构建记忆搜索查询时，关键词/上下文的截断长度。",
    },

    # === B3 组: 上下文多场景注入 ===
    "worldbook_display_cap": {
        "default": 300, "recommended": 300, "min": 100, "max": 2000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "世界书条目展示",
        "description": "群聊/漫想等非主聊天场景中，注入的世界书单条目字符上限。",
    },
    "scp_display_cap": {
        "default": 200, "recommended": 200, "min": 100, "max": 2000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "SCP 条目展示",
        "description": "群聊等非主聊天场景中，注入的 SCP 百科单条目字符上限。",
    },
    "diary_display_cap": {
        "default": 500, "recommended": 500, "min": 100, "max": 5000, "step": 100,
        "unit": "char", "group": "B",
        "display_name": "日记注入展示",
        "description": "群聊/哨兵等非主聊天场景中，注入的日记内容字符上限。",
    },
    "group_history_cap": {
        "default": 300, "recommended": 300, "min": 100, "max": 3000, "step": 100,
        "unit": "char", "group": "B",
        "display_name": "群聊历史消息",
        "description": "群聊上下文中，单条历史消息的字符截断上限。",
    },
    "search_results_cap": {
        "default": 500, "recommended": 500, "min": 100, "max": 5000, "step": 100,
        "unit": "char", "group": "B",
        "display_name": "搜索结果展示",
        "description": "漫想新闻浏览中，搜索结果文本的字符截断上限。",
    },
    "parallel_outline_cap": {
        "default": 300, "recommended": 300, "min": 100, "max": 2000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "平行时空大纲",
        "description": "平行时空角色扮演中，远古大纲的字符截断上限。",
    },
    "parallel_summary_cap": {
        "default": 500, "recommended": 500, "min": 100, "max": 3000, "step": 100,
        "unit": "char", "group": "B",
        "display_name": "平行时空摘要",
        "description": "平行时空角色扮演中，近期摘要的字符截断上限。",
    },
    "fusion_cluster_cap": {
        "default": 400, "recommended": 400, "min": 100, "max": 2000, "step": 50,
        "unit": "char", "group": "B",
        "display_name": "记忆融合簇",
        "description": "记忆融合（memory_fusion）中，单个簇的展示字符截断上限。",
    },

    # === C 组: 输出限制 ===
    "phone_browse_tokens": {
        "default": 200, "recommended": 300, "min": 50, "max": 4000, "step": 50,
        "unit": "tokens", "group": "C",
        "display_name": "陪刷回复长度",
        "description": "刷手机模式中，AI 生成陪聊消息时的最大输出 token 数。200 tokens 约等于 150 个中文字符。",
    },
    "sentinel_tokens": {
        "default": 150, "recommended": 250, "min": 50, "max": 4000, "step": 50,
        "unit": "tokens", "group": "C",
        "display_name": "哨兵告警长度",
        "description": "健康监控哨兵推送提醒消息时的最大输出 token 数。150 tokens 约等于 110 个中文字。",
    },

    # === D 组: 存储上限 ===
    "bookmark_cap": {
        "default": 500, "recommended": 2000, "min": 100, "max": 10000, "step": 100,
        "unit": "char", "group": "D",
        "display_name": "收藏夹内容",
        "description": "收藏消息时保存的内容字符上限。超出此长度的消息被截断存储。",
    },
    "ledger_ctx_cap": {
        "default": 3000, "recommended": 3000, "min": 500, "max": 30000, "step": 500,
        "unit": "char", "group": "D",
        "display_name": "记账本上下文",
        "description": "自动记入小账本时，捕获触发前对话的字符上限。决定记账时带了多长的前因后果。",
    },
    "music_analyzer_cap": {
        "default": 8000, "recommended": 8000, "min": 500, "max": 30000, "step": 500,
        "unit": "char", "group": "D",
        "display_name": "音乐分析数据",
        "description": "分析用户分享的音乐时，音符 JSON 数据的字符上限。长乐曲(超过5分钟)会触发截断，头尾各取一半。",
    },
}


def get_truncation_limit(key: str) -> int:
    """获取截断上限。0 = 无限制（跳过截断）。从 settings.json 读取，回退默认值。"""
    try:
        from mirrow_core.settings_manager import get_setting
        val = get_setting(key)
        if val is not None:
            return int(val)
    except Exception:
        pass
    meta = _TRUNCATION_META.get(key, {})
    return meta.get("default", 0)


def get_all_truncation_configs() -> List[Dict[str, Any]]:
    """返回所有截断配置的完整元数据（供前端渲染设置页）。"""
    results = []
    group_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    for key, meta in sorted(_TRUNCATION_META.items(),
                             key=lambda kv: (group_order.get(kv[1]["group"], 99), kv[0])):
        results.append({
            "key": key,
            "default": meta["default"],
            "recommended": meta["recommended"],
            "min": meta["min"],
            "max": meta["max"],
            "step": meta["step"],
            "unit": meta["unit"],
            "group": meta["group"],
            "display_name": meta["display_name"],
            "description": meta["description"],
            "current": get_truncation_limit(key),
        })
    return results


def get_truncation_defaults_for_settings() -> Dict[str, int]:
    """返回所有 key->default 映射，供 settings_manager._defaults 使用。"""
    return {key: meta["default"] for key, meta in _TRUNCATION_META.items()}