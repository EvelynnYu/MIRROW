"""统一时间戳工具。时区由 TIMEZONE 环境变量控制（默认 Asia/Shanghai）。"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
_BEIJING = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    """返回带时区偏移的 ISO 8601 (YYYY-MM-DDTHH:MM:SS.mmmmmm+08:00)，JavaScript new Date() 自动解析"""
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def now_date() -> str:
    """返回时区感知的日期 (YYYY-MM-DD)，用于日志分目录"""
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def parse_ts(ts_str: str) -> datetime:
    """解析任意格式 ISO 时间戳为 timezone-aware datetime，用于排序/比较。
    兼容 Z / +00:00 / +08:00 / naive 四种输入。
    """
    if not ts_str:
        return datetime.min.replace(tzinfo=_BEIJING)
    try:
        s = ts_str.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=_BEIJING)


def to_beijing_time(iso_str: str) -> str:
    """将任意 ISO 时间戳转为北京时间显示格式。
    输入: Z / +00:00 / naive(假定为北京时间)
    输出: '2026-06-01T14:33+08:00'
    前端 .slice(11,16) → '14:33'
    """
    if not iso_str:
        return ""
    try:
        dt = parse_ts(iso_str)
        beijing = dt.astimezone(_BEIJING)
        return beijing.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    except Exception:
        return iso_str[:16] if 'T' in iso_str else iso_str[:16]
