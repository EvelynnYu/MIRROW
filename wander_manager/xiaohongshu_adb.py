"""Bounded, read-only Xiaohongshu acquisition through a dedicated ADB phone.

The phone is treated as a small body for the ``browse_xiaohongshu`` event.  A
single node may wake the phone, open the official app, choose one visible feed
card, open it, and take a bounded number of screenshots/UI dumps.  This module
deliberately exposes no generic shell entry point: every command is assembled
from a fixed allow-list below.

The source returns screen material only.  It never reads cookies or private
app storage and never invokes interaction controls such as like, collect,
follow, comment, publish, or share.  ADB serials are internal routing data and
are never included in result dictionaries or logs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import random
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import quote

from wander_manager.device_lease import DeviceLeaseManager


logger = logging.getLogger(__name__)

XIAOHONGSHU_PACKAGE = "com.xingin.xhs"
# The installed app's launcher resolver currently maps MAIN/LAUNCHER to this
# exported activity. Keeping the component fixed avoids the OEM-specific
# ``am start -p`` resolver path, which reports a false "unable to resolve"
# result on the dedicated phone.
XIAOHONGSHU_LAUNCH_ACTIVITY = ".index.v2.IndexActivityV2"
XHS_DEEP_LINK_SCHEME = "xhsdiscover"
REMOTE_UI_DUMP_PATH = "/sdcard/mirrow_xhs_ui.xml"

# The source is intentionally conservative.  A single call is enough for a
# node, and a tiny wait gives the app time to draw without keeping the display
# awake or creating a retry loop.
# UIAutomator on the dedicated OEM phone can take a little over eight seconds
# while XHS redraws a search/detail page.  Keep the command bounded, but leave
# enough headroom for one truthful read instead of turning a healthy device
# into a false ``ui_dump_failed`` result.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15.0
DEFAULT_LAUNCH_WAIT_SECONDS = 1.2
DEFAULT_POST_WAIT_SECONDS = 1.4
DEFAULT_MAX_SWIPES = 1
DEFAULT_MIN_DETAIL_TEXT = 80


@dataclass(frozen=True)
class AdbCommandResult:
    """Small subprocess result contract used by the source and its tests."""

    returncode: int = 0
    stdout: str | bytes = ""
    stderr: str | bytes = ""


CommandRunner = Callable[[Sequence[str], float], Any]


@dataclass(frozen=True)
class BatterySnapshot:
    level: int | None
    status: int | None
    plugged: int | None
    charging: bool

    def to_dict(self) -> dict[str, Any]:
        # Do not include the raw dumpsys text: OEM dumps often contain
        # identifiers unrelated to the battery state.
        return {
            "level": self.level,
            "status": self.status,
            "plugged": self.plugged,
            "charging": self.charging,
        }


@dataclass(frozen=True)
class UiCard:
    """A visible, bounded tap target derived from the UI hierarchy."""

    text: str
    bounds: tuple[int, int, int, int]
    resource_id: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def fingerprint(self) -> str:
        raw = f"{self.text}|{self.bounds[0]},{self.bounds[1]},{self.bounds[2]},{self.bounds[3]}"
        return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:24]


@dataclass
class UiSnapshot:
    root: ET.Element | None
    text: str
    packages: frozenset[str] = frozenset()
    width: int = 1080
    height: int = 2290
    raw_xml: str = ""

    @property
    def available(self) -> bool:
        return self.root is not None


@dataclass
class AdbSourceResult:
    """Acquisition result consumed by :mod:`xiaohongshu_handler`."""

    status: str
    source_id: str = ""
    source_url: str = ""
    query: str = ""
    title: str = ""
    ui_text: str = ""
    screenshot_base64: str = ""
    home_screenshot_base64: str = ""
    screenshot_sha256: str = ""
    screenshot_bytes: int = 0
    swipe_count: int = 0
    battery: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_screenshot: bool = False) -> dict[str, Any]:
        result = {
            "status": self.status,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "query": self.query,
            "title": self.title,
            "ui_text": self.ui_text,
            "screenshot_sha256": self.screenshot_sha256,
            "screenshot_bytes": self.screenshot_bytes,
            "screenshot_captured": bool(self.screenshot_base64),
            "swipe_count": self.swipe_count,
            "battery": dict(self.battery),
            "error": self.error,
        }
        if self.details:
            result["details"] = dict(self.details)
        # The screenshot is only for the immediate vision call.  Callers must
        # opt in explicitly; normal event persistence never stores it.
        if include_screenshot and self.screenshot_base64:
            result["screenshot_base64"] = self.screenshot_base64
        if include_screenshot and self.home_screenshot_base64:
            result["home_screenshot_base64"] = self.home_screenshot_base64
        return result


def _default_runner(argv: Sequence[str], timeout_seconds: float) -> AdbCommandResult:
    """Run one fixed argv vector without invoking a shell."""

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        timeout=max(1.0, float(timeout_seconds)),
        shell=False,
    )
    return AdbCommandResult(
        returncode=int(completed.returncode),
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
    )


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _first_int(pattern: str, value: str) -> int | None:
    match = re.search(pattern, value, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def parse_battery_dump(raw: str) -> BatterySnapshot:
    """Parse the stable battery fields emitted by Android ``dumpsys battery``."""

    level = _first_int(r"^\s*(?:level|mLevel)\s*[:=]\s*(\d+)", raw)
    status = _first_int(r"^\s*(?:status|mStatus)\s*[:=]\s*(\d+)", raw)
    plugged = _first_int(r"^\s*(?:plugged|mPlugged)\s*[:=]\s*(\d+)", raw)

    true_power_fields = {
        name
        for name, value in re.findall(
            r"^\s*(AC powered|USB powered|Wireless powered)\s*:\s*(true|false)",
            raw,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if value.lower() == "true"
    }
    charging = bool(true_power_fields) or (plugged is not None and plugged > 0)
    # Android BatteryManager: CHARGING=2, FULL=5.  Some OEM builds omit
    # ``plugged`` while still reporting a charging/full status.
    if status in {2, 5}:
        charging = True
    return BatterySnapshot(level=level, status=status, plugged=plugged, charging=charging)


def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value.strip())
    if not match:
        return None
    try:
        x1, y1, x2, y2 = (int(group) for group in match.groups())
    except ValueError:
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _normalize_ui_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _iter_elements(root: ET.Element) -> Iterable[ET.Element]:
    yield root
    yield from root.iter()


def _element_text(element: ET.Element, cap: int = 220) -> str:
    pieces: list[str] = []
    for child in element.iter():
        for key in ("text", "content-desc"):
            value = _normalize_ui_text(child.attrib.get(key, ""))
            if value and value not in pieces:
                pieces.append(value)
                if sum(len(item) for item in pieces) >= cap:
                    break
        if sum(len(item) for item in pieces) >= cap:
            break
    return _normalize_ui_text(" ".join(pieces))[:cap]


def parse_ui_dump(raw_xml: str) -> UiSnapshot:
    """Parse a UIAutomator dump into capped text and a safe XML tree."""

    if not raw_xml or "<hierarchy" not in raw_xml:
        return UiSnapshot(root=None, text="", raw_xml="")
    try:
        root = ET.fromstring(raw_xml)
    except (ET.ParseError, ValueError):
        return UiSnapshot(root=None, text="", raw_xml="")

    values: list[str] = []
    packages: set[str] = set()
    for element in root.iter():
        package = _normalize_ui_text(element.attrib.get("package", ""))
        if package:
            packages.add(package)
        for key in ("text", "content-desc"):
            value = _normalize_ui_text(element.attrib.get(key, ""))
            if value and value not in values:
                values.append(value)
    text = " ".join(values)
    # The hierarchy root normally carries the real display bounds.
    root_bounds = _parse_bounds(root.attrib.get("bounds", ""))
    width, height = (root_bounds[2], root_bounds[3]) if root_bounds else (1080, 2290)
    return UiSnapshot(
        root=root,
        text=text[:8000],
        packages=frozenset(packages),
        width=max(1, width),
        height=max(1, height),
        raw_xml=raw_xml,
    )


def visible_cards(snapshot: UiSnapshot) -> list[UiCard]:
    """Return conservative, visible, clickable feed candidates.

    XHS resource IDs are frequently obfuscated and vary by release.  Bounds,
    package, clickability and the presence of readable text are the stable
    evidence available to a UI-only adapter.  A card covering almost the whole
    screen is rejected so a modal/root container can never be tapped by this
    method.
    """

    if snapshot.root is None:
        return []
    screen_area = max(1, snapshot.width * snapshot.height)
    navigation_words = {
        "首页", "发现", "消息", "我", "搜索", "首页推荐", "关注", "通知",
        "登录", "登陆", "发布", "拍摄", "返回", "关闭", "取消", "确定",
        # Search/filter tabs and sort chips are clickable on current XHS
        # builds, but they are navigation controls rather than post cards.
        "全部", "用户", "商品", "图片", "视频", "地点", "综合", "店铺",
        "秋上新", "直播", "销量", "价格升序", "价格降序", "筛选",
    }
    cards: list[UiCard] = []
    seen_bounds: set[tuple[int, int, int, int]] = set()
    parents: dict[int, ET.Element] = {}
    for parent in snapshot.root.iter():
        for child in list(parent):
            parents[id(child)] = parent
    for element in snapshot.root.iter():
        if element.attrib.get("package") not in {"", XIAOHONGSHU_PACKAGE}:
            continue
        if element.attrib.get("clickable", "false").lower() != "true":
            continue
        bounds = _parse_bounds(element.attrib.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        area = (x2 - x1) * (y2 - y1)
        if area < 12_000 or area > screen_area * 0.86:
            continue
        # The masonry feed can render a real card down to the top edge of the
        # bottom navigation bar.  Exclude controls that start inside that
        # bar, while retaining a card whose safe tap center is still in the
        # content area; rejecting the whole last row makes a three-post batch
        # report ``no_new_feed_cards`` on an otherwise populated feed.
        if y1 < 120 or y1 >= snapshot.height - 120:
            continue
        # Search suggestions/tabs are short horizontal chips (typically
        # 132px high on the phone) and their labels are dynamic, so a fixed
        # vocabulary cannot safely identify all of them.  Real feed cards
        # occupy a substantially taller bounded region.
        if (y2 - y1) < 220:
            continue
        # A seller/avatar button is often a nested clickable child of the
        # actual post card.  Prefer the outer card so a random browse node
        # cannot accidentally navigate to a seller/profile target.
        ancestor = parents.get(id(element))
        nested_in_card = False
        while ancestor is not None:
            if ancestor.attrib.get("package") in {"", XIAOHONGSHU_PACKAGE} \
                    and ancestor.attrib.get("clickable", "false").lower() == "true":
                ancestor_bounds = _parse_bounds(ancestor.attrib.get("bounds", ""))
                if ancestor_bounds is not None:
                    ancestor_area = (ancestor_bounds[2] - ancestor_bounds[0]) * (ancestor_bounds[3] - ancestor_bounds[1])
                    if ancestor_area > area:
                        nested_in_card = True
                        break
            ancestor = parents.get(id(ancestor))
        if nested_in_card:
            continue
        text = _element_text(element)
        if not text:
            continue
        words = set(text.split())
        if text in navigation_words or (words and words.issubset(navigation_words)):
            continue
        if any(marker in text for marker in ("验证码", "安全验证", "账号异常", "风控")):
            continue
        if bounds in seen_bounds:
            continue
        seen_bounds.add(bounds)
        cards.append(UiCard(
            text=text,
            bounds=bounds,
            resource_id=element.attrib.get("resource-id", ""),
        ))
    return cards


class AdbXiaohongshuSource:
    """A single bounded read-only browsing session on one USB ADB device."""

    def __init__(
        self,
        *,
        serial: str | None = None,
        command_runner: CommandRunner | None = None,
        randomizer: random.Random | Any | None = None,
        sleeper: Callable[[float], Awaitable[Any]] | None = None,
        command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        launch_wait_seconds: float = DEFAULT_LAUNCH_WAIT_SECONDS,
        post_wait_seconds: float = DEFAULT_POST_WAIT_SECONDS,
        max_swipes: int = DEFAULT_MAX_SWIPES,
        min_detail_text: int = DEFAULT_MIN_DETAIL_TEXT,
        lease_manager: DeviceLeaseManager | None = None,
    ) -> None:
        configured = serial if serial is not None else os.getenv("MIRROW_XHS_ADB_SERIAL", "")
        # A configured serial is accepted only as an opaque routing token.  It
        # is never copied into a result or logger message.
        self._configured_serial = str(configured or "").strip()
        self._runner = command_runner or _default_runner
        self._random = randomizer or random.SystemRandom()
        self._sleeper = sleeper or asyncio.sleep
        self.command_timeout_seconds = max(1.0, float(command_timeout_seconds))
        self.launch_wait_seconds = max(0.0, float(launch_wait_seconds))
        self.post_wait_seconds = max(0.0, float(post_wait_seconds))
        self.max_swipes = max(0, min(int(max_swipes), 2))
        self.min_detail_text = max(0, int(min_detail_text))
        self.lease_manager = lease_manager or DeviceLeaseManager(device_key="dedicated_android_phone")

    async def fetch_one(
        self,
        *,
        query: str = "",
        exclude_ids: set[str] | None = None,
        capture_home_screenshot: bool = False,
    ) -> dict[str, Any]:
        lease = await self.lease_manager.acquire_async("xhs_browse", timeout=0)
        try:
            return await self._fetch_one(query=query, exclude_ids=exclude_ids, capture_home_screenshot=capture_home_screenshot)
        finally:
            lease.release()

    async def _fetch_one(
        self,
        *,
        query: str = "",
        exclude_ids: set[str] | None = None,
        capture_home_screenshot: bool = False,
    ) -> dict[str, Any]:
        """Open the feed/search page, read one visible post, and return material."""

        serial, device_status = await self._select_device()
        if not serial:
            return AdbSourceResult(status=device_status, error=device_status).to_dict()

        battery = await self._read_battery(serial)
        if battery is None:
            return AdbSourceResult(status="battery_unavailable", error="battery_unavailable").to_dict()
        if battery.level is None:
            return AdbSourceResult(
                status="battery_unavailable", battery=battery.to_dict(), error="battery_level_unknown"
            ).to_dict()
        # Explicit user requirement: below 20% and not charging means a
        # truthful skip, and crucially no wake/input action is issued.
        if battery.level < 20 and not battery.charging:
            return AdbSourceResult(
                status="battery_low", battery=battery.to_dict(), error="battery_below_20_not_charging"
            ).to_dict()

        state = await self._device_state(serial)
        if state != "device":
            return AdbSourceResult(
                status="device_disconnected", battery=battery.to_dict(), error="device_not_ready"
            ).to_dict()

        wake_status = await self._wake_and_unlock(serial)
        if wake_status != "ok":
            return AdbSourceResult(
                status=wake_status, battery=battery.to_dict(), error=wake_status
            ).to_dict()

        cleaned_query = _normalize_ui_text(query)[:80]
        launch_uri = ""
        if cleaned_query:
            launch_uri = (
                f"{XHS_DEEP_LINK_SCHEME}://search/result?keyword="
                f"{quote(cleaned_query, safe='')}"
            )
            launch_args = (
                "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
                "-d", launch_uri, "-p", XIAOHONGSHU_PACKAGE,
            )
        else:
            launch_args = (
                "shell", "am", "start", "-W", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", "-n",
                f"{XIAOHONGSHU_PACKAGE}/{XIAOHONGSHU_LAUNCH_ACTIVITY}",
            )
        launched = await self._run_device(serial, *launch_args)
        if launched is None or launched.returncode != 0:
            return AdbSourceResult(
                status="app_launch_failed", query=cleaned_query,
                source_url=launch_uri, battery=battery.to_dict(), error="app_launch_failed"
            ).to_dict()

        await self._sleeper(self.launch_wait_seconds)
        feed_snapshot = await self._dump_ui(serial)
        if _is_usb_connection_panel(feed_snapshot):
            # This dedicated OEM phone opens Android's USB-mode panel after a
            # fresh cable/ADB connection.  It is a known system overlay, not
            # XHS content: dismiss it with one bounded Back, then inspect the
            # real app hierarchy.  Every other SystemUI surface remains a
            # truthful popup blocker.
            dismissed = await self._run_device(
                serial, "shell", "input", "keyevent", "KEYCODE_BACK"
            )
            if dismissed is not None and dismissed.returncode == 0:
                await self._sleeper(self.post_wait_seconds)
                feed_snapshot = await self._dump_ui(serial)
        guard = self._guard_snapshot(feed_snapshot)
        if guard:
            return AdbSourceResult(
                status=guard, query=cleaned_query, source_url=launch_uri,
                battery=battery.to_dict(), ui_text=feed_snapshot.text,
                error=guard,
            ).to_dict()

        cards = visible_cards(feed_snapshot)
        # A dedicated phone can retain the last opened post when the process
        # is resumed. Treat that detail screen as a navigation state, not as
        # an absent feed: one fixed Back returns to the list underneath it.
        # There is deliberately no retry loop; if the recovered hierarchy is
        # still not a feed/search page, the caller receives a specific barrier.
        recovered_from_detail = False
        if not cards and _looks_like_detail(feed_snapshot):
            recovered_from_detail = True
            restored = await self._run_device(
                serial, "shell", "input", "keyevent", "KEYCODE_BACK"
            )
            if restored is not None and restored.returncode == 0:
                await self._sleeper(self.post_wait_seconds)
                feed_snapshot = await self._dump_ui(serial)
                guard = self._guard_snapshot(feed_snapshot)
                if guard:
                    return AdbSourceResult(
                        status=guard, query=cleaned_query, source_url=launch_uri,
                        battery=battery.to_dict(), ui_text=feed_snapshot.text,
                        error=guard,
                        details={"feed_recovery": "detail_back"},
                    ).to_dict()
                cards = visible_cards(feed_snapshot)
        if not cards:
            return AdbSourceResult(
                status="no_feed_cards", query=cleaned_query, source_url=launch_uri,
                battery=battery.to_dict(), ui_text=feed_snapshot.text,
                error="no_visible_readable_feed_card",
                details=(
                    {"feed_recovery": "detail_back"}
                    if recovered_from_detail else {}
                ),
            ).to_dict()

        excluded = set(exclude_ids or set())
        available_cards = [card for card in cards if card.fingerprint not in excluded]
        if not available_cards:
            return AdbSourceResult(
                status="no_new_feed_cards", query=cleaned_query, source_url=launch_uri,
                battery=battery.to_dict(), ui_text=feed_snapshot.text,
                error="all_visible_cards_already_seen",
            ).to_dict()
        card = self._random.choice(available_cards)
        # Comment handoff, when explicitly requested by AI, needs the original
        # feed/card frame.  It is kept transient and only exposed through the
        # opt-in screenshot field; normal browsing never captures it.
        home_screenshot = ""
        if capture_home_screenshot:
            home_screenshot = await self._screenshot(serial) or ""
        x, y = card.center
        tapped = await self._run_device(
            serial, "shell", "input", "tap", str(x), str(y)
        )
        if tapped is None or tapped.returncode != 0:
            return AdbSourceResult(
                status="post_open_failed", query=cleaned_query, source_url=launch_uri,
                title=card.text[:200], battery=battery.to_dict(), error="post_open_failed"
            ).to_dict()

        await self._sleeper(self.post_wait_seconds)
        detail_snapshot = await self._dump_ui(serial)
        guard = self._guard_snapshot(detail_snapshot)
        if guard:
            await self._return_to_feed(serial, detail_snapshot, force=True)
            return AdbSourceResult(
                status=guard, query=cleaned_query, source_url=launch_uri,
                title=card.text[:200], battery=battery.to_dict(),
                ui_text=detail_snapshot.text, error=guard,
            ).to_dict()

        screenshot = await self._screenshot(serial)
        if screenshot is None:
            await self._return_to_feed(serial, detail_snapshot, force=True)
            return AdbSourceResult(
                status="screenshot_failed", query=cleaned_query, source_url=launch_uri,
                source_id=card.fingerprint, title=card.text[:200],
                battery=battery.to_dict(), ui_text=detail_snapshot.text,
                error="screenshot_failed",
            ).to_dict()
        swipe_count = 0

        # A single bounded read-more gesture is allowed only when the detail
        # hierarchy is sparse.  We do not swipe indefinitely or retry a failed
        # gesture.  Every swipe is a fixed, non-semantic ADB input action.
        if self.max_swipes and len(detail_snapshot.text) < self.min_detail_text:
            swipe = await self._run_device(
                serial, "shell", "input", "swipe", "540", "1900", "540", "700", "280"
            )
            if swipe is not None and swipe.returncode == 0:
                swipe_count = 1
                await self._sleeper(self.post_wait_seconds)
                after_swipe = await self._dump_ui(serial)
                guard = self._guard_snapshot(after_swipe)
                if guard:
                    await self._return_to_feed(serial, after_swipe, force=True)
                    return AdbSourceResult(
                        status=guard, query=cleaned_query, source_url=launch_uri,
                        source_id=card.fingerprint, title=card.text[:200],
                        battery=battery.to_dict(), ui_text=after_swipe.text,
                        swipe_count=swipe_count, error=guard,
                    ).to_dict()
                detail_snapshot = after_swipe
                refreshed = await self._screenshot(serial)
                if refreshed is not None:
                    screenshot = refreshed

        screenshot_bytes = _decode_base64_bytes(screenshot)
        screenshot_sha = hashlib.sha256(screenshot_bytes).hexdigest() if screenshot_bytes else ""
        # The card title is evidence from the feed hierarchy; prefer a detail
        # line when present but never derive a title from model output.
        title = self._pick_title(detail_snapshot.text, fallback=card.text)
        source_id = card.fingerprint
        # Leave the dedicated phone on the list/search page so the next
        # independent node can choose another visible card. This is one fixed
        # navigation action, never a semantic or social interaction.
        feed_return = await self._return_to_feed(serial, detail_snapshot, force=True)
        return AdbSourceResult(
            status="success",
            source_id=source_id,
            source_url=launch_uri or f"{XHS_DEEP_LINK_SCHEME}://homefeed",
            query=cleaned_query,
            title=title[:200],
            ui_text=detail_snapshot.text[:6000],
            screenshot_base64=screenshot,
            home_screenshot_base64=home_screenshot,
            screenshot_sha256=screenshot_sha,
            screenshot_bytes=len(screenshot_bytes),
            swipe_count=swipe_count,
            battery=battery.to_dict(),
            details={
                "selected_bounds": card.bounds,
                "selected_card_text": card.text[:500],
                "screen_size": [detail_snapshot.width, detail_snapshot.height],
                "feed_return": feed_return,
            },
        ).to_dict(include_screenshot=True)

    async def _invoke(self, argv: Sequence[str]) -> AdbCommandResult | None:
        if not self._allowed_argv(argv):
            # The model never supplies argv, but keeping the guard here makes
            # the module boundary explicit even if a future caller reuses a
            # private helper incorrectly.
            logger.warning("blocked non-whitelisted ADB operation")
            return None
        try:
            result = self._runner(argv, self.command_timeout_seconds)
        except TypeError:
            # Tiny test doubles often accept only argv.  This compatibility
            # branch does not alter the production fixed-argv boundary.
            try:
                result = self._runner(argv)  # type: ignore[misc]
            except Exception as exc:
                logger.debug("ADB command unavailable: %s", type(exc).__name__)
                return None
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("ADB command unavailable: %s", type(exc).__name__)
            return None
        except Exception as exc:
            logger.debug("ADB command failed: %s", type(exc).__name__)
            return None
        try:
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, AdbCommandResult):
                return result
            return AdbCommandResult(
                returncode=int(getattr(result, "returncode", 0) or 0),
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
            )
        except Exception as exc:
            logger.debug("ADB result invalid: %s", type(exc).__name__)
            return None

    async def _run_device(self, serial: str, *args: str) -> AdbCommandResult | None:
        # All callers pass literals from the allow-list.  No public method
        # accepts a shell string or arbitrary command supplied by the model.
        return await self._invoke(("adb", "-s", serial, *args))

    @classmethod
    def _allowed_argv(cls, argv: Sequence[str]) -> bool:
        """Validate the complete ADB argv before it reaches subprocess."""

        parts = tuple(str(item) for item in argv)
        if parts == ("adb", "devices"):
            return True
        if len(parts) < 4 or parts[0:2] != ("adb", "-s") or not parts[2]:
            return False
        return cls._allowed_device_args(parts[3:])

    @staticmethod
    def _allowed_device_args(args: Sequence[str]) -> bool:
        """Fixed command allow-list; no shell, package-private data, or root."""

        parts = tuple(str(item) for item in args)
        if parts in {
            ("get-state",),
            ("shell", "dumpsys", "battery"),
            ("shell", "dumpsys", "window", "policy"),
            ("shell", "uiautomator", "dump", REMOTE_UI_DUMP_PATH),
            ("shell", "cat", REMOTE_UI_DUMP_PATH),
            ("exec-out", "screencap", "-p"),
            ("shell", "input", "keyevent", "KEYCODE_WAKEUP"),
            ("shell", "input", "keyevent", "KEYCODE_BACK"),
            ("shell", "input", "swipe", "540", "1900", "540", "700", "280"),
        }:
            return True
        if len(parts) == 5 and parts[:3] == ("shell", "input", "tap"):
            try:
                x, y = int(parts[3]), int(parts[4])
            except (TypeError, ValueError):
                return False
            return 0 <= x <= 2160 and 0 <= y <= 4320
        if len(parts) >= 10 and parts[:4] == ("shell", "am", "start", "-W"):
            # Only the two app routes built in fetch_one are valid.  URI
            # characters are already percent-encoded by the adapter.
            main = parts[4:] == (
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-n", f"{XIAOHONGSHU_PACKAGE}/{XIAOHONGSHU_LAUNCH_ACTIVITY}",
            )
            if main:
                return True
            if len(parts[4:]) != 6:
                return False
            return parts[4] == "-a" and parts[5] == "android.intent.action.VIEW" \
                and parts[6] == "-d" \
                and parts[7].startswith(f"{XHS_DEEP_LINK_SCHEME}://search/result?keyword=") \
                and parts[8] == "-p" and parts[9] == XIAOHONGSHU_PACKAGE
        return False

    async def _select_device(self) -> tuple[str | None, str]:
        result = await self._invoke(("adb", "devices"))
        if result is None:
            return None, "adb_unavailable"
        if result.returncode != 0:
            return None, "adb_unavailable"
        entries: list[tuple[str, str]] = []
        for line in _text(result.stdout).splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = re.split(r"\s+", line)
            if len(parts) < 2:
                continue
            serial, state = parts[0].strip(), parts[1].strip().lower()
            if serial:
                entries.append((serial, state))

        if self._configured_serial:
            for serial, state in entries:
                if serial == self._configured_serial:
                    if state == "device":
                        return serial, "ok"
                    if state == "unauthorized":
                        return None, "adb_unauthorized"
                    return None, "device_disconnected"
            # A configured route that is not enumerated is intentionally not
            # replaced by another phone.
            return None, "device_disconnected"

        ready = [serial for serial, state in entries if state == "device"]
        if len(ready) == 1:
            return ready[0], "ok"
        if len(ready) > 1:
            return None, "ambiguous_devices"
        if any(state == "unauthorized" for _, state in entries):
            return None, "adb_unauthorized"
        return None, "device_disconnected"

    async def _device_state(self, serial: str) -> str:
        result = await self._run_device(serial, "get-state")
        if result is None or result.returncode != 0:
            return ""
        state = _text(result.stdout).strip().splitlines()
        return state[0].strip().lower() if state else ""

    async def _read_battery(self, serial: str) -> BatterySnapshot | None:
        result = await self._run_device(serial, "shell", "dumpsys", "battery")
        if result is None or result.returncode != 0:
            return None
        return parse_battery_dump(_text(result.stdout))

    async def _wake_and_unlock(self, serial: str) -> str:
        # KEYCODE_WAKEUP is deliberately sent for every node.  It is the only
        # wake action and does not configure a wake lock/stay-awake policy.
        wake = await self._run_device(
            serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"
        )
        if wake is None or wake.returncode != 0:
            return "wake_failed"
        policy = await self._run_device(serial, "shell", "dumpsys", "window", "policy")
        policy_text = _text(policy.stdout) if policy is not None else ""
        if _keyguard_showing(policy_text):
            # The dedicated phone has no PIN/password.  Use one fixed bounded
            # swipe only when Android reports a lock screen; never guess a PIN.
            unlock = await self._run_device(
                serial, "shell", "input", "swipe", "540", "1900", "540", "700", "280"
            )
            if unlock is None or unlock.returncode != 0:
                return "unlock_failed"
            await self._sleeper(self.post_wait_seconds)
            verified = await self._run_device(
                serial, "shell", "dumpsys", "window", "policy"
            )
            verified_text = _text(verified.stdout) if verified is not None else ""
            if not verified_text or _keyguard_showing(verified_text):
                return "unlock_failed"
        return "ok"

    async def _dump_ui(self, serial: str) -> UiSnapshot:
        dumped = await self._run_device(serial, "shell", "uiautomator", "dump", REMOTE_UI_DUMP_PATH)
        if dumped is None or dumped.returncode != 0:
            return UiSnapshot(root=None, text="")
        content = await self._run_device(serial, "shell", "cat", REMOTE_UI_DUMP_PATH)
        if content is None or content.returncode != 0:
            return UiSnapshot(root=None, text="")
        return parse_ui_dump(_text(content.stdout))

    async def _screenshot(self, serial: str) -> str | None:
        result = await self._run_device(serial, "exec-out", "screencap", "-p")
        if result is None or result.returncode != 0:
            return None
        raw = result.stdout if isinstance(result.stdout, bytes) else _text(result.stdout).encode()
        if not raw:
            return None
        return base64.b64encode(raw).decode("ascii")

    async def _return_to_feed(
        self, serial: str, snapshot: UiSnapshot, *, force: bool = False
    ) -> str:
        """Return from a detail view with one fixed, bounded Back action."""

        if not force and not _looks_like_detail(snapshot):
            return "not_detail"
        result = await self._run_device(
            serial, "shell", "input", "keyevent", "KEYCODE_BACK"
        )
        if result is None or result.returncode != 0:
            return "back_failed"
        await self._sleeper(self.post_wait_seconds)
        return "detail_back"

    @staticmethod
    def _pick_title(text: str, *, fallback: str) -> str:
        lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
        ignored = {"首页", "发现", "关注", "消息", "我", "返回", "评论"}
        for line in lines:
            line = _normalize_ui_text(line)
            if line and line not in ignored and len(line) >= 2:
                return line
        return _normalize_ui_text(fallback)[:200]

    @staticmethod
    def _guard_snapshot(snapshot: UiSnapshot) -> str:
        if not snapshot.available:
            return "ui_dump_failed"
        text = snapshot.text
        lowered = text.lower()
        if any(marker in text for marker in ("个人信息保护", "隐私政策", "用户协议")):
            return "consent_required"
        if any(marker in text for marker in ("验证码", "安全验证", "人机验证", "账号异常", "风控")):
            return "verification_required" if "验证码" in text or "验证" in text else "risk_detected"
        non_xhs = {pkg for pkg in snapshot.packages if pkg and pkg != XIAOHONGSHU_PACKAGE}
        if non_xhs:
            return "popup_detected"
        if any(marker in lowered for marker in ("network error", "网络异常", "加载失败")):
            return "popup_detected"
        # ``登录`` is also a persistent corner action on anonymous feed/detail
        # pages.  It must not hide otherwise valid public evidence.  Only a
        # full login/empty screen is gated; visible cards or substantive detail
        # text are sufficient for anonymous read-only browsing.
        if "登录" in text or "登陆" in text:
            if visible_cards(snapshot) or _has_detail_evidence(text):
                return ""
            return "login_required"
        return ""


def _has_detail_evidence(text: str) -> bool:
    """Recognize bounded post text while excluding a login-only prompt."""

    compact = _normalize_ui_text(text)
    for marker in ("登录", "登陆", "注册", "手机号登录", "验证码登录"):
        compact = compact.replace(marker, " ")
    compact = _normalize_ui_text(compact)
    if not compact:
        return False
    # Login screens often contain only short labels.  A longer readable body
    # is evidence of a detail page, but remains a heuristic—not an assertion
    # that the complete post/media was read.
    return len(compact) >= max(40, DEFAULT_MIN_DETAIL_TEXT)


def _looks_like_detail(snapshot: UiSnapshot) -> bool:
    """Recognize a readable post detail without treating it as a feed card."""

    if not snapshot.available or visible_cards(snapshot):
        return False
    if _has_detail_evidence(snapshot.text):
        return True
    # Small but readable detail fixtures/OEM hierarchies often expose a
    # ScrollView/WebView with less than the normal evidence threshold.  The
    # structural marker is enough to justify one Back recovery; it is not used
    # to claim that the post itself contains sufficient evidence.
    compact = _normalize_ui_text(snapshot.text)
    if len(compact) < 24:
        return False
    return any(
        any(marker in str(element.attrib.get("class", "")).lower()
            for marker in ("scrollview", "webview"))
        for element in snapshot.root.iter()
    )


def _keyguard_showing(policy_text: str) -> bool:
    """Recognize common Android/OEM keyguard status spellings."""

    for pattern in (
        r"mshowinglockscreen\s*=\s*true",
        r"mkeyguardshowing\s*=\s*true",
        r"iskeyguardshowing\s*=\s*true",
        r"keyguardshowing\s*=\s*true",
        # Several OEM builds expose the state under
        # ``KeyguardServiceDelegate showing=true`` instead of one of the
        # m*/is* spellings above.  Keep this exact field match narrow enough
        # that ``showingAndNotOccluded`` does not count by accident.
        r"\bshowing\s*=\s*true",
        r"mdreaminglockscreen\s*=\s*true",
    ):
        if re.search(pattern, policy_text, flags=re.IGNORECASE):
            return True
    return False


def _is_usb_connection_panel(snapshot: UiSnapshot) -> bool:
    """Match only the dedicated phone's observed Android USB-mode sheet."""

    if snapshot.packages != frozenset({"com.android.systemui"}):
        return False
    text = _normalize_ui_text(snapshot.text)
    return "USB 用于" in text and any(
        marker in text for marker in ("仅充电", "传输文件", "传输照片", "USB 网络共享")
    )


def _decode_base64_bytes(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=False) if value else b""
    except (ValueError, TypeError):
        return b""


__all__ = [
    "AdbCommandResult",
    "AdbSourceResult",
    "AdbXiaohongshuSource",
    "BatterySnapshot",
    "REMOTE_UI_DUMP_PATH",
    "XIAOHONGSHU_PACKAGE",
    "XIAOHONGSHU_LAUNCH_ACTIVITY",
    "parse_battery_dump",
    "parse_ui_dump",
    "visible_cards",
]
