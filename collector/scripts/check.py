"""Проверки. Ассерты, а не фреймворк; сеть не нужна ни одной из них.

Запуск: uv run -m scripts.check [раздел]      без аргумента — все разделы
Разделы: parsers (разбор на эталонных страницах), raw (целостность сырья),
build (приёмка Ф3), collect (приёмка Ф4).

Каждая следующая фаза дописывает сюда свой раздел. Отдельной фазы «написать
тесты» в плане нет: проверка — часть фазы, а не работа после неё.

Разделу parsers не нужны ни база, ни raw/ — он идёт первым и один запускается
на чистом клоне. Остальные требуют собранных данных.
"""

import csv
import gzip
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

from services import enrich
from services import sources

RAW = Path("data/raw")
FIXTURES = Path("fixtures")
DB = Path("db/leads.db")
OUT = Path("data/leads.csv")
CYRILLIC = re.compile(r"[А-Яа-я]")
RUBRIC_URL = re.compile(r"/rubric/\d+(?:/page/(\d+))?$")
SIDECAR_FIELDS = ("url", "final_url", "status", "fetched_at")


# Эталонные страницы для раздела parsers: по одной на каждый разбор в
# services/sources.py. Сняты из raw/ 16.08.2026 и, в отличие от самого raw/,
# лежат в git — это единственная проверка разбора, которая работает на чистом
# клоне и не зависит от того, что 2GIS и hh отдают сегодня.
#
# Числа ниже — свойства именно этих четырёх файлов, а не «примерно столько».
# Страница в git не меняется, поэтому расхождение означает, что поехал разбор, а
# не что источник поменял вёрстку. Правятся только вместе с фикстурой.
GIS_RUBRIC_CITY, GIS_RUBRIC_ID = "almaty", "653"
GIS_FIRM_BRANCH = "70000001017502602"


def check_parsers():
    """Разбор источников на эталонных страницах: ни базы, ни сети, ни raw/.

    Остальные разделы проверяют данные и ловят поломку разбора задним числом —
    нулями в отчёте. Этот ловит её сразу и называет сломавшийся парсер.
    """
    check_gis_rubric_parsing()
    check_gis_firm_parsing()
    check_hh_parsing()
    check_link_unwrapping()
    check_ig_parsing()


def check_gis_rubric_parsing():
    """Страница рубрики: счётчики пагинации и карточки организаций."""
    state = sources.parse_initial_state(fixture_html("gis_rubric"))
    total, pages, current = sources.parse_search_meta(state)
    assert (total, pages, current) == (670, 56, 1), \
        f"счётчики поиска {(total, pages, current)}, у эталона (670, 56, 1)"

    orgs = sources.parse_org_list(state, GIS_RUBRIC_CITY, GIS_RUBRIC_ID)
    assert len(orgs) == 12, f"организаций {len(orgs)}, у эталона 12"

    first = orgs[0]
    assert first["branch_id"] == "70000001037962810", first["branch_id"]
    assert first["org_id"] == "70000001037962809", first["org_id"]
    assert first["name"] == "Ваша Бухгалтерия, бухгалтерская компания", first["name"]
    assert first["address"] == "проспект Серкебаева, 31", first["address"]
    assert first["city"] == GIS_RUBRIC_CITY and first["rubric_id"] == GIS_RUBRIC_ID

    assert all(o["branch_id"] and o["name"] for o in orgs), "организация без id или названия"
    assert all(CYRILLIC.search(o["name"]) for o in orgs), \
        "кириллица побита разбором JS-строки initialState"

    check_non_orgs_are_skipped()
    print(f"  2gis рубрика: {len(orgs)} организаций, пагинация {total}/{pages}")


def check_non_orgs_are_skipped():
    """Записи без org — не организации, и в orgs им не место.

    Проверяется на синтетическом состоянии, а не на фикстуре: в эталонной
    рубрике все двенадцать записей оказались организациями, и живой страницы,
    доказывающей отсев, у нас нет. Ветка от этого не перестаёт быть нужной —
    2GIS кладёт в ту же ветку остановки и рекламные блоки.
    """
    state = {"data": {"entity": {"profile": {
        "70000001037962810": {"data": {"name": "Компания", "org": {"id": "1"}}},
        "stop_1": {"data": {"name": "Остановка «Абая»"}},
        "empty_1": {},
    }}}}
    rows = sources.parse_org_list(state, GIS_RUBRIC_CITY, GIS_RUBRIC_ID)
    assert [r["branch_id"] for r in rows] == ["70000001037962810"], \
        f"в организации попало лишнее: {[r['branch_id'] for r in rows]}"


def check_gis_firm_parsing():
    """Карточка филиала: каналы связи, по строке на канал."""
    state = sources.parse_initial_state(fixture_html("gis_firm"))
    contacts = sources.parse_firm_card(state, GIS_FIRM_BRANCH)
    by_kind = {}
    for contact in contacts:
        by_kind.setdefault(contact["kind"], set()).add(contact["handle"])

    assert by_kind["phone"] == {"+77272960782", "+77750009131"}, by_kind.get("phone")
    assert by_kind["website"] == {"http://studionomad.kz"}, by_kind.get("website")
    assert by_kind["whatsapp"], "whatsapp у эталонной карточки потерян"

    assert all(c["branch_id"] == GIS_FIRM_BRANCH for c in contacts), "чужой branch_id"
    assert all(c["source_url"].endswith(GIS_FIRM_BRANCH) for c in contacts), \
        "source_url не ведёт на разобранную карточку"
    assert len(contacts) == len({(c["kind"], c["handle"]) for c in contacts}), \
        "канал задвоился: contact_groups перечисляет один и тот же номер дважды"
    print(f"  2gis карточка: {len(contacts)} каналов, типы {sorted(by_kind)}")


def check_hh_parsing():
    """hh: id со страницы списка и JobPosting со страницы вакансии."""
    ids = sources.parse_vacancy_ids(fixture_html("hh_list"))
    assert len(ids) == 50, f"id вакансий {len(ids)}, у эталона 50 — страница списка hh"
    assert all(i.isdigit() and len(i) >= 6 for i in ids), "в id вакансий попал мусор"

    posting = sources.parse_job_posting(fixture_html("hh_vacancy"))
    assert posting, "JobPosting не найден — разбор JSON-LD сломан"
    assert posting["title"] == "Менеджер по продажам, менеджер по работе с клиентами", \
        posting["title"]
    assert posting["hiringOrganization"]["name"] == "Atrium", posting["hiringOrganization"]
    assert posting["datePosted"].startswith("2026-07-31"), posting["datePosted"]

    # Текст вакансии — главный intent-сигнал проекта: по нему ищутся vacancy_sales
    # и цитата для why_now. Обрезанное описание не уронит ни один запрос, просто
    # тихо перестанет давать сигналы.
    description = posting["description"]
    assert len(description) > 2000, f"описание {len(description)} символов — обрезано"
    assert CYRILLIC.search(description), "кириллица побита в тексте вакансии"
    print(f"  hh: {len(ids)} id со списка, вакансия «{posting['title'][:30]}…»")


def check_link_unwrapping():
    """2GIS заворачивает сайт в редирект: в contacts обязан лечь адрес компании."""
    wrapped = "https://link.2gis.ru/go?https://studionomad.kz"
    assert sources.unwrap_2gis_link(wrapped) == "https://studionomad.kz", "редирект не развёрнут"
    assert sources.unwrap_2gis_link("https://studionomad.kz") == "https://studionomad.kz", \
        "прямой адрес испорчен разворачиванием"
    assert sources.unwrap_2gis_link(None) is None


def check_ig_parsing():
    """Лента инстаграма: посты как события с датой.

    Эталон — ответ feed/user аккаунта adalservice__ от 16.08.2026: двенадцать
    постов, среди них карусель, видео и фото.
    """
    feed = sources.parse_ig_feed(fixture_html("ig_feed"))
    assert feed["username"] == "adalservice__", feed["username"]
    assert feed["is_private"] is False, "эталонный аккаунт открытый"

    posts = feed["posts"]
    assert len(posts) == 12, f"постов {len(posts)}, у эталона 12"
    assert sorted({p["type"] for p in posts}) == ["carousel", "image", "video"], \
        f"типы постов {sorted({p['type'] for p in posts})} — у эталона все три"

    # Дата обязана быть сравнимой с fetched_at: на ней держится затухание в
    # score.py, а unix-время молча считало бы любой пост сегодняшним.
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", p["taken_at"])
               for p in posts), "дата поста не в ISO UTC"
    assert all(p["url"].startswith("https://www.instagram.com/p/") for p in posts), \
        "ссылка на пост не собрана — сигналу нечем обосноваться"
    assert any(CYRILLIC.search(p["caption"]) for p in posts), \
        "кириллица побита разбором JSON внутри <html><body>"

    check_ig_empty_caption_is_not_none()
    check_ig_quote_binding(posts)
    newest = max(p["taken_at"] for p in posts)
    print(f"  instagram: {len(posts)} постов, последний {newest[:10]}")


def check_ig_quote_binding(posts):
    """Находка модели привязывается к посту по цитате, а не по её номеру.

    Номер модель иногда сдвигает — в живом прогоне пришёл 0 при нумерации с
    единицы. Цитата же обязана быть дословной, и её отсутствие в подписи значит,
    что модель фразу испортила: такой находке в signals не место.
    """
    real = next(p for p in posts if len(p["caption"]) > 40)
    fragment = real["caption"][10:40]
    bound = enrich.post_with_quote(posts, fragment)
    assert bound and fragment in bound["caption"], "цитата не нашла свой пост"
    assert enrich.post_with_quote(posts, "такой фразы в ленте нет") is None, \
        "выдуманная цитата привязалась к посту — проверка дословности не работает"
    assert enrich.post_with_quote(posts, "") is None, "пустая цитата привязалась к посту"


def check_ig_empty_caption_is_not_none():
    """Пост без подписи даёт пустую строку, а не None.

    Проверяется синтетически: у эталонного аккаунта подписаны все двенадцать
    постов. Ветка от этого не перестаёт быть нужной — инстаграм кладёт в caption
    именно null, и регулярка Ф6 упала бы на нём вместо того, чтобы не найти
    ничего.
    """
    post = sources.parse_ig_post(
        {"code": "ABC", "taken_at": 1786867200, "media_type": 1, "caption": None}
    )
    assert post["caption"] == "", repr(post["caption"])
    assert post["taken_at"] == "2026-08-16T08:00:00Z", post["taken_at"]
    assert post["url"] == "https://www.instagram.com/p/ABC/", post["url"]
    assert sources.ig_username("https://instagram.com/adalservice__/") == "adalservice__"


def fixture_html(name):
    with gzip.open(FIXTURES / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def check_raw():
    """Сырьё цело: у каждой страницы сайдкар, у каждого сайдкара страница."""
    pages = sorted(RAW.glob("*.html.gz"))
    # Сайдкар страницы — `<sha1>.json`. Всё с составным суффиксом (`.serp.json`,
    # `.llm.json`) — кэш другого рода, лежащий в той же папке по тому же правилу.
    sidecars = [f for f in RAW.glob("*.json") if f.name.count(".") == 1]
    assert pages, "raw/ пуст — сырьё потеряно, восстановить нельзя"
    assert len(pages) == len(sidecars), (
        f"страниц {len(pages)}, сайдкаров {len(sidecars)} — "
        "страница без сайдкара слепа к подмене, сайдкар без страницы бесполезен"
    )

    for page in pages:
        sidecar = RAW / f"{page.name.removesuffix('.html.gz')}.json"
        assert sidecar.exists(), f"{page.name} без сайдкара"
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        missing = [f for f in SIDECAR_FIELDS if not meta.get(f)]
        assert not missing, f"{sidecar.name}: пустые поля {missing}"
        assert meta["status"] == 200, f"{sidecar.name}: статус {meta['status']}"
        assert meta["fetched_at"].startswith("20"), meta["fetched_at"]
        # Размер проверяется только у источников: страница 2GIS или hh короче
        # килобайта означает обрыв закачки. Сайт компании — чужая территория, там
        # заглушка припаркованного домена и SPA на одном скрипте это норма, а не
        # порча сырья.
        if "2gis.kz" in meta["url"] or "hh.kz" in meta["url"]:
            with gzip.open(page, "rt", encoding="utf-8") as fh:
                assert len(fh.read(2000)) > 1000, f"{page.name}: страница источника пуста"

    print(f"raw ok — {len(pages)} страниц, у каждой сайдкар с конечным адресом")


def check_build():
    """Приёмка Ф3: содержимое leads.db и воспроизводимость сборки."""
    assert DB.exists(), "leads.db нет — сначала uv run build.py"
    db = sqlite3.connect(DB)

    check_orgs(db)
    check_vacancies(db)
    check_contacts(db)
    check_fetches(db)
    check_profiles(db)
    db.close()
    check_rebuild_is_identical()


def check_orgs(db):
    orgs = count(db, "orgs")
    assert orgs >= 60, f"организаций {orgs}, в сырье их не меньше 60"

    names = [n for (n,) in db.execute("SELECT name FROM orgs")]
    assert all(names), "организация без названия"
    assert sum(bool(CYRILLIC.search(n)) for n in names) > len(names) / 2, \
        "кириллица побита разбором JS-строки initialState"

    per_rubric_city = db.execute(
        "SELECT max(c) FROM (SELECT count(*) c FROM orgs GROUP BY city, rubric_id)"
    ).fetchone()[0]
    assert per_rubric_city <= 60, (
        f"{per_rubric_city} организаций в одной рубрике города — "
        "потолок 2GIS 60, значит в базу попала подменённая страница"
    )
    print(f"  orgs {orgs}, до {per_rubric_city} на рубрику города")


def check_vacancies(db):
    vacancies = count(db, "vacancies")
    assert vacancies >= 100, f"вакансий {vacancies}, в сырье их не меньше 100"

    rows = db.execute("SELECT id, text, employer, city, slug FROM vacancies").fetchall()
    short = [r[0] for r in rows if len(r[1] or "") <= 200]
    assert not short, f"вакансии с обрезанным текстом: {short[:5]}"
    assert all(r[2] for r in rows), "вакансия без работодателя — по нему клеится компания"
    assert all(r[3] and r[4] for r in rows), \
        "вакансия без города и slug'а — страница списка не сопоставилась"
    assert sum(bool(CYRILLIC.search(r[1])) for r in rows) > len(rows) / 2, \
        "кириллица побита в текстах вакансий"

    # Подменённая страница даёт вакансии всего города вперемешку. Признак: slug,
    # которого нет в адресе, куда hh на самом деле привёл.
    for slug, in db.execute("SELECT DISTINCT slug FROM vacancies"):
        landed = db.execute(
            "SELECT final_url FROM fetches WHERE url LIKE ?", (f"%/vacancies/{slug}",)
        ).fetchone()
        assert landed and slug in landed[0], f"вакансии приписаны подменённому slug'у {slug}"
    print(f"  vacancies {vacancies}, тексты целы")


def check_contacts(db):
    cards = db.execute("SELECT count(DISTINCT branch_id) FROM contacts").fetchone()[0]
    firm_pages = db.execute(
        "SELECT count(*) FROM fetches WHERE url LIKE '%2gis.kz/%/firm/%'"
    ).fetchone()[0]
    # Карточка без единого контакта — законный случай: 2GIS позволяет филиалу не
    # указывать ни телефона, ни сайта. На 1866 карточках таких 13. Инвариант не в
    # равенстве, а в том, что молчащих карточек единицы: массовый ноль означал бы,
    # что сломался разбор, а не что источник поскупился.
    silent = firm_pages - cards
    assert silent <= 0.02 * firm_pages, (
        f"карточек в сырье {firm_pages}, с контактами {cards} — "
        f"{silent} молчащих, это больше 2 %: похоже на поломку разбора"
    )

    # Порог «у ≥ 90 % карточек есть телефон» из плана Ф4: телефон — главный канал,
    # без него лид не попадёт в выдачу Ф8. Ассерт Ф3 проверял ровно одну карточку,
    # что на 226 карточках уже ничего не значит.
    with_phone = db.execute(
        "SELECT count(DISTINCT branch_id) FROM contacts WHERE kind = 'phone'"
    ).fetchone()[0]
    assert with_phone >= 0.9 * cards, f"телефон есть у {with_phone} карточек из {cards}"

    phones = [h for (h,) in db.execute("SELECT handle FROM contacts WHERE kind = 'phone'")]
    # Короткие сервисные номера (7788, 1432) в +7 не разворачиваются и это нормально.
    e164 = [p for p in phones if p.startswith("+7")]
    assert len(e164) > 0.95 * len(phones), \
        f"в формате +7 только {len(e164)} телефонов из {len(phones)}"

    sites = [h for (h,) in db.execute("SELECT handle FROM contacts WHERE kind = 'website'")]
    assert not any("link.2gis" in s for s in sites), "редирект link.2gis.ru не развёрнут"
    print(f"  contacts {count(db, 'contacts')} строк, карточек {cards}, с телефоном {with_phone}")


def check_fetches(db):
    pages = len([f for f in RAW.glob("*.json") if not f.name.endswith(".serp.json")])
    urls = count(db, "fetches")
    assert urls <= pages, f"в fetches {urls} адресов при {pages} страницах сырья"
    empty = db.execute("SELECT count(*) FROM fetches WHERE final_url IS NULL").fetchone()[0]
    assert not empty, "адрес без final_url — обнаружение подмены ослепло"
    print(f"  fetches {urls}")


def check_profiles(db):
    """Профили от модели дошли до своих компаний, и ни один не потерялся."""
    import build

    # Профиль опознаёт компанию по паре (название, город) из промпта: названия в
    # базе не уникальны, и по одному названию оплаченный профиль лёг бы чужой
    # компании — это цитата с чужого сайта в письме живому человеку (F20).
    orphan = db.execute(
        "SELECT count(*) FROM profiles p"
        " WHERE NOT EXISTS (SELECT 1 FROM companies c WHERE c.company_id = p.company_id)"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} профилей у несуществующих компаний"

    # Ответ модели стоил денег: если он лежит в raw/, а до базы не доехал, значит
    # ключ разошёлся с промптом, и молча теряется оплаченная работа.
    answers = len(build.load_llm_answers("company_profile"))
    stored = count(db, "profiles")
    assert stored == answers, (
        f"ответов модели в raw/ {answers}, профилей в базе {stored} — "
        "оплаченные ответы не находят свою компанию"
    )
    print(f"  профилей {stored}, все у существующих компаний")


def check_rebuild_is_identical():
    """PRD П5: повторная сборка даёт ту же базу и не трогает сырьё."""
    before = dump_hash(), raw_file_count()
    subprocess.run([sys.executable, "build.py"], check=True, capture_output=True)
    after = dump_hash(), raw_file_count()

    assert before[0] == after[0], "повторная сборка дала другую базу — сборка недетерминирована"
    assert before[1] == after[1], (
        f"файлов в raw/ было {before[1]}, стало {after[1]} — build.py ходил в сеть"
    )
    print(f"  пересборка воспроизводима: sha256 дампа {before[0][:16]}, raw/ {before[1]} файлов")


def check_collect():
    """Приёмка Ф4: обе проверки подмены доказаны на собранном сырье, план соблюдён."""
    assert DB.exists(), "leads.db нет — сначала uv run -m scripts.collect && uv run build.py"
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    db = sqlite3.connect(DB)

    check_pagination_guard(db)
    check_slug_guard(db, config)
    check_plan_coverage(db, config)
    db.close()


def check_pagination_guard(db):
    """Потолок 2GIS пойман на живых страницах, а не предположен.

    Проверка сравнивает запрошенную страницу с той, которую 2GIS реально отдал.
    Она слепа, если в сайдкаре записан конечный адрес вместо запрошенного, —
    поэтому здесь же проверяется, что номер запрошенной страницы виден в url.
    """
    caught = []
    for url, sha in db.execute("SELECT url, sha FROM fetches WHERE url LIKE '%/rubric/%'"):
        match = RUBRIC_URL.search(url)
        if not match:
            continue
        requested = int(match.group(1) or 1)
        current = sources.parse_search_meta(sources.parse_initial_state(page_html(sha)))[2]
        if current != requested:
            caught.append((url, requested, current))

    assert caught, (
        "ни одной подменённой страницы в сырье — потолок пагинации ничем не подтверждён"
    )
    for url, requested, current in caught:
        assert f"/page/{requested}" in url, (
            f"{url}: 2GIS отдал страницу {current}, но номер запрошенной страницы "
            "не сохранён — на такой странице проверка подмены слепа"
        )
    print(f"  подмена пагинации поймана на {len(caught)} страницах, все с запрошенным адресом")


def check_slug_guard(db, config):
    """Каждая собранная страница slug'а — та, что запрошена, а не общий список города."""
    slugs = set(config["hh"]["slugs"])
    checked = 0
    for url, landed in db.execute("SELECT url, final_url FROM fetches WHERE url LIKE '%/vacancies/%'"):
        slug = url.rsplit("/", 1)[1]
        if slug not in slugs:  # отбракованные кандидаты Ф2 лежат в сырье намеренно
            continue
        assert slug.lower() in landed.lower(), f"страница '{slug}' подменена: {landed}"
        checked += 1
    assert checked, "ни одной страницы slug'а из config.toml в сырье"
    print(f"  slug'и: {checked} страниц приземлились там, где запрошены")


def check_plan_coverage(db, config):
    """Собрано ровно то, что задано конфигом, и не больше потолка источника."""
    include = {str(r) for r in config["rubrics"]["include"]}
    rows = db.execute("SELECT city, rubric_id, count(*) FROM orgs GROUP BY city, rubric_id").fetchall()
    assert rows, "orgs пуста — collect.py не собрал ни одной рубрики"
    for city, rubric, found in rows:
        assert rubric in include, f"рубрика {rubric} собрана, но её нет в config include"
        assert city in config["cities"], f"город {city} не из config"
        assert found <= 60, f"{rubric}/{city}: {found} организаций при потолке 2GIS 60"

    total = sum(found for *_, found in rows)
    assert total > 60, f"организаций {total} — сбор не вышел за пределы одной рубрики"
    print(f"  orgs {total} по {len(rows)} парам рубрика×город, потолок 60 соблюдён")


def page_html(sha):
    with gzip.open(RAW / f"{sha}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def dump_hash():
    db = sqlite3.connect(DB)
    digest = hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()
    db.close()
    return digest


def raw_file_count():
    return len(list(RAW.iterdir()))


def count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def check_resolve():
    """Ф5: филиалы схлопнулись в компании, и каждая склейка объяснима."""
    db = sqlite3.connect(DB)
    branches, companies = count(db, "orgs"), count(db, "companies")
    assert companies < branches, f"склейка ничего не дала: {branches} филиалов, {companies} компаний"

    linked = db.execute("SELECT count(DISTINCT branch_id) FROM company_links").fetchone()[0]
    assert linked == branches, f"без компании остались {branches - linked} филиалов"

    orphan = db.execute(
        "SELECT count(*) FROM company_links l"
        " WHERE NOT EXISTS (SELECT 1 FROM orgs o WHERE o.branch_id = l.branch_id)"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} связей ссылаются на несуществующий филиал"

    # Нечёткая склейка обязана быть видна: правило названо, confidence проставлен.
    # Без этого «похоже по имени» неотличимо от «совпал домен», и разбирать
    # ошибочную склейку будет не по чему.
    unexplained = db.execute(
        "SELECT count(*) FROM company_links WHERE rule = 'name_city_fuzzy'"
        " AND (confidence IS NULL OR confidence < 0.8)"
    ).fetchone()[0]
    assert unexplained == 0, f"{unexplained} склеек по имени без внятного confidence"

    multi = db.execute(
        "SELECT company_id, count(*) c FROM company_links GROUP BY company_id"
        " HAVING c > 1 ORDER BY c DESC LIMIT 1"
    ).fetchone()
    assert multi, "ни одной компании из нескольких филиалов — правила не сработали"

    # Привязка работодателя hh к компании 2GIS — самое слабое место конвейера:
    # из 384 вакансий привязаны 4. Число зафиксировано, чтобы падение до нуля
    # не проходило зелёным. Ветку чинит отдельный план; когда починят — поднять
    # порог здесь тем же коммитом, иначе ассерт перестанет что-либо значить.
    LINKED_VACANCIES_FLOOR = 4
    fuzzy = db.execute(
        "SELECT count(*) FROM vacancies WHERE company_id IS NOT NULL"
    ).fetchone()[0]
    total = db.execute("SELECT count(DISTINCT employer) FROM vacancies").fetchone()[0]
    assert fuzzy >= LINKED_VACANCIES_FLOOR, (
        f"вакансий привязано к компаниям {fuzzy}, было {LINKED_VACANCIES_FLOOR} — "
        "склейка работодателей стала хуже, сигналы вакансий исчезнут из скоринга"
    )
    print(f"  компаний {companies} из {branches} филиалов, крупнейшая из {multi[1]}")
    print(f"  вакансий привязано к компаниям {fuzzy}, работодателей всего {total}")
    db.close()


def check_signals():
    """Ф6: сигналы — события с датой и обоснованием, а не флаги."""
    db = sqlite3.connect(DB)
    total = count(db, "signals")
    assert total > 0, "ни одного сигнала: сайты не собраны или детекторы сломаны"

    # Дата обязательна у каждого: без неё сигнал не затухает, и вакансия
    # позапрошлого года весит как вчерашняя. Ровно эта колонка отличает
    # signals от набора булевых флагов в companies.
    undated = db.execute("SELECT count(*) FROM signals WHERE observed_at IS NULL").fetchone()[0]
    assert undated == 0, f"{undated} сигналов без даты наблюдения"

    mute = db.execute(
        "SELECT count(*) FROM signals WHERE quote IS NULL OR quote = '' OR url IS NULL"
    ).fetchone()[0]
    assert mute == 0, f"{mute} сигналов без цитаты или ссылки — why_now из них не собрать"

    orphan = db.execute(
        "SELECT count(*) FROM signals s WHERE NOT EXISTS"
        " (SELECT 1 FROM companies c WHERE c.company_id = s.company_id)"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} сигналов у несуществующих компаний"

    # Один и тот же тип не должен начисляться компании дважды с одного сайта:
    # Bitrix и amoCRM вместе — по-прежнему один факт «есть CRM», а не двойной вес.
    doubled = db.execute(
        "SELECT count(*) FROM (SELECT company_id, type, url FROM signals"
        " GROUP BY company_id, type, url HAVING count(*) > 1)"
    ).fetchone()[0]
    assert doubled == 0, f"{doubled} сигналов задвоены по (компания, тип, источник)"

    # Ветвь vacancy_* сегодня пуста: вакансии почти не привязываются к компаниям
    # (см. LINKED_VACANCIES_FLOOR в check_resolve). Ассерт на конкретные типы
    # поставить нельзя, пока их ноль, — вместо него на виду держится состав
    # семейств, чтобы исчезновение site_* или ig_* не спряталось за общим total.
    families = {row[0] for row in db.execute("SELECT DISTINCT type FROM signals")}
    site_types = {"ads_platform", "crm_widget", "inbound_widget", "service_catalog"}
    assert site_types <= families, (
        f"сигналы сайта потеряны: есть {sorted(families & site_types)}, "
        f"нет {sorted(site_types - families)}"
    )
    assert any(t.startswith("ig_") for t in families), \
        "ни одного сигнала инстаграма — лента не разобрана или склейка аккаунтов сломана"
    if not any(t.startswith("vacancy_") for t in families):
        print("    ! сигналов вакансий нет — ветка hh не работает, см. plans/002")

    check_instagram_signals(db)

    with_signal = db.execute("SELECT count(DISTINCT company_id) FROM signals").fetchone()[0]
    companies = count(db, "companies")
    print(f"  сигналов {total} у {with_signal} компаний из {companies}")
    for row in db.execute("SELECT type, count(*) FROM signals GROUP BY type ORDER BY 2 DESC"):
        print(f"    {row[0]:<16} {row[1]}")
    db.close()


def check_instagram_signals(db):
    """Ф6-IG: заброшенный и живой аккаунт исключают друг друга.

    Аккаунт, молчащий полгода, не может одновременно считаться живым. Если оба
    сигнала стоят у одной компании, то либо порог поехал, либо к компании
    привязаны две разные ленты — и в обоих случаях intent_score завышен вдвое.
    """
    both = db.execute(
        "SELECT count(*) FROM (SELECT company_id FROM signals"
        " WHERE type IN ('ig_dormant', 'ig_active_marketing')"
        " GROUP BY company_id HAVING count(DISTINCT type) > 1)"
    ).fetchone()[0]
    assert both == 0, f"{both} компаний одновременно и заброшены, и активны в инстаграме"

    # Цитата модели обязана стоять в подписи дословно — иначе оператор увидит в
    # why_now фразу, которой в аккаунте нет. Здесь проверяется следствие: пустых
    # и обрезанных цитат у находок модели быть не должно.
    empty = db.execute(
        "SELECT count(*) FROM signals WHERE type IN"
        " ('ig_direct_selling', 'ig_promo', 'ig_hiring_sales') AND length(quote) < 3"
    ).fetchone()[0]
    assert empty == 0, f"{empty} находок модели с пустой цитатой"

    kinds = {row[0] for row in db.execute("SELECT DISTINCT type FROM signals WHERE type LIKE 'ig_%'")}
    print(f"  инстаграм: типы сигналов {sorted(kinds)}")


def check_scores():
    """Ф7: у каждой компании оба скоринга и разбивка, объясняющая число."""
    db = sqlite3.connect(DB)
    companies, scored = count(db, "companies"), count(db, "scores")
    assert scored == companies, f"компаний {companies}, оценено {scored}"

    # Число без разбивки не объясняет ничего, а лид придётся объяснять клиенту.
    # Пустая разбивка допустима только при нулевом счёте.
    silent = db.execute(
        "SELECT count(*) FROM scores WHERE (fit_score > 0 OR intent_score > 0)"
        " AND (breakdown IS NULL OR breakdown = '[]')"
    ).fetchone()[0]
    assert silent == 0, f"{silent} компаний с ненулевым счётом и пустой разбивкой"

    negative = db.execute(
        "SELECT count(*) FROM scores WHERE fit_score < 0 OR intent_score < 0"
    ).fetchone()[0]
    assert negative == 0, "отсутствие сигнала не должно давать отрицательный вес"

    # Разбивка обязана сходиться с числом: расхождение означает, что показанное
    # оператору обоснование не соответствует позиции лида в списке.
    for company_id, intent, breakdown in db.execute(
        "SELECT company_id, intent_score, breakdown FROM scores"
        " WHERE intent_score > 0 ORDER BY company_id LIMIT 200"
    ):
        parts = json.loads(breakdown)
        total = sum(p["contribution"] for p in parts if "signal" in p)
        assert abs(total - intent) < 0.05, f"{company_id}: разбивка {total} против {intent}"

    top = db.execute("SELECT max(intent_score) FROM scores").fetchone()[0]
    with_intent = db.execute("SELECT count(*) FROM scores WHERE intent_score > 0").fetchone()[0]
    print(f"  оценено {scored} компаний, с intent > 0 — {with_intent}, максимум {top}")
    db.close()


def check_leads():
    """Ф8 — приёмка PRD: выдача годится к отправке руками."""
    assert OUT.exists(), "leads.csv нет — сначала uv run report.py"
    with OUT.open(encoding="utf-8-sig") as fh:
        leads = list(csv.DictReader(fh))

    assert len(leads) >= 30, f"в выдаче {len(leads)} лидов, приёмка требует 30 (П1)"

    # F19: компания с прекрасным скорингом, но без рабочего канала — не лид,
    # а мусор в отчёте. Правило не смягчается ради красивого числа.
    for lead in leads:
        kind = lead["канал"].split(":")[0]
        assert kind in ("whatsapp", "phone", "email"), f"{lead['компания']}: канал {kind} (П2)"
        assert lead["канал"].split(": ", 1)[1].strip(), f"{lead['компания']}: пустой канал"

    # F20: обоснование с ссылкой на источник. «Высокий intent_score» обоснованием
    # не является — оператор должен мочь повторить причину в разговоре.
    for lead in leads:
        assert lead["why_now"].strip(), f"{lead['компания']}: пустой why_now (П3)"
        assert lead["источники"].startswith("http"), f"{lead['компания']}: нет ссылки (П3)"

    cities = {lead["город"] for lead in leads}
    assert cities <= {"almaty", "astana"}, f"города вне ICP: {cities} (П1)"

    db = sqlite3.connect(DB)
    suppressed = {row[0] for row in db.execute("SELECT handle FROM suppression")}
    leaked = [l for l in leads if l["канал"].split(": ", 1)[1] in suppressed]
    assert not leaked, f"{len(leaked)} лидов из suppression попали в выдачу (F21)"

    # available() показывается оператору в шапке и задаёт пункт «все N».
    # Он обязан считать ровно то же, что выдача: иначе оператор запросит больше
    # лидов, чем существует, и решит, что часть потерялась.
    import report

    by_hand = sum(
        1 for row in report.candidates(db) if report.best_channel(row["channels"], suppressed)
    )
    counted = report.available(db)
    assert counted == by_hand, (
        f"available() говорит {counted}, полный проход даёт {by_hand} — "
        "счётчик разошёлся с выдачей"
    )
    db.close()

    print(f"  доступно лидов {by_hand}, счётчик с ними согласен")
    print(f"  выдача: {len(leads)} лидов, города {sorted(cities)}, у всех канал и why_now")


def check_web():
    """Веб отдаёт тот же отбор, что CSV, и отказ переживает пересборку.

    Обе проверки существуют против одного класса ошибок: у веба своя копия
    правил. Если она разъедется с report.py, оператор увидит в браузере лид,
    которого нет в выдаче, — а F19 и F21 не про формат вывода, а про закон.
    """
    from db import lead as store
    from routes import leads as web_leads
    from services import suppression as refusals

    db = sqlite3.connect(DB)
    web = web_leads.leads(limit=30)["leads"]
    with OUT.open(encoding="utf-8-sig") as fh:
        csv_names = [lead["компания"] for lead in csv.DictReader(fh)]
    assert [lead["name"] for lead in web] == csv_names, "веб и leads.csv разошлись"

    suppressed = store.suppression_handles(db)
    leaked = [lead for lead in web if lead["channel"]["handle"] in suppressed]
    assert not leaked, f"{len(leaked)} лидов из suppression в веб-выдаче (F21)"

    # Файл — источник истины, таблица — копия. Разойдутся, и запрет исчезнет на
    # ближайшем build.py, потому что схема пересобирается через DROP.
    from_file = refusals.existing_handles() - {""}
    assert from_file == suppressed, f"в файле {from_file}, в базе {suppressed}"

    for lead in web:
        assert lead["why_now"].strip(), f"{lead['name']}: пустой why_now"
    db.close()
    print(f"  веб: {len(web)} лидов, тот же порядок что в CSV, отказов {len(suppressed)}")


SECTIONS = {
    "parsers": check_parsers,
    "raw": check_raw,
    "build": check_build,
    "collect": check_collect,
    "resolve": check_resolve,
    "signals": check_signals,
    "scores": check_scores,
    "leads": check_leads,
    "web": check_web,
}


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(SECTIONS)
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        sys.exit(f"нет раздела {unknown}. Есть: {', '.join(SECTIONS)}")
    for name in wanted:
        SECTIONS[name]()
    print("check ok:", ", ".join(wanted))
