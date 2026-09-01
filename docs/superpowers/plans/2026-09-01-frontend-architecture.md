# Архитектура фронтенда: страницы по сущностям, видимые процессы, прозрачный LLM — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разрезать фронтенд по сущностям вместо границ бэкенда, сделать видимой каждую фоновую операцию и показать оператору полный запрос, ушедший в модель.

**Architecture:** Новый общий модуль `backend/activity.py` — журнал событий в `state.db`, куда пишут все фоновые демоны; путь к базе приходит швом `use()`, как уже сделано для `sender_refusal.use()`. Промпт сохраняется рядом с черновиком в двух новых колонках `messages`. Фронтенд переезжает с трёх «системных» страниц на шесть страниц по сущностям, `LeadCard` распадается на три компонента, каждый в одном месте.

**Tech Stack:** Python 3.13, FastAPI, SQLite (`sqlite3` из stdlib, без ORM), pytest + pytest-asyncio (`asyncio_mode = "auto"`), Next.js 15 App Router, React 19, TypeScript, `@phosphor-icons/react`.

**Spec:** `docs/superpowers/specs/2026-09-01-frontend-architecture-design.md`

## Global Constraints

- Все команды бэкенда — из `backend/`: `uv run pytest`, `uv run pytest collector/tests/test_activity.py -v`.
- Язык кода и комментариев — русский, как во всём проекте. Комментарий объясняет **почему**, а не **что**.
- Тип-хинты обязательны в новом коде системы 3 (`sender/` типизирован); в `collector/` и `writer/` следуй стилю соседнего файла.
- `state.db` — невосстановимый слой: только `CREATE TABLE IF NOT EXISTS` и `ALTER TABLE ADD COLUMN` через `ensure_column`. `DROP TABLE` запрещён.
- Система 3 не имеет права импортировать `collector.*`, система 1 — `sender.*`. Сторожат `sender/tests/test_import_graph.py` и `collector/tests/test_operations.py`. Единственный шов — `collector/api.py`.
- `rebuild.py` не импортирует ничего сетевого — тест графа импортов.
- Фронтенд не хардкодит пороги, календари и подписи, которые знает бэкенд: они приезжают в ответе.
- Один `EventSource` на всё дерево — `components/live.tsx`. Страницы своих стримов не открывают.
- Коммит после каждой задачи. Сообщение на русском, префиксы `feat:` / `fix:` / `refactor:` / `docs:`.

---

## Структура файлов

**Создаются:**

| Файл | Ответственность |
|---|---|
| `backend/activity.py` | Журнал фоновой работы: DDL, `use`, `record`, `recent`, `workers`, `prune` |
| `backend/collector/tests/test_activity.py` | Тесты журнала (модули верхнего уровня тестируются здесь — прецедент: `test_observability.py`) |
| `backend/collector/routes/activity.py` | `GET /api/activity` |
| `frontend/app/activity/page.tsx` | Страница «Процессы» |
| `frontend/app/cold/page.tsx` | Страница «Холодные» |
| `frontend/app/leads/page.tsx` | Страница «Лиды» (выдача) |
| `frontend/app/leads/[id]/page.tsx` | Карточка лида |
| `frontend/app/threads/page.tsx` | Страница «Диалоги» |
| `frontend/components/LeadDossier.tsx` | Данные лида: скор, досье, сигналы, каналы, сырьё, ответы модели |
| `frontend/components/MessageComposer.tsx` | Текст + промпт + действия; общий для `/cold` и `/threads` |
| `frontend/components/RefusalForm.tsx` | Форма отказа (F21), вынута из `LeadCard` |
| `frontend/components/RunBar.tsx` | Полоска активной джобы в `layout.tsx` |

**Изменяются:**

| Файл | Что меняется |
|---|---|
| `backend/collector/api.py` | `activity.use(...)`, роутер `activity` |
| `backend/collector/services/jobs.py` | Запись старта и финала джобы в журнал |
| `backend/collector/routes/events.py` | Событие `activity` в SSE |
| `backend/collector/services/leads.py` | `card()` отдаёт досье, сырьё, ответы модели |
| `backend/collector/db/lead.py` | Запросы досье, сырья, `llm_answers` |
| `backend/sender/services/worker.py` | Записи в журнал; удаление `_last_tick` и `heartbeat()` |
| `backend/sender/services/warmup.py` | Запись исхода прогрева |
| `backend/sender/routes/sender.py` | Запись решений монитора; `heartbeat` из журнала |
| `backend/sender/routes/webhook.py` | Запись входящих и стоп-слов |
| `backend/sender/db/conversation.py` | `add_draft` принимает `prompt` и `model` |
| `backend/sender/db/migrate.py` | `ensure_column` для `messages.prompt` и `messages.model` |
| `backend/sender/services/followup.py` | Вызов `agent.draft` под новый возврат |
| `backend/writer/db/thread_store.py` | `SCHEMA`, `add_draft`, `pending_draft`, `inbox`, `cold_drafts` |
| `backend/writer/services/agent.py` | `draft()` возвращает `Attempt` |
| `backend/writer/services/operations.py` | Вызов `agent.draft` под новый возврат |
| `backend/writer/routes/threads.py` | Ручка промпта, ручка `drafts`, вызов `agent.draft` |
| `frontend/app/api.ts` | Типы и функции новых ручек |
| `frontend/app/layout.tsx` | `RunBar` |
| `frontend/components/live.tsx` | Подписка на событие `activity` |
| `frontend/components/Sidebar.tsx` | Шесть пунктов, новое сопоставление джоб |
| `frontend/app/page.tsx` | «Обзор» без `OperationsPanel`/`RunsHistory`, гайд под новые пути |

**Удаляются (в задаче 11):** `frontend/app/sourcing/page.tsx`, `frontend/app/writer/page.tsx`, `frontend/app/LeadCard.tsx`.

---

# Фаза 1. Журнал процессов

## Task 1: Модуль журнала `backend/activity.py`

**Files:**
- Create: `backend/activity.py`
- Test: `backend/collector/tests/test_activity.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `activity.use(path: str | Path) -> None`
  - `activity.record(actor: str, outcome: str, subject: str | None = None, detail: str | None = None) -> None`
  - `activity.recent(limit: int = 200, actor: str | None = None) -> list[dict]` — ключи `at`, `last_at`, `repeats`, `actor`, `outcome`, `subject`, `detail`, порядок от свежих к старым
  - `activity.workers() -> list[dict]` — ключи `actor`, `last_at`, `events`
  - `activity.prune(days: int = 14) -> int` — сколько строк удалено
  - `activity.NotConfiguredError`

- [ ] **Step 1: Написать падающий тест**

Создай `backend/collector/tests/test_activity.py`:

```python
"""Журнал фоновой работы: схлопывание, ретенция, отказ работать без пути.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_observability.py и test_logctx.py: своего каталога у backend/*.py нет.
"""

from datetime import datetime, timedelta, timezone

import pytest

import activity


@pytest.fixture
def journal(tmp_path):
    activity.use(tmp_path / "state.db")
    yield activity
    activity.use(None)


def test_record_keeps_what_happened(journal):
    journal.record("sender.tick", "sent", subject="+77001112233", detail="тред +77009998877")
    events = journal.recent()
    assert len(events) == 1
    assert events[0]["actor"] == "sender.tick"
    assert events[0]["outcome"] == "sent"
    assert events[0]["subject"] == "+77001112233"
    assert events[0]["detail"] == "тред +77009998877"
    assert events[0]["repeats"] == 1


def test_identical_events_in_a_row_collapse(journal):
    """Тик раз в 20 секунд дал бы 4300 строк в сутки и утопил бы в них
    единственное важное событие."""
    for _ in range(3):
        journal.record("sender.tick", "idle")
    events = journal.recent()
    assert len(events) == 1
    assert events[0]["repeats"] == 3
    assert events[0]["last_at"] >= events[0]["at"]


def test_alternating_outcomes_do_not_collapse(journal):
    """Схлопывается только повтор последней строки: иначе чередование
    sent/idle слилось бы в две вечные строки и порядок событий пропал бы."""
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("sender.tick", "idle")
    assert [event["outcome"] for event in journal.recent()] == ["idle", "sent", "idle"]


def test_different_subjects_do_not_collapse(journal):
    journal.record("sender.monitor", "quarantined", subject="+77001112233")
    journal.record("sender.monitor", "quarantined", subject="+77007776655")
    assert len(journal.recent()) == 2


def test_recent_filters_by_actor_and_limits(journal):
    journal.record("jobs", "started", subject="17")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("jobs", "finished", subject="17")
    assert [event["outcome"] for event in journal.recent(actor="jobs")] == ["finished", "started"]
    assert len(journal.recent(limit=1)) == 1


def test_workers_answer_whether_a_daemon_is_alive(journal):
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "idle")
    journal.record("sender.warmup", "sent", subject="+77001112233")
    workers = {row["actor"]: row for row in journal.workers()}
    assert workers["sender.tick"]["events"] == 1
    assert workers["sender.warmup"]["last_at"] is not None


def test_prune_drops_only_the_old(journal, tmp_path):
    import sqlite3

    journal.record("sender.tick", "idle")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("UPDATE activity SET at = ?, last_at = ?", (stale, stale))
    journal.record("sender.tick", "sent", subject="+77001112233")

    assert journal.prune(days=14) == 1
    assert [event["outcome"] for event in journal.recent()] == ["sent"]


def test_record_returns_the_event_it_wrote(journal):
    """Возврат нужен ленте: публиковать событие в SSE перечитыванием базы
    значило бы лишний запрос на каждый тик и падение на пустом журнале."""
    journal.record("sender.tick", "idle")
    event = journal.record("sender.tick", "idle")
    assert event["outcome"] == "idle"
    assert event["repeats"] == 2


def test_journal_without_a_path_refuses_loudly(tmp_path):
    """Молча проглоченный журнал — ровно та теневая работа, которую он убирает."""
    activity.use(None)
    with pytest.raises(activity.NotConfiguredError):
        activity.record("sender.tick", "idle")
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'activity'`

- [ ] **Step 3: Написать модуль**

Создай `backend/activity.py`:

```python
"""Журнал фоновой работы: кто, когда и с каким исходом что сделал.

Верхний уровень backend/, рядом с config.py и observability.py, по той же
причине: писать сюда обязаны и система 1, и система 3, а импортировать друг
друга они не имеют права.

Путь к state.db приходит швом use() — тем же приёмом, каким collector/api.py
подключает sender_refusal.use(). Соединение открывается на запись и сразу
закрывается: событие «пытались, и вот что вышло» обязано пережить откат
транзакции вызывающего, иначе сломавшийся тик стирал бы след о себе.

Таблицей владеет этот модуль, а не collector/db/schema.sql: тот же принцип,
по которому numbers и outbox живут в sender/db/migrate.py.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  id      INTEGER PRIMARY KEY,
  at      TEXT NOT NULL,           -- когда случилось впервые
  last_at TEXT NOT NULL,           -- когда в последний раз
  repeats INTEGER NOT NULL DEFAULT 1,
  actor   TEXT NOT NULL,           -- sender.tick | sender.monitor | sender.warmup | jobs | webhook
  outcome TEXT NOT NULL,
  subject TEXT,
  detail  TEXT
);
CREATE INDEX IF NOT EXISTS activity_recent ON activity (last_at DESC);
"""

# В state.db одновременно пишут вебхук, тик воркера и воркер джоб. Журнал
# приходит четвёртым писателем со своим соединением, и без ожидания первая же
# встреча двух записей дала бы «database is locked» на ровном месте.
BUSY_TIMEOUT_MS = 5000

_path: Path | None = None


class NotConfiguredError(RuntimeError):
    """use() не звали."""


def use(path: str | Path | None) -> None:
    """Шов: где лежит state.db, знает сборщик приложения, а не журнал.

    Схема применяется здесь, один раз за процесс: executescript на каждой
    записи открывал бы лишнюю транзакцию записи три раза в минуту — ровно
    там, где за право писать и так стоит очередь.
    """
    global _path
    _path = Path(path) if path is not None else None
    if _path is None:
        return
    with closing(_open()) as db, db:
        db.executescript(SCHEMA)


def record(actor: str, outcome: str, subject: str | None = None,
           detail: str | None = None) -> dict:
    """Событие в журнал. Повтор последней строки не создаёт новую.

    Зовётся ВНЕ транзакции вызывающего: собственное соединение, попав внутрь
    чужого `with db:`, ждало бы освобождения базы, которое наступит только
    после его же возврата, — и падало бы по таймауту. Это не педантичность:
    отдельное соединение здесь и нужно затем, чтобы след пережил откат тика.
    """
    stamp = _now()
    event = {"at": stamp, "last_at": stamp, "repeats": 1, "actor": actor,
             "outcome": outcome, "subject": subject, "detail": detail}
    with closing(_connect()) as db, db:
        last = db.execute(
            "SELECT id, actor, outcome, subject, at, repeats FROM activity"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if last is not None and (last[1], last[2], last[3]) == (actor, outcome, subject):
            db.execute("UPDATE activity SET repeats = repeats + 1, last_at = ?"
                       " WHERE id = ?", (stamp, last[0]))
            return {**event, "at": last[4], "repeats": last[5] + 1}
        db.execute(
            "INSERT INTO activity (at, last_at, actor, outcome, subject, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)", (stamp, stamp, actor, outcome, subject, detail))
    return event


def recent(limit: int = 200, actor: str | None = None) -> list[dict]:
    where = " WHERE actor = ?" if actor else ""
    arguments = (actor, limit) if actor else (limit,)
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT at, last_at, repeats, actor, outcome, subject, detail FROM activity"
            + where + " ORDER BY id DESC LIMIT ?", arguments).fetchall()
    return [dict(row) for row in rows]


def workers() -> list[dict]:
    """Пульс демонов. Считается из журнала, а не из переменной в памяти:
    так он переживает перезапуск процесса и не врёт после него."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT actor, MAX(last_at) AS last_at, COUNT(*) AS events"
            " FROM activity GROUP BY actor ORDER BY actor").fetchall()
    return [dict(row) for row in rows]


def prune(days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(_connect()) as db, db:
        return db.execute("DELETE FROM activity WHERE last_at < ?", (cutoff,)).rowcount


def _connect() -> sqlite3.Connection:
    if _path is None:
        raise NotConfiguredError(
            "activity.use(path) не звали — журналу некуда писать. "
            "В приложении это делает collector/api.py, в тестах — фикстура.")
    return _open()


def _open() -> sqlite3.Connection:
    db = sqlite3.connect(_path)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def demo():
    """Журнал пишет, схлопывает и чистит — без сети и без чужих модулей."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        use(Path(tmp) / "state.db")
        record("demo", "idle")
        record("demo", "idle")
        assert recent()[0]["repeats"] == 2
        assert prune(days=0) == 1
        use(None)
    print("activity demo ok — запись, схлопывание, ретенция")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest collector/tests/test_activity.py -v`
Expected: PASS, 8 тестов

- [ ] **Step 5: Проверить self-check модуля**

Run: `cd backend && uv run python activity.py`
Expected: `activity demo ok — запись, схлопывание, ретенция`

- [ ] **Step 6: Коммит**

```bash
git add backend/activity.py backend/collector/tests/test_activity.py
git commit -m "feat(activity): журнал фоновой работы со схлопыванием повторов"
```

---

## Task 2: Демоны пишут в журнал

**Files:**
- Modify: `backend/sender/services/worker.py` (`_send`, `_process`, `_retry`, `sweep_stuck`, `loop`, удалить `_last_tick` и `heartbeat`)
- Modify: `backend/sender/services/warmup.py` (`loop`)
- Modify: `backend/sender/routes/sender.py` (`monitor_numbers`, `status`)
- Modify: `backend/sender/routes/webhook.py`
- Modify: `backend/collector/services/jobs.py` (`run_pending`)
- Modify: `backend/collector/api.py` (шов `activity.use`)
- Modify: `backend/sender/tests/conftest.py` (фикстура настраивает журнал)
- Test: `backend/sender/tests/test_worker_journal.py`

**Interfaces:**
- Consumes: `activity.use`, `activity.record`, `activity.workers` из Task 1.
- Produces: имена акторов, на которые опирается фронтенд и API: `sender.tick`, `sender.monitor`, `sender.warmup`, `jobs`, `webhook`.

**Почему детали пишет не `loop`.** `tick()` возвращает исход **строкой**, и ни номера, ни треда, ни причины в ней нет; на эту строку завязаны десятки утверждений в `sender/tests/`. Менять её на структуру — большой диван правок ради данных, которые у самих шагов уже под рукой. Поэтому `_send` пишет отправку, `_process` — отмену с причиной гейта, `_retry` — перенос, `sweep_stuck` — зависшую строку, а `loop` пишет ровно одно: `idle`, когда `tick()` вернул `None`.

- [ ] **Step 1: Написать падающий тест**

Создай `backend/sender/tests/test_worker_journal.py`:

```python
"""След тика в журнале: отправка называет номер и тред, пустой тик — idle."""

import pytest

import activity
from sender.services import config, worker
from sender.tests.conftest import FakeTransport
from sender.tests.test_worker import INSIDE, ready

CONFIG = config.load()


@pytest.fixture(autouse=True)
def _autopilot_on(monkeypatch):
    """Kill switch (off) не должен глушить отправку — тот же приём, что в
    test_worker_failures.py."""
    monkeypatch.setattr(worker.sender_config, "autopilot", lambda: "full")


async def test_a_sent_row_names_the_number_and_the_thread(db):
    ready(db)                       # номер +77001112233, тред +77010000001
    await worker.tick(db, FakeTransport(), CONFIG, INSIDE)
    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "sent"
    assert event["subject"] == "+77001112233"
    assert "+77010000001" in (event["detail"] or "")


async def test_an_empty_tick_writes_idle_once(db):
    """Пустой тик обязан оставлять след — иначе «воркер умер» и «воркеру
    нечего делать» выглядят одинаково. Второй подряд новую строку не создаёт."""
    await worker.loop_once(db, FakeTransport(), CONFIG, INSIDE)
    await worker.loop_once(db, FakeTransport(), CONFIG, INSIDE)
    events = activity.recent(actor="sender.tick")
    assert len(events) == 1
    assert events[0]["outcome"] == "idle"
    assert events[0]["repeats"] == 2
```

`ready(db)` — существующий помощник из `sender/tests/test_worker.py`: регистрирует активный номер, открывает тред, кладёт черновик и созревшую строку очереди, возвращает `(outbox_id, message_id)`. Второй такой не заводится.

В `backend/sender/tests/conftest.py` добавь настройку журнала в фикстуру `db`:

```python
@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)          # threads и messages
    owner.execute("CREATE TABLE IF NOT EXISTS suppression ("
                  " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
    owner.commit()
    owner.close()
    activity.use(path)                          # журнал пишет в ту же state.db
    connection = migrate.connect(path)          # numbers, outbox, колонки состояния
    yield connection
    connection.close()
    activity.use(None)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest sender/tests/test_worker_journal.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.worker' has no attribute 'loop_once'`

- [ ] **Step 3: Вынести тело цикла и записать исходы**

В `backend/sender/services/worker.py`:

```python
import activity


async def loop_once(db, transport, settings: dict, now: datetime) -> str | None:
    """Один проход цикла: тик плюс след в журнале. Вынесен из loop(), чтобы
    тест проверял след, не заводя бесконечный цикл и не подменяя sleep."""
    outcome = await tick(db, transport, settings, now)
    if outcome is None:
        activity.record("sender.tick", "idle")
    return outcome
```

`loop()` теперь зовёт `loop_once` вместо `tick` напрямую, а `_last_tick` и `heartbeat()` удаляются целиком:

```python
async def loop(db_factory, transport_factory, publish=None) -> None:
    """Тело целиком в try/except: упавшая asyncio-задача исчезает без строки в
    логе, и ноль отправок обнаружился бы через сутки.

    Пульс больше не живёт в памяти процесса: его отдаёт журнал (activity.workers),
    и в отличие от глобальной переменной он переживает перезапуск.

    `publish` приходит снаружи, а не импортом шины collector'а: система 3 не
    знает о существовании системы 1, и шов, где она узнаёт, — тот же
    collector/api.py, что монтирует её роутеры.
    """
    while True:
        interval = FALLBACK_TICK_SECONDS
        try:
            settings = sender_config.load()
            interval = settings["pace"]["tick_seconds"]
            db = db_factory()
            try:
                outcome = await loop_once(db, transport_factory(), settings, _now())
            finally:
                db.close()
            if outcome is not None and publish is not None:
                publish({"type": "refresh", "reason": f"sender.{outcome}"})
        except Exception:
            log.exception("воркер outbox упал на тике")
            activity.record("sender.tick", "crashed", detail="см. логи процесса")
        await asyncio.sleep(interval)
```

Записи в шагах — рядом с существующими `log.info`, чтобы журнал и лог не
разъезжались, и **всегда после закрытия блока `with db:`**, а не внутри него.
Причина не стилистическая: `record` пишет своим соединением, и внутри открытой
транзакции вызывающего оно ждало бы освобождения базы, которое наступит только
после его же возврата, — `database is locked` через пять секунд на каждой
отправке. Все существующие `log.info` в этих местах уже стоят за пределами
транзакции, так что достаточно ставить запись рядом с ними:

- в `_send`, после подтверждённой отправки:
  `activity.record("sender.tick", "sent", subject=row["our_number"], detail=f"тред {row['thread_id']}")`
- в `_process`, в ветке `gates.CANCEL`:
  `activity.record("sender.tick", "cancelled", subject=row["thread_id"], detail=decision.reason)`
- в `_process`, в ветке `gates.RESCHEDULE`:
  `activity.record("sender.tick", "rescheduled", subject=row["thread_id"], detail=decision.reason)`
- в `_retry`: `activity.record("sender.tick", "retry", subject=row["thread_id"], detail=reason)`
- в `sweep_stuck`, на каждую зависшую строку:
  `activity.record("sender.tick", "stuck", subject=str(outbox_id), detail="судьба отправки неизвестна")`

- [ ] **Step 4: Убедиться, что тест журнала проходит**

Run: `cd backend && uv run pytest sender/tests/test_worker_journal.py -v`
Expected: PASS

- [ ] **Step 5: Починить то, что сломало удаление heartbeat()**

`sender/routes/sender.py::status` берёт пульс из журнала:

```python
def _heartbeat() -> str | None:
    """Пульс воркера — из журнала: он переживает перезапуск процесса, а
    глобальная переменная в памяти после него врала «пульса не было»."""
    for row in activity.workers():
        if row["actor"] == "sender.tick":
            return row["last_at"]
    return None
```

и в теле `status()` — `"heartbeat": _heartbeat()`. Контракт `SenderStatus` не меняется.

Run: `cd backend && uv run pytest sender/tests -v`
Expected: PASS. Тесты, звавшие `worker.heartbeat()` (`test_worker_failures.py`, `test_worker_autopilot.py`), переписываются на `activity.workers()`: пульс теперь читается оттуда, и это и есть проверяемое поведение.

- [ ] **Step 6: Записать остальных демонов**

`sender/services/warmup.py::loop`, внутри `try`, после `tick`:

```python
sender_number = await tick(db, transport_factory(), _config(), _now())
if sender_number is None:
    activity.record("sender.warmup", "idle")
else:
    activity.record("sender.warmup", "sent", subject=sender_number)
```

`sender/routes/sender.py::monitor_numbers`, после `health.check`:

```python
events = await health.check(db, build_transport(), settings, moment)
for event in events:
    activity.record("sender.monitor", event.status, subject=event.number,
                    detail=event.reason)
if not events:
    activity.record("sender.monitor", "healthy")
activity.prune()   # ретенция журнала едет на часовом тике, своего таймера не заводим
```

`sender/routes/webhook.py` — три записи по факту решения: принято входящее
(`subject` — `thread_id`), дубликат по `provider_id`, стоп-слово закрыло тред
(`detail` — сработавшее слово).

`collector/services/jobs.py::run_pending`:

```python
job_id = _claim()
if job_id is None:
    return None
activity.record("jobs", "started", subject=str(job_id))
await _execute(job_id)
activity.record("jobs", "finished", subject=str(job_id))
return job_id
```

- [ ] **Step 7: Подключить шов в приложении**

`backend/collector/api.py`, рядом с `sender_refusal.use(_write_refusal)`:

```python
import activity
from collector.services import store

# Тот же шов, что подключает отказ: где лежит state.db, знает сборщик
# приложения, а журнал — модуль верхнего уровня — не знает ни одной из систем.
activity.use(store.STATE)
```

- [ ] **Step 8: Прогнать всё**

Run: `cd backend && uv run pytest`
Expected: PASS целиком. Если падает `collector/tests/test_jobs.py` — журналу не задали путь: добавь `activity.use(tmp_path / "state.db")` в ту фикстуру, которая готовит базу джоб.

- [ ] **Step 9: Коммит**

```bash
git add backend/sender backend/collector backend/activity.py
git commit -m "feat(activity): демоны пишут исходы в журнал, пульс считается из него"
```

---

## Task 3: Ручка `GET /api/activity` и событие SSE

**Files:**
- Create: `backend/collector/routes/activity.py`
- Modify: `backend/collector/api.py` (подключить роутер)
- Modify: `backend/collector/routes/events.py` (документировать новый тип события)
- Test: `backend/collector/tests/test_web.py` (дописать)

**Interfaces:**
- Consumes: `activity.recent`, `activity.workers` из Task 1.
- Produces: `GET /api/activity?limit=200&actor=sender.tick` →
  `{"events": [{at, last_at, repeats, actor, outcome, subject, detail}], "workers": [{actor, last_at, events, silent_after_seconds}]}`

`silent_after_seconds` — порог, после которого демона считать замолчавшим; приезжает с бэкенда, потому что интервалы тиков живут в `sender/config.toml`, а страница их не знает.

- [ ] **Step 1: Написать падающий тест**

Дописать в `backend/collector/tests/test_web.py`:

```python
def test_activity_endpoint_returns_events_and_workers(tmp_path):
    from collector.routes import activity as web_activity

    activity.use(tmp_path / "state.db")
    activity.record("sender.tick", "sent", subject="+77001112233")
    body = web_activity.journal()
    assert body["events"][0]["outcome"] == "sent"
    assert body["workers"][0]["actor"] == "sender.tick"
    assert body["workers"][0]["silent_after_seconds"] > 0
    activity.use(None)


def test_activity_endpoint_filters_by_actor(tmp_path):
    from collector.routes import activity as web_activity

    activity.use(tmp_path / "state.db")
    activity.record("jobs", "started", subject="1")
    activity.record("sender.tick", "idle")
    body = web_activity.journal(actor="jobs")
    assert [event["actor"] for event in body["events"]] == ["jobs"]
    activity.use(None)
```

`import activity` — в шапку файла. Тесты зовут функцию роутера напрямую, без
`TestClient`: так уже устроен весь `test_web.py` (`web_leads.leads(limit=10)`),
и второй способ проверять ручки в проекте не заводится.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_web.py -k activity -v`
Expected: FAIL — 404

- [ ] **Step 3: Написать роутер**

Создай `backend/collector/routes/activity.py`:

```python
"""Журнал фоновой работы для страницы «Процессы».

Порог молчания демона считает бэкенд, а не страница: интервалы тиков живут в
sender/config.toml, и фронтенд про них знать не должен — тот же принцип, что
у календаря прогрева.
"""

from fastapi import APIRouter

import activity

router = APIRouter(prefix="/api/activity")

# Три интервала подряд без единой записи — демон молчит. Один пропущенный тик
# бывает от блокировки базы, три подряд не бывают ни от чего безобидного.
SILENCE_FACTOR = 3

DEFAULT_INTERVAL_SECONDS = 60
INTERVALS = {
    "sender.tick": 20,
    "sender.warmup": 900,
    "sender.monitor": 3600,
    "jobs": 60,
    "webhook": 3600,
}


@router.get("")
def journal(limit: int = 200, actor: str | None = None) -> dict:
    return {
        "events": activity.recent(limit=limit, actor=actor),
        "workers": [
            {**row,
             "silent_after_seconds":
                 INTERVALS.get(row["actor"], DEFAULT_INTERVAL_SECONDS) * SILENCE_FACTOR}
            for row in activity.workers()
        ],
    }
```

Подключи в `backend/collector/api.py`: `from collector.routes import activity as activity_routes` и `app.include_router(activity_routes.router)`.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest collector/tests/test_web.py -k activity -v`
Expected: PASS

- [ ] **Step 5: Оживить ленту через SSE**

Событие с типом `activity` публикуется тем же `bus.publish`, что и `refresh`. В `worker.loop` рядом с публикацией `refresh` добавь:

`record` возвращает записанное событие — его и публикуем. Перечитывать базу
ради последней строки значило бы лишний запрос на каждый тик и `IndexError` на
пустом журнале:

```python
async def loop_once(db, transport, settings: dict, now: datetime) -> str | None:
    outcome = await tick(db, transport, settings, now)
    if outcome is None:
        event = activity.record("sender.tick", "idle")
        if publish is not None:
            publish({"type": "activity", "event": event})
    return outcome
```

`publish` для этого приходит в `loop_once` четвёртым параметром (по умолчанию
`None`), тем же швом, каким он уже приходит в `loop`. Записи из шагов
(`_send`, `_process`) в SSE не публикуются: страница «Процессы» дотянет их
следующим `fetchActivity`, а протаскивать шину в каждый шаг воркера значило бы
раздать системе 3 знание о системе 1.

В докстринге `collector/routes/events.py` перечисли новый тип рядом с `snapshot | job | log | refresh`.

- [ ] **Step 6: Прогнать всё и закоммитить**

Run: `cd backend && uv run pytest`
Expected: PASS

```bash
git add backend/collector backend/sender/services/worker.py
git commit -m "feat(activity): ручка /api/activity и событие ленты в SSE"
```

---

## Task 4: Страница «Процессы»

**Files:**
- Create: `frontend/app/activity/page.tsx`
- Create: `frontend/components/RunBar.tsx`
- Modify: `frontend/app/api.ts`
- Modify: `frontend/components/live.tsx`
- Modify: `frontend/app/layout.tsx`
- Modify: `frontend/components/Sidebar.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/globals.css`

**Interfaces:**
- Consumes: `GET /api/activity` из Task 3.
- Produces: `fetchActivity()`, типы `ActivityEvent`, `ActivityWorker`; компонент `RunBar`; контекстное поле `activityTail: ActivityEvent[]` в `useLive()`.

- [ ] **Step 1: Контракт в `app/api.ts`**

```ts
export type ActivityEvent = {
  at: string;
  last_at: string;
  repeats: number;
  actor: string;
  outcome: string;
  subject: string | null;
  detail: string | null;
};

export type ActivityWorker = {
  actor: string;
  last_at: string | null;
  events: number;
  /** Порог молчания приезжает с бэкенда: интервалы тиков живут в sender/config.toml. */
  silent_after_seconds: number;
};

export function fetchActivity(limit = 200, actor?: string) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (actor) query.set("actor", actor);
  return json<{ events: ActivityEvent[]; workers: ActivityWorker[] }>(`/api/activity?${query}`);
}
```

Добавь `"activity"` в список типов в `subscribeEvents`.

- [ ] **Step 2: Лента в `components/live.tsx`**

В `LiveState` добавь `activityTail: ActivityEvent[]` (начальное значение `[]`), а в обработчик событий — ветку:

```tsx
} else if (data.type === "activity") {
  setState((prev) => ({
    ...prev,
    activityTail: [data.event as ActivityEvent, ...prev.activityTail].slice(0, ACTIVITY_CAP),
  }));
}
```

с `const ACTIVITY_CAP = 200;` рядом с `LOG_CAP`. Второго `EventSource` не появляется — правило «одна подписка на дерево» остаётся целым.

- [ ] **Step 3: Страница**

Создай `frontend/app/activity/page.tsx`. Структура сверху вниз: полоска демонов (зелёная точка, если `Date.now() - last_at < silent_after_seconds * 1000`, иначе серая с подписью «молчит»), `JobMonitor`, лента журнала с фильтром по `actor`, `OperationsPanel`, `RunsHistory`. Лента склеивает `activityTail` из контекста с ответом `fetchActivity()`, дедуплицируя по `(actor, outcome, subject, last_at)`: живые события приезжают по SSE, а первый экран — запросом. У схлопнутой строки рисуй `×{repeats}`.

- [ ] **Step 4: `RunBar` в шапку**

Создай `frontend/components/RunBar.tsx` — узкая полоска: заголовок активной джобы, `шаг N из M`, ссылка «лог» на `/activity`, кнопка «прервать». Возвращает `null`, если активной джобы нет. Смонтируй в `app/layout.tsx` внутри `LiveProvider`, над `<div className="shell">`.

- [ ] **Step 5: Убрать переехавшее с «Обзора»**

Из `frontend/app/page.tsx` удали `OperationsPanel` и `RunsHistory` (они теперь на `/activity`); `PipelineActions` и `JobMonitor` там пока остаются — их судьбу решает Task 11. В сайдбар добавь пункт «Процессы» → `/activity` с иконкой `PulseIcon`.

- [ ] **Step 6: Проверить в браузере**

```bash
cd backend && uv run python main.py     # первый терминал
cd frontend && npm run dev              # второй
```

Открой `http://localhost:3000/activity`. Ожидаемо: демоны с зелёными точками, лента растёт сама без F5 (тик воркера идёт раз в 20 секунд), у пустых тиков — счётчик повторов.

- [ ] **Step 7: Коммит**

```bash
git add frontend
git commit -m "feat(web): страница «Процессы» — демоны, живой журнал, операции"
```

---

# Фаза 2. Промпт и страница «Холодные»

## Task 5: Сохранение промпта рядом с черновиком

**Files:**
- Modify: `backend/writer/db/thread_store.py` (`SCHEMA`, `add_draft`, `pending_draft`)
- Modify: `backend/sender/db/migrate.py` (`ensure_column` для двух колонок)
- Modify: `backend/sender/db/conversation.py` (`add_draft`)
- Modify: `backend/writer/services/agent.py` (`Attempt`)
- Modify: `backend/writer/services/operations.py`, `backend/writer/routes/threads.py`, `backend/sender/services/followup.py`
- Test: `backend/writer/tests/test_agent.py`, `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces:
  - `agent.Attempt(draft: Draft, prompt: list[tuple[str, str]], model: str)`
  - `agent.draft(...) -> Attempt` (вместо `Draft`)
  - `thread_store.add_draft(db, thread_id, text, angle, prompt=None, model=None) -> int`
  - `conversation.add_draft(db, thread_id, text, angle, prompt=None, model=None) -> int`
  - `thread_store.draft_prompt(db, message_id) -> dict | None` — `{"prompt": [...], "model": str}`

- [ ] **Step 1: Написать падающий тест**

В `backend/writer/tests/test_agent.py`:

```python
def test_draft_returns_the_prompt_that_was_sent():
    """Промпт — аудит, а не реконструкция: seed и история завтра будут другими,
    а текст писался по сегодняшним."""
    sent = {}

    class FakeLLM:
        def invoke(self, messages, config=None):
            sent["messages"] = messages
            return Draft(text="привет", angle="crm_widget", stop=False)

    attempt = agent.draft(FakeLLM(), SEED, [], agent.FIRST,
                          session_id="+77001112233", name="writer.first", offer="оффер")
    assert attempt.draft.text == "привет"
    assert attempt.prompt == [list(pair) for pair in sent["messages"]] or \
           attempt.prompt == sent["messages"]
    assert attempt.prompt[0][0] == "system"
    assert "оффер" in attempt.prompt[0][1]
```

и в `backend/writer/tests/test_threads.py` — той же формой, что соседние тесты
файла (база открывается прямо в тесте, фикстур в `writer/tests/` для переписки
нет):

```python
def test_a_stored_draft_keeps_its_prompt():
    """Промпт — аудит: сегодняшний seed и история завтра будут другими, а
    текст писался по сегодняшним."""
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", {"name": "Ромашка", "signals": []})

    message_id = thread_store.add_draft(
        db, "+77010000001", "привет", "crm_widget",
        prompt=[("system", "правила"), ("human", "факты")],
        model="deepseek/deepseek-v4-flash")

    stored = thread_store.draft_prompt(db, message_id)
    assert stored["prompt"] == [["system", "правила"], ["human", "факты"]]
    assert stored["model"] == "deepseek/deepseek-v4-flash"
    db.close()


def test_an_old_draft_without_a_prompt_answers_nothing():
    """Черновики, написанные до этой правки, промпта не имеют. Реконструировать
    его задним числом значило бы выдать догадку за факт."""
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", {"name": "Ромашка", "signals": []})
    message_id = thread_store.add_draft(db, "+77010000001", "привет", "crm_widget")
    assert thread_store.draft_prompt(db, message_id) is None
    db.close()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd backend && uv run pytest writer/tests/test_agent.py writer/tests/test_threads.py -v`
Expected: FAIL — `AttributeError: module 'writer.services.agent' has no attribute 'Attempt'` и `TypeError: add_draft() got an unexpected keyword argument 'prompt'`

- [ ] **Step 3: Колонки в схеме**

В `SCHEMA` внутри `backend/writer/db/thread_store.py` добавь в `CREATE TABLE messages`:

```sql
  prompt     TEXT,               -- json: полный запрос, ушедший в модель
  model      TEXT,               -- чем сгенерировано
```

Существующие базы `CREATE TABLE IF NOT EXISTS` не тронет, поэтому те же две колонки добавь в `CONVERSATION_COLUMNS` в `backend/sender/db/migrate.py`:

```python
    ("messages", "prompt", "TEXT"),
    ("messages", "model", "TEXT"),
```

`ensure_column` уже умеет ровно это; вторая копия механизма не заводится.

**Этого недостаточно.** `CONVERSATION_COLUMNS` доливает `sender/db/migrate.py`,
а таблицей `messages` владеет `writer/db/thread_store.py`, и открыть базу
writer'ом можно, ни разу не позвав `migrate.connect`: так работают
`uv run pytest writer/tests/`, операция `writer.outreach` и всё, что стартует
без демонов системы 3. На такой базе `add_draft(..., prompt=…)` упадёт
`sqlite3.OperationalError: table messages has no column named prompt`.
Поэтому владелец таблицы доливает свои же колонки сам:

```python
def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _ensure_prompt_columns(db)
    return db


def _ensure_prompt_columns(db):
    """CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, а базы
    переписки у всех давно созданы. Колонки владельца доливает владелец:
    полагаться на то, что до него добежит migrate системы 3, значит уронить
    writer везде, где система 3 не стартовала."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    for column in ("prompt", "model"):
        if column not in existing:
            db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
    db.commit()
```

Проверь это тестом на старой форме таблицы:

```python
def test_prompt_columns_are_added_to_an_existing_table(tmp_path):
    """База переписки у всех уже создана, и CREATE TABLE IF NOT EXISTS её не
    тронет — колонки обязан долить владелец таблицы."""
    import sqlite3

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as old:      # таблица без prompt/model
        old.execute("CREATE TABLE messages (message_id INTEGER PRIMARY KEY,"
                    " thread_id TEXT NOT NULL, role TEXT NOT NULL, draft_text TEXT,"
                    " sent_text TEXT, angle TEXT, created_at TEXT NOT NULL, sent_at TEXT)")

    db = thread_store.connect(path)
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    assert {"prompt", "model"} <= columns
    db.close()
```

- [ ] **Step 4: `Attempt` в агенте**

В `backend/writer/services/agent.py`:

```python
@dataclass(frozen=True)
class Attempt:
    """Ход агента вместе с уликой: что именно ушло в модель и какой моделью
    отвечено. Без этого «почему модель написала это» отвечать нечем —
    сегодняшний seed и история уже другие."""
    draft: Draft
    prompt: list[tuple[str, str]]
    model: str
```

`draft()` возвращает `Attempt(draft=result, prompt=messages, model=llm_model_name)`. Имя модели бери из конфига, который уже читают `model()`/`client()`; передавай его в `draft()` параметром `model_name`, чтобы функция не лезла в конфиг сама.

- [ ] **Step 5: Обе функции `add_draft`**

`thread_store.add_draft` и `conversation.add_draft` получают `prompt: list[tuple[str, str]] | None = None` и `model: str | None = None`, пишут `json.dumps(prompt, ensure_ascii=False)` или `None`. Плюс `thread_store.draft_prompt(db, message_id)`:

```python
def draft_prompt(db, message_id):
    """Полный запрос, ушедший в модель. None — черновик написан до того, как
    промпт начали сохранять."""
    row = db.execute("SELECT prompt, model FROM messages WHERE message_id = ?",
                     (message_id,)).fetchone()
    if not row or not row[0]:
        return None
    return {"prompt": json.loads(row[0]), "model": row[1]}
```

- [ ] **Step 6: Три вызова**

- `writer/routes/threads.py::_writer_move` и `make_draft`: `proposal` теперь `Attempt`, поэтому `proposal.draft.stop`, `proposal.draft.text`, `proposal.draft.angle`, а в `add_draft` уходят `prompt=proposal.prompt, model=proposal.model`.
- `writer/services/operations.py`: та же правка внутри цикла.
- `sender/services/followup.py::_write` возвращает `Attempt`; вызывающий код в том же файле распаковывает `.draft` и передаёт промпт в `conversation.add_draft`.

`_seller_move` в `threads.py` возвращает `Move` и не меняется: у ответа продавца промпт собирает langgraph, и `Move` остаётся его формой.

- [ ] **Step 7: Прогнать всё**

Run: `cd backend && uv run pytest`
Expected: PASS. Тесты, ожидавшие от `agent.draft` объект `Draft`, правятся на `.draft` — это и есть новый контракт.

- [ ] **Step 8: Коммит**

```bash
git add backend
git commit -m "feat(writer): промпт и модель сохраняются рядом с черновиком"
```

---

## Task 6: Ручки промпта и очереди холодных черновиков

**Files:**
- Modify: `backend/writer/routes/threads.py`
- Modify: `backend/writer/db/thread_store.py` (`cold_drafts`)
- Modify: `backend/writer/db/leads_source.py` (имена и скоры компаний пачкой)
- Test: `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: `thread_store.draft_prompt` из Task 5.
- Produces:
  - `GET /api/threads/drafts` → `{"drafts": [{company_id, company_name, city, intent_score, thread_id, message_id, draft_text, angle, model, has_prompt}]}`, порядок по убыванию `intent_score`
  - `GET /api/threads/{company_id}/messages/{message_id}/prompt` → `{"prompt": [["system", "…"], ["human", "…"]], "model": "…"}`; `404`, если сообщения нет; `{"prompt": null, "model": null}`, если промпт не сохранялся

- [ ] **Step 1: Написать падающий тест**

В `backend/writer/tests/test_threads.py`:

```python
SEED = {"name": "Ромашка", "signals": []}


def test_cold_drafts_lists_only_the_first_touch():
    """Тред, в котором уже что-то отправлено, — не холодное касание: его
    место в «Диалогах», а не в конвейере проверки первых писем."""
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", SEED)
    thread_store.add_draft(db, "+77010000001", "первое", "crm_widget")
    thread_store.open_thread(db, "+77007776655", "c2", SEED)
    sent_id = thread_store.add_draft(db, "+77007776655", "уже писали", "ads_platform")
    # sent_text пишет ровно один автор — воркер системы 3; тест подтверждает
    # отправку тем же UPDATE'ом, что и соседние тесты файла.
    db.execute("UPDATE messages SET sent_text = ?, sent_at = ? WHERE message_id = ?",
               ("уже писали", thread_store.now(), sent_id))
    db.commit()

    drafts = thread_store.cold_drafts(db)
    assert [row["thread_id"] for row in drafts] == ["+77010000001"]
    assert drafts[0]["has_prompt"] is False
    db.close()


def test_prompt_of_a_draft_that_has_one():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", SEED)
    message_id = thread_store.add_draft(
        db, "+77010000001", "привет", "crm_widget",
        prompt=[("system", "правила"), ("human", "факты")], model="модель")
    assert thread_store.draft_prompt(db, message_id)["prompt"][0][0] == "system"
    db.close()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd backend && uv run pytest writer/tests/test_threads.py -v`
Expected: FAIL — `AttributeError: module 'writer.db.thread_store' has no attribute 'cold_drafts'`

- [ ] **Step 3: Запрос холодных черновиков**

```python
def cold_drafts(db):
    """Треды, где есть черновик и не отправлено ни одного сообщения, — то есть
    ровно первое касание. Порядок задаёт вызывающий: скор живёт в базе лидов."""
    rows = db.execute(
        "SELECT t.thread_id, t.company_id, m.message_id, m.draft_text, m.angle,"
        "       m.model, m.prompt IS NOT NULL"
        " FROM threads t JOIN messages m ON m.thread_id = t.thread_id"
        " WHERE m.role = 'outgoing' AND m.sent_text IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.thread_id = t.thread_id"
        "                   AND s.sent_text IS NOT NULL)"
        " ORDER BY m.message_id DESC"
    ).fetchall()
    keys = ("thread_id", "company_id", "message_id", "draft_text", "angle", "model")
    # has_prompt приводится к bool здесь: SQLite отдаёт 0/1, а контракт
    # фронтенда обещает булево — приводить его в TypeScript значило бы чинить
    # тип не там, где он рождается.
    return [{**dict(zip(keys, row[:6])), "has_prompt": bool(row[6])} for row in rows]
```

- [ ] **Step 4: Ручки**

В `backend/writer/routes/threads.py`:

```python
@router.get("/drafts")
def cold_drafts():
    """Очередь проверки первых писем. Сортировка по скору: проверять выгоднее
    с самых сильных лидов, а скор знает база системы 1."""
    leads, threads = open_stores()
    try:
        drafts = thread_store.cold_drafts(threads)
        cards = leads_source.cards_of(leads, [row["company_id"] for row in drafts])
        merged = [{**row, **cards.get(row["company_id"], {})} for row in drafts]
        merged.sort(key=lambda row: row.get("intent_score") or 0, reverse=True)
        return {"drafts": merged}
    finally:
        leads.close()
        threads.close()


@router.get("/{company_id}/messages/{message_id}/prompt")
def draft_prompt(company_id: str, message_id: int):
    """Отдельной ручкой, а не полем списка: промпт весит 2–4 КБ, и тащить его
    в каждую строку инбокса незачем."""
    leads, threads = open_stores()
    try:
        if not thread_store.message_exists(threads, message_id):
            raise HTTPException(404, f"сообщения {message_id} нет")
        stored = thread_store.draft_prompt(threads, message_id)
        return stored or {"prompt": None, "model": None}
    finally:
        leads.close()
        threads.close()
```

Порядок объявления важен: `/drafts` объявляется **до** `/{company_id}`, иначе FastAPI примет `drafts` за `company_id`.

`leads_source.cards_of(db, company_ids) -> dict[str, dict]` возвращает `{"company_name", "city", "intent_score"}` на компанию — одним запросом с `IN`, а не по одному на строку. Рядом уже живёт `company_names`; если он покрывает только имя, расширь его, а не заводи второй такой же.

`thread_store.message_exists(db, message_id) -> bool` — одна строка `SELECT 1`.

- [ ] **Step 5: Обновить `demo()`**

В `demo()` внизу `threads.py` перечисление путей роутера дополняется двумя новыми — иначе self-check упадёт на несовпадении множеств.

- [ ] **Step 6: Прогнать и закоммитить**

Run: `cd backend && uv run pytest && uv run python -m writer.routes.threads`
Expected: PASS + `web demo ok`

```bash
git add backend/writer
git commit -m "feat(writer): ручки полного промпта и очереди холодных черновиков"
```

---

## Task 7: Страница «Холодные»

**Files:**
- Create: `frontend/app/cold/page.tsx`
- Create: `frontend/components/MessageComposer.tsx`
- Create: `frontend/components/RefusalForm.tsx`
- Modify: `frontend/app/api.ts`
- Modify: `frontend/components/Sidebar.tsx`
- Modify: `frontend/app/globals.css`

**Interfaces:**
- Consumes: `GET /api/threads/drafts`, `GET /api/threads/{cid}/messages/{mid}/prompt` из Task 6; существующие `queueMessage`, `requestDraft`, `refuse`, `fetchQueue`.
- Produces: `MessageComposer` (принимает `companyId`, `messageId`, `threadId`, `text`, `angle`, `model`, `hasPrompt`, `onQueued`, `onRegenerate`) — переиспользуется в Task 11 на `/threads`.

- [ ] **Step 1: Контракт в `app/api.ts`**

```ts
export type ColdDraft = {
  company_id: string;
  company_name: string;
  city: string;
  intent_score: number;
  thread_id: string;
  message_id: number;
  draft_text: string;
  angle: string | null;
  model: string | null;
  has_prompt: boolean;
};

export function fetchColdDrafts() {
  return json<{ drafts: ColdDraft[] }>("/api/threads/drafts");
}

export function fetchPrompt(companyId: string, messageId: number) {
  return json<{ prompt: [string, string][] | null; model: string | null }>(
    `/api/threads/${encodeURIComponent(companyId)}/messages/${messageId}/prompt`,
  );
}
```

- [ ] **Step 2: `RefusalForm` отдельным файлом**

Перенеси `RefusalForm` из `frontend/app/LeadCard.tsx` в `frontend/components/RefusalForm.tsx` **без изменения поведения**: причина обязательна, минимум 3 символа, подпись про необратимость остаётся. Пока просто копия в новом файле — старый `LeadCard` удаляется в Task 11.

- [ ] **Step 3: `MessageComposer`**

Создай `frontend/components/MessageComposer.tsx`: `textarea` с текстом черновика, строка «угол · модель», `<details>` «Полный запрос в модель» (грузит промпт по первому раскрытию, показывает `system` и `human` раздельно, моноширинно), кнопки «Отправить» (`queueMessage`), «Перегенерировать» (`requestDraft`, подписана «стоит денег»). Если `has_prompt === false` — вместо содержимого `<details>` строка «черновик написан до того, как промпт начали сохранять» либо, для автоответов продавца, «собран агентом, см. Langfuse». Прочерк не рисуем: он читался бы как «промпта не было».

После постановки в очередь покажи, с какого номера и не раньше какого времени уйдёт — данные берутся из ответа `queueMessage` (`our_number`, `send_after`), как это уже делает нынешний `Thread`.

- [ ] **Step 4: Страница**

Создай `frontend/app/cold/page.tsx`: слева список из `fetchColdDrafts()` (имя, город, скор), счётчик «N ждут проверки», кнопка пайплайна `write` через `PipelineActions`; справа — шапка с ссылкой на `/leads/[id]`, `MessageComposer`, кнопки «Пропустить» (переход к следующему, ничего не пишет) и `RefusalForm`. Стрелки `←`/`→` и счётчик «3 из 27» над композером.

После «Отправить» или «Больше не писать» список перечитывается, и выделение переходит к следующему черновику: оператор проверяет их подряд, и возврат к пустому экрану на каждом шаге — лишний клик.

- [ ] **Step 5: Сайдбар**

Добавь пункт «Холодные» → `/cold` с иконкой `PaperPlaneTiltIcon` (у «Отправки» смени на `BroadcastIcon`, чтобы иконки не повторялись).

- [ ] **Step 6: Проверить в браузере**

Открой `http://localhost:3000/cold`. Ожидаемо: список черновиков по убыванию скора, текст правится, `<details>` раскрывает system и human, «Отправить» ставит в очередь и показывает номер и время.

- [ ] **Step 7: Коммит**

```bash
git add frontend
git commit -m "feat(web): страница «Холодные» — конвейер черновиков с полным промптом"
```

---

# Фаза 3. Карточка лида

## Task 8: Карточка отдаёт досье, сырьё и ответы модели

**Files:**
- Modify: `backend/collector/db/lead.py`
- Modify: `backend/collector/services/leads.py`
- Test: `backend/collector/tests/test_leads.py`

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces: `GET /api/leads/{company_id}` дополнительно отдаёт:
  - `dossier: {summary, approach, decision_maker, hooks: [{angle, quote, url, source, observed_at}], confidence} | null`
  - `fetches: [{url, final_url, status, fetched_at}]`
  - `llm_answers: [{kind, model, prompt, answer}]`

- [ ] **Step 1: Написать падающий тест**

В `backend/collector/tests/test_leads.py`:

`_published_run(db)` в этом файле уже кладёт компанию `c1` с досье и зацепками.
Не хватает сырья, ответа модели и `breakdown` со ссылкой — их добавляет
локальный помощник:

```python
def _evidence(db):
    """Сырьё, ответ модели и breakdown со ссылкой: карточке нужно показать,
    откуда взяты факты и когда они скачаны."""
    db.execute("UPDATE scores_all SET breakdown = ?",
               ('[{"rule": "crm_widget", "contribution": 2.0,'
                ' "url": "https://romashka.kz/", "quote": "виджет Bitrix24"}]',))
    db.execute("INSERT INTO fetches_all (run_id, url, sha, final_url, status, fetched_at)"
               " VALUES (1, 'https://romashka.kz/', 'abc', 'https://romashka.kz/uslugi',"
               "         200, '2026-08-12T09:14:03Z')")
    db.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
               " VALUES ('site', 'Ромашка | almaty', 'm', 'что делает сайт?', '{}')")
    db.commit()


def test_card_carries_the_dossier_the_prompt_uses(stores):
    """Досье уходит в промпт через seed — значит оператор обязан видеть его
    там же, где решает, писать ли этой компании."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    card = service.card(db, "c1")
    assert card["dossier"]["summary"] == "бухгалтерия"
    assert card["dossier"]["hooks"][0]["quote"] == "оставьте заявку"


def test_card_carries_the_freshness_of_the_raw(stores):
    """final_url показывается всегда, а не только при расхождении с url:
    подмена страницы источником — единственное, что о ней вообще сообщает."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    _evidence(db)
    fetches = service.card(db, "c1")["fetches"]
    assert fetches[0]["fetched_at"] == "2026-08-12T09:14:03Z"
    assert fetches[0]["final_url"] == "https://romashka.kz/uslugi"


def test_card_carries_the_paid_model_answers(stores):
    """Ключ собирается так же, как при записи в analyze.py: «название | город»."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    _evidence(db)
    answers = service.card(db, "c1")["llm_answers"]
    assert [answer["kind"] for answer in answers] == ["site"]
```

Обрати внимание: `name_norm` компании — `Ромашка`, город — `almaty`, поэтому
ключ `llm_answers` в тесте именно `'Ромашка | almaty'`. Если в `card()` ключ
собирается иначе, чем в `analyze.py`, ответы модели просто не найдутся — тест
на это и стоит.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd backend && uv run pytest collector/tests/test_leads.py -v`
Expected: FAIL — `KeyError: 'dossier'`

- [ ] **Step 3: Запросы в `collector/db/lead.py`**

```python
def dossier_of(db, company_id):
    """Досье текущего прогона: то же, что уходит в промпт через seed."""
    row = db.execute(
        "SELECT summary, approach, decision_maker, hooks, confidence"
        " FROM dossiers WHERE company_id = ?", (company_id,)).fetchone()
    if not row:
        return None
    return {"summary": row[0], "approach": row[1], "decision_maker": row[2],
            "hooks": json.loads(row[3] or "[]"), "confidence": row[4]}


def fetches_of(db, urls):
    """Свежесть сырья: когда скачано и не подменил ли источник страницу.

    Ключ — сами url, а не company_id: `company_links` связывает компанию с
    филиалом 2ГИС, а не со страницей, и единственный честный список страниц
    компании — тот, что уже собрал скоринг (export.sources_of по breakdown).
    """
    if not urls:
        return []
    marks = ",".join("?" * len(urls))
    rows = db.execute(
        f"SELECT url, final_url, status, fetched_at FROM fetches"
        f" WHERE url IN ({marks}) ORDER BY fetched_at DESC", tuple(urls)).fetchall()
    return [{"url": url, "final_url": final_url, "status": status, "fetched_at": fetched_at}
            for url, final_url, status, fetched_at in rows]


def llm_answers_of(db, subject, username=None):
    """Оплаченные ответы модели по компании. Ключ собирается так же, как при
    записи (analyze.py): «название | город» для reviews/site/dossier и логин
    инстаграма для ig_signals — второго способа собрать его быть не должно."""
    subjects = [subject] + ([username] if username else [])
    marks = ",".join("?" * len(subjects))
    rows = db.execute(
        f"SELECT kind, model, prompt, answer FROM state.llm_answers"
        f" WHERE subject IN ({marks}) ORDER BY id DESC", subjects).fetchall()
    return [{"kind": kind, "model": model, "prompt": prompt, "answer": answer}
            for kind, model, prompt, answer in rows]
```

- [ ] **Step 4: Собрать в `services/leads.py::card`**

```python
def card(db, company_id):
    """Карточка: каналы, сигналы, разбор скора, досье, сырьё и ответы модели.
    Одна карточка — один запрос: клиенту незачем собирать её из пяти ручек."""
    row = next(report.candidates(db, company_id), None)
    if not row:
        return None
    suppressed = store.suppression_handles(db)
    username = instagram_login(row["channels"])
    return {
        **as_lead(row, report.best_channel(row["channels"], suppressed)),
        "channels": [...],                     # как сейчас
        "signals": store.signals_of(db, company_id),
        "breakdown": row["breakdown"],
        "dossier": store.dossier_of(db, company_id),
        "fetches": store.fetches_of(db, report.sources_of(row["breakdown"]).split()),
        "llm_answers": store.llm_answers_of(db, f"{row['name']} | {row['city']}", username),
    }
```

`instagram_login(channels)` — маленькая чистая функция в том же модуле: достаёт логин из канала вида `instagram`. Если инстаграм-канала нет, возвращает `None`.

- [ ] **Step 5: Прогнать и закоммитить**

Run: `cd backend && uv run pytest collector/tests -v`
Expected: PASS

```bash
git add backend/collector
git commit -m "feat(leads): карточка отдаёт досье, свежесть сырья и ответы модели"
```

---

## Task 9: Страницы «Лиды» и карточка лида

**Files:**
- Create: `frontend/app/leads/page.tsx`
- Create: `frontend/app/leads/[id]/page.tsx`
- Create: `frontend/components/LeadDossier.tsx`
- Modify: `frontend/app/api.ts`
- Modify: `frontend/components/Sidebar.tsx`
- Modify: `frontend/app/globals.css`

**Interfaces:**
- Consumes: расширенный `GET /api/leads/{id}` из Task 8; `RefusalForm` из Task 7.
- Produces: страница `/leads/[id]`, на которую ссылаются `/cold` и `/threads`.

- [ ] **Step 1: Контракт в `app/api.ts`**

```ts
export type Hook = { angle: string; quote: string; url: string | null;
  source: string | null; observed_at: string | null };

export type Dossier = { summary: string | null; approach: string | null;
  decision_maker: string | null; hooks: Hook[]; confidence: number | null };

export type Fetch = { url: string; final_url: string | null;
  status: number | null; fetched_at: string | null };

export type ModelAnswer = { kind: string; model: string; prompt: string; answer: string };
```

и добавь `dossier: Dossier | null; fetches: Fetch[]; llm_answers: ModelAnswer[]` в `LeadDetail`.

- [ ] **Step 2: `/leads` — выдача**

Перенеси `frontend/app/sourcing/page.tsx` в `frontend/app/leads/page.tsx`. Изменения ровно два: правая колонка с `LeadCard` исчезает, вместо неё строки списка — ссылки `Link` на `/leads/[id]`; `PipelineActions` получает только `discover` и `classify`, а `JobMonitor` убирается (его место занял `RunBar` из Task 4). Фильтры городов и лимита остаются как есть.

- [ ] **Step 3: `LeadDossier`**

Создай `frontend/components/LeadDossier.tsx` — секции в порядке спеки:

1. шапка: имя, отрасль, город, домен, `intent`/`fit`;
2. «Почему сейчас»: `why_now`, цитата, метка «от модели / по шаблону»;
3. **«Разбор скора»**: таблица по `breakdown` — правило, вклад, цитата, ссылка, дата. Это поле уже приезжало с бэкенда и нигде не рисовалось;
4. **«Досье модели»**: `summary`, `approach`, `decision_maker`, список `hooks` (угол, цитата, ссылка, дата);
5. «Сигналы» и «Каналы» — перенеси из нынешнего `LeadCard` без изменений, вместе с `ChannelRow`;
6. **«Сырьё»**: таблица `fetches` — url, `final_url`, статус, когда скачано;
7. **«Ответы модели»**: по записи на `llm_answers`, промпт и ответ в `<details>`, моноширинно;
8. «Переписка»: краткая сводка (сколько отправлено, сколько ответов) со ссылкой на `/threads`, без композера;
9. `RefusalForm`.

- [ ] **Step 4: `/leads/[id]`**

Создай `frontend/app/leads/[id]/page.tsx`: читает `id` из параметров маршрута, грузит `fetchLead(id)`, рисует `LeadDossier`, сверху — ссылка «← к выдаче». После отказа возвращает на `/leads`: лид мог уйти из выдачи, и оставлять оператора на карточке, которой больше нет в списке, — врать ему.

- [ ] **Step 5: Проверить в браузере**

Открой `http://localhost:3000/leads`, кликни лида. Ожидаемо: разбор скора с вкладами, досье с зацепками, таблица сырья с датами, ответы модели раскрываются.

- [ ] **Step 6: Коммит**

```bash
git add frontend
git commit -m "feat(web): страница лидов и карточка с разбором скора, досье и сырьём"
```

---

# Фаза 4. Диалоги и уборка

## Task 10: Инбокс сортируется по срочности и знает вид касания

**Files:**
- Modify: `backend/writer/db/thread_store.py` (`inbox`)
- Modify: `backend/writer/routes/threads.py` (`conversation`/`state`)
- Test: `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `GET /api/threads` — порядок: `escalated` → есть непрочитанный ответ → остальные по дате; каждая строка дополнительно несёт `status`
  - `GET /api/threads/{company_id}` — каждое сообщение истории несёт `kind` (`cold` / `followup` / `reply` / `null`) и `message_id`

- [ ] **Step 1: Написать падающий тест**

```python
def test_inbox_puts_the_urgent_first(threads_db):
    """Эскалированный тред ждёт человека прямо сейчас, ответивший — почти;
    молчащий не ждёт никого. Порядок задаёт бэкенд, страница его не считает."""
    # три треда: escalated, с ответом лида, молчащий
    ...
    order = [row["thread_id"] for row in thread_store.inbox(threads_db)]
    assert order == [ESCALATED, ANSWERED, SILENT]


def test_a_thread_we_already_answered_is_not_urgent(threads_db):
    """Лид ответил, мы ответили — ждать нечего. Считать «когда-либо отвечал»
    значило бы держать наверху каждый живой диалог."""
    # тред, где последнее сообщение — наше, стоит ниже треда с ответом лида
    ...


def test_history_carries_the_kind_of_touch(threads_db):
    """reply и followup ставит только автомат — по ним и подписывается
    «отправлено автоматом»."""
    history = thread_store.history(threads_db, "+77001112233")
    assert history[0]["kind"] == "cold"
    assert history[0]["message_id"] > 0
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest writer/tests/test_threads.py -k "urgent or kind" -v`
Expected: FAIL

- [ ] **Step 3: Порядок и вид касания**

`inbox` добавляет в `SELECT` `t.status` и меняет `ORDER BY` на:

```sql
 ORDER BY CASE t.status WHEN 'escalated' THEN 0 ELSE 1 END,
          CASE WHEN (SELECT m.role FROM messages m WHERE m.thread_id = t.thread_id
                     AND m.sent_text IS NOT NULL
                     ORDER BY m.message_id DESC LIMIT 1) = 'incoming'
               THEN 0 ELSE 1 END,
          7 DESC NULLS LAST, t.created_at DESC
```

Наверх поднимается тред, где **последнее** сообщение — от лида, а не любой, где
лид когда-либо отвечал: второе держало бы в голове списка каждый живой диалог,
на который мы уже ответили, и «кому ответить» перестало бы читаться с экрана.

`history` добавляет `message_id` и `kind`. `kind` берётся из `outbox` по `message_id` (`LEFT JOIN outbox o ON o.message_id = m.message_id`), потому что вид касания — собственность системы 3, а не переписки. У входящих `kind` пуст.

- [ ] **Step 4: Прогнать и закоммитить**

Run: `cd backend && uv run pytest writer/tests -v`
Expected: PASS

```bash
git add backend/writer
git commit -m "feat(writer): инбокс по срочности, история знает вид касания"
```

---

## Task 11: Страница «Диалоги», редиректы и снос дублей

**Files:**
- Create: `frontend/app/threads/page.tsx`
- Modify: `frontend/app/sourcing/page.tsx` → редирект
- Modify: `frontend/app/writer/page.tsx` → редирект
- Delete: `frontend/app/LeadCard.tsx`
- Modify: `frontend/components/Sidebar.tsx`, `frontend/app/page.tsx`, `frontend/app/api.ts`

**Interfaces:**
- Consumes: `MessageComposer` (Task 7), инбокс с сортировкой и `kind` (Task 10).
- Produces: финальная навигация из шести пунктов.

- [ ] **Step 1: `/threads`**

Создай `frontend/app/threads/page.tsx` на основе нынешнего `frontend/app/writer/page.tsx`: слева инбокс (порядок задаёт бэкенд, страница не пересортировывает), справа — переписка плюс `MessageComposer`. Вместо `LeadCard` в правой колонке — шапка с именем компании и ссылкой на `/leads/[id]`. У каждого нашего сообщения — вид касания и, для `reply`/`followup`, подпись «отправлено автоматом». Секция очереди (`fetchQueue`) остаётся: она показывает, что уже стоит в `outbox`.

Форма «Ответ лида — вставить как есть» (`addIncoming`) переезжает сюда из
нынешнего `Thread` целиком. Без неё ответ, пришедший мимо системы — с личного
телефона, из другого мессенджера, — записать было бы негде, и следующий ход
агента строился бы на неполной истории.

- [ ] **Step 2: Редиректы вместо старых страниц**

Замени содержимое обоих файлов целиком:

```tsx
import { redirect } from "next/navigation";

/** Страница разъехалась на /leads (выдача) и /leads/[id] (карточка).
 *  Редирект, а не удаление: ссылки в закладках оператора не должны отдавать 404. */
export default function Page() {
  redirect("/leads");
}
```

и то же для `app/writer/page.tsx` → `/threads`.

- [ ] **Step 3: Удалить `LeadCard`**

```bash
rm frontend/app/LeadCard.tsx
```

Убедись, что импортов не осталось:

```bash
grep -rn "LeadCard" frontend --include=*.tsx
```
Expected: пусто

- [ ] **Step 4: Сайдбар и «Обзор»**

Сайдбар: Обзор · Лиды · Холодные · Диалоги · Отправка · Процессы. `systemOf()` сопоставляет `discover|classify|rebuild → /leads`, `write → /cold`.

На «Обзоре» подправь гайд под новые названия и пути (шаг 3 — «Сайдбар → Лиды», шаг 4 — «Сайдбар → Холодные»), а `PipelineActions` и `JobMonitor` убери: запуск теперь контекстный, монитор — в `RunBar` и на «Процессах».

- [ ] **Step 5: Проверить все маршруты**

```bash
cd frontend && npm run build
```
Expected: сборка без ошибок типов.

Затем вручную: `/`, `/leads`, `/leads/<id>`, `/cold`, `/threads`, `/sender`, `/activity`, плюс `/sourcing` и `/writer` — оба должны увести на новые адреса.

- [ ] **Step 6: Коммит**

```bash
git add frontend
git commit -m "refactor(web): страницы по сущностям, редиректы со старых путей, LeadCard разобран"
```

---

## Task 12: Документация

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Обновить разделы**

В «Архитектура frontend/» перепиши карту страниц под шесть новых и добавь абзац про `activity` — журнал фоновой работы, схлопывание повторов, пульс из журнала. В «Слои данных» упомяни таблицу `activity` рядом с описанием `state.db`, а в описании `messages` — колонки `prompt` и `model`. В разделе про систему 3 замени упоминание `heartbeat` из памяти процесса на пульс из журнала.

- [ ] **Step 2: Коммит**

```bash
git add CLAUDE.md
git commit -m "docs: карта страниц, журнал процессов и промпт в CLAUDE.md"
```

---

## Порядок и точки остановки

Фазы независимы и каждая оставляет приложение рабочим:

- после **Фазы 1** (Tasks 1–4) видно всё, что делают демоны;
- после **Фазы 2** (Tasks 5–7) холодные письма проверяются на своём экране с полным промптом;
- после **Фазы 3** (Tasks 8–9) карточка лида показывает всё, что система знает;
- после **Фазы 4** (Tasks 10–12) старых страниц больше нет, дублей тоже.

Старые страницы удаляются только в Task 11 — то есть тогда, когда замена уже стоит и проверена.
