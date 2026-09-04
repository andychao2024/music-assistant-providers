"""LX Music provider for Music Assistant."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.errors import LoginFailed
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger(__name__)

DOMAIN = "lxmusic"
CONF_SERVER_URL = "server_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEFAULT_SOURCE = "default_source"
CONF_SEARCH_SOURCES = "search_sources"

SOURCE_NAMES = {
    "kw": "酷我",
    "kg": "酷狗",
    "tx": "QQ音乐",
    "wy": "网易云音乐",
    "mg": "咪咕音乐",
}

QUALITY_ORDER = ["flac", "320k", "128k"]

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: "MusicAssistant",
    manifest: "ProviderManifest",
    config: "ProviderConfig",
) -> "ProviderInstanceType":
    """Initialize provider with given configuration."""
    return LxMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LxMusicProvider(MusicProvider):
    """Provide LX Music (洛雪音乐服务端) as a music source."""

    _http_session: aiohttp.ClientSession | None = None
    _token: str | None = None

    async def handle_async_init(self) -> None:
        """Handle async setup of the provider.

        关键：setup flow 添加时，框架 (flows.py:_finish_provider_setup) 把表单值放进
        ``setup_data`` 字段（加密），``values`` 字段留空 dict。所以必须用
        ``self.get_setup_value(...)``（从 setup_data 解密读取）；用 ``self.config.get_value(...)``
        只读 ``self.config.values``，永远拿不到 setup flow 填的字段——这就是 2026-09-04
        用户反复看到「400 Missing username or password」的真正根因（settings.json 里
        压根没有 lxmusic 条目，因为 handle_async_init 抛 LoginFailed 后整个
        _create_provider_instance 被回滚删除）。
        """
        self._server_url = str(self.get_setup_value(CONF_SERVER_URL) or "http://localhost:9527").rstrip("/")
        self._username = self.get_setup_value(CONF_USERNAME) or "admin"
        self._password = self.get_setup_value(CONF_PASSWORD) or ""
        self._default_source = self.get_setup_value(CONF_DEFAULT_SOURCE) or "wy"
        raw_sources = self.get_setup_value(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"
        self._search_sources = [
            s.strip() for s in raw_sources.split(",") if s.strip()
        ] or ["wy"]
        # 2026-09-04 诊断:确认 get_setup_value 真的拿到了 setup flow 填的字段。
        # 注意:必须放在 _search_sources 赋值之后,否则 LOGGER.warning 引用
        # self._search_sources 会触发 AttributeError,正好是 UI 上看到的那个错误。
        LOGGER.warning(
            "lxmusic: handle_async_init 取值 | server_url=%r | username=%r | "
            "password_present=%s | password_len=%d | default_source=%r | search_sources=%r",
            self._server_url,
            self._username,
            bool(self._password),
            len(self._password or ""),
            self._default_source,
            self._search_sources,
        )
        # 缓存已解析的 Track 与原始 item（供 get_track / get_stream_details 复用）
        self._track_cache: dict[str, Track] = {}
        self._raw_cache: dict[str, dict[str, Any]] = {}
        # 歌手 / 专辑 / 歌单缓存：记录真实 ID 与名称，供详情 / 曲目接口做兜底回查
        self._artist_cache: dict[str, dict[str, Any]] = {}
        self._album_cache: dict[str, dict[str, Any]] = {}
        self._playlist_cache: dict[str, list[dict[str, Any]]] = {}
        # 广场/网络歌单元数据缓存：item_id -> {name, source, id, img, ...}
        # 供 get_playlist/_build_square_playlist 回填歌单名与封面
        self._square_meta: dict[str, dict[str, Any]] = {}
        # 按专辑 aid 缓存已解析的 Track（搜索/歌手页解析过的同专辑歌曲，
        # 点击专辑时直接可用，避免依赖 albumId 或回搜失败导致专辑无曲目）
        self._album_tracks: dict[str, list[Track]] = {}
        self._user_lists_cache: dict[str, Any] | None = None

        # 防御性校验：LX 服务端对空 username 或 password 返回
        # 400 "Missing username or password"，日志/UI 上看不出是配置缺失。
        # 这里提前抛错，让用户看到清晰提示去 MA 设置里补填密码。
        # 2026-09-04 用户报告即使 setup_flow.py 标了 required=True，仍然 400，
        # 怀疑是前端 SECURE_STRING 渲染 / 表单提交时丢了 password 字段。
        if not str(self._username).strip():
            raise LoginFailed(
                "LX Music 用户名为空。请到 MA 设置 → Providers → LX Music → 配置 "
                "里填写 LX 服务端的登录用户名。"
            )
        if not str(self._password):
            raise LoginFailed(
                "LX Music 密码为空。请到 MA 设置 → Providers → LX Music → 配置 "
                "里填写 LX 服务端的登录密码（在 lxserver 后台可设置/修改）。"
            )

        await self._login()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider.

        2026-09-04 BUG #22 修复:setup flow 把表单值放进 setup_data(values 留空),
        再进入设置页面时如果 default_value 还用字面量,UI 上看到的就是初始默认值,
        用户不知道真实值是多少;若误点保存还会用默认值覆盖 setup_data 的真实值,
        下次启动 handle_async_init 拿到错误 username/password 而登录失败。
        因此 default_value 必须从 self.get_setup_value(...) 读取已存值,
        没有时再 fallback 到字面量。
        """
        return (
            ConfigEntry(
                key=CONF_SERVER_URL,
                type=ConfigEntryType.STRING,
                label="服务端地址",
                default_value=str(
                    self.get_setup_value(CONF_SERVER_URL) or "http://localhost:9527"
                ).rstrip("/"),
                required=True,
                description="LX Music 服务端的完整 URL，例如 http://localhost:9527",
            ),
            ConfigEntry(
                key=CONF_USERNAME,
                type=ConfigEntryType.STRING,
                label="用户名",
                default_value=str(self.get_setup_value(CONF_USERNAME) or "admin"),
                required=True,
            ),
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                label="密码",
                required=True,
                # SECURE_STRING 在前端会以掩码形式回显(虽然从 setup_data 拿到的是明文)
                # 拿不到时回退到空串,让用户重新输入。
                default_value=str(self.get_setup_value(CONF_PASSWORD) or ""),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_SOURCE,
                type=ConfigEntryType.STRING,
                label="默认音源",
                default_value=str(self.get_setup_value(CONF_DEFAULT_SOURCE) or "wy"),
                required=False,
                description="获取播放链接时优先使用的音源 (kw/kg/tx/wy/mg)",
            ),
            ConfigEntry(
                key=CONF_SEARCH_SOURCES,
                type=ConfigEntryType.STRING,
                label="搜索音源",
                default_value=str(
                    self.get_setup_value(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"
                ),
                required=False,
                description="搜索时轮询的音源列表，逗号分隔",
            ),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as we provide streaming media."""
        return True

    async def test_connection(self) -> bool:
        """Test the connection to the LX Music server via login (/api/status 需认证会误报 401)."""
        try:
            session = self._session()
            async with session.post(
                f"{self._server_url}/api/user/login",
                json={"username": self._username, "password": self._password},
            ) as resp:
                return resp.status == 200
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("LX Music 连接测试失败: %s", err)
        return False

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #
    def _session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._http_session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        use_auth: bool = True,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform an HTTP request against the LX Music server."""
        headers: dict[str, str] = {}
        if use_auth and self._token:
            headers["x-user-token"] = self._token
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self._server_url}{path}"
        session = self._session()
        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "json": data,
            "params": params,
            "headers": headers,
        }
        if timeout is not None:
            request_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        async with session.request(**request_kwargs) as resp:
            if resp.status == 401 and use_auth:
                await self._login()
                headers["x-user-token"] = self._token or ""
                async with session.request(
                    method, url, json=data, params=params, headers=headers
                ) as resp2:
                    return await self._handle_response(resp2)
            return await self._handle_response(resp)

    @staticmethod
    async def _handle_response(resp: aiohttp.ClientResponse) -> Any:
        """读取响应；非 2xx 时把服务端返回的正文一并抛出，便于排查。"""
        if resp.status >= 400:
            body = ""
            try:
                body = await resp.text()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"HTTP {resp.status} {resp.reason} 响应体: {body[:500]}"
            )
        return await resp.json()

    async def _login(self) -> None:
        """Authenticate and store the token."""
        # 2026-09-04 诊断:实际发往 LX 服务端的字段到底是不是真的非空。
        # 如果 username 是 '' 或者 password 是 '',那服务端一定返回 400。
        LOGGER.warning(
            "lxmusic: _login 发请求 | url=%s/api/user/login | username=%r | password_len=%d",
            self._server_url,
            self._username,
            len(self._password or ""),
        )
        try:
            result = await self._request(
                "POST",
                "/api/user/login",
                data={"username": self._username, "password": self._password},
                use_auth=False,
            )
        except Exception as err:
            LOGGER.exception("lxmusic: _login 失败: %s", err)
            raise
        if isinstance(result, dict):
            self._token = result.get("token") or result.get("data", {}).get("token")
        if not self._token:
            raise RuntimeError("LX Music 登录失败：未获取到 token")
        LOGGER.info("lxmusic: 已登录 lxserver，当前用户=%s", self._username)

    # ------------------------------------------------------------------ #
    # Search & browse
    # ------------------------------------------------------------------ #
    async def _search_source(
        self, source: str, keyword: str, page: int = 1, page_size: int = 30
    ) -> list[dict[str, Any]]:
        """Search a single source."""
        try:
            result = await self._request(
                "GET",
                "/api/music/search",
                params={
                    "source": source,
                    "name": keyword,
                    "page": page,
                    "limit": page_size,
                },
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("LX 搜索音源 %s 失败: %s", source, err)
            return []
        return self._normalize_list(result)

    @staticmethod
    def _normalize_list(result: Any) -> list[dict[str, Any]]:
        """lxserver 返回结构多样，统一成歌曲/歌单列表。

        兼容：裸数组、{list:[...]}、{songs:[...]}、{musics:[...]}、
        {data:{list:[...]}}、{data:{songs:[...]}}、{data:[...]} 等。
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("list", "songs", "musics"):
                if isinstance(result.get(key), list):
                    return result[key]
            data = result.get("data", result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("list", "songs", "musics"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    async def search(
        self,
        search_query: Any,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search the LX Music server across configured sources.

        lxserver 只提供按歌名搜索的接口，因此歌手/专辑结果由歌曲结果聚合得到。
        """
        results = SearchResults()
        keyword = search_query if isinstance(search_query, str) else search_query.search_term
        want_track = MediaType.TRACK in media_types
        want_artist = MediaType.ARTIST in media_types
        want_album = MediaType.ALBUM in media_types
        want_playlist = MediaType.PLAYLIST in media_types
        if not (want_track or want_artist or want_album or want_playlist):
            return results

        seen_tracks: set[str] = set()
        seen_artists: set[str] = set()
        seen_albums: set[str] = set()
        per_source = max(5, limit // max(1, len(self._search_sources)))

        for source in self._search_sources:
            items = await self._search_source(source, keyword, page_size=per_source)
            for item in items:
                # 歌曲
                if want_track:
                    song_id = self._item_song_id(item)
                    if song_id:
                        uid = f"{source}:{song_id}"
                        if uid not in seen_tracks:
                            seen_tracks.add(uid)
                            track = await self._parse_track(item, source)
                            if track:
                                results.tracks.append(track)
                # 歌手（singer 可能是字符串或数组）；尽量带上真实歌手 ID 供回查
                if want_artist:
                    singer_names, real_artist_id = self._singer_info(item)
                    for artist_name in singer_names:
                        aid = self._artist_item_id(source, artist_name)
                        if aid in seen_artists:
                            continue
                        seen_artists.add(aid)
                        # 优先使用本次搜索到的真实 ID，否则回退到已缓存的
                        rid = real_artist_id or self._artist_cache.get(aid, {}).get(
                            "real_id"
                        )
                        results.artists.append(self._register_artist(source, artist_name, rid))
                # 专辑（优先用真实 albumId，便于点击后拉取歌曲）
                if want_album:
                    album_id = self._album_id(item)
                    album_name = item.get("albumName") or item.get("album") or "未知专辑"
                    aid = (
                        f"{source}:{album_id}"
                        if album_id
                        else self._artist_item_id(source, album_name)
                    )
                    if aid not in seen_albums:
                        seen_albums.add(aid)
                        results.albums.append(self._register_album(source, album_name, album_id))
            # 各类型都达到上限则停止
            if (
                (not want_track or len(results.tracks) >= limit)
                and (not want_artist or len(results.artists) >= limit)
                and (not want_album or len(results.albums) >= limit)
            ):
                break

        # 歌单搜索：先搜网络/广场歌单（songList/search），再按名称匹配用户自己的 lx 歌单
        if want_playlist:
            seen_playlists: set[str] = set()
            for pl in await self._search_song_lists(keyword, limit=limit):
                if pl.item_id not in seen_playlists:
                    seen_playlists.add(pl.item_id)
                    results.playlists.append(pl)
            data = await self._get_user_lists()
            if data:
                kw = keyword.lower()
                for pid, pname, songs in self._iter_user_playlists(data):
                    # 匹配歌单名，或歌单内任意歌曲名/歌手（lxserver 无广场歌单关键词 API）
                    hit = bool(kw) and kw in (pname or "").lower()
                    if not hit:
                        for s in songs:
                            s_names, _ = self._singer_info(s)
                            s_text = (
                                (s.get("name") or s.get("songName") or "")
                                + "、"
                                + "、".join(s_names)
                            ).lower()
                            if kw and kw in s_text:
                                hit = True
                                break
                    if hit:
                        item_id = f"list:{pid}"
                        self._playlist_cache[item_id] = songs
                        results.playlists.append(
                            Playlist(
                                item_id=item_id,
                                provider=self.instance_id,
                                name=pname,
                                provider_mappings={
                                    ProviderMapping(
                                        item_id=item_id,
                                        provider_domain=self.domain,
                                        provider_instance=self.instance_id,
                                    )
                                },
                            )
                        )
        return results

    async def _search_song_lists(
        self, keyword: str, limit: int = 25
    ) -> list[Playlist]:
        """搜索网络/广场歌单（lxserver /api/music/songList/search）。

        遍历配置的音源，把命中的歌单转成 sl:<source>:<id> 的 Playlist，
        并缓存歌单名/封面到 _square_meta，供打开歌单时回填。
        """
        if not keyword:
            return []
        out: list[Playlist] = []
        seen: set[str] = set()
        for source in self._search_sources:
            try:
                result = await self._request(
                    "GET",
                    "/api/music/songList/search",
                    params={
                        "source": source,
                        "text": keyword,
                        "page": 1,
                        "limit": max(5, limit // max(1, len(self._search_sources))),
                    },
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("songList/search 音源 %s 失败: %s", source, err)
                continue
            for sl in self._normalize_list(result):
                sl_id = sl.get("id") or sl.get("listId") or sl.get("playId")
                if not sl_id:
                    continue
                sl_source = sl.get("source") or source
                item_id = f"sl:{sl_source}:{sl_id}"
                if item_id in seen:
                    continue
                seen.add(item_id)
                sl_name = sl.get("name") or sl.get("listName") or str(sl_id)
                sl_img = (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                )
                self._square_meta[item_id] = {
                    "source": sl_source,
                    "id": str(sl_id),
                    "name": sl_name,
                    "img": sl_img,
                }
                playlist = Playlist(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=sl_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=item_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
                if sl_img:
                    playlist.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=sl_img,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                out.append(playlist)
                if len(out) >= limit:
                    return out
        return out

    async def browse(self, path: str | None = None) -> BrowseFolder:
        """Browse the LX Music provider."""
        if not path:
            folder = BrowseFolder(
                item_id="lxmusic://root",
                name="LX Music",
                provider=self.instance_id,
            )
            for source in self._search_sources:
                folder.items.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=f"lxmusic://source/{source}",
                        provider=self.instance_id,
                        name=f"{SOURCE_NAMES.get(source, source)} 热门",
                    )
                )
            folder.items.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id="lxmusic://playlists/user",
                    provider=self.instance_id,
                    name="我的歌单",
                )
            )
            folder.items.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id="lxmusic://playlists/square",
                    provider=self.instance_id,
                    name="广场歌单",
                )
            )
            return [folder]

        if path.startswith("lxmusic://source/"):
            source = path.replace("lxmusic://source/", "")
            folder = BrowseFolder(
                item_id=path,
                name=f"{SOURCE_NAMES.get(source, source)} 热门",
                provider=self.instance_id,
            )
            items = await self._search_source(source, "热门", page_size=20)
            for item in items:
                track = await self._parse_track(item, source)
                if track:
                    folder.items.append(
                        ItemMapping(
                            media_type=MediaType.TRACK,
                            item_id=track.item_id,
                            provider=self.instance_id,
                            name=track.name,
                        )
                    )
            return [folder]

        if path == "lxmusic://playlists/user":
            folder = BrowseFolder(
                item_id=path,
                name="我的歌单",
                provider=self.instance_id,
            )
            async for pl in self.get_library_playlists():
                folder.items.append(
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id=pl.item_id,
                        provider=self.instance_id,
                        name=pl.name,
                    )
                )
            return [folder]

        if path == "lxmusic://playlists/square":
            folder = BrowseFolder(
                item_id=path,
                name="广场歌单",
                provider=self.instance_id,
            )
            try:
                tags = self._normalize_list(
                    await self._request("GET", "/api/music/songList/tags")
                )
                for tag in tags:
                    tid = tag.get("id") or tag.get("name")
                    tname = tag.get("name") or tid
                    if not tid:
                        continue
                    folder.items.append(
                        ItemMapping(
                            media_type=MediaType.PLAYLIST,
                            item_id=f"lxmusic://playlists/square/{tid}",
                            provider=self.instance_id,
                            name=tname,
                        )
                    )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("获取广场歌单标签失败: %s", err)
            return [folder]

        if path.startswith("lxmusic://playlists/square/"):
            tag = path.replace("lxmusic://playlists/square/", "")
            folder = BrowseFolder(
                item_id=path,
                name=tag,
                provider=self.instance_id,
            )
            try:
                lists: list[dict[str, Any]] = []
                # 标签接口可能用 tag 或 id 传参，两种都试
                for param_name in ("tag", "id"):
                    lists = self._normalize_list(
                        await self._request(
                            "GET",
                            "/api/music/songList/list",
                            params={
                                param_name: tag,
                                "source": self._default_source,
                                "page": 1,
                                "limit": 50,
                            },
                        )
                    )
                    if lists:
                        break
                for sl in lists:
                    sl_id = sl.get("id") or sl.get("listId") or sl.get("playId")
                    sl_name = sl.get("name") or sl.get("listName") or sl_id
                    sl_source = sl.get("source") or self._default_source
                    if not sl_id:
                        continue
                    sl_item_id = f"sl:{sl_source}:{sl_id}"
                    self._square_meta[sl_item_id] = {
                        "source": sl_source,
                        "id": str(sl_id),
                        "name": sl_name,
                        "img": (
                            sl.get("img")
                            or sl.get("pic")
                            or sl.get("image")
                            or sl.get("cover")
                            or sl.get("coverImgUrl")
                        ),
                    }
                    folder.items.append(
                        ItemMapping(
                            media_type=MediaType.PLAYLIST,
                            item_id=sl_item_id,
                            provider=self.instance_id,
                            name=sl_name,
                        )
                    )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("获取广场歌单列表失败: %s", err)
            return [folder]

        return [
            BrowseFolder(
                item_id=path,
                name="LX Music",
                provider=self.instance_id,
            )
        ]

    # ------------------------------------------------------------------ #
    # Media item getters
    # ------------------------------------------------------------------ #
    async def get_track(self, prov_track_id: str) -> Track:
        """Get a single track."""
        cached = getattr(self, "_track_cache", {}).get(prov_track_id)
        if cached is not None:
            return cached
        source, song_id = self._split_id(prov_track_id)
        items = await self._search_source(source, song_id, page_size=5)
        for item in items:
            if self._item_song_id(item) == song_id:
                track = await self._parse_track(item, source)
                if track:
                    return track
        raise FileNotFoundError(f"Track {prov_track_id} not found")

    async def get_album(self, prov_album_id: str) -> Album:
        """专辑元数据。

        注意：当前 MA 版本的 Album 模型不含 tracks 字段，曲目需在
        get_album_tracks() 中单独返回，这里只构建元数据。
        """
        info = self._album_cache.get(prov_album_id)
        album_name = (
            info["name"] if info else (self._split_id(prov_album_id)[1] or "未知专辑")
        )
        return Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=album_name or "未知专辑",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """返回专辑曲目：优先真实 albumId，否则用缓存，最后按专辑名回搜。"""
        info = self._album_cache.get(prov_album_id)
        source = info["source"] if info else self._split_id(prov_album_id)[0]
        real_id = info.get("real_id") if info else None
        album_name = (
            info["name"] if info else (self._split_id(prov_album_id)[1] or "未知专辑")
        )
        # 1) 优先用真实 albumId 拉取完整专辑
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/albumSongs",
                    {"source": source, "id": real_id},
                    max_items=100,
                )
                out: list[Track] = []
                for item in items:
                    track = await self._parse_track(item, source)
                    if track:
                        out.append(track)
                if out:
                    return out
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("albumSongs 失败 %s: %s", prov_album_id, err)
        # 2) 用搜索/歌手页已解析的同专辑曲目兜底（最可靠，不依赖 albumId）
        cached = self._album_tracks.get(prov_album_id)
        if cached:
            seen_tracks: set[str] = set()
            out = []
            for track in cached:
                if track.item_id not in seen_tracks:
                    seen_tracks.add(track.item_id)
                    out.append(track)
            if out:
                return out
        # 3) 兜底：按专辑名回搜并过滤同名专辑
        out = []
        if album_name and album_name != "未知专辑":
            try:
                for src in self._search_sources:
                    items = await self._search_source(src, album_name, page_size=30)
                    for item in items:
                        an = item.get("albumName") or item.get("album") or ""
                        if an and an == album_name:
                            track = await self._parse_track(item, src)
                            if track:
                                out.append(track)
                    if len(out) >= 20:
                        break
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("专辑回搜失败 %s: %s", prov_album_id, err)
        return out

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Artists：优先从缓存取名称，否则回退到 ID 拆分。"""
        info = self._artist_cache.get(prov_artist_id)
        if info:
            return self._make_artist(info["source"], info["name"], prov_artist_id)
        source, name = self._split_id(prov_artist_id)
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """歌单元数据：支持用户歌单 list:<id> 与广场歌单 sl:<source>:<id>。

        注意：当前 MA 版本的 Playlist 模型不含 tracks 字段，曲目需在
        get_playlist_tracks() 中单独返回，这里只构建元数据。
        """
        if prov_playlist_id.startswith("sl:"):
            parts = prov_playlist_id.split(":", 2)
            source = parts[1] if len(parts) > 1 else self._default_source
            sl_id = parts[2] if len(parts) > 2 else ""
            return self._make_square_playlist(source, sl_id)

        pid = (
            prov_playlist_id[5:]
            if prov_playlist_id.startswith("list:")
            else prov_playlist_id
        )
        data = await self._get_user_lists()
        name = pid
        songs: list[dict[str, Any]] = []
        if data:
            for pl_id, pl_name, pl_songs in self._iter_user_playlists(data):
                if pl_id == pid:
                    name = pl_name
                    songs = pl_songs
                    break
        # 缓存原始歌曲列表，供 get_playlist_tracks 复用
        self._playlist_cache[prov_playlist_id] = songs
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _make_square_playlist(self, source: str, sl_id: str) -> Playlist:
        """广场歌单元数据：用 _square_meta 缓存回填名字与封面。"""
        item_id = f"sl:{source}:{sl_id}"
        meta = self._square_meta.get(item_id, {})
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=meta.get("name") or sl_id,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if meta.get("img"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=meta["img"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return playlist

    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[Track]:
        """返回歌单内的曲目（当前 MA 协议：曲目与元数据分离，且按 page 分页）。

        MA 的 playlists.tracks() 会以 page=0,1,2... 递增调用本方法，直到某页返回
        空列表才停止。因此这里必须按 page 切片返回，否则会陷入无限循环、导致 UI
        一直拿不到完整曲目列表（表现为歌单打开后没有歌曲）。
        """
        page_size = 100
        start = page * page_size
        if prov_playlist_id.startswith("sl:"):
            parts = prov_playlist_id.split(":", 2)
            source = parts[1] if len(parts) > 1 else self._default_source
            sl_id = parts[2] if len(parts) > 2 else ""
            try:
                items = await self._fetch_paged(
                    "/api/music/songList/detail",
                    {"source": source, "id": sl_id},
                    max_items=1000,
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("广场歌单详情失败 %s: %s", prov_playlist_id, err)
                return []
            out: list[Track] = []
            for item in items[start : start + page_size]:
                track = await self._parse_track(item, source)
                if track:
                    out.append(track)
            return out

        # 用户歌单：优先用缓存的歌曲列表
        songs = self._playlist_cache.get(prov_playlist_id)
        if songs is None:
            await self.get_playlist(prov_playlist_id)
            songs = self._playlist_cache.get(prov_playlist_id, [])
        out = []
        for item in songs[start : start + page_size]:
            src = item.get("source") or self._default_source
            track = await self._parse_track(item, src)
            if track:
                out.append(track)
        return out

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails | None:
        """Get stream details (playback URL) for a track.

        /api/music/url 需要一个 songInfo 对象（至少含 source + songmid），
        quality 取值为 flac/320k/128k。若有缓存的原始 item 则整体传入，命中率更高。
        """
        source, song_id = self._split_id(item_id)
        raw = getattr(self, "_raw_cache", {}).get(item_id)
        sources_to_try = [source, self._default_source, *self._search_sources]
        tried: set[str] = set()
        last_err: str | None = None

        LOGGER.info("lxmusic: 开始获取播放链接 item_id=%s source=%s song_id=%s", item_id, source, song_id)

        for src in sources_to_try:
            if not src or src in tried:
                continue
            tried.add(src)
            # 优先使用缓存的完整 item 作为 songInfo；否则用最小结构。
            # 注意：raw 来自 lxserver 搜索结果，本身不含 source 字段，
            # 因此不能用 raw.get("source") 判断，应直接整体传入并补上 source。
            if raw:
                song_info: dict[str, Any] = dict(raw)
                song_info.setdefault("source", src)
                song_info.setdefault("songmid", song_id)
            else:
                song_info = {"source": src, "songmid": song_id}
            # lxserver 的 /api/music/url 既可能读嵌套的 songInfo，也可能直接读
            # 顶层的 source/songmid（Web 播放器实际发出的结构）。两者都带上，
            # 确保服务端能正确识别平台并匹配到自定义源（如 ikun）。
            for quality in QUALITY_ORDER:
                payload: dict[str, Any] = {
                    "songInfo": song_info,
                    "quality": quality,
                    "source": src,
                    "songmid": song_id,
                    "musicId": song_id,
                }
                try:
                    LOGGER.debug(
                        "lxmusic: 请求播放链接 source=%s quality=%s songInfo=%s",
                        src, quality,
                        {k: v for k, v in song_info.items() if k in ("source", "songmid", "name", "singer")},
                    )
                    # lxserver 的私有自定义源（如 ikun，Owner=admin）只在请求头
                    # x-user-name 携带有效用户名、且经 x-user-token 校验通过时才会
                    # 被纳入候选；否则只匹配公开源，导致 "未找到支持 X 平台的自定义源"。
                    # 注意：用户名来自请求头而非 body 的 clientUsername 字段。
                    result = await self._request(
                        "POST",
                        "/api/music/url",
                        data=payload,
                        timeout=20,
                        extra_headers={"x-user-name": self._username},
                    )
                    LOGGER.debug("lxmusic: 播放链接原始响应 source=%s quality=%s result=%s", src, quality, result)
                    url = self._extract_url(result)
                    if url:
                        LOGGER.info("lxmusic: 成功获取播放链接 %s -> %s", item_id, url[:120])
                        return StreamDetails(
                            item_id=item_id,
                            provider=self.instance_id,
                            audio_format=AudioFormat(content_type=ContentType.MP3),
                            stream_type=StreamType.HTTP,
                            path=url,
                            can_seek=True,
                        )
                    # 有响应但没提取到 url，记录一下帮助排查
                    LOGGER.warning(
                        "lxmusic: 响应中未提取到URL source=%s quality=%s result=%s",
                        src, quality, result,
                    )
                except Exception as err:  # noqa: BLE001
                    last_err = f"{src}/{quality}: {err}"
                    LOGGER.warning("lxmusic: 获取播放链接失败 %s", last_err)
                    continue

        LOGGER.error(
            "lxmusic: 无法获取 %s 的播放链接！已尝试源 %s，最后错误: %s。"
            "请确认：1) lxserver 设置中对应平台的自定义源已启用；"
            "2) 该歌曲在 lxserver Web 播放器中可正常播放。",
            item_id, list(tried), last_err or "(无)",
        )
        return None

    async def get_similar_tracks(
        self, prov_track_id: str, limit: int = 25
    ) -> list[Track]:
        """Return similar tracks using same-source search."""
        source, song_id = self._split_id(prov_track_id)
        items = await self._search_source(source, song_id, page_size=limit)
        out: list[Track] = []
        for item in items:
            track = await self._parse_track(item, source)
            if track and track.item_id != prov_track_id:
                out.append(track)
        return out

    # ------------------------------------------------------------------ #
    # Library sync (LX Server has no standard favorites API)
    # ------------------------------------------------------------------ #
    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Return empty; LX Server has no favorites API.

        2026-09-04 BUG 修复:基类 ``MusicProvider.get_library_tracks`` 是
        ``async def ... AsyncGenerator[Track]``（用 ``async for`` 消费），
        原实现写成 ``return []`` 普通协程，会被框架的同步任务
        （``models/music_provider.py:1657`` ``async for prov_item in
        self.get_library_tracks()``）报错 ``'async for' requires an object
        with __aiter__ method, got coroutine``。这里改为异步生成器
        （``if False: yield`` 保持生成器签名）即可。
        """
        if False:  # noqa: SIM901
            yield  # type: ignore[misc]

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Return empty; LX Server has no favorites API."""
        if False:  # noqa: SIM901
            yield  # type: ignore[misc]

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Return empty; LX Server has no favorites API."""
        if False:  # noqa: SIM901
            yield  # type: ignore[misc]

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """返回用户自己的 lx 歌单（defaultList / loveList / userList）。

        2026-09-04 BUG 修复:基类是 ``AsyncGenerator[Playlist]``，必须 ``yield``
        而不是 ``return list``；调用方在 ``browse()`` 内必须用 ``async for``
        而不是 ``await ... for ...``，否则同步 for-in 无法迭代 async generator。
        """
        data = await self._get_user_lists()
        if not data:
            return
        for pid, pname, songs in self._iter_user_playlists(data):
            item_id = f"list:{pid}"
            self._playlist_cache[item_id] = songs
            yield Playlist(
                item_id=item_id,
                provider=self.instance_id,
                name=pname,
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """歌手专辑：优先 artistAlbums（需真实歌手 ID），否则按歌手名回搜聚合。"""
        info = self._artist_cache.get(prov_artist_id)
        if not info:
            source, name = self._split_id(prov_artist_id)
            info = {"source": source, "name": name, "real_id": None}
        source = info["source"]
        name = info["name"]
        real_id = info.get("real_id")
        albums: list[Album] = []
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/artistAlbums",
                    {"source": source, "id": real_id},
                    max_items=50,
                )
                for item in items:
                    aid_real = self._album_id(item) or item.get("id")
                    aname = (
                        item.get("albumName")
                        or item.get("name")
                        or item.get("album")
                        or "未知专辑"
                    )
                    albums.append(self._register_album(source, aname, aid_real))
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("artistAlbums 失败 %s: %s", prov_artist_id, err)
        if not albums:
            # 兜底：按歌手名回搜，按专辑名聚合
            seen_albums: set[str] = set()
            for src in self._search_sources:
                items = await self._search_source(src, name, page_size=30)
                for item in items:
                    singer_names, _ = self._singer_info(item)
                    if name not in singer_names:
                        continue
                    aname = item.get("albumName") or item.get("album") or ""
                    if not aname or aname in seen_albums:
                        continue
                    seen_albums.add(aname)
                    albums.append(self._register_album(src, aname, self._album_id(item)))
                if len(albums) >= 20:
                    break
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """歌手热门：优先 artistSongs（需真实歌手 ID），否则按歌手名回搜过滤。"""
        info = self._artist_cache.get(prov_artist_id)
        if not info:
            source, name = self._split_id(prov_artist_id)
            info = {"source": source, "name": name, "real_id": None}
        source = info["source"]
        name = info["name"]
        real_id = info.get("real_id")
        tracks: list[Track] = []
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/artistSongs",
                    {"source": source, "id": real_id},
                    max_items=50,
                )
                for item in items:
                    track = await self._parse_track(item, source)
                    if track:
                        tracks.append(track)
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("artistSongs 失败 %s: %s", prov_artist_id, err)
        if not tracks:
            # 兜底：按歌手名回搜并过滤同名歌手
            for src in self._search_sources:
                items = await self._search_source(src, name, page_size=30)
                for item in items:
                    singer_names, _ = self._singer_info(item)
                    if name in singer_names:
                        track = await self._parse_track(item, src)
                        if track:
                            tracks.append(track)
                if len(tracks) >= 20:
                    break
        # 去重
        seen: set[str] = set()
        out: list[Track] = []
        for track in tracks:
            if track.item_id not in seen:
                seen.add(track.item_id)
                out.append(track)
        return out

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_id(prov_id: str) -> tuple[str, str]:
        if ":" in prov_id:
            source, _, song_id = prov_id.partition(":")
            return source, song_id
        return "wy", prov_id

    @staticmethod
    def _item_song_id(item: dict[str, Any]) -> str:
        """服务端搜索结果的 ID 字段为 songmid。"""
        return str(
            item.get("songmid")
            or item.get("songId")
            or item.get("id")
            or ""
        )

    @staticmethod
    def _album_id(item: dict[str, Any]) -> str | None:
        """兼容 albumId / albumid / album_id 等多种字段名。"""
        for key in ("albumId", "albumid", "album_id"):
            value = item.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _singer_info(item: dict[str, Any]) -> tuple[list[str], str | None]:
        """解析歌手名列表与真实歌手 ID。

        lxserver 的 singer 可能是字符串（"周杰伦" 或 "A/B"），
        也可能是数组（[{name, id, mid}, ...]）。统一返回 (名字列表, 真实ID)。
        """
        singer = item.get("singer") or item.get("artist") or ""
        names: list[str] = []
        real_id: str | None = None
        if isinstance(singer, list):
            for sub in singer:
                if isinstance(sub, dict):
                    if sub.get("name"):
                        names.append(str(sub["name"]))
                    if not real_id and (sub.get("id") or sub.get("mid")):
                        real_id = str(sub.get("id") or sub.get("mid"))
                elif isinstance(sub, str) and sub.strip():
                    names.append(sub.strip())
        else:
            text = str(singer)
            names = [
                a.strip()
                for a in text.replace("/", "、").split("、")
                if a.strip()
            ]
            rid = item.get("singerId") or item.get("singerMid") or item.get("singer_id")
            if rid:
                real_id = str(rid)
        return names, real_id

    def _make_artist(self, source: str, name: str, item_id: str | None = None) -> Artist:
        aid = item_id or f"{source}:{hashlib.md5(name.encode()).hexdigest()[:12]}"
        return Artist(
            item_id=aid,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=aid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _make_album(
        self, source: str, album_id: str, name: str, item_id: str | None = None
    ) -> Album:
        aid = item_id or f"{source}:{album_id}"
        return Album(
            item_id=aid,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=aid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    # ------------------------------------------------------------------ #
    # 缓存 / 回查辅助
    # ------------------------------------------------------------------ #
    def _artist_item_id(self, source: str, name: str) -> str:
        return f"{source}:{hashlib.md5(name.encode()).hexdigest()[:12]}"

    def _register_artist(
        self, source: str, name: str, real_id: str | None = None
    ) -> Artist:
        aid = self._artist_item_id(source, name)
        self._artist_cache[aid] = {
            "source": source,
            "name": name,
            "real_id": real_id,
        }
        return self._make_artist(source, name, aid)

    def _register_album(
        self, source: str, name: str, real_id: str | None = None
    ) -> Album:
        aid = f"{source}:{real_id}" if real_id else self._artist_item_id(source, name)
        self._album_cache[aid] = {
            "source": source,
            "name": name,
            "real_id": real_id,
        }
        return self._make_album(source, real_id or name, name, aid)

    async def _fetch_paged(
        self, path: str, params: dict[str, Any], max_items: int = 50
    ) -> list[dict[str, Any]]:
        """分页拉取 lxserver 列表接口（artistSongs/artistAlbums/songList/detail 等）。"""
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < max_items:
            batch = self._normalize_list(
                await self._request(
                    "GET",
                    path,
                    params={**params, "page": page, "limit": 50},
                )
            )
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        return items[:max_items]

    async def _get_user_lists(self) -> dict[str, Any] | None:
        """拉取当前用户的歌单数据（defaultList/loveList/userList），带缓存。"""
        if self._user_lists_cache is not None:
            return self._user_lists_cache
        try:
            data = await self._request("GET", "/api/user/list")
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("获取用户歌单失败: %s", err)
            self._user_lists_cache = None
            return None
        self._user_lists_cache = data if isinstance(data, dict) else None
        return self._user_lists_cache

    @staticmethod
    def _iter_user_playlists(
        data: dict[str, Any]
    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """遍历用户歌单，返回 (id, name, songs) 三元组列表。"""
        out: list[tuple[str, str, list[dict[str, Any]]]] = []
        for key in ("defaultList", "loveList"):
            lst = data.get(key)
            if isinstance(lst, dict):
                out.append(
                    (lst.get("id", key), lst.get("name", key), lst.get("list", []))
                )
        for lst in data.get("userList", []) or []:
            if isinstance(lst, dict):
                out.append(
                    (
                        lst.get("id"),
                        lst.get("name", "未命名歌单"),
                        lst.get("list", []),
                    )
                )
        return out

    @staticmethod
    def _extract_url(result: Any) -> str | None:
        if isinstance(result, str):
            return result or None
        if not isinstance(result, dict):
            return None
        for key in ("url", "playUrl", "src"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        data = result.get("data", result)
        if isinstance(data, dict):
            return data.get("url") or data.get("playUrl") or data.get("src")
        if isinstance(data, str):
            return data or None
        return None

    async def _parse_track(
        self, item: dict[str, Any], source: str
    ) -> Track | None:
        """Convert a raw LX Music item dict into a Track."""
        song_id = self._item_song_id(item)
        if not song_id:
            return None
        name = item.get("name") or item.get("songName") or "未知歌曲"
        singer_names, artist_real_id = self._singer_info(item)
        artist_name = singer_names[0] if singer_names else "未知歌手"
        album_name = item.get("albumName") or item.get("album") or "未知专辑"
        album_real_id = self._album_id(item)
        duration = item.get("interval") or item.get("duration") or item.get("time") or 0
        if isinstance(duration, str):
            duration = self._parse_duration(duration)

        artist_aid = self._artist_item_id(source, artist_name)
        album_aid = (
            f"{source}:{album_real_id}"
            if album_real_id
            else self._artist_item_id(source, album_name)
        )
        # 注册到缓存，供 get_artist / get_album / 歌手页回查
        self._artist_cache.setdefault(
            artist_aid,
            {"source": source, "name": artist_name, "real_id": artist_real_id},
        )
        self._album_cache.setdefault(
            album_aid,
            {"source": source, "name": album_name, "real_id": album_real_id},
        )

        track = Track(
            item_id=f"{source}:{song_id}",
            provider=self.instance_id,
            name=name,
            duration=duration,
            artists=[
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_aid,
                    provider=self.instance_id,
                    name=artist_name,
                )
            ],
            album=ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=album_aid,
                provider=self.instance_id,
                name=album_name,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=f"{source}:{song_id}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.MP3),
                    available=True,
                )
            },
        )
        # 归入其专辑缓存，点击专辑时直接复用（最可靠的专辑曲目来源）
        self._album_tracks.setdefault(album_aid, [])
        if track.item_id not in {t.item_id for t in self._album_tracks[album_aid]}:
            self._album_tracks[album_aid].append(track)

        pic = item.get("img") or item.get("pic") or item.get("image") or item.get("cover")
        if pic:
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=pic,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        # 缓存 Track 与原始 item，供 get_track / get_stream_details 复用
        if hasattr(self, "_track_cache"):
            self._track_cache[track.item_id] = track
        if hasattr(self, "_raw_cache"):
            self._raw_cache[track.item_id] = item
        return track

    @staticmethod
    def _parse_duration(text: str) -> int:
        """Parse 'mm:ss' or 'hh:mm:ss' into seconds."""
        parts = [int(p) for p in text.split(":") if p.isdigit()]
        if not parts:
            return 0
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0]

    async def unload(self, is_removed: bool = False) -> None:
        """Clean up the HTTP session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
