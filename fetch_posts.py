"""Забирает последние 10 постов аккаунта Instagram в JSON со ссылками на медиа.

Запуск: uv run fetch_posts.py viresta.kz [count]
Нужен cookies.json — сделай его через login.py.
"""

import json
import sys
from pathlib import Path

from scrapling.fetchers import Fetcher

API = "https://www.instagram.com/api/v1/feed/user/{user}/username/?count={count}"
APP_ID = "936619743392459"  # публичный web app id инстаграма, он статичный
COOKIES = Path("cookies.json")

MEDIA_TYPE = {1: "image", 2: "video", 8: "carousel"}


def best(versions):
    """Самый большой вариант из image_versions2.candidates / video_versions."""
    return max(versions, key=lambda v: v.get("width", 0) * v.get("height", 0))["url"]


def media_urls(node):
    """Ссылки на медиа одного элемента (пост или слайд карусели)."""
    if node.get("video_versions"):
        return [{"type": "video", "url": best(node["video_versions"])}]
    return [{"type": "image", "url": best(node["image_versions2"]["candidates"])}]


def parse(item):
    children = item.get("carousel_media") or [item]
    return {
        "shortcode": item["code"],
        "url": f"https://www.instagram.com/p/{item['code']}/",
        "taken_at": item["taken_at"],
        "type": MEDIA_TYPE.get(item["media_type"], item["media_type"]),
        "caption": (item.get("caption") or {}).get("text", ""),
        "likes": item.get("like_count"),
        "comments": item.get("comment_count"),
        "media": [m for child in children for m in media_urls(child)],
    }


def fetch(username, count=10):
    if not COOKIES.exists():
        sys.exit("Нет cookies.json — сначала запусти: uv run login.py")
    cookies = json.loads(COOKIES.read_text())

    page = Fetcher.get(
        API.format(user=username, count=count),
        cookies=cookies,
        impersonate="chrome",
        headers={
            "x-ig-app-id": APP_ID,
            "referer": f"https://www.instagram.com/{username}/",
        },
    )
    if page.status != 200:
        sys.exit(f"HTTP {page.status}: сессия протухла или аккаунт закрыт.\n{page.body[:300]}")

    items = page.json().get("items", [])
    if not items:
        sys.exit(f"Постов не вернулось. Ответ: {page.body[:300]}")
    return [parse(i) for i in items[:count]]


def demo():
    """Проверка разбора: карусель фото+видео и одиночное видео."""
    carousel = {
        "code": "ABC", "taken_at": 1, "media_type": 8, "like_count": 5, "comment_count": 2,
        "caption": {"text": "hi"},
        "carousel_media": [
            {"image_versions2": {"candidates": [
                {"url": "small.jpg", "width": 100, "height": 100},
                {"url": "big.jpg", "width": 1080, "height": 1080},
            ]}},
            {"video_versions": [{"url": "v.mp4", "width": 720, "height": 1280}],
             "image_versions2": {"candidates": [{"url": "thumb.jpg", "width": 100, "height": 100}]}},
        ],
    }
    got = parse(carousel)
    assert got["media"] == [
        {"type": "image", "url": "big.jpg"},
        {"type": "video", "url": "v.mp4"},
    ], got["media"]
    assert got["url"] == "https://www.instagram.com/p/ABC/"
    assert got["type"] == "carousel"

    single = {
        "code": "X", "taken_at": 2, "media_type": 2, "caption": None,
        "video_versions": [{"url": "one.mp4", "width": 720, "height": 1280}],
        "image_versions2": {"candidates": [{"url": "t.jpg", "width": 10, "height": 10}]},
    }
    assert parse(single)["media"] == [{"type": "video", "url": "one.mp4"}]
    assert parse(single)["caption"] == ""
    print("demo ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        username = sys.argv[1] if len(sys.argv) > 1 else "viresta.kz"
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        posts = fetch(username, count)
        out = Path(f"{username}_posts.json")
        out.write_text(json.dumps(posts, ensure_ascii=False, indent=2))
        print(f"{len(posts)} постов -> {out}")
