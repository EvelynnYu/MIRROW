"""硅基哨兵 (Silicon Sentinel) API 路由 — 从 main.py 提取"""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sentinel", tags=["sentinel"])


def _get_sentinel():
    """获取哨兵单例，处理未初始化的情况。"""
    try:
        from silicon_perception.sentinel import SENTINEL_AVAILABLE, get_sentinel
    except ImportError:
        SENTINEL_AVAILABLE = False
        get_sentinel = None

    if not SENTINEL_AVAILABLE:
        return None, False, None
    sentinel = get_sentinel() if get_sentinel else None
    if sentinel is None:
        return None, True, None
    return sentinel, True, SENTINEL_AVAILABLE


@router.get("/rules")
async def get_sentinel_rules():
    """返回哨兵规则配置（供前端规则清单渲染）。"""
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), "..", "rules.json")
    if not os.path.exists(config_path):
        return {"rules": []}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/status")
async def get_sentinel_status():
    """获取哨兵运行状态 + 最近告警。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        return {"available": available, "running": False, "message": "哨兵未初始化"}
    return {"available": True, **sentinel.status}


@router.get("/hr/history")
async def get_sentinel_hr_history(hours: int = 24):
    """获取心率历史数据（给前端图表）。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")
    try:
        data = sentinel._store.get_heart_rate_history(hours=hours)
        return {"hours": hours, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/toggle")
async def toggle_sentinel():
    """开关哨兵监控。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")
    new_state = sentinel.toggle()
    return {"enabled": new_state, "message": "哨兵已启用" if new_state else "哨兵已暂停"}


@router.post("/gps/toggle")
async def toggle_sentinel_gps():
    """开关哨兵 GPS 定位。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")
    return sentinel.toggle_gps()


@router.get("/wfh")
async def get_wfh():
    """查询今日居家办公标记。"""
    from mirrow_core.settings_manager import get_setting
    sp = get_setting("silicon_perception") or {}
    today = datetime.now().strftime("%Y-%m-%d")
    return {"wfh_today": sp.get("wfh_date") == today, "wfh_date": sp.get("wfh_date")}


@router.post("/wfh")
async def set_wfh(request: dict):
    """设置今日居家办公标记（勾选→静默"工作时间在家"规则）。次日自动失效。
    Body: {"enabled": true/false}"""
    from mirrow_core.settings_manager import get_setting, set_setting
    sp = get_setting("silicon_perception") or {}
    today = datetime.now().strftime("%Y-%m-%d")
    enabled = bool(request.get("enabled", True))
    sp["wfh_date"] = today if enabled else None
    set_setting("silicon_perception", sp)
    return {"success": True, "wfh_today": enabled, "wfh_date": sp.get("wfh_date")}


@router.post("/hr/toggle")
async def toggle_sentinel_hr():
    """[已废弃] 心率自动重试，不再需要手动开关。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")
    return {"hr_enabled": True, "reason": "deprecated: 心率自动重试，无需手动开关"}



@router.post("/hr/reconnect")
async def reconnect_sentinel_hr():
    """手动重连手环读取心率。PC BLE → 手机 BLE。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")

    def _write_hr_to_store(hr: int, source: str):
        """将心率写入哨兵存储（让前端 status/history 能读到）"""
        try:
            from silicon_perception.collection.base import DataPoint
            snap_id = sentinel._store.insert_snapshot(heart_rate=hr)
            sentinel._store._last_hr = hr
            sentinel._store._last_hr_time = datetime.now().isoformat()
            logger.info(f"[HR_RECONNECT] 心率已写入哨兵存储: {hr} bpm (snap={snap_id})")
        except Exception as e:
            logger.warning(f"[HR_RECONNECT] 写入哨兵存储失败: {e}")

    pc_error = "未尝试"

    # 1. PC BLE 直连（快速失败，8s 超时）
    if sentinel._hr_source is not None:
        try:
            pc_result = await asyncio.wait_for(sentinel.reconnect_hr(), timeout=8.0)
            if pc_result.get("success"):
                hr = pc_result.get("heart_rate")
                logger.info(f"[HR_RECONNECT] PC BLE 重连成功: HR={hr}")
                _write_hr_to_store(hr, "pc_ble")
                pc_result["source"] = "pc_ble"
                return pc_result
            pc_error = pc_result.get("error", "未知错误")
        except asyncio.TimeoutError:
            pc_error = "PC BLE 超时(8s)"
        except Exception as e:
            pc_error = str(e)
    else:
        pc_error = "PC 心率数据源未初始化"

    logger.info(f"[HR_RECONNECT] PC BLE 失败: {pc_error}")

    # 2. 手机 BLE 直连（唯一回退，不走华为健康）
    try:
        from mirrow_core.shared_state import get_mobile_relay_callback
        _relay2 = get_mobile_relay_callback()
        if _relay2:
            import uuid as _hr_uuid2
            from silicon_perception.collection.heart_rate import resolve_hr_device_mac
            ble_result = await asyncio.wait_for(
                _relay2(str(_hr_uuid2.uuid4()), "mobile_heart_rate", {"force": True, "mac": resolve_hr_device_mac() or ""}),
                timeout=15.0
            )
            if isinstance(ble_result, dict) and ble_result.get("success"):
                data = ble_result.get("data", {})
                hr = data.get("heart_rate", "?")
                logger.info(f"[HR_RECONNECT] 手机BLE成功: HR={hr}")
                _write_hr_to_store(hr, "mobile_ble")
                return {"success": True, "heart_rate": hr, "source": "mobile_ble", "pc_error": pc_error}
            # 从 relay 外层 OR 内层 result 取真实错误（mirrowRelayService 把 content 包在 result 内）
            ble_error = (
                ble_result.get("content") or
                (ble_result.get("result", {}) or {}).get("content") or
                "手机BLE返回空"
            ) if isinstance(ble_result, dict) else str(ble_result)
            logger.warning(f"[HR_RECONNECT] 手机BLE失败: {ble_error}")
            return {
                "success": False,
                "error": f"心率读取失败。PC: {pc_error} | 手机BLE: {ble_error}。请确认蓝牙已开启且手环在附近。",
                "pc_error": pc_error,
                "mobile_error": ble_error,
            }
    except asyncio.TimeoutError:
        logger.warning("[HR_RECONNECT] 手机BLE超时(15s)")
    except Exception as e:
        logger.warning(f"[HR_RECONNECT] 手机BLE异常: {e}")

    return {
        "success": False,
        "error": f"心率读取失败。PC: {pc_error} | 手机中继不可用。请确认手机已连接。",
        "pc_error": pc_error,
    }


@router.post("/hr/scan")
async def scan_hr_devices():
    """扫描附近 BLE 设备（PC 端 bleak），供用户选择心率设备。
    返回 [{address, name, rssi, has_hr_service}]，有心率服务/有名字优先。
    注：华为手环用随机地址(RPA)，需先在系统蓝牙配对后 discover 才返回稳定 identity address。"""
    try:
        from bleak import BleakScanner
        from behavior_scheduler.ble_worker import run_ble
    except ImportError:
        return {"success": False, "error": "bleak 不可用", "devices": []}
    HR_SVC = "0000180d-0000-1000-8000-00805f9b34fb"
    try:
        found = await asyncio.wait_for(
            run_ble(BleakScanner.discover(timeout=6.0, return_adv=True)),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {"success": False, "error": "扫描超时(15s)", "devices": []}
    except Exception as e:
        return {"success": False, "error": str(e), "devices": []}
    devices = []
    for addr, tup in (found or {}).items():
        try:
            dev, adv = tup
            name = (adv.local_name or getattr(dev, "name", "") or "")
            uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            devices.append({
                "address": addr,
                "name": name,
                "rssi": adv.rssi,
                "has_hr_service": HR_SVC in uuids,
                "source": "pc",
            })
        except Exception:
            continue
    # 有心率服务 > 有名字 > RSSI 强 优先
    devices.sort(key=lambda d: (not d["has_hr_service"], not d["name"], -(d["rssi"] or -999)))
    return {"success": True, "devices": devices}


@router.post("/hr/scan-mobile")
async def scan_hr_devices_mobile():
    """通过手机 relay 列出已配对蓝牙设备（getBondedDevices），供用户选择心率设备。
    华为手环用随机地址(RPA)，已配对列表返回稳定 identity address。"""
    from mirrow_core.shared_state import get_mobile_relay_callback
    relay = get_mobile_relay_callback()
    if not relay:
        return {"success": False, "error": "手机中继未连接。请确认手机 MIRROW 已打开。", "devices": []}
    import uuid as _u
    try:
        res = await asyncio.wait_for(
            relay(str(_u.uuid4()), "mobile_ble_scan", {"force": True}),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {"success": False, "error": "手机扫描超时(15s)", "devices": []}
    except Exception as e:
        return {"success": False, "error": str(e), "devices": []}
    if isinstance(res, dict) and res.get("success"):
        devs = (res.get("data") or {}).get("devices", [])
        for d in devs:
            if isinstance(d, dict):
                d["source"] = "mobile"
        return {"success": True, "devices": devs}
    err = res.get("content", "手机扫描失败") if isinstance(res, dict) else str(res)
    return {"success": False, "error": err, "devices": []}


@router.post("/gps/test")
async def test_gps():
    """手动触发一次 GPS 定位读取 + 落盘（重启不丢）。"""
    try:
        from silicon_perception.sentinel import get_sentinel
        s = get_sentinel()
        if not s or not s._gps_source:
            return {"success": False, "error": "哨兵未初始化"}
        from mirrow_core.shared_state import get_mobile_relay_callback
        if not get_mobile_relay_callback():
            return {"success": False, "error": "手机中继未连接。请确认手机 MIRROW 已打开。"}

        # 走 GpsSource.read（force）——内部完成 geocode + classify + 更新内存
        dp = await asyncio.wait_for(s._gps_source.read(force=True), timeout=15.0)
        data = dp.data if dp else {}
        if not data.get("location_available"):
            return {"success": False, "error": s._gps_source.last_error or "GPS 暂无定位"}

        lat = data.get("location_lat")
        lng = data.get("location_lng")
        addr = data.get("location_address")
        cat = data.get("location_category")
        # 落盘：写 health_snapshots（重启后可恢复）
        try:
            s._store.insert_snapshot(location_lat=lat, location_lng=lng,
                                     location_address=addr, location_category=cat)
        except Exception as e:
            logger.warning(f"[GPS_TEST] 落盘失败: {e}")
        # 同步 last_snapshot + field_ts（让前端时效不显示"过期"）
        try:
            import datetime as _dt
            now_iso = _dt.datetime.now().isoformat()
            if s._last_snapshot is not None:
                s._last_snapshot.location_lat = lat
                s._last_snapshot.location_lng = lng
                s._last_snapshot.location_address = addr
                s._last_snapshot.location_category = cat
                s._last_snapshot.field_ts["gps"] = now_iso
        except Exception:
            pass
        return {"success": True, "content": f"{cat or '?'} {addr or ''}",
                "lat": lat, "lng": lng, "address": addr, "category": cat}
    except asyncio.TimeoutError:
        return {"success": False, "error": "GPS 读取超时(15s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/events")
async def get_sentinel_events(date: str = "", limit: int = 50, offset: int = 0):
    """查询指定日期的哨兵事件。支持分页。"""
    from datetime import date as _date
    if not date:
        date = _date.today().isoformat()
    try:
        from silicon_perception.recording.health_store import get_store
        conn = get_store()._get_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM sentinel_events WHERE date(timestamp) = ?", (date,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM sentinel_events WHERE date(timestamp) = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (date, limit, offset),
        ).fetchall()
        return {"date": date, "count": len(rows), "total": total, "offset": offset, "events": [dict(r) for r in rows]}
    except Exception as e:
        return {"date": date, "count": 0, "total": 0, "events": [], "error": str(e)}


@router.get("/health")
async def sentinel_health_check():
    """哨兵自检：返回 tick 心跳是否正常。"""
    import time as _time
    try:
        from mirrow_core.shared_state import get_sentinel_last_tick
        last_tick = get_sentinel_last_tick()
    except Exception:
        return {"alive": False, "last_tick_ago_sec": None, "error": "无法读取心跳"}
    if last_tick is None:
        return {"alive": False, "last_tick_ago_sec": None, "message": "哨兵尚未执行过 tick"}
    ago = _time.monotonic() - last_tick
    return {"alive": ago < 120, "last_tick_ago_sec": round(ago, 1)}


@router.post("/refresh")
async def refresh_sentinel_snapshot():
    """强制哨兵立即执行一次完整 tick（重新采集所有数据源 + 重建快照）。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")
    try:
        await sentinel._tick(force=True)
        sn = sentinel._last_snapshot
        return {
            "success": True,
            "snapshot": {
                "heart_rate": sn.heart_rate if sn else None,
                "input_idle_seconds": sn.input_idle_seconds if sn else None,
                "screen_active": sn.screen_active if sn else None,
                "foreground_app": sn.foreground_app if sn else None,
                "mirrow_visible": sn.mirrow_visible if sn else None,
                "user_status": sn.user_status if sn else None,
                "is_period": sn.is_period if sn else None,
                "period_day": sn.period_day if sn else None,
                "steps_today": sn.steps_today if sn else None,
                "steps_stagnant_minutes": sn.steps_stagnant_minutes if sn else None,
                "location_address": sn.location_address if sn else None,
                "location_category": sn.location_category if sn else None,
                "behavior_state": getattr(sn, 'behavior_state', None) if sn else None,
                "last_message_seconds": sn.last_message_seconds if sn else None,
                "screen_time_minutes": getattr(sn, 'screen_time_minutes', None) if sn else None,
                "top_app_category": getattr(sn, 'top_app_category', None) if sn else None,
                "mobile_app_package": getattr(sn, 'mobile_app_package', None) if sn else None,
                "mobile_screen_on": getattr(sn, 'mobile_screen_on', None) if sn else None,
                "mobile_app_name": getattr(sn, 'mobile_app_name', None) if sn else None,
                "tick_count": sentinel._tick_count,
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/gps/geocode")
async def geocode_address(request: dict):
    """地址 → 坐标（调高德地理编码 API）。"""
    address = request.get("address", "").strip()
    if not address:
        return {"success": False, "error": "缺少 address 字段"}
    try:
        # 高德 key 统一：优先环境变量 AMAP_API_KEY，回退 settings（与 gps.py 一致）
        import os as _os
        from mirrow_core.settings_manager import get_setting
        key = _os.getenv("AMAP_API_KEY") or (get_setting("silicon_perception") or {}).get("gps", {}).get("amap_api_key", "")
        if not key:
            return {"success": False, "error": "高德 API key 未配置（请在后端 .env 设置 AMAP_API_KEY）"}
        import httpx
        # 限定城市：复用天气监控城市配置（未配置则不限定，全国搜索）
        from silicon_perception.collection.weather import _get_city
        url = "https://restapi.amap.com/v3/geocode/geo"
        params = {"address": address, "key": key}
        _city = _get_city()
        if _city:
            params["city"] = _city
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
        if data.get("status") == "1" and data.get("geocodes"):
            geo = data["geocodes"][0]
            loc = geo.get("location", "0,0")
            lng, lat = loc.split(",")
            return {
                "success": True,
                "lat": float(lat), "lng": float(lng),
                "address": geo.get("formatted_address", address),
                "district": geo.get("district", ""),
            }
        return {"success": False, "error": data.get("info", "未找到该地址")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/weather/locate")
async def weather_locate():
    """定位当前城市（供天气监控"定位"按钮用）。

    走手机 relay 取坐标 → 高德 regeo 逆地理 → 返回 addressComponent.city。
    只要城市名，不落盘、不改 GPS 数据源状态。失败分级返回中文原因。
    """
    try:
        from mirrow_core.shared_state import get_mobile_relay_callback
        relay = get_mobile_relay_callback()
        if not relay:
            return {"success": False, "error": "手机未连接。请确认手机 MIRROW 已打开。"}

        # 高德 key 统一：env 优先
        import os as _os
        from mirrow_core.settings_manager import get_setting
        key = _os.getenv("AMAP_API_KEY") or (get_setting("silicon_perception") or {}).get("gps", {}).get("amap_api_key", "")
        if not key:
            return {"success": False, "error": "高德 API Key 未配置（请在后端 .env 设置 AMAP_API_KEY）"}

        # 取坐标（force=True 请求新鲜定位 + 弹权限）
        import uuid
        result = await asyncio.wait_for(
            relay(str(uuid.uuid4()), "mobile_location", {"force": True}),
            timeout=15.0,
        )
        if not (isinstance(result, dict) and result.get("success")):
            reason = (result or {}).get("content") or (result or {}).get("error") or "定位失败（请检查定位权限）"
            return {"success": False, "error": reason}
        data = result.get("data", {})
        if isinstance(data, str):
            import json as _json
            try:
                data = _json.loads(data)
            except Exception:
                data = {}
        lat, lng = data.get("lat"), data.get("lng")
        if lat is None or lng is None:
            return {"success": False, "error": "定位返回坐标为空"}

        # 逆地理取城市名
        import httpx
        url = "https://restapi.amap.com/v3/geocode/regeo"
        params = {"location": f"{lng},{lat}", "key": key, "extensions": "base"}
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            geo = resp.json()
        if geo.get("status") != "1":
            return {"success": False, "error": geo.get("info", "逆地理编码失败")}
        comp = geo.get("regeocode", {}).get("addressComponent", {})
        city = comp.get("city") or comp.get("province")
        # 高德直辖市 city 返回空列表，回退 province
        if isinstance(city, list):
            city = comp.get("province")
        if not city:
            return {"success": False, "error": "未能识别城市"}
        return {
            "success": True,
            "city": city,
            "province": comp.get("province"),
            "district": comp.get("district"),
            "address": geo.get("regeocode", {}).get("formatted_address"),
        }
    except asyncio.TimeoutError:
        return {"success": False, "error": "定位超时(15s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Tick 日志（监控台按日期查看）──────────────────────


@router.get("/ticks")
async def get_sentinel_ticks(date: str = ""):
    """返回指定日期的每 tick 快照 + 偏离 + 告警。date: YYYY-MM-DD，默认今天。"""
    import sqlite3
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    db_path = "silicon_perception/data/silicon_perception.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    snapshots = conn.execute(
        "SELECT timestamp, heart_rate, steps_today, input_idle_seconds, "
        "screen_active, location_category, user_status, behavior_state "
        "FROM health_snapshots "
        "WHERE date(timestamp) = ? AND (rowid % 10 = 0 OR rowid = (SELECT MIN(rowid) FROM health_snapshots WHERE date(timestamp) = ?)) "
        "ORDER BY timestamp",
        (date, date)
    ).fetchall()

    deviations = conn.execute(
        "SELECT timestamp, dimension, z_score, severity, deviation_direction, "
        "repeat_count, first_seen_at, last_seen_at "
        "FROM baseline_deviations WHERE date(timestamp) = ? ORDER BY timestamp",
        (date,)
    ).fetchall()

    events = conn.execute(
        "SELECT timestamp, rule_id, priority, message, pushed, concern_level "
        "FROM sentinel_events WHERE date(timestamp) = ? ORDER BY timestamp",
        (date,)
    ).fetchall()
    conn.close()

    ticks = []
    for s in snapshots:
        ts = s["timestamp"]
        ts_devs = [dict(d) for d in deviations if d["timestamp"][:16] == ts[:16]]
        ts_events = [dict(e) for e in events if e["timestamp"][:16] == ts[:16]]
        ticks.append({
            "timestamp": ts, "heart_rate": s["heart_rate"],
            "steps_today": s["steps_today"],
            "input_idle_seconds": s["input_idle_seconds"],
            "screen_active": bool(s["screen_active"]) if s["screen_active"] is not None else None,
            "location_category": s["location_category"],
            "user_status": s["user_status"],
            "behavior_state": s["behavior_state"],
            "deviations": ts_devs, "events": ts_events,
        })

    return {
        "date": date, "tick_count": len(ticks),
        "deviation_count": len(deviations), "event_count": len(events),
        "ticks": ticks,
    }


@router.get("/triggers")
async def get_sentinel_triggers():
    """三级触发器状态总览。"""
    sentinel, available, _ = _get_sentinel()
    if sentinel is None:
        raise HTTPException(status_code=503, detail="哨兵未初始化" if available else "哨兵模块不可用")

    import time as _time
    td = sentinel._trigger_detector if hasattr(sentinel, '_trigger_detector') else None
    now_ts = _time.monotonic()
    last = td._last if td else None

    # L1: 动态——从 trigger_detector 实际检测能力构建（心率已移交 C1/C2，不再在此）
    l1 = [
        {"name": "步数", "last_val": last.steps_today if last else None,
         "events": ["steps_burst", "steps_stagnant"]},
        {"name": "GPS", "last_val": last.location_category if last else None,
         "events": ["gps_moved", "gps_category_change"]},
        {"name": "行为状态", "last_val": last.behavior_state if last else None,
         "events": ["behavior_state_change"]},
        {"name": "应用会话", "last_val": (last.active_app_session if last else None),
         "events": ["app_session_long"]},
        {"name": "用户状态", "last_val": last.user_status if last else None,
         "events": ["status_change_silent"]},
    ]

    # L2: 救命规则（anomaly_detector 直推，绕 Flash）
    l2 = [
        {"id": "C1", "name": "静息心率过高", "desc": "HR>120 + 静坐 + 持续3次"},
        {"id": "C2", "name": "心率骤升", "desc": "ΔHR>40 + 静坐 + 外出豁免"},
    ]

    # 所有异常检测规则（anomaly_detector，供前端总览/触发统一展示）
    try:
        import json as _json, os as _os
        rules_path = _os.path.join(_os.path.dirname(__file__), "..", "rules.json")
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_raw = _json.load(f)
        rules_all = rules_raw.get("rules", [])
    except Exception:
        rules_all = []

    # L3: 保底定时
    l3_next = max(0, 1800 - (now_ts - td._last_flash_time)) if td else 1800
    l3 = {"interval_min": 30, "next_seconds": round(l3_next)}

    # 最近触发记录 + 今日全量历史
    recent = td._last_fired_triggers[:5] if td else []
    history = []
    try:
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        conn = sentinel._store._get_conn()
        rows = conn.execute(
            "SELECT timestamp, message FROM sentinel_events WHERE event_type='flash_judge' AND date(timestamp)=? ORDER BY id DESC LIMIT 50",
            (today,)
        ).fetchall()
        for r in rows:
            ts = r["timestamp"]
            try:
                t = _dt.datetime.fromisoformat(ts).strftime("%H:%M:%S")
            except Exception:
                t = ts
            msg = r["message"] or ""
            # 解析 triggers=[...] push=... |
            import re
            m_trig = re.search(r'triggers=\[(.*?)\]', msg)
            m_push = re.search(r'push=(\w+)', msg)
            trig_list = [x.strip().strip("'\"") for x in m_trig.group(1).split(",")] if m_trig else []
            history.append({
                "time": t,
                "triggers": trig_list,
                "push": m_push.group(1) if m_push else "?",
                "summary": msg.split(" | ", 1)[-1] if " | " in msg else msg[-80:],
            })
    except Exception:
        pass
    summary = sentinel._summarizer.get_summary() if hasattr(sentinel, '_summarizer') and sentinel._summarizer else ""

    return {"l1": l1, "l2": l2, "l3": l3, "rules_instant": [r for r in rules_all if r.get("trigger_type")=="instant"],
            "rules_log": [r for r in rules_all if r.get("trigger_type")=="log"],
            "last_triggers": recent, "trigger_history": history, "flash_summary": summary}
