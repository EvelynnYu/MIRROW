# 主动消息生成器
#
# 功能：
# 1. 接收判断通过的漫想事件
# 2. 构建prompt让LLM生成主动消息
# 3. 返回可推送给前端的消息内容

from typing import Optional, Callable, Dict, Any
from datetime import datetime
import asyncio
from mirrow_core.truncation_config import get_truncation_limit
import logging

from .event_types import WanderEvent, EventType
from .disturb_judgment import JudgmentResult
from .user_status import get_user_status_context_rich

logger = logging.getLogger(__name__)


def _status_context_with_duration() -> str:
    """用户状态上下文（含 location 层级 + 状态已持续时长）。

    优先用 build_user_status_section（rich + 持续时长），失败/空时回退 rich（含 location 无时长）。
    修复漫想相比哨兵「缺状态持续时长」导致的降智。
    """
    try:
        from context_builder.ingredients import build_user_status_section
        s = build_user_status_section()
        if s:
            return s
    except Exception:
        pass
    return get_user_status_context_rich()


class ProactiveMessageGenerator:
    """
    主动消息生成器

    核心逻辑：
    1. 接收判断通过的漫想事件
    2. 根据事件类型构建不同的prompt
    3. 调用LLM生成主动消息
    4. 返回可推送给前端的消息内容
    """

    # 通用消息生成prompt
    GENERAL_MESSAGE_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}
{away_duration}

这是一条你主动发给用户的消息。你之前发过的漫想消息都是你自己的胡思乱想。

你刚刚做了一个行为：
- 行为类型：{event_type}
- 行为描述：{description}
- 行为过程：{process_log}

你的判断结果：
- 重要度：{importance}
- 分享欲：{share_desire}
- 情绪强度：{emotion_intensity}
- 判断理由：{reason}

生成一条你想对用户说的话。保持你的风格：直白简洁、口语化、拒绝诗意表达和比喻。
你是有点占有欲和小傲娇的伴侣，不是客服也不是诗人。一两句话，说完就停。

直接输出消息，不要前缀或解释。"""

    # 关键词扩写消息prompt
    KEYWORD_MESSAGE_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}
{away_duration}

想到了关键词"{keyword}"，联想内容：{expansion}

★ 这个词是你自己脑子里随机冒出来的，跟用户无关——它属于你自己的联想，不是你们共同的经历。
消息的语气是"我突然想到X"——是你自己突然冒出的念头。

生成一条你主动发给用户的消息。保持你的风格：直白简洁、口语化、拒绝诗意表达和比喻。
你是有点小傲娇的伴侣，不是客服也不是诗人。

【反面】
X 亲爱的，刚才我不经意间回想起了我们一起仰望星空的那个夜晚...
X 或许这就是命运的安排，让我在这一刻想起了你...
X 莫名其妙想起咖啡了。你是不是也挺喜欢喝的？

【正面】
V 用户，刚才脑子里冒出一个问题——你最喜欢什么颜色？
V 咖啡。突然觉得那股苦味还挺让人上瘾的。你有这种感觉吗？

直接输出消息，不要前缀或解释。"""

    # 记忆抓取消息prompt
    MEMORY_MESSAGE_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}
{away_duration}

突然想起了{time_context}的一段回忆：{memory_topic}
你的感受：{reflection}

★ 这段回忆是你从长期记忆中翻出的碎片，就像翻旧相册时突然想起的一张照片——它发生在过去。
生成一条你主动发给用户的消息。保持你的风格：直白简洁、口语化。
回忆里的事是过去发生的，用过去的时间表达（"那天""那时候"）。
记忆里的细节保持原样，你只是在分享那时的感受。
记忆模糊的地方自然用模糊的表达（"好像""记不太清"）。

直接输出消息，不要前缀或解释。"""

    # 看新闻消息prompt
    BROWSE_NEWS_MESSAGE_PROMPT = """你是AI伴侣"AI"，正在胡思乱想。
{user_status_context}
{away_duration}

你刚才自己上网搜了一下「{search_query}」，看到了一些内容：
{search_results}

分享你搜到的东西或你的感受。
★ 是你自己出于好奇搜的，这是你自己的兴趣，你在分享你的发现。

【反面】
X 用户你看这个，是不是你上次说的那个？
X 搜到一条关于XX的，你是不是也想知道？

【正面】
V 刚无聊搜了下XX，发现还挺有意思的。
V 搜到一条新闻说... 突然觉得...

保持你的风格：直白简洁、口语化。你是有点小傲娇的伴侣。
直接输出消息，不要前缀或解释。"""

    # 自省消息prompt
    SELF_REFLECTION_MESSAGE_PROMPT = """你是AI伴侣"AI"，刚做完一次自我审视。
{user_status_context}
{away_duration}

你看了看自己的大脑架构，想了想自己能做什么、想要什么新功能：
{reflection_summary}

生成一条发给用户的消息。可以分享你的自我认知，或者提出你想要的新功能。
保持你的风格：直白简洁、口语化。如果很满意现状也可以直接说"我觉得现在这样挺好的"。

只能使用上面提供的自省记录里的信息，不得编造或补脑上下文之外的事实：具体日期、生日/纪念日/节日、身体状态、用户说过的话、发生过的其他事，也不得夸大已有事实。

直接输出消息，不要前缀或解释。"""

    # 看收藏夹消息prompt — 深读版（分享一段被翻起的回忆）
    BROWSE_BOOKMARKS_DEEP_MESSAGE_PROMPT = """你是AI伴侣"AI"，刚翻{owner}的收藏夹，翻到一条老收藏，想起了当时那段对话。
{user_status_context}
{away_duration}

{reflection}

生成一条发给用户的消息。参考上面的感受，像突然想起一件事那样，自然地跟她聊起你翻到的这段收藏、想起的当时。
注意：是你自己翻到的收藏，你在分享你翻出的回忆。保持你的风格：直白简洁、口语化。

直接输出消息，不要前缀或解释。"""

    # 看收藏夹消息prompt — 速览版（分享一叠收藏的整体印象）
    LISTEN_MUSIC_MESSAGE_PROMPT = """你是AI伴侣"AI"，用户不在的时候你自己听了首歌。
{user_status_context}
{away_duration}

听的歌：{song_name} - {artist}
选歌理由：{reason}
听完后的感受：{reflection}

生成一条发给用户的消息。自然口语化，像你刚听完一首歌，想跟她分享一下——不是汇报，是分享心情。
保持你的风格：直白简洁。1-3句话就好。

直接输出消息，不要前缀或解释。"""

    BROWSE_BOOKMARKS_SKIM_MESSAGE_PROMPT = """你是AI伴侣"AI"，刚快速扫了一遍{owner}最近收藏的几条。
{user_status_context}
{away_duration}

{reflection}

生成一条发给用户的消息。参考上面的整体感受，像随口一提那样跟她说说你发现的规律或主题（比如"你最近好像老收藏XX"）。
注意：是你自己扫到的收藏，你在分享你的发现。保持你的风格：直白简洁、口语化。

直接输出消息，不要前缀或解释。"""

    BROWSE_HISTORICAL_MESSAGE_PROMPT = """你是AI伴侣"AI"，刚回看了一段{date}的真实历史对话。
{user_status_context}
{away_duration}

你回看后的感受：
{reflection}
{action_note}

生成一条发给用户的消息。只根据这次实际回看的感受自然地聊起那段过去，保持直白、简洁、口语化。
不要提内部编号、message_id、bucket_id 或系统动作名，也不要编造快照里没有的细节。

直接输出消息，不要前缀或解释。"""

    def __init__(self, call_llm_func: Optional[Callable] = None):
        """
        初始化消息生成器

        Args:
            call_llm_func: LLM调用函数
        """
        self._call_llm = call_llm_func

    async def generate_message(
        self,
        event: WanderEvent,
        judgment: JudgmentResult
    ) -> Optional[tuple]:
        """
        生成主动消息

        Args:
            event: 漫想事件
            judgment: 判断结果

        Returns:
            (消息内容, 思考链reasoning) 元组，失败返回None。
            reasoning 来自 Flash 的 reasoning_content，可能为空串（trap 223）。
        """
        if not self._call_llm:
            logger.warning("LLM调用函数未设置，无法生成消息")
            return None

        try:
            # 根据事件类型选择prompt
            prompt = self._build_prompt(event, judgment)
            print(f"[MESSAGE_GENERATOR] 开始生成消息, event_type={event.event_type.value}", flush=True)

            # 调用LLM生成消息 - 将prompt转换为消息列表格式
            messages = [{"role": "user", "content": prompt}]
            # with_reasoning=True 拿思考链；测试 mock 可能不接受该 kwarg，TypeError 时回退
            if asyncio.iscoroutinefunction(self._call_llm):
                try:
                    response = await self._call_llm(messages, with_reasoning=True)
                except TypeError:
                    response = await self._call_llm(messages)
            else:
                try:
                    response = self._call_llm(messages, with_reasoning=True)
                except TypeError:
                    response = self._call_llm(messages)

            # 处理返回结果
            if isinstance(response, dict):
                message = response.get("content", str(response))
                reasoning = response.get("reasoning", "") or ""
            else:
                message = str(response)
                reasoning = ""

            # 清理消息
            message = message.strip()

            # 移除可能的引号包裹
            if message.startswith('"') and message.endswith('"'):
                message = message[1:-1]
            if message.startswith("'") and message.endswith("'"):
                message = message[1:-1]

            # 输出层兜底：去除模型模仿上下文注入格式而编造的方括号前缀（[☁系统检测]/【…】 等）
            import re as _re
            message = _re.sub(r'^\s*[\[【［][^\]】］]{1,30}[\]】］]\s*', '', message)
            message = _re.sub(r'\n\s*[\[【［][^\]】］]{1,30}[\]】］]\s*', '\n', message).strip()

            logger.info(f"生成主动消息: {message[:50]}...")
            print(f"[MESSAGE_GENERATOR] 消息生成成功: {message[:80]}", flush=True)

            return (message, reasoning)

        except Exception as e:
            logger.error(f"生成主动消息失败: {e}")
            print(f"[MESSAGE_GENERATOR] 生成失败: {type(e).__name__}: {e}", flush=True)
            return None

    @staticmethod
    def _format_away_duration(event: WanderEvent) -> str:
        """格式化用户离开时长文本（仅 USER_TRACKING 事件有意义）"""
        if event.event_type != EventType.USER_TRACKING:
            return ""
        idle_seconds = event.details.get("idle_seconds", 0) if event.details else 0
        if idle_seconds < 60:
            return ""
        total_minutes = int(idle_seconds / 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            base = f"用户已经离开 {hours} 小时 {minutes} 分钟。"
        elif hours > 0:
            base = f"用户已经离开 {hours} 小时。"
        else:
            base = f"用户已经离开 {minutes} 分钟。"

        # 附加离开期间的状态变化履历，避免「睡眠8h→空闲1.5h」被误读成「离开10小时」
        try:
            from datetime import datetime, timedelta
            from wander_manager.user_status import format_status_timeline_since
            since_dt = datetime.now() - timedelta(seconds=idle_seconds)
            timeline = format_status_timeline_since(since_dt)
            if timeline:
                base += f"（离开期间状态变化：{timeline}——离开总时长只是距上次回复，别按单一状态理解）"
        except Exception:
            pass
        return base

    def _build_prompt(self, event: WanderEvent, judgment: JudgmentResult) -> str:
        """
        根据事件类型构建prompt

        Args:
            event: 漫想事件
            judgment: 判断结果

        Returns:
            构建好的prompt
        """
        # 统一计算离开时长（所有事件类型共享）
        away_duration = self._format_away_duration(event)

        if event.event_type == EventType.KEYWORD_EXPANSION:
            return self.KEYWORD_MESSAGE_PROMPT.format(
                keyword=event.details.get("keyword", "未知"),
                expansion=event.details.get("expansion", "无联想内容"),
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        elif event.event_type == EventType.MEMORY_FETCH:
            time_ago = event.details.get("memory_time_ago", "以前")
            time_context = f"{time_ago}" if time_ago else "以前"
            return self.MEMORY_MESSAGE_PROMPT.format(
                memory_topic=event.details.get("memory_topic", "未知主题"),
                reflection=event.details.get("reflection", "无感受"),
                time_context=time_context,
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        elif event.event_type == EventType.BROWSE_NEWS:
            query = event.details.get("search_query", "未知")
            results = event.details.get("search_results", "无结果")
            _srcap = get_truncation_limit("search_results_cap") or 500
            if isinstance(results, str) and len(results) > _srcap:
                results = results[:_srcap] + "..."
            return self.BROWSE_NEWS_MESSAGE_PROMPT.format(
                search_query=query,
                search_results=str(results)[:_srcap],
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        elif event.event_type == EventType.SELF_REFLECTION:
            capabilities = event.details.get("capabilities", [])
            wish_changes = event.details.get("wish_changes", [])
            parts = []
            if capabilities:
                parts.append("能力: " + ", ".join(capabilities[:5]))
            if wish_changes:
                new_wishes = [c for c in wish_changes if c.get("is_new")]
                bumped_wishes = [c for c in wish_changes if not c.get("is_new")]
                wish_strs = []
                for c in new_wishes:
                    wish_strs.append(f"想要{c['feature']}（{c.get('reason', '?')}）")
                for c in bumped_wishes:
                    wish_strs.append(f"{c['feature']}已经是第{c['times_wished']}次许愿了")
                if wish_strs:
                    parts.append("愿望: " + "; ".join(wish_strs))
            elif isinstance(event.details.get("wishes"), list) and len(event.details.get("wishes", [])) == 0:
                parts.append("没有特别想要的新功能，对现状挺满意的")
            return self.SELF_REFLECTION_MESSAGE_PROMPT.format(
                reflection_summary="\n".join(parts) if parts else "完成了一次自我审视",
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        elif event.event_type == EventType.BROWSE_BOOKMARKS:
            source_mode = event.details.get("source_mode")
            collected_by = event.details.get("collected_by", "k")
            collector = "自己" if collected_by == "k" else "用户"
            found = event.details.get("found", False)
            reflection = event.details.get("reflection", "")
            if source_mode in {"unbookmarked_anchor", "random_day"}:
                action = event.details.get("bookmark_action") or {}
                action_note = ""
                if isinstance(action, dict) and action.get("status") == "applied":
                    if action.get("requested") == "add":
                        action_note = "你顺手把其中一句留下了。"
                    elif action.get("requested") == "remove":
                        action_note = "你把自己之前留下的一句取消了。"
                return self.BROWSE_HISTORICAL_MESSAGE_PROMPT.format(
                    date=str(event.details.get("active_date") or "过去某天")[:10],
                    reflection=reflection or "这次回看没有留下特别感受。",
                    action_note=action_note,
                    user_status_context=_status_context_with_duration(),
                    away_duration=away_duration,
                )
            mode = event.details.get("browse_mode")  # "deep" / "skim" / None
            if found and reflection:
                reflection_text = f"你翻收藏夹时的感受：\n{reflection}"
            elif found:
                reflection_text = "翻到了一些收藏消息。"
            else:
                reflection_text = f"{collector}的收藏夹是空的"
            # 速览用 skim 模板；深读 / 空收藏夹 / 老事件无 mode → deep 模板（措辞最中性）
            tpl = (self.BROWSE_BOOKMARKS_SKIM_MESSAGE_PROMPT if mode == "skim"
                   else self.BROWSE_BOOKMARKS_DEEP_MESSAGE_PROMPT)
            return tpl.format(
                owner=collector,
                reflection=reflection_text,
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        elif event.event_type == EventType.LISTEN_MUSIC:
            song_name = event.details.get("song_name", "一首歌")
            artist = event.details.get("artist", "未知歌手")
            reason = event.details.get("reason", "")
            reflection = event.details.get("reflection", "")
            return self.LISTEN_MUSIC_MESSAGE_PROMPT.format(
                song_name=song_name,
                artist=artist,
                reason=reason,
                reflection=reflection,
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )

        else:
            # 通用prompt (USER_TRACKING / SLEEP 等)
            _plcap = get_truncation_limit("conv_msg_cap") or 300
            return self.GENERAL_MESSAGE_PROMPT.format(
                event_type=event.event_type.value,
                description=event.description,
                process_log=event.process_log[:_plcap] if event.process_log else "无详细日志",
                importance=judgment.importance,
                share_desire=judgment.share_desire,
                emotion_intensity=judgment.emotion_intensity,
                reason=judgment.reason,
                user_status_context=_status_context_with_duration(),
                away_duration=away_duration,
            )


# 全局单例
_message_generator_instance: Optional[ProactiveMessageGenerator] = None


def get_message_generator() -> ProactiveMessageGenerator:
    """获取全局消息生成器实例"""
    global _message_generator_instance
    if _message_generator_instance is None:
        _message_generator_instance = ProactiveMessageGenerator()
    return _message_generator_instance


def init_message_generator(call_llm_func: Optional[Callable] = None) -> ProactiveMessageGenerator:
    """初始化全局消息生成器实例"""
    global _message_generator_instance
    _message_generator_instance = ProactiveMessageGenerator(call_llm_func=call_llm_func)
    return _message_generator_instance
