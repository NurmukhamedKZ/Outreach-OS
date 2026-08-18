"""Слой джобов: очередь в state.jobs и один воркер, вызывающий операции.

Продуктовые эндпоинты (/api/pipeline/*, /api/operations/*) не запускают
процессы — они ставят джобу из имён операций, и воркер проводит её от
«в очереди» до «готово», вызывая функции через asyncio.to_thread. Состояние
живёт в базе, а не в памяти: история запусков и прогресс переживают
перезагрузку страницы и перезапуск бэкенда, а идущая операция видна всем
операторам сразу — по SSE и по GET /api/jobs.

Один воркер — не недоделка, а ограничение системы 1: rebuild пересобирает
derived прогоном, и параллельный сбор писал бы в ту же базу. Очередь при
этом честная: поставить можно несколько, исполняются они по одной.

Джоба ≠ команда: у джобы есть шаги с именами операций, и фронт показывает
«шаг 2 из 3: пересборка базы», а не хвост консоли. Сами операции —
services/pipeline (реестр OPERATIONS): своей логики здесь нет, реестр
остаётся единственной точкой, где имя становится функцией.

Отмена кооперативная: флаг в активном контексте шага, а не SIGKILL процессу.
Операции проверяют его в check_cancelled между единицами работы.
"""

import asyncio
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

from services import events, store as engine
from services.pipeline import OPERATIONS, PIPELINES

LOG_LIMIT = 20_000

COLUMNS = (
    "id", "kind", "title", "status", "step", "steps", "log", "progress",
    "result", "exit_code", "error", "created_at", "started_at", "finished_at",
)

_current_ctx = None   # активный контекст шага для кооперативной отмены


class _Cancelled(Exception):
    pass


def connect():
    """Соединение для джоб: state.jobs живёт в state.db, attached к derived."""
    return engine.connect()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pipeline_steps(kind, limit=None):
    """Имена операций пайплайна. limit игнорируется: у операций параметров нет."""
    if kind not in PIPELINES:
        raise KeyError(kind)
    return list(PIPELINES[kind]["steps"])


def enqueue(kind, limit=10):
    if kind in PIPELINES:
        return enqueue_steps(kind, PIPELINES[kind]["title"], pipeline_steps(kind, limit))
    if kind in OPERATIONS:
        return enqueue_steps(kind, kind, [kind])
    raise KeyError(kind)


def enqueue_steps(kind, title, names):
    """Примитив очереди: принимает готовые имена операций. Каталожные пайплайны
    и одиночные операции сходятся здесь — логика постановки одна."""
    with closing(connect()) as db:
        cursor = db.execute(
            "INSERT INTO state.jobs (kind, title, steps, status, created_at)"
            " VALUES (?, ?, ?, 'queued', ?)",
            (kind, title, json.dumps(names, ensure_ascii=False), now()),
        )
        db.commit()
        job_id = cursor.lastrowid
    publish_job(job_id)
    return job_id


def job(job_id):
    with closing(connect()) as db:
        row = db.execute(
            "SELECT * FROM state.jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return as_job(row) if row else None


def recent(limit=20):
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT * FROM state.jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
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
        return db.execute(
            "SELECT log FROM state.jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]


def as_job(row):
    record = dict(zip(COLUMNS, row))
    names = json.loads(record.pop("steps") or "[]")
    log = record.pop("log") or ""
    return {
        **record,
        "steps": [{"name": n, "command": n} for n in names],
        "step_count": len(names),
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
            "SELECT id FROM state.jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE state.jobs SET status = 'running', started_at = ? WHERE id = ?",
            (now(), row[0]),
        )
        db.commit()
        return row[0]


def _raw_steps(job_id):
    """Сырая json-строка steps из state.jobs (не через as_job, который её разбирает)."""
    with closing(connect()) as db:
        return db.execute(
            "SELECT steps FROM state.jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]


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


async def _execute(job_id):
    names = json.loads(_raw_steps(job_id))   # json-строка имён из state.jobs
    for index, name in enumerate(names):
        _update(job_id, step=index)
        ctx, _state = make_context(job_id)
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


def _append_log(job_id, lines):
    with closing(connect()) as db:
        stored = db.execute(
            "SELECT log FROM state.jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
        stored_lines = stored.split("\n") if stored else []
        room = LOG_LIMIT - len(stored_lines)
        if room <= 0:
            return
        stored_lines.extend(lines[:room])
        if len(lines) > room:
            stored_lines.append(f"— вывод длиннее {LOG_LIMIT} строк, дальше не пишем")
        db.execute(
            "UPDATE state.jobs SET log = ? WHERE id = ?",
            ("\n".join(stored_lines), job_id),
        )
        db.commit()


def _update(job_id, **fields):
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with closing(connect()) as db:
        db.execute(
            f"UPDATE state.jobs SET {assignments} WHERE id = ?",
            (*fields.values(), job_id),
        )
        db.commit()
    publish_job(job_id)


def _finish(job_id, status, exit_code=None, error=None):
    _update(job_id, status=status, exit_code=exit_code, error=error, finished_at=now())
    events.publish({"type": "refresh"})


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


def fail_orphans():
    """Перезапуск бэкенда оборвал воркер, но не записи: «running» без воркера —
    ложь оператору. «queued» не трогаем: воркер продолжит их при старте."""
    with closing(connect()) as db:
        db.execute(
            "UPDATE state.jobs SET status = 'failed', error = 'прерван перезапуском бэкенда',"
            " finished_at = ? WHERE status = 'running'",
            (now(),),
        )
        db.commit()


def check_pipelines():
    """Каталог пайплайнов цел: каждый шаг — существующая операция."""
    for kind, pipeline in PIPELINES.items():
        assert pipeline["title"], f"{kind}: нет названия"
        for name in pipeline["steps"]:
            assert name in OPERATIONS, f"{kind}: шаг {name} не в реестре операций"


if __name__ == "__main__":
    check_pipelines()
    print("jobs ok — каталог пайплайнов цел; очередь проверяет pytest")