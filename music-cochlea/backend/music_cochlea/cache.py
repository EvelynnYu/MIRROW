"""
音乐耳蜗缓存层 — SQLite 存储

两表：
- song_meta:  歌曲元数据（永久），key = SHA256(title|artist)
- reaction:   K 的反应（永久，>30 天刷新），key = SHA256(title|artist)
"""

import sqlite3
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, List

from .models import SongIdentity, SongMetadata, CachedReaction

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "music_cochlea.db")
REACTION_REFRESH_DAYS = 30


class SongCache:
    """歌曲元数据 + K 的反应缓存"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    # ── 连接管理 ──

    def _get_conn(self) -> sqlite3.connect:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 建表 ──

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS song_meta (
                    fingerprint TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT,
                    netease_song_id INTEGER,
                    lyrics TEXT DEFAULT '',
                    lyrics_snippet TEXT DEFAULT '',
                    tempo REAL,
                    energy REAL,
                    valence REAL,
                    tags TEXT DEFAULT '',
                    bpm REAL,
                    key_name TEXT DEFAULT '',
                    melody_summary TEXT DEFAULT '',
                    energy_segments TEXT DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    play_count_total INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS reaction (
                    fingerprint TEXT PRIMARY KEY,
                    push_node TEXT DEFAULT '',
                    k_reaction TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    play_count INTEGER DEFAULT 1,
                    last_heard_at TEXT
                );

                -- 每日播放计数（fingerprint + date 唯一）
                CREATE TABLE IF NOT EXISTS daily_play_count (
                    fingerprint TEXT NOT NULL,
                    date TEXT NOT NULL,
                    play_count INTEGER DEFAULT 1,
                    PRIMARY KEY (fingerprint, date)
                );
            """)
            # 兼容迁移：v2 新增的旋律特征列
            for col, col_type in [
                ("tags", "TEXT DEFAULT ''"),
                ("bpm", "REAL"),
                ("key_name", "TEXT DEFAULT ''"),
                ("melody_summary", "TEXT DEFAULT ''"),
                ("energy_segments", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE song_meta ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在

    # ── 元数据 CRUD ──

    def get_meta(self, fingerprint: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM song_meta WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_meta(self, fingerprint: str, identity: SongIdentity,
                    lyrics: str = "", lyrics_snippet: str = "",
                    netease_song_id: Optional[int] = None,
                    tempo: Optional[float] = None,
                    energy: Optional[float] = None,
                    valence: Optional[float] = None,
                    tags: list = None,
                    bpm: Optional[float] = None,
                    key_name: str = "",
                    melody_summary: str = ""):
        now = datetime.now().isoformat()
        tags_str = ",".join(tags) if tags else ""
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT play_count_total FROM song_meta WHERE fingerprint = ?",
                (fingerprint,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE song_meta
                    SET last_seen_at = ?, play_count_total = play_count_total + 1,
                        lyrics = CASE WHEN ? != '' THEN ? ELSE lyrics END,
                        lyrics_snippet = CASE WHEN ? != '' THEN ? ELSE lyrics_snippet END,
                        tags = CASE WHEN ? != '' THEN ? ELSE tags END
                    WHERE fingerprint = ?
                """, (now, lyrics, lyrics, lyrics_snippet, lyrics_snippet,
                      tags_str, tags_str, fingerprint))
            else:
                conn.execute("""
                    INSERT INTO song_meta
                    (fingerprint, title, artist, album, netease_song_id,
                     lyrics, lyrics_snippet, tempo, energy, valence, tags,
                     bpm, key_name, melody_summary,
                     first_seen_at, last_seen_at, play_count_total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    fingerprint, identity.title, identity.artist, identity.album,
                    netease_song_id, lyrics, lyrics_snippet,
                    tempo, energy, valence, tags_str,
                    bpm, key_name, melody_summary,
                    now, now
                ))

    def upsert_melody_features(self, fingerprint: str, bpm: float = None,
                               key_name: str = "", melody_summary: str = "",
                               energy_segments: list = None):
        """更新旋律分析结果到缓存（后台 fire-and-forget 调用）"""
        import json as _json
        seg_json = _json.dumps(energy_segments) if energy_segments else ""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE song_meta
                SET bpm = COALESCE(?, bpm),
                    key_name = CASE WHEN ? != '' THEN ? ELSE key_name END,
                    melody_summary = CASE WHEN ? != '' THEN ? ELSE melody_summary END,
                    energy_segments = CASE WHEN ? != '' THEN ? ELSE energy_segments END
                WHERE fingerprint = ?
            """, (bpm, key_name, key_name, melody_summary, melody_summary,
                  seg_json, seg_json, fingerprint))

    # ── Reaction CRUD ──

    def get_reaction(self, fingerprint: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM reaction WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return dict(row) if row else None

    def needs_refresh(self, fingerprint: str) -> bool:
        """检查 reaction 是否需要刷新（>30 天）"""
        existing = self.get_reaction(fingerprint)
        if not existing:
            return True  # 没有缓存 → 需要生成
        try:
            gen_time = datetime.fromisoformat(existing["generated_at"])
            return (datetime.now() - gen_time).days > REACTION_REFRESH_DAYS
        except (ValueError, KeyError):
            return True

    def upsert_reaction(self, fingerprint: str, k_reaction: str,
                        push_node: str = "", play_count: int = 1):
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO reaction (fingerprint, push_node, k_reaction, generated_at, play_count, last_heard_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    push_node = excluded.push_node,
                    k_reaction = excluded.k_reaction,
                    generated_at = excluded.generated_at,
                    play_count = reaction.play_count + 1,
                    last_heard_at = excluded.last_heard_at
            """, (fingerprint, push_node, k_reaction, now, play_count, now))

    # ── 每日计数 ──

    def increment_daily_count(self, fingerprint: str) -> int:
        """增加今日播放计数，返回今日总次数"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO daily_play_count (fingerprint, date, play_count)
                VALUES (?, ?, 1)
                ON CONFLICT(fingerprint, date) DO UPDATE SET
                    play_count = play_count + 1
            """, (fingerprint, today))
            row = conn.execute(
                "SELECT play_count FROM daily_play_count WHERE fingerprint = ? AND date = ?",
                (fingerprint, today)
            ).fetchone()
            return row["play_count"] if row else 1

    def get_today_count(self, fingerprint: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT play_count FROM daily_play_count WHERE fingerprint = ? AND date = ?",
                (fingerprint, today)
            ).fetchone()
            return row["play_count"] if row else 0

    # ── 缓存歌单查询（LISTEN_MUSIC 用） ──

    def get_cached_playlist(self, limit: int = 30) -> list:
        """
        已有基本元数据的歌单（有歌词即可，旋律分析为可选项），按最近播放排序。
        用于 LISTEN_MUSIC 漫想事件：「先看一眼已有的缓存歌单」。
        返回完整 dict 列表，可直接注入选歌 prompt。
        注：melody_summary 可能为空（basic-pitch 管道有已知 bug，见 melody_cache/_analyze_error.txt）。
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT s.*, r.k_reaction, r.generated_at as reaction_at
                FROM song_meta s
                LEFT JOIN reaction r ON s.fingerprint = r.fingerprint
                WHERE s.lyrics IS NOT NULL AND s.lyrics != ''
                ORDER BY s.last_seen_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_recently_played(self, limit: int = 20) -> list:
        """
        最近听过的歌（含无旋律分析的），按最近播放排序。
        用于 Pro 选歌时了解近期听歌历史。
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT s.*, r.k_reaction, r.generated_at as reaction_at
                FROM song_meta s
                LEFT JOIN reaction r ON s.fingerprint = r.fingerprint
                ORDER BY s.last_seen_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_meta_by_netease_id(self, netease_song_id: int) -> Optional[dict]:
        """通过网易云歌曲 ID 查找缓存"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM song_meta WHERE netease_song_id = ?",
                (netease_song_id,)
            ).fetchone()
            return dict(row) if row else None
