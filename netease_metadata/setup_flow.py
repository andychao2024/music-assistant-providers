"""Setup flow for the NetEase Metadata provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from . import ConfigKeys

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect API URL and metadata options."""
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=ConfigKeys.API_URL,
                type=ConfigEntryType.STRING,
                label="自建云音乐API地址",
                description="你的自建云音乐API服务地址（如http://localhost:3003）",
                required=True,
                default_value=str(existing.get(ConfigKeys.API_URL) or "http://localhost:3003"),
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_ARTIST_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用艺术家元数据获取",
                default_value=bool(existing.get(ConfigKeys.ENABLE_ARTIST_METADATA, True)),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_ALBUM_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用专辑元数据获取",
                default_value=bool(existing.get(ConfigKeys.ENABLE_ALBUM_METADATA, True)),
                required=False,
                description="优先从歌曲提取专辑ID + 发行年份写入标签",
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_TRACK_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用歌曲元数据获取",
                default_value=bool(existing.get(ConfigKeys.ENABLE_TRACK_METADATA, True)),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_IMAGES,
                type=ConfigEntryType.BOOLEAN,
                label="启用图片获取",
                default_value=bool(existing.get(ConfigKeys.ENABLE_IMAGES, True)),
                required=False,
            ),
        ],
        step_id="user",
    )
    await session.finish({
        ConfigKeys.API_URL: str(values[ConfigKeys.API_URL]).strip().rstrip("/"),
        ConfigKeys.ENABLE_ARTIST_METADATA: bool(values.get(ConfigKeys.ENABLE_ARTIST_METADATA, True)),
        ConfigKeys.ENABLE_ALBUM_METADATA: bool(values.get(ConfigKeys.ENABLE_ALBUM_METADATA, True)),
        ConfigKeys.ENABLE_TRACK_METADATA: bool(values.get(ConfigKeys.ENABLE_TRACK_METADATA, True)),
        ConfigKeys.ENABLE_IMAGES: bool(values.get(ConfigKeys.ENABLE_IMAGES, True)),
    })
