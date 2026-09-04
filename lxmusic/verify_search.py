"""验证脚本：直接对 lxserver 调 songList/search，确认返回结构与本插件解析一致。

用法：
    python3 verify_search.py [server_url] [username] [password] [keyword]

不传参时用代码里的默认值（http://localhost:9527, admin, 空密码）。
"""
from __future__ import annotations

import asyncio
import sys

import aiohttp

SERVER = "http://localhost:9527"
USERNAME = "admin"
PASSWORD = ""
KEYWORD = "抖音神曲"
SOURCES = ["kw", "kg", "tx", "wy", "mg"]


def _normalize_list(result):
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


async def main() -> None:
    server = (sys.argv[1] if len(sys.argv) > 1 else SERVER).rstrip("/")
    username = sys.argv[2] if len(sys.argv) > 2 else USERNAME
    password = sys.argv[3] if len(sys.argv) > 3 else PASSWORD
    keyword = sys.argv[4] if len(sys.argv) > 4 else KEYWORD

    print(f"==> 服务端: {server}")
    print(f"==> 账号:   {username}")
    print(f"==> 关键词: {keyword}")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        # 1) 登录
        token = None
        try:
            async with session.post(
                f"{server}/api/user/login",
                json={"username": username, "password": password},
            ) as resp:
                body = await resp.json()
                print(f"[login] HTTP {resp.status} -> {body}")
                if isinstance(body, dict):
                    token = body.get("token") or body.get("data", {}).get("token")
        except Exception as err:  # noqa: BLE001
            print(f"[login] 连接失败: {err}")
            return

        if not token:
            print("[login] 未获取到 token，无法继续。请确认密码。")
            return
        print(f"[login] 成功，token 前缀: {token[:12]}...")

        headers = {"x-user-token": token}

        # 2) 逐音源搜索歌单
        total = 0
        for source in SOURCES:
            try:
                async with session.get(
                    f"{server}/api/music/songList/search",
                    params={"source": source, "text": keyword, "page": 1, "limit": 10},
                    headers=headers,
                ) as resp:
                    raw = await resp.json()
                    items = _normalize_list(raw)
                    print(f"\n[search:{source}] HTTP {resp.status} 命中 {len(items)} 条")
                    for sl in items[:3]:
                        sl_id = sl.get("id") or sl.get("listId") or sl.get("playId")
                        sl_name = sl.get("name") or sl.get("listName") or sl_id
                        sl_img = (
                            sl.get("img") or sl.get("pic") or sl.get("image")
                            or sl.get("cover") or sl.get("coverImgUrl")
                        )
                        print(f"   - id={sl_id} name={sl_name!r} img={'有' if sl_img else '无'}")
                        total += 1
            except Exception as err:  # noqa: BLE001
                print(f"[search:{source}] 失败: {err}")

        # 3) 取第一个命中歌单验证 detail 接口
        if total == 0:
            print("\n[detail] 无命中歌单，跳过 detail 验证。")
            return


if __name__ == "__main__":
    asyncio.run(main())
