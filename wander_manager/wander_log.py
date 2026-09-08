# 漫想日志层
#
# 功能：
# 1. 记录每次漫想事件的类型、时间、过程
# 2. 支持内存存储和持久化存储
# 3. 支持日志查询和清理
# 4. Phase 4: 集成持久化模块

from datetime import datetime, timedelta
from typing import List, Optional
from dataclasses import dataclass, field
import logging
import asyncio

from .event_types import EventType, WanderEvent

logger = logging.getLogger(__name__)


@dataclass
class WanderLogEntry:
    """漫想日志条目"""
    event_type: EventType
    timestamp: datetime
    description: str
    process_log: str
    event_id: str = ""                         # 关联的 WanderEvent ID（去重/追踪）
    details: Optional[dict] = None             # 事件详情（idle_seconds, status_consistent 等）
    judgment_result: Optional[dict] = None     # 主动打扰判断结果
    pushed: bool = False                       # 是否已推送给用户

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "description": self.description,
            "process_log": self.process_log,
            "event_id": self.event_id,
            "details": self.details,
            "judgment_result": self.judgment_result,
            "pushed": self.pushed
        }

    @classmethod
    def from_event(cls, event: WanderEvent) -> "WanderLogEntry":
        """从事件创建日志条目"""
        return cls(
            event_type=event.event_type,
            timestamp=event.timestamp,
            description=event.description,
            process_log=event.process_log,
            event_id=event.event_id,
            details=event.details.copy() if event.details else None,
        )


class WanderLog:
    """
    漫想日志层 - 记录漫想事件

    核心逻辑：
    - 接收漫想创建层产生的事件
    - 记录事件类型、时间、过程
    - 支持内存存储和持久化存储
    - 支持日志查询和清理
    """

    # 默认配置
    DEFAULT_RETENTION_HOURS = 24  # 日志保留时间：1天

    def __init__(
        self,
        retention_hours: int = None,
        persistence_enabled: bool = False,
        log_file: str = None
    ):
        """
        初始化漫想日志层

        Args:
            retention_hours: 日志保留时间（小时）
            persistence_enabled: 是否启用持久化
            log_file: 日志文件路径
        """
        self.retention_hours = retention_hours or self.DEFAULT_RETENTION_HOURS
        self._entries: List[WanderLogEntry] = []
        self._persistence_enabled = False  # JSON 持久化已废弃，统一使用 SQLite

        # 从 SQLite 加载历史日志到内存（最近 retention_hours 内的）
        try:
            from .wander_log_sqlite import query_logs
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(hours=self.retention_hours)
            sqlite_rows = query_logs(limit=500, since=cutoff)
            for row in sqlite_rows:
                try:
                    et = EventType(row["event_type"])
                except ValueError:
                    continue
                entry = WanderLogEntry(
                    event_type=et,
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    description=row.get("description", ""),
                    process_log=row.get("process_log", ""),
                    event_id=row.get("event_id", ""),
                    details=row.get("details"),
                    judgment_result=row.get("judgment_result"),
                    pushed=row.get("pushed", False),
                )
                self._entries.append(entry)
            logger.info(f"从 SQLite 加载 {len(self._entries)} 条历史日志")
        except Exception:
            pass

    def add_entry(self, event: WanderEvent) -> WanderLogEntry:
        """
        添加日志条目

        Args:
            event: 漫想事件

        Returns:
            创建的日志条目
        """
        entry = WanderLogEntry.from_event(event)
        self._entries.append(entry)

        self.cleanup_expired()

        # SQLite 持久化（排除 SLEEP 和 USER_TRACKING）
        try:
            from .wander_log_sqlite import store_event
            store_event(
                event_id=event.event_id,
                event_type=event.event_type.value,
                timestamp=event.timestamp.isoformat(),
                description=event.description,
                process_log=event.process_log,
                details=event.details,
                session_id=event.details.get("session_id", "") if event.details else "",
            )
        except Exception:
            pass

        logger.info(f"漫想日志已添加: {entry.event_type.value} @ {entry.timestamp}")
        return entry

    def update_judgment(self, entry: WanderLogEntry, judgment: dict, pushed: bool):
        """
        更新日志条目的判断结果

        Args:
            entry: 日志条目
            judgment: 判断结果
            pushed: 是否推送
        """
        entry.judgment_result = judgment
        entry.pushed = pushed

        # SQLite 更新
        try:
            from .wander_log_sqlite import update_judgment
            update_judgment(entry.event_id, judgment, pushed)
        except Exception:
            pass

        logger.info(f"漫想日志已更新: {entry.event_type.value}, 推送={pushed}")

    def get_entries(
        self,
        event_type: Optional[EventType] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
        pushed_only: Optional[bool] = None,
    ) -> List[WanderLogEntry]:
        """
        查询日志条目。当 since 是过去日期时直接查 SQLite（内存仅保留24h）。
        """
        if since is not None and since.tzinfo is not None:
            since = since.replace(tzinfo=None)
        is_past_date = since is not None and since.date() < datetime.now().date()

        entries: List[WanderLogEntry] = []
        if not is_past_date:
            entries = list(self._entries)
            if event_type:
                entries = [e for e in entries if e.event_type == event_type]
            if since:
                entries = [e for e in entries if e.timestamp >= since]
            if pushed_only is not None:
                entries = [e for e in entries if e.pushed == pushed_only]
            if len(entries) >= limit:
                entries = sorted(entries, key=lambda e: e.timestamp, reverse=True)
                return entries[:limit]

        # 内存不足或查询过去日期 → 回退 SQLite
        oldest_memory = min((e.timestamp for e in self._entries), default=None)
        need_sqlite = is_past_date or (since and (oldest_memory is None or since < oldest_memory))
        if need_sqlite or len(entries) < limit:
            try:
                from .wander_log_sqlite import query_logs
                sqlite_rows = query_logs(
                    limit=limit,
                    pushed=pushed_only,
                    since=since,
                    date=None,
                )
                memory_ids = {e.event_id for e in entries}
                for row in sqlite_rows:
                    if row.get("event_id") in memory_ids:
                        continue
                    try:
                        et = EventType(row["event_type"])
                    except ValueError:
                        continue
                    sqlite_entry = WanderLogEntry(
                        event_type=et,
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        description=row.get("description", ""),
                        process_log=row.get("process_log", ""),
                        event_id=row.get("event_id", ""),
                        details=row.get("details"),
                        judgment_result=row.get("judgment_result"),
                        pushed=row.get("pushed", False),
                    )
                    entries.append(sqlite_entry)
            except Exception:
                pass

        # SQLite 合并后重新过滤
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        if event_type:
            entries = [e for e in entries if e.event_type == event_type]
        if pushed_only is not None:
            entries = [e for e in entries if e.pushed == pushed_only]

        entries = sorted(entries, key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def get_recent_entries(self, count: int = 10, pushed_only: Optional[bool] = None) -> List[WanderLogEntry]:
        """
        获取最近的日志条目

        Args:
            count: 数量
            pushed_only: None=全部, True=仅已推送, False=仅已丢弃

        Returns:
            日志条目列表
        """
        return self.get_entries(limit=count, pushed_only=pushed_only)

    def get_entries_since_last_user_tracking(self) -> List[WanderLogEntry]:
        """
        获取自上次用户追踪以来的所有事件

        Returns:
            日志条目列表
        """
        entries = []
        for entry in reversed(self._entries):
            entries.insert(0, entry)
            if entry.event_type == EventType.USER_TRACKING:
                break
        return entries

    def get_entries_since_last_sleep(self) -> List[WanderLogEntry]:
        """
        获取自上次休眠以来的所有事件

        Returns:
            日志条目列表
        """
        entries = []
        for entry in reversed(self._entries):
            entries.insert(0, entry)
            if entry.event_type == EventType.SLEEP:
                break
        return entries

    def cleanup_expired(self):
        """清理过期日志"""
        cutoff = datetime.now() - timedelta(hours=self.retention_hours)
        before_count = len(self._entries)
        self._entries = [e for e in self._entries if e.timestamp >= cutoff]
        after_count = len(self._entries)

        if before_count != after_count:
            logger.info(f"清理过期日志: {before_count} -> {after_count}")

    def clear(self):
        """清空所有日志"""
        self._entries.clear()

        # 清空持久化文件
        if self._persistence:
            self._persistence.save([])

        logger.info("漫想日志已清空")

    def get_stats(self, pushed_only: Optional[bool] = None,
                  since: Optional[datetime] = None) -> dict:
        """
        获取日志统计（支持过滤）

        Args:
            pushed_only: None=全部, True=仅已推送, False=仅已丢弃
            since: 起始时间过滤
        """
        entries = self._entries
        if since:
            if since.tzinfo is not None:
                since = since.replace(tzinfo=None)
            entries = [e for e in entries if e.timestamp >= since]
        if pushed_only is not None:
            entries = [e for e in entries if e.pushed == pushed_only]

        total = len(entries)

        type_counts = {}
        for entry in entries:
            type_name = entry.event_type.value
            type_counts[type_name] = type_counts.get(type_name, 0) + 1

        pushed_count = sum(1 for e in entries if e.pushed)

        return {
            "total_entries": total,
            "type_counts": type_counts,
            "pushed_count": pushed_count,
            "retention_hours": self.retention_hours,
            "persistence_enabled": self._persistence_enabled
        }

    async def start_persistence(self):
        """持久化已由 SQLite 实时处理，此方法保留兼容性不做实际操作"""
        pass

    async def stop_persistence(self):
        """持久化已由 SQLite 实时处理，此方法保留兼容性不做实际操作"""
        pass

    def force_save(self):
        """持久化已由 SQLite 实时处理，此方法保留兼容性不做实际操作"""
        pass


# 全局单例
_wander_log_instance: Optional[WanderLog] = None


def get_wander_log() -> WanderLog:
    """获取全局漫想日志实例"""
    global _wander_log_instance
    if _wander_log_instance is None:
        _wander_log_instance = WanderLog()
    return _wander_log_instance


def init_wander_log(
    retention_hours: int = None,
    persistence_enabled: bool = False,
    log_file: str = None
) -> WanderLog:
    """初始化全局漫想日志实例"""
    global _wander_log_instance
    _wander_log_instance = WanderLog(
        retention_hours=retention_hours,
        persistence_enabled=persistence_enabled,
        log_file=log_file
    )
    return _wander_log_instance
