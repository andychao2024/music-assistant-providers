"""云音乐元数据提供者 for Music Assistant UI (v1.7.5 精简版)."""

from __future__ import annotations

import os
import asyncio
from json import JSONDecodeError
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast, Dict, List, Optional, Set
from datetime import datetime
from contextlib import suppress

import aiohttp.client_exceptions
import mutagen
from mutagen.id3 import ID3, APIC, TPE1, TALB, TIT2, TPE2, error
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture
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

# ------------------------------
# 常量定义
# ------------------------------
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
    WRITE_TAGS = "write_tags_to_file"
    API_URL = "api_url"
    AUTO_TRIGGER_NO_IMAGE = "auto_trigger_no_image"

IMG_MAPPING: Dict[str, ImageType] = {
    "artist_picUrl": ImageType.THUMB,
    "album_picUrl": ImageType.THUMB,
    "track_picUrl": ImageType.THUMB,
}

ARTIST_NAME_SEPARATORS = ['/', '\\', '|', ',', '；', ';', '+']
CACHE_TTL = 86400 * 7

# ------------------------------
# 数据模型定义
# ------------------------------
@dataclass
class CloudMusicArtistDetail(DataClassDictMixin):
    id: int
    name: str
    cover: Optional[str] = None
    avatar: Optional[str] = None
    transNames: List[str] = field(default_factory=list)
    alias: List[str] = field(default_factory=list)
    identities: List[str] = field(default_factory=list)
    briefDesc: Optional[str] = None
    albumSize: Optional[int] = None
    musicSize: Optional[int] = None
    mvSize: Optional[int] = None
    musicBrainzId: Optional[str] = None

@dataclass
class CloudMusicArtist(DataClassDictMixin):
    id: int
    name: str
    picUrl: Optional[str] = None
    genre: Optional[str] = None
    briefDesc: Optional[str] = None
    desc: Optional[str] = None
    musicBrainzId: Optional[str] = None

@dataclass
class CloudMusicAlbum(DataClassDictMixin):
    id: int
    name: str
    picUrl: Optional[str] = None
    publishTime: Optional[int] = None
    genre: Optional[str] = None
    description: Optional[str] = None
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

# ------------------------------
# 工具函数
# ------------------------------
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

# ------------------------------
# 核心Provider类
# ------------------------------
class CloudMusicMetadataProvider(MetadataProvider):
    throttler = ThrottlerManager(rate_limit=10, period=1)
    
    api_url: str
    write_tags: bool
    auto_trigger_no_image: bool
    enable_artist_metadata: bool
    enable_album_metadata: bool
    enable_track_metadata: bool
    enable_images: bool

    async def handle_async_init(self) -> None:
        """异步初始化"""
        self.cache = self.mass.cache
        
        # 读取配置
        self.api_url = self.config.get_value(ConfigKeys.API_URL, "").rstrip("/")
        self.write_tags = self.config.get_value(ConfigKeys.WRITE_TAGS, False)
        self.auto_trigger_no_image = self.config.get_value(ConfigKeys.AUTO_TRIGGER_NO_IMAGE, True)
        self.enable_artist_metadata = self.config.get_value(ConfigKeys.ENABLE_ARTIST_METADATA, True)
        self.enable_album_metadata = self.config.get_value(ConfigKeys.ENABLE_ALBUM_METADATA, True)
        self.enable_track_metadata = self.config.get_value(ConfigKeys.ENABLE_TRACK_METADATA, True)
        self.enable_images = self.config.get_value(ConfigKeys.ENABLE_IMAGES, True)
        
        # 初始化日志
        if not self.api_url:
            self.logger.error("[云音乐元数据] API地址未配置，插件将无法正常工作")
        else:
            self.logger.info("[云音乐元数据] 初始化完成，API地址: %s", self.api_url)

    # ------------------------------
    # 艺术家元数据获取
    # ------------------------------
    async def get_artist_metadata(self, artist: Artist) -> Optional[MediaItemMetadata]:
        """获取艺术家元数据（纯净版）"""
        if not self.enable_artist_metadata:
            return None
        
        cleaned_artist_name = clean_artist_name(artist.name)
        if not cleaned_artist_name:
            return None
        
        # 检查图片状态
        if self._has_valid_artist_images(artist) and not self.auto_trigger_no_image:
            return None
        
        try:
            # 搜索艺术家
            search_data = await self._get_data(
                endpoint="search",
                keywords=cleaned_artist_name,
                type=100,
                limit=1
            )
            
            if not search_data or not search_data.get("result", {}).get("artists"):
                return None
            
            artist_search_data = CloudMusicArtist.from_dict(search_data["result"]["artists"][0])
            
            # 获取详情
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
            
            # 构建元数据（纯净版）
            metadata = self._build_artist_metadata_v175(artist, artist_search_data, artist_detail)
            
            # 添加更新成功日志
            self.logger.info("[云音乐元数据] 成功更新艺术家元数据: %s", cleaned_artist_name)
            
            return metadata
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 获取艺术家元数据失败: %s, 错误: %s", cleaned_artist_name, str(e))
            return None

    # ------------------------------
    # 专辑元数据获取
    # ------------------------------
    async def get_album_metadata(self, album: Album) -> Optional[MediaItemMetadata]:
        """获取专辑元数据"""
        if not self.enable_album_metadata:
            return None
        
        # 构建搜索关键词
        original_album_name = f"{album.name} {album.version}" if album.version else album.name
        cleaned_album_name = original_album_name.strip()
        artist_name = ""
        
        if album.artists:
            artist_name = clean_artist_name(album.artists[0].name)
        
        # Various Artists特殊处理
        search_keyword = cleaned_album_name
        if artist_name.strip().lower() != "various artists" and artist_name:
            search_keyword = f"{cleaned_album_name} {artist_name}"
        
        if not search_keyword:
            return None
        
        try:
            # 构建搜索参数
            search_params = {
                "keywords": search_keyword,
                "type": 10,
                "limit": 3
            }
            
            # 添加MBID
            mbid = album.get_external_id(ExternalID.MB_RELEASEGROUP)
            if mbid:
                search_params["mbid"] = mbid
            
            # 获取数据
            data = await self._get_data(endpoint="search",** search_params)
            
            if not data or not data.get("result", {}).get("albums"):
                return None
            
            # 解析数据
            album_data = CloudMusicAlbum.from_dict(data["result"]["albums"][0])
            
            # 构建元数据
            metadata = self._build_album_metadata(album, album_data)
            
            # 批量写入标签
            if self.write_tags and hasattr(album, "tracks") and album.tracks:
                asyncio.create_task(self._batch_write_album_tags(album, album_data))
            
            return metadata
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 获取专辑元数据失败: %s, 错误: %s", search_keyword, str(e))
            return None

    # ------------------------------
    # 歌曲元数据获取
    # ------------------------------
    async def get_track_metadata(self, track: Track) -> Optional[MediaItemMetadata]:
        """获取歌曲元数据"""
        if not self.enable_track_metadata:
            return None
        
        # 构建搜索关键词
        original_track_name = f"{track.name} {track.version}" if track.version else track.name
        cleaned_track_name = original_track_name.strip()
        artist_name = ""
        
        for track_artist in track.artists:
            artist_name = clean_artist_name(track_artist.name)
        
        search_keyword = f"{cleaned_track_name} {artist_name}".strip()
        if not search_keyword:
            return None
        
        try:
            # 构建搜索参数
            search_params = {
                "keywords": search_keyword,
                "type": 1,
                "limit": 1
            }
            
            if track.mbid:
                search_params["mbid"] = track.mbid
            
            # 获取数据
            data = await self._get_data(endpoint="search", **search_params)
            
            if not data or not data.get("result", {}).get("songs"):
                # 降级写入基础标签
                if self.write_tags and hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
                    album_name = track.album.name if (hasattr(track, "album") and track.album) else ""
                    await self._write_basic_tags(track.file_path, artist_name, album_name, cleaned_track_name)
                return None
            
            # 解析数据
            track_data = CloudMusicTrack.from_dict(data["result"]["songs"][0])
            
            # 构建元数据
            metadata = self._build_track_metadata(track, track_data)
            
            # 写入完整标签
            if self.write_tags and hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
                api_artist_name = track_data.ar[0].get("name", artist_name) if track_data.ar else artist_name
                cleaned_artist = clean_artist_name(api_artist_name)
                album_name = track_data.al.get("name", "")
                cover_url = track_data.al.get("picUrl", "")
                
                await self._write_track_tags(
                    file_path=track.file_path,
                    artist_name=cleaned_artist,
                    album_name=album_name,
                    track_name=track_data.name,
                    cover_url=cover_url
                )
            
            return metadata
            
        except Exception as e:
            self.logger.error("[云音乐元数据] 获取歌曲元数据失败: %s, 错误: %s", search_keyword, str(e))
            return None

    # ------------------------------
    # 私有方法
    # ------------------------------
    def _has_valid_artist_images(self, artist: Artist) -> bool:
        """检查艺术家是否有有效图片"""
        if not self.enable_images:
            return False
        
        if not hasattr(artist, 'images') or not artist.images:
            return False
        
        valid_images = [img for img in artist.images if img.path and img.type == ImageType.THUMB]
        return len(valid_images) > 0

    @use_cache(CACHE_TTL, persistent=True)
    @throttle_with_retries
    async def _get_data(self, endpoint: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """获取API数据"""
        if not self.api_url:
            return None
        
        url = f"{self.api_url}/{endpoint}"
        
        try:
            async with self.mass.http_session.get(
                url, 
                params=kwargs, 
                ssl=False, 
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                # 处理限流
                if response.status == 429:
                    backoff_time = int(response.headers.get("Retry-After", 5))
                    raise ResourceTemporarilyUnavailable("API限流", backoff_time=backoff_time)
                
                # 处理临时错误
                if response.status in (502, 503):
                    raise ResourceTemporarilyUnavailable("服务器临时不可用", backoff_time=10)
                
                # 处理客户端错误
                if response.status in (400, 401, 404):
                    return None
                
                response.raise_for_status()
                return cast(Dict[str, Any], await response.json(loads=json_loads))
                
        except ResourceTemporarilyUnavailable:
            raise
        except Exception:
            return None

    def _build_artist_metadata_v175(self, artist: Artist, artist_search: CloudMusicArtist, artist_detail: Optional[CloudMusicArtistDetail] = None) -> MediaItemMetadata:
        """构建艺术家元数据（纯净版：移除别名+去掉“歌手介绍”标题）"""
        metadata = MediaItemMetadata()
        use_detail = artist_detail is not None

        # 仅保留纯净的歌手简介
        if use_detail and artist_detail.briefDesc:
            metadata.description = artist_detail.briefDesc.strip()
        elif artist_search.briefDesc or artist_search.desc:
            metadata.description = (artist_search.briefDesc or artist_search.desc or "").strip()
        else:
            metadata.description = ""

        # 流派信息
        metadata.genres = {artist_search.genre} if artist_search.genre else set()
        
        # 图片
        if self.enable_images:
            metadata.images = UniqueList()
            pic_url = None
            
            if use_detail:
                pic_url = artist_detail.avatar or artist_detail.cover
            if not pic_url:
                pic_url = artist_search.picUrl
            
            if pic_url:
                metadata.images.append(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=pic_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        
        # 更新MBID
        mbid = None
        if use_detail and artist_detail.musicBrainzId:
            mbid = artist_detail.musicBrainzId
        elif artist_search.musicBrainzId:
            mbid = artist_search.musicBrainzId
        
        if mbid and not artist.mbid and artist.provider == "library":
            if isinstance(artist, ItemMapping):
                artist = self.mass.music.artists.artist_from_item_mapping(artist)
            
            artist.mbid = mbid
            asyncio.create_task(self.mass.music.artists.update_item_in_library(artist.item_id, artist))
        
        return metadata

    def _build_album_metadata(self, album: Album, album_data: CloudMusicAlbum) -> MediaItemMetadata:
        """构建专辑元数据"""
        metadata = MediaItemMetadata()
        
        # 基础元数据
        metadata.genres = {album_data.genre} if album_data.genre else set()
        metadata.description = album_data.description or ""
        
        # 图片
        if self.enable_images and album_data.picUrl:
            metadata.images = UniqueList()
            metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_data.picUrl,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        
        # 年份
        year = parse_timestamp(album_data.publishTime)
        if not album.year and year:
            album.year = year
        
        # 更新MBID
        if album_data.musicBrainzId and not album.get_external_id(ExternalID.MB_RELEASEGROUP) and album.provider == "library":
            album.add_external_id(ExternalID.MB_RELEASEGROUP, album_data.musicBrainzId)
            asyncio.create_task(self.mass.music.albums.update_item_in_library(album.item_id, album))
        
        return metadata

    def _build_track_metadata(self, track: Track, track_data: CloudMusicTrack) -> MediaItemMetadata:
        """构建歌曲元数据"""
        metadata = MediaItemMetadata()
        
        # 基础元数据
        metadata.lyrics = track_data.lyric or ""
        metadata.genres = {track_data.genre} if track_data.genre else set()
        metadata.description = track_data.description or ""
        
        # 图片
        if self.enable_images and (pic_url := track_data.al.get("picUrl")):
            metadata.images = UniqueList()
            metadata.images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=pic_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        
        # 更新艺术家MBID
        artist_mbid = track_data.ar[0].get("musicBrainzId") if track_data.ar else None
        if artist_mbid:
            for track_artist in track.artists:
                if not track_artist.mbid and track_artist.provider == "library":
                    if isinstance(track_artist, ItemMapping):
                        track_artist = self.mass.music.artists.artist_from_item_mapping(track_artist)
                    
                    track_artist.mbid = artist_mbid
                    asyncio.create_task(self.mass.music.artists.update_item_in_library(track_artist.item_id, track_artist))
        
        return metadata

    async def _batch_write_album_tags(self, album: Album, album_data: CloudMusicAlbum):
        """批量写入专辑标签"""
        if not album.tracks:
            return
        
        cover_url = album_data.picUrl or ""
        artist_name = clean_artist_name(album.artists[0].name) if album.artists else ""
        album_name = album_data.name or album.name
        
        # 构建任务列表
        tasks = []
        for track in album.tracks:
            if hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
                track_name = track.name or ""
                tasks.append(
                    self._write_track_tags(
                        file_path=track.file_path,
                        artist_name=artist_name,
                        album_name=album_name,
                        track_name=track_name,
                        cover_url=cover_url
                    )
                )
        
        # 执行任务
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            error_count = sum(1 for r in results if isinstance(r, Exception))
            
            if error_count > 0:
                self.logger.warning("[云音乐元数据] 专辑标签写入失败 %d/%d", error_count, len(tasks))

    async def _write_basic_tags(self, file_path: str, artist_name: str, album_name: str, track_name: str):
        """写入基础标签"""
        try:
            if file_path.lower().endswith(".mp3"):
                audio = MP3(file_path, ID3=ID3)
                with suppress(error):
                    audio.add_tags()
                
                if artist_name:
                    audio.tags.add(TPE1(encoding=3, text=artist_name))
                    audio.tags.add(TPE2(encoding=3, text=artist_name))
                if album_name:
                    audio.tags.add(TALB(encoding=3, text=album_name))
                if track_name:
                    audio.tags.add(TIT2(encoding=3, text=track_name))
                
                audio.save()
            
            elif file_path.lower().endswith(".flac"):
                audio = FLAC(file_path)
                
                if artist_name:
                    audio["artist"] = artist_name
                    audio["albumartist"] = artist_name
                if album_name:
                    audio["album"] = album_name
                if track_name:
                    audio["title"] = track_name
                
                audio.save()
                
        except Exception as e:
            self.logger.error("[云音乐元数据] 写入基础标签失败: %s, 错误: %s", os.path.basename(file_path), str(e))

    async def _write_track_tags(self, file_path: str, artist_name: str, album_name: str, track_name: str, cover_url: str = ""):
        """写入完整标签（含封面）"""
        try:
            if file_path.lower().endswith(".mp3"):
                audio = MP3(file_path, ID3=ID3)
                with suppress(error):
                    audio.add_tags()
                
                # 基础标签
                if artist_name:
                    audio.tags.add(TPE1(encoding=3, text=artist_name))
                    audio.tags.add(TPE2(encoding=3, text=artist_name))
                if album_name:
                    audio.tags.add(TALB(encoding=3, text=album_name))
                if track_name:
                    audio.tags.add(TIT2(encoding=3, text=track_name))
                
                # 封面
                if cover_url:
                    try:
                        async with self.mass.http_session.get(cover_url, timeout=3) as resp:
                            cover_data = await resp.read()
                            audio.tags.add(APIC(
                                encoding=3,
                                mime="image/jpeg",
                                type=3,
                                desc="Cover",
                                data=cover_data
                            ))
                    except Exception:
                        pass
                
                audio.save()
            
            elif file_path.lower().endswith(".flac"):
                audio = FLAC(file_path)
                
                # 基础标签
                if artist_name:
                    audio["artist"] = artist_name
                    audio["albumartist"] = artist_name
                if album_name:
                    audio["album"] = album_name
                if track_name:
                    audio["title"] = track_name
                
                # 封面
                if cover_url:
                    try:
                        async with self.mass.http_session.get(cover_url, timeout=3) as resp:
                            cover_data = await resp.read()
                            audio.clear_pictures()
                            pic = Picture()
                            pic.data = cover_data
                            pic.mime = "image/jpeg"
                            pic.type = 3
                            audio.add_picture(pic)
                    except Exception:
                        pass
                
                audio.save()
                
        except Exception as e:
            self.logger.error("[云音乐元数据] 写入完整标签失败: %s, 错误: %s", os.path.basename(file_path), str(e))

# ------------------------------
# 插件初始化函数
# ------------------------------
async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """初始化插件实例"""
    return CloudMusicMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: Optional[str] = None,
    action: Optional[str] = None,
    values: Optional[Dict[str, ConfigValueType]] = None,
) -> tuple[ConfigEntry, ...]:
    """返回配置项"""
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
            key=ConfigKeys.WRITE_TAGS,
            type=ConfigEntryType.BOOLEAN,
            label="写入元数据到本地文件标签",
            default_value=False,
            description="自动补充缺失的ID3标签并写入本地音乐文件",
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.AUTO_TRIGGER_NO_IMAGE,
            type=ConfigEntryType.BOOLEAN,
            label="无图片时自动触发获取",
            default_value=True,
            description="当艺术家无图片时自动触发云音乐API请求",
            required=False,
        ),
        ConfigEntry(
            key=ConfigKeys.API_URL,
            type=ConfigEntryType.STRING,
            label="自建云音乐API地址",
            description="你的自建云音乐API服务地址",
            required=True,
            default_value="http://localhost:3003",
        ),
    )