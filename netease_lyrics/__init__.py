"""
Netease Cloud Music API Lyrics Metadata Provider for Music Assistant
网易云音乐 API 歌词自动补全（支持毫秒级歌词同步）
Version 1.7.0 - 适配 Music Assistant 2.11.x
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

PLUGIN_VERSION = "1.7.0"

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.LYRICS,
}


class ConfigKeys:
    """配置项键名常量类"""

    BASE_URL = "base_url"
    UPDATE_EXISTING_LYRICS = "update_existing_lyrics"
    LYRICS_OFFSET_MS = "lyrics_offset_ms"
    LYRICS_DISPLAY_MODE = "lyrics_display_mode"


DEFAULT_BASE_URL = "http://localhost:3000"
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
# 增强 LRC (ELRC) 格式：[mm:ss.xx-N] — N 为歌曲总时长(秒),由 lxserver 转码产生
ELRC_TIMESTAMP_PATTERN = re.compile(r"^\[(\d{1,2}):(\d{2})\.(\d{2,3})-\d+\]\s*(.*)$")

_CLEAN_BRACKET_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_CLEAN_FEAT_RE = re.compile(r"\b(feat\.?|featuring|ft\.?|with)\b.*$", re.I)
_CLEAN_VERSION_RE = re.compile(r"\([^)]*(live|remix|cover|伴奏|原版|inst|instrumental)[^)]*\)", re.I)

# 元信息关键词（中英文 + 全半角冒号），用于识别"作词 : XXX"这类元信息行
# 用户偏好:有真歌词时保留元信息头部,只有元信息时才丢弃
# 元信息行格式: "作词 : XXX" 或 "Lyrics by: XXX" - 关键字在前,冒号在后
_METADATA_PREFIX_RE = re.compile(
    r"^\s*("
    r"作词|作曲|编曲|制作|混音|母带|录音|出品|监制|统筹|总策划|"
    r"艺人|翻译|出处|发行|封面|推广|企宣|经纪人|"
    r"OP|SP|"
    r"Lyrics?|Composers?|Arrangers?|Producers?|Mix|Master|Recordings?|"
    r"Labels?|Publishers?|Distributions?|Cover|Designs?|Vocals?|Perform"
    r")\s*[:：]\s*\S",
    re.IGNORECASE,
)

# 占位文本 - NetEase 在无歌词时返回的提示
_PLACEHOLDER_RE = re.compile(
    r"^\s*(暂无歌词|纯音乐[，,]?\s*请欣赏|暂无歌词[，,]?\s*请欣赏)\s*$"
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """初始化插件实例"""
    return NeteaseMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseMusicProvider(MetadataProvider):
    """云音乐歌词插件：提供毫秒级同步歌词"""

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

        base = self.base_url.rstrip("/")
        self.search_api_url = f"{base}/cloudsearch"
        self.lyric_api_url = f"{base}/lyric"
        # NetEase 官方结构化端点:返回 YRC/LRC 混合格式,元信息与歌词天然分离
        self.lyric_new_api_url = f"{base}/lyric/new"

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

    # 名称清洗与匹配
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

    # 网络请求（均带缓存与限流）
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

        端点策略:优先 /lyric/new(NetEase 官方结构化端点,YRC/LRC 混合格式,元信息天然分离),
        失败时回退 /lyric(lxserver 聚合转码产物,可能含 ELRC 元信息)。
        对每个端点返回的原文+翻译都做"同步/纯文本/元信息/占位"三分类,过滤掉
        元信息/占位后根据"是否真有同步行"决定返回 LRC 还是纯文本。
        """
        params = {"id": song_id, "lv": -1, "kv": -1, "tv": -1}
        for endpoint in (self.lyric_new_api_url, self.lyric_api_url):
            try:
                async with self.mass.http_session.get(endpoint, params=params) as response:
                    if response.status == 204:
                        continue
                    response.raise_for_status()
                    data = cast("dict[str, Any]", await response.json())
                    synced_lyrics = data.get("lrc", {}).get("lyric", "") or ""
                    translated_lyrics = data.get("tlyric", {}).get("lyric", "") or ""

                    o_synced, o_plain = self._parse_yrc_lrc(synced_lyrics)
                    t_synced, t_plain = self._parse_yrc_lrc(translated_lyrics)

                    o_real_synced = [(ts, t) for ts, t in o_synced if not self._is_metadata_or_placeholder(t)]
                    t_real_synced = [(ts, t) for ts, t in t_synced if not self._is_metadata_or_placeholder(t)]
                    o_real_plain = [t for t in o_plain if not self._is_metadata_or_placeholder(t)]
                    t_real_plain = [t for t in t_plain if not self._is_metadata_or_placeholder(t)]

                    if o_real_synced or t_real_synced:
                        orig_out = self._build_lrc_from_synced(o_real_synced) if o_real_synced else ""
                        trans_out = self._build_lrc_from_synced(t_real_synced) if t_real_synced else ""
                        self.logger.debug(
                            f"[云音乐歌词插件] 获取歌词成功 | 端点: {endpoint} | 歌曲ID: {song_id} | "
                            f"原文同步行: {len(o_real_synced)} | 翻译同步行: {len(t_real_synced)}"
                        )
                        return orig_out, trans_out

                    if o_real_plain or t_real_plain:
                        orig_out = "\n".join(o_real_plain) if o_real_plain else ""
                        trans_out = "\n".join(t_real_plain) if t_real_plain else ""
                        self.logger.debug(
                            f"[云音乐歌词插件] 纯文本歌词(过滤元信息) | 端点: {endpoint} | "
                            f"歌曲ID: {song_id} | 原文行数: {len(o_real_plain)}"
                        )
                        return orig_out, trans_out

                    self.logger.debug(
                        f"[云音乐歌词插件] 无真歌词 | 端点: {endpoint} | 歌曲ID: {song_id}"
                    )
            except (ClientResponseError, json.JSONDecodeError, ContentTypeError) as e:
                self.logger.debug(
                    f"[云音乐歌词插件] 获取歌词失败 | 端点: {endpoint} | 歌曲ID: {song_id} | 错误: {e}"
                )
                continue
            except Exception as e:
                self.logger.debug(
                    f"[云音乐歌词插件] 获取歌词异常 | 端点: {endpoint} | 歌曲ID: {song_id} | "
                    f"错误: {e} | 类型: {type(e).__name__}"
                )
                continue
        return "", ""

    # LRC 处理
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

    def _parse_yrc_lrc(self, raw: str) -> tuple[list[tuple[int, str]], list[str]]:
        """三分类解析原始歌词文本。

        NetEase API 在不同端点可能返回以下格式混合:
          - LRC  [mm:ss.xx] 文本         → 同步行
          - LRC  [mm:ss] 文本            → 同步行
          - LRC  [mm:ss.xx-N] 文本       → ELRC(lxserver 转码产物),常见作元信息载体
          - YRC  {\\"t\\":-1,\\"c\\":[...]}   → 元信息/标题(忽略)
          - YRC  {\\"t\\":0,\\"c\\":[...]}    → 同步行
          - 纯文本(无时间戳)             → 纯文本行

        返回:
          (同步行列表[(ms, text)], 纯文本行列表[str])
        """
        synced: list[tuple[int, str]] = []
        plain: list[str] = []

        if not raw:
            return synced, plain

        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue

            # LRC 元信息头 [ti:...][ar:...][al:...][au:...][by:...][offset:...][length:...]
            if stripped.startswith("[") and stripped.endswith("]") and ":" in stripped[1:-1]:
                inner = stripped[1:-1].split(":", 1)[0].lower()
                if inner in {"ti", "ar", "al", "au", "by", "offset", "length", "kana", "language"}:
                    continue
                # [00:00.00] 形式的时间戳行以 [ 开头但不以内 ] 形式包含 :
                # 已由后续 NORMALIZED_LRC_PATTERN / NON_STANDARD_LRC_PATTERN 处理

            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                text = self._extract_yrc_text(obj)
                if not text:
                    continue
                ts = obj.get("t")
                if isinstance(ts, (int, float)) and ts >= 0:
                    synced.append((int(ts), text))
                # t:-1 或缺 t 字段 → 元信息行,不归类为 plain(留给 _is_metadata_or_placeholder)
                continue

            elrc_match = ELRC_TIMESTAMP_PATTERN.match(stripped)
            if elrc_match:
                minutes = int(elrc_match.group(1))
                seconds = int(elrc_match.group(2))
                ms_part = elrc_match.group(3).ljust(3, "0")
                milliseconds = int(ms_part)
                timestamp_ms = (minutes * 60 * 1000) + (seconds * 1000) + milliseconds
                text = elrc_match.group(4).strip()
                if text:
                    synced.append((timestamp_ms, text))
                continue

            std_match = NORMALIZED_LRC_PATTERN.match(stripped)
            if std_match:
                minutes = int(std_match.group(1))
                seconds = int(std_match.group(2))
                ms_part = std_match.group(3).ljust(3, "0")
                milliseconds = int(ms_part)
                timestamp_ms = (minutes * 60 * 1000) + (seconds * 1000) + milliseconds
                text = std_match.group(4).strip()
                if text:
                    synced.append((timestamp_ms, text))
                continue

            ns_match = NON_STANDARD_LRC_PATTERN.match(stripped)
            if ns_match:
                minutes = int(ns_match.group(1))
                seconds = int(ns_match.group(2))
                text = stripped[ns_match.end():].strip()
                if text:
                    synced.append((minutes * 60 * 1000 + seconds * 1000, text))
                continue

            plain.append(stripped)

        return synced, plain

    @staticmethod
    def _extract_yrc_text(obj: dict[str, Any]) -> str:
        """从 YRC JSON 行中提取纯文本(拼接所有 c[].tx)。"""
        cells = obj.get("c") or []
        parts: list[str] = []
        for cell in cells:
            tx = cell.get("tx") if isinstance(cell, dict) else None
            if tx:
                parts.append(str(tx))
        return "".join(parts).strip()

    def _is_metadata_or_placeholder(self, text: str) -> bool:
        """判断单行歌词是否为元信息或占位文本(应被丢弃)。

        元信息: "作词 : XXX" / "Lyricist: XXX" / "[00:00.00] 作词 : XXX"
        占位:   "暂无歌词" / "纯音乐,请欣赏"
        """
        if not text:
            return True
        if _PLACEHOLDER_RE.match(text):
            return True
        # 元信息行通常以 "标签:" 开头;同时容忍前置时间戳 [mm:ss.xx]
        stripped = NORMALIZED_LRC_PATTERN.sub("", text, count=1).strip()
        if _METADATA_PREFIX_RE.match(stripped):
            return True
        return False

    def _is_all_metadata_or_placeholder(self, text: str) -> bool:
        """判断整段歌词是否全部由元信息/占位组成。

        用于 save 之前的"双重保险":即使上游过滤漏过,也不写脏数据进数据库。
        """
        if not text or not text.strip():
            return True
        has_real_line = False
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            ts_match = NORMALIZED_LRC_PATTERN.match(stripped)
            content = ts_match.group(4).strip() if ts_match else stripped
            if content and not self._is_metadata_or_placeholder(content):
                has_real_line = True
                break
        return not has_real_line

    def _build_lrc_from_synced(self, parsed_lines: list[tuple[int, str]]) -> str:
        """构建标准 LRC 文本(语义化封装 _build_lrc)。"""
        return self._build_lrc(parsed_lines)

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

    def _build_plain_lyrics(self, lyrics: str) -> MediaItemMetadata | None:
        """构建纯文本（无时间戳）歌词元数据。

        若全部内容都是元信息/占位文本,返回 None,避免脏数据污染数据库。
        """
        if self._is_all_metadata_or_placeholder(lyrics):
            return None
        metadata = MediaItemMetadata()
        metadata.lyrics = lyrics
        return metadata

    # 元数据落地
    def _save_metadata_to_db(self, track: Track, metadata: MediaItemMetadata, track_name: str) -> None:
        """将歌词写入数据库/元数据缓存。

        写入前做"双重保险":
          1. 字段级空检查(防止空字符串污染)
          2. 内容级真歌词检查(防止元信息/占位文本污染)

        MA 2.11.x 适配说明:
          - 2.11 移除了 `mass.metadata.save_item_metadata`(2.10.x 用于持久化
            非库内曲目的 provider 元数据)。现在非 library 曲目由 core 在
            按需查询时直接消费 get_track_metadata 的返回值,无需插件落库;
            library 曲目仍走 update_item_in_library(该 API 在 2.11 中保留)。
          - 2.11 的 tracks._update_library_item 对 metadata 做 fill-gap 合并,
            因此"更新已有歌词"开启时强制覆盖须携带在当前 track 对象上,
            此处直接改写 track.metadata 后整体回写,与 core enrichment 路径一致。
        """
        try:
            if not track.metadata:
                track.metadata = MediaItemMetadata()

            lrc_value = metadata.lrc_lyrics or ""
            plain_value = metadata.lyrics or ""

            lrc_value = lrc_value.strip()
            plain_value = plain_value.strip()

            if lrc_value and not self._is_all_metadata_or_placeholder(lrc_value):
                track.metadata.lrc_lyrics = lrc_value
            if plain_value and not self._is_all_metadata_or_placeholder(plain_value):
                track.metadata.lyrics = plain_value

            if track.provider == "library":
                self.mass.create_task(self.mass.music.tracks.update_item_in_library(track.item_id, track))
            elif hasattr(self.mass.metadata, "save_item_metadata"):
                # MA <=2.10.x 兼容:持久化非库内曲目的 provider 元数据
                self.mass.create_task(self.mass.metadata.save_item_metadata(track))
            # MA 2.11.x: 非 library 曲目无需落库,core 直接使用本方法返回值
        except Exception as e:
            self.logger.error(f"[云音乐歌词插件] 歌词写入数据库失败 | 歌曲: {track_name} | 错误: {str(e)}")

    # 主流程
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
            metadata = self._build_plain_lyrics(plain_fallback)
            if metadata is None:
                self.logger.debug(
                    f"[云音乐歌词插件] 纯文本为元信息/占位,丢弃 | 歌曲: {track.name}"
                )
            else:
                self.logger.debug(f"[云音乐歌词插件] 纯文本歌词 | 歌曲: {track.name}")
                self._save_metadata_to_db(track, metadata, track.name)
                return metadata

        self.logger.debug(f"[云音乐歌词插件] 无歌词 | 歌曲: {track.name}")
        return None
