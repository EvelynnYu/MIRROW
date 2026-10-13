"""统一的硅基哨兵事实合同与输出接地守卫。

哨兵有两个模型阶段：第一层判断本次感知和是否主动联系，第二层把
决定写成发给人类伙伴的自然消息。两层都只能把本模块确认过的、当前且高
置信的事实当作硬约束；未知、陈旧或互相冲突的观测不应被模型伪装成
确定事实，也不应阻止自然的关心、人格表达或同义改写。

这里集中维护少量必要的语言识别规则。调用方不应各自再实现正则或
“看起来像事实”的字符串判断。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


class FactState(str, Enum):
    """事实可用性，不把缺失/陈旧/冲突折叠成一个假值。"""

    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"
    CONFLICT = "conflict"


# 兼容更直观的调用方命名。
FactStatus = FactState


@dataclass(frozen=True)
class SentinelFact:
    """一个字段级、可审计的事实。"""

    name: str
    value: Any = None
    state: FactState = FactState.UNKNOWN
    source: str = "snapshot"
    captured_at: Optional[str] = None
    age_seconds: Optional[float] = None
    confidence: str = "high"
    evidence: str = ""

    @property
    def usable(self) -> bool:
        return self.state is FactState.KNOWN and self.confidence == "high"

    @property
    def status(self) -> FactState:
        """Readable alias for callers that call the enum a status."""
        return self.state

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SentinelFact":
        raw_state = value.get("state", FactState.UNKNOWN.value)
        try:
            state = FactState(raw_state)
        except (TypeError, ValueError):
            state = FactState.UNKNOWN
        return cls(
            name=str(value.get("name") or ""),
            value=value.get("value"),
            state=state,
            source=str(value.get("source") or "snapshot"),
            captured_at=value.get("captured_at"),
            age_seconds=value.get("age_seconds"),
            confidence=str(value.get("confidence") or "high"),
            evidence=str(value.get("evidence") or ""),
        )


@dataclass(frozen=True)
class GroundingRejection:
    """一次明确的字段级矛盾，便于审计和诊断。"""

    field: str
    reason_code: str
    expected: str
    observed: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class GroundingResult:
    accepted: bool
    rejections: List[GroundingRejection] = field(default_factory=list)

    @property
    def rejection_reasons(self) -> List[Dict[str, str]]:
        return [item.to_dict() for item in self.rejections]

    @property
    def reasons_by_field(self) -> Dict[str, List[Dict[str, str]]]:
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for item in self.rejections:
            grouped.setdefault(item.field, []).append(item.to_dict())
        return grouped


def _as_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _age_seconds(captured_at: Any, now: datetime) -> Optional[float]:
    captured = _as_datetime(captured_at)
    if captured is None:
        return None
    return max(0.0, (now - captured).total_seconds())


def _snapshot_value(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _fresh_fact(
    snapshot: Any,
    name: str,
    value: Any,
    *,
    source: str,
    max_age: float,
    captured_at: Any = None,
    evidence: str = "",
    now: Optional[datetime] = None,
) -> SentinelFact:
    """Build a fact while preserving the difference between unknown and stale."""
    if value is None:
        return SentinelFact(
            name=name, state=FactState.UNKNOWN, value=None, source=source,
            captured_at=str(captured_at) if captured_at else None,
            evidence=evidence,
        )

    if captured_at is None:
        field_ts = _snapshot_value(snapshot, "field_ts", {}) or {}
        captured_at = field_ts.get(source) or field_ts.get(name)
    now = now or datetime.now()
    age = _age_seconds(captured_at, now)
    # A value without a trustworthy capture timestamp cannot be used as a fresh
    # hard fact. This is intentional: cached values must not silently become
    # current simply because the process restarted.
    state = FactState.KNOWN if age is not None and age <= max_age else FactState.STALE
    return SentinelFact(
        name=name,
        value=value,
        state=state,
        source=source,
        captured_at=str(captured_at) if captured_at else None,
        age_seconds=age,
        evidence=evidence,
    )


def _normalize_location(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"home", "家", "在家", "home_wifi"}:
        return "home"
    if text in {"away", "out", "elsewhere", "外出", "在外", "outside"}:
        return "away"
    if text in {"work", "公司", "office"}:
        return "work"
    return text or None


def _address_hint(value: Any) -> Optional[str]:
    """Return no category for a free-form address.

    Reverse-geocoded text is a location detail, not a home/away authority.
    In particular, substring matching ``家`` turns addresses such as
    ``国家...`` into a false home signal.  A category may only come from the
    structured GPS category or fresh home WiFi; the helper remains as a
    compatibility seam for callers that imported the old private name.
    """
    del value
    return None


def _location_detail_fact(snapshot: Any, now: datetime) -> SentinelFact:
    """Keep fresh GPS detail separate from the coarse home/away fact."""
    field_ts = _snapshot_value(snapshot, "field_ts", {}) or {}
    wifi_value = _snapshot_value(snapshot, "wifi_is_home")
    wifi_ts = field_ts.get("wifi")
    wifi_age = _age_seconds(wifi_ts, now)
    if wifi_value is True and wifi_age is not None and wifi_age <= 300:
        # Home WiFi is the current location authority.  A retained GPS detail
        # belongs to an earlier outdoor observation and must not survive as a
        # second, contradictory current fact.
        return SentinelFact(
            name="location_detail", value=None, state=FactState.UNKNOWN,
            source="wifi", captured_at=str(wifi_ts), age_seconds=wifi_age,
            evidence="GPS detail suppressed by fresh home WiFi authority",
        )
    gps_ts = field_ts.get("gps")
    gps_age = _age_seconds(gps_ts, now)
    address = _snapshot_value(snapshot, "location_address")
    lat = _snapshot_value(snapshot, "location_lat")
    lng = _snapshot_value(snapshot, "location_lng")
    if address not in (None, ""):
        detail: Any = str(address).strip()
    elif lat is not None and lng is not None:
        detail = f"{lat},{lng}"
    else:
        detail = None
    if detail is None:
        return SentinelFact(
            name="location_detail", value=None, state=FactState.UNKNOWN,
            source="gps", captured_at=str(gps_ts) if gps_ts else None,
            age_seconds=gps_age, evidence="GPS detail unavailable",
        )
    state = FactState.KNOWN if gps_age is not None and gps_age <= 1800 else FactState.STALE
    return SentinelFact(
        name="location_detail", value=detail, state=state, source="gps",
        captured_at=str(gps_ts) if gps_ts else None, age_seconds=gps_age,
        evidence="fresh GPS address/coordinates" if state is FactState.KNOWN else "GPS detail is not fresh",
    )


def _registered_place_facts(snapshot, now):
    wifi_ts = (_snapshot_value(snapshot, "field_ts", {}) or {}).get("wifi")
    wifi_age = _age_seconds(wifi_ts, now)
    home = _snapshot_value(snapshot, "wifi_is_home") is True and wifi_age is not None and wifi_age <= 300
    values = {
        "location_place": _snapshot_value(snapshot, "location_place_name"),
        "location_environment": _snapshot_value(snapshot, "location_environment") if _snapshot_value(snapshot, "location_place_name") else None,
    }
    return {name: _fresh_fact(
        snapshot, name, None if home else value, source="gps_place", max_age=1800,
        evidence="GPS matched a registered place radius; environment is the person's place setting, building interior is not measured",
        now=now,
    ) for name, value in values.items()}


def _location_fact(snapshot: Any, now: Optional[datetime] = None) -> SentinelFact:
    """Resolve location with fresh home WiFi as the only hard home authority."""
    now = now or datetime.now()
    wifi_value = _snapshot_value(snapshot, "wifi_is_home")
    field_ts = _snapshot_value(snapshot, "field_ts", {}) or {}
    wifi_ts = field_ts.get("wifi")
    wifi_age = _age_seconds(wifi_ts, now)
    wifi_fresh = wifi_value is not None and wifi_age is not None and wifi_age <= 300

    gps_category = _normalize_location(_snapshot_value(snapshot, "location_category"))
    gps_address = _snapshot_value(snapshot, "location_address")
    # A free-form address is deliberately not converted into a category.
    # Keep it in the separate location_detail fact below.
    gps_value = gps_category
    gps_ts = field_ts.get("gps")
    gps_age = _age_seconds(gps_ts, now)
    gps_present = gps_category is not None or gps_address not in (None, "")
    gps_fresh = gps_category is not None and gps_age is not None and gps_age <= 1800

    # A fresh home WiFi observation is authoritative and deliberately hides a
    # cached/outdated GPS address. This prevents an old location leaking into a
    # push after the user arrived home.
    if wifi_fresh:
        return SentinelFact(
            name="location", value="home" if bool(wifi_value) else "away",
            state=FactState.KNOWN, source="wifi", captured_at=str(wifi_ts),
            age_seconds=wifi_age, evidence="fresh home WiFi authority",
        )

    # Only an explicitly structured address classification is comparable to a
    # structured GPS category. Free-form reverse-geocoded text remains a
    # detail and never manufactures a conflict or a home classification.
    address_category = _normalize_location(
        _snapshot_value(snapshot, "location_address_category")
        or _snapshot_value(snapshot, "gps_address_category")
    )
    if gps_fresh and gps_category and address_category:
        if gps_category != address_category:
            return SentinelFact(
                name="location", value=None, state=FactState.CONFLICT,
                source="gps", captured_at=str(gps_ts), age_seconds=gps_age,
                evidence="structured GPS address/category conflict without fresh home WiFi",
            )
    if gps_fresh:
        return SentinelFact(
            name="location", value=gps_value, state=FactState.KNOWN,
            source="gps", captured_at=str(gps_ts), age_seconds=gps_age,
            evidence="fresh GPS category/address",
        )

    if gps_present and gps_category is None and gps_age is not None and gps_age <= 1800:
        # Fresh coordinates/address without a structured category are still a
        # useful detail, but they cannot prove home or away.
        return SentinelFact(
            name="location", value=None, state=FactState.UNKNOWN,
            source="gps", captured_at=str(gps_ts), age_seconds=gps_age,
            evidence="fresh GPS detail without structured category",
        )

    if gps_present or wifi_value is not None:
        # Preserve the reason a value cannot be used. A stale WiFi/GPS value is
        # materially different from a source that never reported anything.
        stale_source = "wifi" if wifi_value is not None else "gps"
        stale_ts = wifi_ts if wifi_value is not None else gps_ts
        stale_age = wifi_age if wifi_value is not None else gps_age
        return SentinelFact(
            name="location", value=None, state=FactState.STALE,
            source=stale_source, captured_at=str(stale_ts) if stale_ts else None,
            age_seconds=stale_age, evidence="location source is not fresh",
        )
    return SentinelFact(name="location", state=FactState.UNKNOWN, source="gps/wifi")


def build_sentinel_fact_contract(
    snapshot: Any,
    summary: Any = None,
    *,
    now: Optional[datetime] = None,
) -> "SentinelFactContract":
    """Extract the small set of facts that may be hard-grounded.

    ``summary`` is accepted for callers that already carry a structured
    snapshot/summary object, but free-form model prose is never treated as an
    authority. Only snapshot fields with fresh timestamps become ``known``.
    """
    del summary  # The model's prose cannot upgrade sensor confidence.
    now = now or datetime.now()
    field_ts = _snapshot_value(snapshot, "field_ts", {}) or {}

    physical_idle = _snapshot_value(snapshot, "input_idle_seconds")
    if physical_idle is None:
        physical_idle = _snapshot_value(snapshot, "physical_input_idle_seconds")
    input_fact = _fresh_fact(
        snapshot, "input_idle_seconds", physical_idle, source="input_idle",
        max_age=180, captured_at=field_ts.get("input_idle"), evidence="Raw Input local source", now=now,
    )
    if input_fact.usable:
        input_value = "active" if float(physical_idle) < 120 else "idle"
        input_fact = SentinelFact(
            **{**input_fact.to_dict(), "name": "input_activity", "value": input_value,
               "state": FactState.KNOWN},
        )
    else:
        input_fact = SentinelFact(
            name="input_activity", value=None, state=input_fact.state,
            source=input_fact.source, captured_at=input_fact.captured_at,
            age_seconds=input_fact.age_seconds, evidence=input_fact.evidence,
        )

    remote_idle = _snapshot_value(snapshot, "remote_input_idle_seconds")
    remote_fact = _fresh_fact(
        snapshot, "remote_input_idle_seconds", remote_idle, source="input_idle",
        max_age=180, captured_at=field_ts.get("input_idle"), evidence="Raw Input remote source", now=now,
    )
    if remote_fact.usable:
        remote_fact = SentinelFact(
            **{**remote_fact.to_dict(), "name": "remote_input_activity",
               "value": "active" if float(remote_idle) < 120 else "idle",
               "state": FactState.KNOWN},
        )
    else:
        remote_fact = SentinelFact(
            name="remote_input_activity", value=None, state=remote_fact.state,
            source=remote_fact.source, captured_at=remote_fact.captured_at,
            age_seconds=remote_fact.age_seconds, evidence=remote_fact.evidence,
        )

    steps_value = _snapshot_value(snapshot, "steps_today")
    steps_fact = _fresh_fact(
        snapshot, "steps_today", steps_value, source="steps", max_age=900,
        evidence="today's step counter", now=now,
    )

    is_period = _snapshot_value(snapshot, "is_period")
    period_day = _snapshot_value(snapshot, "period_day")
    period_fact = _fresh_fact(
        snapshot, "period", bool(is_period) if is_period is not None else None,
        source="period", max_age=1800, evidence="period source", now=now,
    )
    period_conflict = bool(period_fact.usable and not bool(is_period) and period_day is not None)
    if period_fact.usable and period_fact.value is True and period_day is not None:
        # Keep the day as a separate fact so a wrong ordinal can be diagnosed.
        period_value: Any = {"active": True, "day": period_day}
    elif period_fact.usable:
        period_value = {"active": bool(period_fact.value), "day": None}
    else:
        period_value = None
    period_fact = SentinelFact(
        name="period", value=period_value,
        state=FactState.CONFLICT if period_conflict else period_fact.state,
        source=period_fact.source, captured_at=period_fact.captured_at,
        age_seconds=period_fact.age_seconds, evidence=period_fact.evidence,
    )

    facts = {
        "input_activity": input_fact,
        "remote_input_activity": remote_fact,
        "location": _location_fact(snapshot, now=now),
        "location_detail": _location_detail_fact(snapshot, now),
        "steps_today": steps_fact,
        "period": period_fact,
        **_registered_place_facts(snapshot, now),
    }
    if _snapshot_value(snapshot, "user_status") in {"sleeping", "napping"}:
        # Source coverage, not a verdict that the person is asleep/awake.
        recent_input = any(f.usable and f.value == "active" for f in (input_fact, remote_fact))
        phone_ts = field_ts.get("current_app")
        phone_age = _age_seconds(phone_ts, now)
        phone_active = (
            phone_age is not None and phone_age <= 300
            and _snapshot_value(snapshot, "mobile_screen_on") is True
            and bool(_snapshot_value(snapshot, "mobile_app_package"))
            and _snapshot_value(snapshot, "mobile_app_package") not in {
                "com.android.systemui", "com.oplus.launcher", "com.android.launcher", "com.android.launcher3",
            }
        )
        awake_evidence = any(
            item.get("state") == "rest_declaration_with_device_awake_evidence"
            for item in (_snapshot_value(snapshot, "semantic_observations", []) or [])
            if isinstance(item, Mapping)
        )
        facts["rest_activity_evidence"] = SentinelFact(
            name="rest_activity_evidence", value=bool(recent_input or phone_active or awake_evidence),
            state=FactState.KNOWN, source="current_snapshot_source_coverage",
            evidence="Only fresh attributable input, content-app or cross-source awake evidence; not screen pixels/processes",
        )
    return SentinelFactContract(facts=facts)


@dataclass(frozen=True)
class SentinelFactContract:
    """Typed fact contract shared by Flash judgment and push generation."""

    facts: Mapping[str, SentinelFact] = field(default_factory=dict)
    version: str = "sentinel-facts-v1"

    def get(self, name: str) -> SentinelFact:
        return self.facts.get(name, SentinelFact(name=name))

    def known(self, name: str) -> bool:
        return self.get(name).usable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "facts": {name: fact.to_dict() for name, fact in self.facts.items()},
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SentinelFactContract":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        raw_facts = value.get("facts", value)
        facts: Dict[str, SentinelFact] = {}
        if isinstance(raw_facts, Mapping):
            for name, item in raw_facts.items():
                if isinstance(item, Mapping):
                    parsed = SentinelFact.from_dict({**dict(item), "name": name})
                    facts[str(name)] = parsed
        elif isinstance(raw_facts, list):
            for item in raw_facts:
                if isinstance(item, Mapping):
                    parsed = SentinelFact.from_dict(item)
                    if parsed.name:
                        facts[parsed.name] = parsed
        return cls(facts=facts, version=str(value.get("version") or "sentinel-facts-v1"))

    @classmethod
    def from_snapshot(
        cls, snapshot: Any, summary: Any = None, *, now: Optional[datetime] = None,
    ) -> "SentinelFactContract":
        return build_sentinel_fact_contract(snapshot, summary, now=now)

    def to_prompt_text(self) -> str:
        lines = ["哨兵事实合同（只把标为‘当前确认’的事实当作硬事实）："]
        labels = {
            "input_activity": ("本地键鼠", {"active": "活跃", "idle": "空闲"}),
            "remote_input_activity": ("远程键鼠输入", {"active": "活跃", "idle": "空闲"}),
            "rest_activity_evidence": ("休息声明的近期活动证据覆盖", {True: "有可复核观测（不等于确认未睡）", False: "未取得可复核活动证据；窗口/进程存在不构成矛盾"}),
            "location": ("位置", {"home": "在家", "away": "外出", "work": "在公司"}),
            "location_place": ("登记地点范围匹配", {}),
            "location_environment": ("登记地点环境（GPS未测量建筑内外）", {"indoor": "室内落脚处", "outdoor": "户外地点", "unknown": "室内外未知"}),
            "steps_today": ("今日步数", {}),
            "period": ("生理期", {}),
        }
        for name, fact in self.facts.items():
            title, value_map = labels.get(name, (name, {}))
            if fact.usable:
                if name == "period" and isinstance(fact.value, Mapping):
                    value = "第%s天" % fact.value.get("day") if fact.value.get("active") and fact.value.get("day") else ("是" if fact.value.get("active") else "否")
                else:
                    value = value_map.get(fact.value, fact.value)
                suffix = ""
                if fact.age_seconds is not None:
                    suffix = "（%.0f秒前采集）" % fact.age_seconds
                lines.append(f"- {title}：{value}{suffix}［当前确认］")
            else:
                lines.append(f"- {title}：{fact.state.value}［不可作为确定事实］")
        lines.append("证据范围：前台窗口与后台进程只说明软件存在；画面变化不证明人类伙伴操作。已同步睡眠分钟是带窗口的记录，不等于完整睡眠时长。")
        return "\n".join(lines)

    def to_context_text(self) -> str:
        """Objective-only projection suitable for a Context Builder neuron."""
        lines = ["当前哨兵观测事实："]
        for name, fact in self.facts.items():
            value = fact.value
            if isinstance(value, Mapping):
                value = dict(value)
            lines.append(
                f"- field={name}; value={value!r}; state={fact.state.value}; "
                f"source={fact.source}; captured_at={fact.captured_at or 'unknown'}; "
                f"age_seconds={fact.age_seconds if fact.age_seconds is not None else 'unknown'}; "
                f"confidence={fact.confidence}; evidence={fact.evidence or 'unknown'}"
            )
        return "\n".join(lines)

    def validate_text(self, text: Any) -> GroundingResult:
        """Reject only explicit contradictions with fresh, high-confidence facts."""
        text = str(text or "")
        if not text:
            return GroundingResult(accepted=True)
        rejections: List[GroundingRejection] = []

        inputs = (self.get("input_activity"), self.get("remote_input_activity"))
        if not any(f.usable and f.value == "active" for f in inputs):
            if _contains_assertive(text, _PC_USE_PATTERNS):
                rejections.append(_reject(
                    "pc_use", "fresh attributable input", "asserted person using PC",
                    "unsupported_pc_use", "当前未取得新鲜本地/远程操作证据，窗口或进程存在不能确认人类伙伴正在使用电脑",
                ))
        rest = self.get("rest_activity_evidence")
        if rest.usable and rest.value is False and _contains_assertive(text, _REST_CONFLICT_PATTERNS):
            rejections.append(_reject(
                "rest_activity_evidence", "no attributable activity evidence", "asserted sleep contradiction",
                "unsupported_rest_activity", "当前没有可复核的休息后活动证据，不能把挂着的窗口/进程或已同步睡眠片段写成睡眠声明矛盾",
            ))

        input_fact = self.get("input_activity")
        if input_fact.usable:
            if input_fact.value == "active" and _contains_assertive(text, _INPUT_IDLE_PATTERNS):
                rejections.append(_reject(
                    "input_activity", "active", "idle/no_input", "fresh_input_active",
                    "文本明确说键鼠/人一直未操作，但当前本地输入仍活跃",
                ))
            elif input_fact.value == "idle" and _contains_assertive(text, _INPUT_ACTIVE_PATTERNS):
                rejections.append(_reject(
                    "input_activity", "idle", "active/input", "fresh_input_idle",
                    "文本明确说键鼠正在操作，但当前本地输入为空闲",
                ))

        remote_fact = self.get("remote_input_activity")
        if remote_fact.usable:
            if remote_fact.value == "active" and _contains_assertive(text, _REMOTE_ABSENT_PATTERNS):
                rejections.append(_reject(
                    "remote_input_activity", "active", "no_remote_input", "fresh_remote_input_active",
                    "文本否认远程输入，但当前有新鲜远程输入",
                ))
            elif remote_fact.value == "idle" and _contains_assertive(text, _REMOTE_ACTIVE_PATTERNS):
                rejections.append(_reject(
                    "remote_input_activity", "idle", "remote_input", "fresh_remote_input_idle",
                    "文本明确说正在远程操作，但当前远程输入为空闲",
                ))

        location = self.get("location")
        if location.usable and location.value in {"home", "away", "work"}:
            away_claim = _contains_assertive(text, _LOCATION_AWAY_PATTERNS)
            home_claim = _contains_assertive(text, _LOCATION_HOME_PATTERNS)
            if location.value == "home" and away_claim and not home_claim:
                rejections.append(_reject(
                    "location", "home", "away/outside", "fresh_home", "文本明确说人类伙伴在外面/公司",
                ))
            elif location.value in {"away", "work"} and home_claim and not away_claim:
                rejections.append(_reject(
                    "location", location.value, "home", "fresh_away", "文本明确说人类伙伴在家",
                ))

        steps = self.get("steps_today")
        if steps.usable and isinstance(steps.value, (int, float)):
            numeric_claim = _extract_step_claim(text)
            if numeric_claim is not None and numeric_claim != int(steps.value):
                rejections.append(_reject(
                    "steps_today", str(int(steps.value)), str(numeric_claim), "fresh_steps",
                    "文本中的明确步数与今日步数不一致",
                ))
            elif float(steps.value) > 0 and _contains_assertive(text, _NO_STEPS_PATTERNS):
                rejections.append(_reject(
                    "steps_today", str(int(steps.value)), "zero/no_steps", "fresh_steps",
                    "文本明确说今天没有走动，但今日步数大于零",
                ))
            elif float(steps.value) == 0 and _contains_assertive(text, _POSITIVE_STEPS_PATTERNS):
                rejections.append(_reject(
                    "steps_today", "0", "positive_steps", "fresh_steps",
                    "文本明确说今天走过路，但今日步数为零",
                ))

        period = self.get("period")
        if period.usable and isinstance(period.value, Mapping):
            active = bool(period.value.get("active"))
            if active and _contains_assertive(text, _PERIOD_FALSE_PATTERNS):
                rejections.append(_reject(
                    "period", "active", "not_period", "fresh_period",
                    "文本否认当前生理期，但传感器确认仍在生理期",
                ))
            elif not active and _contains_assertive(text, _PERIOD_TRUE_PATTERNS):
                rejections.append(_reject(
                    "period", "inactive", "period", "fresh_period",
                    "文本明确说正在生理期，但传感器确认不在生理期",
                ))
            day = period.value.get("day")
            day_claim = _extract_period_day(text)
            if active and day is not None and day_claim is not None and day_claim != int(day):
                rejections.append(_reject(
                    "period", f"day_{int(day)}", f"day_{day_claim}", "fresh_period",
                    "文本中的生理期天数与当前记录不一致",
                ))

        return GroundingResult(accepted=not rejections, rejections=rejections)

    def validate(self, text: Any) -> GroundingResult:
        """Short alias used by output adapters and unit tests."""
        return self.validate_text(text)

    def redact_untrusted_location_context(self, text: Any) -> str:
        """Remove location-bearing context lines when a fresh authority exists.

        The push recipe also receives a frozen activity block for continuity.
        If that block contains a stale GPS/address line, asking the model to
        ignore it is weaker than removing that line before the call. The
        current contract is re-added separately as the sole location fact.
        """
        raw = str(text or "")
        location = self.get("location")
        detail = self.get("location_detail")
        if not (location.usable or detail.usable or self.get("location_place").usable):
            return raw
        markers = (
            "位置", "地点", "地址", "GPS", "WiFi", "wifi", "公司", "办公室", "外出",
            "在家", "家里", "home", "away", "office", "街道", "办事处", "小区",
            "园区", "大厦", "广场",
        )
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            tail = re.split(r"[:：]", stripped, maxsplit=1)[-1]
            address_like = bool(re.search(
                r"^[\u4e00-\u9fff]{2,}(?:街道|办事处|小区|园区|社区|大厦|广场|路|巷|号|区)[\u4e00-\u9fff0-9-]*$",
                tail,
            ))
            if any(marker in line for marker in markers) or address_like:
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def location_detail_changed_from(self, previous: Any) -> bool:
        """Whether two contracts carry different usable GPS details.

        Coarse ``away`` is intentionally insufficient here: two successive
        outdoor places must still invalidate a frozen observation.  A missing
        detail is not treated as a change because it is not evidence either
        way; callers can choose the safer fallback when one side is unknown.
        """
        old_contract = SentinelFactContract.from_dict(previous)
        for name in ("location_place", "location_environment"):
            current_place, old_place = self.get(name), old_contract.get(name)
            if current_place.value != old_place.value and (
                current_place.usable or old_place.value is not None
            ):
                return True
        current = self.get("location_detail")
        old = old_contract.get("location_detail")
        if current.value is None or old.value is None:
            return False
        # The current detail must be fresh to make a replacement decision;
        # the frozen detail may have become stale while waiting, but it still
        # identifies text that must not be replayed.
        if not current.usable:
            return False
        return str(current.value) != str(old.value)


def _reject(field_name: str, expected: str, observed: str, reason_code: str, message: str) -> GroundingRejection:
    return GroundingRejection(
        field=field_name, reason_code=reason_code, expected=expected,
        observed=observed, message=message,
    )


def _contains_assertive(text: str, patterns: Iterable[str]) -> bool:
    """Match explicit assertions, skipping questions/speculation/negated asks."""
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            left = text[max(0, match.start() - 8):match.start()]
            right = text[match.end():match.end() + 5]
            if "吗" in right or "是否" in left or "是不是" in left:
                continue
            if any(marker in left for marker in (
                "想知道", "想问", "我想", "希望", "担心", "关心", "不知道",
                "不确定", "觉得", "感觉", "似乎", "好像", "可能", "也许", "如果",
                "计划", "打算", "准备", "将要", "预备", "预计", "目标", "还差", "尚差", "等她", "等待", "想",
                "上次", "以前", "过去", "曾经",
                "声明", "说过", "告诉我",
            )):
                continue
            return True
    return False


def _extract_step_claim(text: str) -> Optional[int]:
    patterns = (
        r"(?:今天|今日)\s*(?:已经|已|累计|共)?\s*(?:走了|走过|走完|步数(?:为|是)?|累计步数(?:为|是)?|目前步数(?:为|是)?)[：:]?\s*(\d+)\s*步?",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            prefix = text[max(0, match.start() - 12):match.start()]
            if any(marker in prefix for marker in ("目标", "还差", "尚差", "计划", "打算", "准备")):
                continue
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _extract_period_day(text: str) -> Optional[int]:
    match = re.search(r"(?:生理期|经期)(?:第\s*)?(\d+)\s*天", text)
    if not match:
        return None
    prefix = text[max(0, match.start() - 8):match.start()]
    if any(marker in prefix for marker in ("上次", "以前", "过去", "曾经")):
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


# The patterns live here so both Flash layers make exactly the same decision.
_INPUT_IDLE_PATTERNS = (
    r"(?:键(?:盘鼠标)?|键鼠|鼠标|电脑|计算机|本地输入)\s*(?:当前|现在|此刻|一直|长时间|持续)?\s*(?:未(?:有)?输入|没(?:有)?输入|空闲|无输入|未操作|没有操作|没在操作|不在操作|未动|没动|没有动)",
    r"(?:当前|现在|此刻)\s*(?:没有|没|未)\s*(?:操作|使用)\s*(?:电脑|键盘|鼠标|键鼠)",
    r"(?:local\s+)?input\s*(?:idle|inactive)",
)
_PC_USE_PATTERNS = (
    r"(?:人类伙伴|你|她)\s*(?:当前|现在|此刻|还|仍|仍然|一直|持续)?\s*(?:正在|在)\s*(?:用|使用|操作)\s*(?:电脑|计算机|ChatGPT|Codex|VS\s*Code|Code|Cursor)",
    r"(?:人类伙伴|你|她)\s*(?:当前|现在|此刻)?\s*(?:正在|还在|仍在|在)\s*(?:写代码|编程|编码|玩电脑游戏|玩PC游戏)",
)
_REST_CONFLICT_PATTERNS = (
    r"(?:与|和).{0,8}(?:睡眠|小憩|休息)声明.{0,4}(?:不符|矛盾|不一致)",
    r"(?:睡眠|小憩|休息)声明.{0,4}(?:不符|矛盾|不一致)",
)
_INPUT_ACTIVE_PATTERNS = (
    r"(?:键(?:盘鼠标)?|键鼠|鼠标|电脑|计算机|本地输入)\s*(?:当前|现在|此刻|正(?:在)?|一直|持续|保持)?\s*(?:活跃|有输入|正在输入|操作中|使用中|操作)",
    r"(?<!系统)(?<!手机)(?<!后台)(?<!程序)(?<!软件)(?<!应用)(?:当前|现在|此刻|刚刚|正在)\s*(?:操作|使用)\s*(?:电脑|键盘|鼠标|键鼠)",
    r"(?:键盘|鼠标|键鼠)\s*(?:有|存在)\s*输入",
    r"(?:local\s+)?input\s*active",
)
_REMOTE_ACTIVE_PATTERNS = (
    r"(?:远程(?:桌面|控制|操作|输入)|手机远控|remote\s*(?:control|input))\s*(?:中|活跃|有输入|正在(?:控制|操作|输入)|操作)",
)
_REMOTE_ABSENT_PATTERNS = (
    r"没有远程(?:控制|操作|输入)", r"未进行远程(?:控制|操作)", r"没有人在远程",
)
_LOCATION_HOME_PATTERNS = (
    r"(?:人类伙伴|你|她|人)(?!的)\s*(?:现在|当前|此刻|目前|已经)?\s*(?:在家里|在家中|在家)(?!的)",
    r"(?:现在|当前|此刻|目前)\s*(?:在家里|在家中|在家)(?!的)",
    r"(?:刚刚|刚才|已经)\s*到家(?!的)",
    r"(?:人类伙伴|你|她|人)(?!的)\s*到家了",
)
_LOCATION_AWAY_PATTERNS = (
    r"(?:人类伙伴|你|她|人)(?!的)\s*(?:现在|当前|此刻|目前|已经)?\s*(?:在外面|在外|外出中|在公司|在办公室|不在家|离开家|公司办公)(?!的)",
    r"(?:现在|当前|此刻|目前)\s*(?:在外面|在外|外出中|在公司|在办公室|不在家|离开家|公司办公)(?!的)",
)
_NO_STEPS_PATTERNS = (
    r"(?:今天|今日)(?:一整天)?\s*(?:没(?:有)?走(?:路|动)?|未走(?:路|动)?)",
    r"(?:今天|今日)\s*步数(?:为|是)?\s*0",
)
_POSITIVE_STEPS_PATTERNS = (
    r"(?:今天|今日)\s*(?:已经|已|累计|共)?\s*(?:走了|走过|走完)\s*[1-9]\d*\s*步?",
    r"(?:今天|今日)\s*步数(?:为|是)?\s*[1-9]\d*",
    r"(?:今天|今日)\s*(?:确实)?走过路",
)
_PERIOD_FALSE_PATTERNS = (
    r"(?:现在|当前|此刻|目前)\s*(?:不在生理期|不是生理期|经期(?:已经|已)?结束|没来月经|未在经期)",
    r"(?:这次|本次)\s*经期\s*(?:已经|已)?\s*结束",
)
_PERIOD_TRUE_PATTERNS = (
    r"(?:现在|当前|此刻|目前)\s*(?:在生理期|正在生理期|经期中|来月经)",
    r"(?:这次|本次)\s*(?:正在)?\s*(?:生理期|经期|来月经)",
    r"(?:正在生理期|在生理期)",
    r"(?:经期|生理期)\s*第\s*\d+\s*天",
)


def grounding_repair_prompt(
    *,
    layer: str,
    original: str,
    contract: SentinelFactContract,
    rejections: Iterable[GroundingRejection],
    reconsider_contact: bool = False,
) -> str:
    """One bounded repair instruction shared by both model layers."""
    reasons = "；".join(item.message for item in rejections)
    return (
        f"这是哨兵{layer}的接地修复。原输出与当前新鲜事实发生明确矛盾：{reasons}。\n"
        f"{contract.to_prompt_text()}\n"
        + ("请基于修正后的事实重新判断 should_push 与 push_motivation；错误前提不代表真实联系意愿，独立的关心或人格动机仍由你判断。"
         if reconsider_contact else "请只修正上述事实矛盾，保留自然的关心/人格表达和原本的主动联系意图；")
        + "未知、陈旧或冲突事实不要补猜，不要把遗漏当错误。"
        + ("仍只输出一个 JSON 对象，字段为 observation、should_push、push_motivation。" if layer == "摘要" else "只输出准备发给人类伙伴的一条自然短消息，不要解释修复过程。")
        + f"\n原输出：{original}"
    )


def natural_care_fallback(*, should_push: bool = True) -> str:
    """Safe, sensor-free fallback after the single repair attempt."""
    if should_push:
        return "我刚刚有点惦记你，想问问你现在还好吗？"
    return "我刚刚留意到一点变化，先把这份在意放在这里。"


def natural_care_motivation(*, should_push: bool = True) -> str:
    """Sensor-free motivation used when a model's reason is contaminated."""
    if should_push:
        return "我只是想联系你，确认你现在还好吗。"
    return "暂时没有形成新的联系念头。"


def validate_grounding(text: Any, contract: SentinelFactContract) -> GroundingResult:
    """Functional adapter for callers that keep the contract separate."""
    return contract.validate_text(text)


# Backward/ergonomic alias: keep one implementation and one pattern table.
build_fact_contract = build_sentinel_fact_contract


__all__ = [
    "FactState", "FactStatus", "SentinelFact", "GroundingRejection",
    "GroundingResult", "SentinelFactContract", "build_sentinel_fact_contract",
    "build_fact_contract", "validate_grounding", "grounding_repair_prompt",
    "natural_care_fallback", "natural_care_motivation",
]
