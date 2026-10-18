"""Setup flow for the LX Music provider."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_DEFAULT_SOURCE,
    CONF_PASSWORD,
    CONF_SEARCH_SOURCES,
    CONF_SERVER_URL,
    CONF_USERNAME,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

_LOGGER = logging.getLogger(__name__)


async def run_setup(session: SetupSession) -> None:
    """Collect server URL, credentials and source options."""
    existing = session.context.setup_data or {}
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SERVER_URL,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(existing.get(CONF_SERVER_URL) or "http://localhost:9527"),
                description="Full URL of the LX Music server, e.g. http://localhost:9527",
            ),
            ConfigEntry(
                key=CONF_USERNAME,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=str(existing.get(CONF_USERNAME) or "admin"),
            ),
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                label="Password",
                required=True,
                default_value=str(existing.get(CONF_PASSWORD) or ""),
                description=(
                    "Login password for the lxserver account (required). "
                    "Forgot it or never set one? Reset it in the lxserver "
                    "admin panel (usually the settings / account page of "
                    "the LX server web UI), then come back."
                ),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_SOURCE,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=str(existing.get(CONF_DEFAULT_SOURCE) or "wy"),
                description="Source preferred when resolving stream URLs (kw/kg/tx/wy/mg)",
            ),
            ConfigEntry(
                key=CONF_SEARCH_SOURCES,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=str(existing.get(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"),
                description="Comma-separated list of sources polled during search",
            ),
        ],
        step_id="user",
    )
    # 诊断日志：2026-09-04 用户报告填了密码仍报"密码为空"，需要确认 MA 前端
    # 是否真的把 password 字段传回。values[CONF_PASSWORD] 应该是个非空字符串；
    # 如果是 None/空，说明前端 SECURE_STRING 渲染或 form 提交时丢了字段。
    # 注意只记长度不记明文：密码会永久留在日志里（2026-10-10 代码审查意见）。
    _LOGGER.warning(
        "lxmusic setup_flow DIAG: values keys=%s | server_url=%r | username=%r | "
        "password_present=%s | password_type=%s | password_len=%s | "
        "default_source=%r | search_sources=%r",
        sorted(values.keys()),
        values.get(CONF_SERVER_URL),
        values.get(CONF_USERNAME),
        CONF_PASSWORD in values,
        type(values.get(CONF_PASSWORD)).__name__,
        len(str(values.get(CONF_PASSWORD) or "")),
        values.get(CONF_DEFAULT_SOURCE),
        values.get(CONF_SEARCH_SOURCES),
    )
    try:
        await session.finish({
            CONF_SERVER_URL: str(values[CONF_SERVER_URL]).strip().rstrip("/"),
            CONF_USERNAME: str(values[CONF_USERNAME]).strip(),
            CONF_PASSWORD: str(values.get(CONF_PASSWORD) or ""),
            CONF_DEFAULT_SOURCE: str(values.get(CONF_DEFAULT_SOURCE) or "wy").strip(),
            CONF_SEARCH_SOURCES: str(values.get(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg").strip(),
        })
    except SetupFlowError as err:
        # 2026-09-04 关键诊断:MA 框架的 _run_flow (controllers/config/flows.py) 会把
        # setup flow 抛出的所有异常完全吞掉,只 session.publish_abort() 给前端,
        # **不写任何 LOGGER**。如果不这里捕获,日志里看不到任何错误,settings.json
        # 里也没 lxmusic 条目,就完全无法定位根因。
        _LOGGER.exception(
            "lxmusic setup_flow finish failed: %s | translation_key=%s | "
            "values=%r",
            err,
            getattr(err, "translation_key", None),
            {
                CONF_SERVER_URL: values.get(CONF_SERVER_URL),
                CONF_USERNAME: values.get(CONF_USERNAME),
                "password_present": bool(values.get(CONF_PASSWORD)),
            },
        )
        raise
