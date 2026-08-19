"""Временный скрипт: все посты Instagram-аккаунта в один JSON.

Запуск (из backend/): uv run collector/ig/dump_all_posts.py numberonetrans
Нужен cookies.json — сделай его через login.py:
    uv run collector/ig/login.py
Результат: data/{username}_all_posts.json
"""

import json
import sys
import time
from pathlib import Path

from scrapling.fetchers import Fetcher

API = "https://www.instagram.com/api/v1/feed/user/{user}/username/?count={count}"
APP_ID = "936619743392459"  # публичный web app id инстаграма, он статичный
COOKIES = Path("data/cookies.json")

MEDIA_TYPE = {1: "image", 2: "video", 8: "carousel"}


def best(versions):
    """Самый большой вариант из image_versions2.candidates / video_versions."""
    return max(versions, key=lambda v: v.get("width", 0) * v.get("height", 0))["url"]


def media_info(node):
    """Всё про медиа одного элемента (пост или слайд карусели)."""
    info = {"type": "video" if node.get("video_versions") else "image"}
    if node.get("video_versions"):
        v = max(node["video_versions"], key=lambda x: x.get("width", 0) * x.get("height", 0))
        info["url"] = v["url"]
        info["width"] = v.get("width")
        info["height"] = v.get("height")
        info["duration"] = node.get("video_duration")
        info["view_count"] = node.get("view_count")
    else:
        cands = node["image_versions2"]["candidates"]
        best_c = max(cands, key=lambda c: c.get("width", 0) * c.get("height", 0))
        info["url"] = best_c["url"]
        info["width"] = best_c.get("width")
        info["height"] = best_c.get("height")
        info["all_candidates"] = [c.get("url") for c in cands]
    return info


def extract_hashtags_and_mentions(caption):
    hashtags = []
    mentions = []
    if not caption:
        return hashtags, mentions
    for token in caption.split():
        if token.startswith("#"):
            hashtags.append(token[1:].rstrip(".,!?"))
        elif token.startswith("@"):
            mentions.append(token[1:].rstrip(".,!?"))
    return hashtags, mentions


def parse(item):
    children = item.get("carousel_media") or [item]
    caption = (item.get("caption") or {}).get("text", "")
    hashtags, mentions = extract_hashtags_and_mentions(caption)
    loc = item.get("location") or {}
    return {
        "shortcode": item["code"],
        "url": f"https://www.instagram.com/p/{item['code']}/",
        "pk": item.get("pk"),
        "taken_at": item["taken_at"],
        "type": MEDIA_TYPE.get(item["media_type"], item["media_type"]),
        "caption": caption,
        "hashtags": hashtags,
        "mentions": mentions,
        "likes": item.get("like_count"),
        "comments": item.get("comment_count"),
        "comments_disabled": item.get("comments_disabled"),
        "is_pinned": item.get("is_pinned", False),
        "is_verified": item.get("is_verified", False),
        "view_count": item.get("view_count"),
        "play_count": item.get("play_count"),
        "video_duration": item.get("video_duration"),
        "location": {
            "name": loc.get("name"),
            "short_name": loc.get("short_name"),
            "address": loc.get("address"),
            "city": loc.get("city"),
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
        } if loc else None,
        "usertags": [
            u.get("user", {}).get("username")
            for u in (item.get("usertags", {}) or {}).get("in", [])
        ],
        "media_count": len(children),
        "media": [media_info(c) for c in children],
    }


def fetch_all(username, count=50, pause=2.0):
    if not COOKIES.exists():
        sys.exit("Нет cookies.json — сначала запусти: uv run collector/ig/login.py")
    cookies = json.loads(COOKIES.read_text())

    all_posts = []
    seen = set()
    max_id = None
    page_no = 0

    while True:
        page_no += 1
        url = API.format(user=username, count=count)
        if max_id:
            url += f"&max_id={max_id}"

        page = Fetcher.get(
            url,
            cookies=cookies,
            impersonate="chrome",
            headers={
                "x-ig-app-id": APP_ID,
                "referer": f"https://www.instagram.com/{username}/",
            },
        )
        if page.status != 200:
            sys.exit(f"HTTP {page.status} на странице {page_no}: сессия протухла.\n{page.body[:300]}")

        data = page.json()
        items = data.get("items", [])
        if not items:
            break

        new = 0
        for item in items:
            code = item["code"]
            if code in seen:
                continue
            seen.add(code)
            all_posts.append(parse(item))
            new += 1

        print(f"стр. {page_no}: +{new} постов (всего {len(all_posts)})")
        next_id = data.get("next_max_id")
        if not next_id or not data.get("more_available"):
            break
        max_id = next_id
        time.sleep(pause)  # не спамить, чтобы не поймать rate limit

    return all_posts


if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else "numberonetrans"
    posts = fetch_all(username)
    out = Path(f"data/{username}_all_posts.json")
    out.write_text(json.dumps(posts, ensure_ascii=False, indent=2))
    print(f"\nИтого {len(posts)} постов -> {out}")