"""NetEase Metadata provider for Music Assistant UI (v1.5.16)."""

from __future__ import annotations

import os
import asyncio
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, cast
from datetime import datetime

import aiohttp.client_exceptions
import mutagen
from mutagen.id3 import ID3, APIC, TPE1, TALB, TIT2, TPE2, error
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
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

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# ------------------------------
# Core Configuration
# ------------------------------
SUPPORTED_FEATURES = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
}

# Configuration constants
CONF_ENABLE_ARTIST_METADATA = "enable_artist_metadata"
CONF_ENABLE_ALBUM_METADATA = "enable_album_metadata"
CONF_ENABLE_TRACK_METADATA = "enable_track_metadata"
CONF_ENABLE_IMAGES = "enable_images"
CONF_WRITE_TAGS = "write_tags_to_file"
CONF_NETEASE_API_URL = "netease_api_url"
CONF_AUTO_TRIGGER_NO_IMAGE = "auto_trigger_no_image"

# Image type mapping (aligned with AudioDB spec)
IMG_MAPPING = {
    "artist_picUrl": ImageType.THUMB,
    "album_picUrl": ImageType.THUMB,
    "track_picUrl": ImageType.THUMB,
}

# ------------------------------
# Plugin Initialization
# ------------------------------
async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given config."""
    return NetEaseMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries to setup this provider (aligned with AudioDB spec)."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_ENABLE_ARTIST_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Artist Metadata Fetch",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_ALBUM_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Album Metadata Fetch",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_TRACK_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Track Metadata Fetch",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ENABLE_IMAGES,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Artist/Album/Track Image Fetch",
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_WRITE_TAGS,
            type=ConfigEntryType.BOOLEAN,
            label="Write Metadata to Local File Tags (beta)",
            default_value=False,
            description="Automatically fill missing ID3 tags and write to local music files",
            required=False,
        ),
        ConfigEntry(
            key=CONF_AUTO_TRIGGER_NO_IMAGE,
            type=ConfigEntryType.BOOLEAN,
            label="Auto Trigger When No Artist Image",
            default_value=True,
            description="Automatically trigger NetEase API request if artist has no image",
            required=False,
        ),
        ConfigEntry(
            key=CONF_NETEASE_API_URL,
            type=ConfigEntryType.STRING,
            label="Self-hosted NetEase API URL",
            description="URL of your self-hosted NetEase Music API",
            required=True,
            default_value="http://BaseURL:3003",
        ),
    )

# ------------------------------
# Core Provider Class
# ------------------------------
class NetEaseMetadataProvider(MetadataProvider):
    """NetEase Music Metadata Provider (v1.5.16)."""

    throttler: Throttler
    netease_api_url: str
    write_tags: bool
    auto_trigger_no_image: bool

    async def handle_async_init(self) -> None:
        """Async initialization of provider."""
        self.cache = self.mass.cache
        self.netease_api_url = self.config.get_value(CONF_NETEASE_API_URL)
        self.write_tags = self.config.get_value(CONF_WRITE_TAGS, False)
        self.auto_trigger_no_image = self.config.get_value(CONF_AUTO_TRIGGER_NO_IMAGE, True)
        self.throttler = Throttler(rate_limit=10, period=1)
        
        # 核心初始化日志 - 仅保留关键信息
        self.logger.debug(
            "[云音乐元数据 v1.5.16] 初始化完成 - API地址: %s, 写入标签: %s",
            self.netease_api_url, self.write_tags
        )

    # ------------------------------
    # Artist Metadata (精简日志)
    # ------------------------------
    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Get artist metadata from NetEase."""
        # Clean artist name
        artist.name = self._clean_artist_name(artist.name)
        
        # Basic feature check
        if not self.config.get_value(CONF_ENABLE_ARTIST_METADATA):
            return None
        
        # Check for existing valid images
        has_artist_images = self._check_artist_has_valid_images(artist)
        if has_artist_images and not self.auto_trigger_no_image:
            return None
        
        # Validate artist name
        artist_name = artist.name.strip()
        if not artist_name:
            return None
        
        # Normalize artist name for search
        search_name = self._normalize_artist_name(artist_name)
        
        # Get data from API
        if not (data := await self._get_data("search", keywords=search_name, type=100, limit=1)):
            return None
        
        # Parse API response
        if not data.get("result") or not data["result"].get("artists"):
            return None
        
        artist_data = data["result"]["artists"][0]
        
        # Parse metadata
        metadata = self._parse_artist_metadata(artist, artist_data)
        return metadata

    # ------------------------------
    # Album Metadata (移除歌手替换功能，保留封面获取)
    # ------------------------------
    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Get album metadata from NetEase (移除歌手替换，仅保留封面/年份等信息获取)."""
        # Basic feature check
        if not self.config.get_value(CONF_ENABLE_ALBUM_METADATA):
            return None
        
        # Build search keyword
        album_name = f"{album.name} {album.version}" if album.version else album.name
        target_artist_name = ""
        if album.artists:
            target_artist_name = self._clean_artist_name(album.artists[0].name).strip()
        
        # 核心逻辑：当歌手是Various Artists时，仅用专辑名作为关键词
        if target_artist_name.strip().lower() == "various artists":
            search_keyword = album_name.strip()
        else:
            search_keyword = f"{album_name.strip()} {target_artist_name}".strip()
        
        if not search_keyword:
            return None
        
        # Build search parameters
        search_params = {"keywords": search_keyword, "type": 10, "limit": 3}
        album_mbid = album.get_external_id(ExternalID.MB_RELEASEGROUP)
        if album_mbid:
            search_params["mbid"] = album_mbid
        
        # Get data from API
        if not (data := await self._get_data("search",** search_params)):
            return None
        
        # Parse API response
        if not data.get("result") or not data["result"].get("albums"):
            return None
        
        # 选择第一个匹配结果
        album_data = data["result"]["albums"][0]
        
        # Parse metadata（重点保留封面信息）
        metadata = self._parse_album_metadata(album, album_data)
        
        # Write tags in batch if enabled（保留封面写入）
        if self.write_tags and hasattr(album, "tracks") and album.tracks:
            await self._batch_write_album_tags(album, album_data)
        
        return metadata

    # ------------------------------
    # Track Metadata (精简日志)
    # ------------------------------
    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Get track metadata from NetEase."""
        # Basic feature check
        if not self.config.get_value(CONF_ENABLE_TRACK_METADATA):
            return None
        
        # Build search keyword
        track_name = f"{track.name} {track.version}" if track.version else track.name
        artist_name = ""
        for track_artist in track.artists:
            cleaned_artist_name = self._clean_artist_name(track_artist.name)
            artist_name = cleaned_artist_name.strip()
        
        search_keyword = f"{track_name.strip()} {artist_name}".strip()
        if not search_keyword:
            return None
        
        # Build search parameters
        search_params = {"keywords": search_keyword, "type": 1, "limit": 1}
        if track.mbid:
            search_params["mbid"] = track.mbid
        
        # Get data from API
        if not (data := await self._get_data("search",** search_params)):
            # Fallback: write basic tags if enabled
            if self.write_tags and hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
                album_name = track.album.name if (hasattr(track, "album") and track.album) else ""
                await self._fast_write_basic_tags(track.file_path, artist_name, album_name, track_name)
            return None
        
        # Parse API response
        if not data.get("result") or not data["result"].get("songs"):
            return None
        
        track_data = data["result"]["songs"][0]
        
        # Parse metadata
        metadata = self._parse_track_metadata(track, track_data)
        
        # Write full tags if enabled
        if self.write_tags and hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
            # Clean special characters in artist name from API
            api_artist_name = track_data.get("ar", [{}])[0].get("name", artist_name)
            cleaned_api_artist_name = self._clean_artist_name(api_artist_name)
            
            await self._fast_write_track_tags(
                file_path=track.file_path,
                artist_name=cleaned_api_artist_name,
                album_name=track_data.get("al", {}).get("name", ""),
                track_name=track_data.get("name", track_name),
                cover_url=track_data.get("al", {}).get("picUrl", "")
            )
        
        return metadata

    # ------------------------------
    # Private Helper Methods
    # ------------------------------
    def _clean_artist_name(self, artist_name: str) -> str:
        """Clean artist name by removing separators and extra spaces."""
        if not artist_name:
            return ""
            
        separators = ['/', '\\', '|', '，', '；', ';', '+']
        
        # Iterate separators, cut at first occurrence
        cleaned_name = artist_name.strip()
        for sep in separators:
            if sep in cleaned_name:
                cleaned_name = cleaned_name.split(sep)[0].strip()
                break
        
        # Handle multiple spaces
        cleaned_name = ' '.join(cleaned_name.split())
        
        return cleaned_name

    def _check_artist_has_valid_images(self, artist: Artist) -> bool:
        """Check if artist has valid images."""
        if not self.config.get_value(CONF_ENABLE_IMAGES):
            return False
        
        has_images_attr = hasattr(artist, 'images')
        if not has_images_attr or not artist.images:
            return False
        
        # Filter valid images
        valid_images = [img for img in artist.images if img.path and img.type == ImageType.THUMB]
        return len(valid_images) > 0

    def _normalize_artist_name(self, artist_name: str) -> str:
        """Normalize artist name for search."""
        # 只替换 + 符号，保留 - 和 &
        normalized = artist_name.replace("+", " ").strip()
        return normalized

    @use_cache(86400 * 7, persistent=True)
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any] | None:
        """Get data from NetEase API."""
        url = f"{self.netease_api_url}/{endpoint}"
        
        try:
            async with (
                self.throttler,
                self.mass.http_session.get(url, params=kwargs, ssl=False, timeout=5) as response,
            ):
                if response.status != 200:
                    self.logger.warning(
                        "[云音乐元数据 v1.5.16] API请求失败 - 接口地址: %s, 状态码: %d",
                        url, response.status
                    )
                    return None
                
                result = cast("dict[str, Any]", await response.json())
                return result
                
        except (
            aiohttp.client_exceptions.ContentTypeError,
            JSONDecodeError,
            aiohttp.client_exceptions.ClientConnectorError,
            aiohttp.client_exceptions.ServerDisconnectedError,
            TimeoutError,
        ) as e:
            self.logger.error(
                "[云音乐元数据 v1.5.16] API请求异常 - 接口地址: %s, 错误: %s",
                url, str(e)
            )
            return None

    def _parse_artist_metadata(self, artist: Artist, artist_data: dict[str, Any]) -> MediaItemMetadata:
        """Parse NetEase artist data to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        
        # Basic metadata
        metadata.genres = {artist_data.get("genre", "")} if artist_data.get("genre") else set()
        metadata.description = artist_data.get("briefDesc", "") or artist_data.get("desc", "")
        
        # Images
        if self.config.get_value(CONF_ENABLE_IMAGES):
            metadata.images = UniqueList()
            if pic_url := artist_data.get("picUrl"):
                metadata.images.append(
                    MediaItemImage(
                        type=IMG_MAPPING["artist_picUrl"],
                        path=pic_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        
        # Update MBID if available
        mbid = artist_data.get("musicBrainzId")
        if not artist.mbid and artist.provider == "library" and mbid:
            if isinstance(artist, ItemMapping):
                artist = self.mass.music.artists.artist_from_item_mapping(artist)
            
            artist.mbid = mbid
            asyncio.create_task(self.mass.music.artists.update_item_in_library(artist.item_id, artist))
        
        return metadata

    def _parse_album_metadata(self, album: Album, album_data: dict[str, Any]) -> MediaItemMetadata:
        """Parse NetEase album data to MediaItemMetadata (保留封面/年份解析)."""
        metadata = MediaItemMetadata()
        
        # Basic metadata
        metadata.genres = {album_data.get("genre", "")} if album_data.get("genre") else set()
        metadata.description = album_data.get("description", "")
        
        # 重点保留封面信息
        if self.config.get_value(CONF_ENABLE_IMAGES):
            metadata.images = UniqueList()
            if pic_url := album_data.get("picUrl"):
                metadata.images.append(
                    MediaItemImage(
                        type=IMG_MAPPING["album_picUrl"],
                        path=pic_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        
        # 年份解析
        publish_time = album_data.get("publishTime")
        if not album.year and publish_time:
            try:
                timestamp_ms = int(publish_time)
                timestamp_s = timestamp_ms // 1000
                publish_date = datetime.fromtimestamp(timestamp_s)
                album.year = publish_date.year
            except (ValueError, TypeError):
                pass
        
        # Update MBID if available
        mbid = album_data.get("musicBrainzId")
        if (
            not album.get_external_id(ExternalID.MB_RELEASEGROUP)
            and album.provider == "library"
            and mbid
        ):
            album.add_external_id(ExternalID.MB_RELEASEGROUP, mbid)
            asyncio.create_task(self.mass.music.albums.update_item_in_library(album.item_id, album))
        
        return metadata

    def _parse_track_metadata(self, track: Track, track_data: dict[str, Any]) -> MediaItemMetadata:
        """Parse NetEase track data to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        
        # Basic metadata
        metadata.lyrics = track_data.get("lyric", "")
        metadata.genres = {track_data.get("genre", "")} if track_data.get("genre") else set()
        metadata.description = track_data.get("description", "")
        
        # Images
        if self.config.get_value(CONF_ENABLE_IMAGES):
            metadata.images = UniqueList()
            if pic_url := track_data.get("al", {}).get("picUrl"):
                metadata.images.append(
                    MediaItemImage(
                        type=IMG_MAPPING["track_picUrl"],
                        path=pic_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        
        # Update artist MBID if available
        artist_mbid = track_data.get("ar", [{}])[0].get("musicBrainzId")
        for track_artist in track.artists:
            if (
                not track_artist.mbid
                and track_artist.provider == "library"
                and artist_mbid
            ):
                if isinstance(track_artist, ItemMapping):
                    track_artist = self.mass.music.artists.artist_from_item_mapping(track_artist)
                
                track_artist.mbid = artist_mbid
                asyncio.create_task(self.mass.music.artists.update_item_in_library(track_artist.item_id, track_artist))
        
        return metadata

    # ------------------------------
    # Tag Writing Methods (保留封面写入)
    # ------------------------------
    async def _batch_write_album_tags(self, album: Album, album_data: dict):
        """Batch write tags for album tracks (保留封面写入)."""
        if not album.tracks or len(album.tracks) == 0:
            return
        
        cover_url = album_data.get("picUrl", "")
        artist_name = ""
        if hasattr(album, "artists") and album.artists:
            artist_name = self._clean_artist_name(album.artists[0].name).strip()
        album_name = album_data.get("name", album.name).strip()
        
        # Process all tracks
        tasks = []
        for track in album.tracks:
            if hasattr(track, "file_path") and track.file_path and os.path.exists(track.file_path):
                track_name = track.name.strip() if hasattr(track, "name") else ""
                tasks.append(self._fast_write_track_tags(
                    file_path=track.file_path,
                    artist_name=artist_name,
                    album_name=album_name,
                    track_name=track_name,
                    cover_url=cover_url
                ))
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Count errors
            error_count = sum(1 for result in results if isinstance(result, Exception))
            if error_count > 0:
                self.logger.warning(
                    "[云音乐元数据 v1.5.16] 专辑标签写入失败数: %d/%d - 专辑名: %s",
                    error_count, len(tasks), album.name
                )

    async def _fast_write_basic_tags(self, file_path: str, artist_name: str, album_name: str, track_name: str):
        """Fast write basic tags to local file."""
        cleaned_artist_name = self._clean_artist_name(artist_name)
        
        try:
            if file_path.lower().endswith(".mp3"):
                audio = MP3(file_path, ID3=ID3)
                try:
                    audio.add_tags()
                except error:
                    pass
                
                if cleaned_artist_name:
                    audio.tags.add(TPE1(encoding=3, text=cleaned_artist_name))
                    audio.tags.add(TPE2(encoding=3, text=cleaned_artist_name))
                if album_name:
                    audio.tags.add(TALB(encoding=3, text=album_name))
                if track_name:
                    audio.tags.add(TIT2(encoding=3, text=track_name))
                
                audio.save()
            
            elif file_path.lower().endswith(".flac"):
                audio = FLAC(file_path)
                
                if cleaned_artist_name:
                    audio["artist"] = cleaned_artist_name
                    audio["albumartist"] = cleaned_artist_name
                if album_name:
                    audio["album"] = album_name
                if track_name:
                    audio["title"] = track_name
                
                audio.save()
                
        except Exception as e:
            self.logger.error(
                "[云音乐元数据 v1.5.16] 写入基础标签失败 - 文件: %s, 错误: %s",
                file_path, str(e)
            )

    async def _fast_write_track_tags(self, file_path: str, artist_name: str, album_name: str, track_name: str, cover_url: str):
        """Fast write full tags (including cover) to local file."""
        cleaned_artist_name = self._clean_artist_name(artist_name)
        
        try:
            if file_path.lower().endswith(".mp3"):
                audio = MP3(file_path, ID3=ID3)
                try:
                    audio.add_tags()
                except error:
                    pass
                
                # Basic tags
                if cleaned_artist_name:
                    audio.tags.add(TPE1(encoding=3, text=cleaned_artist_name))
                    audio.tags.add(TPE2(encoding=3, text=cleaned_artist_name))
                if album_name:
                    audio.tags.add(TALB(encoding=3, text=album_name))
                if track_name:
                    audio.tags.add(TIT2(encoding=3, text=track_name))
                
                # 保留封面写入
                if cover_url:
                    try:
                        async with self.mass.http_session.get(cover_url, timeout=3) as resp:
                            cover_data = await resp.read()
                            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))
                    except Exception:
                        pass
                
                audio.save()
            
            elif file_path.lower().endswith(".flac"):
                audio = FLAC(file_path)
                
                # Basic tags
                if cleaned_artist_name:
                    audio["artist"] = cleaned_artist_name
                    audio["albumartist"] = cleaned_artist_name
                if album_name:
                    audio["album"] = album_name
                if track_name:
                    audio["title"] = track_name
                
                # 保留封面写入
                if cover_url:
                    try:
                        async with self.mass.http_session.get(cover_url, timeout=3) as resp:
                            cover_data = await resp.read()
                            audio.clear_pictures()
                            audio.add_picture(mutagen.flac.Picture(data=cover_data, mime="image/jpeg", type=3))
                    except Exception:
                        pass
                
                audio.save()
                
        except Exception as e:
            self.logger.error(
                "[云音乐元数据 v1.5.16] 写入完整标签失败 - 文件: %s, 错误: %s",
                file_path, str(e)
            )