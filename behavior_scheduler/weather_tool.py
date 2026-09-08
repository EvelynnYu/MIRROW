# 天气查询工具

import httpx
from .base_tool import BaseTool, ToolResult, ToolStatus


class WeatherTool(BaseTool):
    """天气查询工具，使用wttr.in免费API"""

    name = "get_weather"
    single_use = True  # 每轮对话只查一次天气，防止 Flash 重复提取导致多轮循环
    description = "查询指定城市的天气情况。当用户询问天气相关问题时使用。"

    def get_user_facing_description(self, **kwargs) -> str:
        city = kwargs.get("city", "")
        if city:
            return f"正在查询{city}的天气"
        return "正在查询天气"

    parameters_schema = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名称，如'北京'、'上海'、'广州'"
            }
        },
        "required": ["city"]
    }

    async def execute(self, city: str) -> ToolResult:
        """查询天气"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # wttr.in 支持中文城市名
                url = f"https://wttr.in/{city}?format=j1&lang=zh"
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()

            # 提取关键天气信息
            current = data.get("current_condition", [{}])[0]
            weather_desc = current.get("lang_zh", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "未知"))
            temp = current.get("temp_C", "未知")
            feels_like = current.get("FeelsLikeC", "未知")
            humidity = current.get("humidity", "未知")
            wind = current.get("windspeedKmph", "未知")

            result = f"{city}当前天气：{weather_desc}，温度{temp}°C（体感{feels_like}°C），湿度{humidity}%，风速{wind}km/h"

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=result
            )

        except httpx.HTTPError as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"天气查询失败：{str(e)}"
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"天气查询出错：{str(e)}"
            )
