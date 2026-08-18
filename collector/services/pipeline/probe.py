"""Пробы: посмотреть источник глазами. Одна операция на каждую пробу, fn(ctx)."""

import hashlib
import json
import os
import tomllib
from pathlib import Path

from services import sources
from services.fetch import HttpError, get
from services.sources import parse_firm_card
from services.sources import parse_initial_state as state
from services.sources import parse_org_list as orgs
from services.sources import parse_search_meta as meta

CONFIG = Path("config.toml")

COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum
LIST_URL = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
FIRM_URL = "https://2gis.kz/{city}/firm/{branch_id}"
RUBRICS_ROOT = "https://2gis.kz/almaty/rubrics"
RUBRICS_BRANCH = "https://2gis.kz/almaty/rubrics/subrubrics/{id}"
SERP_API = "https://google.serper.dev/search"


def gis_list(ctx):
    """Пробная рубрика: первая из include на первом городе. Ответ — dict."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    city = config["cities"][0]
    rubric = config["rubrics"]["include"][0]
    ctx.log(f"рубрика {rubric} / {city}")
    rows = collect_list(city, rubric, ctx)
    return {"rubric": rubric, "city": city, "rows": rows}


def gis_firm(ctx):
    """Контакты первой карточки из пробной рубрики. Ответ — dict."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    city = config["cities"][0]
    rubric = config["rubrics"]["include"][0]
    s = state(get(LIST_URL.format(city=city, rubric=rubric, page=1), cookies=COOKIE))
    rows = orgs(s, city, rubric)
    if not rows:
        return {"city": city, "rubric": rubric, "row": None}
    first = rows[0]
    html = get(FIRM_URL.format(city=city, branch_id=first["branch_id"]), cookies=COOKIE)
    contacts = parse_firm_card(state(html), first["branch_id"])
    return {
        "city": city, "rubric": rubric,
        "branch_id": first["branch_id"], "name": first["name"],
        "contacts": contacts,
    }


def gis_rubrics(ctx):
    """Ветка B2B-услуг (корень 110609) из рубрикатора. Ответ — dict."""
    rows = walk(["110609"], ctx)
    return {"branch": "110609", "rows": rows}


def serp(ctx):
    """Пробный запрос Serper: первый ЛПР из базы. Ключ из .env, как и всегда."""
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError(
            "SERPER_API_KEY пуст. Поднять бэкенд: "
            "uv run --env-file .env uvicorn api:app --port 8787"
        )
    query = default_query()
    ctx.log(f"запрос: {query}")
    rows = parse_serp(search(query, key), query)
    return {"query": query, "rows": rows}


def default_query():
    """Первый ЛПР базы — «директор <компания>». Никакой магии вне config."""
    from services import store as engine
    db = engine.connect()
    try:
        row = db.execute(
            "SELECT name FROM companies c JOIN scores s USING (company_id)"
            " WHERE s.intent_score > 0 ORDER BY s.intent_score DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    name = row[0] if row else "Ромашка"
    return f"директор {name}"


# --- списки организаций -----------------------------------------------------


def collect_list(city, rubric, ctx):
    """Собрать рубрику до потолка пагинации (то же, что gis_list.collect)."""
    s = state(get(LIST_URL.format(city=city, rubric=rubric, page=1), cookies=COOKIE))
    total, pages, _ = meta(s)
    ctx.log(f"  {total} организаций, {pages} страниц по версии 2GIS")

    rows = orgs(s, city, rubric)
    for n in range(2, pages + 1):
        html = get(LIST_URL.format(city=city, rubric=rubric, page=n), cookies=COOKIE)
        s = state(html)
        _, _, current = meta(s)
        if current != n:
            ctx.log(f"  стр. {n}: 2GIS вернул страницу {current} — потолок пагинации, стоп")
            break
        rows += orgs(s, city, rubric)
    return rows


# --- рубрикатор -------------------------------------------------------------


def named(rubricator, ids):
    """Пары (id, название) для перечисленных id. Длинные id — геообъекты, не рубрики."""
    out = []
    for i in ids:
        if i.isdigit() and len(i) < 10:
            v = rubricator["items"].get(i) or {}
            out.append((i, v.get("name") or v.get("caption")))
    return out


def roots():
    r = state(get(RUBRICS_ROOT, cookies=COOKIE))["data"]["rubricator"]
    return named(r, next(iter(r["lists"].values()))["data"])  # на корне список один


def branch(rubric_id):
    """Своё название и дети рубрики. Дети — только в lists['67_<id>_ru_KZ']."""
    try:
        html = get(RUBRICS_BRANCH.format(id=rubric_id), cookies=COOKIE)
    except HttpError as e:
        if e.status == 404:
            return None, []  # у листа нет страницы подрубрик
        raise
    r = state(html)["data"]["rubricator"]
    own = (r["items"].get(rubric_id) or {}).get("name")
    kids = r["lists"].get(f"67_{rubric_id}_ru_KZ")
    return own, (named(r, kids["data"]) if kids else [])


def walk(start, ctx):
    """Обход вширь от корней или от заданных рубрик."""
    queue = [(i, None, None) for i in start] if start else \
            [(i, name, None) for i, name in roots()]
    rows, seen = [], set()
    while queue:
        ctx.check_cancelled()
        rid, name, parent = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        own, kids = branch(rid)
        rows.append({"id": rid, "name": name or own, "parent_id": parent})
        queue += [(k, kname, rid) for k, kname in kids]
    return rows


# --- serper -----------------------------------------------------------------


def search(query, key, country="kz", lang="ru"):
    """Ответ Serper в слой сырья — повторный запрос денег не стоит."""
    import json as _json

    from scrapling.fetchers import Fetcher

    from services import storage

    storage.RAW.mkdir(exist_ok=True)
    f = storage.RAW / (hashlib.sha1(f"serp:{query}:{country}:{lang}".encode()).hexdigest() + ".serp.json")
    if f.exists():
        return _json.loads(f.read_text(encoding="utf-8"))

    page = Fetcher.post(
        SERP_API,
        json={"q": query, "gl": country, "hl": lang},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    if page.status != 200:
        raise RuntimeError(f"Serper HTTP {page.status}: {str(page.html_content)[:200]}")
    data = page.json()
    f.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def parse_serp(data, query):
    return sources.parse_serper(data, query)