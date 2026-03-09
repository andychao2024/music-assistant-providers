"""
GD Studio Music Provider for Music Assistant v1.1.26
播放歌曲时自动获取LRC格式歌词（支持多音源）
"""
from __future__ import annotations

import asyncio
import logging
import time
import struct
import re
from typing import TYPE_CHECKING, Dict, Optional, List, Tuple, Any, cast
from collections import defaultdict

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
    ConfigEntryType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ConfigValueOption,
)
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

__version__ = "1.1.26"
DOMAIN = "gd_studio_music"
API_BASE_URL = "https://music-api.gdstudio.xyz/api.php"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
API_TIMEOUT = aiohttp.ClientTimeout(total=15)

CONF_DEFAULT_SOURCE = "default_source"
CONF_AUDIO_QUALITY = "audio_quality"
CONF_IMAGE_SIZE = "image_size"

LRC_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]")
NON_STANDARD_LRC_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})\](?!\.)")
ENABLE_LYRICS = True
UPDATE_EXISTING_LYRICS = False

STABLE_SOURCES = [("网易云音乐", "netease"), ("酷我音乐", "kuwo"), ("JOOX", "joox")]
SOURCE_VALUES = [v for _, v in STABLE_SOURCES]
SOURCE_DISPLAY_MAP = {"netease": "网易云音乐", "kuwo": "酷我音乐", "joox": "JOOX"}

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

QUALITY_FALLBACK = {
    "999": ["999", "740"],
    "740": ["740", "320"],
    "320": ["320"],
    "192": ["192", "128"],
    "128": ["128"],
    "992": ["992", "320"],
    "1003": ["1003", "320"],
    "886": ["886", "320"],
}

RATE_LIMIT_DURATION = 300
RATE_LIMIT_MAX_REQUESTS = 150

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

_api_request_counter = defaultdict(int)
_track_cache = {}
_duration_cache = {}
_source_swap_cache = {}
_failed_track_cache = {}

def format_duration(seconds: int) -> str:
    return "00:00" if seconds <= 0 else f"{seconds // 60:02d}:{seconds % 60:02d}"

def calculate_audio_duration(data: bytes, bitrate: int, content_type: str, total_size: int = 0) -> int:
    try:
        if content_type == "flac" or content_type == "lossless":
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
                
                if block_type == 0 and offset + 4 + block_size <= len(data):
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
                        
                        return int(total_samples / sample_rate) if sample_rate > 0 and total_samples > 0 else 0
                    break
                
                offset += 4 + block_size
                if is_last:
                    break
            return 0
        else:
            bytes_per_second = (bitrate * 1000) // 8 or 40000
            use_size = total_size if total_size > 0 else len(data)
            return int(use_size / bytes_per_second)
    except Exception as e:
        _LOGGER.error(f"音频时长解析失败: {str(e)}")
        return 0

async def fetch_audio_duration(url: str, bitrate: int, content_type: str = "mp3") -> int:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            head_resp = await session.head(url, headers={'User-Agent': USER_AGENT}, allow_redirects=True)
            full_content_length = int(head_resp.headers.get('Content-Length', 0)) if head_resp.status == 200 else 0
            
            if full_content_length > 0:
                duration = calculate_audio_duration(b"", bitrate, content_type, full_content_length)
                if duration > 0:
                    return duration
            
            download_resp = await session.get(
                url, 
                headers={'User-Agent': USER_AGENT, 'Range': 'bytes=0-5242880'},
                timeout=aiohttp.ClientTimeout(total=5)
            )
            
            if download_resp.status not in [200, 206]:
                return 0
                
            total_size = int(download_resp.headers.get('Content-Length', 0))
            data = await download_resp.read()
            return calculate_audio_duration(data, bitrate, content_type, total_size)
                    
    except asyncio.TimeoutError:
        _LOGGER.error(f"时长获取超时 | URL: {url[:50]}")
    except Exception as e:
        _LOGGER.error(f"时长获取失败: {str(e)} | URL: {url[:50]}")
    
    return 0

def normalize_lrc(lrc_content: str) -> str:
    if not lrc_content:
        return ""
    
    normalized_lines = []
    for line in lrc_content.split("\n"):
        line = line.strip()
        if not line or line.startswith(("[ti:", "[ar:", "[al:", "[au:", "[by:")):
            continue
        
        if LRC_TIMESTAMP_PATTERN.match(line):
            normalized_lines.append(line)
        elif NON_STANDARD_LRC_PATTERN.match(line):
            match = NON_STANDARD_LRC_PATTERN.search(line)
            if match:
                minutes = match.group(1).zfill(2)
                seconds = match.group(2).zfill(2)
                lyric_content = NON_STANDARD_LRC_PATTERN.sub("", line).strip()
                normalized_lines.append(f"[{minutes}:{seconds}.000] {lyric_content}")
    
    return "\n".join(normalized_lines)

@use_cache(3600 * 24 * 14)
@throttle_with_retries
async def get_lyrics(self, track_id: str, source: str = "netease") -> str | None:
    params = {"types": "lyric", "source": source, "id": track_id}
    
    try:
        async with self.mass.http_session.get(
            API_BASE_URL, 
            params=params, 
            ssl=False, 
            timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status == 429:
                raise ResourceTemporarilyUnavailable("API限流", backoff_time=int(response.headers.get("Retry-After", 5)))
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable("服务器临时不可用", backoff_time=10)
            if response.status in (400, 401, 404):
                return None
            
            response.raise_for_status()
            data = cast("dict[str, Any]", await response.json())
            
            lrc_content = data.get("tlyric", "") or data.get("lyric", "")
            return normalize_lrc(lrc_content) if lrc_content else None
            
    except Exception as e:
        _LOGGER.error(f"获取歌词异常 | 曲目ID: {track_id} | 错误: {str(e)}")
        return None

async def update_track_lyrics(self, track: Track, track_id: str, source: str) -> None:
    if not ENABLE_LYRICS:
        return
    
    has_lyrics = track.metadata and (track.metadata.lyrics or track.metadata.lrc_lyrics)
    if has_lyrics and not UPDATE_EXISTING_LYRICS:
        return
    
    clean_track_id = track_id.split("_")[-1] if "_" in track_id else track_id
    lyrics = await self.get_lyrics(clean_track_id, source)
    
    if lyrics:
        if not track.metadata:
            track.metadata = MediaItemMetadata()
        track.metadata.lrc_lyrics = lyrics
        track.metadata.lyrics = lyrics

def check_rate_limit() -> tuple[bool, str]:
    now = time.time()
    
    for timestamp in list(_api_request_counter.keys()):
        if timestamp < now - RATE_LIMIT_DURATION:
            del _api_request_counter[timestamp]
    
    total_requests = sum(_api_request_counter.values())
    if total_requests >= RATE_LIMIT_MAX_REQUESTS:
        tip_msg = f"请求频繁，请1分钟后再试（5分钟内最多{RATE_LIMIT_MAX_REQUESTS}次）"
        _LOGGER.warning(f"限流触发 | {tip_msg}")
        return False, tip_msg
    
    _api_request_counter[int(now // 60) * 60] += 1
    return True, ""

def get_full_track_id(raw_id: str, source: str = None) -> str:
    source = source or "netease"
    return str(raw_id) if "_" in str(raw_id) else f"{source}_{raw_id}"

def create_metadata(source: str, duration: int, pic_url: str = None) -> MediaItemMetadata:
    metadata = MediaItemMetadata()
    source_display = SOURCE_DISPLAY_MAP.get(source, source)
    
    metadata.genres = {source_display}
    metadata.extra = {
        "duration_seconds": duration,
        "duration_formatted": format_duration(duration),
        "source": source,
        "tags": [source_display]
    }
    
    if pic_url:
        metadata.images = UniqueList()
        metadata.images.append(MediaItemImage(
            type=ImageType.THUMB, 
            path=pic_url, 
            provider=DOMAIN,
            remotely_accessible=True
        ))
    
    return metadata

class GDStudioMusicProvider(MusicProvider):
    throttler: ThrottlerManager
    
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
        
        self._default_source = self._default_source if self._default_source in SOURCE_VALUES else "joox"
        self._image_size = self._image_size if self._image_size in ["300", "500"] else "300"
        self._br_param = QUALITY_MAPPING.get(self._audio_quality, "320")
        
        self.throttler = ThrottlerManager(rate_limit=5, period=1)
        self.get_lyrics = get_lyrics.__get__(self)
        self.update_track_lyrics = update_track_lyrics.__get__(self)
        
        self._session = aiohttp.ClientSession(
            timeout=API_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            connector=aiohttp.TCPConnector(ssl=False, limit=5),
            trust_env=True
        )
        
        _LOGGER.info(f"GD Studio Music v{__version__} 初始化完成")

    async def handle_async_stop(self) -> None:
        if hasattr(self, "_session") and not self._session.closed:
            try:
                await asyncio.wait_for(self._session.close(), timeout=5)
            except Exception as e:
                _LOGGER.warning(f"关闭会话失败: {e}")
        
        _api_request_counter.clear()

    async def _api_request(self, api_type: str, params: dict) -> tuple[dict | list, str]:
        allow_request, tip_msg = check_rate_limit()
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
                return pic_url if pic_url.startswith(("http://", "https://")) else None
        except Exception as e:
            _LOGGER.error(f"获取封面失败: {e}")
        
        return None

    async def _get_stream_url(self, track_id: str, initial_source: str) -> tuple[Optional[str], Optional[str], Optional[str], str]:
        start_time = time.time()
        target_br = self._br_param
        cache_key = f"{track_id}_{target_br}"
        fail_key = f"{track_id}_{initial_source}"
        
        if fail_key in _failed_track_cache:
            if _failed_track_cache[fail_key] > time.time():
                return None, None, None, "该歌曲暂时无法播放，请1分钟后再试"
            del _failed_track_cache[fail_key]
        
        if cache_key in _source_swap_cache:
            cached = _source_swap_cache[cache_key]
            if cached.get("expire", 0) > time.time():
                return cached["url"], cached["br"], cached["source"], ""
            del _source_swap_cache[cache_key]
        
        source_list = [initial_source] if initial_source in SOURCE_VALUES else []
        source_list.extend([s for s in SOURCE_VALUES if s not in source_list])
        quality_list = QUALITY_FALLBACK.get(target_br, ["320"])
        
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
            
            if tip_msg or not isinstance(url_data, dict):
                continue
            
            if url_data.get("url") and url_data.get("url").strip() and url_data.get("br", -1) != -1:
                url = url_data["url"].strip()
                actual_br = str(url_data.get("br", br))
                
                _source_swap_cache[cache_key] = {
                    "url": url,
                    "br": actual_br,
                    "source": source,
                    "expire": time.time() + 3600
                }
                
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
                
                if tip_msg or not isinstance(url_data, dict):
                    continue
                
                if url_data.get("url") and url_data.get("url").strip() and url_data.get("br", -1) != -1:
                    url = url_data["url"].strip()
                    actual_br = str(url_data.get("br", br))
                    
                    _source_swap_cache[cache_key] = {
                        "url": url,
                        "br": actual_br,
                        "source": source,
                        "expire": time.time() + 3600
                    }
                    
                    return url, actual_br, source, ""
        
        _failed_track_cache[fail_key] = time.time() + 60
        return None, None, None, "该歌曲暂时无法获取播放链接，请稍后再试"

    async def search(self, search_query: str, media_types: list[MediaType], limit: int = 15) -> SearchResults:
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
        
        _LOGGER.debug(f"搜索请求 | 关键词={search_query} | 音源={self._default_source}")
        data, tip_msg = await self._api_request("search", search_params)
        
        if tip_msg or not isinstance(data, list):
            _LOGGER.warning(f"搜索失败 | {tip_msg or 'API返回非列表数据'}")
            return res
        
        tracks = []
        source_display = SOURCE_DISPLAY_MAP.get(self._default_source, self._default_source)
        
        for idx, item in enumerate(data[:15]):
            try:
                track_id_raw = item.get("id")
                if not track_id_raw:
                    continue
                    
                item_id = get_full_track_id(track_id_raw, self._default_source)
                name = item.get("name", "未知歌曲").strip()
                
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
                    "duration": 0,
                    "duration_formatted": format_duration(0),
                    "source": self._default_source,
                    "raw_duration": item.get("duration", 0)
                }

                pic_url = await self._fetch_pic_url(pic_id, self._default_source) if self._image_size and pic_id else None
                metadata = create_metadata(self._default_source, 0, pic_url)

                track = Track(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=name,
                    duration=0,
                    provider_mappings={
                        ProviderMapping(
                            item_id=str(track_id_raw), 
                            provider_domain=DOMAIN, 
                            provider_instance=self.instance_id
                        )
                    },
                    metadata=metadata
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

                tracks.append(track)
                
            except Exception as e:
                _LOGGER.error(f"解析搜索结果失败 | 索引={idx} | 错误={str(e)}")
                continue
        
        _LOGGER.debug(f"搜索完成 | 关键词={search_query} | 结果数={len(tracks)}")
        res.tracks = tracks
        return res

    async def get_track(self, prov_track_id: str, fallback_info: Optional[Dict] = None) -> Track:
        full_track_id = get_full_track_id(prov_track_id, self._default_source)
        
        if full_track_id in _track_cache:
            cached = _track_cache[full_track_id]
            name = cached["name"]
            artist = cached["artist"]
            album = cached["album"]
            duration = _duration_cache.get(full_track_id, cached["duration"]) or _duration_cache.get(prov_track_id, 0)
            pic_id = cached["pic_id"]
            source = cached["source"]
        else:
            source = self._default_source
            if "_" in full_track_id:
                source, rid = full_track_id.split("_", 1)
            source = source if source in SOURCE_VALUES else self._default_source
            
            name = fallback_info.get("name", full_track_id[:8]) if fallback_info else full_track_id[:8]
            artist = fallback_info.get("artist", "未知艺术家") if fallback_info else "未知艺术家"
            album = fallback_info.get("album", "未知专辑") if fallback_info else "未知专辑"
            duration = _duration_cache.get(full_track_id, _duration_cache.get(prov_track_id, 0))
            pic_id = ""

        pic_url = await self._fetch_pic_url(pic_id, source) if pic_id else None
        metadata = create_metadata(source, duration, pic_url)

        track = Track(
            item_id=full_track_id,
            provider=self.instance_id,
            name=name,
            duration=duration,
            provider_mappings={
                ProviderMapping(
                    item_id=full_track_id.split("_")[-1], 
                    provider_domain=DOMAIN, 
                    provider_instance=self.instance_id
                )
            },
            metadata=metadata
        )
        
        if ENABLE_LYRICS:
            asyncio.create_task(self.update_track_lyrics(track, full_track_id, source))
        
        track.artists.append(ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=f"{source}_artist_{artist}",
            provider=self.instance_id,
            name=artist
        ))
        
        track.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=f"{source}_album_{album}",
            provider=self.instance_id,
            name=album
        )
        
        return track

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        if media_type != MediaType.TRACK or not item_id:
            raise ValueError("仅支持单曲播放")
            
        source = self._default_source
        track_id = item_id
        if "_" in item_id:
            source, track_id = item_id.split("_", 1)
        source = source if source in SOURCE_VALUES else self._default_source

        url, br_str, success_source, tip_msg = await self._get_stream_url(track_id, source)
        if not url:
            raise ValueError(tip_msg or f"无法获取播放链接 | 曲目ID={item_id}")
        
        try:
            actual_br = int(br_str)
        except ValueError:
            actual_br = 320
            _LOGGER.warning(f"无法解析比特率: {br_str}，使用默认320 kbps")
        
        full_item_id = get_full_track_id(item_id, source)
        track_duration = _duration_cache.get(full_item_id, _duration_cache.get(item_id, 0))
        
        if track_duration <= 0:
            content_type = "flac" if br_str in ["740", "999"] else "mp3"
            if "908" in br_str or "992" in br_str or "700" in br_str or "1003" in br_str or "886" in br_str:
                content_type = "mp3"
            
            track_duration = await fetch_audio_duration(url, actual_br, content_type)
            
            if track_duration <= 0:
                track_duration = _track_cache.get(full_item_id, {}).get("raw_duration", 240) or 240
        
        if full_item_id not in _duration_cache:
            _duration_cache[full_item_id] = track_duration
            _duration_cache[item_id] = track_duration
            if full_item_id in _track_cache:
                _track_cache[full_item_id]["duration"] = track_duration
                _track_cache[full_item_id]["duration_formatted"] = format_duration(track_duration)
        
        try:
            updated_track = await self.get_track(full_item_id)
            if updated_track.duration != track_duration:
                _LOGGER.warning(f"Track.duration更新后值不匹配 | 预期={track_duration} | 实际={updated_track.duration}")
        except Exception as e:
            _LOGGER.warning(f"更新Track.duration异常: {str(e)}")
        
        content_type = CONTENT_TYPE_MAPPING.get(br_str, ContentType.MP3)
        try:
            audio_format = AudioFormat(
                content_type=content_type,
                sample_rate=48000 if content_type == ContentType.FLAC else 44100,
                bit_depth=24 if content_type == ContentType.FLAC else 16,
                bit_rate=actual_br,
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
            "bitrate": actual_br,
            "source": success_source
        }
        
        _LOGGER.debug(f"播放流构建完成 | 曲目={full_item_id} | 音源={success_source} | 音质={br_str}k | 时长={format_duration(track_duration)}")
        return stream_details

    async def get_album(self, prov_album_id: str) -> Album:
        source = self._default_source
        album_name = "未知专辑"
        
        if "_album_" in prov_album_id:
            parts = prov_album_id.split("_album_", 1)
            source = parts[0].split("_")[0] if len(parts) > 0 and "_" in parts[0] else self._default_source
            album_name = parts[1] if len(parts) > 1 else "未知专辑"
        
        source = source if source in SOURCE_VALUES else self._default_source

        metadata = create_metadata(source, 0)
        
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
            },
            metadata=metadata
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
            album.metadata.images = UniqueList()
            album.metadata.images.append(MediaItemImage(
                type=ImageType.THUMB,
                path=cover_url,
                provider=self.instance_id,
                remotely_accessible=True,
            ))

        return album

    async def get_artist(self, prov_artist_id: str) -> Artist:
        source = self._default_source
        artist_name = "未知艺术家"
        
        if "_artist_" in prov_artist_id:
            parts = prov_artist_id.split("_artist_", 1)
            source = parts[0].split("_")[0] if len(parts) > 0 and "_" in parts[0] else self._default_source
            artist_name = parts[1] if len(parts) > 1 else "未知艺术家"
        
        source = source if source in SOURCE_VALUES else self._default_source

        metadata = create_metadata(source, 0)
        
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
            },
            metadata=metadata
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
            artist.metadata.images = UniqueList()
            artist.metadata.images.append(MediaItemImage(
                type=ImageType.THUMB,
                path=cover_url,
                provider=self.instance_id,
                remotely_accessible=True,
            ))

        return artist

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

async def setup(mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig) -> MusicProvider:
    _LOGGER.info(f"启动 GD Studio Music v{__version__}")
    return GDStudioMusicProvider(mass, manifest, config)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    return (
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