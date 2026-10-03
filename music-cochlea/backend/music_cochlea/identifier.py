"""
歌曲指纹识别 — ShazamIO 异步识别（⏸ 延后到和语音模块一起）

当前仅提供占位接口，实际识别通过窗口标题/MediaSession 完成。
WASAPI + ShazamIO 路径后续实现时参考 PyNowPlaying (MIT)。
"""

from typing import Optional
from .models import SongIdentity


class SongIdentifier:
    """ShazamIO 音频指纹识别（⏸ 延后）"""

    async def identify(self, audio_bytes: bytes) -> Optional[SongIdentity]:
        """从音频片段识别歌曲——待 WASAPI 路径上线后实现"""
        return None
