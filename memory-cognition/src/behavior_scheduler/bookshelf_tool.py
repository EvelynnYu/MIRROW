# 书柜工具（bookshelf）— Agent 主动翻看自己的生活档案（只读）
#
# source 分区：diary / calendar / events / bookmarks / conversation / open_loops。
# 带 query 时可限定分区，也可 source=all 跨书柜做混合检索；不带 query
# 时保留原来的确定性浏览方式。
# 全部只读查询，绝不 touch、绝不写库。内容全量返回不截断。
#
# 触发（供 Flash/Pro 识别）：翻日记 / 看看收藏 / 翻对话 / 翻事件 等"查/翻生活档案"类说法

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any, Dict

from .base_tool import BaseTool, ToolResult, ToolStatus
from message_roles import is_ui_only_role

logger = logging.getLogger(__name__)


def _strip_diary_todo(content: str) -> str:
    """从日记 content 剥离待办段，保留正文 + 「要记住的事」。

    日记正文由 Flash 生成，段落结构是 LLM emergent 不固定（实测 120 条 4 种形态）：
    - 两段式：'今天需要记住的事：' + '---' + '今天需要记住的待办：'
    - 单段带'待办部分'：'今天需要记住的事，待办部分：'（整段即待办段）
    - 单段纯'要记住的事'：'今天需要记住的事：'
    - 纯叙事无标记（26/120）
    规则：只保留正文 + 「要记住的事」段，剥离含「待办」的独立段。
    """
    lines = content.split("\n")
    # 1. 去前导格式化标题（'# YYYY-MM-DD 星期X' / '## 日记' / '## 日记正文'）
    body = [ln for ln in lines if not ln.startswith("#")]
    text = "\n".join(body)

    # 2. 找「今天需要记住…」标题行（行首 + 冒号结尾的宽松匹配，覆盖所有变体）
    title_re = re.compile(r"^今天需要记住.+[：:]")
    title_idx = None
    for i, ln in enumerate(body):
        if title_re.match(ln.strip()):
            title_idx = i
            break
    if title_idx is None:
        return text  # 纯叙事，整段即正文

    # 3. 正文 = 标题行之前
    main = "\n".join(body[:title_idx]).strip()

    # 4. 编号段内按 '---' 切段，逐段判断保留/剥离
    segs, cur = [], []
    for ln in body[title_idx:]:
        if ln.strip() == "---":
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        segs.append(cur)

    keep = []
    for seg in segs:
        seg_text = "\n".join(seg).strip()
        if not seg_text:
            continue
        first_line = seg[0].strip()
        # 第一段标题不含「待办」→ 要记住的事，保留；含「待办」（含单段'待办部分'变体）→ 剥离
        if "待办" in first_line:
            continue
        keep.append(seg_text)

    result = main
    if keep:
        result = (main + "\n\n" if main else "") + "\n\n".join(keep)
    return result.strip()


def _msg_label(m: Dict[str, Any]) -> str:
    """对话回放的消息来源标注。漫想/哨兵/提醒/音乐是 Agent 的行为或感知，保留并标注。"""
    if m.get("is_wander"):
        return "[Agent💭]"
    if m.get("is_sentinel"):
        return "[Agent🛡️]"
    if m.get("is_reminder"):
        return "[Agent⏰]"
    mid = m.get("message_id") or ""
    if mid.startswith("music_"):
        return "[音乐]"
    role = m.get("role")
    if role == "user":
        return "[人类伙伴]"
    if role == "system":
        return "[群聊摘要]"
    return "[Agent]"


class BookshelfTool(BaseTool):
    """书柜：Agent 主动查询自己的生活档案（只读）"""

    name = "bookshelf"

    description = (
        "只读回看生活档案。自动记忆召回没有想起、相似往事很多或需要枚举时，"
        "用 source=memory 按大意深搜 Memory V2 的事件/线程与认知摘要；需要核对"
        "原话、原因或过程时再设 detail=true。跨多个时段时可明确列出起点与后续短线索，"
        "同日候选不等于已证实因果。已知日期的整段原始对话、收藏、日记、"
        "纪念日、重要事件和已结束开放线索仍按原分区浏览。"
    )
    flash_description = (
        "深搜记忆摘要并按需展开原话，或按日期/分区翻生活档案；"
        "记忆深搜的 query 只保留问题本身，不复制另列的线索；"
        "明确列出的多条线索按序提取为 stage_queries"
    )
    pro_guide = (
        "本轮自动召回是有界候选，不代表全部历史。没有想起来、候选含混、存在多次相似"
        "经历或人类伙伴要求列举时，用 source=memory + query 主动深搜同一套 Memory V2 摘要；"
        "跨时段回忆需要分开查时，在 @bookshelf 动作中保留完整问题，另用清晰分隔的"
        "2—4 条短检索线索按时间顺序列出起点与后续；未列出时只按完整问题深搜。"
        "只有确实需要原话、原因或过程才加 detail=true。source=conversation 留给已知日期的"
        "原始对话浏览或摘要仍无命中时的最后兜底。"
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["all", "memory", "diary", "calendar", "events", "bookmarks", "conversation", "open_loops"],
                "description": "memory=主动深搜 Memory V2 摘要；其他值为旧档案分区；默认 all",
            },
            "query": {
                "type": "string", "maxLength": 300,
                "description": (
                    "source=memory 时填写问题本身，即使有 stage_queries 也保留；"
                    "不复制动作前缀或另列的线索清单。其他分区浏览可省略"
                ),
            },
            "stage_queries": {
                "type": "array",
                "items": {"type": "string", "maxLength": 120},
                "minItems": 2,
                "maxItems": 4,
                "description": (
                    "可选，仅动作明确列出多条检索线索时原样提取；"
                    "按时间顺序排列，第一条是起点，其余是后续。未列出则省略"
                ),
            },
            "detail": {
                "type": "boolean",
                "description": "仅 source=memory：是否附带首个命中事件的一处精确原话邻窗，默认 false",
            },
            "date": {"type": "string", "description": "日期 YYYY-MM-DD（diary 必选 / conversation 查某天对话）"},
            "year": {"type": "integer", "description": "年份（calendar 列出某年纪念日，默认今年）"},
            "month": {"type": "string", "description": "月份 YYYY-MM（calendar 细化到月，可选）"},
            "collected_by": {"type": "string", "description": "收藏来源（bookmarks 过滤，user/k）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "浏览或搜索返回上限"},
            "min_score": {"type": "number", "minimum": 0, "maximum": 100, "description": "搜索强相关分数线，默认55；不是事实概率"},
            "session_id": {"type": "string", "description": "会话ID（conversation 翻指定会话，替代按日期）"},
        },
        "required": [],
    }

    @property
    def input_schema(self) -> Dict[str, Any]:
        return self.parameters_schema

    def get_user_facing_description(self, **kwargs) -> str:
        labels = {
            "memory": "正在深度回忆...",
            "diary": "正在翻书柜·日记...",
            "calendar": "正在翻书柜·纪念日...",
            "events": "正在翻书柜·重要事件...",
            "bookmarks": "正在翻书柜·收藏夹...",
            "conversation": "正在翻书柜·对话...",
            "open_loops": "正在翻书柜·开放线索...",
        }
        return labels.get(kwargs.get("source", ""), "正在打开书柜...")

    async def execute(self, **kwargs) -> ToolResult:
        try:
            source = kwargs.get("source", "")
            query = str(kwargs.get("query") or "").strip()
            if kwargs.get("stage_queries") is not None:
                from memory_v2.active_query_plan import plan_from_stage_queries

                stage_plan = plan_from_stage_queries(kwargs.get("stage_queries"))
                if stage_plan is not None:
                    source = source or "memory"
                    if source == "memory" and not query:
                        query = "；".join(stage_plan.queries)[:300]
            if query:
                return await self._search({**kwargs, "source": source or "all", "query": query})
            if source == "diary":
                return await self._diary(kwargs)
            if source == "calendar":
                return await self._calendar(kwargs)
            if source == "events":
                return await self._events(kwargs)
            if source == "bookmarks":
                return await self._bookmarks(kwargs)
            if source == "conversation":
                return await self._conversation(kwargs)
            if source == "open_loops":
                return await self._open_loops(kwargs)
            return ToolResult(
                status=ToolStatus.ERROR, content="",
                error=("翻书柜需要 query（按大意搜索）或 source（按分区浏览）；"
                       "source 可选 all/memory/diary/calendar/events/bookmarks/conversation/open_loops"))
        except Exception as e:
            logger.exception("bookshelf 失败")
            return ToolResult(status=ToolStatus.ERROR, content="", error=f"书柜查询失败: {str(e)}")

    async def _search(self, kw: Dict[str, Any]) -> ToolResult:
        """跨档案混合搜索；原始档案只读，向量仅写可重建的旁路缓存。"""
        source = str(kw.get("source") or "all")
        if source == "memory":
            return await self._memory_search(kw)

        from event_chronicle import get_global_chronicle
        from .bookshelf_search import (
            SearchDocument, get_bookshelf_search_service, sanitize_archive_text,
        )

        base_sources = {"conversation", "bookmarks", "events", "diary"}
        sources = base_sources if source == "all" else ({source} & base_sources)
        extras = []
        if source in {"all", "calendar"}:
            try:
                from calendar_manager.manager import get_global_calendar_manager

                year = int(kw.get("year") or datetime.now().year)
                for event in get_global_calendar_manager().list_events(year=year):
                    if not getattr(event, "is_active", False) or event.event_type != "memorial":
                        continue
                    extras.append(SearchDocument(
                        id=f"calendar:{event.id}", source="calendar",
                        text=sanitize_archive_text(
                            f"{event.title} {event.description} {event.emotion}", limit=1200
                        ),
                        date=event.date_string, metadata={"calendar_event_id": event.id},
                    ))
            except Exception as exc:
                logger.info("bookshelf calendar search source unavailable: %s", type(exc).__name__)
        if source in {"all", "open_loops"}:
            try:
                from open_loops import get_open_loop_store

                for item in get_open_loop_store().list_resolved(limit=500):
                    extras.append(SearchDocument(
                        id=f"open_loops:{item.get('id', '')}", source="open_loops",
                        text=sanitize_archive_text(item.get("content"), limit=1200),
                        date=str(item.get("resolved_at") or "")[:10],
                        metadata={"open_loop_id": item.get("id", "")},
                    ))
            except Exception as exc:
                logger.info("bookshelf open-loop search source unavailable: %s", type(exc).__name__)

        chronicle = get_global_chronicle()
        service = get_bookshelf_search_service(chronicle.db_path)
        try:
            from .execution_context import get_request_user_message

            current_request = get_request_user_message()
        except Exception:
            current_request = ""
        raw_min_score = kw.get("min_score")
        min_score = 55.0 if raw_min_score is None else float(raw_min_score)
        result = await service.search(
            kw["query"], sources=sources,
            limit=max(1, min(int(kw.get("limit") or 5), 10)),
            min_score=max(0.0, min(min_score, 100.0)),
            extra_documents=extras,
            exclude_text=current_request,
        )
        hits = result.get("hits") or []
        if not hits:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=(f"书柜中没有足以确认与「{sanitize_archive_text(kw['query'], limit=80)}」"
                         "相关的记录；已尝试关键词、模糊和语义检索。"),
                extra_data={"search_query": result.get("query", ""), "search_results": []},
            )

        weak = bool(result.get("used_weak_fallback"))
        heading = "没有强匹配，以下是最接近的候选" if weak else "找到这些相关档案"
        lines = [
            f"🔎 {heading}（匹配分是排序分，不是事实概率；共 {len(hits)} 条）:"
        ]
        tier_label = {"high": "高相关", "possible": "可能相关", "weak": "弱相关"}
        for index, hit in enumerate(hits, 1):
            source_label = hit.get("source_label") or hit.get("source") or "档案"
            date = hit.get("date") or "日期未知"
            score = float(hit.get("match_score") or 0)
            tier = tier_label.get(hit.get("match_tier"), "可能相关")
            content = sanitize_archive_text(hit.get("content"), limit=900)
            matched = "、".join(hit.get("matched_terms") or [])
            reason = f"；命中：{matched}" if matched else "；主要由语义相似召回"
            lines.append(
                f"\n{index}. [{source_label} · {date} · {tier} {score:.0f}分{reason}]\n{content}"
            )
        if not result.get("semantic_available"):
            lines.append("\n注：本次语义索引不可用，结果由关键词和模糊匹配降级产生。")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content="\n".join(lines),
            extra_data={
                "search_query": result.get("query", ""),
                "semantic_available": bool(result.get("semantic_available")),
                "used_weak_fallback": weak,
                "search_results": hits,
            },
        )

    async def _memory_search(self, kw: Dict[str, Any]) -> ToolResult:
        """Actively search the same Memory V2 summaries used by auto recall."""

        from .bookshelf_search import sanitize_archive_text
        from memory_v2.active_query_plan import plan_from_stage_queries
        from memory_v2.context_shadow import search_memory_v2_from_environment
        from time_utils import now_date

        try:
            from .execution_context import get_request_identity

            request_session_id, request_message_id, request_turn_id = get_request_identity()
        except Exception:
            request_session_id, request_message_id, request_turn_id = "", "", ""

        requested_limit = max(1, min(int(kw.get("limit") or 8), 10))
        query_text = str(kw.get("query") or "")
        active_plan = plan_from_stage_queries(kw.get("stage_queries"))
        stage_observation: dict[str, object] = {
            "source": (
                "agent_action" if active_plan is not None
                else "invalid" if kw.get("stage_queries") is not None
                else "none"
            ),
            "branch_count": len(active_plan.queries) if active_plan else 0,
        }
        observation, text = await asyncio.to_thread(
            search_memory_v2_from_environment,
            query_text,
            reference_date=date.fromisoformat(now_date()),
            limit=requested_limit,
            include_source_detail=bool(kw.get("detail", False)),
            session_id=request_session_id,
            current_message_id=request_message_id,
            turn_id=request_turn_id,
            active_plan=active_plan,
            stage_metrics=stage_observation,
        )
        observation = {**observation, "stage_search": stage_observation}
        status = str(observation.get("status") or "error")
        if text:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=text,
                extra_data={"memory_v2_search": observation},
            )

        query = sanitize_archive_text(kw.get("query"), limit=80)
        if status == "no_hits":
            message = (
                f"你在已有的事件、长期认识和可追溯摘要里，没有找到足以确认与「{query}」"
                "相关的记录。可以换一种更具体的大意再想，或在知道日期时翻看那天的原始对话。"
            )
        elif status == "warming":
            message = "你的记忆索引正在更新，这一次暂时没有翻到可确认的材料。"
        elif status in {"disabled", "not_configured"}:
            message = "这一次没有可用的深度回忆入口，因此没有返回未经确认的替代内容。"
        else:
            message = "这一次深度回忆没有成功读取，因此没有返回未经核验的替代内容。"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=message,
            extra_data={"memory_v2_search": observation},
        )

    async def _diary(self, kw: Dict[str, Any]) -> ToolResult:
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        date = kw.get("date", "")
        if not date:
            return ToolResult(status=ToolStatus.ERROR, content="", error="日记需要指定日期 date=YYYY-MM-DD")
        d = chronicle.get_diary_entry_by_date(date)
        if not d:
            return ToolResult(status=ToolStatus.SUCCESS, content=f"{date} 那天没有日记")
        content = _strip_diary_todo(d.content)
        return ToolResult(status=ToolStatus.SUCCESS, content=f"📔 {d.date} 的日记:\n{content}")

    async def _calendar(self, kw: Dict[str, Any]) -> ToolResult:
        from calendar_manager.manager import get_global_calendar_manager
        from calendar_manager import database as db
        mgr = get_global_calendar_manager()
        year = kw.get("year") or datetime.now().year
        events = mgr.list_events(year=year)
        events = [ev for ev in events if ev.event_type == "memorial" and ev.is_active]
        lines = []
        if events:
            lines.append(f"🗓 {year} 年日历事件:")
            for ev in events:
                em = f"（{ev.emotion}）" if getattr(ev, "emotion", "") else ""
                # 纪念日是虚拟年 0000-MM-DD，真实年是 YYYY-MM-DD，取 [5:] 都得 MM-DD
                lines.append(f"  {ev.date_string[5:]} {ev.title}{em}")
        # 补充：今天的纪念日/生理期（只读，不写 last_scanned_at）
        try:
            today_mem = db.get_memorial_events_for_today()
            if today_mem:
                lines.append("今天: " + "、".join(ev.title for ev in today_mem))
            pi = db.get_period_info_today()
            if pi and pi.get("active"):
                lines.append(f"今天生理期第 {pi.get('day_number')} 天")
        except Exception:
            pass
        if not lines:
            return ToolResult(status=ToolStatus.SUCCESS, content="日历上还没有纪念日")
        return ToolResult(status=ToolStatus.SUCCESS, content="\n".join(lines))

    async def _events(self, kw: Dict[str, Any]) -> ToolResult:
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        events = chronicle.list_important_events(limit=100)
        if not events:
            return ToolResult(status=ToolStatus.SUCCESS, content="事件纪里还没有重要事件")
        lines = [f"⭐ 事件纪重要事件（共{len(events)}条）:"]
        for e in events:
            lines.append(f"  {e.date_string} {e.event_text}" + (f"（{e.emotion}）" if e.emotion else ""))
        return ToolResult(status=ToolStatus.SUCCESS, content="\n".join(lines))

    async def _bookmarks(self, kw: Dict[str, Any]) -> ToolResult:
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        items, total = chronicle.list_bookmarks(
            collected_by=kw.get("collected_by"), offset=0, limit=kw.get("limit", 50))
        if not items:
            return ToolResult(status=ToolStatus.SUCCESS, content="收藏夹还是空的")
        who = {"user": "人类伙伴", "agent": "Agent"}.get(kw.get("collected_by"), "全部")
        lines = [f"🔖 收藏夹（{who}，共 {total} 条）:"]
        for it in items:
            content = (it.get("content") or "").strip()
            created = (it.get("created") or "")[:10]
            if content:
                lines.append(f"  - {content}（{created}）")
            else:  # 旧书签无内容快照，回退显示原消息 ID
                mid = it.get("original_msg_id") or ""
                lines.append(f"  - [无内容快照] 原消息 {mid}（{created}）")
        return ToolResult(status=ToolStatus.SUCCESS, content="\n".join(lines))

    async def _conversation(self, kw: Dict[str, Any]) -> ToolResult:
        from event_chronicle import get_global_chronicle
        chronicle = get_global_chronicle()
        date = kw.get("date", "")
        session_id = kw.get("session_id", "")
        if session_id:
            msgs = chronicle.get_messages_by_session_id(session_id, limit=500, desc=False)
            label = f"会话 {session_id}"
        elif date:
            msgs = chronicle.get_messages_by_date(date)
            label = date
        else:
            sids = chronicle.get_recent_active_sessions(limit=1)
            if not sids:
                return ToolResult(status=ToolStatus.SUCCESS, content="还没有对话记录")
            msgs = chronicle.get_messages_by_session_id(sids[0], limit=500, desc=False)
            label = "最近一次对话"
        # 只滤 UI-only 标记 + 空 content，其余全保留（漫想/哨兵/提醒/system 是对话流一部分）
        msgs = [m for m in msgs
                if not is_ui_only_role(m.get("role")) and (m.get("content") or "").strip()]
        if not msgs:
            return ToolResult(status=ToolStatus.SUCCESS, content=f"{label} 没有对话内容")
        lines = [f"📅 {label} 的对话:"]
        for m in msgs:
            lines.append(f"{_msg_label(m)} {m.get('content')}")
        return ToolResult(status=ToolStatus.SUCCESS, content="\n".join(lines))

    async def _open_loops(self, kw: Dict[str, Any]) -> ToolResult:
        from open_loops import get_open_loop_store

        items = get_open_loop_store().list_resolved(limit=kw.get("limit", 100))
        if not items:
            return ToolResult(status=ToolStatus.SUCCESS, content="还没有已经结束的开放线索")
        lines = [f"🗂 Agent 已结束的开放线索（共 {len(items)} 条）:"]
        for item in items:
            ended = (item.get("resolved_at") or "")[:16].replace("T", " ")
            lines.append(f"  - [{item['id']}] {item['content']}（结束于 {ended}）")
        return ToolResult(status=ToolStatus.SUCCESS, content="\n".join(lines))
