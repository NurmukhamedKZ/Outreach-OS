"""Разбор источников: чистые функции от сырья к строкам.

Тела перенесены из рабочих скриптов дословно. Разбор проверен на живых данных
августа 2026 и не улучшается: каждая его строка написана против конкретной
особенности источника.

Здесь нет ни сети, ни диска, ни print. Это условие того, что build.py остаётся
чистой функцией от raw/ к leads.db, а рабочие скрипты и build.py разбирают одну
и ту же страницу одинаково — второй копии разбора в проекте не существует.
"""

import json
import re
from datetime import datetime, timezone

INITIAL_STATE = re.compile(r"var initialState = JSON\.parse\('(.*?)'\);", re.S)
JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
VACANCY_ID = re.compile(r"/vacancy/(\d{6,})")

CONTACT_KINDS = ("phone", "website", "email", "instagram", "whatsapp")
FIRM_URL = "https://2gis.kz/{city}/firm/{branch_id}"

IG_POST_URL = "https://www.instagram.com/p/{shortcode}/"
IG_MEDIA_TYPE = {1: "image", 2: "video", 8: "carousel"}


def parse_initial_state(html):
    """initialState — JS-строка с JSON внутри. Снимаем только JS-экранирование."""
    raw = INITIAL_STATE.search(html).group(1)
    return json.loads(re.sub(r"\\(['\\])", r"\1", raw))


def parse_search_meta(state):
    """total, pages и фактическая страница из ветки поиска.

    currentPage — единственный честный признак потолка пагинации: начиная с
    шестой страницы 2GIS отдаёт HTTP 200 с содержимым первой, а total и pages
    продолжают обещать больше.
    """
    profile = state["data"]["search"]["profile"]
    data = profile[next(iter(profile))]["data"]
    return data["total"], data["pages"], data.get("currentPage")


def parse_org_list(state, city, rubric_id):
    """Карточки организаций из ветки entity. Записи без org — не организации."""
    rows = []
    for branch_id, node in state["data"]["entity"]["profile"].items():
        data = node.get("data") or {}
        org = data.get("org")
        if not org:
            continue
        reviews = data.get("reviews") or {}
        rows.append({
            "branch_id": branch_id,
            "org_id": org.get("id"),
            "name": data.get("name"),
            "org_name": org.get("name"),
            "branch_count": org.get("branch_count"),
            "rubrics": [r.get("name") for r in data.get("rubrics") or []],
            "address": data.get("address_name"),
            "point": data.get("point"),
            "review_count": reviews.get("general_review_count"),
            "rating": reviews.get("general_rating"),
            "city": city,
            "rubric_id": str(rubric_id),
        })
    return rows


def parse_firm_card(state, branch_id):
    """Контакты карточки филиала — по строке на канал.

    Плоские строки вместо словаря с разнотипными полями (phones список, website
    строка, instagram строка): правило «есть телефон → лид готов» не должно
    разбирать пять разных форм одного и того же.
    """
    data = state["data"]["entity"]["profile"][branch_id]["data"]
    source_url = FIRM_URL.format(city=data.get("city_alias"), branch_id=branch_id)

    rows, seen = [], set()
    for group in data.get("contact_groups") or []:
        for contact in group.get("contacts") or []:
            kind = contact.get("type")
            if kind not in CONTACT_KINDS:
                continue
            handle = contact.get("value") or contact.get("url") or contact.get("text")
            if kind == "website":
                handle = unwrap_2gis_link(handle)
            if not handle or (kind, handle) in seen:
                continue
            seen.add((kind, handle))
            rows.append({
                "branch_id": branch_id,
                "kind": kind,
                "handle": handle,
                "source_url": source_url,
            })
    return rows


def unwrap_2gis_link(url):
    """2GIS заворачивает сайт в редирект link.2gis.ru — настоящий URL в хвосте после '?'."""
    if url and "link.2gis." in url and "?" in url:
        return url.split("?", 1)[1]
    return url


def parse_vacancy_ids(html):
    """id вакансий со страницы списка hh."""
    return sorted(set(VACANCY_ID.findall(html)))


def parse_job_posting(html):
    """JSON-LD JobPosting — hh отдаёт вакансию структурно, парсить HTML не нужно."""
    for block in JSON_LD.findall(html):
        try:
            posting = json.loads(block)
        except json.JSONDecodeError:
            continue
        if posting.get("@type") == "JobPosting":
            return posting
    return None


def parse_ig_feed(body):
    """Аккаунт и его посты из ответа feed/user инстаграма.

    Разбирается именно лента, а не профиль: web_profile_info отвечает 400 на
    половине аккаунтов и с 2026 года отдаёт edge_owner_to_timeline_media пустым,
    то есть постов там больше нет вовсе. Лента ответила на 18 запросах из 18.
    """
    data = json.loads(json_body(body))
    user = data.get("user") or {}
    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "is_private": bool(user.get("is_private")),
        "posts": [parse_ig_post(item) for item in data.get("items") or []],
    }


def parse_ig_post(item):
    """Пост как событие с датой — материал для сигналов Ф6.

    Ссылок на медиа здесь нет намеренно: ни один сигнал не смотрит на картинку,
    а хранить в базе протухающие за сутки CDN-адреса незачем.
    """
    return {
        "shortcode": item["code"],
        "url": IG_POST_URL.format(shortcode=item["code"]),
        "taken_at": iso_utc(item["taken_at"]),
        "type": IG_MEDIA_TYPE.get(item.get("media_type"), str(item.get("media_type"))),
        "caption": ((item.get("caption") or {}).get("text") or "").strip(),
        "likes": item.get("like_count"),
        "comments": item.get("comment_count"),
    }


def ig_username(handle):
    """Логин аккаунта из ссылки, как её отдаёт 2GIS: https://instagram.com/<логин>."""
    return handle.rstrip("/").rsplit("/", 1)[-1]


def json_body(body):
    """JSON из ответа, который fetch.get завернул в <html><body>.

    Scrapling разбирает как разметку любой ответ, включая ответ API. Тело при
    этом остаётся дословным — проверено сравнением с сырыми байтами, — поэтому
    достаточно вырезать сам JSON, а не заводить второй путь забора ради него.
    """
    return body[body.index("{"): body.rindex("}") + 1]


def iso_utc(taken_at):
    """Unix-время инстаграма -> ISO UTC, в котором даты хранят остальные таблицы.

    Без этого observed_at не сравнить с fetched_at, и затухание в score.py
    молча считало бы любой пост сегодняшним.
    """
    return datetime.fromtimestamp(taken_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_serper(data, query):
    return [
        {
            "query": query,
            "url": r.get("link"),
            "title": r.get("title"),
            "snippet": r.get("snippet"),
            "position": r.get("position"),
        }
        for r in data.get("organic") or []
    ]
