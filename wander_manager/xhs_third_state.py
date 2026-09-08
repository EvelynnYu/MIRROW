"""Third-state, user-gated access to the read-only XHS Wander event.

The normal tool catalogue contains one deliberately generic proxy.  It never
mentions the capability it fronts, and it is useful only while the current
message has an in-memory, single-use grant.  The grant is request state rather
than chat content: it is bound to the exact session/message identity and is
consumed atomically before the real v3 event is entered.

This module is intentionally small.  It does not contain ADB operations or a
second XHS implementation; ``XhsThirdStateTool`` delegates to the existing
Wander v3 runner through ``WanderManager.browse_xhs_from_chat``.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from behavior_scheduler.base_tool import BaseTool, ToolResult, ToolStatus


XHS_CAPABILITY = "browse_xiaohongshu"
THIRD_STATE_TOOL_NAME = "third_state_capability"
GRANT_TTL_SECONDS = 300.0
MAX_QUERY_CHARS = 60
DEFAULT_TARGET_COUNT = 3
MAX_TARGET_COUNT = 5
XHS_GATE_FAILURE_MESSAGE = "本轮没有可执行的只读浏览请求。"


@dataclass(frozen=True)
class XhsGrant:
    """Opaque, short-lived grant held only in the current process."""

    token: str
    session_id: str
    message_id: str
    turn_id: str
    capability: str
    issued_at: float
    expires_at: float
    max_uses: int = 1
    uses: int = 0
    allowed_intents: tuple[str, ...] = ("recommend",)
    bound_query: str = ""
    # The model never supplies this value.  It is derived from the original
    # user sentence and travels only in request-local backend state.
    target_count: int = DEFAULT_TARGET_COUNT
    # This is an authorization fact for the backend handoff only.  It is not a
    # model/tool argument and never contains a draft supplied by the model.
    comment_requested: bool = False
    max_query_chars: int = MAX_QUERY_CHARS

    @property
    def count(self) -> int:
        """Compatibility alias for callers that call the goal ``count``."""
        return self.target_count

    @property
    def goal_count(self) -> int:
        """Name used by runtime callers when describing a count goal."""
        return self.target_count


@dataclass(frozen=True)
class XhsGrantIssue:
    """Safe result of matching one user message.

    The opaque token is deliberately not exposed here.  The scheduler keeps
    the grant in a context variable and the proxy consumes it internally.
    """

    grant: Optional[XhsGrant] = None
    dynamic_fact: str = ""
    matched: bool = False


@dataclass(frozen=True)
class XhsGrantUse:
    grant: XhsGrant
    # Kept as an explicit empty envelope for callers that log a consumed
    # grant.  Intent/query are read from ``grant`` below, never from this
    # model-supplied payload.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class _IntentMatch:
    intent: str
    query: str = ""


class XhsCapabilityGate:
    """Deterministic matcher plus an atomic in-memory single-use grant store."""

    _PLATFORM_MARKERS = ("小红书", "xhs", "xiaohongshu")
    # A platform and its action are often separated by a short status clause
    # in ordinary Chinese (for example, "小红书权限问题说是可以了，你去刷刷").
    # Keep this matcher bounded and clause-aware; it is not a general intent
    # classifier and must not turn an unrelated later sentence into a grant.
    _NONCONTIGUOUS_MAX_GAP = 48
    _ACTION_PATTERNS = (
        ("search", r"(?:搜索|搜(?:一下|下)?|找找|找一下|查(?:一下|下)?)"),
        ("read", r"(?:读(?:帖|一下)?|阅读|读取(?:一下)?|看帖子|打开帖子)"),
        ("recommend", r"(?:刷+|浏览|看+)"),
    )
    _SEARCH_MARKERS = ("搜索", "搜一下", "搜下", "搜", "找一下", "找找", "查一下")
    # "看小红书" is a feed/recommendation request, not a read of one
    # identified post.  Keep it out of this tuple because it is a substring of
    # the common "看看小红书" phrasing.
    _READ_MARKERS = (
        "读帖", "读一下", "读小红书", "读一下小红书", "看帖子", "看一下帖子",
        "读取小红书", "读取一下小红书", "阅读小红书", "打开帖子",
    )
    _RECOMMEND_MARKERS = (
        "推荐小红书", "推荐帖子", "推荐几个帖子", "刷帖", "刷帖子", "刷小红书",
        "刷一下小红书", "刷刷小红书", "浏览小红书", "看看小红书", "看一下小红书",
        "看小红书", "打开小红书", "小红书看看", "小红书上看看",
        "小红书看一下", "小红书上看一下", "小红书刷", "小红书上刷",
        "小红书浏览", "小红书上浏览",
    )
    _COMMENT_MARKERS = (
        "评论", "留言", "写评论", "写个评论", "帮我评论", "替我评论",
    )
    _COMMENT_NEGATION_MARKERS = (
        "不要评论", "别评论", "不评论", "不用评论", "无需评论", "不发评论",
    )
    _NEGATION_MARKERS = ("不看小红书", "别看小红书", "不用看小红书", "不要看小红书", "不想看小红书")
    _GENERIC_QUERY_MARKERS = frozenset({"帖子", "几个帖子", "一下", "下"})
    _COUNT_WORDS = {
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "１": 1,
        "２": 2,
        "３": 3,
        "４": 4,
        "５": 5,
    }

    def __init__(self, *, clock: Callable[[], float] = time.time, ttl_seconds: float = GRANT_TTL_SECONDS):
        self._clock = clock
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._grants: dict[str, XhsGrant] = {}

    def issue(
        self,
        *,
        session_id: str,
        message_id: str,
        user_message: str,
        turn_id: str = "",
    ) -> XhsGrantIssue:
        """Issue a grant only for an explicit, high-confidence XHS intent.

        Matching is intentionally conservative.  Generic words such as
        ``推荐``/``看看`` never authorize this capability by themselves.
        Issuing a grant performs no device action.
        """

        sid = str(session_id or "").strip()
        mid = str(message_id or "").strip()
        tid = str(turn_id or mid).strip()
        text = " ".join(str(user_message or "").split()).strip()
        match = self._match(user_message)
        if not sid or not mid or not tid or match is None:
            return XhsGrantIssue(matched=False)

        now = float(self._clock())
        grant = XhsGrant(
            token=secrets.token_urlsafe(32),
            session_id=sid,
            message_id=mid,
            turn_id=tid,
            capability=XHS_CAPABILITY,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
            # The grant is scoped to the concrete intent detected in this
            # message.  A later model/tool payload cannot silently turn a
            # search grant into a recommendation/read grant.
            allowed_intents=(match.intent,),
            bound_query=self._normalize_query(match.query),
            target_count=self._target_count(text, match.intent),
            comment_requested=self._has_comment_request(text=user_message),
        )
        with self._lock:
            self._purge_locked(now)
            self._grants[grant.token] = grant
        # Keep this fact short and objective.  It is appended to the request's
        # dynamic tail only; no token, raw user text, tool name, JSON or
        # model-facing parameter tutorial is included.
        query = self._safe_fact_text(grant.bound_query) or "未指定主题"
        # This tail is visible to AI.  Keep it as plain objective device facts;
        # grant/replay vocabulary, protocol names, and model argument names
        # stay entirely inside the backend boundary.
        fact = (
            "AI的专用小红书手机/行动设备可用于本轮只读浏览；"
            f"设备侧绑定主题为“{query}”，目标{grant.target_count}篇；"
            "与用户当前使用手机分离；尚未执行本轮浏览。"
        )
        return XhsGrantIssue(grant=grant, dynamic_fact=fact, matched=True)

    def is_available(
        self,
        grant: Optional[XhsGrant],
        *,
        session_id: str,
        message_id: str,
        turn_id: str,
        capability: str = XHS_CAPABILITY,
    ) -> bool:
        """Check a grant without consuming it.

        This is deliberately a read-only, lock-protected check used by the
        grant-aware Flash completer.  It must not turn a preview/other tool
        round into a consume operation.
        """

        if grant is None:
            return False
        token = str(grant.token or "")
        now = float(self._clock())
        with self._lock:
            self._purge_locked(now)
            stored = self._grants.get(token)
            if stored is None or now >= stored.expires_at:
                return False
            return (
                stored.session_id == str(session_id or "")
                and stored.message_id == str(message_id or "")
                and stored.turn_id == str(turn_id or "")
                and stored.capability == str(capability or "")
                and stored.uses < stored.max_uses
            )

    def consume(
        self,
        grant: Optional[XhsGrant],
        *,
        session_id: str,
        message_id: str,
        turn_id: str,
        capability: str,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> XhsGrantUse:
        """Atomically validate and spend one grant.

        The token is looked up by identity inside the store.  All identity and
        capability/parameter checks occur while holding the same lock as the
        use increment, so two concurrent tool calls cannot both succeed.
        """

        # The proxy is deliberately zero-parameter.  An empty transport
        # envelope is harmless, but any caller-supplied value is rejected
        # before looking up or spending the grant.
        if parameters is not None and (
            not isinstance(parameters, Mapping) or bool(parameters)
        ):
            raise PermissionError("capability_parameters_forbidden")
        if grant is None:
            raise PermissionError("capability_grant_missing")
        token = str(grant.token or "")
        now = float(self._clock())
        with self._lock:
            self._purge_locked(now)
            stored = self._grants.get(token)
            if stored is None:
                raise PermissionError("capability_grant_invalid_or_replayed")
            if stored.session_id != str(session_id or "") or stored.message_id != str(message_id or ""):
                raise PermissionError("capability_grant_message_mismatch")
            if stored.turn_id != str(turn_id or ""):
                raise PermissionError("capability_grant_turn_mismatch")
            if stored.capability != str(capability or ""):
                raise PermissionError("capability_grant_capability_mismatch")
            if now >= stored.expires_at:
                self._grants.pop(token, None)
                raise PermissionError("capability_grant_expired")
            if stored.uses >= stored.max_uses:
                self._grants.pop(token, None)
                raise PermissionError("capability_grant_replayed")
            # Consume before the external operation.  A device/process crash
            # therefore cannot turn a single grant into an implicit retry.
            self._grants.pop(token, None)
            return XhsGrantUse(grant=stored, parameters={})

    def revoke(self, grant: Optional[XhsGrant]) -> None:
        if grant is None:
            return
        with self._lock:
            self._grants.pop(str(grant.token or ""), None)

    def _purge_locked(self, now: float) -> None:
        expired = [token for token, grant in self._grants.items() if now >= grant.expires_at]
        for token in expired:
            self._grants.pop(token, None)

    @classmethod
    def _match(cls, value: Any) -> Optional[_IntentMatch]:
        text = " ".join(str(value or "").split()).strip()
        lowered = text.lower()
        if not text or not any(marker in lowered for marker in cls._PLATFORM_MARKERS):
            return None
        if any(marker in text for marker in cls._NEGATION_MARKERS) or cls._has_negation(text):
            return None

        # Do this before the literal marker tables.  A separated form such as
        # "小红书权限好了，帮我搜索猫咪" still contains the literal "搜索",
        # but extracting from the platform tail would incorrectly bind
        # "权限好了，帮我搜索猫咪" as the topic.  The bounded matcher also
        # requires an execution cue, so permission/status discussion alone does
        # not open the capability.
        separated = cls._match_non_contiguous(text)
        if separated is not None:
            return separated

        # Search/read markers are checked before recommendation markers because
        # a message such as “搜索小红书并推荐几个” has a concrete query intent.
        if any(marker in text for marker in cls._SEARCH_MARKERS):
            query = cls._extract_query(text, cls._SEARCH_MARKERS, intent="search")
            return _IntentMatch("search", "" if query in cls._GENERIC_QUERY_MARKERS else query)
        if any(marker in text for marker in cls._READ_MARKERS):
            query = cls._extract_query(text, cls._READ_MARKERS, intent="read")
            return _IntentMatch("read", "" if query in cls._GENERIC_QUERY_MARKERS else query)
        if any(marker in text for marker in cls._RECOMMEND_MARKERS):
            query = cls._extract_query(text, cls._RECOMMEND_MARKERS, intent="recommend")
            return _IntentMatch("recommend", "" if query in cls._GENERIC_QUERY_MARKERS else query)
        # Allow a quantity between the natural verb and the platform name,
        # e.g. "看一篇小红书" / "读两篇小红书".  The ordinary marker table
        # intentionally stays literal so broad conversational uses do not
        # grant access accidentally.
        counted_action = re.search(
            r"(?P<verb>读|阅读|看+|刷+|浏览)\s*"
            r"(?:(?:[0-9０-９]+|[一两二三四五六七八九十])\s*"
            r"(?:篇|个|则|条)\s*)?"
            r"(?:小红书|xiaohongshu|xhs)",
            text,
            flags=re.IGNORECASE,
        )
        if counted_action:
            intent = "read" if counted_action.group("verb") in {"读", "阅读"} else "recommend"
            return _IntentMatch(intent, "")
        return None

    @classmethod
    def _match_non_contiguous(cls, text: str) -> Optional[_IntentMatch]:
        """Match one explicit platform/action request with a bounded gap.

        The regular literal markers above intentionally remain the fast path.
        This fallback covers natural status/request clauses where the words do
        not touch.  It only considers one platform occurrence and one action in
        the same short clause (or a question followed by an explicit command),
        then binds search/read topics from the action tail rather than the whole
        sentence.
        """

        platforms = list(re.finditer(r"(?:小红书|xiaohongshu|xhs)", text, flags=re.IGNORECASE))
        if not platforms:
            return None

        action_matches: list[tuple[str, re.Match[str]]] = []
        for intent, pattern in cls._ACTION_PATTERNS:
            action_matches.extend(
                (intent, match)
                for match in re.finditer(pattern, text, flags=re.IGNORECASE)
            )
        # Search/read take precedence over recommendation when an action span
        # overlaps (the ordinary marker path follows the same ordering).
        action_matches.sort(key=lambda item: (item[1].start(), cls._intent_rank(item[0])))

        for intent, action in action_matches:
            if not cls._action_is_explicit(text, action, intent):
                continue
            for platform in platforms:
                if platform.end() <= action.start():
                    gap = text[platform.end():action.start()]
                    if not cls._same_request_gap_allowed(text, gap, action, intent):
                        continue
                    query = cls._extract_query_after_action(text, action, intent)
                    return _IntentMatch(intent, query)
                if action.end() <= platform.start():
                    gap = text[action.end():platform.start()]
                    if not cls._same_request_gap_allowed(text, gap, action, intent):
                        continue
                    query = cls._extract_query_after_action(text, action, intent)
                    return _IntentMatch(intent, query)
        return None

    @staticmethod
    def _intent_rank(intent: str) -> int:
        return {"search": 0, "read": 1, "recommend": 2}.get(intent, 9)

    @classmethod
    def _same_request_gap_allowed(
        cls,
        text: str,
        gap: str,
        action: re.Match[str],
        intent: str,
    ) -> bool:
        if not gap or len(gap) > cls._NONCONTIGUOUS_MAX_GAP:
            return False
        # A full stop/semicolon is a hard request boundary.  A question mark
        # may join a capability check to an explicit follow-up command, which
        # is the natural form used by the chat path ("能刷不？能刷你就去刷刷").
        if re.search(r"[。！!；;]", gap):
            return False
        if re.search(r"[？?]", gap):
            if not cls._has_capability_check_before(text, action.start()):
                return False
            return cls._has_strong_execution_cue(text, action, intent)
        return True

    @staticmethod
    def _has_capability_check_before(text: str, action_start: int) -> bool:
        prefix = text[max(0, action_start - 32):action_start]
        # This is intentionally a small vocabulary: it recognizes a follow-up
        # command after a capability question, not arbitrary cross-sentence
        # topic drift.
        return bool(
            re.search(
                r"(?:能|可以|能否|是否|可不可以).{0,12}"
                r"(?:刷|看|浏览|搜索|搜|读).{0,4}(?:不|吗|么|？|\?)",
                prefix,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _action_is_explicit(cls, text: str, action: re.Match[str], intent: str) -> bool:
        """Require an execution cue and reject negated/conditional-only verbs."""

        action_text = action.group(0)
        before = text[max(0, action.start() - 18):action.start()]
        after = text[action.end():min(len(text), action.end() + 28)]
        local = f"{before}{action_text}{after}"

        # Do not let a negative command or a failed-status description pass
        # merely because it contains the same verb as a positive request.
        if re.search(
            r"(?:不要|别|不想|不用|无需|不必|勿|不会|无法|没|未|(?<!能)不能|(?<!能)不)\s*$",
            before,
        ):
            return False
        if re.match(r"\s*(?:了)?不(?:了|行|成|起|动|到)", after):
            return False

        # "看看能不能刷" is a capability question, not by itself an action.
        # A later explicit command in the same message is handled separately.
        if re.match(
            r"\s*(?:能不能|能否|是否|可不可以|能|可以).{0,10}"
            r"(?:刷|看|浏览|搜|读)(?:.{0,4}(?:不|吗|么))?",
            after,
        ):
            return False
        # A trailing bare "不" followed by a question mark is the short
        # capability-question form ("能刷不？"), not a successful action.
        if re.match(r"\s*不\s*[？?]", after):
            return False

        strong_prefix = bool(
            re.search(
                r"(?:请|帮我|给我|替我|你(?:就)?|去|就|来|试试|先|现在|立即|帮忙)\s*$",
                before,
            )
        )
        repeated = len(action_text) >= 2 and action_text not in {"阅读", "浏览", "搜索"}
        if intent == "recommend":
            has_object = bool(re.search(r"帖子|贴|笔记|内容|感兴趣", after))
            return strong_prefix or repeated or has_object
        if intent == "read":
            return strong_prefix or bool(re.search(r"帖子|贴|笔记|内容", after))
        # A search needs either an imperative cue or an actual query tail.
        query_tail = re.sub(r"^[\s：:，,、-]+", "", after)
        query_tail = re.sub(r"^(?:一下|下)\s*", "", query_tail)
        return strong_prefix or bool(query_tail and not re.match(r"(?:吗|嘛|呢|不|么)", query_tail))

    @classmethod
    def _has_strong_execution_cue(cls, text: str, action: re.Match[str], intent: str) -> bool:
        before = text[max(0, action.start() - 18):action.start()]
        after = text[action.end():min(len(text), action.end() + 28)]
        return bool(
            re.search(r"(?:请|帮我|给我|替我|去|就|来|试试|现在|立即)\s*$", before)
            or (intent == "recommend" and (len(action.group(0)) >= 2 or re.search(r"帖子|贴|笔记|内容|感兴趣", after)))
            or (intent == "read" and re.search(r"帖子|贴|笔记|内容", after))
        )

    @classmethod
    def _extract_query_after_action(cls, text: str, action: re.Match[str], intent: str) -> str:
        if intent == "recommend":
            # Recommendation topics are only accepted with explicit topic
            # language, regardless of where the action appeared.
            return cls._extract_query(text, (), intent=intent)
        tail = text[action.end():]
        tail = re.sub(r"^[\s上里中：:，,、-]+", "", tail)
        tail = re.sub(r"^(?:一下|下|帮我|给我)\s*", "", tail)
        tail = cls._clean_query_tail(tail, strip_generic_suffix=False)
        return tail[:MAX_QUERY_CHARS]

    @classmethod
    def _extract_query(cls, text: str, markers: tuple[str, ...], *, intent: str = "") -> str:
        # Recommendation requests are normally a feed action, not a search.
        # Only an explicit topic/search cue may bind a query.  This prevents
        # conversational tails such as "了吗？新功能~" from becoming a topic.
        if intent == "recommend":
            topic_patterns = (
                r"(?:关于|有关|主题|关键词|话题)(?:是|为)?\s*[：:]?\s*"
                r"(?P<query>[^，。！？!?~～]{1,60})",
                r"(?:搜索|搜|查找|查)(?:一下|下)?\s*[：:]?\s*"
                r"(?P<query>[^，。！？!?~～]{1,60})",
            )
            for pattern in topic_patterns:
                match = re.search(pattern, text)
                if match:
                    return cls._clean_query_tail(match.group("query"), strip_generic_suffix=True)
            return ""

        # Prefer text following the platform marker; this covers both
        # “搜小红书猫咪” and “小红书上搜索猫咪” without an LLM.
        tail = re.split(r"(?:小红书|xiaohongshu|xhs)", text, maxsplit=1, flags=re.IGNORECASE)[-1]
        tail = re.sub(r"^[\s上里中：:，,、-]*(?:的)?", "", tail)
        marker_pattern = "|".join(re.escape(item) for item in sorted(markers, key=len, reverse=True))
        tail = re.sub(rf"^(?:{marker_pattern})\s*(?:一下|下)?\s*", "", tail)
        # The action marker may be followed by a colon/comma (for example
        # "小红书搜索：猫咪"); strip that separator only after removing the
        # marker so it cannot become part of the query.
        tail = re.sub(r"^[\s：:，,、-]+", "", tail)
        # If the platform appeared before the action and the tail still starts
        # with a connective, remove only that connective.  Never use the whole
        # user message as a query.
        tail = re.sub(r"^(?:一下|下|帮我|给我)\s*", "", tail)
        tail = cls._clean_query_tail(tail)
        if not tail or any(word in tail for word in ("并推荐", "然后推荐")):
            return ""
        return tail[:MAX_QUERY_CHARS]

    @staticmethod
    def _clean_query_tail(value: Any, *, strip_generic_suffix: bool = False) -> str:
        """Trim punctuation/utterance tails while preserving a real topic."""

        punctuation = " ：:，,、。！？!?~～\"'“”「」『』"
        query = " ".join(str(value or "").split()).strip(punctuation)
        # Question/acknowledgement continuations are not topics.  The pattern
        # is structural rather than tied to one observed sentence.
        query = re.sub(r"^(?:了|吗|嘛|呢|吧|呀|啊|哦|诶|诶呀)+", "", query)
        query = re.sub(r"(?:了|吗|嘛|呢|吧|呀|啊|哦|诶|诶呀)+$", "", query)
        # The marker removal above can expose punctuation (for example
        # "了吗？猫咪"); trim it once more without touching punctuation inside
        # an otherwise explicit topic.
        query = query.strip(punctuation)
        # A quantity belongs to the request goal, not to the search topic.
        # Keep it out of the deep-link query while retaining the count in the
        # grant's backend-only target field.
        number = r"(?:[0-9０-９]+|[一两二三四五六七八九十])"
        quantity = (
            rf"(?:(?:{number}\s*(?:到|至|[-~～])\s*{number}|"
            rf"{number}\s*{number})\s*(?:篇|个|则|条)\s*(?:帖子|贴|笔记|内容)?"
            rf"|{number}\s*(?:篇|个|则|条)\s*(?:帖子|贴|笔记|内容)?)"
        )
        quantity_with_leading_words = (
            rf"(?:[，,、]\s*(?:给我|来|看)?\s*|"
            rf"(?:给我|来|看)\s+|\s+){quantity}$"
        )
        query = re.sub(quantity_with_leading_words, "", query)
        query = re.sub(rf"{quantity}$", "", query).strip(punctuation)
        if strip_generic_suffix:
            query = re.sub(r"(?:的)?(?:帖子|笔记|内容)$", "", query).strip()
        return query[:MAX_QUERY_CHARS]

    @staticmethod
    def _has_negation(text: str) -> bool:
        """Reject short natural-language negations around the platform/action."""

        lowered = str(text or "").lower()
        platform = r"(?:小红书|xhs|xiaohongshu)"
        # Include bare 不/没/未 and modal negatives as well as the longer
        # forms.  Otherwise phrases such as "我不刷小红书" would pass merely
        # because the recommendation marker "刷小红书" is present.
        # In "能不能刷小红书" the second 不 is part of a capability
        # question, not a negative command.  Do not classify 不/不能 when it
        # is immediately preceded by 能; the ordinary "我不能刷" form still
        # matches.
        negative = r"(?:不要|别|不想|不用|无需|不必|勿|不会|(?<!能)不能|无法|先不|(?<!能)不|没|未)"
        # Both "不要看小红书" and "小红书不要搜索" forms are common.  Keep
        # the window small so an unrelated earlier sentence cannot suppress a
        # later explicit request.
        return bool(
            re.search(rf"{negative}.{{0,6}}{platform}", lowered, flags=re.IGNORECASE)
            or re.search(rf"{platform}.{{0,6}}{negative}", lowered, flags=re.IGNORECASE)
        )

    @staticmethod
    def _normalize_query(value: Any) -> str:
        return " ".join(str(value or "").split())[:MAX_QUERY_CHARS]

    @classmethod
    def _has_comment_request(cls, text: Any) -> bool:
        normalized = " ".join(str(text or "").split()).strip()
        if not normalized or any(marker in normalized for marker in cls._COMMENT_NEGATION_MARKERS):
            return False
        return any(marker in normalized for marker in cls._COMMENT_MARKERS)

    @staticmethod
    def _safe_fact_text(value: Any) -> str:
        """Bound a natural-language query before placing it in the fact tail."""

        text = " ".join(str(value or "").split())
        # Keep the fact a plain sentence even if a query contains delimiters
        # that resemble an internal payload or control marker.
        text = re.sub(r"[{}\[\]<>`\r\n]", " ", text)
        text = text.replace("“", "").replace("”", "").replace('"', "").replace("'", "")
        return " ".join(text.split())[:MAX_QUERY_CHARS]

    @classmethod
    def _target_count(cls, text: str, intent: str) -> int:
        """Resolve the user's requested number without exposing a tool arg.

        Feed/search requests default to three independent posts.  An explicit
        2--5 post quantity is respected and bounded at five.  A singular
        request, or a concrete ``read`` request without a quantity, is one
        post.  Numbers that are part of a topic (for example a year) are not
        interpreted as a quantity because a post unit is required.
        """

        normalized = " ".join(str(text or "").split()).strip()
        if not normalized:
            return DEFAULT_TARGET_COUNT

        # "三到五篇" / "2-5个帖子" means the upper end is the explicit goal;
        # it remains bounded by the same hard cap as a single number.
        token = r"(?:[0-9０-９]+|[一两二三四五六七八九十])"
        range_pattern = re.compile(
            rf"(?P<low>{token})\s*(?:到|至|[-~～])\s*(?P<high>{token})"
            r"\s*(?:篇|个|则|条)\s*(?:帖子|贴|笔记|内容)?"
        )
        match = range_pattern.search(normalized)
        if match:
            high = cls._number_token(match.group("high"))
            if high is not None:
                return max(1, min(MAX_TARGET_COUNT, high))

        # "三五个帖子" / "两三个" is the colloquial bounded range form.
        colloquial = re.search(
            rf"(?P<low>{token})\s*(?P<high>{token})\s*(?:个|篇|则|条)\s*(?:帖子|贴|笔记|内容)?",
            normalized,
        )
        if colloquial:
            high = cls._number_token(colloquial.group("high"))
            if high is not None:
                return max(1, min(MAX_TARGET_COUNT, high))

        if re.search(r"(?:单篇|只看一篇|只读一篇|只搜一篇|一篇|一个|一帖|一则)", normalized):
            return 1

        explicit = re.search(
            rf"(?P<count>{token})\s*(?:篇|个|则|条)\s*(?:帖子|贴|笔记|内容)?",
            normalized,
        )
        if explicit:
            count = cls._number_token(explicit.group("count"))
            if count is not None:
                return max(1, min(MAX_TARGET_COUNT, count))

        if intent == "read":
            return 1
        return DEFAULT_TARGET_COUNT

    @classmethod
    def _number_token(cls, value: Any) -> Optional[int]:
        raw = str(value or "").strip()
        if raw in cls._COUNT_WORDS:
            return cls._COUNT_WORDS[raw]
        try:
            return int(raw.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except (TypeError, ValueError):
            return None


_CURRENT_GRANT: contextvars.ContextVar[Optional[XhsGrant]] = contextvars.ContextVar(
    "mirrow_xhs_third_state_grant", default=None
)
_CURRENT_IDENTITY: contextvars.ContextVar[tuple[str, str, str]] = contextvars.ContextVar(
    "mirrow_xhs_third_state_identity", default=("", "", "")
)


def bind_grant(grant: Optional[XhsGrant], *, session_id: str, message_id: str, turn_id: str):
    """Bind request-local grant/identity; caller must reset the returned token."""

    return (
        _CURRENT_GRANT.set(grant),
        _CURRENT_IDENTITY.set((str(session_id or ""), str(message_id or ""), str(turn_id or ""))),
    )


def reset_grant(binding) -> None:
    if not binding:
        return
    grant_token, identity_token = binding
    _CURRENT_GRANT.reset(grant_token)
    _CURRENT_IDENTITY.reset(identity_token)


class XhsThirdStateTool(BaseTool):
    """Generic proxy whose real capability is disclosed only dynamically."""

    name = THIRD_STATE_TOOL_NAME
    description = "读取AI的专用小红书行动手机当前可见的只读内容。"
    flash_description = "读取AI的专用小红书行动手机当前可见的只读内容"
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    single_use = True
    # The hidden real handler is never registered in the normal tool
    # catalogue; this proxy exposes only the bounded read-only description.
    visible_to_pro = True

    def __init__(
        self,
        *,
        gate: Optional[XhsCapabilityGate] = None,
        runner: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    ):
        self.gate = gate or _DEFAULT_GATE
        self.runner = runner or _run_v3_chat_event

    async def execute(self, **kwargs) -> ToolResult:
        grant = _CURRENT_GRANT.get()
        session_id, message_id, turn_id = _CURRENT_IDENTITY.get()
        try:
            used = self.gate.consume(
                grant,
                session_id=session_id,
                message_id=message_id,
                turn_id=turn_id,
                capability=XHS_CAPABILITY,
                parameters=kwargs,
            )
        except PermissionError as exc:
            # A grant mismatch is a request/turn boundary failure, not an
            # Android or ADB permission report.  Keep the diagnostic reason in
            # the internal error field while giving AI a neutral factual result
            # so it does not send the user to re-authorize the phone.
            return ToolResult(ToolStatus.FAILED, XHS_GATE_FAILURE_MESSAGE, error=str(exc))

        try:
            result = self.runner(
                session_id=session_id,
                # The execution facts are held by the consumed grant.  The
                # model only supplied an empty call envelope, so it cannot
                # alter intent/query or smuggle a comment draft into runtime.
                intent=(used.grant.allowed_intents[0] if used.grant.allowed_intents else ""),
                query=used.grant.bound_query,
                count=used.grant.target_count,
                source="chat_tool",
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return ToolResult(ToolStatus.ERROR, "只读设备能力执行异常。", error=type(exc).__name__)

        payload = result if isinstance(result, Mapping) else {"status": "invalid_result"}
        status = str(payload.get("status") or "failed")
        content = _compact_result(payload)
        extra = _comment_extra_data(payload)
        if status == "success":
            return ToolResult(ToolStatus.SUCCESS, content, extra_data=extra)
        return ToolResult(ToolStatus.FAILED, content, error=str(payload.get("error") or status), extra_data=extra)


def _compact_result(payload: Mapping[str, Any]) -> str:
    """Return bounded evidence without screenshots or opaque grant data.

    The runner's aggregate is already evidence-only, but this boundary also
    sanitizes injected/test results so a future handler cannot accidentally
    put raw ADB/UI payloads or model audit text into AI's follow-up prompt.
    """

    def _source(value: Any, *, fallback_id: str = "") -> dict[str, str]:
        if not isinstance(value, Mapping):
            value = {}
        provider = str(value.get("provider") or value.get("source_provider") or "")[:120]
        url = str(value.get("url") or value.get("source_url") or "")[:500]
        source_id = str(value.get("source_id") or fallback_id or "")[:200]
        return {"provider": provider, "url": url, "source_id": source_id}

    def _post(value: Any, index: int = 0) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        source_id = str(value.get("source_id") or "")[:200]
        reliable = _source(value.get("reliable_source") or value.get("source"), fallback_id=source_id)
        if not reliable.get("provider") and not reliable.get("url"):
            reliable = _source(value, fallback_id=source_id)
        result: dict[str, Any] = {
            "index": int(value.get("index") or index or 1),
            "node_id": str(value.get("node_id") or "")[:80],
            "source_id": source_id,
            "title": str(value.get("title") or "未命名帖子").strip()[:240],
            "summary": str(value.get("summary") or value.get("content_summary") or "未取得文字摘要").strip()[:900],
            "evidence_excerpt": str(value.get("evidence_excerpt") or "").strip()[:500],
            "reliable_source": reliable,
        }
        return result

    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, Mapping):
        aggregate = {}
    raw_posts = payload.get("posts")
    if not isinstance(raw_posts, list):
        raw_posts = aggregate.get("posts") if isinstance(aggregate.get("posts"), list) else []
    posts = [_post(item, index) for index, item in enumerate(raw_posts, 1)]
    posts = [item for item in posts if item]
    target_count = payload.get("target_count", aggregate.get("target_count"))
    success_count = payload.get("success_count", aggregate.get("success_count"))
    cross_items = [
        {
            "title": item["title"],
            "summary": item["summary"],
            "evidence_excerpt": item["evidence_excerpt"],
            "reliable_source": item["reliable_source"],
        }
        for item in posts
    ]
    safe: dict[str, Any] = {}
    for key in (
        "status", "barrier", "error", "run_id", "activity_id", "node_id",
        "node_ids", "source", "delivery_status", "source_id", "source_url",
        "query", "title", "content_summary", "evidence_excerpt", "limitations",
        "target_count", "success_count", "successful_count", "post_count",
    ):
        value = payload.get(key)
        if key == "node_ids" and isinstance(value, list):
            value = [str(item)[:80] for item in value[:5]]
        elif key in {"source_url"}:
            value = str(value or "")[:500]
        elif key in {"content_summary", "limitations"}:
            value = str(value or "")[:1200]
        elif key in {"evidence_excerpt"}:
            value = str(value or "")[:500]
        if value not in (None, "", [], {}):
            safe[key] = value

    if posts:
        safe["posts"] = posts[:5]
        if aggregate.get("summary"):
            safe["aggregate_summary"] = str(aggregate.get("summary"))[:2000]
        safe["cross_post_summary_material"] = {
            "target_count": target_count,
            "success_count": success_count,
            "items": cross_items[:5],
            "comparison_basis": str(
                aggregate.get("cross_post_summary_material", {}).get("comparison_basis")
                if isinstance(aggregate.get("cross_post_summary_material"), Mapping)
                else aggregate.get("comparison_basis")
                or "横向材料仅由各篇独立的专用手机屏幕证据组成；未把未读内容补入总结。"
            )[:300],
        }
    elif isinstance(aggregate.get("summary"), str) and aggregate.get("summary"):
        safe["aggregate_summary"] = str(aggregate["summary"])[:2000]

    status = str(payload.get("status") or "")
    barrier = str(payload.get("barrier") or payload.get("error") or "")
    try:
        target_number = int(target_count or 0)
        success_number = int(success_count or 0)
    except (TypeError, ValueError):
        target_number = success_number = 0
    if barrier == "no_feed_cards":
        safe["feedback"] = "页面没有识别到可读帖子，暂时没有完成本轮浏览。"
    elif barrier == "no_new_feed_cards":
        safe["feedback"] = "页面上的可读帖子都已经读过，本轮没有取得新的帖子。"
    elif status == "partial_success" or (target_number and 0 < success_number < target_number):
        safe["feedback"] = f"本轮取得{success_number}/{target_number}篇；未取得的部分不作推断。"
    elif status and status not in {"success"} and not posts:
        safe["feedback"] = "本轮只读浏览未完成；未取得可核实的帖子材料。"

    comment = payload.get("comment_delivery")
    if isinstance(comment, Mapping):
        delivery = {
            key: comment.get(key)
            for key in ("status", "post_id", "crop_applied", "action", "device_interaction")
            if comment.get(key) not in (None, "")
        }
        try:
            from .chat_attachment_boundary import sanitize_chat_image_metadata
            safe_image = sanitize_chat_image_metadata(comment.get("image"))
        except Exception:
            safe_image = None
        if safe_image is not None:
            delivery["image"] = safe_image
        safe["comment_delivery"] = delivery

    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))


def _comment_extra_data(payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    delivery = payload.get("comment_delivery")
    if not isinstance(delivery, Mapping) or delivery.get("status") != "ready_for_user":
        return None
    image = delivery.get("image")
    if not isinstance(image, Mapping):
        return None
    # Reuse the ownership/path boundary rather than accepting a mere prefix;
    # this rejects traversal and alternate static roots before frontend data.
    try:
        from .chat_attachment_boundary import sanitize_chat_image_metadata
        safe_image = sanitize_chat_image_metadata(image)
    except Exception:
        safe_image = None
    if safe_image is None:
        return None
    return {"xhs_comment": True, "images": [safe_image], "action": "由用户手动发送"}


async def _run_v3_chat_event(**kwargs) -> dict[str, Any]:
    """Resolve the already-initialized Wander manager without creating one."""

    try:
        from mirrow_core.shared_state import get_wander_manager
        manager = get_wander_manager()
    except Exception:
        manager = None
    if manager is None or not hasattr(manager, "browse_xhs_from_chat"):
        return {"status": "awaiting_runtime", "barrier": "wander_runtime_unavailable"}
    return await manager.browse_xhs_from_chat(**kwargs)


_DEFAULT_GATE = XhsCapabilityGate()


def get_default_xhs_gate() -> XhsCapabilityGate:
    return _DEFAULT_GATE


__all__ = [
    "DEFAULT_TARGET_COUNT",
    "GRANT_TTL_SECONDS",
    "MAX_TARGET_COUNT",
    "THIRD_STATE_TOOL_NAME",
    "XHS_CAPABILITY",
    "XhsCapabilityGate",
    "XhsGrant",
    "XhsGrantIssue",
    "XhsThirdStateTool",
    "bind_grant",
    "get_default_xhs_gate",
    "reset_grant",
]
