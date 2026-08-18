"""Ф6: сигналы — события с датой и обоснованием, а не флаги.

Требует собранной data/derived.db — на чистом клоне раздел пропускается.
"""

SITE_TYPES = {"ads_platform", "crm_widget", "inbound_widget", "service_catalog"}


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
    cfg = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    weights = cfg["scoring"]["intent"]
    for t in REVIEW_TYPES:
        assert t in weights, f"нет веса для {t} — сигнал не наберёт цену"


def test_reviews_missed_lead_picks_newest_one():
    """Дедуп жалоб одного типа: newest_review_match отдаёт не больше одной пары
    на вызов — иначе одинаковые (company_id, type, observed_at, url) от одного
    филиала столкнулись бы по PRIMARY KEY signals_all."""
    from services.enrich import newest_review_match

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


def test_reviews_missed_lead_no_match_is_none():
    from services.enrich import newest_review_match
    assert newest_review_match([], [{"type": "не дозвонились", "quote": "нет такого отзыва"}]) is None


def test_reviews_quote_is_verbatim(live_db):
    """Главная проверка: цитата отзыва стоит дословно в сырье.

    Для этого сигналы отзывов перепривязываются к филиалу и сверяются с текстом
    отзыва из raw/. Не нашлась дословно — модель исказила, и в signals ей не место.
    """
    import gzip
    import json
    import services.storage as storage

    quotes = live_db.execute(
        "SELECT quote FROM signals WHERE type LIKE 'reviews_%' AND length(quote) > 2"
    ).fetchall()
    raw_text = ""
    for sidecar in sorted(storage.RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue   # .llm.json / .serp.json — кэш другого рода, не страница
        if "reviews.2gis.com" not in json.loads(sidecar.read_text(encoding="utf-8"))["url"]:
            continue
        sha = sidecar.name.removesuffix(".json")
        raw_text += gzip.open(storage.RAW / f"{sha}.html.gz", "rt", encoding="utf-8").read()
    for (quote,) in quotes:
        assert quote in raw_text, f"цитата отзыва не дословна в сырье: {quote!r}"


SITE_AI_TYPES = {"site_hiring_sales", "site_no_pricing"}


def test_site_ai_types_have_weights(live_db):
    import tomllib
    from pathlib import Path
    weights = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]["intent"]
    for t in SITE_AI_TYPES:
        assert t in weights, f"нет веса для {t}"