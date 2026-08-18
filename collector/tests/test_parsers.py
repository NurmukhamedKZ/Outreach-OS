"""Разбор источников на эталонных страницах: ни базы, ни сети, ни raw/.

Единственный раздел, работающий на чистом клоне: страницы лежат в fixtures/ и в
git, числа ниже — свойства именно этих файлов, а не «примерно столько».
"""

import gzip
import re
from pathlib import Path

from services import enrich, sources

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures
CYRILLIC = re.compile(r"[А-Яа-я]")
GIS_RUBRIC_CITY, GIS_RUBRIC_ID = "almaty", "653"
GIS_FIRM_BRANCH = "70000001017502602"


def fixture_html(name):
    with gzip.open(FIXTURES / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def test_gis_rubric_parsing():
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

    test_non_orgs_are_skipped()


def test_non_orgs_are_skipped():
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


def test_gis_firm_parsing():
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


def test_link_unwrapping():
    """2GIS заворачивает сайт в редирект: в contacts обязан лечь адрес компании."""
    wrapped = "https://link.2gis.ru/go?https://studionomad.kz"
    assert sources.unwrap_2gis_link(wrapped) == "https://studionomad.kz", "редирект не развёрнут"
    assert sources.unwrap_2gis_link("https://studionomad.kz") == "https://studionomad.kz", \
        "прямой адрес испорчен разворачиванием"
    assert sources.unwrap_2gis_link(None) is None


def test_ig_parsing():
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

    test_ig_empty_caption_is_not_none()
    test_ig_quote_binding(posts)


def test_ig_quote_binding(posts=None):
    """Находка модели привязывается к посту по цитате, а не по её номеру.

    Номер модель иногда сдвигает — в живом прогоне пришёл 0 при нумерации с
    единицы. Цитата же обязана быть дословной, и её отсутствие в подписи значит,
    что модель фразу испортила: такой находке в signals не место.
    """
    posts = posts or sources.parse_ig_feed(fixture_html("ig_feed"))["posts"]
    real = next(p for p in posts if len(p["caption"]) > 40)
    fragment = real["caption"][10:40]
    bound = enrich.post_with_quote(posts, fragment)
    assert bound and fragment in bound["caption"], "цитата не нашла свой пост"
    assert enrich.post_with_quote(posts, "такой фразы в ленте нет") is None, \
        "выдуманная цитата привязалась к посту — проверка дословности не работает"
    assert enrich.post_with_quote(posts, "") is None, "пустая цитата привязалась к посту"


def test_ig_empty_caption_is_not_none():
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