# 用户追踪服务
#
# 功能：
# 1. 截取用户屏幕 + 摄像头快照
# 2. 使用GLM-4V综合分析
# 3. 判断用户当前活动状态
# 4. 生成追踪报告

import base64
import httpx
import os
import logging
import asyncio
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

# 摄像头依赖检查
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("cv2库未安装，摄像头功能不可用")


class UserActivityType(Enum):
    """用户活动类型"""
    WORKING = "working"           # 工作/学习
    GAMING = "gaming"             # 游戏
    BROWSING = "browsing"         # 浏览网页
    WATCHING = "watching"         # 看视频
    CHATTING = "chatting"         # 聊天
    IDLE = "idle"                 # 空闲/无活动
    UNKNOWN = "unknown"           # 未知


@dataclass
class TrackingResult:
    """追踪结果"""
    activity_type: UserActivityType
    activity_description: str
    confidence: float  # 0-1
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    screenshot_path: Optional[str] = None
    camera_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "activity_type": self.activity_type.value,
            "activity_description": self.activity_description,
            "confidence": self.confidence,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
            "screenshot_path": self.screenshot_path,
            "camera_path": self.camera_path
        }


class UserTrackingService:
    """
    用户追踪服务

    核心逻辑：
    1. 截取屏幕
    2. 使用GLM-4V分析
    3. 返回用户活动状态
    """

    # GLM-4V分析提示词（截屏，可选摄像头）
    ANALYSIS_PROMPT = """你是一个用户活动分析助手。请分析屏幕截图{camera_hint}，判断用户当前正在做什么。
{declared_status_hint}
请严格按照以下JSON格式输出，不要输出其他内容：
{{
    "activity_type": "working/gaming/browsing/watching/chatting/idle/unknown",
    "activity_description": "简短描述用户正在做什么（20字以内）",
    "confidence": 0.0-1.0,
    "status_consistent": true/false,
    "status_mismatch_detail": "",
    "details": {{
        "app_name": "识别到的主要应用名称",
        "window_title": "窗口标题（如果可见）",
        "key_elements": ["屏幕上的关键元素1", "关键元素2"],
        "camera_observation": "摄像头观察到的内容简述（如未提供摄像头图片则填'未提供'）",
        "user_presence": "用户是否在摄像头前（是/否/不确定）"
    }}
}}

活动类型说明：
- working: 工作/学习（文档编辑、代码编写、PPT等）
- gaming: 游戏（游戏界面）
- browsing: 浏览网页（浏览器、社交媒体等）
- watching: 看视频（视频播放器、直播等）
- chatting: 聊天（聊天软件、通讯工具等）
- idle: 空闲（桌面、锁屏、无明显活动，或是停留在 MIRROW AI 伴侣应用界面上）
- unknown: 无法判断

重要识别指引：
- MIRROW 是一款 AI 伴侣桌面应用，窗口标题包含"Mirrow"。如果你看到窗口标题包含"Mirrow"或界面明显是 AI 对话界面（有 AI 头像/名称如"AI"等），这是 MIRROW 应用本身。用户停留在 MIRROW 界面上属于挂机行为，应归类为 idle，而非 chatting。
- chatting 是用户在使用其他聊天软件（微信、QQ、Discord 等）与他人聊天。

status_consistent 说明：
- 如果用户声明了状态，请判断实际活动是否与声明一致
- 一致填 true，不一致填 false（并在 status_mismatch_detail 中简述差异）
- 如果未提供声明状态，填 true，status_mismatch_detail 留空

只输出JSON，不要有其他内容。"""

    ANALYSIS_PROMPT_NO_CAMERA = """你是一个用户活动分析助手。请分析这张屏幕截图，判断用户当前正在做什么。
{declared_status_hint}
请严格按照以下JSON格式输出，不要输出其他内容：
{{
    "activity_type": "working/gaming/browsing/watching/chatting/idle/unknown",
    "activity_description": "简短描述用户正在做什么（20字以内）",
    "confidence": 0.0-1.0,
    "status_consistent": true/false,
    "status_mismatch_detail": "",
    "details": {{
        "app_name": "识别到的主要应用名称",
        "window_title": "窗口标题（如果可见）",
        "key_elements": ["屏幕上的关键元素1", "关键元素2"],
        "camera_observation": "未提供摄像头图片",
        "user_presence": "不确定"
    }}
}}

活动类型说明：
- working: 工作/学习（文档编辑、代码编写、PPT等）
- gaming: 游戏（游戏界面）
- browsing: 浏览网页（浏览器、社交媒体等）
- watching: 看视频（视频播放器、直播等）
- chatting: 聊天（聊天软件、通讯工具等）
- idle: 空闲（桌面、锁屏、无明显活动，或是停留在 MIRROW AI 伴侣应用界面上）
- unknown: 无法判断

重要识别指引：
- MIRROW 是一款 AI 伴侣桌面应用，窗口标题包含"Mirrow"。如果你看到窗口标题包含"Mirrow"或界面明显是 AI 对话界面（有 AI 头像/名称如"AI"等），这是 MIRROW 应用本身。用户停留在 MIRROW 界面上属于挂机行为，应归类为 idle，而非 chatting。
- chatting 是用户在使用其他聊天软件（微信、QQ、Discord 等）与他人聊天。

status_consistent 说明：
- 如果用户声明了状态，请判断实际活动是否与声明一致
- 一致填 true，不一致填 false（并在 status_mismatch_detail 中简述差异）
- 如果未提供声明状态，填 true，status_mismatch_detail 留空

只输出JSON，不要有其他内容。"""

    def __init__(
        self,
        save_screenshots: bool = False,
        screenshot_dir: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        """
        初始化用户追踪服务（使用统一视觉入口 call_vision_api / call_llm_api_visual）

        Args:
            save_screenshots: 是否保存截图
            screenshot_dir: 截图保存目录
            http_client: 可复用的 httpx 客户端（共享连接池 DNS 缓存）
        """
        self.save_screenshots = save_screenshots
        self.screenshot_dir = screenshot_dir or os.path.join(os.getcwd(), "tracking_screenshots")
        self._http_client = http_client
        self._track_lock = asyncio.Lock()

        # 检查截屏依赖
        self._mss_available = self._check_mss()

        if self.save_screenshots and not os.path.exists(self.screenshot_dir):
            os.makedirs(self.screenshot_dir, exist_ok=True)

        logger.info("用户追踪服务初始化完成（DS V4-Pro + GLM-4V 降级）")

    def _check_mss(self) -> bool:
        """检查mss库是否可用"""
        try:
            import mss
            return True
        except ImportError:
            logger.warning("mss库未安装，截屏功能不可用")
            return False

    # 外部注入的 MIRROW 可见性检查回调（用于前端心跳等补充信号）
    _external_mirrow_check: Optional[Callable[[], bool]] = None

    @staticmethod
    def set_mirrow_check_callback(callback: Optional[Callable[[], bool]]) -> None:
        """注入外部 MIRROW 可见性检查回调（如前端页面可见性心跳）。"""
        UserTrackingService._external_mirrow_check = callback

    @staticmethod
    def _get_foreground_window_title() -> Optional[str]:
        """获取当前前台窗口标题（Windows only）。失败返回 None。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                logger.debug(f"前台窗口标题: {buf.value}")
                return buf.value
            else:
                logger.debug(f"GetWindowTextLengthW 返回 0 (hwnd={hwnd}), 可能是 UIPI 权限隔离或桌面")
        except Exception as e:
            logger.debug(f"获取前台窗口标题异常: {e}")
        return None

    @staticmethod
    def _is_mirrow_foreground() -> bool:
        """检查前台窗口是否为 MIRROW（窗口标题 + 外部回调双信号）。"""
        # 信号1: Windows API 窗口标题
        title = UserTrackingService._get_foreground_window_title()
        if title:
            t = title.lower()
            dev_keywords = [
                "visual studio code", "visual studio", "powershell",
                "cmd.exe", "command prompt", "terminal", "windows powershell",
                "cursor", "windsurf",
            ]
            is_dev_tool = any(kw in t for kw in dev_keywords)
            if not is_dev_tool and "mirrow" in t:
                return True
        # 信号2: 前端页面可见性心跳（不依赖 Windows API，不受 UIPI 影响）
        if UserTrackingService._external_mirrow_check and UserTrackingService._external_mirrow_check():
            return True
        return False

    @staticmethod
    def _is_screen_active(img: "Image.Image") -> bool:
        """
        本地快速判断屏幕是否活跃（有第三方应用窗口 vs 桌面/锁屏/MIRROW闲置）。
        活跃屏幕色彩方差大；闲置屏幕均匀。MIRROW 界面也视为闲置（挂机）。
        """
        import numpy as np
        try:
            if UserTrackingService._is_mirrow_foreground():
                return False  # MIRROW 界面视为闲置（挂机）

            # 像素方差分析
            small = img.resize((256, int(256 * img.size[1] / img.size[0])), 2)
            arr = np.array(small, dtype=np.float32)
            std = np.std(arr)
            return std > 40.0
        except Exception:
            return True  # 判断失败时保守：认为活跃，跳过摄像头

    @staticmethod
    def _compress_image(img: "Image.Image", max_size: int = 1280, jpeg_quality: int = 70) -> str:
        """
        压缩图像并返回 base64 编码

        等比例缩放至长边不超过 max_size，转为 JPEG 减小体积。
        """
        import io
        from PIL import Image

        w, h = img.size
        scale = max_size / max(w, h)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=jpeg_quality)
        return base64.b64encode(buffer.getvalue()).decode('utf-8')

    async def capture_screen(self) -> Optional[str]:
        """
        截取屏幕并返回base64编码（压缩后）

        Returns:
            base64编码的图片，失败返回None
        """
        if not self._mss_available:
            logger.error("截屏功能不可用，请安装mss库")
            return None

        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                # 获取主显示器
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)

                # 转换为PIL Image
                img = Image.frombytes('RGB', screenshot.size, screenshot.rgb)

                # 可选：保存原始截图
                if self.save_screenshots:
                    filename = f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    filepath = os.path.join(self.screenshot_dir, filename)
                    img.save(filepath)
                    logger.debug(f"截图已保存: {filepath}")

                base64_data = self._compress_image(img, max_size=1280, jpeg_quality=70)
                return base64_data

        except Exception as e:
            logger.error(f"截屏失败: {e}")
            return None

    async def capture_camera(self, device_index: int = 0) -> Optional[str]:
        """
        使用摄像头捕获快照并返回base64编码（压缩后）

        Args:
            device_index: 摄像头设备索引，默认0

        Returns:
            base64编码的图片，失败返回None
        """
        if not CV2_AVAILABLE:
            logger.warning("cv2库未安装，摄像头功能不可用")
            return None

        loop = asyncio.get_event_loop()

        def _sync_capture() -> Optional[bytes]:
            """同步摄像头捕获，在 executor 中执行以避免阻塞事件循环。"""
            import cv2 as _cv2
            cap = _cv2.VideoCapture(device_index)
            try:
                if not cap.isOpened():
                    logger.warning(f"无法打开摄像头设备（索引 {device_index}）")
                    return None

                cap.set(_cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(_cv2.CAP_PROP_FRAME_HEIGHT, 720)

                for _ in range(5):
                    cap.read()

                ret, frame = cap.read()
                if not ret:
                    logger.error("摄像头捕获失败，无法获取图像帧")
                    return None

                _, buf = _cv2.imencode('.jpg', frame, [_cv2.IMWRITE_JPEG_QUALITY, 85])
                return buf.tobytes()
            finally:
                cap.release()

        try:
            from PIL import Image
            jpg_bytes = await loop.run_in_executor(None, _sync_capture)
            if jpg_bytes is None:
                return None

            import io as _io
            img = Image.open(_io.BytesIO(jpg_bytes))
            img = img.convert('RGB')

            if self.save_screenshots:
                filename = f"camera_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                filepath = os.path.join(self.screenshot_dir, filename)
                img.save(filepath)
                logger.debug(f"摄像头快照已保存: {filepath}")

            return self._compress_image(img, max_size=640, jpeg_quality=60)

        except Exception as e:
            logger.error(f"摄像头捕获失败: {e}")
            return None

    # ── behavior_state 路由（硅基哨兵便宜预判，避免对着空 PC 截图）──────────

    def _read_behavior_state(self) -> dict:
        """读取硅基哨兵最新快照的 behavior_state + 手机前台，作为便宜的路由预判。

        PC 字段每 tick 实时可靠；手机字段断连时为 None（哨兵不编造）。
        读取失败返回 {}（回退到原截图+vision 路径）。
        """
        try:
            from silicon_perception.sentinel import get_sentinel
            s = get_sentinel()
            snap = getattr(s, "_last_snapshot", None) if s else None
            if snap is None:
                return {}
            return {
                "state": getattr(snap, "behavior_state", "") or "",
                "mobile_app": getattr(snap, "mobile_app_name", None),
                "mobile_screen_on": getattr(snap, "mobile_screen_on", None),
            }
        except Exception:
            return {}

    @staticmethod
    def _route_from_behavior_state(bs: dict) -> str:
        """把 behavior_state 中文串映射为路由：pc / phone / away / unknown。"""
        state = bs.get("state") or ""
        if not state or state == "unknown":
            return "unknown"
        # 双设备（同时在用手机和电脑）→ 人在桌前，走 PC
        if "电脑" in state and "手机" in state:
            return "pc"
        if "没在设备前" in state:
            return "away"
        if "用手机" in state or "手机亮屏" in state:
            return "phone"
        if "玩电脑" in state or "电脑在放东西" in state:
            return "pc"
        return "unknown"

    async def capture_phone_screen(self) -> Optional[str]:
        """通过手机 relay 截取手机屏幕，返回 base64（未连接/失败返回 None）。

        依赖 mobile_screenshot 工具（Android AccessibilityService/Shizuku 实现）。
        """
        try:
            from mirrow_core.shared_state import get_mobile_relay_callback
            relay = get_mobile_relay_callback()
            if not relay:
                return None
            import uuid
            req = str(uuid.uuid4())
            result = await relay(req, "mobile_screenshot", {})
            if not isinstance(result, dict):
                return None
            # 结果可能平铺，也可能嵌在 result["result"] 内
            candidates = [result, result.get("result") if isinstance(result.get("result"), dict) else None]
            for obj in candidates:
                if not isinstance(obj, dict):
                    continue
                for k in ("image_base64", "image", "base64", "screenshot"):
                    v = obj.get(k)
                    if v and isinstance(v, str):
                        return v
            return None
        except Exception as e:
            logger.warning(f"手机截图失败: {e}")
            return None

    async def analyze_images(
        self,
        screen_base64: str,
        camera_base64: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        declared_status_text: Optional[str] = None,
        phone_base64: Optional[str] = None,
        behavior_state_hint: Optional[str] = None,
    ) -> TrackingResult:
        """
        使用统一视觉入口分析屏幕截图和摄像头快照（DS V4-Pro 主 / GLM-4V 降级）。

        Args:
            screen_base64: 屏幕截图的base64编码
            camera_base64: 摄像头快照的base64编码（可选）
            custom_prompt: 自定义分析提示词
            declared_status_text: 用户声明状态文本（用于一致性判断）
            phone_base64: 手机截图 base64（人不在 PC 前时提供，让模型分析手机屏内容）
            behavior_state_hint: 硅基传感器检测到的行为状态（辅助判断）

        Returns:
            追踪结果
        """
        # 构建声明状态提示 + 传感器/手机屏辅助提示
        declared_hint = ""
        if behavior_state_hint:
            declared_hint += behavior_state_hint + "\n"
        if phone_base64:
            declared_hint += ("附带的第二张图是用户手机的屏幕截图——她此刻不在电脑前，"
                              "请以手机屏内容为主判断她在做什么，电脑截图仅作参考。\n")
        if declared_status_text:
            declared_hint += f"用户当前声明的状态是：{declared_status_text}。请对比实际观察结果，判断是否一致。"

        if custom_prompt:
            prompt = custom_prompt
        elif camera_base64 or phone_base64:
            prompt = self.ANALYSIS_PROMPT.format(
                camera_hint="和另一张辅助图片",
                declared_status_hint=declared_hint
            )
        else:
            prompt = self.ANALYSIS_PROMPT_NO_CAMERA.format(
                declared_status_hint=declared_hint
            )

        # 构建消息内容（支持多图）
        content = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": "来源标签：PC屏幕。下一项就是该来源图片。"},
        ]
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screen_base64}"}
        })
        if phone_base64:
            content.append({
                "type": "text", "text": "来源标签：手机屏幕。下一项就是该来源图片。"
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{phone_base64}"}
            })
        if camera_base64:
            content.append({
                "type": "text", "text": "来源标签：PC摄像头。下一项就是该来源图片。"
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{camera_base64}"}
            })

        messages = [{"role": "user", "content": content}]

        # 使用统一视觉入口（主 flash-vision + 降级 GLM-4V）
        from mirrow_core.llm_runtime import call_llm_api_visual, vision_fallback_for
        from mirrow_core.shared_state import get_current_image_model

        primary = get_current_image_model()
        fallback = vision_fallback_for(primary)

        content_text = None
        try:
            result = await call_llm_api_visual(
                messages, primary, response_format={"type": "json_object"},
            )
            content_text = result.get("content")
        except Exception as e:
            logger.warning(f"视觉主模型 {primary} 失败: {e}，降级到 {fallback}")

        if content_text is None:
            try:
                result = await call_llm_api_visual(
                    messages, fallback, response_format={"type": "json_object"},
                )
                content_text = result.get("content")
            except Exception as e:
                logger.error(f"视觉降级模型 {fallback} 也失败: {e}")

        if content_text is None:
            return self._create_error_result("视觉模型均调用失败")

        return self._parse_analysis_result(content_text)

    def _parse_analysis_result(self, content: str) -> TrackingResult:
        """解析GLM返回的分析结果"""
        import json

        try:
            # 尝试提取JSON
            json_str = content

            # 处理markdown代码块
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                parts = json_str.split("```")
                if len(parts) >= 2:
                    json_str = parts[1]

            # 提取JSON对象
            start_idx = json_str.find("{")
            end_idx = json_str.rfind("}")
            if start_idx != -1 and end_idx != -1:
                json_str = json_str[start_idx:end_idx + 1]

            data = json.loads(json_str.strip())

            # 解析活动类型
            activity_str = data.get("activity_type", "unknown").lower()
            try:
                activity_type = UserActivityType(activity_str)
            except ValueError:
                activity_type = UserActivityType.UNKNOWN

            details = data.get("details", {})
            # 提取状态一致性字段，存入 details 供下游使用
            details["status_consistent"] = data.get("status_consistent", True)
            details["status_mismatch_detail"] = data.get("status_mismatch_detail", "")

            return TrackingResult(
                activity_type=activity_type,
                activity_description=data.get("activity_description", "未知活动"),
                confidence=float(data.get("confidence", 0.5)),
                details=details,
                timestamp=datetime.now()
            )

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}, 内容: {content[:200]}")
            return self._create_error_result(f"JSON解析失败")
        except Exception as e:
            logger.error(f"结果解析失败: {e}")
            return self._create_error_result(f"解析失败")

    def _create_error_result(self, error_msg: str) -> TrackingResult:
        """创建错误结果"""
        return TrackingResult(
            activity_type=UserActivityType.UNKNOWN,
            activity_description=error_msg,
            confidence=0.0,
            details={"error": error_msg},
            timestamp=datetime.now()
        )

    async def track(self, custom_prompt: Optional[str] = None, declared_status_text: Optional[str] = None, force_camera: bool = False) -> TrackingResult:
        """
        执行一次追踪：截屏 → 判断屏幕活跃度 → 按需摄像头 → GLM-4V分析。
        屏幕活跃时跳过摄像头；闲置时才调用摄像头确认用户是否离席。

        Args:
            custom_prompt: 自定义分析提示词
            declared_status_text: 用户声明状态文本（用于一致性判断）
            force_camera: 强制调用摄像头（用于 out 等屏幕活跃不代表用户在电脑前的状态）

        Returns:
            追踪结果
        """
        if self._track_lock.locked():
            logger.warning("用户追踪正在执行，跳过本次追踪")
            return self._create_error_result("用户追踪正在执行，跳过本次追踪")

        async with self._track_lock:
            # Step 0: 读 behavior_state 做便宜的路由预判（人在PC/手机/离席）
            bs = self._read_behavior_state()
            route = self._route_from_behavior_state(bs)
            bs_state = bs.get("state") or ""
            print(f"[USER_TRACKING] behavior_state={bs_state!r} → route={route}", flush=True)

            # ── behavior_state 路由：不在设备前 → 跳过截图+GLM，直接判一致 ──
            if route == "away":
                print(f"[USER_TRACKING] behavior_state判定离席 → 跳过截图和视觉分析", flush=True)
                return TrackingResult(
                    activity_type=UserActivityType.IDLE,
                    activity_description="behavior_state判定离席，跳过视觉分析",
                    confidence=1.0,
                    details={"status_consistent": True, "status_mismatch_detail": "", "route": "away", "analysis_available": False},
                )

            # Step 1: 截屏（PC）—— 仅 route=pc/unknown 时截，route=phone 时跳过 PC 截图
            screen_base64 = None
            source = {"route": route, "screen_attempted": route != "phone", "screen_available": False, "phone_screen_attempted": route == "phone", "phone_screen_available": False, "camera_attempted": False, "camera_available": False}
            if route != "phone":
                screen_base64 = await self.capture_screen()
                source["screen_available"] = bool(screen_base64)
                if not screen_base64:
                    result = self._create_error_result("截屏失败"); result.details.update(source); result.details["error"] = "截屏失败"; return result

            # Step 1b: route=phone → 只抓手机屏，不做 PC 截屏
            phone_base64 = None
            if route == "phone":
                try:
                    from mirrow_core.shared_state import get_mobile_connected
                    if get_mobile_connected():
                        phone_base64 = await self.capture_phone_screen()
                        source["phone_screen_available"] = bool(phone_base64)
                        if phone_base64:
                            print(f"[USER_TRACKING] route=phone → 已抓手机屏, base64长度={len(phone_base64)}", flush=True)
                        else:
                            print(f"[USER_TRACKING] route=phone 但手机截图失败/未连接 → 回退空结果", flush=True)
                            result = self._create_error_result("手机端截图失败，且PC端无人"); result.details.update(source); result.details["error"] = "手机端截图失败"; return result
                except Exception as e:
                    print(f"[USER_TRACKING] 手机截图路径异常: {e}", flush=True)
                    source["phone_screen_error"] = str(e); result = self._create_error_result(f"手机端截图异常: {e}"); result.details.update(source); result.details["error"] = str(e); return result

            # Step 2: 本地判断屏幕活跃度 + 是否在 MIRROW 页面
            camera_base64 = None
            mirrow_foreground = self._is_mirrow_foreground()
            # 诊断：输出两个信号的状态
            win_title = UserTrackingService._get_foreground_window_title()
            hb_active = UserTrackingService._external_mirrow_check() if UserTrackingService._external_mirrow_check else False
            print(f"[USER_TRACKING] 信号1(窗口标题)={repr(win_title)}, 信号2(心跳回调)={hb_active}, mirrow_foreground={mirrow_foreground}", flush=True)
            try:
                import base64 as _b64
                import io as _io
                from PIL import Image as _Image
                img_data = _b64.b64decode(screen_base64)
                img = _Image.open(_io.BytesIO(img_data))
                screen_active = self._is_screen_active(img)
                print(f"[USER_TRACKING] screen_active={screen_active}", flush=True)
                if phone_base64 and not force_camera:
                    # 已拿到手机屏（人在手机上，本就在场）→ 无需摄像头
                    logger.info("已获取手机屏，跳过摄像头调用")
                    print("[USER_TRACKING] 有手机屏 → 跳过摄像头", flush=True)
                elif screen_active and not force_camera:
                    logger.info("屏幕活跃，跳过摄像头调用")
                    print("[USER_TRACKING] 屏幕活跃 → 跳过摄像头", flush=True)
                elif screen_active and force_camera:
                    logger.info("屏幕活跃但 force_camera=True（如 out 状态），仍调用摄像头")
                    print("[USER_TRACKING] 屏幕活跃但 force_camera=True → 调用摄像头...", flush=True)
                    source["camera_attempted"] = True; camera_base64 = await self.capture_camera(); source["camera_available"] = bool(camera_base64)
                    if camera_base64:
                        print(f"[USER_TRACKING] force_camera 摄像头调用成功, base64长度={len(camera_base64)}", flush=True)
                else:
                    logger.info("屏幕闲置，调用摄像头确认用户是否在场")
                    print("[USER_TRACKING] 屏幕闲置 → 调用摄像头...", flush=True)
                    source["camera_attempted"] = True; camera_base64 = await self.capture_camera(); source["camera_available"] = bool(camera_base64)
                    if not camera_base64:
                        logger.warning("摄像头快照捕获失败，仅使用截屏分析")
                        print("[USER_TRACKING] 摄像头调用失败，仅用截屏", flush=True)
                    else:
                        print(f"[USER_TRACKING] 摄像头调用成功, base64长度={len(camera_base64)}", flush=True)
            except Exception as e:
                logger.warning(f"屏幕活跃度判断失败: {e}，保守跳过摄像头")
                print(f"[USER_TRACKING] 活跃度判断异常: {e}", flush=True)
                screen_active = True  # 判断失败时认为活跃，不额外调摄像头

            # Step 3: 视觉模型分析（DS V4-Pro 主模型，GLM-4V 自动降级）
            bs_hint = f"系统传感器检测到：{bs_state}。" if bs_state else ""
            result = await self.analyze_images(
                screen_base64=screen_base64,
                camera_base64=camera_base64,
                custom_prompt=custom_prompt,
                declared_status_text=declared_status_text,
                phone_base64=phone_base64,
                behavior_state_hint=bs_hint,
            )

            # 注入 MIRROW 前台标记，供下游区分"在 MIRROW 聊天"和"在其他平台聊天"
            result.details["mirrow_foreground"] = mirrow_foreground
            result.details.update(source)
            result.details["screen_active"] = screen_active if route != "phone" else None
            result.details["analysis_available"] = not bool(result.details.get("error"))

            # 硬覆盖：Windows API 已确认 MIRROW 在前台时，覆盖 LLM 的 activity_type。
            # GLM-4V 可能将 MIRROW 的聊天界面误判为"chatting"，
            # 但停留在 MIRROW 界面上本质是挂机（idle），不是在其他平台聊天。
            if mirrow_foreground and result.activity_type == UserActivityType.CHATTING:
                result.activity_type = UserActivityType.IDLE
                result.activity_description = "MIRROW界面上（挂机中）"

            logger.info(f"用户追踪完成: {result.activity_type.value} - {result.activity_description} (screen_active={screen_active}, mirrow_foreground={mirrow_foreground})")
            print(f"[USER_TRACKING] 完成: activity={result.activity_type.value}, confidence={result.confidence:.2f}, screen_active={screen_active}, mirrow_foreground={mirrow_foreground}", flush=True)
            return result


async def get_mobile_activity() -> Optional[dict]:
    """通过 WebSocket 中继获取手机端活动信息。

    返回: {foreground_app, screen_on, active_duration_s, processes} 或 None（手机未连接/超时）
    """
    try:
        from mirrow_core.shared_state import get_mobile_relay_callback
        if not get_mobile_relay_callback():
            return None
        import uuid
        request_id = str(uuid.uuid4())
        result = await get_mobile_relay_callback()(request_id, "mobile_processes", {})
        return result if result else None
    except Exception:
        return None


# 全局单例
_tracking_service_instance: Optional[UserTrackingService] = None


def get_user_tracking_service(http_client: Optional[httpx.AsyncClient] = None) -> UserTrackingService:
    """获取全局用户追踪服务实例"""
    global _tracking_service_instance
    if _tracking_service_instance is None:
        _tracking_service_instance = UserTrackingService(http_client=http_client)
    elif http_client is not None and _tracking_service_instance._http_client is None:
        _tracking_service_instance._http_client = http_client
    return _tracking_service_instance


def init_user_tracking_service(
    save_screenshots: bool = False,
    screenshot_dir: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> UserTrackingService:
    """初始化全局用户追踪服务实例（使用统一视觉入口）"""
    global _tracking_service_instance
    _tracking_service_instance = UserTrackingService(
        save_screenshots=save_screenshots,
        screenshot_dir=screenshot_dir,
        http_client=http_client,
    )
    return _tracking_service_instance
