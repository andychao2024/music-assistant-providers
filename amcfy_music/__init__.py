"""
Amcfy Music Subsonic Bridge Plugin for Music Assistant.

Subsonic API bridge for Amcfy Music client - browse and stream all MA music
sources (local files, Spotify, Tidal, NetEase, etc.) via Subsonic protocol.
v1.0.8
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import base64
import hashlib
import json
import os
import random
import re
import secrets
import time
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, Callable

import aiohttp
from aiohttp import web

from music_assistant.helpers.audio import get_mime_type
from music_assistant.helpers.images import get_image_data
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ContentType, MediaType
from music_assistant_models.media_items import Album, Artist, MediaItemImage, Playlist, Track

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant

DOMAIN = "amcfy_music"
SUBSONIC_VERSION = "1.16.1"
CONF_TOKEN = "token"
CONF_SEARCH_SCOPE = "search_scope"
# v1.3.0 BUG #71: QQ 音乐免费账号只给 88 秒试听时,默认继续播放而不是跳过。
# 否则客户端会卡死 5 次重试(8s+)才切到下一首。
CONF_STREAM_PREVIEWS = "stream_previews"
# v1.3.2: 高频噪音 debug 日志通过此开关门控,默认关。开启后会输出每个 HTTP
# 请求、封面查找、音频流分支等诊断信息,仅排查问题时临时启用。
CONF_DEBUG_VERBOSE = "debug_verbose"
LIBRARY_MAX = 99999

ENDPOINT_MAP: dict[str, str] = {
    "ping": "handle_ping",
    "getLicense": "handle_get_license",
    "getScanStatus": "handle_get_scan_status",
    "getMusicFolders": "handle_get_music_folders",
    "getIndexes": "handle_get_indexes",
    "getArtists": "handle_get_artists",
    "getArtist": "handle_get_artist",
    "getAlbum": "handle_get_album",
    "getSong": "handle_get_song",
    "getMusicDirectory": "handle_get_music_directory",
    "getAlbumList": "handle_get_album_list",
    "getAlbumList2": "handle_get_album_list",
    "search2": "handle_search2",
    "search3": "handle_search3",
    "getRandomSongs": "handle_random_songs",
    "getSongsByGenre": "handle_songs_by_genre",
    "getGenres": "handle_get_genres",
    "getCoverArt": "handle_get_cover_art",
    "getLyrics": "handle_get_lyrics",
    "getLyricsBySongId": "handle_get_lyrics_by_song_id",
    "star": "handle_star",
    "unstar": "handle_unstar",
    "setRating": "handle_set_rating",
    "getStarred": "handle_get_starred",
    "getStarred2": "handle_get_starred",
    "getPlaylists": "handle_get_playlists",
    "getPlaylist": "handle_get_playlist",
    "stream": "handle_stream",
    "download": "handle_stream",
    "scrobble": "handle_scrobble",
    "getUser": "handle_get_user",
    "getArtistInfo2": "handle_get_artist_info2",
    "getAlbumInfo2": "handle_get_album_info2",
    "getOpenSubsonicExtensions": "handle_open_subsonic_extensions",
    "getServerStatus": "handle_ping",
}

COVER_ART_MIME: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
}


PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0e"
    b"IDATx\x9cc\xf8\x0f\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
AUDIO_EXTS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".opus", ".aac", ".m4a", ".wma", ".aiff", ".alac", ".dsf", ".dff"})


def _safe_int(val, default=0):
    """Safely convert a value to int."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def _guess_image_mime(data: bytes) -> str:
    for magic, mime in COVER_ART_MIME.items():
        if data[: len(magic)] == magic:
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _dict_to_xml(data: dict, parent: ET.Element) -> None:
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    child = ET.SubElement(parent, key)
                    _dict_to_xml(item, child)
                else:
                    child = ET.SubElement(parent, key)
                    child.text = str(item)
        elif isinstance(value, dict):
            child = ET.SubElement(parent, key)
            _dict_to_xml(value, child)
        elif isinstance(value, bool):
            parent.set(key, "true" if value else "false")
        elif isinstance(value, (int, float)):
            parent.set(key, str(value))
        elif isinstance(value, str) and value:
            parent.set(key, value)

SUBSONIC_XMLNS = "http://subsonic.org/restapi"
ET.register_namespace('', SUBSONIC_XMLNS)


def _xml_response(
    data: dict | None = None,
    status: str = "ok",
    version: str = SUBSONIC_VERSION,
    fmt: str = "xml",
) -> web.Response:
    if fmt == "json":
        payload = {"subsonic-response": {"status": status, "version": version, "xmlns": SUBSONIC_XMLNS}}
        if data:
            payload["subsonic-response"].update(data)
        return web.Response(
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            charset="utf-8",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    root = ET.Element("subsonic-response")
    root.set("status", status)
    root.set("version", version)
    root.set("xmlns", SUBSONIC_XMLNS)
    if data:
        _dict_to_xml(data, root)
    return web.Response(
        body=ET.tostring(root, encoding="utf-8"),
        content_type="text/xml",
        charset="utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _format_timestamp(ts: int | float | datetime | None) -> str:
    if ts is None:
        ts = time.time()
    elif isinstance(ts, datetime):
        ts = ts.timestamp()
    elif isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000
    else:
        ts = time.time()
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _find_image_id(item: Track | Album | Artist) -> str:
    images = (item.metadata and item.metadata.images) or []
    for img in images:
        if img.proxy_id or img.path:
            return item.uri or item.item_id
    if isinstance(item, Track) and item.album:
        album = item.album
        if hasattr(album, "metadata") and album.metadata and album.metadata.images:
            for img in album.metadata.images:
                if img.proxy_id or img.path:
                    return album.uri or album.item_id
    if isinstance(item, Album) and item.artists:
        artist = item.artists[0]
        if hasattr(artist, "metadata") and artist.metadata and artist.metadata.images:
            return artist.uri or artist.item_id
    return ""


def _guess_content_type(track: Track) -> tuple[str, str]:
    """v1.3.0 重构:改用 MA 标准 ContentType 枚举 + get_mime_type 工具,
    替代手写 extension-substring 匹配。优先从 provider_mapping.audio_format.content_type
    推,fallback 从 output_format_str / 文件扩展名推。
    """
    suffix, ct = "mp3", "audio/mpeg"
    af = None
    if track.provider_mappings:
        pm = next(iter(track.provider_mappings))
        af = getattr(pm, "audio_format", None)
    if af:
        # 优先用 MA AudioFormat.content_type(ContentType 枚举)
        ctype = getattr(af, "content_type", None)
        if ctype and str(ctype) not in ("UNKNOWN", "?"):
            ct = get_mime_type(ctype)
            suffix = str(ctype).lower()
        else:
            # fallback: 从 output_format_str 解析
            fmt = (af.output_format_str or "").lower()
            parsed = ContentType.try_parse(fmt) if hasattr(ContentType, "try_parse") else None
            if parsed and str(parsed) not in ("UNKNOWN", "?"):
                ct = get_mime_type(parsed)
                suffix = str(parsed).lower()
            elif "flac" in fmt:
                suffix, ct = "flac", "audio/flac"
            elif "wav" in fmt or "pcm" in fmt:
                suffix, ct = "wav", "audio/wav"
            elif "opus" in fmt:
                suffix, ct = "opus", "audio/opus"
            elif "ogg" in fmt:
                suffix, ct = "ogg", "audio/ogg"
            elif "aac" in fmt:
                suffix, ct = "aac", "audio/aac"
    return suffix, ct


def _song_dict(track: Track) -> dict:
    artist = track.artists[0] if track.artists else None
    album = track.album
    suffix, ct = _guess_content_type(track)
    bit_rate = None
    file_size = 0
    if track.provider_mappings:
        pm = next(iter(track.provider_mappings))
        af = getattr(pm, "audio_format", None)
        if af:
            bit_rate = af.bit_rate
        file_size = getattr(pm, "file_size", 0) or 0
    genres = track.metadata and track.metadata.genres
    if genres and not hasattr(genres, '__iter__'):
        genres = [genres] if genres else []
    year = (album and album.year) or ""
    if not year and track.metadata and track.metadata.release_date:
        year = track.metadata.release_date.year
    return {
        "id": track.uri or track.item_id,
        "parent": album.uri if album else "",
        "title": track.name,
        "artist": artist.name if artist else "Unknown",
        "isDir": False,
        "coverArt": _find_image_id(track),
        "year": year or 0,
        "album": album.name if album else "",
        "track": track.track_number or 0,
        "duration": track.duration or 0,
        "size": file_size,
        "suffix": suffix,
        "contentType": ct,
        "path": track.uri or "",
        "discNumber": track.disc_number or 0,
        "type": "music",
        "created": _format_timestamp(track.date_added),
        "played": _format_timestamp(track.last_played) if getattr(track, "last_played", None) else None,
        "starred": _format_timestamp(track.date_added) if track.favorite else None,
        "albumId": album.uri if album else "",
        "artistId": artist.uri if artist else "",
        "genre": next(iter(genres), "") if genres else "",
        "bitRate": bit_rate or 0,
    }


def _album_dict(album: Album, track_count: int = 0, duration: int = 0) -> dict:
    artist = album.artists[0] if album.artists else None
    genres = album.metadata and album.metadata.genres
    if genres and isinstance(genres, str):
        genres = [genres]
    return {
        "id": album.uri or album.item_id,
        "name": album.name,
        "artist": artist.name if artist else "Unknown",
        "artistId": artist.uri if artist else "",
        "coverArt": _find_image_id(album),
        "songCount": track_count,
        "duration": duration,
        "created": _format_timestamp(album.date_added),
        "played": _format_timestamp(album.last_played) if getattr(album, "last_played", None) else None,
        "year": album.year or 0,
        "genre": next(iter(genres), "") if genres else "",
        "starred": _format_timestamp(album.date_added) if album.favorite else None,
    }


def _artist_dict(artist: Artist, album_count: int = 0) -> dict:
    return {
        "id": artist.uri or artist.item_id,
        "name": artist.name,
        "albumCount": album_count,
        "coverArt": _find_image_id(artist),
        "starred": _format_timestamp(artist.date_added) if artist.favorite else None,
    }


def _build_index(artist_dicts: list[dict]) -> list[dict]:
    idx: dict[str, list[dict]] = {}
    for a in artist_dicts:
        letter = (a.get("name", "") or "")[:1].upper() or "#"
        if letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            letter = "#"
        idx.setdefault(letter, []).append(a)
    return [
        {"name": letter, "artist": idx[letter]}
        for letter in sorted(idx.keys())
    ]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    return AmcfyBridgePlugin(mass, manifest, config)


def _get_raw_config_value(mass, instance_id: str, key: str, default: str = "") -> str:
    """Read a raw stored provider config value across MA 2.9.x / 2.10.x."""
    try:
        raw = mass.config.get_raw_provider_config_value(instance_id, key, default=None)
        if raw is None:
            raw = mass.config.get(f"providers/{instance_id}/values/{key}", default)
        return str(raw) if raw else default
    except Exception:
        return default


def _persist_token(mass, instance_id: str, token: str) -> None:
    """Persist a new token and sync the running provider instance."""
    try:
        setter = getattr(mass.config, "set_raw_provider_config_value", None)
        if setter is not None:
            setter(instance_id, CONF_TOKEN, token)
        else:
            mass.config.set(f"providers/{instance_id}/values/{CONF_TOKEN}", token)
    except Exception:
        pass
    try:
        provider = mass.get_provider(instance_id)
        if provider is not None:
            provider._api_token = token
    except Exception:
        pass


CACHE_TTL = 300

class AmcfyBridgePlugin(PluginProvider):
    _unload_callbacks: list[Callable[[], None]]
    _api_token: str
    _lyrics_cache: dict[str, tuple[float, Any]]

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        super().__init__(mass, manifest, config)
        self._unload_callbacks = []
        self._api_token = ""
        self._lyrics_cache = {}
        self._response_fmt = "xml"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        # values (UI edits) win over setup_data (only written by the setup flow).
        # Otherwise UI edits would be masked by the original setup-time token.
        token = self.config.get_value(CONF_TOKEN) or self.get_setup_value(CONF_TOKEN, "") or ""
        if not token:
            token = secrets.token_hex(16)

        search_scope = self.get_setup_value(CONF_SEARCH_SCOPE, "library") or self.config.get_value(CONF_SEARCH_SCOPE) or "library"
        if not search_scope:
            search_scope = "library"

        return (
            ConfigEntry(
                key=CONF_TOKEN,
                type=ConfigEntryType.STRING,
                label="API Token",
                description="Token for Subsonic auth (p=xxx or t=md5(token+salt)&s=salt).",
                required=True,
                default_value="",
                value=token,
            ),
            ConfigEntry(
                key="regenerate_token",
                type=ConfigEntryType.ACTION,
                action="regenerate_token",
                action_label="Regenerate Token",
                label="Regenerate Token",
                description="Generate a new random API token",
            ),
            ConfigEntry(
                key=CONF_SEARCH_SCOPE,
                type=ConfigEntryType.STRING,
                label="Search Scope",
                description="搜索范围: 本地曲库(Library only) / 全部曲库(All sources including online)",
                default_value="library",
                required=True,
                value=search_scope,
                options=[
                    ConfigValueOption(value="library", title="本地曲库 (Library only)"),
                    ConfigValueOption(value="all", title="全部曲库 (All sources including online)"),
                ],
            ),
            # v1.3.0 BUG #71: QQ 音乐/部分音源免费账号只给 88s 试听片段。
            # 默认开启:不去掉试听,直接 stream 88s,客户端收到 EOF 自然切下一首,
            # 避免反复 5 次重试导致 UI 卡死 8s+。
            # 关闭后恢复 v1.2.x 的严格跳过行为(只跳试听,不播 88s)。
            ConfigEntry(
                key=CONF_STREAM_PREVIEWS,
                type=ConfigEntryType.BOOLEAN,
                label="播放试听片段 (Stream previews)",
                description=(
                    "当某音源(QQ音乐免费账号等)只提供 88 秒试听片段时,是否继续播放。"
                    "关闭后会跳过该歌曲并尝试下一个 mapping,可能导致客户端卡在'加载中'。"
                    "默认开启,以避免免费账号场景下大量歌曲被卡住。"
                ),
                default_value=True,
                required=False,
            ),
            # v1.3.2: 高频 debug 日志门控开关,默认关。开启后输出每个 HTTP
            # 请求、封面查找、音频流分支、歌词 fallback 等诊断信息,仅排查问题时临时启用。
            ConfigEntry(
                key=CONF_DEBUG_VERBOSE,
                type=ConfigEntryType.BOOLEAN,
                label="启用详细调试日志 (Verbose debug logs)",
                description=(
                    "默认关闭。开启后会输出每个 HTTP 请求、封面查找、音频流切换"
                    "等诊断信息,日志量会显著增加,仅用于排查问题时临时启用。"
                ),
                default_value=False,
                required=False,
            ),
        )

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...] | None:
        if action == "regenerate_token":
            new_token = secrets.token_hex(16)
            _persist_token(self.mass, self.instance_id, new_token)
            self._api_token = new_token
            return await self.get_config_entries()
        return None

    async def loaded_in_mass(self) -> None:
        # values (UI edits) win over setup_data (only written by the setup flow).
        # Otherwise UI edits would be masked by the original setup-time token.
        token = self.config.get_value(CONF_TOKEN) or self.get_setup_value(CONF_TOKEN, "") or ""
        if not token:
            token = secrets.token_hex(16)
            _persist_token(self.mass, self.instance_id, token)
            self.logger.info("Generated new API token: %s", token)
        else:
            _persist_token(self.mass, self.instance_id, token)
        self._api_token = token
        cb = self.mass.webserver.register_dynamic_route(
            "/rest/*", self._handle_request, "*"
        )
        self._unload_callbacks.append(cb)
        self.logger.info("Amcfy Music Subsonic Bridge ready at /rest/*")

    async def unload(self, is_removed: bool = False) -> None:
        for cb in self._unload_callbacks:
            try:
                cb()
            except Exception:
                pass
        self._unload_callbacks.clear()

    def _verify_token(self, token: str, salt: str) -> bool:
        return (
            hashlib.md5((self._api_token + salt).encode()).hexdigest().lower()
            == token.lower()
        )

    def _check_auth(self, params: dict[str, str]) -> bool:
        p, t, s = params.get("p", ""), params.get("t", ""), params.get("s", "")
        api_key = params.get("apikey", "") or params.get("api_key", "")
        if t and s:
            return self._verify_token(t, s)
        if not p and api_key:
            p = api_key
        if p:
            if p.startswith("enc:"):
                try:
                    p = bytes.fromhex(p[4:]).decode()
                except (ValueError, UnicodeDecodeError):
                    return False
            return p == self._api_token
        return False

    def _respond(self, data=None, status="ok", fmt=None):
        return _xml_response(data, status=status, fmt=fmt or self._response_fmt)

    def _error(self, code=0, message="", status_code=200, fmt=None):
        resp = _xml_response(
            {"error": {"code": code, "message": message}}, status="failed", fmt=fmt or self._response_fmt
        )
        resp.set_status(status_code)
        return resp

    async def _handle_request(self, request: web.Request) -> web.Response:
        if request.method.upper() == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            })
        endpoint = request.path.removesuffix(".view").rsplit("/", 1)[-1]
        params = {k.lower(): (v[0] if isinstance(v, list) else v) if isinstance(v, (str, list)) else str(v) for k, v in request.query.items()}
        if request.method.upper() == "POST" and request.body_exists:
            try:
                form = await request.post()
                for k, v in form.items():
                    params[k.lower()] = str(v)
            except Exception:
                pass
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Basic ") and "p" not in params and "t" not in params:
            try:
                decoded = base64.b64decode(auth_hdr[6:]).decode()
                if ":" in decoded:
                    user, pw = decoded.split(":", 1)
                    params.setdefault("u", user)
                    params["p"] = pw
            except Exception:
                pass
        self._response_fmt = params.get("f", "json")
        if self.config.get_value(CONF_DEBUG_VERBOSE):
            self.logger.debug("Request: %s %s", request.method, request.path_qs)

        if endpoint != "ping" and not self._check_auth(params):
            self.logger.debug("Auth failed for %s: query_keys=%s basic_auth=%s", endpoint, list(params.keys()), bool(auth_hdr))
            return self._error(40, "Wrong username or password", status_code=401)

        handler_name = ENDPOINT_MAP.get(endpoint)
        if not handler_name:
            return self._error(0, f"Unknown endpoint: {endpoint}")

        handler = getattr(self, handler_name, None)
        if not handler:
            return self._error(0, f"Not implemented: {endpoint}")

        try:
            return await handler(request, params)
        except Exception as e:
            self.logger.debug("Error handling %s: %s", endpoint, e)
            return self._error(0, str(e)[:200])

    async def handle_ping(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return self._respond()

    async def handle_get_license(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return self._respond({"license": {"valid": True, "email": "amcfy@local", "key": "", "date": "", "licenseExpires": "2099-12-31T23:59:59"}})

    async def handle_get_scan_status(self, request: web.Request, params: dict[str, str]) -> web.Response:
        try:
            artists = await self.mass.music.artists.library_count()
            albums = await self.mass.music.albums.library_count()
            tracks = await self.mass.music.tracks.library_count()
        except Exception:
            artists = albums = tracks = 0
        return self._respond({"scanStatus": {
            "scanning": False, "count": tracks, "songsCount": tracks,
            "folderCount": albums, "albumCount": albums, "artistCount": artists,
        }})

    async def _get_album_count_map(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        try:
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            for alb in albums:
                if isinstance(alb, Album) and alb.artists:
                    for artist in alb.artists:
                        key = artist.uri or artist.item_id
                        counts[key] = counts.get(key, 0) + 1
        except Exception:
            pass
        return counts

    async def handle_get_artists(self, request: web.Request, params: dict[str, str]) -> web.Response:
        artists = await self.mass.music.artists.library_items(limit=LIBRARY_MAX)
        album_counts = await self._get_album_count_map()
        indexed = _build_index([_artist_dict(a, album_counts.get(a.uri or a.item_id, 0)) for a in artists])
        indexes_xml = {"ignoredArticles": "The El La Los Las Le Les", "index": indexed} if indexed else {"index": []}
        return self._respond({"artists": indexes_xml})

    async def handle_get_music_folders(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return self._respond({"musicFolders": {"musicFolder": [{"id": 1, "name": "Music"}]}})

    async def handle_get_user(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return self._respond({"user": {
            "username": params.get("u", "admin"),
            "adminRole": True, "settingsRole": True, "downloadRole": True,
            "uploadRole": True, "playlistRole": True, "coverArtRole": True,
            "commentRole": True, "streamRole": True, "jukeboxRole": True,
        }})

    async def handle_get_indexes(self, request: web.Request, params: dict[str, str]) -> web.Response:
        artists = await self.mass.music.artists.library_items(limit=LIBRARY_MAX)
        album_counts = await self._get_album_count_map()
        indexed = _build_index([_artist_dict(a, album_counts.get(a.uri or a.item_id, 0)) for a in artists])
        indexes_xml = {"ignoredArticles": "The El La Los Las Le Les", "index": indexed} if indexed else {"index": []}
        return self._respond({"indexes": indexes_xml})

    async def _resolve_artist(self, raw_id: str) -> Artist | None:
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
            if isinstance(item, Artist):
                return item
            return None
        except Exception:
            try:
                return await self.mass.music.artists.get_library_item(raw_id)
            except Exception:
                return None

    async def _resolve_album(self, raw_id: str) -> Album | None:
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
            if isinstance(item, Album):
                return item
            return None
        except Exception:
            try:
                return await self.mass.music.albums.get_library_item(raw_id)
            except Exception:
                return None

    async def _resolve_track(self, raw_id: str) -> Track | None:
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
            if isinstance(item, Track):
                return item
            return None
        except Exception:
            try:
                return await self.mass.music.tracks.get_library_item(raw_id)
            except Exception:
                return None

    async def handle_get_artist(self, request: web.Request, params: dict[str, str]) -> web.Response:
        artist = await self._resolve_artist(params.get("id", ""))
        if not artist:
            return self._error(70, "Artist not found")
        albums: list[Album] = []
        try:
            albums = await self.mass.music.artists.albums(artist.item_id, artist.provider)
        except Exception:
            pass
        sub_albums = []
        for alb in albums:
            tracks_for_alb = []
            try:
                tracks_for_alb = await self.mass.music.albums.tracks(alb.item_id, alb.provider)
            except Exception:
                pass
            dur = sum(getattr(t, "duration", 0) or 0 for t in tracks_for_alb)
            if isinstance(alb, Album):
                sub_albums.append(_album_dict(alb, len(tracks_for_alb), dur))
            else:
                sub_albums.append({
                    "id": alb.uri or alb.item_id,
                    "name": alb.name,
                    "artist": artist.name,
                    "artistId": artist.uri or artist.item_id,
                    "coverArt": _find_image_id(alb) if hasattr(alb, "metadata") else "",
                    "songCount": len(tracks_for_alb),
                    "duration": dur,
                    "created": _format_timestamp(getattr(alb, "date_added", None)),
                    "year": getattr(alb, "year", 0) or 0,
                })
        artist_xml = {
            "id": artist.uri or artist.item_id,
            "name": artist.name,
            "coverArt": _find_image_id(artist),
            "albumCount": len(albums),
        "starred": _format_timestamp(artist.date_added) if artist.favorite else None,
        }
        return self._respond({"artist": {**artist_xml, "album": sub_albums}})

    async def handle_get_album(self, request: web.Request, params: dict[str, str]) -> web.Response:
        album = await self._resolve_album(params.get("id", ""))
        if not album:
            return self._error(70, "Album not found")
        tracks: list[Track] = []
        try:
            tracks = await self.mass.music.albums.tracks(album.item_id, album.provider)
        except Exception:
            pass
        artist = album.artists[0] if album.artists else None
        genres = album.metadata and album.metadata.genres
        dur = sum(getattr(t, "duration", 0) or 0 for t in tracks)
        album_data = {
            "id": album.uri or album.item_id,
            "name": album.name,
            "artist": artist.name if artist else "Unknown",
            "artistId": artist.uri if artist else "",
            "coverArt": _find_image_id(album),
            "songCount": len(tracks),
            "duration": dur,
            "created": _format_timestamp(album.date_added),
            "year": album.year or 0,
            "genre": next(iter(genres), "") if genres else "",
            "starred": _format_timestamp(album.date_added) if album.favorite else None,
        }
        return self._respond({"album": {**album_data, "song": [_song_dict(t) for t in tracks]}})

    async def handle_get_song(self, request: web.Request, params: dict[str, str]) -> web.Response:
        track = await self._resolve_track(params.get("id", ""))
        if not track:
            return self._error(70, "Song not found")
        return self._respond({"song": _song_dict(track)})

    async def handle_get_music_directory(self, request: web.Request, params: dict[str, str]) -> web.Response:
        raw_id = params.get("id", "")
        item = None
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
        except Exception:
            pass
        if not item:
            return self._error(70, "Not found")
        if isinstance(item, Artist):
            albums: list[Album] = []
            try:
                albums = await self.mass.music.artists.albums(item.item_id, item.provider)
            except Exception:
                pass
            children = [{
                "id": a.uri or a.item_id,
                "title": a.name, "isDir": True,
                "parent": item.uri or item.item_id,
                "artist": item.name,
                "artistId": item.uri or item.item_id,
                "coverArt": _find_image_id(a) if hasattr(a, "metadata") else "",
                "year": getattr(a, "year", 0) or 0,
                "type": "album",
            } for a in albums]
            return self._respond({"directory": {
                "id": item.uri or item.item_id,
                "name": item.name,
                "child": children,
            }})
        if isinstance(item, Album):
            tracks: list[Track] = []
            try:
                tracks = await self.mass.music.albums.tracks(item.item_id, item.provider)
            except Exception:
                pass
            return self._respond({"directory": {
                "id": item.uri or item.item_id,
                "name": item.name,
                "child": [_song_dict(t) for t in tracks],
            }})
        if isinstance(item, Track):
            return self._respond({"directory": {
                "id": item.uri or item.item_id,
                "name": item.name,
                "child": [_song_dict(item)],
            }})
        return self._error(70, "Unsupported type")

    async def handle_get_album_list(self, request: web.Request, params: dict[str, str]) -> web.Response:
        atype = params.get("type", "newest")
        size = _safe_int(params.get("size", "50"))
        offset = _safe_int(params.get("offset", "0"))
        is_id3 = request.path.removesuffix(".view").endswith("2")

        albums: list[Album] = []
        if atype == "newest":
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            all_albums.sort(key=lambda a: a.date_added or 0, reverse=True)
            albums = all_albums
        elif atype == "alphabeticalByName":
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, order_by="name")
        elif atype == "alphabeticalByArtist":
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, order_by="sort_name")
        elif atype == "frequent":
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            all_albums.sort(key=lambda a: getattr(a, "play_count", 0) or 0, reverse=True)
            albums = all_albums
        elif atype == "recent":
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            all_albums.sort(key=lambda a: getattr(a, "last_played", 0) or 0, reverse=True)
            albums = [a for a in all_albums if getattr(a, "last_played", 0)]
        elif atype == "starred":
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, favorite=True)
        elif atype == "random":
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            random.shuffle(all_albums)
            albums = all_albums
        elif atype == "byYear":
            from_year = _safe_int(params.get("fromyear", "1900"))
            to_year = _safe_int(params.get("toyear", "2100"))
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            albums = [a for a in all_albums if a.year and from_year <= a.year <= to_year]
        elif atype == "byGenre":
            genre = params.get("genre", "")
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            albums = [a for a in all_albums if a.metadata and a.metadata.genres and genre in a.metadata.genres]
        else:
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)

        sliced = albums[offset:offset + size]
        sub_albums = []
        for alb in sliced:
            if isinstance(alb, Album):
                tracks_for_alb = []
                try:
                    tracks_for_alb = await self.mass.music.albums.tracks(alb.item_id, alb.provider)
                except Exception:
                    pass
                dur = sum(getattr(t, "duration", 0) or 0 for t in tracks_for_alb)
                sub_albums.append(_album_dict(alb, len(tracks_for_alb), dur))
            else:
                sub_albums.append({
                    "id": alb.uri or alb.item_id,
                    "name": alb.name, "artist": "", "artistId": "",
                    "coverArt": _find_image_id(alb) if hasattr(alb, "metadata") else "",
                    "songCount": 0, "duration": 0,
                    "created": _format_timestamp(getattr(alb, "date_added", None)),
                    "year": getattr(alb, "year", 0) or 0,
                })
        wrapper = "albumList2" if is_id3 else "albumList"
        return self._respond({wrapper: {"album": sub_albums}})

    async def handle_search2(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return await self._handle_search(request, params, "search2")

    async def handle_search3(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return await self._handle_search(request, params, "search3")

    def _resolve_search_order(self, order: str, by: str) -> str:
        """Map Navidrome-style search order/by params to MA order_by values."""
        order = (order or "name").lower()
        desc = (by or "ASC").upper() == "DESC"
        base = {
            "playdate": "last_played",
            "played": "last_played",
            "playcount": "play_count",
            "created": "timestamp_added",
            "name": "sort_name",
            "album": "sort_name",
            "artist": "artist_name",
            "year": "year",
            "random": "random",
        }.get(order, "sort_name")
        if base == "random":
            return "random"
        return f"{base}_desc" if desc else base

    async def _handle_search(self, request: web.Request, params: dict[str, str], search_type: str) -> web.Response:
        query = params.get("query", "")
        artist_count = _safe_int(params.get("artistcount", "20"))
        artist_offset = _safe_int(params.get("artistoffset", "0"))
        album_count = _safe_int(params.get("albumcount", "20"))
        album_offset = _safe_int(params.get("albumoffset", "0"))
        song_count = _safe_int(params.get("songcount", "20"))
        song_offset = _safe_int(params.get("songoffset", "0"))

        if not query:
            order_by = self._resolve_search_order(params.get("order", ""), params.get("by", "ASC"))
            tracks = await self.mass.music.tracks.library_items(
                limit=song_count, offset=song_offset, order_by=order_by
            )
            songs = [_song_dict(t) for t in tracks if isinstance(t, Track)]
            key = "searchResult3" if search_type == "search3" else "searchResult2"
            return self._respond({key: {"artist": [], "album": [], "song": songs}})

        scope = self.config.get_value(CONF_SEARCH_SCOPE) or "library"
        library_only = scope == "library"
        results = await self.mass.music.search(
            query,
            media_types=[MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK],
            limit=max(artist_count, album_count, song_count) * 3,
            library_only=library_only,
        )

        artists = [_artist_dict(a) for a in results.artists[artist_offset:artist_offset + artist_count] if isinstance(a, Artist)]
        albums = []
        for a in results.albums[album_offset:album_offset + album_count]:
            if isinstance(a, Album):
                albums.append(_album_dict(a))
            else:
                albums.append({
                    "id": a.uri or a.item_id, "name": a.name, "artist": "", "artistId": "",
                    "coverArt": _find_image_id(a) if hasattr(a, "metadata") else "",
                    "songCount": 0, "duration": 0,
                    "created": _format_timestamp(getattr(a, "date_added", None)),
                    "year": getattr(a, "year", 0) or 0,
                })
        songs = [_song_dict(t) for t in results.tracks[song_offset:song_offset + song_count] if isinstance(t, Track)]

        if search_type == "search3":
            return self._respond({"searchResult3": {"artist": artists, "album": albums, "song": songs}})
        return self._respond({"searchResult2": {"artist": artists, "album": albums, "song": songs}})

    async def handle_random_songs(self, request: web.Request, params: dict[str, str]) -> web.Response:
        size = _safe_int(params.get("size", "10"))
        tracks = await self.mass.music.tracks.library_items(limit=LIBRARY_MAX)
        selected = random.sample(list(tracks), min(size, len(tracks)))
        return self._respond({"randomSongs": {"song": [_song_dict(t) for t in selected]}})

    async def handle_get_genres(self, request: web.Request, params: dict[str, str]) -> web.Response:
        genres: dict[str, int] = {}
        tracks = await self.mass.music.tracks.library_items(limit=LIBRARY_MAX)
        for t in tracks:
            if t.metadata and t.metadata.genres:
                for g in t.metadata.genres:
                    genres[g] = genres.get(g, 0) + 1
        glist = [{"genre": {"value": name, "songCount": count, "albumCount": 0}} for name, count in sorted(genres.items())]
        return self._respond({"genres": {"genre": glist}})

    async def handle_songs_by_genre(self, request: web.Request, params: dict[str, str]) -> web.Response:
        genre = params.get("genre", "")
        count = _safe_int(params.get("count", "10"))
        offset = _safe_int(params.get("offset", "0"))
        tracks = await self.mass.music.tracks.library_items(limit=LIBRARY_MAX)
        if genre:
            filtered = [t for t in tracks if t.metadata and t.metadata.genres and genre in t.metadata.genres]
        else:
            filtered = list(tracks)
        return self._respond({"songsByGenre": {"song": [_song_dict(t) for t in filtered[offset:offset + count]]}})

    async def handle_get_cover_art(self, request: web.Request, params: dict[str, str]) -> web.Response:
        raw_id = params.get("id", "")
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
            if not item:
                return web.Response(body=PLACEHOLDER_PNG, content_type="image/png", status=200, headers=CORS_HEADERS)
            images: list[MediaItemImage] = []
            if item.metadata and item.metadata.images:
                images = list(item.metadata.images)
                if self.config.get_value(CONF_DEBUG_VERBOSE):
                    self.logger.debug("Cover: %d images on item, first: path=%s prov=%s", len(images), images[0].path, images[0].provider)
            if not images and isinstance(item, Track):
                album = item.album
                if album and not isinstance(album, str):
                    if hasattr(album, "metadata") and album.metadata and album.metadata.images:
                        images = list(album.metadata.images)
                        if self.config.get_value(CONF_DEBUG_VERBOSE):
                            self.logger.debug("Cover: %d images on album", len(images))
            if not images:
                return web.Response(body=PLACEHOLDER_PNG, content_type="image/png", status=200, headers=CORS_HEADERS)
            img = images[0]
            img_bytes = await get_image_data(self.mass, img.path, img.provider)
            if img_bytes and len(img_bytes) > 100:
                return web.Response(body=img_bytes, content_type=_guess_image_mime(img_bytes), headers=CORS_HEADERS)
            if self.config.get_value(CONF_DEBUG_VERBOSE):
                self.logger.debug("Cover: image too small (%d bytes) for %s", len(img_bytes) if img_bytes else 0, raw_id)
        except Exception as e:
            # 真异常降级为 warning,避免 verbose 关闭后丢失诊断线索
            self.logger.warning("Cover art error for %s: %s", raw_id, e)
        return web.Response(body=PLACEHOLDER_PNG, content_type="image/png", status=200, headers=CORS_HEADERS)

    async def _get_lyrics_cached(self, track) -> tuple | None:
        cache_key = track.uri or track.item_id
        now = time.time()
        if cache_key in self._lyrics_cache:
            ts, data = self._lyrics_cache[cache_key]
            if now - ts < CACHE_TTL:
                return data
        plain = None
        lrc = None
        # Fast path: read whatever lyrics are already attached to the track / cached
        # by MA (no network round-trip). This avoids 30s+ waits on repeat plays.
        try:
            get_lyrics = getattr(self.mass.metadata, "get_track_lyrics", None)
            if get_lyrics is not None:
                result = await get_lyrics(track)
                if result and (result[0] or result[1]):
                    plain = result[0]
                    lrc = result[1]
        except Exception as e:
            if self.config.get_value(CONF_DEBUG_VERBOSE):
                self.logger.debug("get_track_lyrics (fast) failed for %s: %s", track.uri, e)

        # Slow path: only if we still have no lyrics, force-refresh rich metadata
        # (triggers lyrics providers like netease_lyrics). Best-effort — for pure
        # online provider items the final write-to-library step raises, but lyrics
        # are already merged into track.metadata before that, so we swallow it.
        if not (plain or lrc):
            try:
                if track.metadata and track.metadata.lyrics:
                    track.metadata.lyrics = None
                    track.metadata.lrc_lyrics = None
                update_meta = getattr(self.mass.metadata, "_update_track_metadata", None)
                if update_meta is not None:
                    try:
                        await update_meta(track, force_refresh=True)
                    except Exception as e:
                        if self.config.get_value(CONF_DEBUG_VERBOSE):
                            self.logger.debug(
                                "lyrics metadata refresh (non-fatal) for %s: %s", track.uri, e
                            )
                get_lyrics = getattr(self.mass.metadata, "get_track_lyrics", None)
                if get_lyrics is not None:
                    result = await get_lyrics(track)
                    if result:
                        plain = result[0]
                        lrc = result[1]
            except Exception as e:
                if self.config.get_value(CONF_DEBUG_VERBOSE):
                    self.logger.debug("get_track_lyrics failed for %s: %s\n%s", track.uri, e, __import__("traceback").format_exc())
        if not lrc:
            try:
                legacy = getattr(self.mass.metadata, "_get_track_lyrics", None)
                if legacy is not None:
                    raw = await legacy(track)
                    lrc = raw[1] if raw else None
            except Exception:
                pass
        data = (plain, lrc)
        if len(self._lyrics_cache) > 200:
            cutoff = now - CACHE_TTL
            self._lyrics_cache = {k: v for k, v in self._lyrics_cache.items() if v[0] > cutoff}
        self._lyrics_cache[cache_key] = (now, data)
        return data

    async def handle_get_lyrics(self, request: web.Request, params: dict[str, str]) -> web.Response:
        artist_name = params.get("artist", "")
        title = params.get("title", "")
        track = None
        text = ""
        lrc = ""
        try:
            results = await self.mass.music.search(
                f"{artist_name} {title}",
                media_types=[MediaType.TRACK],
                limit=1,
                library_only=True,
            )
            if results.tracks:
                track = results.tracks[0]
                lyrics_data = await self._get_lyrics_cached(track)
                if lyrics_data:
                    text = lyrics_data[0] or ""
                    lrc = lyrics_data[1] if len(lyrics_data) > 1 else ""
        except Exception:
            pass
        value = lrc if lrc else text
        return self._respond({
            "lyrics": {
                "artist": artist_name,
                "title": title,
                "value": value,
                "content": value,
            }
        })

    async def handle_get_lyrics_by_song_id(self, request: web.Request, params: dict[str, str]) -> web.Response:
        track = await self._resolve_track(params.get("id", ""))
        if not track:
            return self._error(70, "Song not found")
        lyrics_data = await self._get_lyrics_cached(track)
        lyrics_text = lyrics_data[0] if lyrics_data else None
        lrc_text = lyrics_data[1] if lyrics_data and len(lyrics_data) > 1 else None

        text = lrc_text if lrc_text else (lyrics_text or "")
        if not text:
            return self._respond({"lyricsList": {"lyrics": [], "structuredLyrics": []}})

        display_artist = track.artists[0].name if track.artists else ""

        structured = []
        if lrc_text:
            lines = []
            for line in lrc_text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^\[(\d+):(\d+)(?:[.:](\d+))?\](.*)", line)
                if m:
                    mins = int(m.group(1))
                    secs = int(m.group(2))
                    frac_str = m.group(3)
                    frac = int(frac_str) if frac_str else 0
                    if frac > 999:
                        frac = int(round(frac / 10)) if frac >= 1000 else frac
                    start_ms = mins * 60000 + secs * 1000 + frac
                    display = m.group(4).strip()
                    if display:
                        lines.append({"start": start_ms, "value": display})
            if lines:
                structured.append({
                    "displayArtist": display_artist,
                    "displayTitle": track.name,
                    "lang": "chi",
                    "synced": True,
                    "line": lines,
                })
        if not structured:
            structured.append({
                "displayArtist": display_artist,
                "displayTitle": track.name,
                "lang": "chi",
                "synced": False,
                "line": [{"start": 0, "value": text}],
            })

        return self._respond({
            "lyricsList": {
                "lyrics": [{"content": text, "artist": display_artist, "title": track.name}],
                "structuredLyrics": structured,
            }
        })

    async def handle_star(self, request: web.Request, params: dict[str, str]) -> web.Response:
        # BUG #75: 同 unstar,_handle_request line 536 已经 lowercase 所有 key。
        # 这里必须用小写 key 才能拿到 SID。
        ids = list(filter(None, (params.get(k, "") for k in ("id", "albumid", "artistid"))))
        for sid in ids:
            try:
                await self.mass.music.add_item_to_favorites(sid)
            except Exception:
                pass
        return self._respond()

    async def handle_unstar(self, request: web.Request, params: dict[str, str]) -> web.Response:
        # BUG #75: _handle_request 已经在 line 536 把所有 key 强制 lowercase,
        # 这里 get 必须用小写 key ("artistid"/"albumid"/"id")才能拿到 SID。
        # 之前直接 get("artistId") 永远拿到空串,ids=[],unstar no-op。
        ids = list(filter(None, (params.get(k, "") for k in ("id", "albumid", "artistid"))))
        for sid in ids:
            # BUG #73 v1.3.3: 之前传 item.item_id(provider-instance id)给
            # remove_item_from_favorites,MA 实际要 library_item_id,导致 unstar
            # 永远无效(getStarred 下次又出现)。改为先用 get_library_item_by_prov_id
            # 拿到 library 里的 item_id 再移除。
            try:
                await self._unstar_one(sid)
            except Exception:
                pass
        return self._respond()

    async def _unstar_one(self, sid: str) -> None:
        """从 MA 库移除单个收藏。返回 None 表示无操作(解析失败/未在库)。"""
        # BUG #75 v1.3.3: MA 2.10.x remove_item_from_favorites 第二参数要
        # library_item_id(provider-instance id 不行)。先用 get_library_item_by_prov_id
        # 把 provider-instance item.item_id 映射到 library_item_id 再移除。
        try:
            item = await self.mass.music.get_item_by_uri(sid)
            if not item:
                self.logger.debug("unstar: get_item_by_uri(%r) -> None, skip", sid)
                return
            lib_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=item.media_type,
                item_id=item.item_id,
                provider_instance_id_or_domain=item.provider,
            )
            if not lib_item:
                # 不在库里(从未 star 过),no-op
                return
            await self.mass.music.remove_item_from_favorites(item.media_type, lib_item.item_id)
        except Exception as e:
            self.logger.warning("unstar failed for %r: %r", sid, e)

    async def handle_set_rating(self, request: web.Request, params: dict[str, str]) -> web.Response:
        # v1.3.3: OpenSubsonic setRating。MA 库没有 user_rating 字段,只支持
        # favorite boolean,所以映射为: rating>=1 → star, rating==0 → unstar。
        # 大多数客户端只展示"已收藏",这个映射无可见差异。
        sid = params.get("id", "")
        rating = _safe_int(params.get("rating", "0"))
        if not sid:
            return self._error(10, "id parameter required")
        try:
            if rating > 0:
                await self.mass.music.add_item_to_favorites(sid)
            else:
                await self._unstar_one(sid)
        except Exception:
            pass
        return self._respond()

    async def handle_get_starred(self, request: web.Request, params: dict[str, str]) -> web.Response:
        # v1.3.3: OpenSubsonic tabKey 过滤。空串 = 全部(旧行为),否则只返回
        # artists / albums / songs 之一。客户端不读的部分跳过,减少 MA 查询。
        is_id3 = request.path.removesuffix(".view").endswith("2")
        wrapper = "starred2" if is_id3 else "starred"
        tab_key = params.get("tabkey", "").strip().lower()
        include_artists = tab_key in ("", "artists")
        include_albums = tab_key in ("", "albums")
        include_songs = tab_key in ("", "songs")
        payload: dict = {}
        album_counts = await self._get_album_count_map()
        if include_artists:
            artists = await self.mass.music.artists.library_items(limit=LIBRARY_MAX, favorite=True)
            payload["artist"] = [_artist_dict(a, album_counts.get(a.uri or a.item_id, 0)) for a in artists]
        else:
            payload["artist"] = []
        if include_albums:
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, favorite=True)
            payload["album"] = [_album_dict(a) for a in albums]
        else:
            payload["album"] = []
        if include_songs:
            tracks = await self.mass.music.tracks.library_items(limit=LIBRARY_MAX, favorite=True)
            payload["song"] = [_song_dict(t) for t in tracks]
        else:
            payload["song"] = []
        return self._respond({wrapper: payload})

    async def handle_get_playlists(self, request: web.Request, params: dict[str, str]) -> web.Response:
        playlists = await self.mass.music.playlists.library_items(limit=LIBRARY_MAX)

        async def _count(pl):
            cnt = 0
            dur = 0
            try:
                async for t in self.mass.music.playlists.tracks(pl.item_id, pl.provider):
                    if isinstance(t, Track):
                        cnt += 1
                        dur += t.duration or 0
            except Exception:
                pass
            return pl, cnt, dur

        results = await asyncio.gather(*[_count(p) for p in playlists])
        plist = []
        for p, cnt, dur in results:
            if cnt == 0:
                continue
            plist.append({
                "id": p.uri or p.item_id, "name": p.name,
                "owner": getattr(p, "owner", "") or "admin",
                "public": False,
                "songCount": cnt,
                "duration": dur,
                "created": _format_timestamp(getattr(p, "date_added", None)),
                "coverArt": _find_image_id(p) if hasattr(p, "metadata") else "",
            })
        return self._respond({"playlists": {"playlist": plist}})

    async def handle_get_playlist(self, request: web.Request, params: dict[str, str]) -> web.Response:
        raw_id = params.get("id", "")
        playlist = None
        try:
            item = await self.mass.music.get_item_by_uri(raw_id)
            if isinstance(item, Playlist):
                playlist = item
        except Exception:
            pass
        if not playlist:
            try:
                playlist = await self.mass.music.playlists.get_library_item(raw_id)
            except Exception:
                pass
        if not playlist:
            return self._error(70, "Playlist not found")

        tracks: list[Track] = []
        try:
            async for t in self.mass.music.playlists.tracks(playlist.item_id, playlist.provider):
                if isinstance(t, Track):
                    tracks.append(t)
        except Exception:
            pass

        dur = sum(getattr(t, "duration", 0) or 0 for t in tracks)
        return self._respond({
            "playlist": {
                "id": playlist.uri or playlist.item_id,
                "name": playlist.name,
                "owner": getattr(playlist, "owner", "") or "admin",
                "public": False,
                "songCount": len(tracks),
                "duration": dur,
                "created": _format_timestamp(getattr(playlist, "date_added", None)),
                "entry": [_song_dict(t) for t in tracks],
            },
        })

    async def handle_scrobble(self, request: web.Request, params: dict[str, str]) -> web.Response:
        track = await self._resolve_track(params.get("id", ""))
        if track:
            try:
                submission = params.get("submission", "true").lower() != "false"
                await self.mass.music.mark_item_played(
                    track,
                    fully_played=submission,
                    seconds_played=_safe_int(params.get("time", "0")),
                )
            except Exception:
                pass
        return self._respond()

    async def handle_get_artist_info2(self, request: web.Request, params: dict[str, str]) -> web.Response:
        count = _safe_int(params.get("count", 25))
        info = {"artistInfo2": {
            "biography": "", "musicBrainzId": "", "lastFmUrl": "",
            "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "",
            "similarArtist": [],
        }}
        artist = await self._resolve_artist(params.get("id", ""))
        if artist:
            if artist.metadata:
                info["artistInfo2"]["biography"] = artist.metadata.description or ""
            try:
                similar = await self.mass.music.artists.similar_artists(
                    artist.item_id, artist.provider, limit=count
                )
                info["artistInfo2"]["similarArtist"] = [_artist_dict(a) for a in similar]
            except Exception:
                pass
        return self._respond(info)

    async def handle_get_album_info2(self, request: web.Request, params: dict[str, str]) -> web.Response:
        return self._respond({"albumInfo2": {
            "notes": "", "musicBrainzId": "", "lastFmUrl": "",
            "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "",
        }})

    async def handle_open_subsonic_extensions(self, request: web.Request, params: dict[str, str]) -> web.Response:
        exts = [{"extension": {"name": k, "versions": [1]}} for k in ("formPost", "songLyrics")]
        return self._respond({"openSubsonicExtensions": exts})

    async def _resolve_source(self, stream_details, pm):
        """Try multiple strategies to resolve the audio source path/URL."""
        for attr in ("path", "url", "data", "stream_url", "media_url", "content_uri", "source", "uri"):
            val = getattr(stream_details, attr, None)
            if val and isinstance(val, str) and len(val) > 5:
                return val
            if val and isinstance(val, dict):
                for k in ("url", "path", "stream_url", "uri"):
                    v = val.get(k)
                    if v and isinstance(v, str) and len(v) > 5:
                        return v
        if pm:
            url = getattr(pm, "url", None)
            if url and isinstance(url, str) and len(url) > 5:
                return url
            pid = getattr(pm, "item_id", None)
            if pid and isinstance(pid, str) and (pid.startswith("/") or "://" in pid):
                return pid
        sid = getattr(stream_details, "item_id", None)
        if sid and isinstance(sid, str) and len(sid) > 10:
            return sid
        return None

    async def _stream_file(self, resp, file_path, seek_pos, request=None):
        """Stream a local file with Range support (async I/O)."""
        loop = asyncio.get_running_loop()
        try:
            file_size = await loop.run_in_executor(None, os.path.getsize, file_path)
            if seek_pos:
                resp.headers["Content-Range"] = "bytes {}-{}/{}".format(seek_pos, file_size - 1, file_size)
            resp.headers["Content-Length"] = str(file_size - seek_pos)
            fd = await loop.run_in_executor(None, lambda: open(file_path, "rb"))
            try:
                if seek_pos:
                    await loop.run_in_executor(None, fd.seek, seek_pos)
                while True:
                    if request and getattr(request, 'transport', None) and request.transport.is_closing():
                        return resp
                    chunk = await loop.run_in_executor(None, fd.read, 65536)
                    if not chunk:
                        break
                    await resp.write(chunk)
            finally:
                await loop.run_in_executor(None, fd.close)
            return resp
        except (ConnectionResetError, ConnectionAbortedError, ConnectionError):
            return resp
        except Exception as e:
            self.logger.debug("File stream fail: %s: %s", file_path, e)
            return None

    async def _stream_via_ffmpeg(self, resp, url, suffix, request=None):
        """Stream audio via ffmpeg subprocess (bypasses HTTP proxy issues with CDNs)."""
        headers = await self._get_browser_headers(url)
        headers_str = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        cmd = [
            "ffmpeg",
            "-analyzeduration", "50000", "-probesize", "50000",
            "-flags", "+low_delay",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-headers", headers_str,
            "-i", url,
            "-c", "copy", "-f", suffix,
            "-fflags", "+nobuffer",
            "-",
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            buffer = b""
            while True:
                if request and getattr(request, 'transport', None) and request.transport.is_closing():
                    proc.kill()
                    return resp
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) >= 65536 or len(chunk) == 0:
                    await resp.write(buffer)
                    buffer = b""
            if buffer:
                await resp.write(buffer)
            await proc.wait()
            err_text = (await proc.stderr.read()).decode("utf-8", errors="replace")
            if proc.returncode != 0:
                _host = url.split("//", 1)[-1].split("/", 1)[0] if isinstance(url, str) and "://" in url else url[:60]
                self.logger.info(
                    "stream: ffmpeg failed | host=%s | returncode=%d | err_first_line=%s",
                    _host, proc.returncode, (err_text.strip().splitlines() or [""])[0][:200],
                )
            return resp
        except (ConnectionResetError, ConnectionAbortedError, ConnectionError):
            return resp
        except Exception as e:
            _host = url.split("//", 1)[-1].split("/", 1)[0] if isinstance(url, str) and "://" in url else url[:60]
            self.logger.info(
                "stream: ffmpeg exception | host=%s | err=%s: %s",
                _host, type(e).__name__, e,
            )
            return None
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def _get_browser_headers(self, url: str) -> dict[str, str]:
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        if "qq.com" in url or "qpic.cn" in url:
            h["Referer"] = "https://y.qq.com/"
            h["Origin"] = "https://y.qq.com"
        if "126.net" in url or "163.com" in url or "music.163.com" in url:
            h["Referer"] = "https://music.163.com/"
            h["Origin"] = "https://music.163.com"
        return h

    @staticmethod
    def _classify_probe_bytes(head: bytes) -> str:
        """BUG #62 v1.2.6: 把嗅探读到的首块字节分类为可读描述(只用于日志)。

        注意:嗅探和首块魔数识别已 inline 到 _try_stream_provider 内,
        这里只负责给失败时生成可读日志(不再发独立 GET)。
        魔数识别:
          - FLAC: b'fLaC'                  (66 4c 61 43)
          - MP3:  b'ID3' 或 0xFF 0xFB/F3/F2/FA
          - OGG:  b'OggS'                  (4f 67 67 53)
          - WAV:  b'RIFF'                  (52 49 46 46)
        """
        if not head:
            return "empty body"
        if head[:4] == b"fLaC":
            return "flac"
        if head[:3] == b"ID3":
            return f"got ID3-tagged MP3 (head={head[:8].hex()})"
        if len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2, 0xFA):
            return f"got raw MP3 (head={head[:6].hex()})"
        if head[:4] == b"OggS":
            return f"got OGG (head={head[:8].hex()})"
        if head[:4] == b"RIFF":
            return f"got WAV (head={head[:8].hex()})"
        return f"unknown encoding (head={head[:8].hex()})"

    async def _stream_data(self, resp, data, chunk_size=65536):
        if data is None:
            return resp
        if isinstance(data, bytes):
            for i in range(0, len(data), chunk_size):
                await resp.write(data[i:i + chunk_size])
            return resp
        try:
            async for chunk in data:
                if isinstance(chunk, bytes):
                    await resp.write(chunk)
                elif isinstance(chunk, str):
                    await resp.write(chunk.encode())
            return resp
        except Exception:
            return resp

    async def _proxy_stream(self, resp, url, request, seek_pos, suffix, retries=1,
                            upstream_resp=None, pre_chunk=b""):
        """Proxy stream with producer/consumer buffering (BUG #45 2026-09-05).

        解耦"上游拉"与"客户端写",消除毛刺延迟导致的 APP "网络不给力"停止。
        - 上游读到 chunk → put 到 asyncio.Queue(满则 backpressure)
        - client_task 从 Queue → resp.write(64KB flush一次,保首字节快)
        - 客户端断 → upstream_task 立即 cancel,释放 CDN 流量
        - 上游 total=180s(BUG #63 v1.2.6: 30s 切大 FLAC 慢节点太短,改 120s;v1.3.0: 120s 仍不够 80MB+慢节点,改 180s),背压保住内存 ≤ 4MB
        - 仅 0 字节 + 上游错才降级 ffmpeg(原"3 次 retry 才降级"太慢)

        BUG #62 v1.2.6 新参数:
          - upstream_resp: caller 已经嗅探过的 aiohttp.ClientResponse。
            非 None 时 _proxy_stream 不再自己发 GET,直接消费 caller 提供的 src
            (嗅探和 stream 共享同一 connection,避免 CDN 节点不一致)。
            此时 src 的生命周期由 caller 管理(用 try/finally release)。
          - pre_chunk: caller 在嗅探时已经从 src.content 读到的字节。
            需要在 stream 开头先发给客户端(然后再继续读 src.content 剩余字节)。
        """
        proxy_headers = await self._get_browser_headers(url)
        _host = url.split("//", 1)[-1].split("/", 1)[0] if isinstance(url, str) and "://" in url else url[:60]
        # BUG #59: 网易云 CDN(*.music.126.net,如 m701/m801/d1.* 子域名)对 Range 请求偷换内容——
        # seek_pos>0 时给客户端返回 MP3 字节流(Content-Type 仍是 audio/mpeg),
        # 而原始资源是 FLAC。客户端按 FLAC 解码 MP3 → DecodeError。
        # 检测到网易云 host 时,去掉 Range 头,永远拉完整 FLAC,本地按 seek_pos 切片转发。
        # BUG #60: v1.2.2 漏掉所有子域名——host 是 m701.music.126.net,后缀 .music.126.net 带前导点,
        # endswith("music.126.net") 永远 False。改为精确匹配 + 子域后缀。
        _is_netease = isinstance(_host, str) and (_host == "music.126.net" or _host.endswith(".music.126.net"))
        if seek_pos:
            proxy_headers["Range"] = f"bytes={seek_pos}-"
            if _is_netease:
                # 网易云特殊处理:不传 Range 头,让上游返回完整 FLAC(避免被偷换为 MP3)
                proxy_headers.pop("Range", None)
                self.logger.info(
                    "stream: BUG #59 netease range strip | host=%s | seek=%d | drop Range to get full FLAC",
                    _host, seek_pos,
                )
        import time as _time
        _t_start = _time.monotonic()

        BUFFER_MAX = 2 * 1024 * 1024        # 2 MB 队列上限(BUG #60: 网易云慢节点 1.2MB/s 时,
                                                # 4MB 队列会让客户端等太满;减到 2MB 加快 flush 节奏)
        UPSTREAM_CHUNK = 256 * 1024         # 上游每次 256 KB(减少 syscall)
        CLIENT_PEEK = 64 * 1024             # 客户端 flush 阈值 64 KB(BUG #60: 16KB 让 FLAC 解码器
                                                # 凑不齐 STREAMINFO + 首帧,在内部等下一批数据时 timeout;
                                                # 64KB 是 FLAC/Opus 解码器典型启动 buffer)
        _qsize = max(1, BUFFER_MAX // UPSTREAM_CHUNK)

        # ===== 切歌快速释放(BUG #47) =====
        # 客户端切歌 → client_task 检测到 transport 关 → 设 _shared["aborted"]=True 并主动
        # release() 上游 aiohttp 响应。upstream_task 下一次 while 检查到 aborted 立刻 return。
        # 不靠 cancel() 的异步取消,避免切歌后还持续占用 CDN 带宽 / 连接池槽位几十毫秒到几秒。
        _shared = {
            "src": None,           # aiohttp 上游响应引用
            "aborted": False,      # 客户端断开信号(供 upstream_task 检查)
            "cl": None,            # BUG #58: 上游 Content-Length(upstream_task 内赋值,_proxy_stream 主作用域读)
            "cr": None,            # BUG #58: 上游 Content-Range
        }

        async def _release_upstream() -> None:
            """BUG #47: 主动释放上游 aiohttp 响应,让 CDN 连接立刻回到连接池。

            client_task 检测到客户端断开时调用。
            upstream_task 下一次 while 检查到 _shared["aborted"] 会立刻 return,
            此函数同时调用 src.release() 提前关闭 aiohttp 响应,避免:
              - cancel() 是异步的,要等下一次 await 才生效(几十 ms ~ 几 s)
              - 上游 aiohttp 连接占住 connector pool slot 不释放
              - 切歌太快的下一首请求排队等连接
            """
            _shared["aborted"] = True
            s = _shared["src"]
            if s is not None and not s.closed:
                try:
                    await s.release()
                except Exception:
                    pass

        buffer_queue: asyncio.Queue = asyncio.Queue(maxsize=_qsize)
        upstream_err: list = []
        bytes_produced = 0

        # ===== TEMP DIAG (BUG #46) =====
        # 诊断 APP 端 "Failed to parse HTTP, 72 is expected to be a Hex digit" 错误
        # 看 amcfy 实际给客户端写的是什么内容。诊断完会删掉。
        async def upstream_task():
            nonlocal bytes_produced
            _fb_logged = False
            # BUG #59: 网易云拿完整 FLAC 后,需要丢弃前 seek_pos 字节再转发给客户端
            # seek_pos 一般是 FLAC header 大小(约 8KB),远小于 UPSTREAM_CHUNK
            _netease_skip = seek_pos if _is_netease else 0
            _is_caller_src = upstream_resp is not None
            try:
                if _is_caller_src:
                    # BUG #62 v1.2.6: caller 已嗅探过的 src,直接消费它
                    # 不在这里用 async with(src 由 caller 管理生命周期)
                    src = upstream_resp
                    _shared["src"] = src
                    if src.status not in (200, 206):
                        upstream_err.append(RuntimeError(f"upstream {src.status}"))
                        self.logger.info(
                            "stream: proxy upstream non-2xx (caller src) | host=%s | status=%d",
                            _host, src.status,
                        )
                        return
                    up_cl = src.headers.get("Content-Length")
                    up_cr = src.headers.get("Content-Range")
                    _shared["cl"] = up_cl
                    _shared["cr"] = up_cr
                    if src.headers.get("Accept-Ranges") and not resp.headers.get("Accept-Ranges"):
                        resp.headers["Accept-Ranges"] = src.headers["Accept-Ranges"]
                    # 处理 caller 嗅探时已经读到的 pre_chunk 字节(8 字节魔数)
                    # BUG #63 v1.2.6 hotfix: 不要直接修改函数参数 pre_chunk,
                    # Python 看到函数体内有 pre_chunk= 赋值就会把整个函数的 pre_chunk
                    # 当作 local(即使该赋值在不会被执行的分支),导致第一处
                    # `if pre_chunk:` 就抛 UnboundLocalError(cannot access local variable)。
                    # 用局部变量 _remaining 代替,函数参数 pre_chunk 保持不变。
                    if pre_chunk:
                        _remaining = pre_chunk
                        if _netease_skip > 0:
                            if len(_remaining) <= _netease_skip:
                                _netease_skip -= len(_remaining)
                                _remaining = b""
                            else:
                                _remaining = _remaining[_netease_skip:]
                                _netease_skip = 0
                        if _remaining:
                            _fb_logged = True
                            self.logger.info(
                                "stream: buffered first byte (pre_chunk from sniff) | host=%s | latency=%.3fs | cl=%s | sniff_bytes=%d",
                                _host, _time.monotonic() - _t_start, up_cl or "?", len(_remaining),
                            )
                            await buffer_queue.put(_remaining)
                            bytes_produced += len(_remaining)
                    # 正常读 src.content 剩余字节
                    while True:
                        if _shared["aborted"] or (request.transport and request.transport.is_closing()):
                            self.logger.info(
                                "stream: upstream aborted (client closed) | host=%s | bytes=%d",
                                _host, bytes_produced,
                            )
                            return
                        chunk = await src.content.read(UPSTREAM_CHUNK)
                        if not chunk:
                            return
                        if _netease_skip > 0:
                            if len(chunk) <= _netease_skip:
                                _netease_skip -= len(chunk)
                                continue
                            chunk = chunk[_netease_skip:]
                            _netease_skip = 0
                        if not _fb_logged:
                            _fb_logged = True
                            self.logger.info(
                                "stream: buffered first byte | host=%s | latency=%.3fs | cl=%s",
                                _host, _time.monotonic() - _t_start, up_cl or "?",
                            )
                        await buffer_queue.put(chunk)
                        bytes_produced += len(chunk)
                    return  # caller-src 路径结束(由 caller 在外层 release src)

                # 老路径:caller 没提供 src,自己 GET
                async with self.mass.http_session.get(
                    url, headers=proxy_headers,
                    # BUG #63 v1.2.6: 30s 太短,切大 FLAC 慢节点 30s 内读不完;改为 120s
                    timeout=aiohttp.ClientTimeout(total=180, connect=5)
                ) as src:
                    _shared["src"] = src  # 让 client_task 能主动 release() 上游响应(BUG #47)
                    if src.status not in (200, 206):
                        upstream_err.append(RuntimeError(f"upstream {src.status}"))
                        self.logger.info(
                            "stream: proxy upstream non-2xx | host=%s | status=%d",
                            _host, src.status,
                        )
                        return
                    up_cl = src.headers.get("Content-Length")
                    up_cr = src.headers.get("Content-Range")
                    # BUG #58: 把上游 Content-Length/Range 提升到 _shared,供 _proxy_stream 主作用域读取
                    # (嵌套函数内的局部变量对父作用域不可见,直接引用会 NameError)
                    _shared["cl"] = up_cl
                    _shared["cr"] = up_cr
                    # BUG #61: 不在 upstream_task 写 resp.headers["Content-Length"] / Content-Range。
                    # await resp.prepare(request) 已经在 caller 调用过,headers 已冻结,
                    # 这里写也发不到客户端;而且对网易云 seek 场景(file_size=0),
                    # caller 没设 Content-Length/Range,aiohttp 自动 chunked,
                    # 客户端按 chunk 流式接收,不会因为 Content-Length 与实际字节数不符而断开。
                    if src.headers.get("Accept-Ranges") and not resp.headers.get("Accept-Ranges"):
                        resp.headers["Accept-Ranges"] = src.headers["Accept-Ranges"]
                    while True:
                        # BUG #47: 客户端切歌信号优先检查,即使 transport 还没标记 closing 也立刻停
                        if _shared["aborted"] or (request.transport and request.transport.is_closing()):
                            self.logger.info(
                                "stream: upstream aborted (client closed) | host=%s | bytes=%d",
                                _host, bytes_produced,
                            )
                            return
                        chunk = await src.content.read(UPSTREAM_CHUNK)
                        if not chunk:
                            return
                        # BUG #59: 网易云特殊处理——拉完整 FLAC 后,丢弃前 seek_pos 字节再入队
                        # 跳过的字节不计入 bytes_produced(客户端实际没收到)
                        if _netease_skip > 0:
                            if len(chunk) <= _netease_skip:
                                # 整个 chunk 都属于要跳过的部分,直接丢弃继续读
                                _netease_skip -= len(chunk)
                                continue
                            chunk = chunk[_netease_skip:]
                            _netease_skip = 0
                        if not _fb_logged:
                            _fb_logged = True
                            self.logger.info(
                                "stream: buffered first byte | host=%s | latency=%.3fs | cl=%s",
                                _host, _time.monotonic() - _t_start, up_cl or "?",
                            )
                        await buffer_queue.put(chunk)
                        bytes_produced += len(chunk)
            except (ConnectionResetError, ConnectionAbortedError, aiohttp.ClientPayloadError, ConnectionError) as e:
                upstream_err.append(e)
                self.logger.warning(
                    "stream: upstream connection error | host=%s | bytes=%d | err=%s: %s",
                    _host, bytes_produced, type(e).__name__, e,
                )
            except asyncio.TimeoutError as e:
                upstream_err.append(e)
                self.logger.warning(
                    "stream: upstream timeout | host=%s | bytes=%d | total=%ds",
                    _host, bytes_produced,
                    # BUG #63 v1.2.7: 老代码 hardcode 30s;改成读 proxy_headers 实际 timeout 不方便,
                    # 改 hardcode 180 与 _proxy_stream 的 ClientTimeout(total=180) 保持一致
                    # (caller-src 路径下,触发 timeout 的可能是嗅探时的 5s,aiohttp.ClientTimeout 绑整个请求)
                    120,
                )
            except Exception as e:
                upstream_err.append(e)
                self.logger.warning(
                    "stream: upstream unexpected error | host=%s | bytes=%d | err=%s: %s",
                    _host, bytes_produced, type(e).__name__, e,
                )
            finally:
                await buffer_queue.put(None)

        async def client_task():
            peeked: list = []
            peeked_size = 0
            _client_aborted = False
            _client_aborted_bytes = 0
            while True:
                chunk = await buffer_queue.get()
                if chunk is None:
                    if peeked:
                        try:
                            await resp.write(b"".join(peeked))
                        except (ConnectionResetError, ConnectionAbortedError) as e:
                            _client_aborted = True
                            _client_aborted_bytes = bytes_produced
                            _shared["aborted"] = True
                            await _release_upstream()
                            self.logger.warning(
                                "stream: client closed during flush | host=%s | upstream_bytes=%d | err=%s",
                                _host, bytes_produced, e,
                            )
                            break
                        except Exception as e:
                            _shared["aborted"] = True
                            await _release_upstream()
                            self.logger.warning(
                                "stream: client write failed (final flush) | host=%s | err=%s: %s",
                                _host, type(e).__name__, e,
                            )
                            break
                    break
                peeked.append(chunk)
                peeked_size += len(chunk)
                if peeked_size >= CLIENT_PEEK:
                    try:
                        await resp.write(b"".join(peeked))
                    except (ConnectionResetError, ConnectionAbortedError) as e:
                        _client_aborted = True
                        _client_aborted_bytes = bytes_produced
                        _shared["aborted"] = True
                        await _release_upstream()
                        self.logger.warning(
                            "stream: client closed mid-stream | host=%s | upstream_bytes=%d | flushed=%d | err=%s",
                            _host, bytes_produced, peeked_size, e,
                        )
                        break
                    except Exception as e:
                        _shared["aborted"] = True
                        await _release_upstream()
                        self.logger.warning(
                            "stream: client write failed (mid-stream) | host=%s | err=%s: %s",
                            _host, type(e).__name__, e,
                        )
                        break
                    peeked = []
                    peeked_size = 0
            if _client_aborted:
                # 不要让异常往上抛触发 traceback,upstream_task 自己会被 cancel
                return

        up_t = asyncio.create_task(upstream_task())
        cli_t = asyncio.create_task(client_task())
        try:
            await cli_t
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self.logger.warning(
                "stream: client_task crashed unexpectedly | host=%s | err=%s: %s",
                _host, type(e).__name__, e,
            )
        finally:
            # BUG #47: 先主动释放上游(关闭 aiohttp 响应,让连接池立即空出 slot),
            # 再 cancel upstream_task。比单纯 cancel 快几十 ms ~ 几 s。
            try:
                s = _shared["src"]
                if s is not None and not s.closed:
                    await s.release()
            except Exception:
                pass
            _shared["aborted"] = True
            if not up_t.done():
                up_t.cancel()
            try:
                await up_t
            except (asyncio.CancelledError, Exception):
                pass

        if bytes_produced == 0 and upstream_err:
            self.logger.info(
                "stream: buffered fallback to ffmpeg | host=%s | err=%s",
                _host, upstream_err[0],
            )
            return await self._stream_via_ffmpeg(resp, url, suffix, request)

        # 检测截断:bytes_produced 小于上游 Content-Length 且上游无错误 → 客户端中途断开
        # BUG #58: up_cl 是 nested function upstream_task 的局部变量,主作用域读不到;改读 _shared["cl"]
        up_cl = _shared["cl"]
        _duration = _time.monotonic() - _t_start
        # BUG #59: 网易云拿完整 FLAC 后切片转发,客户端期望字节 = up_cl - seek_pos
        _client_expected = int(up_cl) - (seek_pos if _is_netease else 0) if up_cl else 0
        if up_cl and bytes_produced < _client_expected and not upstream_err:
            self.logger.warning(
                "stream: buffered truncated | host=%s | sent=%d/%s | pct=%.1f%% | duration=%.2fs",
                _host, bytes_produced, _client_expected, 100.0 * bytes_produced / max(_client_expected, 1), _duration,
            )
        elif upstream_err:
            self.logger.warning(
                "stream: buffered done with upstream err | host=%s | bytes=%d | duration=%.2fs | err=%s",
                _host, bytes_produced, _duration, upstream_err[0],
            )
        else:
            self.logger.info(
                "stream: buffered done | host=%s | bytes=%d | duration=%.2fs",
                _host, bytes_produced, _duration,
            )
        return resp

    async def _stream_head(self, track, content_type) -> web.Response:
        file_size = 0
        if track.provider_mappings:
            pm = next(iter(track.provider_mappings))
            file_size = getattr(pm, "file_size", 0) or 0
        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
        if file_size > 0:
            headers["Content-Length"] = str(file_size)
        return web.Response(status=200, headers=headers)

    async def _try_stream_provider(
        self, track: Track, mapping, request: web.Request, params: dict[str, str],
        suffix: str, content_type: str,
    ) -> web.StreamResponse | None:
        """Try to stream from a single provider mapping. Returns response on success, None on failure."""
        music_provider = None
        try:
            music_provider = self.mass.get_provider(mapping.provider_instance)
        except Exception:
            pass
        if not music_provider and mapping.provider_domain:
            for prov in self.mass.providers:
                if prov.domain == mapping.provider_domain:
                    music_provider = prov
                    break
        if not music_provider or not hasattr(music_provider, "get_stream_details"):
            self.logger.info(
                "stream: skip mapping, no usable provider | track=%s | provider_instance=%s | provider_domain=%s",
                params.get("id", ""), mapping.provider_instance, mapping.provider_domain,
            )
            return None

        self.logger.info(
            "stream: try mapping | track=%s | provider=%s/%s | item_id=%s | audio_format=%s",
            params.get("id", ""),
            mapping.provider_domain, mapping.provider_instance,
            mapping.item_id,
            getattr(mapping, "audio_format", None),
        )

        stream_details = None
        try:
            stream_details = await music_provider.get_stream_details(mapping.item_id, MediaType.TRACK)
        except Exception as e:
            self.logger.info(
                "stream: get_stream_details raised | track=%s | provider=%s | item_id=%s | err=%s: %s",
                params.get("id", ""), mapping.provider_domain, mapping.item_id,
                type(e).__name__, e,
            )
            return None
        if not stream_details:
            self.logger.info(
                "stream: get_stream_details returned None | track=%s | provider=%s | item_id=%s",
                params.get("id", ""), mapping.provider_domain, mapping.item_id,
            )
            return None

        # Surface what the provider gave us so playback issues are debuggable from the
        # log alone — provider domain, audio format, stream URL host, duration, flags.
        sd_path = getattr(stream_details, "path", None) or ""
        url_host = ""
        if isinstance(sd_path, str) and sd_path.startswith(("http://", "https://")):
            try:
                url_host = sd_path.split("//", 1)[1].split("/", 1)[0]
            except Exception:
                url_host = sd_path[:60]
        sd_data = getattr(stream_details, "data", None)
        sd_data_preview = bool(isinstance(sd_data, dict) and sd_data.get("preview"))
        self.logger.info(
            "stream: got stream_details | track=%s | provider=%s | stream_type=%s | "
            "format=%s/%s | duration=%ss | size=%s | url_host=%s | data_preview=%s | "
            "expiration=%s | can_seek=%s",
            params.get("id", ""),
            mapping.provider_domain,
            getattr(stream_details, "stream_type", None),
            getattr(stream_details, "audio_format", None),
            content_type,
            getattr(stream_details, "duration", None),
            getattr(stream_details, "size", None),
            url_host or "(none)",
            sd_data_preview,
            getattr(stream_details, "expiration", None),
            getattr(stream_details, "can_seek", None),
        )

        # Skip preview-only streams so handle_stream falls through to the next
        # provider mapping. QQ Music (and similar) explicitly mark 30-second
        # preview snippets via data["preview"]=True and a non-None duration;
        # without this check the Amcfy client would play ~30s, hit EOF, and
        # skip the song on its own.
        sd_data = getattr(stream_details, "data", None)
        is_preview = isinstance(sd_data, dict) and bool(sd_data.get("preview"))
        stream_duration = getattr(stream_details, "duration", None)
        track_duration = getattr(track, "duration", None) or 0
        if (
            not is_preview
            and stream_duration
            and track_duration > 0
            and stream_duration + 10 < track_duration
        ):
            is_preview = True
        if is_preview:
            # v1.3.0 BUG #71: 试听片段默认 stream(听 88s 总比卡死 5 次重试强)。
            # 用户可通过 stream_previews=False 关闭,恢复 v1.2.x 严格跳过行为。
            # self.config.get_value 默认 None 表示用户未设置 → fallback True。
            _sp = self.config.get_value(CONF_STREAM_PREVIEWS)
            if _sp is None:
                _sp = True
            if _sp:
                self.logger.info(
                    "Provider %s only offers a preview snippet for %s "
                    "(track=%ss, stream=%ss); streaming preview anyway "
                    "(stream_previews=True) — client will EOF cleanly after ~88s",
                    mapping.provider_domain, mapping.item_id, track_duration, stream_duration,
                )
                # 不 return None,继续走 stream 路径
            else:
                self.logger.info(
                    "Provider %s only offers a preview snippet for %s "
                    "(track=%ss, stream=%ss); trying next mapping "
                    "(stream_previews=False)",
                    mapping.provider_domain, mapping.item_id, track_duration, stream_duration,
                )
                return None

        seek_pos = 0
        range_hdr = request.headers.get("Range", "")
        if range_hdr:
            # v1.3.0: 同时支持 bytes=N-(开区间到末尾) / bytes=N-M(开闭区间)
            # 客户端(Subsonic API / Amcfy APP)切歌/seek 任意形式都解析正确。
            # 注意:bytes=-N(最后 N 字节)在 audio 流场景极少用,暂不处理。
            m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
            if m:
                seek_pos = int(m.group(1))
                # bytes=0- / bytes=N- 形式 group(2) 是空串,直接取 start。
                # bytes=N-M 形式 seek_pos 取 N(M 是上限,FLAC/MP3 流自然到 EOF)。

        stream_type = getattr(stream_details, "stream_type", None)
        stream_type_str = str(stream_type) if stream_type is not None else ""

        common_headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
        file_size = getattr(mapping, "file_size", 0) or 0
        if file_size > 0:
            # BUG #59: Range 请求时,Content-Length 是 seek_pos 起的字节数(客户端期望值)
            # 不是完整文件大小。网易云 CDN 拿完整 FLAC 后切片转发,客户端字节数 = file_size - seek_pos。
            common_headers["Content-Length"] = str(file_size - seek_pos if seek_pos else file_size)
        # Honor the client's Range request with 206 Partial Content so Subsonic
        # clients (e.g. Amcfy Music Dart) recognize the stream as seekable.
        # Without 206, some clients abort the connection on 200 + Content-Range.
        if range_hdr and seek_pos is not None:
            _status = 206
            if file_size > 0:
                common_headers["Content-Range"] = "bytes {}-{}/{}".format(seek_pos, file_size - 1, file_size)
        else:
            _status = 200
        self.logger.info(
            "stream: resp prepare | track=%s | status=%d | range=%r | seek=%d | size=%d | ct=%s",
            params.get("id", ""), _status, range_hdr, seek_pos, file_size, content_type,
        )
        resp = web.StreamResponse(status=_status, headers=dict(common_headers))

        # Strategy 1: CUSTOM stream type – use get_audio_stream
        if stream_type_str in ("custom", "CUSTOM"):
            fn = getattr(music_provider, "get_audio_stream", None)
            if fn is not None:
                await resp.prepare(request)
                try:
                    agen = fn(stream_details, seek_position=seek_pos)
                    async for chunk in agen:
                        if getattr(request, 'transport', None) and request.transport.is_closing():
                            return resp
                        await resp.write(chunk)
                    return resp
                except NotImplementedError:
                    if self.config.get_value(CONF_DEBUG_VERBOSE):
                        self.logger.debug("get_audio_stream not implemented for %s", mapping.provider_domain)
                except (ConnectionResetError, ConnectionAbortedError):
                    return resp
                except Exception as e:
                    # 真异常降级为 warning:每个 stream 都会进这里,开 debug 会刷屏
                    self.logger.warning("get_audio_stream failed: %s", e)

        # Strategy 2: HTTP URL — try aiohttp proxy FIRST for fast first-byte,
        # fall back to ffmpeg if proxy can't pull (CDN quirks, ssl issues, etc.)
        source = await self._resolve_source(stream_details, mapping)
        _source_host = ""
        if isinstance(source, str) and "://" in source:
            _source_host = source.split("//", 1)[-1].split("/", 1)[0]

        # BUG #62 v1.2.6: 网易云 CDN 在不带 Range 请求时会随机返回 FLAC 或降级 MP3
        # (Content-Type 永远撒谎说 audio/mpeg)。v1.3.0: 嗅探 trigger 泛化 —
        # 任何 host 满足以下任一条件都嗅探(因为 kg/qq/mg 等 CDN 也可能有同类问题):
        #   1. 无损/高码率格式(FLAC / WAV / DSD / ALAC)
        #   2. netease 已知问题 host(*.music.126.net)
        # 嗅探 OK 后 **保留 aiohttp connection 给 stream 用**(同一个 TCP 连接,
        # 命中同一个 CDN 节点,避免 v1.2.5 sniff/stream 不同 connection 命中
        # 不同节点导致嗅探 OK 但 stream 仍拿到 MP3 的问题)。
        # 嗅探失败 → 释放 src + 重新 get_stream_details 拿新 URL 重试,最多 3 次。
        # 嗅探必须在 await resp.prepare(request) 之前完成:
        #   - 嗅探失败时不浪费 206 响应(因为嗅探时还没 prepare)
        #   - 嗅探 OK 后保留的 src 给 _proxy_stream 消费
        probe_src = None
        pre_chunk = b""
        if source and source.startswith(("http://", "https://")):
            _is_netease = isinstance(_source_host, str) and (
                _source_host == "music.126.net" or _source_host.endswith(".music.126.net")
            )
            _ct_lower = content_type.lower() if isinstance(content_type, str) else ""
            _is_lossless = any(t in _ct_lower for t in ("flac", "wav", "dsd", "alac", "ape"))
            if _is_netease or _is_lossless:
                _actual_source = source
                for _attempt in range(3):
                    try:
                        _probe_headers = await self._get_browser_headers(_actual_source)
                        _probe_to = aiohttp.ClientTimeout(
                            # BUG #63 v1.2.7: 嗅探 timeout 必须和 stream 一致(=180s,v1.3.0)。
                            # aiohttp.ClientTimeout 绑整个请求生命周期,嗅探 OK 后 caller-src
                            # 路径下 stream 仍用同一个 response 对象;嗅探 timeout=5s 会在 stream
                            # 期间触发取消(stream 经常 5s 内读不完慢节点大 FLAC),
                            # 错误日志里的 total=30s 其实是 hardcode,真实是嗅探的 5s。
                            # 嗅探动作本身 5s 内必然完成(只读 8 字节),180s 不会触发嗅探 timeout,
                            # 但允许 stream 继续在 180s 框架内跑。
                            total=180, connect=5
                        )
                        probe_src = await self.mass.http_session.get(
                            _actual_source, headers=_probe_headers, timeout=_probe_to
                        )
                        if probe_src.status not in (200, 206):
                            self.logger.warning(
                                "stream: BUG #62 v1.2.6 sniff http %d | attempt=%d | url_host=%s",
                                probe_src.status, _attempt + 1, _source_host,
                            )
                            await probe_src.release()
                            probe_src = None
                            continue
                        # 嗅探:读前 8 字节
                        _head = await probe_src.content.read(8)
                        if not _head:
                            await probe_src.release()
                            probe_src = None
                            continue
                        # FLAC magic: 66 4c 61 43 = b"fLaC"
                        if _head[:4] == b"fLaC":
                            pre_chunk = _head
                            source = _actual_source
                            # v1.3.0 BUG #70: netease provider mapping.file_size=0,
                            # 但嗅探已经拿到上游 Content-Length(=完整 FLAC 大小)。
                            # 没有这个值,客户端 Range 请求时拿不到 Content-Length/Range
                            # → 不一致的 header 导致客户端 close + retry(用户报
                            # "切歌单后播放停止" 现象)。嗅探成功后用 up_cl 补 file_size,
                            # 再修正 resp.headers 让客户端收到正确 Content-Length/Range。
                            if file_size == 0:
                                _up_cl = probe_src.headers.get("Content-Length")
                                if _up_cl and _up_cl.isdigit() and int(_up_cl) > 0:
                                    file_size = int(_up_cl)
                                    if seek_pos > 0:
                                        _slice = file_size - seek_pos
                                        resp.headers["Content-Length"] = str(_slice)
                                        resp.headers["Content-Range"] = "bytes {}-{}/{}".format(
                                            seek_pos, file_size - 1, file_size
                                        )
                                    else:
                                        resp.headers["Content-Length"] = str(file_size)
                                    self.logger.info(
                                        "stream: BUG #70 v1.3.0 fill size from sniff | url_host=%s | file_size=%d | seek=%d",
                                        _source_host, file_size, seek_pos,
                                    )
                            self.logger.info(
                                "stream: BUG #62 v1.2.6 sniff OK (same-conn) | attempt=%d | url_host=%s | pre_chunk=%dB",
                                _attempt + 1, _source_host, len(pre_chunk),
                            )
                            break
                        # 不是 FLAC → 释放 src,准备重试
                        _detail = self._classify_probe_bytes(_head)
                        self.logger.warning(
                            "stream: BUG #62 v1.2.6 sniff FAIL (same-conn) | attempt=%d | detail=%s | url_host=%s",
                            _attempt + 1, _detail, _source_host,
                        )
                        await probe_src.release()
                        probe_src = None
                        if _attempt < 2:
                            # 重新 get_stream_details 拿新 URL(可能命中不同 CDN 节点)
                            try:
                                _new_sd = await music_provider.get_stream_details(mapping.item_id, MediaType.TRACK)
                            except Exception as _e:
                                self.logger.warning(
                                    "stream: BUG #62 v1.2.6 retry get_stream_details failed | err=%s: %s",
                                    type(_e).__name__, _e,
                                )
                                _new_sd = None
                            if _new_sd:
                                _new_url = await self._resolve_source(_new_sd, mapping)
                                if _new_url and isinstance(_new_url, str) and _new_url != _actual_source:
                                    _actual_source = _new_url
                                    if "://" in _actual_source:
                                        _source_host = _actual_source.split("//", 1)[-1].split("/", 1)[0]
                    except Exception as _e:
                        self.logger.warning(
                            "stream: BUG #62 v1.2.6 sniff err | attempt=%d | err=%s: %s",
                            _attempt + 1, type(_e).__name__, _e,
                        )
                        if probe_src is not None:
                            try:
                                await probe_src.release()
                            except Exception:
                                pass
                        probe_src = None
                if probe_src is None:
                    # 3 次都失败:用最后一次的 URL 走老路径(self-GET),让 _proxy_stream 自己 GET
                    source = _actual_source
                    self.logger.error(
                        "stream: BUG #62 v1.2.6 sniff all 3 attempts failed | url_host=%s | fallback to self-GET path",
                        _source_host,
                    )

        # ===== BUG #62 v1.2.6: 在嗅探/解析 source 之后再 prepare =====
        # 嗅探可能需要重试若干次 → 期间不应阻塞 client。
        # 嗅探失败 → 释放 probe_src + 不传 src 给 _proxy_stream(走老路径)。
        # 嗅探成功 → 传 probe_src + pre_chunk 给 _proxy_stream,共享 connection。
        await resp.prepare(request)

        if source and source.startswith(("http://", "https://")):
            self.logger.info(
                "stream: strategy=proxy (preferred for fast TTFB) | host=%s | track=%s | sniff=%s",
                _source_host, params.get("id", ""), "same-conn" if probe_src is not None else "self-get",
            )
            try:
                if probe_src is not None:
                    # BUG #62 v1.2.6: caller 嗅探 OK,把 src + pre_chunk 传下去
                    # _proxy_stream 不会自己 GET,直接消费 probe_src
                    result = await self._proxy_stream(
                        resp, source, request, seek_pos, suffix,
                        upstream_resp=probe_src, pre_chunk=pre_chunk,
                    )
                else:
                    # 老路径:_proxy_stream 自己 GET
                    result = await self._proxy_stream(resp, source, request, seek_pos, suffix)
            finally:
                # BUG #62 v1.2.6: caller-src 路径下 _proxy_stream 不用 async with,
                # caller 兜底 release(probe_src.closed 时 release 是 no-op)
                if probe_src is not None and not probe_src.closed:
                    try:
                        await probe_src.release()
                    except Exception:
                        pass
            if result:
                return result
            # proxy_stream fell back to ffmpeg internally; only reach here if
            # both proxy AND ffmpeg returned None.
            self.logger.info(
                "stream: proxy+ffmpeg both failed for %s", _source_host,
            )
        elif source:
            result = await self._stream_file(resp, source, seek_pos, request)
            if result:
                return result

        # Strategy 4: data field contents
        data = getattr(stream_details, "data", None)
        if data is not None:
            if isinstance(data, bytes) and len(data) > 100:
                result = await self._stream_data(resp, data)
                if result:
                    return result
            elif isinstance(data, dict):
                for key in ("audio", "bytes", "data", "content", "blob"):
                    val = data.get(key)
                    if val:
                        if isinstance(val, bytes) and len(val) > 100:
                            if await self._stream_data(resp, val):
                                return resp
                        elif isinstance(val, str):
                            if val.startswith(("http://", "https://")):
                                result = await self._proxy_stream(resp, val, request, seek_pos, suffix)
                                if result:
                                    return result
                            elif len(val) > 5:
                                if await self._stream_data(resp, val.encode()):
                                    return resp

        # Strategy 5: file path from stream_details
        for attr in ("path", "item_id", "uri"):
            fp = getattr(stream_details, attr, None)
            if fp and isinstance(fp, str) and os.path.isabs(fp):
                exists = await asyncio.get_running_loop().run_in_executor(None, os.path.exists, fp)
                if exists:
                    result = await self._stream_file(resp, fp, seek_pos, request)
                    if result:
                        return result

        # Strategy 6: pm.item_id as file path
        pid = getattr(mapping, "item_id", None)
        if pid and isinstance(pid, str) and pid.startswith("/"):
            exists = await asyncio.get_running_loop().run_in_executor(None, os.path.exists, pid)
            if exists:
                result = await self._stream_file(resp, pid, seek_pos, request)
                if result:
                    return result

        # Strategy 7: Fallback to ffmpeg with original path
        orig_path = getattr(stream_details, "path", None)
        if orig_path:
            result = await self._stream_via_ffmpeg(resp, orig_path, suffix, request)
            if result:
                return result

        return None

    async def handle_stream(self, request: web.Request, params: dict[str, str]) -> web.StreamResponse | web.Response:
        track = await self._resolve_track(params.get("id", ""))
        if not track:
            return self._error(70, "Track not found")

        suffix, content_type = _guess_content_type(track)

        is_head = request.method.upper() == "HEAD"
        if is_head:
            return await self._stream_head(track, content_type)

        mp = track.provider_mappings
        if not mp:
            return self._error(0, "No provider mapping")

        track_name = getattr(track, "name", "") or ""
        track_duration = getattr(track, "duration", None)
        self.logger.info(
            "stream: handle_stream | track=%s | name=%r | duration=%ss | mappings=%d | suffix=%s | content_type=%s",
            params.get("id", ""), track_name, track_duration, len(mp), suffix, content_type,
        )

        for mapping in mp:
            result = await self._try_stream_provider(track, mapping, request, params, suffix, content_type)
            if result is not None:
                return result

        self.logger.info(
            "stream: all mappings failed | track=%s | name=%r | mappings_tried=%d",
            params.get("id", ""), track_name, len(mp),
        )
        return self._error(0, "Stream unavailable")
