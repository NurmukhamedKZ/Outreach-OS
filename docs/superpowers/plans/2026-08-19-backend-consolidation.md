# backend/ Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `collector/`, `writer/`, `sender/` under one `backend/` package with one venv, one `backend/main.py` entry point, and mirrored `routes/services/db/schemas` layout in both `collector` and `writer`; replace `writer/scripts/write.py` with a queued operation (`writer.outreach`) wired into the same job queue system 1 already uses.

**Architecture:** `collector`, `writer`, `sender` become real Python packages (`__init__.py` everywhere) under a single `backend/pyproject.toml` venv, imported with absolute paths (`collector.services.jobs`, `writer.routes.threads`). `backend/main.py` puts `backend/` on `sys.path` once and imports the FastAPI app assembled in `collector/api.py`; that file is the only place `collector` imports `writer`/`sender` (unchanged seam, now via a normal package import instead of a `sys.path` hack).

**Tech Stack:** Python 3.13, FastAPI, uv (single workspace-less project), pytest, SQLite, langchain / langchain-openrouter.

**Spec:** `docs/superpowers/specs/2026-08-19-backend-consolidation-design.md`

## Global Constraints

- Zero product behavior changes: same leads, same dossiers, same drafts, same suppression rules. This is a move/rename/wiring task only.
- Every internal import in `collector` and `writer` is **absolute**, rooted at the package name (`collector.X`, `writer.X`) — no relative dots, no bare `from services import x`, everywhere, including inside each system's own files and its own tests.
- `collector/store/` → `collector/db/`. `writer` gets the same four folders as `collector`: `routes/ services/ db/ schemas/`.
- `writer.outreach` takes its batch size from `writer/config.toml`'s `[llm].top_n` — no new API parameter.
- `sender/` stays a one-file stub — no folders added there in this plan.
- Every task ends with a green `uv run pytest` slice before moving to the next task.

---

### Task 1: Scaffold `backend/` — move directories, one venv

**Files:**
- Create: `backend/pyproject.toml`
- Move: `collector/` → `backend/collector/`, `writer/` → `backend/writer/`, `sender/` → `backend/sender/`
- Delete: `backend/collector/pyproject.toml`, `backend/collector/uv.lock`, `backend/writer/pyproject.toml`, `backend/writer/uv.lock`
- Test: none yet (no importable code changes) — verified by `uv sync` succeeding

**Interfaces:**
- Produces: `backend/pyproject.toml` — the one venv every later task's `uv run` commands use. `backend/.venv` and `backend/uv.lock` are generated, not hand-written.

- [ ] **Step 1: Confirm nothing uncommitted is about to get lost in the move**

Run: `git status --short collector writer sender`
Expected: empty, or only changes you already know about and are fine moving as-is. If it's dirty in a way you don't recognize, stop and ask before proceeding — do not move directories out from under uncommitted work you don't understand.

- [ ] **Step 2: Move the three systems under `backend/`**

```bash
mkdir backend
git mv collector backend/collector
git mv writer backend/writer
git mv sender backend/sender
```

- [ ] **Step 3: Remove the two per-system project files**

```bash
git rm backend/collector/pyproject.toml backend/collector/uv.lock
git rm backend/writer/pyproject.toml backend/writer/uv.lock
```

- [ ] **Step 4: Write the merged `backend/pyproject.toml`**

Union of both old dependency lists (writer's `langchain`/`langchain-openrouter`/`fastapi` were already a subset of collector's):

```toml
[project]
name = "backend"
version = "0.1.0"
description = "Три системы продукта — один процесс"
requires-python = ">=3.13"
dependencies = [
    "scrapling[fetchers]>=0.4.12",
    # scrapling 0.4.12 просит Chrome 149, а датасет 0.14.0 знает максимум 143 ->
    # ValueError при импорте StealthySession. 0.15.0 чинит.
    "apify-fingerprint-datapoints>=0.15.0",
    "fastapi>=0.141.1",
    "uvicorn>=0.52.3",
    "langchain>=1.3.15",
    "langchain-openrouter>=0.2.8",
]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["collector/tests", "writer/tests"]
```

- [ ] **Step 5: Sync and verify the venv resolves**

Run: `cd backend && uv sync`
Expected: creates `backend/uv.lock` and `backend/.venv` without dependency-resolution errors.

Run: `cd backend && uv run python -c "import fastapi, langchain, langchain_openrouter, scrapling; print('deps ok')"`
Expected: prints `deps ok`. (This does not yet import `collector`/`writer` — those aren't packages until Tasks 2–3.)

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "chore: move collector/writer/sender under backend/, one venv"
```

---

### Task 2: `collector` becomes a real package (`store/` → `db/`, absolute imports)

**Files:**
- Move: `backend/collector/store/` → `backend/collector/db/`
- Create: `backend/collector/__init__.py`, `backend/collector/routes/__init__.py`, `backend/collector/services/__init__.py`, `backend/collector/db/__init__.py`, `backend/collector/schemas/__init__.py` (all empty)
- Modify: every `.py` file under `backend/collector/` (34 files — full list in Step 3) — import lines only
- Modify: `backend/collector/services/store.py:19,46` — `store/schema.sql` path and docstring
- Modify: `backend/collector/tests/test_operations.py` — module-path resolution logic + one stale path list entry
- Modify: `backend/collector/tests/test_schema.py:33` — docstring wording
- Test: `backend/collector/tests/` (existing suite, run via `cd backend && uv run pytest collector/tests/`)

**Interfaces:**
- Produces: `collector.*` as an importable absolute package root — every later task (writer's cross-reference, `main.py`, Docker) assumes this works.

- [ ] **Step 1: Rename `store/` to `db/`**

```bash
git mv backend/collector/store backend/collector/db
```

- [ ] **Step 2: Add `__init__.py` to every package level**

```bash
touch backend/collector/__init__.py
touch backend/collector/routes/__init__.py
touch backend/collector/services/__init__.py
touch backend/collector/db/__init__.py
touch backend/collector/schemas/__init__.py
```

`backend/collector/services/pipeline/__init__.py` already exists (it's the operations registry) — leave its content in place, its self-import gets fixed by the sed pass in Step 3. `backend/collector/tests/` does **not** get an `__init__.py` — pytest doesn't need it to be a package, `pythonpath` from Task 1 already makes `collector.*` importable from any cwd.

- [ ] **Step 3: Run the mechanical import-prefix rewrite**

This single sed pass covers every import line in collector (verified against a full repo grep before writing this plan — all 34 files, including the four `from services.pipeline import ...` lines inside test function bodies in `test_operations.py`):

```bash
find backend/collector -name "*.py" -not -path "*__pycache__*" -print0 | xargs -0 sed -i '' \
  -e 's/from services\./from collector.services./g' \
  -e 's/from services import/from collector.services import/g' \
  -e 's/import services\./import collector.services./g' \
  -e 's/from routes import/from collector.routes import/g' \
  -e 's/from store import/from collector.db import/g' \
  -e 's/from schemas\./from collector.schemas./g' \
  -e 's/from schemas import/from collector.schemas import/g'
```

(macOS/BSD sed — `-i ''` with the empty-string argument for in-place edit without a backup file.)

Files this touches (for reference — the sed above already does all of them, nothing further to hand-edit for the import lines themselves): `api.py`; `routes/{events,jobs,leads,operations,pipeline,runs,stats,suppression}.py`; `services/{enrich,fetch,jobs,leads,metrics,resolve,score,sources,storage,store,suppression}.py`; `services/pipeline/{__init__,analyze,collect,export,llm,probe,rebuild}.py`; `db/lead.py`; `tests/{conftest,test_build,test_collect,test_jobs,test_leads,test_parsers,test_raw,test_runs,test_schema,test_storage,test_web,test_operations}.py`.

- [ ] **Step 4: Fix the `db/schema.sql` path in `services/store.py`**

`backend/collector/services/store.py:19` currently reads (unaffected by the sed above — it's a path string literal, not an import):

```python
SCHEMA = Path(__file__).resolve().parent.parent / "store" / "schema.sql"
```

Change to:

```python
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
```

And the docstring of `connect()` at line 46 — change `store/schema.sql и делится маркерами` to `db/schema.sql и делится маркерами`.

- [ ] **Step 5: Fix `test_operations.py`'s import-graph walker**

The walker resolves dotted import paths onto the filesystem relative to `collector/`. After Step 3, import strings inside collector now start with `collector.` themselves (e.g. `from collector.services import enrich`), so resolving them relative to `collector/` would double up (`collector/collector/services/enrich.py`). Resolve relative to `backend/` (the parent of `collector/`) instead.

Current top of file:

```python
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
```

Change to:

```python
COLLECTOR = Path(__file__).resolve().parent.parent
BACKEND_ROOT = COLLECTOR.parent   # корень абсолютных импортов: collector.X живёт здесь
FORBIDDEN = ("services/fetch", "scrapling")


def _resolve(root, node):
    """Кандидаты-пути, на которые может ссылаться Import/ImportFrom.

    `from collector.services import enrich` означает модуль services/enrich.py
    относительно `root` (BACKEND_ROOT — dotted-путь включает сам пакет
    `collector`, поэтому резолвить его надо на уровень выше COLLECTOR).
    `from collector.services.pipeline import X` — либо services/pipeline.py,
    либо X-подмодуль. Возвращаем несколько кандидатов; reachable_modules берёт
    только существующие.
    """
```

Then in `reachable_modules`, change the one call site:

```python
                stack.extend(_resolve(COLLECTOR, node))
```

to:

```python
                stack.extend(_resolve(BACKEND_ROOT, node))
```

- [ ] **Step 6: Fix the `store` → `db` rename in the cross-db-write test**

`test_no_operation_writes_to_both_dbs` currently has:

```python
    modules += [root / "store" / "lead.py", root.parent / "writer" / "thread_store.py"]
```

Change only the `store` → `db` part — **leave the writer path as-is for now**, it still points at the real (not-yet-moved) file until Task 3:

```python
    modules += [root / "db" / "lead.py", root.parent / "writer" / "thread_store.py"]
```

- [ ] **Step 7: Fix the stale docstring in `test_schema.py`**

`backend/collector/tests/test_schema.py:33`, inside `test_views_match_schema_file`'s docstring:

```python
    """Определения view в базе совпадают с store/schema.sql.
```

Change to:

```python
    """Определения view в базе совпадают с db/schema.sql.
```

- [ ] **Step 8: Run the collector test suite**

Run: `cd backend && uv run pytest collector/tests/ -v`
Expected: all pass. If anything fails on a leftover unconverted import, grep for it directly: `grep -rn "^from services\|^from routes\|^from store\|^from schemas\|^import services\." backend/collector --include="*.py" | grep -v __pycache__` should return nothing.

- [ ] **Step 9: Commit**

```bash
git add backend/collector
git commit -m "refactor(collector): real package, store/ -> db/, absolute imports"
```

---

### Task 3: `writer` gets the same shape as `collector`

**Files:**
- Create: `backend/writer/routes/`, `backend/writer/services/`, `backend/writer/db/` (with `__init__.py` each), `backend/writer/__init__.py`, `backend/writer/schemas/__init__.py`
- Move: `writer/web.py` → `writer/routes/threads.py`; `writer/agent.py` → `writer/services/agent.py`; `writer/config.py` → `writer/services/config.py`; `writer/leads_source.py` → `writer/db/leads_source.py`; `writer/thread_store.py` → `writer/db/thread_store.py`
- Modify: `backend/writer/routes/threads.py`, `backend/writer/services/agent.py`, `backend/writer/services/config.py` — import lines / path depth
- Modify: `backend/writer/tests/{test_leads,test_prompt,test_schema,test_threads,conftest}.py` — import lines + two docstring/path fixes
- Modify: `backend/collector/tests/test_operations.py` — one path-list entry, now that writer's file has actually moved
- Test: `backend/writer/tests/` + re-run `backend/collector/tests/test_operations.py`

**Interfaces:**
- Consumes: `collector.*` importable (Task 2).
- Produces: `writer.routes.threads.router` (the `APIRouter`, same object `collector/api.py` will mount in Task 5), `writer.services.agent`, `writer.services.config`, `writer.db.leads_source`, `writer.db.thread_store` — the exact module paths Task 4's `writer/services/operations.py` imports.

- [ ] **Step 1: Create the new folders and move files**

```bash
mkdir -p backend/writer/routes backend/writer/services backend/writer/db
git mv backend/writer/web.py backend/writer/routes/threads.py
git mv backend/writer/agent.py backend/writer/services/agent.py
git mv backend/writer/config.py backend/writer/services/config.py
git mv backend/writer/leads_source.py backend/writer/db/leads_source.py
git mv backend/writer/thread_store.py backend/writer/db/thread_store.py
touch backend/writer/__init__.py
touch backend/writer/routes/__init__.py
touch backend/writer/services/__init__.py
touch backend/writer/db/__init__.py
touch backend/writer/schemas/__init__.py
```

- [ ] **Step 2: Fix `routes/threads.py`'s imports**

Current top of file:

```python
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import agent
import config
import leads_source
import thread_store
```

Change to:

```python
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from writer.services import agent, config
from writer.db import leads_source, thread_store
```

No other line in this file changes — every use is `agent.X` / `config.X` / `leads_source.X` / `thread_store.X`, and those names still resolve after the import rewrite.

- [ ] **Step 3: Fix `services/agent.py`'s schema import**

`backend/writer/services/agent.py:14` currently:

```python
from schemas.outreach import Draft
```

Change to:

```python
from writer.schemas.outreach import Draft
```

- [ ] **Step 4: Fix `services/config.py`'s path depth**

The module moved one level deeper (`writer/config.py` → `writer/services/config.py`), so `HOME` needs one more `.parent`:

```python
HOME = Path(__file__).resolve().parent
```

Change to:

```python
HOME = Path(__file__).resolve().parent.parent
```

- [ ] **Step 5: Fix the four writer test files' imports**

`backend/writer/tests/test_leads.py:8`:
```python
import leads_source
```
→
```python
from writer.db import leads_source
```
Also fix the docstring at `test_leads.py:3`: `collector/store/schema.sql` → `collector/db/schema.sql`.

`backend/writer/tests/test_prompt.py:3-4`:
```python
import agent
import config
```
→
```python
from writer.services import agent, config
```

`backend/writer/tests/test_threads.py:7`:
```python
import thread_store
```
→
```python
from writer.db import thread_store
```

`backend/writer/tests/test_schema.py:8`:
```python
from schemas.outreach import BANNED, MAX_CHARS, Draft
```
→
```python
from writer.schemas.outreach import BANNED, MAX_CHARS, Draft
```

- [ ] **Step 6: Fix `conftest.py`'s schema path**

`backend/writer/tests/conftest.py:4` (docstring) and `:18` (code) both say `collector/store/schema.sql`. The directory depth is unchanged — `writer/tests/conftest.py` still sits three levels below the common `backend/` parent of `collector/` and `writer/`, exactly as `writer/tests/conftest.py` sat three levels below the repo root before. Only the folder name changes:

```python
COLLECTOR_SCHEMA = Path(__file__).resolve().parent.parent.parent / "collector" / "store" / "schema.sql"
```
→
```python
COLLECTOR_SCHEMA = Path(__file__).resolve().parent.parent.parent / "collector" / "db" / "schema.sql"
```

And the docstring line: `разбив collector/store/schema.sql на две половины` → `разбив collector/db/schema.sql на две половины`.

- [ ] **Step 7: Finish the writer-path fix in `collector/tests/test_operations.py`**

Task 2 deliberately left this line pointing at writer's old flat path because the file hadn't moved yet. It has now:

```python
    modules += [root / "db" / "lead.py", root.parent / "writer" / "thread_store.py"]
```
→
```python
    modules += [root / "db" / "lead.py", root.parent / "writer" / "db" / "thread_store.py"]
```

- [ ] **Step 8: Run both test suites**

Run: `cd backend && uv run pytest writer/tests/ collector/tests/test_operations.py -v`
Expected: all pass. If an import error mentions a bare `agent`/`config`/`leads_source`/`thread_store`/`schemas`, grep for stragglers: `grep -rn "^import agent\|^import config\|^import leads_source\|^import thread_store\|^from schemas" backend/writer --include="*.py" | grep -v __pycache__`.

- [ ] **Step 9: Commit**

```bash
git add backend/writer backend/collector/tests/test_operations.py
git commit -m "refactor(writer): mirror collector's routes/services/db/schemas layout"
```

---

### Task 4: `writer.outreach` — queue operation replacing `scripts/write.py`

**Files:**
- Create: `backend/writer/services/operations.py`
- Create: `backend/writer/tests/test_operations.py`
- Delete: `backend/writer/scripts/` (whole directory, including `write.py`)
- Test: `backend/writer/tests/test_operations.py`

**Interfaces:**
- Consumes: `writer.services.agent.model`/`.draft`/`.FIRST` (existing), `writer.services.config.load` (existing), `writer.db.leads_source.connect`/`.candidates`, `writer.db.thread_store.connect`/`.thread`/`.open_thread`/`.add_draft` (existing, unchanged signatures).
- Produces: `writer.services.operations.open_new_threads(ctx) -> {"drafted": int}` and `writer.services.operations.require_api_key() -> None | raises RuntimeError` — the two names Task 5's registry wiring imports.

- [ ] **Step 1: Write the failing test**

Create `backend/writer/tests/test_operations.py`:

```python
"""Операция очереди writer.outreach — замена scripts/write.py."""

from types import SimpleNamespace

import pytest

from writer.services import operations


class DummyCtx:
    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, current, total, label):
        pass

    def check_cancelled(self):
        pass


def test_require_api_key_without_env_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        operations.require_api_key()


def test_open_new_threads_without_api_key_raises_before_touching_db(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        operations.open_new_threads(DummyCtx())


def test_open_new_threads_skips_existing_threads_and_drafts_only_new(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    candidates = [
        {"thread_id": "t1", "company_id": "c1", "seed": {"name": "Alpha"}},
        {"thread_id": "t2", "company_id": "c2", "seed": {"name": "Beta"}},
    ]
    existing_threads = {"t1"}
    drafted = []

    monkeypatch.setattr(operations.leads_source, "connect",
                         lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.leads_source, "candidates",
                         lambda db, limit: candidates)
    monkeypatch.setattr(operations.thread_store, "connect",
                         lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(operations.thread_store, "thread",
                         lambda db, thread_id: thread_id in existing_threads)
    monkeypatch.setattr(operations.thread_store, "open_thread", lambda *a: None)
    monkeypatch.setattr(operations.thread_store, "add_draft",
                         lambda db, thread_id, text, angle: drafted.append(thread_id))
    monkeypatch.setattr(operations.agent, "model", lambda config: "llm-stub")
    monkeypatch.setattr(operations.agent, "draft",
                         lambda *a, **kw: SimpleNamespace(stop=False, text="hi", angle="pain"))

    ctx = DummyCtx()
    result = operations.open_new_threads(ctx)

    assert result == {"drafted": 1}
    assert drafted == ["t2"]
    assert any("Beta" in line for line in ctx.logs)
```

- [ ] **Step 2: Run it, confirm it fails on import**

Run: `cd backend && uv run pytest writer/tests/test_operations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'writer.services.operations'`.

- [ ] **Step 3: Write `writer/services/operations.py`**

```python
"""Операция очереди: черновики первым сообщениям top_n новым лидам.

Замена writer/scripts/write.py. В реестр её подключает collector/api.py (тот
же шов, что монтирует роутер writer'а) — сам writer по-прежнему не знает о
существовании collector'а.
"""

import os

from writer.services import agent, config
from writer.db import leads_source, thread_store

CONFIG = config.load()


def open_new_threads(ctx):
    require_api_key()
    limit = CONFIG["llm"]["top_n"]
    leads = leads_source.connect(CONFIG["leads_db"])
    threads = thread_store.connect(CONFIG["threads_db"])
    try:
        fresh = [lead for lead in leads_source.candidates(leads, limit * 3)
                 if not thread_store.thread(threads, lead["thread_id"])][:limit]
        ctx.log(f"писем: {len(fresh)}, модель {CONFIG['llm']['model']}")
        llm = agent.model(CONFIG)
        for number, lead in enumerate(fresh, 1):
            ctx.check_cancelled()
            ctx.progress(number, len(fresh), lead["seed"]["name"])
            thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
            proposal = agent.draft(llm, lead["seed"], [], agent.FIRST, offer=CONFIG["offer"]["text"])
            if proposal.stop:
                ctx.log(f"{lead['seed']['name']}: агент советует не писать — повода в данных нет")
                continue
            thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)
            ctx.log(f"{lead['seed']['name']}: черновик готов ({proposal.angle})")
        return {"drafted": len(fresh)}
    finally:
        leads.close()
        threads.close()


def require_api_key():
    """Отказать до сети и до открытия баз, а не в середине прогона."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY не задан в процессе бэкенда. "
            "Поднимать так: uv run --env-file collector/.env python main.py"
        )
```

- [ ] **Step 4: Run the tests again**

Run: `cd backend && uv run pytest writer/tests/test_operations.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Delete the old script**

```bash
git rm -r backend/writer/scripts
```

- [ ] **Step 6: Run the full writer suite to confirm nothing referenced the deleted script**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/writer/services/operations.py backend/writer/tests/test_operations.py
git commit -m "feat(writer): writer.outreach queue operation, remove scripts/write.py"
```

---

### Task 5: Wire `backend/main.py` and register the `write` pipeline

**Files:**
- Create: `backend/main.py`
- Modify: `backend/collector/api.py`
- Modify: `backend/writer/tests/test_operations.py` (add one wiring test)
- Test: `backend/writer/tests/test_operations.py::test_write_pipeline_is_registered_in_collector_queue`, then full suite

**Interfaces:**
- Consumes: `writer.routes.threads.router` (Task 3), `writer.services.operations.open_new_threads` (Task 4), `collector.services.pipeline.OPERATIONS`/`PIPELINES` (Task 2, unchanged dict objects).
- Produces: `collector.api.app` — the FastAPI instance `main.py` serves; `PIPELINES["write"]` — the pipeline kind `frontend/app/writer/page.tsx` already filters for.

- [ ] **Step 1: Write `backend/main.py`**

```python
"""Точка входа. Три системы — один процесс, один пакетный корень (backend/).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from collector.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
```

- [ ] **Step 2: Rewrite the import/wiring block in `collector/api.py`**

Read the current file first — after Task 2's sed pass, the top of `backend/collector/api.py` looks like this:

```python
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from collector.routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
from collector.services import jobs as queue

# Система 2 живёт своим проектом и своей базой; здесь только склейка, чтобы у
# оператора остались одна консоль и один порт. Каталог добавляется в путь
# целиком: writer импортирует свои модули по коротким именам, как делает и сам
# collector. Стаб системы 3 называется stub.py: имена web и api заняты модулями
# collector и writer, а точка входа у стаба одна и зависимости пусты.
sys.path.append(str(Path(__file__).resolve().parent.parent / "writer"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "sender"))
import stub as sender  # noqa: E402
import web as writer  # noqa: E402
```

Replace the whole block (from `import asyncio` down through `import web as writer  # noqa: E402`) with:

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from collector.routes import events, jobs, leads, operations, pipeline, runs, stats, suppression
from collector.services import jobs as queue
from collector.services.pipeline import OPERATIONS, PIPELINES
from sender import stub as sender
from writer.routes import threads as writer
from writer.services import operations as writer_operations

# Единственное место, где collector знает о существовании writer'а — тот же
# шов, что монтирует его роутер: подключает операцию очереди в общий реестр.
OPERATIONS["writer.outreach"] = writer_operations.open_new_threads
PIPELINES["write"] = {"title": "Черновики топ-N", "steps": ("writer.outreach",)}
```

The rest of the file (`WEB_ORIGINS`, `lifespan`, `app = FastAPI(...)`, `app.add_middleware(...)`, all the `app.include_router(...)` lines) is unchanged — `writer.router` and `sender.router` still resolve because `writer`/`sender` are still the names bound above, just via real imports instead of `sys.path` + bare `import`.

- [ ] **Step 3: Add the wiring test**

Append to `backend/writer/tests/test_operations.py`:

```python
def test_write_pipeline_is_registered_in_collector_queue():
    import collector.api  # noqa: F401 — импорт наполняет реестр операций
    from collector.services.jobs import check_pipelines
    from collector.services.pipeline import OPERATIONS, PIPELINES

    assert "writer.outreach" in OPERATIONS
    assert PIPELINES["write"]["steps"] == ("writer.outreach",)
    check_pipelines()
```

- [ ] **Step 4: Run it**

Run: `cd backend && uv run pytest writer/tests/test_operations.py -v`
Expected: all 4 tests pass, including the new one. If `collector.api` fails to import, the error will point at whichever line still references the old `sys.path`/bare-import style — fix it and re-run.

- [ ] **Step 5: Run the whole backend suite**

Run: `cd backend && uv run pytest -v`
Expected: all pass (collector + writer combined, per `testpaths` in `backend/pyproject.toml`).

- [ ] **Step 6: Manual smoke test of the real server**

```bash
cd backend && uv run --env-file collector/.env python main.py &
sleep 2
curl -s http://127.0.0.1:8787/api/pipeline | python3 -m json.tool
kill %1
```

Expected: the JSON response's `"pipelines"` list includes an entry with `"kind": "write", "title": "Черновики топ-N"`. (If `collector/.env` has no `OPENROUTER_API_KEY`, that's fine — this endpoint doesn't need it, only `POST .../write` and `/draft` do.)

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/collector/api.py backend/writer/tests/test_operations.py
git commit -m "feat: backend/main.py entry point, wire writer.outreach into the job queue"
```

---

### Task 6: Docker

**Files:**
- Move: `backend/collector/Dockerfile` → `backend/Dockerfile`
- Modify: `docker-compose.yml`
- Test: `docker compose config` (YAML/context validation; a full `docker compose build` is a manual optional check, noted below)

- [ ] **Step 1: Move and rewrite the Dockerfile**

```bash
git mv backend/collector/Dockerfile backend/Dockerfile
```

Replace its content:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY collector/ collector/
COPY writer/ writer/
COPY sender/ sender/
COPY main.py ./

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага collect.* и probe.* падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "python", "main.py"]
```

- [ ] **Step 2: Update `docker-compose.yml`**

Read the current file first. Change:

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: collector/Dockerfile
    ports:
      - "8787:8787"
    env_file:
      - path: collector/.env
        required: false
    volumes:
      # Сырьё и базы — невосстановимые или производные слои (см. CLAUDE.md),
      # им место на хосте, а не в слоях образа.
      - ./collector/data:/app/collector/data
      - ./collector/config.toml:/app/collector/config.toml
      - ./collector/.env:/app/collector/.env
      - ./writer/config.toml:/app/writer/config.toml
      - ./writer/.env:/app/writer/.env
```

to:

```yaml
services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "8787:8787"
    env_file:
      - path: backend/collector/.env
        required: false
    volumes:
      # Сырьё и базы — невосстановимые или производные слои (см. CLAUDE.md),
      # им место на хосте, а не в слоях образа.
      - ./backend/collector/data:/app/collector/data
      - ./backend/collector/config.toml:/app/collector/config.toml
      - ./backend/collector/.env:/app/collector/.env
      - ./backend/writer/config.toml:/app/writer/config.toml
      - ./backend/writer/.env:/app/writer/.env
```

Leave the `frontend:` service block as-is. In the top-of-file comment block (above `services:`), replace the sentence "writer — не отдельный сервис: его роутер монтируется в api.py (collector/api.py), а .venv собирается тем же образом backend'а, потому что операции системы 2 вызываются в том же процессе воркера джобов (services/jobs.py), а writer монтируется через sys.path." with "writer — не отдельный сервис: его роутер монтируется в api.py (backend/collector/api.py), а venv у него общий с collector'ом (backend/pyproject.toml), потому что операции системы 2 вызываются в том же процессе воркера джобов (collector/services/jobs.py)." — the rest of that comment block (about `cp collector/.env.example collector/.env`) stays, since those paths are still correct relative to `backend/`.

- [ ] **Step 3: Validate**

Run: `docker compose config`
Expected: prints the resolved compose config without errors (this only validates YAML + build context existence, it doesn't build the image).

Optional (slower, needs network for base image + `scrapling install`): `docker compose build backend` — if you run this, expect it to succeed; if Docker isn't available in this environment, skip and note it in the task's completion message.

- [ ] **Step 4: Commit**

```bash
git add backend/Dockerfile docker-compose.yml
git commit -m "chore: move Dockerfile under backend/, one uv sync"
```

---

### Task 7: Docs — `CLAUDE.md` and `README.md`

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: `CLAUDE.md` — add the `backend/` umbrella note**

After line 16 (end of the "Что это" system list, before "Реализованы системы 1 и 2..."), insert:

```markdown
Все три системы физически лежат в `backend/` (`backend/collector/`,
`backend/writer/`, `backend/sender/`) под одним venv и одним
`backend/main.py`; ниже относительные имена `collector/`, `writer/`,
`sender/` всегда подразумевают путь внутри `backend/`.
```

- [ ] **Step 2: `CLAUDE.md` — rewrite the "Команды" intro and bash blocks**

Replace:

```markdown
Python-конвейер живёт в `collector/`, продуктовый дашборд — во `frontend/`.
Команды ниже, кроме веба, запускаются из `collector/`. Сбор, анализ и прогоны —
операции воркера (`services/pipeline/`), а не скрипты; из консоли они ставятся
джобами через веб, из тестов — вызовом функции с RunContext.

```bash
uv run pytest tests/                     # тесты вместо scripts/check.py, без сети
uv run pytest tests/test_parsers.py      # один раздел — одним файлом
cd writer && uv run pytest tests/        # тесты системы 2
```

Разведка источника вручную: `uv run python -c "from services.pipeline import probe;
probe.gis_list(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None,
check_cancelled=lambda: None))"` — пробы ходят в сеть и читают config.toml.

Веб — два процесса, браузеру нужен только порт 3000:

```bash
cd collector && uv run --env-file .env python -m uvicorn api:app --port 8787 --reload   # FastAPI
cd frontend && npm run dev                                   # Next.js -> http://localhost:3000
```

`python -m uvicorn`, а не голый `uvicorn`: без `-m` uv берёт системный бинарарь
с чужим python и падает на импорте зависимостей проекта.
```

with:

```markdown
Продуктовый дашборд — во `frontend/`. Команды ниже, кроме веба, запускаются
из `backend/`. Сбор, анализ и прогоны — операции воркера
(`collector/services/pipeline/`), а не скрипты; из консоли они ставятся
джобами через веб, из тестов — вызовом функции с RunContext.

```bash
uv run pytest                                    # тесты обеих систем, без сети
uv run pytest collector/tests/test_parsers.py    # один раздел — одним файлом
cd writer && uv run pytest tests/                # тесты системы 2, тот же venv
```

Разведка источника вручную: `uv run python -c "from collector.services.pipeline import probe;
probe.gis_list(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None,
check_cancelled=lambda: None))"` — пробы ходят в сеть и читают config.toml.

Веб — два процесса, браузеру нужен только порт 3000:

```bash
uv run --env-file collector/.env python main.py               # FastAPI, все три системы
cd frontend && npm run dev                                    # Next.js -> http://localhost:3000
```

`main.py` поднимает то же приложение, что раньше собирал `collector/api.py`
через `uvicorn api:app` — включая роутеры writer'а и sender'а. Для
reload-режима: `uv run uvicorn main:app --port 8787 --reload`.
```

- [ ] **Step 3: `CLAUDE.md` — fix `collector/.env` mentions to be relative-to-backend**

Line (originally 54): `\`SERPER_API_KEY\` и \`OPENROUTER_API_KEY\` берутся из \`collector/.env\`` — already correct relative to the new "commands run from `backend/`" framing from Step 2, no change needed (it's already `collector/.env`, which now correctly means `backend/collector/.env`).

- [ ] **Step 4: `CLAUDE.md` — fix the writer section**

Replace:

```markdown
Отдельный uv-проект: свои зависимости, своя база, свой pytest. Читает
```

with:

```markdown
Тот же venv, что у collector'а (`backend/pyproject.toml`), но свой пакет и
своя база: `writer.*` не импортирует `collector.*` напрямую, только через шов
в `collector/api.py`. Читает
```

Replace:

```markdown
```bash
cd writer
uv run --env-file .env -m scripts.write [сколько]   # первые сообщения топ-N лидам
uv run pytest tests/                                # schema, threads, leads, prompt
uv run -m web                                       # роутер собирается, базы открываются
```

`scripts.write` — это всегда N **новых** компаний: у кого тред уже есть, того
скрипт пропускает и идёт дальше по списку.
```

with:

```markdown
```bash
cd backend
uv run pytest writer/tests/    # schema, threads, leads, prompt, operations
```

«Черновики топ-N» — не скрипт, а операция очереди `writer.outreach`
(пайплайн `write`): кнопка «Черновики топ-N» на странице «Персонализация»
или `POST /api/pipeline/write`. Она всегда обрабатывает N **новых**
компаний: у кого тред уже есть, того пропускает и идёт дальше по списку.
```

Replace:

```markdown
**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/web.py`
```

with:

```markdown
**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/routes/threads.py`
```

Replace the repeated uvicorn command block:

```bash
cd collector && uv run --env-file .env python -m uvicorn api:app --port 8787 --reload
```

with:

```bash
cd backend && uv run --env-file collector/.env python main.py
```

- [ ] **Step 5: `CLAUDE.md` — fix `store/schema.sql` mentions**

Three occurrences, all `store/schema.sql` → `db/schema.sql`: in the `data/derived.db` bullet, the `data/state.db` bullet (both under "Слои данных"), and the "Документация" section's closing mention ("источник для `db/schema.sql`").

- [ ] **Step 6: `README.md` — same category of fixes**

Add the same `backend/` umbrella note used in Step 1 near the top (after the documents line, before "## Команды").

In the "## Команды" section, replace:

```markdown
Python-конвейер живёт в `collector/`, продуктовый дашборд — во `frontend/`.
Сбор, анализ и пересборка — операции воркера (`services/pipeline/`), а не
скрипты: из консоли они ставятся джобами через веб, из тестов — вызовом функции
с RunContext. Логика и сеть в `services/`, схема с запросами в `store/`, сырьё —
в `data/raw/`, две базы — в `data/`. Команды ниже запускаются из `collector/`.

```bash
uv run pytest tests/                     # тесты вместо scripts/check.py, без сети
cd writer && uv run pytest tests/        # тесты системы 2
```
```

with:

```markdown
Продуктовый дашборд — во `frontend/`. Сбор, анализ и пересборка — операции
воркера (`collector/services/pipeline/`), а не скрипты: из консоли они
ставятся джобами через веб, из тестов — вызовом функции с RunContext. Логика
и сеть в `services/`, схема с запросами в `db/`, сырьё — в `data/raw/`, две
базы — в `data/`. Команды ниже запускаются из `backend/`.

```bash
uv run pytest                            # тесты обеих систем, без сети
cd writer && uv run pytest tests/        # тесты системы 2, тот же venv
```
```

In "## Веб-дашборд", replace:

```bash
cd collector && uv run --env-file .env python -m uvicorn api:app --port 8787 --reload   # FastAPI
cd frontend && npm run dev                                   # Next.js -> http://localhost:3000
```

with:

```bash
uv run --env-file collector/.env python main.py               # FastAPI, все три системы (из backend/)
cd frontend && npm run dev                                    # Next.js -> http://localhost:3000
```

In "## Две базы", replace `store/schema.sql` with `db/schema.sql`.

- [ ] **Step 7: Read both files back and sanity-check**

Run: `grep -n "store/schema.sql\|scripts.write\|writer/web.py\|cd collector &&\|cd writer\b" CLAUDE.md README.md`
Expected: no remaining matches (the `cd writer && uv run pytest tests/` command lines are intentionally kept — those still work — so this grep for `cd writer\b` alone is expected to still hit those two lines; only confirm nothing *else* unexpected shows up).

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: update paths and commands for backend/ consolidation"
```

---

## Final check

- [ ] `cd backend && uv run pytest -v` — full suite green.
- [ ] `cd backend && uv run python main.py` starts, `GET /api/pipeline` lists `write`, `Ctrl-C` stops cleanly.
- [ ] `cd frontend && npm run dev`, open `/writer` — the "Черновики топ-N" button is visible (it was already coded to appear once `PIPELINES["write"]` exists; no frontend changes were made in this plan).
- [ ] `git log --oneline -8` shows the 7 commits from this plan in order.
