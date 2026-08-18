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

import agent
import config
import leads_source
import thread_store
from schemas.outreach import BANNED, MAX_CHARS, Draft

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
    assert [s["type"] for s in lead["seed"]["signals"]] == ["crm_widget"], lead["seed"]

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
        db.execute("INSERT INTO signals VALUES (?, 'crm_widget', '2026-08-01', 3.0, ?, ?)",
                   (company_id, "виджет Bitrix24", "https://romashka.kz/"))

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


def check_threads():
    """Черновик не становится историей, пока оператор не подтвердил отправку.

    Это и есть протокол системы 2: если бы черновик попадал в историю сразу,
    следующий ход агента строился бы на сообщении, которого лид не получал.
    """
    db = thread_store.connect(":memory:")
    seed = {"name": "Ромашка", "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"}]}

    assert thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред не открылся"
    assert not thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред открылся дважды"
    assert thread_store.thread(db, "+77010000001")["seed"] == seed, "seed не пережил запись"

    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте, ...", "crm_widget")
    assert thread_store.history(db, "+77010000001") == [], \
        "неподтверждённый черновик попал в историю"
    assert thread_store.pending_draft(db, "+77010000001")["message_id"] == message_id

    thread_store.confirm(db, message_id, "Здравствуйте! Правленый оператором текст")
    assert thread_store.pending_draft(db, "+77010000001") is None, "черновик остался висеть"
    history = thread_store.history(db, "+77010000001")
    assert [m["text"] for m in history] == ["Здравствуйте! Правленый оператором текст"], history
    assert history[0]["role"] == "outgoing", history[0]

    # Исходный черновик обязан пережить правку: разница между предложенным и
    # отправленным — единственная бесплатная разметка для калибровки промпта.
    stored = db.execute("SELECT draft_text FROM messages WHERE message_id = ?", (message_id,))
    assert stored.fetchone()[0] == "Здравствуйте, ...", "черновик затёрт правкой оператора"

    thread_store.add_incoming(db, "+77010000001", "а сколько это стоит?")
    roles = [m["role"] for m in thread_store.history(db, "+77010000001")]
    assert roles == ["outgoing", "incoming"], roles
    assert thread_store.used_angles(db, "+77010000001") == ["crm_widget"], \
        "угол отправленного сообщения потерян — follow-up повторит его"
    assert thread_store.silent_days(db, "+77010000001", thread_store.now()) == 0
    db.close()
    print("  threads: черновик отделён от отправленного, углы и правки целы")


def check_schema():
    """Слоп не проходит схему, а не «не рекомендуется промптом».

    Запрет живёт в валидаторе намеренно: with_structured_output повторит вызов
    на невалидном ответе, а инструкция в промпте была бы просьбой, которую
    модель нарушает ровно в тех случаях, ради которых написана система 2.
    """
    good = Draft(text="Здравствуйте! Увидел на сайте виджет Bitrix24...",
                 angle="crm_widget", stop=False)
    assert good.angle == "crm_widget"

    for slop in ("Просто напоминаю о себе", "just checking in on this", "Поднимаю наверх"):
        try:
            Draft(text=slop, angle="followup", stop=False)
        except ValueError:
            continue
        raise AssertionError(f"пустой follow-up прошёл схему: {slop!r}")

    try:
        Draft(text="а" * (MAX_CHARS + 1), angle="crm_widget", stop=False)
    except ValueError:
        pass
    else:
        raise AssertionError("сообщение длиннее потолка прошло схему")

    assert Draft(text="Здравствуйте!", angle="none").stop is False, "stop по умолчанию не False"
    print(f"  schema: {len(BANNED)} запрещённых фраз, потолок {MAX_CHARS} символов")


def check_prompt():
    """Промпт хода: контекст лида, состоявшаяся переписка и задача — и ничего сверх."""
    seed = {
        "name": "Ромашка", "city": "almaty", "industry": "бухгалтерия",
        "why_now": "ищет клиентов", "quote": "оставьте заявку",
        "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"},
                    {"type": "ads_platform", "quote": "Google Ads"}],
    }
    history = [
        {"role": "outgoing", "text": "Первое сообщение", "angle": "crm_widget"},
        {"role": "incoming", "text": "а сколько это стоит?", "angle": None},
    ]

    text = agent.prompt(seed, history, agent.REPLY)
    assert "Ромашка" in text and "бухгалтерия" in text, "контекст лида не попал в промпт"
    assert "виджет Bitrix24" in text, "цитата сигнала потеряна"
    assert "а сколько это стоит?" in text, "ответ лида не попал в промпт"
    assert agent.REPLY in text, "задача хода не попала в промпт"

    # Углы follow-up: использованный не предлагается второй раз, иначе «новый
    # повод» окажется тем же самым, только другими словами.
    assert agent.unused_angles(seed, ["crm_widget"]) == ["ads_platform"]
    assert agent.unused_angles(seed, ["crm_widget", "ads_platform"]) == []
    assert "3" in agent.followup_task(3, ["ads_platform"]), "в follow-up не видно, сколько молчат"

    # Системная роль несёт оффер из конфига: без него модель напишет письмо про
    # услугу, которой у нас нет.
    fake = FakeModel(Draft(text="Здравствуйте!", angle="ads_platform"))
    result = agent.draft(fake, seed, history, agent.REPLY, offer=CONFIG["offer"]["text"])
    assert result.angle == "ads_platform", result
    assert fake.seen[0][0] == "system", fake.seen[0]
    assert CONFIG["offer"]["text"].strip()[:40] in fake.seen[0][1], "оффер не дошёл до модели"
    print("  prompt: контекст, история и оффер на месте, углы не повторяются")


class FakeModel:
    """Заглушка вместо сети: проверяем, что уходит в модель, а не что она вернёт."""

    def __init__(self, answer):
        self.answer, self.seen = answer, None

    def invoke(self, messages):
        self.seen = messages
        return self.answer


SECTIONS = {
    "schema": check_schema,
    "prompt": check_prompt,
    "threads": check_threads,
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
