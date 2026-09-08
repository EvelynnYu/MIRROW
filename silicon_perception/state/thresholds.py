"""三层阈值解析 — 用户设置 → behavior_profile 基线 → 硬编码回退。

设计：
    resolve(key) = user_override[key]  if 用户手动配
                 = baseline.derive(key) if 基线可信
                 = HARDCODED_FALLBACK[key]  兜底

只放**状态引擎**阈值（切状态用）。哨兵阈值（回复沉默/心率偏离/久坐）归后续独立"哨兵配置页"，
共用本机制但配置分页。

持久化：data/state_thresholds.json（用户手动配的值 + 每键的自动同步开关）。
"""

from __future__ import annotations

import os
import json
import logging
from typing import Dict, Optional, Callable, Any

logger = logging.getLogger(__name__)

_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "state_thresholds.json"
)

# 硬编码回退（最底层兜底）
_FALLBACK: Dict[str, float] = {
    "idle_active_sec": 120,        # 键鼠 idle < 此值 → 在电脑前活跃
    "idle_away_sec": 300,          # 键鼠 idle > 此值（+屏幕不活跃+手机灭屏）→ 离开
    "phone_glance_sec": 120,       # 手机连续亮屏 < 此值 → 视为瞄一眼，保持 at_computer
    "coding_sustain_sec": 900,     # 前台编码工具持续 ≥ 此值 → coding（15min）
    "gaming_coding_sustain_sec": 1200,  # 声明 gaming 期间切 coding 需更长确认（20min）
    "napping_hr_recover_delta": 10,     # napping 切出：心率回升超过 resting+此值
    "confirm_samples": 2,          # 连续确认次数（防抖）
    "min_dwell_sec": 120,          # 最小驻留时间（防高频抖动）
}


def _derive_idle_away(bp) -> Optional[float]:
    """从 pc_active 基线派生离开阈值。当前保守：基线可信时取更贴合的值，否则 None。"""
    try:
        cache = getattr(bp, "_cache", {}) or {}
        pc = cache.get("pc_active", {}) or {}
        # pc_active 是按小时活跃比，暂不直接映射为秒数阈值——留待哨兵页细化。
        # 阶段1 返回 None（用回退），保留接口。
        return None
    except Exception:
        return None


def _derive_napping_hr(bp) -> Optional[float]:
    """napping 切出 delta 无需基线派生（delta 本身是相对量），基线提供 resting_mean 供引擎用。"""
    return None


# 每键的基线派生函数（无则该键不走基线层）
_BASELINE_DERIVE: Dict[str, Callable[[Any], Optional[float]]] = {
    "idle_away_sec": _derive_idle_away,
    "napping_hr_recover_delta": _derive_napping_hr,
}


class Thresholds:
    """三层阈值解析单例。"""

    def __init__(self):
        # {key: value} 用户手动配
        self.user_override: Dict[str, float] = {}
        # {key: bool} 每键"每活跃日自动同步基线"开关
        self.auto_sync: Dict[str, bool] = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(_OVERRIDE_FILE):
                with open(_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.user_override = data.get("user_override", {}) or {}
                self.auto_sync = data.get("auto_sync", {}) or {}
        except Exception as e:
            logger.warning(f"Thresholds 加载失败: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_OVERRIDE_FILE), exist_ok=True)
            with open(_OVERRIDE_FILE, "w", encoding="utf-8") as f:
                json.dump({"user_override": self.user_override, "auto_sync": self.auto_sync},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Thresholds 保存失败: {e}")

    def _baseline_value(self, key: str) -> Optional[float]:
        """尝试从 behavior_profile 基线派生该键的值。不可用返回 None。"""
        derive = _BASELINE_DERIVE.get(key)
        if not derive:
            return None
        try:
            from silicon_perception.analysis.behavior_profile import get_behavior_profile
            bp = get_behavior_profile()
            # 仅在整体置信度足够时采信基线
            if hasattr(bp, "_overall_confidence") and bp._overall_confidence() < 0.7:
                return None
            return derive(bp)
        except Exception:
            return None

    def resolve(self, key: str) -> float:
        """三层取值：用户设置 → 基线 → 回退。"""
        if key in self.user_override:
            return self.user_override[key]
        bv = self._baseline_value(key)
        if bv is not None:
            return bv
        return _FALLBACK.get(key, 0)

    def resting_hr(self) -> Optional[float]:
        """静息心率基线（供 napping 切出判定，非阈值 key）。"""
        try:
            from silicon_perception.analysis.behavior_profile import get_behavior_profile
            bp = get_behavior_profile()
            cache = getattr(bp, "_cache", {}) or {}
            rest = (cache.get("heart_rate", {}) or {}).get("resting_mean", 0)
            return float(rest) if rest else None
        except Exception:
            return None

    # ---------- 前端配置 UI 支持 ----------
    def set_override(self, key: str, value: float):
        if key in _FALLBACK:
            self.user_override[key] = value
            self._save()

    def clear_override(self, key: str):
        self.user_override.pop(key, None)
        self._save()

    def set_auto_sync(self, key: str, enabled: bool):
        self.auto_sync[key] = bool(enabled)
        self._save()

    def sync_from_baseline(self, key: str) -> bool:
        """「按基线更新」：把当前基线值填入 user_override。基线不可用返回 False。"""
        bv = self._baseline_value(key)
        if bv is None:
            return False
        self.user_override[key] = bv
        self._save()
        return True

    def apply_auto_sync(self):
        """每活跃日调用：对开了 auto_sync 的键，把基线值同步进 override（持久化）。"""
        changed = False
        for key, on in self.auto_sync.items():
            if not on:
                continue
            bv = self._baseline_value(key)
            if bv is not None:
                self.user_override[key] = bv
                changed = True
        if changed:
            self._save()

    def describe(self) -> dict:
        """供前端渲染阈值配置：每键的 有效值/来源层/基线值/是否手配/自动同步。"""
        out = {}
        for key, fb in _FALLBACK.items():
            bv = self._baseline_value(key)
            if key in self.user_override:
                eff, layer = self.user_override[key], "user"
            elif bv is not None:
                eff, layer = bv, "baseline"
            else:
                eff, layer = fb, "fallback"
            out[key] = {
                "effective": eff,
                "layer": layer,
                "fallback": fb,
                "baseline": bv,
                "user_value": self.user_override.get(key),
                "auto_sync": self.auto_sync.get(key, False),
            }
        return out


_thresholds: Optional[Thresholds] = None


def get_thresholds() -> Thresholds:
    global _thresholds
    if _thresholds is None:
        _thresholds = Thresholds()
    return _thresholds
