"""Проверки системы 2. Ассерты, а не фреймворк; ни сети, ни ключей, ни LLM.

Запуск: uv run -m scripts.check [раздел]      без аргумента — все разделы
Разделы: schema (схема ответа модели), threads (переписка), leads (отбор),
prompt (сборка промпта хода).

Ни один раздел не требует собранной базы collector'а: leads строит свою на
:memory: по collector/db/schema.sql. Отсюда главное свойство — набор проходит
на чистом клоне и ловит поломку сразу, а не задним числом пустой выдачей.
"""

import sqlite3
import sys

import config
import leads_source

CONFIG = config.load()


def check_leads():
    """Отбор кандидатов: F19 и F21 воспроизведены запросом, не импортом collector'а."""
    db = synthetic_leads_db()
    found = leads_source.candidates(db, limit=10)
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
    assert lead["seed"]["why_now"] == "ищет клиентов", lead["seed"]
    assert [s["type"] for s in lead["seed"]["signals"]] == ["vacancy_sales"], lead["seed"]

    assert leads_source.is_suppressed(db, "+77010000002"), "отказ не виден по handle"
    assert not leads_source.is_suppressed(db, "+77010000001"), "лишний handle в отказах"

    assert [row["company_id"] for row in found] == ["c_ok", "c_phone"], \
        "порядок отбора не по intent"
    db.close()
    print(f"  leads: отобрано {len(found)}, F19 и F21 соблюдены")


def synthetic_leads_db():
    """База формы collector'а с пятью подготовленными случаями.

    Схема берётся из collector/db/schema.sql, а не переписывается здесь: writer
    не импортирует код collector'а, но форму базы обязан читать из одного места,
    иначе проверка пройдёт на выдуманной таблице.
    """
    db = sqlite3.connect(":memory:")
    db.executescript((CONFIG["leads_db"].parent / "schema.sql").read_text(encoding="utf-8"))
    for company_id, name, intent in (
        ("c_ok", "Ромашка", 6.0),
        ("c_phone", "Лютик", 4.0),
        ("c_no_channel", "Тишина", 5.0),
        ("c_suppressed", "Отказ", 5.5),
        ("c_short_number", "Короткий", 5.0),
        ("c_no_intent", "Пусто", 0.0),
    ):
        branch = f"b_{company_id}"
        db.execute("INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?)",
                   (company_id, name, None, "almaty", "653", "2026-08-01"))
        db.execute("INSERT INTO scores VALUES (?, ?, ?, ?)", (company_id, 5.0, intent, "[]"))
        db.execute("INSERT INTO orgs VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (branch, None, name, name, 1, "almaty", "653", "ул. Абая, 1", None, None))
        db.execute("INSERT INTO company_links VALUES (?, ?, 'self', 1.0)", (company_id, branch))
        db.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?)",
                   (company_id, "модель", "бухгалтерия", None, None,
                    "ищет клиентов", "оставьте заявку", 0.8))
        db.execute("INSERT INTO signals VALUES (?, 'vacancy_sales', '2026-08-01', 3.0, ?, ?)",
                   (company_id, "нужен менеджер по продажам", "https://hh.kz/vacancy/1"))

    contacts = (
        ("b_c_ok", "whatsapp", "https://wa.me/77010000001?text=%D0%9F%D0%B8%D1%88%D1%83"),
        ("b_c_ok", "phone", "+77010000009"),
        ("b_c_phone", "phone", "+77010000003"),
        ("b_c_suppressed", "whatsapp", "https://wa.me/77010000002"),
        ("b_c_short_number", "phone", "1400"),
        ("b_c_no_intent", "phone", "+77010000004"),
    )
    for branch, kind, handle in contacts:
        db.execute("INSERT INTO contacts VALUES (?, ?, ?, NULL)", (branch, kind, handle))
    db.execute("INSERT INTO suppression VALUES ('+77010000002', '2026-08-10', 'просил не писать')")
    db.commit()
    return db


SECTIONS = {
    "leads": check_leads,
}


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(SECTIONS)
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        sys.exit(f"нет раздела {unknown}. Есть: {', '.join(SECTIONS)}")
    for name in wanted:
        SECTIONS[name]()
    print("check ok:", ", ".join(wanted))
