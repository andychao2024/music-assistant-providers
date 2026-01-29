"""
Netease Cloud Music API Lyrics Metadata Provider for Music Assistant
云音乐API 歌词自动补全（支持毫秒级歌词同步）
Version 1.2.6
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientResponseError, ContentTypeError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.media_items import MediaItemMetadata, Track

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}

# 配置项
CONF_BASE_URL = "base_url"
DEFAULT_BASE_URL = "http://BaseURL:3003"
USER_AGENT = "MusicAssistant (https://github.com/music-assistant/server)"

LRC_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]")
NON_STANDARD_LRC_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\](?!\.)")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """初始化插件实例"""
    return NeteaseMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """插件配置项"""
    return (
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API 服务地址",
            description="自建云音乐 API 地址（示例：http://192.168.110.156:3003）",
            default_value=DEFAULT_BASE_URL,
            required=False,
        ),
    )


class NeteaseMusicProvider(MetadataProvider):
    """云音乐歌词插件：提供毫秒级同步歌词"""

    async def handle_async_init(self) -> None:
        """初始化插件"""
        self.base_url = self.config.get_value(CONF_BASE_URL)
        self.search_api_url = f"{self.base_url}/search" if not self.base_url.endswith("/") else f"{self.base_url}search"
        self.lyric_api_url = f"{self.base_url}/lyric" if not self.base_url.endswith("/") else f"{self.base_url}lyric"
        rate_limit = 10 if self.base_url == DEFAULT_BASE_URL else 1
        period = 5 if self.base_url == DEFAULT_BASE_URL else 1
        self.throttler = ThrottlerManager(rate_limit=rate_limit, period=period)

    def _normalize_lrc(self, lrc_content: str) -> str:
        if not lrc_content:
            return ""
        
        normalized_lines = []
        for line in lrc_content.split("\n"):
            line = line.strip()
            if not line or line.startswith(("[ti:", "[ar:", "[al:", "[au:", "[by:")):
                continue
            
            if LRC_TIMESTAMP_PATTERN.match(line):
                normalized_lines.append(line)
 
            elif NON_STANDARD_LRC_PATTERN.match(line):
                match = NON_STANDARD_LRC_PATTERN.search(line)
                if match:
                    minutes = match.group(1).zfill(2)
                    seconds = match.group(2).zfill(2)
                    lyric_content = NON_STANDARD_LRC_PATTERN.sub("", line).strip()
                    normalized_lines.append(f"[{minutes}:{seconds}.000] {lyric_content}")
        
        return "\n".join(normalized_lines)

    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _search_song_id(self, track_name: str, artist_name: str) -> str | None:
        """搜索歌曲ID（缓存14天，带限流）"""
        params = {"keywords": f"{track_name} {artist_name}", "type": 1, "limit": 10}
        headers = {"User-Agent": USER_AGENT}

        try:
            async with self.mass.http_session.get(self.search_api_url, params=params, headers=headers) as response:
                response.raise_for_status()
                if response.status == 204:
                    return None
                data = cast("dict[str, Any]", await response.json())
                if data.get("code") == 200 and data.get("result") and data["result"].get("songs"):
                    return str(data["result"]["songs"][0]["id"])
            return None
        except (ClientResponseError, json.JSONDecodeError, ContentTypeError):
            return None

    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _get_lyrics(self, song_id: str) -> str | None:
        """获取同步歌词（缓存14天，带限流）"""
        headers = {"User-Agent": USER_AGENT}
        params = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}

        try:
            async with self.mass.http_session.get(self.lyric_api_url, params=params, headers=headers) as response:
                response.raise_for_status()
                if response.status == 204:
                    return None
                data = cast("dict[str, Any]", await response.json())
                synced_lyrics = data.get("lrc", {}).get("lyric", "")
                return self._normalize_lrc(synced_lyrics) if synced_lyrics else None
        except (ClientResponseError, json.JSONDecodeError, ContentTypeError):
            return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """获取歌曲歌词元数据"""
        if track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics):
            return None
        if not track.artists or not track.duration:
            return None

        artist_name = track.artists[0].name
        track_name = re.sub(r"\(.*?\)|\[.*?\]|-.*?$", "", track.name).strip()

        song_id = await self._search_song_id(track_name, artist_name)
        if not song_id:
            return None
        synced_lyrics = await self._get_lyrics(song_id)
        if not synced_lyrics:
            return None

        metadata = MediaItemMetadata()
        metadata.lrc_lyrics = synced_lyrics
        return metadata