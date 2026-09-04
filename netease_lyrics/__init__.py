"""
Netease Cloud Music API Lyrics Metadata Provider for Music Assistant
网易云音乐 API 歌词自动补全（支持毫秒级歌词同步）
Version 1.5.0 - 优化匹配准确率、识别率与歌词滚动；双语歌词恢复 1.3.0 精确时间戳配对方式
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

PLUGIN_VERSION = "1.5.0"

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}


# 配置项常量
class ConfigKeys:
    """配置项键名常量类"""

    BASE_URL = "base_url"
    UPDATE_EXISTING_LYRICS = "update_existing_lyrics"
    LYRICS_OFFSET_MS = "lyrics_offset_ms"
    LYRICS_DISPLAY_MODE = "lyrics_display_mode"


# 配置项默认值
DEFAULT_BASE_URL = "http://localhost:3003"
DEFAULT_UPDATE_EXISTING_LYRICS = False
DEFAULT_LYRICS_OFFSET_MS = 0
DEFAULT_LYRICS_DISPLAY_MODE = "bilingual"

# 最低匹配分：低于该分视为未匹配。
# 调用自建网易云 API，宜适当放宽（原阈值0.3），优先保证识别率
MIN_MATCH_SCORE = 0.3

# 歌词候选数量：只取匹配分最高的前几个，逐个尝试，优先选带时间戳的同步歌词
CANDIDATE_LIMIT = 6

LYRICS_DISPLAY_MODE_OPTIONS = [
    ConfigValueOption("双语歌词", "bilingual"),
    ConfigValueOption("仅原文歌词", "original"),
    ConfigValueOption("仅翻译歌词", "translation"),
]

LRC_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]")
NON_STANDARD_LRC_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\](?!\.)")
NORMALIZED_LRC_PATTERN = re.compile(r"^\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)$")

# 搜索关键词清洗：去掉括号/收录/版本标注与 feat 信息
_CLEAN_BRACKET_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_CLEAN_FEAT_RE = re.compile(r"\b(feat\.?|featuring|ft\.?|with)\b.*$", re.I)
_CLEAN_VERSION_RE = re.compile(r"\([^)]*(live|remix|cover|伴奏|原版|inst|instrumental)[^)]*\)", re.I)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """初始化插件实例"""
    return NeteaseMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseMusicProvider(MetadataProvider):
    """云音乐歌词插件：提供毫秒级同步歌词"""

    # 配置项属性
    base_url: str
    update_existing_lyrics: bool
    lyrics_offset_ms: int
    lyrics_display_mode: str

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        return (
            ConfigEntry(
                key=ConfigKeys.BASE_URL,
                type=ConfigEntryType.STRING,
                label="API 服务地址",
                description="自建云音乐 API 地址（示例：http://192.168.110.156:3003）",
                default_value=str(self.get_setup_value(ConfigKeys.BASE_URL) or DEFAULT_BASE_URL),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.UPDATE_EXISTING_LYRICS,
                type=ConfigEntryType.BOOLEAN,
                label="更新已有歌词",
                description="当歌曲已有歌词时，是否强制获取并更新为云音乐的歌词",
                default_value=bool(
                    self.get_setup_value(
                        ConfigKeys.UPDATE_EXISTING_LYRICS, DEFAULT_UPDATE_EXISTING_LYRICS
                    )
                ),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.LYRICS_OFFSET_MS,
                type=ConfigEntryType.INTEGER,
                label="歌词偏移（毫秒，正数延后/负数提前）",
                description="手动微调歌词时间：正数=歌词延后显示，负数=歌词提前显示",
                default_value=int(
                    self.get_setup_value(
                        ConfigKeys.LYRICS_OFFSET_MS, DEFAULT_LYRICS_OFFSET_MS
                    )
                ),
                required=False,
            ),
            ConfigEntry(
                key=ConfigKeys.LYRICS_DISPLAY_MODE,
                type=ConfigEntryType.STRING,
                label="歌词显示模式",
                description="选择双语歌词、仅原文歌词或仅翻译歌词",
                default_value=str(
                    self.get_setup_value(
                        ConfigKeys.LYRICS_DISPLAY_MODE, DEFAULT_LYRICS_DISPLAY_MODE
                    )
                ),
                options=LYRICS_DISPLAY_MODE_OPTIONS,
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """初始化插件（读取配置项）"""
        self.base_url = self.get_setup_value(ConfigKeys.BASE_URL) or DEFAULT_BASE_URL
        self.update_existing_lyrics = bool(
            self.get_setup_value(
                ConfigKeys.UPDATE_EXISTING_LYRICS, DEFAULT_UPDATE_EXISTING_LYRICS
            )
        )
        self.lyrics_offset_ms = int(
            self.get_setup_value(ConfigKeys.LYRICS_OFFSET_MS, DEFAULT_LYRICS_OFFSET_MS)
        )
        self.lyrics_display_mode = str(
            self.get_setup_value(
                ConfigKeys.LYRICS_DISPLAY_MODE, DEFAULT_LYRICS_DISPLAY_MODE
            )
        ).strip().lower()
        if self.lyrics_display_mode not in ("bilingual", "original", "translation"):
            self.lyrics_display_mode = DEFAULT_LYRICS_DISPLAY_MODE

        # 拼接 API 地址
        base = self.base_url.rstrip("/")
        self.search_api_url = f"{base}/cloudsearch"
        self.lyric_api_url = f"{base}/lyric"

        # 限流参数（本地 API 服务器，提高并发）
        self.rate_limit = 50 if self.base_url == DEFAULT_BASE_URL else 5
        self.period = 5 if self.base_url == DEFAULT_BASE_URL else 1
        self.throttler = ThrottlerManager(rate_limit=self.rate_limit, period=self.period)

        self.logger.info(
            f"[云音乐歌词插件 v{PLUGIN_VERSION}] 初始化完成 | API: {self.base_url} | "
            f"限流: {self.rate_limit}次/{self.period}秒 | 更新已有歌词: "
            f"{'启用' if self.update_existing_lyrics else '禁用'} | 偏移: {self.lyrics_offset_ms}ms | "
            f"显示模式: {self.lyrics_display_mode}"
        )

    # ------------------------------------------------------------------
    # 名称清洗与匹配
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_search_name(name: str) -> str:
        """清洗歌名用于搜索关键词：去括号/版本/feat 标注，保留核心词"""
        name = _CLEAN_BRACKET_RE.sub(" ", name)
        name = _CLEAN_FEAT_RE.sub(" ", name)
        name = re.sub(r"[-—–]+", " ", name)
        name = re.sub(r"[^\w\u4e00-\u9fff]+", " ", name)
        return re.sub(r"\s+", " ", name).strip()

    @staticmethod
    def _normalize_name(name: str) -> str:
        """归一化名称用于严格比较：小写、去括号/feat/标点"""
        name = name.lower().strip()
        name = _CLEAN_BRACKET_RE.sub("", name)
        name = _CLEAN_FEAT_RE.sub("", name)
        return re.sub(r"[^\w]+", "", name)

    def _match_score(self, song: dict[str, Any], track_name: str, artists: list[str]) -> float:
        """对单个搜索结果打分：标题匹配 + 艺术家匹配"""
        song_name = self._normalize_name(song.get("name", ""))
        song_artists = [self._normalize_name(a.get("name", "")) for a in song.get("ar") or []]
        q_name = self._normalize_name(track_name)

        title_score = 0.0
        if song_name and song_name == q_name:
            title_score = 1.0
        elif song_name and q_name:
            longer, shorter = max(len(q_name), len(song_name)), min(len(q_name), len(song_name))
            if shorter / max(longer, 1) >= 0.5:
                if q_name in song_name or song_name in q_name:
                    title_score = 0.7 * (shorter / max(longer, 1))

        query_artists = [self._normalize_name(a) for a in artists if a]
        if not query_artists:
            return title_score

        matched = 0
        for qa in query_artists:
            for sa in song_artists:
                if qa and sa and (qa == sa or (len(qa) > 1 and len(sa) > 1 and (qa in sa or sa in qa))):
                    matched += 1
                    break
        artist_score = matched / len(query_artists)

        if title_score >= 0.7:
            if artist_score >= 0.5:
                # 标题高度匹配且歌手吻合时，以标题为主，艺术家作为兜底
                return max(title_score, artist_score * 0.7 + title_score * 0.3)
            # 标题高度匹配但歌手对不上：降权，避免把同名翻唱/不同艺人版本误认为原唱
            return max(title_score * 0.5, artist_score * 0.8 + title_score * 0.2)
        # 标题匹配一般时，艺术家权重高一点，但保留标题的贡献
        return max(artist_score * 0.8 + title_score * 0.2, artist_score, title_score)

    # ------------------------------------------------------------------
    # 网络请求（均带缓存与限流）
    # ------------------------------------------------------------------
    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _search_api(self, keywords: str) -> list[dict[str, Any]]:
        """调用云音乐搜索接口（缓存14天）"""
        self.logger.debug(f"[云音乐歌词插件] 搜索接口 | 关键词: {keywords}")
        params = {"keywords": keywords, "type": 1, "limit": 15}
        try:
            async with self.mass.http_session.get(self.search_api_url, params=params) as response:
                response.raise_for_status()
                if response.status == 204:
                    return []
                data = cast("dict[str, Any]", await response.json())
                if data.get("code") == 200 and data.get("result") and data["result"].get("songs"):
                    return data["result"]["songs"]
        except (ClientResponseError, json.JSONDecodeError, ContentTypeError) as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索接口失败 | 关键词: {keywords} | 错误: {e}")
        except Exception as e:
            self.logger.debug(f"[云音乐歌词插件] 搜索接口异常 | 关键词: {keywords} | 错误: {e}")
        return []

    @use_cache(3600 * 24 * 14)
    async def _search_candidate_ids(self, track_name: str, artists: list[str]) -> list[str]:
        """搜索歌曲ID候选列表（匹配分数降序，去重，缓存14天）"""
        name = self._clean_search_name(track_name)
        artist_str = " ".join(a for a in artists if a)

        # 依次尝试：歌名+艺术家 -> 仅歌名 -> 仅艺术家
        strategies = []
        if name:
            if artist_str:
                strategies.append(f"{name} {artist_str}")
            strategies.append(name)
        elif artist_str:
            strategies.append(artist_str)

        candidates: list[tuple[float, str]] = []
        seen: set[str] = set()
        for keywords in strategies:
            songs = await self._search_api(keywords)
            if not songs:
                continue
            scored = sorted(
                ((self._match_score(s, track_name, artists), str(s["id"])) for s in songs if s.get("id")),
                key=lambda x: x[0],
                reverse=True,
            )
            for score, song_id in scored:
                if score < MIN_MATCH_SCORE or song_id in seen:
                    continue
                seen.add(song_id)
                candidates.append((score, song_id))
        candidates = candidates[:CANDIDATE_LIMIT]

        if candidates:
            self.logger.debug(
                f"[云音乐歌词插件] 匹配候选 | 歌曲: {track_name} | 候选数: {len(candidates)} | "
                f"最高分: {candidates[0][0]:.2f}"
            )
        else:
            self.logger.debug(f"[云音乐歌词插件] 搜索未匹配 | 歌曲: {track_name} | 艺术家: {artists}")
        return [song_id for _, song_id in candidates]

    @use_cache(3600 * 24 * 14)
    @throttle_with_retries
    async def _get_lyrics_v2(self, song_id: str) -> tuple[str, str]:
        """获取同步歌词和翻译歌词（缓存14天，带限流）。

        始终返回两个字符串（无值用空串），避免 MA use_cache 还原时丢弃
        None 元素导致解包失败（not enough values to unpack）。
        """
        params = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}
        try:
            async with self.mass.http_session.get(self.lyric_api_url, params=params) as response:
                response.raise_for_status()
                if response.status == 204:
                    return "", ""

                data = cast("dict[str, Any]", await response.json())
                synced_lyrics = data.get("lrc", {}).get("lyric", "") or ""
                translated_lyrics = data.get("tlyric", {}).get("lyric", "") or ""

                normalized_lyrics = self._normalize_lrc(synced_lyrics)
                normalized_translated = self._normalize_lrc(translated_lyrics)

                if normalized_lyrics or normalized_translated:
                    self.logger.debug(
                        f"[云音乐歌词插件] 获取歌词成功 | 歌曲ID: {song_id} | "
                        f"原文有效: {bool(normalized_lyrics)} | 翻译有效: {bool(normalized_translated)}"
                    )
                    return normalized_lyrics, normalized_translated
                if synced_lyrics:
                    # 纯文本歌词（无时间戳）
                    self.logger.debug(f"[云音乐歌词插件] 纯文本歌词（无时间戳）| 歌曲ID: {song_id}")
                    return synced_lyrics, ""
                self.logger.debug(f"[云音乐歌词插件] 标准化后为空 | 歌曲ID: {song_id}")
        except (ClientResponseError, json.JSONDecodeError, ContentTypeError) as e:
            self.logger.debug(f"[云音乐歌词插件] 获取歌词失败 | 歌曲ID: {song_id} | 错误: {e}")
        except Exception as e:
            self.logger.debug(
                f"[云音乐歌词插件] 获取歌词异常 | 歌曲ID: {song_id} | 错误: {e} | 类型: {type(e).__name__}"
            )
        return "", ""

    # ------------------------------------------------------------------
    # LRC 处理
    # ------------------------------------------------------------------
    def _normalize_lrc(self, lrc_content: str) -> str:
        """标准化歌词格式：过滤元信息头与空行"""
        if not lrc_content:
            return ""

        normalized_lines = []
        for line in lrc_content.split("\n"):
            line = line.strip()
            if not line or line.startswith(("[ti:", "[ar:", "[al:", "[au:", "[by:")):
                continue
            if LRC_TIMESTAMP_PATTERN.match(line) or NON_STANDARD_LRC_PATTERN.match(line):
                normalized_lines.append(line)

        return "\n".join(normalized_lines)

    def _parse_lrc(self, lrc_content: str) -> list[tuple[int, str]]:
        """解析标准化后的LRC歌词，跳过空行"""
        parsed_lines = []
        if not lrc_content:
            return parsed_lines

        for line in lrc_content.split("\n"):
            match = NORMALIZED_LRC_PATTERN.match(line.strip())
            if not match:
                continue

            minutes = int(match.group(1))
            seconds = int(match.group(2))
            milliseconds = int(match.group(3).ljust(3, "0"))
            lyric_content = match.group(4).strip()
            if not lyric_content:
                continue
            timestamp_ms = (minutes * 60 * 1000) + (seconds * 1000) + milliseconds
            parsed_lines.append((timestamp_ms, lyric_content))

        return parsed_lines

    def _build_lrc(self, parsed_lines: list[tuple[int, str]]) -> str:
        """将歌词时间轴重建为LRC文本。

        强制按时间戳升序排序并去重，确保前端歌词可以正常滚动。
        """
        seen: set[tuple[int, str]] = set()
        ordered = []
        for timestamp_ms, lyric_content in sorted(parsed_lines, key=lambda x: (x[0], x[1])):
            timestamp_ms = max(0, timestamp_ms)
            key = (timestamp_ms, lyric_content)
            if key in seen:
                continue
            seen.add(key)
            ordered.append((timestamp_ms, lyric_content))

        lrc_lines = []
        for timestamp_ms, lyric_content in ordered:
            minutes = timestamp_ms // 60000
            seconds = (timestamp_ms % 60000) // 1000
            milliseconds = timestamp_ms % 1000
            lrc_lines.append(f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}] {lyric_content}".rstrip())
        return "\n".join(lrc_lines)

    def _merge_lyrics(self, original_lrc: str | None, translated_lrc: str | None) -> str | None:
        """按配置合并原文歌词和翻译歌词（双语模式: 原文 / 翻译 拼接为一行）"""
        original_lines = self._parse_lrc(original_lrc or "")
        translated_lines = self._parse_lrc(translated_lrc or "")

        if not original_lines and not translated_lines:
            return None

        if self.lyrics_display_mode == "original":
            merged_lines = original_lines or translated_lines
        elif self.lyrics_display_mode == "translation":
            merged_lines = translated_lines or original_lines
        else:
            original_map = {ts: text for ts, text in original_lines if text}
            translated_map = {ts: text for ts, text in translated_lines if text}
            merged_lines = []
            for timestamp_ms in sorted(set(original_map) | set(translated_map)):
                original_text = original_map.get(timestamp_ms, "")
                translated_text = translated_map.get(timestamp_ms, "")
                if original_text and translated_text and original_text != translated_text:
                    merged_lines.append((timestamp_ms, f"{original_text} / {translated_text}"))
                else:
                    merged_lines.append((timestamp_ms, original_text or translated_text))

        if self.lyrics_offset_ms != 0:
            merged_lines = [
                (max(0, timestamp_ms + self.lyrics_offset_ms), lyric_content)
                for timestamp_ms, lyric_content in merged_lines
            ]

        return self._build_lrc(merged_lines)

    @staticmethod
    def _build_plain_lyrics(lyrics: str) -> MediaItemMetadata:
        """构建纯文本（无时间戳）歌词元数据"""
        metadata = MediaItemMetadata()
        metadata.lyrics = lyrics
        return metadata

    # ------------------------------------------------------------------
    # 元数据落地
    # ------------------------------------------------------------------
    def _save_metadata_to_db(self, track: Track, metadata: MediaItemMetadata, track_name: str) -> None:
        """将歌词写入数据库/元数据缓存"""
        try:
            if not track.metadata:
                track.metadata = MediaItemMetadata()
            if metadata.lrc_lyrics:
                track.metadata.lrc_lyrics = metadata.lrc_lyrics
            if metadata.lyrics:
                track.metadata.lyrics = metadata.lyrics

            if track.provider == "library":
                self.mass.create_task(self.mass.music.tracks.update_item_in_library(track.item_id, track))
            else:
                save_item_metadata = getattr(self.mass.metadata, "save_item_metadata", None)
                if save_item_metadata:
                    self.mass.create_task(save_item_metadata(track))
        except Exception as e:
            self.logger.error(f"[云音乐歌词插件] 歌词写入数据库失败 | 歌曲: {track_name} | 错误: {str(e)}")

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """获取歌曲歌词元数据"""
        self.logger.debug(
            f"[云音乐歌词插件] 处理歌曲 | ID: {track.item_id} | 歌曲: {track.name} | "
            f"艺术家: {[a.name for a in track.artists] if track.artists else '无'}"
        )

        has_lyrics = track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics)
        if has_lyrics and not self.update_existing_lyrics:
            self.logger.debug(f"[云音乐歌词插件] 跳过 | 已有歌词且更新开关关闭 | 歌曲: {track.name}")
            return None

        if not track.name:
            self.logger.debug("[云音乐歌词插件] 跳过 | 歌曲名为空")
            return None

        artists = [a.name for a in track.artists if a.name] if track.artists else []
        song_ids = await self._search_candidate_ids(track.name, artists)
        if not song_ids:
            return None

        plain_fallback: str | None = None
        for song_id in song_ids:
            lyrics = await self._get_lyrics_v2(song_id)
            original_lyrics = lyrics[0] if lyrics else ""
            translated_lyrics = lyrics[1] if lyrics and len(lyrics) > 1 else ""

            if not original_lyrics and not translated_lyrics:
                continue

            synced_lyrics = self._merge_lyrics(original_lyrics, translated_lyrics)
            if synced_lyrics:
                metadata = MediaItemMetadata()
                metadata.lrc_lyrics = synced_lyrics
                metadata.lyrics = synced_lyrics  # 兼容原生数据库的lyrics字段
                self._save_metadata_to_db(track, metadata, track.name)
                self.logger.info(
                    f"[云音乐歌词插件] 歌词处理完成 | 歌曲: {track.name} | 歌词长度: {len(synced_lyrics)}"
                )
                return metadata

            if not plain_fallback and original_lyrics:
                plain_fallback = original_lyrics

        # 所有候选都没有同步歌词时，退而求其次用纯文本歌词
        if plain_fallback:
            self.logger.debug(f"[云音乐歌词插件] 纯文本歌词 | 歌曲: {track.name}")
            metadata = self._build_plain_lyrics(plain_fallback)
            self._save_metadata_to_db(track, metadata, track.name)
            return metadata

        self.logger.debug(f"[云音乐歌词插件] 无歌词 | 歌曲: {track.name}")
        return None
