"""
音乐耳蜗数据模型
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
import hashlib


@dataclass
class SongIdentity:
    """歌曲身份——从窗口标题或 MediaSession 解析得到"""
    title: str
    artist: str
    album: Optional[str] = None
    source: str = "pc"  # "pc" | "mobile"
    detected_at: str = ""  # ISO timestamp

    @property
    def fingerprint(self) -> str:
        """稳定缓存 key：SHA256(title|artist)"""
        return hashlib.sha256(
            f"{self.title.lower().strip()}|{self.artist.lower().strip()}".encode()
        ).hexdigest()[:16]

    @property
    def display_name(self) -> str:
        return f"{self.title} - {self.artist}"


@dataclass
class SongMetadata:
    """歌曲元数据——从 pyncm/Spotify 获取"""
    identity: SongIdentity
    netease_song_id: Optional[int] = None
    lyrics: str = ""          # 完整歌词文本
    lyrics_snippet: str = ""  # 歌词片段（截断 ~500 字符）
    # 音频特征（可选，来自 Spotify 或 basic-pitch）
    tempo: Optional[float] = None
    energy: Optional[float] = None
    valence: Optional[float] = None
    # 风格标签
    tags: List[str] = field(default_factory=list)
    # 旋律分析（v2，librosa / eryu）
    melody_summary: str = ""
    bpm: Optional[float] = None
    key_name: Optional[str] = None
    energy_segments: List[dict] = field(default_factory=list)  # [{start, end, avgEnergy, maxEnergy}]
    # 记忆关联
    memory_snippets: List[str] = field(default_factory=list)


@dataclass
class CachedReaction:
    """K 对一首歌的反应——缓存到 SQLite"""
    song_fingerprint: str
    k_reaction: str           # K 的 ~200 字感受
    push_node: str = ""       # "start" | "middle" | "end"
    generated_at: str = ""    # ISO timestamp
    play_count: int = 1
    last_heard_at: str = ""


@dataclass
class MusicCochleaState:
    """音乐耳蜗当前状态"""
    active: bool = False
    current_song: Optional[SongIdentity] = None
    current_metadata: Optional[SongMetadata] = None
    elapsed_seconds: float = 0.0    # 当前歌曲已播放时长
    play_count_today: int = 0       # 今日第几次听这首歌
    last_push_at: float = 0.0       # monotonic, 上次推送时间
    last_push_song: str = ""        # 上次推送的歌（fingerprint，用于冷却）
    cooldown_until: Dict[str, float] = field(default_factory=dict)  # fingerprint → monotonic
    source: str = ""                # "pc" | "mobile"
    music_mode_on_at: float = 0.0   # monotonic, 音乐模式开启时间
    session_song_count: int = 0     # 本轮音乐模式听了几首
    session_songs: List[SongIdentity] = field(default_factory=list)  # 本轮听过的歌（供品味进化）
