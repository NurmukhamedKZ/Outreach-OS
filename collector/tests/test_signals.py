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