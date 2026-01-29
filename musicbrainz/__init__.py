"""The NetEase Metadata provider for Music Assistant (forked from Musicbrainz)."""

from __future__ import annotations

import re
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from functools import lru_cache

from mashumaro import DataClassDictMixin
from mashumaro.exceptions import MissingField
from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.media_items import Album, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# 核心配置
SUPPORTED_FEATURES: set[ProviderFeature] = set()
CONF_NETEASE_API_URL = "netease_api_url" 
DEFAULT_NETEASE_API_URL = "http://BaseURL:3003"

# 限流与缓存配置
THROTTLE_RATE_LIMIT = 3
THROTTLE_PERIOD = 30
CACHE_TTL = 3600
REQUEST_TIMEOUT = 10

# 生成占位符ID
@lru_cache(maxsize=1000)
def generate_placeholder_id(name: str) -> str:
    """生成符合UUID格式的占位符ID"""
    namespace = uuid.NAMESPACE_OID
    placeholder_id = str(uuid.uuid5(namespace, f"netease_{name}"))
    return placeholder_id

async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """初始化Provider实例"""
    return MusicbrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """返回配置项"""
    from music_assistant_models.config_entries import ConfigEntry, ConfigEntryType
    return (
        ConfigEntry(
            key=CONF_NETEASE_API_URL,
            type=ConfigEntryType.STRING,
            label="NetEase API URL",
            description="URL of your self-hosted NetEase Cloud Music API",
            default_value=DEFAULT_NETEASE_API_URL,
            required=True,
        ),
    )

def replace_hyphens(
    data: dict[str, Any] | list[dict[str, Any]] | Any,
) -> dict[str, Any] | list[dict[str, Any]] | Any:
    """将连字符键替换为下划线"""
    if isinstance(data, dict):
        return {key.replace("-", "_"): replace_hyphens(value) for key, value in data.items()}
    if isinstance(data, list):
        return [replace_hyphens(x) for x in data]
    return data

# 数据模型类
@dataclass
class MusicBrainzTag(DataClassDictMixin):
    count: int
    name: str

@dataclass
class MusicBrainzAlias(DataClassDictMixin):
    name: str
    sort_name: str
    locale: str | None = None
    type: str | None = None
    primary: bool | None = None
    begin_date: str | None = None
    end_date: str | None = None

@dataclass
class MusicBrainzArtist(DataClassDictMixin):
    id: str
    name: str
    sort_name: str
    aliases: list[MusicBrainzAlias] | None = None
    tags: list[MusicBrainzTag] | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzArtist:
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzArtist.from_dict(alt_data)

@dataclass
class MusicBrainzArtistCredit(DataClassDictMixin):
    name: str
    artist: MusicBrainzArtist

@dataclass
class MusicBrainzReleaseGroup(DataClassDictMixin):
    id: str
    title: str
    primary_type: str | None = None
    primary_type_id: str | None = None
    secondary_types: list[str] | None = None
    secondary_type_ids: list[str] | None = None
    artist_credit: list[MusicBrainzArtistCredit] | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzReleaseGroup:
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzReleaseGroup.from_dict(alt_data)

@dataclass
class MusicBrainzTrack(DataClassDictMixin):
    id: str
    number: str
    title: str
    length: int | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzTrack:
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzTrack.from_dict(alt_data)

@dataclass
class MusicBrainzMedia(DataClassDictMixin):
    format: str
    track: list[MusicBrainzTrack]
    position: int = 0
    track_count: int = 0
    track_offset: int = 0

@dataclass
class MusicBrainzRelease(DataClassDictMixin):
    id: str
    status_id: str
    count: int
    title: str
    status: str
    artist_credit: list[MusicBrainzArtistCredit]
    release_group: MusicBrainzReleaseGroup
    track_count: int = 0
    media: list[MusicBrainzMedia] = field(default_factory=list)
    date: str | None = None
    country: str | None = None
    disambiguation: str | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRelease:
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRelease.from_dict(alt_data)

@dataclass
class MusicBrainzRecording(DataClassDictMixin):
    id: str
    title: str
    artist_credit: list[MusicBrainzArtistCredit] = field(default_factory=list)
    length: int | None = None
    first_release_date: str | None = None
    isrcs: list[str] | None = None
    tags: list[MusicBrainzTag] | None = None
    disambiguation: str | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRecording:
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRecording.from_dict(alt_data)

class MusicbrainzProvider(MetadataProvider):
    """网易云音乐元数据提供器"""
    throttler = ThrottlerManager(rate_limit=THROTTLE_RATE_LIMIT, period=THROTTLE_PERIOD)

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
        api_url: str = DEFAULT_NETEASE_API_URL,
    ):
        super().__init__(mass, manifest, config, supported_features)
        self.api_url = self.config.get_value(CONF_NETEASE_API_URL, DEFAULT_NETEASE_API_URL)
        self.request_headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)",
            "Accept": "application/json"
        }
        
    async def handle_async_init(self) -> None:
        """异步初始化"""
        self.cache = self.mass.cache

    @use_cache(CACHE_TTL)
    async def search(
        self, artistname: str, albumname: str, trackname: str, trackversion: str | None = None
    ) -> tuple[MusicBrainzArtist, MusicBrainzReleaseGroup, MusicBrainzRecording] | None:
        """搜索网易云音乐元数据"""
        trackname, trackversion = parse_title_and_version(trackname, trackversion)

        search_params = {
            "keywords": f"{artistname} {albumname} {trackname}",
            "type": 1,
            "limit": 1
        }

        try:
            result = await self.get_data("search", **search_params)

            if not result or not result.get("result") or not result["result"].get("songs") or len(result["result"]["songs"]) == 0:
                return None

            track_data = result["result"]["songs"][0]

            # 数据校验与占位符生成
            required_fields = ["id", "name"]
            missing_fields = [f for f in required_fields if f not in track_data]
            if missing_fields:
                if "id" in missing_fields:
                    track_data["id"] = generate_placeholder_id(f"{artistname}_{albumname}_{trackname}")
                if "name" in missing_fields:
                    track_data["name"] = trackname

            # 解析歌手数据
            if "ar" not in track_data or not isinstance(track_data["ar"], list) or len(track_data["ar"]) == 0:
                artist_id = generate_placeholder_id(artistname)
                artist_data = {"id": artist_id, "name": artistname}
            else:
                artist_data = track_data["ar"][0]
                artist_data["id"] = artist_data.get("id", generate_placeholder_id(artist_data.get("name", artistname)))
                artist_data["name"] = artist_data.get("name", artistname)

            # 解析专辑数据
            if "al" not in track_data or not isinstance(track_data["al"], dict):
                album_id = generate_placeholder_id(f"{artistname}_{albumname}")
                album_data = {"id": album_id, "name": albumname}
            else:
                album_data = track_data["al"]
                album_data["id"] = album_data.get("id", generate_placeholder_id(album_data.get("name", f"{artistname}_{albumname}")))
                album_data["name"] = album_data.get("name", albumname)

            # 构建返回对象
            artist = MusicBrainzArtist(
                id=str(artist_data["id"]),
                name=artist_data["name"],
                sort_name=artist_data["name"]
            )

            release_group = MusicBrainzReleaseGroup(
                id=str(album_data["id"]),
                title=album_data["name"],
                artist_credit=[MusicBrainzArtistCredit(
                    name=artist_data["name"],
                    artist=artist
                )]
            )

            recording = MusicBrainzRecording(
                id=str(track_data["id"]),
                title=track_data["name"],
                length=track_data.get("dt"),
                artist_credit=[MusicBrainzArtistCredit(
                    name=artist_data["name"],
                    artist=artist
                )],
                disambiguation=trackversion
            )

            return (artist, release_group, recording)
            
        except KeyError:
            return None
        except Exception:
            return None

    @use_cache(CACHE_TTL)
    async def get_artist_details(self, artist_id: str) -> MusicBrainzArtist:
        """获取歌手详情"""
        try:
            uuid.UUID(artist_id)
            if artist_id.startswith(str(uuid.NAMESPACE_OID)[:8]):
                artist = MusicBrainzArtist(
                    id=artist_id,
                    name=f"Artist_{artist_id[:8]}",
                    sort_name=f"Artist_{artist_id[:8]}"
                )
                return artist
        except ValueError:
            pass
        
        try:
            result = await self.get_data(f"artist/detail?id={artist_id}")
            
            if not result or "data" not in result or "artist" not in result["data"]:
                raise ValueError("No artist data")
                
            artist_data = result["data"]["artist"]
            
            artist_id = str(artist_data.get("id", generate_placeholder_id(artist_data.get("name", f"artist_{artist_id}"))))
            artist_name = artist_data.get("name", f"Unknown Artist {artist_id[:8]}")
            
            aliases_list = artist_data.get("alias", [])
            aliases = [MusicBrainzAlias(name=alias, sort_name=alias) for alias in aliases_list]
            
            artist = MusicBrainzArtist(
                id=artist_id,
                name=artist_name,
                sort_name=artist_name,
                aliases=aliases if aliases else None
            )
            
            return artist
            
        except Exception:
            placeholder_id = generate_placeholder_id(f"artist_{artist_id}")
            artist = MusicBrainzArtist(
                id=placeholder_id,
                name=f"Unknown Artist {artist_id[:8]}",
                sort_name=f"Unknown Artist {artist_id[:8]}"
            )
            return artist

    @use_cache(CACHE_TTL)
    async def get_recording_details(self, recording_id: str) -> MusicBrainzRecording:
        """获取歌曲详情"""
        try:
            uuid.UUID(recording_id)
            if recording_id.startswith(str(uuid.NAMESPACE_OID)[:8]):
                artist = MusicBrainzArtist(
                    id=generate_placeholder_id("unknown_artist"),
                    name="Unknown Artist",
                    sort_name="Unknown Artist"
                )
                recording = MusicBrainzRecording(
                    id=recording_id,
                    title=f"Track_{recording_id[:8]}",
                    artist_credit=[MusicBrainzArtistCredit(
                        name="Unknown Artist",
                        artist=artist
                    )]
                )
                return recording
        except ValueError:
            pass
        
        try:
            result = await self.get_data(f"song/detail?ids={recording_id}")
            
            if not result or "songs" not in result or not result["songs"]:
                raise ValueError("No track data")
                
            track_data = result["songs"][0]
            
            if "ar" in track_data and track_data["ar"]:
                artist_data = track_data["ar"][0]
            else:
                artist_data = {"id": generate_placeholder_id("unknown_artist"), "name": "Unknown Artist"}

            track_id = str(track_data.get("id", generate_placeholder_id(f"track_{recording_id}")))
            track_name = track_data.get("name", f"Unknown Track {track_id[:8]}")
            artist_id = str(artist_data.get("id", generate_placeholder_id("unknown_artist")))
            artist_name = artist_data.get("name", "Unknown Artist")
            
            artist = MusicBrainzArtist(
                id=artist_id,
                name=artist_name,
                sort_name=artist_name
            )
            
            recording = MusicBrainzRecording(
                id=track_id,
                title=track_name,
                length=track_data.get("dt"),
                artist_credit=[MusicBrainzArtistCredit(
                    name=artist_name,
                    artist=artist
                )]
            )
            
            return recording
            
        except Exception:
            placeholder_id = generate_placeholder_id(f"track_{recording_id}")
            artist = MusicBrainzArtist(
                id=generate_placeholder_id("unknown_artist"),
                name="Unknown Artist",
                sort_name="Unknown Artist"
            )
            recording = MusicBrainzRecording(
                id=placeholder_id,
                title=f"Unknown Track {recording_id[:8]}",
                artist_credit=[MusicBrainzArtistCredit(
                    name="Unknown Artist",
                    artist=artist
                )]
            )
            return recording

    @use_cache(CACHE_TTL)
    async def get_release_details(self, album_id: str) -> MusicBrainzRelease:
        """获取专辑详情"""
        try:
            uuid.UUID(album_id)
            if album_id.startswith(str(uuid.NAMESPACE_OID)[:8]):
                artist = MusicBrainzArtist(
                    id=generate_placeholder_id("unknown_artist"),
                    name="Unknown Artist",
                    sort_name="Unknown Artist"
                )
                release_group = MusicBrainzReleaseGroup(
                    id=album_id,
                    title=f"Album_{album_id[:8]}",
                    artist_credit=[MusicBrainzArtistCredit(
                        name="Unknown Artist",
                        artist=artist
                    )]
                )
                release = MusicBrainzRelease(
                    id=album_id,
                    status_id="official",
                    count=0,
                    title=f"Album_{album_id[:8]}",
                    status="official",
                    artist_credit=[MusicBrainzArtistCredit(
                        name="Unknown Artist",
                        artist=artist
                    )],
                    release_group=release_group,
                    track_count=0
                )
                return release
        except ValueError:
            pass
        
        try:
            result = await self.get_data(f"album/detail?id={album_id}")
            
            if not result or "album" not in result:
                raise ValueError("No album data")
                
            album_data = result["album"]
            
            artist_data = album_data.get("artist", {"id": generate_placeholder_id("unknown_artist"), "name": "Unknown Artist"})

            album_id = str(album_data.get("id", generate_placeholder_id(f"album_{album_id}")))
            album_name = album_data.get("name", f"Unknown Album {album_id[:8]}")
            artist_id = str(artist_data.get("id", generate_placeholder_id("unknown_artist")))
            artist_name = artist_data.get("name", "Unknown Artist")
            
            publish_time = album_data.get("publishTime")
            publish_date = str(publish_time)[:10] if publish_time else None
            
            artist = MusicBrainzArtist(
                id=artist_id,
                name=artist_name,
                sort_name=artist_name
            )
            
            release_group = MusicBrainzReleaseGroup(
                id=album_id,
                title=album_name
            )
            
            release = MusicBrainzRelease(
                id=album_id,
                status_id="official",
                count=album_data.get("size", 0),
                title=album_name,
                status="official",
                artist_credit=[MusicBrainzArtistCredit(
                    name=artist_name,
                    artist=artist
                )],
                release_group=release_group,
                track_count=album_data.get("size", 0),
                date=publish_date
            )
            
            return release
            
        except Exception:
            placeholder_id = generate_placeholder_id(f"album_{album_id}")
            artist = MusicBrainzArtist(
                id=generate_placeholder_id("unknown_artist"),
                name="Unknown Artist",
                sort_name="Unknown Artist"
            )
            release_group = MusicBrainzReleaseGroup(
                id=placeholder_id,
                title=f"Unknown Album {album_id[:8]}",
                artist_credit=[MusicBrainzArtistCredit(
                    name="Unknown Artist",
                    artist=artist
                )]
            )
            release = MusicBrainzRelease(
                id=placeholder_id,
                status_id="official",
                count=0,
                title=f"Unknown Album {album_id[:8]}",
                status="official",
                artist_credit=[MusicBrainzArtistCredit(
                    name="Unknown Artist",
                    artist=artist
                )],
                release_group=release_group,
                track_count=0
            )
            return release

    @use_cache(CACHE_TTL)
    async def get_releasegroup_details(self, releasegroup_id: str) -> MusicBrainzReleaseGroup:
        """获取专辑组详情"""
        try:
            uuid.UUID(releasegroup_id)
            if releasegroup_id.startswith(str(uuid.NAMESPACE_OID)[:8]):
                artist = MusicBrainzArtist(
                    id=generate_placeholder_id("unknown_artist"),
                    name="Unknown Artist",
                    sort_name="Unknown Artist"
                )
                release_group = MusicBrainzReleaseGroup(
                    id=releasegroup_id,
                    title=f"Album_{releasegroup_id[:8]}",
                    artist_credit=[MusicBrainzArtistCredit(
                        name="Unknown Artist",
                        artist=artist
                    )]
                )
                return release_group
        except ValueError:
            pass
        
        try:
            result = await self.get_data(f"album/detail?id={releasegroup_id}")
            
            if not result or "album" not in result:
                raise ValueError("No album data")
                
            album_data = result["album"]
            
            artist_data = album_data.get("artist", {"id": generate_placeholder_id("unknown_artist"), "name": "Unknown Artist"})

            album_id = str(album_data.get("id", generate_placeholder_id(f"releasegroup_{releasegroup_id}")))
            album_name = album_data.get("name", f"Unknown Album {album_id[:8]}")
            artist_id = str(artist_data.get("id", generate_placeholder_id("unknown_artist")))
            artist_name = artist_data.get("name", "Unknown Artist")
            
            artist = MusicBrainzArtist(
                id=artist_id,
                name=artist_name,
                sort_name=artist_name
            )
            
            release_group = MusicBrainzReleaseGroup(
                id=album_id,
                title=album_name,
                artist_credit=[MusicBrainzArtistCredit(
                    name=artist_name,
                    artist=artist
                )]
            )
            
            return release_group
            
        except Exception:
            placeholder_id = generate_placeholder_id(f"releasegroup_{releasegroup_id}")
            artist = MusicBrainzArtist(
                id=generate_placeholder_id("unknown_artist"),
                name="Unknown Artist",
                sort_name="Unknown Artist"
            )
            release_group = MusicBrainzReleaseGroup(
                id=placeholder_id,
                title=f"Unknown Album {releasegroup_id[:8]}",
                artist_credit=[MusicBrainzArtistCredit(
                    name="Unknown Artist",
                    artist=artist
                )]
            )
            return release_group

    async def get_artist_details_by_album(
        self, artistname: str, ref_album: Album
    ) -> MusicBrainzArtist | None:
        """通过专辑匹配歌手"""
        if mb_id := ref_album.get_external_id(ExternalID.MB_RELEASEGROUP):
            with suppress(InvalidDataError):
                result = await self.get_releasegroup_details(mb_id)
                if result and result.artist_credit:
                    for artist_credit in result.artist_credit:
                        if compare_strings(artist_credit.artist.name, artistname):
                            return artist_credit.artist
                            
        if mb_id := ref_album.get_external_id(ExternalID.MB_ALBUM):
            with suppress(InvalidDataError):
                result = await self.get_release_details(mb_id)
                if result and result.artist_credit:
                    for artist_credit in result.artist_credit:
                        if compare_strings(artist_credit.artist.name, artistname):
                            return artist_credit.artist
                            
        return None

    async def get_artist_details_by_track(
        self, artistname: str, ref_track: Track
    ) -> MusicBrainzArtist | None:
        """通过歌曲匹配歌手"""
        if not ref_track.mbid:
            return None
            
        with suppress(InvalidDataError):
            result = await self.get_recording_details(ref_track.mbid)
            if result and result.artist_credit:
                for artist_credit in result.artist_credit:
                    if compare_strings(artist_credit.artist.name, artistname):
                        return artist_credit.artist
                        
        return None

    async def get_artist_details_by_resource_url(
        self, resource_url: str
    ) -> MusicBrainzArtist | None:
        """通过URL匹配歌手（暂不支持）"""
        return None

    @use_cache(CACHE_TTL)
    @throttle_with_retries
    async def get_data(self, endpoint: str, **kwargs: str) -> Any:
        """调用云音乐API获取数据"""
        url = f"{self.api_url.rstrip('/')}/{endpoint.lstrip('/')}"
        
        try:
            async with self.mass.http_session.get(
                url, 
                headers=self.request_headers, 
                params=kwargs, 
                ssl=False,
                timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 429:
                    backoff_time = int(response.headers.get("Retry-After", 0))
                    raise ResourceTemporarilyUnavailable("Rate Limiter", backoff_time=backoff_time)
                    
                if response.status in (502, 503):
                    raise ResourceTemporarilyUnavailable(backoff_time=10)
                    
                if response.status in (400, 401, 404):
                    return None
                    
                response.raise_for_status()
                response_text = await response.text()
                data = json_loads(response_text)
                return data
                
        except Exception as err:
            raise

        return None