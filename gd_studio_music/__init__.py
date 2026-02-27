"""
GD Studio Music Provider for Music Assistant v1.1.21 
"""
from __future__ import annotations

import asyncio
import logging
import time
import struct
from typing import TYPE_CHECKING, Dict, Optional, List
from collections import defaultdict

import aiohttp
from yarl import URL

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ConfigValueOption,
    ConfigEntryType,
)

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant

__version__ = "1.1.21"
DOMAIN = "gd_studio_music"

# 配置项常量
CONF_DEFAULT_SOURCE = "default_source"
CONF_AUDIO_QUALITY = "audio_quality"
CONF_IMAGE_SIZE = "image_size"

# 日志配置
_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

# 核心工具函数
def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "00:00"
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins:02d}:{secs:02d}"

def calculate_mp3_duration(data: bytes, bitrate: int = 320, url: str = "", total_size: int = 0) -> int:
    try:
        bytes_per_second = (bitrate * 1000) // 8
        if bytes_per_second == 0:
            bytes_per_second = 40000
        
        use_size = total_size if total_size > 0 else len(data)
        return int(use_size / bytes_per_second)
    except Exception as e:
        _LOGGER.error(f"MP3时长解析失败: {str(e)}")
        return 0

def calculate_flac_duration(data: bytes, url: str = "") -> int:
    try:
        if len(data) < 40 or not data.startswith(b"fLaC"):
            return 0
        
        offset = 4
        while offset + 4 <= len(data):
            block_header = data[offset:offset+4]
            if len(block_header) < 4:
                break
                
            is_last = (block_header[0] & 0x80) != 0
            block_type = block_header[0] & 0x7F
            block_size = struct.unpack('>I', b'\x00' + block_header[1:4])[0]
            
            if block_type == 0:
                if offset + 4 + block_size > len(data):
                    break
                stream_info = data[offset+4:offset+4+block_size]
                if len(stream_info) >= 18:
                    sample_rate = (stream_info[0] << 12) | (stream_info[1] << 4) | (stream_info[2] >> 4)
                    total_samples = (
                        (stream_info[10] << 24) | 
                        (stream_info[11] << 16) | 
                        (stream_info[12] << 8) | 
                        stream_info[13]
                    ) << 12
                    total_samples |= (stream_info[14] << 4) | (stream_info[15] >> 4)
                    
                    if sample_rate > 0 and total_samples > 0:
                        return int(total_samples / sample_rate)
                break
            
            offset += 4 + block_size
            if is_last:
                break
                
        return 0
    except Exception as e:
        _LOGGER.error(f"FLAC时长解析失败: {str(e)}")
        return 0

async def fetch_audio_duration(url: str, actual_br: int, content_type: str = "mp3", timeout: int = 10) -> int:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            head_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            async with session.head(url, headers=head_headers, allow_redirects=True) as head_resp:
                full_content_length = 0
                if head_resp.status == 200:
                    cl = head_resp.headers.get('Content-Length')
                    if cl and cl.isdigit():
                        full_content_length = int(cl)
            
            bitrate = actual_br
            
            if full_content_length > 0:
                duration = calculate_mp3_duration(b"", bitrate, url, full_content_length)
                if duration > 0:
                    return duration
            
            download_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Range': 'bytes=0-5242880'
            }
            
            async with session.get(url, headers=download_headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in [200, 206]:
                    return 0
                
                content_length = resp.headers.get('Content-Length')
                total_size = 0
                if content_length and content_length.isdigit():
                    total_size = int(content_length)
                
                if "flac" in content_type.lower() or "lossless" in url.lower():
                    data = await resp.read()
                    return calculate_flac_duration(data, url)
                else:
                    data = await resp.read()
                    return calculate_mp3_duration(data, bitrate, url, total_size)
                    
    except asyncio.TimeoutError:
        _LOGGER.error(f"时长获取超时 | URL: {url[:50]}")
    except Exception as e:
        _LOGGER.error(f"时长获取失败: {str(e)} | URL: {url[:50]}")
    
    return 0

# 核心配置
STABLE_SOURCES = [
    ("网易云音乐", "netease"),
    ("酷我音乐", "kuwo"),
    ("JOOX", "joox"),
]
SOURCE_VALUES = [v for _, v in STABLE_SOURCES]

QUALITY_MAPPING = {
    "standard": "128",
    "higher": "192",
    "exhigh": "320",
    "lossless": "740",
    "hires": "999",
}

CONTENT_TYPE_MAPPING = {
    "128": ContentType.MP3,
    "192": ContentType.MP3,
    "320": ContentType.MP3,
    "740": ContentType.FLAC,
    "999": ContentType.FLAC,
    "908": ContentType.MP3,
    "700": ContentType.MP3,
    "992": ContentType.MP3,
    "1003": ContentType.MP3,
    "886": ContentType.MP3,
}

API_TIMEOUT = aiohttp.ClientTimeout(total=15)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
API_BASE_URL = "https://music-api.gdstudio.xyz/api.php"

# 缓存与限流
_api_request_counter = defaultdict(int)
RATE_LIMIT_DURATION = 300
RATE_LIMIT_MAX_REQUESTS = 150

_track_cache = {}
_duration_cache = {}
_source_swap_cache = {}
_failed_track_cache = {}

def _check_rate_limit() -> tuple[bool, str]:
    now = time.time()
    cutoff_time = now - RATE_LIMIT_DURATION
    for timestamp in list(_api_request_counter.keys()):
        if timestamp < cutoff_time:
            del _api_request_counter[timestamp]
    
    total_requests = sum(_api_request_counter.values())
    if total_requests >= RATE_LIMIT_MAX_REQUESTS:
        tip_msg = f"请求频繁，请1分钟后再试（5分钟内最多{RATE_LIMIT_MAX_REQUESTS}次）"
        _LOGGER.warning(f"限流触发 | {tip_msg}")
        return False, tip_msg
    
    current_minute = int(now // 60) * 60
    _api_request_counter[current_minute] += 1
    return True, ""

def get_full_track_id(raw_id: str, source: str = None) -> str:
    if not source:
        source = "netease"
    
    if "_" in str(raw_id):
        return str(raw_id)
    
    return f"{source}_{raw_id}"

async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> MusicProvider:
    _LOGGER.info(f"启动 {DOMAIN} v{__version__}")
    return GDStudioMusicProvider(mass, manifest, config)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    config_entries = (
        ConfigEntry(
            key=CONF_DEFAULT_SOURCE,
            type=ConfigEntryType.STRING,
            label="默认音乐源",
            required=True,
            default_value="joox",
            options=[ConfigValueOption(title=name, value=value) for name, value in STABLE_SOURCES],
        ),
        ConfigEntry(
            key=CONF_AUDIO_QUALITY,
            type=ConfigEntryType.STRING,
            label="音质",
            default_value="exhigh",
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
            label="封面尺寸",
            required=True,
            default_value="300",
            options=[
                ConfigValueOption(title="小图 (300px)", value="300"),
                ConfigValueOption(title="大图 (500px)", value="500"),
            ],
        ),
    )
    return config_entries

class GDStudioMusicProvider(MusicProvider):
    @property
    def domain(self) -> str:
        return DOMAIN

    @property
    def supported_features(self) -> set[ProviderFeature]:
        return {ProviderFeature.SEARCH}

    async def handle_async_init(self) -> None:
        self._default_source = str(self.config.get_value(CONF_DEFAULT_SOURCE, "joox")).strip()
        self._audio_quality = str(self.config.get_value(CONF_AUDIO_QUALITY, "exhigh")).strip()
        self._image_size = str(self.config.get_value(CONF_IMAGE_SIZE, "300")).strip()
        self._br_param = QUALITY_MAPPING.get(self._audio_quality, "320")
        
        if self._image_size not in ["300", "500"]:
            self._image_size = "300"
        if self._default_source not in SOURCE_VALUES:
            self._default_source = "joox"
            
        self._session = aiohttp.ClientSession(
            timeout=API_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            connector=aiohttp.TCPConnector(ssl=False, limit=5),
            trust_env=True
        )
        _LOGGER.info(f"初始化完成 | 音源={self._default_source} | 音质={self._audio_quality} | 音质参数={self._br_param}")

    async def handle_async_stop(self) -> None:
        if hasattr(self, "_session") and not self._session.closed:
            try:
                await asyncio.wait_for(self._session.close(), timeout=5)
            except Exception as e:
                _LOGGER.warning(f"关闭会话失败: {e}")
        
        _api_request_counter.clear()
        _LOGGER.info("插件停止，已清理会话和计数器")

    async def _api_request(self, api_type: str, params: dict) -> tuple[dict | list, str]:
        allow_request, tip_msg = _check_rate_limit()
        if not allow_request:
            return [] if api_type == "search" else {}, tip_msg
        
        try:
            async with self._session.get(
                API_BASE_URL, 
                params=params, 
                allow_redirects=True,
                timeout=API_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None), ""
        except Exception as e:
            error_msg = f"API请求失败：{str(e)[:50]}"
            _LOGGER.error(error_msg)
            return [] if api_type == "search" else {}, error_msg

    async def _fetch_pic_url(self, pic_id: str, source: str) -> Optional[str]:
        if not pic_id or pic_id == "unknown":
            return None
            
        try:
            pic_data, _ = await self._api_request(
                "pic", 
                {"types": "pic", "source": source, "id": pic_id, "size": self._image_size}
            )
            
            if isinstance(pic_data, dict) and pic_data.get("url"):
                pic_url = pic_data["url"].strip()
                if pic_url.startswith(("http://", "https://")):
                    return pic_url
        except Exception as e:
            _LOGGER.error(f"获取封面失败: {e}")
        
        return None

    async def _get_stream_url_with_swap(
        self, 
        track_id: str, 
        initial_source: str
    ) -> tuple[Optional[str], Optional[str], Optional[str], str]:
        start_time = time.time()
        target_br = self._br_param
        cache_key = f"{track_id}_{target_br}"
        fail_key = f"{track_id}_{initial_source}"
        
        if fail_key in _failed_track_cache:
            if _failed_track_cache[fail_key] > time.time():
                tip_msg = f"该歌曲暂时无法播放，请1分钟后再试"
                return None, None, None, tip_msg
            else:
                del _failed_track_cache[fail_key]
        
        if cache_key in _source_swap_cache:
            cached = _source_swap_cache[cache_key]
            if cached.get("expire", 0) > time.time():
                elapsed_time = round((time.time() - start_time) * 1000, 2)
                _LOGGER.info(f"缓存命中 | 曲目={track_id} | 音源={cached['source']} | 耗时={elapsed_time}ms")
                return cached["url"], cached["br"], cached["source"], ""
            else:
                del _source_swap_cache[cache_key]
        
        source_list = [initial_source] if initial_source in SOURCE_VALUES else []
        for source in SOURCE_VALUES:
            if source not in source_list:
                source_list.append(source)
        
        quality_list = {
            "999": ["999", "740"],
            "740": ["740", "320"],
            "320": ["320"],
            "192": ["192", "128"],
            "128": ["128"],
            "992": ["992", "320"],
            "1003": ["1003", "320"],
            "886": ["886", "320"],
        }.get(target_br, ["320"])
        
        total_attempts = 0
        max_attempts = 3
        
        for source in source_list:
            if total_attempts >= max_attempts:
                break
                
            br = quality_list[0]
            total_attempts += 1
            
            url_data, tip_msg = await self._api_request(
                "url", 
                {"types": "url", "source": source, "id": track_id, "br": br}
            )
            
            if tip_msg:
                return None, None, None, tip_msg
            
            if (isinstance(url_data, dict) and 
                url_data.get("url") and 
                url_data.get("url").strip() and 
                url_data.get("br", -1) != -1):
                
                url = url_data["url"].strip()
                actual_br = str(url_data.get("br", br))
                
                _source_swap_cache[cache_key] = {
                    "url": url,
                    "br": actual_br,
                    "source": source,
                    "expire": time.time() + 3600
                }
                
                elapsed_time = round((time.time() - start_time) * 1000, 2)
                _LOGGER.info(f"换源成功 | 曲目={track_id} | 音源={source} | 音质={actual_br}k | 耗时={elapsed_time}ms")
                return url, actual_br, source, ""
        
        if total_attempts < max_attempts and len(quality_list) > 1:
            for source in source_list:
                if total_attempts >= max_attempts:
                    break
                    
                br = quality_list[1]
                total_attempts += 1
                
                url_data, tip_msg = await self._api_request(
                    "url", 
                    {"types": "url", "source": source, "id": track_id, "br": br}
                )
                
                if tip_msg:
                    return None, None, None, tip_msg
                
                if (isinstance(url_data, dict) and 
                    url_data.get("url") and 
                    url_data.get("url").strip() and 
                    url_data.get("br", -1) != -1):
                    
                    url = url_data["url"].strip()
                    actual_br = str(url_data.get("br", br))
                    
                    _source_swap_cache[cache_key] = {
                        "url": url,
                        "br": actual_br,
                        "source": source,
                        "expire": time.time() + 3600
                    }
                    
                    elapsed_time = round((time.time() - start_time) * 1000, 2)
                    _LOGGER.info(f"降级成功 | 曲目={track_id} | 音源={source} | 音质={actual_br}k | 耗时={elapsed_time}ms")
                    return url, actual_br, source, ""
        
        _failed_track_cache[fail_key] = time.time() + 60
        elapsed_time = round((time.time() - start_time) * 1000, 2)
        tip_msg = f"该歌曲暂时无法获取播放链接，请稍后再试"
        _LOGGER.error(f"播放链接获取失败 | 曲目={track_id} | 耗时={elapsed_time}ms")
        return None, None, None, tip_msg

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 15
    ) -> SearchResults:
        res = SearchResults()
        
        if MediaType.TRACK not in media_types or not search_query.strip():
            return res
        
        search_params = {
            "types": "search",
            "source": self._default_source,
            "name": search_query.strip(),
            "count": 15,
            "pages": 1
        }
        
        _LOGGER.info(f"搜索请求 | 关键词={search_query} | 音源={self._default_source}")
        data, tip_msg = await self._api_request("search", search_params)
        
        if tip_msg:
            _LOGGER.warning(f"搜索限流 | {tip_msg}")
            return res
        
        if not isinstance(data, list):
            _LOGGER.warning(f"API返回非列表数据 | 数据类型={type(data)}")
            return res
        
        tracks = []
        
        for idx, item in enumerate(data[:15]):
            try:
                track_id_raw = item.get("id")
                if not track_id_raw:
                    continue
                    
                item_id = get_full_track_id(track_id_raw, self._default_source)
                name = item.get("name", "未知歌曲").strip()
                
                dur = 0
                
                artists = item.get("artist", [])
                if isinstance(artists, str):
                    artists = [artists]
                artist_list = [a.strip() for a in artists[:3] if a and a.strip()]
                artist_name = artist_list[0] if artist_list else "未知艺术家"
                album_name = item.get("album", "未知专辑").strip()
                pic_id = item.get("pic_id", "")

                _track_cache[item_id] = {
                    "name": name,
                    "artist": artist_name,
                    "album": album_name,
                    "pic_id": pic_id,
                    "duration": dur,
                    "duration_formatted": format_duration(dur),
                    "source": self._default_source,
                    "raw_duration": item.get("duration", 0)
                }

                track = Track(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=name,
                    duration=dur,
                    provider_mappings={
                        ProviderMapping(
                            item_id=str(track_id_raw), 
                            provider_domain=DOMAIN, 
                            provider_instance=self.instance_id
                        )
                    },
                )
                
                for a in artist_list:
                    track.artists.append(ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=f"{self._default_source}_artist_{a}",
                        provider=self.instance_id,
                        name=a
                    ))
                
                track.album = ItemMapping(
                    media_type=MediaType.ALBUM,
                    item_id=f"{self._default_source}_album_{album_name}",
                    provider=self.instance_id,
                    name=album_name
                )
                
                if pic_id:
                    url = await self._fetch_pic_url(pic_id, self._default_source)
                    if url:
                        track.metadata.images = [MediaItemImage(
                            type=ImageType.THUMB, 
                            path=url, 
                            provider=self.instance_id
                        )]

                tracks.append(track)
                
            except Exception as e:
                _LOGGER.error(f"解析搜索结果失败 | 索引={idx} | 错误={str(e)}")
                continue
        
        _LOGGER.info(f"搜索完成 | 关键词={search_query} | 结果数={len(tracks)}")
        res.tracks = tracks
        return res

    async def get_track(self, prov_track_id: str, fallback_info: Optional[Dict] = None) -> Track:
        full_track_id = get_full_track_id(prov_track_id, self._default_source)
        
        if full_track_id in _track_cache:
            cached = _track_cache[full_track_id]
            name = cached["name"]
            artist = cached["artist"]
            album = cached["album"]
            cache_duration = _duration_cache.get(full_track_id, cached["duration"])
            if cache_duration <= 0 and prov_track_id in _duration_cache:
                cache_duration = _duration_cache[prov_track_id]
            dur = cache_duration
            formatted_dur = format_duration(dur)
            pic_id = cached["pic_id"]
            source = cached["source"]
        else:
            if "_" in full_track_id:
                source, rid = full_track_id.split("_", 1)
            else:
                source = self._default_source
                rid = full_track_id
                
            if source not in SOURCE_VALUES:
                source = self._default_source

            name = rid[:8]
            artist = "未知艺术家"
            album = "未知专辑"
            dur = _duration_cache.get(full_track_id, _duration_cache.get(prov_track_id, 0))
            formatted_dur = format_duration(dur)
            pic_id = ""

            if fallback_info and isinstance(fallback_info, dict):
                name = fallback_info.get("name", name).strip()
                artist = fallback_info.get("artist", artist).strip()
                album = fallback_info.get("album", album).strip()

        track = Track(
            item_id=full_track_id,
            provider=self.instance_id,
            name=name,
            duration=dur,
            provider_mappings={
                ProviderMapping(
                    item_id=full_track_id.split("_")[-1], 
                    provider_domain=DOMAIN, 
                    provider_instance=self.instance_id
                )
            },
        )
        
        track.metadata.duration = dur
        track.metadata.extra = {
            "duration_seconds": dur,
            "duration_formatted": formatted_dur,
            "source": source
        }
        
        for a in [artist]:
            track.artists.append(ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=f"{source}_artist_{a}",
                provider=self.instance_id,
                name=a
            ))
        
        track.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=f"{source}_album_{album}",
            provider=self.instance_id,
            name=album
        )
        
        if pic_id:
            url = await self._fetch_pic_url(pic_id, source)
            if url:
                track.metadata.images = [MediaItemImage(
                    type=ImageType.THUMB, 
                    path=url, 
                    provider=self.instance_id
                )]
        
        return track

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        start_time = time.time()
        
        if media_type != MediaType.TRACK or not item_id:
            raise ValueError("仅支持单曲播放")
            
        if "_" in item_id:
            source, track_id = item_id.split("_", 1)
        else:
            source = self._default_source
            track_id = item_id
            
        if source not in SOURCE_VALUES:
            source = self._default_source

        url, br_str, success_source, tip_msg = await self._get_stream_url_with_swap(track_id, source)
        
        if not url:
            elapsed_time = round((time.time() - start_time) * 1000, 2)
            error_msg = tip_msg if tip_msg else f"无法获取播放链接 | 曲目ID={item_id}"
            _LOGGER.error(f"播放流获取失败 | 曲目={item_id} | 耗时={elapsed_time}ms")
            raise ValueError(error_msg)
        
        try:
            actual_br = int(br_str)
        except ValueError:
            actual_br = 320
            _LOGGER.warning(f"无法解析比特率: {br_str}，使用默认320 kbps")
        
        # 解析音频时长
        track_duration = 0
        
        full_item_id = get_full_track_id(item_id, source)
        if full_item_id in _duration_cache:
            track_duration = _duration_cache[full_item_id]
        elif item_id in _duration_cache:
            track_duration = _duration_cache[item_id]
        else:
            content_type = "flac" if br_str in ["740", "999"] else "mp3"
            if "908" in br_str or "992" in br_str or "700" in br_str or "1003" in br_str or "886" in br_str:
                content_type = "mp3"
            
            track_duration = await fetch_audio_duration(url, actual_br, content_type)
            
            if track_duration <= 0:
                if full_item_id in _track_cache:
                    raw_duration = _track_cache[full_item_id].get("raw_duration", 0)
                    if raw_duration > 0:
                        track_duration = raw_duration
                    else:
                        track_duration = 240
                else:
                    track_duration = 240
        
        if track_duration <= 0:
            track_duration = 240

        if full_item_id not in _duration_cache:
            _duration_cache[full_item_id] = track_duration
            _duration_cache[item_id] = track_duration
            if full_item_id in _track_cache:
                _track_cache[full_item_id]["duration"] = track_duration
                _track_cache[full_item_id]["duration_formatted"] = format_duration(track_duration)
        
        # 更新Track.duration
        try:
            if full_item_id in _track_cache:
                old_duration = _track_cache[full_item_id]["duration"]
                _track_cache[full_item_id]["duration"] = track_duration
                _track_cache[full_item_id]["duration_formatted"] = format_duration(track_duration)
            
            updated_track = await self.get_track(full_item_id)
            if updated_track.duration == track_duration:
                _LOGGER.info(f"Track.duration更新成功 | ID={full_item_id} | 新值={updated_track.duration}秒 ({format_duration(updated_track.duration)})")
            else:
                _LOGGER.warning(f"Track.duration更新后值不匹配 | 预期={track_duration} | 实际={updated_track.duration}")
                
        except Exception as e:
            _LOGGER.warning(f"更新Track.duration异常: {str(e)}，但已更新缓存")
        
        # 确定音频格式
        content_type = CONTENT_TYPE_MAPPING.get(br_str, ContentType.MP3)
        try:
            bit_depth = 24 if content_type == ContentType.FLAC else 16
            bit_rate = actual_br
            sample_rate = 48000 if content_type == ContentType.FLAC else 44100
            
            audio_format = AudioFormat(
                content_type=content_type,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                bit_rate=bit_rate,
            )
        except Exception as e:
            audio_format = AudioFormat(
                content_type=ContentType.MP3,
                sample_rate=44100,
                bit_depth=16,
                bit_rate=320,
            )
            _LOGGER.warning(f"音频格式解析失败，使用默认值 | 错误={str(e)}")

        stream_details = StreamDetails(
            item_id=full_item_id,
            provider=self.instance_id,
            audio_format=audio_format,
            stream_type=StreamType.HTTP,
            path=url,
            duration=track_duration,
        )
        
        stream_details.metadata = {
            "duration_seconds": track_duration,
            "duration_formatted": format_duration(track_duration),
            "bitrate": bit_rate,
            "source": success_source
        }
        
        elapsed_time = round((time.time() - start_time) * 1000, 2)
        formatted_dur = format_duration(track_duration)
        _LOGGER.info(f"播放流构建完成 | 曲目={full_item_id} | 音源={success_source} | 音质={br_str}k | 时长={formatted_dur} | 耗时={elapsed_time}ms")
        
        return stream_details

    # 专辑/艺术家相关方法
    async def get_album(self, prov_album_id: str) -> Album:
        source = self._default_source
        album_name = "未知专辑"

        if "_album_" in prov_album_id:
            parts = prov_album_id.split("_album_", 1)
            source_part = parts[0] if len(parts) > 0 else self._default_source
            album_name = parts[1] if len(parts) > 1 else "未知专辑"
            if "_" in source_part:
                source = source_part.split("_")[0]
        
        if source not in SOURCE_VALUES:
            source = self._default_source

        album = Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=album_name,
            provider_mappings={
                ProviderMapping(
                    item_id=album_name,
                    provider_domain=DOMAIN,
                    provider_instance=self.instance_id
                )
            }
        )

        cover_url = None
        for track_id, cached in _track_cache.items():
            if cached.get("album") == album_name and cached.get("source") == source:
                pic_id = cached.get("pic_id")
                if pic_id:
                    cover_url = await self._fetch_pic_url(pic_id, source)
                    if cover_url:
                        break

        if cover_url:
            album.metadata.images = [MediaItemImage(
                type=ImageType.THUMB,
                path=cover_url,
                provider=self.instance_id,
            )]

        return album

    async def get_artist(self, prov_artist_id: str) -> Artist:
        source = self._default_source
        artist_name = "未知艺术家"

        if "_artist_" in prov_artist_id:
            parts = prov_artist_id.split("_artist_", 1)
            source_part = parts[0] if len(parts) > 0 else self._default_source
            artist_name = parts[1] if len(parts) > 1 else "未知艺术家"
            if "_" in source_part:
                source = source_part.split("_")[0]
        
        if source not in SOURCE_VALUES:
            source = self._default_source

        artist = Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=artist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_name,
                    provider_domain=DOMAIN,
                    provider_instance=self.instance_id
                )
            }
        )

        cover_url = None
        for track_id, cached in _track_cache.items():
            if cached.get("artist") == artist_name and cached.get("source") == source:
                pic_id = cached.get("pic_id")
                if pic_id:
                    cover_url = await self._fetch_pic_url(pic_id, source)
                    if cover_url:
                        break

        if cover_url:
            artist.metadata.images = [MediaItemImage(
                type=ImageType.THUMB,
                path=cover_url,
                provider=self.instance_id,
            )]

        return artist

    # 空实现方法
    async def get_album_tracks(self, prov_album_id: str) -> list:
        return []
        
    async def get_artist_albums(self, prov_artist_id: str) -> list:
        return []
        
    async def get_artist_toptracks(self, prov_artist_id: str) -> list:
        return []
        
    async def get_playlist(self, prov_playlist_id: str) -> None:
        raise NotImplementedError("暂不支持歌单功能")
        
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list:
        return []
        
    async def get_library_playlists(self) -> list:
        return []
        
    async def browse(self, path: str | None = None) -> list:
        return []