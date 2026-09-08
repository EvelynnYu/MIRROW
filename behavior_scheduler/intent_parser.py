"""
意图解析器 - 用 Flash 模型检测自然语言中的工具调用意图。

提供：
- create_lightweight_caller：用于 main.py 的轻量 Flash 调用工厂
- TOOL_KEYWORD_MAP / _REMOTE_FALLBACK_KEYWORDS：关键词数据，供 scheduler 预筛
- IntentParser 类已于 2026-06-22 删除（从未实例化），意图解析现由 Flash 提取 + 关键词预筛替代
"""

from __future__ import annotations

import json
import logging
import asyncio
import random
import os
from mirrow_core.token_tracker import log_token_usage
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# DeepSeek Flash 配置（与 main.py 共用）
DEEPSEEK_FLASH_API_KEY = os.getenv("DEEPSEEK_FLASH_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_FLASH_API_URL = os.getenv("DEEPSEEK_FLASH_API_URL", "https://api.deepseek.com/v1")
DEEPSEEK_FLASH_MODEL = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash")
REMOTE_INTENT_PARSER_ENABLED = os.getenv("INTENT_PARSER_REMOTE_ENABLED", "true").lower() == "true"
INTENT_PARSER_TIMEOUT = float(os.getenv("INTENT_PARSER_REMOTE_TIMEOUT_SECONDS", "10.0"))

# 工具意图关键词（快速预筛，命中后才调 API 做精确判断）
TOOL_KEYWORD_MAP = {
    # 🆕 get_weather 已移除天气关键词——天气现在由 Sentinel WeatherSource 自动注入上下文
    # take_screenshot / capture_camera 关键词已合并到 eyes——子工具对 Pro/Flash 隐藏
    "manage_scheduled_task": [
        "列出", "移除", "去掉", "撤销",
        "不再提醒", "关掉提醒", "停掉提醒", "删除提醒", "取消提醒",
        "删掉提醒", "删了那个", "清除提醒", "删了提醒", "所有提醒", "有哪些提醒",
        "查看任务", "查看提醒", "看看提醒", "看看任务", "看一下提醒",
        "关掉日程", "取消日程",
        "过期", "过期的", "过期提醒", "不需要了",
        # 口语表达（Flash 判断语境是否为提醒相关）
        "删掉！", "删了！", "不许提醒！", "删掉。", "删了。",
        # 注意：不含裸动词"删除""取消""删""清除""干掉""清掉"——这些匹配面太宽
    ],
    "create_scheduled_task": [
        "提醒我", "设个提醒", "定个闹钟", "定时提醒", "到点叫我",
        "叫我", "喊我", "通知我", "到点了", "别忘了", "记得提醒我",
        "到时候告诉我", "点钟叫我", "分钟后", "小时后",
        "每天", "每周", "每月", "定期", "每天早", "每天晚上", "每晚",
        "帮你设", "帮我设", "帮我定", "给我设", "定个", "设一个", "设提醒",
        "给你设",  # AI说"给你设个提醒"时的主动创建模式
        "的提醒",  # 覆盖"十点的提醒""一个X点的提醒"等中间有词隔开的模式
        "定时", "闹钟", "日程",
    ],
    "web_search": [
        "搜索", "搜一下", "上网查",
        "查查", "查一查", "搜搜", "搜一搜", "搜一个", "查一个",
        "上网搜", "搜搜看", "查查看", "搜点什么", "查点什么",
    ],
    "web_browser": ["打开网页", "访问网站", "浏览网页"],
    "shell": ["执行命令", "运行命令", "命令行"],
    "toys": [
        # 设备名词
        "玩具", "toy", "情趣玩具", "情趣玩具", "小玩具", "情趣玩具", "蓝牙玩具",
        "震动棒", "振动棒", "按摩棒",
        # 品牌/型号
        "Lush", "XXD", "lush", "xxd", "toys",
        # 动作信号词（注意：不含单字"震"——会匹配"地震""震撼""震惊"等几十个日常词）
        "振动", "震动", "震颤", "震感",
        # 频率/模式调节（复合词防止误触发:"间歇"匹配"间歇性补觉"等日常用语）
        "频率调", "调频率", "间歇模式", "间歇震动", "间歇振动", "脉冲震动",
        # 档位/强度（复合词）
        "档位", "升档", "降档", "调档", "调高", "调低", "加强度",
        # 切换/控制（复合词）
        "切换", "换成", "调到", "改到", "切回",
        # 停止/暂停
        "停一下", "别震了", "够了停下", "关震",
        "震停", "让它停", "把玩具关了",
        # 临时控制（有门控保护，日常不会误触）
        "震我", "震一下", "换个模式", "换频率",
        "强一点", "弱一点", "开一下", "关一下",
    ],
    "toy2": [
        # 设备名
        "第二个玩具", "另一个玩具", "新玩具", "另一个情趣玩具",
        "toy2", "二号玩具", "玩具", "toy2", "玩具", "玩具", "玩具",
        # 模式/操作（中英文覆盖）
        "吮吸", "吸吮", "伸缩", "双重", "叠加", "combo",
        "stretch", "suck",
        "吸住", "吸我",
        # 档位/强度
        "档位", "升档", "降档", "调档", "调高", "调低", "加强度",
        "拉满", "全开", "最强", "最大档", "停玩具",
        "轻一点", "适中", "温柔点", "大力点",
        # 切换/控制
        "切换", "换成", "调到", "改到", "切回", "再来",
        # 停止/暂停
        "停一下", "别震了", "别动了", "够了", "关震", "停下来",
        "停下", "停了", "让它停", "关了它",
        # 加热/临时操作
        "热一点", "加温", "温一下", "开加热",
        # 预设模式名（v3 三阶段 9 个）
        "轻吟", "激奏", "微澜", "潮起", "深吮", "酥麻", "炽焰", "巅峰", "停歇",
        # 振动相关
        "振动", "震动", "嗡嗡", "震颤",
    ],
    "manage_calendar": [
        "纪念日", "写日历", "记日历", "日历里写", "往日历", "设个纪念日",
        "创建纪念日", "加个纪念日", "写进日历", "记在日历",
    ],
    "manage_ledger": [
        "记账", "记一笔", "记下了", "账本", "翻账本", "记仇", "这笔账", "欠着",
        "你完了", "看我怎么收拾你", "给我等着", "你等着",
        "笔账",  # 覆盖"记了一堆账""18笔账"等口语模式
        "几笔",  # 覆盖"记了几笔""有几笔了"等计数表达
        "消账", "销账", "清账", "划账", "把账", "销了", "消了",
        "划掉", "删账", "删了账", "记账本", "翻一下账",
        "这笔删", "那笔删", "把账消", "把账销", "把账清", "把账删",
        # query/detail 新增
        "查总账", "多少次", "排名", "哪种最多",
        "查详细", "这条怎么回事", "为什么记这笔", "详细账目", "前因后果", "当时怎么回事",
        "乱记账", "假账", "错账", "瞎记账",
    ],
    "cloud_music": [
        # 推荐/搜索（覆盖"推一首歌""推个歌""来点音乐"等自然变体）
        "推歌", "推几首", "推一首", "推个", "推荐首歌", "来点音乐",
        "放首歌", "放一首", "放个歌", "放个", "整点歌",
        "放点音乐", "来首歌", "每日推荐", "今天推荐", "推荐点歌", "推荐几首",
        "直接放", "放一下", "放来听", "播来听", "来一首", "来首",
        # 播放控制
        "放歌", "播放", "切歌", "下一首", "上一首", "暂停", "继续放",
        "换一首", "换首歌", "不听这首", "跳过", "放给我听", "放出来",
        # 音量
        "音量", "大声点", "小声点", "太吵了",
        # 查询
        "现在在放什么歌", "什么歌", "这首歌", "在听什么",
        # 收藏/登录
        "收藏这首歌", "喜欢这首歌",
        # 网易云模式（明确的指令性词）
        "网易云", "音乐模式", "听歌模式",
        # 注意：不包含通用词"音乐""听歌""歌曲""想听"——这些在日常对话中出现频率太高，误触发风险极大
    ],
    "_general": [
        "让我去查", "帮你删", "帮你搜", "帮我查一下", "你去查一下",
        "帮你查查", "帮你搜搜", "我查查", "我搜搜", "搜搜看",
        "删掉", "删了", "删这个", "删那个", "把这个删", "删第",
        # 档位（玩具两边共用，复合词防误触发）
        "2档", "3档", "4档", "5档", "一档", "二档", "升档", "降档", "调档", "档位",
        # 停止/控制
        "停玩具", "停了它", "停止", "停下", "关了它", "关玩具",
    ],
    "send_voice": [
        "语音", "发语音", "语音说", "用声音说", "语音消息",
        "说句话", "说出来", "读出来", "念出来",
        "语音回复", "语音回答", "语音告诉",
        "再发一条", "发条语音", "来个语音", "能发语音", "发个语音",
        "用语音", "语音跟", "语音和我", "语音叫我",
        "一条语音", "发我一条", "发我一", "发条消息",
        "我想用语音", "让我用语音", "用语音跟你说",
        "跟你说句话", "跟你说一句", "跟你说说", "说给你听",
        "让我说一句", "听我说", "听听我的声音", "听听看",
    ],
    "band": [
        "心率", "心跳", "脉搏",
        "手环", "华为手环", "测心率", "看心率",
        "读心率", "我的心率", "心率多少",
        "连续心率", "监测心率",
        "步数", "今天走了", "走了多少步", "看看步数",
        "睡眠", "昨晚睡", "昨晚睡眠", "昨晚睡得", "睡了多久", "睡得好", "睡眠数据",
        "血氧", "测血氧", "血氧多少",
        "健康数据", "健康汇总",
    ],
    "check_phone": [
        "查手机", "看看手机", "手机状态", "手机怎么样", "查一下手机",
        "手机电量", "电量多少", "还有多少电", "快没电了", "充电状态",
        "在充电吗", "电池多少", "手机没电", "还剩多少电",
        "屏幕时间", "用了多久手机", "手机使用", "今天玩了多久",
        "玩了多久手机", "看看用了", "手机统计", "手机用时",
        "用了多少时间", "屏幕使用", "截屏", "手机截图",
        # AI 视角主动查手机（"看看你手机"等，命中后 Flash 兜底判断）
        "看看你手机", "你手机在干嘛", "手机在干嘛",
    ],
    "eyes": [
        # 原 eyes 关键词
        "睁眼", "瞧瞧你", "瞄一眼", "瞅一眼", "看眼",
        # AI 视角主动预告（"让我看看你"等，命中后 Flash 兜底判断）
        "看看你", "让我看看你", "看你一眼", "瞄你一眼",
        # 合并 take_screenshot 关键词（子工具已隐藏，统一走 eyes）
        "截图", "截屏", "屏幕截图", "截张图", "截下图", "看屏幕", "桌面上有", "屏幕上看",
        # 合并 capture_camera 关键词（子工具已隐藏，统一走 eyes）
        "拍照", "摄像头", "相机", "拍张", "照张", "照照", "前置", "cam",
        "打开摄像", "我看到你", "摄像头看到", "看看周围", "看看环境",
        "照张相", "拍张照", "自拍", "拍张看看", "照张看看",
    ],
    "schedule_self_task": [
        "等会儿", "过会儿", "过一阵", "等一下再", "一会后",
        "半小时后", "30分钟后", "几分钟后", "回头再", "晚点再",
        "等等再", "我待会", "等下我", "我过会", "晚一点",
        "稍等再", "再过", "待会再", "等一等",
    ],
}
# 纪念日检测信号词（2-4 字短词，覆盖自然语言变体，LLM Flash 做最终判断）
MEMORIAL_SIGNAL_WORDS = [
    # 核心记忆动词
    "记下", "记住", "不会忘", "忘不了", "难忘", "记得今天",
    "纪念", "纪念日", "值得纪念",
    # 第一次/初始
    "第一次", "头一次",
    # 每年
    "每年", "年年",
    # 特殊/重要
    "特别的日子", "重要的日子", "有意义的日子", "有意义",
    # 承诺/约定
    "定个规矩", "以后每年",
    # 补充
    "认识的第", "周年", "转折点", "新的开始",
]

MEMORIAL_SIGNAL_SET = set(MEMORIAL_SIGNAL_WORDS)


def has_memorial_signal(text: str) -> bool:
    """检查文本是否包含纪念日信号词（短词粗筛，LLM Flash 做最终判断）"""
    text_lower = text.lower()
    return any(kw in text_lower for kw in MEMORIAL_SIGNAL_SET)


_REMOTE_FALLBACK_KEYWORDS = {
    *TOOL_KEYWORD_MAP["eyes"],  # 合并了 take_screenshot + capture_camera 关键词
    *TOOL_KEYWORD_MAP["create_scheduled_task"],
    *TOOL_KEYWORD_MAP["manage_scheduled_task"],
    *TOOL_KEYWORD_MAP["manage_calendar"],
    *TOOL_KEYWORD_MAP["manage_ledger"],
    *TOOL_KEYWORD_MAP["web_search"],
    *TOOL_KEYWORD_MAP["web_browser"],
    *TOOL_KEYWORD_MAP["shell"],
    *TOOL_KEYWORD_MAP["toys"],
    *TOOL_KEYWORD_MAP["toy2"],
    *TOOL_KEYWORD_MAP["cloud_music"],
    *TOOL_KEYWORD_MAP["send_voice"],
    *TOOL_KEYWORD_MAP["band"],
    *TOOL_KEYWORD_MAP["check_phone"],
    *TOOL_KEYWORD_MAP["schedule_self_task"],
    *TOOL_KEYWORD_MAP["_general"],
}

# 深夜模式门控关键词——深夜 OFF 时排除，避免日常对话误触发
_NIGHT_GATED_KEYWORDS = frozenset({
    *TOOL_KEYWORD_MAP.get("toys", set()),
    *TOOL_KEYWORD_MAP.get("toy2", set()),
})






def create_lightweight_caller(api_key: str):
    """
    创建轻量级 LLM 调用函数，签名与 call_llm_for_wander 一致。

    用于分类/提取类任务（DisturbJudgment 等），
    使用 DeepSeek Flash，快速且可靠。
    """
    from openai import AsyncOpenAI
    timeout_seconds = float(os.getenv("LIGHTWEIGHT_LLM_TIMEOUT_SECONDS", "15.0"))

    client = AsyncOpenAI(
        api_key=api_key or DEEPSEEK_FLASH_API_KEY,
        base_url=DEEPSEEK_FLASH_API_URL,
        timeout=timeout_seconds,
        max_retries=1,
    )

    async def _call(messages: list) -> dict:
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=DEEPSEEK_FLASH_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=256,
                ),
                timeout=timeout_seconds + 2.0,
            )
            content = response.choices[0].message.content or ""
            asyncio.create_task(log_token_usage("lightweight_intent", DEEPSEEK_FLASH_MODEL, response.usage if hasattr(response, 'usage') else None))
            return {"content": content}
        except Exception as e:
            logger.warning(f"Lightweight LLM call failed: {e}")
            return {"content": ""}

    return _call
