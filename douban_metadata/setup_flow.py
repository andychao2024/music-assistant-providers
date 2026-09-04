"""Setup flow for the Douban Metadata provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from . import ConfigKeys

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect metadata options."""
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=ConfigKeys.ENABLE_ARTIST_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用艺术家元数据",
                default_value=True,
                required=False,
                description="获取艺术家封面、简介、流派",
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_ALBUM_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用专辑元数据",
                default_value=True,
                required=False,
                description="获取专辑封面、流派、年份、简介",
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_TRACK_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="启用曲目元数据",
                default_value=True,
                required=False,
                description="获取曲目封面、流派",
            ),
            ConfigEntry(
                key=ConfigKeys.ENABLE_IMAGES,
                type=ConfigEntryType.BOOLEAN,
                label="启用封面下载",
                default_value=True,
                required=False,
                description="下载高清封面图片",
            ),
        ],
        step_id="user",
    )
    await session.finish({
        ConfigKeys.ENABLE_ARTIST_METADATA: bool(values.get(ConfigKeys.ENABLE_ARTIST_METADATA, True)),
        ConfigKeys.ENABLE_ALBUM_METADATA: bool(values.get(ConfigKeys.ENABLE_ALBUM_METADATA, True)),
        ConfigKeys.ENABLE_TRACK_METADATA: bool(values.get(ConfigKeys.ENABLE_TRACK_METADATA, True)),
        ConfigKeys.ENABLE_IMAGES: bool(values.get(ConfigKeys.ENABLE_IMAGES, True)),
    })
