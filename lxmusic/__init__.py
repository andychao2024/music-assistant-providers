"""LX Music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncGenerator, Sequence
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
# 2026-09-04:导入排行榜开关(默认开启)。
# 关闭后 ``get_library_playlists`` 不再 yield 51 个 LX 排行榜。
# 已存在的虚拟歌单会保留在 DB,但 sync 不会再次 yield,等同冻结。
CONF_IMPORT_LEADERBOARDS = "import_leaderboards"
# 2026-09-06 Task #76: 用户要求把排行榜和广场歌单拆成两个独立开关。
# 关闭后 ``get_library_playlists`` 不再 yield 各 tag top N 的广场歌单
# (per_tag=5,典型 25~300 个,取决于 tag 数)。
CONF_IMPORT_SQUARE = "import_square_playlists"

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
        # 2026-09-04:导入排行榜开关,默认 True。关闭后 get_library_playlists
        # 不再 yield 51 个 LX 排行榜。已存在的虚拟歌单保留在 DB,sync 不再 yield。
        # get_setup_value 在 setup flow 第一次保存时是 None,这里 ``or True`` 保证默认开。
        self._import_leaderboards = bool(
            self.get_setup_value(CONF_IMPORT_LEADERBOARDS) or True
        )
        # 2026-09-06 Task #76: 拆分独立开关 — 广场歌单导入。
        self._import_square = bool(
            self.get_setup_value(CONF_IMPORT_SQUARE) or True
        )
        raw_sources = self.get_setup_value(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"
        self._search_sources = [
            s.strip() for s in raw_sources.split(",") if s.strip()
        ] or ["wy"]
        # 2026-09-04 诊断:确认 get_setup_value 真的拿到了 setup flow 填的字段。
        # 注意:必须放在 _search_sources 赋值之后,否则 LOGGER.warning 引用
        # self._search_sources 会触发 AttributeError,正好是 UI 上看到的那个错误。
        LOGGER.info(
            "lxmusic: init server=%s user=%s sources=%s default=%s",
            self._server_url,
            self._username,
            self._search_sources,
            self._default_source,
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
        # BUG #83 (2026-09-06): tags API 缓存 (raw_tags, parent_id -> [sub_id])
        # BUG #88 (2026-09-06): 改 (raw_tags, hotTag list) — LX /songList/list 不按 tag 过滤,父 tag 展开会重复
        # 顶层 BrowseFolder 列表 + 子层判断 parent/sub_tag 都基于这份缓存
        # BUG #88 (2026-09-06): 改为 (raw tags list, hotTag list)
        self._square_tags_cache: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        # 按专辑 aid 缓存已解析的 Track（搜索/歌手页解析过的同专辑歌曲，
        # 点击专辑时直接可用，避免依赖 albumId 或回搜失败导致专辑无曲目）
        self._album_tracks: dict[str, list[Track]] = {}
        self._user_lists_cache: dict[str, Any] | None = None
        # BUG #34 (2026-09-04) 修复: 用户在 lxserver 端新建/删除/重命名歌单后,
        # MA 不会自动发现——_user_lists_cache 被永久缓存,后续所有
        # get_library_playlists 调用都返回 MA 启动那一刻的快照。
        # 加 TTL 让 sync 任务定期重新拉取。60 秒足够短,能捕获新歌单;
        # 又不会让每次 get_playlist / search 都打 lxserver。
        self._user_lists_cache_time: float = 0.0
        self._user_lists_cache_lock = asyncio.Lock()
        self._USER_LISTS_CACHE_TTL: float = 60.0
        # 用户歌单歌曲补封面缓存: (source, name, singer) -> pic URL 或 None
        # 避免每次打开用户歌单都重搜同一首歌
        self._pic_enrich_cache: dict[tuple[str, str, str], str | None] = {}
        # 跨平台重搜 songmid 缓存: (source, name, singer) -> songmid 或 None
        # 避免重复用同一 (歌名, 歌手) 在同一平台反复搜
        self._songmid_resolve_cache: dict[tuple[str, str, str], str | None] = {}
        # 虚拟歌单元数据缓存 (排行榜 + 广场精选): list:board:... / list:songlist:...
        # 在 get_library_playlists yield 时填充, get_playlist() 复用,避免 MA 二次访问
        # 元数据时再去打 lxserver。
        self._virtual_meta: dict[str, dict[str, Any]] = {}
        # 广场精选歌单每个 tag 取 top N(防止歌单总数爆炸)。排行榜不受限,全部导入。
        self._LEADERBOARD_TOPN: int = 5

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

        # 2026-09-04 启动后强制重跑 sync 任务。
        # 原因:MA 默认 sync 周期是 hourly(every=12),也就是 12 小时一次。
        # 用户改了 _LEADERBOARD_TOPN / 广场歌单命名后,等不到 sync 重跑,
        # UI 看不出新效果。
        #
        # 之前用的是 schedule_provider_sync (只 schedule,10s 后才 run),
        # 但实测发现该调用在 MA 启动早期可能不生效 (coroutine 跑得太早,
        # config controller / providers 列表还没就绪),导致 sync 不会执行。
        # 改用 start_sync:内部先 schedule 再立刻 run_task,绕过 initial_delay
        # 不稳定的问题。同时先 await 一段缓冲,等 MA 完全启动完,避免同样问题。
        #
        # catch 所有异常 —— sync 失败不应拖死 handle_async_init,
        # 否则 provider 加载失败导致用户在 UI 上看不到 LX provider。
        try:
            async def _delayed_sync() -> None:
                # 等 MA 完全启动 + 一些 provider 完成 sync 调度,
                # 再强制重跑 LX 歌单同步 (10s 通常够)。
                await asyncio.sleep(10)
                try:
                    await self.mass.music.start_sync(
                        media_types=[MediaType.PLAYLIST],
                        providers=[self.instance_id],
                    )
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("lxmusic: 启动后强制 sync 失败: %s", err)

            self.mass.create_task(_delayed_sync())
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("lxmusic: 调度启动 sync 失败: %s", err)

        # 注:2026-09-04 引入的双层名字 cleanup 已完成(单层化为 LX ·<tag>:<name>),
        # DB 残留清空,后续启动每次都跑 cleaned=0 是浪费 IO,这里不再调度。

        # BUG #94 (2026-09-07): 启动后后台预 fill 排行榜
        # 161 个榜单 + pic 串行拉取总耗时 ~25s,browse 子目录点击如果首次
        # 才触发 fill,UI 会空白很久,体感"读不出歌单"。改为 handle_async_init
        # 里后台跑一次,启动后几秒就开始,~30s 内把 _virtual_meta 填好,
        # 届时用户首次点"排行榜"就是毫秒级返回(走缓存守卫)。
        # catch 所有异常 —— 预 fill 失败不应拖死 handle_async_init,
        # 否则 provider 加载失败导致用户在 UI 上看不到 LX provider。
        try:
            async def _prefetch_leaderboards() -> None:
                # 等 MA 主循环起来 + 内部请求协程池就绪再开,避免启动
                # 早期跟其他 sync 抢资源;3s 缓冲实测足够。
                await asyncio.sleep(3)
                if not getattr(self, "_import_leaderboards", True):
                    LOGGER.debug("lxmusic: 排行榜开关关闭,跳过预 fill")
                    return
                try:
                    await self._fill_leaderboards([])
                    LOGGER.info("lxmusic: 排行榜后台预 fill 完成")
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("lxmusic: 排行榜后台预 fill 失败: %s", err)

            self.mass.create_task(_prefetch_leaderboards())
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("lxmusic: 调度排行榜预 fill 失败: %s", err)

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
            # 2026-09-06 BUG #80: 排行榜 / 广场歌单已经从 #/library/playlists
            # 移除(改为只在 #/browse 浏览页可见)。开关改名为"在浏览页显示",
            # 关闭后 browse 入口 LX Music 下不再显示排行榜 / 广场歌单子菜单。
            ConfigEntry(
                key=CONF_IMPORT_LEADERBOARDS,
                type=ConfigEntryType.BOOLEAN,
                label="浏览页显示排行榜",
                default_value=bool(
                    self.get_setup_value(CONF_IMPORT_LEADERBOARDS) or True
                ),
                required=False,
                description=(
                    "开启时 #/browse → LX Music 下显示排行榜入口(约 50 个榜单)。"
                    "关闭后该入口消失,不影响其他功能。"
                ),
            ),
            ConfigEntry(
                key=CONF_IMPORT_SQUARE,
                type=ConfigEntryType.BOOLEAN,
                label="浏览页显示广场歌单",
                default_value=bool(
                    self.get_setup_value(CONF_IMPORT_SQUARE) or True
                ),
                required=False,
                description=(
                    "开启时 #/browse → LX Music 下显示广场歌单入口"
                    "(每分类 top 5,共约 25~300 个)。关闭后该入口消失。"
                ),
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
        {tags:[...]}、{boards:[...]}、{playlists:[...]}、
        {data:{list:[...]}}、{data:{songs:[...]}}、{data:[...]} 等。
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("list", "songs", "musics", "tags", "boards", "playlists", "tags"):
                if isinstance(result.get(key), list):
                    return result[key]
            data = result.get("data", result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("list", "songs", "musics", "tags", "boards", "playlists", "tags"):
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

    async def browse(self, path: str | None = None) -> Sequence[BrowseFolder | ItemMapping | Any]:
        """Browse the LX Music provider.

        2026-09-06 BUG #80 split: virtual playlists (leaderboards / square /
        defaultList) moved out of ``#/library/playlists`` and live ONLY here.

        2026-09-06 BUG #81 fix: MA 2.x BrowseFolder 没有 ``items`` 字段
        (``items`` 只在 ``RecommendationFolder`` 里),``browse()`` 必须返回扁平
        ``Sequence[BrowseFolder | ItemMapping | MediaItem]`` —— 每项是同级子节点,
        通过 ``BrowseFolder.item_id`` 作为子路径(provider 自动拼 ``path``),
        MA 再用该 path 触发下一级 ``browse()``。
        """
        return await self._browse_impl(path)

    async def _browse_impl(self, path: str | None = None) -> Sequence[BrowseFolder | ItemMapping | Any]:
        """Browse 的实际实现 (详见 ``browse()`` docstring)."""
        _P = self.instance_id
        if not path or path == f"{_P}://":
            # 顶层:返回所有顶级子 folder(BrowseFolder 列表,不含 items 字段)
            children: list[BrowseFolder | ItemMapping] = []
            # 5 个源"热门"——纯文件夹入口,点开走 source/{src} 子分支显示热门歌曲。
            # 2026-09-06 用户决定:lxserver 端"热门"不是真歌单(只是关键词搜索的歌曲列表),
            # 不包装成虚拟 Playlist,保持 BrowseFolder 最简形式。
            for source in self._search_sources:
                children.append(
                    BrowseFolder(
                        item_id=f"source/{source}",
                        provider=self.instance_id,
                        name=f"{SOURCE_NAMES.get(source, source)} 热门",
                    )
                )
            # BUG #98 (2026-09-07): 「我的歌单」BrowseFolder 取消。
            # 用户的 LX webplayer 歌单已经通过 get_library_playlists
            # 走 MA library/playlists 标准 sync, browse 顶层再放一个入口
            # 是冗余 + 多一次点击。 直接删, 不在 browse 里暴露。
            children.append(
                BrowseFolder(
                    item_id="playlists/recent",
                    provider=self.instance_id,
                    name="我最近播放",
                )
            )
            if getattr(self, "_import_leaderboards", True):
                children.append(
                    BrowseFolder(
                        item_id="playlists/board",
                        provider=self.instance_id,
                        name="排行榜",
                    )
                )
            if getattr(self, "_import_square", True):
                children.append(
                    BrowseFolder(
                        item_id="playlists/square",
                        provider=self.instance_id,
                        name="广场歌单",
                    )
                )
            return children

        if path.startswith(f"{_P}://source/"):
            source = path.replace(f"{_P}://source/", "")
            items = await self._search_source(source, "热门", page_size=20)
            children: list[ItemMapping] = []
            for item in items:
                track = await self._parse_track(item, source)
                if not track:
                    continue
                # BUG #82 (2026-09-06): ItemMapping.image 补封面 —— _parse_track
                # 已经把 pic 写到 track.metadata.images,这里把第一张 THUMB 转成
                # ItemMapping.image,否则 UI 列表里歌曲没封面。
                thumb_image = None
                if track.metadata and track.metadata.images:
                    for img in track.metadata.images:
                        if img.type == ImageType.THUMB:
                            thumb_image = img
                            break
                children.append(
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id=track.item_id,
                        provider=self.instance_id,
                        name=track.name,
                        image=thumb_image,
                    )
                )
            return children

        # BUG #98 (2026-09-07): 「playlists/user」子分支已废弃。
        # 用户 LX webplayer 歌单走 library/playlists 标准 sync,
        # 此处不再提供 browse 入口, 避免与 sync 重复展示。

        if path == f"{_P}://playlists/recent":
            children = []
            data = await self._get_user_lists()
            if data:
                for pid, pname, songs in self._iter_user_playlists(data):
                    if pid == "__default__":
                        item_id = f"list:{pid}"
                        self._playlist_cache[item_id] = songs
                        children.append(
                            ItemMapping(
                                media_type=MediaType.PLAYLIST,
                                item_id=item_id,
                                provider=self.instance_id,
                                name=pname,
                            )
                        )
                        break
            return children

        if path == f"{_P}://playlists/board":
            # BUG #94 (2026-09-07): 取消平台中间层
            # v1.1.7 ~ v1.1.8 这里是 4 个 BrowseFolder(酷狗榜单/酷我榜单/
            # 网易云榜单/QQ音乐榜单),用户反馈"进入排行榜文件夹读不出歌单",
            # 且中间这层多一次点击没价值。改成直接平铺 161 个榜单,
            # 用 [酷狗]/[酷我]/[网易]/[QQ] 前缀在 name 上区分同名榜单
            # (4 个源都有"飙升榜"等)。
            # 缓存守卫 _collect_virtual_meta -> _fill_leaderboards,
            # handle_async_init 后台预 fill 后,首次点开也是毫秒级。
            children: list[ItemMapping] = []
            boards = await self._collect_virtual_meta(kinds=("board",))
            for b in boards:
                b_pic = (b.get("pic") or "").strip()
                b_image = (
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=b_pic,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                    if b_pic
                    else None
                )
                children.append(
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id=b["item_id"],
                        provider=self.instance_id,
                        name=b["name"],
                        image=b_image,
                    )
                )
            return children

        if path == f"{_P}://playlists/square" or path == f"{_P}://playlists/square/all":
            # 2026-09-06 BUG #88/89/91: LX /songList/list 不按 tag 过滤(curl 验证
            # 华语/流行 两次请求前 5 个 id 完全一致),任何 hotTag/父→子 展开
            # 切来切去都是同一份 source 全量。
            # BUG #92 (2026-09-06): 用户反馈"多了一个二级目录",原 v1.1.5
            # 顶层还塞了个"LX 广场歌单" BrowseFolder,点进去才看到 200 个
            # ItemMapping,等于多一次点击。改成顶层直接展示 200 个歌单,
            # 1 级就看到全部。`playlists/square/all` 兼容旧 URL。
            children: list[ItemMapping] = []
            try:
                items = await self._fetch_square_all_items()
                children.extend(items)
                LOGGER.debug(
                    "lxmusic: 广场歌单 | top %d (扁平展示)",
                    len(children),
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("获取广场歌单失败: %s", err)
            return children

        # 未知路径:返回空列表(避免 items 属性报错)
        return []
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

        2026-09-04 BUG #36 扩展: 同时支持虚拟歌单:
        - list:board:<src>:<bangid>   → 排行榜
        - list:songlist:<src>:<sl_id> → 广场精选
        """
        # 虚拟歌单(排行榜 / 广场精选): 复用 get_library_playlists yield 时缓存的元数据
        if prov_playlist_id.startswith(("list:board:", "list:songlist:")):
            meta = self._virtual_meta.get(prov_playlist_id)
            playlist = Playlist(
                item_id=prov_playlist_id,
                provider=self.instance_id,
                name=(meta or {}).get("name") or prov_playlist_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            # BUG #95 (2026-09-07): 字段名错配
            # _fill_leaderboards 写的是 ``pic``(排行榜自己的封面字段),
            # 而 _fetch_square_* 写的是 ``img``(LX 广场歌单返回字段)。
            # 原代码统一读 ``img`` 导致排行榜永远没封面。兼容两种 key。
            img = (meta or {}).get("pic") or (meta or {}).get("img")
            if img:
                playlist.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=img,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            return playlist
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

        2026-09-04 扩展: 虚拟歌单 ID 派发:
        - list:board:<src>:<bangid>     → 排行榜 (调 /api/music/leaderboard/list)
        - list:songlist:<src>:<sl_id>   → 广场精选歌单 (调 /api/music/songList/detail)
        - 其他 (list:__default__ / list:__love__ / list:<user_id>) → 用户自建歌单
        """
        page_size = 100
        start = page * page_size
        # 排行榜虚拟歌单
        if prov_playlist_id.startswith("list:board:"):
            parts = prov_playlist_id.split(":", 3)
            source = parts[2] if len(parts) > 2 else ""
            bangid = parts[3] if len(parts) > 3 else ""
            return await self._get_leaderboard_tracks(source, bangid, page)
        # 广场精选歌单虚拟 ID
        if prov_playlist_id.startswith("list:songlist:"):
            parts = prov_playlist_id.split(":", 3)
            source = parts[2] if len(parts) > 2 else self._default_source
            sl_id = parts[3] if len(parts) > 3 else ""
            return await self._get_songlist_tracks(source, sl_id, page)
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
        # BUG #32 (2026-09-04) 修复: lxserver /api/user/list 返回的 MusicInfo 只有
        # id/name/singer/source/interval/meta,没有 img/pic/image/cover 字段,
        # 用户歌单歌曲在 MA 上显示无封面。第一页触发时,并发重搜补全。
        page_songs = songs[start : start + page_size]
        if page == 0 and page_songs:
            await self._enrich_playlist_pics(page_songs)
        out = []
        for item in page_songs:
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

        BUG #33 (2026-09-04) 修复: lxserver 服务端 /api/music/url 只做同源
        多 API fallback（如 wy 平台下切 meting/ikun），不会跨平台搜索歌曲。
        当用户歌单里的歌来自 wy 平台，但 wy 自定义源（如 metingapi 网关）整体
        失效时，无法 fallback 到 kw/kg/tx/mg。

        BUG #35 (2026-09-04) 修复: 之前客户端自己做跨平台重搜 fallback
        (用 name+singer 在 kw/kg/tx/mg 上搜同名 songmid),结果搜索匹配不可靠:
        kw/kg/tx/mg 上同名歌曲可能是翻唱/remix/不同版本,导致 MA 播放"错曲"。
        已知跨平台 fallback 错曲问题,这里彻底禁用。

        客户端这里只做:
        1) 同源 (source) 内多 quality 循环,让 lxserver 自己按自定义源
           (meting/ikun/...) 多 API fallback
        2) URL 健康检查: 拿到 URL 后调 _check_url_playable 做一次 content-type
           检查,过滤掉网关挂了但仍返回 200 的"假 URL"
        3) 如果原平台所有 quality 都拿不到健康 URL,直接放弃——用户需要在
           lxserver Web 端配置其他可用的自定义源(如换 meting 镜像、加 ikun)。

        跨平台 fallback 留作 `_resolve_songmid_for_src` 方法,但目前不在
        get_stream_details 里调用,留作未来扩展点。
        """
        source, song_id = self._split_id(item_id)
        raw = getattr(self, "_raw_cache", {}).get(item_id)
        # BUG #35: 只在原平台 source 上循环 quality,不做跨平台 fallback。
        # 跨平台搜索匹配精度不够,会导致同名翻唱被错播。
        sources_to_try = [source]
        tried: set[str] = set()
        last_err: str | None = None

        # 从 raw 拆出 name/singer/interval (虽然不用作 fallback,但保留作日志/调试用)
        if raw:
            item_name = (raw.get("name") or raw.get("songName") or "").strip()
            singer_raw = raw.get("singer") or raw.get("singerName") or ""
            if isinstance(singer_raw, list):
                singer_name = ""
                if singer_raw and isinstance(singer_raw[0], dict):
                    singer_name = singer_raw[0].get("name", "")
                elif singer_raw:
                    singer_name = str(singer_raw[0])
                singer_name = singer_name or singer_raw[0].get("name", "") if singer_raw else ""
            elif isinstance(singer_raw, str):
                singer_name = singer_raw.split("/")[0].split("、")[0].strip()
            else:
                singer_name = ""
            item_interval = raw.get("interval") or raw.get("duration") or raw.get("time") or ""
        else:
            item_name = ""
            singer_name = ""
            item_interval = ""

        LOGGER.debug(
            "lxmusic: 开始获取播放链接 item_id=%s source=%s song_id=%s name=%r singer=%r interval=%r sources_to_try=%s",
            item_id, source, song_id, item_name, singer_name, item_interval, sources_to_try,
        )

        for src in sources_to_try:
            if not src or src in tried:
                continue
            tried.add(src)

            # BUG #35: 跨平台 fallback 已禁用,只走原平台
            actual_songmid: str | None = None
            if raw:
                actual_songmid = (
                    raw.get("songmid")
                    or raw.get("id")
                    or raw.get("songId")
                    or song_id
                )
            else:
                actual_songmid = song_id

            # 优先使用缓存的完整 item 作为 songInfo；否则用最小结构。
            # BUG #31 (2026-09-04) 修复: 强制覆盖为当前 src,确保切源时服务端能
            # 正确识别目标平台。注意 songmid 不再用 setdefault,要写 actual_songmid
            if raw:
                song_info: dict[str, Any] = dict(raw)
                song_info["source"] = src
                song_info.setdefault("songmid", actual_songmid or song_id)
            else:
                song_info = {
                    "source": src,
                    "songmid": actual_songmid,
                    "name": item_name,
                    "singer": singer_name,
                }
            LOGGER.debug(
                "lxmusic: 尝试源 src=%s songmid=%s (item_id=%s, songInfo.source=%s)",
                src, actual_songmid, item_id, song_info.get("source"),
            )
            # lxserver 的 /api/music/url 既可能读嵌套的 songInfo，也可能直接读
            # 顶层的 source/songmid（Web 播放器实际发出的结构）。两者都带上，
            # 确保服务端能正确识别平台并匹配到自定义源（如 ikun）。
            for quality in QUALITY_ORDER:
                payload: dict[str, Any] = {
                    "songInfo": song_info,
                    "quality": quality,
                    "source": src,
                    "songmid": actual_songmid,
                    "musicId": actual_songmid,
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
                        # BUG #33 健康检查: 网关挂了(如 metingapi.nanorocky.top
                        # 被 CF 拦截)会返回 200 + 空内容,过滤掉
                        if not await self._check_url_playable(url):
                            last_err = f"{src}/{quality}: URL 不可播放(网关挂了?) {url[:60]}"
                            LOGGER.warning("lxmusic: %s", last_err)
                            continue
                        # BUG #44 (2026-09-05) 双重保险识别 content_type:
                        # 之前硬编码 ContentType.MP3,但 lxserver 在服务端"最高音质"
                        # 设置下,实际可能返回 flac(典型 URL 末尾 .flac,典型 lxserver
                        # 响应字段 type="flac")。硬编码 MP3 会让 amcfy 桥接给 APP
                        # 发 Content-Type: audio/mpeg,APP 用 mp3 解码器解 flac 字节流
                        # → 听起来"糊/破音"。先按 lxserver 响应里的 type 字段匹配,
                        # 兜底再按 URL 后缀。
                        ct = self._pick_content_type(result, url, quality)
                        LOGGER.debug(
                            "lxmusic: 成功获取播放链接 %s -> %s (quality=%s content_type=%s)",
                            item_id, url[:120], quality, ct,
                        )
                        return StreamDetails(
                            item_id=item_id,
                            provider=self.instance_id,
                            audio_format=AudioFormat(content_type=ct),
                            stream_type=StreamType.HTTP,
                            path=url,
                            can_seek=True,
                        )
                    # 有响应但没提取到 url，记录一下帮助排查
                    LOGGER.debug(
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
        """Yield user-owned playlists (webplayer + loveList) to MA library.

        2026-09-06 BUG #80: virtual playlists (LX leaderboards / square /
        defaultList "我最近播放") are NOT yielded here -- they live in
        ``#/browse`` only. Browse paths call ``_collect_virtual_meta()`` to
        populate ``_virtual_meta`` cache, then construct ItemMapping entries.

        Yield order:
        - ``__love__`` ("洛雪收藏")
        - userList webplayer_* (LX server user-created local playlists)
        """
        data = await self._get_user_lists()
        if data:
            for pid, pname, songs in self._iter_user_playlists(data):
                # BUG #80: skip defaultList ("我最近播放"). It is a virtual
                # playlist exposed only via browse path "{instance_id}://playlists/recent".
                if pid == "__default__":
                    continue
                item_id = f"list:{pid}"
                self._playlist_cache[item_id] = songs
                # 2026-09-04 Task #42: 用第一首歌的封面做歌单封面。
                first_song = songs[0] if songs else None
                first_pic = ""
                if isinstance(first_song, dict):
                    first_pic = (
                        first_song.get("img")
                        or first_song.get("pic")
                        or first_song.get("image")
                        or ""
                    ).strip()
                playlist = Playlist(
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
                if first_pic:
                    playlist.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=first_pic,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                yield playlist

    def _library_item_needs_update(
        self, library_item, prov_item
    ) -> bool:
        """判断 library 里的 LX 歌单是否需要 update。

        2026-09-04 简化 LX 广场歌单命名后,DB 里 50 个旧双层名字(`LX 歌单·主题·70后:...`)
        没被新 sync 更新。原因:基类 ``_library_item_needs_update`` 只看
        ``provider_mappings`` 和 ``date_added``,不看 ``name`` —— LX 虚拟歌单
        的 item_id 稳定,但 name 是从 display 模板动态生成的,改模板后老名字
        永远不会被覆盖,只能等 deletion(同步库不会删除相同 item_id 的歌单)。

        这里 override 加 name 比较:如果 sync 发现 name 不同就触发 update。
        对用户歌单(``list:default`` / ``list:love`` / ``list:user:<id>``)也安全
        —— 用户歌单名改了 MA 也会跟着 update。

        2026-09-04 Task #42 扩展:再加 images 比较。LX 虚拟歌单封面来自 lxserver
        详情页第一首的 pic,这个字段在 sync 流程里不在 sync_details 比较里
        (MA base 用 ``LibraryItemSyncDetails`` 只看 scalar 列),所以即使
        yield 时带了 metadata.images,只要 name 没变 DB 里旧 image 就一直
        是 null。这里把 images path 也纳入比较,封面变了就触发 update。
        """
        base = super()._library_item_needs_update(library_item, prov_item)
        lib_name = getattr(library_item, "name", None)
        prov_name = getattr(prov_item, "name", None)
        name_differs = lib_name != prov_name

        # 比较 images:取第一张 THUMB 的 path,简化成 url 字符串集合
        def _first_thumb_path(item) -> str:
            meta = getattr(item, "metadata", None)
            if not meta:
                return ""
            images = getattr(meta, "images", None) or []
            for img in images:
                # ImageType 可能不暴露 .value, 退化为 str 比较
                t = getattr(img, "type", None)
                if str(t).endswith("THUMB") or str(t) == "thumb" or str(t) == "ImageType.THUMB":
                    return (getattr(img, "path", "") or "").strip()
            return ""

        lib_img = _first_thumb_path(library_item)
        prov_img = _first_thumb_path(prov_item)
        image_differs = lib_img != prov_img

        needs = base or name_differs or image_differs
        # INFO 级日志,即使 logger level 设到 INFO 也能看到 —— DEBUG 没出现
        # 是因为 LX provider 主 logger 在 sync 阶段被替换为 MusicProvider 的
        # ``self.logger``(见 music_provider.py sync 流程),而我们 LOGGER 指向
        # ``music_assistant.providers.lxmusic``(provider __name__),二者其实是
        # 同一个 logger,但下游 code 调用 super() 后 LOG 走 self.logger
        # (provider 级)。这里强制 INFO 方便观察。
        # 2026-09-04 配合 _cleanup_double_layer_names,override 这里日志
        # 改回 DEBUG,不让同步时刷出几十行 WARNING。但用户在 MA UI 里把 LX
        # provider logger 设到 DEBUG 就能看到。
        if (name_differs and not base) or image_differs:
            LOGGER.debug(
                "lxmusic: 触发 update | lib_id=%s prov_item_id=%s name_diff=%s image_diff=%s (lib=%r prov=%r)",
                getattr(library_item, "item_id", "?"),
                getattr(prov_item, "item_id", "?"),
                name_differs, image_differs,
                lib_img[:60], prov_img[:60],
            )
        return needs

    # 2026-09-04 简化 LX 广场歌单命名:把 DB 残留的 'LX 歌单·X·Y:...' 双层
    # 名字直接改成单层 'LX 歌单·Y:...'。
    #
    # 必要性:``_library_item_needs_update`` override 只在 sync yield 同
    # item_id 时触发,但 ``get_library_playlists`` 每个 tag 只取 top-N
    # (``_LEADERBOARD_TOPN=5``),老的 sl_id 不在 top-N 里就 yield 不到,
    # override 没机会跑,DB 残留几百条双层名字。这里在 sync 启动后跑一次
    # 直接 DB UPDATE,绕过 sync 范围限制,把残留的双层全部简化。
    #
    # 2026-09-04 二次简化:用户进一步要求去掉"歌单"两字,新模板
    # ``LX ·<tag>:<name>``。cleanup 同步把 ``LX 歌单·<tag>:<name>`` 改成
    # ``LX ·<tag>:<name>``。一次 regex 同时兼容双层 ``LX 歌单·X·Y:Z`` 和
    # 单层 ``LX 歌单·Y:Z`` 两种历史格式(双层用 group(3)、单层用 group(2))。
    _SONGLIST_NAME_RE = re.compile(r"^(LX )歌单·(?:[^·]+·)?([^·:]+)(:.*)$")

    async def _cleanup_double_layer_names(self) -> None:
        """把 DB 里 LX 广场歌单的名字统一改成 ``LX ·<tag>:<name>``。

        直接用 ``self.mass.music.database.update`` 改 playlists.name,
        不用 ``update_item_in_library`` —— 避免触发 ``MEDIA_ITEM_UPDATED``
        事件干扰用户当前播放;前端下次拉 playlist 列表会看到新名字。
        """
        pattern = self._SONGLIST_NAME_RE
        db = self.mass.music.database
        # JOIN provider_mappings 过滤 LX provider 的歌单,避免误改其他 provider。
        # 注意:不要 ``GROUP BY``,只用 ``DISTINCT``,因为 aiosqlite Row 在
        # ``iter_rows_from_query`` 里没有标准 Mapping 接口,用 row[0]/row[1]
        # 按位置访问最稳。
        query = (
            "SELECT DISTINCT pl.item_id, pl.name "
            "FROM playlists pl "
            "JOIN provider_mappings pm ON pl.item_id = pm.item_id "
            "WHERE pm.provider_domain = :domain "
            "  AND pl.name LIKE 'LX 歌单·%'"
        )
        cleaned = 0
        skipped = 0
        failed = 0
        try:
            async for row in db.iter_rows_from_query(
                query, params={"domain": self.domain}
            ):
                # aiosqlite.Row 是 sqlite3.Row 的别名,既支持 row[0]/row[1]
                # 按位置访问,又支持 row['item_id'] 按名字访问。统一按位置
                # 访问,避免命名 dict 解析失败导致漏清理。
                item_id = row[0]
                old_name = row[1] or ""
                if not item_id:
                    continue
                m = pattern.match(old_name)
                if not m:
                    skipped += 1
                    continue
                # group(1)="LX ", group(2)=<tag>(无论双层/单层都拿到最终 tag),
                # group(3)=:<rest>
                new_name = f"LX ·{m.group(2)}{m.group(3)}"
                if new_name == old_name:
                    skipped += 1
                    continue
                try:
                    await db.update(
                        "playlists",
                        match={"item_id": int(item_id)},
                        values={"name": new_name},
                    )
                    cleaned += 1
                    if cleaned <= 3 or cleaned % 100 == 0:
                        LOGGER.info(
                            "lxmusic: 歌单名 cleanup | item_id=%s '%s' -> '%s'",
                            item_id, old_name, new_name,
                        )
                except Exception as err:  # noqa: BLE001
                    failed += 1
                    LOGGER.debug(
                        "lxmusic: 歌单名 cleanup 失败 | item_id=%s err=%s",
                        item_id, err,
                    )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("lxmusic: 歌单名 cleanup query 失败: %s", err)
            return
        LOGGER.info(
            "lxmusic: 歌单名 cleanup 完成 | cleaned=%d skipped=%d failed=%d",
            cleaned, skipped, failed,
        )

    async def sync_library(self, media_type: MediaType) -> None:
        """Override base class: after base sync, force-sync LX webplayer playlist names to MA.

        BUG #79: user renamed webplayer playlist in LX app, but MA name stayed old.
        Reason: MA Playlist._update_library_item hardcodes name=cur_item.name when
        overwrite=False, and base sync uses overwrite=False by default. Only the
        is_dynamic+not_editable+(name/images diff) branch uses overwrite=True,
        which LX webplayer playlists do not hit.

        This override re-compares webplayer_* names after base sync and calls
        update_item_in_library(overwrite=True) on mismatches.
        """
        await super().sync_library(media_type)
        if media_type == MediaType.PLAYLIST:
            try:
                await self._sync_webplayer_playlist_names()
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("lxmusic: webplayer name sync failed: %s", err)

    async def _sync_webplayer_playlist_names(self) -> None:
        """Compare LX server userList webplayer_* playlists, update MA DB name on mismatch.

        BUG #79 helper. Only touches list:webplayer_* item_ids:
        - defaultList / loveList / virtual playlists (leaderboards/square) are NOT touched
          here; they are handled by other cleanup/yield paths.
        - Runs after lxmusic base sync, calls update_item_in_library(overwrite=True)
          to flush the latest name to DB.

        Note: update_item_in_library triggers MEDIA_ITEM_UPDATED event and cache
        cleanup -- this is desired. base MusicProvider.on_item_updated is a no-op,
        so no write-back to LX server (avoiding loops).
        """
        data = await self._get_user_lists()
        if not data:
            return
        user_list = data.get("userList") or []
        if not isinstance(user_list, list):
            return
        db = self.mass.music.database
        matched = 0
        updated = 0
        failed = 0
        query = (
            "SELECT pl.item_id, pl.name "
            "FROM playlists pl "
            "JOIN provider_mappings pm ON pl.item_id = pm.item_id "
            "WHERE pm.provider_domain = :domain "
            "  AND pm.provider_item_id = :prov_item_id "
            "LIMIT 1"
        )
        for entry in user_list:
            if not isinstance(entry, dict):
                continue
            pl_id = entry.get("id")
            new_name = (entry.get("name") or "").strip()
            if not isinstance(pl_id, str) or not pl_id or not new_name:
                continue
            if not pl_id.startswith("webplayer_"):
                # Only process user-created/renamed webplayer local playlists.
                # Skip other userList entries (lxserver built-in/default) to avoid
                # accidentally overwriting MA virtual playlists.
                continue
            prov_item_id = f"list:{pl_id}"
            row = None
            try:
                async for r in db.iter_rows_from_query(
                    query,
                    params={"domain": self.domain, "prov_item_id": prov_item_id},
                ):
                    row = r
                    break
            except Exception as err:  # noqa: BLE001
                failed += 1
                LOGGER.debug(
                    "lxmusic: webplayer name query failed | prov_item_id=%s err=%s",
                    prov_item_id, err,
                )
                continue
            if row is None:
                # MA DB does not have this row yet (first sync?), will be added next time
                continue
            item_id = int(row[0])
            old_name = row[1] or ""
            if old_name == new_name:
                matched += 1
                continue
            try:
                pl_obj = Playlist(
                    item_id=prov_item_id,
                    provider=self.instance_id,
                    name=new_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=prov_item_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
                await self.mass.music.playlists.update_item_in_library(
                    item_id=item_id,
                    update=pl_obj,
                    overwrite=True,
                )
                updated += 1
                LOGGER.info(
                    "lxmusic: webplayer playlist rename | prov_item_id=%s '%s' -> '%s'",
                    prov_item_id, old_name, new_name,
                )
            except Exception as err:  # noqa: BLE001
                failed += 1
                LOGGER.debug(
                    "lxmusic: webplayer rename failed | prov_item_id=%s err=%s",
                    prov_item_id, err,
                )
        if updated > 0 or failed > 0:
            LOGGER.info(
                "lxmusic: webplayer name sync done | matched=%d updated=%d failed=%d",
                matched, updated, failed,
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

    async def _get_leaderboard_tracks(
        self, source: str, bangid: str, page: int = 0
    ) -> list[Track]:
        """获取排行榜内的歌曲(虚拟歌单 list:board: 派发)。

        2026-09-04 BUG #36: lxserver /api/music/leaderboard/list 返回
        ``{list, total, page, limit, source}``,list 每首含 songmid (纯数字)
        所以能直接走对应平台官方源播放,无需再做前缀剥除。

        BUG #95 (2026-09-07): 原代码永远传 ``page=1``,不管 MA 要第几页。
        MA playlists.tracks() 以 page=0,1,2 递增调用本方法,永远拿 page=1
        的数据 → 排行榜 >100 首时拿不全 → 客户端"没歌曲"体感。同时
        page=1 对应数据已经切片过 start..start+page_size,MA 看到 100 条
        又会继续请求下一页 → 无谓重复拉取。本方法直接传 MA 给的
        page(MA 是 0-based,lxserver 是 1-based,+1 对齐)。
        """
        page_size = 100
        start = page * page_size
        try:
            data = await self._request(
                "GET",
                "/api/music/leaderboard/list",
                params={"source": source, "bangid": bangid, "page": page + 1},
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("lxmusic: 排行榜拉取失败 %s/%s: %s", source, bangid, err)
            return []
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            items = data.get("list", []) or []
        elif isinstance(data, list):
            items = data
        out: list[Track] = []
        for item in items[start : start + page_size]:
            if not isinstance(item, dict):
                continue
            # 排行榜 songmid 已是纯数字, _parse_track 的剥前缀分支不会触发
            track = await self._parse_track(item, source)
            if track:
                out.append(track)
        return out

    async def _get_songlist_tracks(
        self, source: str, sl_id: str, page: int = 0
    ) -> list[Track]:
        """获取广场精选歌单的歌曲(虚拟歌单 list:songlist: 派发)。

        2026-09-04 BUG #36: 调 /api/music/songList/detail, 歌曲 songmid 是
        纯数字(由服务端绑定到对应 source 的官方库)。
        """
        page_size = 100
        start = page * page_size
        try:
            items = await self._fetch_paged(
                "/api/music/songList/detail",
                {"source": source, "id": sl_id},
                max_items=1000,
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("lxmusic: 广场歌单详情失败 %s/%s: %s", source, sl_id, err)
            return []
        out: list[Track] = []
        for item in items[start : start + page_size]:
            if not isinstance(item, dict):
                continue
            track = await self._parse_track(item, source)
            if track:
                out.append(track)
        return out

    async def _get_user_lists(self) -> dict[str, Any] | None:
        """拉取当前用户的歌单数据（defaultList/loveList/userList），带 TTL 缓存。

        BUG #34 (2026-09-04) 修复: 之前缓存永久不过期,导致用户在 lxserver
        端新建的歌单永远不会被 MA 发现。这里加 60s TTL,既能保证 sync 任务
        能周期性看到新歌单,又不会让 search/get_playlist 等高频调用都打
        lxserver。用 lock 防止并发触发时重复请求。
        """
        now = asyncio.get_event_loop().time()
        if (
            self._user_lists_cache is not None
            and (now - self._user_lists_cache_time) < self._USER_LISTS_CACHE_TTL
        ):
            return self._user_lists_cache
        async with self._user_lists_cache_lock:
            # 双重检查,避免锁外等待的协程拿到旧缓存后重请求
            now = asyncio.get_event_loop().time()
            if (
                self._user_lists_cache is not None
                and (now - self._user_lists_cache_time) < self._USER_LISTS_CACHE_TTL
            ):
                return self._user_lists_cache
            try:
                data = await self._request("GET", "/api/user/list")
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("获取用户歌单失败: %s", err)
                # 失败时不更新缓存,让下次仍走缓存(如果之前缓存有效)
                # 但也不清掉旧缓存,避免临时网络抖动导致全量丢失
                return self._user_lists_cache
            if isinstance(data, dict):
                self._user_lists_cache = data
                self._user_lists_cache_time = asyncio.get_event_loop().time()
                LOGGER.debug(
                    "lxmusic: _get_user_lists 刷新缓存 userList 数量=%d",
                    len(data.get("userList", []) or []),
                )
            else:
                LOGGER.debug(
                    "lxmusic: /api/user/list 返回非 dict: %r", type(data).__name__,
                )
            return self._user_lists_cache

    async def _collect_virtual_meta(
        self, kinds: tuple[str, ...] = ("board", "songlist")
    ) -> list[dict[str, str]]:
        """Populate ``_virtual_meta`` for LX leaderboards / square playlists.

        2026-09-06 BUG #80 split: virtual playlists are NOT yielded into the
        MA library any more (no more ``get_library_playlists`` yield of
        ``list:board:*`` / ``list:songlist:*``). They live only in the browse
        tree under ``{instance_id}://playlists/board`` and
        ``{instance_id}://playlists/square/<tag>``.

        This helper is the single source of truth for those virtual playlists:
        it does the same LX server fetch + pic concurrency the old yield block
        did, then writes ``_virtual_meta[item_id]`` so ``get_playlist`` can
        still resolve metadata via the existing ``_virtual_meta`` path.

        Returns: list of ``{"item_id", "name", "pic", "kind"}`` dicts for
        callers to build ``ItemMapping`` entries.

        ``kinds`` controls what to fetch (subset of ``("board", "songlist")``)
        and respects the ``_import_leaderboards`` / ``_import_square`` flags
        (skipped entries are filtered out, no error).
        """
        out: list[dict[str, str]] = []

        # --- leaderboards ---
        if "board" in kinds and getattr(self, "_import_leaderboards", True):
            # BUG #94 (2026-09-07): 缓存守卫
            # 4 源 boards + 161 个 pic 拉取总耗时 ~25s,用户点开榜单子文件夹
            # 时 browse 触发 _collect_virtual_meta 又跑一遍,UI 上空白直到
            # 超时,体感"读不出歌单"。第一次 fill 后把 pic 存到 _virtual_meta,
            # 后续 browse 直接从 _virtual_meta 构造 out 返回,毫秒级响应。
            cached_boards: list[tuple[str, dict[str, Any]]] = [
                (iid, meta)
                for iid, meta in self._virtual_meta.items()
                if isinstance(meta, dict) and meta.get("kind") == "board"
            ]
            if cached_boards:
                # BUG #96 (2026-09-07): 加 mg=4(咪咕)。
                # LX 服务端实际支持 5 个 source,原代码只 4 个,
                # 导致咪咕 10 个榜单完全没被拉取。
                source_order = {"kg": 0, "kw": 1, "wy": 2, "tx": 3, "mg": 4}

                def _is_hot_cached(name: str) -> bool:
                    return (
                        "飙升" in name
                        or "TOP500" in name
                        or "热歌榜" in name
                        or "新歌榜" in name
                    )

                cached_boards.sort(
                    key=lambda pair: (
                        source_order.get(pair[1].get("source", ""), 99),
                        0 if _is_hot_cached(pair[1].get("name", "")) else 1,
                        pair[1].get("bangid", ""),
                    )
                )
                for iid, meta in cached_boards:
                    out.append({
                        "item_id": iid,
                        "name": meta.get("name", ""),
                        "pic": meta.get("pic", ""),
                        "kind": "board",
                        "source": meta.get("source", ""),
                    })
                LOGGER.debug(
                    "lxmusic: 排行榜从缓存读 %d 条,跳过 4 源重拉",
                    len(cached_boards),
                )
            else:
                # 缓存未命中:首次 fill 4 源 boards + pic。browse 子目录
                # 在 _prefetch_leaderboards 后台任务完成后才会进这里,届时
                # _virtual_meta 已有 board,直接走上面缓存分支。
                await self._fill_leaderboards(out)
        elif "board" in kinds:
            LOGGER.debug("lxmusic: 排行榜开关关闭,browse 入口不展示排行榜")

        # --- square playlists (按 tag 各取 top N) ---
        if "songlist" in kinds and getattr(self, "_import_square", True):
            per_tag = getattr(self, "_LEADERBOARD_TOPN", 5)
            try:
                tags_resp = await self._request("GET", "/api/music/songList/tags")
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("lxmusic: 拉取歌单分类失败: %s", err)
                tags_resp = None
            if isinstance(tags_resp, dict):
                seen_sl_ids: set[str] = set()
                tag_groups = tags_resp.get("tags", []) or []
                sl_jobs: list[dict[str, Any]] = []
                for group in tags_resp.get("tags", []) or []:
                    if not isinstance(group, dict):
                        continue
                    tag_list = group.get("list", []) or []
                    for tag in tag_list:
                        if not isinstance(tag, dict):
                            continue
                        tag_id = (tag.get("id") or "").strip()
                        tag_src = (tag.get("source") or "").strip()
                        if not tag_id or not tag_src:
                            continue
                        try:
                            sl_resp = await self._request(
                                "GET",
                                "/api/music/songList/list",
                                params={"source": tag_src, "tagId": tag_id, "page": 1},
                            )
                        except Exception as err:  # noqa: BLE001
                            LOGGER.debug(
                                "lxmusic: 拉取广场歌单列表失败 %s/%s: %s",
                                tag_src, tag_id, err,
                            )
                            continue
                        if not isinstance(sl_resp, dict):
                            continue
                        picked = 0
                        for sl in sl_resp.get("list", []) or []:
                            if not isinstance(sl, dict) or picked >= per_tag:
                                continue
                            sl_id = str(sl.get("id") or "").strip()
                            if not sl_id or sl_id in seen_sl_ids:
                                continue
                            seen_sl_ids.add(sl_id)
                            picked += 1
                            sl_name = (sl.get("name") or "未命名歌单").strip()
                            sl_author = (sl.get("author") or "").strip()
                            display = f"LX ·{tag_id}:{sl_name}"
                            if sl_author:
                                display = f"{display} - {sl_author}"
                            item_id = f"list:songlist:{tag_src}:{sl_id}"
                            self._playlist_cache.setdefault(item_id, [])
                            self._virtual_meta[item_id] = {
                                "name": display,
                                "source": tag_src,
                                "sl_id": sl_id,
                                "img": sl.get("img") or "",
                                "author": sl.get("author") or "",
                                "kind": "songlist",
                                "tag_id": tag_id,
                            }
                            sl_jobs.append({
                                "item_id": item_id,
                                "source": tag_src,
                                "sl_id": sl_id,
                                "display": display,
                                "tag_id": tag_id,
                                "img": (sl.get("img") or "").strip(),
                            })

                sl_pic_map: dict[str, str] = {}
                if sl_jobs:
                    sem = asyncio.Semaphore(8)

                    async def _fetch_sl_pic(job: dict[str, Any]) -> tuple[str, str | None]:
                        async with sem:
                            pic = await self._fetch_first_pic(
                                "/api/music/songList/detail",
                                source=job["source"],
                                id=job["sl_id"],
                            )
                            return (job["item_id"], pic)

                    results = await asyncio.gather(
                        *(_fetch_sl_pic(j) for j in sl_jobs),
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, BaseException):
                            LOGGER.debug("lxmusic: fetch sl detail pic 异常: %s", r)
                            continue
                        item_id, pic = r
                        if pic:
                            sl_pic_map[item_id] = pic

                LOGGER.debug(
                    "lxmusic: browse 准备展示 %d 个 LX 广场歌单 (拿到 %d 个封面)",
                    len(sl_jobs), len(sl_pic_map),
                )
                for job in sl_jobs:
                    item_id = job["item_id"]
                    display = job["display"]
                    pic = sl_pic_map.get(item_id) or job.get("img", "")
                    out.append({
                        "item_id": item_id,
                        "name": display,
                        "pic": pic,
                        "kind": "songlist",
                        "tag_id": job["tag_id"],
                    })
        elif "songlist" in kinds:
            LOGGER.debug("lxmusic: 广场歌单开关关闭,browse 入口不展示广场歌单")

        return out

    async def _fill_leaderboards(self, out: list[dict[str, str]]) -> None:
        """首次 fill 4 源 LX 排行榜 boards + 封面,填进 out 与 _virtual_meta。

        BUG #94 (2026-09-07) 修复:
        - 原代码 `_collect_virtual_meta` 用 ``if/elif`` 但条件完全相同,
          导致 elif 分支(首次 fill)永不执行,browse 永远拿不到榜单,
          体感"读不出歌单"。这里把 fill 抽成独立 async 方法,挂在
          cache 守卫的 else 分支下调用,逻辑才走得到。
        - 用户要求去掉 source 中间层 BrowseFolder,161 个榜单平铺,
          name 格式 ``[酷狗]/[酷我]/[网易]/[QQ] xxx``,本方法负责
          写入这种展示名,供 browse `playlists/board` 一次返回。
        """
        leaderboard_sources: tuple[str, ...] = ("kg", "kw", "wy", "tx", "mg")
        # BUG #94 (2026-09-07): [kg]/[kw]/[wy]/[tx] → [酷狗]/[酷我]/[网易]/[QQ]
        # BUG #96 (2026-09-07): LX 服务端实际支持 5 个 source(kg/kw/wy/tx/mg),
        # MG 有 10 个榜单,原代码漏了 mg 导致咪咕榜单完全不显示。
        # 排行榜前缀要短(用户要求),跟 SOURCE_NAMES 的"咪咕音乐"等
        # 长名风格不同,这里单独维护一张短前缀表;新平台同步加两边。
        source_label: dict[str, str] = {
            "kg": "酷狗", "kw": "酷我", "wy": "网易", "tx": "QQ", "mg": "咪咕",
        }
        boards_resp_map: dict[str, dict[str, Any]] = {}
        for src in leaderboard_sources:
            try:
                resp = await self._request(
                    "GET",
                    "/api/music/leaderboard/boards",
                    params={"source": src},
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("lxmusic: 拉取 %s 排行榜分类失败: %s", src, err)
                resp = None
            if isinstance(resp, dict):
                boards_resp_map[src] = resp
            # 错开请求,降低触发 LX 互斥锁的概率
            await asyncio.sleep(0.3)

        all_jobs: list[dict[str, str]] = []
        for src in leaderboard_sources:
            resp = boards_resp_map.get(src)
            if not resp:
                continue
            src_actual = (resp.get("source") or src).strip()
            src_zh = source_label.get(src_actual, src_actual)
            for board in (resp.get("list") or []):
                if not isinstance(board, dict):
                    continue
                bangid = str(board.get("bangid") or "").strip()
                bname = (board.get("name") or "未命名榜单").strip()
                if not bangid:
                    continue
                all_jobs.append({
                    "source": src_actual,
                    "bangid": bangid,
                    "bname": bname,
                    # BUG #94: [酷狗]/[酷我]/[网易]/[QQ] 中文前缀
                    "name": f"[{src_zh}] {bname}",
                })

        board_pic_map: dict[str, str] = {}
        # BUG #97 (2026-09-07): 空榜单不导入
        # 实测 LX 服务端部分榜单返回空 list 或 500(如 mg 欧美榜 19190036
        # 500、tx 有声榜 75 返回 0 首),导入后用户点进去没歌曲。
        # 直接发 /leaderboard/list?page=1 一次拿 list,既判断空/异常
        # 又能取首首 pic——比之前 _fetch_first_pic 多 0 次请求。
        skipped_empty: list[tuple[str, str]] = []  # (bangid, name) 供日志
        skipped_keys: set[str] = set()
        if all_jobs:
            # BUG #93: pic 拉取串行(Semaphore=1)。
            # Semaphore=8 在 161 个榜单场景下会大量触发 LX 互斥锁,
            # 拿到空 pic。串行慢一点但稳定,每个 pic ~150ms,
            # 161 个 ~25s;browse 后台预 fill 后这个延迟对用户隐藏。
            sem = asyncio.Semaphore(1)

            async def _probe_board(
                job: dict[str, str],
            ) -> tuple[str, list[dict[str, Any]]]:
                """返回 (key, items);items 为空 / 异常 → 跳过此榜单。"""
                key = f"{job['source']}:{job['bangid']}"
                async with sem:
                    for retry in range(3):
                        try:
                            data = await self._request(
                                "GET",
                                "/api/music/leaderboard/list",
                                params={
                                    "source": job["source"],
                                    "bangid": job["bangid"],
                                    "page": 1,
                                },
                            )
                            items = data.get("list", []) if isinstance(data, dict) else []
                            if not isinstance(items, list):
                                items = []
                            return (key, items)
                        except Exception as err:  # noqa: BLE001
                            if retry < 2:
                                await asyncio.sleep(0.5 * (2 ** retry))
                            else:
                                LOGGER.debug(
                                    "lxmusic: probe %s list 3 次失败: %s",
                                    key, err,
                                )
                                return (key, [])
                    return (key, [])

            results = await asyncio.gather(
                *(_probe_board(j) for j in all_jobs),
                return_exceptions=True,
            )
            for j, r in zip(all_jobs, results):
                if isinstance(r, BaseException):
                    LOGGER.debug("lxmusic: probe board 异常: %s", r)
                    skipped_keys.add(f"{j['source']}:{j['bangid']}")
                    skipped_empty.append((j["bangid"], j["bname"]))
                    continue
                key, items = r
                if not items:
                    skipped_keys.add(key)
                    skipped_empty.append((j["bangid"], j["bname"]))
                    continue
                # 取首首 pic(优先 item.pic / al.picUrl)
                first = items[0]
                if isinstance(first, dict):
                    pic = (
                        first.get("pic")
                        or ((first.get("al") or {}).get("picUrl") if isinstance(first.get("al"), dict) else "")
                        or first.get("img")
                        or ""
                    ).strip()
                    if pic:
                        board_pic_map[key] = pic

        # 过滤掉空榜单
        if skipped_empty:
            LOGGER.info(
                "lxmusic: 跳过 %d 个空榜单 (服务器返回空 list 或 500): %s",
                len(skipped_empty),
                ", ".join(f"{bid}/{nm}" for bid, nm in skipped_empty[:5])
                + ("..." if len(skipped_empty) > 5 else ""),
            )
        LOGGER.info(
            "lxmusic: 首次 fill %d 个 LX 排行榜 (5 源, 拿到 %d 个封面, 跳过 %d 个空)",
            len(all_jobs), len(board_pic_map), len(skipped_empty),
        )
        for job in all_jobs:
            bangid = job["bangid"]
            src = job["source"]
            key = f"{src}:{bangid}"
            if key in skipped_keys:
                # BUG #97 (2026-09-07): 空榜单 / 服务端 500,不导入。
                continue
            item_id = f"list:board:{src}:{bangid}"
            self._playlist_cache.setdefault(item_id, [])
            self._virtual_meta[item_id] = {
                "name": job["name"],
                "source": src,
                "bangid": bangid,
                "kind": "board",
                "pic": board_pic_map.get(key, ""),
            }
            out.append({
                "item_id": item_id,
                "name": job["name"],
                "pic": board_pic_map.get(key, ""),
                "kind": "board",
                "source": src,
            })

    async def _fetch_first_pic(self, path: str, **params) -> str | None:
        """拉取 LX API 列表的第一项封面 URL。

        2026-09-04 Task #42 实现:用户要求"歌单用第一首歌封面做歌单封面"。
        LX server 排行榜/广场歌单返回的顶层 ``img`` 字段常常是空字符串或缺失,
        导致 sync 出来的虚拟歌单封面是空。在 sync 阶段并发拉每个榜单/歌单的第一页
        第一首,把 ``pic`` 字段赋给 ``playlist.metadata.images``。

        用于:
        - 用户歌单:实际不需要 helper(``_iter_user_playlists`` 已经把 songs 拉回来
          了,直接 ``songs[0].get("pic")`` 即可),仅用于排行榜和广场歌单。
        - 排行榜:``/api/music/leaderboard/list?source=X&bangid=Y&page=1``
        - 广场歌单:``/api/music/songList/detail?source=X&id=Y`` (此处网易云某些
          歌曲 ``al.picUrl`` 为空导致首歌曲 pic 不可用,调用方应 fallback 到歌单
          自身封面 ``sl.get("img")``,见 ``get_library_playlists`` 广场歌单 yield 段)

        任一环节失败都返回 ``None``,由调用方决定是否赋值,不影响原 yield 流程。
        """
        try:
            resp = await self._request("GET", path, params=params)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("lxmusic: fetch_first_pic %s 失败: %s", path, err)
            return None
        if not isinstance(resp, dict):
            return None
        items = resp.get("list") or []
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = (first.get("pic") or first.get("img") or "").strip()
        return url or None

    async def _get_square_tags_cached(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """获取 tags API 缓存,首次调用 fetch,后续复用。

        返回 (raw tags list, hotTag list)。
        BUG #83 (2026-09-06): browse 顶层 + 子层共用这份缓存,避免重复请求。
        BUG #88 (2026-09-06): LX /songList/list 不按 tag 过滤(无论传父 tag
        还是 sub_tag 都返回 source 全量),所以丢弃 parent→sub 展开,改用
        API 直接返回的 hotTag 字段(10 个)作为顶层热门分类。
        """
        cached = getattr(self, "_square_tags_cache", None)
        if cached is not None:
            return cached
        resp = await self._request("GET", "/api/music/songList/tags")
        raw = self._normalize_list(
            resp.get("tags") if isinstance(resp, dict) else resp
        )
        hot_tags: list[dict[str, Any]] = []
        if isinstance(resp, dict):
            for h in self._normalize_list(resp.get("hotTag")):
                if isinstance(h, dict):
                    hot_tags.append(h)
        self._square_tags_cache = (raw, hot_tags)
        LOGGER.info(
            "lxmusic: 缓存广场 tags API | 父 tag=%d 个, hotTag=%d 个",
            len(raw), len(hot_tags),
        )
        return self._square_tags_cache

    async def _fetch_square_all_items(self) -> list[ItemMapping]:
        """拉 LX /songList/list 全量,按歌曲数(total)降序,取 top 200。

        BUG #89 (2026-09-06): LX /songList/list 不按 tag 过滤,任何
        父→子展开或 hotTag 切片切来切去都是同一份 source 全量(curl 验证
        华语/流行 两次请求前 5 个 id 完全一致)→ 改单层 + 拉全量。
        BUG #90 (2026-09-06): LX 服务端 /songList/list 真的分页,每页
        固定 36 条,total=1752。只拉 page=1 给用户 36 条首页热门 →
        必须翻页拉全量。
        BUG #91 (2026-09-06 Part 3): 实测 LX 服务端对**并发请求做了互斥锁**,
        asyncio.gather 并发 10 页时只有"最后一页"返回数据,其他 9 页全
        空 list;并发 3 页时只有 page=3 返回数据。**必须顺序翻页**。
        实现: page=1..N 顺序拉,每页 retry 3 次退避 0.5/1/2s,空 list 即
        末尾停止;用第一页 total 估算 max_pages (兜底 50);跨页去重 + 按
        total 降序 + top 200。
        """
        page_size = 36  # LX 服务端硬编码每页 36,客户端 limit 被忽略

        # 第一页:拿 total + 起始 list
        first_resp = await self._request(
            "GET",
            "/api/music/songList/list",
            params={
                "source": self._default_source,
                "page": 1,
                "limit": page_size,
            },
        )
        first_lists = self._normalize_list(first_resp)
        server_total = 0
        if isinstance(first_resp, dict):
            try:
                server_total = int(first_resp.get("total") or 0)
            except (TypeError, ValueError):
                server_total = 0

        # 根据 total 估算 max_pages (兜底 50)
        if server_total > 0:
            max_pages = min((server_total // page_size) + 2, 50)
        else:
            max_pages = 50  # total 字段缺失,兜底

        all_lists: list[dict[str, Any]] = list(first_lists)

        # 顺序翻页 2..max_pages
        for p in range(2, max_pages + 1):
            page_lists: list[dict[str, Any]] = []
            for retry in range(3):
                try:
                    resp = await self._request(
                        "GET",
                        "/api/music/songList/list",
                        params={
                            "source": self._default_source,
                            "page": p,
                            "limit": page_size,
                        },
                    )
                    page_lists = self._normalize_list(resp)
                    break
                except Exception as err:  # noqa: BLE001
                    LOGGER.debug(
                        "lxmusic: 广场歌单 page=%d 重试 %d: %s",
                        p, retry + 1, err,
                    )
                    await asyncio.sleep(0.5 * (2 ** retry))
            if not page_lists:
                # 空 list(末尾 / 服务端到此为止) 或 3 次重试全失败 → 停止
                break
            all_lists.extend(page_lists)

        # 全局去重 + 构建 ItemMapping (跨页去重,LX 偶尔可能跨页重复)
        out: list[ItemMapping] = []
        seen: set[str] = set()
        for sl in all_lists:
            sl_id_raw = sl.get("id") or sl.get("listId") or sl.get("playId")
            sl_id = str(sl_id_raw) if sl_id_raw else ""
            if not sl_id or sl_id in seen:
                continue
            seen.add(sl_id)
            sl_name = sl.get("name") or sl.get("listName") or sl_id
            sl_source = sl.get("source") or self._default_source
            sl_item_id = f"sl:{sl_source}:{sl_id}"
            sl_total = 0
            try:
                sl_total = int(sl.get("total") or 0)
            except (TypeError, ValueError):
                pass
            self._square_meta[sl_item_id] = {
                "source": sl_source,
                "id": sl_id,
                "name": sl_name,
                "img": (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                ),
                "total": sl_total,
            }
            sl_img = (self._square_meta[sl_item_id].get("img") or "").strip()
            sl_image = (
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sl_img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if sl_img
                else None
            )
            out.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id=sl_item_id,
                    provider=self.instance_id,
                    name=sl_name,
                    image=sl_image,
                )
            )
        # 按 total 降序 + 取 top 200
        out.sort(
            key=lambda im: self._square_meta.get(im.item_id, {}).get("total", 0),
            reverse=True,
        )
        LOGGER.info(
            "lxmusic: 广场歌单 | 翻页拉到 %d 条 (跨页去重后), top %d, server_total=%d",
            len(out), min(200, len(out)), server_total,
        )
        return out[:200]

    async def _fetch_square_sub_tag_items(
        self,
        sub_tid: str,
        tag_name: str = "",
    ) -> list[ItemMapping]:
        """拉取单个 sub_tag 的歌单列表 (去重 + 缓存 _square_meta + ItemMapping.image)。

        BUG #83: browse `playlists/square/{id}` 子层 helper。
        BUG #88 (2026-09-06): LX /songList/list 不按 tag 过滤,所有 sub_tag
        返回相同内容。给 ItemMapping.name 加 [tag_name] 前缀,让用户能看出
        这份歌单是从哪个 hotTag 子层点开的(避免切换分类时看到一模一样
        的名字误以为是 bug)。tag_name 为空时不加前缀。
        BUG #89 (2026-09-06): 当前已不被调用(LX 不按 tag 过滤的硬约束),
        保留函数定义供后续扩展/回滚,顶层 browse 改走 _fetch_square_all_items。
        """
        lists: list[dict[str, Any]] = []
        for param_name in ("tag", "id"):
            lists = self._normalize_list(
                await self._request(
                    "GET",
                    "/api/music/songList/list",
                    params={
                        param_name: sub_tid,
                        "source": self._default_source,
                        "page": 1,
                        "limit": 50,
                    },
                )
            )
            if lists:
                break
        out: list[ItemMapping] = []
        seen_local: set[str] = set()
        for sl in lists:
            sl_id_raw = sl.get("id") or sl.get("listId") or sl.get("playId")
            sl_id = str(sl_id_raw) if sl_id_raw else ""
            if not sl_id or sl_id in seen_local:
                continue
            seen_local.add(sl_id)
            sl_name = sl.get("name") or sl.get("listName") or sl_id
            sl_source = sl.get("source") or self._default_source
            sl_item_id = f"sl:{sl_source}:{sl_id}"
            # BUG #88 (2026-09-06): 给展示名加 [hotTag] 前缀 (用 | 分隔避免和
            # 歌单自身名字里可能含的 [ ] 冲突);缓存里仍存原始名,_get_playlist
            # 拿到真实数据后会覆盖 _square_meta.name,不影响下游。
            display_name = f"[{tag_name}] {sl_name}" if tag_name else sl_name
            self._square_meta[sl_item_id] = {
                "source": sl_source,
                "id": sl_id,
                "name": sl_name,
                "img": (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                ),
            }
            sl_img = (self._square_meta[sl_item_id].get("img") or "").strip()
            sl_image = (
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sl_img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if sl_img
                else None
            )
            out.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id=sl_item_id,
                    provider=self.instance_id,
                    name=display_name,
                    image=sl_image,
                )
            )
        return out

    @staticmethod
    def _iter_user_playlists(
        data: dict[str, Any]
    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """遍历用户歌单，返回 (id, name, songs) 三元组列表。

        BUG #34 (2026-09-04) 修复: lxserver 实际返回结构里:
        - defaultList / loveList 是「歌曲 list」(顶格就是 LX.Music.MusicInfo 数组),
          不是嵌套 {id,name,list} 的 dict。原代码用 ``isinstance(lst, dict)`` 判断,
          永远跳过,导致"我最近播放 / 我收藏 (lxserver 约定名;MA 显示为'洛雪收藏')"两个内置歌单从未同步进 MA。
        - userList 是「歌单 list」,每项是 {id, name, list:[歌曲...]} 的歌单 dict。
          原代码对 userList 处理是对的,但 defaultList/loveList 一直没暴露。
        这里把 defaultList / loveList 各自打包成一个虚拟歌单(用 __default__ /
        __love__ 当 item_id,MA 不会和真实歌单冲突)。
        """
        out: list[tuple[str, str, list[dict[str, Any]]]] = []
        # defaultList = "我最近播放", loveList = "我收藏"(lxserver 约定) —— MA 显示名 "洛雪收藏"
        for key, virtual_id, virtual_name in (
            ("defaultList", "__default__", "我最近播放"),
            ("loveList", "__love__", "洛雪收藏"),
        ):
            lst = data.get(key)
            if isinstance(lst, list):
                # 顶层就是歌曲数组,直接当虚拟歌单用
                out.append((virtual_id, virtual_name, lst))
            elif isinstance(lst, dict):
                # 旧版/兼容: 部分 lxserver 版本可能仍嵌套 {id,name,list}
                out.append(
                    (lst.get("id", key), lst.get("name", virtual_name), lst.get("list", []))
                )
        for lst in data.get("userList", []) or []:
            if isinstance(lst, dict):
                pl_id = lst.get("id")
                if not pl_id:
                    # 没有 id 的歌单跳过,避免与虚拟歌单冲突
                    continue
                out.append(
                    (
                        pl_id,
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

    @staticmethod
    def _pick_content_type(
        result: Any, url: str, quality: str,
    ) -> Any:
        """双重保险识别 lxserver 实际返回的音频 content_type。

        BUG #44 (2026-09-05): 之前硬编码 ContentType.MP3,在用户 lxserver 端开启
        "最高音质"时,实际 URL 是 flac,导致 amcfy 桥接给 APP 发 audio/mpeg,
        APP 用 mp3 解码器解 flac 字节流 → 听到糊/破音。

        识别优先级:
        1) lxserver 响应里的 type/quality 字段(lxserver 通常在 result.data.type
           或 result.type 返回 "flac"/"320k"/"128k")
        2) URL 后缀兜底(.flac/.mp3/.m4a/.ogg/.opus)
        3) 都没识别到才 fallback 到 MP3(对 128k/320k 默认就是 MP3)
        """
        # 第一保险: lxserver 响应里的 type 字段
        type_hint = ""
        if isinstance(result, dict):
            for container in (result, result.get("data") if isinstance(result.get("data"), dict) else {}):
                t = container.get("type") if isinstance(container, dict) else None
                if isinstance(t, str) and t:
                    type_hint = t.lower()
                    break
        if not type_hint:
            type_hint = (quality or "").lower()

        if type_hint in ("flac", "flac24bit", "flac24", "lossless", "ape", "wav"):
            return ContentType.FLAC
        if type_hint in ("320k", "320", "mp3", "128k", "128"):
            return ContentType.MP3
        if type_hint in ("aac", "m4a", "alac"):
            return ContentType.AAC if type_hint == "aac" else ContentType.M4A
        if type_hint in ("ogg", "vorbis"):
            return ContentType.OGG
        if type_hint == "opus":
            return ContentType.OPUS

        # 第二保险: URL 后缀
        u = (url or "").lower().split("?", 1)[0]
        if u.endswith(".flac"):
            return ContentType.FLAC
        if u.endswith(".m4a"):
            return ContentType.M4A
        if u.endswith(".aac"):
            return ContentType.AAC
        if u.endswith(".ogg"):
            return ContentType.OGG
        if u.endswith(".opus"):
            return ContentType.OPUS
        if u.endswith(".mp3"):
            return ContentType.MP3

        # 终极 fallback
        return ContentType.MP3

    @staticmethod
    def _infer_metadata_content_type(item: dict[str, Any], source: str) -> Any:
        """按 lxserver item 里可用的字段,推断 ProviderMapping 应该宣告的 content_type。

        BUG #44 (2026-09-05): 之前所有 ProviderMapping 都被硬编码 MP3,导致 amcfy
        桥接永远发 audio/mpeg。看 item 里是否有 type/quality/url/试听链接后缀。

        - item.type == "flac"/"320k"/"128k": 直接用
        - item.types 数组 (lxserver 部分版本): 选最高优先级含 flac 的
        - item 中 meta / _quality / quality 字段
        - lxserver 试听预览 URL 后缀
        - 都没: 按平台默认(优先 FLAC,因为"最高音质"设置下多数平台会优先返回 flac)
        """
        # 1) 单值 type
        t = item.get("type")
        if isinstance(t, str) and t:
            tl = t.lower()
            if "flac" in tl:
                return ContentType.FLAC
            if "320" in tl or "128" in tl:
                return ContentType.MP3
            if "aac" in tl or "m4a" in tl:
                return ContentType.M4A
            if "ogg" in tl:
                return ContentType.OGG
            if "opus" in tl:
                return ContentType.OPUS

        # 2) 多音质数组
        for key in ("types", "qualities", "_quality", "qualityList"):
            arr = item.get(key)
            if isinstance(arr, list) and arr:
                # 优先找含 flac 的
                for q in arr:
                    if isinstance(q, str) and "flac" in q.lower():
                        return ContentType.FLAC
                for q in arr:
                    if isinstance(q, str) and ("320" in q.lower() or "128" in q.lower()):
                        return ContentType.MP3
                break

        # 3) 单个 quality 字段
        q = item.get("quality") or item.get("_quality")
        if isinstance(q, str) and q:
            ql = q.lower()
            if "flac" in ql:
                return ContentType.FLAC
            if "320" in ql or "128" in ql or "mp3" in ql:
                return ContentType.MP3
            if "m4a" in ql or "aac" in ql:
                return ContentType.M4A

        # 4) 试听/预览 URL 后缀
        for key in ("previewUrl", "preview_url", "trialUrl", "_preview"):
            val = item.get(key)
            if isinstance(val, str) and val:
                u = val.lower().split("?", 1)[0]
                if u.endswith(".flac"):
                    return ContentType.FLAC
                if u.endswith(".m4a"):
                    return ContentType.M4A
                if u.endswith(".aac"):
                    return ContentType.AAC
                if u.endswith(".ogg"):
                    return ContentType.OGG
                if u.endswith(".opus"):
                    return ContentType.OPUS
                if u.endswith(".mp3"):
                    return ContentType.MP3
                break

        # 5) 按平台默认最高音质推断(用户服务端"最高音质"设置,平台支持 flac 就标 flac)
        source_default_flac = {
            "kw", "kg", "tx", "wy", "qq", "netease", "163", "mg",
        }
        if source in source_default_flac:
            return ContentType.FLAC
        return ContentType.MP3

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

        # BUG #36 (2026-09-04) 修复: lxserver 对 songid 带前缀 (wy_xxxxx) 与
        # 纯数字 (xxxxx) 走完全不同的解析路径——
        # - wy_1973665667 → 走用户配置的自定义源 (如 metingapi 网关,可能挂)
        # - 1973665667 → 走网易官方源 (m701.music.126.net 直链,稳)
        # 搜索 hit 自带 songmid 字段且是纯数字,所以播放正常;
        # 自建歌单歌曲的 id 是 wy_1973665667,卡在 metingapi。
        # 这里把"剥前缀纯数字版"作为 songmid 写到 raw_cache 的副本,下游
        # get_stream_details 拿到 raw 时直接用,无需关心 ID 格式。
        # 注意: track.item_id 仍用原带前缀 ID (避免改了 MA 已收藏的 item_id),
        # 关键修改只在 raw_cache 的副本上。
        prefix = f"{source}_"
        if (
            song_id.startswith(prefix)
            and not item.get("songmid")
            and song_id[len(prefix):].isdigit()
        ):
            raw_for_cache: dict[str, Any] = dict(item)
            raw_for_cache["songmid"] = song_id[len(prefix):]
            LOGGER.debug(
                "lxmusic: 剥前缀给 raw 补 songmid=%s (item.id=%s)",
                raw_for_cache["songmid"], song_id,
            )
        else:
            raw_for_cache = item

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
                    # BUG #44 (2026-09-05): ProviderMapping.audio_format 也曾硬编码 MP3,
                    # 导致 amcfy 桥接用 mp3.content_type 推 Content-Type=audio/mpeg,
                    # 但 stream_details.path 可能是 .flac,APP 拿 mp3 解码器解 flac 字节流
                    # 会听起来糊。这里按 lxserver item 里能识别的字段推断 quality,
                    # 拿不到就保守默认 FLAC(lxserver 端"最高音质"设置下多数平台优先 flac,
                    # 拿不到 flac 时 get_stream_details 实际 URL 后缀会兜底)。这样 amcfy
                    # 的 _guess_content_type 通过 af.output_format_str(含 "flac")会发 audio/flac。
                    audio_format=AudioFormat(
                        content_type=self._infer_metadata_content_type(item, source),
                    ),
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
            self._raw_cache[track.item_id] = raw_for_cache
        return track

    async def _enrich_playlist_pics(self, items: list[dict[str, Any]]) -> None:
        """补全用户歌单歌曲的封面 + 专辑信息 (就地修改 items)。

        lxserver 的 /api/user/list 返回的 LX.Music.MusicInfo 没有 img/pic/image/cover
        /albumName/albumId 字段,而广场歌单 / 搜索结果都有 —— 这导致用户自己
        创建/收藏的歌单歌曲在 MA 上没封面且显示"未知专辑"。解决办法: 用
        name + singer 调 /api/music/search 重搜,取时长匹配 (±5s, 避免 BUG #35
        同名翻唱/remix 错匹配) 的第一条 hit,写回 pic/albumName/albumId。

        用 _pic_enrich_cache 缓存 (source, name, singer, interval) -> (pic,
        album_name, album_id) 避免重复搜。并发 5 个搜索避免串行太慢。

        2026-09-04 BUG #37 扩展: 同时补专辑 (albumName/albumId), 解决用户自建
        歌单歌曲显示"未知专辑"。
        """
        if not items:
            return
        sem = asyncio.Semaphore(5)

        def _extract_singer(item: dict[str, Any]) -> str:
            singer_raw = item.get("singer") or ""
            if isinstance(singer_raw, list):
                if singer_raw and isinstance(singer_raw[0], dict):
                    return singer_raw[0].get("name", "") or ""
                if singer_raw and isinstance(singer_raw[0], str):
                    return str(singer_raw[0])
                return ""
            return str(singer_raw).split("/")[0].split("、")[0].strip()

        async def _enrich_one(item: dict[str, Any]) -> None:
            src = item.get("source") or self._default_source
            name = (item.get("name") or "").strip()
            singer_key = _extract_singer(item)
            if not name:
                return
            need_pic = not (
                item.get("img") or item.get("pic") or item.get("image") or item.get("cover")
            )
            need_album = not (item.get("albumName") or item.get("album")) or not self._album_id(item)
            if not need_pic and not need_album:
                return
            # 原始 interval (用于时长校验, 避免同名翻唱)
            item_interval = item.get("interval") or item.get("duration") or item.get("time") or ""
            expected_seconds = (
                item_interval if isinstance(item_interval, int) else self._parse_duration(str(item_interval))
            )
            cache_key = (src, name, singer_key, expected_seconds)
            if cache_key in self._pic_enrich_cache:
                cached = self._pic_enrich_cache[cache_key]
                if isinstance(cached, dict):
                    if need_pic and cached.get("pic"):
                        item["pic"] = cached["pic"]
                    if need_album and cached.get("album_name"):
                        item["albumName"] = cached["album_name"]
                        if cached.get("album_id"):
                            item["albumId"] = cached["album_id"]
                return
            async with sem:
                keyword = f"{name} {singer_key}".strip()
                try:
                    hits = await self._search_source(src, keyword, page=1, page_size=10)
                except Exception as err:  # noqa: BLE001
                    LOGGER.debug(
                        "lxmusic: 用户歌单歌曲补元数据失败 %s/%s/%s: %s",
                        src, name, singer_key, err,
                    )
                    self._pic_enrich_cache[cache_key] = None
                    return
                picked_pic: str | None = None
                picked_album_name: str | None = None
                picked_album_id: str | None = None
                fallback_pic: str | None = None  # 时长不匹配但有 pic 的兜底
                for hit in hits:
                    pic = (
                        hit.get("img")
                        or hit.get("pic")
                        or hit.get("image")
                        or hit.get("cover")
                    )
                    aname = hit.get("albumName") or hit.get("album")
                    aid = (
                        hit.get("albumId")
                        or hit.get("albumid")
                        or hit.get("album_id")
                    )
                    if not pic and not aname:
                        continue
                    # 时长校验
                    hit_interval = hit.get("interval") or hit.get("duration") or ""
                    hit_seconds = self._parse_duration(str(hit_interval)) if hit_interval else 0
                    interval_ok = (
                        expected_seconds <= 0
                        or hit_seconds <= 0
                        or abs(hit_seconds - expected_seconds) <= 5
                    )
                    if interval_ok:
                        if not picked_pic and pic:
                            picked_pic = pic
                        if not picked_album_name and aname:
                            picked_album_name = aname
                            picked_album_id = str(aid) if aid else None
                        if picked_pic and picked_album_name:
                            break
                    else:
                        # 时长不符但 pic 在,留作封面兜底
                        if not fallback_pic and pic:
                            fallback_pic = pic
                # 写入 item (in-place)
                if need_pic:
                    final_pic = picked_pic or fallback_pic
                    if final_pic:
                        item["pic"] = final_pic
                if need_album and picked_album_name:
                    item["albumName"] = picked_album_name
                    if picked_album_id:
                        item["albumId"] = picked_album_id
                # 缓存: 即使没补全也缓存 None, 避免每次都重搜
                if picked_pic or picked_album_name or fallback_pic:
                    self._pic_enrich_cache[cache_key] = {
                        "pic": picked_pic or fallback_pic,
                        "album_name": picked_album_name,
                        "album_id": picked_album_id,
                    }
                else:
                    self._pic_enrich_cache[cache_key] = None
                if picked_pic or picked_album_name:
                    LOGGER.debug(
                        "lxmusic: 用户歌单歌曲补元数据 %s/%s/%s -> pic=%s album=%s/%s",
                        src, name, singer_key,
                        bool(item.get("pic")), item.get("albumName"), item.get("albumId"),
                    )

        await asyncio.gather(*(_enrich_one(it) for it in items))

    async def _resolve_songmid_for_src(
        self,
        src: str,
        name: str,
        singer: str,
        expected_interval: str | int | None = None,
    ) -> str | None:
        """跨平台重搜获取该平台的 songmid。

        lxserver 的 /api/music/url 只走同源自定义源（如 wy 平台 → meting/ikun），
        不跨平台 fallback。当 wy 平台的自定义源（如 metingapi 网关）失效时，
        lxmusic 客户端必须自行跨平台重搜，拿到 kw/kg/tx/mg 平台的真实 songmid，
        再调用 /api/music/url 用对应平台获取播放链接。

        BUG #35 (2026-09-04) 修复: 之前直接取第一条搜索 hit 当成"同一首歌",
        实际上 kw/kg/tx/mg 平台搜 "海屿你 马也_Crabbit" 可能返回:
        - 同名翻唱版本
        - 同名不同专辑的 remix
        - 同名其他歌手的歌曲
        这些都让 MA 播放"错曲"。这里加 expected_interval 入参,要求搜索 hit 的
        interval 与原歌单歌曲差异在 ±5s 内才算同一首,否则丢弃。

        用 (src, name, singer, interval) 缓存避免重复搜。
        """
        cache_key = (src, name, singer, expected_interval or "")
        if cache_key in self._songmid_resolve_cache:
            return self._songmid_resolve_cache[cache_key]
        if not name:
            self._songmid_resolve_cache[cache_key] = None
            return None
        # 把 expected_interval 转成秒,用于校验命中
        expected_seconds: int = 0
        if expected_interval:
            if isinstance(expected_interval, int):
                expected_seconds = expected_interval
            else:
                expected_seconds = self._parse_duration(str(expected_interval))
        keyword = f"{name} {singer}".strip()
        try:
            hits = await self._search_source(src, keyword, page=1, page_size=10)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug(
                "lxmusic: 跨平台重搜失败 %s/%s/%s: %s", src, name, singer, err,
            )
            self._songmid_resolve_cache[cache_key] = None
            return None
        for hit in hits:
            mid = self._item_song_id(hit)
            if not mid:
                continue
            # 时长校验: 如果原歌单歌曲有时长,要求搜索 hit 时长差异 ≤5s
            if expected_seconds > 0:
                hit_seconds = self._parse_duration(str(hit.get("interval", "")))
                if hit_seconds <= 0:
                    # 搜索 hit 没时长字段,无法校验,跳过避免错曲
                    continue
                if abs(hit_seconds - expected_seconds) > 5:
                    LOGGER.debug(
                        "lxmusic: 跨平台重搜 %s 命中 %s/%s 时长不符 (原=%ds, hit=%ds),跳过",
                        src, hit.get("name"), hit.get("singer"),
                        expected_seconds, hit_seconds,
                    )
                    continue
            self._songmid_resolve_cache[cache_key] = mid
            LOGGER.debug(
                "lxmusic: 跨平台重搜 %s/%s/%s -> songmid=%s (时长校验通过)",
                src, name, singer, mid,
            )
            return mid
        LOGGER.warning(
            "lxmusic: 跨平台重搜 %s/%s/%s 未找到时长匹配的歌曲,放弃 fallback (避免错曲)",
            src, name, singer,
        )
        self._songmid_resolve_cache[cache_key] = None
        return None

    async def _check_url_playable(self, url: str) -> bool:
        """HEAD 检查 URL 是否返回音频内容。

        metingapi.nanorocky.top 这类网关代理在某些时段会返回 200 + 空内容
        （CF 反爬虫拦截），lxserver 服务端用 needle HEAD 解析 302 重定向也
        拿不到真实音频 URL。把这种 gateway URL 直接交给 ffmpeg 会报
        "Invalid data found when processing input"。客户端需要自己做一次
        content-type 检查，过滤掉非音频 URL。

        返回 True: 可播放（content-type 是 audio/* / video/* / m3u8 等）
        返回 False: 不可播放（text/html 空内容、网关挂了）

        BUG #33 (2026-09-04) 已知 metingapi.nanorocky.top 整体失效且
        lxserver 直接返回 gateway URL,这里把该域名直接判为不可用,让
        /api/music/url 之外的跨平台 fallback 有机会被触发。
        """
        if not url or not url.startswith("http"):
            return False
        # 黑名单:已知会返回 200+空内容 / CF 418 的域名,直接跳过
        # 避免做 HEAD 请求也拿不到有效反馈
        bad_hosts = ("metingapi.nanorocky.top",)
        if any(h in url for h in bad_hosts):
            LOGGER.warning(
                "lxmusic: URL 命中已知失效网关 url=%s,跳过", url[:80],
            )
            return False
        try:
            session = self._session()
            async with session.head(url, allow_redirects=True, timeout=8) as resp:
                ct = (resp.headers.get("Content-Type") or "").lower()
                playable = (
                    ct.startswith("audio/")
                    or ct.startswith("video/")
                    or ct.startswith("application/octet-stream")
                    or "mpegurl" in ct
                    or "mp2t" in ct
                )
                if not playable:
                    LOGGER.warning(
                        "lxmusic: URL content-type=%s 不可播放 url=%s",
                        ct, url[:80],
                    )
                return playable
        except Exception as err:  # noqa: BLE001
            # 网络错误时让 fallback 继续尝试其他源,不要在这里放过坏 URL
            LOGGER.warning(
                "lxmusic: HEAD 检查异常 url=%s err=%s,按不可用处理",
                url[:80], err,
            )
            return False

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
