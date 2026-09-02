"""Отбор кандидатов: F19 и F21 воспроизведены запросом, не импортом collector'а.

База формы collector'а строится из collector/db/schema.sql (см. conftest):
всегда та же форма, что у живого derived.db, иначе проверка прошла бы на
выдуманной таблице.
"""

from datetime import date

from writer.db import leads_source
from writer.db.leads_source import PitchRules

RULES = PitchRules(
    pitchable=frozenset({"reviews_unanswered_complaint", "site_hiring_sales"}),
    max_age_days=60,
    today=date(2026, 9, 2),
)


def _extra_signals(db, run_id=1):
    """Три случая рядом с уже лежащим crm_widget: годный, старый, безцитатный."""
    rows = (
        ("reviews_unanswered_complaint", "2026-08-25", "второй раз не дозвонился", "https://2gis.kz/1"),
        ("site_hiring_sales", "2026-05-01", "ищем менеджера по продажам", "https://romashka.kz/jobs"),
        ("reviews_missed_lead", "2026-08-30", None, "https://2gis.kz/2"),
    )
    for kind, observed_at, quote, url in rows:
        db.execute("INSERT INTO signals_all (run_id, company_id, type, observed_at, weight,"
                   " quote, url) VALUES (?, 'c_ok', ?, ?, 4.0, ?, ?)",
                   (run_id, kind, observed_at, quote, url))
    db.commit()


def _seed(db, run_id=1):
    """Пять случаев в *_all-таблицах + current_run, как у боевой базы."""
    for company_id, name, intent in (
        ("c_ok", "Ромашка", 6.0),
        ("c_phone", "Лютик", 4.0),
        ("c_no_channel", "Тишина", 5.0),
        ("c_suppressed", "Отказ", 5.5),
        ("c_short_number", "Короткий", 5.0),
        ("c_no_intent", "Пусто", 0.0),
    ):
        branch = f"b_{company_id}"
        db.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city, rubric_id)"
                   " VALUES (?, ?, ?, 'almaty', '653')",
                   (run_id, company_id, name))
        db.execute("INSERT INTO scores_all (run_id, company_id, fit_score, intent_score, breakdown)"
                   " VALUES (?, ?, 5.0, ?, '[]')", (run_id, company_id, intent))
        db.execute("INSERT INTO orgs_all (run_id, branch_id, name, org_name, branch_count,"
                   " city, rubric_id, address) VALUES (?, ?, ?, ?, 1, 'almaty', '653', 'ул. Абая, 1')",
                   (run_id, branch, name, name))
        db.execute("INSERT INTO company_links_all (run_id, company_id, branch_id, rule, confidence)"
                   " VALUES (?, ?, ?, 'self', 1.0)", (run_id, company_id, branch))
        db.execute("INSERT INTO dossiers_all (run_id, company_id, model, summary,"
                   " hooks, pains, approach, sources, confidence)"
                   " VALUES (?, ?, 'модель', 'бухгалтерия', '[]',"
                   " '[{\"statement\": \"ищет клиентов\", \"evidence\": [], \"severity\": \"видно явно\"}]',"
                   " 'заходить через рост', '[\"reviews\"]', 0.8)",
                   (run_id, company_id))
        db.execute("INSERT INTO signals_all (run_id, company_id, type, observed_at, weight,"
                   " quote, url) VALUES (?, ?, 'crm_widget', '2026-08-01', 3.0,"
                   " 'виджет Bitrix24', 'https://romashka.kz/')", (run_id, company_id))

    contacts = (
        ("b_c_ok", "whatsapp", "https://wa.me/77010000001?text=%D0%9F%D0%B8%D1%88%D1%83"),
        ("b_c_ok", "phone", "+77010000009"),
        ("b_c_phone", "phone", "+77010000003"),
        ("b_c_suppressed", "whatsapp", "https://wa.me/77010000002"),
        ("b_c_short_number", "phone", "1400"),
        ("b_c_no_intent", "phone", "+77010000004"),
    )
    for branch, kind, handle in contacts:
        db.execute("INSERT INTO contacts_all (run_id, branch_id, kind, handle, source_url)"
                   " VALUES (?, ?, ?, ?, NULL)", (run_id, branch, kind, handle))
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000002', '2026-08-10', 'просил не писать')")
    db.execute("INSERT INTO current_run (id, run_id) VALUES (1, ?)", (run_id,))
    db.commit()


def test_pitchable_fresh_and_quoted_signals_only(leads_db):
    db = leads_db
    _seed(db)
    _extra_signals(db)

    signals = leads_source.signals_of(db, "c_ok", RULES)
    kinds = [signal["type"] for signal in signals]

    assert kinds == ["reviews_unanswered_complaint"], (
        "в поводы просочилось лишнее: crm_widget — не повод, site_hiring_sales старше"
        f" 60 дней, reviews_missed_lead без цитаты. Получено: {kinds}"
    )


def test_company_without_pitchable_signals_stays_in_candidates(leads_db):
    db = leads_db
    _seed(db)

    found = leads_source.candidates(db, RULES)
    without_pitch = [lead for lead in found if lead["company_id"] == "c_phone"]

    assert without_pitch, "лид без поводов исчез из выдачи вместо stop=true у агента"
    assert without_pitch[0]["seed"]["signals"] == [], "непригодный сигнал попал в seed"


def test_candidates_follow_f19_and_f21(leads_db):
    db = leads_db
    _seed(db)
    found = leads_source.candidates(db, RULES, limit=10)
    by_company = {row["company_id"]: row for row in found}

    assert "c_ok" in by_company, "лид с WhatsApp не попал в отбор"
    assert "c_no_channel" not in by_company, "лид без канала попал в отбор (F19)"
    assert "c_suppressed" not in by_company, "лид из suppression попал в отбор (F21)"
    assert "c_short_number" not in by_company, "сервисный короткий номер сошёл за канал"
    assert "c_no_intent" not in by_company, "компания без intent попала в отбор"

    lead = by_company["c_ok"]
    assert lead["thread_id"] == "+77010000001", lead["thread_id"]
    assert lead["channel_kind"] == "whatsapp", lead["channel_kind"]
    assert lead["seed"]["name"] == "Ромашка", lead["seed"]
    assert lead["seed"]["dossier"]["summary"] == "бухгалтерия", lead["seed"]
    assert lead["seed"]["dossier"]["approach"] == "заходить через рост", lead["seed"]
    # crm_widget остаётся сигналом и продолжает поднимать лид в очереди по
    # intent_score, но поводом для письма не является: «у вас на сайте виджет
    # CRM» — не боль, и цитировать в первом касании нечего.
    assert [s["type"] for s in lead["seed"]["signals"]] == [], lead["seed"]

    assert leads_source.is_suppressed(db, "+77010000002"), "отказ не виден по handle"
    assert not leads_source.is_suppressed(db, "+77010000001"), "лишний handle в отказах"

    assert [row["company_id"] for row in found] == ["c_ok", "c_phone"], \
        "порядок отбора не по intent"
    db.close()


def test_candidates_without_limit_returns_everyone(leads_db):
    """limit=None — вызывающая сторона (operations.open_new_threads) сама режет
    список после фильтра «уже есть тред», и не может заранее знать, сколько
    кандидатов из начала списка тот фильтр отсеет."""
    db = leads_db
    _seed(db)
    found = leads_source.candidates(db, RULES)
    assert [row["company_id"] for row in found] == ["c_ok", "c_phone"]
    db.close()