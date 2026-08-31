# Sender, часть 2: очередь исходящих и воркер — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Замкнуть путь «черновик → лид»: оператор жмёт кнопку в карточке треда, сообщение уходит в окно 10:00–18:00 с прогретого номера в пределах дневного лимита, `sent_text` заполняется только после подтверждения транспорта, а `autopilot = off` останавливает всё за одно нажатие.

**Architecture:** Одна asyncio-задача в lifespan, тик раз в 20 секунд, одна строка очереди за тик — при двух сообщениях в час последовательная обработка бесплатно снимает все гонки. Решение об отправке принимают чистые функции (`gates.py`), в сеть ходит только `transport.py`, а состояние живёт в `state.db`: `outbox` пришла с частью 1, колонки состояния добавляются идемпотентным `ensure_column`. Постановка в очередь — единственная функция (`services/queue.py`), общая для кнопки оператора и автопилота, поэтому путь отправки ровно один.

**Tech Stack:** Python 3.13, FastAPI, `sqlite3`, `zoneinfo`/`datetime` (stdlib), `tomllib`, pytest + pytest-asyncio (`asyncio_mode = "auto"`). Новых зависимостей часть не добавляет.

**Spec:** `docs/superpowers/specs/2026-08-28-sender-outbox-worker-design.md`
(зонтик: `docs/superpowers/specs/2026-08-23-sender-whatsapp-design.md`;
часть 1, обязательная предшественница: `docs/superpowers/specs/2026-08-28-sender-transport-and-numbers-design.md`)

## Global Constraints

- **Одна строка за тик, параллелизма нет.** Тик берёт ровно одну созревшую строку. Захват — `UPDATE outbox SET status='sending' WHERE outbox_id=? AND status='pending'`: ноль обновлённых строк значит, что её взял кто-то другой, и мы молча уходим.
- **`sender/transport.py` — единственная точка выхода в сеть.** Воркер, `queue.py` и вебхук получают transport аргументом; `sender/tests/test_import_graph.py` это сторожит.
- **Новых таблиц эта часть не создаёт.** `outbox` и `numbers` пришли с частью 1, DDL не меняется. Колонки в чужих таблицах — только через `sender/db/migrate.py::ensure_column()`. `DROP` в `state.db` запрещён.
- **Функции `sender/db/outbox.py` и `sender/db/conversation.py` не коммитят.** Коммитит вызывающий через `with db:`. Так выполняется правило 2 зонтичной спеки: `threads.status` меняется в одной транзакции с записью в `messages`/`outbox`, иначе падение процесса между двумя коммитами даёт тред в `active` без отправленного сообщения. (`sender/db/numbers.py` из части 1 коммитит внутри — его конвенцию не трогаем.)
- **«Сейчас» — аргумент, не `datetime.now()` внутри.** `now: datetime` параметром у всех функций гейтов, очереди и воркера; `now()` зовётся один раз на границе (роутер, цикл).
- **Часовой пояс.** В базе всё UTC, ISO-8601 с точностью до секунд (`timespec="seconds"`), как `numbers.stamp`. `Asia/Almaty` из конфига используется ровно в одном месте — гейте окна отправки.
- **`config.toml` — единственное место конфигурации.** Значения копируются дословно: `[window] timezone = "Asia/Almaty"`, `hours = [10, 18]`, `weekdays = [1, 2, 3, 4, 5]`; `[pace] tick_seconds = 20`, `jitter_minutes = [2, 15]`; `[retry] backoff_minutes = [1, 5, 30]`, `stuck_after_minutes = 5`; `[cadence] max_touches = 3`.
- **`autopilot` читается каждым тиком заново.** Kill switch, который надо перезапускать процессом, — не kill switch.
- **Отмена и перенос — разные вещи.** `cancelled` — «это сообщение уже не нужно», перенос `send_after` — «нужно, но не сейчас». Их подмена друг другом либо спамит, либо тихо теряет follow-up; тесты проверяют раздельно.
- **Не переотправлять автоматически.** Дубликат в холодном аутриче — прямой повод нажать Report, потеря сообщения — минус один лид из пятидесяти. Ретрай только на `sent: false` от Node (сообщение точно не ушло); неопределённость (`TransportError`, зависшая `sending`) уходит человеку статусом `stuck`.
- **Тесты без сети.** `FakeTransport` из `sender/tests/`, «сейчас» — константа. Живой WhatsApp — ручной смоук-чеклист Task 14, не pytest.
- **Статусы треда:** `queued | active | exhausted | escalated | unreachable | closed_refused | closed_junk | blocked_channel`. Из `escalated` автоматического выхода нет.

## Решения, принятые поверх спеки (согласованы с заказчиком 2026-08-29)

1. **Постановка в очередь живёт в sender, а не в writer.** `POST /api/threads/{company_id}/sent` удаляется; появляется `POST /api/sender/queue`. Причина: иначе writer начинает импортировать sender и ходить в сеть, а он не импортирует даже collector. Направление зависимостей остаётся односторонним — sender читает и правит `threads`/`messages`, writer о sender'е не знает.
2. **Правленый оператором текст — новая колонка `messages.queued_text`.** `draft_text` — что предложила модель (не меняется), `queued_text` — что подтвердил оператор (его шлёт воркер), `sent_text` — что реально ушло. Перезапись `draft_text` обесценила бы разметку draft/sent, на которой стоит калибровка промпта этапа 3 выката.
3. **`sent_text` заполняется по `sent: true` от транспорта.** Плюс в этой части появляется `sender/routes/webhook.py`, обрабатывающий **только** `kind: "status"`: `delivered_at`/`read_at` и переход `queued → active` по доставке. `kind: "incoming"` отвечает 200 и пишет строку в лог — иначе Node будет ретраить входящие вечно (`WEBHOOK_MAX_ATTEMPTS` в `node/index.js`), а агента-продавца подключит часть 3.
4. **Kill switch — файл `sender/autopilot`.** `config.toml` остаётся значением по умолчанию, файл — переопределением; `POST /api/sender/autopilot` пишет его атомарно (write + `Path.replace`). Перезапись `config.toml` роботом потребовала бы `tomli-w` и стирала бы комментарии, которые правит человек.
5. **`transport.check()` и `pool.assign()` — в момент постановки в очередь, а не в пайплайне `write`.** Спека относит их к открытию треда, но операция `writer.outreach` исполняется синхронно в `asyncio.to_thread`, а `transport.check` асинхронный и его клиент закэширован на главный цикл. `POST /api/sender/queue` — уже async-ручка того же цикла: проверка делается там, оператор сразу видит «у этого номера нет WhatsApp» вместо строки, которая тихо отменится через двадцать секунд. Проверка по-прежнему одна на лида: её результат — `threads.our_number` (живой) или `status='unreachable'` (мёртвый), и то и другое проверяется до вызова.
6. **Тик сам ставит в очередь только холодные первые касания и только при `autopilot = full`.** `replies` остаётся валидным значением и проверяется гейтом, но ставить по нему пока нечего — ответы в диалоге приносит часть 3.

## Отступления от буквы спеки — и почему

- **`threads.status` объявляется с `DEFAULT 'queued'`, а не `DEFAULT 'escalated'`.** Намерение спеки — «треды, существовавшие до системы 3, вёл человек, миграция не имеет права отдать их роботу». Буквальный `DEFAULT 'escalated'` сделал бы `escalated`'ом и каждый **новый** тред, а из `escalated` автомат по правилу 1 уже не выходит — система бы не отправила ничего никогда. Поэтому намерение реализуется одноразовым бэкфиллом: в момент, когда колонка создаётся (`ensure_column` вернул True), существующие строки получают `'escalated'` одним `UPDATE`. Новые треды, которые `thread_store.open_thread` вставляет, ничего не зная о колонке, приезжают в `queued`. Плюс: writer не учит имён колонок sender'а.
- **Heartbeat воркера живёт в памяти процесса, а не в базе.** Спека просит «время последнего тика в базе», но часть 2 по сквозному контракту не заводит своих таблиц, а колонка в чужой ради одного скаляра — хуже. Симптом, который heartbeat ловит (воркер умер, процесс жив), полностью виден изнутри процесса: `/api/stats` отдаёт его тот же самый процесс. Переживать перезапуск heartbeat'у незачем — воркер поднимается вместе с ним.
- **`unreachable` выставляет постановка в очередь, а не открытие треда** — см. решение 5 выше.
- **`exhausted` выставляется в момент отправки последнего разрешённого касания** (`touch_no` дошёл до `max_touches`, ответов нет). Каденцию и генерацию follow-up приносит часть 3; здесь механика перехода реализована и покрыта тестом, чтобы часть 3 её только использовала.

---

## Task 1: Миграция — колонки состояния треда и сообщения

**Files:**
- Modify: `backend/sender/db/migrate.py`
- Test: `backend/sender/tests/test_migrate.py`

**Interfaces:**
- Consumes: `migrate.connect(path)`, `migrate.ensure_column(db, table, column, ddl)` из части 1.
- Produces: после `migrate.apply(db)` в `state.db` есть `threads.status` (`queued` по умолчанию, у существовавших строк `escalated`), `threads.our_number`, `threads.auto_replies`, `threads.next_touch_at`, `threads.touch_no`, `messages.provider_id`, `messages.handled_at`, `messages.queued_text` и уникальный индекс `messages_provider`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_migrate.py`:

```python
def conversation_tables(db):
    """Таблицы переписки создаёт их владелец (writer/collector), sender их только
    правит. В тесте создаём их той же формы, что thread_store.SCHEMA."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
          thread_id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
          seed TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          message_id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL,
          role TEXT NOT NULL, draft_text TEXT, sent_text TEXT, angle TEXT,
          created_at TEXT NOT NULL, sent_at TEXT);
    """)
    db.commit()


def test_conversation_columns_are_added(db):
    conversation_tables(db)
    migrate.apply(db)
    assert {"status", "our_number", "auto_replies", "next_touch_at", "touch_no"} \
        <= columns(db, "threads")
    assert {"provider_id", "handled_at", "queued_text"} <= columns(db, "messages")


def test_threads_that_existed_before_system_3_stay_with_the_human(db):
    """Их вёл человек. Миграция не имеет права отдать их роботу."""
    conversation_tables(db)
    db.execute("INSERT INTO threads VALUES ('+77010000001', 'c1', '{}', '2026-08-01T10:00:00+00:00')")
    db.commit()

    migrate.apply(db)

    status = db.execute("SELECT status FROM threads").fetchone()[0]
    assert status == "escalated", status


def test_thread_opened_after_migration_is_queued(db):
    """open_thread системы 2 вставляет четыре колонки и о status не знает:
    новый тред обязан приезжать в состояние, из которого автомат пишет."""
    conversation_tables(db)
    migrate.apply(db)

    db.execute("INSERT INTO threads VALUES ('+77010000002', 'c2', '{}', '2026-08-29T10:00:00+00:00')")
    db.commit()

    status = db.execute(
        "SELECT status FROM threads WHERE thread_id = '+77010000002'").fetchone()[0]
    assert status == "queued", status


def test_ensure_column_skips_a_table_that_does_not_exist_yet(db):
    """Порядок создания схемы не гарантирован: sender может подняться раньше,
    чем collector создаст threads. Падать нельзя — колонка доедет следующим
    connect'ом."""
    assert migrate.ensure_column(db, "threads", "status", "TEXT") is False


def test_provider_id_is_unique_but_nulls_do_not_collide(db):
    """Уникальный индекс закрывает повтор вебхука: транспорт повторяет доставку
    события, пока мы не ответили 200."""
    conversation_tables(db)
    migrate.apply(db)
    insert = ("INSERT INTO messages (thread_id, role, created_at, provider_id)"
              " VALUES ('+77010000001', 'outgoing', '2026-08-29T10:00:00+00:00', ?)")
    db.execute(insert, ("3EB0",))
    db.execute(insert, (None,))
    db.execute(insert, (None,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("3EB0",))
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_migrate.py -v`
Expected: FAIL — `test_conversation_columns_are_added` и соседи падают (`sqlite3.OperationalError: no such table: threads` у `ensure_column`, отсутствующие колонки в остальных).

- [ ] **Step 3: Реализовать миграцию**

В `backend/sender/db/migrate.py` заменить `apply` и `ensure_column`, добавив ниже них:

```python
# Колонки состояния в чужих таблицах. Таблицы принадлежат системе 2, состояние
# в них ведёт система 3: другого места для «с какого номера идёт чат» нет —
# заводить свою копию треда значило бы два источника правды на одну переписку.
CONVERSATION_COLUMNS = (
    ("threads", "our_number", "TEXT"),
    ("threads", "auto_replies", "INTEGER NOT NULL DEFAULT 0"),
    ("threads", "next_touch_at", "TEXT"),
    ("threads", "touch_no", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "provider_id", "TEXT"),
    ("messages", "handled_at", "TEXT"),
    ("messages", "queued_text", "TEXT"),
)


def apply(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    # Кому ушло прогревочное сообщение. У боевой строки получатель выводится из
    # треда, у прогревочной треда нет — а знать его надо: пассивная фаза требует
    # не «сколько отправлено», а «как давно этот номер что-то получал».
    ensure_column(db, "outbox", "recipient", "TEXT")
    _conversation_state(db)
    db.commit()


def _conversation_state(db: sqlite3.Connection) -> None:
    """Состояние треда и сообщения. Ничего не делает, пока таблиц переписки нет:
    их создаёт владелец (writer/collector), и порядок старта не гарантирован."""
    if ensure_column(db, "threads", "status", "TEXT NOT NULL DEFAULT 'queued'"):
        # Треды, существовавшие до системы 3, вёл человек: они остаются в
        # состоянии, из которого автомат не пишет. Новые приезжают в 'queued'
        # значением по умолчанию — open_thread системы 2 о колонке не знает.
        db.execute("UPDATE threads SET status = 'escalated'")
    for table, column, ddl in CONVERSATION_COLUMNS:
        ensure_column(db, table, column, ddl)
    if _has_table(db, "messages"):
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_provider"
                   " ON messages (provider_id) WHERE provider_id IS NOT NULL")


def ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """True, если колонку добавили; False, если она уже была или таблицы ещё нет."""
    if not _has_table(db, table):
        return False
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    db.commit()
    return True


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone() is not None
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_migrate.py -v`
Expected: PASS, включая старый `test_apply_is_idempotent` (второй `apply` на той же базе не падает и не переписывает `escalated` обратно).

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/db/migrate.py backend/sender/tests/test_migrate.py
git commit -m "feat(sender): колонки состояния треда и сообщения в state.db"
```

---

## Task 2: `sender/db/outbox.py` — очередь исходящих

**Files:**
- Create: `backend/sender/db/outbox.py`
- Test: `backend/sender/tests/test_outbox.py`

**Interfaces:**
- Consumes: таблицу `outbox` из части 1 (Task 1 её DDL не менял).
- Produces:
  - `outbox.put(db, message_id, thread_id, our_number, now) -> int` (кидает `AlreadyQueuedError`)
  - `outbox.due(db, now) -> dict | None`
  - `outbox.claim(db, outbox_id, now) -> bool`
  - `outbox.mark_sent(db, outbox_id, provider_id, now) -> None`
  - `outbox.reschedule(db, outbox_id, send_after, now) -> None`
  - `outbox.cancel(db, outbox_id, reason, now) -> None`
  - `outbox.retry(db, outbox_id, send_after, now) -> None`
  - `outbox.fail(db, outbox_id, error, now) -> None`
  - `outbox.mark_stuck(db, outbox_id, now) -> None`
  - `outbox.sending_since(db, now, minutes) -> list[dict]`
  - `outbox.delivered(db, provider_id, moment, read) -> bool`
  - `outbox.last_sent_at(db, our_number) -> str | None`
  - `outbox.counters(db, now) -> dict` с ключами `queued`, `sent_today`, `overdue`
  - `outbox.recent(db, limit) -> list[dict]`
  - Ни одна из них не коммитит.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_outbox.py`:

```python
"""Очередь исходящих: постановка, захват строки, ретраи и счётчики.

Ни одна функция здесь не коммитит: статус треда, сообщение и строку очереди
воркер меняет одной транзакцией.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sender.db import migrate, outbox

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = migrate.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def queued(db, message_id=1, thread_id="+77010000001", number="+77001112233", now=NOW):
    with db:
        return outbox.put(db, message_id, thread_id, number, now)


def test_put_creates_a_pending_row_ready_to_go(db):
    outbox_id = queued(db)
    row = outbox.due(db, NOW)
    assert row["outbox_id"] == outbox_id
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_one_queue_row_per_message(db):
    """Структурный запрет двойной отправки: повтор падает на уровне базы, а не
    на уровне «мы вроде проверяли»."""
    queued(db, message_id=7)
    with pytest.raises(outbox.AlreadyQueuedError):
        queued(db, message_id=7)


def test_due_ignores_rows_whose_time_has_not_come(db):
    with db:
        outbox_id = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
        outbox.reschedule(db, outbox_id, NOW + timedelta(hours=2), NOW)
    assert outbox.due(db, NOW) is None
    assert outbox.due(db, NOW + timedelta(hours=2))["outbox_id"] == outbox_id


def test_claim_succeeds_once(db):
    """Захват строки вместо лока: ноль обновлённых строк значит, что её взял
    кто-то другой, и мы молча уходим."""
    outbox_id = queued(db)
    with db:
        assert outbox.claim(db, outbox_id, NOW) is True
    with db:
        assert outbox.claim(db, outbox_id, NOW) is False


def test_mark_sent_records_the_provider_id(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
    row = db.execute("SELECT status, provider_id FROM outbox").fetchone()
    assert (row["status"], row["provider_id"]) == ("sent", "3EB0")


def test_retry_counts_attempts_and_moves_the_time(db):
    outbox_id = queued(db)
    with db:
        outbox.retry(db, outbox_id, NOW + timedelta(minutes=1), NOW)
    row = outbox.due(db, NOW + timedelta(minutes=1))
    assert row["attempts"] == 1 and row["status"] == "pending"


def test_cancel_and_reschedule_are_different_outcomes(db):
    """Первое — «это сообщение уже не нужно», второе — «нужно, но не сейчас».
    Смешать их значит либо спамить, либо тихо терять follow-up."""
    cancelled = queued(db, message_id=1)
    postponed = queued(db, message_id=2)
    with db:
        outbox.cancel(db, cancelled, "отказ лида", NOW)
        outbox.reschedule(db, postponed, NOW + timedelta(hours=20), NOW)

    assert outbox.due(db, NOW + timedelta(days=7))["outbox_id"] == postponed
    row = db.execute("SELECT status, error FROM outbox WHERE outbox_id = ?",
                     (cancelled,)).fetchone()
    assert row["status"] == "cancelled" and row["error"] == "отказ лида"


def test_sending_since_finds_rows_stuck_longer_than_the_limit(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
    assert outbox.sending_since(db, NOW + timedelta(minutes=4), 5) == []
    stale = outbox.sending_since(db, NOW + timedelta(minutes=6), 5)
    assert [row["outbox_id"] for row in stale] == [outbox_id]


def test_delivered_marks_the_row_by_provider_id(db):
    outbox_id = queued(db)
    with db:
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
        assert outbox.delivered(db, "3EB0", NOW, read=False) is True
        assert outbox.delivered(db, "нет такого", NOW, read=False) is False
    row = db.execute("SELECT delivered_at, read_at FROM outbox").fetchone()
    assert row["delivered_at"] == "2026-09-01T12:00:00+00:00" and row["read_at"] is None


def test_last_sent_at_looks_only_at_this_number(db):
    with db:
        first = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
        outbox.claim(db, first, NOW)
        outbox.mark_sent(db, first, "3EB0", NOW)
    assert outbox.last_sent_at(db, "+77001112233") == "2026-09-01T12:00:00+00:00"
    assert outbox.last_sent_at(db, "+77009998877") is None


def test_counters_separate_the_queue_from_the_overdue(db):
    """Созревшие, но не отправленные — единственный симптом, по которому
    одинаково видно и вставший воркер, и наглухо закрытые гейты."""
    queued(db, message_id=1)
    with db:
        later = outbox.put(db, 2, "+77010000002", "+77001112233", NOW)
        outbox.reschedule(db, later, NOW + timedelta(days=1), NOW)
        sent = outbox.put(db, 3, "+77010000003", "+77001112233", NOW)
        outbox.claim(db, sent, NOW)
        outbox.mark_sent(db, sent, "3EB0", NOW)

    counters = outbox.counters(db, NOW + timedelta(minutes=1))
    assert counters == {"queued": 2, "sent_today": 1, "overdue": 1}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_outbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.db.outbox'`.

- [ ] **Step 3: Реализовать модуль**

Создать `backend/sender/db/outbox.py`:

```python
"""Очередь исходящих. Одна строка — одно сообщение лиду.

Текста здесь нет: он в messages. Дублировать его сюда значило бы иметь две
версии одного сообщения и вопрос «какая из них ушла».

Ни одна функция не коммитит. Воркер меняет статус треда, сообщение и строку
очереди одной транзакцией (`with db:`): падение процесса между двумя коммитами
дало бы тред в active без отправленного сообщения — лида, которому мы «уже
написали», а он ничего не получал.
"""

import sqlite3
from datetime import datetime, timedelta

FIELDS = ("outbox_id, message_id, thread_id, our_number, send_after, status,"
          " attempts, provider_id, error, created_at, updated_at")


class AlreadyQueuedError(Exception):
    """UNIQUE(message_id): одно сообщение — максимум одна строка очереди."""


def put(db: sqlite3.Connection, message_id: int, thread_id: str,
        our_number: str, now: datetime) -> int:
    moment = stamp(now)
    try:
        cursor = db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (message_id, thread_id, our_number, moment, moment, moment))
    except sqlite3.IntegrityError as error:
        raise AlreadyQueuedError(message_id) from error
    return cursor.lastrowid


def due(db: sqlite3.Connection, now: datetime) -> dict | None:
    """Одна созревшая строка. Одна, а не пачка: при двух сообщениях в час
    последовательная обработка бесплатно снимает все гонки."""
    row = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE status = 'pending' AND send_after <= ?"
        " ORDER BY send_after, outbox_id LIMIT 1", (stamp(now),)).fetchone()
    return dict(row) if row else None


def claim(db: sqlite3.Connection, outbox_id: int, now: datetime) -> bool:
    """Захват строки. False — её взял кто-то другой, и мы молча уходим."""
    cursor = db.execute(
        "UPDATE outbox SET status = 'sending', updated_at = ?"
        " WHERE outbox_id = ? AND status = 'pending'", (stamp(now), outbox_id))
    return cursor.rowcount == 1


def mark_sent(db: sqlite3.Connection, outbox_id: int, provider_id: str | None,
              now: datetime) -> None:
    db.execute(
        "UPDATE outbox SET status = 'sent', provider_id = ?, updated_at = ?"
        " WHERE outbox_id = ?", (provider_id, stamp(now), outbox_id))


def reschedule(db: sqlite3.Connection, outbox_id: int, send_after: datetime,
               now: datetime) -> None:
    """«Нужно, но не сейчас»: строка возвращается в pending к новому сроку."""
    db.execute(
        "UPDATE outbox SET status = 'pending', send_after = ?, updated_at = ?"
        " WHERE outbox_id = ?", (stamp(send_after), stamp(now), outbox_id))


def retry(db: sqlite3.Connection, outbox_id: int, send_after: datetime,
          now: datetime) -> None:
    """Перенос с расходом попытки: транспорт сказал, что фрейм не ушёл."""
    db.execute(
        "UPDATE outbox SET status = 'pending', send_after = ?, attempts = attempts + 1,"
        " updated_at = ? WHERE outbox_id = ?", (stamp(send_after), stamp(now), outbox_id))


def cancel(db: sqlite3.Connection, outbox_id: int, reason: str, now: datetime) -> None:
    """«Это сообщение уже не нужно»: отказ лида, тред забрал человек."""
    db.execute(
        "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
        " WHERE outbox_id = ?", (reason, stamp(now), outbox_id))


def fail(db: sqlite3.Connection, outbox_id: int, error: str, now: datetime) -> None:
    db.execute(
        "UPDATE outbox SET status = 'failed', error = ?, updated_at = ?"
        " WHERE outbox_id = ?", (error, stamp(now), outbox_id))


def mark_stuck(db: sqlite3.Connection, outbox_id: int, now: datetime) -> None:
    """Результат неизвестен: ушло или нет — знает только телефон. Exactly-once
    здесь сознательно не строится.

    ponytail: при нужде сверять с историей чата, которую Baileys отдаёт при
    реконнекте; при нашем объёме это единицы случаев в год.
    """
    db.execute(
        "UPDATE outbox SET status = 'stuck', updated_at = ? WHERE outbox_id = ?",
        (stamp(now), outbox_id))


def sending_since(db: sqlite3.Connection, now: datetime, minutes: int) -> list[dict]:
    """Строки, висящие в sending дольше лимита. Отправка занимает секунды."""
    border = stamp(now - timedelta(minutes=minutes))
    rows = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE status = 'sending' AND updated_at <= ?",
        (border,)).fetchall()
    return [dict(row) for row in rows]


def delivered(db: sqlite3.Connection, provider_id: str, moment: datetime,
              read: bool) -> bool:
    """False — строки с таким provider_id нет: событие не наше или пришло
    раньше, чем мы записали ответ транспорта."""
    column = "read_at" if read else "delivered_at"
    cursor = db.execute(
        f"UPDATE outbox SET {column} = ? WHERE provider_id = ? AND {column} IS NULL",
        (stamp(moment), provider_id))
    return cursor.rowcount == 1


def last_sent_at(db: sqlite3.Connection, our_number: str) -> str | None:
    """Когда с этого номера последний раз что-то ушло — вход для джиттера."""
    return db.execute(
        "SELECT max(updated_at) FROM outbox WHERE our_number = ? AND status = 'sent'",
        (our_number,)).fetchone()[0]


def counters(db: sqlite3.Connection, now: datetime) -> dict:
    """Счётчики дашборда. `overdue` — созревшие, но не отправленные: единственный
    симптом, по которому одинаково видно и вставший воркер, и закрытые гейты."""
    day = now.date().isoformat()
    return {
        "queued": _count(db, "status IN ('pending', 'sending')"),
        "sent_today": _count(
            db, "status = 'sent' AND message_id IS NOT NULL AND updated_at >= ?",
            (f"{day}T00:00:00+00:00",)),
        "overdue": _count(db, "status = 'pending' AND send_after <= ?", (stamp(now),)),
    }


def recent(db: sqlite3.Connection, limit: int) -> list[dict]:
    rows = db.execute(
        f"SELECT {FIELDS} FROM outbox WHERE message_id IS NOT NULL"
        " ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _count(db: sqlite3.Connection, condition: str, arguments: tuple = ()) -> int:
    return db.execute(
        f"SELECT count(*) FROM outbox WHERE {condition}", arguments).fetchone()[0]


def stamp(moment: datetime) -> str:
    """Тот же формат, что numbers.stamp: сравнения границ суток и сроков идут
    лексикографически и верны ровно пока каждый штамп записан в UTC."""
    return moment.isoformat(timespec="seconds")
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_outbox.py sender/tests/test_import_graph.py -v`
Expected: PASS — все тесты очереди зелёные, граф импортов не тронут.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/db/outbox.py backend/sender/tests/test_outbox.py
git commit -m "feat(sender): очередь исходящих — постановка, захват, ретраи, счётчики"
```

---

## Task 3: `sender/db/conversation.py` — состояние треда и текст сообщения

**Files:**
- Create: `backend/sender/db/conversation.py`, `backend/sender/tests/conftest.py`
- Test: `backend/sender/tests/test_conversation.py`

**Interfaces:**
- Consumes: колонки из Task 1.
- Produces:
  - `conversation.STATUSES`, `conversation.AUTOMATON_STOPS`
  - `conversation.get(db, thread_id) -> dict | None` — ключи `thread_id`, `company_id`, `status`, `our_number`, `touch_no`
  - `conversation.set_status(db, thread_id, status) -> None`
  - `conversation.assign_number(db, thread_id, our_number) -> None`
  - `conversation.pending_message(db, thread_id) -> int | None`
  - `conversation.outgoing_text(db, message_id) -> str`
  - `conversation.set_queued_text(db, message_id, text) -> None`
  - `conversation.confirm_sent(db, message_id, provider_id, now) -> None`
  - `conversation.bump_touch(db, thread_id, max_touches) -> None`
  - `conversation.has_replies(db, thread_id) -> bool`
  - `conversation.is_cold(db, thread_id) -> bool`
  - `conversation.first_touch_candidates(db, limit) -> list[dict]`
  - `conversation.cold_threads_of(db, our_number) -> list[dict]`
  - Ни одна не коммитит.
- Фикстура `db` из `conftest.py` доступна всем последующим задачам.

- [ ] **Step 1: Написать общую фикстуру**

Создать `backend/sender/tests/conftest.py`:

```python
"""Общая база тестов системы 3: state.db со всеми тремя слоями.

Таблицы переписки создаёт их владелец — thread_store системы 2, — а sender
только доливает свои колонки. Так же это происходит и в проде: collector
применяет schema.sql на старте, sender ALTER'ит уже существующее.
"""

from datetime import datetime, timezone

import pytest

from sender.db import migrate
from writer.db import thread_store

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)   # среда, 18:00 в Алматы


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)          # threads и messages
    owner.execute("CREATE TABLE IF NOT EXISTS suppression ("
                  " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
    owner.commit()
    owner.close()
    connection = migrate.connect(path)          # numbers, outbox, колонки состояния
    yield connection
    connection.close()


class FakeTransport:
    """Транспорт без сети. `sent=False` — честное «фрейм в сокет не ушёл»."""

    def __init__(self, sent=True, has_whatsapp=True):
        self.sent_calls = []
        self.checked = []
        self._sent = sent
        self._has_whatsapp = has_whatsapp

    async def send(self, number, to, text, key, kind="text"):
        from sender.transport import Sent
        self.sent_calls.append({"number": number, "to": to, "text": text, "key": key})
        return Sent(sent=self._sent, provider_id="3EB0" if self._sent else None,
                    error=None if self._sent else "loggedOut")

    async def check(self, number, to):
        self.checked.append((number, to))
        return self._has_whatsapp
```

- [ ] **Step 2: Написать падающие тесты**

Создать `backend/sender/tests/test_conversation.py`:

```python
"""Состояние треда: кто ведёт чат, каким текстом и сколько раз уже касались."""

from datetime import timedelta

from sender.db import conversation
from sender.tests.conftest import NOW


def open_thread(db, thread_id="+77010000001", company_id="c1", status="queued"):
    db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at, status)"
               " VALUES (?, ?, '{}', ?, ?)",
               (thread_id, company_id, "2026-09-01T10:00:00+00:00", status))
    db.commit()


def add_draft(db, thread_id="+77010000001", text="Здравствуйте!"):
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, created_at)"
        " VALUES (?, 'outgoing', ?, 'crm_widget', ?)",
        (thread_id, text, "2026-09-01T10:00:00+00:00"))
    db.commit()
    return cursor.lastrowid


def test_get_returns_the_state_columns(db):
    open_thread(db)
    thread = conversation.get(db, "+77010000001")
    assert thread["status"] == "queued"
    assert thread["our_number"] is None and thread["touch_no"] == 0
    assert conversation.get(db, "нет такого треда") is None


def test_number_is_assigned_once_and_does_not_change(db):
    """Для лида сообщение с другого номера — новый чат без истории."""
    open_thread(db)
    with db:
        conversation.assign_number(db, "+77010000001", "+77001112233")
    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"


def test_outgoing_text_prefers_what_the_operator_confirmed(db):
    """draft_text — что предложила модель, queued_text — что подтвердил оператор.
    Уходит второе, а первое остаётся рядом: разница draft/sent — единственная
    бесплатная разметка для калибровки промпта."""
    open_thread(db)
    message_id = add_draft(db, text="Черновик модели")
    assert conversation.outgoing_text(db, message_id) == "Черновик модели"

    with db:
        conversation.set_queued_text(db, message_id, "Правленый оператором текст")

    assert conversation.outgoing_text(db, message_id) == "Правленый оператором текст"
    assert db.execute("SELECT draft_text FROM messages").fetchone()[0] == "Черновик модели"


def test_confirm_sent_fills_history_only_after_the_transport_said_yes(db):
    open_thread(db)
    message_id = add_draft(db)
    with db:
        conversation.set_queued_text(db, message_id, "Правленый текст")
    assert conversation.pending_message(db, "+77010000001") == message_id

    with db:
        conversation.confirm_sent(db, message_id, "3EB0", NOW)

    row = db.execute("SELECT sent_text, sent_at, provider_id FROM messages").fetchone()
    assert row["sent_text"] == "Правленый текст"
    assert row["provider_id"] == "3EB0" and row["sent_at"] == "2026-09-02T12:00:00+00:00"
    assert conversation.pending_message(db, "+77010000001") is None


def test_third_touch_without_a_reply_exhausts_the_thread(db):
    """Молчит три касания — автомату больше нечего сказать."""
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "+77010000001", max_touches=3)
        conversation.bump_touch(db, "+77010000001", max_touches=3)
    assert conversation.get(db, "+77010000001")["status"] == "active"

    with db:
        conversation.bump_touch(db, "+77010000001", max_touches=3)

    thread = conversation.get(db, "+77010000001")
    assert (thread["touch_no"], thread["status"]) == (3, "exhausted")


def test_a_thread_with_replies_is_never_exhausted(db):
    open_thread(db, status="active")
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000001', 'incoming', 'а сколько стоит?', ?, ?)",
               ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"))
    db.commit()
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", max_touches=3)
    assert conversation.get(db, "+77010000001")["status"] == "active"
    assert conversation.has_replies(db, "+77010000001") is True


def test_first_touch_candidates_are_threads_with_a_draft_and_no_queue_row(db):
    open_thread(db, "+77010000001", "c1")
    ready = add_draft(db, "+77010000001")
    open_thread(db, "+77010000002", "c2")
    already = add_draft(db, "+77010000002")
    db.execute("INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
               " status, created_at, updated_at) VALUES (?, '+77010000002', '+7700',"
               " ?, 'pending', ?, ?)", (already, *[NOW.isoformat(timespec="seconds")] * 3))
    open_thread(db, "+77010000003", "c3", status="escalated")
    add_draft(db, "+77010000003")
    db.commit()

    candidates = conversation.first_touch_candidates(db, limit=10)

    assert [row["message_id"] for row in candidates] == [ready], candidates


def test_cold_threads_of_a_number_are_the_ones_without_history(db):
    """При бане номера они переезжают; тред с ответами так переехать не может —
    продолжение с чужого номера выглядит как «кто это?»."""
    open_thread(db, "+77010000001", "c1", status="queued")
    open_thread(db, "+77010000002", "c2", status="active")
    for thread_id in ("+77010000001", "+77010000002"):
        with db:
            conversation.assign_number(db, thread_id, "+77001112233")
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000002', 'outgoing', 'ушло', ?, ?)",
               ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"))
    db.commit()

    cold = conversation.cold_threads_of(db, "+77001112233")

    assert [row["thread_id"] for row in cold] == ["+77010000001"]
    assert conversation.is_cold(db, "+77010000002") is False
```

- [ ] **Step 3: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_conversation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.db.conversation'`.

- [ ] **Step 4: Реализовать модуль**

Создать `backend/sender/db/conversation.py`:

```python
"""Состояние переписки глазами системы 3: кто ведёт чат и что уходит лиду.

Таблицы чужие — их владелец система 2, — но состояние в них ведёт система 3:
своей копии треда у sender'а нет и быть не должно, иначе на одну переписку
станет два источника правды. Здесь только колонки состояния и тексты; правила
«что писать» остаются в writer'е.

Ни одна функция не коммитит: статус треда меняется в одной транзакции с
записью в messages/outbox.
"""

import sqlite3
from datetime import datetime

STATUSES = ("queued", "active", "exhausted", "escalated", "unreachable",
            "closed_refused", "closed_junk", "blocked_channel")

# Состояния, из которых автомат не пишет. `escalated` — тупик по правилу 1
# зонтичной спеки: выйти из него имеет право только человек.
AUTOMATON_STOPS = ("escalated", "unreachable", "exhausted",
                   "closed_refused", "closed_junk")

FIELDS = "thread_id, company_id, status, our_number, touch_no"


class UnknownThreadError(Exception):
    """Треда нет: почти всегда опечатка в номере, а не гонка."""


def get(db: sqlite3.Connection, thread_id: str) -> dict | None:
    row = db.execute(f"SELECT {FIELDS} FROM threads WHERE thread_id = ?",
                     (thread_id,)).fetchone()
    return dict(row) if row else None


def set_status(db: sqlite3.Connection, thread_id: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус треда: {status}")
    db.execute("UPDATE threads SET status = ? WHERE thread_id = ?", (status, thread_id))


def assign_number(db: sqlite3.Connection, thread_id: str, our_number: str) -> None:
    """Один раз на тред: для лида сообщение с другого номера — новый чат."""
    db.execute("UPDATE threads SET our_number = ? WHERE thread_id = ?",
               (our_number, thread_id))


def pending_message(db: sqlite3.Connection, thread_id: str) -> int | None:
    row = db.execute(
        "SELECT message_id FROM messages WHERE thread_id = ? AND role = 'outgoing'"
        " AND sent_text IS NULL ORDER BY message_id DESC LIMIT 1",
        (thread_id,)).fetchone()
    return row["message_id"] if row else None


def outgoing_text(db: sqlite3.Connection, message_id: int) -> str:
    """Что уйдёт лиду: подтверждённое оператором, иначе черновик модели."""
    row = db.execute(
        "SELECT coalesce(queued_text, draft_text) FROM messages WHERE message_id = ?",
        (message_id,)).fetchone()
    if row is None or not row[0]:
        raise UnknownThreadError(f"сообщению {message_id} нечего отправлять")
    return row[0]


def set_queued_text(db: sqlite3.Connection, message_id: int, text: str) -> None:
    """Правка оператора ложится рядом с черновиком, а не вместо него."""
    db.execute("UPDATE messages SET queued_text = ? WHERE message_id = ?",
               (text, message_id))


def confirm_sent(db: sqlite3.Connection, message_id: int, provider_id: str | None,
                 now: datetime) -> None:
    """История треда — только состоявшееся. Зовётся после ответа транспорта."""
    db.execute(
        "UPDATE messages SET sent_text = coalesce(queued_text, draft_text),"
        " sent_at = ?, provider_id = ? WHERE message_id = ?",
        (now.isoformat(timespec="seconds"), provider_id, message_id))


def bump_touch(db: sqlite3.Connection, thread_id: str, max_touches: int) -> None:
    """Касание израсходовано. Последнее без ответа закрывает тред в exhausted:
    автомату больше нечего сказать, а каденцию наполнит часть 3."""
    db.execute("UPDATE threads SET touch_no = touch_no + 1 WHERE thread_id = ?",
               (thread_id,))
    thread = get(db, thread_id)
    if thread["touch_no"] >= max_touches and not has_replies(db, thread_id):
        set_status(db, thread_id, "exhausted")


def has_replies(db: sqlite3.Connection, thread_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND role = 'incoming' LIMIT 1",
        (thread_id,)).fetchone() is not None


def is_cold(db: sqlite3.Connection, thread_id: str) -> bool:
    """Холодный — тот, в котором ещё ничего не состоялось: его можно увести на
    другой номер, не создавая у лида чата «кто это?»."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND sent_text IS NOT NULL LIMIT 1",
        (thread_id,)).fetchone() is None


def first_touch_candidates(db: sqlite3.Connection, limit: int) -> list[dict]:
    """Готовые к отправке первые касания: черновик есть, строки очереди нет."""
    rows = db.execute(
        "SELECT m.message_id, m.thread_id FROM messages m"
        " JOIN threads t USING (thread_id)"
        " LEFT JOIN outbox o ON o.message_id = m.message_id"
        " WHERE m.role = 'outgoing' AND m.sent_text IS NULL AND o.outbox_id IS NULL"
        "   AND t.status = 'queued'"
        " ORDER BY m.message_id LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def cold_threads_of(db: sqlite3.Connection, our_number: str) -> list[dict]:
    rows = db.execute(
        f"SELECT {FIELDS} FROM threads WHERE our_number = ?"
        " AND thread_id NOT IN (SELECT thread_id FROM messages"
        "                       WHERE sent_text IS NOT NULL)",
        (our_number,)).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/ -v`
Expected: PASS — новые тесты зелёные, тесты части 1 не сломались.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/db/conversation.py backend/sender/tests/conftest.py backend/sender/tests/test_conversation.py
git commit -m "feat(sender): состояние треда и текст сообщения глазами системы 3"
```

---

## Task 4: `sender/services/gates.py` — проверки перед отправкой

**Files:**
- Create: `backend/sender/services/gates.py`
- Test: `backend/sender/tests/test_gates.py`

**Interfaces:**
- Consumes: `config["window"]`, `config["pace"]["jitter_minutes"]` из части 1.
- Produces:
  - `gates.Attempt` — DTO: `thread_status: str`, `suppressed: bool`, `number_status: str`, `capacity: int`, `last_sent_at: str | None`, `jitter_minutes: float`
  - `gates.Decision` — DTO: `action: str` (`send` | `cancel` | `reschedule`), `reason: str`, `send_after: datetime | None`, `blame: str`
  - `gates.SUPPRESSION`, `gates.THREAD`, `gates.WINDOW`, `gates.NUMBER`, `gates.JITTER` — значения `blame`: кто именно закрыл гейт. Воркер ветвится по ним, а не по русскому тексту `reason`.
  - `gates.check(attempt, now, config) -> Decision`
  - `gates.next_window_start(now, window) -> datetime` (UTC)

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_gates.py`:

```python
"""Гейты перед отправкой: от юридического к техническому.

Отмена и перенос проверяются раздельно. Их подмена друг другом и есть тот баг,
который либо спамит, либо тихо теряет follow-up.
"""

from datetime import datetime, timezone

import pytest

from sender.services import config, gates

CONFIG = config.load()
# Среда, 07:00 UTC = 12:00 в Алматы: середина рабочего окна.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def attempt(**overrides):
    base = {"thread_status": "queued", "suppressed": False, "number_status": "active",
            "capacity": 5, "last_sent_at": None, "jitter_minutes": 2.0}
    return gates.Attempt(**{**base, **overrides})


def test_everything_open_lets_the_message_through():
    assert gates.check(attempt(), INSIDE, CONFIG).action == "send"


def test_suppression_cancels_even_after_the_row_was_queued():
    """Лид мог отказаться за те три дня, что follow-up лежал в очереди (F21)."""
    decision = gates.check(attempt(suppressed=True), INSIDE, CONFIG)
    assert decision.action == "cancel"


@pytest.mark.parametrize("status", ["escalated", "closed_refused", "closed_junk",
                                    "unreachable", "exhausted"])
def test_a_thread_the_automaton_must_not_touch_cancels(status):
    """Человек уже взял тред — автомат в него не пишет."""
    assert gates.check(attempt(thread_status=status), INSIDE, CONFIG).action == "cancel"


def test_outside_the_window_postpones_and_never_cancels():
    """«Нужно, но не сейчас». Отмена здесь потеряла бы касание навсегда."""
    saturday = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    decision = gates.check(attempt(), saturday, CONFIG)
    assert decision.action == "reschedule"
    # Понедельник, 10:00 Алматы = 05:00 UTC.
    assert decision.send_after == datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)


def test_before_the_window_opens_waits_for_the_same_day():
    early = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)     # 08:00 в Алматы
    decision = gates.check(attempt(), early, CONFIG)
    assert decision.send_after == datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)


def test_after_the_window_closes_waits_for_the_next_day():
    late = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)     # 19:00 в Алматы
    decision = gates.check(attempt(), late, CONFIG)
    assert decision.send_after == datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)


def test_exhausted_daily_limit_postpones():
    decision = gates.check(attempt(capacity=0), INSIDE, CONFIG)
    assert decision.action == "reschedule" and decision.blame == gates.NUMBER


def test_a_number_that_is_not_active_postpones():
    """Карантин и прогрев — не повод отменять сообщение."""
    decision = gates.check(attempt(number_status="quarantined"), INSIDE, CONFIG)
    assert decision.action == "reschedule" and decision.blame == gates.NUMBER


def test_blame_separates_the_number_from_the_clock():
    """Воркер по этому полю решает, предлагать ли треду другой номер: ночью
    другой номер не поможет, а выбранный лимит — поможет."""
    saturday = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    assert gates.check(attempt(), saturday, CONFIG).blame == gates.WINDOW
    assert gates.check(attempt(last_sent_at="2026-09-02T06:59:00+00:00",
                               jitter_minutes=15.0), INSIDE, CONFIG).blame == gates.JITTER


def test_jitter_holds_the_number_after_the_previous_send():
    """Отправки подряд — машинный почерк, а почерк здесь и оценивают."""
    decision = gates.check(
        attempt(last_sent_at="2026-09-02T06:59:00+00:00", jitter_minutes=15.0),
        INSIDE, CONFIG)
    assert decision.action == "reschedule"
    assert decision.send_after == datetime(2026, 9, 2, 7, 14, tzinfo=timezone.utc)


def test_jitter_is_over_and_the_message_goes():
    assert gates.check(
        attempt(last_sent_at="2026-09-02T06:40:00+00:00", jitter_minutes=15.0),
        INSIDE, CONFIG).action == "send"


def test_the_legal_gate_wins_over_the_technical_one():
    """Порядок гейтов не косметика: отказ отменяет, даже если сейчас ночь и
    номер в карантине — иначе строка ушла бы в перенос и вернулась завтра."""
    decision = gates.check(
        attempt(suppressed=True, capacity=0, number_status="banned"),
        datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc), CONFIG)
    assert decision.action == "cancel"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_gates.py -v`
Expected: FAIL — `ImportError: cannot import name 'gates'`.

- [ ] **Step 3: Реализовать модуль**

Создать `backend/sender/services/gates.py`:

```python
"""Что проверяется перед каждой отправкой — и в каком порядке.

Порядок не косметический: от юридического к техническому. Отказ обязан
отменить сообщение раньше, чем окно или лимит успеют предложить перенос, —
иначе строка вернётся завтра и уйдёт человеку, который просил не писать.

Различие двух исходов принципиально. `cancel` — «это сообщение уже не нужно»,
`reschedule` — «нужно, но не сейчас». Смешать их значит либо спамить, либо
тихо терять follow-up.

Чистые функции: ни базы, ни сети, ни `datetime.now()` внутри.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sender.db import conversation

SEND = "send"
CANCEL = "cancel"
RESCHEDULE = "reschedule"

# Кто закрыл гейт. Воркер ветвится по этим значениям, а не по тексту reason:
# перенос из-за номера лечится другим номером, перенос из-за ночи — нет.
SUPPRESSION = "suppression"
THREAD = "thread"
WINDOW = "window"
NUMBER = "number"
JITTER = "jitter"

SENDING_NUMBER_STATUS = "active"


@dataclass(frozen=True)
class Attempt:
    """Всё, что гейтам нужно знать о строке очереди. Собирает его воркер:
    сюда не должна протечь ни база, ни sqlite3.Row."""
    thread_status: str
    suppressed: bool
    number_status: str
    capacity: int
    last_sent_at: str | None
    jitter_minutes: float


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    send_after: datetime | None = None
    blame: str = ""


def check(attempt: Attempt, now: datetime, config: dict) -> Decision:
    if attempt.suppressed:
        return Decision(CANCEL, "стоит отказ (F21)", blame=SUPPRESSION)
    if attempt.thread_status in conversation.AUTOMATON_STOPS:
        return Decision(CANCEL, f"тред в состоянии {attempt.thread_status}", blame=THREAD)

    window_opens = next_window_start(now, config["window"])
    if window_opens > now:
        return Decision(RESCHEDULE, "вне окна отправки", window_opens, WINDOW)

    tomorrow = next_window_start(_tomorrow(now, config["window"]), config["window"])
    if attempt.number_status != SENDING_NUMBER_STATUS:
        return Decision(RESCHEDULE, f"номер в статусе {attempt.number_status}",
                        tomorrow, NUMBER)
    if attempt.capacity <= 0:
        return Decision(RESCHEDULE, "дневной лимит номера выбран", tomorrow, NUMBER)

    ready_at = _jitter_over_at(attempt)
    if ready_at is not None and ready_at > now:
        return Decision(RESCHEDULE, "джиттер между отправками с номера",
                        ready_at, JITTER)
    return Decision(SEND, "гейты открыты")


def next_window_start(now: datetime, window: dict) -> datetime:
    """Ближайший момент внутри окна: сам `now`, если мы уже внутри.

    Единственное место, где из конфига берётся Asia/Almaty: в базе всё в UTC, а
    рабочие часы — свойство человека на том конце, а не сервера.
    """
    zone = ZoneInfo(window["timezone"])
    opens, closes = window["hours"]
    local = now.astimezone(zone)
    if local.isoweekday() in window["weekdays"]:
        if local.hour < opens:
            return _at(local, opens)
        if local.hour < closes:
            return now
    candidate = _at(local, opens) + timedelta(days=1)
    while candidate.isoweekday() not in window["weekdays"]:
        candidate += timedelta(days=1)
    return candidate.astimezone(now.tzinfo)


def _tomorrow(now: datetime, window: dict) -> datetime:
    """Завтра в тот же час: дальше next_window_start подвинет на начало окна."""
    return now.astimezone(ZoneInfo(window["timezone"])) + timedelta(days=1)


def _at(local: datetime, hour: int) -> datetime:
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def _jitter_over_at(attempt: Attempt) -> datetime | None:
    if attempt.last_sent_at is None:
        return None
    return (datetime.fromisoformat(attempt.last_sent_at)
            + timedelta(minutes=attempt.jitter_minutes))
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_gates.py -v`
Expected: PASS (12 тестов).

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/gates.py backend/sender/tests/test_gates.py
git commit -m "feat(sender): гейты перед отправкой — отказ, состояние треда, окно, лимит, джиттер"
```

---

## Task 5: `autopilot` — режим и kill switch

**Files:**
- Modify: `backend/sender/services/config.py`, `backend/.gitignore` (создать, если нет)
- Test: `backend/sender/tests/test_config.py`

**Interfaces:**
- Consumes: `config.load()["autopilot"]["mode"]` — значение по умолчанию.
- Produces: `config.MODES`, `config.autopilot() -> str`, `config.set_autopilot(mode) -> None`. Файл-переопределение — `backend/sender/autopilot`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_config.py`:

```python
import pytest

from sender.services import config as sender_config


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """Переключатель в tmp: боевой файл трогать нельзя — тест не имеет права
    включить автопилот на рабочей машине."""
    override = tmp_path / "autopilot"
    monkeypatch.setattr(sender_config, "OVERRIDE", override)
    return override


def test_autopilot_defaults_to_the_config_file(switch):
    assert sender_config.autopilot() == "off"


def test_override_wins_over_the_config(switch):
    sender_config.set_autopilot("replies")
    assert sender_config.autopilot() == "replies"
    assert switch.read_text(encoding="utf-8").strip() == "replies"


def test_garbage_in_the_override_falls_back_to_the_config(switch):
    """Битый файл не имеет права включить отправку: неизвестное значение —
    это «мы не знаем режим», а не «шли всё подряд»."""
    switch.write_text("fulll\n", encoding="utf-8")
    assert sender_config.autopilot() == "off"


def test_set_autopilot_rejects_an_unknown_mode(switch):
    with pytest.raises(ValueError):
        sender_config.set_autopilot("turbo")
    assert not switch.exists()


def test_no_temporary_file_survives_the_switch(switch):
    """Запись атомарна: недописанный файл, прочитанный тиком, — это режим,
    которого никто не выбирал."""
    sender_config.set_autopilot("full")
    assert [path.name for path in switch.parent.iterdir()] == ["autopilot"]
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.config' has no attribute 'OVERRIDE'`.

- [ ] **Step 3: Реализовать переключатель**

Дописать в `backend/sender/services/config.py`:

```python
import logging

log = logging.getLogger(__name__)

MODES = ("off", "replies", "full")

# Kill switch отдельным файлом, а не строкой в config.toml: автопилот
# выключается кнопкой за секунду, а config.toml правит человек — робот, лезущий
# в тот же файл, стирает его комментарии и дерётся с ним за содержимое.
OVERRIDE = HOME / "autopilot"


def autopilot() -> str:
    """Режим на сейчас. Читается каждым тиком: kill switch, который надо
    выключать перезапуском процесса, — не kill switch."""
    if OVERRIDE.exists():
        mode = OVERRIDE.read_text(encoding="utf-8").strip()
        if mode in MODES:
            return mode
        log.warning("в %s лежит неизвестный режим %r — беру значение из config.toml",
                    OVERRIDE, mode)
    return load()["autopilot"]["mode"]


def set_autopilot(mode: str) -> None:
    """Запись атомарна: недописанный файл, прочитанный тиком, — это режим,
    которого никто не выбирал."""
    if mode not in MODES:
        raise ValueError(f"неизвестный режим автопилота: {mode}; бывают {MODES}")
    temporary = OVERRIDE.with_name(f"{OVERRIDE.name}.tmp")
    temporary.write_text(f"{mode}\n", encoding="utf-8")
    temporary.replace(OVERRIDE)
```

- [ ] **Step 4: Спрятать переключатель от git**

Дописать в `backend/.gitignore` (создать файл, если его нет):

```gitignore
# Режим автопилота — состояние машины, а не конфигурация проекта: на боевом
# сервере он «full», в репозитории его быть не должно вовсе.
sender/autopilot
sender/autopilot.tmp
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_config.py -v && git status --short backend/`
Expected: PASS; `git status` не показывает `sender/autopilot`.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/services/config.py backend/sender/tests/test_config.py backend/.gitignore
git commit -m "feat(sender): autopilot как kill switch — файл-переопределение поверх config.toml"
```

---

## Task 6: `sender/services/queue.py` — единственный вход в очередь

**Files:**
- Create: `backend/sender/services/queue.py`
- Test: `backend/sender/tests/test_queue.py`

**Interfaces:**
- Consumes: `outbox.put`, `conversation.*` (Task 2, 3), `pool.assign` и `pool.NoNumberAvailableError` из части 1, `transport.check`.
- Produces:
  - `queue.enqueue(db, transport, thread_id, now, config) -> int` — id строки очереди; коммитит сама (она и есть граница транзакции постановки)
  - `queue.NotReachableError`, `queue.NothingToQueueError`, `queue.ClosedThreadError`
  - пробрасывает `outbox.AlreadyQueuedError` и `pool.NoNumberAvailableError`

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_queue.py`:

```python
"""Постановка в очередь: одна функция для кнопки оператора и для автопилота.

Ручное подтверждение и автомат кладут строку в один и тот же outbox — путь
отправки ровно один.
"""

import pytest

from sender.db import conversation, numbers, outbox
from sender.services import config, pool, queue
from sender.tests.conftest import NOW, FakeTransport
from sender.tests.test_conversation import add_draft, open_thread

CONFIG = config.load()


def working_number(db, number="+77001112233"):
    """Номер, которому календарь уже разрешил холодные касания: день 11+."""
    started = NOW.replace(year=2026, month=8, day=10)
    numbers.register(db, number, f"sessions/{number}", started)
    numbers.set_status(db, number, "active")
    return number


async def test_enqueue_checks_whatsapp_assigns_a_number_and_queues(db):
    working_number(db)
    open_thread(db)
    message_id = add_draft(db)
    transport = FakeTransport()

    outbox_id = await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert transport.checked == [("+77001112233", "+77010000001")]
    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"
    assert outbox.due(db, NOW)["outbox_id"] == outbox_id
    assert outbox.due(db, NOW)["message_id"] == message_id


async def test_a_number_without_whatsapp_makes_the_thread_unreachable(db):
    """Городской номер из 2GIS проверяется один раз, и результат хранится:
    иначе планировщик каждый день долбит проверку по мёртвым номерам."""
    working_number(db)
    open_thread(db)
    add_draft(db)

    with pytest.raises(queue.NotReachableError):
        await queue.enqueue(db, FakeTransport(has_whatsapp=False), "+77010000001",
                            NOW, CONFIG)

    assert conversation.get(db, "+77010000001")["status"] == "unreachable"
    assert outbox.due(db, NOW) is None


async def test_an_unreachable_thread_is_never_checked_again(db):
    working_number(db)
    open_thread(db, status="unreachable")
    add_draft(db)
    transport = FakeTransport()

    with pytest.raises(queue.ClosedThreadError):
        await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert transport.checked == [], "мёртвый номер проверили второй раз"


async def test_the_number_is_checked_once_per_lead(db):
    working_number(db)
    open_thread(db)
    add_draft(db)
    transport = FakeTransport()
    await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)
    db.execute("UPDATE messages SET sent_text = 'ушло', sent_at = ?", (NOW.isoformat(),))
    db.commit()
    add_draft(db)

    await queue.enqueue(db, transport, "+77010000001", NOW, CONFIG)

    assert len(transport.checked) == 1, transport.checked


async def test_queueing_the_same_message_twice_hits_the_database(db):
    """Планировщик физически не может поставить одно сообщение дважды."""
    working_number(db)
    open_thread(db)
    add_draft(db)
    await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)

    with pytest.raises(outbox.AlreadyQueuedError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_a_thread_the_human_took_cannot_be_queued(db):
    working_number(db)
    open_thread(db, status="escalated")
    add_draft(db)

    with pytest.raises(queue.ClosedThreadError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_nothing_to_send_is_not_a_crash(db):
    working_number(db)
    open_thread(db)

    with pytest.raises(queue.NothingToQueueError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)


async def test_no_free_number_leaves_the_thread_untouched(db):
    """Все номера греются или выбрали лимит — это перенос, а не отказ:
    ни статус треда, ни очередь трогать нельзя."""
    open_thread(db)
    add_draft(db)

    with pytest.raises(pool.NoNumberAvailableError):
        await queue.enqueue(db, FakeTransport(), "+77010000001", NOW, CONFIG)

    assert conversation.get(db, "+77010000001")["status"] == "queued"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_queue.py -v`
Expected: FAIL — `ImportError: cannot import name 'queue'`.

- [ ] **Step 3: Реализовать модуль**

Создать `backend/sender/services/queue.py`:

```python
"""Постановка в очередь: единственный вход, общий для кнопки и автопилота.

Создание черновика никогда не ставит строку само. Ставят двое — оператор
кнопкой и тик воркера, — и оба приходят сюда: путь отправки ровно один, а
значит и гейты ровно одни.

Здесь же, а не при открытии треда, живёт проверка «есть ли у номера WhatsApp».
Причина техническая: пайплайн `write` исполняется синхронно в потоке, а
`transport.check` асинхронный. Причина продуктовая важнее — оператор узнаёт о
мёртвом номере в момент нажатия, а не из строки, которая тихо отменится через
двадцать секунд. Проверка по-прежнему одна на лида: её результат хранится
в `threads.our_number` либо в статусе `unreachable`.
"""

import logging
from datetime import datetime

from sender.db import conversation, outbox
from sender.services import pool

log = logging.getLogger(__name__)


class NotReachableError(Exception):
    """У номера лида нет WhatsApp. Тред остаётся в базе в статусе unreachable:
    отсутствие треда заставило бы проверять этот номер снова и снова."""


class NothingToQueueError(Exception):
    """Черновика нет — отправлять нечего."""


class ClosedThreadError(Exception):
    """Тред в состоянии, из которого автомат не пишет."""


async def enqueue(db, transport, thread_id: str, now: datetime, config: dict) -> int:
    thread = conversation.get(db, thread_id)
    if thread is None:
        raise conversation.UnknownThreadError(thread_id)
    if thread["status"] in conversation.AUTOMATON_STOPS:
        raise ClosedThreadError(f"тред {thread_id} в состоянии {thread['status']}")

    message_id = conversation.pending_message(db, thread_id)
    if message_id is None:
        raise NothingToQueueError(thread_id)

    our_number = thread["our_number"] or await _first_number(
        db, transport, thread_id, now, config)
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, our_number, now)
    log.info("в очередь: тред %s, сообщение %s, с номера %s",
             thread_id, message_id, our_number)
    return outbox_id


async def _first_number(db, transport, thread_id: str, now: datetime,
                        config: dict) -> str:
    """Номер выбирается один раз на тред: для лида сообщение с другого номера —
    новый чат без истории, поэтому перебалансировки здесь нет."""
    number = pool.assign(db, now, config)          # NoNumberAvailableError наружу
    if not await transport.check(number, thread_id):
        with db:
            conversation.set_status(db, thread_id, "unreachable")
        log.info("у %s нет WhatsApp — тред закрыт как unreachable", thread_id)
        raise NotReachableError(thread_id)
    with db:
        conversation.assign_number(db, thread_id, number)
    return number
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_queue.py -v`
Expected: PASS (8 тестов).

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/queue.py backend/sender/tests/test_queue.py
git commit -m "feat(sender): постановка в очередь с проверкой WhatsApp и выбором номера"
```

---

## Task 7: Воркер — тик, гейты, отправка

**Files:**
- Create: `backend/sender/services/worker.py`
- Test: `backend/sender/tests/test_worker.py`

**Interfaces:**
- Consumes: `outbox.*`, `conversation.*`, `gates.*`, `numbers.get`, `pool.capacity`.
- Produces:
  - `worker.tick(db, transport, config, now) -> str | None` — исход тика: `sent`, `cancelled`, `rescheduled`, `taken`, `None` (делать было нечего)
  - `worker.TICK_OUTCOMES` — кортеж возможных исходов, чтобы тесты и роутер не сверялись со строками наугад

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_worker.py`:

```python
"""Тик воркера: одна созревшая строка за раз, гейты, отправка.

Всё без сети: транспорт и «сейчас» приезжают аргументами.
"""

from datetime import datetime, timedelta, timezone

from sender.db import conversation, numbers, outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_conversation import add_draft, open_thread

CONFIG = config.load()
# Среда, 07:00 UTC = 12:00 в Алматы: середина рабочего окна.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)


def ready(db, thread_id="+77010000001", status="queued", number="+77001112233",
          text="Здравствуйте!"):
    """Тред с черновиком, номером и строкой очереди, созревшей прямо сейчас."""
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    open_thread(db, thread_id, status=status)
    message_id = add_draft(db, thread_id, text)
    with db:
        conversation.assign_number(db, thread_id, number)
        outbox_id = outbox.put(db, message_id, thread_id, number, INSIDE)
    return outbox_id, message_id


async def test_nothing_due_is_not_an_error(db):
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) is None


async def test_the_happy_path_sends_and_writes_history(db):
    outbox_id, message_id = ready(db)
    with db:
        conversation.set_queued_text(db, message_id, "Правленый оператором текст")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"

    assert transport.sent_calls[0]["text"] == "Правленый оператором текст"
    assert transport.sent_calls[0]["to"] == "+77010000001"
    assert transport.sent_calls[0]["number"] == "+77001112233"
    row = db.execute("SELECT status, provider_id FROM outbox WHERE outbox_id = ?",
                     (outbox_id,)).fetchone()
    assert (row["status"], row["provider_id"]) == ("sent", "3EB0")
    message = db.execute("SELECT sent_text, provider_id FROM messages").fetchone()
    assert message["sent_text"] == "Правленый оператором текст"
    assert message["provider_id"] == "3EB0"
    assert conversation.get(db, "+77010000001")["touch_no"] == 1


async def test_without_an_operator_edit_the_draft_goes_as_is(db):
    ready(db, text="Черновик модели")
    transport = FakeTransport()
    await worker.tick(db, transport, CONFIG, INSIDE)
    assert transport.sent_calls[0]["text"] == "Черновик модели"


async def test_suppression_cancels_the_row_before_the_transport(db):
    """Проверяется перед каждой отправкой, а не при постановке: лид мог
    отказаться за те три дня, что follow-up лежал в очереди."""
    outbox_id, _ = ready(db)
    db.execute("INSERT INTO suppression VALUES ('+77010000001', '2026-09-02', 'просил не писать')")
    db.commit()
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "cancelled"

    assert transport.sent_calls == [], "отправили тому, кто просил не писать"
    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"


async def test_an_escalated_thread_cancels_the_row(db):
    outbox_id, _ = ready(db, status="escalated")
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "cancelled"
    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"


async def test_outside_the_window_the_row_survives(db):
    """Перенос, а не отмена: сообщение нужно, просто не сейчас."""
    outbox_id, _ = ready(db)
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, NIGHT) == "rescheduled"

    assert transport.sent_calls == []
    row = db.execute("SELECT status, send_after FROM outbox WHERE outbox_id = ?",
                     (outbox_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["send_after"] == "2026-09-03T05:00:00+00:00"


async def test_an_exhausted_daily_limit_postpones_the_row(db):
    outbox_id, _ = ready(db)
    # Дневной лимит номера на 21-й день прогрева — потолок 30; выбираем его.
    with db:
        for index in range(30):
            spent = outbox.put(db, 1000 + index, None, "+77001112233", INSIDE)
            outbox.claim(db, spent, INSIDE)
            outbox.mark_sent(db, spent, f"id{index}", INSIDE)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "rescheduled"

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "pending"


async def test_a_cold_thread_moves_to_a_free_number_instead_of_waiting(db):
    """Тред, в котором ещё ничего не состоялось, ждать сутки не обязан: истории,
    которую сломал бы новый номер, у него ещё нет. Переезд — отдельный тик:
    одна строка за тик, и следующий уже отправляет."""
    ready(db)
    numbers.register(db, "+77009998877", "sessions/+77009998877",
                     INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77009998877", "active")
    with db:
        for index in range(30):
            spent = outbox.put(db, 2000 + index, None, "+77001112233", INSIDE)
            outbox.claim(db, spent, INSIDE)
            outbox.mark_sent(db, spent, f"id{index}", INSIDE)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "rescheduled"
    assert conversation.get(db, "+77010000001")["our_number"] == "+77009998877"

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) == "sent"
    assert transport.sent_calls[0]["number"] == "+77009998877"


async def test_the_night_is_not_cured_by_another_number(db):
    """Перенос из-за окна номер не лечит: наш почерк тут ни при чём, спит лид."""
    ready(db)
    numbers.register(db, "+77009998877", "sessions/+77009998877",
                     INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77009998877", "active")

    assert await worker.tick(db, FakeTransport(), CONFIG, NIGHT) == "rescheduled"

    assert conversation.get(db, "+77010000001")["our_number"] == "+77001112233"


async def test_a_row_already_in_flight_is_not_picked_up_again(db):
    """Строка в sending — чужая работа или авария; тик её не трогает, а разбирает
    отдельная метла (Task 8)."""
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    transport = FakeTransport()
    assert await worker.tick(db, transport, CONFIG, INSIDE) is None
    assert transport.sent_calls == []


async def test_the_last_allowed_touch_exhausts_the_thread(db):
    """Молчит три касания — автомату больше нечего сказать."""
    ready(db, status="active")
    db.execute("UPDATE threads SET touch_no = 2 WHERE thread_id = '+77010000001'")
    db.commit()

    await worker.tick(db, FakeTransport(), CONFIG, INSIDE)

    assert conversation.get(db, "+77010000001")["status"] == "exhausted"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_worker.py -v`
Expected: FAIL — `ImportError: cannot import name 'worker'`.

- [ ] **Step 3: Реализовать тик**

Создать `backend/sender/services/worker.py`:

```python
"""Воркер outbox: один тик — одна созревшая строка.

Параллелизма нет и не надо: при двух сообщениях в час последовательная
обработка бесплатно снимает все гонки между воркером и вебхуком. Захват строки
делается условным UPDATE, а не локом: ноль обновлённых строк значит, что её
взял кто-то другой, и мы молча уходим.
"""

import logging
import random
from datetime import datetime

from sender.db import conversation, numbers, outbox
from sender.services import gates, pool

log = logging.getLogger(__name__)

TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken")


async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего."""
    row = outbox.due(db, now)
    if row is None:
        return None

    decision = _decide(db, row, now, config)
    if decision.action == gates.CANCEL:
        with db:
            outbox.cancel(db, row["outbox_id"], decision.reason, now)
        log.info("отменено (%s): тред %s", decision.reason, row["thread_id"])
        return "cancelled"
    if decision.action == gates.RESCHEDULE:
        _postpone(db, row, decision, now, config)
        return "rescheduled"

    with db:
        if not outbox.claim(db, row["outbox_id"], now):
            return None
    return await _send(db, transport, row, now, config)


def _decide(db, row: dict, now: datetime, config: dict) -> gates.Decision:
    """Всё, что гейтам нужно знать, собирается здесь: сами гейты — чистые
    функции и в базу не ходят."""
    thread = conversation.get(db, row["thread_id"])
    number = numbers.get(db, row["our_number"])
    attempt = gates.Attempt(
        thread_status=thread["status"],
        suppressed=_suppressed(db, row["thread_id"]),
        number_status=number["status"],
        capacity=pool.capacity(db, row["our_number"], now, config),
        last_sent_at=outbox.last_sent_at(db, row["our_number"]),
        jitter_minutes=random.uniform(*config["pace"]["jitter_minutes"]),
    )
    return gates.check(attempt, now, config)


def _suppressed(db, handle: str) -> bool:
    """Отказ — юридический контур (F21): единственный источник истины — база."""
    return db.execute("SELECT 1 FROM suppression WHERE handle = ?",
                      (handle,)).fetchone() is not None


def _postpone(db, row: dict, decision: gates.Decision, now: datetime,
              config: dict) -> None:
    """Перенос. Если строку задержал номер — холодному треду сначала предлагается
    другой: ждать сутки из-за чужого выбранного лимита ему незачем, истории,
    которую сломал бы новый номер, у него ещё нет. Ночь и джиттер другим номером
    не лечатся, поэтому ветка смотрит на blame, а не на текст причины."""
    if decision.blame == gates.NUMBER and _try_another_number(db, row, now, config):
        with db:
            outbox.reschedule(db, row["outbox_id"], now, now)
        return
    with db:
        outbox.reschedule(db, row["outbox_id"], decision.send_after, now)
    log.info("перенос (%s): тред %s -> %s",
             decision.reason, row["thread_id"], decision.send_after)


def _try_another_number(db, row: dict, now: datetime, config: dict) -> bool:
    """True — тред переехал и строку можно пробовать прямо сейчас."""
    if not conversation.is_cold(db, row["thread_id"]):
        return False
    try:
        number = pool.assign(db, now, config)
    except pool.NoNumberAvailableError:
        return False
    if number == row["our_number"]:
        return False
    with db:
        conversation.assign_number(db, row["thread_id"], number)
        db.execute("UPDATE outbox SET our_number = ? WHERE outbox_id = ?",
                   (number, row["outbox_id"]))
    log.info("тред %s переехал на номер %s", row["thread_id"], number)
    return True


async def _send(db, transport, row: dict, now: datetime, config: dict) -> str:
    text = conversation.outgoing_text(db, row["message_id"])
    result = await transport.send(row["our_number"], row["thread_id"], text,
                                  key=f"outbox-{row['outbox_id']}")
    if not result.sent:
        return await _retry(db, row, result.error, now, config)
    # Правило 2 зонтичной спеки: статус треда и запись в messages/outbox — одной
    # транзакцией. Иначе падение между коммитами даёт тред, которому мы «уже
    # написали», а лид ничего не получал.
    with db:
        outbox.mark_sent(db, row["outbox_id"], result.provider_id, now)
        conversation.confirm_sent(db, row["message_id"], result.provider_id, now)
        conversation.bump_touch(db, row["thread_id"], config["cadence"]["max_touches"])
    log.info("ушло: тред %s, сообщение %s, provider %s",
             row["thread_id"], row["message_id"], result.provider_id)
    return "sent"
```

- [ ] **Step 4: Временная заглушка ретрая**

`_retry` появится в Task 8. Пока дописать в конец `worker.py`, чтобы Task 7 был самодостаточен:

```python
async def _retry(db, row: dict, error: str | None, now: datetime, config: dict) -> str:
    """Безопасный ретрай: `sent: false` значит, что фрейм в сокет не ушёл.
    Расписание попыток — Task 8."""
    with db:
        outbox.reschedule(db, row["outbox_id"], now, now)
    log.warning("не ушло (%s): тред %s", error, row["thread_id"])
    return "rescheduled"
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_worker.py -v`
Expected: PASS (11 тестов).

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/services/worker.py backend/sender/tests/test_worker.py
git commit -m "feat(sender): тик воркера — гейты, захват строки, отправка"
```

---

## Task 8: Идемпотентность — безопасный ретрай, `stuck`, уведомления

**Files:**
- Modify: `backend/sender/services/worker.py`
- Test: `backend/sender/tests/test_worker_failures.py`

**Interfaces:**
- Consumes: `outbox.retry`, `outbox.fail`, `outbox.mark_stuck`, `outbox.sending_since`, `notify.send`, `transport.TransportError`.
- Produces: `worker.sweep_stuck(db, config, now) -> list[int]` — id строк, ушедших в `stuck`; `worker.tick` зовёт её первой.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_worker_failures.py`:

```python
"""Что делает воркер, когда отправка не удалась или её судьба неизвестна.

Оба исхода плохи, но не одинаково: дубликат в холодном аутриче — прямой повод
нажать Report, потеря сообщения — минус один лид из пятидесяти. Отсюда правило:
переотправляем только то, про что транспорт сказал «не ушло»; всё неясное
уходит человеку.
"""

from datetime import timedelta

import pytest

from sender.db import outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_worker import INSIDE, ready

CONFIG = config.load()


class DeadTransport:
    """Node не ответил: результат отправки неизвестен."""

    async def send(self, number, to, text, key, kind="text"):
        from sender.transport import TransportError
        raise TransportError("POST /send: соединение закрыто")


@pytest.fixture(autouse=True)
def telegram(monkeypatch):
    """Уведомления перехватываются: тест не имеет права писать в телеграм."""
    sent = []

    async def fake(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(worker.notify, "send", fake)
    return sent


async def test_a_frame_that_never_left_is_retried_with_backoff(db):
    """`sent: false` — честное «фрейм в сокет не ушёл»: сообщение точно не
    доставлено, повтор дубликата не создаст."""
    outbox_id, _ = ready(db)

    assert await worker.tick(db, FakeTransport(sent=False), CONFIG, INSIDE) == "retry"

    row = db.execute("SELECT status, attempts, send_after FROM outbox").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["send_after"] == outbox.stamp(INSIDE + timedelta(minutes=1))


async def test_the_backoff_grows_and_the_fourth_failure_gives_up(db, telegram):
    outbox_id, _ = ready(db)
    moment = INSIDE
    for delay in CONFIG["retry"]["backoff_minutes"]:            # 1, 5, 30
        assert await worker.tick(db, FakeTransport(sent=False), CONFIG, moment) == "retry"
        row = db.execute("SELECT send_after FROM outbox").fetchone()
        assert row["send_after"] == outbox.stamp(moment + timedelta(minutes=delay))
        moment = moment + timedelta(minutes=delay)

    assert await worker.tick(db, FakeTransport(sent=False), CONFIG, moment) == "failed"

    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "failed"
    assert len(telegram) == 1 and "+77010000001" in telegram[0]


async def test_an_unknown_outcome_goes_to_a_human_not_to_a_retry(db, telegram):
    """Процесс мог умереть между отправкой и записью результата. Переотправлять
    вслепую нельзя — дубликат дороже потери."""
    ready(db)

    assert await worker.tick(db, DeadTransport(), CONFIG, INSIDE) == "stuck"

    row = db.execute("SELECT status, attempts FROM outbox").fetchone()
    assert (row["status"], row["attempts"]) == ("stuck", 0)
    assert db.execute("SELECT sent_text FROM messages").fetchone()[0] is None
    assert "проверь в телефоне" in telegram[0].lower()


async def test_a_row_hanging_in_sending_is_swept_to_stuck(db, telegram):
    """Отправка занимает секунды. Пять минут в sending — это авария, а не работа."""
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    swept = worker.sweep_stuck(db, CONFIG, INSIDE + timedelta(minutes=6))

    assert swept == [outbox_id]
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "stuck"


async def test_a_fresh_row_in_sending_is_left_alone(db):
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)

    assert worker.sweep_stuck(db, CONFIG, INSIDE + timedelta(minutes=4)) == []


async def test_a_stuck_row_is_never_resent(db, telegram):
    outbox_id, _ = ready(db)
    with db:
        outbox.claim(db, outbox_id, INSIDE)
    later = INSIDE + timedelta(minutes=6)
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, later) is None

    assert transport.sent_calls == [], "застрявшую строку переотправили"
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "stuck"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_worker_failures.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.worker' has no attribute 'sweep_stuck'`.

- [ ] **Step 3: Реализовать ретраи и метлу**

В `backend/sender/services/worker.py` заменить заглушку `_retry`, добавить импорты и метлу:

```python
from datetime import datetime, timedelta

from sender import notify
from sender.transport import TransportError
```

Начало `tick` — метла первой, до выборки созревшей строки:

```python
async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего."""
    for outbox_id in sweep_stuck(db, config, now):
        await notify.send(f"Отправка {outbox_id} висит в sending дольше "
                          f"{config['retry']['stuck_after_minutes']} минут. "
                          "Проверь в телефоне, ушло или нет")
    row = outbox.due(db, now)
    ...
```

Тело `_send` — обёртка вокруг транспорта и настоящий `_retry`:

```python
async def _send(db, transport, row: dict, now: datetime, config: dict) -> str:
    text = conversation.outgoing_text(db, row["message_id"])
    try:
        result = await transport.send(row["our_number"], row["thread_id"], text,
                                      key=f"outbox-{row['outbox_id']}")
    except TransportError as error:
        # «Мы не знаем, ушло ли». Это случай для человека, а не для повтора:
        # дубликат в холодном аутриче — прямой повод нажать Report.
        with db:
            outbox.mark_stuck(db, row["outbox_id"], now)
        log.warning("судьба отправки в тред %s неизвестна: %s", row["thread_id"], error)
        await notify.send(f"Отправка в {row['thread_id']} оборвалась: {error}. "
                          "Проверь в телефоне, ушло или нет")
        return "stuck"
    if not result.sent:
        return await _retry(db, row, result.error, now, config)
    ...


async def _retry(db, row: dict, error: str | None, now: datetime, config: dict) -> str:
    """Безопасный ретрай. `sent: false` — сокет отвалился, номер разлогинен:
    сообщение точно не ушло, и повтор дубликата не создаст."""
    backoff = config["retry"]["backoff_minutes"]
    attempt = row["attempts"] + 1
    if attempt > len(backoff):
        with db:
            outbox.fail(db, row["outbox_id"], error or "транспорт отказал", now)
        log.error("отправка в тред %s не удалась %s раз — сдаёмся",
                  row["thread_id"], len(backoff))
        await notify.send(f"Не смогли отправить в {row['thread_id']} "
                          f"{len(backoff)} раза подряд: {error}")
        return "failed"
    with db:
        outbox.retry(db, row["outbox_id"],
                     now + timedelta(minutes=backoff[attempt - 1]), now)
    log.warning("не ушло (%s), попытка %s из %s: тред %s",
                error, attempt, len(backoff), row["thread_id"])
    return "retry"


def sweep_stuck(db, config: dict, now: datetime) -> list[int]:
    """Строки, висящие в sending дольше лимита. Отправка занимает секунды, так
    что это не работа, а последствие смерти процесса между send и записью
    результата.

    ponytail: exactly-once здесь сознательно не строится — при нашем объёме это
    единицы случаев в год; путь апгрейда — сверять с историей чата, которую
    Baileys отдаёт при реконнекте.
    """
    stale = outbox.sending_since(db, now, config["retry"]["stuck_after_minutes"])
    with db:
        for row in stale:
            outbox.mark_stuck(db, row["outbox_id"], now)
            log.error("строка %s зависла в sending: ушло или нет — знает телефон",
                      row["outbox_id"])
    return [row["outbox_id"] for row in stale]
```

Добавить `"retry"`, `"failed"`, `"stuck"` в `TICK_OUTCOMES`.

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/ -v`
Expected: PASS — новые тесты зелёные, `test_worker.py` из Task 7 не сломался.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/worker.py backend/sender/tests/test_worker_failures.py
git commit -m "feat(sender): безопасный ретрай, stuck и уведомления вместо слепой переотправки"
```

---

## Task 9: Автопостановка при `full`, heartbeat и цикл

**Files:**
- Modify: `backend/sender/services/worker.py`
- Test: `backend/sender/tests/test_worker_autopilot.py`

**Interfaces:**
- Consumes: `config.autopilot()` (Task 5), `queue.enqueue` (Task 6), `conversation.first_touch_candidates` (Task 3).
- Produces:
  - `worker.heartbeat() -> str | None` — ISO-время последнего тика
  - `worker.loop(db_factory, transport_factory, publish=None) -> None` — бесконечный цикл для lifespan; `publish` — колбэк шины событий, приходящий снаружи (система 3 не импортирует collector)
  - `worker.tick` при `autopilot = "full"` ставит в очередь одно холодное касание за тик

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_worker_autopilot.py`:

```python
"""Режимы автопилота и живучесть цикла.

Автопилот, который нельзя остановить одним нажатием за секунду, — не автопилот,
а происшествие. Поэтому режим читается каждым тиком заново.
"""

import asyncio

import pytest

from sender.db import numbers, outbox
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_conversation import add_draft, open_thread
from sender.tests.test_worker import INSIDE

CONFIG = config.load()


@pytest.fixture
def drafted(db):
    """Тред с готовым черновиком, который никто не ставил в очередь."""
    from datetime import timedelta
    numbers.register(db, "+77001112233", "sessions/x", INSIDE - timedelta(days=20))
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db)
    return add_draft(db)


@pytest.fixture
def mode(monkeypatch):
    """Режим подменяется целиком: боевой файл-переключатель тест не трогает."""
    def switch(value):
        monkeypatch.setattr(worker.config, "autopilot", lambda: value)
    return switch


async def test_off_sends_nothing_by_itself(db, drafted, mode):
    """Kill switch: ноль отправок и ноль строк в очереди."""
    mode("off")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) is None

    assert transport.sent_calls == []
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_replies_does_not_start_cold_outreach(db, drafted, mode):
    """Холодные касания и follow-up ждут кнопки: автомат отвечает только в
    начатом диалоге, а диалогов здесь ещё нет."""
    mode("replies")
    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) is None
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


async def test_full_queues_one_cold_touch_per_tick(db, drafted, mode):
    mode("full")
    transport = FakeTransport()

    assert await worker.tick(db, transport, CONFIG, INSIDE) == "queued"

    row = outbox.due(db, INSIDE)
    assert row["message_id"] == drafted and row["our_number"] == "+77001112233"
    assert transport.sent_calls == [], "постановка и отправка — разные тики"


async def test_the_operator_button_works_in_any_mode(db, drafted, mode):
    """Кнопка ставит в очередь при любом режиме — режим ограничивает автомат,
    а не человека."""
    from sender.services import queue
    mode("off")

    outbox_id = await queue.enqueue(db, FakeTransport(), "+77010000001", INSIDE, CONFIG)

    assert outbox.due(db, INSIDE)["outbox_id"] == outbox_id


async def test_a_number_without_whatsapp_does_not_stall_the_tick(db, drafted, mode):
    """Мёртвый номер закрывает тред, а не тик: следующая строка обязана
    обработаться в тот же заход очереди на следующем тике."""
    mode("full")

    assert await worker.tick(db, FakeTransport(has_whatsapp=False), CONFIG, INSIDE) is None

    from sender.db import conversation
    assert conversation.get(db, "+77010000001")["status"] == "unreachable"


async def test_the_loop_survives_an_exception_and_keeps_the_heartbeat_moving(db, monkeypatch):
    """Упавшая asyncio-задача исчезает без строки в логе, и ноль отправок
    обнаруживается через сутки."""
    ticks = []

    async def explode(*_args, **_kwargs):
        ticks.append(1)
        raise RuntimeError("база отвалилась")

    async def stop_after_two(_seconds):
        if len(ticks) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(worker, "tick", explode)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_two)

    with pytest.raises(asyncio.CancelledError):
        await worker.loop(lambda: db, FakeTransport)

    assert len(ticks) == 2, "цикл умер на первом же исключении"
    assert worker.heartbeat() is not None
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_worker_autopilot.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.worker' has no attribute 'heartbeat'`.

- [ ] **Step 3: Реализовать автопостановку, heartbeat и цикл**

Дописать в `backend/sender/services/worker.py`:

```python
import asyncio
from datetime import timezone

from sender.services import config as sender_config, queue

# Один вход в очередь на тик. Автопилот, ставящий пачку, отличается от живого
# отправителя ровно тем, из-за чего номера и банят.
COLD_PER_TICK = 1

# Кто уходит сам в каком режиме. `replies` наполнит часть 3: ответы в диалоге
# приносит она, а холодные касания и follow-up остаются на кнопке.
COLD_MODES = ("full",)

_last_tick: datetime | None = None
```

В `tick`, между метлой и выборкой созревшей строки:

```python
    if sender_config.autopilot() in COLD_MODES:
        if await _queue_cold_touch(db, transport, config, now):
            return "queued"
```

и сама постановка:

```python
async def _queue_cold_touch(db, transport, config: dict, now: datetime) -> bool:
    """Одно холодное касание за тик. False — ставить нечего или номер лида
    оказался мёртвым: и то и другое не повод ронять тик."""
    for candidate in conversation.first_touch_candidates(db, COLD_PER_TICK):
        try:
            await queue.enqueue(db, transport, candidate["thread_id"], now, config)
            return True
        except (queue.NotReachableError, queue.ClosedThreadError,
                queue.NothingToQueueError, outbox.AlreadyQueuedError) as skip:
            log.info("не ставим в очередь %s: %s", candidate["thread_id"], skip)
        except pool.NoNumberAvailableError:
            log.info("свободных номеров нет — холодные касания ждут завтра")
            return False
    return False


def heartbeat() -> str | None:
    """Время последнего тика. Живёт в памяти процесса, а не в базе: воркер
    поднимается вместе с процессом, а симптом, который heartbeat ловит (задача
    умерла, процесс жив), виден изнутри того же процесса — им же и отдаётся
    в /api/stats."""
    return _last_tick.isoformat(timespec="seconds") if _last_tick else None


async def loop(db_factory, transport_factory, publish=None) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и ноль отправок обнаружился бы через сутки. Heartbeat двигается даже
    на исключении — иначе «воркер умер» и «воркеру нечего делать» выглядели бы
    одинаково.

    `publish` приходит снаружи, а не импортом шины collector'а: система 3 не
    знает о существовании системы 1, и шов, где она узнаёт, — тот же
    collector/api.py, что монтирует её роутеры.
    """
    global _last_tick
    while True:
        settings = sender_config.load()
        try:
            db = db_factory()
            try:
                outcome = await tick(db, transport_factory(), settings, _now())
            finally:
                db.close()
            if outcome is not None and publish is not None:
                publish({"type": "refresh", "reason": f"sender.{outcome}"})
        except Exception:
            log.exception("воркер outbox упал на тике")
        _last_tick = _now()
        await asyncio.sleep(settings["pace"]["tick_seconds"])


def _now() -> datetime:
    return datetime.now(timezone.utc)
```

Добавить `"queued"` в `TICK_OUTCOMES`.

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/ -v`
Expected: PASS. В `test_the_loop_survives_an_exception_and_keeps_the_heartbeat_moving` `db.close()` вызывается фикстурой повторно — sqlite3 это допускает.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/worker.py backend/sender/tests/test_worker_autopilot.py
git commit -m "feat(sender): режимы автопилота, heartbeat и живучий цикл воркера"
```

---

## Task 10: Вебхук статусов доставки

**Files:**
- Create: `backend/sender/routes/webhook.py`
- Test: `backend/sender/tests/test_webhook.py`

**Interfaces:**
- Consumes: `outbox.delivered`, `conversation.get`/`set_status`; payload от `sender/node/index.js` (`{kind, number, provider_id, status}` и `{kind, number, from, provider_id, text}`).
- Produces: `POST /api/sender/webhook`, роутер `webhook.router`; `webhook.DELIVERED`, `webhook.READ` — коды статусов Baileys.

**Границы:** здесь обрабатывается только `kind = "status"`. `kind = "incoming"` отвечает 200 и пишет строку в лог — иначе Node ретраит входящее до `WEBHOOK_MAX_ATTEMPTS` и топит лог; агента-продавца на него подключит часть 3. Подписи у события пока нет: `sender_webhook_secret` вводит часть 3, и до неё ручка не должна торчать наружу — событие статуса не несёт текста и умеет ровно одно: проставить `delivered_at`/`read_at` на нашей же строке.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_webhook.py`:

```python
"""Статусы доставки от Node: delivered/read и переход треда в active."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sender.db import conversation, outbox
from sender.routes import webhook
from sender.tests.conftest import NOW
from sender.tests.test_conversation import add_draft, open_thread


@pytest.fixture
def http(db, monkeypatch):
    monkeypatch.setattr(webhook, "connect", lambda: db)
    monkeypatch.setattr(webhook, "now", lambda: NOW)
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def sent_row(db, thread_id="+77010000001"):
    open_thread(db, thread_id)
    message_id = add_draft(db, thread_id)
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, "+77001112233", NOW)
        outbox.claim(db, outbox_id, NOW)
        outbox.mark_sent(db, outbox_id, "3EB0", NOW)
        conversation.confirm_sent(db, message_id, "3EB0", NOW)
    return outbox_id


def test_delivery_marks_the_row_and_wakes_the_thread(db, http):
    """queued -> active: лид получил сообщение, чат состоялся."""
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is not None
    assert conversation.get(db, "+77010000001")["status"] == "active"


def test_read_marks_read_at(db, http):
    sent_row(db)
    http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.READ})
    row = db.execute("SELECT delivered_at, read_at FROM outbox").fetchone()
    assert row["read_at"] is not None


def test_a_repeated_event_changes_nothing(db, http):
    """Транспорт повторяет доставку события, если мы не ответили 200."""
    sent_row(db)
    event = {"kind": "status", "number": "+77001112233",
             "provider_id": "3EB0", "status": webhook.DELIVERED}
    http.post("/api/sender/webhook", json=event)
    first = db.execute("SELECT delivered_at FROM outbox").fetchone()[0]

    assert http.post("/api/sender/webhook", json=event).status_code == 200

    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] == first


def test_delivery_does_not_resurrect_a_thread_the_human_took(db, http):
    """Из escalated автоматического выхода нет — даже по доставке."""
    sent_row(db)
    with db:
        conversation.set_status(db, "+77010000001", "escalated")

    http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert conversation.get(db, "+77010000001")["status"] == "escalated"


def test_a_status_for_a_message_we_do_not_know_is_still_accepted(db, http):
    """200, иначе Node будет повторять его до упора. Прогревочные строки как раз
    такие: провайдерский id у них наш, а строки для лида за ними нет."""
    assert http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "неизвестный", "status": webhook.DELIVERED}).status_code == 200


def test_incoming_is_accepted_and_parked_until_part_3(db, http, caplog):
    """Ответить 200 обязаны: иначе Node ретраит входящее и топит лог."""
    response = http.post("/api/sender/webhook", json={
        "kind": "incoming", "number": "+77001112233", "from": "+77010000001@s.whatsapp.net",
        "provider_id": "3EB1", "text": "а сколько это стоит?"})

    assert response.status_code == 200
    assert response.json()["handled"] is False
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: FAIL — `ImportError: cannot import name 'webhook' from 'sender.routes'`.

- [ ] **Step 3: Реализовать роутер**

Создать `backend/sender/routes/webhook.py`:

```python
"""События от Node: статусы доставки. Входящие — часть 3.

Отвечать 200 обязательно даже на то, что мы не умеем обрабатывать: Node
повторяет доставку события, пока не получит 2xx, и незнакомый `kind` иначе
крутился бы в ретраях, забивая лог настоящими авариями.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from sender.db import conversation, migrate, outbox
from sender.services import config

router = APIRouter(prefix="/api/sender")
log = logging.getLogger(__name__)

# Коды статусов Baileys: 3 — доставлено устройству, 4 — прочитано. Это константы
# протокола, а не настройка: в config.toml им делать нечего.
DELIVERED = 3
READ = 4

# Доставка — единственное честное подтверждение того, что чат состоялся: лид
# получил сообщение, и тред перестаёт быть просто поставленным в очередь.
STARTS_THE_CHAT = "queued"


class Event(BaseModel):
    kind: str
    number: str | None = None
    provider_id: str | None = None
    status: int | None = None
    text: str | None = None


def connect() -> sqlite3.Connection:
    return migrate.connect(config.load()["state_db"])


def now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/webhook")
def receive(event: Event) -> dict:
    if event.kind != "status" or event.provider_id is None:
        log.info("событие %s пока не обрабатывается (часть 3): %s",
                 event.kind, event.provider_id)
        return {"handled": False}
    with closing(connect()) as db:
        return {"handled": _record_status(db, event, now())}


def _record_status(db: sqlite3.Connection, event: Event, moment: datetime) -> bool:
    """False — строки с таким provider_id нет: событие о прогревочной отправке
    или о чужом сообщении. Это норма, а не ошибка."""
    with db:
        known = outbox.delivered(db, event.provider_id, moment,
                                 read=event.status == READ)
        if event.status == READ:
            outbox.delivered(db, event.provider_id, moment, read=False)
        if not known:
            return False
        _wake_the_thread(db, event.provider_id)
    return True


def _wake_the_thread(db: sqlite3.Connection, provider_id: str) -> None:
    row = db.execute("SELECT thread_id FROM outbox WHERE provider_id = ?",
                     (provider_id,)).fetchone()
    if row is None or row["thread_id"] is None:
        return
    thread = conversation.get(db, row["thread_id"])
    if thread and thread["status"] == STARTS_THE_CHAT:
        conversation.set_status(db, row["thread_id"], "active")
        log.info("тред %s -> active: доставлено", row["thread_id"])
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: PASS (6 тестов).

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/routes/webhook.py backend/sender/tests/test_webhook.py
git commit -m "feat(sender): вебхук статусов доставки, тред просыпается по delivered"
```

---

## Task 11: Здоровье номеров по боевым отправкам и переезд тредов при бане

**Files:**
- Modify: `backend/sender/db/outbox.py`, `backend/sender/services/health.py`, `backend/sender/services/pool.py`
- Test: `backend/sender/tests/test_health.py`, `backend/sender/tests/test_pool_relocation.py`

**Interfaces:**
- Consumes: `outbox.delivered_at`/`read_at` (Task 10), `conversation.cold_threads_of`/`has_replies` (Task 3), пороги `config["health"]`.
- Produces:
  - `outbox.rates(db, number, now, window_days) -> dict` с ключами `sent`, `delivered`, `replies`
  - `pool.relocate(db, number, now, config) -> dict` с ключами `moved`, `escalated`, `stranded`
  - `health.check` доводит номер до `quarantined` по delivered/reply rate и зовёт `pool.relocate` на `banned`

- [ ] **Step 1: Написать падающие тесты здоровья**

Дописать в `backend/sender/tests/test_health.py`:

```python
def test_low_delivered_rate_quarantines_the_number(db, sent_messages):
    """Констатация задним числом, но других сигналов после реконнектов нет."""
    number = warmed(db, "+77001112233")
    for index in range(20):
        record_send(db, number, delivered=index < 10)     # 50% при пороге 80%

    events = await_check(db, report={number: {"state": "open", "reconnects": 0}})

    assert [event.status for event in events] == ["quarantined"]
    assert "доставлено" in events[0].reason


def test_a_small_sample_is_not_a_verdict(db, sent_messages):
    """19 отправок — это не статистика, а начало недели."""
    number = warmed(db, "+77001112233")
    for index in range(19):
        record_send(db, number, delivered=False)

    assert await_check(db, report={number: {"state": "open", "reconnects": 0}}) == []


def test_low_reply_rate_quarantines_and_notifies(db, sent_messages):
    number = warmed(db, "+77001112233")
    for _ in range(20):
        record_send(db, number, delivered=True)

    events = await_check(db, report={number: {"state": "open", "reconnects": 0}})

    assert [event.status for event in events] == ["quarantined"]
    assert "ответ" in events[0].reason
    assert len(sent_messages) == 1


def test_sends_older_than_the_window_do_not_count(db, sent_messages):
    """Окно семь дней: прошлый месяц не должен объяснять сегодняшний карантин."""
    number = warmed(db, "+77001112233")
    for _ in range(20):
        record_send(db, number, delivered=False, days_ago=30)

    assert await_check(db, report={number: {"state": "open", "reconnects": 0}}) == []
```

Файл держит **свою** фикстуру `db` (только `migrate.connect`, без таблиц переписки) — её надо удалить: новым тестам нужны `threads`/`messages`, а их даёт фикстура из `conftest.py` (Task 3). Существующие `sent_messages` и `transport_reporting` остаются как есть. Вспомогательные функции — выше тестов:

```python
import asyncio
from datetime import timedelta

from sender.db import numbers, outbox
from sender.tests.test_worker import INSIDE

_next_message_id = iter(range(1000, 9999))


def warmed(db, number):
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    return number


def record_send(db, number, delivered, days_ago=0, replied=False):
    """Боевая отправка задним числом: строка outbox со `sent` и нужной датой."""
    moment = INSIDE - timedelta(days=days_ago)
    message_id = next(_next_message_id)
    thread_id = f"+7701000{message_id}"
    db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
               " VALUES (?, ?, '{}', ?)", (thread_id, thread_id, outbox.stamp(moment)))
    with db:
        outbox_id = outbox.put(db, message_id, thread_id, number, moment)
        outbox.claim(db, outbox_id, moment)
        outbox.mark_sent(db, outbox_id, f"id{message_id}", moment)
        if delivered:
            outbox.delivered(db, f"id{message_id}", moment, read=False)
    if replied:
        db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
                   " VALUES (?, 'incoming', 'ок', ?, ?)",
                   (thread_id, outbox.stamp(moment), outbox.stamp(moment)))
        db.commit()


def await_check(db, report):
    """Синхронная обёртка: сам health.check async, а тесты здесь про арифметику.
    `transport_reporting` — уже существующий помощник этого файла."""
    return asyncio.run(health.check(db, transport_reporting(report), CONFIG, INSIDE))
```

Каждый из четырёх новых тестов берёт фикстуру `sent_messages`: `health.check` зовёт телеграм, и без подмены тест ходил бы в сеть. Тест низкого reply rate использует `record_send(..., replied=False)` — двадцать отправок без единого ответа; тест доставки — `delivered=False` у половины (порядок гейтов в `_verdict` ставит доставку раньше ответов, поэтому вердикты не путаются).

- [ ] **Step 2: Написать падающие тесты переезда**

Создать `backend/sender/tests/test_pool_relocation.py`:

```python
"""Бан номера: холодные треды переезжают, тред с ответами уходит человеку.

Продолжение начатого разговора с чужого номера выглядит для лида как «кто это?»
— поэтому переехать имеет право только тред, в котором ещё ничего не состоялось.
"""

from datetime import timedelta

from sender.db import conversation, numbers
from sender.services import config, pool
from sender.tests.test_conversation import add_draft, open_thread
from sender.tests.test_worker import INSIDE

CONFIG = config.load()


def active_number(db, number):
    numbers.register(db, number, f"sessions/{number}", INSIDE - timedelta(days=20))
    numbers.set_status(db, number, "active")
    return number


def test_cold_threads_move_to_a_free_number(db):
    banned = active_number(db, "+77001112233")
    active_number(db, "+77009998877")
    open_thread(db, "+77010000001", status="queued")
    with db:
        conversation.assign_number(db, "+77010000001", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["moved"] == 1
    thread = conversation.get(db, "+77010000001")
    assert thread["our_number"] == "+77009998877" and thread["status"] == "queued"


def test_a_thread_with_replies_goes_to_the_human(db):
    banned = active_number(db, "+77001112233")
    active_number(db, "+77009998877")
    open_thread(db, "+77010000002", status="active")
    message_id = add_draft(db, "+77010000002")
    db.execute("UPDATE messages SET sent_text = 'ушло' WHERE message_id = ?", (message_id,))
    db.execute("INSERT INTO messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000002', 'incoming', 'ок', ?, ?)",
               (INSIDE.isoformat(), INSIDE.isoformat()))
    db.commit()
    with db:
        conversation.assign_number(db, "+77010000002", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["escalated"] == 1
    assert conversation.get(db, "+77010000002")["status"] == "escalated"


def test_without_a_spare_number_threads_wait_in_blocked_channel(db):
    """Не «потеряли», а «канал закрыт»: появится номер — переезд повторится."""
    banned = active_number(db, "+77001112233")
    open_thread(db, "+77010000001", status="queued")
    with db:
        conversation.assign_number(db, "+77010000001", banned)

    result = pool.relocate(db, banned, INSIDE, CONFIG)

    assert result["stranded"] == 1
    assert conversation.get(db, "+77010000001")["status"] == "blocked_channel"
```

- [ ] **Step 3: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_pool_relocation.py sender/tests/test_health.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.pool' has no attribute 'relocate'`.

- [ ] **Step 4: Реализовать метрики и переезд**

В `backend/sender/db/outbox.py`:

```python
def rates(db: sqlite3.Connection, our_number: str, now: datetime,
          window_days: int) -> dict:
    """Боевые отправки номера за окно: сколько ушло, дошло и получило ответ.

    Прогревочные строки (`message_id IS NULL`) исключены намеренно: свои номера
    читают друг друга всегда, и статистика по ним говорила бы о нас, а не о том,
    как WhatsApp относится к номеру.
    """
    border = stamp(now - timedelta(days=window_days))
    row = db.execute(
        "SELECT count(*), count(delivered_at),"
        " (SELECT count(DISTINCT m.thread_id) FROM messages m"
        "  WHERE m.role = 'incoming' AND m.thread_id IN"
        "        (SELECT thread_id FROM outbox WHERE our_number = ?"
        "         AND status = 'sent' AND message_id IS NOT NULL AND updated_at >= ?))"
        " FROM outbox WHERE our_number = ? AND status = 'sent'"
        "   AND message_id IS NOT NULL AND updated_at >= ?",
        (our_number, border, our_number, border)).fetchone()
    return {"sent": row[0], "delivered": row[1], "replies": row[2]}
```

В `backend/sender/services/pool.py`:

```python
def relocate(db: sqlite3.Connection, number: str, now: datetime, config: dict) -> dict:
    """Забаненный номер отпускает свои треды.

    Холодный тред переезжает и возвращается в очередь — терять его не за что.
    Тред с ответами уходит человеку: продолжение начатого разговора с чужого
    номера выглядит для лида как «кто это?», и чинить это должен не автомат.
    Свободного номера нет — тред ждёт в blocked_channel, а не пропадает.
    """
    moved = escalated = stranded = 0
    cold = {row["thread_id"] for row in conversation.cold_threads_of(db, number)}
    for thread in _threads_of(db, number):
        with db:
            if thread["thread_id"] not in cold:
                conversation.set_status(db, thread["thread_id"], "escalated")
                escalated += 1
                continue
            conversation.set_status(db, thread["thread_id"], "blocked_channel")
        try:
            spare = assign(db, now, config)
        except NoNumberAvailableError:
            stranded += 1
            continue
        with db:
            conversation.assign_number(db, thread["thread_id"], spare)
            conversation.set_status(db, thread["thread_id"], "queued")
        moved += 1
    log.warning("номер %s отпустил треды: переехало %s, к человеку %s, ждут %s",
                number, moved, escalated, stranded)
    return {"moved": moved, "escalated": escalated, "stranded": stranded}


def _threads_of(db: sqlite3.Connection, number: str) -> list[dict]:
    rows = db.execute(
        f"SELECT {conversation.FIELDS} FROM threads WHERE our_number = ?"
        " AND status NOT IN ('escalated', 'closed_refused', 'closed_junk')",
        (number,)).fetchall()
    return [dict(row) for row in rows]
```

В `backend/sender/services/health.py` — две отложенные частью 1 строки и вызов переезда:

```python
def _verdict(row: dict, state: dict | None, thresholds: dict,
             rates: dict) -> Event | None:
    ...
    if rates["sent"] >= thresholds["min_sample"]:
        if rates["delivered"] / rates["sent"] < thresholds["min_delivered_rate"]:
            return Event(row["number"], "quarantined",
                         f"доставлено {rates['delivered']} из {rates['sent']}")
        if rates["replies"] / rates["sent"] < thresholds["min_reply_rate"]:
            return Event(row["number"], "quarantined",
                         f"ответов {rates['replies']} на {rates['sent']} отправок")
    return None
```

и в `check`, после `numbers.set_status`:

```python
        if event.status == "banned":
            pool.relocate(db, event.number, now, config)
```

`check` получает `now: datetime` аргументом — поправить её сигнатуру и вызов в `sender/routes/sender.py::monitor_numbers`.

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/ -v`
Expected: PASS — включая тесты здоровья из части 1 (реконнекты и `loggedOut` работают как раньше).

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/db/outbox.py backend/sender/services/health.py backend/sender/services/pool.py backend/sender/tests/test_health.py backend/sender/tests/test_pool_relocation.py
git commit -m "feat(sender): здоровье по боевым отправкам и переезд тредов при бане"
```

---

## Task 12: Веб-контур — очередь, kill switch, воркер в lifespan, счётчики

**Files:**
- Modify: `backend/sender/routes/sender.py`, `backend/collector/api.py`, `backend/collector/services/metrics.py`, `backend/writer/routes/threads.py`, `backend/writer/db/thread_store.py`
- Test: `backend/sender/tests/test_routes.py`, `backend/collector/tests/test_jobs.py`, `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: `queue.enqueue`, `worker.loop`, `worker.heartbeat`, `outbox.counters`/`recent`, `config.autopilot`/`set_autopilot`.
- Produces:
  - `POST /api/sender/queue` `{thread_id, text?}` → карточка строки очереди; 404 — треда нет, 409 — тред закрыт или сообщение уже в очереди, 422 — у номера нет WhatsApp, 503 — свободных номеров нет
  - `GET /api/sender/queue?thread_id=` → `{"queue": [...], "recent": [...]}`
  - `POST /api/sender/autopilot` `{mode}` → `{"autopilot": mode}`
  - `GET /api/sender` дополняется ключами `queue` (счётчики) и `heartbeat`
  - `/api/stats` → `snapshot["sender"]` дополняется теми же ключами
  - `POST /api/threads/{company_id}/sent` и `thread_store.confirm` удаляются: `sent_text` теперь пишет ровно один автор — воркер, после ответа транспорта

- [ ] **Step 1: Написать падающие тесты роутера**

Дописать в `backend/sender/tests/test_routes.py`:

```python
def test_queue_puts_the_operators_text_into_the_outbox(client_with_thread):
    http, db = client_with_thread

    body = http.post("/api/sender/queue", json={
        "thread_id": "+77010000001", "text": "Правленый оператором текст"}).json()

    assert body["status"] == "pending"
    assert db.execute("SELECT queued_text FROM messages").fetchone()[0] \
        == "Правленый оператором текст"
    assert db.execute("SELECT sent_text FROM messages").fetchone()[0] is None, \
        "сообщение попало в историю до отправки"


def test_queueing_the_same_message_twice_is_a_conflict(client_with_thread):
    http, _ = client_with_thread
    http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    response = http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    assert response.status_code == 409


def test_a_number_without_whatsapp_answers_with_words_not_a_traceback(client_no_whatsapp):
    http, _ = client_no_whatsapp

    response = http.post("/api/sender/queue", json={"thread_id": "+77010000001"})

    assert response.status_code == 422
    assert "whatsapp" in response.json()["detail"].lower()


def test_autopilot_switches_and_is_visible_in_the_status(client, switch):
    http, _ = client

    assert http.post("/api/sender/autopilot", json={"mode": "full"}).json() == \
        {"autopilot": "full"}
    assert http.get("/api/sender").json()["autopilot"] == "full"


def test_autopilot_rejects_an_unknown_mode(client, switch):
    http, _ = client
    assert http.post("/api/sender/autopilot", json={"mode": "turbo"}).status_code == 422


def test_status_carries_the_queue_and_the_heartbeat(client):
    http, _ = client
    body = http.get("/api/sender").json()
    assert set(body["queue"]) == {"queued", "sent_today", "overdue"}
    assert "heartbeat" in body
```

Фикстуры — туда же. `client` из части 1 остаётся, `switch` берётся из Task 5 (импортом `from sender.tests.test_config import switch`):

```python
from datetime import timedelta

from sender.db import numbers
from sender.tests.conftest import FakeTransport
from sender.tests.test_conversation import add_draft, open_thread


def _client_with(monkeypatch, tmp_path, transport):
    """Роутер поверх базы со всеми тремя слоями и транспортом без сети."""
    from writer.db import thread_store
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)
    owner.execute("CREATE TABLE IF NOT EXISTS suppression ("
                  " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
    owner.commit()
    owner.close()
    db = migrate.connect(path)
    monkeypatch.setattr(routes, "connect", lambda: migrate.connect(path))
    monkeypatch.setattr(routes, "now", lambda: NOW)
    monkeypatch.setattr(routes, "build_transport", lambda: transport)
    numbers.register(db, "+77001112233", "sessions/x", NOW - timedelta(days=20))
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001")
    add_draft(db, "+77010000001")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), db


@pytest.fixture
def client_with_thread(tmp_path, monkeypatch):
    http, db = _client_with(monkeypatch, tmp_path, FakeTransport())
    yield http, db
    db.close()


@pytest.fixture
def client_no_whatsapp(tmp_path, monkeypatch):
    http, db = _client_with(monkeypatch, tmp_path, FakeTransport(has_whatsapp=False))
    yield http, db
    db.close()
```

`NOW` в этом файле — `datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)`, вторник, 17:00 в Алматы: внутри окна отправки, чтобы гейты не мешали проверять ручку.

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `cd backend && uv run pytest sender/tests/test_routes.py -v`
Expected: FAIL — 404 на `/api/sender/queue`.

- [ ] **Step 3: Дописать роутер**

В `backend/sender/routes/sender.py`:

```python
from sender.db import conversation, migrate, numbers, outbox
from sender.services import config, health, pool, queue, warmup, worker

RECENT_SENDS = 10


class QueueRequest(BaseModel):
    thread_id: str
    text: str | None = None


class AutopilotRequest(BaseModel):
    mode: str


@router.post("/queue", status_code=201)
async def enqueue(body: QueueRequest) -> dict:
    """Кнопка оператора. Работает в любом режиме автопилота: режим ограничивает
    автомат, а не человека."""
    settings = config.load()
    with closing(connect()) as db:
        if body.text is not None:
            message_id = conversation.pending_message(db, body.thread_id)
            if message_id is None:
                raise HTTPException(409, "отправлять нечего: черновика нет")
            if not body.text.strip():
                raise HTTPException(400, "пустой текст отправленным не бывает")
            with db:
                conversation.set_queued_text(db, message_id, body.text.strip())
        try:
            outbox_id = await queue.enqueue(db, build_transport(), body.thread_id,
                                            now(), settings)
        except conversation.UnknownThreadError:
            raise HTTPException(404, f"треда {body.thread_id} нет") from None
        except queue.NotReachableError:
            raise HTTPException(422, f"у {body.thread_id} нет WhatsApp — "
                                     "тред закрыт как unreachable") from None
        except (queue.ClosedThreadError, queue.NothingToQueueError,
                outbox.AlreadyQueuedError) as conflict:
            raise HTTPException(409, str(conflict)) from None
        except pool.NoNumberAvailableError as busy:
            raise HTTPException(503, str(busy)) from None
        return _queue_row(db, outbox_id)


@router.get("/queue")
def show_queue(thread_id: str | None = None) -> dict:
    with closing(connect()) as db:
        rows = outbox.recent(db, RECENT_SENDS)
        if thread_id is not None:
            rows = [row for row in rows if row["thread_id"] == thread_id]
        return {"queue": [row for row in rows if row["status"] in ("pending", "sending")],
                "recent": rows}


@router.post("/autopilot")
def switch_autopilot(body: AutopilotRequest) -> dict:
    """Kill switch. Одно нажатие, никакого перезапуска процесса."""
    try:
        config.set_autopilot(body.mode)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    log.warning("автопилот переключён в %s", body.mode)
    return {"autopilot": body.mode}
```

`status()` — вместо `settings["autopilot"]["mode"]` брать `config.autopilot()` и добавить:

```python
            "queue": outbox.counters(db, now()),
            "heartbeat": worker.heartbeat(),
```

Плюс приватный `_queue_row(db, outbox_id) -> dict` — строка очереди для фронтенда, и:

```python
async def send_queue(publish=None) -> None:
    """Цикл воркера для lifespan. `publish` прокидывается из collector/api.py:
    шина событий принадлежит системе 1, и знать о ней sender не обязан."""
    await worker.loop(connect, build_transport, publish)
```

- [ ] **Step 4: Поднять воркер в lifespan и отдать счётчики**

В `backend/collector/api.py`:

```python
from collector.services import events
...
    # Шина событий приезжает в sender параметром — тот же шов, что монтирует
    # его роутеры и подключает операцию writer'а в общий реестр.
    sending = asyncio.create_task(sender.send_queue(events.publish))
```
рядом с `monitor` и `warming`, и `sending.cancel()` после `yield`. Событие `refresh` после каждого результативного тика — то, по чему `LiveProvider` перечитывает страницы: свою SSE-подписку sender не заводит (§3 спеки — «тот же `/api/events`»).

Смонтировать вебхук Task 10:

```python
from sender.routes import sender, webhook as sender_webhook
...
app.include_router(sender_webhook.router)
```

В `backend/collector/services/metrics.py::sender_stats` — очередь и heartbeat рядом с номерами:

```python
def sender_stats():
    """Номера по статусам, очередь и пульс воркера.

    Счётчики очереди берутся у самой системы 3, а не своим SQL: дублировать её
    определение «созревших, но не отправленных» здесь значило бы иметь две
    версии главного симптома аварии.
    """
    from sender.db import outbox
    from sender.services import worker
    db_path = threads_db_path()
    if not db_path.exists():
        return {"status": "live", "numbers": {}, "queue": {}, "heartbeat": None}
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT status, count(*) FROM numbers GROUP BY status").fetchall()
            queue = outbox.counters(db, datetime.now(timezone.utc))
        except sqlite3.OperationalError:
            return {"status": "live", "numbers": {}, "queue": {}, "heartbeat": None}
    return {"status": "live", "numbers": {row[0]: row[1] for row in rows},
            "queue": queue, "heartbeat": worker.heartbeat()}
```

Расширить `collector/tests/test_jobs.py::test_frontend_contract`:

```python
    assert set(snapshot["sender"]) == {"status", "numbers", "queue", "heartbeat"}
    paths = {route.path for route in sender.router.routes}
    assert {"/api/sender/queue", "/api/sender/autopilot"} <= paths, paths
```

- [ ] **Step 5: Убрать вторую точку записи `sent_text`**

Из `backend/writer/routes/threads.py` удалить эндпоинт `mark_sent` и его путь из `demo()`; из `backend/writer/db/thread_store.py` удалить `confirm` — единственный автор `sent_text` теперь воркер, после ответа транспорта. В `backend/writer/tests/test_threads.py` заменить вызов на прямой UPDATE (тест проверяет фильтр `history`, а не пропавшую функцию):

```python
    db.execute("UPDATE messages SET sent_text = ?, sent_at = ? WHERE message_id = ?",
               ("Здравствуйте! Правленый оператором текст", thread_store.now(), message_id))
    db.commit()
```

Дописать туда же тест границы:

```python
def test_writer_never_writes_sent_text_itself():
    """Отправку подтверждает система 3 после ответа транспорта. Вторая точка
    записи sent_text означала бы историю треда из сообщений, которых лид не
    получал."""
    assert not hasattr(thread_store, "confirm")
    source = (Path(thread_store.__file__).parent.parent / "routes" / "threads.py").read_text()
    assert "sent_text" not in source, "writer снова пишет sent_text сам"
```

- [ ] **Step 6: Прогнать все тесты бэкенда**

Run: `cd backend && uv run pytest -q`
Expected: PASS — три раздела тестов (collector, writer, sender) зелёные.

- [ ] **Step 7: Коммит**

```bash
git add backend/sender/routes/sender.py backend/sender/tests/test_routes.py backend/collector/api.py backend/collector/services/metrics.py backend/collector/tests/test_jobs.py backend/writer/routes/threads.py backend/writer/db/thread_store.py backend/writer/tests/test_threads.py
git commit -m "feat(sender): ручка очереди, kill switch, воркер в lifespan, счётчики"
```

---

## Task 13: Фронтенд — «в очереди» в карточке и очередь на странице «Отправка»

**Files:**
- Modify: `frontend/app/api.ts`, `frontend/app/LeadCard.tsx`, `frontend/app/sender/page.tsx`, `frontend/app/globals.css`

**Interfaces:**
- Consumes: `POST /api/sender/queue`, `GET /api/sender/queue`, `POST /api/sender/autopilot`, расширенный `GET /api/sender`.
- Produces: `queueMessage`, `fetchQueue`, `setAutopilot`, типы `QueueRow`, расширенный `SenderStatus`.

- [ ] **Step 1: Описать контракт в api.ts**

В `frontend/app/api.ts` удалить `markSent` и добавить:

```ts
export type QueueRow = {
  outbox_id: number;
  message_id: number | null;
  thread_id: string | null;
  our_number: string;
  send_after: string;
  status: string;
  attempts: number;
  error: string | null;
};

/** Кнопка оператора. Постановка в очередь живёт в системе 3: отправляет она же. */
export function queueMessage(threadId: string, text: string) {
  return post<QueueRow>("/api/sender/queue", { thread_id: threadId, text });
}

export function fetchQueue(threadId?: string) {
  const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
  return json<{ queue: QueueRow[]; recent: QueueRow[] }>(`/api/sender/queue${query}`);
}

export function setAutopilot(mode: "off" | "replies" | "full") {
  return post<{ autopilot: string }>("/api/sender/autopilot", { mode });
}
```

и дописать в `SenderStatus`:

```ts
  queue: { queued: number; sent_today: number; overdue: number };
  heartbeat: string | null;
```

- [ ] **Step 2: Третье состояние сообщения в карточке треда**

В `frontend/app/LeadCard.tsx`, в компоненте `Thread`: держать `queue` рядом с `conversation` (`fetchQueue(conversation.thread_id)` при загрузке и после постановки), заменить кнопку и подпись:

```tsx
  const pending = queue.find((row) => row.message_id === conversation.draft?.message_id);

  <button
    disabled={busy || !text.trim() || Boolean(pending)}
    onClick={() => run(async () => {
      await queueMessage(conversation.thread_id, text);
      setQueue((await fetchQueue(conversation.thread_id)).queue);
      return fetchConversation(companyId);
    })}
  >
    {pending ? "В очереди" : "Поставить в очередь"}
  </button>
  {pending && (
    <p className="note">
      Уйдёт с номера <span className="mono">{pending.our_number}</span> не раньше{" "}
      {WHEN.format(new Date(pending.send_after))}. В историю треда сообщение попадёт
      после подтверждения отправки.
    </p>
  )}
```

Требование спеки — три состояния сообщения: черновик → в очереди → отправлено. `pending` рисуется классом `queued` в том же списке `thread-log`, между черновиком и историей.

- [ ] **Step 3: Очередь и kill switch на странице «Отправка»**

В `frontend/app/sender/page.tsx` добавить секцию над пулом номеров:

```tsx
      <section className="card">
        <h2>Очередь</h2>
        <div className="counters">
          <span>в очереди <b>{status?.queue.queued ?? 0}</b></span>
          <span>отправлено сегодня <b>{status?.queue.sent_today ?? 0}</b></span>
          <span className={status && status.queue.overdue > 0 ? "has-replies" : ""}>
            созрели, но стоят <b>{status?.queue.overdue ?? 0}</b>
          </span>
          <span>пульс воркера <b>{status?.heartbeat ? WHEN.format(new Date(status.heartbeat)) : "—"}</b></span>
        </div>
        <div className="autopilot">
          {(["off", "replies", "full"] as const).map((mode) => (
            <button
              key={mode}
              className="btn"
              aria-current={status?.autopilot === mode}
              onClick={() => { setError(null); setAutopilot(mode).then(reload).catch(report); }}
            >
              {AUTOPILOT_LABEL[mode]}
            </button>
          ))}
        </div>
      </section>
```

с подписями `AUTOPILOT_LABEL = { off: "Стоп", replies: "Только ответы", full: "Полный автопилот" }` и таблицей последних отправок из `fetchQueue()` (без `thread_id`). «Созрели, но стоят» — единственный симптом, по которому одинаково видно и вставший воркер, и закрытые гейты, поэтому он выделяется цветом.

Страница подписывается на общий стрим, а не заводит свой: `const { refreshTick } = useLive();` и `useEffect(reload, [reload, refreshTick])`. Воркер публикует `refresh` после каждого результативного тика (Task 12), так что счётчики двигаются сами. То же самое в `Thread` карточки лида: `fetchQueue(conversation.thread_id)` перечитывается по `refreshTick`, иначе «в очереди» не сменится на отправленное без перезагрузки страницы.

- [ ] **Step 4: Стили**

В `frontend/app/globals.css`, рядом с `.thread-log` и `.row` — только токены из `:root`, своих цветов не вводить:

```css
/* Третье состояние сообщения: уже не черновик, ещё не история. Пунктир вместо
   сплошной границы — то же различие, что между «написано» и «получено». */
.thread-log li.queued {
  border-left: 1px dashed var(--hairline);
  opacity: 0.75;
}

.autopilot {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.autopilot .btn[aria-current="true"] {
  background: var(--ink);
  color: var(--canvas);
}
```

Имена токенов сверить с `:root` в том же файле — если там `--line` вместо `--hairline`, взять существующее, а не заводить новое.

- [ ] **Step 5: Проверить руками**

```bash
cd backend && uv run python main.py
cd frontend && npm run dev
```
Ожидаемо: на `/sender` видны счётчики очереди и три кнопки режима; переключение мгновенно меняет подпись в шапке. В карточке треда на `/writer` кнопка «Поставить в очередь» после нажатия становится «В очереди» и показывает номер и срок; история треда пуста, пока сообщение не ушло.

- [ ] **Step 6: Коммит**

```bash
git add frontend/app/api.ts frontend/app/LeadCard.tsx frontend/app/sender/page.tsx frontend/app/globals.css
git commit -m "feat(frontend): очередь и kill switch на странице отправки, «в очереди» в карточке треда"
```

---

## Task 14: Документация и смоук-чеклист

**Files:**
- Modify: `CLAUDE.md`, `docs/PRD.md`, `docs/superpowers/specs/2026-08-28-sender-outbox-worker-design.md`

- [ ] **Step 1: CLAUDE.md**

В секции «Веб как обвязка» дописать к абзацу про систему 3: очередь `outbox` и воркер живут в том же процессе (`sender/services/worker.py`, тик 20 секунд, одна строка за тик); постановка в очередь — `POST /api/sender/queue`, а не эндпоинт writer'а; `sent_text` пишет ровно один автор — воркер, после ответа транспорта; `autopilot` переключается файлом `backend/sender/autopilot` (`off | replies | full`), он же kill switch; `POST /api/sender/webhook` пока принимает только статусы доставки. Добавить в «Слои данных» строку про `messages.queued_text` (что подтвердил оператор, между черновиком модели и отправленным).

- [ ] **Step 2: docs/PRD.md**

Требования системы 3, касающиеся очереди и автопилота, перевести из «заявлено» в реализуемые; оставить в «заявлено» входящие, агента-продавца и каденцию — это часть 3.

- [ ] **Step 3: Пометить спеку сделанной**

В шапке `docs/superpowers/specs/2026-08-28-sender-outbox-worker-design.md` заменить «Статус: дизайн» на «Статус: реализовано <дата>, план — `docs/superpowers/plans/2026-08-29-sender-outbox-worker.md`» и перечислить три отступления из шапки плана.

- [ ] **Step 4: Ручной смоук — то, что pytest проверить не может**

Node поднят (`cd backend/sender/node && npm start`), номер прогрет до дня 11+, `autopilot = off`:

1. Открыть тред в `/writer`, нажать «Черновик первого сообщения», отредактировать текст, нажать «Поставить в очередь». Ожидаемо: кнопка стала «В очереди», в истории треда пусто.
2. Дождаться тика (до 20 секунд). Ожидаемо: сообщение пришло на телефон получателя; в карточке треда оно перешло в историю; на `/sender` `отправлено сегодня` +1.
3. Проверить `messages`: `draft_text` — текст модели, `queued_text` — правка оператора, `sent_text` = `queued_text`, `provider_id` заполнен.
4. Поставить второе сообщение в очередь и нажать «Стоп» до тика. Ожидаемо: строка остаётся `pending` (kill switch останавливает автопостановку, но не отменяет то, что поставил человек), отправка идёт — это осознанно: оператор нажал кнопку сам.
5. Погасить Node и поставить сообщение в очередь. Ожидаемо: 503 от `/api/sender/queue` с внятным текстом, не трассировка.
6. Проверить окно: перевести часы машины за 18:00 Алматы, поставить сообщение. Ожидаемо: строка переносится на 10:00 следующего рабочего дня, а не отменяется.

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md docs/PRD.md docs/superpowers/specs/2026-08-28-sender-outbox-worker-design.md
git commit -m "docs: система 3 — очередь, воркер и автопилот вместо заявленного"
```

---

## Definition of Done

- `cd backend && uv run pytest` — зелёный целиком.
- Оператор жмёт кнопку в карточке треда, сообщение уходит в окно 10:00–18:00 Asia/Almaty с прогретого номера в пределах дневного лимита.
- `sent_text` заполняется только после ответа транспорта; `draft_text` и `queued_text` сохранены рядом.
- `autopilot = off` останавливает автопостановку за одно нажатие в UI, без перезапуска процесса.
- Отказ, `escalated` и закрытые треды отменяют строку; окно, лимит и джиттер — переносят. Проверено раздельными тестами.
- Повторная постановка одного сообщения падает на `UNIQUE(message_id)`; строка, зависшая в `sending`, уходит в `stuck` и не переотправляется; `sent: false` даёт backoff 1/5/30 и `failed` после трёх попыток.
- Воркер не умирает молча: исключение логируется, цикл продолжается, heartbeat виден в `/api/stats` и на странице «Отправка».
- Бан номера уводит холодные треды на новый номер и эскалирует треды с ответами.

## Чего в этой части нет — и это не забыто

- **Входящие и агент-продавец.** Вебхук принимает `kind: "incoming"`, отвечает 200 и пишет в лог; разбирать его будет часть 3.
- **Подпись вебхука.** `sender_webhook_secret` вводит часть 3. До неё ручка не должна быть доступна из интернета: событие статуса не несёт текста и умеет только проставить `delivered_at`/`read_at`, но открывать её наружу без подписи всё равно нечего.
- **Каденция follow-up.** Колонки `next_touch_at`, `touch_no`, `auto_replies` добавлены здесь (дробить `ensure_column` по частям смысла нет), `touch_no` двигает воркер, но планирование касаний — часть 3.
- **Приоритет между прогревом и боевыми касаниями.** Оба тратят один дневной лимит номера (`ponytail:`-комментарий в `warmup._next_sender` из части 1). При пяти касаниях в день этапа 3 конфликта не возникает; станет тесно — приоритет решается в `pool.capacity`, а не удвоением счётчиков.
- **Exactly-once.** Сознательно: при нашем объёме это единицы случаев в год, и неопределённость уходит человеку, а не в слепой повтор.
