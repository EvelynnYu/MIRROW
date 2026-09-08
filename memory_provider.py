"""
MemoryProvider 协议 —— MIRROW 上下文模块的记忆检索抽象

本模块定义 MemoryProvider 协议(一组标准化的异步方法签名)和
MemoryItem 数据结构。使用者在初始化 ContextBuilder 时注入自己的
Provider 实现(向量库 / SQLite / 纯文件等均可),Builder 通过协议
调用——不绑定任何具体存储。

不注入 Provider 时记忆和待办段自动降级为空,不影响其他上下文段。
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class MemoryItem:
    """单条记忆碎片的标准化数据结构。

    属性名保持与 OB_Rev bucket metadata 兼容,但不再依赖 OB_Rev——
    任何存储后端只要把数据映射到此结构,就能接入 ContextBuilder。
    """
    id: str                               # 记忆唯一 ID
    content: str                          # 记忆正文(自然语言,已去 wikilink)
    topic: str = ""                       # 主题/标题
    timestamp: str = ""                   # ISO 8601 创建时间(YYYY-MM-DDTHH:MM:SS)
    importance_score: float = 0.5         # 重要性(0.0-1.0)
    valence: Optional[float] = None       # 情感效价(-1.0 负面 ~ +1.0 正面)
    arousal: Optional[float] = None       # 情感唤醒度(0.0 平静 ~ 1.0 强烈)
    domain: str = ""                      # 所属领域(如 "日常/饮食"、"情感")
    tags: list[str] = field(default_factory=list)  # 标签列表(如 ["旅行","海边"])

    @property
    def metadata(self) -> dict:
        """兼容旧版代码:返回 OB_Rev 风格的 metadata dict。"""
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "domain": self.domain,
            "tags": self.tags,
        }


@dataclass
class TodoItem:
    """单条待办事项。"""
    bucket_id: str                        # 待办唯一 ID
    content: str                          # 待办内容
    urgency: str = "medium"               # "high" / "medium" / "low"
    days_old: int = 0                     # 创建至今的天数


@runtime_checkable
class MemoryProvider(Protocol):
    """记忆检索提供者协议。

    实现本协议中标记的方法即可接入 ContextBuilder——
    search() 对应 memory 段,list_todos() 对应 todo 段。
    未实现的方法在 Builder 中被跳过(降级行为是安全的)。
    """

    async def search(
        self, text: str, top_k: int = 5, query: Optional[str] = None,
        exclude_tags: Optional[list[str]] = None,
    ) -> list[MemoryItem]:
        """语义/关键词搜索记忆。

        Args:
            text: 用于构建检索输入的用户消息或上下文文本
            top_k: 返回条数上限
            query: 可选显式搜索词(为 None 时从 text 自动提取)
            exclude_tags: 排除含这些标签的记忆
        Returns:
            记忆列表,按相关性降序。无结果时返回空列表。
        """
        ...

    async def list_todos(
        self, max_results: int = 10, include_dormant: bool = False,
    ) -> list[TodoItem]:
        """列出活跃待办事项。

        Args:
            max_results: 返回条数上限
            include_dormant: 是否包含已完成的条目
        Returns:
            待办列表,按紧急度和时间排序。无结果时返回空列表。
        """
        ...

    async def touch_todo(self, todo_id: str) -> bool:
        """标记待办为「最近仍被提及」(重设衰减时钟)。

        Args:
            todo_id: 待办唯一 ID(bucket_id 或 TodoItem.bucket_id)
        Returns:
            True 表示操作成功,False 表示条目不存在。
        """
        ...


# ── 最小示例实现(SQLite) ────────────────────────────────

class SQLiteMemoryProvider:
    """基于 SQLite 的最小 MemoryProvider 实现,供参考。

    使用方式:
        provider = SQLiteMemoryProvider("data/my_memories.db")
        builder.build("FULL_CHAT", memory_provider=provider, ...)
    """

    def __init__(self, db_path: str):
        try:
            import aiosqlite
        except ImportError:
            raise ImportError(
                "SQLiteMemoryProvider 需要 aiosqlite: pip install aiosqlite"
            )
        self._db_path = db_path
        self._aiosqlite = aiosqlite

    async def _ensure_table(self):
        async with self._aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    topic TEXT DEFAULT '',
                    timestamp TEXT DEFAULT '',
                    importance_score REAL DEFAULT 0.5,
                    valence REAL,
                    arousal REAL,
                    domain TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    bucket_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    urgency TEXT DEFAULT 'medium',
                    days_old INTEGER DEFAULT 0,
                    dormant INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def search(
        self, text: str, top_k: int = 5, query: Optional[str] = None,
        exclude_tags: Optional[list[str]] = None,
    ) -> list[MemoryItem]:
        await self._ensure_table()
        search_term = query or text
        async with self._aiosqlite.connect(self._db_path) as db:
            db.row_factory = self._aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM memories WHERE content LIKE ? "
                "ORDER BY importance_score DESC LIMIT ?",
                (f"%{search_term}%", top_k),
            )
            rows = await cur.fetchall()
        return [
            MemoryItem(
                id=r["id"], content=r["content"], topic=r["topic"],
                timestamp=r["timestamp"], importance_score=r["importance_score"],
                valence=r["valence"], arousal=r["arousal"],
                domain=r["domain"], tags=__import__("json").loads(r["tags"]),
            )
            for r in rows
        ]

    async def list_todos(
        self, max_results: int = 10, include_dormant: bool = False,
    ) -> list[TodoItem]:
        await self._ensure_table()
        async with self._aiosqlite.connect(self._db_path) as db:
            db.row_factory = self._aiosqlite.Row
            dormant_clause = "" if include_dormant else "WHERE dormant = 0"
            cur = await db.execute(
                f"SELECT * FROM todos {dormant_clause} "
                "ORDER BY CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                "days_old DESC LIMIT ?",
                (max_results,),
            )
            rows = await cur.fetchall()
        return [
            TodoItem(
                bucket_id=r["bucket_id"], content=r["content"],
                urgency=r["urgency"], days_old=r["days_old"],
            )
            for r in rows
        ]

    async def touch_todo(self, todo_id: str) -> bool:
        await self._ensure_table()
        async with self._aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE todos SET days_old = 0 WHERE bucket_id = ?", (todo_id,)
            )
            await db.commit()
            return cur.rowcount > 0
