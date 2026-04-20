"""豆瓣音乐元数据提供者 for Music Assistant (v1.5.1).
通过豆瓣公开接口补全音乐库中艺术家简介、专辑封面、流派、发行年份等元数据。
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, cast

import aiohttp.client_exceptions

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
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

SUPPORTED_FEATURES: Set[ProviderFeature] = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
}

DOUBAN_BASE_URL = "https://music.douban.com"
DOUBAN_SUGGEST_URL = "https://www.douban.com/j/search_suggest"

RATE_LIMIT = 1
RATE_PERIOD = 5

CACHE_TTL = 86400 * 7

REQUEST_TIMEOUT = 10

CRAWLER_USER_AGENT = (
    "MusicAssistant-DoubanMetadata/1.0 "
    "(https://github.com/music-assistant/server; polite crawler)"
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_IMG_PATH_PATTERN = re.compile(r"/img/(?:artist|subject|musician)/(?:small|medium|[sml])/")
_VIEW_IMG_PATTERN = re.compile(r"/view/(?:subject|artist|celebrity)/[sml]/")
_PERSONAGE_IMG_PATTERN = re.compile(r"/view/personage/[sml]/")

SUBJECT_ID_RE = re.compile(r"/subject/(\d+)/")
MUSICIAN_ID_RE = re.compile(r"/musician/(\d+)/")
_SEARCH_DATA_RE = re.compile(r'window\.__DATA__\s*=\s*({.*?});', re.DOTALL)
_IMG_DOMAIN_RE = re.compile(r'(https?://)(img\d+)(\.doubanio\.com/)(.*)')


def _cover_sort_key(url: str) -> int:
    if "/view/celebrity/" in url:
        return 0
    if "/view/subject/" in url:
        return 1
    if "/view/personage/" in url:
        return 2
    if "/img/musician/" in url:
        return 3
    return 4


def _parse_rating(r: Any) -> float:
    if not r:
        return -1.0
    try:
        return float(r)
    except (ValueError, TypeError):
        return -1.0


def _rating_to_stars(rating: float) -> str:
    if rating <= 0:
        return "☆☆☆☆☆"
    stars_5 = round(rating / 2.0 * 2 + 0.001) / 2
    stars_5 = max(0.5, min(5.0, stars_5))
    full_stars = int(stars_5)
    has_half = (stars_5 - full_stars) >= 0.5
    empty_stars = 5 - full_stars - (1 if has_half else 0)
    result = "★" * full_stars
    if has_half:
        result += "½"
    result += "☆" * empty_stars
    return result

ARTIST_NAME_SEPARATORS = ["/", "\\", "|", ",", "；", ";", "+", "&", "、"]

KNOWN_GENRES = {
    "流行", "摇滚", "民谣", "爵士", "古典", "电子", "嘻哈", "说唱",
    "R&B", "R&amp;B", "蓝调", "布鲁斯", "乡村", "灵魂", "放克",
    "雷鬼", "朋克", "金属", "核", "后摇", "氛围", "实验", "噪音",
    "新世纪", "new age", "britpop", "indie", "alternative", "post-rock",
    "grunge", "emo", "ska", "reggae", "dub", "trip-hop", "lo-fi",
    "synth", "synth-pop", "dream pop", "shoegaze", "math rock",
    "post-punk", "new wave", "dark wave", "gothic", "industrial",
    "drum and bass", "dubstep", "house", "techno", "trance",
    "ambient", "downtempo", "chillout", "lounge",
    "latin", "bossa nova", "salsa", "samba",
    "世界音乐", "中国传统", "戏曲", "国风", "古风",
    "轻音乐", "纯音乐", "newage", "治愈",
    "影视", "原声", "ost", "soundtrack",
    "儿童", "儿歌", "童谣",
    "有声书", "播客", "脱口秀",
    "节奏布鲁斯", "rnb",
}

class ConfigKeys:
    ENABLE_ARTIST_METADATA = "enable_artist_metadata"
    ENABLE_ALBUM_METADATA = "enable_album_metadata"
    ENABLE_TRACK_METADATA = "enable_track_metadata"
    ENABLE_IMAGES = "enable_images"


def clean_artist_name(name: Optional[str]) -> str:
    if not name:
        return ""
    name = name.strip()
    for sep in ARTIST_NAME_SEPARATORS:
        if sep in name:
            name = name.split(sep)[0].strip()
            break
    return " ".join(name.split())


def upgrade_img_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = _PERSONAGE_IMG_PATTERN.sub(lambda m: m.group(0).replace("/s/", "/l/").replace("/m/", "/l/"), url)
    url = _VIEW_IMG_PATTERN.sub(lambda m: m.group(0).replace("/s/", "/l/").replace("/m/", "/l/"), url)
    url = _IMG_PATH_PATTERN.sub(lambda m: m.group(0).replace("/medium/", "/large/").replace("/small/", "/large/").replace("/m/", "/l/").replace("/s/", "/l/"), url)
    return url


def _upgrade_view_img_url(url: Optional[str]) -> Optional[str]:
    return upgrade_img_url(url)


def _score_album(target_name: str, candidate_name: str, target_artist: str, candidate_artist: str) -> float:
    score = 0.0
    t_lower = target_name.lower().strip()
    c_lower = candidate_name.lower().strip()

    if t_lower == c_lower:
        score += 60
    elif t_lower in c_lower or c_lower in t_lower:
        score += 35

    if target_artist and candidate_artist:
        ta = target_artist.lower()
        ca = candidate_artist.lower()
        if ta == ca:
            score += 40
        elif ta in ca or ca in ta:
            score += 20

    return min(score, 100.0)


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _unescape(text: str) -> str:
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ").strip()


class DoubanPageParser:

    @staticmethod
    def parse_album_detail(html: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if og_image:
            result["cover_url"] = upgrade_img_url(og_image.group(1))
        else:
            cover_match = re.search(r'<img[^>]+src="(https?://img\d+\.doubanio\.com/view/subject/[^"]+)"[^>]*>', html)
            if not cover_match:
                cover_match = re.search(r'<img[^>]+src="(https?://img\d+\.doubanio\.com/img/subject/[^"]+)"[^>]*>', html)
            if cover_match:
                result["cover_url"] = upgrade_img_url(cover_match.group(1))

        hidden_match = re.search(r'<span[^>]+class="all\s+hidden"[^>]*>(.*?)</span>', html, re.DOTALL)
        if hidden_match:
            result["description"] = _unescape(_strip_tags(hidden_match.group(1))).strip()
        else:
            intro_match = re.search(r'<div[^>]+id="intro"[^>]*>(.*?)</div>', html, re.DOTALL)
            if intro_match:
                result["description"] = _unescape(_strip_tags(intro_match.group(1))).strip()
            else:
                og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
                if og_desc:
                    result["description"] = _unescape(og_desc.group(1)).strip()

        title_match = re.search(r"<title>(.*?)[\s\r\n]*[\(（]豆瓣[\)）]", html, re.DOTALL)
        if title_match:
            result["title"] = _unescape(title_match.group(1).strip())

        info_start = html.find('id="info"')
        info_section = html[info_start:info_start + 5000] if info_start >= 0 else html

        artist_link_match = re.search(r'表演者[：:]\s*<a[^>]+href="([^"]*)"[^>]*>([^<]+)</a>', info_section)
        if artist_link_match:
            result["artist_url"] = artist_link_match.group(1)
            result["artist"] = _unescape(artist_link_match.group(2)).strip()
        else:
            artist_match = re.search(r'表演者[：:]\s*([^<\n]+)', info_section)
            if artist_match:
                result["artist"] = _unescape(artist_match.group(1)).strip()

        genre_match = re.search(r'流派[：:]\s*</span>\s*(?:&nbsp;)?\s*<a[^>]*>([^<]+)</a>', info_section)
        if not genre_match:
            genre_match = re.search(r'流派[：:]\s*</span>\s*(?:&nbsp;)?\s*([^<\n]+)', info_section)
        if genre_match:
            result["genre"] = _unescape(genre_match.group(1)).strip()

        date_match = re.search(r'发行时间[：:]\s*</span>\s*(?:&nbsp;)?\s*([^<\n]+)', info_section)
        if date_match:
            date_str = _unescape(date_match.group(1)).strip()
            result["release_date"] = date_str
            year_m = re.search(r"(\d{4})", date_str)
            if year_m:
                result["release_year"] = int(year_m.group(1))

        pub_match = re.search(r'出版者[：:]\s*</span>\s*(?:&nbsp;)?\s*([^<\n]+)', info_section)
        if pub_match:
            result["publisher"] = _unescape(pub_match.group(1)).strip()

        tracklist: List[str] = []
        tracklist_section = re.search(r'<div[^>]+id="track_list"[^>]*>(.*?)</div>', html, re.DOTALL)
        if tracklist_section:
            tracks_raw = re.findall(r"<(?:li|td)[^>]*>([^<]+)", tracklist_section.group(1))
            for t in tracks_raw:
                cleaned = _unescape(t).strip()
                if cleaned and not cleaned.startswith("<!--"):
                    tracklist.append(cleaned)
        result["tracklist"] = tracklist

        rating_match = re.search(r'<strong[^>]*class="ll\s+rating_num"[^>]*>(.*?)</strong>', html, re.DOTALL)
        if rating_match:
            rating_val = rating_match.group(1).strip()
            if rating_val:
                try:
                    result["rating"] = float(rating_val)
                except (ValueError, TypeError):
                    pass

        votes_match = re.search(r'<span[^>]*property="v:votes"[^>]*>(\d+)</span>', html)
        if not votes_match:
            votes_match = re.search(r'(\d+)\s*人评价', html)
        if votes_match:
            try:
                result["rating_count"] = int(votes_match.group(1))
            except (ValueError, TypeError):
                pass

        return result

    @staticmethod
    def parse_artist_detail(html: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        name_match = re.search(r"<title>(.*?)[\s\r\n]*[\(（]豆瓣[\)）]", html, re.DOTALL)
        if name_match:
            result["name"] = _unescape(name_match.group(1).strip())

        og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if og_image:
            result["cover_url"] = upgrade_img_url(og_image.group(1))
        else:
            cover_match = re.search(r'<img[^>]+src="(https?://img\d+\.doubanio\.com/img/artist/[^"]+)"[^>]*>', html)
            if not cover_match:
                cover_match = re.search(r'<img[^>]+src="(https?://img\d+\.doubanio\.com/view/[^"]+)"[^>]*>', html)
            if cover_match:
                result["cover_url"] = upgrade_img_url(cover_match.group(1))

        hidden_match = re.search(r'<span[^>]+class="all\s+hidden"[^>]*>(.*?)</span>', html, re.DOTALL)
        if hidden_match:
            result["description"] = _unescape(_strip_tags(hidden_match.group(1))).strip()
        else:
            intro_match = re.search(r'<div[^>]+id="intro"[^>]*>(.*?)</div>', html, re.DOTALL)
            if intro_match:
                result["description"] = _unescape(_strip_tags(intro_match.group(1))).strip()
            else:
                intro_match = re.search(r'<div[^>]+class="[^"]*intro[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                if intro_match:
                    result["description"] = _unescape(_strip_tags(intro_match.group(1))).strip()
                else:
                    og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
                    if og_desc:
                        result["description"] = _unescape(og_desc.group(1)).strip()

        genres: List[str] = []
        tag_section = re.search(r'<div[^>]+class="[^"]*tags[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
        if tag_section:
            genres = [_unescape(t).strip() for t in re.findall(r"<a[^>]+>([^<]+)</a>", tag_section.group(1)) if t.strip()]
        result["genres"] = genres

        return result


class DoubanMetadataProvider(MetadataProvider):
    throttler: ThrottlerManager

    enable_artist_metadata: bool
    enable_album_metadata: bool
    enable_track_metadata: bool
    enable_images: bool

    async def handle_async_init(self) -> None:
        self.cache = self.mass.cache

        self.enable_artist_metadata = self.config.get_value(ConfigKeys.ENABLE_ARTIST_METADATA, True)
        self.enable_album_metadata = self.config.get_value(ConfigKeys.ENABLE_ALBUM_METADATA, True)
        self.enable_track_metadata = self.config.get_value(ConfigKeys.ENABLE_TRACK_METADATA, True)
        self.enable_images = self.config.get_value(ConfigKeys.ENABLE_IMAGES, True)

        self.throttler = ThrottlerManager(rate_limit=RATE_LIMIT, period=RATE_PERIOD)
        self.logger.info("[豆瓣元数据] 初始化完成（限流: %d req/%ds）", RATE_LIMIT, RATE_PERIOD)

    async def get_artist_metadata(self, artist: Artist) -> Optional[MediaItemMetadata]:
        if not self.enable_artist_metadata:
            return None

        name = clean_artist_name(artist.name)
        if not name:
            return None

        existing_description = artist.metadata.description.strip() if (artist.metadata and artist.metadata.description) else ""

        known_album_names = []
        known_track_names = []
        if hasattr(artist, "albums") and artist.albums:
            known_album_names = [a.name.lower().strip() for a in artist.albums[:10] if getattr(a, "name", None)]
        if hasattr(artist, "tracks") and artist.tracks:
            known_track_names = [t.name.lower().strip() for t in artist.tracks[:10] if getattr(t, "name", None)]

        try:
            search_results = await self._search_douban(name)
            if not search_results:
                return None

            best_artist_item = None
            for item in search_results:
                if item.get("type") != "artist":
                    continue
                item_artist = (item.get("artist") or "").lower().strip()
                if bool(item_artist) and (name.lower() in item_artist or item_artist in name.lower()):
                    best_artist_item = item
                    break

            metadata = MediaItemMetadata()
            artist_cover_url = ""
            artist_douban_url = ""
            has_artist_photo = False
            use_album_fallback = False

            if best_artist_item:
                artist_douban_url = best_artist_item.get("url", "")
                artist_cover_url = best_artist_item.get("cover_url", "")
                has_artist_photo = bool(artist_cover_url)

                if best_artist_item.get("genre"):
                    metadata.genres = {_unescape(best_artist_item["genre"])}

                summary_parts = []
                if birthday := best_artist_item.get("birthday"):
                    summary_parts.append(f"生日：{birthday}")
                if works := best_artist_item.get("representative_works"):
                    summary_parts.append(f"代表作：{'、'.join(works[:5])}")
                if fans := best_artist_item.get("fans"):
                    summary_parts.append(f"{fans} 豆瓣收藏")

                desc_parts = []
                if summary_parts:
                    desc_parts.append(" | ".join(summary_parts))
                if artist_douban_url:
                    desc_parts.append(f'<a href="{artist_douban_url}" target="_blank">查看豆瓣主页</a>')
                if existing_description:
                    desc_parts.append(existing_description)
                metadata.description = "\n".join(desc_parts)

            else:
                use_album_fallback = True
                album_candidates_scored = []

                for item in search_results:
                    if item.get("type") != "album" or not item.get("id"):
                        continue
                    item_artist = (item.get("artist") or "").lower().strip()
                    item_title = (item.get("title") or "").lower().strip()
                    item_rating = _parse_rating(item.get("rating"))
                    score = 0.0

                    if item_artist:
                        if name.lower() == item_artist:
                            score += 50
                        elif name.lower() in item_artist or item_artist in name.lower():
                            score += 35
                        else:
                            score -= 30

                    for kn in known_album_names:
                        if kn == item_title:
                            score += 40
                            break
                        elif kn in item_title or item_title in kn:
                            score += 25
                            break

                    if score < 30:
                        for tn in known_track_names:
                            if tn in item_title or item_title in tn:
                                score += 15
                                break

                    if item_rating > 0:
                        score += min(item_rating, 5)
                    album_candidates_scored.append((item, score))

                album_candidates_scored.sort(key=lambda x: x[1], reverse=True)
                if not album_candidates_scored or album_candidates_scored[0][1] <= 0:
                    return None

                best_album_item = album_candidates_scored[0][0]
                douban_url = best_album_item.get("url", "") or f"{DOUBAN_BASE_URL}/subject/{best_album_item['id']}/"
                artist_douban_url = douban_url
                artist_cover_url = best_album_item.get("cover_url", "")

                if best_album_item.get("genre"):
                    metadata.genres = {_unescape(best_album_item["genre"])}

                desc_parts = []
                item_rating = _parse_rating(best_album_item.get("rating"))
                if item_rating > 0:
                    desc_parts.append(f"{_rating_to_stars(item_rating)} {item_rating:.1f}")
                desc_parts.append(f'<a href="{douban_url}" target="_blank">查看豆瓣专辑页</a>')
                if existing_description:
                    desc_parts.append(existing_description)
                metadata.description = "\n".join(desc_parts)

            if self.enable_images:
                url_score_map: Dict[str, float] = {}
                seen_urls = set()

                if not has_artist_photo and artist_cover_url and artist_cover_url not in seen_urls:
                    url_score_map[artist_cover_url] = -1.0
                    seen_urls.add(artist_cover_url)

                for item in search_results:
                    c = item.get("cover_url")
                    if c and c not in seen_urls:
                        url_score_map[c] = _parse_rating(item.get("rating"))
                        seen_urls.add(c)

                album_candidates = sorted(url_score_map.keys(), key=lambda u: (-url_score_map[u], _cover_sort_key(u)))
                all_candidates = []
                if has_artist_photo and artist_cover_url:
                    all_candidates.append(artist_cover_url)
                all_candidates.extend(u for u in album_candidates if u not in all_candidates)

                if all_candidates:
                    metadata.images = UniqueList()
                    for c_url in all_candidates:
                        metadata.images.append(MediaItemImage(type=ImageType.THUMB, path=c_url, provider=self.instance_id, remotely_accessible=True))

            if metadata.description:
                try:
                    if isinstance(artist, ItemMapping):
                        artist = self.mass.music.artists.artist_from_item_mapping(artist)
                    if not artist.metadata:
                        artist.metadata = MediaItemMetadata()
                    artist.metadata.description = metadata.description
                    if artist.provider == "library":
                        await self.mass.music.artists.update_item_in_library(artist.item_id, artist)
                    else:
                        await self.mass.metadata.save_item_metadata(artist)
                except Exception:
                    self.logger.exception("[豆瓣元数据] 艺术家简介写入失败: %s", name)

            return metadata

        except Exception:
            self.logger.exception("[豆瓣元数据] 艺术家元数据获取失败: %s", name)
            return None

    async def get_album_metadata(self, album: Album) -> Optional[MediaItemMetadata]:
        if not self.enable_album_metadata:
            return None

        original_artists = album.artists.copy() or []
        album_name = f"{album.name} {album.version}".strip() if album.version else album.name.strip()
        artist_name = clean_artist_name(original_artists[0].name) if original_artists else ""

        try:
            search_query = f"{album_name} {artist_name}".strip()
            if search_query == album_name:
                search_results = await self._search_douban(search_query)
            else:
                combined_task = asyncio.create_task(self._search_douban(search_query))
                name_only_task = asyncio.create_task(self._search_douban(album_name))
                combined_results, name_only_results = await asyncio.gather(combined_task, name_only_task)
                search_results = combined_results or name_only_results

            if not search_results:
                album.artists = original_artists
                return None

            album_items = [r for r in search_results if r.get("type") == "album"]
            if not album_items:
                album.artists = original_artists
                return None

            scored = [(item, _score_album(album_name, item.get("title", ""), artist_name, item.get("artist", ""))) for item in album_items]
            scored.sort(key=lambda x: x[1], reverse=True)
            best_item, best_score = scored[0]

            if best_score < 20:
                album.artists = original_artists
                return None

            subject_id = best_item.get("id", "")
            detail = await self._fetch_album_detail(subject_id)
            metadata = MediaItemMetadata()

            if not detail:
                if best_item.get("year"):
                    try:
                        metadata.genres = {f"发行年份:{int(best_item['year'])}"}
                        album.year = album.year or int(best_item["year"])
                    except (ValueError, TypeError):
                        pass
                cover_url = best_item.get("cover_url")
                if self.enable_images and cover_url:
                    metadata.images = UniqueList()
                    metadata.images.append(MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.instance_id, remotely_accessible=True))
                album.artists = original_artists
                return metadata

            album_douban_url = f"{DOUBAN_BASE_URL}/subject/{subject_id}/"
            raw_desc = detail.get("description", "")
            album_rating = _parse_rating(detail.get("rating"))
            album_rating_count = detail.get("rating_count", 0)

            desc_parts = []
            if album_rating > 0:
                desc_parts.append(f"{_rating_to_stars(album_rating)} {album_rating:.1f} ({album_rating_count}人评价)")
            else:
                desc_parts.append("暂无评分，欢迎在豆瓣为专辑打分 ☆")
            desc_parts.append(f'<a href="{album_douban_url}" target="_blank">查看豆瓣专辑页</a>')
            if raw_desc:
                desc_parts.append(raw_desc)
            metadata.description = "\n".join(desc_parts)

            if detail.get("release_year") and not album.year:
                album.year = detail["release_year"]

            genres = set()
            if detail.get("genre"):
                genres.add(_unescape(detail["genre"]))
            if detail.get("publisher"):
                genres.add(_unescape(detail["publisher"]))
            if detail.get("release_year"):
                genres.add(f"发行年份:{detail['release_year']}")
            metadata.genres = genres

            cover_url = detail.get("cover_url") or best_item.get("cover_url")
            if self.enable_images and cover_url:
                metadata.images = UniqueList()
                metadata.images.append(MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.instance_id, remotely_accessible=True))

            if metadata.description:
                try:
                    if not album.metadata:
                        album.metadata = MediaItemMetadata()
                    album.metadata.description = metadata.description
                    if album.provider == "library":
                        await self.mass.music.albums.update_item_in_library(album.item_id, album)
                    else:
                        await self.mass.metadata.save_item_metadata(album)
                except Exception:
                    self.logger.exception("[豆瓣元数据] 专辑简介写入失败: %s", album_name)

            album.artists = original_artists
            return metadata

        except Exception:
            self.logger.error("[豆瓣元数据] 专辑元数据获取失败: %s", album_name)
            album.artists = original_artists
            return None

    async def get_track_metadata(self, track: Track) -> Optional[MediaItemMetadata]:
        if not self.enable_track_metadata:
            return None

        track_name = f"{track.name} {track.version}".strip() if track.version else track.name.strip()
        artist_name = clean_artist_name(track.artists[0].name) if track.artists else ""
        search_query = f"{track_name} {artist_name}".strip() or track_name

        if not search_query:
            return None

        try:
            search_results = await self._search_douban(search_query)
            if not search_results:
                return None

            best_item = search_results[0]
            subject_id = best_item.get("id", "")
            detail = await self._fetch_album_detail(subject_id) if subject_id else None
            metadata = MediaItemMetadata()

            cover_url = detail.get("cover_url") if detail else best_item.get("cover_url")
            if self.enable_images and cover_url:
                metadata.images = UniqueList()
                metadata.images.append(MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.instance_id, remotely_accessible=True))

            if detail and detail.get("genre"):
                metadata.genres = {_unescape(detail["genre"])}

            return metadata

        except Exception:
            self.logger.error("[豆瓣元数据] 曲目元数据获取失败: %s", search_query)
            return None

    async def resolve_image(self, path: str) -> str | bytes:
        if not path or "doubanio.com" not in path:
            return path

        headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": "https://music.douban.com/"}
        candidate_urls = [path]

        m = _IMG_DOMAIN_RE.match(path)
        if m:
            prefix, _, suffix, rest = m.groups()
            for n in (1, 2, 3, 5, 6, 7, 9):
                alt = f"{prefix}img{n}{suffix}{rest}"
                if alt != path:
                    candidate_urls.append(alt)

        for try_url in candidate_urls:
            try:
                async with self.mass.http_session.get(try_url, headers=headers, ssl=False, timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                    if len(data) < 2048:
                        continue
                    return data
            except Exception:
                continue

        self.logger.warning("[豆瓣元数据] 图片下载失败: %s", path)
        return path

    @use_cache(CACHE_TTL, persistent=True)
    @throttle_with_retries
    async def _fetch_json(self, url: str) -> Optional[Any]:
        headers = {
            "User-Agent": CRAWLER_USER_AGENT,
            "Accept": "application/json, text/javascript, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.douban.com/",
        }
        try:
            async with self.mass.http_session.get(url, headers=headers, ssl=True, allow_redirects=True, timeout=REQUEST_TIMEOUT) as response:
                if response.status == 429:
                    backoff = int(response.headers.get("Retry-After", 30))
                    self.logger.warning("[豆瓣元数据] 触发限流，退避 %ds", backoff)
                    raise ResourceTemporarilyUnavailable("限流", backoff_time=backoff)
                if response.status in (403, 418):
                    self.logger.warning("[豆瓣元数据] 反爬检测，退避60s")
                    raise ResourceTemporarilyUnavailable("反爬", backoff_time=60)
                if response.status >= 400:
                    return None
                return await response.json(loads=json_loads)
        except ResourceTemporarilyUnavailable:
            raise
        except Exception:
            return None

    @use_cache(CACHE_TTL, persistent=True)
    @throttle_with_retries
    async def _fetch_html(self, url: str) -> Optional[str]:
        headers = {
            "User-Agent": CRAWLER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": DOUBAN_BASE_URL,
        }
        try:
            async with self.mass.http_session.get(url, headers=headers, ssl=True, allow_redirects=True, timeout=REQUEST_TIMEOUT) as response:
                if response.status == 429:
                    backoff = int(response.headers.get("Retry-After", 30))
                    self.logger.warning("[豆瓣元数据] 触发限流，退避 %ds", backoff)
                    raise ResourceTemporarilyUnavailable("限流", backoff_time=backoff)
                if response.status in (403, 418):
                    self.logger.warning("[豆瓣元数据] 反爬检测，退避60s")
                    raise ResourceTemporarilyUnavailable("反爬", backoff_time=60)
                if response.status >= 400:
                    return None
                return await response.text(encoding="utf-8", errors="replace")
        except ResourceTemporarilyUnavailable:
            raise
        except Exception:
            return None

    async def _search_douban(self, keyword: str) -> List[Dict[str, Any]]:
        items = await self._search_douban_full(keyword)
        if len(items) < 3:
            suggest_items = await self._search_suggest(keyword)
            existing_ids = {it.get("id") for it in items}
            for si in suggest_items:
                if si.get("id") not in existing_ids:
                    items.append(si)
        return items

    async def _search_suggest(self, keyword: str) -> List[Dict[str, Any]]:
        encoded = urllib.parse.quote(keyword)
        url = f"{DOUBAN_SUGGEST_URL}?q={encoded}&cat=1003"
        data = await self._fetch_json(url)
        if not data or not isinstance(data, dict):
            return []

        items = []
        for card in data.get("cards", []):
            if card.get("type") != "music":
                continue
            entry_url = card.get("url", "")
            subject_id = SUBJECT_ID_RE.search(entry_url).group(1) if SUBJECT_ID_RE.search(entry_url) else ""
            if not subject_id:
                continue

            parts = [p.strip() for p in card.get("card_subtitle", "").split("/")]
            rating = artist = year = ""
            for part in parts:
                if "分" in part:
                    rating = part.replace("分", "").strip()
                elif re.match(r"^\d{4}$", part):
                    year = part
                elif part:
                    artist = part

            items.append({
                "id": subject_id,
                "type": "album",
                "title": card.get("title", ""),
                "cover_url": upgrade_img_url(card.get("cover_url", "")),
                "url": entry_url,
                "year": year,
                "artist": artist,
                "rating": rating,
            })
        return items

    async def _search_douban_full(self, keyword: str) -> List[Dict[str, Any]]:
        encoded = urllib.parse.quote(keyword)
        search_url = f"https://search.douban.com/music/subject_search?search_text={encoded}&cat=1003"
        html = await self._fetch_html(search_url)
        if not html:
            return []

        data_match = _SEARCH_DATA_RE.search(html)
        if not data_match:
            return []

        try:
            data = json_loads(data_match.group(1))
        except Exception:
            return []

        items = []
        for raw in data.get("items", []):
            entry_url = raw.get("url", "")
            is_artist = any(isinstance(l, dict) and l.get("text") == "艺术家" for l in raw.get("labels", []))

            if is_artist:
                musician_id = MUSICIAN_ID_RE.search(entry_url).group(1) if MUSICIAN_ID_RE.search(entry_url) else ""
                if not musician_id:
                    continue

                abstract_2 = raw.get("abstract_2", "")
                artist_genre = birthday = ""
                works = []
                if abstract_2:
                    a2_parts = [p.strip() for p in abstract_2.split("/")]
                    for part in a2_parts:
                        if re.match(r"^\d{4}年", part):
                            birthday = part
                            continue
                        if not artist_genre:
                            for sp in part.split("/"):
                                if sp in KNOWN_GENRES or sp.lower() in KNOWN_GENRES:
                                    artist_genre = _unescape(sp)
                                    break
                            if artist_genre:
                                continue
                        if part and not re.match(r"^\d+$", part):
                            works.append(_unescape(part))

                fans = ""
                fans_m = re.match(r"(\d[\d,]*)\s*人收藏", raw.get("abstract", ""))
                if fans_m:
                    fans = fans_m.group(1).replace(",", "")

                items.append({
                    "id": f"musician_{musician_id}",
                    "type": "artist",
                    "title": raw.get("title", ""),
                    "cover_url": upgrade_img_url(raw.get("cover_url", "")),
                    "url": entry_url,
                    "year": "",
                    "artist": raw.get("title", ""),
                    "rating": "",
                    "musician_id": musician_id,
                    "genre": artist_genre,
                    "birthday": birthday,
                    "representative_works": works,
                    "fans": fans,
                })

            elif raw.get("tpl_name") == "search_subject":
                subject_id = SUBJECT_ID_RE.search(entry_url).group(1) if SUBJECT_ID_RE.search(entry_url) else ""
                if not subject_id:
                    continue

                abstract = raw.get("abstract", "")
                artist = year = genre = ""
                parts = [p.strip() for p in abstract.split("/")]
                for i, part in enumerate(parts):
                    if i == 0:
                        artist = part
                    elif re.match(r"^\d{4}", part):
                        year = re.match(r"^(\d{4})", part).group(1)
                    elif part not in ("专辑", "单曲", "EP", "视频", "合辑"):
                        genre = part

                rating = str(raw.get("rating", {}).get("value", ""))
                items.append({
                    "id": subject_id,
                    "type": "album",
                    "title": raw.get("title", ""),
                    "cover_url": upgrade_img_url(raw.get("cover_url", "")),
                    "url": entry_url,
                    "year": year,
                    "artist": artist,
                    "rating": rating,
                    "genre": genre,
                })
        return items

    async def _fetch_album_detail(self, subject_id: str) -> Optional[Dict[str, Any]]:
        url = f"{DOUBAN_BASE_URL}/subject/{subject_id}/"
        html = await self._fetch_html(url)
        return DoubanPageParser.parse_album_detail(html) if html else None

    async def _fetch_artist_detail(self, artist_id: str) -> Optional[Dict[str, Any]]:
        url = f"{DOUBAN_BASE_URL}/artist/{artist_id}/"
        html = await self._fetch_html(url)
        return DoubanPageParser.parse_artist_detail(html) if html else None


async def setup(mass: "MusicAssistant", manifest: "ProviderManifest", config: "ProviderConfig") -> "ProviderInstanceType":
    return DoubanMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(mass: "MusicAssistant", instance_id: Optional[str] = None, action: Optional[str] = None, values: Optional[Dict[str, "ConfigValueType"]] = None) -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(
            key=ConfigKeys.ENABLE_ARTIST_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用艺术家元数据",
            default_value=True,
            required=False,
            description="获取艺术家封面、简介、流派",
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_ALBUM_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用专辑元数据",
            default_value=True,
            required=False,
            description="获取专辑封面、流派、年份、简介",
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_TRACK_METADATA,
            type=ConfigEntryType.BOOLEAN,
            label="启用曲目元数据",
            default_value=True,
            required=False,
            description="获取曲目封面、流派",
        ),
        ConfigEntry(
            key=ConfigKeys.ENABLE_IMAGES,
            type=ConfigEntryType.BOOLEAN,
            label="启用封面下载",
            default_value=True,
            required=False,
            description="下载高清封面图片",
        ),
    )