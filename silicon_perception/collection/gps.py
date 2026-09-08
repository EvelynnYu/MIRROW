"""GPS 定位数据源 — 手机 relay + 高德逆地理编码

每 10 分钟通过 mobile_relay 获取手机 GPS 坐标，
调用高德 API 逆地理编码获取地址。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from .base import DataSource, DataPoint

logger = logging.getLogger(__name__)

# 默认锚点半径（米）
DEFAULT_HOME_RADIUS = 200
DEFAULT_WORK_RADIUS = 200


class GpsSource(DataSource):
    """GPS 定位 — 手机 relay + 高德 API"""

    name = "gps_location"
    def __init__(self):
        self._last_lat: Optional[float] = None
        self._last_lng: Optional[float] = None
        self._last_address: Optional[str] = None
        self._last_success_time: Optional[str] = None
        self._consecutive_failures: int = 0
        self._api_key: Optional[str] = None
        self._last_error: Optional[str] = None
        # 从 health_snapshots 恢复最近一次定位（重启后不显示"过期XX分钟"）
        try:
            from silicon_perception.recording.health_store import get_store
            conn = get_store()._get_conn()
            row = conn.execute(
                "SELECT location_lat, location_lng, location_address, location_category, timestamp "
                "FROM health_snapshots WHERE location_lat IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_lat = row["location_lat"]
                self._last_lng = row["location_lng"]
                self._last_address = row["location_address"]
                self._last_success_time = row["timestamp"]
                logger.info(f"GpsSource: 从 DB 恢复定位 {self._last_address or f'{self._last_lat:.4f},{self._last_lng:.4f}'}")
        except Exception:
            pass

    @property
    def tick_interval(self) -> int:
        """失败 ≥5 次后每次 tick 都重试，直到成功恢复。"""
        return 1 if self._consecutive_failures >= 5 else 10

    async def read(self, force: bool = False) -> DataPoint:
        coords = await self._read_from_phone(force=force)
        if coords is None:
            self._consecutive_failures += 1
            return DataPoint(source=self.name, data={
                "location_available": False,
                "location_lat": None,
                "location_lng": None,
            })

        self._consecutive_failures = 0
        self._last_error = None
        lat, lng = coords
        prev_lat, prev_lng = self._last_lat, self._last_lng
        self._last_lat, self._last_lng = lat, lng
        self._last_success_time = __import__('datetime').datetime.now().isoformat()

        # 三级研判：显著移动(>500m)或分类可能变化时才调 API
        need_api = True
        if self._last_address and prev_lat and prev_lng:
            dist = self._haversine(lat, lng, prev_lat, prev_lng)
            if dist < 500:
                need_api = False  # 在家不动，省 API 配额
                logger.info(f"GpsSource: 位置末显著移动 ({dist:.0f}m)，复用上次地址")

        if need_api:
            address = await self._reverse_geocode(lat, lng)
            self._last_address = address
        else:
            address = self._last_address

        # 分类（家/公司/通勤/其他）
        category = self._classify(lat, lng)

        return DataPoint(source=self.name, data={
            "location_available": True,
            "location_lat": lat,
            "location_lng": lng,
            "location_address": address,
            "location_category": category,
        }, captured_at=self._last_success_time)

    async def _read_from_phone(self, force: bool = False) -> Optional[tuple]:
        """通过 mobile_relay 获取 GPS 坐标。force=True 手动刷新（弹权限+请求新鲜定位）。"""
        try:
            from mirrow_core.shared_state import get_mobile_relay_callback
            relay = get_mobile_relay_callback()
            if not relay:
                self._last_error = "手机未连接"
                return None
            import uuid
            result = await asyncio.wait_for(
                relay(str(uuid.uuid4()), "mobile_location", {"force": force}),
                timeout=15.0,
            )
            if isinstance(result, dict) and result.get("success"):
                data = result.get("data", {})
                # 兼容旧版：data 可能是 JSON 字符串
                if isinstance(data, str):
                    import json as _json
                    try:
                        data = _json.loads(data)
                    except Exception:
                        data = {}
                lat = data.get("lat")
                lng = data.get("lng")
                if lat is not None and lng is not None:
                    return (float(lat), float(lng))
            # 失败：捕获手机端原因
            if isinstance(result, dict):
                self._last_error = result.get("content") or result.get("error") or "定位失败"
            return None
        except asyncio.TimeoutError:
            self._last_error = "定位超时(15s)"
            logger.info("GpsSource: 手机 relay 超时")
            return None
        except Exception as e:
            self._last_error = f"读取异常: {str(e)[:60]}"
            logger.info(f"GpsSource: 读取失败: {e}")
            return None

    async def _reverse_geocode(self, lat: float, lng: float) -> Optional[str]:
        """调用高德逆地理编码 API。"""
        api_key = self._get_api_key()
        if not api_key:
            return None
        try:
            import httpx
            url = "https://restapi.amap.com/v3/geocode/regeo"
            params = {
                "location": f"{lng},{lat}",
                "key": api_key,
                "radius": "200",
                "extensions": "base",
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "1":
                        regeo = data.get("regeocode", {})
                        return regeo.get("formatted_address")
            return None
        except Exception as e:
            logger.info(f"GpsSource: 逆地理编码失败: {e}")
            return None

    def _classify(self, lat: float, lng: float) -> str:
        """基于预设锚点分类当前位置。"""
        cfg = self._get_gps_config()
        home_lat = cfg.get("home_lat")
        home_lng = cfg.get("home_lng")
        work_lat = cfg.get("work_lat")
        work_lng = cfg.get("work_lng")

        # 优先判断工作地（避免家/公司坐标相同时误判）
        if work_lat and work_lng:
            dist = self._haversine(lat, lng, work_lat, work_lng)
            if dist <= cfg.get("work_radius_m", DEFAULT_WORK_RADIUS):
                return "work"

        if home_lat and home_lng:
            dist = self._haversine(lat, lng, home_lat, home_lng)
            if dist <= cfg.get("home_radius_m", DEFAULT_HOME_RADIUS):
                return "home"

        return "elsewhere"

    @staticmethod
    def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """计算两点间距离（米），Haversine 公式。"""
        import math
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _get_api_key(self) -> Optional[str]:
        # 优先从环境变量读取（开源兼容），回退 settings.json
        import os as _os
        key = _os.getenv("AMAP_API_KEY")
        if key:
            return key
        try:
            from mirrow_core.settings_manager import get_setting
            ss = get_setting("silicon_perception") or {}
            return ss.get("gps", {}).get("amap_api_key")
        except Exception:
            return None

    def _get_gps_config(self) -> dict:
        """读取 GPS 锚点配置：SQLite 主存储 → settings.json 缓存回退。"""
        try:
            from silicon_perception.recording.health_store import get_store
            schedule = get_store().get_user_schedule()
            if schedule and schedule.get("home_lat") is not None:
                return {
                    "home_lat": schedule.get("home_lat"),
                    "home_lng": schedule.get("home_lng"),
                    "work_lat": schedule.get("work_lat"),
                    "work_lng": schedule.get("work_lng"),
                    "home_radius_m": schedule.get("home_radius_m", 200),
                    "work_radius_m": schedule.get("work_radius_m", 200),
                }
        except Exception:
            pass
        # 回退 settings.json（首次使用、SQLite 未初始化时）
        try:
            from mirrow_core.settings_manager import get_setting
            ss = get_setting("silicon_perception") or {}
            return ss.get("gps", {})
        except Exception:
            return {}

    @property
    def last_change_seconds_ago(self) -> float:
        """距上次 GPS 定位成功的秒数（供 context_annotator 使用）。"""
        if self._last_success_time is None:
            return 999999.0
        try:
            from datetime import datetime
            return (datetime.now() - datetime.fromisoformat(self._last_success_time)).total_seconds()
        except Exception:
            return 999999.0

    @property
    def connection_state(self) -> str:
        if self._consecutive_failures >= 3:
            return "failed"
        if self._last_lat is not None:
            return "connected"
        return "disconnected"

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败原因（供前端显示）。"""
        return self._last_error

    @property
    def last_location(self) -> Optional[dict]:
        if self._last_lat is None:
            return None
        return {
            "lat": self._last_lat,
            "lng": self._last_lng,
            "address": self._last_address,
        }
