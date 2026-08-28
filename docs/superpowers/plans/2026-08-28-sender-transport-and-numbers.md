# Sender, часть 1: транспорт и пул номеров — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать продукту живые прогретые номера WhatsApp: Node-сервис на Baileys за швом `sender/transport.py`, пул номеров в `state.db`, календарь прогрева и монитор здоровья — всё, с чего часть 2 сможет отправлять.

**Architecture:** Node — тупая труба (сокеты Baileys + четыре HTTP-ручки), все решения в Python. `sender/transport.py` — единственный модуль системы, ходящий в сеть; тест графа импортов это сторожит, как `test_rebuild_import_graph_has_no_network` сторожит `rebuild.py`. Пул и журнал отправок живут в `state.db` рядом с перепиской и отказами: невосстановимый слой, схема через `CREATE TABLE IF NOT EXISTS`, ALTER — через идемпотентный `ensure_column`. Календарь прогрева и выбор номера — чистые функции, принимающие «сейчас» аргументом, поэтому тестируются без сети и без сна.

**Tech Stack:** Python 3.13, FastAPI, `httpx` (async, `MockTransport` в тестах), `sqlite3`, `tomllib`, pytest; Node 22 + `baileys`, `node:http` без фреймворка.

**Spec:** `docs/superpowers/specs/2026-08-28-sender-transport-and-numbers-design.md`
(зонтик: `docs/superpowers/specs/2026-08-23-sender-whatsapp-design.md`)

## Global Constraints

- **`sender/transport.py` — единственная точка выхода в сеть во всей системе 3.** Ни `pool`, ни `warmup`, ни `health`, ни роутеры не импортируют `httpx`. Сторожит `sender/tests/test_import_graph.py`.
- **`DROP` в `state.db` запрещён** — невосстановимый слой. Только `CREATE TABLE IF NOT EXISTS` и `ensure_column`.
- **`config.toml` — единственное место конфигурации.** Пороги, окна, календарь прогрева, лимиты не хардкодятся; читается `tomllib` (stdlib), пути резолвятся от каталога `sender/`, а не от cwd (тот же приём, что `writer/services/config.py`).
- **Секреты — только через `backend/config.py`.** `os.environ` и `--env-file` в коде sender'а не появляются.
- **«Сейчас» — аргумент, не `datetime.now()` внутри.** Все функции календаря, пула и здоровья принимают `now: datetime` параметром; `now()` зовётся один раз на границе (роутер, планировщик).
- **Часовой пояс.** В базе всё UTC, ISO-8601 с точностью до секунд (`timespec="seconds"`), как `thread_store.now()`. `Asia/Almaty` из конфига используется только при вычислении окна отправки — это часть 2.
- **`pool` импортирует `warmup` на уровне модуля; `warmup` импортирует `pool` внутри функции.** Обратное направление даёт цикл импортов и `ImportError` на старте.
- **Тесты без сети.** `transport` в тестах подменяется целиком либо собирается на `httpx.MockTransport`. Живой WhatsApp — ручной смоук-чеклист в Task 5, не pytest.
- **Терминология статусов номера:** `new | warming | active | quarantined | banned`. `banned` — терминальный: прогрев обнуляется полностью.
- Значения календаря прогрева (`socket_delay_hours = 24`, `passive_days = 3`, `internal_ramp = [6, 12, 20, 30, 45, 60]`, `cold_start_day = 11`, `cold_ramp = [5, 10, 15, 20, 25, 30]`, `ceiling = 30`) и порогов здоровья (`window_days = 7`, `min_sample = 20`, `min_delivered_rate = 0.8`, `min_reply_rate = 0.05`, `reconnects_per_day_alert = 5`) копируются в `sender/config.toml` из спеки дословно.

## Отступление от спеки, принятое в этом плане

Спека говорит: «дневной расход по номеру считается `count(*)` по outbox за сутки», а таблицу `outbox` относит к части 2. Но дневной лимит нужен уже здесь — прогрев шлёт внутренние сообщения по `internal_ramp`, и их надо считать. При этом прогревочное сообщение **не является сообщением лиду**: строки в `messages` у него нет.

Решение: `outbox` создаётся здесь (Task 2), с `message_id` и `thread_id` **nullable**. Прогревочная отправка — строка с `message_id IS NULL`. Уникальный индекс `outbox_one_per_message` продолжает работать: в SQLite `NULL` в уникальном индексе не конфликтует сам с собой. Часть 2 получает таблицу уже созданной и не меняет её DDL.

Цена: одна таблица создаётся раньше, чем наполняется логикой очереди. Альтернатива — отдельный журнал прогрева — это вторая таблица с тем же смыслом и два места, где считается дневной расход.

---

## Task 1: Пакет `sender/` и его конфиг

**Files:**
- Create: `backend/sender/__init__.py`, `backend/sender/config.toml`, `backend/sender/services/__init__.py`, `backend/sender/services/config.py`
- Modify: `backend/config.py`, `backend/pyproject.toml`
- Test: `backend/sender/tests/test_config.py`

**Interfaces:**
- Consumes: ничего — первая задача.
- Produces: `sender.services.config.load() -> dict` с ключами `autopilot`, `window`, `pace`, `cadence`, `limits`, `warmup`, `health`, `retry` и абсолютным `Path` в `config["state_db"]`. Все последующие задачи берут пороги отсюда. `settings.sender_node_url`, `settings.telegram_bot_token`, `settings.telegram_chat_id` — из `backend/config.py`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_config.py`:

```python
"""Конфиг системы 3: единственное место порогов, пути от каталога sender/."""

from pathlib import Path

from sender.services import config


def test_load_resolves_state_db_to_absolute_path():
    loaded = config.load()
    assert isinstance(loaded["state_db"], Path)
    assert loaded["state_db"].is_absolute()
    assert loaded["state_db"].name == "state.db"


def test_load_carries_warmup_calendar_from_spec():
    warmup = config.load()["warmup"]
    assert warmup["socket_delay_hours"] == 24
    assert warmup["passive_days"] == 3
    assert warmup["internal_ramp"] == [6, 12, 20, 30, 45, 60]
    assert warmup["cold_start_day"] == 11
    assert warmup["cold_ramp"] == [5, 10, 15, 20, 25, 30]
    assert warmup["ceiling"] == 30


def test_autopilot_starts_off():
    """Kill switch по умолчанию закрыт: система, приезжающая в 'full',
    начала бы писать лидам в момент первого запуска."""
    assert config.load()["autopilot"]["mode"] == "off"
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.services'`

- [ ] **Step 3: Создать пакет и конфиг**

`backend/sender/__init__.py` — пустой файл.
`backend/sender/services/__init__.py` — пустой файл.
Каталог `backend/sender/tests/` — **без** `__init__.py`: тесты в проекте не
пакеты, их собирает `--import-mode=importlib` (см. комментарий в pyproject).

`backend/sender/config.toml`:

```toml
# Единственное место конфигурации системы 3, по образцу collector/config.toml
# и writer/config.toml. Пути относительны каталогу sender/, а не текущему:
# тот же конфиг читается и из sender/, и из backend/ (uvicorn монтирует
# роутеры sender'а в общее приложение).

state_db = "../collector/data/state.db"

[autopilot]
mode = "off"                    # off | replies | full; он же kill switch

[window]
timezone = "Asia/Almaty"
hours    = [10, 18]
weekdays = [1, 2, 3, 4, 5]

[pace]
tick_seconds   = 20
jitter_minutes = [2, 15]

[cadence]
follow_up_days = [3, 7]
max_touches    = 3

[limits]
auto_replies_per_thread = 1
max_reply_chars         = 600

[warmup]
socket_delay_hours = 24
passive_days       = 3
internal_ramp      = [6, 12, 20, 30, 45, 60]
cold_start_day     = 11
cold_ramp          = [5, 10, 15, 20, 25, 30]
ceiling            = 30
# Как часто просыпается прогрев. Одна отправка за тик: шесть сообщений подряд
# отличаются от живого общения ровно тем, из-за чего номера и банят.
tick_minutes       = 10

[health]
window_days              = 7
min_sample               = 20
min_delivered_rate       = 0.8
min_reply_rate           = 0.05
reconnects_per_day_alert = 5

[retry]
backoff_minutes     = [1, 5, 30]
stuck_after_minutes = 5
```

`backend/sender/services/config.py`:

```python
"""Конфиг системы 3. Пути резолвятся от каталога sender/, а не от cwd.

Тот же файл читается из двух рабочих каталогов: из backend/ (uvicorn) и из
sender/ (ручные проверки). Относительный путь означал бы в этих двух случаях
разные базы, и пул номеров тихо разъехался бы с перепиской.
"""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent


def load() -> dict:
    config = tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
    config["state_db"] = (HOME / config["state_db"]).resolve()
    return config
```

- [ ] **Step 4: Добавить секреты в `backend/config.py`**

В класс `Settings`, после `langfuse_host`:

```python
    sender_node_url: str = "http://127.0.0.1:8788"
    sender_webhook_secret: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
```

`sender_node_url` со значением по умолчанию, а не `None`: Node всегда на том же
хосте, и адрес — не секрет. Остальные три — секреты, их отсутствие должно быть
отличимо от пустой строки.

- [ ] **Step 5: Подключить тесты sender'а к pytest**

В `backend/pyproject.toml`:

```toml
testpaths = ["collector/tests", "writer/tests", "sender/tests"]
```

И в `dependencies` добавить строку (httpx стоит транзитивно через langfuse,
но полагаться на чужую зависимость нельзя — она уедет с обновлением):

```toml
    "httpx>=0.28",
```

- [ ] **Step 6: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv sync && uv run pytest sender/tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 7: Убедиться, что ничего не сломано**

Run: `cd backend && uv run pytest`
Expected: все прежние тесты зелёные, +3 новых

- [ ] **Step 8: Коммит**

```bash
git add backend/sender backend/config.py backend/pyproject.toml backend/uv.lock
git commit -m "feat(sender): пакет системы 3, config.toml и секреты в settings"
```

---

## Task 2: Схема и идемпотентные миграции

**Files:**
- Create: `backend/sender/db/__init__.py`, `backend/sender/db/migrate.py`
- Test: `backend/sender/tests/test_migrate.py`

**Interfaces:**
- Consumes: `sender.services.config.load()["state_db"]` из Task 1.
- Produces:
  - `sender.db.migrate.SCHEMA: str` — DDL таблиц `numbers` и `outbox`.
  - `sender.db.migrate.ensure_column(db, table: str, column: str, ddl: str) -> bool` — `True`, если колонка добавлена; `False`, если уже была.
  - `sender.db.migrate.apply(db) -> None` — применяет `SCHEMA` и все ALTER'ы.
  - `sender.db.migrate.connect(path: Path) -> sqlite3.Connection` — соединение с `row_factory = sqlite3.Row` и применённой схемой.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_migrate.py`:

```python
"""Миграции невосстановимого слоя: DROP запрещён, ALTER идемпотентен.

Схема state.db создаётся на каждом старте процесса. ALTER TABLE ADD COLUMN в
SQLite не идемпотентен, поэтому второй запуск упал бы с duplicate column name —
ровно это здесь и проверяется.
"""

import sqlite3

import pytest

from sender.db import migrate


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def tables(db):
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_connect_creates_numbers_and_outbox(db):
    assert {"numbers", "outbox"} <= tables(db)


def test_apply_is_idempotent(db):
    """Второй старт процесса на той же базе не падает."""
    migrate.apply(db)
    migrate.apply(db)
    assert {"numbers", "outbox"} <= tables(db)


def test_ensure_column_reports_whether_it_added(db):
    db.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
    assert migrate.ensure_column(db, "threads", "our_number", "TEXT") is True
    assert migrate.ensure_column(db, "threads", "our_number", "TEXT") is False
    assert "our_number" in columns(db, "threads")


def test_warmup_row_needs_no_message(db):
    """Прогревочная отправка — строка outbox без message_id: сообщения лиду
    за ней нет, а в дневной лимит номера она входит наравне с боевой."""
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+7700', '2026-08-28T09:00:00+00:00', 'sent',"
        "         '2026-08-28T09:00:00+00:00', '2026-08-28T09:00:00+00:00')")
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+7700', '2026-08-28T10:00:00+00:00', 'sent',"
        "         '2026-08-28T10:00:00+00:00', '2026-08-28T10:00:00+00:00')")
    db.commit()
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_one_queue_row_per_message(db):
    """Структурный запрет двойной отправки: одно сообщение — одна строка."""
    row = ("1", "+7700", "2026-08-28T09:00:00+00:00", "pending",
           "2026-08-28T09:00:00+00:00", "2026-08-28T09:00:00+00:00")
    insert = ("INSERT INTO outbox (message_id, our_number, send_after, status,"
              " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    db.execute(insert, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, row)
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.db'`

- [ ] **Step 3: Написать миграции**

`backend/sender/db/__init__.py` — пустой файл.

`backend/sender/db/migrate.py`:

```python
"""Схема системы 3 в state.db — невосстановимый слой.

Ровно как у переписки системы 2: CREATE TABLE IF NOT EXISTS, никаких DROP.
Отличие в том, что sender ещё и правит чужие таблицы (threads, messages), а
ALTER TABLE ADD COLUMN в SQLite не идемпотентен — при том, что схема
применяется на каждом старте процесса. Отсюда ensure_column: смотрит
PRAGMA table_info и добавляет колонку, только если её нет. Alembic ради двух
таблиц в проект не тащим.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
-- Очередь исходящих. Текста здесь нет: он в messages.draft_text.
-- message_id/thread_id пусты у прогревочных отправок: сообщения лиду за ними
-- нет, но в дневной лимит номера они входят наравне с боевыми.
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id    INTEGER PRIMARY KEY,
  message_id   INTEGER REFERENCES messages (message_id),
  thread_id    TEXT,
  our_number   TEXT NOT NULL,
  send_after   TEXT NOT NULL,   -- не раньше этого времени, UTC
  status       TEXT NOT NULL,   -- pending | sending | sent | failed | cancelled | stuck
  attempts     INTEGER NOT NULL DEFAULT 0,
  provider_id  TEXT,            -- id сообщения у транспорта
  delivered_at TEXT,
  read_at      TEXT,
  error        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, send_after);
CREATE INDEX IF NOT EXISTS outbox_by_number ON outbox (our_number, status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_per_message ON outbox (message_id);

-- Пул наших номеров.
CREATE TABLE IF NOT EXISTS numbers (
  number      TEXT PRIMARY KEY,
  session_dir TEXT NOT NULL,
  status      TEXT NOT NULL,   -- new | warming | active | quarantined | banned
  started_at  TEXT NOT NULL,   -- дата регистрации: от неё считается день прогрева
  note        TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    apply(db)
    return db


def apply(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """True, если колонку добавили; False, если она уже была."""
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    db.commit()
    return True
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_migrate.py -v`
Expected: 5 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/db backend/sender/tests/test_migrate.py
git commit -m "feat(sender): схема numbers/outbox и идемпотентный ensure_column"
```

---

## Task 3: Пул номеров — чтение и запись

**Files:**
- Create: `backend/sender/db/numbers.py`
- Test: `backend/sender/tests/test_numbers.py`

**Interfaces:**
- Consumes: `sender.db.migrate.connect` из Task 2.
- Produces:
  - `numbers.register(db, number: str, session_dir: str, now: datetime) -> None` — заводит номер в статусе `new`.
  - `numbers.get(db, number: str) -> dict` — поднимает `UnknownNumberError`, если номера нет.
  - `numbers.all(db) -> list[dict]`
  - `numbers.set_status(db, number: str, status: str, note: str | None = None) -> None` — поднимает `UnknownNumberError` / `ValueError` на чужом статусе.
  - `numbers.sent_today(db, number: str, now: datetime) -> int` — отправлено с этого номера за календарные сутки UTC.
  - `numbers.UnknownNumberError`, `numbers.STATUSES: tuple[str, ...]`

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_numbers.py`:

```python
"""Пул номеров: статусы, регистрация, дневной расход."""

from datetime import datetime, timezone

import pytest

from sender.db import migrate, numbers


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def sent_row(db, number, at):
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES (?, ?, 'sent', ?, ?)", (number, at, at, at))
    db.commit()


def test_register_starts_in_new(db):
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    assert numbers.get(db, "+77001112233")["status"] == "new"
    assert numbers.get(db, "+77001112233")["started_at"] == "2026-08-28T12:00:00+00:00"


def test_get_unknown_number_raises(db):
    with pytest.raises(numbers.UnknownNumberError):
        numbers.get(db, "+70000000000")


def test_set_status_rejects_unknown_status(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    with pytest.raises(ValueError):
        numbers.set_status(db, "+77001112233", "почти забанен")


def test_set_status_records_note(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    numbers.set_status(db, "+77001112233", "quarantined", note="delivered rate 0.61")
    row = numbers.get(db, "+77001112233")
    assert row["status"] == "quarantined"
    assert row["note"] == "delivered rate 0.61"


def test_sent_today_counts_only_this_number_and_this_day(db):
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    sent_row(db, "+77001112233", "2026-08-28T09:00:00+00:00")
    sent_row(db, "+77001112233", "2026-08-28T23:59:59+00:00")
    sent_row(db, "+77001112233", "2026-08-27T23:00:00+00:00")   # вчера
    sent_row(db, "+77009998877", "2026-08-28T09:00:00+00:00")   # чужой номер
    assert numbers.sent_today(db, "+77001112233", NOW) == 2


def test_sent_today_ignores_unsent_rows(db):
    """Строка в очереди — ещё не расход: лимит тратит отправка, а не намерение."""
    numbers.register(db, "+77001112233", "sessions/x", NOW)
    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
        " VALUES ('+77001112233', ?, 'pending', ?, ?)",
        ("2026-08-28T09:00:00+00:00",) * 3)
    db.commit()
    assert numbers.sent_today(db, "+77001112233", NOW) == 0
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_numbers.py -v`
Expected: FAIL — `ImportError: cannot import name 'numbers' from 'sender.db'`

- [ ] **Step 3: Написать модуль**

`backend/sender/db/numbers.py`:

```python
"""Пул наших номеров. Один номер — одна SIM, одна сессия Baileys.

Дневной расход не денормализуется: считается count(*) по outbox за сутки. При
пятидесяти сообщениях в день это бесплатно, а отдельный счётчик рано или поздно
разъедется с реальностью — и разъедется молча.
"""

import sqlite3
from datetime import datetime

STATUSES = ("new", "warming", "active", "quarantined", "banned")

FIELDS = "number, session_dir, status, started_at, note"


class UnknownNumberError(Exception):
    """Номера нет в пуле — почти всегда опечатка в номере, а не гонка."""


def register(db: sqlite3.Connection, number: str, session_dir: str, now: datetime) -> None:
    db.execute(
        f"INSERT INTO numbers ({FIELDS}) VALUES (?, ?, 'new', ?, NULL)",
        (number, session_dir, stamp(now)))
    db.commit()


def get(db: sqlite3.Connection, number: str) -> dict:
    row = db.execute(
        f"SELECT {FIELDS} FROM numbers WHERE number = ?", (number,)).fetchone()
    if row is None:
        raise UnknownNumberError(number)
    return dict(row)


def all(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(f"SELECT {FIELDS} FROM numbers ORDER BY started_at").fetchall()
    return [dict(row) for row in rows]


def set_status(db: sqlite3.Connection, number: str, status: str,
               note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус номера: {status}")
    get(db, number)
    db.execute(
        "UPDATE numbers SET status = ?, note = ? WHERE number = ?",
        (status, note, number))
    db.commit()


def sent_today(db: sqlite3.Connection, number: str, now: datetime) -> int:
    """Отправлено с номера за календарные сутки UTC — и боевого, и прогревочного."""
    day = now.date().isoformat()
    row = db.execute(
        "SELECT count(*) FROM outbox"
        " WHERE our_number = ? AND status = 'sent' AND updated_at >= ? AND updated_at < ?",
        (number, f"{day}T00:00:00+00:00", f"{day}T23:59:59.999999+00:00")).fetchone()
    return row[0]


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_numbers.py -v`
Expected: 6 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/db/numbers.py backend/sender/tests/test_numbers.py
git commit -m "feat(sender): пул номеров и дневной расход по outbox"
```

---

## Task 4: `transport.py` — единственная точка выхода в сеть

**Files:**
- Create: `backend/sender/transport.py`
- Test: `backend/sender/tests/test_transport.py`, `backend/sender/tests/test_import_graph.py`

**Interfaces:**
- Consumes: `settings.sender_node_url` из Task 1.
- Produces:
  - `transport.Transport(base_url: str, client: httpx.AsyncClient)` с методами
    `async send(number: str, to: str, text: str, key: str, kind: str = "text") -> Sent`,
    `async check(number: str, to: str) -> bool`,
    `async pair(number: str) -> str`,
    `async health() -> dict[str, dict]`.
  - `transport.Sent` — `dataclass(frozen=True)` с полями `sent: bool`, `provider_id: str | None`, `error: str | None`.
  - `transport.TransportError` — Node ответил не-2xx или не ответил.
  - `transport.build() -> Transport` — фабрика на боевом `httpx.AsyncClient` и `settings.sender_node_url`.

  `Sent.sent` — честный флаг «фрейм ушёл в сокет»; на нём часть 2 будет
  различать безопасный ретрай и неопределённость. Поэтому «не удалось
  отправить» приезжает объектом `Sent(sent=False, ...)`, а исключение
  `TransportError` означает, что мы не знаем даже этого.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_transport.py`:

```python
"""Шов с Node. Живого WhatsApp здесь нет: httpx.MockTransport отвечает вместо
Node, и проверяется ровно контракт — что уходит в ручку и что приезжает назад.
"""

import json

import httpx
import pytest

from sender import transport


def stub(handler) -> transport.Transport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return transport.Transport("http://node.test", client)


@pytest.mark.asyncio
async def test_send_passes_idempotency_key_and_returns_provider_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True, "sent": True, "provider_id": "3EB0"})

    result = await stub(handler).send("+7700", "+7701", "привет", key="42")

    assert seen["key"] == "42"
    assert seen["type"] == "text"
    assert result == transport.Sent(sent=True, provider_id="3EB0", error=None)


@pytest.mark.asyncio
async def test_send_reports_socket_failure_without_raising():
    """sent=False — сообщение точно не ушло; это не авария, а повод ретраить."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "sent": False, "error": "loggedOut"})

    result = await stub(handler).send("+7700", "+7701", "привет", key="42")

    assert result.sent is False
    assert result.error == "loggedOut"


@pytest.mark.asyncio
async def test_http_error_raises_transport_error():
    """Не-2xx от Node — неопределённость: ушло или нет, неизвестно."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(transport.TransportError):
        await stub(handler).send("+7700", "+7701", "привет", key="42")


@pytest.mark.asyncio
async def test_check_returns_bool():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_whatsapp": False})

    assert await stub(handler).check("+7700", "+7701") is False


@pytest.mark.asyncio
async def test_pair_returns_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "ABCD-1234"})

    assert await stub(handler).pair("+7700") == "ABCD-1234"


@pytest.mark.asyncio
async def test_health_returns_state_per_number():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"+7700": {"state": "connected", "reconnects": 2}})

    assert (await stub(handler).health())["+7700"]["state"] == "connected"
```

Создать `backend/sender/tests/test_import_graph.py`:

```python
"""Граница системы 3: в сеть ходит только transport.py.

Тот же статический обход графа импортов, что сторожит отсутствие сети в
rebuild.py (collector/tests/test_operations.py). Проверяется не дисциплина, а
факт: если pool или health начнут импортировать httpx, тест покраснеет.
"""

import ast
from pathlib import Path

SENDER = Path(__file__).resolve().parent.parent
BACKEND_ROOT = SENDER.parent
NETWORK = ("httpx", "requests", "urllib.request", "scrapling")
ALLOWED = ("sender/transport.py", "sender/notify.py")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_only_transport_and_notify_touch_the_network():
    offenders = []
    for path in sorted(SENDER.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        if rel.startswith("sender/tests/") or rel in ALLOWED:
            continue
        if any(module.startswith(NETWORK) for module in imports(path)):
            offenders.append(rel)
    assert offenders == [], f"в сеть ходит не только transport.py: {offenders}"


def test_the_guard_itself_sees_transport():
    """Страж бесполезен, если перестал находить файлы: transport.py обязан
    попадать в выборку и обязан импортировать httpx."""
    assert any(module.startswith("httpx") for module in imports(SENDER / "transport.py"))
```

- [ ] **Step 2: Прогнать тесты, убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_transport.py sender/tests/test_import_graph.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.transport'`

- [ ] **Step 3: Добавить `pytest-asyncio`**

В `backend/pyproject.toml`:

```toml
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=1.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

`asyncio_mode = "auto"` — чтобы не расставлять `@pytest.mark.asyncio` на каждый
тест; маркеры в примерах выше остаются валидными и при `auto`.

- [ ] **Step 4: Написать транспорт**

`backend/sender/transport.py`:

```python
"""Шов между Python и Node: единственный модуль системы 3, ходящий в сеть.

Node — тупая труба: держит сокеты Baileys, шлёт текст, принимает события. Он не
знает про лидов, треды, лимиты и расписание. Причина не эстетическая: Node —
единственная часть системы, которую нельзя протестировать без живого WhatsApp,
поэтому всё, что содержит решение, обязано жить там, где есть база и pytest.

Отсюда же деление ответов. `Sent(sent=False)` — честное «фрейм в сокет не ушёл»,
на нём строится безопасный ретрай. `TransportError` — «мы не знаем, ушло ли»,
и это уже случай для человека, а не для повтора.
"""

from dataclasses import dataclass

import httpx

from config import settings

TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Sent:
    sent: bool
    provider_id: str | None
    error: str | None


class TransportError(Exception):
    """Node не ответил или ответил не-2xx: результат отправки неизвестен."""


class Transport:
    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, number: str, to: str, text: str, key: str,
                   kind: str = "text") -> Sent:
        payload = {"number": number, "to": to, "text": text, "type": kind, "key": key}
        body = await self._post("/send", payload)
        return Sent(sent=bool(body.get("sent")),
                    provider_id=body.get("provider_id"),
                    error=body.get("error"))

    async def check(self, number: str, to: str) -> bool:
        body = await self._post("/check", {"number": number, "to": to})
        return bool(body["has_whatsapp"])

    async def pair(self, number: str) -> str:
        body = await self._post("/pair", {"number": number})
        return body["code"]

    async def health(self) -> dict[str, dict]:
        try:
            response = await self._client.get(f"{self._base_url}/health",
                                              timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TransportError(f"GET /health: {error}") from error
        return response.json()

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            response = await self._client.post(f"{self._base_url}{path}", json=payload,
                                               timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TransportError(f"POST {path}: {error}") from error
        return response.json()


def build() -> Transport:
    return Transport(settings.sender_node_url, httpx.AsyncClient())
```

- [ ] **Step 5: Прогнать тесты, убедиться, что проходят**

Run: `cd backend && uv sync && uv run pytest sender/tests/ -v`
Expected: все зелёные (test_transport: 6, test_import_graph: 2)

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/transport.py backend/sender/tests backend/pyproject.toml backend/uv.lock
git commit -m "feat(sender): transport.py как единственная точка выхода в сеть"
```

---

## Task 5: Node-сервис на Baileys

**Files:**
- Create: `backend/sender/node/package.json`, `backend/sender/node/index.js`, `backend/sender/node/.gitignore`
- Modify: `.gitignore` (корневой), `backend/sender/config.toml`

**Interfaces:**
- Consumes: контракт ручек из Task 4 — `POST /send {number, to, text, type, key} -> {ok, sent, provider_id}`, `POST /check {number, to} -> {has_whatsapp}`, `POST /pair {number} -> {code}`, `GET /health -> {номер: {state, reconnects}}`.
- Produces: вебхук в Python — `POST {PYTHON_URL}/api/sender/webhook` с телом `{kind, number, ...}`, где `kind` = `incoming | status | connection`. Обработчик вебхука пишется в части 3; здесь Node только шлёт и, получив не-2xx, повторяет с интервалом.

**Перед реализацией:** сверить актуальный API Baileys с README пакета
(`npm view baileys`, README на npmjs.com). Имена `useMultiFileAuthState`,
`requestPairingCode`, `onWhatsApp`, событий `connection.update` /
`messages.upsert` / `creds.update` соответствуют версии на момент написания
плана; библиотека неофициальная и ломает API чаще официальных.

- [ ] **Step 1: Завести проект Node**

`backend/sender/node/package.json`:

```json
{
  "name": "sender-node",
  "private": true,
  "type": "module",
  "scripts": {
    "start": "node index.js"
  },
  "dependencies": {
    "baileys": "^7.0.0",
    "pino": "^9.0.0"
  }
}
```

`backend/sender/node/.gitignore`:

```
node_modules/
sessions/
```

В корневой `.gitignore` добавить строку `backend/sender/node/sessions/` —
дублирование намеренное: сессии не должны уехать в git ни при каком способе
добавления файлов. Потеря сессии = номер, который надо заново подключать
телефоном; попадание сессии в git = чужой доступ к живому WhatsApp.

- [ ] **Step 2: Написать сервис**

`backend/sender/node/index.js`:

```javascript
// Тупая труба к WhatsApp: сокеты Baileys и четыре ручки. Здесь нет ни лидов,
// ни тредов, ни лимитов, ни расписания — всё это решает Python, потому что
// эту часть нельзя протестировать без живого WhatsApp.

import http from "node:http";
import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";

const PORT = Number(process.env.SENDER_NODE_PORT ?? 8788);
const PYTHON_URL = process.env.SENDER_PYTHON_URL ?? "http://127.0.0.1:8787";
const SESSIONS = path.resolve("sessions");
const WEBHOOK_RETRY_MS = 5000;

const log = pino({ level: "info" });
const sockets = new Map();   // number -> {sock, state, reconnects}

const jid = (number) => `${number.replace(/\D/g, "")}@s.whatsapp.net`;

async function connect(number) {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(SESSIONS, number));
  const sock = makeWASocket({ auth: state, logger: log.child({ number }) });
  const entry = sockets.get(number) ?? { reconnects: 0 };
  sockets.set(number, { ...entry, sock, state: "connecting" });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const current = sockets.get(number);
    if (update.connection === "open") {
      sockets.set(number, { ...current, state: "connected" });
    }
    if (update.connection === "close") {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      sockets.set(number, {
        ...current,
        state: loggedOut ? "loggedOut" : "reconnecting",
        reconnects: current.reconnects + (loggedOut ? 0 : 1),
      });
      // loggedOut не переподключается: сессия мертва, номер требует телефона.
      if (!loggedOut) setTimeout(() => connect(number), WEBHOOK_RETRY_MS);
    }
    await notify({ kind: "connection", number, state: sockets.get(number).state });
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const message of messages) {
      if (message.key.fromMe) continue;
      await notify({
        kind: "incoming",
        number,
        from: message.key.remoteJid,
        provider_id: message.key.id,
        text: message.message?.conversation
          ?? message.message?.extendedTextMessage?.text
          ?? "",
      });
    }
  });

  sock.ev.on("messages.update", async (updates) => {
    for (const update of updates) {
      await notify({
        kind: "status",
        number,
        provider_id: update.key.id,
        status: update.update?.status ?? null,
      });
    }
  });

  return sock;
}

// Вебхук повторяется, пока Python не ответит 2xx: событие о доставке, потерянное
// из-за перезапуска бэкенда, — это номер, который навсегда останется без метрики.
async function notify(payload) {
  try {
    const response = await fetch(`${PYTHON_URL}/api/sender/webhook`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    log.warn({ error: String(error), payload }, "вебхук не доставлен, повтор");
    setTimeout(() => notify(payload), WEBHOOK_RETRY_MS);
  }
}

async function socketOf(number) {
  if (!sockets.get(number)?.sock) await connect(number);
  return sockets.get(number).sock;
}

const routes = {
  // type=text — единственный вид боевой отправки; audio/image нужны прогреву,
  // которому требуется разнообразный контент. Медиа приезжает как путь к файлу
  // внутри node/media/, потому что Python не должен гонять байты через ручку.
  "POST /send": async ({ number, to, text, type = "text", key }) => {
    const sock = await socketOf(number);
    const content = {
      text: { text },
      image: { image: { url: text }, caption: "" },
      audio: { audio: { url: text }, ptt: true },
    }[type];
    if (!content) return { ok: false, sent: false, error: `unknown type ${type}` };
    try {
      const sent = await sock.sendMessage(jid(to), content);
      log.info({ number, to, key }, "отправлено");
      return { ok: true, sent: true, provider_id: sent.key.id };
    } catch (error) {
      return { ok: false, sent: false, error: String(error) };
    }
  },
  "POST /check": async ({ number, to }) => {
    const sock = await socketOf(number);
    const [found] = await sock.onWhatsApp(jid(to));
    return { has_whatsapp: Boolean(found?.exists) };
  },
  "POST /pair": async ({ number }) => {
    const sock = await socketOf(number);
    return { code: await sock.requestPairingCode(number.replace(/\D/g, "")) };
  },
};

function healthReport() {
  const report = {};
  for (const [number, entry] of sockets) {
    report[number] = { state: entry.state, reconnects: entry.reconnects };
  }
  return report;
}

http.createServer(async (request, response) => {
  const route = `${request.method} ${request.url}`;
  const reply = (code, body) => {
    response.writeHead(code, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  };
  if (route === "GET /health") return reply(200, healthReport());
  const handler = routes[route];
  if (!handler) return reply(404, { error: "no such route" });
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    reply(200, await handler(JSON.parse(Buffer.concat(chunks).toString() || "{}")));
  } catch (error) {
    log.error({ error: String(error), route }, "ручка упала");
    reply(500, { error: String(error) });
  }
}).listen(PORT, () => log.info({ port: PORT }, "sender-node слушает"));
```

- [ ] **Step 3: Установить зависимости**

Run: `cd backend/sender/node && npm install`
Expected: `node_modules/` создан, `package-lock.json` появился

- [ ] **Step 4: Ручной смоук — регистрация номера**

Автоматического теста здесь нет и не будет: без живого WhatsApp проверять
нечего, а с живым это уже не тест. Чеклист:

```bash
cd backend/sender/node && npm start          # терминал 1
curl -s -XPOST localhost:8788/pair -H 'content-type: application/json' \
     -d '{"number":"+77001112233"}'          # терминал 2
```

1. Ручка вернула pairing code вида `ABCD-1234`.
2. Код введён на телефоне: WhatsApp → Связанные устройства → Привязка по коду.
3. `curl -s localhost:8788/health` показывает `{"+77001112233": {"state": "connected", ...}}`.
4. Каталог `sessions/+77001112233/` появился и непуст.

- [ ] **Step 5: Ручной смоук — отправка самому себе**

```bash
curl -s -XPOST localhost:8788/send -H 'content-type: application/json' \
     -d '{"number":"+77001112233","to":"+77001112233","text":"проверка","key":"smoke-1"}'
```

Ожидается `{"ok":true,"sent":true,"provider_id":"..."}` и сообщение в своём же
чате. Это единственная проверка этапа 1 из спеки.

- [ ] **Step 6: Записать команду запуска в конфиг проекта**

В `backend/sender/config.toml`, в конец, добавить комментарий-напоминание рядом
с блоком `[warmup]`:

```toml
# Node-сервис поднимается отдельным процессом:
#   cd backend/sender/node && npm start
# Адрес, по которому его ищет Python, — settings.sender_node_url.
```

- [ ] **Step 7: Коммит**

```bash
git add backend/sender/node .gitignore backend/sender/config.toml
git commit -m "feat(sender): node-сервис baileys за четырьмя ручками"
```

---

## Task 6: Календарь прогрева

**Files:**
- Create: `backend/sender/services/warmup.py`
- Test: `backend/sender/tests/test_warmup.py`

**Interfaces:**
- Consumes: `config.load()["warmup"]` из Task 1, `numbers.get` из Task 3.
- Produces:
  - `warmup.Phase` — `StrEnum` со значениями `socket_delay`, `passive`, `internal`, `cold`.
  - `warmup.Plan` — `dataclass(frozen=True)`: `day: int`, `phase: Phase`, `daily_limit: int`, `cold_allowed: bool`.
  - `warmup.plan(started_at: str, now: datetime, warmup_config: dict) -> Plan`
  - `warmup.day_of(started_at: str, now: datetime) -> int` — день прогрева, 1 в сутки регистрации.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_warmup.py`:

```python
"""Календарь прогрева — чистая функция от даты регистрации и «сейчас».

Числа взяты из спеки дословно; тест сторожит границы фаз, потому что
единственный дорогой способ узнать об ошибке здесь — бан номера.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.services import config, warmup

STARTED = "2026-08-01T09:00:00+00:00"
WARMUP = config.load()["warmup"]


def at(day: int) -> datetime:
    """Полдень N-го дня прогрева: день 1 — сутки регистрации."""
    return datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=day - 1)


@pytest.mark.parametrize("day, phase", [
    (1, warmup.Phase.socket_delay),
    (2, warmup.Phase.passive),
    (4, warmup.Phase.passive),
    (5, warmup.Phase.internal),
    (10, warmup.Phase.internal),
    (11, warmup.Phase.cold),
    (40, warmup.Phase.cold),
])
def test_phase_boundaries(day, phase):
    assert warmup.plan(STARTED, at(day), WARMUP).phase is phase


def test_socket_delay_day_sends_nothing():
    """Первые сутки номер вообще не подключается: сама привязка сокета —
    событие, которое WhatsApp видит."""
    assert warmup.plan(STARTED, at(1), WARMUP).daily_limit == 0


def test_passive_days_send_nothing_outgoing():
    assert warmup.plan(STARTED, at(3), WARMUP).daily_limit == 0


def test_internal_ramp_follows_the_config():
    assert warmup.plan(STARTED, at(5), WARMUP).daily_limit == 6
    assert warmup.plan(STARTED, at(6), WARMUP).daily_limit == 12


def test_cold_ramp_starts_at_day_eleven_and_stops_at_ceiling():
    assert warmup.plan(STARTED, at(11), WARMUP).daily_limit == 5
    assert warmup.plan(STARTED, at(40), WARMUP).daily_limit == WARMUP["ceiling"]


def test_cold_touches_forbidden_before_day_eleven():
    """Холодные касания — примерно с 11-го дня, не с четвёртого."""
    assert warmup.plan(STARTED, at(10), WARMUP).cold_allowed is False
    assert warmup.plan(STARTED, at(11), WARMUP).cold_allowed is True
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_warmup.py -v`
Expected: FAIL — `ImportError: cannot import name 'warmup'`

- [ ] **Step 3: Написать календарь**

`backend/sender/services/warmup.py`:

```python
"""Календарь прогрева номера: день от регистрации -> что ему сегодня можно.

Источник чисел — рекомендации по прогреву номера WhatsApp, перенесённые в
config.toml. Три вещи, в которых календарь строже интуиции: первые сутки сокет
вообще не привязывается, дни 2-4 номер только принимает, а холодные касания
начинаются примерно с одиннадцатого дня, не с четвёртого.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Phase(StrEnum):
    socket_delay = "socket_delay"
    passive = "passive"
    internal = "internal"
    cold = "cold"


@dataclass(frozen=True)
class Plan:
    day: int
    phase: Phase
    daily_limit: int
    cold_allowed: bool


def day_of(started_at: str, now: datetime) -> int:
    """День прогрева: сутки регистрации — первый."""
    started = date.fromisoformat(started_at[:10])
    return (now.date() - started).days + 1


def plan(started_at: str, now: datetime, warmup_config: dict) -> Plan:
    day = day_of(started_at, now)
    phase = _phase(day, warmup_config)
    limit = _limit(day, phase, warmup_config)
    return Plan(day=day, phase=phase, daily_limit=limit, cold_allowed=phase is Phase.cold)


def _phase(day: int, warmup_config: dict) -> Phase:
    if day <= warmup_config["socket_delay_hours"] // 24:
        return Phase.socket_delay
    if day <= 1 + warmup_config["passive_days"]:
        return Phase.passive
    if day < warmup_config["cold_start_day"]:
        return Phase.internal
    return Phase.cold


def _limit(day: int, phase: Phase, warmup_config: dict) -> int:
    if phase in (Phase.socket_delay, Phase.passive):
        return 0
    if phase is Phase.internal:
        return _from_ramp(day - (1 + warmup_config["passive_days"]),
                          warmup_config["internal_ramp"], warmup_config["ceiling"])
    return _from_ramp(day - warmup_config["cold_start_day"] + 1,
                      warmup_config["cold_ramp"], warmup_config["ceiling"])


def _from_ramp(step: int, ramp: list[int], ceiling: int) -> int:
    """Шаг за пределами рампы — потолок: рампа кончается, доверие нет."""
    if step > len(ramp):
        return ceiling
    return min(ramp[step - 1], ceiling)
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_warmup.py -v`
Expected: 12 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/warmup.py backend/sender/tests/test_warmup.py
git commit -m "feat(sender): календарь прогрева номера"
```

---

## Task 7: Выбор номера и дневная ёмкость

**Files:**
- Create: `backend/sender/services/pool.py`
- Test: `backend/sender/tests/test_pool.py`

**Interfaces:**
- Consumes: `numbers.all`, `numbers.sent_today` (Task 3), `warmup.plan` (Task 6), `config.load()` (Task 1).
- Produces:
  - `pool.capacity(db, number: str, now: datetime, config: dict) -> int` — сколько сообщений номер может отправить сегодня ещё.
  - `pool.assign(db, now: datetime, config: dict) -> str` — наименее загруженный сегодня `active` номер; поднимает `pool.NoNumberAvailableError`, если свободных нет.
  - `pool.NoNumberAvailableError`

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_pool.py`:

```python
"""Выбор номера и дневная ёмкость.

Главное правило здесь отрицательное: warming-номер не получает боевых отправок
ни при каких условиях. Все выбрали лимит — это перенос на завтра, а не отправка
через непрогретый номер.
"""

from datetime import datetime, timezone

import pytest

from sender.db import migrate, numbers
from sender.services import config, pool

CONFIG = config.load()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)   # день 32 для номеров ниже


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def add(db, number, status, started_at="2026-08-01T09:00:00+00:00"):
    numbers.register(db, number, f"sessions/{number}", datetime.fromisoformat(started_at))
    numbers.set_status(db, number, status)


def sent(db, number, count, day="2026-09-01"):
    for index in range(count):
        at = f"{day}T{index % 24:02d}:00:00+00:00"
        db.execute(
            "INSERT INTO outbox (our_number, send_after, status, created_at, updated_at)"
            " VALUES (?, ?, 'sent', ?, ?)", (number, at, at, at))
    db.commit()


def test_capacity_is_daily_limit_minus_sent(db):
    add(db, "+7700", "active")
    sent(db, "+7700", 4)
    assert pool.capacity(db, "+7700", NOW, CONFIG) == CONFIG["warmup"]["ceiling"] - 4


def test_capacity_never_goes_negative(db):
    add(db, "+7700", "active")
    sent(db, "+7700", CONFIG["warmup"]["ceiling"] + 5)
    assert pool.capacity(db, "+7700", NOW, CONFIG) == 0


def test_assign_picks_the_least_loaded_active_number(db):
    add(db, "+7700", "active")
    add(db, "+7701", "active")
    sent(db, "+7700", 10)
    sent(db, "+7701", 2)
    assert pool.assign(db, NOW, CONFIG) == "+7701"


def test_assign_ignores_warming_numbers(db):
    """Непрогретый номер не берёт боевую отправку даже когда он единственный."""
    add(db, "+7702", "warming")
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_assign_ignores_quarantined_and_banned(db):
    add(db, "+7703", "quarantined")
    add(db, "+7704", "banned")
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_assign_raises_when_everyone_is_out_of_capacity(db):
    """Все выбрали лимит — это перенос на завтра, а не отправка сверх лимита."""
    add(db, "+7700", "active")
    sent(db, "+7700", CONFIG["warmup"]["ceiling"])
    with pytest.raises(pool.NoNumberAvailableError):
        pool.assign(db, NOW, CONFIG)


def test_capacity_of_freshly_registered_number_is_zero(db):
    """Первые сутки — socket_delay: ёмкость ноль независимо от статуса."""
    add(db, "+7705", "active", started_at="2026-09-01T09:00:00+00:00")
    assert pool.capacity(db, "+7705", NOW, CONFIG) == 0
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_pool.py -v`
Expected: FAIL — `ImportError: cannot import name 'pool'`

- [ ] **Step 3: Написать пул**

`backend/sender/services/pool.py`:

```python
"""Кто отправляет: выбор номера и его дневная ёмкость.

`our_number` присваивается треду один раз и дальше не меняется: для лида
сообщение с другого номера — новый чат без истории. Поэтому здесь нет
«перебалансировки» — только выбор при первом касании.
"""

import sqlite3
from datetime import datetime

from sender.db import numbers
from sender.services import warmup

SENDING_STATUS = "active"


class NoNumberAvailableError(Exception):
    """Свободных номеров нет: все выбрали лимит, греются или в карантине.
    Это перенос отправки на завтра, а не повод писать через warming."""


def capacity(db: sqlite3.Connection, number: str, now: datetime, config: dict) -> int:
    row = numbers.get(db, number)
    limit = warmup.plan(row["started_at"], now, config["warmup"]).daily_limit
    return max(0, limit - numbers.sent_today(db, number, now))


def assign(db: sqlite3.Connection, now: datetime, config: dict) -> str:
    free = [(capacity(db, row["number"], now, config), row["number"])
            for row in numbers.all(db) if row["status"] == SENDING_STATUS]
    usable = [(left, number) for left, number in free if left > 0]
    if not usable:
        raise NoNumberAvailableError("нет активного номера с непочатым дневным лимитом")
    return max(usable)[1]
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_pool.py -v`
Expected: 7 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/pool.py backend/sender/tests/test_pool.py
git commit -m "feat(sender): выбор номера по дневной ёмкости"
```

---

## Task 8: Telegram — уведомления, требующие человека

**Files:**
- Create: `backend/sender/notify.py`
- Test: `backend/sender/tests/test_notify.py`
- Modify: `backend/sender/tests/test_import_graph.py` (уже в `ALLOWED` из Task 4 — проверить, что так и есть)

**Interfaces:**
- Consumes: `settings.telegram_bot_token`, `settings.telegram_chat_id` (Task 1).
- Produces: `async notify.send(text: str, client: httpx.AsyncClient | None = None) -> bool` — `True`, если сообщение ушло; `False`, если канал не настроен. Исключения наружу не выпускает: упавшее уведомление не должно ронять монитор здоровья.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_notify.py`:

```python
"""Telegram: только события, требующие человека.

Отсутствие ключей — не ошибка, а «канал не настроен»: на машине разработчика
монитор здоровья обязан работать без бота.
"""

import httpx
import pytest

from sender import notify


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "T0KEN")
    monkeypatch.setattr(notify.settings, "telegram_chat_id", "42")


async def test_returns_false_without_token(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", None)
    assert await notify.send("номер +7700 в карантине") is False


async def test_posts_text_to_configured_chat(configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await notify.send("номер +7700 забанен", client) is True
    assert "T0KEN" in seen["url"]
    assert "+7700" in seen["body"]


async def test_swallows_transport_failure(configured):
    """Упавшее уведомление не должно ронять монитор здоровья: авария номера
    важнее аварии телеграма."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await notify.send("что угодно", client) is False
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.notify'`

- [ ] **Step 3: Написать модуль**

`backend/sender/notify.py`:

```python
"""Telegram: эскалации и аварии. Одно сообщение на событие.

Уведомление на каждый тик превращается в шум, который перестают читать за
неделю, — а вместе с ним перестают читать и аварии номеров. Поэтому сюда
попадают только события, после которых человек обязан что-то сделать.
"""

import logging

import httpx

from config import settings

TIMEOUT_SECONDS = 10

log = logging.getLogger(__name__)


async def send(text: str, client: httpx.AsyncClient | None = None) -> bool:
    """False — канал не настроен или телеграм недоступен. Наружу не падаем:
    авария номера важнее аварии уведомления о ней."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        log.info("telegram не настроен, уведомление только в лог: %s", text)
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {"chat_id": settings.telegram_chat_id, "text": text}
    owned = client is None
    client = client or httpx.AsyncClient()
    try:
        response = await client.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return True
    except httpx.HTTPError as error:
        log.warning("telegram не принял уведомление (%s): %s", error, text)
        return False
    finally:
        if owned:
            await client.aclose()
```

- [ ] **Step 4: Прогнать тесты, убедиться, что проходят**

Run: `cd backend && uv run pytest sender/tests/test_notify.py sender/tests/test_import_graph.py -v`
Expected: 3 + 2 passed — граф импортов зелёный, потому что `notify.py` в `ALLOWED`

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/notify.py backend/sender/tests/test_notify.py
git commit -m "feat(sender): telegram-канал аварий"
```

---

## Task 9: Монитор здоровья номеров

**Files:**
- Create: `backend/sender/services/health.py`
- Test: `backend/sender/tests/test_health.py`

**Interfaces:**
- Consumes: `transport.Transport.health` (Task 4), `numbers.all/set_status` (Task 3), `notify.send` (Task 8), `config.load()["health"]` (Task 1).
- Produces:
  - `health.Event` — `dataclass(frozen=True)`: `number: str`, `status: str`, `reason: str`.
  - `async health.check(db, transport, config: dict) -> list[Event]` — сверяет отчёт Node с пулом, двигает статусы, шлёт уведомления, возвращает случившееся.

  Метрики delivered/reply rate здесь **не считаются**: считать их не по чему, пока
  никто не отправляет боевое. Они приезжают в части 2 вместе с воркером — вход
  для них уже есть (`outbox.delivered_at`).

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_health.py`:

```python
"""Монитор здоровья: единственная метрика, срабатывающая заранее.

delivered и reply rate констатируют задним числом; частые реконнекты и запросы
повторной авторизации — задокументированный ранний признак приближающейся
блокировки. Поэтому карантин по реконнектам стоит ДО бана, а не после.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sender.db import migrate, numbers
from sender.services import config, health

CONFIG = config.load()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def sent_messages(monkeypatch):
    sent = []

    async def fake_send(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(health.notify, "send", fake_send)
    return sent


def add(db, number, status):
    numbers.register(db, number, f"sessions/{number}", NOW)
    numbers.set_status(db, number, status)


def transport_reporting(report):
    async def get_health():
        return report

    return SimpleNamespace(health=get_health)


async def test_logged_out_bans_immediately(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "loggedOut", "reconnects": 0}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "banned"
    assert [event.status for event in events] == ["banned"]
    assert sent_messages


async def test_reconnect_spike_quarantines_before_ban(db, sent_messages):
    add(db, "+7700", "active")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "quarantined"


async def test_healthy_number_is_left_alone(db, sent_messages):
    add(db, "+7700", "active")
    report = {"+7700": {"state": "connected", "reconnects": 1}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"
    assert sent_messages == []


async def test_banned_number_is_not_revived_by_a_good_report(db, sent_messages):
    """banned — терминальный: вернувшийся номер по репутации является новым."""
    add(db, "+7700", "banned")
    report = {"+7700": {"state": "connected", "reconnects": 0}}

    await health.check(db, transport_reporting(report), CONFIG)

    assert numbers.get(db, "+7700")["status"] == "banned"


async def test_quarantine_is_not_announced_twice(db, sent_messages):
    """Монитор ходит раз в час: повторный вердикт по тому же номеру не должен
    капать в телеграм, иначе эскалации утонут в шуме."""
    add(db, "+7700", "quarantined")
    spike = CONFIG["health"]["reconnects_per_day_alert"] + 1
    report = {"+7700": {"state": "connected", "reconnects": spike}}

    events = await health.check(db, transport_reporting(report), CONFIG)

    assert events == []
    assert sent_messages == []


async def test_number_missing_from_report_is_left_alone(db, sent_messages):
    """Номер, о котором Node молчит, — это не диагноз: сокет мог ещё не подняться."""
    add(db, "+7700", "active")

    events = await health.check(db, transport_reporting({}), CONFIG)

    assert events == []
    assert numbers.get(db, "+7700")["status"] == "active"
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'health'`

- [ ] **Step 3: Написать монитор**

`backend/sender/services/health.py`:

```python
"""Здоровье номеров: сверка отчёта Node с пулом раз в час.

Единственный ранний сигнал — реконнекты и запросы повторной авторизации:
отключение связанных устройств задокументировано как признак приближающейся
блокировки. Всё остальное (delivered rate, reply rate) констатирует задним
числом и появится в части 2, когда будет что считать.
"""

import logging
import sqlite3
from dataclasses import dataclass

from sender import notify
from sender.db import numbers

log = logging.getLogger(__name__)

TERMINAL = ("banned",)


@dataclass(frozen=True)
class Event:
    number: str
    status: str
    reason: str


async def check(db: sqlite3.Connection, transport, config: dict) -> list[Event]:
    report = await transport.health()
    events = [event for row in numbers.all(db)
              if (event := _verdict(row, report.get(row["number"]), config["health"]))]
    for event in events:
        numbers.set_status(db, event.number, event.status, note=event.reason)
        log.warning("номер %s -> %s: %s", event.number, event.status, event.reason)
        await notify.send(f"Номер {event.number} → {event.status}: {event.reason}")
    return events


def _verdict(row: dict, state: dict | None, thresholds: dict) -> Event | None:
    """None — трогать нечего. Номер, о котором Node молчит, не диагностируется:
    сокет мог ещё не подняться, а карантин по молчанию остановил бы пул."""
    if row["status"] in TERMINAL or state is None:
        return None
    if state["state"] == "loggedOut":
        return Event(row["number"], "banned", "loggedOut от транспорта")
    if state["reconnects"] > thresholds["reconnects_per_day_alert"]:
        if row["status"] == "quarantined":
            return None   # уже в карантине: второе уведомление о том же — шум
        return Event(row["number"], "quarantined",
                     f"реконнектов за сутки: {state['reconnects']}")
    return None
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_health.py -v`
Expected: 6 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/health.py backend/sender/tests/test_health.py
git commit -m "feat(sender): монитор здоровья номеров и карантин до бана"
```

---

## Task 10: Внутренний прогрев — номера пишут друг другу

**Files:**
- Modify: `backend/sender/services/warmup.py`, `backend/sender/config.toml`
- Test: `backend/sender/tests/test_warmup_run.py`

**Interfaces:**
- Consumes: `warmup.plan` (Task 6), `numbers.all/set_status/sent_today` (Task 3), `pool.capacity` (Task 7), `transport.Transport.send` (Task 4).
- Produces:
  - `async warmup.tick(db, transport, config: dict, now: datetime) -> str | None` — одна прогревочная отправка за тик; возвращает номер, с которого ушло, или `None`, если сегодня никому не положено.
  - `async warmup.loop(db_factory, transport_factory, interval_seconds: int) -> None` — бесконечный цикл вокруг `tick`.
  - `warmup.PHRASES_KEY = "phrases"` — тексты внутренних сообщений живут в `config.toml`, а не в коде.

  Прогрев планирует Python: он решает, какой номер кому пишет и когда, и дёргает
  тот же `/send`. Для Node внутренняя переписка неотличима от боевой — поэтому
  прогревочная отправка кладёт строку в `outbox` (`message_id IS NULL`) и тратит
  дневной лимит наравне с боевой.

- [ ] **Step 1: Добавить тексты в конфиг**

В `backend/sender/config.toml`, в блок `[warmup]`:

```toml
# Тексты внутренней переписки. Прогрев требует разнообразия: одинаковые
# сообщения между одними и теми же номерами — сами по себе сигнал.
phrases = [
  "привет, как дела",
  "созвонимся завтра?",
  "принял, спасибо",
  "хорошо, договорились",
  "буду через час",
  "ок, посмотрю и отвечу",
]
```

- [ ] **Step 2: Написать падающий тест**

Создать `backend/sender/tests/test_warmup_run.py`:

```python
"""Прогревочный тик: кто кому пишет и что при этом тратится.

Главная проверка снова отрицательная — номер в socket_delay и passive не
отправляет ничего. Дни 2-4 новый номер только принимает; исходящие начинаются
с пятого дня и только своим.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.db import migrate, numbers
from sender.services import config, warmup

CONFIG = config.load()
STARTED = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


class FakeTransport:
    def __init__(self, sent=True):
        self.calls = []
        self._sent = sent

    async def send(self, number, to, text, key, kind="text"):
        self.calls.append({"number": number, "to": to, "text": text, "kind": kind})
        from sender.transport import Sent
        return Sent(sent=self._sent, provider_id="3EB0" if self._sent else None,
                    error=None if self._sent else "loggedOut")


def at(day: int) -> datetime:
    return STARTED + timedelta(days=day - 1, hours=3)


def add(db, number, status, started=STARTED):
    numbers.register(db, number, f"sessions/{number}", started)
    numbers.set_status(db, number, status)


async def test_socket_delay_day_sends_nothing(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(1)) is None
    assert transport.calls == []


async def test_passive_days_send_nothing(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(3)) is None
    assert transport.calls == []


async def test_internal_phase_writes_to_another_of_our_numbers(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) == "+7700"
    assert transport.calls[0]["to"] == "+7701"
    assert transport.calls[0]["text"] in CONFIG["warmup"]["phrases"]


async def test_successful_send_spends_the_daily_limit(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()

    await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, "+7700", at(5)) == 1


async def test_failed_send_does_not_spend_the_limit(db):
    """sent=False — фрейм в сокет не ушёл; лимит тратит доставка, а не попытка."""
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport(sent=False)

    await warmup.tick(db, transport, CONFIG, at(5))

    assert numbers.sent_today(db, "+7700", at(5)) == 0


async def test_exhausted_number_is_skipped(db):
    add(db, "+7700", "warming")
    add(db, "+7701", "active")
    transport = FakeTransport()
    for _ in range(CONFIG["warmup"]["internal_ramp"][0]):
        await warmup.tick(db, transport, CONFIG, at(5))

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None


async def test_lonely_number_has_nobody_to_write_to(db):
    """Курица и яйцо: первый номер греется руками, автомату писать некому."""
    add(db, "+7700", "warming")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None
    assert transport.calls == []


async def test_banned_number_never_warms(db):
    add(db, "+7700", "banned")
    add(db, "+7701", "active")
    transport = FakeTransport()

    assert await warmup.tick(db, transport, CONFIG, at(5)) is None


async def test_new_number_becomes_warming_when_the_delay_passes(db):
    """Статус догоняет календарь сам: сутки молчания прошли — номер греется."""
    add(db, "+7700", "new")
    add(db, "+7701", "active")

    await warmup.tick(db, FakeTransport(), CONFIG, at(5))

    assert numbers.get(db, "+7700")["status"] == "warming"
```

- [ ] **Step 3: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_warmup_run.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.warmup' has no attribute 'tick'`

- [ ] **Step 4: Дописать прогрев в `warmup.py`**

Добавить в `backend/sender/services/warmup.py` (импорты — вверх файла):

```python
import asyncio
import logging
import random
import sqlite3

from sender.db import numbers

log = logging.getLogger(__name__)

WARMING_STATUSES = ("new", "warming")
REACHABLE_STATUSES = ("warming", "active")


async def tick(db: sqlite3.Connection, transport, config: dict,
               now: datetime) -> str | None:
    """Одна прогревочная отправка. None — сегодня никому не положено.

    Одна за тик, а не пачка: прогрев, отправляющий шесть сообщений подряд,
    отличается от живого общения ровно тем, из-за чего номера и банят.
    """
    sender_number = _next_sender(db, config, now)
    if sender_number is None:
        return None
    recipient = _recipient(db, sender_number)
    if recipient is None:
        log.info("прогрев %s: писать некому, первый номер греется руками",
                 sender_number)
        return None

    stamp = numbers.stamp(now)
    result = await transport.send(
        sender_number, recipient,
        random.choice(config["warmup"][PHRASES_KEY]),
        key=f"warmup-{sender_number}-{stamp}")
    if not result.sent:
        log.warning("прогрев %s -> %s не ушёл: %s", sender_number, recipient,
                    result.error)
        return None

    db.execute(
        "INSERT INTO outbox (our_number, send_after, status, provider_id,"
        " created_at, updated_at) VALUES (?, ?, 'sent', ?, ?, ?)",
        (sender_number, stamp, result.provider_id, stamp, stamp))
    db.commit()
    return sender_number


def _next_sender(db: sqlite3.Connection, config: dict, now: datetime) -> str | None:
    """Наименее загруженный сегодня греющийся номер, которому ещё положено.

    Импорт pool здесь, а не наверху файла, — не стиль, а разрыв цикла:
    pool импортирует warmup на уровне модуля ради календаря. Поднимете этот
    импорт вверх — получите ImportError на старте процесса.
    """
    from sender.services import pool

    candidates = []
    for row in numbers.all(db):
        if row["status"] not in WARMING_STATUSES:
            continue
        current = plan(row["started_at"], now, config["warmup"])
        if current.phase is not Phase.internal:
            continue
        if row["status"] == "new":
            numbers.set_status(db, row["number"], "warming")
        left = pool.capacity(db, row["number"], now, config)
        if left > 0:
            candidates.append((left, row["number"]))
    return max(candidates)[1] if candidates else None


def _recipient(db: sqlite3.Connection, sender_number: str) -> str | None:
    """Пишем только своим и только тем, кто способен принять."""
    others = [row["number"] for row in numbers.all(db)
              if row["number"] != sender_number
              and row["status"] in REACHABLE_STATUSES]
    return random.choice(others) if others else None


async def loop(db_factory, transport_factory, interval_seconds: int) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и остановившийся прогрев обнаружился бы датой, когда номер так и не
    стал боевым."""
    while True:
        try:
            db = db_factory()
            try:
                await tick(db, transport_factory(), _config(), _now())
            finally:
                db.close()
        except Exception:
            log.exception("прогрев упал на тике")
        await asyncio.sleep(interval_seconds)
```

Вверху файла, рядом с `Phase`:

```python
PHRASES_KEY = "phrases"
```

`_config` и `_now` — локальные импорты-обёртки, чтобы `loop` не тянул конфиг на
уровень модуля (тесты `tick` конфиг подают сами):

```python
def _config() -> dict:
    from sender.services import config
    return config.load()


def _now() -> datetime:
    return datetime.now(timezone.utc)
```

и `from datetime import date, datetime, timezone` вместо прежнего импорта.

- [ ] **Step 5: Прогнать тесты, убедиться, что проходят**

Run: `cd backend && uv run pytest sender/tests/test_warmup.py sender/tests/test_warmup_run.py -v`
Expected: 12 + 9 passed

- [ ] **Step 6: Проверить, что граф импортов не покраснел**

Run: `cd backend && uv run pytest sender/tests/test_import_graph.py -v`
Expected: 2 passed — `warmup.py` получает транспорт аргументом и `httpx` не импортирует

- [ ] **Step 7: Коммит**

```bash
git add backend/sender/services/warmup.py backend/sender/config.toml backend/sender/tests/test_warmup_run.py
git commit -m "feat(sender): внутренний прогрев — номера пишут друг другу по календарю"
```

---

## Task 11: Роутер вместо стаба, часовой монитор, счётчики

**Files:**
- Create: `backend/sender/routes/__init__.py`, `backend/sender/routes/sender.py`
- Delete: `backend/sender/stub.py`
- Modify: `backend/collector/api.py:26`, `backend/collector/api.py:72`, `backend/collector/api.py:43-49` (lifespan), `backend/collector/services/metrics.py:36`
- Test: `backend/sender/tests/test_routes.py`

**Interfaces:**
- Consumes: всё предыдущее — `migrate.connect`, `numbers`, `pool.capacity`, `warmup.plan`, `transport.build`, `health.check`.
- Produces:
  - `GET /api/sender` → `{"status": "live", "autopilot": "off", "numbers": [...]}`; каждый номер — `{number, status, started_at, day, phase, daily_limit, sent_today, capacity, note}`.
  - `POST /api/sender/numbers` `{number}` → регистрирует номер в статусе `new`, возвращает карточку.
  - `POST /api/sender/numbers/{number}/pair` → `{"code": "ABCD-1234"}`.
  - `POST /api/sender/numbers/{number}/status` `{status, note}` → ручной перевод (`warming` → `active` после прогрева, возврат из карантина).
  - `sender.routes.sender.monitor_numbers()` — корутина часового цикла для lifespan.
  - `metrics.snapshot()["sender"]` → `{"status": "live", "numbers": {"active": 2, "warming": 1, ...}}`.

  Фронтенд по-прежнему не хардкодит содержимое раздела: он рисует то, что
  приехало из `GET /api/sender`. Меняется только ответ — с «скоро» на живой пул.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sender/tests/test_routes.py`:

```python
"""Веб-контур системы 3: пул наружу, регистрация номера, pairing code."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sender.db import migrate, numbers
from sender.routes import sender as routes

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = migrate.connect(tmp_path / "state.db")
    monkeypatch.setattr(routes, "connect", lambda: db)
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app), db
    db.close()


def test_status_lists_the_pool_with_warmup_day(client):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    body = http.get("/api/sender").json()

    assert body["status"] == "live"
    assert body["autopilot"] == "off"
    assert body["numbers"][0]["number"] == "+77001112233"
    assert body["numbers"][0]["day"] == 1
    assert body["numbers"][0]["phase"] == "socket_delay"


def test_register_number_creates_it_in_new(client):
    http, db = client

    created = http.post("/api/sender/numbers", json={"number": "+77001112233"}).json()

    assert created["status"] == "new"
    assert numbers.get(db, "+77001112233")["status"] == "new"


def test_register_rejects_duplicate(client):
    http, _ = client
    http.post("/api/sender/numbers", json={"number": "+77001112233"})

    response = http.post("/api/sender/numbers", json={"number": "+77001112233"})

    assert response.status_code == 409


def test_pair_returns_code_from_transport(client, monkeypatch):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    class FakeTransport:
        async def pair(self, number):
            return "ABCD-1234"

    monkeypatch.setattr(routes, "build_transport", lambda: FakeTransport())

    assert http.post("/api/sender/numbers/+77001112233/pair").json() == {"code": "ABCD-1234"}


def test_pair_of_unknown_number_is_404(client, monkeypatch):
    http, _ = client

    class FakeTransport:
        async def pair(self, number):
            raise AssertionError("транспорт не должен зваться на неизвестный номер")

    monkeypatch.setattr(routes, "build_transport", lambda: FakeTransport())

    assert http.post("/api/sender/numbers/+70000000000/pair").status_code == 404


def test_set_status_rejects_unknown_status(client):
    http, db = client
    numbers.register(db, "+77001112233", "sessions/x", NOW)

    response = http.post("/api/sender/numbers/+77001112233/status",
                         json={"status": "почти активен"})

    assert response.status_code == 422
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `cd backend && uv run pytest sender/tests/test_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.routes'`

- [ ] **Step 3: Написать роутер**

`backend/sender/routes/__init__.py` — пустой файл.

`backend/sender/routes/sender.py`:

```python
"""Веб-контур системы 3. Раздел «Отправка» перестаёт быть заглушкой на 501.

Фронтенд по-прежнему не знает содержимого раздела и рисует то, что приехало из
GET /api/sender — меняется ответ, а не договорённость.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sender.db import migrate, numbers
from sender.services import config, health, pool, warmup
from sender.transport import build as build_transport

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 3600


class NewNumber(BaseModel):
    number: str


class NewStatus(BaseModel):
    status: str
    note: str | None = None


def connect() -> sqlite3.Connection:
    return migrate.connect(config.load()["state_db"])


def now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
def status() -> dict:
    settings = config.load()
    db = connect()
    try:
        return {
            "status": "live",
            "autopilot": settings["autopilot"]["mode"],
            "numbers": [card(db, row, settings) for row in numbers.all(db)],
        }
    finally:
        db.close()


@router.post("/numbers", status_code=201)
def register(body: NewNumber) -> dict:
    settings = config.load()
    db = connect()
    try:
        numbers.get(db, body.number)
    except numbers.UnknownNumberError:
        numbers.register(db, body.number, f"sessions/{body.number}", now())
        card_of = card(db, numbers.get(db, body.number), settings)
        db.close()
        return card_of
    db.close()
    raise HTTPException(409, f"номер {body.number} уже в пуле")


@router.post("/numbers/{number}/pair")
async def pair(number: str) -> dict:
    db = connect()
    try:
        numbers.get(db, number)
    except numbers.UnknownNumberError:
        raise HTTPException(404, f"номера {number} нет в пуле") from None
    finally:
        db.close()
    return {"code": await build_transport().pair(number)}


@router.post("/numbers/{number}/status")
def set_status(number: str, body: NewStatus) -> dict:
    settings = config.load()
    db = connect()
    try:
        numbers.set_status(db, number, body.status, body.note)
        return card(db, numbers.get(db, number), settings)
    except numbers.UnknownNumberError:
        raise HTTPException(404, f"номера {number} нет в пуле") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    finally:
        db.close()


def card(db: sqlite3.Connection, row: dict, settings: dict) -> dict:
    """Строка пула для дашборда: статус плюс то, что из него не видно, —
    день прогрева, фаза и остаток дневного лимита."""
    moment = now()
    plan = warmup.plan(row["started_at"], moment, settings["warmup"])
    return {
        **row,
        "day": plan.day,
        "phase": str(plan.phase),
        "daily_limit": plan.daily_limit,
        "sent_today": numbers.sent_today(db, row["number"], moment),
        "capacity": pool.capacity(db, row["number"], moment, settings),
    }


async def monitor_numbers() -> None:
    """Часовой цикл здоровья. Тело целиком в try/except: упавшая asyncio-задача
    исчезает без строки в логе, и бан номера обнаружился бы через сутки."""
    while True:
        try:
            db = connect()
            try:
                await health.check(db, build_transport(), config.load())
            finally:
                db.close()
        except Exception:
            log.exception("монитор здоровья номеров упал на тике")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `cd backend && uv run pytest sender/tests/test_routes.py -v`
Expected: 6 passed

- [ ] **Step 5: Заменить стаб в приложении**

Удалить `backend/sender/stub.py`.

В `backend/collector/api.py` заменить строку 26:

```python
from sender.routes import sender
```

Строка 72 остаётся `app.include_router(sender.router)` — имя не меняется.

В `lifespan` (`backend/collector/api.py:41-47`) добавить часовой монитор рядом
с воркером очереди — целиком функция становится такой:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue.fail_orphans()
    worker = asyncio.create_task(queue.worker_loop())
    monitor = asyncio.create_task(sender.monitor_numbers())
    warming = asyncio.create_task(sender.warm_numbers())
    yield
    queue.cancel_current()  # без этого фоновый поток текущей джобы держит процесс живым
    worker.cancel()
    monitor.cancel()
    warming.cancel()
```

Обе задачи sender'а — обёртки в `sender/routes/sender.py`, чтобы `api.py` не
знал ни про конфиг системы 3, ни про способ добыть соединение:

```python
async def warm_numbers() -> None:
    await warmup.loop(connect, build_transport,
                      config.load()["warmup"]["tick_minutes"] * 60)
```

- [ ] **Step 6: Отдать счётчики пула в `/api/stats`**

В `backend/collector/services/metrics.py`, в `snapshot()`, заменить строку
`"sender": {"status": "coming_soon"},` на `"sender": sender_stats(),` и добавить
функцию рядом с `writer_stats()`:

```python
def sender_stats():
    """Номера по статусам. Путь до state.db читается так же, как у writer'а, —
    из конфига системы, а не импортом её модулей."""
    db_path = threads_db_path()
    if not db_path.exists():
        return {"status": "live", "numbers": {}}
    with closing(sqlite3.connect(db_path)) as db:
        rows = db.execute(
            "SELECT status, count(*) FROM numbers GROUP BY status").fetchall()
    return {"status": "live", "numbers": dict(rows)}
```

Таблицы `numbers` может не быть на базе, где sender ещё ни разу не стартовал —
дашборд не должен падать целиком из-за раздела, который не поднимали:

```python
def sender_stats():
    db_path = threads_db_path()
    if not db_path.exists():
        return {"status": "live", "numbers": {}}
    with closing(sqlite3.connect(db_path)) as db:
        try:
            rows = db.execute(
                "SELECT status, count(*) FROM numbers GROUP BY status").fetchall()
        except sqlite3.OperationalError:
            return {"status": "live", "numbers": {}}
    return {"status": "live", "numbers": dict(rows)}
```

(вариант выше заменяет тот, что в предыдущем блоке — писать сразу этот)

- [ ] **Step 7: Прогнать весь набор**

Run: `cd backend && uv run pytest`
Expected: всё зелёное; `collector/tests/test_web.py` и `test_jobs.py` могут
ожидать `coming_soon` — если да, поправить ожидание на `live` вместе с этим шагом.

- [ ] **Step 8: Коммит**

```bash
git add backend/sender backend/collector/api.py backend/collector/services/metrics.py
git rm backend/sender/stub.py
git commit -m "feat(sender): роутер пула вместо стаба, часовой монитор здоровья"
```

---

## Task 12: Страница «Отправка» — пул вместо заглушки

**Files:**
- Modify: `frontend/app/sender/page.tsx`, `frontend/app/api.ts`
- Test: ручная проверка в браузере (в проекте нет фронтового раннера)

**Interfaces:**
- Consumes: `GET /api/sender` из Task 11.
- Produces: страница `/sender` со списком номеров, днём прогрева, фазой, остатком дневного лимита и кнопкой регистрации номера.

- [ ] **Step 1: Обновить типы и клиент**

В `frontend/app/api.ts` заменить тип `SenderStatus` (сейчас — `{status, title, planned}`):

```ts
export type SenderNumber = {
  number: string;
  status: "new" | "warming" | "active" | "quarantined" | "banned";
  started_at: string;
  day: number;
  phase: "socket_delay" | "passive" | "internal" | "cold";
  daily_limit: number;
  sent_today: number;
  capacity: number;
  note: string | null;
};

export type SenderStatus = {
  status: string;
  autopilot: "off" | "replies" | "full";
  numbers: SenderNumber[];
};
```

Добавить рядом с `fetchSender`:

```ts
export async function registerNumber(number: string): Promise<SenderNumber> {
  return post("/api/sender/numbers", { number });
}

export async function pairNumber(number: string): Promise<{ code: string }> {
  return post(`/api/sender/numbers/${encodeURIComponent(number)}/pair`, {});
}
```

`post<T>` — существующий хелпер в `frontend/app/api.ts:251`; своего `fetch` с
руками выставленными заголовками не заводить.

- [ ] **Step 2: Переписать страницу**

`frontend/app/sender/page.tsx` — заменить содержимое:

```tsx
"use client";

/** Система 3: отправка. Пул номеров с днём прогрева и остатком дневного
 * лимита. Содержимое по-прежнему целиком приезжает с бэкенда (GET /api/sender),
 * страница не знает ни календаря прогрева, ни порогов.
 */

import { useCallback, useEffect, useState } from "react";
import { fetchSender, pairNumber, registerNumber, type SenderStatus } from "../api";

const PHASE_LABEL: Record<string, string> = {
  socket_delay: "сокет не привязан",
  passive: "только входящие",
  internal: "внутренний прогрев",
  cold: "боевые касания",
};

export default function Sender() {
  const [status, setStatus] = useState<SenderStatus | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [number, setNumber] = useState("");

  const reload = useCallback(() => {
    fetchSender().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(reload, [reload]);

  return (
    <>
      <header className="page-head">
        <h1>Отправка</h1>
        <span className="page-sub">
          система 3 · автопилот: {status?.autopilot ?? "…"}
        </span>
      </header>

      <section className="card">
        <h2>Пул номеров</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Номер</th><th>Статус</th><th>День</th><th>Фаза</th>
              <th>Сегодня</th><th>Осталось</th>
            </tr>
          </thead>
          <tbody>
            {(status?.numbers ?? []).map((row) => (
              <tr key={row.number}>
                <td className="mono">{row.number}</td>
                <td>{row.status}</td>
                <td>{row.day}</td>
                <td>{PHASE_LABEL[row.phase] ?? row.phase}</td>
                <td>{row.sent_today} / {row.daily_limit}</td>
                <td>{row.capacity}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Подключить номер</h2>
        <input
          className="input mono"
          placeholder="+77001112233"
          value={number}
          onChange={(event) => setNumber(event.target.value)}
        />
        <button
          className="btn"
          onClick={() => registerNumber(number).then(reload)}
          disabled={!number}
        >
          Добавить в пул
        </button>
        <button
          className="btn"
          onClick={() => pairNumber(number).then((body) => setCode(body.code))}
          disabled={!number}
        >
          Получить код привязки
        </button>
        {code && (
          <p className="mono">
            Введите на телефоне: WhatsApp → Связанные устройства → Привязка по коду → {code}
          </p>
        )}
      </section>
    </>
  );
}
```

Классы (`card`, `table`, `input`, `btn`, `mono`) — те, что уже есть в
`globals.css`. Своих стилей не добавлять; если нужного класса нет, взять
ближайший из страницы `/writer`.

- [ ] **Step 2a: Убрать мёртвые стили заглушки**

Если `soon-hero`, `soon-list`, `badge-pill` в `globals.css` больше нигде не
используются (`grep -rn "soon-hero\|soon-list" frontend/app`) — удалить их.
Зона «скоро» осталась только у системы, которой больше нет.

- [ ] **Step 3: Ручная проверка**

```bash
cd backend && uv run python main.py            # терминал 1
cd frontend && npm run dev                     # терминал 2
```

1. `http://localhost:3000/sender` — таблица пуста, ошибок в консоли нет.
2. Добавить номер → строка появилась, статус `new`, день 1, фаза
   «сокет не привязан», лимит 0.
3. «Получить код привязки» при поднятом Node → на экране код.
4. `curl -s localhost:8787/api/stats | jq .sender` → номера по статусам.

- [ ] **Step 4: Коммит**

```bash
git add frontend/app/sender/page.tsx frontend/app/api.ts frontend/app/globals.css
git commit -m "feat(frontend): страница отправки показывает пул номеров"
```

---

## Task 13: Документация

**Files:**
- Modify: `CLAUDE.md`, `docs/PRD.md`

- [ ] **Step 1: Переписать секцию системы 3 в `CLAUDE.md`**

В списке трёх систем заменить описание системы 3:

```markdown
3. **Sending infrastructure** — WhatsApp как главный канал: пул номеров,
   прогрев SIM, очередь исходящих, автопилот. Живёт в `sender/`.
```

И заменить строку «Система 3 заявлена стабом `sender/stub.py`» в секции
«Веб как обвязка» на описание того, что появилось: пул номеров в `state.db`,
`transport.py` как единственная точка выхода в сеть, Node-сервис на Baileys
отдельным процессом, часовой монитор здоровья в lifespan.

Добавить в «Команды» запуск Node:

```bash
cd backend/sender/node && npm start   # сокеты WhatsApp, порт 8788
```

Добавить в «Слои данных» строку: `backend/sender/node/sessions/` —
невосстановимый слой (ключи сессий WhatsApp), не в git, в тот же бэкап, что
`state.db`.

- [ ] **Step 2: Отметить в `docs/PRD.md`**

Требования системы 3, касающиеся транспорта и номеров, перевести из «заявлено»
в «реализуется»; требования очереди и автопилота оставить как есть — они часть 2.

- [ ] **Step 3: Коммит**

```bash
git add CLAUDE.md docs/PRD.md
git commit -m "docs: система 3 — транспорт и пул номеров вместо стаба"
```

---

## Definition of Done

- [ ] `cd backend && uv run pytest` — зелёный, включая `sender/tests/` (≈50 тестов в девяти файлах).
- [ ] `sender/tests/test_import_graph.py` подтверждает: в сеть ходят только `transport.py` и `notify.py`.
- [ ] Ручной смоук Task 5 пройден: сообщение самому себе доставлено.
- [ ] На SIM сохранены в контакты остальные наши номера: прогрев требует
      общения именно с контактами, а в multi-device контакты подтягиваются
      с телефона.
- [ ] `GET /api/sender` отдаёт живой пул, `sender/stub.py` удалён, 501 больше нет.
- [ ] Номер зарегистрирован, `sessions/<number>/` непуст и не в git.
- [ ] `CLAUDE.md` больше не обещает домены, DNS и прогрев почтовых ящиков.

## Чего в этой части нет — и это не забыто

- Очередь как очередь: постановка, гейты, `autopilot`, воркер — часть 2.
- Метрики delivered / reply rate в мониторе здоровья — считать нечего, пока не
  отправляется боевое; вход для них (`outbox.delivered_at`) уже есть.
- Открытие треда с `transport.check()` и `pool.assign()` — часть 2: там
  меняется пайплайн `write`, и туда же приезжает состояние `unreachable`.
- Переезд тредов при бане номера (холодные — на новый номер, с ответами — в
  `escalated`) — часть 2: двигать `threads.status` нечем, пока колонки нет.
- Обработчик `POST /api/sender/webhook` — часть 3. До неё Node шлёт события в
  ручку, которой нет, получает 404 и повторяет; на прогрев это не влияет, но
  залогировать это в Node стоит один раз, а не каждые пять секунд.
