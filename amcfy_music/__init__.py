"""
Amcfy Music Subsonic Bridge Plugin for Music Assistant.

Subsonic API bridge for Amcfy Music client - browse and stream all MA music
sources (local files, Spotify, Tidal, NetEase, etc.) via Subsonic protocol.
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

from music_assistant.helpers.images import get_image_data
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.media_items import Album, Artist, MediaItemImage, Playlist, Track

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant

DOMAIN = "amcfy_music"
SUBSONIC_VERSION = "1.16.1"
CONF_TOKEN = "token"
CONF_SEARCH_SCOPE = "search_scope"
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
    if not ts:
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
    suffix, ct = "mp3", "audio/mpeg"
    af = None
    if track.provider_mappings:
        pm = next(iter(track.provider_mappings))
        af = getattr(pm, "audio_format", None)
    if af:
        fmt = (af.output_format_str or "").lower()
        if "flac" in fmt:
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
        "starred": _format_timestamp(track.date_added) if track.favorite else None,
        "albumId": album.uri if album else "",
        "artistId": artist.uri if artist else "",
        "genre": next(iter(genres), "") if genres else "",
        "bitRate": bit_rate or "",
    }


def _album_dict(album: Album, track_count: int = 0, duration: int = 0) -> dict:
    artist = album.artists[0] if album.artists else None
    genres = album.metadata and album.metadata.genres
    return {
        "id": album.uri or album.item_id,
        "name": album.name,
        "artist": artist.name if artist else "Unknown",
        "artistId": artist.uri if artist else "",
        "coverArt": _find_image_id(album),
        "songCount": track_count,
        "duration": duration,
        "created": _format_timestamp(album.date_added),
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


async def get_config_entries(
    mass: MusicAssistant | None = None,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    if action == "regenerate_token":
        new_token = secrets.token_hex(16)
        return (
            ConfigEntry(
                key=CONF_TOKEN,
                type=ConfigEntryType.STRING,
                label="API Token",
                description="Token for Subsonic auth (p=xxx or t=md5(token+salt)&s=salt).",
                required=True,
                value=new_token,
            ),
        )

    return (
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.STRING,
            label="API Token",
            description="Token for Subsonic auth (p=xxx or t=md5(token+salt)&s=salt).",
            required=True,
            value=secrets.token_hex(16),
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
            options=[
                ConfigValueOption("library", title="本地曲库 (Library only)"),
                ConfigValueOption("all", title="全部曲库 (All sources including online)"),
            ],
        ),
    )


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

    async def loaded_in_mass(self) -> None:
        self._api_token = self.config.get_value(CONF_TOKEN) or ""
        if not self._api_token:
            self._api_token = secrets.token_hex(16)
            try:
                await self.mass.config.set_raw_provider_config_value(
                    self.instance_id, self.domain, CONF_TOKEN, self._api_token
                )
                self.logger.info("Generated new API token: %s", self._api_token)
            except Exception:
                self.logger.warning("Could not persist generated token")
        for path in ("/rest/*", "/rest/rest/*", "//rest/*"):
            cb = self.mass.webserver.register_dynamic_route(
                path, self._handle_request, "*"
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
        params = {k.lower(): v if isinstance(v, str) else str(v[-1]) for k, v in request.query.items()}
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
        size = int(params.get("size", "50"))
        offset = int(params.get("offset", "0"))
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
            albums = all_albums
        elif atype == "starred":
            albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, favorite=True)
        elif atype == "random":
            all_albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX)
            random.shuffle(all_albums)
            albums = all_albums
        elif atype == "byYear":
            from_year = int(params.get("fromYear", "1900"))
            to_year = int(params.get("toYear", "2100"))
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

    async def _handle_search(self, request: web.Request, params: dict[str, str], search_type: str) -> web.Response:
        query = params.get("query", "")
        artist_count = int(params.get("artistcount", "20"))
        artist_offset = int(params.get("artistoffset", "0"))
        album_count = int(params.get("albumcount", "20"))
        album_offset = int(params.get("albumoffset", "0"))
        song_count = int(params.get("songcount", "20"))
        song_offset = int(params.get("songoffset", "0"))

        if not query:
            recent = await self.mass.music.tracks.library_items(limit=song_count)
            songs = [_song_dict(t) for t in recent if isinstance(t, Track)]
            key = "searchResult3" if search_type == "search3" else "searchResult2"
            return self._respond({key: {"artist": [], "album": [], "song": songs}})

        scope = self.config.get_value(CONF_SEARCH_SCOPE) or "library"
        library_only = scope == "library"
        results = await self.mass.music.search(
            query,
            media_types=[MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK],
            limit=artist_count + album_count + song_count,
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
        size = int(params.get("size", "10"))
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
        count = int(params.get("count", "10"))
        offset = int(params.get("offset", "0"))
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
                self.logger.debug("Cover: %d images on item, first: path=%s prov=%s", len(images), images[0].path, images[0].provider)
            if not images and isinstance(item, Track):
                album = item.album
                if album and not isinstance(album, str):
                    if hasattr(album, "metadata") and album.metadata and album.metadata.images:
                        images = list(album.metadata.images)
                        self.logger.debug("Cover: %d images on album", len(images))
            if not images:
                return web.Response(body=PLACEHOLDER_PNG, content_type="image/png", status=200, headers=CORS_HEADERS)
            img = images[0]
            img_bytes = await get_image_data(self.mass, img.path, img.provider)
            if img_bytes and len(img_bytes) > 100:
                return web.Response(body=img_bytes, content_type=_guess_image_mime(img_bytes), headers=CORS_HEADERS)
            self.logger.debug("Cover: image too small (%d bytes) for %s", len(img_bytes) if img_bytes else 0, raw_id)
        except Exception as e:
            self.logger.debug("Cover art error for %s: %s", raw_id, e)
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
        try:
            if track.metadata and track.metadata.lyrics:
                track.metadata.lyrics = None
                track.metadata.lrc_lyrics = None
            await self.mass.metadata._update_track_metadata(track, force_refresh=True)
            result = await self.mass.metadata.get_track_lyrics(track)
            plain = result[0] if result else None
        except Exception:
            pass
        try:
            raw = await self.mass.metadata._get_track_lyrics(track)
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
        ids = list(filter(None, (params.get(k, "") for k in ("id", "albumId", "artistId"))))
        for sid in ids:
            try:
                await self.mass.music.add_item_to_favorites(sid)
            except Exception:
                pass
        return self._respond()

    async def handle_unstar(self, request: web.Request, params: dict[str, str]) -> web.Response:
        ids = list(filter(None, (params.get(k, "") for k in ("id", "albumId", "artistId"))))
        for sid in ids:
            try:
                item = await self.mass.music.get_item_by_uri(sid)
                if item:
                    iid = item.item_id or sid
                    await self.mass.music.remove_item_from_favorites(item.media_type, iid)
                else:
                    await self.mass.music.remove_item_from_favorites(None, sid)
            except Exception:
                try:
                    await self.mass.music.remove_item_from_favorites(None, sid)
                except Exception:
                    pass
        return self._respond()

    async def handle_get_starred(self, request: web.Request, params: dict[str, str]) -> web.Response:
        artists = await self.mass.music.artists.library_items(limit=LIBRARY_MAX, favorite=True)
        albums = await self.mass.music.albums.library_items(limit=LIBRARY_MAX, favorite=True)
        tracks = await self.mass.music.tracks.library_items(limit=LIBRARY_MAX, favorite=True)
        album_counts = await self._get_album_count_map()
        is_id3 = request.path.removesuffix(".view").endswith("2")
        wrapper = "starred2" if is_id3 else "starred"
        return self._respond({
            wrapper: {
                "artist": [_artist_dict(a, album_counts.get(a.uri or a.item_id, 0)) for a in artists],
                "album": [_album_dict(a) for a in albums],
                "song": [_song_dict(t) for t in tracks],
            }
        })

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
                await self.mass.music.mark_item_played(
                    track.media_type, track.uri or track.item_id,
                    source="Subsonic", elapsed_time=int(params.get("time", "0")),
                )
            except Exception:
                pass
        return self._respond()

    async def handle_get_artist_info2(self, request: web.Request, params: dict[str, str]) -> web.Response:
        count = int(params.get("count", 25))
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
                self.logger.debug("FFmpeg exit %d for %s: %s", proc.returncode, url[:60], err_text[:200])
            return resp
        except (ConnectionResetError, ConnectionAbortedError, ConnectionError):
            return resp
        except Exception as e:
            self.logger.debug("FFmpeg stream fail: %s: %s", url[:60], e)
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
            h["Cookie"] = "uin=o511092004; qm_keyst=fcde5b87-a2c2-4e02-8347-573c10c6ea95"
        if "126.net" in url or "163.com" in url or "music.163.com" in url:
            h["Referer"] = "https://music.163.com/"
            h["Origin"] = "https://music.163.com"
        return h

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

    async def _proxy_stream(self, resp, url, request, seek_pos, suffix, retries=3):
        proxy_headers = await self._get_browser_headers(url)
        if seek_pos:
            proxy_headers["Range"] = f"bytes={seek_pos}-"
        for retry in range(retries):
            try:
                async with self.mass.http_session.get(
                    url, headers=proxy_headers,
                    timeout=aiohttp.ClientTimeout(total=60, connect=10)
                ) as src:
                    if src.status not in (200, 206):
                        self.logger.debug("proxy upstream %d for %s", src.status, url[:60])
                        continue
                    while True:
                        if getattr(request, 'transport', None) and request.transport.is_closing():
                            return resp
                        chunk = await src.content.read(65536)
                        if not chunk:
                            break
                        await resp.write(chunk)
                return resp
            except (ConnectionResetError, ConnectionAbortedError, aiohttp.ClientPayloadError, ConnectionError):
                if retry < 2:
                    await asyncio.sleep(1.5 * (retry + 1))
                    continue
            except Exception:
                if retry < 2:
                    await asyncio.sleep(1.5 * (retry + 1))
                    continue
        return await self._stream_via_ffmpeg(resp, url, suffix, request)

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

    async def handle_stream(self, request: web.Request, params: dict[str, str]) -> web.StreamResponse | web.Response:
        track = await self._resolve_track(params.get("id", ""))
        if not track:
            return self._error(70, "Track not found")

        suffix, content_type = _guess_content_type(track)

        is_head = request.method.upper() == "HEAD"
        if is_head:
            return await self._stream_head(track, content_type)

        pm, music_provider = None, None
        mp = track.provider_mappings
        if mp:
            pm = next(iter(mp))
        if not pm:
            return self._error(0, "No provider mapping")

        for mapping in mp:
            try:
                music_provider = self.mass.get_provider(mapping.provider_instance)
            except Exception:
                pass
            if not music_provider and mapping.provider_domain:
                for prov in self.mass.providers:
                    if prov.domain == mapping.provider_domain:
                        music_provider = prov
                        break
            if music_provider and hasattr(music_provider, "get_stream_details"):
                pm = mapping
                break
            else:
                self.logger.debug("No provider for mapping: inst=%s dom=%s",
                    mapping.provider_instance, mapping.provider_domain)
        if not music_provider or not hasattr(music_provider, "get_stream_details"):
            self.logger.debug(
                "No streaming provider found for %s (%d mappings)",
                params.get("id", ""), len(mp))
            return self._error(0, "Provider does not support streaming")

        try:
            stream_details = await music_provider.get_stream_details(pm.item_id, MediaType.TRACK)
        except Exception as e:
            self.logger.debug("get_stream_details failed: %s", e)
            return self._error(0, "Stream unavailable")

        seek_pos = 0
        range_hdr = request.headers.get("Range", "")
        if range_hdr:
            m = re.match(r"bytes=(\d+)-", range_hdr)
            if m:
                seek_pos = int(m.group(1))

        stream_type = getattr(stream_details, "stream_type", None)
        stream_type_str = str(stream_type) if stream_type is not None else ""

        common_headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
        if pm:
            file_size = getattr(pm, "file_size", 0) or 0
            if file_size > 0:
                common_headers["Content-Length"] = str(file_size)
        resp = web.StreamResponse(status=200, headers=dict(common_headers))
        await resp.prepare(request)

        # Strategy 1: CUSTOM stream type – use get_audio_stream
        if stream_type_str in ("custom", "CUSTOM"):
            fn = getattr(music_provider, "get_audio_stream", None)
            if fn is not None:
                try:
                    agen = fn(stream_details, seek_position=seek_pos)
                    async for chunk in agen:
                        if getattr(request, 'transport', None) and request.transport.is_closing():
                            return resp
                        await resp.write(chunk)
                    return resp
                except NotImplementedError:
                    self.logger.debug("get_audio_stream not implemented for %s", pm.provider_domain)
                except (ConnectionResetError, ConnectionAbortedError):
                    return resp
                except Exception as e:
                    self.logger.debug("get_audio_stream failed: %s", e)

        # Strategy 2: try ffmpeg first for HTTP URLs (handles CDN redirects/reconnects better)
        source = await self._resolve_source(stream_details, pm)
        if source and source.startswith(("http://", "https://")):
            result = await self._stream_via_ffmpeg(resp, source, suffix, request)
            if result:
                return result
        # Strategy 3: HTTP proxy (fallback if ffmpeg unavailable)
        if source:
            if source.startswith(("http://", "https://")):
                result = await self._proxy_stream(resp, source, request, seek_pos, suffix)
                if result:
                    return result
            else:
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

        # Strategy 4: file path from stream_details
        for attr in ("path", "item_id", "uri"):
            fp = getattr(stream_details, attr, None)
            if fp and isinstance(fp, str) and os.path.isabs(fp):
                exists = await asyncio.get_running_loop().run_in_executor(None, os.path.exists, fp)
                if exists:
                    result = await self._stream_file(resp, fp, seek_pos, request)
                    if result:
                        return result

        # Strategy 5: pm.item_id as file path
        if pm:
            pid = getattr(pm, "item_id", None)
            if pid and isinstance(pid, str) and pid.startswith("/"):
                exists = await asyncio.get_running_loop().run_in_executor(None, os.path.exists, pid)
                if exists:
                    result = await self._stream_file(resp, pid, seek_pos, request)
                    if result:
                        return result

        # Strategy 6: Fallback to ffmpeg with original path
        orig_path = getattr(stream_details, "path", None)
        if orig_path:
            result = await self._stream_via_ffmpeg(resp, orig_path, suffix, request)
            if result:
                return result

        self.logger.debug("All streaming strategies failed for track %s", params.get("id", ""))
        return resp
