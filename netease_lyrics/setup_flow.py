"""Setup flow for the NetEase Lyrics provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from . import (
    ConfigKeys,
    DEFAULT_BASE_URL,
    DEFAULT_LYRICS_DISPLAY_MODE,
    DEFAULT_LYRICS_OFFSET_MS,
    DEFAULT_UPDATE_EXISTING_LYRICS,
    LYRICS_DISPLAY_MODE_OPTIONS,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect lyrics API options."""
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=ConfigKeys.BASE_URL,
                type=ConfigEntryType.STRING,
                label="API 服务地址",
                description="自建云音乐 API 地址（示例：http://192.168.110.156:3003）",
                default_value=str(existing.get(ConfigKeys.BASE_URL) or DEFAULT_BASE_URL),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.UPDATE_EXISTING_LYRICS,
                type=ConfigEntryType.BOOLEAN,
                label="更新已有歌词",
                description="当歌曲已有歌词时，是否强制获取并更新为云音乐的歌词",
                default_value=bool(existing.get(ConfigKeys.UPDATE_EXISTING_LYRICS, DEFAULT_UPDATE_EXISTING_LYRICS)),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.LYRICS_OFFSET_MS,
                type=ConfigEntryType.INTEGER,
                label="歌词偏移（毫秒，正数延后/负数提前）",
                description="手动微调歌词时间：正数=歌词延后显示，负数=歌词提前显示",
                default_value=int(existing.get(ConfigKeys.LYRICS_OFFSET_MS, DEFAULT_LYRICS_OFFSET_MS)),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.LYRICS_DISPLAY_MODE,
                type=ConfigEntryType.STRING,
                label="歌词显示模式",
                description="选择双语歌词、仅原文歌词或仅翻译歌词",
                default_value=str(existing.get(ConfigKeys.LYRICS_DISPLAY_MODE, DEFAULT_LYRICS_DISPLAY_MODE)),
                options=LYRICS_DISPLAY_MODE_OPTIONS,
                required=False,
            ),
        ],
        step_id="user",
    )
    await session.finish({
        ConfigKeys.BASE_URL: str(values.get(ConfigKeys.BASE_URL, DEFAULT_BASE_URL)).strip(),
        ConfigKeys.UPDATE_EXISTING_LYRICS: bool(values.get(ConfigKeys.UPDATE_EXISTING_LYRICS, DEFAULT_UPDATE_EXISTING_LYRICS)),
        ConfigKeys.LYRICS_OFFSET_MS: int(values.get(ConfigKeys.LYRICS_OFFSET_MS, DEFAULT_LYRICS_OFFSET_MS)),
        ConfigKeys.LYRICS_DISPLAY_MODE: str(values.get(ConfigKeys.LYRICS_DISPLAY_MODE, DEFAULT_LYRICS_DISPLAY_MODE)).strip().lower(),
    })
