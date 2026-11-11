"""Ф6: сигналы — события с датой и обоснованием, а не флаги.

Требует собранной data/derived.db — на чистом клоне раздел пропускается.
"""

import functools
import gzip
import json

SITE_TYPES = {"ads_platform", "crm_widget", "inbound_widget"}


REVIEWS_HOST = "reviews.2gis.com"
INSTAGRAM_HOST = "instagram.com"


def _pages(belongs) -> list[str]:
    """Тела страниц одного вида источника: belongs(url) решает, чей это файл.

    Списком, а не конкатенацией: raw/ весит гигабайты, и `text += ...` в цикле
    — квадратичная склейка, из-за которой эти две проверки занимали пять минут
    из шести.
    """
    import collector.services.storage as storage

    bodies = []
    for sidecar in sorted(storage.RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue   # .llm.json / .serp.json — кэш другого рода, не страница
        url = json.loads(sidecar.read_text(encoding="utf-8")).get("url") or ""
        if not belongs(url):
            continue
        gz = storage.RAW / f"{sidecar.name.removesuffix('.json')}.html.gz"
        if gz.exists():
            bodies.append(gzip.open(gz, "rt", encoding="utf-8").read())
    return bodies


@functools.cache
def _review_text() -> str:
    """Тексты отзывов и официальных ответов — РАЗОБРАННЫЕ, а не сырой JSON.

    Ответ API 2GIS экранирует кавычки внутри текста, и отзыв со словом
    «эксперт» в кавычках не находится в файле дословно, хотя цитата верна.
    Разбор берётся из sources.py — той же единственной копии, которой
    пользуется прод: сверять цитату с байтами, которых модель не видела,
    значит проверять не то.
    """
    from collector.services import sources

    return "\n".join(
        f"{row.get('text') or ''}\n{row.get('official_answer') or ''}"
        for body in _pages(lambda url: REVIEWS_HOST in url)
        for row in sources.parse_reviews(body, "branch"))


@functools.cache
def _comment_text() -> str:
    """Реплики под постами — тем же разбором и по той же причине."""
    from collector.services import sources

    return "\n".join(
        row.get("text") or ""
        for body in _pages(lambda url: "/comments/" in url)
        for row in sources.parse_ig_comments(body, "pk"))


@functools.cache
def _site_text() -> str:
    """Страницы сайтов компаний: HTML отдаётся как есть, экранирования в нём нет.

    Всё, что не отзывы и не инстаграм: их тексты живут в JSON и разбираются
    отдельно, а цитате с сайта нечего делать в чужом источнике — найденная
    там, она означала бы, что проверка ничего не проверяет.
    """
    return "\n".join(_pages(
        lambda url: REVIEWS_HOST not in url and INSTAGRAM_HOST not in url))


def test_signals_are_dated_and_grounded(live_db):
    db = live_db
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

    # На виду держится состав семейств, чтобы исчезновение site_* или ig_* не
    # спряталось за общим total.
    families = {row[0] for row in db.execute("SELECT DISTINCT type FROM signals")}
    assert SITE_TYPES <= families, (
        f"сигналы сайта потеряны: есть {sorted(families & SITE_TYPES)}, "
        f"нет {sorted(SITE_TYPES - families)}"
    )
    assert any(t.startswith("ig_") for t in families), \
        "ни одного сигнала инстаграма — лента не разобрана или склейка аккаунтов сломана"

    test_instagram_signals_exclude_each_other(db)

    with_signal = db.execute("SELECT count(DISTINCT company_id) FROM signals").fetchone()[0]
    companies = count(db, "companies")
    assert 0 < with_signal <= companies


def test_instagram_signals_exclude_each_other(live_db):
    """Ф6-IG: заброшенный и живой аккаунт исключают друг друга.

    Аккаунт, молчащий полгода, не может одновременно считаться живым. Если оба
    сигнала стоят у одной компании, то либо порог поехал, либо к компании
    привязаны две разные ленты — и в обоих случаях intent_score завышен вдвое.
    """
    db = live_db
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


def count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


REVIEW_TYPES = {"reviews_missed_lead", "reviews_unanswered_complaint"}


def test_review_signal_types_have_weights(live_db):
    """Новые типы отзывов существуют в config.toml — сигналу нужна цена."""
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config.toml").read_text(encoding="utf-8"))
    weights = cfg["scoring"]["intent"]
    for t in REVIEW_TYPES:
        assert t in weights, f"нет веса для {t} — сигнал не наберёт цену"
    assert "service_catalog" not in weights, "service_catalog должен быть удалён"


def test_reviews_missed_lead_picks_newest_one():
    """Дедуп жалоб одного типа: newest_review_match отдаёт не больше одной пары
    на вызов — иначе одинаковые (company_id, type, observed_at, url) от одного
    филиала столкнулись бы по PRIMARY KEY signals_all."""
    from collector.services.enrich import newest_review_match

    reviews = [
        {"branch_id": "b1", "text": "не дозвонились вчера", "date_created": "2026-08-01"},
        {"branch_id": "b1", "text": "не дозвонились сегодня", "date_created": "2026-08-02"},
    ]
    complaints = [
        {"type": "не дозвонились", "quote": "не дозвонились вчера"},
        {"type": "не дозвонились", "quote": "не дозвонились сегодня"},
    ]
    match = newest_review_match(reviews, complaints)
    assert match is not None
    review, quote = match
    assert quote == "не дозвонились сегодня", "должна выбираться самая свежая жалоба"


def test_missed_lead_and_other_complaints_do_not_overlap():
    """Регрессия: одна жалоба не должна давать reviews_missed_lead и
    reviews_unanswered_complaint одновременно — иначе один отрицательный
    отзыв весит вдвое под двумя именами сигнала."""
    from collector.services.enrich import missed_lead_complaints, other_complaints

    complaints = [
        {"type": "не дозвонились", "quote": "не дозвонились, деньги не вернули"},
        {"type": "качество работы", "quote": "сделали плохо"},
    ]

    missed = missed_lead_complaints(complaints)
    other = other_complaints(complaints)

    assert [c["quote"] for c in missed] == ["не дозвонились, деньги не вернули"]
    assert [c["quote"] for c in other] == ["сделали плохо"]


def test_reviews_missed_lead_no_match_is_none():
    from collector.services.enrich import newest_review_match
    assert newest_review_match([], [{"type": "не дозвонились", "quote": "нет такого отзыва"}]) is None


def test_reviews_quote_is_verbatim(live_db):
    """Главная проверка: цитата отзыва стоит дословно в тексте отзыва.

    Не нашлась — модель исказила, и в signals ей не место. Сверяется с
    разобранным текстом, а не с телом ответа API: экранированные кавычки в
    JSON — свойство транспорта, а не отзыва, и модель их не видела.
    """
    quotes = live_db.execute(
        "SELECT quote FROM signals WHERE type LIKE 'reviews_%' AND length(quote) > 2"
    ).fetchall()
    text = _review_text()
    for (quote,) in quotes:
        assert quote in text, f"цитата отзыва не дословна в сырье: {quote!r}"


SITE_AI_TYPES = {"site_hiring_sales", "site_no_pricing"}


def test_site_ai_types_have_weights(live_db):
    import tomllib
    from pathlib import Path
    weights = tomllib.loads((Path(__file__).resolve().parent.parent / "config.toml").read_text(encoding="utf-8"))["scoring"]["intent"]
    for t in SITE_AI_TYPES:
        assert t in weights, f"нет веса для {t}"


IG_LAYER_TYPES = {"ig_unanswered_question", "ig_reach_declining"}


def test_ig_layer_types_have_weights(live_db):
    import tomllib
    from pathlib import Path
    weights = tomllib.loads((Path(__file__).resolve().parent.parent / "config.toml").read_text(encoding="utf-8"))["scoring"]["intent"]
    for t in IG_LAYER_TYPES:
        assert t in weights, f"нет веса для {t}"


def test_reach_trend_uses_median():
    """Один залетевший пост не создаёт ложный тренд — медиана, не среднее."""
    from collector.services import enrich
    posts = [{"taken_at": f"2026-08-0{i}T00:00:00Z", "likes": l}
             for i, l in enumerate([10, 12, 11, 13, 100, 8, 9, 10])]
    db = _memory_db()
    enrich.reach_declining(db, 1, "c", posts, {"ig_reach_declining": 3.0}, "2026-08-09T00:00:00Z")
    rows = db.execute("SELECT type FROM signals_all WHERE company_id='c'").fetchall()
    assert [r[0] for r in rows] == ["ig_reach_declining"], "тренд не сработал на медиане"


def test_reach_trend_skips_short_feeds():
    """Лента короче 6 постов — отсутствие сигнала, а не нулевой."""
    from collector.services import enrich
    posts = [{"taken_at": f"2026-08-0{i}T00:00:00Z", "likes": l}
             for i, l in enumerate([20, 1, 20])]
    db = _memory_db()
    enrich.reach_declining(db, 1, "c", posts, {"ig_reach_declining": 3.0}, "2026-08-09T00:00:00Z")
    rows = db.execute("SELECT count(*) FROM signals_all WHERE company_id='c'").fetchone()[0]
    assert rows == 0, "короткая лента дала тренд"


def test_old_ig_llm_signals_retired(live_db):
    """ig_promo/ig_hiring_sales/ig_direct_selling — слой их больше не читает,
    даже если в state.llm_answers остались исторические ответы kind='ig_signals'."""
    db = live_db
    families = {row[0] for row in db.execute("SELECT DISTINCT type FROM signals")}
    retired = {"ig_promo", "ig_hiring_sales", "ig_direct_selling"}
    assert not (families & retired), f"старый LLM-слой инстаграма всё ещё пишет: {families & retired}"


# Где искать цитату каждого вида сигнала. Источник у каждого свой, и текст
# из него достаётся своим разбором: искать цитату отзыва в HTML сайта — то же
# самое, что не искать вовсе.
QUOTE_SOURCE = {
    "reviews_missed_lead": _review_text,
    "reviews_unanswered_complaint": _review_text,
    "ig_unanswered_question": _comment_text,
    "site_hiring_sales": _site_text,
}


def test_quote_is_verbatim_for_ai_signals(live_db):
    """Главная проверка спеки §6: цитата каждого AI-сигнала стоит дословно в сырье.

    reviews/site/instagram — единственная защита от выдуманного факта в письме
    живому человеку, и на ней же стоит отбор поводов системы 2: сигнал без
    цитаты поводом не считается, поэтому цитата обязана быть настоящей.
    """
    rows = live_db.execute(
        "SELECT type, quote FROM signals WHERE type IN"
        f" ({','.join('?' * len(QUOTE_SOURCE))}) AND length(quote) > 2",
        tuple(QUOTE_SOURCE)).fetchall()
    assert rows, "ни одной цитаты AI-сигналов — проверять нечего"
    for signal_type, quote in rows:
        assert quote in QUOTE_SOURCE[signal_type](), \
            f"{signal_type}: цитата не дословна в сырье: {quote!r}"


def _memory_db():
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE signals_all (run_id INTEGER, company_id TEXT, type TEXT,"
        " observed_at TEXT, weight REAL, quote TEXT, url TEXT)")
    return db