# 截屏工具

import base64
import os
import logging
from .base_tool import BaseTool, ToolResult, ToolStatus

logger = logging.getLogger(__name__)

# Windows截屏依赖
try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False


class ScreenshotTool(BaseTool):
    """电脑截屏工具，使用视觉模型分析"""

    name = "take_screenshot"
    single_use = True  # 每轮对话只截一次屏
    description = "截取当前电脑屏幕画面并分析。当用户想让你看屏幕上的内容、桌面、网页、窗口时使用。"

    def get_user_facing_description(self, **kwargs) -> str:
        return "正在截取屏幕画面"

    parameters_schema = {
        "type": "object",
        "properties": {
            "analysis_prompt": {
                "type": "string",
                "description": "对截图的分析要求，如'用户在做什么'、'屏幕上有什么重要信息'"
            }
        },
        "required": []
    }

    def __init__(self):
        self.image_model = "glm"  # 默认使用GLM，可通过set_image_model切换

    def set_image_model(self, model: str):
        """切换图像分析模型（对齐 config_router：deepseek/glm）"""
        if model in ("deepseek", "glm"):
            self.image_model = model

    async def execute(self, analysis_prompt: str = "请描述屏幕上的内容，用户正在做什么") -> ToolResult:
        """截屏并分析"""
        if not MSS_AVAILABLE:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error="截屏功能需要安装mss库：pip install mss"
            )

        try:
            # 截屏
            with mss.mss() as sct:
                # 获取主显示器
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)

                # 转换为base64
                img_bytes = screenshot.rgb
                # 需要转换为正确的PNG格式
                from PIL import Image
                import io

                img = Image.frombytes('RGB', screenshot.size, screenshot.rgb)
                buffer = io.BytesIO()
                img.save(buffer, format='PNG')
                base64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')

            # 保存到本地
            from .capture_storage import save_and_get_path
            file_path = save_and_get_path("screenshot", buffer.getvalue())

            # 调用统一视觉入口分析（自动压缩 + glm↔deepseek 降级）
            try:
                from llm_client import call_vision_api
            except ImportError:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=f"截图已保存，但视觉分析不可用（llm_client 模块未安装）。\n\n[截图已保存: {file_path}]"
                )
            vision = await call_vision_api(base64_data, analysis_prompt, model=self.image_model)
            result = vision.get("content", "") if isinstance(vision, dict) else str(vision)

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=f"{result}\n\n[截图已保存: {file_path}]"
            )

        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"截屏失败：{str(e)}"
            )
