# Writer (Система 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать `writer/` — систему 2, которая по данным `collector/db/leads.db` ведёт тред переписки на каждого лида и предлагает оператору черновик следующего сообщения в WhatsApp.

**Architecture:** Тред = строка в `writer/threads.db` плюс её сообщения. Один ход переписки = один вызов модели со structured output (`Draft`), без langgraph и без цикла tool-calling: инструментов у агента нет. Черновик не попадает в историю, пока оператор не подтвердил отправку, — поэтому память живёт в собственной таблице `messages`, а не в чекпойнтере. Writer читает базу collector'а и никогда в неё не пишет; отказы уходят существующим эндпоинтом collector'а.

**Tech Stack:** Python 3.13, uv, sqlite3 (stdlib), tomllib (stdlib), pydantic, langchain-openrouter, FastAPI (только тонкий роутер), Next.js 16 (существующая консоль).

**Spec:** `docs/superpowers/specs/2026-08-16-writer-design.md`

## Global Constraints

- Python `>=3.13`, запуск через `uv run` из каталога `writer/`.
- Зависимости writer'а: только `langchain`, `langchain-openrouter` (+ stdlib). **langgraph и langgraph-checkpoint-sqlite не добавляются** — решение спеки.
- Writer **никогда не пишет** в `collector/db/leads.db`. Единственное, что он туда шлёт, — ничего: отказ оператор оформляет существующим `POST /api/suppression` collector'а.
- `writer/threads.db` — невосстановимый слой (как `collector/data/raw/`): схема создаётся через `CREATE TABLE IF NOT EXISTS`, `DROP` в writer'е запрещён, база в git не кладётся.
- Тестов в привычном смысле в проекте нет: тестовый набор — `writer/scripts/check.py` с разделами, ассерты вместо фреймворка, сети и LLM в нём быть не должно.
- Конфигурация только в `writer/config.toml`, читается через `tomllib`. Ничего из неё не хардкодится в коде.
- F19 (нет канала — нет лида) и F21 (suppression до касания) воспроизводятся SQL-запросом writer'а, без импорта кода collector'а.
- Комментарии и строки — по-русски, в стиле `collector/` (объясняют «почему», а не «что»).

### Отклонения от спеки (осознанные, вносить как есть)

1. `Message` и `RefusalMark` как pydantic-модели не появляются: строка переписки — это `dict` из sqlite, а отказ уже полностью умеет collector (`services/suppression.py`, `RefusalForm` в `LeadCard.tsx`). Вторая модель поверх готового flow'а ничего не добавляет.
2. В спеке `sent_text` у incoming — `null`. Здесь текст лида кладётся именно в `sent_text`, а `draft_text` остаётся пустым. Тогда история треда — это ровно `WHERE sent_text IS NOT NULL`: один предикат вместо двух, и «состоявшееся» отличается от «предложенного» одним полем.
3. `writer/api.py` называется `writer/web.py` — имя `api` уже занято модулем `collector/api.py`, и при монтировании роутера они бы столкнулись в `sys.modules`.
4. Команда `scripts.write` не добавляется в белый список `collector/routes/runs.py`: у неё другой рабочий каталог, а первые сообщения оператор всё равно запускает кнопкой из карточки.

---

## File Structure

**Создаётся:**

| Файл | Ответственность |
|---|---|
| `writer/pyproject.toml` | uv-проект системы 2, две зависимости |
| `writer/config.toml` | оффер, модель, каденция follow-up, пути к базам |
| `writer/config.py` | чтение config.toml; пути резолвятся относительно `writer/`, а не cwd |
| `writer/schemas/outreach.py` | `Draft` — что модель обязана вернуть, включая запрет слопа |
| `writer/thread_store.py` | `threads.db`: схема и ходы по переписке (draft / confirm / incoming) |
| `writer/leads_source.py` | чтение `collector/db/leads.db`: кандидаты, каналы, seed, suppression |
| `writer/agent.py` | системный промпт, сборка промпта хода, один вызов модели |
| `writer/scripts/write.py` | сетевая сторона: первые сообщения для топ-N лидов |
| `writer/scripts/check.py` | тестовый набор: разделы `schema`, `threads`, `leads`, `prompt` |
| `writer/web.py` | FastAPI-роутер `/api/threads/*` для консоли оператора |

**Меняется:**

| Файл | Что |
|---|---|
| `collector/api.py` | монтирует роутер writer'а (пять строк склейки) |
| `frontend/app/api.ts` | типы и вызовы `/api/threads/*` |
| `frontend/app/LeadCard.tsx` | секция «Переписка» в карточке лида |
| `frontend/app/globals.css` | стили секции переписки |
| `.gitignore` | `writer/threads.db` |
| `CLAUDE.md` | секция про систему 2 |

---

## Task 1: Каркас writer/ и отбор лидов

**Files:**
- Create: `writer/pyproject.toml`, `writer/config.toml`, `writer/config.py`, `writer/leads_source.py`
- Test: `writer/scripts/check.py` (создаётся здесь, разделы дописывают следующие задачи)

**Interfaces:**
- Consumes: `collector/db/schema.sql` (только как форма базы для синтетической проверки), `collector/db/leads.db` (чтение).
- Produces:
  - `config.load() -> dict` — конфиг, где `leads_db` и `threads_db` уже `Path`, резолвнутые от каталога `writer/`.
  - `leads_source.connect(path) -> sqlite3.Connection` — база collector'а, открытая только на чтение.
  - `leads_source.candidates(db, limit) -> list[dict]` с ключами `company_id`, `thread_id`, `channel_kind`, `seed`; `seed` — dict `{name, city, industry, why_now, quote, signals: [{type, quote, url}]}`.
  - `leads_source.seed_of(db, company_id) -> dict | None` — тот же `seed` для одной компании.
  - `leads_source.is_suppressed(db, handle) -> bool`.
  - `leads_source.thread_id_of(db, company_id) -> tuple[str, str] | None` — `(kind, номер)` для одной компании.

- [ ] **Step 1: Создать uv-проект writer/**

`writer/pyproject.toml`:

```toml
[project]
name = "writer"
version = "0.1.0"
description = "Система 2: агентная персонализация исходящей переписки"
requires-python = ">=3.13"
dependencies = [
    "langchain>=1.3.15",
    "langchain-openrouter>=0.2.8",
]
```

Проверить, что окружение ставится: `cd writer && uv sync` — ожидается создание `writer/.venv` без ошибок.

- [ ] **Step 2: Написать config.toml**

`writer/config.toml`:

```toml
# Единственное место конфигурации системы 2, по образцу collector/config.toml.
# Пути относительны каталогу writer/, а не текущему: тот же конфиг читается и из
# writer/ (uv run -m scripts.write), и из collector/ (uvicorn монтирует роутер).

leads_db = "../collector/db/leads.db"
threads_db = "threads.db"

[llm]
# Та же модель, что у classify: проверена на русских текстах и дёшева.
model = "deepseek/deepseek-v4-flash"
# Не 0, как в classify: там извлечение фактов из готового текста, здесь —
# сообщение живому человеку, и нулевая температура даёт одинаковые письма.
temperature = 0.7
# Сколько лидов берёт scripts.write за прогон без аргумента.
top_n = 10

[cadence]
# Молчание: на какой день предлагать follow-up. Длина списка — сколько
# follow-up'ов всего: третий раз человеку, который дважды промолчал, не пишем.
followup_days = [3, 7]

[offer]
# Оффер живёт текстом, а не в коде: смена продукта не должна требовать правки
# промпта. Модель обязана опираться на него и не выдумывать деталей сверх.
text = """
Мы строим систему исходящего лидогена под ключ: находим компании, которым
услуга нужна прямо сейчас (по вакансиям, рекламе, сайту и соцсетям), пишем
каждому персонально по его же ситуации и доводим до встречи.
Клиент даёт профиль своего покупателя и получает встречи в календаре, а не
список контактов. Работаем по Казахстану, первые результаты — за 2-3 недели.
"""
```

- [ ] **Step 3: Написать config.py**

`writer/config.py`:

```python
"""Конфиг системы 2. Пути резолвятся от каталога writer/, а не от cwd.

Тот же файл читается из двух рабочих каталогов: из writer/ (scripts.write) и из
collector/ (uvicorn, который монтирует роутер writer'а). Относительный путь
означал бы в этих двух случаях разные базы, и переписка тихо ушла бы не туда.
"""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent


def load():
    config = tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
    config["leads_db"] = (HOME / config["leads_db"]).resolve()
    config["threads_db"] = (HOME / config["threads_db"]).resolve()
    return config
```

- [ ] **Step 4: Написать провальную проверку отбора**

`writer/scripts/check.py` (файл создаётся целиком; следующие задачи дописывают в него разделы и в `SECTIONS`):

```python
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
```

- [ ] **Step 5: Запустить и убедиться, что падает**

Run: `cd writer && uv run -m scripts.check leads`
Expected: FAIL — `ModuleNotFoundError: No module named 'leads_source'`

- [ ] **Step 6: Написать leads_source.py**

`writer/leads_source.py`:

```python
"""Кандидаты из базы collector'а. Только чтение — писать туда нельзя ничем.

Отбор повторён запросом, а не импортом report.py: система 2 живёт в своём
окружении и не должна падать оттого, что у collector'а поехали зависимости.
Цена решения — вторая копия правил F19/F21, и держит её честной раздел leads в
scripts/check.py: он гоняет отбор на синтетической базе формы collector'а.

Канал только один из двух: writer пишет в WhatsApp, а туда годится и номер,
собранный как phone. Почта из приоритета исключена намеренно — 2GIS её почти не
отдаёт, и система 3 для неё ещё не построена.
"""

import re
import sqlite3

CHANNEL_PRIORITY = ("whatsapp", "phone")

# Короче этого 2GIS отдаёт не телефон компании, а сервисный короткий номер
# (1400, 5151): написать в WhatsApp по нему нельзя.
MIN_PHONE_DIGITS = 10

CANDIDATES = (
    "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
    "       p.industry, p.why_now, p.quote"
    " FROM companies c JOIN scores s USING (company_id)"
    " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
    " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    " LEFT JOIN profiles p USING (company_id)"
    " WHERE s.intent_score > 0"
    " ORDER BY s.intent_score DESC, s.fit_score DESC, c.company_id"
)


def connect(path):
    """Только чтение: база collector'а пересобирается через DROP, и любая запись
    сюда исчезнет на ближайшем build.py, успев при этом заблокировать сборку."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def candidates(db, limit):
    """Лиды по убыванию intent, у которых есть номер и нет отказа."""
    suppressed = suppression_handles(db)
    channels = channels_by_company(db)
    found = []
    for company_id, name, city, industry, why_now, quote in db.execute(CANDIDATES):
        channel = best_channel(channels.get(company_id, []), suppressed)
        if not channel:
            continue
        found.append({
            "company_id": company_id,
            "thread_id": channel[1],
            "channel_kind": channel[0],
            "seed": {
                "name": name,
                "city": city,
                "industry": industry,
                "why_now": why_now,
                "quote": quote,
                "signals": signals_of(db, company_id),
            },
        })
        if len(found) == limit:
            break
    return found


def thread_id_of(db, company_id):
    """Канал одной компании — карточке в вебе остальные восемьсот не нужны."""
    suppressed = suppression_handles(db)
    channels = channels_by_company(db, company_id)
    return best_channel(channels.get(company_id, []), suppressed)


def seed_of(db, company_id):
    """Контекст лида для затравки треда. None, если компания исчезла из базы."""
    row = db.execute(CANDIDATES.replace(" WHERE s.intent_score > 0",
                                        " WHERE c.company_id = ?"), (company_id,)).fetchone()
    if not row:
        return None
    _, name, city, industry, why_now, quote = row
    return {
        "name": name, "city": city, "industry": industry,
        "why_now": why_now, "quote": quote, "signals": signals_of(db, company_id),
    }


def signals_of(db, company_id):
    """Сигналы системы 1 — они же углы для follow-up: у каждого своя цитата."""
    rows = db.execute(
        "SELECT type, quote, url FROM signals WHERE company_id = ?"
        " ORDER BY weight DESC, observed_at DESC, type",
        (company_id,),
    )
    return [{"type": kind, "quote": quote, "url": url} for kind, quote, url in rows]


def channels_by_company(db, company_id=None):
    narrowing = " WHERE l.company_id = ?" if company_id else ""
    arguments = (company_id,) if company_id else ()
    rows = db.execute(
        "SELECT DISTINCT l.company_id, k.kind, k.handle FROM contacts k"
        " JOIN company_links l USING (branch_id)" + narrowing +
        " ORDER BY l.company_id, k.kind, k.handle",
        arguments,
    )
    grouped = {}
    for company, kind, handle in rows:
        grouped.setdefault(company, []).append((kind, dialable(handle)))
    return grouped


def dialable(handle):
    """+7XXXXXXXXXX из чего угодно: 2GIS отдаёт WhatsApp ссылкой wa.me с зашитым
    чужим приветствием, а thread_id обязан быть одинаков для ссылки и для номера
    — иначе один и тот же человек получит два независимых треда."""
    digits = re.sub(r"\D", "", (handle or "").split("?")[0])
    return f"+{digits}" if len(digits) == 11 else handle


def best_channel(channels, suppressed):
    for kind in CHANNEL_PRIORITY:
        for channel_kind, handle in channels:
            if channel_kind != kind or handle in suppressed:
                continue
            if len(re.sub(r"\D", "", handle)) < MIN_PHONE_DIGITS:
                continue
            return kind, handle
    return None


def is_suppressed(db, handle):
    """Проверяется перед каждым ходом, а не только при отборе: отказ мог прийти
    после того, как тред открыли (F21)."""
    return bool(db.execute(
        "SELECT 1 FROM suppression WHERE handle = ?", (handle,)
    ).fetchone())


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM suppression")}
```

- [ ] **Step 7: Запустить проверку — должна пройти**

Run: `cd writer && uv run -m scripts.check leads`
Expected: PASS, строка вида `leads: отобрано 2, F19 и F21 соблюдены`

- [ ] **Step 8: Закрыть threads.db от git**

В `.gitignore`, после блока про `collector/db/leads.db`, добавить:

```gitignore
# Переписка системы 2: невосстановима (как data/raw/), бэкапится отдельно
writer/threads.db
```

- [ ] **Step 9: Коммит**

```bash
git add writer/pyproject.toml writer/uv.lock writer/config.toml writer/config.py \
        writer/leads_source.py writer/scripts/check.py .gitignore
git commit -m "feat(writer): каркас системы 2 и отбор лидов из базы collector'а"
```

---

## Task 2: Хранилище переписки

**Files:**
- Create: `writer/thread_store.py`
- Modify: `writer/scripts/check.py` (раздел `threads`)

**Interfaces:**
- Consumes: `config.load()["threads_db"]`.
- Produces:
  - `thread_store.connect(path) -> sqlite3.Connection` (схема создаётся, если её нет)
  - `open_thread(db, thread_id, company_id, seed) -> bool` — False, если тред уже был
  - `thread(db, thread_id) -> dict | None` с ключами `thread_id`, `company_id`, `seed`, `created_at`
  - `history(db, thread_id) -> list[dict]` — только состоявшееся, ключи `role`, `text`, `angle`, `sent_at`
  - `pending_draft(db, thread_id) -> dict | None` — ключи `message_id`, `draft_text`, `angle`, `created_at`
  - `add_draft(db, thread_id, text, angle) -> int` (message_id)
  - `confirm(db, message_id, sent_text) -> None`
  - `add_incoming(db, thread_id, text) -> None`
  - `used_angles(db, thread_id) -> list[str]`
  - `silent_days(db, thread_id, today) -> int | None`

- [ ] **Step 1: Написать провальную проверку**

Добавить в `writer/scripts/check.py` (импорт `import thread_store` рядом с остальными, раздел — в `SECTIONS`):

```python
def check_threads():
    """Черновик не становится историей, пока оператор не подтвердил отправку.

    Это и есть протокол системы 2: если бы черновик попадал в историю сразу,
    следующий ход агента строился бы на сообщении, которого лид не получал.
    """
    db = thread_store.connect(":memory:")
    seed = {"name": "Ромашка", "signals": [{"type": "vacancy_sales", "quote": "нужен продавец"}]}

    assert thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред не открылся"
    assert not thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред открылся дважды"
    assert thread_store.thread(db, "+77010000001")["seed"] == seed, "seed не пережил запись"

    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте, ...", "vacancy_sales")
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
    assert thread_store.used_angles(db, "+77010000001") == ["vacancy_sales"], \
        "угол отправленного сообщения потерян — follow-up повторит его"
    assert thread_store.silent_days(db, "+77010000001", thread_store.now()) == 0
    db.close()
    print("  threads: черновик отделён от отправленного, углы и правки целы")
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `cd writer && uv run -m scripts.check threads`
Expected: FAIL — `ModuleNotFoundError: No module named 'thread_store'`

- [ ] **Step 3: Написать thread_store.py**

`writer/thread_store.py`:

```python
"""Переписка: threads.db. Невосстановимый слой системы 2.

Единственное отличие от базы collector'а — и оно важное: схема создаётся через
CREATE TABLE IF NOT EXISTS, а не DROP. leads.db пересобирается из raw/ за
секунды, а переписку восстановить неоткуда: она существует только здесь.
Отсюда же запрет на миграции через пересоздание — таблицы правятся ALTER'ом.

Память агента — эта таблица. Строка со статусом «черновик» (sent_text пуст) в
историю не попадает: агент должен видеть то, что лид получил, а не то, что мы
ему предлагали отправить.
"""

import json
import sqlite3
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,   -- номер WhatsApp, +7XXXXXXXXXX
  company_id TEXT NOT NULL,
  seed       TEXT NOT NULL,      -- json: контекст лида из системы 1 на момент открытия
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads (thread_id),
  role       TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),
  draft_text TEXT,               -- что предложила модель; у incoming пусто
  sent_text  TEXT,               -- что реально ушло или пришло; пусто = черновик
  angle      TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, message_id);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_thread(db, thread_id, company_id, seed):
    """False, если тред уже есть: seed переписывать нельзя — он снимок момента,
    когда мы решили писать, и именно на него ссылается первое сообщение."""
    if thread(db, thread_id):
        return False
    db.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?)",
        (thread_id, company_id, json.dumps(seed, ensure_ascii=False), now()),
    )
    db.commit()
    return True


def thread(db, thread_id):
    row = db.execute(
        "SELECT thread_id, company_id, seed, created_at FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"thread_id": row[0], "company_id": row[1],
            "seed": json.loads(row[2]), "created_at": row[3]}


def thread_of_company(db, company_id):
    row = db.execute(
        "SELECT thread_id FROM threads WHERE company_id = ?", (company_id,)
    ).fetchone()
    return thread(db, row[0]) if row else None


def history(db, thread_id):
    """Состоявшееся: отправленное оператором и пришедшее от лида. Больше ничего."""
    rows = db.execute(
        "SELECT role, sent_text, angle, sent_at FROM messages"
        " WHERE thread_id = ? AND sent_text IS NOT NULL ORDER BY message_id",
        (thread_id,),
    )
    return [{"role": role, "text": text, "angle": angle, "sent_at": sent_at}
            for role, text, angle, sent_at in rows]


def pending_draft(db, thread_id):
    row = db.execute(
        "SELECT message_id, draft_text, angle, created_at FROM messages"
        " WHERE thread_id = ? AND role = 'outgoing' AND sent_text IS NULL"
        " ORDER BY message_id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"message_id": row[0], "draft_text": row[1], "angle": row[2], "created_at": row[3]}


def add_draft(db, thread_id, text, angle):
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, created_at)"
        " VALUES (?, 'outgoing', ?, ?, ?)",
        (thread_id, text, angle, now()),
    )
    db.commit()
    return cursor.lastrowid


def confirm(db, message_id, sent_text):
    """Правленый текст ложится рядом с черновиком, а не вместо него."""
    db.execute(
        "UPDATE messages SET sent_text = ?, sent_at = ? WHERE message_id = ?",
        (sent_text, now(), message_id),
    )
    db.commit()


def add_incoming(db, thread_id, text):
    """Ответ лида. Правкам не подлежит, поэтому draft_text у него пуст."""
    stamp = now()
    db.execute(
        "INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
        " VALUES (?, 'incoming', ?, ?, ?)",
        (thread_id, text, stamp, stamp),
    )
    db.commit()


def used_angles(db, thread_id):
    """Углы уже отправленных сообщений: follow-up обязан взять новый."""
    rows = db.execute(
        "SELECT DISTINCT angle FROM messages WHERE thread_id = ?"
        " AND sent_text IS NOT NULL AND angle IS NOT NULL ORDER BY message_id",
        (thread_id,),
    )
    return [angle for (angle,) in rows]


def silent_days(db, thread_id, today):
    """Сколько дней прошло с последнего касания. None — писать ещё не начинали."""
    last = db.execute(
        "SELECT max(sent_at) FROM messages WHERE thread_id = ? AND sent_text IS NOT NULL",
        (thread_id,),
    ).fetchone()[0]
    if not last:
        return None
    return (date.fromisoformat(today[:10]) - date.fromisoformat(last[:10])).days
```

- [ ] **Step 4: Запустить проверку — должна пройти**

Run: `cd writer && uv run -m scripts.check threads`
Expected: PASS, строка `threads: черновик отделён от отправленного, углы и правки целы`

- [ ] **Step 5: Коммит**

```bash
git add writer/thread_store.py writer/scripts/check.py
git commit -m "feat(writer): threads.db — переписка, где черновик отделён от отправленного"
```

---

## Task 3: Схема ответа модели

**Files:**
- Create: `writer/schemas/outreach.py`
- Modify: `writer/scripts/check.py` (раздел `schema`)

**Interfaces:**
- Produces: `outreach.Draft` — поля `text: str`, `angle: str`, `stop: bool`; `outreach.BANNED: tuple[str, ...]`; `outreach.MAX_CHARS: int`.

- [ ] **Step 1: Написать провальную проверку**

Добавить в `writer/scripts/check.py` (импорт `from schemas.outreach import Draft`, раздел — в `SECTIONS`):

```python
def check_schema():
    """Слоп не проходит схему, а не «не рекомендуется промптом».

    Запрет живёт в валидаторе намеренно: with_structured_output повторит вызов
    на невалидном ответе, а инструкция в промпте была бы просьбой, которую
    модель нарушает ровно в тех случаях, ради которых написана система 2.
    """
    good = Draft(text="Здравствуйте! Увидел вакансию менеджера по продажам...",
                 angle="vacancy_sales", stop=False)
    assert good.angle == "vacancy_sales"

    for slop in ("Просто напоминаю о себе", "just checking in on this", "Поднимаю наверх"):
        try:
            Draft(text=slop, angle="followup", stop=False)
        except ValueError:
            continue
        raise AssertionError(f"пустой follow-up прошёл схему: {slop!r}")

    try:
        Draft(text="а" * (MAX_CHARS + 1), angle="vacancy_sales", stop=False)
    except ValueError:
        pass
    else:
        raise AssertionError("сообщение длиннее потолка прошло схему")

    assert Draft(text="Здравствуйте!", angle="none").stop is False, "stop по умолчанию не False"
    print(f"  schema: {len(BANNED)} запрещённых фраз, потолок {MAX_CHARS} символов")
```

Импорты для раздела: `from schemas.outreach import BANNED, MAX_CHARS, Draft`.

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `cd writer && uv run -m scripts.check schema`
Expected: FAIL — `ModuleNotFoundError: No module named 'schemas.outreach'`

- [ ] **Step 3: Написать схему**

`writer/schemas/outreach.py`:

```python
"""Что модель обязана вернуть на каждый ход переписки.

Схема узкая по той же причине, что и CompanyProfile у системы 1: решение
«писать или не писать» принимает человек, модель только предлагает текст.
Поле stop — не решение, а сигнал оператору: «данных для нового повода нет».
"""

from pydantic import BaseModel, Field, field_validator

# Фразы, ради отсутствия которых и написана система 2. Follow-up без нового
# повода — это тот же шаблон, отправленный второй раз, и именно он превращает
# исходящую переписку в спам. Список пополняется по живым прогонам.
BANNED = (
    "checking in",
    "just bumping",
    "просто напоминаю",
    "напоминаю о себе",
    "поднимаю наверх",
    "хотел узнать, видели ли вы",
)

# Длиннее этого сообщение в WhatsApp не читают: оно приходит одним экраном.
MAX_CHARS = 700


class Draft(BaseModel):
    text: str = Field(description=(
        "сообщение в WhatsApp этому человеку: по-русски, на «вы», без приветственных"
        " шаблонов и без списков. Опирается на факт из данных о компании"
    ))
    angle: str = Field(description=(
        "чем цепляем — тип сигнала из данных (vacancy_sales, ads_platform, ig_promo)"
        " или 'answer', если это ответ на реплику лида"
    ))
    stop: bool = Field(False, description=(
        "true, если писать не о чем: нового повода нет или лид явно отказался"
    ))

    @field_validator("text")
    @classmethod
    def without_slop(cls, text):
        lowered = text.lower()
        used = [phrase for phrase in BANNED if phrase in lowered]
        if used:
            raise ValueError(
                f"пустое напоминание вместо нового повода: {used}. "
                "Каждое сообщение обязано нести факт о компании, которого не было раньше"
            )
        if len(text) > MAX_CHARS:
            raise ValueError(f"{len(text)} символов при потолке {MAX_CHARS}")
        return text
```

- [ ] **Step 4: Запустить проверку — должна пройти**

Run: `cd writer && uv run -m scripts.check schema`
Expected: PASS, строка вида `schema: 6 запрещённых фраз, потолок 700 символов`

- [ ] **Step 5: Коммит**

```bash
git add writer/schemas/outreach.py writer/scripts/check.py
git commit -m "feat(writer): схема Draft — запрет пустых follow-up на уровне валидации"
```

---

## Task 4: Ход переписки

**Files:**
- Create: `writer/agent.py`
- Modify: `writer/scripts/check.py` (раздел `prompt`)

**Interfaces:**
- Consumes: `outreach.Draft`, `config.load()`, seed из `leads_source`, историю из `thread_store`.
- Produces:
  - `agent.model(config) -> Runnable` — `ChatOpenRouter(...).with_structured_output(Draft)`
  - `agent.draft(llm, seed, history, task) -> Draft`
  - `agent.prompt(seed, history, task) -> str`
  - `agent.FIRST: str`, `agent.REPLY: str`
  - `agent.followup_task(days, unused_angles) -> str`
  - `agent.unused_angles(seed, used) -> list[str]`

- [ ] **Step 1: Написать провальную проверку**

Добавить в `writer/scripts/check.py` (импорт `import agent`, раздел — в `SECTIONS`):

```python
def check_prompt():
    """Промпт хода: контекст лида, состоявшаяся переписка и задача — и ничего сверх."""
    seed = {
        "name": "Ромашка", "city": "almaty", "industry": "бухгалтерия",
        "why_now": "ищет клиентов", "quote": "оставьте заявку",
        "signals": [{"type": "vacancy_sales", "quote": "нужен менеджер по продажам"},
                    {"type": "ads_platform", "quote": "Google Ads"}],
    }
    history = [
        {"role": "outgoing", "text": "Первое сообщение", "angle": "vacancy_sales"},
        {"role": "incoming", "text": "а сколько это стоит?", "angle": None},
    ]

    text = agent.prompt(seed, history, agent.REPLY)
    assert "Ромашка" in text and "бухгалтерия" in text, "контекст лида не попал в промпт"
    assert "нужен менеджер по продажам" in text, "цитата сигнала потеряна"
    assert "а сколько это стоит?" in text, "ответ лида не попал в промпт"
    assert agent.REPLY in text, "задача хода не попала в промпт"

    # Углы follow-up: использованный не предлагается второй раз, иначе «новый
    # повод» окажется тем же самым, только другими словами.
    assert agent.unused_angles(seed, ["vacancy_sales"]) == ["ads_platform"]
    assert agent.unused_angles(seed, ["vacancy_sales", "ads_platform"]) == []
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
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `cd writer && uv run -m scripts.check prompt`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 3: Написать agent.py**

`writer/agent.py`:

```python
"""Один ход переписки — один вызов модели. Ни графа, ни цикла tool-calling.

Инструментов у агента нет: весь контекст приходит из системы 1 и из истории
треда, а ответ обязан лечь в Draft. Цикл агента здесь нечего крутить, поэтому
вместо фреймворка — три функции и with_structured_output, как в classify.py.

Память треда живёт в thread_store, а не в чекпойнтере: протокол
draft -> правка оператора -> отправка требует, чтобы неподтверждённое сообщение
не попадало в историю, а чекпойнтер дописывает ответ модели в состояние сам.
"""

from langchain_openrouter import ChatOpenRouter

from schemas.outreach import Draft

MAX_RETRIES = 2

# Рассуждение выключено по той же причине, что в classify: задача — написать
# короткое сообщение по готовым фактам, а не рассуждать. Ответ приходит за
# секунды, reasoning-токены не оплачиваются.
REASONING = {"enabled": False}

SYSTEM = """Ты пишешь исходящие сообщения в WhatsApp от лица команды, которая предлагает:
{offer}

Правила, которые не обсуждаются:
- Одно сообщение — один конкретный факт об этой компании, взятый из данных ниже.
  Без факта сообщение не отправляется: пиши stop=true.
- Пиши так, как пишет человек в мессенджере: 3-5 коротких предложений, на «вы»,
  без списков, без «Надеюсь, у вас всё хорошо», без слова «уникальный».
- Не выдумывай фактов о компании. Всё, чего нет в данных, не существует.
- Заканчивай одним понятным вопросом, на который легко ответить «да» или «нет».
- Каждое следующее сообщение несёт новый повод. Напоминание о предыдущем письме
  поводом не является."""


def model(config):
    """Клиент модели. Ключ ChatOpenRouter берёт из окружения сам — отсюда
    запуск через --env-file .env, как у classify.py."""
    return ChatOpenRouter(
        model=config["llm"]["model"],
        temperature=config["llm"]["temperature"],
        max_retries=MAX_RETRIES,
        reasoning=REASONING,
    ).with_structured_output(Draft, method="json_schema")


def draft(llm, seed, history, task, offer=""):
    return llm.invoke([
        ("system", SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, task)),
    ])


FIRST = (
    "Задача: первое сообщение этой компании. Возьми самый сильный сигнал, назови"
    " конкретный факт о ней и спроси, актуально ли это сейчас."
)

REPLY = (
    "Задача: ответить на последнюю реплику лида. Отвечай по существу вопроса, не"
    " повторяй уже сказанное и не начинай заново с приветствия."
)


def followup_task(days, unused):
    """Follow-up без нового угла запрещён: если углы кончились, честнее stop."""
    if not unused:
        return (
            f"Задача: лид молчит {days} дней, и неиспользованных поводов больше нет."
            " Верни stop=true и пустое по смыслу сообщение — писать не о чем."
        )
    return (
        f"Задача: лид молчит {days} дней. Напиши сообщение с НОВЫМ поводом —"
        f" возьми угол {unused[0]} из сигналов выше. Про предыдущее сообщение не"
        " упоминай вообще."
    )


def unused_angles(seed, used):
    return [signal["type"] for signal in seed["signals"] if signal["type"] not in used]


def prompt(seed, history, task):
    parts = [
        f"Компания: {seed['name']}",
        f"Город: {seed['city']}",
        f"Чем занимается: {seed['industry'] or 'неизвестно'}",
    ]
    if seed.get("why_now"):
        parts.append(f"Почему пишем сейчас: {seed['why_now']}")
    if seed.get("quote"):
        parts.append(f"Цитата с её сайта: «{seed['quote']}»")
    if seed["signals"]:
        parts.append("Сигналы (это и есть возможные поводы):")
        parts += [f"  {s['type']}: {s['quote'] or ''}".rstrip() for s in seed["signals"]]
    if history:
        parts.append("Переписка:")
        parts += [f"  {'мы' if m['role'] == 'outgoing' else 'они'}: {m['text']}"
                  for m in history]
    parts.append(task)
    return "\n".join(parts)
```

- [ ] **Step 4: Запустить проверку — должна пройти**

Run: `cd writer && uv run -m scripts.check prompt`
Expected: PASS, строка `prompt: контекст, история и оффер на месте, углы не повторяются`

- [ ] **Step 5: Прогнать весь набор**

Run: `cd writer && uv run -m scripts.check`
Expected: PASS всех четырёх разделов, финальная строка `check ok: schema, threads, leads, prompt`

- [ ] **Step 6: Коммит**

```bash
git add writer/agent.py writer/scripts/check.py
git commit -m "feat(writer): ход переписки — один вызов модели на сообщение"
```

---

## Task 5: Первые сообщения из командной строки

**Files:**
- Create: `writer/scripts/write.py`, `writer/.env.example`

**Interfaces:**
- Consumes: `config.load()`, `leads_source.candidates`, `thread_store.open_thread/add_draft`, `agent.model/draft/FIRST`.
- Produces: CLI `uv run --env-file .env -m scripts.write [сколько]`; печатает готовые к правке черновики.

- [ ] **Step 1: Написать scripts/write.py**

`writer/scripts/write.py`:

```python
"""Первые сообщения топ-N лидам. Сетевая сторона системы 2, как classify.py у системы 1.

Тред открывается один раз и переживает пересборку базы collector'а: seed —
снимок момента, когда мы решили писать. Компания, у которой тред уже есть,
пропускается, поэтому повторный запуск ничего не стоит и ничего не портит.

Отправляет по-прежнему человек: скрипт печатает черновики, оператор правит их в
консоли и жмёт «отправлено». Система 3 (доставка) не построена.

Запуск:
  uv run --env-file .env -m scripts.write        топ из config.toml [llm].top_n
  uv run --env-file .env -m scripts.write 3      первые 3
"""

import os
import sys

import agent
import config
import leads_source
import thread_store


def main():
    settings = config.load()
    limit = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else settings["llm"]["top_n"]
    require_api_key()

    leads = leads_source.connect(settings["leads_db"])
    threads = thread_store.connect(settings["threads_db"])
    fresh = [lead for lead in leads_source.candidates(leads, limit * 3)
             if not thread_store.thread(threads, lead["thread_id"])][:limit]
    print(f"писем: {len(fresh)}, модель {settings['llm']['model']}")

    llm = agent.model(settings)
    for number, lead in enumerate(fresh, 1):
        thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
        proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                               offer=settings["offer"]["text"])
        show(number, lead, proposal)
        if not proposal.stop:
            thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)

    leads.close()
    threads.close()
    print("Дальше: открыть консоль оператора и отправить черновики руками")


def require_api_key():
    """Отказать до первого запроса, а не в середине прогона."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit(
            "OPENROUTER_API_KEY пуст.\n"
            "  1) вписать ключ с https://openrouter.ai/keys в writer/.env\n"
            "  2) uv run --env-file .env -m scripts.write"
        )


def show(number, lead, proposal):
    print(f"\n{number}. {lead['seed']['name']} — {lead['thread_id']} ({lead['channel_kind']})")
    if proposal.stop:
        print("   агент советует не писать: повода в данных нет")
        return
    print(f"   угол: {proposal.angle}")
    print("   " + proposal.text.replace("\n", "\n   "))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Написать .env.example**

`writer/.env.example`:

```dotenv
# Ключ OpenRouter: https://openrouter.ai/keys
# Скопировать файл в writer/.env и вписать ключ — .env в git не лежит.
# ChatOpenRouter читает переменную из окружения сам, отсюда --env-file .env.
OPENROUTER_API_KEY=
```

- [ ] **Step 3: Проверить отказ без ключа**

Run: `cd writer && uv run -m scripts.write 1`
Expected: выход с сообщением `OPENROUTER_API_KEY пуст.` и ненулевым кодом — сеть не тронута.

- [ ] **Step 4: Живой прогон на одном лиде**

Скопировать ключ: `cp ../collector/.env .env`

Run: `cd writer && uv run --env-file .env -m scripts.write 1`
Expected: печатается один черновик с углом; в `writer/threads.db` появляются одна строка `threads` и одна `messages` со статусом черновика.

Проверить глазами: сообщение по-русски, ссылается на факт из сигналов, короче 700 символов, без «просто напоминаю».

- [ ] **Step 5: Проверить, что повтор не задваивает треды**

Run: `cd writer && uv run --env-file .env -m scripts.write 1`
Expected: скрипт берёт **следующего** лида, а не повторяет первого: `write N` всегда даёт N новых компаний. У первой компании по-прежнему один тред и один черновик — второго вызова модели на неё не было.

```bash
uv run python -c "
import sqlite3
db = sqlite3.connect('threads.db')
for row in db.execute('select t.thread_id, count(m.message_id) from threads t left join messages m using (thread_id) group by t.thread_id'):
    print(row)
"
```
Expected: у каждого треда ровно одно сообщение.

- [ ] **Step 6: Прогнать проверки**

Run: `cd writer && uv run -m scripts.check`
Expected: PASS, `check ok: schema, threads, leads, prompt`

- [ ] **Step 7: Коммит**

```bash
git add writer/scripts/write.py writer/.env.example
git commit -m "feat(writer): scripts.write — первые сообщения топ-N лидам"
```

---

## Task 6: Консоль оператора

**Files:**
- Create: `writer/web.py`
- Modify: `collector/api.py:19-35`, `frontend/app/api.ts`, `frontend/app/LeadCard.tsx`, `frontend/app/globals.css`, `CLAUDE.md`

**Interfaces:**
- Consumes: `agent`, `config`, `leads_source`, `thread_store`.
- Produces HTTP:
  - `GET /api/threads/{company_id}` → `{thread_id, channel_kind, messages: [{role, text, angle, sent_at}], draft: {message_id, draft_text, angle} | null, stop: bool}`; `thread_id` есть всегда (канал компании), `messages` пуст, пока тред не открыт.
  - `POST /api/threads/{company_id}/draft` тело `{kind: "first" | "reply" | "followup"}` → тот же объект. Единственный эндпоинт, который ходит в сеть.
  - `POST /api/threads/{company_id}/sent` тело `{text}` → тот же объект.
  - `POST /api/threads/{company_id}/incoming` тело `{text}` → тот же объект.

- [ ] **Step 1: Написать роутер**

`writer/web.py`:

```python
"""HTTP над threads.db для консоли оператора. Монтируется в collector/api.py.

Файл называется web.py, а не api.py: имя api уже занято модулем collector'а, и
два одноимённых модуля в одном процессе столкнулись бы в sys.modules.

Отказ здесь не оформляется: он уже полностью умеет collector
(POST /api/suppression), а вторая точка входа в юридический контур — это второй
шанс разойтись с suppression.csv. Writer только проверяет отказ перед каждым
ходом (F21).

Проверка: uv run -m web
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import agent
import config
import leads_source
import thread_store

router = APIRouter(prefix="/api/threads")

CONFIG = config.load()

KINDS = ("first", "reply", "followup")


class DraftRequest(BaseModel):
    kind: str


class TextRequest(BaseModel):
    text: str


@router.get("/{company_id}")
def conversation(company_id: str):
    leads, threads = open_stores()
    try:
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/draft")
def make_draft(company_id: str, request: DraftRequest):
    """Единственный эндпоинт, который стоит денег. Тред открывается здесь же."""
    if request.kind not in KINDS:
        raise HTTPException(400, f"ход {request.kind!r} не бывает: {list(KINDS)}")

    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        thread = thread_store.thread(threads, channel[1])
        if not thread:
            seed = leads_source.seed_of(leads, company_id)
            if not seed:
                raise HTTPException(404, f"компании {company_id} нет в базе лидов")
            thread_store.open_thread(threads, channel[1], company_id, seed)
            thread = thread_store.thread(threads, channel[1])

        history = thread_store.history(threads, channel[1])
        task = task_of(request.kind, threads, thread)
        proposal = agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                               offer=CONFIG["offer"]["text"])
        if not proposal.stop:
            thread_store.add_draft(threads, channel[1], proposal.text, proposal.angle)
        return {**state(leads, threads, company_id), "stop": proposal.stop}
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/sent")
def mark_sent(company_id: str, request: TextRequest):
    """Правленый оператором текст становится историей. Черновик остаётся рядом."""
    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        draft = thread_store.pending_draft(threads, channel[1])
        if not draft:
            raise HTTPException(409, "отправлять нечего: черновика нет")
        if not request.text.strip():
            raise HTTPException(400, "пустой текст отправленным не бывает")
        thread_store.confirm(threads, draft["message_id"], request.text.strip())
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


@router.post("/{company_id}/incoming")
def add_incoming(company_id: str, request: TextRequest):
    leads, threads = open_stores()
    try:
        channel = channel_of(leads, company_id)
        if not thread_store.thread(threads, channel[1]):
            raise HTTPException(409, "переписки ещё не было — сначала черновик")
        if not request.text.strip():
            raise HTTPException(400, "пустой ответ лида не бывает")
        thread_store.add_incoming(threads, channel[1], request.text.strip())
        return state(leads, threads, company_id)
    finally:
        leads.close()
        threads.close()


def open_stores():
    return (leads_source.connect(CONFIG["leads_db"]),
            thread_store.connect(CONFIG["threads_db"]))


def channel_of(leads, company_id):
    """Канал компании, он же thread_id. Отказ проверяется здесь — то есть перед
    каждым ходом, а не только при отборе (F21)."""
    channel = leads_source.thread_id_of(leads, company_id)
    if not channel:
        raise HTTPException(409, "писать некуда: нет рабочего номера или стоит отказ")
    return channel


def task_of(kind, threads, thread):
    if kind == "first":
        return agent.FIRST
    if kind == "reply":
        return agent.REPLY
    days = thread_store.silent_days(threads, thread["thread_id"], thread_store.now()) or 0
    unused = agent.unused_angles(thread["seed"], thread_store.used_angles(threads, thread["thread_id"]))
    return agent.followup_task(days, unused)


def state(leads, threads, company_id):
    channel = channel_of(leads, company_id)
    return {
        "thread_id": channel[1],
        "channel_kind": channel[0],
        "messages": thread_store.history(threads, channel[1]),
        "draft": thread_store.pending_draft(threads, channel[1]),
        "stop": False,
    }


def demo():
    """Роутер собирается, конфиг читается, базы открываются — без сети и модели."""
    leads, threads = open_stores()
    assert leads_source.candidates(leads, 1) is not None
    assert thread_store.thread(threads, "нет такого треда") is None
    assert {route.path for route in router.routes} == {
        "/api/threads/{company_id}",
        "/api/threads/{company_id}/draft",
        "/api/threads/{company_id}/sent",
        "/api/threads/{company_id}/incoming",
    }
    leads.close()
    threads.close()
    print("web demo ok — роутер собран, базы открываются")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 2: Проверить роутер без сервера**

Run: `cd writer && uv run -m web`
Expected: `web demo ok — роутер собран, базы открываются`

- [ ] **Step 3: Смонтировать роутер в collector/api.py**

В `collector/api.py` заменить блок импортов (строки 13-22) на:

```python
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import report
from db import lead as store
from routes import leads, runs, suppression
from services import suppression as refusals

# Система 2 живёт своим проектом и своей базой; здесь только склейка, чтобы у
# оператора остались одна консоль и один порт. Каталог добавляется в путь
# целиком: writer импортирует свои модули по коротким именам, как делает и сам
# collector.
sys.path.append(str(Path(__file__).resolve().parent.parent / "writer"))
import web as writer  # noqa: E402
```

И после `app.include_router(suppression.router)` добавить:

```python
app.include_router(writer.router)
```

- [ ] **Step 4: Проверить, что бэкенд поднимается вместе с writer'ом**

Run: `cd collector && uv run python -c "import api; print([r.path for r in api.app.routes if 'threads' in r.path])"`
Expected: печатаются четыре пути `/api/threads/...`

Если падает на `ModuleNotFoundError: langchain_openrouter` — зависимость уже есть в `collector/pyproject.toml`, выполнить `cd collector && uv sync`.

- [ ] **Step 5: Проверить эндпоинты живьём**

Поднять бэкенд: `cd collector && uv run uvicorn api:app --port 8787` (фоном).

Взять company_id первого лида: `curl -s localhost:8787/api/leads?limit=1 | python -c "import json,sys; print(json.load(sys.stdin)['leads'][0]['company_id'])"`

```bash
curl -s localhost:8787/api/threads/<company_id>
curl -s -X POST localhost:8787/api/threads/<company_id>/draft -H 'Content-Type: application/json' -d '{"kind":"first"}'
```

Expected: первый ответ — `messages: []`, `draft: null`; второй — заполненный `draft` с `angle`.

- [ ] **Step 6: Добавить контракт во фронтенд**

В конец `frontend/app/api.ts`:

```ts
export type ThreadMessage = {
  role: "outgoing" | "incoming";
  text: string;
  angle: string | null;
  sent_at: string | null;
};

export type Conversation = {
  thread_id: string;
  channel_kind: string;
  messages: ThreadMessage[];
  draft: { message_id: number; draft_text: string; angle: string | null } | null;
  /** Агент советует не писать: нового повода в данных нет. Решает оператор. */
  stop: boolean;
};

export function fetchConversation(companyId: string) {
  return json<Conversation>(`/api/threads/${encodeURIComponent(companyId)}`);
}

/** Единственный вызов, который стоит денег: один ход = один запрос к модели. */
export function requestDraft(companyId: string, kind: "first" | "reply" | "followup") {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/draft`, { kind });
}

export function markSent(companyId: string, text: string) {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/sent`, { text });
}

export function addIncoming(companyId: string, text: string) {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/incoming`, { text });
}

function post<T>(url: string, body: unknown) {
  return json<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
```

- [ ] **Step 7: Добавить секцию «Переписка» в карточку**

В `frontend/app/LeadCard.tsx` добавить импорты:

```ts
import {
  addIncoming,
  fetchConversation,
  markSent,
  requestDraft,
  type Conversation,
} from "./api";
```

Вставить `<Thread companyId={companyId} />` в `LeadCard` перед `<RefusalForm ... />`, и добавить компонент в конец файла:

```tsx
/** Переписка с лидом. Черновик правится прямо здесь: в историю треда попадает
 *  то, что оператор реально отправил, — иначе следующий ход агента строился бы
 *  на сообщении, которого лид не получал. */
function Thread({ companyId }: { companyId: string }) {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    fetchConversation(companyId)
      .then((data) => !stale && apply(data))
      .catch((error) => !stale && setFailure((error as Error).message));
    return () => {
      stale = true;
    };
  }, [companyId]);

  function apply(data: Conversation) {
    setConversation(data);
    setText(data.draft?.draft_text ?? "");
  }

  async function run(action: () => Promise<Conversation>) {
    setBusy(true);
    setFailure(null);
    try {
      apply(await action());
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!conversation) return <p className="placeholder">Переписка: загрузка…</p>;

  // Ход выбирается по последней реплике: после ответа лида нужен ответ, после
  // нашего сообщения — новый повод. Молчание отличается от диалога только этим.
  const last = conversation.messages[conversation.messages.length - 1];
  const kind = !last ? "first" : last.role === "incoming" ? "reply" : "followup";

  return (
    <section className="thread">
      <h3>Переписка · {conversation.thread_id}</h3>

      <ol className="thread-log">
        {conversation.messages.map((message, index) => (
          <li key={index} className={message.role}>
            <span className="thread-who">{message.role === "outgoing" ? "мы" : "они"}</span>
            <span>{message.text}</span>
          </li>
        ))}
        {conversation.messages.length === 0 && <li className="placeholder">Ещё не писали.</li>}
      </ol>

      {conversation.draft ? (
        <div className="thread-draft">
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={6} />
          <p className="note">
            Угол: <span className="mono">{conversation.draft.angle}</span>. Правьте текст здесь —
            в историю уйдёт отправленный вариант, исходный черновик сохранится рядом.
          </p>
          <button disabled={busy || !text.trim()} onClick={() => run(() => markSent(companyId, text))}>
            Отправлено
          </button>
        </div>
      ) : (
        <button disabled={busy} onClick={() => run(() => requestDraft(companyId, kind))}>
          {kind === "first"
            ? "Черновик первого сообщения"
            : kind === "reply"
              ? "Черновик ответа"
              : "Черновик с новым поводом"}
        </button>
      )}

      {conversation.messages.length > 0 && (
        <form
          className="thread-reply"
          onSubmit={(event) => {
            event.preventDefault();
            run(() => addIncoming(companyId, reply)).then(() => setReply(""));
          }}
        >
          <input
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            placeholder="Ответ лида — вставить как есть"
          />
          <button type="submit" disabled={busy || !reply.trim()}>
            Записать ответ
          </button>
        </form>
      )}

      {conversation.stop && (
        <p className="note">Агент советует не писать: нового повода в данных нет.</p>
      )}
      {failure && <p className="failure">{failure}</p>}
    </section>
  );
}
```

- [ ] **Step 8: Добавить стили**

В конец `frontend/app/globals.css`:

```css
/* Переписка: наша реплика прижата влево, ответ лида — вправо, чтобы очередь
   хода читалась без подписей. */
.thread { border-top: 1px solid var(--line, #e5e5e5); margin-top: 1.5rem; padding-top: 1rem; }
.thread-log { display: flex; flex-direction: column; gap: 0.5rem; list-style: none; padding: 0; }
.thread-log li { display: flex; gap: 0.5rem; max-width: 40rem; }
.thread-log li.incoming { align-self: flex-end; flex-direction: row-reverse; }
.thread-who { font-size: 0.75rem; opacity: 0.6; min-width: 2rem; }
.thread-draft textarea { width: 100%; font: inherit; padding: 0.5rem; }
.thread-reply { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.thread-reply input { flex: 1; font: inherit; padding: 0.4rem; }
```

- [ ] **Step 9: Проверить цикл в браузере**

Поднять оба процесса:

```bash
cd collector && uv run uvicorn api:app --port 8787 --reload
cd frontend && npm run dev
```

Открыть `http://localhost:3000`, выбрать лид и пройти цикл целиком:
1. «Черновик первого сообщения» → появился текст с углом;
2. поправить текст → «Отправлено» → сообщение ушло в историю, поле черновика исчезло;
3. вставить ответ лида → «Записать ответ» → реплика в истории справа;
4. «Черновик с новым поводом» → новый черновик учитывает ответ.

Expected: все четыре шага проходят, ошибок в консоли браузера нет.

- [ ] **Step 10: Прогнать проверки обеих систем**

Run: `cd writer && uv run -m scripts.check`
Expected: `check ok: schema, threads, leads, prompt`

Run: `cd collector && uv run -m scripts.check web && uv run api.py`
Expected: PASS — склейка не сломала выдачу и юридический контур collector'а.

- [ ] **Step 11: Описать систему 2 в CLAUDE.md**

В `CLAUDE.md` заменить строку `2. **AI-персонализация** — письма и цепочки follow-up по данным системы 1. Будет жить в `writer/`. Пока не начато — папка пустая.` на:

```markdown
2. **AI-персонализация** — тред переписки на каждого лида и черновик следующего
   сообщения. Живёт в `writer/` (см. секцию ниже).
```

И добавить перед секцией «Слои данных»:

```markdown
## Архитектура writer/ (система 2)

Отдельный uv-проект: свои зависимости, своя база, своя команда `check`. Читает
`collector/db/leads.db` и **никогда** в неё не пишет — отбор кандидатов (F19,
F21) воспроизведён запросом в `leads_source.py`, а не импортом `report.py`,
чтобы система 2 не падала от чужих зависимостей. Цена — вторая копия правил,
её держит честной раздел `leads` в `writer/scripts/check.py`.

```bash
cd writer
uv run --env-file .env -m scripts.write [сколько]   # первые сообщения топ-N лидам
uv run -m scripts.check [раздел]                    # schema, threads, leads, prompt
```

**`threads.db` — невосстановимый слой**, как `data/raw/` у системы 1: схема
создаётся через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён, в git не лежит.

**Черновик ≠ отправленное.** Ответ модели ложится в `messages.draft_text`, а в
историю треда попадает только `sent_text` — то, что оператор реально отправил
после правки. Историей агента считается ровно `WHERE sent_text IS NOT NULL`:
иначе следующий ход строился бы на сообщении, которого лид не получал. Разница
draft/sent — единственная бесплатная разметка для калибровки промпта.

**Ни langgraph, ни чекпойнтера.** Инструментов у агента нет, на ход нужен один
вызов со structured output — `agent.py` это три функции и `with_structured_output`,
как в `classify.py`. Чекпойнтер дописывал бы неподтверждённый черновик в
состояние сам и дал бы второй источник правды рядом с `messages`.

**Запрет пустых follow-up — в схеме, а не в промпте** (`schemas/outreach.py`):
`with_structured_output` повторит вызов на невалидном ответе, а инструкция в
промпте была бы просьбой, которую модель нарушает именно там, где важно.

**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/web.py`
(`/api/threads/*`), фронтенд получает секцию «Переписка» в карточке лида.
Отказ по-прежнему оформляется эндпоинтом collector'а: вторая точка входа в
юридический контур — второй шанс разойтись с `suppression.csv`.
```

- [ ] **Step 12: Коммит**

```bash
git add writer/web.py collector/api.py frontend/app/api.ts frontend/app/LeadCard.tsx \
        frontend/app/globals.css CLAUDE.md
git commit -m "feat(writer): консоль переписки — черновик, правка, отправка, ответ лида"
```

---

## Что осталось за рамками плана

- **Каденция follow-up никем не выполняется автоматически.** `[cadence].followup_days`
  читается только для текста задачи; кто именно решает, что пора писать, — оператор
  по кнопке. Планировщик появится вместе с системой 3, ему же нужна отправка.
- **Калибровка промпта.** Первые 30 переписок дадут пары draft/sent; править
  `SYSTEM` и `BANNED` по ним, а не раньше.
- **Команда `write` в белом списке `routes/runs.py`** — у неё другой рабочий
  каталог, а из карточки черновик и так запускается кнопкой.
