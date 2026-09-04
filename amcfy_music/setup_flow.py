"""Setup flow for the Amcfy Music Subsonic Bridge."""
from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from . import CONF_SEARCH_SCOPE, CONF_TOKEN

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect token and search scope options."""
    existing = session.context.setup_data or {}
    default_token = str(existing.get(CONF_TOKEN) or secrets.token_hex(16))

    values = await session.form(
        [
            ConfigEntry(
                key=CONF_TOKEN,
                type=ConfigEntryType.STRING,
                label="API Token",
                description="Token for Subsonic auth (p=xxx or t=md5(token+salt)&s=salt)。已自动生成随机 Token，可直接使用或修改。",
                required=True,
                value=default_token,
            ),
            ConfigEntry(
                key=CONF_SEARCH_SCOPE,
                type=ConfigEntryType.STRING,
                label="Search Scope",
                description="搜索范围: 本地曲库(Library only) / 全部曲库(All sources including online)",
                default_value=str(existing.get(CONF_SEARCH_SCOPE) or "library"),
                required=True,
                options=[
                    ConfigValueOption(value="library", title="本地曲库 (Library only)"),
                    ConfigValueOption(value="all", title="全部曲库 (All sources including online)"),
                ],
            ),
        ],
        step_id="user",
    )
    await session.finish({
        CONF_TOKEN: str(values[CONF_TOKEN]).strip(),
        CONF_SEARCH_SCOPE: str(values.get(CONF_SEARCH_SCOPE, "library")),
    })
