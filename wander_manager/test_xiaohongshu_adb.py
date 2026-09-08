"""Deterministic tests for the read-only Android ADB Xiaohongshu source."""

from __future__ import annotations

import asyncio
import json
import unittest

from wander_manager.xiaohongshu_adb import (
    AdbCommandResult,
    AdbXiaohongshuSource,
    XIAOHONGSHU_LAUNCH_ACTIVITY,
    XIAOHONGSHU_PACKAGE,
    _is_usb_connection_panel,
    _keyguard_showing,
    parse_ui_dump,
    visible_cards,
)
from wander_manager.xiaohongshu_handler import BrowseXiaohongshuHandler


FEED_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation='0'>
  <node package='com.xingin.xhs' class='android.widget.FrameLayout'
        bounds='[0,0][1080,2290]' clickable='false' text='' content-desc=''>
    <node package='com.xingin.xhs' class='android.view.ViewGroup'
          bounds='[0,280][700,1300]' clickable='true' text='猫咪收纳攻略'
          content-desc='猫咪收纳攻略 一平米也能放下猫用品' resource-id='xhs-card'/>
  </node>
</hierarchy>"""

DETAIL_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation='0'>
  <node package='com.xingin.xhs' class='android.widget.FrameLayout'
        bounds='[0,0][1080,2290]' clickable='false' text='' content-desc=''>
    <node package='com.xingin.xhs' class='android.widget.ScrollView'
          bounds='[0,120][1080,2150]' clickable='false' text=''
          content-desc=''>
      <node package='com.xingin.xhs' class='android.widget.TextView'
            bounds='[60,240][1020,600]' clickable='false'
            text='猫咪收纳攻略 一平米也能放下猫用品 先按常用程度分区，再把猫砂和清洁用品放在通风位置。'
            content-desc='猫咪收纳攻略 一平米也能放下猫用品 先按常用程度分区，再把猫砂和清洁用品放在通风位置。'/>
    </node>
  </node>
</hierarchy>"""

CONSENT_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation='0'>
  <node package='com.xingin.xhs' class='android.widget.FrameLayout'
        bounds='[0,0][1080,2290]' clickable='false' text='' content-desc=''>
    <node package='com.xingin.xhs' class='android.widget.TextView'
          bounds='[200,600][900,760]' clickable='false' text='个人信息保护提示'
          content-desc='个人信息保护提示'/>
    <node package='com.xingin.xhs' class='android.widget.Button'
          bounds='[200,1600][900,1740]' clickable='true' text='同意'
          content-desc='同意'/>
  </node>
</hierarchy>"""

USB_PANEL_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation='0'>
  <node package='com.android.systemui' class='android.widget.FrameLayout'
        bounds='[0,0][1080,2290]' text='' content-desc=''>
    <node package='com.android.systemui' class='android.widget.TextView'
          bounds='[80,500][900,620]' text='USB 用于' content-desc=''/>
    <node package='com.android.systemui' class='android.widget.TextView'
          bounds='[80,700][900,820]' text='仅充电' content-desc=''/>
  </node>
</hierarchy>"""


class FakeAdb:
    def __init__(self, *, battery: str, dumps: list[str], screenshot: bytes = b"png"):
        self.battery = battery
        self.dumps = list(dumps)
        self.screenshot = screenshot
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, _timeout=0.0):
        args = tuple(argv)
        self.calls.append(args)
        if args == ("adb", "devices"):
            return AdbCommandResult(stdout="List of devices attached\nopaque-phone\tdevice\n")
        if args[-2:] == ("dumpsys", "battery"):
            return AdbCommandResult(stdout=self.battery)
        if args[-1:] == ("get-state",):
            return AdbCommandResult(stdout="device\n")
        if args[-3:] == ("dumpsys", "window", "policy"):
            return AdbCommandResult(stdout="mShowingLockscreen=false\n")
        if args[-3:] == ("uiautomator", "dump", "/sdcard/mirrow_xhs_ui.xml"):
            return AdbCommandResult(stdout="UI hierchary dumped to: /sdcard/mirrow_xhs_ui.xml\n")
        if args[-2:] == ("cat", "/sdcard/mirrow_xhs_ui.xml"):
            # The first cat is the feed and the second is the post detail.
            return AdbCommandResult(stdout=self.dumps.pop(0))
        if args[-2:] == ("screencap", "-p"):
            return AdbCommandResult(stdout=self.screenshot)
        return AdbCommandResult(stdout="")


class XiaohongshuAdbTests(unittest.TestCase):
    def test_recognizes_oem_keyguard_showing_field(self):
        self.assertTrue(_keyguard_showing(
            "KeyguardServiceDelegate\n  showing=true\n  showingAndNotOccluded=true"
        ))
        self.assertFalse(_keyguard_showing("showingAndNotOccluded=true"))

    def test_recognizes_only_known_system_usb_panel(self):
        self.assertTrue(_is_usb_connection_panel(parse_ui_dump(USB_PANEL_XML)))
        lockscreen = USB_PANEL_XML.replace("USB 用于", "乐划锁屏")
        self.assertFalse(_is_usb_connection_panel(parse_ui_dump(lockscreen)))

    def test_dismisses_known_usb_panel_once_before_reading_feed(self):
        runner = FakeAdb(
            battery="level: 100\nstatus: 5\nplugged: 2\n",
            dumps=[USB_PANEL_XML, FEED_XML, DETAIL_XML],
        )
        source = AdbXiaohongshuSource(
            command_runner=runner,
            launch_wait_seconds=0,
            post_wait_seconds=0,
            min_detail_text=0,
            randomizer=type("Choice", (), {"choice": staticmethod(lambda values: values[0])})(),
            sleeper=lambda _seconds: asyncio.sleep(0),
        )

        payload = asyncio.run(source.fetch_one())

        self.assertEqual("success", payload["status"])
        back_calls = [args for args in runner.calls if args[-2:] == ("keyevent", "KEYCODE_BACK")]
        self.assertEqual(2, len(back_calls))  # USB sheet, then post detail.

    def test_filters_search_tabs_and_nested_seller_targets(self):
        xml = """<hierarchy bounds='[0,0][1080,2290]'>
          <node package='com.xingin.xhs' clickable='true' bounds='[12,266][228,398]' text='全部'/>
          <node package='com.xingin.xhs' clickable='true' bounds='[15,544][533,1481]' text='猫粮收纳 ¥22.9 已售49'>
            <node package='com.xingin.xhs' clickable='true' bounds='[45,1397][503,1451]' text='某店铺'/>
          </node>
          <node package='com.xingin.xhs' clickable='true' bounds='[396,266][564,398]' text='商品'/>
          <node package='com.xingin.xhs' clickable='true' bounds='[360,398][534,530]' text='小推车'/>
        </hierarchy>"""
        cards = visible_cards(parse_ui_dump(xml))
        self.assertEqual(1, len(cards))
        self.assertTrue(cards[0].text.startswith("猫粮收纳 ¥22.9 已售49"))
        self.assertNotEqual((45, 1397, 503, 1451), cards[0].bounds)

    def test_reads_one_real_screen_and_uses_search_deep_link(self):
        runner = FakeAdb(
            battery="level: 53\nstatus: 2\nplugged: 1\nUSB powered: true\n",
            dumps=[FEED_XML, DETAIL_XML],
            screenshot=b"real-png-bytes",
        )
        source = AdbXiaohongshuSource(
            command_runner=runner,
            launch_wait_seconds=0,
            post_wait_seconds=0,
            min_detail_text=0,
            randomizer=type("Choice", (), {"choice": staticmethod(lambda values: values[0])})(),
            sleeper=lambda _seconds: asyncio.sleep(0),
        )

        async def vision(_image, _prompt, **_kwargs):
            return {
                "model": "flash-vision-test",
                "content": json.dumps({
                    "title": "猫咪收纳攻略",
                    "summary": "画面中的帖子介绍了小空间猫用品的分区收纳。",
                    "reflection": "这个方法挺实用。",
                }, ensure_ascii=False),
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

        payload = asyncio.run(BrowseXiaohongshuHandler(
            device_source=source,
            vision_func=vision,
        ).fetch_one_post(activity_reason="看看猫咪收纳"))
        self.assertEqual("success", payload["status"])
        self.assertEqual("猫咪收纳", payload["query"])
        self.assertEqual("xiaohongshu_android_adb", payload["source_provider"])
        self.assertIn("分区收纳", payload["content_summary"])
        self.assertIn("猫咪收纳攻略", payload["evidence_excerpt"])
        self.assertNotIn("screenshot_base64", payload)
        self.assertTrue(payload["screenshot_captured"])
        self.assertEqual(0, payload["swipe_count"])

        deep_links = [args for args in runner.calls if "xhsdiscover://search/result?keyword=" in " ".join(args)]
        self.assertEqual(1, len(deep_links))
        # No model-controlled shell string or interaction action is exposed.
        command_text = " ".join(" ".join(args) for args in runner.calls)
        for forbidden in ("like", "collect", "follow", "comment", "publish", "cookie", "run-as"):
            self.assertNotIn(forbidden, command_text.lower())

    def test_low_battery_skips_before_wake_or_input(self):
        runner = FakeAdb(
            battery="level: 12\nstatus: 3\nplugged: 0\nUSB powered: false\n",
            dumps=[],
        )
        source = AdbXiaohongshuSource(command_runner=runner)
        payload = asyncio.run(source.fetch_one())
        self.assertEqual("battery_low", payload["status"])
        self.assertEqual(12, payload["battery"]["level"])
        self.assertFalse(any("input" in args for args in runner.calls))

    def test_homefeed_uses_fixed_launcher_component(self):
        runner = FakeAdb(
            battery="level: 53\nstatus: 2\nplugged: 1\n",
            dumps=[FEED_XML, DETAIL_XML],
        )
        source = AdbXiaohongshuSource(
            command_runner=runner,
            launch_wait_seconds=0,
            post_wait_seconds=0,
            min_detail_text=0,
            sleeper=lambda _seconds: asyncio.sleep(0),
        )

        payload = asyncio.run(source.fetch_one())
        self.assertEqual("success", payload["status"])
        launch_calls = [
            args for args in runner.calls
            if "android.intent.action.MAIN" in args
        ]
        self.assertEqual(1, len(launch_calls))
        self.assertIn(
            f"{XIAOHONGSHU_PACKAGE}/{XIAOHONGSHU_LAUNCH_ACTIVITY}",
            launch_calls[0],
        )

    def test_command_boundary_rejects_arbitrary_shell_and_social_actions(self):
        self.assertTrue(AdbXiaohongshuSource._allowed_argv(
            (
                "adb", "-s", "opaque-phone", "shell", "am", "start", "-W",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", "-n",
                f"{XIAOHONGSHU_PACKAGE}/{XIAOHONGSHU_LAUNCH_ACTIVITY}",
            )
        ))
        self.assertFalse(AdbXiaohongshuSource._allowed_argv(
            ("adb", "-s", "opaque-phone", "shell", "rm", "-rf", "/")
        ))
        self.assertFalse(AdbXiaohongshuSource._allowed_argv(
            ("adb", "-s", "opaque-phone", "shell", "input", "tap", "1", "2", "like")
        ))

    def test_consent_gate_does_not_tap_the_consent_button(self):
        runner = FakeAdb(
            battery="level: 53\nstatus: 2\nplugged: 1\n",
            dumps=[CONSENT_XML],
        )
        source = AdbXiaohongshuSource(
            command_runner=runner,
            launch_wait_seconds=0,
            post_wait_seconds=0,
            sleeper=lambda _seconds: asyncio.sleep(0),
        )
        payload = asyncio.run(source.fetch_one())
        self.assertEqual("consent_required", payload["status"])
        input_calls = [args for args in runner.calls if "input" in args]
        self.assertEqual(1, len(input_calls))
        self.assertIn("KEYCODE_WAKEUP", input_calls[0])
        self.assertFalse(any("tap" in args or "swipe" in args for args in input_calls))


if __name__ == "__main__":
    unittest.main()
