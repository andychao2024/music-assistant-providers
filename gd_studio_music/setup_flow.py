"""Setup flow for the GD Studio Music provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from . import CONF_AUDIO_QUALITY, CONF_DEFAULT_SOURCE, CONF_IMAGE_SIZE, STABLE_SOURCES

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect source, audio quality and image size options."""
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_DEFAULT_SOURCE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(existing.get(CONF_DEFAULT_SOURCE) or "joox"),
                options=[ConfigValueOption(title=name, value=value) for name, value in STABLE_SOURCES],
            ),
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(existing.get(CONF_AUDIO_QUALITY) or "exhigh"),
                options=(
                    ConfigValueOption(title="标准 (128k)", value="standard"),
                    ConfigValueOption(title="较高 (192k)", value="higher"),
                    ConfigValueOption(title="极高 (320k)", value="exhigh"),
                    ConfigValueOption(title="无损 (740k)", value="lossless"),
                    ConfigValueOption(title="Hi-Res (999k)", value="hires"),
                ),
            ),
            ConfigEntry(
                key=CONF_IMAGE_SIZE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(existing.get(CONF_IMAGE_SIZE) or "300"),
                options=[
                    ConfigValueOption(title="小图 (300px)", value="300"),
                    ConfigValueOption(title="大图 (500px)", value="500"),
                ],
            ),
        ],
        step_id="user",
    )
    await session.finish({
        CONF_DEFAULT_SOURCE: str(values[CONF_DEFAULT_SOURCE]),
        CONF_AUDIO_QUALITY: str(values[CONF_AUDIO_QUALITY]),
        CONF_IMAGE_SIZE: str(values[CONF_IMAGE_SIZE]),
    })
