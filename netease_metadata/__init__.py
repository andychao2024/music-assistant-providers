"""云音乐元数据提供者 for Music Assistant UI (v1.9.8 发布版).
获取最新版本 https://gitee.com/andychao2020/music-assistant-providers
核心功能：优先从歌曲搜索结果提取专辑ID，精准匹配目标专辑 + 发行年份写入标签 + 精简日志
"""

from __future__ import annotations

import asyncio
from json import JSONDecodeError
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast, Dict, List, Optional, Set, Tuple
from datetime import datetime

import aiohttp.client_exceptions
from mashumaro import DataClassDictMixin

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
    ImageType,
    ProviderFeature,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Track,
    UniqueList,
)
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.json import json_loads
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# 常量定义
SUPPORTED_FEATURES: Set[ProviderFeature] = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
}

class ConfigKeys:
    ENABLE_ARTIST_METADATA = "enable_artist_metadata"
    ENABLE_ALBUM_METADATA = "enable_album_metadata"
    ENABLE_TRACK_METADATA = "enable_track_metadata"
    ENABLE_IMAGES = "enable_images"
    API_URL = "api_url"

# 正则表达式
import re
SOUNDTRACK_SUFFIX_PATTERN = re.compile(r'\s*(电视原声带|电影原声带|原声大碟|OST|Original Soundtrack)\s*', re.IGNORECASE)
COMMON_SUFFIX_PATTERN = re.compile(r'\s*\([^)]*\)|\s*\[[^]]*\]|\s*-\s*.*$')

# 配置参数
ARTIST_NAME_SEPARATORS = ['/', '\\', '|', ',', '；', ';', '+']
CACHE_TTL = 86400 * 7
API_RATE_LIMIT = 2
API_RATE_PERIOD = 1
ALBUM_IMAGE_PARAM = "?param=500y500"

# 数据模型定义
@dataclass
class CloudMusicArtistDetail(DataClassDictMixin):
    id: int
    name: str
    cover: Optional[str] = None
    avatar: Optional[str] = None
    briefDesc: Optional[str] = None
    musicBrainzId: Optional[str] = None

@dataclass
class CloudMusicArtist(DataClassDictMixin):
    id: int
    name: str
    picUrl: Optional[str] = None
    genre: Optional[str] = None
    briefDesc: Optional[str] = None
    musicBrainzId: Optional[str] = None

@dataclass
class CloudMusicAlbum(DataClassDictMixin):
    id: int
    name: str
    picUrl: Optional[str] = None
    publishTime: Optional[int] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    musicBrainzId: Optional[str] = None

@dataclass
class CloudMusicTrack(DataClassDictMixin):
    id: int
    name: str
    ar: List[Dict[str, Any]] = field(default_factory=list)
    al: Dict[str, Any] = field(default_factory=dict)
    lyric: Optional[str] = None
    genre: Optional[str] = None
    description: Optional[str] = None

# 工具函数
def clean_artist_name(artist_name: Optional[str]) -> str:
    if not artist_name:
        return ""
    
    cleaned_name = artist_name.strip()
    for sep in ARTIST_NAME_SEPARATORS:
        if sep in cleaned_name:
            cleaned_name = cleaned_name.split(sep)[0].strip()
            break
    
    return ' '.join(cleaned_name.split())

def parse_timestamp(timestamp_ms: Optional[int]) -> Optional[int]:
    if not timestamp_ms:
        return None
    
    try:
        return datetime.fromtimestamp(timestamp_ms // 1000).year
    except (ValueError, TypeError):
        return None

def fix_album_image_url(pic_url: Optional[str]) -> Optional[str]:
    if not pic_url:
        return None
    if ALBUM_IMAGE_PARAM not in pic_url:
        return f"{pic_url}{ALBUM_IMAGE_PARAM}"
    return pic_url

def simplify_album_name(album_name: str) -> Tuple[str, str]:
    simplified = COMMON_SUFFIX_PATTERN.sub('', album_name)
    core_name = simplified
    simplified = SOUNDTRACK_SUFFIX_PATTERN.sub('', simplified)
    return simplified.strip(), core_name.strip()

def calculate_album_match_score(target_name: str, candidate_name: str, target_artist: str, candidate_artist: str) -> float:
    score = 0.0
    
    if candidate_name.lower() == target_name.lower():
        score += 60
    elif simplify_album_name(candidate_name)[0].lower() == simplify_album_name(target_name)[0].lower():
        score += 40
    elif simplify_album_name(target_name)[0].lower() in candidate_name.lower():
        score += 20
    
    if SOUNDTRACK_SUFFIX_PATTERN.search(target_name) and SOUNDTRACK_SUFFIX_PATTERN.search(candidate_name):
        score += 20
    
    target_artist_lower = target_artist.lower()
    candidate_artist_lower = candidate_artist.lower()
    if target_artist_lower and (target_artist_lower in candidate_artist_lower or "群星" in candidate_artist_lower):
        score += 20
    
    return min(score, 100.0)

# 核心Provider类
class CloudMusicMetadataProvider(MetadataProvider):
    throttler: ThrottlerManager
    
    api_url: str
    enable_artist_metadata: bool
    enable_album_metadata: bool
    enable_track_metadata: bool
    enable_images: bool

    async def handle_async_init(self) -> None:
        self.cache = self.mass.cache
        
        self.api_url = self.config.get_value(ConfigKeys.API_URL, "").rstrip("/")
        self.enable_artist_metadata = self.config.get_value(ConfigKeys.ENABLE_ARTIST_METADATA, True)
        self.enable_album_metadata = self.config.get_value(ConfigKeys.ENABLE_ALBUM_METADATA, True)
        self.enable_track_metadata = self.config.get_value(ConfigKeys.ENABLE_TRACK_METADATA, True)
        self.enable_images = self.config.get_value(ConfigKeys.ENABLE_IMAGES, True)
        
        self.throttler = ThrottlerManager(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
        
        if not self.api_url:
            self.logger.error("[云音乐元数据] API地址未配置，插件将无法正常工作")
        else:
            self.logger.info("[云音乐元数据] v1.9.8 发布版初始化完成")

    async def get_artist_metadata(self, artist: Artist) -> Optional[MediaItemMetadata]:
        if not self.enable_artist_metadata:
            return None
        
        cleaned_artist_name = clean_artist_name(artist.name)
        if not cleaned_artist_name:
            return None
        
        try:
            search_data = await self._get_data(
                endpoint="search",
                keywords=cleaned_artist_name,
                type=100,
                limit=1
            )
            
            if not search_data or not search_data.get("result", {}).get("artists"):
                self.logger.debug("[云音乐元数据] 艺术家匹配失败: %s (未找到相关结果)", cleaned_artist_name)
                return None
            
            artist_search_data = CloudMusicArtist.from_dict(search_data["result"]["artists"][0])
            
            detail_data = await self._get_data(
                endpoint="artist/detail",
                id=artist_search_data.id
            )
            
            artist_detail = None
            if detail_data and detail_data.get("code") == 200 and detail_data.get("data", {}).get("artist"):
                try:
                    artist_detail = CloudMusicArtistDetail.from_dict(detail_data["data"]["artist"])
                except Exception:
                    pass
            
            metadata = self._build_artist_metadata_v175(artist, artist_search_data, artist_detail)
            self.logger.debug("[云音乐元数据] 艺术家匹配成功: %s", cleaned_artist_name)
            return metadata
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 艺术家元数据获取失败: %s", cleaned_artist_name)
            return None

    async def _extract_album_id_from_songs(self, search_keywords: List[str], album_name: str, artist_name: str) -> Optional[str]:
        for idx, keyword in enumerate(search_keywords):
            search_data = await self._get_data(endpoint="search", keywords=keyword, type=1, limit=20)
            
            if not search_data or not search_data.get("result", {}).get("songs"):
                continue
            
            album_map: Dict[str, Dict[str, Any]] = {}
            for song in search_data["result"]["songs"]:
                album_info = song.get("album", {})
                album_id = str(album_info.get("id", ""))
                album_name_api = album_info.get("name", "")
                
                if album_id and album_name_api:
                    album_map[album_id] = album_info
            
            if not album_map:
                continue
            
            target_album_id = None
            max_score = 0.0
            for album_id, album_info in album_map.items():
                score = calculate_album_match_score(album_name, album_info.get("name", ""), 
                                                  artist_name, album_info.get("artist", {}).get("name", "群星"))
                
                if score > max_score:
                    max_score = score
                    target_album_id = album_id
            
            if target_album_id and max_score >= 50:
                self.logger.debug("[云音乐元数据] 专辑匹配成功: %s (从歌曲提取ID: %s, 匹配得分: %.1f)", 
                              album_name, target_album_id, max_score)
                return target_album_id
        
        self.logger.debug("[云音乐元数据] 专辑匹配失败: %s (歌曲搜索未找到有效专辑ID)", album_name)
        return None

    async def get_album_metadata(self, album: Album) -> Optional[MediaItemMetadata]:
        if not self.enable_album_metadata:
            return None
        
        original_artists = album.artists.copy() if album.artists else []
        original_album_name = f"{album.name} {album.version}" if album.version else album.name
        cleaned_album_name = original_album_name.strip()
        artist_name = clean_artist_name(original_artists[0].name) if original_artists else ""
        
        search_strategies = []
        if artist_name.strip().lower() != "various artists" and artist_name:
            search_strategies.append(f"{cleaned_album_name} {artist_name}")
        search_strategies.append(cleaned_album_name)
        simplified_name, _ = simplify_album_name(cleaned_album_name)
        if simplified_name:
            search_strategies.append(f"{simplified_name} 电视原声带")
        search_strategies = list(dict.fromkeys([s for s in search_strategies if s.strip()]))
        
        if not search_strategies:
            album.artists = original_artists
            return None
        
        try:
            target_album_id = await self._extract_album_id_from_songs(search_strategies, cleaned_album_name, artist_name)
            
            album_data_raw = {}
            if target_album_id:
                album_detail = await self._get_data(endpoint="album", id=target_album_id)
                if album_detail and album_detail.get("code") == 200:
                    album_data_raw = album_detail.get("album", {})
            else:
                search_data = None
                for keyword in search_strategies:
                    search_params = {"keywords": keyword, "type": 10, "limit": 10}
                    mbid = album.get_external_id(ExternalID.MB_RELEASEGROUP)
                    if mbid:
                        search_params["mbid"] = mbid
                    
                    search_data = await self._get_data(endpoint="search", **search_params)
                    if search_data and search_data.get("result", {}).get("albums"):
                        break
                
                if not search_data or not search_data.get("result", {}).get("albums"):
                    search_data = await self._get_data(endpoint="search", keywords=cleaned_album_name, type=10, limit=10)
                    if not search_data or not search_data.get("result", {}).get("albums"):
                        self.logger.debug("[云音乐元数据] 专辑匹配失败: %s (专辑搜索无结果)", cleaned_album_name)
                        album.artists = original_artists
                        return None
                
                albums = search_data["result"]["albums"]
                scored_albums = [(item, calculate_album_match_score(cleaned_album_name, item.get("name", ""), 
                                                                   artist_name, item.get("artist", {}).get("name", "群星"))) 
                               for item in albums]
                scored_albums.sort(key=lambda x: x[1], reverse=True)
                best_album, max_score = scored_albums[0]
                
                if max_score < 30:
                    self.logger.debug("[云音乐元数据] 专辑匹配失败: %s (匹配得分过低: %.1f)", cleaned_album_name, max_score)
                    album.artists = original_artists
                    return None
                
                album_id = best_album.get("id")
                if not album_id:
                    album.artists = original_artists
                    return None
                
                self.logger.debug("[云音乐元数据] 专辑匹配成功: %s (专辑搜索ID: %s, 匹配得分: %.1f)", 
                              cleaned_album_name, album_id, max_score)
                
                album_detail = await self._get_data(endpoint="album", id=album_id)
                album_data_raw = album_detail.get("album", best_album) if (album_detail and album_detail.get("code") == 200) else best_album
            
            return await self._build_album_metadata_from_raw(album, album_data_raw, original_artists)
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 专辑元数据获取失败: %s", cleaned_album_name)
            album.artists = original_artists
            return None

    async def _build_album_metadata_from_raw(self, album: Album, album_data_raw: Dict[str, Any], original_artists: List[Artist]) -> Optional[MediaItemMetadata]:
        if not album_data_raw:
            return None
        
        album_data = CloudMusicAlbum.from_dict(album_data_raw)
        album_data.picUrl = fix_album_image_url(album_data_raw.get("picUrl") or album_data.picUrl)
        
        album_desc = album_data_raw.get("description", "").strip()
        album_data.description = album_desc
        
        tag_list = []
        release_year = parse_timestamp(album_data.publishTime)
        if release_year:
            tag_list.append(f"发行年份:{release_year}")
            if not album.year:
                album.year = release_year
        
        if album_data_raw.get("tags") and album_data_raw.get("tags").strip():
            tag_list.extend([t.strip() for t in album_data_raw.get("tags").split(',') if t.strip()])
        if album_data_raw.get("subType") and album_data_raw.get("subType").strip():
            tag_list.append(album_data_raw.get("subType").strip())
        if album_data_raw.get("company") and album_data_raw.get("company").strip():
            tag_list.append(f"{album_data_raw.get('company').strip()}")
        
        album_data.tags = ','.join(list(set(tag_list))) if tag_list else None
        
        metadata = MediaItemMetadata()
        metadata.description = album_data.description or ""
        if album_data.tags:
            metadata.genres = set(album_data.tags.split(','))
        if self.enable_images and album_data.picUrl:
            metadata.images = UniqueList()
            metadata.images.append(
                MediaItemImage(type=ImageType.THUMB, path=album_data.picUrl, provider=self.instance_id, remotely_accessible=True)
            )
        
        if album_data.musicBrainzId and not album.get_external_id(ExternalID.MB_RELEASEGROUP) and album.provider == "library":
            album.add_external_id(ExternalID.MB_RELEASEGROUP, album_data.musicBrainzId)
            asyncio.create_task(self.mass.music.albums.update_item_in_library(album.item_id, album))
        
        album.artists = original_artists
        if not album.artists and album_data_raw.get("artist", {}).get("name"):
            artist_from_api = Artist(
                name=album_data_raw.get("artist", {}).get("name"),
                provider="library",
                item_id=f"api_{album_data_raw.get('artist', {}).get('id', '0')}"
            )
            album.artists = [artist_from_api]
        
        return metadata

    async def get_track_metadata(self, track: Track) -> Optional[MediaItemMetadata]:
        if not self.enable_track_metadata:
            return None
        
        original_track_name = f"{track.name} {track.version}" if track.version else track.name
        cleaned_track_name = original_track_name.strip()
        artist_name = ""
        
        for track_artist in track.artists:
            artist_name = clean_artist_name(track_artist.name)
        
        search_keyword = f"{cleaned_track_name} {artist_name}".strip()
        if not search_keyword:
            return None
        
        try:
            search_params = {"keywords": search_keyword, "type": 1, "limit": 1}
            if track.mbid:
                search_params["mbid"] = track.mbid
            
            data = await self._get_data(endpoint="search", **search_params)
            if not data or not data.get("result", {}).get("songs"):
                self.logger.debug("[云音乐元数据] 歌曲匹配失败: %s (未找到相关结果)", search_keyword)
                return None
            
            track_data = CloudMusicTrack.from_dict(data["result"]["songs"][0])
            if "picUrl" in track_data.al:
                track_data.al["picUrl"] = fix_album_image_url(track_data.al["picUrl"])
            
            metadata = self._build_track_metadata(track, track_data)
            self.logger.debug("[云音乐元数据] 歌曲匹配成功: %s", search_keyword)
            return metadata
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 歌曲元数据获取失败: %s", search_keyword)
            return None

    def _has_valid_artist_images(self, artist: Artist) -> bool:
        if not self.enable_images:
            return False
        if not hasattr(artist, 'images') or not artist.images:
            return False
        valid_images = [img for img in artist.images if img.path and img.type == ImageType.THUMB]
        return len(valid_images) > 0

    @use_cache(CACHE_TTL, persistent=True)
    @throttle_with_retries
    async def _get_data(self, endpoint: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if not self.api_url:
            return None
        
        url = f"{self.api_url}/{endpoint}"
        try:
            async with self.mass.http_session.get(
                url, params=kwargs, ssl=False, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 429:
                    backoff_time = int(response.headers.get("Retry-After", 5))
                    raise ResourceTemporarilyUnavailable("API限流", backoff_time=backoff_time)
                if response.status in (502, 503):
                    raise ResourceTemporarilyUnavailable("服务器临时不可用", backoff_time=10)
                if response.status in (400, 401, 404):
                    return None
                response.raise_for_status()
                return cast(Dict[str, Any], await response.json(loads=json_loads))
        except ResourceTemporarilyUnavailable:
            raise
        except Exception:
            return None

    def _build_artist_metadata_v175(self, artist: Artist, artist_search: CloudMusicArtist, artist_detail: Optional[CloudMusicArtistDetail] = None) -> MediaItemMetadata:
        metadata = MediaItemMetadata()
        use_detail = artist_detail is not None

        if use_detail and artist_detail.briefDesc:
            metadata.description = artist_detail.briefDesc.strip()
        elif artist_search.briefDesc:
            metadata.description = artist_search.briefDesc.strip()
        else:
            metadata.description = ""

        metadata.genres = {artist_search.genre} if artist_search.genre else set()
        
        if self.enable_images:
            metadata.images = UniqueList()
            pic_url = artist_detail.avatar or artist_detail.cover if use_detail else artist_search.picUrl
            if pic_url:
                metadata.images.append(
                    MediaItemImage(type=ImageType.THUMB, path=pic_url, provider=self.instance_id, remotely_accessible=True)
                )
        
        mbid = artist_detail.musicBrainzId if use_detail else artist_search.musicBrainzId
        if mbid and not artist.mbid and artist.provider == "library":
            if isinstance(artist, ItemMapping):
                artist = self.mass.music.artists.artist_from_item_mapping(artist)
            artist.mbid = mbid
            asyncio.create_task(self.mass.music.artists.update_item_in_library(artist.item_id, artist))
        
        return metadata

    def _build_track_metadata(self, track: Track, track_data: CloudMusicTrack) -> MediaItemMetadata:
        metadata = MediaItemMetadata()
        metadata.lyrics = track_data.lyric or ""
        metadata.genres = {track_data.genre} if track_data.genre else set()
        metadata.description = track_data.description or ""
        
        if self.enable_images and (pic_url := track_data.al.get("picUrl")):
            metadata.images = UniqueList()
            metadata.images.append(
                MediaItemImage(type=ImageType.THUMB, path=fix_album_image_url(pic_url), provider=self.instance_id, remotely_accessible=True)
            )
        
        artist_mbid = track_data.ar[0].get("musicBrainzId") if track_data.ar else None
        if artist_mbid:
            for track_artist in track.artists:
                if not track_artist.mbid and track_artist.provider == "library":
                    if isinstance(track_artist, ItemMapping):
                        track_artist = self.mass.music.artists.artist_from_item_mapping(track_artist)
                    track_artist.mbid = artist_mbid
                    asyncio.create_task(self.mass.music.artists.update_item_in_library(track_artist.item_id, track_artist))
        
        return metadata

# 插件初始化
async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    return CloudMusicMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: Optional[str] = None,
    action: Optional[str] = None,
    values: Optional[Dict[str, ConfigValueType]] = None,
) -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(
            key=ConfigKeys.ENABLE_ARTIST_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用艺术家元数据获取",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_ALBUM_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用专辑元数据获取",
            default_value=True,
            required=False,
            description="v1.9.8 发布版：优先从歌曲提取专辑ID + 发行年份写入标签",
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_TRACK_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用歌曲元数据获取",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_IMAGES,
            type=ConfigEntryType.BOOLEAN,
            label="启用图片获取",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.API_URL,
            type=ConfigEntryType.STRING,
            label="自建云音乐API地址",
            description="你的自建云音乐API服务地址（如http://localhost:3003）",
            required=True,
            default_value="http://localhost:3003",
        ),
    )