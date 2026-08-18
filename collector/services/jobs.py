"""Слой джобов: очередь в db/ops.db и один воркер, исполняющий шаги.

Продуктовые эндпоинты (/api/pipeline/*) не запускают скрипты по одному —
они ставят джобу из шагов, и воркер проводит её от «в очереди» до «готово».
Состояние живёт в базе, а не в памяти: история запусков и прогресс переживают
перезагрузку страницы и перезапуск бэкенда, а идущий процесс виден всем
операторам сразу — по SSE и по GET /api/jobs.

Один воркер — не недоделка, а ограничение системы 1: build.py пересобирает
leads.db через DROP, и параллельный сбор писал бы в ту же базу. Очередь при
этом честная: поставить можно несколько, исполняются они по одной.

Джоба ≠ команда: у джобы есть шаги с именами, и фронт показывает «шаг 2 из 3:
пересборка базы», а не хвост консоли. Шаги — те же `uv run ...` из README,
своей логики здесь нет, белый список argv остаётся единственной защитой.

Соглашение о прогрессе внутри шага: строка вывода вида
`#progress {"label": "...", "current": 34, "total": 120}`
разбирается воркером и уходит в событие job — счётчики появляются в UI без
второго канала связи. Скрипты таких строк пока не пишут; парсер ждёт их.
"""

import asyncio
import json
import os
import shlex
import signal
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from services import events

BACKEND = Path(__file__).resolve().parent.parent
WRITER = BACKEND.parent / "writer"
OPS_DB = Path("db/ops.db")
LOG_LIMIT = 20_000
LOG_FLUSH_LINES = 20
LOG_FLUSH_SECONDS = 0.5
PROGRESS_PREFIX = "#progress "

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,          -- имя пайплайна; "custom" — служебные запуски проверок
  title      TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  step       INTEGER NOT NULL DEFAULT 0,
  steps      TEXT NOT NULL,          -- json: [{name, argv, cwd}]
  log        TEXT NOT NULL DEFAULT '',
  progress   TEXT,                   -- json последнего #progress шага
  exit_code  INTEGER,
  error      TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
"""

STEPS = {
    "collect": {"name": "Сбор сырья", "argv": ["uv", "run", "-m", "scripts.collect"], "cwd": BACKEND},
    "classify": {
        "name": "Профиль и why_now",
        "argv": ["uv", "run", "--env-file", ".env", "-m", "scripts.classify"],
        "cwd": BACKEND,
    },
    "classify_ig": {
        "name": "Смысл подписей Instagram",
        "argv": ["uv", "run", "--env-file", ".env", "-m", "scripts.classify_ig"],
        "cwd": BACKEND,
    },
    "build": {"name": "Пересборка базы", "argv": ["uv", "run", "build.py"], "cwd": BACKEND},
    "report": {"name": "Выгрузка leads.csv", "argv": ["uv", "run", "report.py"], "cwd": BACKEND},
}

PIPELINES = {
    "discover": {"title": "Поиск новых лидов", "steps": ("collect", "build", "report")},
    "classify": {"title": "Обогащение и оценка", "steps": ("classify", "classify_ig", "build", "report")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("build", "report")},
    "write": {"title": "Черновики топ-N лидам", "steps": ("write",)},
}

COLUMNS = (
    "id", "kind", "title", "status", "step", "steps", "log", "progress",
    "exit_code", "error", "created_at", "started_at", "finished_at",
)


def connect():
    db = sqlite3.connect(OPS_DB)
    db.executescript(SCHEMA)
    return db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pipeline_steps(kind, limit):
    if kind == "write":
        argv = ["uv", "run", "--env-file", ".env", "-m", "scripts.write", str(limit)]
        return [{"name": f"Черновики топ-{limit}", "argv": argv, "cwd": WRITER}]
    return [dict(STEPS[name]) for name in PIPELINES[kind]["steps"]]


def enqueue(kind, limit=10):
    if kind not in PIPELINES:
        raise KeyError(kind)
    return enqueue_steps(kind, PIPELINES[kind]["title"], pipeline_steps(kind, limit))


def enqueue_steps(kind, title, steps):
    """Примитив очереди: принимает готовые шаги. Каталожные пайплайны и проверки
    сходятся здесь — логика постановки одна. cwd хранится строкой: база не должна
    знать о pathlib."""
    steps = [{**step, "cwd": str(step["cwd"])} for step in steps]
    with closing(connect()) as db:
        cursor = db.execute(
            "INSERT INTO jobs (kind, title, steps, status, created_at)"
            " VALUES (?, ?, ?, 'queued', ?)",
            (kind, title, json.dumps(steps, ensure_ascii=False), now()),
        )
        db.commit()
        job_id = cursor.lastrowid
    publish_job(job_id)
    return job_id


def job(job_id):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return as_job(row) if row else None


def recent(limit=20):
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [as_job(row) for row in rows]


def active():
    return next((record for record in recent() if record["status"] in ("queued", "running")), None)


def tail(job_id, offset=0):
    """Хвост лога от offset по строкам — фолбэк на случай, если SSE недоступен."""
    record = job(job_id)
    if not record:
        return None
    lines = log_of(job_id).split("\n") if record["log_lines"] else []
    return {"lines": lines[offset:], "offset": len(lines)}


def log_of(job_id):
    with closing(connect()) as db:
        return db.execute("SELECT log FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def as_job(row):
    record = dict(zip(COLUMNS, row))
    steps = json.loads(record.pop("steps"))
    log = record.pop("log") or ""
    return {
        **record,
        "steps": [{"name": s["name"], "command": shlex.join(s["argv"])} for s in steps],
        "step_count": len(steps),
        "log_lines": log.count("\n") + 1 if log else 0,
        "progress": json.loads(record["progress"]) if record["progress"] else None,
    }


def publish_job(job_id):
    events.publish({"type": "job", "job": job(job_id)})


async def worker_loop():
    while True:
        await run_pending()
        await asyncio.sleep(0.5)


async def run_pending():
    """Исполняет одну следующую джобу, если очередь не пуста. Воркер зовёт это
    в цикле, проверки — по одному разу."""
    job_id = _claim()
    if job_id is None:
        return None
    await _execute(job_id)
    return job_id


def _claim():
    with closing(connect()) as db:
        row = db.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (now(), row[0]),
        )
        db.commit()
        return row[0]


async def _execute(job_id):
    steps = json.loads(_raw_steps(job_id))
    for index, step in enumerate(steps):
        _update(job_id, step=index)
        outcome = await _run_step(job_id, step)
        if outcome == "cancelled":
            _finish(job_id, "cancelled")
            return
        if outcome != 0:
            _finish(job_id, "failed", exit_code=outcome, error=f"шаг «{step['name']}» не прошёл")
            return
    _finish(job_id, "done", exit_code=0)


def _raw_steps(job_id):
    with closing(connect()) as db:
        return db.execute("SELECT steps FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


async def _run_step(job_id, step):
    global _current
    try:
        process = await asyncio.create_subprocess_exec(
            *step["argv"],
            cwd=step["cwd"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
    except OSError as failure:
        _append_log(job_id, [f"— запуск не удался: {failure}"])
        return -1

    _current = {"job_id": job_id, "process": process, "cancelled": False}
    buffered, flushed_at = [], time.monotonic()
    async for raw in process.stdout:
        buffered.append(raw.decode(errors="replace").rstrip("\n"))
        if len(buffered) >= LOG_FLUSH_LINES or time.monotonic() - flushed_at >= LOG_FLUSH_SECONDS:
            _flush_log(job_id, buffered)
            buffered, flushed_at = [], time.monotonic()
    _flush_log(job_id, buffered)
    await process.wait()

    cancelled, _current = _current["cancelled"], None
    if cancelled and process.returncode != 0:
        return "cancelled"
    return process.returncode


def _flush_log(job_id, lines):
    if not lines:
        return
    _append_log(job_id, lines)
    progress = _parse_progress(lines)
    if progress:
        _update(job_id, progress=json.dumps(progress, ensure_ascii=False))
    events.publish({"type": "log", "job_id": job_id, "lines": lines})


def _parse_progress(lines):
    for line in reversed(lines):
        if not line.startswith(PROGRESS_PREFIX):
            continue
        try:
            return json.loads(line[len(PROGRESS_PREFIX):])
        except json.JSONDecodeError:
            return None
    return None


def _append_log(job_id, lines):
    with closing(connect()) as db:
        stored = db.execute("SELECT log FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        stored_lines = stored.split("\n") if stored else []
        room = LOG_LIMIT - len(stored_lines)
        if room <= 0:
            return
        stored_lines.extend(lines[:room])
        if len(lines) > room:
            stored_lines.append(f"— вывод длиннее {LOG_LIMIT} строк, дальше не пишем")
        db.execute("UPDATE jobs SET log = ? WHERE id = ?", ("\n".join(stored_lines), job_id))
        db.commit()


def _update(job_id, **fields):
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with closing(connect()) as db:
        db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))
        db.commit()
    publish_job(job_id)


def _finish(job_id, status, exit_code=None, error=None):
    _update(job_id, status=status, exit_code=exit_code, error=error, finished_at=now())
    events.publish({"type": "refresh"})


def cancel(job_id):
    """Снимает queued мгновенно; running убивает всю группу процессов —
    `uv run` порождает python, и выживший потомок держал бы трубу открытой."""
    record = job(job_id)
    if not record:
        raise KeyError(job_id)
    if record["status"] == "queued":
        _finish(job_id, "cancelled")
        return job(job_id)
    if record["status"] == "running" and _current and _current["job_id"] == job_id:
        _current["cancelled"] = True
        try:
            os.killpg(os.getpgid(_current["process"].pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    return job(job_id)


def fail_orphans():
    """Перезапуск бэкенда убил процессы, но не записи: «running» без процесса —
    ложь оператору. «queued» не трогаем: воркер продолжит их при старте."""
    with closing(connect()) as db:
        db.execute(
            "UPDATE jobs SET status = 'failed', error = 'прерван перезапуском бэкенда',"
            " finished_at = ? WHERE status = 'running'",
            (now(),),
        )
        db.commit()


def check_pipelines():
    """Каталог пайплайнов цел: шаги существуют, каталоги на месте, argv — uv run."""
    for kind, pipeline in PIPELINES.items():
        assert pipeline["title"], f"{kind}: нет названия"
        for step in pipeline_steps(kind, limit=5):
            assert step["argv"][:2] == ["uv", "run"], f"{kind}: {step['argv']} — не uv run"
            assert step["cwd"].is_dir(), f"{kind}: {step['cwd']} — не каталог"


if __name__ == "__main__":
    check_pipelines()
    print("jobs ok — каталог пайплайнов цел; очередь проверяет раздел jobs в scripts/check.py")
