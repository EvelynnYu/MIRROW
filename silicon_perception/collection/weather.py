"""天气数据源 — wttr.in 免费 API，一天一更新。

城市可配置：环境变量 WEATHER_CITY → settings.json silicon_perception.weather.city → 空。
数据仅作 L1 聚合层原料（上下文注入），不参与 trigger / anomaly_detector / baseline。
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)


def _get_city() -> Optional[str]:
    """天气监控城市：env → settings → None（开源默认不填）。

    仿 gps.py:_get_api_key() 的降级范式。返回空表示未配置，
    调用方应跳过采集而非拼坏 URL。
    """
    city = os.getenv("WEATHER_CITY")
    if city and city.strip():
        return city.strip()
    try:
        from mirrow_core.settings_manager import get_setting
        ss = get_setting("silicon_perception") or {}
        city = (ss.get("weather", {}) or {}).get("city")
        if city and str(city).strip():
            return str(city).strip()
    except Exception:
        pass
    return None


class WeatherSource(DataSource):
    """天气 — wttr.in，一天一次"""

    name = "weather"

    def __init__(self):
        self._temp: Optional[int] = None
        self._humidity: Optional[int] = None
        self._desc: Optional[str] = None
        self._last_fetch_date: Optional[str] = None
        # 从 daily_health_summary 恢复上次天气（重启恢复）
        try:
            from silicon_perception.recording.health_store import get_store
            store = get_store()
            conn = store._get_conn()
            row = conn.execute(
                "SELECT weather_temp, weather_humidity, weather_desc, date "
                "FROM daily_health_summary WHERE weather_temp IS NOT NULL "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row:
                self._temp = row["weather_temp"]
                self._humidity = row["weather_humidity"]
                self._desc = row["weather_desc"]
                self._last_fetch_date = row["date"]
                logger.info(f"WeatherSource: 从 DB 恢复天气 {self._desc} {self._temp}°C {self._humidity}%")
        except Exception:
            pass

    @property
    def tick_interval(self) -> int:
        """一天一次。首次启动无数据时立即获取。"""
        return 1 if self._last_fetch_date is None else 86400

    async def read(self) -> DataPoint:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_fetch_date == today:
            # 今日已获取，返回缓存
            return DataPoint(source=self.name, data={
                "weather_temp": self._temp,
                "weather_humidity": self._humidity,
                "weather_desc": self._desc,
            })

        # 调 wttr.in
        try:
            city = _get_city()
            if not city:
                # 未配置城市：跳过采集，不拼坏 URL
                logger.info("WeatherSource: 未配置监控城市（WEATHER_CITY / settings.weather.city），跳过采集")
                return DataPoint(source=self.name, data={
                    "weather_temp": self._temp,
                    "weather_humidity": self._humidity,
                    "weather_desc": self._desc,
                })
            import httpx
            url = f"https://wttr.in/{city}?format=j1&lang=zh"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            current = data.get("current_condition", [{}])[0]
            self._desc = (
                current.get("lang_zh", [{}])[0].get("value")
                or current.get("weatherDesc", [{}])[0].get("value", "")
            )
            self._temp = int(current.get("temp_C", 0)) if current.get("temp_C") else None
            self._humidity = int(current.get("humidity", 0)) if current.get("humidity") else None
            self._last_fetch_date = today

            # 写入 L1
            try:
                from silicon_perception.recording.health_store import get_store
                get_store().upsert_daily_summary(
                    today,
                    weather_temp=self._temp,
                    weather_humidity=self._humidity,
                    weather_desc=self._desc,
                )
            except Exception:
                pass

            logger.info(f"WeatherSource: {city} {self._desc} {self._temp}°C {self._humidity}%")
            return DataPoint(source=self.name, data={
                "weather_temp": self._temp,
                "weather_humidity": self._humidity,
                "weather_desc": self._desc,
            })

        except Exception as e:
            logger.info(f"WeatherSource: 获取失败 ({e})，{'' if self._temp else '无缓存'}")
            return DataPoint(source=self.name, data={
                "weather_temp": self._temp,
                "weather_humidity": self._humidity,
                "weather_desc": self._desc,
            })

    @property
    def connection_state(self) -> str:
        if self._temp is not None:
            return "connected"
        return "disconnected"

    @property
    def today_weather(self) -> Optional[dict]:
        """供外部读取今日天气。"""
        if self._temp is None:
            return None
        return {
            "temp": self._temp,
            "humidity": self._humidity,
            "desc": self._desc,
        }
