# План реализации: песочница аутрича

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Прогнать весь аутрич — первое касание, диалог, follow-up, прогрев — не отправив в WhatsApp ни одного сообщения: оператор играет лида на странице `/sandbox`, транспорт подменён, часы двигаются кнопкой.

**Architecture:** Два модуля верхнего уровня забирают себе то, что раньше знали по пять и по шесть мест: `backend/paths.py` — путь к невосстановимому слою, `backend/clock.py` — «сейчас» продукта. Оба — швы с `use()`, как `activity.py`. Поверх них пакет `backend/sandbox/`: прогон — отдельный файл базы, подменный Node отдаёт контракт настоящего ручка в ручку, события уезжают в свой же `POST /api/sender/webhook`. Системы 1–3 про песочницу не знают ни строкой.

**Tech Stack:** Python 3.13 (uv, один venv на `backend/`), FastAPI, SQLite, httpx, pytest + pytest-asyncio; фронтенд — Next.js App Router, `@phosphor-icons/react`.

**Spec:** `docs/superpowers/specs/2026-09-03-outreach-sandbox-design.md`

## Global Constraints

- Тесты гоняются из `backend/`: `uv run pytest`. Сети в тестах нет.
- `state.db` — невосстановимый слой: только `CREATE TABLE IF NOT EXISTS` и `ALTER TABLE ADD COLUMN`, `DROP` запрещён.
- Колонки таблицы доливает её владелец: `threads`/`messages` — `writer/db/thread_store.py`, `outbox`/`numbers` — `sender/db/migrate.py`, `sandbox_meta` — `backend/sandbox/runs.py`.
- `writer.*` не импортирует `collector.*`; `sender.*` не импортирует ни того, ни другого. `backend/sandbox/` импортирует всех, его — никто.
- Модули верхнего уровня `backend/*.py` тестируются в `collector/tests/` (прецедент: `test_activity.py`, `test_observability.py`).
- Тип-хинты на каждой сигнатуре, `is None` вместо `== None`, `pathlib.Path` вместо строк, никаких изменяемых значений по умолчанию.
- Комментарии объясняют «почему», а не «что». Русский язык в комментариях, логах и UI.
- Роутеры песочницы монтируются только при `SANDBOX=1`; без флага их в приложении нет.
- Каждая задача заканчивается коммитом с `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: `backend/paths.py` — один владелец пути к невосстановимому слою

**Files:**
- Create: `backend/paths.py`, `backend/collector/tests/test_paths.py`
- Modify: `backend/collector/services/store.py:16-17,54,63`, `backend/collector/api.py:63-64,82`, `backend/writer/services/config.py:17`, `backend/writer/config.toml:6`, `backend/writer/services/operations.py:21`, `backend/writer/routes/threads.py:189-191`, `backend/writer/db/leads_source.py:37-43`, `backend/collector/services/metrics.py:14,17,24,46,66,101-103`, `backend/sender/services/config.py:26`, `backend/sender/config.toml:6`, `backend/sender/routes/sender.py:47-48`, `backend/sender/routes/webhook.py:91-92`, `backend/collector/tests/conftest.py:21`, `backend/collector/tests/test_runs.py:150`, `backend/collector/tests/test_jobs.py:108,114`, `backend/sender/tests/test_config.py:10-15`

**Interfaces:**
- Consumes: ничего.
- Produces: `paths.PRODUCTION_STATE: Path`, `paths.state_db() -> Path`, `paths.use_run(path: Path | None) -> None`, `paths.run_path() -> Path | None`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/collector/tests/test_paths.py`:

```python
"""Путь к невосстановимому слою: один владелец на три системы.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import paths


def test_production_database_by_default():
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.state_db().name == "state.db"


def test_production_path_lives_in_collector_data():
    """Путь тот же, что раньше называла константа store.STATE: переезда файла
    эта задача не делает."""
    assert paths.PRODUCTION_STATE.parent.name == "data"
    assert paths.PRODUCTION_STATE.parent.parent.name == "collector"


def test_run_overrides_production_and_none_returns_it_back(tmp_path):
    paths.use_run(tmp_path / "run.db")
    try:
        assert paths.state_db() == tmp_path / "run.db"
        assert paths.run_path() == tmp_path / "run.db"
    finally:
        paths.use_run(None)
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert paths.run_path() is None


def test_use_run_accepts_a_string(tmp_path):
    """Путь приезжает из json ручки песочницы строкой, а внутри всё на Path."""
    paths.use_run(str(tmp_path / "run.db"))
    try:
        assert paths.state_db() == tmp_path / "run.db"
    finally:
        paths.use_run(None)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paths'`

- [ ] **Step 3: Написать `backend/paths.py`**

```python
"""Где лежит невосстановимый слой. Единственный владелец пути на три системы.

Модуль верхнего уровня по тому же основанию, что config.py и activity.py: путь
к state.db называли пять мест — константа store.STATE, два config.toml, ATTACH
в leads_source и metrics, — и «если песочница» в каждом из них было бы пятью
шансами забыть про один.

Прогон песочницы подменяет базу здесь и больше нигде. В бою use_run() никто не
зовёт, и state_db() возвращает тот же файл, что раньше называла константа.
"""

from pathlib import Path

PRODUCTION_STATE = Path(__file__).resolve().parent / "collector" / "data" / "state.db"

_run: Path | None = None


def state_db() -> Path:
    """Боевая база или база активного прогона песочницы."""
    return _run if _run is not None else PRODUCTION_STATE


def use_run(path: Path | str | None) -> None:
    """Шов: какой прогон активен, знает песочница, а paths — только путь.
    None возвращает боевую базу."""
    global _run
    _run = Path(path) if path is not None else None


def run_path() -> Path | None:
    """Активный прогон или None. Нужен предохранителям: писать в боевую базу
    из ручки песочницы нельзя, и отличить одно от другого можно только здесь."""
    return _run
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `cd backend && uv run pytest collector/tests/test_paths.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Перевести collector на `paths`**

В `backend/collector/services/store.py` удалить строку 17 (`STATE = DATA / "state.db"`), добавить `import paths` к импортам и заменить два обращения внутри `connect()`:

```python
def connect():
    """Соединение к derived.db с ATTACH state. Включает обе схемы.

    derived — главная (WAL), state — attached (WAL). Схема читается из
    db/schema.sql и делится маркерами на две половины; DDL живёт в одном
    файле, а не в двух местах.

    STATE-блок применяется к отдельному соединению, чьей ГЛАВНОЙ базой является
    state.db: его DDL без префикса (`CREATE TABLE IF NOT EXISTS suppression`)
    иначе создал бы таблицы в derived (главной базе текущего соединения), и
    `state.suppression` не существовал бы.

    Какой это файл — боевой или прогон песочницы — решает paths, а не эта
    функция: путь к невосстановимому слою один на три системы.
    """
    state = paths.state_db()
    state_db = sqlite3.connect(state)
    state_db.executescript(_schema("STATE"))
    state_db.commit()
    state_db.close()

    db = sqlite3.connect(DERIVED, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(_schema("DERIVED"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("ATTACH DATABASE ? AS state", (str(state),))
    db.execute("PRAGMA state.journal_mode=WAL")
    db.commit()
    return db
```

Там же добавить публичный доступ к STATE-блоку — он понадобится песочнице в Task 3, а второй копии DDL в проекте быть не должно:

```python
def state_schema() -> str:
    """STATE-блок schema.sql для того, кто создаёт свою базу невосстановимого
    слоя (песочница). Публичный, потому что альтернатива — вторая копия DDL.
    """
    return _schema("STATE")
```

В `backend/collector/api.py` заменить строки 63-64 и 82:

```python
activity.use(paths.state_db())
analytics.use(paths.state_db(), store.DERIVED)
```

```python
    thread_store.connect(paths.state_db()).close()
```

и добавить `import paths` рядом с `import activity`.

- [ ] **Step 6: Перевести writer на `paths`**

В `backend/writer/config.toml` удалить строку `threads_db = "../collector/data/state.db"`.
В `backend/writer/services/config.py` удалить строку `config["threads_db"] = (HOME / config["threads_db"]).resolve()`.

В `backend/writer/services/operations.py:21` — `threads = thread_store.connect(paths.state_db())`, добавить `import paths`.

В `backend/writer/routes/threads.py` — `open_stores()`:

```python
def open_stores():
    return (leads_source.connect(CONFIG["leads_db"]),
            thread_store.connect(paths.state_db()))
```

добавить `import paths`.

В `backend/writer/db/leads_source.py` — `connect()`:

```python
def connect(path):
    """Только чтение: derived.db + ATTACH невосстановимого слоя для фильтра
    отказов. Какой это файл, решает paths: в песочнице suppression пустой, и
    выдумывать его имя рядом с derived.db нельзя — базы лежат врозь."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute(f"ATTACH DATABASE 'file:{paths.state_db()}?mode=ro' AS state", ())
    return db
```

добавить `import paths` и удалить строку `from pathlib import Path` (строка 18): после этой правки `Path` в файле больше не используется, и ruff покраснеет на неиспользованном импорте.

- [ ] **Step 7: Перевести sender и metrics на `paths`**

В `backend/sender/config.toml` удалить строку `state_db = "../collector/data/state.db"` вместе с абзацем комментария про относительные пути над ней (он объяснял именно эту строку).
В `backend/sender/services/config.py` удалить строку `config["state_db"] = (HOME / config["state_db"]).resolve()`.

В `backend/sender/routes/sender.py:47-48` и `backend/sender/routes/webhook.py:91-92` — одинаково:

```python
def connect() -> sqlite3.Connection:
    return migrate.connect(paths.state_db())
```

добавить `import paths` в оба файла.

В `backend/collector/services/metrics.py` удалить функцию `threads_db_path()` (строки 101-103), константу `WRITER_HOME` (строка 24) и импорты `tomllib` (строка 14) и `from pathlib import Path` (строка 17) — после правки оба перестают использоваться; заменить оба вызова (`writer_stats`, `sender_stats`) на `db_path = paths.state_db()`, добавить `import paths`. Шапку модуля поправить — абзац про чтение пути из `writer/config.toml` больше не описывает код:

```python
Путь до невосстановимого слоя приходит из backend/paths.py — единственного
владельца: у системы 2 свои зависимости, и тащить их в collector ради одного
пути значило бы падать от чужого requirements.
```

- [ ] **Step 8: Починить тесты, которые держались за старые имена**

`backend/collector/tests/conftest.py:21` и `backend/collector/tests/test_runs.py:150` подменяли константу `engine.STATE`. Теперь подменяется боевой путь у его владельца — в обоих местах:

```python
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
```

с `import paths` в шапке каждого файла (`engine.DERIVED` в тех же строках остаётся как было).

`backend/collector/tests/test_jobs.py:108` проверял согласие путей через удалённую функцию. В `test_frontend_contract` заменить строки 108-118 целиком — и **обязательно переименовать локальную переменную `paths`**, иначе присваивание сделает имя локальным на всю функцию и строка 108 упадёт `UnboundLocalError`:

```python
    assert paths.state_db().name == "state.db", \
        "путь к невосстановимому слою разошёлся с paths.PRODUCTION_STATE"

    from sender.routes import sender

    assert sender.router.prefix == "/api/sender"
    mounted = {route.path for route in sender.router.routes}
    assert {"/api/sender", "/api/sender/numbers", "/api/sender/queue",
            "/api/sender/autopilot"} <= mounted, mounted
```

Добавить `import paths` в шапку файла.

В `backend/sender/tests/test_config.py` удалить `test_load_resolves_state_db_to_absolute_path` целиком: ключа `state_db` в конфиге больше нет, а путь проверяет `collector/tests/test_paths.py`.

- [ ] **Step 9: Прогнать всё**

Run: `cd backend && uv run pytest -q`
Expected: PASS, ни одного упавшего теста. Если падает `writer/tests/test_operations.py` или `collector/tests/test_jobs.py` — искать оставшийся `CONFIG["threads_db"]` или `config.load()["state_db"]`: `grep -rn "threads_db\|state_db" --include="*.py" --include="*.toml" backend/ | grep -v tests`

- [ ] **Step 10: Коммит**

```bash
git add backend/paths.py backend/collector backend/writer backend/sender
git commit -m "$(cat <<'EOF'
refactor: путь к невосстановимому слою — один владелец backend/paths.py

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `backend/clock.py` — «сейчас» продукта

**Files:**
- Create: `backend/clock.py`, `backend/collector/tests/test_clock.py`
- Modify: `backend/sender/routes/sender.py:51-52`, `backend/sender/routes/webhook.py:95-96`, `backend/sender/services/warmup.py:260-261`, `backend/sender/services/worker.py:336-337`, `backend/sender/db/conversation.py:264-265`, `backend/writer/db/thread_store.py:74-75`

**Interfaces:**
- Consumes: ничего.
- Produces: `clock.now() -> datetime` (aware, UTC), `clock.use(offset: timedelta | None) -> None`, `clock.offset() -> timedelta`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/collector/tests/test_clock.py`:

```python
"""«Сейчас» продукта: системное время в бою, сдвинутое в песочнице."""

from datetime import datetime, timedelta, timezone

import pytest

import clock


@pytest.fixture(autouse=True)
def reset():
    """Смещение — глобальное состояние процесса: тест, забывший его вернуть,
    сдвинул бы время всем следующим."""
    yield
    clock.use(None)


def test_now_is_system_time_without_a_run():
    assert abs(clock.now() - datetime.now(timezone.utc)) < timedelta(seconds=1)
    assert clock.now().tzinfo is not None
    assert clock.offset() == timedelta()


def test_offset_moves_now_forward():
    clock.use(timedelta(days=3))
    moved = clock.now() - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < moved < timedelta(days=3, seconds=1)


def test_use_none_returns_the_system_clock():
    clock.use(timedelta(days=3))
    clock.use(None)
    assert abs(clock.now() - datetime.now(timezone.utc)) < timedelta(seconds=1)


def test_the_seam_reaches_system_three():
    """Шов бесполезен, если его позвал не весь продукт: тик воркера и вебхук
    обязаны видеть то же «сейчас», что и пульт песочницы."""
    from sender.routes import sender as sender_routes
    from sender.routes import webhook
    from sender.services import worker

    clock.use(timedelta(days=3))
    for moment in (sender_routes.now(), webhook.now(), worker._now()):
        assert moment - datetime.now(timezone.utc) > timedelta(days=2, hours=23)


def test_the_seam_reaches_the_conversation_stamp():
    """Время записи сообщения тоже принадлежит прогону: иначе в ленте
    песочницы у первого касания и у follow-up стояла бы одна дата."""
    from writer.db import thread_store

    clock.use(timedelta(days=3))
    stamped = datetime.fromisoformat(thread_store.now())
    assert stamped - datetime.now(timezone.utc) > timedelta(days=2, hours=23)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest collector/tests/test_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clock'`

- [ ] **Step 3: Написать `backend/clock.py`**

```python
"""«Сейчас» продукта. В бою — системное время, в песочнице — сдвинутое.

Шов, а не настройка: смещение принадлежит прогону песочницы, и какой прогон
активен, знает она — clock хранит только сдвиг, тем же приёмом, каким
activity.use() хранит путь к журналу.

В бою use() не зовёт никто, смещение нулевое, и clock.now() — это буквально
datetime.now(timezone.utc). Поэтому переводить на него можно всё, включая
код, который в песочнице не работает.
"""

from datetime import datetime, timedelta, timezone

_offset = timedelta()


def now() -> datetime:
    """Момент, который продукт считает настоящим. Всегда с зоной: наивное
    время в очереди означало бы окно отправки не в том часовом поясе."""
    return datetime.now(timezone.utc) + _offset


def use(offset: timedelta | None) -> None:
    """Шов: сдвиг приходит от того, кто знает про прогоны. None — бой."""
    global _offset
    _offset = offset if offset is not None else timedelta()


def offset() -> timedelta:
    return _offset
```

- [ ] **Step 4: Перевести шесть мест на `clock.now()`**

`backend/sender/routes/sender.py` и `backend/sender/routes/webhook.py` — в обоих:

```python
def now() -> datetime:
    return clock.now()
```

с `import clock` в шапке; импорт `timezone` убрать, если он больше не используется в файле.

`backend/sender/services/warmup.py:260-261` и `backend/sender/services/worker.py:336-337` — то же самое для `_now()` (имя функции не менять).

`backend/sender/db/conversation.py:264-265`:

```python
def now() -> str:
    return clock.now().isoformat(timespec="seconds")
```

`backend/writer/db/thread_store.py:74-75`:

```python
def now():
    return clock.now().isoformat(timespec="seconds")
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest collector/tests/test_clock.py -v && uv run pytest -q`
Expected: PASS — 5 passed в файле, вся сюита зелёная. Существующие тесты о часах не знают: смещение нулевое, поведение прежнее.

- [ ] **Step 6: Коммит**

```bash
git add backend/clock.py backend/collector/tests/test_clock.py backend/sender backend/writer
git commit -m "$(cat <<'EOF'
feat: backend/clock.py — «сейчас» продукта одним швом

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Прогон песочницы — отдельный файл базы

**Files:**
- Create: `backend/sandbox/__init__.py`, `backend/sandbox/runs.py`, `backend/sandbox/tests/__init__.py`, `backend/sandbox/tests/test_runs.py`
- Modify: `backend/pyproject.toml:24` (`testpaths`)

**Interfaces:**
- Consumes: `paths.use_run`, `paths.state_db`, `clock.use`, `store.state_schema`, `store.DERIVED`, `thread_store.connect`, `migrate.connect`, `numbers.register`, `activity.use`, `analytics.use`.
- Produces:
  - `runs.RUNS_DIR: Path`, `runs.SANDBOX_NUMBER: str`
  - `runs.Run` — frozen dataclass: `run_id: str`, `company_id: str`, `created_at: str`, `warmed: bool`, `offset: timedelta`, свойство `path: Path`
  - `runs.UnknownRunError(Exception)`
  - `runs.create(company_id: str, warmed: bool, moment: datetime) -> Run`
  - `runs.all() -> list[Run]`
  - `runs.get(run_id: str) -> Run`
  - `runs.activate(run_id: str) -> Run`
  - `runs.active() -> Run | None`
  - `runs.shift(seconds: int) -> Run`
  - `runs.deactivate() -> None`

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sandbox/tests/__init__.py` (пустой) и `backend/sandbox/tests/test_runs.py`:

```python
"""Прогон — отдельный файл базы: чистота контекста обеспечена физически."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import clock
import paths
from sandbox import runs

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Прогоны уезжают в tmp_path: тест, создавший файл в data/sandbox/,
    остался бы в репозитории навсегда."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    yield
    runs.deactivate()


def test_create_makes_a_database_with_all_three_schemas():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    assert run.path.exists()
    with sqlite3.connect(run.path) as db:
        tables = {row[0] for row in
                  db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"threads", "messages", "outbox", "numbers", "suppression",
            "sandbox_meta"} <= tables


def test_create_seeds_one_number_warmed_or_new():
    warm = runs.create("c_warm", warmed=True, moment=MOMENT)
    cold = runs.create("c_cold", warmed=False, moment=MOMENT)
    assert _number_status(warm) == "active"
    assert _number_status(cold) == "new"


def test_run_id_carries_the_company_and_the_moment():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    assert run.run_id == "20260903-0900-c_romashka"
    assert run.company_id == "c_romashka"
    assert run.warmed is True


def test_two_runs_of_one_company_do_not_share_history():
    """Ради этого прогон и сделан файлом: thread_id — это номер телефона и
    первичный ключ threads, второй прогон по тому же лиду упёрся бы в него."""
    first = runs.create("c_romashka", warmed=True, moment=MOMENT)
    second = runs.create("c_romashka", warmed=True,
                         moment=MOMENT + timedelta(minutes=1))
    assert first.path != second.path
    _open_thread(first, "+77010000001")
    assert _threads(second) == []


def test_activate_switches_every_seam():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    assert paths.state_db() == run.path
    assert runs.active().run_id == run.run_id


def test_deactivate_returns_the_production_database():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.deactivate()
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert runs.active() is None
    assert clock.offset() == timedelta()


def test_shift_moves_the_clock_and_survives_reactivation():
    """Смещение принадлежит прогону, а не процессу: иначе прогон, начатый
    вчера с «+3 дня», после перезапуска бэкенда откатился бы назад."""
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.shift(3 * 24 * 3600)
    assert clock.offset() == timedelta(days=3)

    runs.deactivate()
    runs.activate(run.run_id)
    assert clock.offset() == timedelta(days=3)
    assert runs.get(run.run_id).offset == timedelta(days=3)


def test_shift_accumulates():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.shift(3600)
    runs.shift(3600)
    assert clock.offset() == timedelta(hours=2)


def test_shift_without_an_active_run_is_an_error():
    with pytest.raises(runs.UnknownRunError):
        runs.shift(3600)


def test_all_lists_newest_first():
    old = runs.create("c_one", warmed=True, moment=MOMENT)
    new = runs.create("c_two", warmed=True, moment=MOMENT + timedelta(minutes=1))
    assert [run.run_id for run in runs.all()] == [new.run_id, old.run_id]


def test_get_of_a_missing_run_is_an_error():
    with pytest.raises(runs.UnknownRunError):
        runs.get("20260903-0900-c_nobody")


def _number_status(run):
    with sqlite3.connect(run.path) as db:
        return db.execute("SELECT status FROM numbers").fetchone()[0]


def _open_thread(run, thread_id):
    with sqlite3.connect(run.path) as db:
        db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
                   " VALUES (?, 'c_romashka', '{}', '2026-09-03T09:00:00+00:00')",
                   (thread_id,))


def _threads(run):
    with sqlite3.connect(run.path) as db:
        return [row[0] for row in db.execute("SELECT thread_id FROM threads")]
```

- [ ] **Step 2: Добавить каталог тестов в pytest**

В `backend/pyproject.toml` строка `testpaths`:

```toml
testpaths = ["collector/tests", "writer/tests", "sender/tests", "sandbox/tests"]
```

- [ ] **Step 3: Убедиться, что тест падает**

Run: `cd backend && uv run pytest sandbox/tests/test_runs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sandbox'`

- [ ] **Step 4: Написать `backend/sandbox/runs.py`**

Создать `backend/sandbox/__init__.py` с одной строкой докстринга:

```python
"""Песочница аутрича: подменный транспорт, роль лида, машина времени.

Знает про все три системы; про неё не знает никто — это сторожит
sandbox/tests/test_import_graph.py.
"""
```

Создать `backend/sandbox/runs.py`:

```python
"""Прогон песочницы — отдельный файл базы невосстановимого слоя.

Почему файл, а не флаг на треде: thread_id — это номер телефона и первичный
ключ threads, поэтому второй прогон по тому же лиду упёрся бы в занятый ключ, а
«чистый контекст» пришлось бы изображать фильтром в каждом запросе агента — и
воронка с инбоксом учились бы про тестовые треды забывать. Файл делает чистоту
контекста физической: прошлого сценария нет, потому что его нет в базе.

Схему наполняют её настоящие владельцы (thread_store, migrate, schema.sql), а
не копия DDL: база прогона обязана отличаться от боевой только содержимым.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import activity
import analytics
import clock
import paths
from collector.services import store
from sender.db import migrate, numbers
from writer.db import thread_store

RUNS_DIR = Path(__file__).resolve().parent.parent / "collector" / "data" / "sandbox"

# Наш номер в песочнице один: пул проверяется прогревом и монитором, а не
# количеством SIM, и второй номер добавил бы только выбор в гейте.
SANDBOX_NUMBER = "+77000000001"

META = "CREATE TABLE IF NOT EXISTS sandbox_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class UnknownRunError(Exception):
    """Прогона с таким id нет — или ни один не активен."""


@dataclass(frozen=True)
class Run:
    run_id: str
    company_id: str
    created_at: str
    warmed: bool
    offset: timedelta

    @property
    def path(self) -> Path:
        return RUNS_DIR / f"{self.run_id}.db"


_active: str | None = None


def create(company_id: str, warmed: bool, moment: datetime) -> Run:
    """Новый прогон: файл, три миграции, мета, один номер в пуле.

    `warmed` — единственный параметр создания, и он не косметический: без
    прогретого номера первый же прогон упёрся бы в календарь прогрева и не
    отправил бы ничего, а без нового номера нельзя проверить сам прогрев.
    """
    run = Run(run_id=f"{moment:%Y%m%d-%H%M}-{company_id}", company_id=company_id,
              created_at=moment.isoformat(timespec="seconds"), warmed=warmed,
              offset=timedelta())
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _apply_schemas(run.path)
    _write_meta(run.path, {"company_id": company_id, "created_at": run.created_at,
                           "warmed": str(int(warmed)), "offset_seconds": "0"})
    with closing(sqlite3.connect(run.path)) as db:
        numbers.register(db, SANDBOX_NUMBER, session_dir="sandbox", now=moment,
                         skip_warmup=warmed)
    return run


def all() -> list[Run]:
    """Новые сверху: список прогонов читается как список чатов."""
    if not RUNS_DIR.exists():
        return []
    # Файл без меты — прогон, чьё создание оборвалось между схемой и метой.
    # Пропускается, а не роняет список: иначе одна такая крошка навсегда
    # убила бы страницу, и починить её было бы нечем, кроме shell.
    found = [run for run in (_read_or_none(path) for path in RUNS_DIR.glob("*.db"))
             if run is not None]
    return sorted(found, key=lambda run: run.run_id, reverse=True)


def get(run_id: str) -> Run:
    path = RUNS_DIR / f"{run_id}.db"
    if not path.exists():
        raise UnknownRunError(run_id)
    return _read(path)


def active() -> Run | None:
    return get(_active) if _active is not None else None


def activate(run_id: str) -> Run:
    """Переключить прогон целиком: база, журнал, аналитика, часы.

    Соединения везде короткие, поэтому живой процесс переключается без
    перезапуска — тик воркера на следующем обороте откроет уже новую базу.
    """
    global _active
    run = get(run_id)
    paths.use_run(run.path)
    activity.use(run.path)
    analytics.use(run.path, store.DERIVED)
    clock.use(run.offset)
    _active = run_id
    return run


def deactivate() -> None:
    """Вернуть боевые базу и часы. Зовётся при выключении песочницы и из
    тестов: забытое смещение сдвинуло бы время всему процессу."""
    global _active
    _active = None
    paths.use_run(None)
    activity.use(paths.state_db())
    analytics.use(paths.state_db(), store.DERIVED)
    clock.use(None)


def shift(seconds: int) -> Run:
    """Двинуть часы активного прогона. Смещение накапливается и ложится в
    мету: оно принадлежит прогону, а не процессу."""
    run = active()
    if run is None:
        raise UnknownRunError("активного прогона нет")
    moved = run.offset + timedelta(seconds=seconds)
    _write_meta(run.path, {"offset_seconds": str(int(moved.total_seconds()))})
    clock.use(moved)
    return get(run.run_id)


def _apply_schemas(path: Path) -> None:
    """Три владельца по очереди. Порядок не случаен: threads и messages
    создаёт система 2, и колонки состояния системы 3 доливаются в уже
    существующие таблицы — как и при боевом старте процесса."""
    thread_store.connect(path).close()
    with closing(sqlite3.connect(path)) as db:
        db.executescript(store.state_schema())
        db.execute(META)
        db.commit()
    migrate.connect(path).close()


def _write_meta(path: Path, values: dict[str, str]) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO sandbox_meta (key, value) VALUES (?, ?)"
                       " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                       list(values.items()))
        db.commit()


def _read(path: Path) -> Run:
    with closing(sqlite3.connect(path)) as db:
        meta = dict(db.execute("SELECT key, value FROM sandbox_meta").fetchall())
    return Run(run_id=path.stem, company_id=meta["company_id"],
               created_at=meta["created_at"], warmed=meta["warmed"] == "1",
               offset=timedelta(seconds=int(meta["offset_seconds"])))


def _read_or_none(path: Path) -> Run | None:
    try:
        return _read(path)
    except (sqlite3.DatabaseError, KeyError):
        return None
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest sandbox/tests/test_runs.py -v`
Expected: PASS, 11 passed

- [ ] **Step 6: Прогнать всю сюиту**

Run: `cd backend && uv run pytest -q`
Expected: PASS — `store.state_schema()` новый, старые тесты его не трогают.

- [ ] **Step 7: Коммит**

```bash
git add backend/sandbox backend/pyproject.toml backend/collector/services/store.py
git commit -m "$(cat <<'EOF'
feat(sandbox): прогон — отдельный файл базы, часы принадлежат прогону

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Подменный Node — контракт ручка в ручку

**Files:**
- Create: `backend/sandbox/node.py`, `backend/sandbox/faults.py`, `backend/sandbox/tests/test_node.py`
- Modify: ничего

**Interfaces:**
- Consumes: `runs.active`, `runs.SANDBOX_NUMBER`, `config.settings`.
- Produces:
  - `faults.Faults` — frozen dataclass: `send: str` (`"ok"` | `"not_sent"` | `"unknown"`), `delivery: str` (`"delivered"` | `"read"` | `"silent"`), `number: str` (`"connected"` | `"loggedOut"` | `"stalled"`), `has_whatsapp: bool`
  - `faults.current() -> Faults`, `faults.update(**values: object) -> Faults`, `faults.reset() -> None`
  - `node.router: APIRouter` с префиксом `/api/sandbox/node`
  - `node.deliver(payload: dict) -> bool` — шов отправки события в свой вебхук
  - `node.forget_keys() -> None` — сброс журнала идемпотентности
  - `node.settle() -> None` — дождаться фоновых доставок статуса
  - `node.STATUS_PAUSE_SECONDS: int`, `node.DELIVERED: int`, `node.READ: int`

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sandbox/tests/test_node.py`:

```python
"""Подменный Node: тот же контракт, что у sender/node/index.js.

Транспорт системы 3 не переписывается ни строкой, поэтому проверяется ровно
то, на что он опирается: sent/провайдерский id, идемпотентность по ключу и
разница между «не ушло» и «неизвестно».
"""

import pytest
from fastapi import HTTPException

from sandbox import faults, node


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    node.forget_keys()
    faults.reset()
    sent = []
    monkeypatch.setattr(node, "deliver", _record(sent))
    # Статус уезжает фоновой задачей с паузой: в тесте её ждёт node.settle(),
    # а пауза обнуляется, чтобы сюита не спала секунду на каждой отправке.
    monkeypatch.setattr(node, "STATUS_PAUSE_SECONDS", 0)
    yield sent
    faults.reset()


def _record(sent):
    async def deliver(payload: dict) -> bool:
        sent.append(payload)
        return True
    return deliver


async def test_send_returns_a_provider_id():
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is True
    assert body["provider_id"]


async def test_repeat_by_key_does_not_send_twice(clean):
    first = await node.send(node.SendRequest(number="+77000000001",
                                             to="+77010000001", text="привет",
                                             key="outbox-1"))
    again = await node.send(node.SendRequest(number="+77000000001",
                                             to="+77010000001", text="привет",
                                             key="outbox-1"))
    await node.settle()
    assert again["provider_id"] == first["provider_id"]
    assert len(clean) == 1, "дубликат в холодном аутриче — прямой повод нажать Report"


async def test_not_sent_is_an_honest_no(clean):
    faults.update(send="not_sent")
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is False
    assert body["provider_id"] is None
    await node.settle()
    assert clean == [], "несостоявшаяся отправка не может иметь статуса доставки"


async def test_unknown_is_a_five_hundred():
    """`sent:false` — обещание «фрейм в сокет не ушёл», на нём строится
    безопасный ретрай. Неизвестность обязана приезжать как TransportError."""
    faults.update(send="unknown")
    with pytest.raises(HTTPException) as failure:
        await node.send(node.SendRequest(number="+77000000001",
                                         to="+77010000001", text="привет",
                                         key="outbox-1"))
    assert failure.value.status_code == 500


async def test_delivery_status_goes_to_our_own_webhook(clean):
    await node.send(node.SendRequest(number="+77000000001", to="+77010000001",
                                     text="привет", key="outbox-1"))
    await node.settle()
    assert clean[0]["kind"] == "status"
    assert clean[0]["status"] == node.DELIVERED
    assert clean[0]["provider_id"]


async def test_read_status_when_asked(clean):
    faults.update(delivery="read")
    await node.send(node.SendRequest(number="+77000000001", to="+77010000001",
                                     text="привет", key="outbox-1"))
    await node.settle()
    assert clean[0]["status"] == node.READ


async def test_silence_leaves_the_thread_in_queued(clean):
    faults.update(delivery="silent")
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is True
    await node.settle()
    assert clean == []


async def test_check_follows_the_switch():
    assert await node.check(node.CheckRequest(number="+77000000001",
                                              to="+77010000001")) == {"has_whatsapp": True}
    faults.update(has_whatsapp=False)
    assert await node.check(node.CheckRequest(number="+77000000001",
                                              to="+77010000001")) == {"has_whatsapp": False}


async def test_health_reports_the_switched_state():
    faults.update(number="loggedOut")
    report = await node.health()
    assert report[node.SANDBOX_NUMBER]["state"] == "loggedOut"


async def test_pair_and_qr_answer_without_a_phone():
    """Страница пула обязана работать: она зовёт обе ручки на живом номере."""
    assert (await node.pair(node.NumberRequest(number="+77000000001")))["code"]
    assert (await node.qr(node.NumberRequest(number="+77000000001")))["qr"].startswith("data:image")
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest sandbox/tests/test_node.py -v`
Expected: FAIL — `ImportError: cannot import name 'faults' from 'sandbox'`

- [ ] **Step 3: Написать `backend/sandbox/faults.py`**

```python
"""Тумблеры аварий. Настройка стенда, а не факт истории.

Поэтому живут в памяти процесса и не переживают перезапуск — в отличие от
часов, которые принадлежат прогону и лежат в его базе. Авария, пережившая
перезапуск, объяснялась бы потом полдня.
"""

from dataclasses import dataclass, replace

SEND = ("ok", "not_sent", "unknown")
DELIVERY = ("delivered", "read", "silent")
NUMBER = ("connected", "loggedOut", "stalled")


@dataclass(frozen=True)
class Faults:
    send: str = "ok"
    delivery: str = "delivered"
    number: str = "connected"
    has_whatsapp: bool = True


_current = Faults()

_ALLOWED = {"send": SEND, "delivery": DELIVERY, "number": NUMBER}


def current() -> Faults:
    return _current


def update(**values: object) -> Faults:
    """Частичное обновление: пульт шлёт только то, что переключили."""
    global _current
    for field, allowed in _ALLOWED.items():
        given = values.get(field)
        if given is not None and given not in allowed:
            raise ValueError(f"{field}: {given!r}, бывают {allowed}")
    _current = replace(_current, **{key: value for key, value in values.items()
                                    if value is not None})
    return _current


def reset() -> None:
    global _current
    _current = Faults()
```

- [ ] **Step 4: Добавить `sandbox_self_url` в настройки**

В `backend/config.py`, в класс `Settings`, после `sender_node_url`:

```python
    # Куда песочница шлёт события самой себе. Отдельной переменной, а не
    # склейкой из uvicorn_host: адрес вебхука должен переживать смену хоста
    # бэкенда (докер слушает 0.0.0.0, а ходить туда по 0.0.0.0 нельзя).
    sandbox_self_url: str = "http://127.0.0.1:8787"
```

- [ ] **Step 5: Написать `backend/sandbox/node.py`**

```python
"""Подменный Node: те же пять ручек, что у sender/node/index.js, и столько же
знания о продукте — нисколько.

sender/transport.py не переписывается ни строкой: он ходит по
settings.sender_node_url, и в песочнице этот адрес указывает сюда. Значит
проверяются настоящие гейты, настоящий ретрай и настоящая идемпотентность, а
не их изображение.
"""

import asyncio
import logging
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from sandbox import faults
from sandbox.runs import SANDBOX_NUMBER

router = APIRouter(prefix="/api/sandbox/node")
log = logging.getLogger(__name__)

# Те же константы протокола, что читает вебхук: 3 — доставлено, 4 — прочитано.
DELIVERED = 3
READ = 4

# Статус уезжает фоном и повторяется, пока вебхук не подтвердит, что нашёл
# строку очереди. Настоящий Node отделён от Python сетью, и его статус всегда
# приезжает позже, чем воркер успевает записать provider_id в outbox. Здесь
# сети нет: статус, отправленный прямо из /send, встретил бы строку ещё без
# provider_id — и доставка потерялась бы молча.
STATUS_ATTEMPTS = 5
STATUS_PAUSE_SECONDS = 1

# 1x1 прозрачный png: QR настоящему WhatsApp здесь не нужен, а страница пула
# рисует картинку и обязана получить картинку.
FAKE_QR = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
           "lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


class SendRequest(BaseModel):
    number: str
    to: str
    text: str
    key: str | None = None
    type: str = "text"


class CheckRequest(BaseModel):
    number: str
    to: str


class NumberRequest(BaseModel):
    number: str


# key идемпотентности -> provider_id уже отправленного, как в настоящем Node.
_sent_keys: dict[str, str] = {}

# Фоновые доставки статусов. Держатся в множестве по той же причине, по которой
# их держит любой, кто зовёт create_task: задача без ссылки может быть собрана
# сборщиком мусора на середине.
_pending: set[asyncio.Task] = set()


async def settle() -> None:
    """Дождаться уехавших статусов. Нужно тестам — в бою их ждёт время."""
    while _pending:
        await asyncio.gather(*list(_pending))


def forget_keys() -> None:
    """Журнал идемпотентности живёт, пока живёт процесс — как в Node. Сброс
    нужен тестам и смене прогона: ключ outbox-1 бывает в каждом."""
    _sent_keys.clear()


@router.post("/send")
async def send(request: SendRequest) -> dict:
    if request.key and request.key in _sent_keys:
        log.info("повтор по ключу %s, второй отправки нет", request.key)
        return {"ok": True, "sent": True, "provider_id": _sent_keys[request.key]}
    switch = faults.current()
    if switch.send == "unknown":
        raise HTTPException(500, "транспорт не ответил: судьба отправки неизвестна")
    if switch.send == "not_sent":
        return {"ok": False, "sent": False, "provider_id": None,
                "error": "sandbox: фрейм в сокет не ушёл"}
    provider_id = f"SBX{uuid4().hex[:12].upper()}"
    if request.key:
        _sent_keys[request.key] = provider_id
    if switch.delivery != "silent":
        status = READ if switch.delivery == "read" else DELIVERED
        task = asyncio.create_task(_status_later(request.number, provider_id, status))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return {"ok": True, "sent": True, "provider_id": provider_id}


@router.post("/check")
async def check(request: CheckRequest) -> dict:
    return {"has_whatsapp": faults.current().has_whatsapp}


@router.post("/pair")
async def pair(request: NumberRequest) -> dict:
    return {"code": "SBX-0000"}


@router.post("/qr")
async def qr(request: NumberRequest) -> dict:
    return {"qr": FAKE_QR}


@router.get("/health")
async def health() -> dict:
    return {SANDBOX_NUMBER: {"state": faults.current().number, "reconnects": 0}}


async def deliver(payload: dict) -> bool:
    """Событие в свой же вебхук — тем же HTTP и с тем же заголовком, что у
    настоящего Node. Короткого пути (прямого вызова функции) здесь нет
    намеренно: он проверял бы не ту систему."""
    headers = ({"x-sender-secret": settings.sender_webhook_secret}
               if settings.sender_webhook_secret else {})
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{settings.sandbox_self_url}/api/sender/webhook",
                                     json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return bool(response.json().get("handled"))


async def _status_later(number: str, provider_id: str, status: int) -> None:
    for attempt in range(STATUS_ATTEMPTS):
        await asyncio.sleep(STATUS_PAUSE_SECONDS)
        try:
            if await deliver({"kind": "status", "number": number,
                              "provider_id": provider_id, "status": status}):
                return
        except httpx.HTTPError as error:
            log.warning("статус %s не доставлен (попытка %s): %s",
                        provider_id, attempt + 1, error)
    log.error("статус %s так и не нашёл строку очереди", provider_id)
```

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest sandbox/tests/test_node.py -v`
Expected: PASS, 10 passed

- [ ] **Step 7: Коммит**

```bash
git add backend/sandbox backend/config.py
git commit -m "$(cat <<'EOF'
feat(sandbox): подменный Node и четыре тумблера аварий

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Пульт — прогоны, часы, тумблеры, входящее от лида

**Files:**
- Create: `backend/sandbox/routes.py`, `backend/sandbox/tests/test_routes.py`
- Modify: ничего

**Interfaces:**
- Consumes: `runs.*`, `faults.*`, `node.deliver`, `clock.now`, `paths.state_db`.
- Produces: `routes.router: APIRouter` с префиксом `/api/sandbox` и ручками
  `GET /runs`, `POST /runs`, `POST /runs/{run_id}/activate`, `POST /clock`,
  `GET /faults`, `POST /faults`, `POST /incoming`;
  `routes.PRESETS: dict[str, int]`; `routes.thread_of_active_run() -> str`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sandbox/tests/test_routes.py`:

```python
"""Пульт: прогоны, часы, тумблеры и входящее от лида.

Входящее уезжает в ту же ручку /api/sender/webhook, которую дёргает настоящий
Node: стоп-слова, дедуп и гашение расписания обязаны быть боевыми.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import clock
from sandbox import faults, node, routes, runs

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    sent = []

    async def deliver(payload: dict) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(node, "deliver", deliver)
    yield sent
    runs.deactivate()
    faults.reset()


def _run_with_thread(thread_id="+77010000001"):
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    with sqlite3.connect(run.path) as db:
        db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
                   " VALUES (?, 'c_romashka', '{}', '2026-09-03T09:00:00+00:00')",
                   (thread_id,))
    return run


async def test_create_run_returns_it_active():
    body = await routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    assert body["run"]["company_id"] == "c_romashka"
    assert runs.active().run_id == body["run"]["run_id"]


async def test_list_runs_marks_the_active_one():
    first = await routes.create_run(routes.NewRun(company_id="c_one", warmed=True))
    await routes.create_run(routes.NewRun(company_id="c_two", warmed=True))
    await routes.activate_run(first["run"]["run_id"])
    listed = (await routes.list_runs())["runs"]
    assert [run["active"] for run in listed].count(True) == 1
    assert next(run for run in listed if run["active"])["company_id"] == "c_one"


async def test_clock_preset_moves_the_clock():
    await routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    body = await routes.move_clock(routes.ClockMove(preset="day"))
    assert clock.offset() == timedelta(days=1)
    assert body["offset_seconds"] == 86400


async def test_clock_accepts_raw_seconds():
    await routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    await routes.move_clock(routes.ClockMove(seconds=900))
    assert clock.offset() == timedelta(minutes=15)


async def test_clock_to_the_window_lands_inside_it():
    """«К открытию окна» обязано попадать внутрь окна отправки, иначе кнопка
    врёт: гейт снова скажет «не время»."""
    await routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    await routes.move_clock(routes.ClockMove(preset="window"))
    almaty = clock.now() + timedelta(hours=5)      # Asia/Almaty = UTC+5
    assert 10 <= almaty.hour < 18
    assert almaty.isoweekday() <= 5


async def test_faults_round_trip():
    await routes.set_faults(routes.NewFaults(send="not_sent"))
    assert (await routes.get_faults())["send"] == "not_sent"
    assert (await routes.get_faults())["delivery"] == "delivered", "частичное обновление"


async def test_unknown_fault_value_is_a_422():
    with pytest.raises(HTTPException) as failure:
        await routes.set_faults(routes.NewFaults(send="куда-то"))
    assert failure.value.status_code == 422


async def test_incoming_goes_through_the_real_webhook(sandbox):
    _run_with_thread()
    await routes.incoming(routes.NewIncoming(text="сколько стоит?"))
    assert sandbox[-1]["kind"] == "incoming"
    assert sandbox[-1]["from"] == "77010000001@s.whatsapp.net"
    assert sandbox[-1]["text"] == "сколько стоит?"
    assert sandbox[-1]["provider_id"]


async def test_incoming_without_a_thread_is_a_409():
    runs.activate(runs.create("c_romashka", warmed=True, moment=MOMENT).run_id)
    with pytest.raises(HTTPException) as failure:
        await routes.incoming(routes.NewIncoming(text="привет"))
    assert failure.value.status_code == 409


async def test_everything_needs_an_active_run():
    for call in (routes.move_clock(routes.ClockMove(preset="day")),
                 routes.incoming(routes.NewIncoming(text="привет"))):
        with pytest.raises(HTTPException) as failure:
            await call
        assert failure.value.status_code == 409
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest sandbox/tests/test_routes.py -v`
Expected: FAIL — `ImportError: cannot import name 'routes' from 'sandbox'`

- [ ] **Step 3: Написать `backend/sandbox/routes.py`**

```python
"""Пульт песочницы. Продуктовых решений здесь нет: только прогоны, часы,
тумблеры и «ответить как лид».

Черновик и постановку в очередь пульт не дублирует — их зовут существующие
ручки writer'а и sender'а. Вторая точка входа в постановку сообщения означала
бы вторые гейты, а проверять надо те же, что в бою.
"""

import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import clock
import paths
from sandbox import faults, node, runs

router = APIRouter(prefix="/api/sandbox")
log = logging.getLogger(__name__)

# Часовой пояс окна отправки живёт в sender/config.toml; здесь он нужен только
# для пресета «к открытию окна», и берётся оттуда же — см. _to_window.
PRESETS = {"jitter": 15 * 60, "hour": 3600, "day": 86400}

PERSONAL_JID = "@s.whatsapp.net"


class NewRun(BaseModel):
    company_id: str
    warmed: bool = True


class ClockMove(BaseModel):
    seconds: int | None = None
    preset: str | None = None


class NewFaults(BaseModel):
    send: str | None = None
    delivery: str | None = None
    number: str | None = None
    has_whatsapp: bool | None = None


class NewIncoming(BaseModel):
    text: str = ""


@router.get("/runs")
async def list_runs() -> dict:
    active = runs.active()
    return {"runs": [_card(run, active) for run in runs.all()]}


@router.post("/runs", status_code=201)
async def create_run(body: NewRun) -> dict:
    # Системное время, а не clock.now(): id прогона и дата регистрации номера
    # не должны наследовать сдвиг прошлого прогона — иначе новый прогон
    # рождается в будущем и его календарь прогрева врёт с первого дня.
    run = runs.create(body.company_id, body.warmed, datetime.now(timezone.utc))
    runs.activate(run.run_id)
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/runs/{run_id}/activate")
async def activate_run(run_id: str) -> dict:
    try:
        run = runs.activate(run_id)
    except runs.UnknownRunError:
        raise HTTPException(404, f"прогона {run_id} нет") from None
    node.forget_keys()
    return {"run": _card(run, run)}


@router.post("/clock")
async def move_clock(body: ClockMove) -> dict:
    seconds = _seconds(body)
    try:
        run = runs.shift(seconds)
    except runs.UnknownRunError:
        raise HTTPException(409, "активного прогона нет: сначала создайте его") from None
    return {"offset_seconds": int(run.offset.total_seconds()),
            "now": clock.now().isoformat(timespec="seconds")}


@router.get("/faults")
async def get_faults() -> dict:
    return faults.current().__dict__


@router.post("/faults")
async def set_faults(body: NewFaults) -> dict:
    try:
        return faults.update(**body.model_dump()).__dict__
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/incoming")
async def incoming(body: NewIncoming) -> dict:
    """Ответ лида уезжает в ту же ручку, что дёргает настоящий Node на
    messages.upsert: стоп-слова, дедуп и гашение расписания — боевые."""
    thread_id = thread_of_active_run()
    payload = {"kind": "incoming", "number": runs.SANDBOX_NUMBER,
               "from": f"{thread_id.lstrip('+')}{PERSONAL_JID}",
               "provider_id": f"SBXIN{uuid4().hex[:10].upper()}",
               "text": body.text}
    return {"handled": await node.deliver(payload)}


def thread_of_active_run() -> str:
    """Тред прогона — он один: прогон заводится на одного лида."""
    if runs.active() is None:
        raise HTTPException(409, "активного прогона нет: сначала создайте его")
    with closing(sqlite3.connect(paths.state_db())) as db:
        row = db.execute("SELECT thread_id FROM threads"
                         " ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(409, "треда ещё нет: сначала черновик первого письма")
    return row[0]


def _seconds(body: ClockMove) -> int:
    if body.seconds is not None:
        return body.seconds
    if body.preset == "window":
        return _to_window()
    if body.preset in PRESETS:
        return PRESETS[body.preset]
    raise HTTPException(422, f"непонятный сдвиг: {body.preset or body.seconds}")


def _to_window() -> int:
    """Секунды до 10:00 ближайшего рабочего дня по времени окна отправки.

    Пресет обязан попадать ВНУТРЬ окна, а не на его границу: гейт сравнивает
    час строго, и «ровно 10:00» после джиттера снова оказалось бы «не время».
    """
    from sender.services import config as sender_config

    settings = sender_config.load()["window"]
    zone = timezone(timedelta(hours=5)) if settings["timezone"] == "Asia/Almaty" \
        else timezone.utc
    local = clock.now().astimezone(zone)
    opens, _ = settings["hours"]
    target = local.replace(hour=opens, minute=30, second=0, microsecond=0)
    while target <= local or target.isoweekday() not in settings["weekdays"]:
        target += timedelta(days=1)
        target = target.replace(hour=opens, minute=30, second=0, microsecond=0)
    return int((target - local).total_seconds())


def _card(run: runs.Run, active: runs.Run | None) -> dict:
    return {"run_id": run.run_id, "company_id": run.company_id,
            "created_at": run.created_at, "warmed": run.warmed,
            "offset_seconds": int(run.offset.total_seconds()),
            "active": active is not None and active.run_id == run.run_id}
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest sandbox/tests/test_routes.py -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Коммит**

```bash
git add backend/sandbox
git commit -m "$(cat <<'EOF'
feat(sandbox): пульт — прогоны, часы, тумблеры, ответ лида

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Лента чата — судьба каждого сообщения

**Files:**
- Create: `backend/sandbox/chat.py`, `backend/sandbox/tests/test_chat.py`
- Modify: `backend/sandbox/routes.py` (добавить `GET /chat`)

**Interfaces:**
- Consumes: `paths.state_db`, `runs.active`, `routes.thread_of_active_run`.
- Produces: `chat.view(db: sqlite3.Connection, thread_id: str) -> dict` со схемой
  `{"thread_id", "company_id", "stage", "status", "touch_no", "next_touch_at", "bubbles": [...]}`,
  где пузырь — `{"message_id", "role", "kind", "text", "at", "fate"}`,
  `kind ∈ {"draft", "queued", "sent", "incoming"}`,
  `fate` — `None` или `{"status", "attempts", "send_after", "delivered_at", "read_at", "error"}`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/sandbox/tests/test_chat.py`:

```python
"""Лента песочницы: под каждым нашим пузырём — его настоящая судьба.

Разница draft/queued/sent — единственная бесплатная разметка для калибровки
промпта, и в песочнице она обязана быть видна первой.
"""

import sqlite3

import pytest

from sandbox import chat
from sender.db import migrate
from writer.db import thread_store

THREAD = "+77010000001"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "run.db"
    thread_store.connect(path).close()
    connection = migrate.connect(path)
    connection.execute(
        "INSERT INTO threads (thread_id, company_id, seed, created_at, stage,"
        " status, touch_no) VALUES (?, 'c_romashka', '{}',"
        " '2026-09-03T09:00:00+00:00', 'probing', 'active', 2)", (THREAD,))
    connection.commit()
    yield connection
    connection.close()


def _message(db, **values):
    columns = {"thread_id": THREAD, "role": "outgoing", "draft_text": None,
               "queued_text": None, "sent_text": None,
               "created_at": "2026-09-03T09:05:00+00:00", **values}
    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    cursor = db.execute(f"INSERT INTO messages ({names}) VALUES ({marks})",
                        list(columns.values()))
    db.commit()
    return cursor.lastrowid


def test_thread_state_travels_with_the_feed(db):
    view = chat.view(db, THREAD)
    assert view["stage"] == "probing"
    assert view["status"] == "active"
    assert view["touch_no"] == 2


def test_draft_queued_and_sent_are_three_different_bubbles(db):
    _message(db, draft_text="черновик")
    _message(db, draft_text="черновик", queued_text="подтверждено")
    _message(db, draft_text="черновик", queued_text="подтверждено",
             sent_text="ушло", sent_at="2026-09-03T10:00:00+00:00")
    assert [bubble["kind"] for bubble in chat.view(db, THREAD)["bubbles"]] == \
        ["draft", "queued", "sent"]


def test_bubble_shows_the_text_that_matters_at_its_stage(db):
    _message(db, draft_text="черновик", queued_text="правка оператора")
    assert chat.view(db, THREAD)["bubbles"][0]["text"] == "правка оператора"


def test_incoming_is_the_lead_speaking(db):
    _message(db, role="incoming", sent_text="сколько стоит?")
    bubble = chat.view(db, THREAD)["bubbles"][0]
    assert bubble["kind"] == "incoming"
    assert bubble["text"] == "сколько стоит?"
    assert bubble["fate"] is None


def test_fate_comes_from_the_queue(db):
    message_id = _message(db, draft_text="черновик", queued_text="подтверждено")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, attempts, error, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00',"
        " 'failed', 3, 'sandbox: фрейм в сокет не ушёл',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T09:30:00+00:00')",
        (message_id, THREAD))
    db.commit()
    fate = chat.view(db, THREAD)["bubbles"][0]["fate"]
    assert fate["status"] == "failed"
    assert fate["attempts"] == 3
    assert fate["error"]


def test_delivery_marks_reach_the_bubble(db):
    message_id = _message(db, draft_text="ч", queued_text="q", sent_text="ушло")
    db.execute(
        "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
        " status, attempts, delivered_at, read_at, created_at, updated_at)"
        " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', 'sent', 1,"
        " '2026-09-03T10:00:05+00:00', '2026-09-03T10:02:00+00:00',"
        " '2026-09-03T09:06:00+00:00', '2026-09-03T10:02:00+00:00')",
        (message_id, THREAD))
    db.commit()
    fate = chat.view(db, THREAD)["bubbles"][0]["fate"]
    assert fate["delivered_at"] and fate["read_at"]


def test_the_last_live_row_wins_when_a_message_was_requeued(db):
    """Кончившаяся в failed строка не запрещает поставить сообщение заново, и
    в ленте обязана быть видна свежая попытка, а не похороненная."""
    message_id = _message(db, draft_text="ч", queued_text="q")
    for status, updated in (("failed", "2026-09-03T09:30:00+00:00"),
                            ("pending", "2026-09-03T10:00:00+00:00")):
        db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, attempts, created_at, updated_at)"
            " VALUES (?, ?, '+77000000001', '2026-09-03T10:00:00+00:00', ?, 1,"
            " '2026-09-03T09:06:00+00:00', ?)",
            (message_id, THREAD, status, updated))
    db.commit()
    assert chat.view(db, THREAD)["bubbles"][0]["fate"]["status"] == "pending"


def test_bubbles_are_in_the_order_they_happened(db):
    _message(db, draft_text="первое", sent_text="первое")
    _message(db, role="incoming", sent_text="ответ")
    _message(db, draft_text="второе")
    assert [bubble["role"] for bubble in chat.view(db, THREAD)["bubbles"]] == \
        ["outgoing", "incoming", "outgoing"]
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `cd backend && uv run pytest sandbox/tests/test_chat.py -v`
Expected: FAIL — `ImportError: cannot import name 'chat' from 'sandbox'`

- [ ] **Step 3: Написать `backend/sandbox/chat.py`**

```python
"""Лента прогона: сообщение плюс его судьба в очереди.

Собирается здесь, а не в writer/routes/threads.py: инбоксу оператора судьба
строки очереди не нужна, а песочнице она и есть содержание — «не ушло, попытка
2 из 3» отличается от «доставлено» ровно тем, ради чего стенд и построен.
"""

import sqlite3

FIELDS = ("message_id, role, draft_text, queued_text, sent_text, created_at,"
          " sent_at")

FATE = ("status, attempts, send_after, delivered_at, read_at, error")


def view(db: sqlite3.Connection, thread_id: str) -> dict:
    db.row_factory = sqlite3.Row
    thread = db.execute(
        "SELECT thread_id, company_id, stage, status, touch_no, next_touch_at"
        " FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    rows = db.execute(f"SELECT {FIELDS} FROM messages WHERE thread_id = ?"
                      " ORDER BY message_id", (thread_id,)).fetchall()
    return {**dict(thread),
            "bubbles": [_bubble(db, row) for row in rows]}


def _bubble(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    kind, text = _kind_and_text(row)
    return {"message_id": row["message_id"], "role": row["role"], "kind": kind,
            "text": text, "at": row["sent_at"] or row["created_at"],
            "fate": _fate(db, row["message_id"]) if row["role"] == "outgoing" else None}


def _kind_and_text(row: sqlite3.Row) -> tuple[str, str]:
    """Три состояния нашего сообщения — три разных факта: что предложила
    модель, что подтвердил оператор и что реально ушло. Показывается самое
    позднее из случившихся: правка оператора важнее черновика, который он
    правил."""
    if row["role"] == "incoming":
        return "incoming", row["sent_text"] or ""
    if row["sent_text"]:
        return "sent", row["sent_text"]
    if row["queued_text"]:
        return "queued", row["queued_text"]
    return "draft", row["draft_text"] or ""


def _fate(db: sqlite3.Connection, message_id: int) -> dict | None:
    """Свежая строка очереди, а не первая: кончившаяся в failed не запрещает
    поставить сообщение заново, и в ленте нужна текущая попытка."""
    row = db.execute(f"SELECT {FATE} FROM outbox WHERE message_id = ?"
                     " ORDER BY updated_at DESC, outbox_id DESC LIMIT 1",
                     (message_id,)).fetchone()
    return dict(row) if row is not None else None
```

- [ ] **Step 4: Подключить ленту к пульту**

В `backend/sandbox/routes.py` добавить импорт `from sandbox import chat` и ручку рядом с остальными:

```python
@router.get("/chat")
async def read_chat() -> dict:
    thread_id = thread_of_active_run()
    with closing(sqlite3.connect(paths.state_db())) as db:
        return chat.view(db, thread_id)
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest sandbox/tests -v`
Expected: PASS — 8 passed в `test_chat.py`, остальные файлы песочницы зелёные.

- [ ] **Step 6: Коммит**

```bash
git add backend/sandbox
git commit -m "$(cat <<'EOF'
feat(sandbox): лента чата — судьба каждого сообщения рядом с текстом

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Монтирование под флагом, предохранитель, граница импортов

**Files:**
- Create: `backend/sandbox/tests/test_import_graph.py`, `backend/sandbox/tests/test_guard.py`
- Modify: `backend/config.py` (флаг `sandbox`), `backend/collector/api.py` (монтирование и предохранитель), `backend/sandbox/__init__.py` (функция `mount`), `backend/.env.example`

**Interfaces:**
- Consumes: `settings.sandbox`, `settings.sender_node_url`, `routes.router`, `node.router`.
- Produces: `sandbox.mount(app: FastAPI, settings) -> None`, `sandbox.SandboxMisconfigured(RuntimeError)`.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sandbox/tests/test_guard.py`:

```python
"""Предохранитель: песочница с боевым транспортом — это письма живым людям."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import sandbox


def _settings(sandbox_on: bool, node_url: str):
    return SimpleNamespace(sandbox=sandbox_on, sender_node_url=node_url)


def test_off_by_default_mounts_nothing():
    app = FastAPI()
    sandbox.mount(app, _settings(False, "http://127.0.0.1:8788"))
    assert not [route for route in app.routes
                if getattr(route, "path", "").startswith("/api/sandbox")]


def test_on_mounts_both_routers():
    app = FastAPI()
    sandbox.mount(app, _settings(True, "http://127.0.0.1:8787/api/sandbox/node"))
    mounted = {route.path for route in app.routes}
    assert "/api/sandbox/runs" in mounted
    assert "/api/sandbox/node/send" in mounted


def test_on_with_the_real_transport_refuses_to_start():
    """Тестовый прогон с боевым SENDER_NODE_URL ушёл бы живым людям с боевых
    номеров. Падать на старте — единственный момент, когда это ещё дёшево."""
    with pytest.raises(sandbox.SandboxMisconfigured) as failure:
        sandbox.mount(FastAPI(), _settings(True, "http://127.0.0.1:8788"))
    assert "SENDER_NODE_URL" in str(failure.value)
```

Создать `backend/sandbox/tests/test_import_graph.py`:

```python
"""Граница песочницы: она знает про системы, системы про неё — нет.

Тот же статический обход графа импортов, что сторожит отсутствие сети в
rebuild.py и в системе 3. Единственное исключение — collector/api.py: он и есть
место, где приложение собирается, и все швы живут там.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent
ASSEMBLER = "collector/api.py"

# Обходятся корни систем и модули верхнего уровня, а не весь backend: rglob по
# нему заодно прочёл бы .venv — тысячи чужих файлов на каждом прогоне сюиты.
ROOTS = ("collector", "writer", "sender")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def sources() -> list[Path]:
    found = list(BACKEND.glob("*.py"))
    for root in ROOTS:
        found.extend((BACKEND / root).rglob("*.py"))
    return sorted(found)


def test_nobody_imports_the_sandbox():
    offenders = []
    for path in sources():
        rel = path.relative_to(BACKEND).as_posix()
        if rel == ASSEMBLER:
            continue
        if any(module == "sandbox" or module.startswith("sandbox.")
               for module in imports(path)):
            offenders.append(rel)
    assert offenders == [], f"песочницу импортирует не только сборщик: {offenders}"


def test_the_guard_itself_sees_the_assembler():
    """Страж бесполезен, если перестал находить файлы: сборщик обязан
    импортировать песочницу."""
    assert any(module.startswith("sandbox") for module in imports(BACKEND / ASSEMBLER))
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd backend && uv run pytest sandbox/tests/test_guard.py sandbox/tests/test_import_graph.py -v`
Expected: FAIL — `AttributeError: module 'sandbox' has no attribute 'mount'`

- [ ] **Step 3: Добавить флаг в настройки и пример окружения**

В `backend/config.py`, в класс `Settings`, рядом с `sandbox_self_url`:

```python
    # Песочница: подменный транспорт и отдельная база вместо боевых. Флагом, а
    # не наличием файла: включать её должно быть так же явно, как выключать.
    sandbox: bool = False
```

В `backend/.env.example` дописать в конец:

```
# Песочница аутрича. Включённая, она подменяет транспорт и базу целиком:
# SENDER_NODE_URL обязан указывать на подменный Node, иначе бэкенд не стартует.
SANDBOX=0
# SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node
```

- [ ] **Step 4: Написать `sandbox.mount`**

Дописать в `backend/sandbox/__init__.py`:

```python
from fastapi import FastAPI

SANDBOX_NODE_PATH = "/api/sandbox/node"


class SandboxMisconfigured(RuntimeError):
    """Песочница включена, а транспорт боевой — письма ушли бы живым людям."""


def mount(app: FastAPI, settings) -> None:
    """Роутеры песочницы существуют только при SANDBOX=1.

    Не «если флаг, то не работать», а «если нет флага, то и ручек нет»: тот же
    принцип, которым rebuild лишён возможности сходить в сеть — невозможность,
    а не дисциплина.
    """
    if not settings.sandbox:
        return
    if SANDBOX_NODE_PATH not in settings.sender_node_url:
        raise SandboxMisconfigured(
            f"SANDBOX=1, но SENDER_NODE_URL={settings.sender_node_url} — это боевой"
            f" транспорт. Пропишите {SANDBOX_NODE_PATH} или выключите песочницу.")
    from sandbox import node, routes

    app.include_router(routes.router)
    app.include_router(node.router)
```

- [ ] **Step 5: Позвать `mount` из сборщика**

В `backend/collector/api.py` после последнего `app.include_router(...)`:

```python
# Песочница монтируется здесь по той же причине, по которой здесь живут
# остальные швы: api.py — единственное место, где системы видят друг друга.
sandbox.mount(app, settings)
```

с `import sandbox` и `from config import settings` в шапке: `settings` в `collector/api.py` сейчас не импортирован (`grep -n "from config" backend/collector/api.py` — пусто).

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `cd backend && uv run pytest sandbox/tests -v && uv run pytest -q`
Expected: PASS — вся сюита зелёная, включая граф импортов.

- [ ] **Step 7: Проверить руками, что бэкенд поднимается в обоих режимах**

```bash
cd backend && uv run python -c "
from collector import api
print('без песочницы:', [r.path for r in api.app.routes if r.path.startswith('/api/sandbox')])
"
```
Expected: `без песочницы: []`

```bash
cd backend && SANDBOX=1 SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node uv run python -c "
from collector import api
print('с песочницей:', sorted(r.path for r in api.app.routes if r.path.startswith('/api/sandbox')))
"
```
Expected: список из двенадцати путей, среди них `/api/sandbox/chat` и `/api/sandbox/node/send`.

```bash
cd backend && SANDBOX=1 uv run python -c "from collector import api" 2>&1 | tail -3
```
Expected: `SandboxMisconfigured: SANDBOX=1, но SENDER_NODE_URL=http://127.0.0.1:8788 — это боевой транспорт…`

- [ ] **Step 8: Коммит**

```bash
git add backend/sandbox backend/config.py backend/collector/api.py backend/.env.example
git commit -m "$(cat <<'EOF'
feat(sandbox): монтирование под флагом и предохранитель на старте

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Страница `/sandbox`

**Files:**
- Create: `frontend/app/sandbox/page.tsx`
- Modify: `frontend/app/api.ts` (типы и вызовы песочницы), `frontend/components/Sidebar.tsx:6-25` (пункт навигации), `frontend/app/globals.css` (классы ленты и пульта)

**Interfaces:**
- Consumes: ручки `/api/sandbox/*` из Task 5 и 6, `useLive().refreshTick`, `requestDraft` и `fetchLeads` из `app/api.ts`.
- Produces: страница `/sandbox`; в `api.ts` — `SandboxRun`, `SandboxFaults`, `SandboxChat`, `SandboxBubble`, `fetchSandboxRuns`, `createSandboxRun`, `activateSandboxRun`, `moveSandboxClock`, `fetchSandboxFaults`, `setSandboxFaults`, `fetchSandboxChat`, `sendSandboxIncoming`.

- [ ] **Step 1: Добавить контракт в `frontend/app/api.ts`**

Дописать в конец файла:

```ts
// ---- Песочница: подменный транспорт и роль лида ----

export type SandboxRun = {
  run_id: string;
  company_id: string;
  created_at: string;
  warmed: boolean;
  offset_seconds: number;
  active: boolean;
};

export type SandboxFaults = {
  send: "ok" | "not_sent" | "unknown";
  delivery: "delivered" | "read" | "silent";
  number: "connected" | "loggedOut" | "stalled";
  has_whatsapp: boolean;
};

/** Судьба нашего сообщения в очереди. У входящего её нет: лид ничего не ждёт. */
export type SandboxFate = {
  status: string;
  attempts: number;
  send_after: string;
  delivered_at: string | null;
  read_at: string | null;
  error: string | null;
};

export type SandboxBubble = {
  message_id: number;
  role: "outgoing" | "incoming";
  kind: "draft" | "queued" | "sent" | "incoming";
  text: string;
  at: string;
  fate: SandboxFate | null;
};

export type SandboxChat = {
  thread_id: string;
  company_id: string;
  stage: string;
  status: string;
  touch_no: number;
  next_touch_at: string | null;
  bubbles: SandboxBubble[];
};

export function fetchSandboxRuns() {
  return json<{ runs: SandboxRun[] }>("/api/sandbox/runs");
}

export function createSandboxRun(companyId: string, warmed: boolean) {
  return post<{ run: SandboxRun }>("/api/sandbox/runs", {
    company_id: companyId,
    warmed,
  });
}

export function activateSandboxRun(runId: string) {
  return post<{ run: SandboxRun }>(
    `/api/sandbox/runs/${encodeURIComponent(runId)}/activate`,
    {},
  );
}

/** preset: jitter | hour | day | window. Что они значат, решает бэкенд. */
export function moveSandboxClock(preset: string) {
  return post<{ offset_seconds: number; now: string }>("/api/sandbox/clock", { preset });
}

export function fetchSandboxFaults() {
  return json<SandboxFaults>("/api/sandbox/faults");
}

export function setSandboxFaults(change: Partial<SandboxFaults>) {
  return post<SandboxFaults>("/api/sandbox/faults", change);
}

export function fetchSandboxChat() {
  return json<SandboxChat>("/api/sandbox/chat");
}

export function sendSandboxIncoming(text: string) {
  return post<{ handled: boolean }>("/api/sandbox/incoming", { text });
}
```

Если хелпера `post` в файле нет — проверить: `grep -n "function post" frontend/app/api.ts`; он используется существующими вызовами (`requestDraft`), так что должен быть.

- [ ] **Step 2: Добавить пункт навигации**

В `frontend/components/Sidebar.tsx` в импорт иконок добавить `FlaskIcon`, в массив `NAV` — последним элементом:

```tsx
  { href: "/sandbox", label: "Песочница", icon: FlaskIcon },
```

- [ ] **Step 3: Написать страницу**

Создать `frontend/app/sandbox/page.tsx`:

```tsx
"use client";

/** Песочница: я играю лида, продукт работает по-настоящему.

 Слева лента с судьбой каждого сообщения, справа пульт: часы, аварии, прогоны.
 Ничего своего страница не решает — все состояния приезжают с бэкенда.
 */

import { useCallback, useEffect, useState } from "react";
import {
  activateSandboxRun,
  createSandboxRun,
  fetchSandboxChat,
  fetchSandboxFaults,
  fetchSandboxRuns,
  moveSandboxClock,
  queueMessage,
  requestDraft,
  sendSandboxIncoming,
  setSandboxFaults,
  type SandboxBubble,
  type SandboxChat,
  type SandboxFaults,
  type SandboxRun,
} from "../api";
import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const KIND_LABELS: Record<SandboxBubble["kind"], string> = {
  draft: "черновик",
  queued: "в очереди",
  sent: "отправлено",
  incoming: "",
};

const SHIFTS = [
  { preset: "jitter", label: "+15 мин" },
  { preset: "hour", label: "+1 час" },
  { preset: "day", label: "+1 день" },
  { preset: "window", label: "к открытию окна" },
];

const SWITCHES: { field: keyof SandboxFaults; label: string; options: string[] }[] = [
  { field: "send", label: "Отправка", options: ["ok", "not_sent", "unknown"] },
  { field: "delivery", label: "Доставка", options: ["delivered", "read", "silent"] },
  { field: "number", label: "Номер", options: ["connected", "loggedOut", "stalled"] },
];

export default function SandboxPage() {
  const { refreshTick } = useLive();
  const [runs, setRuns] = useState<SandboxRun[]>([]);
  const [chat, setChat] = useState<SandboxChat | null>(null);
  const [faults, setFaults] = useState<SandboxFaults | null>(null);
  const [company, setCompany] = useState("");
  const [warmed, setWarmed] = useState(true);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchSandboxRuns().then((data) => setRuns(data.runs)).catch(report);
    fetchSandboxFaults().then(setFaults).catch(report);
    // Треда может ещё не быть — это не ошибка, а состояние «сначала черновик».
    fetchSandboxChat().then(setChat).catch(() => setChat(null));
  }, []);

  function report(error: Error) {
    setFailure(error.message);
  }

  useEffect(reload, [reload, refreshTick]);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setFailure(null);
    try {
      await action();
      reload();
    } catch (error) {
      report(error as Error);
    } finally {
      setBusy(false);
    }
  }

  const active = runs.find((run) => run.active) ?? null;
  // Ставится последний непоставленный черновик: очередь принимает текст, а
  // какой именно — единственный вопрос, на который лента уже отвечает.
  const draft = chat?.bubbles.filter((bubble) => bubble.kind === "draft").at(-1) ?? null;

  return (
    <>
      <header className="page-head">
        <h1>Песочница</h1>
        <span className="page-sub">
          Я лид, продукт настоящий: транспорт подменён, часы двигаются кнопкой.
        </span>
      </header>

      {failure && <div className="failure">{failure}</div>}

      <div className="sandbox-runs">
        <select
          className="input"
          value={active?.run_id ?? ""}
          onChange={(event) => act(() => activateSandboxRun(event.target.value))}
        >
          <option value="" disabled>
            прогон не выбран
          </option>
          {runs.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.company_id} · {WHEN.format(new Date(run.created_at))}
              {run.warmed ? "" : " · номер новый"}
            </option>
          ))}
        </select>
        <input
          className="input"
          placeholder="company_id из выдачи"
          value={company}
          onChange={(event) => setCompany(event.target.value)}
        />
        <label className="sandbox-check">
          <input
            type="checkbox"
            checked={warmed}
            onChange={(event) => setWarmed(event.target.checked)}
          />
          номер прогрет
        </label>
        <button
          className="btn"
          disabled={busy || !company}
          onClick={() => act(() => createSandboxRun(company, warmed))}
        >
          Новый прогон
        </button>
      </div>

      <div className="split">
        <div className="detail">
          <ol className="thread-log">
            {chat?.bubbles.map((bubble) => (
              <li
                key={bubble.message_id}
                className={bubble.role === "incoming" ? "thread-reply" : ""}
              >
                <span className="thread-who">
                  {bubble.role === "incoming" ? "лид" : "мы"}
                  {KIND_LABELS[bubble.kind] && (
                    <span className="message-kind"> · {KIND_LABELS[bubble.kind]}</span>
                  )}
                </span>
                <p className="row-text">{bubble.text}</p>
                <span className="sandbox-fate mono">
                  {WHEN.format(new Date(bubble.at))}
                  {bubble.fate && ` · ${fateOf(bubble.fate)}`}
                </span>
              </li>
            ))}
            {chat === null && (
              <li className="placeholder">
                Треда ещё нет. Создайте прогон и нажмите «Черновик первого письма».
              </li>
            )}
          </ol>

          <div className="composer">
            <textarea
              className="input"
              rows={3}
              placeholder="ответить как лид (пусто = медиа без текста)"
              value={reply}
              onChange={(event) => setReply(event.target.value)}
            />
            <div className="composer-actions">
              <button
                className="btn"
                disabled={busy || chat === null}
                onClick={() =>
                  act(async () => {
                    await sendSandboxIncoming(reply);
                    setReply("");
                  })
                }
              >
                Отправить как лид
              </button>
              <button
                className="btn-secondary"
                disabled={busy || !active}
                onClick={() => act(() => requestDraft(active!.company_id, "first"))}
              >
                Черновик первого письма
              </button>
              <button
                className="btn-secondary"
                disabled={busy || !draft || !chat}
                onClick={() => act(() => queueMessage(chat!.thread_id, draft!.text))}
              >
                Поставить в очередь
              </button>
            </div>
          </div>
        </div>

        <aside className="roster">
          <section className="card">
            <h2 className="card-sub">Прогон</h2>
            {chat ? (
              <ul className="sandbox-facts">
                <li>
                  Тред <span className="mono">{chat.thread_id}</span>
                </li>
                <li>Этап: {chat.stage}</li>
                <li>Статус: {chat.status}</li>
                <li>Касаний: {chat.touch_no}</li>
                <li>
                  Следующее касание:{" "}
                  {chat.next_touch_at
                    ? WHEN.format(new Date(chat.next_touch_at))
                    : "не запланировано"}
                </li>
              </ul>
            ) : (
              <p className="placeholder">Тред не открыт.</p>
            )}
          </section>

          <section className="card">
            <h2 className="card-sub">Часы</h2>
            <p className="note mono">
              сдвиг: {Math.round((active?.offset_seconds ?? 0) / 3600)} ч
            </p>
            <div className="controls">
              {SHIFTS.map(({ preset, label }) => (
                <button
                  key={preset}
                  className="btn-quiet"
                  disabled={busy || !active}
                  onClick={() => act(() => moveSandboxClock(preset))}
                >
                  {label}
                </button>
              ))}
            </div>
          </section>

          <section className="card">
            <h2 className="card-sub">Аварии</h2>
            {faults &&
              SWITCHES.map(({ field, label, options }) => (
                <label key={field} className="sandbox-switch">
                  {label}
                  <select
                    className="input"
                    value={String(faults[field])}
                    onChange={(event) =>
                      act(() => setSandboxFaults({ [field]: event.target.value }))
                    }
                  >
                    {options.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            {faults && (
              <label className="sandbox-check">
                <input
                  type="checkbox"
                  checked={faults.has_whatsapp}
                  onChange={(event) =>
                    act(() => setSandboxFaults({ has_whatsapp: event.target.checked }))
                  }
                />
                у лида есть WhatsApp
              </label>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}

/** Судьба строки очереди словами. Словарь бэкенда — статусы outbox. */
function fateOf(fate: NonNullable<SandboxBubble["fate"]>): string {
  if (fate.read_at) return "прочитано";
  if (fate.delivered_at) return "доставлено";
  if (fate.status === "failed") return `не ушло, попыток ${fate.attempts}`;
  if (fate.status === "stuck") return "stuck — разбирает человек";
  if (fate.status === "cancelled") return "отменено";
  if (fate.status === "sent") return "отправлено, доставки нет";
  return `в очереди с ${WHEN.format(new Date(fate.send_after))}`;
}
```

- [ ] **Step 4: Дописать классы в `frontend/app/globals.css`**

В конец файла:

```css
.sandbox-runs {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.sandbox-check {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--muted);
}

.sandbox-switch {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 8px;
}

.sandbox-facts {
  list-style: none;
  padding: 0;
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
}

/* Судьба сообщения — не украшение ленты, а её содержание: набор мельче
   текста, но не серее подписи автора. */
.sandbox-fate {
  font-size: 11px;
  color: var(--muted);
}
```

Если переменной `--muted` в `:root` нет — взять ту, которой пользуются `.note` и `.placeholder`: `grep -n "^\.note" -A4 frontend/app/globals.css`.

- [ ] **Step 5: Дописать три оставшихся блока пульта**

Пул, журнал и автопилот приезжают из уже существующих ручек — своих
песочница для них не заводит. В `frontend/app/sandbox/page.tsx` дописать в
существующий импорт из `"../api"` пять имён (одним списком, второго импорта из
того же модуля не заводить):

```tsx
  fetchActivity,
  fetchSender,
  setAutopilot,
  type ActivityEvent,
  type SenderStatus,
```

добавить состояние рядом с остальным:

```tsx
  const [pool, setPool] = useState<SenderStatus | null>(null);
  const [journal, setJournal] = useState<ActivityEvent[]>([]);
```

дополнить `reload`:

```tsx
    fetchSender().then(setPool).catch(report);
    fetchActivity(20).then((data) => setJournal(data.events)).catch(report);
```

и дописать три секции в `<aside className="roster">` после блока «Аварии»:

```tsx
          <section className="card">
            <h2 className="card-sub">Пул</h2>
            <ul className="sandbox-facts">
              {pool?.numbers.map((number) => (
                <li key={number.number}>
                  <span className="mono">{number.number}</span> · {number.status} ·
                  день {number.day} ({number.phase}) · {number.sent_today} из{" "}
                  {number.daily_limit}
                </li>
              ))}
              {pool?.numbers.length === 0 && (
                <li className="placeholder">Номеров в прогоне нет.</li>
              )}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-sub">Журнал</h2>
            <ul className="sandbox-facts">
              {journal.map((event) => (
                <li key={`${event.actor}-${event.at}-${event.outcome}`}>
                  <span className="mono">{WHEN.format(new Date(event.last_at))}</span>{" "}
                  {event.actor} · {event.outcome}
                  {event.repeats > 1 && ` ×${event.repeats}`}
                </li>
              ))}
              {journal.length === 0 && (
                <li className="placeholder">Демоны ещё не отчитывались.</li>
              )}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-sub">Автопилот</h2>
            <div className="controls">
              {(["off", "replies", "full"] as const).map((mode) => (
                <button
                  key={mode}
                  className={pool?.autopilot === mode ? "btn" : "btn-quiet"}
                  disabled={busy}
                  onClick={() => act(() => setAutopilot(mode))}
                >
                  {mode}
                </button>
              ))}
            </div>
            <p className="note">
              Тот же файл-переключатель, что в бою: второй копии kill switch нет.
            </p>
          </section>
```

Поля `ActivityEvent` (`actor`, `outcome`, `last_at`, `repeats`) сверить с
`frontend/app/api.ts` — тип уже объявлен там для страницы «Процессы».

- [ ] **Step 6: Проверить руками**

```bash
cd backend && SANDBOX=1 SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node uv run python main.py
```
и в другом окне `cd frontend && npm run dev`.

Открыть `http://localhost:3000/sandbox`, вписать `company_id` любой компании из `/leads`, нажать «Новый прогон» → «Черновик первого письма». Ожидается: черновик появился в ленте серой подписью «черновик».

Дальше — «Поставить в очередь» на той же странице, затем «к открытию окна» и «+15 мин» на пульте. Ожидается: пузырь меняет подпись на «в очереди», потом «отправлено», потом «доставлено» — на это уходит один-два тика воркера (20 секунд каждый).

Ответить как лид «сколько стоит?» → в ленте появляется входящее, следующим тиком — ответ продавца.

Переключить «Отправка» в `not_sent` и повторить постановку в очередь. Ожидается: «не ушло, попыток 1», дальше 2 и 3, затем строка кончается.

- [ ] **Step 7: Коммит**

```bash
git add frontend/app/sandbox frontend/app/api.ts frontend/app/globals.css frontend/components/Sidebar.tsx
git commit -m "$(cat <<'EOF'
feat(web): страница «Песочница» — лента в роли лида и пульт стенда

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Документация

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Дописать секцию в `CLAUDE.md`**

После секции «Слои данных» добавить:

```markdown
## Песочница (`backend/sandbox/`)

Стенд, на котором аутрич проходится целиком без единого сообщения в WhatsApp.
Включается флагом `SANDBOX=1` в `backend/.env` вместе с
`SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node`; без флага роутеров
песочницы в приложении нет вовсе, а с флагом и боевым адресом транспорта
приложение не стартует — тестовый прогон, ушедший живым людям, дороже падения.

**Прогон — отдельный файл базы** (`data/sandbox/<run>.db`): `thread_id` — это
номер телефона и первичный ключ `threads`, поэтому второй прогон по тому же
лиду упёрся бы в занятый ключ, а «чистый контекст» пришлось бы изображать
фильтром в пяти местах. Схему наполняют настоящие владельцы таблиц, `derived.db`
остаётся боевой и только на чтение: лид в прогоне — настоящая компания.

**`backend/paths.py`** — единственный владелец пути к невосстановимому слою на
три системы (раньше его называли пять мест). **`backend/clock.py`** — «сейчас»
продукта; шесть мест зовут его вместо `datetime.now`. Смещение принадлежит
прогону и лежит в его базе: иначе прогон, начатый со сдвигом «+3 дня», после
перезапуска бэкенда откатился бы назад во времени. В бою смещение нулевое.

**Подменный Node** (`sandbox/node.py`) отдаёт те же пять ручек, что
`sender/node/index.js`, и события шлёт в свой же `POST /api/sender/webhook` —
тем же HTTP, с тем же секретом, через тот же дедуп. Статус доставки уезжает
фоном с повтором, пока вебхук не подтвердит: без сети он обогнал бы запись
`provider_id` в `outbox`. Четыре тумблера аварий (`sandbox/faults.py`) живут в
памяти процесса — авария, пережившая перезапуск, объяснялась бы потом полдня.

Песочница знает про все три системы; её не импортирует никто, кроме сборщика
`collector/api.py` — сторожит `sandbox/tests/test_import_graph.py`.
```

- [ ] **Step 2: Прогнать всё в последний раз**

Run: `cd backend && uv run pytest -q`
Expected: PASS, вся сюита зелёная.

- [ ] **Step 3: Коммит**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: песочница аутрича в CLAUDE.md

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```
