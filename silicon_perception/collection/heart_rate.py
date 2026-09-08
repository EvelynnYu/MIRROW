"""心率数据源 — BLE 手环"""

import asyncio
import logging
from typing import Optional
from datetime import datetime

from .base import DataSource, DataPoint, _parse_hr

logger = logging.getLogger(__name__)


def resolve_hr_device_mac() -> Optional[str]:
    """读取心率设备 MAC：环境变量 HR_DEVICE_MAC → SQLite user_schedule → settings.json → None。
    模块级函数，供 PC BLE (HeartRateSource) 和手机 relay 调用点共用。开源兼容（仿 gps.py._get_api_key）。"""
    import os as _os
    mac = _os.getenv("HR_DEVICE_MAC")
    if mac and mac.strip():
        return mac.strip()
    try:
        from silicon_perception.recording.health_store import get_store
        s = get_store().get_user_schedule()
        if s and s.get("hr_device_mac"):
            return s["hr_device_mac"]
    except Exception:
        pass
    try:
        from mirrow_core.settings_manager import get_setting
        ss = get_setting("silicon_perception") or {}
        return (ss.get("hr_device") or {}).get("mac")
    except Exception:
        return None


class HeartRateSource(DataSource):
    """BLE 心率数据源，复用 band_tool.py 的 BLE 连接和解析逻辑"""

    name = "heart_rate"

    HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    DEFAULT_HR_MAC = None  # 开源默认不填；用户在哨兵面板扫描选设备后存入 SQLite user_schedule

    def __init__(self):
        self._client = None
        self._ble_available = False
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._last_success_time: Optional[str] = None
        # 从 health_snapshots 恢复最近一次心率（重启后不用等下一个采集周期）
        try:
            from silicon_perception.recording.health_store import get_store
            conn = get_store()._get_conn()
            row = conn.execute(
                "SELECT heart_rate, timestamp FROM health_snapshots "
                "WHERE heart_rate IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_success_time = row["timestamp"]
        except Exception:
            pass
        try:
            from bleak import BleakClient, BleakScanner
            from behavior_scheduler.ble_worker import run_ble
            self._BleakClient = BleakClient
            self._BleakScanner = BleakScanner
            self._run_ble = run_ble
            self._ble_available = True
        except ImportError:
            logger.warning("HeartRateSource: bleak 不可用，心率监控禁用")

    def _get_device_mac(self) -> Optional[str]:
        """读取心率设备 MAC（复用模块级 resolve_hr_device_mac，回退类默认）。"""
        return resolve_hr_device_mac() or self.DEFAULT_HR_MAC

    @property
    def connection_state(self) -> str:
        if not self._ble_available:
            return "unavailable"
        if self._consecutive_failures >= 3:
            return "failed"
        if self._client and self._client.is_connected:
            return "connected"
        return "disconnected"

    async def read(self) -> DataPoint:
        if not self._ble_available:
            return DataPoint(source=self.name)

        async with self._lock:
            try:
                await self._ensure_connected()
                hr = await self._read_once()
                if hr is not None:
                    self._consecutive_failures = 0
                    self._last_success_time = datetime.now().isoformat()
                    return DataPoint(source=self.name, data={"heart_rate": hr}, captured_at=self._last_success_time)
                else:
                    self._consecutive_failures += 1
                    return DataPoint(source=self.name, data={"heart_rate": None})
            except Exception as e:
                logger.debug(f"HeartRateSource 读取失败: {e}")
                self._consecutive_failures += 1
                await self._disconnect()
                return DataPoint(source=self.name)

    async def reconnect_and_read(self) -> dict:
        if not self._ble_available:
            return {"success": False, "heart_rate": None, "error": "bleak 不可用，心率监控禁用"}
        mac = self._get_device_mac()
        if not mac:
            return {"success": False, "heart_rate": None, "error": "未配置心率设备，请在哨兵面板-健康-心率扫描选择"}
        async with self._lock:
            old_failures = self._consecutive_failures  # 保存旧值，失败时恢复
            await self._disconnect()
            self._consecutive_failures = 0
            try:
                # 预扫描设备，预热 Windows BLE 缓存 → 抑制配对弹窗
                device = None
                try:
                    device = await self._run_ble(
                        self._BleakScanner.find_device_by_address(mac, timeout=5.0)
                    )
                except Exception:
                    pass
                self._client = self._BleakClient(device or mac, timeout=10.0)
                await self._run_ble(self._client.connect())
                await asyncio.sleep(0.5)
                for attempt in range(3):
                    hr = await self._read_once()
                    if hr is not None:
                        self._last_success_time = datetime.now().isoformat()
                        logger.info(f"HeartRateSource: 重连成功，HR={hr} bpm (attempt {attempt+1})")
                        return {"success": True, "heart_rate": hr, "error": None}
                    logger.debug(f"HeartRateSource: 重连读取 attempt {attempt+1} 无数据")
                    await asyncio.sleep(1.0)
                logger.warning("HeartRateSource: 重连后 3 次读取均无数据")
                self._consecutive_failures = max(old_failures, 3)  # 无数据也算失败，防止状态跳变
                return {"success": False, "heart_rate": None, "error": "已连接但 3 次读取均无心率数据，请确认手环心率广播已开启"}
            except Exception as e:
                self._consecutive_failures = max(old_failures, 3)  # 失败恢复旧值，至少标记 failed
                logger.warning(f"HeartRateSource: 重连失败: {e}")
                return {"success": False, "heart_rate": None, "error": str(e)}

    async def _ensure_connected(self):
        if not self._ble_available:
            raise RuntimeError("bleak 不可用")
        if self._client and self._client.is_connected:
            return
        mac = self._get_device_mac()
        if not mac:
            raise RuntimeError("未配置心率设备（请在哨兵面板-健康-心率扫描选择）")
        # 预扫描设备，预热 Windows BLE 缓存 → 抑制配对弹窗
        device = None
        try:
            device = await self._run_ble(
                self._BleakScanner.find_device_by_address(mac, timeout=5.0)
            )
        except Exception:
            pass
        self._client = self._BleakClient(device or mac, timeout=10.0)
        await self._run_ble(self._client.connect())

    async def _read_once(self) -> Optional[int]:
        readings = []

        def handler(sender, data):
            readings.append(_parse_hr(data))

        await self._run_ble(self._client.start_notify(self.HR_MEASUREMENT_UUID, handler))
        await asyncio.sleep(3.0)
        await self._run_ble(self._client.stop_notify(self.HR_MEASUREMENT_UUID))
        return readings[-1] if readings else None

    async def _disconnect(self):
        if self._client:
            try:
                await self._run_ble(self._client.disconnect())
            except Exception:
                pass
            self._client = None

    def disconnect(self):
        """公开断开 BLE 连接（供哨兵 pause 时释放资源）"""
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                _asyncio.create_task(self._disconnect())
            else:
                loop.run_until_complete(self._disconnect())
        except Exception:
            pass
        self._consecutive_failures = 0  # 主动断开不算失败

    async def stop(self):
        await self._disconnect()
