# Хранилище и операционный слой системы 1 — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Свести четыре хранилища к двум базам (`state.db` + `derived.db`) и папке `raw/`, ввести версионирование прогонов вместо перезаписи, заменить CLI-скрипты на HTTP-операции и перевести проверки с самописного `check.py` на pytest — не изменив ни одной строки выдачи.

**Architecture:** Граница «вычислимое / порождённое» становится физической: `derived.db` пересобирается прогонами с `run_id`, `state.db` хранит невосстановимое (отказы, переписка, оплаченные ответы модели). Читающий код держится на view `orgs` и т.д., поэтому `report.py`, `routes/`, `services/leads.py`, `writer/leads_source.py` не меняются. Исполнение джобов переходит с subprocess на in-process вызовы функций-операций через `asyncio.to_thread`.

**Tech Stack:** Python 3.13 + uv, SQLite (WAL), FastAPI/uvicorn, pytest (единственная новая зависимость), Next.js (фронтенд).

**Spec:** [docs/superpowers/specs/2026-08-18-storage-and-operations-design.md](../specs/2026-08-18-storage-and-operations-design.md)

## Global Constraints

- Команды `uv run` запускаются **из каталога `collector/`**, если не сказано иное; команды writer'а — из `writer/`.
- **`data/raw/` не трогать.** Ни один файл не удалять и не править. Адаптер хранилища читает их как есть.
- **Инвариант выдачи (критерий приёмки, спека §6):** после миграции на том же сырье и конфиге — 1660 компаний, 1867 организаций, 7226 контактов, 1876 сигналов, 40 профилей, тот же порядок выдачи. Расхождение — условие остановки.
- **`build.py` не ходит в сеть:** `rebuild.py` не тянет `services.fetch` и `scrapling` ни прямо, ни транзитивно — проверяется статически по графу импортов и поведенчески (счёт файлов в `raw/` до и после).
- **Ни одна операция не пишет в обе базы сразу** (транзакция через две базы в WAL не атомарна) — проверяется тестом: ни одна функция записи не открывает обе базы.
- **`state.db` — единственный невосстановимый слой** рядом с `raw/`: схема через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён, `sent_text`/отказы/ответы модели не стираются.
- **Отказы в `state.suppression`, источник истины — база**, `suppression.csv` удаляется. `GET /api/suppression/export.csv` — выгрузка по требованию.
- Папка `db/` переименовывается в `store/`; в ней остаются только `schema.sql` и `lead.py` (код, а не данные).
- `pytest` — единственная новая зависимость, в dev-зависимостях. Ассерты переносятся из `check.py` дословно (меняется обёртка, не смысл).
- В рабочем дереве лежат незакоммиченные изменения (система 2, docker, hh). Коммитить **только перечисленные в задаче файлы** через явный `git add <путь>`. Никаких `git add -A` / `git add .` / `git commit -a`.
- Стиль сообщений — conventional commits, по-русски, как в `git log`.
- Функции-операции получают `RunContext` для прогресса, структурного лога и кооперативной отмены; возвращают dict-результат, а не код возврата.

---

## Структура файлов

| Файл | Что с ним происходит | Задача |
|---|---|---|
| `collector/db/` → `collector/store/` | переименование каталога (`schema.sql`, `lead.py`) | 1 |
| `collector/store/schema.sql` | новая схема: `runs`, `current_run`, `*_all` + view, `state`-таблицы | 2 |
| `collector/store/lead.py` | подключение через `store.connect()` (derived + ATTACH state) | 2 |
| `collector/services/store.py` | **новый**: `connect()`, `new_run()`, `activate_run()`, `run_history()`, обёртки записи | 2, 3 |
| `collector/services/storage.py` | **новый**: адаптер сырья `put/get/exists/iter_pages` | 4 |
| `collector/services/pipeline/__init__.py` | реестр `OPERATIONS` | 9 |
| `collector/services/pipeline/collect.py` | из `scripts/collect.py` (gis, sites, instagram как операции) | 7 |
| `collector/services/pipeline/analyze.py` | из `scripts/classify.py` + `scripts/classify_ig.py` | 8 |
| `collector/services/pipeline/rebuild.py` | из `build.py` (прогон вместо DROP) | 3 |
| `collector/services/pipeline/export.py` | из `report.py` | 6 |
| `collector/services/pipeline/probe.py` | из `services/probes/*` | 7 |
| `collector/services/jobs.py` | воркер зовёт функции, не процессы; кооперативная отмена | 9 |
| `collector/routes/pipeline.py` | каталог + `POST /api/pipeline/{kind}` | 10 |
| `collector/routes/operations.py` | **новый**: `POST /api/operations/{name}` | 10 |
| `collector/routes/runs.py` | **новый**: `GET /api/runs`, `POST /api/runs/{id}/activate` | 10 |
| `collector/routes/suppression.py` | `GET /api/suppression/export.csv` | 10 |
| `collector/api.py` | монтирует новые роутеры, lifespan | 10 |
| `collector/tests/*` | pytest-набор из `scripts/check.py` | 5 |
| `writer/tests/*` | pytest-набор из `writer/scripts/check.py` | 5 |
| `writer/thread_store.py` | путь к базе → `data/state.db` | 2 |
| `writer/config.toml` | пути → `../collector/data/derived.db`, `../collector/data/state.db` | 2 |
| `collector/scripts/migrate.py` | **новый, временный**: перенос четырёх хранилищ | 11 |
| `frontend/app/api.ts` | типы/функции: операции, runs, экспорт | 12 |
| `frontend/components/OperationsPanel.tsx` | **новый**: вторая группа кнопок | 12 |
| `frontend/components/RunsHistory.tsx` | **новый**: история прогонов + откат | 12 |
| `frontend/app/page.tsx` | вставка панелей | 12 |
| `.gitignore`, `CLAUDE.md`, `docs/*` | актуализация | 13 |
| удаляются | `scripts/collect.py`, `scripts/classify.py`, `scripts/classify_ig.py`, `build.py`, `report.py`, `scripts/check.py`, `services/probes/*`, `data/suppression.csv`, `db/ops.db`, `writer/threads.db` | на протяжении |

---

## Task 1: переименование `db/` → `store/` и перенос путей

Каталог `db/` обещает базы, которых в новой схеме не будет. Переименование чистое: правится только место, где лежат `schema.sql` и `lead.py`, и все импорты/пути, которые на них ссылаются. Никакой логики не меняется, `db/leads.db` и `db/ops.db` пока остаются на месте (их перенесут задачи 2 и 11).

**Files:**
- Rename: `collector/db/` → `collector/store/`
- Modify: `collector/build.py` (`SCHEMA = Path("db/schema.sql")` → `store/schema.sql`)
- Modify: `collector/api.py` (`Path("db/schema.sql")` в `demo()`)
- Modify: `collector/db/lead.py` → `collector/store/lead.py` (docstring, без логики)
  (путь writer'а к схеме в `writer/scripts/check.py` здесь **не** правится — он выводится из конфига и исправится в Task 2)

**Interfaces:**
- Consumes: ничего.
- Produces: `collector/store/schema.sql`, `collector/store/lead.py` — новые пути, на которые ссылаются задачи 2–11.

- [ ] **Step 1: снять эталон выдачи (критерий приёмки, спека §6)**

Пока `report.py` и старая `db/leads.db` на месте, зафиксировать эталон, с которым Task 11 сверит новый прогон. Без этого эталона приёмка «то же содержимое» не с чем сравнивать, а позже `report.py` уже будет удалён (Task 6):

```bash
cd collector
uv run report.py 30
shasum -a 256 data/leads.csv
sqlite3 db/leads.db "select count(*) from companies; select count(*) from orgs; select count(*) from contacts; select count(*) from signals; select count(*) from profiles;"
```

Ожидается: sha `data/leads.csv` (записать в Global Constraints как эталон), и `1660 / 1867 / 7226 / 1876 / 40`. Этот sha подставляется в задачу Task 14 (сравнение выдачи).

- [ ] **Step 2: перенести только код, не данные**

`git mv db store` перенёс бы **всё** — включая неотслеживаемые `db/leads.db` и `db/ops.db`, которые Task 11 ещё должна прочитать со старого места. Поэтому двигаются только два кодовых файла; данные остаются в `db/` до миграции:

```bash
cd collector
git mv db/schema.sql store/schema.sql
git mv db/lead.py store/lead.py
# проверить, что данные на месте (их не трогаем):
ls db/leads.db db/ops.db
```

Ожидается: оба файла данных существуют.

- [ ] **Step 3: править путь к схеме в `build.py`**

```bash
cd collector
sed -i '' 's|SCHEMA = Path("db/schema.sql")|SCHEMA = Path("store/schema.sql")|' build.py
```

- [ ] **Step 4: править путь к схеме в `api.py`**

```bash
cd collector
sed -i '' 's|Path("db/schema.sql")|Path("store/schema.sql")|' api.py
```

- [ ] **Step 5: обновить импорты `db` → `store`**

Пакет `db` больше не существует (переименован в `store`). Найти и заменить импорт во **всех** местах:

```bash
cd collector
rg -l "from db import lead" --glob '*.py' | xargs sed -i '' 's|from db import lead|from store import lead|'
rg -l "import db" --glob '*.py'
```

Ожидается: первый `rg` заменяет в семи файлах (`api.py`, `routes/leads.py`, `routes/suppression.py`, `services/leads.py`, `services/suppression.py`, `services/metrics.py`, `scripts/check.py`); второй `rg` не выводит ничего.

- [ ] **Step 6: убедиться, что `writer/scripts/check.py` найдёт схему**

Путь к схеме в writer'е строится не строкой, а от конфига: `CONFIG["leads_db"].parent / "schema.sql"` (см. `writer/scripts/check.py:60`). Правок в writer'е **не делать** — путь исправится, когда Task 2 сменит `leads_db` на `../collector/data/derived.db`. Сейчас только проверить, что файл лежит по новому пути:

```bash
cd collector
ls store/schema.sql
```

Ожидается: файл существует. (Раздел `parsers` в `scripts/check.py` схемы не трогает.)

- [ ] **Step 7: прогнать демо-проверки, что пути и импорты живы**

```bash
cd collector
uv run -m scripts.check parsers
uv run python -c "import api"
```

Ожидается: `check ok: parsers` и импорт `api` без `ModuleNotFoundError` (проверяет, что все `from store import lead` резолвятся).

- [ ] **Step 8: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/store collector/build.py collector/api.py \
  collector/routes collector/services collector/scripts
git commit -m "refactor(collector): db/ -> store/ (только описание схемы и запросы)"
```

> `git add collector/store` подхватит переименование и `store/lead.py`. Данные `db/leads.db`/`db/ops.db` остаются на месте до Task 11. `writer/scripts/check.py` не трогается (его путь исправится в Task 2).

---

## Task 2: новая схема — две базы, версионирование, view

Создаются `data/derived.db` (версионированные таблицы + view текущего прогона) и `data/state.db` (невосстановимое), обе в WAL. `store/lead.py` читает через `services/store.connect()` с `ATTACH state AS state`. Читающие модули (`report.py`, `routes/*`, `services/leads.py`, `writer/leads_source.py`) продолжают обращаться к `orgs`, `companies` и т.д. — это теперь view, поэтому их код не меняется.

**Files:**
- Modify: `collector/store/schema.sql` (полностью новая схема)
- Create: `collector/services/store.py`
- Modify: `collector/store/lead.py` (использовать `store.connect()`)
- Modify: `collector/services/metrics.py` (`threads_db_path` → `data/state.db`)
- Modify: `writer/config.toml`, `writer/config.py`, `writer/thread_store.py`
- Modify: `collector/.gitignore` (в корне) — `data/state.db`, `data/derived.db`, `-wal`/`-shm`
- Create: `collector/tests/conftest.py`, `collector/tests/test_schema.py`

**Interfaces:**
- Produces: `store.connect()` → sqlite-соединение к `derived.db` с `ATTACH state AS state`, `row_factory` настроен. `store.new_run(note) -> run_id`, `store.activate_run(run_id)`, `store.run_history() -> list[dict]`. Таблицы: `state.suppression`, `state.threads`, `state.messages`, `state.llm_answers`, `state.jobs`, `derived.runs`, `derived.current_run`, `derived.orgs_all`… `derived.orgs` (view).
- Consumes: ничего.

- [ ] **Step 1: написать новый `store/schema.sql`**

Схема строится из двух частей (state и derived), применяемых `executescript`-ом к соответствующей базе. Файл делится **маркерами** `-- STATE --` / `-- DERIVED --` и закрывается `-- END --` (их разбирает `store._schema`, см. Step 2). Ниже — содержимое для `data/state.db` (порождённое), открывается маркером `-- STATE --`:

```sql
-- STATE --
-- state.db — порождённое: невосстановимо, CREATE TABLE IF NOT EXISTS, DROP запрещён.
CREATE TABLE IF NOT EXISTS suppression (
  handle   TEXT PRIMARY KEY,
  added_at TEXT NOT NULL,
  reason   TEXT
);

CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  seed       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads (thread_id),
  role       TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),
  draft_text TEXT,
  sent_text  TEXT,
  angle      TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, message_id);

CREATE TABLE IF NOT EXISTS llm_answers (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  subject    TEXT NOT NULL,   -- (название, город) для profile, логин для ig_signals
  model      TEXT NOT NULL,
  prompt     TEXT NOT NULL,
  answer     TEXT NOT NULL    -- json ответа
);

-- Очередь и история запусков операций (спека §1: state.db = suppression, jobs,
-- threads, messages, llm_answers). result — json результата операции.
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  title      TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  step       INTEGER NOT NULL DEFAULT 0,
  steps      TEXT NOT NULL,          -- json: [имена операций]
  log        TEXT NOT NULL DEFAULT '',
  progress   TEXT,
  result     TEXT,
  exit_code  INTEGER,
  error      TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
```

Содержимое для `data/derived.db` (вычислимое, версионируется), открывается маркером `-- DERIVED --`:

```sql
-- DERIVED --
-- derived.db — вычислимое: пересобирается прогонами, run_id в первичном ключе.
CREATE TABLE IF NOT EXISTS runs (
  run_id       INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  code_version TEXT,
  config_hash  TEXT,
  note         TEXT
);
-- Ровно одна строка (id=1). Указатель выдачи: view читают только её.
CREATE TABLE IF NOT EXISTS current_run (
  id     INTEGER PRIMARY KEY CHECK (id = 1),
  run_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fetches_all (
  run_id     INTEGER NOT NULL,
  url        TEXT NOT NULL,
  sha        TEXT NOT NULL,
  final_url  TEXT,
  status     INTEGER,
  fetched_at TEXT,
  PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS orgs_all (
  run_id       INTEGER NOT NULL,
  branch_id    TEXT NOT NULL,
  org_id       TEXT,
  name         TEXT,
  org_name     TEXT,
  branch_count INTEGER,
  city         TEXT,
  rubric_id    TEXT,
  address      TEXT,
  rating       REAL,
  review_count INTEGER,
  PRIMARY KEY (run_id, branch_id)
);
CREATE TABLE IF NOT EXISTS contacts_all (
  run_id     INTEGER NOT NULL,
  branch_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  handle     TEXT NOT NULL,
  source_url TEXT,
  PRIMARY KEY (run_id, branch_id, kind, handle)
);
CREATE TABLE IF NOT EXISTS companies_all (
  run_id     INTEGER NOT NULL,
  company_id TEXT NOT NULL,
  name_norm  TEXT,
  domain     TEXT,
  city       TEXT,
  rubric_id  TEXT,
  PRIMARY KEY (run_id, company_id)
);
CREATE TABLE IF NOT EXISTS company_links_all (
  run_id     INTEGER NOT NULL,
  company_id TEXT NOT NULL,
  branch_id  TEXT NOT NULL,
  rule       TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (run_id, company_id, branch_id, rule)
);
CREATE TABLE IF NOT EXISTS signals_all (
  run_id      INTEGER NOT NULL,
  company_id  TEXT NOT NULL,
  type        TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  weight      REAL,
  quote       TEXT,
  url         TEXT,
  PRIMARY KEY (run_id, company_id, type, observed_at, url)
);
CREATE TABLE IF NOT EXISTS scores_all (
  run_id      INTEGER NOT NULL,
  company_id  TEXT NOT NULL,
  fit_score   REAL,
  intent_score REAL,
  breakdown   TEXT,
  PRIMARY KEY (run_id, company_id)
);
CREATE TABLE IF NOT EXISTS profiles_all (
  run_id         INTEGER NOT NULL,
  company_id     TEXT NOT NULL,
  model          TEXT,
  industry       TEXT,
  size_hint      TEXT,
  has_sales_team INTEGER,
  why_now        TEXT,
  quote          TEXT,
  confidence     REAL,
  PRIMARY KEY (run_id, company_id)
);

CREATE VIEW IF NOT EXISTS fetches AS
  SELECT f.* FROM fetches_all f JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS orgs AS
  SELECT o.* FROM orgs_all o JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS contacts AS
  SELECT c.* FROM contacts_all c JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS companies AS
  SELECT c.* FROM companies_all c JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS company_links AS
  SELECT l.* FROM company_links_all l JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS signals AS
  SELECT s.* FROM signals_all s JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS scores AS
  SELECT s.* FROM scores_all s JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS profiles AS
  SELECT p.* FROM profiles_all p JOIN current_run USING (run_id);
-- END --
```

Записать файл целиком (заменить текущий `store/schema.sql`). Убедиться, что в файле ровно по одному маркеру `-- STATE --`, `-- DERIVED --`, `-- END --`:

```bash
cd collector
rg -c "^-- (STATE|DERIVED|END) --$" store/schema.sql
```

Ожидается: `3` (по одному вхождению каждого из трёх маркеров).

- [ ] **Step 2: написать `services/store.py`**

```python
"""Подключение к двум базам и управление прогонами.

state.db (порождённое) ATTACH'ится к derived.db (вычислимое): читающий код
держится на view текущего прогона и видит обе базы одним соединением. Запись
в обе базы одной транзакцией невозможна (WAL не атомарен между файлами),
поэтому ни одна операция не пишет туда и сюда — это сторожит тест.
"""

import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived.db"
STATE = DATA / "state.db"

SCHEMA = Path(__file__).resolve().parent.parent / "store" / "schema.sql"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _schema(part):
    """Часть schema.sql от маркера `-- {part} --` до следующего маркера
    (-- DERIVED -- для STATE-блока, -- END -- для DERIVED-блока).

    Файл устроен как: -- STATE -- <state-таблицы> -- DERIVED -- <derived-таблицы и view> -- END --
    Разрез идёт по следующему маркеру ПОСЛЕ start, иначе `_schema("DERIVED")` нашёл
    бы сам себя и вернул пустую строку.
    """
    text = SCHEMA.read_text(encoding="utf-8")
    start = text.index(f"-- {part} --")
    markers = ("-- DERIVED --", "-- END --")
    stops = [text.index(m, start + 1) for m in markers if text.find(m, start + 1) != -1]
    end = min(stops)
    return text[start:end]


def connect():
    """Соединение к derived.db с ATTACH state. Включает обе схемы.

    derived — главная (WAL), state — attached (WAL). Схема читается из
    store/schema.sql и делится маркерами на две половины; DDL живёт в одном
    файле, а не в двух местах.

    STATE-блок применяется к отдельному соединению, чьей ГЛАВНОЙ базой является
    state.db: его DDL без префикса (`CREATE TABLE IF NOT EXISTS suppression`)
    иначе создал бы таблицы в derived (главной базе текущего соединения), и
    `state.suppression` не существовал бы.
    """
    state_db = sqlite3.connect(STATE)
    state_db.executescript(_schema("STATE"))
    state_db.commit()
    state_db.close()

    db = sqlite3.connect(DERIVED)
    db.row_factory = sqlite3.Row
    db.executescript(_schema("DERIVED"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("ATTACH DATABASE ? AS state", (str(STATE),))
    db.execute("PRAGMA state.journal_mode=WAL")
    db.commit()
    return db
```

> В `store/schema.sql` каждая из двух половин обрамляется маркером-комментарием: ровно один `-- STATE --` и один `-- DERIVED --`, и в самом конце файла ровно один `-- END --`. Разметка пишется в Task 2 Step 1 (см. ниже) так:

```sql
-- STATE --
CREATE TABLE IF NOT EXISTS suppression (...);
...
-- DERIVED --
CREATE TABLE IF NOT EXISTS runs (...);
...
CREATE VIEW IF NOT EXISTS profiles AS ...;
-- END --
```

> `journal_mode=WAL` для attached `state` применяется через `PRAGMA state.journal_mode=WAL` (проверено на живой паре). Убедиться, что обе базы в WAL: `SELECT * FROM pragma_journal_mode` и `SELECT * FROM pragma_state_journal_mode`.

Добавить управление прогонами в тот же модуль:

```python
def new_run(db, note=None):
    """Начать прогон: вернуть run_id. current_run пока не трогается."""
    import subprocess
    code_version = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip() or None
    config_hash = config_digest()
    cur = db.execute(
        "INSERT INTO runs (started_at, code_version, config_hash, note)"
        " VALUES (?, ?, ?, ?)", (now(), code_version, config_hash, note)
    )
    db.commit()
    return cur.lastrowid


def config_digest():
    import hashlib
    path = Path(__file__).resolve().parent.parent / "config.toml"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activate_run(db, run_id):
    """Подмена выдачи: прошлый прогон или новый. Откат — той же функцией.

    current_run — таблица ровно из одной строки (id=1, CHECK в схеме): первый
    вызов вставляет её, последующие конфликтуют по id и обновляют. Иначе каждый
    activate добавлял бы строку, и view вернули бы строки всех прогонов разом.
    """
    db.execute(
        "INSERT INTO current_run (id, run_id) VALUES (1, ?)"
        " ON CONFLICT (id) DO UPDATE SET run_id = excluded.run_id",
        (run_id,),
    )
    db.commit()


def finish_run(db, run_id):
    db.execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (now(), run_id))
    db.commit()


def run_history(db, limit=50):
    rows = db.execute(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]
```

Ключевой хелпер для сборки — **чтение сквозь `current_run` во время пересборки неверно**: пока прогон строится, `current_run` ещё указывает на прошлый (или ни на какой) прогон, а view `orgs`/`companies`/`signals`/`fetches` вернули бы чужие данные. Поэтому все **внутренние чтения сборки** идут в `*_all` с фильтром по строящемуся `run_id`. Для простых чтений «вся таблица по run_id» — общий хелпер:

```python
def build_read(db, run_id, table):
    """SELECT * FROM {table}_all WHERE run_id = ? — чтение в рамках строящегося прогона.

    Продуктовые читатели (report/routes/leads/writer) используют view {table}
    текущего прогона; сборка использует физическую *_all и свой run_id.
    Это каноническая форма «прочитать таблицу прогона» — используется тестом
    test_rebuild_reads_own_run, чтобы доказать инвариант «сборка не видит чужой
    прогон». JOIN-чтения (fill_profiles/enrich/resolve/score) хардкодят *_all и
    run_id прямо в SQL — хелпер им не подходит, и дублировать его там не нужно.
    """
    return db.execute(
        f"SELECT * FROM {table}_all WHERE run_id = ?", (run_id,)
    )
```

Затем переписать `collector/store/lead.py`, чтобы он использовал `store.connect()` вместо `sqlite3.connect(DB)`:

```python
"""Запросы к базе, из которых собирается выдача. Читает view текущего прогона.

Соединение даёт services.store.connect() — derived.db с ATTACH state. Только
чтение, кроме записи в state.suppression (невосстановимый слой).
"""
import sqlite3
from services import store as engine


def connect():
    return engine.connect()


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM state.suppression")}
```

Аналогично остальные функции `lead.py` (`refusals`, `add_refusal`, `signals_of`, `stats`) — менять только префикс таблицы: `suppression` → `state.suppression`; `signals`, `scores`, `companies` остаются без префикса (это view derived). `add_refusal` пишет в `state.suppression`.

- [ ] **Step 3: править `writer` на новую базу**

`writer/config.toml`:

```toml
leads_db = "../collector/data/derived.db"
threads_db = "../collector/data/state.db"
```

`writer/thread_store.py` **не меняется** (ни схема, ни логика): он по-прежнему принимает путь в `connect(path)` и создаёт таблицы `threads`/`messages` через `CREATE TABLE IF NOT EXISTS`. Writer — отдельный uv-проект и не импортирует collector; он открывает `state.db` напрямую (своим `sqlite3.connect`) и пишет переписку в ту же базу, куда collector пишет отказы и ответы модели. Обе стороны используют `CREATE TABLE IF NOT EXISTS`, поэтому таблицы разных систем в одном файле не конфликтуют; `state.db` в WAL держит collector при `ATTACH`. Пути резолвит `writer/config.py` относительно `writer/`, так что `../collector/data/state.db` → `collector/data/state.db`. Убедиться:

```bash
cd writer
uv run -m scripts.check threads
```

Ожидается: `check ok: threads` (тест использует `thread_store.connect(":memory:")`, путь конфига не влияет — но запуск подтверждает, что модуль жив).

**`writer/leads_source.py` — ATTACH state для фильтра отказов.** Отказы переехали в `state.suppression`, а view не могут ссылаться на attached базу, поэтому в derived таблицы `suppression` нет. Writer обязан читать отказы из `state.suppression` (F21 — проверяется перед каждым ходом). Его `connect()` открывает только `derived.db`; добавить ATTACH state:

```python
def connect(path):
    """Только чтение: derived.db + ATTACH state.db для фильтра отказов."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    state = Path(path).parent / "state.db"
    db.execute(f"ATTACH DATABASE 'file:{state}?mode=ro' AS state", ())
    return db
```

А `suppression_handles` / `is_suppressed` меняют запрос на `state.suppression`:

```python
def is_suppressed(db, handle):
    return bool(db.execute(
        "SELECT 1 FROM state.suppression WHERE handle = ?", (handle,)
    ).fetchone())


def suppression_handles(db):
    return {row[0] for row in db.execute("SELECT handle FROM state.suppression")}
```

> `thread_store.connect` (пишет в `state.db`) не меняется: writer открывает state.db своим `sqlite3.connect` и создаёт `threads`/`messages` через `CREATE TABLE IF NOT EXISTS`. Обе стороны (collector при ATTACH и writer напрямую) используют один файл `state.db`; таблицы разных систем не конфликтуют.

- [ ] **Step 4: править `metrics.py`**

`services/metrics.py::threads_db_path` возвращает путь из `writer/config.toml` — уже читает `config["threads_db"]`, который теперь `../collector/data/state.db`. Проверить, что `WRITER_HOME / config["threads_db"]` резолвится в `collector/data/state.db`. Оставить как есть, только обновить docstring.

- [ ] **Step 5: править `.gitignore`**

В корневом `.gitignore` заменить блоки про базы:

```gitignore
# Производное: пересобирается прогонами из raw/, в git не кладётся
collector/data/derived.db
collector/data/derived.db-wal
collector/data/derived.db-shm

# Порождённое: невосстановимо (как raw/), бэкапится отдельно
collector/data/state.db
collector/data/state.db-wal
collector/data/state.db-shm

# Устаревшие пути (удаляются при миграции)
collector/db/leads.db
collector/db/ops.db
writer/threads.db
```

- [ ] **Step 6: написать pytest — `tests/conftest.py` и `tests/test_schema.py`**

Добавить `pytest` в dev-зависимости `collector/pyproject.toml`:

```toml
[dependency-groups]
dev = ["pytest>=8"]
```

`collector/tests/conftest.py`:

```python
"""Фикстуры: временные базы в каталоге, а не боевые data/."""

import pytest
import sqlite3
from pathlib import Path

import services.store as engine


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Две временные базы + соединение, подменяющие боевые data/."""
    monkeypatch.setattr(engine, "DERIVED", tmp_path / "derived.db")
    monkeypatch.setattr(engine, "STATE", tmp_path / "state.db")
    db = engine.connect()
    yield db
    db.close()
```

`collector/tests/test_schema.py`:

```python
"""Схема: порождённые таблицы переживают пересборку, view видят текущий прогон."""

import services.store as engine


def test_views_read_current_run(stores):
    """View orgs читает ровно тот прогон, на который указывает current_run."""
    db = stores
    run1 = engine.new_run(db, note="первый")
    run2 = engine.new_run(db, note="второй")
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'b1')", (run1,))
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'b2')", (run2,))
    db.commit()

    engine.activate_run(db, run1)
    assert db.execute("SELECT branch_id FROM orgs").fetchone()["branch_id"] == "b1"

    engine.activate_run(db, run2)  # второй вызов — обновление, не вторая строка
    assert db.execute("SELECT branch_id FROM orgs").fetchone()["branch_id"] == "b2"
    assert db.execute("SELECT count(*) FROM current_run").fetchone()[0] == 1, \
        "current_run обязан держать ровно одну строку"
```

(Остальные инварианты — «view не ссылается на attached state», «запись в одну базу» — добавятся в Task 3.)

- [ ] **Step 7: прогнать pytest**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: `test_views_read_current_run` PASS.

- [ ] **Step 8: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/store collector/services/store.py collector/services/metrics.py \
  writer/config.toml writer/config.py writer/thread_store.py .gitignore \
  collector/pyproject.toml collector/tests
git commit -m "feat(collector): две базы по классу данных, версионирование прогонов"
```

---

## Task 3: пересборка — прогон вместо DROP

`build.py` становится `services/pipeline/rebuild.py`: наполняет `*_all`-таблицы новым `run_id` и переключает `current_run` одной транзакцией. Удаляется приём «собрать в `.building` и подменить файл». `first_seen` больше не нужен (появляется сам первым прогоном).

**Files:**
- Create: `collector/services/pipeline/rebuild.py`
- Delete: `collector/build.py`
- Modify: `collector/services/enrich.py` (переписать вызовы `signals` → `signals_all` с `run_id`)
- Modify: `collector/services/resolve.py` (аналогично)
- Modify: `collector/services/score.py` (аналогично)
- Modify: `collector/tests/test_build.py` (новый pytest-файл)

**Interfaces:**
- Consumes: `engine.new_run`, `engine.activate_run`, `engine.finish_run`, `store.connect()`. Чтение сырья — локальным `load_pages()` (адаптер `storage` появится в Task 4 и придёт сюда в Task 7).
- Produces: `rebuild.run(ctx) -> dict` — единственная точка записи в derived, чистая функция от `raw/` и `config.toml`; `rebuild.html_of(page)`, `rebuild.load_llm_answers(db, run_id, kind)` — хелперы, которые `enrich` и `analyze` (Task 8) зовут вместо `build`.

- [ ] **Step 1: написать `services/pipeline/rebuild.py`**

Перенести логику `build.py` дословно, но: каждая запись получает `run_id`; `fill_suppression` удаляется (отказы живут в state и не пересобираются); `first_seen`-колонка не пишется; **внутренние чтения сборки идут в `*_all` с фильтром по строящемуся `run_id`, а не в view `current_run`** (см. разбор в Task 2). Обновлённые SQL-строки:

```python
"""Пересборка derived.db из raw/ — прогон вместо DROP. Сети здесь нет.

Чтение внутри сборки идёт в *_all с фильтром run_id: пока прогон строится,
current_run ещё указывает на прошлый прогон, и view orgs/companies/signals/
fetches вернули бы чужие данные. Продуктовые читатели смотрят в view текущего
прогона (только запрос отказов сменил префикс на state.suppression, Task 6/2).
"""

import gzip
import json
import re
import tomllib
from pathlib import Path

from services import enrich, resolve, score

RAW = Path("data/raw")
SCHEMA = Path("store/schema.sql")

GIS_LIST_URL = re.compile(r"2gis\.kz/([a-z]+)/rubric/(\d+)(?:/page/(\d+))?$")
GIS_FIRM_URL = re.compile(r"2gis\.kz/([a-z]+)/firm/(\d+)$")


def load_pages():
    """Сайдкары raw/, отсортированные. Временная локальная копия build.load_pages;
    Task 4 вводит services/storage.iter_pages, и rebuild переходит на него в Task 7."""
    pages = []
    for sidecar in sorted(RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue
        sha = sidecar.name.removesuffix(".json")
        page = RAW / f"{sha}.html.gz"
        if not page.exists():
            continue
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["sha"] = sha
        meta["path"] = page
        pages.append(meta)
    return sorted(pages, key=lambda p: (p["url"], p["sha"]))


def html_of(page):
    with gzip.open(page["path"], "rt", encoding="utf-8") as fh:
        return fh.read()


def load_llm_answers(db, run_id, kind):
    """Кэшированные ответы модели заданного вида.

    Источник — state.llm_answers (Task 8). Пока таблица не заполнена (до
    миграции Task 11) читает raw/*.llm.json тем же разбором, что build.py:
    kind берётся из поля answer, subject не нужен.
    """
    rows = db.execute(
        "SELECT subject, model, prompt, answer FROM state.llm_answers WHERE kind = ?",
        (kind,),
    ).fetchall()
    if rows:
        return [{"subject": r["subject"], "model": r["model"], "prompt": r["prompt"],
                 "answer": json.loads(r["answer"])} for r in rows]
    answers = []
    for path in sorted(RAW.glob("*.llm.json")):
        answer = json.loads(path.read_text(encoding="utf-8"))
        if answer.get("kind", "company_profile") == kind:
            answers.append(answer)
    return answers


def run(ctx):
    from services import store as engine
    ctx.log("пересборка из raw/")
    db = engine.connect()
    run_id = engine.new_run(db)
    pages = load_pages()
    # Пишем *все* таблицы нового прогона. Пока current_run не переключён,
    # читатели видят прежний прогон: ни один промежуточный коммит ниже не
    # трогает current_run, поэтому атомарность выдачи держится одним финальным
    # activate_run (одна UPDATE), а не одной большой транзакцией.
    try:
        fill_fetches(db, run_id, pages)
        fill_orgs(db, run_id, pages)
        fill_contacts(db, run_id, pages)
        resolve.resolve(db, run_id)
        enrich.enrich(db, run_id, pages, scoring_weights())
        fill_profiles(db, run_id)
        score.score_all(db, run_id, *ranking_config())
        engine.activate_run(db, run_id)   # публикация — атомарно, последней
        engine.finish_run(db, run_id)
    except BaseException:
        # Прогон брошен: current_run не переключён, читатели целы. Записи
        # нового run_id остаются в *_all сиротами — это и есть «отменённый
        # прогон», который не стал текущим.
        raise
    finally:
        db.close()
    ctx.progress(1, 1, "пересборка завершена")
    return {"run_id": run_id}
```

(Функции `fill_fetches`, `fill_orgs`, `fill_contacts`, `fill_profiles`, `scoring_weights`, `ranking_config` переносятся из `build.py` дословно, кроме `run_id`-параметра в INSERT и удаления `fill_suppression`; `load_pages`/`html_of` заданы выше. `fill_profiles` читает `llm_answers` уже из `state.llm_answers` в Task 8; пока — из `raw/*.llm.json` как раньше. `enrich.py` и `analyze.py` (Task 8) до сих пор `import build` — Task 6 переводит их на этот модуль, см. ниже.)

**Критично — как читает сборка.** У `fill_orgs`/`fill_contacts` чтения из базы нет (они пишут из `raw/`), поэтому их достаточно: INSERT в `orgs_all`/`contacts_all`/`fetches_all` с `run_id`. Но у `resolve`, `enrich`, `score` и `fill_profiles` **внутренние SELECT читают таблицы, которые стали view** `current_run`. Эти SELECT обязаны перейти на `*_all` с фильтром `run_id`. Ниже — точные правки по каждому файлу.

**`services/resolve.py`** — `load_branches` читает `orgs` и `contacts` (view). Перевести на `orgs_all`/`contacts_all` по `run_id`:

```python
def resolve(db, run_id):
    branches = load_branches(db, run_id)
    groups = group_branches(branches)
    write_companies(db, run_id, branches, groups)


def load_branches(db, run_id):
    """branch_id -> всё, что нужно для склейки. Отсортировано ради дампа."""
    rows = db.execute(
        "SELECT branch_id, org_id, org_name, name, city, rubric_id"
        " FROM orgs_all WHERE run_id = ? ORDER BY branch_id", (run_id,)
    ).fetchall()
    contacts = {}
    for branch_id, kind, handle in db.execute(
        "SELECT branch_id, kind, handle FROM contacts_all"
        " WHERE run_id = ? ORDER BY branch_id, kind, handle", (run_id,)
    ):
        contacts.setdefault(branch_id, []).append((kind, handle))
    # … далее без изменений …
```

`write_companies(db, run_id, branches, groups)`: INSERT в `companies_all (run_id, company_id, name_norm, domain, city, rubric_id)` и `company_links_all (run_id, company_id, branch_id, rule, confidence)`. **Убрать колонку `first_seen` и её `NULL`.**

**`services/enrich.py`** — три места читают базу:

- `site_signals` → `site_pages(db, run_id, pages)` и `fetched_at_of(db, run_id, url)`: `SELECT company_id, domain FROM companies_all WHERE run_id = ? AND domain IS NOT NULL ORDER BY company_id`; `SELECT fetched_at FROM fetches_all WHERE run_id = ? AND url = ?`.
- `instagram_signals` → `horizon = SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?`; `companies_by_username(db, run_id)` → `SELECT l.company_id, c.handle FROM company_links_all l JOIN contacts_all c ON c.branch_id = l.branch_id WHERE l.run_id = ? AND c.run_id = ? AND c.kind = 'instagram' ORDER BY l.company_id`.
- Все INSERT в `signals_all` получают `run_id`.

`enrich.enrich(db, run_id, pages, weights)` вызывает `site_signals(db, run_id, pages, weights)` и `instagram_signals(db, run_id, pages, weights)`.

**`services/enrich.py` — заменить `import build`.** В трёх функциях (`feeds_by_username`, `ig_answers`, `site_pages`) локальный `import build` с вызовами `build.html_of(...)` / `build.load_llm_answers(...)` перестанет резолвиться, когда Task 3 удалит `build.py`. Импорт остаётся локальным (кольцевой безопасно: `rebuild` импортирует `enrich`), но указывает на новый модуль:

```python
def site_pages(db, run_id, pages):
    from services.pipeline import rebuild   # локально: rebuild импортирует enrich
    ...
    yield company_id, url, rebuild.html_of(page)
```

Аналогично `feeds_by_username` (зовёт `rebuild.html_of`) и `ig_answers(db, run_id)` (зовёт `rebuild.load_llm_answers(db, run_id, "ig_signals")`). `rebuild.load_llm_answers` читает `state.llm_answers` (в Task 8; пока — `raw/*.llm.json`).

**`services/score.py`** — `score_all` читает `fetches` (горизонт), `companies`, `signals` (все view). Перевести на `*_all`:

```python
def score_all(db, run_id, config, rubrics):
    horizon = db.execute(
        "SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    signals = signals_by_company(db, run_id)
    for company_id, city, rubric_id, domain in db.execute(
        "SELECT company_id, city, rubric_id, domain FROM companies_all"
        " WHERE run_id = ? ORDER BY company_id", (run_id,)
    ).fetchall():
        # … без изменений …
```

`signals_by_company(db, run_id)` → `SELECT company_id, type, observed_at, weight, quote, url FROM signals_all WHERE run_id = ? ORDER BY company_id, type, observed_at, url`. INSERT в `scores_all (run_id, company_id, fit_score, intent_score, breakdown)`.

**`rebuild.fill_profiles`** — карта компаний читает view `companies`. Перевести на `companies_all` по `run_id`:

```python
companies = {
    (name, city): company_id
    for company_id, name, city in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city"
        " FROM companies_all c"
        " LEFT JOIN company_links_all l ON l.company_id = c.company_id"
        "   AND l.run_id = ? AND l.rule = 'self'"
        " LEFT JOIN orgs_all o ON o.branch_id = l.branch_id AND o.run_id = ?"
        " WHERE c.run_id = ?",
        (run_id, run_id, run_id),
    )
}
```

INSERT в `profiles_all (run_id, company_id, model, industry, size_hint, has_sales_team, why_now, quote, confidence)`.

> Почему это необходимо: если оставить чтение через view, первый прогон (пустой `current_run`) вернёт ноль строк, а последующие — данные **прошлого** прогона. Критерий приёмки «то же содержимое» не выполнится ни при том, ни при другом. Именно эта правка — суть пересборки прогоном.

- [ ] **Step 2: написать pytest `tests/test_build.py`**

Проверка «сборка пишет и читает в рамках своего run_id, а не чужого»:

```python
"""Пересборка: прогон читает и пишет в рамках своего run_id, не трогая чужой."""

import services.store as engine


def test_rebuild_reads_own_run(stores):
    """Внутренние чтения сборки идут в *_all по run_id, а не в view current_run.

    Прогон 2 строится, пока current_run указывает на прогон 1: компания из
    прогона 1 не должна попадать в чтения сборки прогона 2.
    """
    db = stores
    run1 = engine.new_run(db)
    run2 = engine.new_run(db)
    db.execute("INSERT INTO companies_all (run_id, company_id, domain) VALUES (?, 'c1', 'a.kz')",
               (run1,))
    engine.activate_run(db, run1)   # опубликован прогон 1
    # чтение «в рамках строящегося прогона» 2 ничего не видит из прогона 1
    rows = list(engine.build_read(db, run2, "companies"))
    assert rows == [], f"чтение прогона 2 подхватило данные прогона 1: {rows}"
    # а чтение прогона 1 их видит
    assert [r["company_id"] for r in engine.build_read(db, run1, "companies")] == ["c1"]


def test_build_does_not_publish_until_activate(stores):
    """До activate_run выдача (view) показывает прежний прогон, не строящийся."""
    db = stores
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (1, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (2, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm) VALUES (1, 'c1', 'А')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm) VALUES (2, 'c2', 'Б')")
    engine.activate_run(db, 1)   # опубликован прогон 1
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c1"]

    # прогон 2 построен, но ещё не опубликован — выдача по-прежнему c1
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c1"]
    engine.activate_run(db, 2)   # публикация
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c2"]
```

(Полноценный поведенческий тест «два прогона на снимке raw/ идентичны» добавляется в Task 5 вместе с переездом `check_rebuild_is_identical`.)

- [ ] **Step 3: прогнать pytest и пересборку на живых данных**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: зелёный. Затем вручную убедиться, что пересборка из существующего raw/ даёт те же числа:

```bash
cd collector
uv run python -c "
from services.pipeline import rebuild
from services import store
print(rebuild.run(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None, check_cancelled=lambda: None)))
"
```

Ожидается: `{"run_id": 1}`. Затем сверить, что view текущего прогона дают эталонные числа (проверка критерия приёмки):

- [ ] **Step 4: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/pipeline/rebuild.py collector/services/enrich.py \
  collector/services/resolve.py collector/services/score.py \
  collector/tests/test_build.py
git rm collector/build.py
git commit -m "feat(collector): пересборка прогоном вместо DROP, first_seen убран"
```

---

## Task 4: адаптер хранилища сырья

Доступ к `raw/` идёт через `services/storage.py`, а не через `Path` по всему коду. Единственная реализация — локальная папка, ровно с нынешним поведением (`<sha1(url)>.html.gz` + сайдкар JSON). Переезд на R2/MinIO станет вторым файлом рядом.

**Files:**
- Create: `collector/services/storage.py`
- Modify: `collector/services/fetch.py` (использовать адаптер для чтения/записи)
- Create: `collector/tests/test_storage.py`

**Interfaces:**
- Produces: `storage.put(url, body, meta) -> sha`; `storage.get(sha) -> str` (распакованный текст); `storage.exists(url) -> bool`; `storage.iter_pages()` (сайдкары снимка, как `build.load_pages`). `storage.RAW` — путь к `data/raw`.
- Consumes: ничего.

- [ ] **Step 1: написать `services/storage.py`**

```python
"""Адаптер хранилища сырья. Единственная реализация — локальная папка.

Переезд на объектное хранилище (R2/MinIO) — второй файл с тем же интерфейсом:
схема и разбор не знают, откуда приходят страницы. raw/ невосстановимо:
страница удаляется, перекачать нельзя.
"""

import gzip
import hashlib
import json
from pathlib import Path

RAW = Path("data/raw")


def _paths(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return RAW / f"{h}.html.gz", RAW / f"{h}.json"


def put(url, body, meta):
    """Записать страницу + сайдкар. Сайдкар пишется последним: страница без него
    считается недокачанной и берётся заново (иначе потерялся бы final_url)."""
    page_path, sidecar_path = _paths(url)
    RAW.mkdir(exist_ok=True)
    with gzip.open(page_path, "wt", encoding="utf-8") as fh:
        fh.write(body)
    sidecar_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha1(url.encode()).hexdigest()


def get(sha):
    """Распакованный текст страницы по sha."""
    path = RAW / f"{sha}.html.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def exists(url):
    page_path, sidecar_path = _paths(url)
    if not (page_path.exists() and sidecar_path.exists()):
        return False
    landed = json.loads(sidecar_path.read_text(encoding="utf-8"))["final_url"]
    return "captcha" not in landed.lower()


def iter_pages():
    """Сайдкары снимка, отсортированные: порядок вставки задаёт содержимое дампа."""
    pages = []
    for sidecar in sorted(RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue  # .serp.json, .llm.json — кэш другого рода
        sha = sidecar.name.removesuffix(".json")
        page = RAW / f"{sha}.html.gz"
        if not page.exists():
            continue
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["sha"] = sha
        meta["path"] = page
        pages.append(meta)
    return sorted(pages, key=lambda p: (p["url"], p["sha"]))
```

- [ ] **Step 2: переписать `fetch.py` и `rebuild.load_pages` на адаптер**

`fetch.get` использует `storage.put`/`storage.get`/`storage.exists` вместо прямых `Path` операций. `fetch.is_cached(url)` → `storage.exists(url)`; `fetch.final_url(url)` читает сайдкар через адаптер. `_paths` убирается.

**`rebuild.load_pages()` делегирует адаптеру.** В Task 3 сборка читала `data/raw` локальным `load_pages()`. Теперь единственным источником сырья становится `storage`, и `rebuild.load_pages` сводится к одному вызову (иначе у теста `test_two_runs_identical` не было бы точки подмены пути):

```python
def load_pages():
    from services import storage
    return storage.iter_pages()
```

Так `rebuild` читает `storage.RAW`, и `monkeypatch.setattr(storage, "RAW", ...)` перенаправляет сборку во временный снимок. `rebuild.html_of(page)` остаётся (читает `page["path"]` из сайдкара, который возвращает `storage.iter_pages`).

- [ ] **Step 3: написать pytest `tests/test_storage.py`**

```python
"""Адаптер: put → get возвращает то же; exists не врёт."""

import gzip
import json
from pathlib import Path

import services.storage as storage


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://2gis.kz/almaty/rubric/653/page/7"
    sha = storage.put(url, "<html>привет</html>",
                      {"url": url, "final_url": url, "status": 200,
                       "fetched_at": "2026-08-12T09:14:03Z"})
    assert storage.get(sha) == "<html>привет</html>"
    assert storage.exists(url) is True
    assert storage.exists("https://2gis.kz/almaty/rubric/653/page/99") is False


def test_captcha_sidecar_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RAW", tmp_path)
    url = "https://2gis.kz/almaty/rubric/653"
    storage.put(url, "x", {"url": url, "final_url": "https://captcha.example", "status": 200})
    assert storage.exists(url) is False, "заглушка капчи не считается страницей"
```

- [ ] **Step 4: прогнать pytest**

```bash
cd collector
uv run pytest tests/test_storage.py -v
```

Ожидается: 2 PASS.

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/storage.py collector/services/fetch.py collector/tests/test_storage.py
git commit -m "feat(collector): адаптер хранилища сырья services/storage.py"
```

---

## Task 5: pytest — перенос `scripts/check.py`

Раздел = файл в `collector/tests/`. Ассерты переносятся дословно; меняется обёртка (pytest вместо самописного реестра). `scripts/check.py` удаляется. `parsers` остаётся единственным, работающим на чистом клоне.

**Files:**
- Create: `collector/tests/test_parsers.py`, `test_raw.py`, `test_runs.py`, `test_operations.py`, `test_resolve.py`, `test_signals.py`, `test_scores.py`, `test_leads.py`, `test_web.py`, `test_jobs.py`
- Delete: `collector/scripts/check.py`
- Modify: `collector/tests/conftest.py` (фикстуры снимка raw/)

**Interfaces:**
- Produces: pytest-функции `test_*` по каждому разделу.
- Consumes: `storage.iter_pages`, `store.connect`.

- [ ] **Step 1: перенести `check_parsers` в `tests/test_parsers.py`**

Содержимое — функции `check_gis_rubric_parsing`, `check_gis_firm_parsing`, `check_link_unwrapping`, `check_ig_parsing` (и их хелперы `fixture_html`) из `scripts/check.py`, переименованные в `test_*` и объявленные pytest-функциями. Ассерты и фикстуры — дословно:

```python
"""Разбор источников на эталонных страницах: ни базы, ни сети, ни raw/."""

import gzip
from pathlib import Path

from services import enrich, sources

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures
GIS_RUBRIC_CITY, GIS_RUBRIC_ID = "almaty", "653"
GIS_FIRM_BRANCH = "70000001017502602"


def fixture_html(name):
    with gzip.open(FIXTURES / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def test_gis_rubric_parsing():
    state = sources.parse_initial_state(fixture_html("gis_rubric"))
    total, pages, current = sources.parse_search_meta(state)
    assert (total, pages, current) == (670, 56, 1)
    # … далее дословно из check.py …
```

Перенести каждую из 30 функций-проверок в соответствующий `tests/test_*.py`, переименовав `check_x` → `test_x`. Оставить порядок и содержимое ассертов без изменений. `check_raw`, `check_build` (в новой форме — `test_runs`), `check_collect`, `check_resolve`, `check_signals`, `check_scores`, `check_leads`, `check_web`, `check_jobs` — каждый в свой файл.

- [ ] **Step 2: `test_runs.py` — новое (спека §5)**

```python
"""Версионирование: во время открытой пересборки читатель видит прошлый прогон;
после COMMIT — новый; отменённый прогон не становится текущим."""

import sqlite3

import services.store as engine


def _seed(db, run_id, branch):
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (?, '2026-08-01T00:00:00Z')", (run_id,))
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, ?)", (run_id, branch))


def test_reader_sees_snapshot(stores):
    """Читатель на отдельном соединении видит прошлый прогон, пока писатель
    держит открытую транзакцию пересборки; после COMMIT — новый (WAL).

    Транзакция имитирует то, что делает rebuild.run: пишет новый прогон и
    переключает current_run внутри одного незакрытого BEGIN IMMEDIATE, а
    activate_run снаружи не зовётся (он коммитит сам)."""
    db = stores
    _seed(db, 1, "old")
    _seed(db, 2, "new")
    engine.activate_run(db, 1)
    db.commit()

    reader = sqlite3.connect(f"file:{engine.DERIVED}?mode=ro", uri=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE current_run SET run_id = 2")   # переключение до COMMIT
        assert reader.execute("SELECT branch_id FROM orgs").fetchone()[0] == "old", \
            "читатель увидел незакоммиченный прогон"
        db.commit()
        assert reader.execute("SELECT branch_id FROM orgs").fetchone()[0] == "new"
    finally:
        reader.close()


def test_rollback(stores):
    """activate прошлого прогона возвращает прежнюю выдачу."""
    db = stores
    _seed(db, 1, "v1")
    _seed(db, 2, "v2")
    engine.activate_run(db, 2)
    db.commit()
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v2"

    engine.activate_run(db, 1)   # откат к прогону 1
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v1"


def test_cancelled_run_not_current(stores):
    """Отменённый прогон остаётся в runs без finished_at и не становится текущим."""
    db = stores
    _seed(db, 1, "v1")
    engine.activate_run(db, 1)
    db.commit()

    run_id = engine.new_run(db)          # строящийся прогон
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'v2')", (run_id,))
    db.commit()
    # отменён: finish_run и activate_run не вызваны
    assert db.execute("SELECT run_id FROM current_run").fetchone()[0] == 1
    row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["finished_at"] is None
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v1"
```

- [ ] **Step 3: `test_operations.py` — реестр и граница «нет сети»**

```python
"""Граница сборки: rebuild.py не тянет сеть даже транзитивно.

Статический обход графа импортов: у rebuild и всего, что он импортирует,
в резолвленных модулях не должно быть services/fetch и scrapling.
"""
import ast
from pathlib import Path

# Корень collector/ — там лежат services/, store/, db/.
COLLECTOR = Path(__file__).resolve().parent.parent
FORBIDDEN = ("services/fetch", "scrapling")


def _resolve(root, node):
    """Кандидаты-пути, на которые может ссылаться Import/ImportFrom.

    `from services import enrich` означает модуль services/enrich.py (services —
    namespace-пакет без __init__.py). `from services.pipeline import X` — либо
    services/pipeline.py, либо X-подмодуль. Возвращаем несколько кандидатов;
    reachable_modules берёт только существующие.
    """
    out = []
    if isinstance(node, ast.Import):
        for n in node.names:
            out.append(root / (n.name.replace(".", "/") + ".py"))
    elif isinstance(node, ast.ImportFrom) and node.module:
        base = root / node.module.replace(".", "/")
        out.append(base.with_suffix(".py"))
        for n in node.names:
            out.append(root / (f"{node.module}.{n.name}".replace(".", "/") + ".py"))
    return out


def reachable_modules(entry):
    seen, stack = set(), [Path(entry)]
    while stack:
        path = stack.pop()
        path = path.resolve()
        if path in seen or not path.exists() or path.suffix != ".py":
            continue
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                stack.extend(_resolve(COLLECTOR, node))
    return seen


def test_rebuild_import_graph_has_no_network():
    from services.pipeline import rebuild
    modules = reachable_modules(rebuild.__file__)
    assert modules, "обход графа импортов не дошёл ни до одного модуля — walker сломан"
    for m in modules:
        rel = m.relative_to(COLLECTOR).as_posix()
        assert not any(bad in rel for bad in FORBIDDEN), \
            f"rebuild тянет сеть через {rel}"
```

> `test_all_operations_callable` (проверка, что каждое имя в `OPERATIONS` вызываемо) переносится в Task 9 — там, где `services/pipeline/__init__.py` впервые создаётся. В Task 5 `OPERATIONS` ещё не существует.

- [ ] **Step 4: перенести данные-зависимые разделы на фикстуры**

`check_raw`, `check_collect`, `check_leads`, `check_web` писались против настоящих `data/raw` и `db/leads.db` с конкретными числами — «дословно» их не перенести, ожидаемые значения не воспроизводятся пустой фикстурой. Их pytest-версии используют **снимок raw/ из двух эталонных фикстур** (`fixtures/gis_rubric.html.gz`, `fixtures/gis_firm.html.gz`), как в `test_two_runs_identical` (Task 14), и временные базы через фикстуру `stores`:

- `test_raw`: прогнать `_snapshot`-помощник (перенести из Task 14 в `conftest.py`), затем те же инварианты, что `check_raw` (сайдкар у каждой страницы, final_url непустой), но против снимка из двух страниц.
- `test_collect`: `check_plan_coverage`/`check_pagination_guard` требуют живого сырья 2GIS с пойманной подменой. В pytest их не воспроизвести фикстурой; **пропустить** через `pytest.mark.skip` с пояснением, что подмена ловится разбором `parse_search_meta` (покрыто в `test_parsers`), а не целостным прогоном.
- `test_leads`: `check_leads` читает `leads.csv` (продукт `export`). Запускать после `export.run` на временном снимке; эталонный порядок выдачи — из старого `leads.csv` до миграции (Task 11 сравнивает его вручную).
- `test_web`: перенести `check_web`, заменив путь к базе на `stores`-соединение и отказы на `state.suppression`.

`check_jobs_contract` (перенос в `test_jobs.py`) — правка ассерта пути: `metrics.threads_db_path().name` теперь `"state.db"`, а не `"threads.db"` (переписка переехала в `state.db`):

```python
assert metrics.threads_db_path().name == "state.db", \
    "путь state.db разошёлся с config.toml системы 2"
```

**`test_generated_tables_survive` (спека §5) — порождённые таблицы переживают пересборку:**

```python
def test_generated_tables_survive(stores):
    """Отказ, тред и ответ модели на месте после нового прогона."""
    db = stores
    # отказ
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000000', '2026-08-10', 'просил')")
    # тред + сообщение
    db.execute("INSERT INTO state.threads (thread_id, company_id, seed, created_at)"
               " VALUES ('+77010000001', 'c1', '{}', '2026-08-10')")
    db.execute("INSERT INTO state.messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000001', 'outgoing', 'привет', '2026-08-10', '2026-08-10')")
    # ответ модели
    db.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
               " VALUES ('company_profile', 'X | Y', 'm', 'p', '{}')")
    db.commit()
    # новый прогон: пересборка не трогает state.*
    run_id = engine.new_run(db)
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm)"
               " VALUES (?, 'c1', 'А')", (run_id,))
    engine.activate_run(db, run_id)
    assert db.execute("SELECT count(*) FROM state.suppression").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM state.messages").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM state.llm_answers").fetchone()[0] == 1
```

**Writer: pytest вместо `writer/scripts/check.py`.** Добавить `pytest` в `writer/pyproject.toml` dev-группу и перенести разделы `schema`/`threads`/`leads`/`prompt` в `writer/tests/`. `synthetic_leads_db` (бывший `check_leads`) переписывается под новую схему. Writer — отдельный uv-проект, `services.store` collector'а ему недоступен, но он может собрать соединение сам, разбив `collector/store/schema.sql` на две половины (та же логика, что `store._schema`), и передать его чистым функциям `leads_source.*` (они принимают соединение параметром). Схема читается из `collector/store/schema.sql`, а не из `CONFIG["leads_db"].parent / "schema.sql"` (который теперь указывает на несуществующий `data/schema.sql`):

```python
# writer/tests/conftest.py
import sqlite3
from pathlib import Path

import pytest

COLLECTOR_SCHEMA = Path(__file__).resolve().parent.parent.parent / "collector" / "store" / "schema.sql"


def _part(marker, text):
    start = text.index(f"-- {marker} --")
    stops = [text.index(m, start + 1) for m in ("-- DERIVED --", "-- END --")
             if text.find(m, start + 1) != -1]
    return text[start:min(stops)]


@pytest.fixture
def leads_db():
    """Соединение формы collector'а: derived + view + ATTACH state.suppression.

    STATE-блок целиком применять нельзя: его DDL без префикса создал бы таблицы
    в главной (derived). Writer для отбора читает из state только suppression —
    её и создаём с префиксом state. (threads/messages создаёт thread_store.connect.)
    """
    text = COLLECTOR_SCHEMA.read_text(encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_part("DERIVED", text))
    db.execute("ATTACH DATABASE ':memory:' AS state")
    db.executescript(
        "CREATE TABLE IF NOT EXISTS state.suppression ("
        "  handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)"
    )
    return db
```

В `tests/test_leads.py` — наполнение пяти случаев (`c_ok`/`c_phone`/…) через `*_all`-таблицы + `current_run` (напрямую `INSERT INTO current_run (id, run_id) VALUES (1, ?)`), отказ — в `state.suppression`, затем `leads_source.candidates(db, limit)` и те же ассерты F19/F21, что в `check_leads`. `writer/scripts/check.py` удаляется.

- [ ] **Step 5: удалить `scripts/check.py` и прогнать pytest**

```bash
cd collector
git rm scripts/check.py
uv run pytest tests/ -v
cd ../writer
uv run pytest tests/ -v
```

Ожидается: весь набор зелёный в обоих проектах. Раздел `parsers` работает на чистом клоне (без raw/ и баз).

- [ ] **Step 6: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/tests writer/tests writer/pyproject.toml
git rm collector/scripts/check.py writer/scripts/check.py
git commit -m "test: scripts/check.py -> pytest, раздел = файл; writer на новых базах"
```

---

## Task 6: экспорт CSV как операция

`report.py` становится `services/pipeline/export.py`: та же логика `build_leads`/`candidates`/`best_channel`, но возвращает dict-результат через `RunContext` и пишет CSV тем же путём `data/leads.csv`.

**Files:**
- Create: `collector/services/pipeline/export.py`
- Delete: `collector/report.py`
- Modify: `collector/services/leads.py`, `writer/leads_source.py` — они импортируют `report`; заменить импорт на `services.pipeline.export`.
- Create: `collector/tests/test_leads.py` (перенос `check_leads`)

**Interfaces:**
- Produces: `export.run(ctx, limit=DEFAULT_LIMIT) -> dict` (число лидов, путь). `export.build_leads(db, limit)`, `export.best_channel`, `export.available`, `export.candidates`, `export.why_now`, `export.sources_of`, `export.DEFAULT_LIMIT` — те же сигнатуры, что у `report`.
- Consumes: `store.connect()`.

- [ ] **Step 1: написать `services/pipeline/export.py`**

Скопировать `report.py` дословно, переименовав `main()` → `run(ctx, limit=30)`. `DB = Path("db/leads.db")` заменяется на соединение через `store.connect()`. `OUT` остаётся `data/leads.csv`. `run` возвращает `{"wrote": n, "path": str(OUT)}`:

```python
def run(ctx, limit=DEFAULT_LIMIT):
    db = store.connect()
    try:
        leads = build_leads(db, limit)
        write_csv(leads)
        return {"wrote": len(leads), "path": str(OUT)}
    finally:
        db.close()
```

**`write_csv` не вызывает `sys.exit` при пустой выдаче.** Исходный `report.py` на пустых лидах делал `sys.exit(...)` — это `BaseException`, которое `_execute` воркера (Task 9) ловит как `Exception` и не перехватит: экспорт-джоба застряла бы в «running». Вместо этого `write_csv` пишет пустой CSV и возвращает ноль, а диагностика идёт через `ctx.log`:

**Единственное отступление от «дословно» — запрос отказов.** В `report.py` отказы читаются `SELECT handle FROM suppression` в двух местах: `build_leads` (`report.py:52`) и `available` (`report.py:228`). В новой схеме таблицы `suppression` в derived **нет** — она живёт как `state.suppression` (view не может ссылаться на attached базу, спека §1). Поэтому оба запроса меняются на `SELECT handle FROM state.suppression`. Это сознательное исключение из «читающий код не меняется»: правило держится для `orgs`/`companies`/`signals`/`scores`, но фильтр отказов обязан сменить префикс. `report.py:52` и `report.py:228` — единственные места с этим изменением.

> Остальные читатели отказов: `store/lead.py::suppression_handles` уже использует `state.suppression` (Task 2), `writer/leads_source.py::suppression_handles` — отдельная копия, её правит Task 2-в-writer (см. ниже в Task 6 Step 2).

- [ ] **Step 2: править импортёры**

Все, кто импортировал `report`, переходят на `export`:

- `collector/services/leads.py`: `import report` → `from services.pipeline import export as report`.
- `collector/routes/leads.py`: `import report` → `from services.pipeline import export as report`.
- `collector/api.py`: `import report` (строка 14) и использование `report.best_channel` в `demo()` → `from services.pipeline import export as report`.

`writer/leads_source.py` не импортирует `report` (вторая копия запроса) — оставить.

- [ ] **Step 3: перенести `check_leads` в `tests/test_leads.py`**

Файл создаётся в Task 5 как фикстурный (двухстраничный снимок); здесь — как тест `export.run` на этом снимке. «Дословно» из `check.py` не переносится: ассерты вроде `len(leads) >= 30` невоспроизводимы двумя страницами фикстур. Вместо этого проверить контракт экспорта на снимке: `export.run` пишет `data/leads.csv` и возвращает число лидов; у каждого лида `why_now` и рабочий канал; отказ из `state.suppression` убирает лид (F21):

```python
def test_export_filters_refusals(stores, tmp_path, monkeypatch):
    """F21: отказ из state.suppression убирает канал, а без канала — лид."""
    db = stores
    # опубликованный прогон с одной компанией и каналом
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (1, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city, domain)"
               " VALUES (1, 'c1', 'Ромашка', 'almaty', NULL)")
    db.execute("INSERT INTO company_links_all (run_id, company_id, branch_id, rule, confidence)"
               " VALUES (1, 'c1', 'b1', 'self', 1.0)")
    db.execute("INSERT INTO orgs_all (run_id, branch_id, name, city, rubric_id)"
               " VALUES (1, 'b1', 'Ромашка', 'almaty', '653')")
    db.execute("INSERT INTO contacts_all (run_id, branch_id, kind, handle)"
               " VALUES (1, 'b1', 'whatsapp', '+77010000001')")
    db.execute("INSERT INTO scores_all (run_id, company_id, fit_score, intent_score, breakdown)"
               " VALUES (1, 'c1', 5.0, 6.0, '[]')")
    engine.activate_run(db, 1)
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000001', '2026-08-10', 'просил')")
    db.commit()
    leads = export.build_leads(db, limit=10)
    assert leads == [], "отказ обязан убрать лида из выдачи (F21)"
```

- [ ] **Step 4: прогнать pytest**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: зелёный, `test_leads` PASS.

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/pipeline/export.py collector/services/leads.py \
  collector/routes/leads.py collector/tests/test_leads.py
git rm collector/report.py
git commit -m "feat(collector): report.py -> операция export, выдача не меняется"
```

---

## Task 7: операции сбора (collect) и пробы (probe)

`scripts/collect.py` становится `services/pipeline/collect.py` с операциями `gis`, `sites`, `instagram`; `services/probes/*` собираются в `services/pipeline/probe.py`. Обе — функции с `RunContext`, прогрессом и кооперативной отменой, возвращающие dict.

**Files:**
- Create: `collector/services/pipeline/collect.py`
- Create: `collector/services/pipeline/probe.py`
- Delete: `collector/scripts/collect.py`, `collector/services/probes/{gis_list,gis_firm,gis_rubrics,serp}.py`
- Modify: `collector/services/jobs.py` (STEPS ссылаются на операции — Task 9)
- Create: `collector/tests/test_operations.py` (расширить: операции не падают на пустом состоянии)

**Interfaces:**
- Produces: `collect.gis(ctx) -> dict`, `collect.sites(ctx) -> dict`, `collect.instagram(ctx) -> dict`, `probe.gis_list(ctx) -> dict`, `probe.gis_firm(ctx) -> dict`, `probe.gis_rubrics(ctx) -> dict`, `probe.serp(ctx) -> dict`. Все принимают **ровно один аргумент `ctx: RunContext`** — это контракт воркера (`OPERATIONS[name](ctx)` в Task 9). Параметры пробы берутся из config.toml (по умолчанию первая рубрика/город) или из `ctx`.
- Consumes: `storage`, `fetch`, `sources`, `store.connect` (для доменов/аккаунтов).

- [ ] **Step 1: написать `services/pipeline/collect.py`**

Перенести `collect_org_lists`, `collect_firm_cards`, `collect_sites`, `collect_instagram` и их хелперы из `scripts/collect.py` дословно, но: вместо `print` — `ctx.log(...)` и `ctx.progress(current, total, label)`; вместо `sys.exit` при смерти сессии — поднять `RuntimeError`; между единицами работы — `ctx.check_cancelled()`. `site_domains`/`instagram_accounts` читают домены из view через `store.connect()`, а не из `db/leads.db`. Возвращают dict вроде `{"collected": n, "skipped": m}`.

```python
def sites(ctx):
    from services import store as engine
    db = engine.connect()
    try:
        domains = [r[0] for r in db.execute(
            "SELECT DISTINCT domain FROM companies WHERE domain IS NOT NULL"
            " ORDER BY domain")]
    finally:
        db.close()
    budget = Budget(None)   # без потолка: только главные страницы, дедуп по raw/
    collected = skipped = 0
    for number, domain in enumerate(domains, 1):
        ctx.check_cancelled()
        try:
            site_page(budget, domain)
            collected += 1
        except Exception:
            skipped += 1
        ctx.progress(number, len(domains), "главные страницы")
    return {"collected": collected, "skipped": skipped}
```

(Полностью — переносом тела `collect_sites`; `Budget` из `scripts/collect.py` сохраняется дословно, но каждая операция сбора строит свой `Budget(None)` внутри и `ctx.check_cancelled()` вызывается в циклах. `gis`/`instagram` — аналогично.)

- [ ] **Step 2: написать `services/pipeline/probe.py`**

Перенести `collect`/`search` из четырёх `services/probes/*` в один модуль как функции, возвращающие dict вместо `print`+`jsonl`. Каждая — **одноаргументная** `fn(ctx)` (контракт воркера): параметры берутся из config.toml по умолчанию, ответ — dict. `sources` импортируется на уровне модуля, `tomllib` — stdlib:

```python
"""Пробы: посмотреть источник глазами. Одна операция на каждую пробу, fn(ctx)."""
import tomllib
from pathlib import Path

from services import sources

CONFIG = Path("config.toml")


def gis_list(ctx):
    """Пробная рубрика: первая из include на первом городе. Ответ — dict."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    city = config["cities"][0]
    rubric = config["rubrics"]["include"][0]
    rows = collect_list(city, rubric, ctx)   # перенос gis_list.collect с ctx
    return {"rubric": rubric, "city": city, "rows": rows}
```

`gis_firm(ctx)`/`gis_rubrics(ctx)`/`serp(ctx)` — аналогично: подставить первое разумное значение из config, вернуть dict. Никакие аргументы кроме `ctx` операции не принимают.

- [ ] **Step 3: расширить `tests/test_operations.py`**

```python
def test_collect_ops_accept_runcontext():
    """Операции сбора принимают RunContext и возвращают dict."""
    from services.pipeline import collect
    ctx = DummyContext()
    # не запускаем сеть — только проверяем, что сигнатуры живы
    assert callable(collect.gis) and callable(collect.sites) and callable(collect.instagram)


class DummyContext:
    def progress(self, current, total, label): ...
    def log(self, message): ...
    def check_cancelled(self): ...
```

- [ ] **Step 4: прогнать pytest**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: зелёный.

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/pipeline/collect.py collector/services/pipeline/probe.py \
  collector/tests/test_operations.py
git rm collector/scripts/collect.py collector/services/probes/gis_list.py \
  collector/services/probes/gis_firm.py collector/services/probes/gis_rubrics.py \
  collector/services/probes/serp.py
git commit -m "feat(collector): сбор и пробы — операции с RunContext вместо скриптов"
```

---

## Task 8: операции анализа (analyze) и перенос ответов модели в state

`scripts/classify.py` + `scripts/classify_ig.py` становятся `services/pipeline/analyze.py` с операциями `profile` и `ig_signals`. Оплаченные ответы модели переезжают из `raw/*.llm.json` в `state.llm_answers` (миграция — Task 11; здесь — чтение из state при наличии, иначе из raw).

**Files:**
- Create: `collector/services/pipeline/analyze.py`
- Delete: `collector/scripts/classify.py`, `collector/scripts/classify_ig.py`
- Modify: `collector/services/pipeline/rebuild.py` (`fill_profiles` читает `state.llm_answers`)
- Modify: `collector/services/enrich.py` (`ig_answers` читает `state.llm_answers`)

**Interfaces:**
- Produces: `analyze.profile(ctx) -> dict`, `analyze.ig_signals(ctx) -> dict`. `analyze.cache_path(model, prompt) -> Path`, `analyze.structured_model(...)` (общий клиент).
- Consumes: `store.connect()`, `state.llm_answers`.

- [ ] **Step 1: написать `services/pipeline/analyze.py`**

Перенести `classify.py` и `classify_ig.py` в один модуль. Обе операции получают `RunContext` и кладут ответы в `state.llm_answers` через `store_answer`. Полный текст:

```python
"""Анализ: один вызов модели на компанию/аккаунт. Ответы кэшируются в state.llm_answers.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента.
"""

import hashlib
import json
import os
import sys
import tomllib
from pathlib import Path

from langchain_openrouter import ChatOpenRouter

CONFIG = Path("config.toml")
RAW = Path("data/raw")

ANSWER_KIND = "company_profile"
IG_ANSWER_KIND = "ig_signals"
MAX_RETRIES = 2
CAPTION_CHARS = 700
REASONING = {"enabled": False}

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По данным о компании определи, "
    "нужна ли ей помощь с привлечением клиентов ПРЯМО СЕЙЧАС, и обоснуй это "
    "дословной цитатой с её сайта. Не выдумывай фактов: если для поля нет "
    "основания в данных, верни null. Отвечай по-русски."
)

IG_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Тебе дают подписи к постам "
    "инстаграм-аккаунта компании. Найди те, где компания САМА собирает заявки "
    "руками: зовёт написать в директ или WhatsApp, оставить контакт, звонит "
    "номером в подписи; объявляет акцию или скидку; ищет менеджера по продажам. "
    "Подписи бывают на русском и на казахском — разбирай оба. "
    "Описание услуги — не призыв: «подготовка юридических консультаций» это "
    "услуга, а «запишитесь на консультацию» — призыв. "
    "Ничего не выдумывай: quote обязана быть дословной фразой из подписи. "
    "Не нашёл ничего — верни пустой список."
)


def profile(ctx):
    from services import store as engine
    from schemas.company_profile import CompanyProfile
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
        companies = top_companies(db, config["top_n"])
        site_text = site_texts(db)
        llm = structured_model(config["model"], CompanyProfile)
        spent = 0
        for number, company in enumerate(companies, 1):
            ctx.check_cancelled()
            prompt = build_prompt(company, site_text.get(company["company_id"], ""), config)
            subject = f"{company['name']} | {company['city']}"   # строка, не кортеж
            if not answered(db, ANSWER_KIND, subject, config["model"], prompt):
                answer = llm.invoke([("system", SYSTEM), ("human", prompt)])
                store_answer(db, ANSWER_KIND, subject, config["model"], prompt,
                             {"profile": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(companies), "профили")
        return {"companies": len(companies), "new_calls": spent}
    finally:
        db.close()


def ig_signals(ctx):
    from services import store as engine
    from schemas.ig_signals import IgSignals
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
        accounts = feed_accounts()
        if not accounts:
            return {"accounts": 0, "new_calls": 0}
        llm = structured_model(config["model"], IgSignals)
        spent = failed = 0
        for number, (username, posts) in enumerate(accounts, 1):
            ctx.check_cancelled()
            prompt = ig_prompt(username, posts)
            subject = username
            if not answered(db, IG_ANSWER_KIND, subject, config["model"], prompt):
                try:
                    answer = llm.invoke([("system", IG_SYSTEM), ("human", prompt)])
                    store_answer(db, IG_ANSWER_KIND, subject, config["model"], prompt,
                                 {"signals": answer.model_dump()["signals"]})
                    spent += 1
                except Exception:
                    failed += 1
            ctx.progress(number, len(accounts), "подписи инстаграма")
        return {"accounts": len(accounts), "new_calls": spent, "failed": failed}
    finally:
        db.close()


def store_answer(db, kind, subject, model, prompt, answer):
    """Ответ кладётся в state.llm_answers вместе с запросом: через месяц промпт
    будет другим, и без запроса нельзя понять, на что модель отвечала."""
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, subject, model, prompt, json.dumps(answer, ensure_ascii=False)),
    )
    db.commit()


def answered(db, kind, subject, model, prompt):
    """Есть ли уже оплаченный ответ на этот промпт.

    Источник истины — state.llm_answers (Task 8). Пока таблица не заполнена
    (до миграции Task 11), фолбэк на файловый кэш raw/*.llm.json. Ключ совпадает
    с store_answer: (kind, subject, model, prompt) — иначе анализ переспросил бы
    модель и задвоил оплаченные ответы после миграции.
    """
    hit = db.execute(
        "SELECT 1 FROM state.llm_answers WHERE kind = ? AND subject = ?"
        " AND model = ? AND prompt = ? LIMIT 1",
        (kind, subject, model, prompt),
    ).fetchone()
    if hit:
        return True
    return cache_path(model, prompt).exists()   # фолбэк до миграции


def structured_model(model, schema):
    """Модель с валидацией схемы: разбор ответа и повторы при невалидной схеме —
    на стороне LangChain. Схема параметром: у profile и ig_signals она разная."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY пуст. Поднять бэкенд: "
            "uv run --env-file .env uvicorn api:app --port 8787"
        )
    return ChatOpenRouter(
        model=model, temperature=0, max_retries=MAX_RETRIES, reasoning=REASONING,
    ).with_structured_output(schema, method="json_schema")


def cache_path(model, prompt):
    digest = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()
    return RAW / f"{digest}.llm.json"
```

> Остальные хелперы переносятся дословно из `classify.py`/`classify_ig.py`: `top_companies(db, limit)`, `site_texts(db)`, `build_prompt(company, text, config)`, `feed_accounts()`, `ig_prompt(username, posts)` (бывший `build_prompt` ig-версии), `username_of(url)`. Они читают view `companies`/`signals` (продуктовое чтение — корректно, анализ идёт по опубликованному прогону, а не во время сборки).

`subject` для profile — строка `"название | город"` из промпта (тот же ключ, что `company_of_prompt`); для ig_signals — логин. `rebuild.fill_profiles` и `enrich.ig_answers` читают через `rebuild.load_llm_answers(db, run_id, kind)` — функцию, уже определённую в Task 3: сначала `state.llm_answers`, а пока она не заполнена (до миграции Task 11) — `raw/*.llm.json` тем же разбором. **Переопределять `load_llm_answers` в этой задаче не нужно** — сигнатура `(db, run_id, kind)` сохраняется, чтобы call-site `enrich.ig_answers` из Task 3 не ломался.

- [ ] **Step 2: править `rebuild.py` и `enrich.py`**

`fill_profiles(db, run_id)` и `enrich.ig_answers(db, run_id)` используют `rebuild.load_llm_answers(db, run_id, kind)` (уже из Task 3) — отдельного переопределения нет:

```python
# rebuild.fill_profiles
for answer in rebuild.load_llm_answers(db, run_id, "company_profile"):
    ...

# enrich.ig_answers
for answer in rebuild.load_llm_answers(db, run_id, "ig_signals"):
    ...
```

`store_answer` (запись в `state.llm_answers`) определена один раз — в Task 8 Step 1; второй копии в проекте не должно быть.

- [ ] **Step 3: прогнать pytest**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: зелёный.

- [ ] **Step 4: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/pipeline/analyze.py collector/services/pipeline/rebuild.py \
  collector/services/enrich.py
git rm collector/scripts/classify.py collector/scripts/classify_ig.py
git commit -m "feat(collector): classify* -> операция analyze, ответы модели в state.llm_answers"
```

---

## Task 9: джобы — вызов функций вместо subprocess

`services/jobs.py` перестаёт запускать процессы. Воркер зовёт функции-операции через `asyncio.to_thread`, кооперативная отмена через флаг, результат и ошибка (тип + текст) ложатся в `jobs.result`/`jobs.error`. Прогресс приходит из `RunContext`, а не из парсинга `#progress`.

**Files:**
- Modify: `collector/services/jobs.py`
- Create: `collector/services/pipeline/__init__.py` (реестр `OPERATIONS`)
- Modify: `collector/tests/test_jobs.py`, `collector/tests/test_operations.py`

**Interfaces:**
- Produces: `jobs.enqueue(name, limit=None) -> job_id` (операция или пайплайн), `jobs.OPERATIONS`, `jobs.PIPELINES`, `RunContext` (`ctx.progress`, `ctx.log`, `ctx.check_cancelled`, `ctx.cancel()`). Джоба получает поля `result` (dict) и `error` (тип+текст).
- Consumes: операции из `services.pipeline`.

- [ ] **Step 1: написать `services/pipeline/__init__.py`**

```python
"""Реестр операций. Белый список argv был защитой от инъекции в shell;
реестр функций — та же защита по построению: имени нет в словаре, вызывать нечего."""

from services.pipeline import analyze, collect, export, probe, rebuild

OPERATIONS = {
    "collect.gis": collect.gis,
    "collect.sites": collect.sites,
    "collect.instagram": collect.instagram,
    "analyze.profile": analyze.profile,
    "analyze.ig_signals": analyze.ig_signals,
    "rebuild": rebuild.run,
    "export": export.run,
    "probe.gis_list": probe.gis_list,
    "probe.gis_firm": probe.gis_firm,
    "probe.gis_rubrics": probe.gis_rubrics,
    "probe.serp": probe.serp,
}

PIPELINES = {
    "discover": {"title": "Поиск новых лидов", "steps": ("collect.gis", "collect.sites",
                 "collect.instagram", "rebuild", "export")},
    "classify": {"title": "Обогащение и оценка", "steps": ("analyze.profile",
                 "analyze.ig_signals", "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}
```

- [ ] **Step 2: переписать `services/jobs.py`**

Схема `jobs` получает колонку `result TEXT` и оставляет `error TEXT` (теперь — `"Тип: сообщение"` вместо номера). Воркер:

```python
def make_context(job_id):
    global _current_ctx
    state = {"cancelled": False}

    def check_cancelled():
        if state["cancelled"]:
            raise _Cancelled()

    def progress(current, total, label):
        _update(job_id, progress=json.dumps({"current": current, "total": total,
                                             "label": label}, ensure_ascii=False))

    def log(message):
        _append_log(job_id, [message])
        events.publish({"type": "log", "job_id": job_id, "lines": [message]})

    ctx = SimpleNamespace(
        check_cancelled=check_cancelled, progress=progress, log=log,
        cancel=lambda: state.update(cancelled=True),
    )
    _current_ctx = (job_id, ctx)   # cancel(job_id) находит активный контекст
    return ctx, state


class _Cancelled(Exception):
    pass


async def _execute(job_id):
    names = json.loads(_raw_steps(job_id))   # json-строка имён из state.jobs
    for index, name in enumerate(names):
        _update(job_id, step=index)
        ctx, state = make_context(job_id)
        try:
            result = await asyncio.to_thread(OPERATIONS[name], ctx)
            _update(job_id, result=json.dumps(result, ensure_ascii=False))
        except _Cancelled:
            _finish(job_id, "cancelled")
            return
        except Exception as error:
            _finish(job_id, "failed", error=f"{type(error).__name__}: {error}")
            return
    _finish(job_id, "done", exit_code=0)


def _raw_steps(job_id):
    """Сырая json-строка steps из state.jobs (не через as_job, который её разбирает)."""
    with closing(connect()) as db:
        return db.execute("SELECT steps FROM state.jobs WHERE id = ?", (job_id,)).fetchone()[0]
```

`cancel(job_id)` теперь кооперативный — вместо `os.killpg(SIGKILL)` ставит флаг в активном контексте шага:

```python
def cancel(job_id):
    """Снимает queued мгновенно; running — кооперативно, между единицами работы."""
    record = job(job_id)
    if not record:
        raise KeyError(job_id)
    if record["status"] == "queued":
        _finish(job_id, "cancelled")
        return job(job_id)
    if record["status"] == "running" and _current_ctx and _current_ctx[0] == job_id:
        _current_ctx[1].cancel()   # флаг — операция проверит его в check_cancelled
    return job(job_id)
```

`_parse_progress`, `PROGRESS_PREFIX`, subprocess-хелперы удаляются. `STEPS` (argv) удаляется; `pipeline_steps` возвращает имена операций. `check_pipelines` проверяет, что каждое имя в `OPERATIONS`.

**Джобы живут в `state.db`, а не в отдельном `ops.db`.** `jobs.py` больше не держит свой `OPS_DB` и свою схему: таблица `jobs` создаётся схемой state (Task 2). Соединение даёт `store.connect()` (derived + ATTACH state), запись идёт в `state.jobs`:

```python
from services import store as engine

def connect():
    """Соединение для джоб: state.jobs живёт в state.db, attached к derived."""
    return engine.connect()
```

Все запросы `jobs.py` к таблице `jobs` получают префикс `state.` (как `suppression_handles` в `lead.py`). `COLUMNS` дополняется полем `result`. `enqueue_steps` кладёт в `steps` **плоский список имён операций** (`json.dumps(names)`); `_raw_steps` возвращает его строкой воркеру, а `as_job` форматирует для фронтенда как список `[{"name": n}]` (контракт `Job.steps: JobStep[]`, `command` исчез — argv больше нет):

```python
def as_job(row):
    record = dict(zip(COLUMNS, row))
    names = json.loads(record.pop("steps") or "[]")
    return {**record,
            "steps": [{"name": n, "command": n} for n in names],
            "step_count": len(names)}
```

**Реэкспорты для роутеров.** `routes/operations.py` и `routes/pipeline.py` обращаются к `jobs.OPERATIONS` и `jobs.PIPELINES`, поэтому они объявляются на уровне модуля, а не локально в `_execute`:

```python
from services.pipeline import OPERATIONS, PIPELINES

_current_ctx = None   # активный контекст для кооперативной отмены
```

`_execute` использует модульный `OPERATIONS`; `_current_ctx` выставляется в `make_context` при старте шага и читается `cancel`.

- [ ] **Step 3: обновить `tests/test_jobs.py`**

Переписать `check_jobs_lifecycle`/`check_jobs_cancel` на операции с фейковым `RunContext`. `steps` — список строк-имён (не кортежей, иначе `OPERATIONS[name]` с кортежем упадёт по нехэшируемости):

```python
def test_job_lifecycle(stores, tmp_path, monkeypatch):
    """Джоба доходит до done; результат операции попадает в state.jobs."""
    from services import jobs
    from services.pipeline import export as export_op
    monkeypatch.setattr(export_op, "OUT", tmp_path / "leads.csv")   # не трогать боевой CSV
    job_id = jobs.enqueue_steps("custom", "Тест", ["export"])
    asyncio.run(jobs.run_pending())
    record = jobs.job(job_id)
    assert record["status"] == "done"
    assert record["result"] is not None      # json результата export.run


def test_job_failure_is_typed(stores):
    from services import jobs
    job_id = jobs.enqueue_steps("custom", "Провал", ["нет_такой_операции"])
    asyncio.run(jobs.run_pending())
    record = jobs.job(job_id)
    assert record["status"] == "failed"
    assert "KeyError" in record["error"] or "нет_такой" in record["error"]
```

> `export.run` читает `OUT` как атрибут модуля (`export.OUT`) в момент вызова, поэтому `monkeypatch.setattr(export_op, "OUT", ...)` перенаправляет запись во временный файл и не затирает боевой `data/leads.csv` во время прогона pytest.

> Тесты зовут воркер внутри фикстуры `stores` (Task 2), которая подменяет `engine.DERIVED`/`STATE` на временные файлы — `jobs.connect()`/`store.connect()` открывают их, а не боевые `data/`. Никакой `OPS_DB` больше нет, и его не трогаем.

Плюс в `test_operations.py` (Task 9, реестр уже существует) — `test_all_operations_callable` и `test_no_write_module_opens_both_dbs`:

```python
def test_all_operations_callable():
    from services.pipeline import OPERATIONS
    for name, fn in OPERATIONS.items():
        assert callable(fn), name


def test_no_write_module_opens_both_dbs():
    """Ни один модуль записи не открывает обе базы напрямую (спека §1).

    Единственное место с обоими путями — services/store.py (ATTACH). Модули
    записи (rebuild/suppression/jobs/thread_store) пишут через store.connect()
    в свою схему и не держат оба файла сами; транзакция через две базы не
    атомарна, и такого по построению быть не должно.
    """
    from services import store as engine
    write_modules = [
        Path(engine.__file__).resolve().parent.parent / "services" / "pipeline" / "rebuild.py",
        Path(engine.__file__).resolve().parent.parent / "services" / "suppression.py",
        Path(engine.__file__).resolve().parent.parent / "services" / "jobs.py",
        Path(engine.__file__).resolve().parent.parent.parent / "writer" / "thread_store.py",
    ]
    for path in write_modules:
        text = path.read_text(encoding="utf-8")
        assert not ("derived.db" in text and "state.db" in text), \
            f"{path.name} открывает обе базы — нарушение атомарности"
```

> Тесты воркера и операций гоняют в фикстуре `stores` (Task 2), которая подменяет `engine.DERIVED`/`STATE` на временные файлы, поэтому `jobs.run_pending()`/`store.connect()` не трогают боевые `data/`.

- [ ] **Step 4: прогнать pytest**

```bash
cd collector
uv run pytest tests/ -v
```

Ожидается: зелёный.

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/jobs.py collector/services/pipeline/__init__.py \
  collector/tests/test_jobs.py collector/tests/test_operations.py
git commit -m "feat(collector): джобы зовут операции через asyncio.to_thread, кооперативная отмена"
```

---

## Task 10: HTTP-эндпоинты операций, прогонов и экспорта отказов

Новые роутеры: операции, история прогонов, откат, экспорт CSV отказов. Каталог `/api/pipeline` расширяется.

**Files:**
- Create: `collector/routes/operations.py`
- Create: `collector/routes/runs.py`
- Modify: `collector/routes/pipeline.py`
- Modify: `collector/routes/suppression.py`
- Modify: `collector/routes/jobs.py`
- Modify: `collector/api.py`

**Interfaces:**
- Produces: `GET /api/pipeline`, `POST /api/pipeline/{kind}`, `POST /api/operations/{name}`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`, `GET /api/runs`, `POST /api/runs/{id}/activate`, `GET /api/suppression/export.csv`, `GET /api/events`.
- Consumes: `jobs`, `engine.run_history`, `engine.activate_run`.

- [ ] **Step 1: `routes/operations.py`**

```python
"""Одиночные операции. Не «запусти скрипт», а «выполни операцию»."""

from fastapi import APIRouter, HTTPException
from services import jobs

router = APIRouter(prefix="/api/operations")


@router.post("/{name}")
async def start(name: str):
    if name not in jobs.OPERATIONS:
        raise HTTPException(404, f"нет операции {name}. Есть: {', '.join(jobs.OPERATIONS)}")
    job_id = jobs.enqueue(name)
    return {"job": jobs.job(job_id)}
```

- [ ] **Step 2: `routes/runs.py`**

```python
"""История прогонов и откат выдачи."""

from fastapi import APIRouter, HTTPException
from services import store as engine

router = APIRouter(prefix="/api/runs")


@router.get("")
def runs():
    db = engine.connect()
    try:
        return {"runs": engine.run_history(db)}
    finally:
        db.close()


@router.post("/{run_id}/activate")
def activate(run_id: int):
    db = engine.connect()
    try:
        exists = db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not exists:
            raise HTTPException(404, f"прогона {run_id} нет")
        engine.activate_run(db, run_id)
        return {"run_id": run_id}
    finally:
        db.close()
```

- [ ] **Step 3: править `routes/pipeline.py`** — каталог возвращает и пайплайны, и операции:

```python
@router.get("")
def catalogue():
    return {
        "pipelines": [{"kind": k, "title": p["title"], "steps": list(p["steps"])}
                      for k, p in jobs.PIPELINES.items()],
        "operations": sorted(jobs.OPERATIONS),
    }
```

- [ ] **Step 4: править `routes/suppression.py` и `services/suppression.py`**

Отказы живут только в `state.suppression`. `routes/suppression.py` заменяется целиком (сохраняет `GET`-список и `POST`-отказ, добавляет `GET /export.csv`):

```python
"""Отказы: одна запись в state.suppression; выгрузка CSV — по требованию."""

import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from store import lead as store
from schemas.refusal import Refusal
from services import suppression as service

router = APIRouter(prefix="/api/suppression")

@router.get("")
def refusals():
    db = store.connect()
    try:
        return store.refusals(db)
    finally:
        db.close()


@router.post("", status_code=201)
def refuse(refusal: Refusal):
    """Отказ — одна запись в state.suppression, двухфазной записи больше нет."""
    db = store.connect()
    try:
        added = service.refuse(db, refusal.handle, refusal.reason)
    finally:
        db.close()
    return {"handle": refusal.handle.strip(), "added": added}


@router.get("/export.csv")
def export_csv():
    db = store.connect()
    try:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["handle", "added_at", "reason"])
        for handle, added_at, reason in db.execute(
            "SELECT handle, added_at, reason FROM state.suppression"
            " ORDER BY added_at, handle"):
            writer.writerow([handle, added_at, reason])
        return StreamingResponse(
            iter([buffer.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=suppression.csv"})
    finally:
        db.close()
```

`services/suppression.py` переписывается: файл-источник `suppression.csv` и двухфазная запись удаляются, остаётся один путь записи в `state.suppression`:

```python
"""Отказы: единственный источник истины — state.suppression (невосстановимый слой)."""

from store import lead as store


def refuse(db, handle, reason):
    """Одна запись в state.suppression. Повтор не задваивается."""
    handle, reason = handle.strip(), reason.strip()
    if handle in store.suppression_handles(db):
        return False
    from datetime import date
    store.add_refusal(db, handle, reason, date.today().isoformat())
    return True
```

Удаляются `existing_handles()` (заменено `store.suppression_handles`), `append_refusal`, константа `SUPPRESSION`, импорт `csv`. `api.py::demo()`/`check_refusal_reaches_both_stores` (проверка «файл и база не разъехались») больше не нужны — см. Task 5 (тест на то, что отказ переживает пересборку, остаётся в `test_schema::test_generated_tables_survive`).

- [ ] **Step 5: править `routes/jobs.py`** — `POST /{job_id}/cancel` уже есть; `jobs.cancel` теперь кооперативный (задача 9), сигнатура не меняется.

- [ ] **Step 6: монтировать роутеры в `api.py`**

```python
from routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
app.include_router(operations.router)
app.include_router(runs.router)
```

- [ ] **Step 7: прогнать pytest и поднять веб**

```bash
cd collector
uv run pytest tests/ -v
uv run python -m uvicorn api:app --port 8787 &
sleep 3
curl -s localhost:8787/api/pipeline
curl -s localhost:8787/api/runs
kill %1
```

Ожидается: каталог с `pipelines` и `operations`; `{"runs": [...]}` с как минимум прогоном из Task 3.

- [ ] **Step 8: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/routes collector/api.py
git commit -m "feat(collector): эндпоинты операций, прогонов и экспорта отказов"
```

---

## Task 11: миграция четырёх хранилищ

Одноразовый `scripts/migrate.py`, удаляемый после применения. Переносит `db/leads.db` → derived (прогон №1), `db/ops.db` → `state.jobs`, `writer/threads.db` → `state.threads`/`state.messages`, `data/suppression.csv` → `state.suppression`, `raw/*.llm.json` → `state.llm_answers`.

**Files:**
- Create: `collector/scripts/migrate.py`
- Create: `collector/tests/test_migration.py`

**Interfaces:**
- Consumes: `store.connect`, `storage`.

- [ ] **Step 1: написать `scripts/migrate.py`**

```python
"""Одноразовый перенос старых хранилищ в data/state.db и data/derived.db.
Удаляется после применения."""

import csv
import json
import sqlite3
from pathlib import Path

from services import store as engine
from services import storage

# Пути старых хранилищ. Параметризуются, чтобы тесты гоняли миграцию на временных
# файлах; продакшн-запуск `uv run -m scripts.migrate` использует значения по умолчанию.
OLD_LEADS = Path("db/leads.db")
OLD_OPS = Path("db/ops.db")
OLD_THREADS = Path("../writer/threads.db")   # от collector/: ../writer/threads.db
OLD_SUPPRESSION = Path("data/suppression.csv")


def migrate(db=None, old_leads=OLD_LEADS, old_ops=OLD_OPS,
            old_threads=OLD_THREADS, old_suppression=OLD_SUPPRESSION):
    """Перенос всех хранилищ. db=None — открыть боевые data/; иначе — переданное."""
    own = db is None
    if own:
        db = engine.connect()
    try:
        migrate_ops(db, old_ops)
        migrate_threads(db, old_threads)
        migrate_suppression(db, old_suppression)
        migrate_llm(db, storage.RAW)
        migrate_leads(db, old_leads)
    finally:
        if own:
            db.close()


def migrate_leads(db, old):
    """db/leads.db -> derived, прогон №1 с note='миграция'."""
    if not old.exists():
        return
    src = sqlite3.connect(old)
    src.row_factory = sqlite3.Row
    run_id = engine.new_run(db, note="миграция")
    for table in ("fetches", "orgs", "contacts", "companies", "company_links",
                  "signals", "scores", "profiles"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        cols_wo_first_seen = [c for c in cols if c != "first_seen"]
        placeholders = ", ".join(["?"] * len(cols_wo_first_seen))
        all_cols = ", ".join(cols_wo_first_seen)
        rows = src.execute(f"SELECT {all_cols} FROM {table}")
        db.executemany(
            f"INSERT OR REPLACE INTO {table}_all (run_id, {all_cols})"
            f" VALUES (?, {placeholders})",
            [(run_id, *[r[c] for c in cols_wo_first_seen]) for r in rows],
        )
    engine.activate_run(db, run_id)
    engine.finish_run(db, run_id)
    db.commit()
    src.close()
```

(Функции `migrate_ops(db, old)`, `migrate_threads(db, old)`, `migrate_suppression(db, old)`, `migrate_llm(db, raw_dir)` переносят соответствующие таблицы в `state.*` с `INSERT OR REPLACE` — каждая принимает путь источника как параметр. Для `llm` — `subject` восстанавливается тем же разбором промпта, что `company_of_prompt`/`username_of`: первая строка `Компания: <название>`/`Город: <город>` → `(название, город)`; `Инстаграм: <логин>` → `<логин>`. Ответ, для которого компания не нашлась, не переносится (он и раньше молча игнорировался).)

- [ ] **Step 2: написать `tests/test_migration.py`**

```python
"""Миграция: перенос хранилищ; ответ для ненайденной компании не переносится."""

import csv
import json
import sqlite3
from pathlib import Path

import scripts.migrate as migrate
from services import store as engine


def _write_llm(raw_dir, kind, subject_line, model="deepseek/x", payload=None):
    """Сырой .llm.json с промптом, первая строка которого задаёт subject."""
    prompt = subject_line + "\nданные"
    body = {"kind": kind, "model": model, "prompt": prompt}
    body.update(payload or {})
    (raw_dir / f"{hash(prompt)}.llm.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")


def test_suppression_migrated(stores, tmp_path):
    csv_path = tmp_path / "suppression.csv"
    csv_path.write_text("handle,added_at,reason\n+77010000000,2026-08-10,просил\n",
                        encoding="utf-8")
    migrate.migrate(db=stores, old_suppression=csv_path)
    rows = stores.execute("SELECT handle, reason FROM state.suppression").fetchall()
    assert [(r["handle"], r["reason"]) for r in rows] == [("+77010000000", "просил")]


def test_llm_answer_for_unknown_company_not_migrated(stores, tmp_path):
    """Ответ с промптом про компанию, которой нет в базах, не переносится."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_llm(raw_dir, "company_profile",
               "Компания: НетТакая\nГород: almaty",
               payload={"profile": {"industry": "бухгалтерия"}})
    migrate.migrate(db=stores, old_leads=tmp_path / "nope.db",  # базы нет
                    old_ops=tmp_path / "nope.db", old_threads=tmp_path / "nope.db",
                    old_suppression=tmp_path / "nope.csv")
    migrate._llm_answers_from_dir(stores, raw_dir)  # прямой вызов переноса
    assert stores.execute("SELECT count(*) FROM state.llm_answers").fetchone()[0] == 0, \
        "ответ для ненайденной компании не должен переноситься"
```

> Тесты зовут подфункции миграции с явными путями, поэтому `migrate_*` обязаны принимать пути параметрами (как задано выше), а `migrate._llm_answers_from_dir(db, raw_dir)` — отдельная переносимая функция: собирает ответы из `raw_dir/*.llm.json`, восстанавливает `subject` и переносит только те, чья компания/аккаунт есть в базе. Пока `state.llm_answers`-таблица не заполнена (до Task 8 она была в raw/), перенос не потеряет оплаченные ответы.

- [ ] **Step 3: запустить миграцию на живых данных и сверить с критерием приёмки**

```bash
cd collector
uv run -m scripts.migrate
uv run python -c "
import sqlite3
db = sqlite3.connect('data/derived.db')
db.row_factory = sqlite3.Row
for t in ('companies','orgs','contacts','signals','profiles'):
    print(t, db.execute(f'SELECT count(*) FROM {t}').fetchone()[0])
"
```

Ожидается: `companies 1660`, `orgs 1867`, `contacts 7226`, `signals 1876`, `profiles 40`. Затем сверить порядок выдачи:

```bash
cd collector
uv run python -c "
from services.pipeline import export
from services import store
print(export.run(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None, check_cancelled=lambda: None)))
"
shasum -a 256 data/leads.csv
```

Ожидается: sha совпадает с эталоном из Global Constraints (снять эталон до миграции, если его ещё нет: `uv run report.py && shasum -a 256 data/leads.csv` на старой базе).

- [ ] **Step 4: удалить старые файлы после успешной миграции**

Старые базы и suppression.csv — данные (в gitignore или untracked): удаляются `rm -f`, не `git rm` (иначе git rm упадёт на неотслеживаемом файле). `migrate.py` — одноразовый скрипт, и `test_migration.py` проверял его: оба удаляются вместе (после применения миграции проверять больше нечего):

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
rm -f collector/db/leads.db collector/db/ops.db collector/data/suppression.csv writer/threads.db
git rm collector/scripts/migrate.py collector/tests/test_migration.py
rmdir collector/db 2>/dev/null || true   # каталог оставался только под данные
```

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git commit -m "feat(collector): миграция четырёх хранилищ в две базы, критерий приёмки соблюдён"
```

> `data/state.db` и `data/derived.db` в `.gitignore` — не коммитятся (это данные). Удаление `migrate.py`/`test_migration.py` и старых файлов уже в индексе после `git rm`/`rm -f`; стадия `git add` не нужна, но если `git commit` жалуется на пустой индекс — добавить `.gitignore` и docs-правки из Task 13.

---

## Task 12: фронтенд — операции, история прогонов, прогресс

Консоль получает вторую группу кнопок (операции), экран истории прогонов с откатом. Прогресс-бар начинает работать впервые: операции шлют `progress` через `RunContext`.

**Files:**
- Modify: `frontend/app/api.ts`
- Create: `frontend/components/OperationsPanel.tsx`
- Create: `frontend/components/RunsHistory.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/sourcing/page.tsx` (переход на `fetchCatalogue`)
- Modify: `frontend/app/writer/page.tsx` (переход на `fetchCatalogue`)

**Interfaces:**
- Consumes: `GET /api/pipeline` (новый формат), `POST /api/operations/{name}`, `GET /api/runs`, `POST /api/runs/{id}/activate`.

- [ ] **Step 1: `frontend/app/api.ts` — типы и функции**

```ts
export type Operation = { name: string };
export type Run = { run_id: number; started_at: string; finished_at: string | null;
  code_version: string | null; config_hash: string | null; note: string | null };

export type PipelineCatalogue = {
  pipelines: Pipeline[];
  operations: string[];
};

export function fetchCatalogue() {
  return json<PipelineCatalogue>("/api/pipeline");
}

export function startOperation(name: string) {
  return json<{ job: Job }>(`/api/operations/${encodeURIComponent(name)}`, { method: "POST" });
}

export function fetchRuns() {
  return json<{ runs: Run[] }>("/api/runs");
}

export function activateRun(runId: number) {
  return json<{ run_id: number }>(`/api/runs/${runId}/activate`, { method: "POST" });
}
```

Обновить `Pipeline` (каталог теперь `pipelines` + `operations`). Проверить использование `fetchPipelines` в `page.tsx`, `sourcing/page.tsx`, `writer/page.tsx` — перевести на `fetchCatalogue`.

- [ ] **Step 2: `frontend/components/OperationsPanel.tsx`**

```tsx
"use client";

import { useState } from "react";
import { startOperation } from "@/app/api";
import { useLive } from "./live";

export function OperationsPanel({ operations }: { operations: string[] }) {
  const { active } = useLive();
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  async function run(name: string) {
    setBusy(name);
    setFailure(null);
    try {
      await startOperation(name);
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="pipeline-actions">
      {operations.map((name) => (
        <button key={name} className="btn btn-quiet"
          disabled={!!active || busy === name} onClick={() => run(name)}>
          {busy === name ? "ставится…" : name}
        </button>
      ))}
      {failure && <p className="form-error">{failure}</p>}
    </div>
  );
}
```

- [ ] **Step 3: `frontend/components/RunsHistory.tsx`**

```tsx
"use client";

import { useEffect, useState } from "react";
import { activateRun, fetchRuns, type Run } from "@/app/api";

export function RunsHistory() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns().then((data) => setRuns(data.runs)).catch(() => undefined);
  }, []);

  async function rollback(runId: number) {
    setMessage(null);
    try {
      await activateRun(runId);
      setMessage(`выдача переключена на прогон ${runId}`);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  return (
    <section className="card">
      <h3>История прогонов</h3>
      <div className="jobs-list">
        {runs.map((run) => (
          <div key={run.run_id} className="jobs-row">
            <span className="jobs-title">
              прогон {run.run_id}{run.note ? ` · ${run.note}` : ""}
            </span>
            <span className="jobs-when mono">
              {run.finished_at ? run.finished_at.slice(0, 19) : "не завершён"}
            </span>
            <button className="btn btn-quiet" onClick={() => rollback(run.run_id)}>
              вернуть выдачу
            </button>
          </div>
        ))}
      </div>
      {message && <p className="hint">{message}</p>}
    </section>
  );
}
```

- [ ] **Step 4: `frontend/app/page.tsx`** — вставить панели

```tsx
const [catalogue, setCatalogue] = useState<PipelineCatalogue | null>(null);
useEffect(() => {
  fetchCatalogue().then(setCatalogue).catch(() => undefined);
}, []);
// …
<PipelineActions pipelines={catalogue?.pipelines ?? []} />
<OperationsPanel operations={catalogue?.operations ?? []} />
<JobMonitor />
{/* после «Истории запусков» */}
<RunsHistory />
```

**Step 5: мигрировать `sourcing/page.tsx` и `writer/page.tsx`.** Обе страницы звали `fetchPipelines()` (тип `Pipeline[]`), но Task 10 сменил `/api/pipeline` на `{pipelines, operations}`. Перевести на `fetchCatalogue()`:

- `frontend/app/sourcing/page.tsx`: `fetchPipelines().then((all) => setPipelines(all.filter((p) => p.kind !== "write")))` → `fetchCatalogue().then((c) => setPipelines(c.pipelines.filter((p) => p.kind !== "write")))`.
- `frontend/app/writer/page.tsx`: `fetchPipelines().then((all) => setPipelines(all.filter((p) => p.kind === "write")))` → `fetchCatalogue().then((c) => setPipelines(c.pipelines.filter((p) => p.kind === "write")))`.

(Без этого `.filter` на объекте `{pipelines, operations}` упадёт в рантайме.)

- [ ] **Step 6: проверить сборку фронта**

```bash
cd frontend
npm run build
```

Ожидается: сборка без ошибок типов. (Прогресс-бар уже читает `active.progress` в `JobMonitor`; теперь операции его шлют.)

- [ ] **Step 7: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add frontend/app/api.ts frontend/components/OperationsPanel.tsx \
  frontend/components/RunsHistory.tsx frontend/app/page.tsx \
  frontend/app/sourcing/page.tsx frontend/app/writer/page.tsx
git commit -m "feat(frontend): панель операций и история прогонов с откатом"
```

---

## Task 13: актуализация документации и зависимостей

Обновляются `CLAUDE.md`, README, docker-инструкции и `.gitignore`; pytest-зависимость фиксируется. Документация должна отражать: две базы, прогоны, операции, pytest вместо check.py, папку `store/`.

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `collector/Dockerfile`, `docker-compose.yml`
- Modify: `docs/ARCHITECTURE_v2.md` (источник схемы), `docs/SPEC.md`

**Interfaces:**
- Consumes: всё реализованное.

- [ ] **Step 1: править `CLAUDE.md`**

Секции «Архитектура collector/», «writer/», «Слои данных», «Команды» — заменить пути `db/leads.db`/`db/ops.db`/`writer/threads.db`/`data/suppression.csv` на `data/derived.db`/`data/state.db`, `store/schema.sql`, `services/store.py`; команды `build.py`/`report.py`/`scripts.check` → `uv run pytest`; добавить операции и прогоны. Править строку «Разделы check.py» на «pytest-файлы». Выполнить самопроверку объёма (< 2500 токенов).

- [ ] **Step 2: править `README.md`** — то же кратко.

- [ ] **Step 3: править `collector/Dockerfile` и `docker-compose.yml`** — убрать `uv run build.py`/`report.py` из команд, заменить на pytest/операции; смонтировать `data/` (обе базы + raw) вместо `db/`.

- [ ] **Step 4: править `docs/ARCHITECTURE_v2.md` §4** — новая схема (runs, current_run, `*_all`+view, state-таблицы), чтобы источник схемы совпадал с `store/schema.sql`.

- [ ] **Step 5: закоммитить**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add CLAUDE.md README.md collector/Dockerfile docker-compose.yml \
  docs/ARCHITECTURE_v2.md docs/SPEC.md
git commit -m "docs: две базы, прогоны, операции и pytest в документации"
```

---

## Task 14: финальная приёмка

Сквозная проверка по критерию приёмки спеки §6: два прогона подряд на неизменном сырье дают идентичное содержимое; выдача совпадает с эталоном; `rebuild` не ходит в сеть; ни одна операция не пишет в обе базы.

**Files:**
- Modify: `collector/tests/test_runs.py` (дополнить идентичностью двух прогонов)

- [ ] **Step 1: написать тест идентичности двух прогонов**

Два прогона на одном снимке `raw/` обязаны дать одинаковое содержимое `*_all`
(детерминизм rebuild — критерий §6). Тест строит временный снимок из двух эталонных
фикстур (`fixtures/gis_rubric.html.gz`, `fixtures/gis_firm.html.gz`), подменяет
`storage.RAW` и `engine.DERIVED`/`STATE` на временные файлы и гоняет `rebuild.run`
дважды, сравнивая дампы:

```python
"""Два прогона на неизменном сырье дают идентичное содержимое (спека §6)."""

import gzip
import hashlib
import json
import types
from pathlib import Path

import services.storage as storage
import services.store as engine
from services.pipeline import rebuild

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures
CONTEXT = types.SimpleNamespace(log=lambda *a: None,
                                progress=lambda *a: None,
                                check_cancelled=lambda: None)


def _snapshot(tmp_path):
    """Снимок raw/ из двух эталонных страниц 2GIS (рубрика + карточка)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    urls = [
        ("https://2gis.kz/almaty/rubric/653", "gis_rubric"),
        ("https://2gis.kz/almaty/firm/70000001017502602", "gis_firm"),
    ]
    for url, name in urls:
        sha = hashlib.sha1(url.encode()).hexdigest()
        with gzip.open(FIXTURES / f"{name}.html.gz", "rb") as src, \
             gzip.open(raw / f"{sha}.html.gz", "wb") as dst:
            dst.write(src.read())
        (raw / f"{sha}.json").write_text(json.dumps(
            {"url": url, "final_url": url, "status": 200,
             "fetched_at": "2026-08-12T09:14:03Z"}, ensure_ascii=False), encoding="utf-8")
    return raw


def _dump(db, table, run_id):
    """Строки *_all ровно одного прогона: иначе сравнение 1-го и 2-го прогонов
    сравнило бы данные прогона 1 с (прогон1 + прогон2)."""
    rows = db.execute(
        f"SELECT * FROM {table}_all WHERE run_id = ? ORDER BY rowid", (run_id,)
    ).fetchall()
    return [tuple(r) for r in rows]


def test_two_runs_identical(tmp_path, monkeypatch):
    raw = _snapshot(tmp_path)
    monkeypatch.setattr(storage, "RAW", raw)
    monkeypatch.setattr(engine, "DERIVED", tmp_path / "derived.db")
    monkeypatch.setattr(engine, "STATE", tmp_path / "state.db")

    db = engine.connect()
    rebuild.run(CONTEXT)
    run1 = db.execute("SELECT max(run_id) FROM runs").fetchone()[0]
    first = {t: _dump(db, t, run1) for t in ("fetches", "orgs", "contacts", "signals", "scores")}

    rebuild.run(CONTEXT)
    run2 = db.execute("SELECT max(run_id) FROM runs").fetchone()[0]
    second = {t: _dump(db, t, run2) for t in ("fetches", "orgs", "contacts", "signals", "scores")}

    for table in first:
        assert first[table] == second[table], \
            f"прогоны дали разные данные в {table}: {first[table]} vs {second[table]}"
    # второй прогон — отдельный run_id, а не перезапись первого
    run_ids = {r[0] for r in db.execute("SELECT run_id FROM runs")}
    assert len(run_ids) == 2, run_ids
    db.close()
```

> Тест полагается на то, что `rebuild.run` читает сырьё через `storage.RAW` (а не
> через собственную константу): Task 4 переводит `rebuild.load_pages()` на
> `storage.iter_pages()`, поэтому `monkeypatch.setattr(storage, "RAW", raw)`
> перенаправляет чтение во временный снимок. Если сборка читает `data/raw`
> напрямую, тест прочитал бы настоящие 464 МБ — это сигнал, что Task 4 не доведён.

- [ ] **Step 2: прогнать весь набор**

```bash
cd collector && uv run pytest tests/ -v
cd ../writer && uv run pytest tests/ -v
```

Ожидается: всё зелёное в обоих проектах.

- [ ] **Step 3: сверить выдать с эталоном**

```bash
cd collector
uv run python -c "
from services.pipeline import export
from services import store
export.run(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None, check_cancelled=lambda: None))
"
shasum -a 256 data/leads.csv
```

Ожидается: sha совпадает с эталоном из Global Constraints.

- [ ] **Step 4: проверить, что `rebuild` не ходит в сеть**

```bash
cd collector
uv run pytest tests/test_operations.py::test_rebuild_import_graph_has_no_network -v
```

Ожидается: PASS.

- [ ] **Step 5: закоммитить (если были правки тестов)**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/tests/test_runs.py
git commit -m "test(collector): идентичность двух прогонов и финальная приёмка"
```

---

## Self-Review

**Spec coverage:**
- §1 Две базы — Tasks 2, 11; отказы в базу + export.csv — Task 10 (включая переписывание `services/suppression.py` и сохранение `POST`-отказа); переписка в общей базе — Task 2 (+ writer ATTACH state); «ни одна операция не пишет в обе» — тест в Task 9. `store.connect()` применяет STATE-схему к собственной базе state.db, DERIVED+view — к derived, затем ATTACH (иначе таблицы state создались бы в derived).
- §2 Версионирование — Tasks 2, 3; view сохраняет читающий код — Task 2 (кроме двух запросов `suppression`→`state.suppression` в export и writer, Task 6/2); переключение выдачи/откат — Task 10; first_seen сам собой — Task 3.
- §3 Адаптер хранилища — Task 4 (включая перевод `rebuild.load_pages` на `storage.iter_pages`).
- §4 Операции вместо скриптов — Tasks 6, 7, 8, 9, 10; RunContext/реестр — Task 9; in-process исключение/типизированная ошибка — Task 9; «rebuild не ходит в сеть» статически+поведенчески — Tasks 5, 14.
- §5 pytest — Tasks 5, 6, 8, 9; writer/tests — Task 5; `test_generated_tables_survive` и граф-импортов — Task 5.
- §6 Критерий приёмки — Tasks 3, 11, 14.
- §7 Миграция — Task 11 (одноразовый `migrate.py` + `test_migration.py`, оба удаляются).
- §8 Фронтенд — Task 12.
- §9 Риски (нет атомарности между базами — тест; in-process исключение — обработчик) — Tasks 3, 9.

**Placeholder scan:** каждый шаг содержит конкретный код/команду; плейсхолдеров (`...`, «TBD») в коде нет. Оставшиеся `// …`/`# …` — эллипсисы в diff-фрагментах, не заменяющие исполняемый код.

**Type consistency:** `store.connect()` возвращает соединение с `state.`-префиксом и view текущего прогона; сборка читает `*_all` с фильтром `run_id` (Task 3), продукт — view (Task 2); `store.activate_run` — upsert ровно одной строки `current_run`. `analyze.profile/ig_signals` берут `ctx`, `store_answer` пишет строковый `subject`, `answered` проверяет кэш по `state.llm_answers`+фолбэк на файл. `RunContext` имеет `progress(current, total, label)`, `log(message)`, `check_cancelled()` — согласовано в Task 9 и используется в 6–8. Джобы живут в `state.jobs` через `store.connect()` (Task 9). `migrate_*` принимают пути параметрами (Task 11).
