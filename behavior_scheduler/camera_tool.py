# 摄像头工具

import base64
import os
import io
import logging
from .base_tool import BaseTool, ToolResult, ToolStatus
from typing import Optional

logger = logging.getLogger(__name__)

# 摄像头依赖
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

# PIL用于图像处理
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class CameraTool(BaseTool):
    """摄像头工具，捕获实时快照并使用视觉模型分析"""

    name = "capture_camera"
    description = "使用摄像头捕获用户本人/环境的实时画面并分析。当用户让你看真人、看周围环境、打开摄像头时使用。"
    single_use = True  # 每轮对话只允许一次调用，防止 Pro/Flash 循环重复执行

    def get_user_facing_description(self, **kwargs) -> str:
        return "正在通过摄像头观察"

    parameters_schema = {
        "type": "object",
        "properties": {
            "analysis_prompt": {
                "type": "string",
                "description": "对摄像头快照的分析要求，如'用户在做什么'、'周围环境是什么样的'、'用户表情如何'",
                "default": "请描述摄像头画面中的内容，包括人物、环境、物品等"
            },
            "device_index": {
                "type": "integer",
                "description": "摄像头设备索引，默认为0（第一个摄像头）",
                "default": 0
            }
        },
        "required": []
    }

    def __init__(self, device_name: str = "TIGA Device"):
        self.image_model = "glm"
        self.device_name = device_name
        self._device_index = None  # 延迟查找

    def set_image_model(self, model: str):
        """切换图像分析模型（对齐 config_router：deepseek/glm）"""
        if model in ("deepseek", "glm"):
            self.image_model = model

    def _find_device_index(self) -> int:
        """查找可用摄像头设备索引（快速，只试默认后端 index 0）"""
        if not CV2_AVAILABLE:
            return 0
        try:
            cap = cv2.VideoCapture(0)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                if ret:
                    self._device_index = 0
                    logger.info("[CameraTool] 找到摄像头: 索引0 默认后端")
                    return 0
        except Exception:
            pass
        return 0

    def _open_capture(self, device_index: int):
        """打开摄像头设备（默认后端，不做 DShow 扫描避免兼容性警告海啸）"""
        cap = cv2.VideoCapture(device_index)
        if not cap.isOpened():
            # 仅尝试相邻索引
            for i in [1]:
                cap = cv2.VideoCapture(i)
                if cap.isOpened():
                    return cap
        return cap

    async def execute(
        self,
        analysis_prompt: str = "请描述摄像头画面中的内容，包括人物、环境、物品等",
        device_index: Optional[int] = None
    ) -> ToolResult:
        """捕获摄像头快照并分析"""

        if not CV2_AVAILABLE:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error="摄像头功能需要安装opencv-python库：pip install opencv-python"
            )

        if not PIL_AVAILABLE:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error="图像处理需要安装Pillow库：pip install Pillow"
            )

        try:
            # 确定设备索引
            if device_index is None:
                env_index = os.getenv("CAMERA_DEVICE_INDEX")
                if env_index is not None and env_index.strip().isdigit():
                    device_index = int(env_index.strip())
                else:
                    device_index = self._device_index if self._device_index is not None else self._find_device_index()

            logger.info(f"[CameraTool] 尝试打开摄像头，设备索引: {device_index}")

            # 打开摄像头
            cap = self._open_capture(device_index)

            if not cap.isOpened():
                available = self.list_devices()
                hint = "未枚举到可用摄像头"
                if available:
                    hint = f"可用设备索引: {', '.join(str(d['index']) for d in available)}"
                error_msg = (
                    f"无法打开摄像头设备（索引 {device_index}）。{hint}。"
                    f"当前配置的摄像头名称是 {self.device_name!r}。"
                    "请检查摄像头是否已连接、未被远程桌面/会议软件占用，并确认系统隐私权限允许桌面应用访问摄像头。"
                )
                logger.error(f"[CameraTool] 错误: {error_msg}")
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content="",
                    error=error_msg
                )

            logger.info(f"[CameraTool] 摄像头已打开，设置分辨率...")

            # 设置分辨率（可选）
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

            # 等待摄像头稳定（跳过前几帧，增加预热时间）
            for i in range(15):
                ret_warm, _ = cap.read()
                if ret_warm and i >= 5:
                    # 第5帧之后已稳定，提前退出预热
                    break

            logger.info(f"[CameraTool] 捕获帧...")

            # 捕获帧（带重试）
            ret, frame = None, None
            for attempt in range(3):
                ret, frame = cap.read()
                if ret and frame is not None:
                    break
                logger.info(f"[CameraTool] 帧读取失败，重试 {attempt + 1}/3...")
                import asyncio as _asyncio
                await _asyncio.sleep(0.2)

            cap.release()

            if not ret or frame is None:
                # 列出可用设备帮助诊断
                available = self.list_devices()
                avail_info = ""
                if available:
                    avail_info = "；可用设备: " + ", ".join(
                        f"{d['index']}({d['resolution']})" for d in available
                    )
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content="",
                    error=f"摄像头捕获失败，无法获取图像帧（设备索引 {device_index}）{avail_info}。请检查摄像头是否被其他应用占用。"
                )

            logger.info(f"[CameraTool] 帧捕获成功，尺寸: {frame.shape}")

            # 转换颜色空间（BGR -> RGB）
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # 转换为PIL Image，缩放到合理尺寸（减少API负载和超时风险）
            img = Image.fromarray(frame_rgb)
            max_dim = 1024
            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
                logger.info(f"[CameraTool] 图片已缩放: {w}x{h} -> {img.size[0]}x{img.size[1]}")

            # PNG保存到本地（无损存档）
            png_buffer = io.BytesIO()
            img.save(png_buffer, format='PNG')
            from .capture_storage import save_and_get_path
            file_path = save_and_get_path("camera", png_buffer.getvalue())

            # JPEG编码用于API调用（更小的payload，减少传输和模型处理时间）
            jpg_buffer = io.BytesIO()
            img.save(jpg_buffer, format='JPEG', quality=85)
            base64_data = base64.b64encode(jpg_buffer.getvalue()).decode('utf-8')

            logger.info(f"[CameraTool] 调用视觉模型分析... (JPEG大小: {len(jpg_buffer.getvalue())} bytes)")

            # 调用统一视觉入口分析（自动压缩 + glm↔deepseek 降级）
            try:
                from llm_client import call_vision_api
            except ImportError:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=f"拍摄完成，但视觉分析不可用（llm_client 模块未安装）。\n\n[快照已保存: {file_path}]"
                )
            vision = await call_vision_api(base64_data, analysis_prompt, model=self.image_model)
            result = vision.get("content", "") if isinstance(vision, dict) else str(vision)

            logger.info(f"[CameraTool] 分析完成，结果长度: {len(result)}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=f"{result}\n\n[快照已保存: {file_path}]"
            )

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            tb_last_line = tb.strip().split('\n')[-1] if tb.strip() else '无详情'
            error_detail = f"摄像头操作失败：{str(e) or repr(e)}\n{tb}"
            logger.error(f"[CameraTool] 异常: {error_detail}")
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"摄像头操作失败：{str(e) or repr(e)} [{type(e).__name__}] — {tb_last_line}"
            )

    def list_devices(self) -> list:
        """列出可用的摄像头设备（用于调试）"""
        if not CV2_AVAILABLE:
            return []

        devices = []
        for i in range(10):
            cap = self._open_capture(i)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    devices.append({
                        "index": i,
                        "resolution": f"{width}x{height}"
                    })
                cap.release()

        return devices
