"""
Netease Cloud Music API Lyrics Metadata Provider for Music Assistant
云音乐API 歌词自动补全（支持毫秒级歌词同步）
Version 1.2.8 - 新增“更新已有歌词”开关
获取最新版本 https://gitee.com/andychao2020/music-assistant-providers
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientResponseError, ContentTypeError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
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

# 配置项常量
class ConfigKeys:
    """配置项键名常量类"""
    BASE_URL = "base_url"
    UPDATE_EXISTING_LYRICS = "update_existing_lyrics"  # 新增：是否更新已有歌词的开关
    LYRICS_OFFSET_MS = "lyrics_offset_ms"  # 新增：歌词时间偏移（毫秒）
    LYRICS_DISPLAY_MODE = "lyrics_display_mode"  # 新增：歌词显示模式

# 配置项默认值
DEFAULT_BASE_URL = "http://localhost:3003"
DEFAULT_UPDATE_EXISTING_LYRICS = False  # 默认不更新已有歌词
DEFAULT_LYRICS_OFFSET_MS = 0  # 默认不偏移
DEFAULT_LYRICS_DISPLAY_MODE = "bilingual"  # 默认双语歌词

LYRICS_DISPLAY_MODE_OPTIONS = [
    ConfigValueOption("双语歌词", "bilingual"),
    ConfigValueOption("仅原文歌词", "original"),
    ConfigValueOption("仅翻译歌词", "translation"),
]

LRC_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]")
NON_STANDARD_LRC_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\](?!\.)")
NORMALIZED_LRC_PATTERN = re.compile(r"^\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)$")


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
    """插件配置项（新增“更新已有歌词”开关）"""
    return (
        ConfigEntry(
            key=ConfigKeys.BASE_URL,
            type=ConfigEntryType.STRING,
            label="API 服务地址",
            description="自建云音乐 API 地址（示例：http://192.168.110.156:3003）",
            default_value=DEFAULT_BASE_URL,
            required=False,
        ),
        # 新增：是否更新已有歌词的开关配置
        ConfigEntry(
            key=ConfigKeys.UPDATE_EXISTING_LYRICS,
            type=ConfigEntryType.BOOLEAN,
            label="更新已有歌词",
            description="当歌曲已有歌词时，是否强制获取并更新为云音乐的歌词",
            default_value=DEFAULT_UPDATE_EXISTING_LYRICS,
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.LYRICS_OFFSET_MS,
            type=ConfigEntryType.INTEGER,
            label="歌词偏移（毫秒，正数延后/负数提前）",
            description="手动微调歌词时间：正数=歌词延后显示，负数=歌词提前显示",
            default_value=DEFAULT_LYRICS_OFFSET_MS,
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.LYRICS_DISPLAY_MODE,
            type=ConfigEntryType.STRING,
            label="歌词显示模式",
            description="选择双语歌词、仅原文歌词或仅翻译歌词",
            default_value=DEFAULT_LYRICS_DISPLAY_MODE,
            options=LYRICS_DISPLAY_MODE_OPTIONS,
            required=False,
        ),
    )


class NeteaseMusicProvider(MetadataProvider):
    """云音乐歌词插件：提供毫秒级同步歌词（新增“更新已有歌词”开关）"""

    # 新增：配置项属性
    base_url: str
    update_existing_lyrics: bool  # 是否更新已有歌词
    lyrics_offset_ms: int  # 歌词时间偏移（毫秒）
    lyrics_display_mode: str  # 歌词显示模式

    async def handle_async_init(self) -> None:
        """初始化插件（读取新增的开关配置）"""
        # 读取配置项
        self.base_url = self.config.get_value(ConfigKeys.BASE_URL, DEFAULT_BASE_URL)
        # 新增：读取“是否更新已有歌词”的开关
        self.update_existing_lyrics = self.config.get_value(
            ConfigKeys.UPDATE_EXISTING_LYRICS,
            DEFAULT_UPDATE_EXISTING_LYRICS
        )
        # 新增：读取歌词偏移和显示模式
        self.lyrics_offset_ms = int(
            self.config.get_value(
                ConfigKeys.LYRICS_OFFSET_MS,
                DEFAULT_LYRICS_OFFSET_MS
            )
        )
        self.lyrics_display_mode = str(
            self.config.get_value(
                ConfigKeys.LYRICS_DISPLAY_MODE,
                DEFAULT_LYRICS_DISPLAY_MODE
            )
        ).strip().lower()
        if self.lyrics_display_mode not in ("bilingual", "original", "translation"):
            self.lyrics_display_mode = DEFAULT_LYRICS_DISPLAY_MODE
        
        self.logger.debug(f"[云音乐歌词插件] 初始化 - 读取配置项 | base_url: {self.base_url} | update_existing_lyrics: {self.update_existing_lyrics} | lyrics_offset_ms: {self.lyrics_offset_ms} | lyrics_display_mode: {self.lyrics_display_mode}")
        
        # 拼接API地址
        self.search_api_url = f"{self.base_url}/search" if not self.base_url.endswith("/") else f"{self.base_url}search"
        self.lyric_api_url = f"{self.base_url}/lyric" if not self.base_url.endswith("/") else f"{self.base_url}lyric"
        self.logger.debug(f"[云音乐歌词插件] 初始化 - 拼接API地址 | 搜索接口: {self.search_api_url} | 歌词接口: {self.lyric_api_url}")
        
        # 设置限流参数
        self.rate_limit = 10 if self.base_url == DEFAULT_BASE_URL else 1
        self.period = 5 if self.base_url == DEFAULT_BASE_URL else 1
        self.throttler = ThrottlerManager(rate_limit=self.rate_limit, period=self.period)
        self.logger.debug(f"[云音乐歌词插件] 初始化 - 设置限流规则 | 速率限制: {self.rate_limit}次/{self.period}秒 | 原因: {'默认地址' if self.base_url == DEFAULT_BASE_URL else '自定义地址'}")
        
        # 初始化完成日志
        self.logger.info(
            f"[云音乐歌词插件 v1.2.8] 初始化完成 - API地址: {self.base_url} | 限流规则: {self.rate_limit}次/{self.period}秒 | 更新已有歌词: {'启用' if self.update_existing_lyrics else '禁用'} | 歌词偏移: {self.lyrics_offset_ms}ms | 显示模式: {self.lyrics_display_mode}"
        )
        self.logger.debug(f"[云音乐歌词插件] 初始化 - 插件实例ID: {self.instance_id} | 支持功能: {SUPPORTED_FEATURES}")

    def _normalize_lrc(self, lrc_content: str) -> str:
        """标准化歌词格式（增强日志）"""
        self.logger.debug(f"[云音乐歌词插件] 开始标准化歌词 | 原始歌词长度: {len(lrc_content)}字符 | 行数: {len(lrc_content.split('\n')) if lrc_content else 0}")
        
        if not lrc_content:
            self.logger.debug(f"[云音乐歌词插件] 标准化歌词 - 输入为空，返回空字符串")
            return ""
        
        normalized_lines = []
        skip_count = 0  # 统计跳过的行
        standard_count = 0  # 统计标准格式行
        converted_count = 0  # 统计转换的非标准行
        
        for line_num, line in enumerate(lrc_content.split("\n"), 1):
            line = line.strip()
            self.logger.debug(f"[云音乐歌词插件] 标准化歌词 - 处理第{line_num}行: {line[:50]}..." if len(line) > 50 else f"[云音乐歌词插件] 标准化歌词 - 处理第{line_num}行: {line}")
            
            # 跳过空行/标签行
            if not line or line.startswith(("[ti:", "[ar:", "[al:", "[au:", "[by:")):
                skip_count += 1
                self.logger.debug(f"[云音乐歌词插件] 标准化歌词 - 第{line_num}行: 跳过（空行/标签行）")
                continue
            
            # 标准毫秒级歌词
            if LRC_TIMESTAMP_PATTERN.match(line):
                normalized_lines.append(line)
                standard_count += 1
                self.logger.debug(f"[云音乐歌词插件] 标准化歌词 - 第{line_num}行: 保留（标准毫秒格式）")
            
            # 非标准歌词（无毫秒）转换
            elif NON_STANDARD_LRC_PATTERN.match(line):
                match = NON_STANDARD_LRC_PATTERN.search(line)
                if match:
                    minutes = match.group(1).zfill(2)
                    seconds = match.group(2).zfill(2)
                    lyric_content = NON_STANDARD_LRC_PATTERN.sub("", line).strip()
                    new_line = f"[{minutes}:{seconds}.000] {lyric_content}"
                    normalized_lines.append(new_line)
                    converted_count += 1
                    self.logger.debug(f"[云音乐歌词插件] 标准化歌词 - 第{line_num}行: 转换（非标准→标准）| 原行: {line} | 新行: {new_line}")
        
        # 标准化结果统计
        normalized_content = "\n".join(normalized_lines)
        self.logger.debug(
            f"[云音乐歌词插件] 标准化歌词完成 | 统计: 总行数={line_num} | 跳过={skip_count} | 标准格式={standard_count} | 转换={converted_count} | 输出长度={len(normalized_content)}字符"
        )
        return normalized_content

    def _parse_lrc(self, lrc_content: str) -> list[tuple[int, str]]:
        """解析标准化后的LRC歌词"""
        parsed_lines = []
        if not lrc_content:
            return parsed_lines

        for line in lrc_content.split("\n"):
            match = NORMALIZED_LRC_PATTERN.match(line.strip())
            if not match:
                continue

            minutes = int(match.group(1))
            seconds = int(match.group(2))
            milliseconds_raw = match.group(3)
            milliseconds = int(milliseconds_raw.ljust(3, "0"))
            lyric_content = match.group(4).strip()
            timestamp_ms = (minutes * 60 * 1000) + (seconds * 1000) + milliseconds
            parsed_lines.append((timestamp_ms, lyric_content))

        return parsed_lines

    def _build_lrc(self, parsed_lines: list[tuple[int, str]]) -> str:
        """将歌词时间轴重新构建为LRC文本"""
        lrc_lines = []
        for timestamp_ms, lyric_content in parsed_lines:
            timestamp_ms = max(0, timestamp_ms)
            minutes = timestamp_ms // 60000
            seconds = (timestamp_ms % 60000) // 1000
            milliseconds = timestamp_ms % 1000
            lrc_lines.append(f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}] {lyric_content}".rstrip())
        return "\n".join(lrc_lines)

    def _merge_lyrics(self, original_lrc: str | None, translated_lrc: str | None) -> str | None:
        """按配置合并原文歌词和翻译歌词"""
        original_lines = self._parse_lrc(original_lrc or "")
        translated_lines = self._parse_lrc(translated_lrc or "")

        if not original_lines and not translated_lines:
            return None

        if self.lyrics_display_mode == "original":
            merged_lines = original_lines or translated_lines
        elif self.lyrics_display_mode == "translation":
            merged_lines = translated_lines or original_lines
        else:
            original_map = {timestamp_ms: lyric for timestamp_ms, lyric in original_lines if lyric}
            translated_map = {timestamp_ms: lyric for timestamp_ms, lyric in translated_lines if lyric}
            merged_lines = []
            for timestamp_ms in sorted(set(original_map) | set(translated_map)):
                original_text = original_map.get(timestamp_ms, "")
                translated_text = translated_map.get(timestamp_ms, "")
                if original_text and translated_text and original_text != translated_text:
                    merged_text = f"{original_text} / {translated_text}"
                else:
                    merged_text = original_text or translated_text
                merged_lines.append((timestamp_ms, merged_text))

        if self.lyrics_offset_ms != 0:
            merged_lines = [
                (timestamp_ms + self.lyrics_offset_ms, lyric_content)
                for timestamp_ms, lyric_content in merged_lines
            ]

        return self._build_lrc(merged_lines)

    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _search_song_id(self, track_name: str, artist_name: str) -> str | None:
        """搜索歌曲ID（缓存14天，带限流）- 增强日志"""
        self.logger.debug(f"[云音乐歌词插件] 开始搜索歌曲ID | 歌曲名: {track_name} | 艺术家: {artist_name}")
        
        # 构建请求参数
        params = {"keywords": f"{track_name} {artist_name}", "type": 1, "limit": 10}
        self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 构建请求 | URL: {self.search_api_url} | 参数: {params}")

        try:
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 发起HTTP请求 | 限流状态: {self.rate_limit}次/{self.period}秒")
            async with self.mass.http_session.get(self.search_api_url, params=params) as response:
                self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 响应状态 | 状态码: {response.status} | 状态文本: {response.reason}")
                
                response.raise_for_status()
                if response.status == 204:
                    self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 响应204（无内容）| 歌曲: {track_name} | 艺术家: {artist_name}")
                    return None
                
                data = cast("dict[str, Any]", await response.json())
                self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 响应数据 | 原始JSON: {json.dumps(data)[:500]}...")
                
                if data.get("code") == 200 and data.get("result") and data["result"].get("songs"):
                    song_id = str(data["result"]["songs"][0]["id"])
                    self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID成功 | 歌曲: {track_name} | 艺术家: {artist_name} | 匹配ID: {song_id}")
                    return song_id
                else:
                    self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 响应无有效数据 | code: {data.get('code')} | result: {data.get('result')}")
            
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID失败 - 未匹配到歌曲 | 歌曲: {track_name} | 艺术家: {artist_name}")
            return None
        
        except ClientResponseError as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - HTTP错误 | 歌曲: {track_name} | 艺术家: {artist_name} | 状态码: {e.status} | 错误: {e.message}")
        except json.JSONDecodeError as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - JSON解析错误 | 歌曲: {track_name} | 艺术家: {artist_name} | 错误: {str(e)}")
        except ContentTypeError as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 内容类型错误 | 歌曲: {track_name} | 艺术家: {artist_name} | 错误: {str(e)}")
        except Exception as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索歌曲ID - 未知错误 | 歌曲: {track_name} | 艺术家: {artist_name} | 错误: {str(e)} | 类型: {type(e).__name__}")
        
        return None

    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _get_lyrics(self, song_id: str) -> tuple[str | None, str | None]:
        """获取同步歌词和翻译歌词（缓存14天，带限流）- 增强日志"""
        self.logger.debug(f"[云音乐歌词插件] 开始获取歌词 | 歌曲ID: {song_id}")
        
        params = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}
        self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 构建请求 | URL: {self.lyric_api_url} | 参数: {params}")

        try:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 发起HTTP请求 | 限流状态: {self.rate_limit}次/{self.period}秒")
            async with self.mass.http_session.get(self.lyric_api_url, params=params) as response:
                self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 响应状态 | 歌曲ID: {song_id} | 状态码: {response.status} | 状态文本: {response.reason}")
                
                response.raise_for_status()
                if response.status == 204:
                    self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 响应204（无内容）| 歌曲ID: {song_id}")
                    return None, None
                
                data = cast("dict[str, Any]", await response.json())
                self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 响应数据 | 歌曲ID: {song_id} | 原始JSON: {json.dumps(data)[:500]}...")
                
                synced_lyrics = data.get("lrc", {}).get("lyric", "")
                translated_lyrics = data.get("tlyric", {}).get("lyric", "")
                self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 原始歌词 | 歌曲ID: {song_id} | 原文长度: {len(synced_lyrics)}字符 | 翻译长度: {len(translated_lyrics)}字符")
                
                normalized_lyrics = self._normalize_lrc(synced_lyrics) if synced_lyrics else None
                normalized_translated_lyrics = self._normalize_lrc(translated_lyrics) if translated_lyrics else None
                if normalized_lyrics or normalized_translated_lyrics:
                    self.logger.debug(f"[云音乐歌词插件] 获取歌词成功 | 歌曲ID: {song_id} | 原文有效: {bool(normalized_lyrics)} | 翻译有效: {bool(normalized_translated_lyrics)}")
                else:
                    self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 标准化后为空 | 歌曲ID: {song_id}")
                
                return normalized_lyrics, normalized_translated_lyrics
        
        except ClientResponseError as e:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词 - HTTP错误 | 歌曲ID: {song_id} | 状态码: {e.status} | 错误: {e.message}")
        except json.JSONDecodeError as e:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词 - JSON解析错误 | 歌曲ID: {song_id} | 错误: {str(e)}")
        except ContentTypeError as e:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 内容类型错误 | 歌曲ID: {song_id} | 错误: {str(e)}")
        except Exception as e:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词 - 未知错误 | 歌曲ID: {song_id} | 错误: {str(e)} | 类型: {type(e).__name__}")
        
        return None, None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """获取歌曲歌词元数据（新增“更新已有歌词”开关逻辑）"""
        self.logger.debug(
            f"[云音乐歌词插件] 开始处理歌曲 | 歌曲ID: {track.item_id} | 歌曲名: {track.name} | 艺术家: {[a.name for a in track.artists] if track.artists else '无'}"
        )
        
        # 新增：检查已有歌词 + 开关逻辑
        has_lyrics = track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics)
        if has_lyrics:
            if not self.update_existing_lyrics:
                self.logger.debug(f"[云音乐歌词插件] 跳过处理 - 歌曲已有歌词（更新开关已禁用）| 歌曲: {track.name}")
                return None
            else:
                self.logger.debug(f"[云音乐歌词插件] 强制处理 - 歌曲已有歌词（更新开关已启用）| 歌曲: {track.name}")
        
        # 校验必要信息
        if not track.artists or not track.duration:
            self.logger.debug(
                f"[云音乐歌词插件] 跳过处理 - 歌曲信息不全 | 歌曲: {track.name} | 有艺术家: {bool(track.artists)} | 有时长: {bool(track.duration)}"
            )
            return None

        # 清理歌曲名和艺术家名
        artist_name = track.artists[0].name
        raw_track_name = track.name
        track_name = re.sub(r"\(.*?\)|\[.*?\]|-.*?$", "", track.name).strip()
        self.logger.debug(f"[云音乐歌词插件] 清理歌曲名 | 原始名称: {raw_track_name} | 清理后: {track_name} | 艺术家: {artist_name}")

        # 搜索歌曲ID
        self.logger.debug(f"[云音乐歌词插件] 调用搜索方法 | 目标: 获取歌曲ID | 歌曲: {track_name} | 艺术家: {artist_name}")
        song_id = await self._search_song_id(track_name, artist_name)
        if not song_id:
            self.logger.debug(f"[云音乐歌词插件] 处理终止 - 未找到歌曲ID | 歌曲: {track_name} | 艺术家: {artist_name}")
            return None
        self.logger.debug(f"[云音乐歌词插件] 搜索结果 | 歌曲ID: {song_id} | 歌曲: {track_name} | 艺术家: {artist_name}")
        
        # 获取标准化歌词
        self.logger.debug(f"[云音乐歌词插件] 调用歌词方法 | 目标: 获取标准化歌词 | 歌曲ID: {song_id}")
        lyrics_result = await self._get_lyrics(song_id)
        if isinstance(lyrics_result, tuple | list):
            if len(lyrics_result) >= 2:
                original_lyrics, translated_lyrics = lyrics_result[0], lyrics_result[1]
            elif len(lyrics_result) == 1:
                original_lyrics, translated_lyrics = lyrics_result[0], None
            else:
                original_lyrics, translated_lyrics = None, None
        else:
            original_lyrics, translated_lyrics = lyrics_result, None
        synced_lyrics = self._merge_lyrics(original_lyrics, translated_lyrics)
        if not synced_lyrics:
            self.logger.debug(f"[云音乐歌词插件] 处理终止 - 未找到歌曲歌词 | 歌曲ID: {song_id} | 歌曲: {track_name}")
            return None
        self.logger.debug(f"[云音乐歌词插件] 歌词获取结果 | 歌曲ID: {song_id} | 显示模式: {self.lyrics_display_mode} | 偏移: {self.lyrics_offset_ms}ms | 歌词长度: {len(synced_lyrics)}字符")

        # 构建元数据对象
        self.logger.debug(f"[云音乐歌词插件] 开始构建歌词元数据 | 歌曲: {track.name} | 提供者: {track.provider} | 歌曲ID: {track.item_id}")
        metadata = MediaItemMetadata()
        metadata.lrc_lyrics = synced_lyrics
        metadata.lyrics = synced_lyrics  # 兼容原生数据库的lyrics字段
        self.logger.debug(f"[云音乐歌词插件] 更新歌词元数据 | 歌曲: {track.name} | 歌词长度: {len(synced_lyrics)}")
        self.logger.debug(f"[云音乐歌词插件] 构建元数据 | lrc_lyrics: {bool(metadata.lrc_lyrics)} | lyrics: {bool(metadata.lyrics)}")
        self.logger.debug(f"[云音乐歌词插件] 返回歌词元数据 | 歌曲: {track.name} | 提供者: {track.provider}")

        # 尝试写入原生数据库/元数据缓存，兼容不同版本的 Music Assistant
        try:
            self.logger.debug(f"[云音乐歌词插件] 开始写入数据库 | 歌曲: {track.name} | 提供者: {track.provider} | 歌曲ID: {track.item_id}")

            if not track.metadata:
                track.metadata = MediaItemMetadata()
                self.logger.debug(f"[云音乐歌词插件] 初始化元数据对象 | 歌曲: {track.name}")

            track.metadata.lrc_lyrics = synced_lyrics
            track.metadata.lyrics = synced_lyrics
            self.logger.debug(f"[云音乐歌词插件] 更新track元数据 | 歌曲: {track.name} | 歌词长度: {len(track.metadata.lrc_lyrics)}")

            # 区分本地库/第三方平台写入
            if track.provider == "library":
                self.logger.debug(f"[云音乐歌词插件] 写入本地库 | 歌曲ID: {track.item_id}")
                await self.mass.music.tracks.update_item_in_library(track.item_id, track)
                self.logger.debug(f"[云音乐歌词插件] 本地库写入成功 | 歌曲: {track.name}")
            else:
                save_item_metadata = getattr(self.mass.metadata, "save_item_metadata", None)
                if save_item_metadata:
                    self.logger.debug(f"[云音乐歌词插件] 写入元数据缓存 | 歌曲: {track.name} | 提供者: {track.provider}")
                    await save_item_metadata(track)
                    self.logger.debug(f"[云音乐歌词插件] 元数据缓存写入成功 | 歌曲: {track.name}")
                else:
                    self.logger.debug(f"[云音乐歌词插件] 跳过元数据缓存写入 | 歌曲: {track.name} | 原因: 当前MA版本未提供save_item_metadata接口")
        except Exception as e:
            self.logger.error(f"[云音乐歌词插件] 歌词写入数据库失败 | 歌曲: {track.name} | 错误: {str(e)}", exc_info=True)
        
        self.logger.info(f"[云音乐歌词插件] 歌词处理完成 | 歌曲: {track.name} | 状态: 成功获取歌词")

        return metadata
