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
import logging
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import activity
import logctx
from collector.services import events, store as engine
from collector.services.pipeline import OPERATIONS, PIPELINES

logger = logging.getLogger(__name__)

# Потолок лога в символах, а не в строках: строку дописывает сам SQLite
# (log = log || ?), и мерить длину он умеет, а считать переводы строки — нет.
LOG_LIMIT_CHARS = 2_000_000
OVERFLOW_NOTE = "\n— лог длиннее потолка, дальше не пишем"

_current_ctxs = {}    # job_id -> контексты бегущих шагов: дорожек может быть несколько
_cancelled = set()    # job_id, которым уже пришла отмена


class _Cancelled(Exception):
    pass


def connect():
    """Соединение для джоб: state.jobs живёт в state.db, attached к derived."""
    return engine.connect()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pipeline_steps(kind):
    """Имена операций пайплайна."""
    if kind not in PIPELINES:
        raise KeyError(kind)
    return list(PIPELINES[kind]["steps"])


def enqueue_pipeline(kind):
    """Поставить пайплайн: несколько операций одной джобой."""
    return enqueue_steps(kind, PIPELINES[kind]["title"], pipeline_steps(kind))


def enqueue_operation(name):
    """Поставить одну операцию.

    Отдельно от пайплайна, а не общим enqueue с поиском по обоим реестрам:
    имена пересекаются («rebuild» — и операция, и пайплайн), и общий поиск
    молча ставил бы пайплайн там, где просили операцию.
    """
    if name not in OPERATIONS:
        raise KeyError(name)
    return enqueue_steps(name, name, [name])


def plan_steps(declaration):
    """Объявление шагов -> плоский список {name, stage, lane, status, progress}.

    Строка — стадия из одной дорожки; кортеж — одна стадия, где каждый элемент
    идёт своей дорожкой параллельно соседям, а строки внутри дорожки — по
    очереди. Плоский список, а не дерево, ровно по одной причине: его читает
    фронтенд и рисует списком, и вложенность стоила бы ему рекурсивного
    компонента ради двух веток. Порядок — (стадия, дорожка), как в объявлении.
    """
    steps = []
    for stage, entry in enumerate(declaration):
        lanes = entry if isinstance(entry, (tuple, list)) else (entry,)
        for lane, chain in enumerate(lanes):
            for name in ((chain,) if isinstance(chain, str) else chain):
                steps.append({"name": name, "stage": stage, "lane": lane,
                              "status": "pending", "progress": None})
    return steps


def enqueue_steps(kind, title, names):
    """Примитив очереди: принимает объявление шагов. Каталожные пайплайны и
    одиночные операции сходятся здесь — логика постановки одна."""
    with closing(connect()) as db:
        cursor = db.execute(
            "INSERT INTO state.jobs (kind, title, steps, status, created_at)"
            " VALUES (?, ?, ?, 'queued', ?)",
            (kind, title, json.dumps(plan_steps(names), ensure_ascii=False), now()),
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
    """Строка state.jobs -> то, что видит фронт. Порядок колонок берётся у самой
    строки (row_factory=Row), а не из копии списка рядом со схемой.

    step — число завершённых шагов, а не индекс текущего: в стадии с двумя
    дорожками текущих шагов два, и одним индексом это не выражается. Колонка
    state.jobs.step больше не заполняется и осталась в схеме мёртвой, как
    exit_code от эпохи subprocess.

    Прогресс живёт в шагах, а не в джобе: у стадии с двумя дорожками их два.
    Колонка state.jobs.progress больше не заполняется и осталась в схеме
    мёртвой — как step и exit_code. Миграции ради трёх мёртвых колонок не
    затевается: state.db невосстановима, и ALTER на ней стоит дороже, чем
    строка в докстринге.
    """
    record = dict(row)
    steps = [as_step(entry) for entry in json.loads(record.pop("steps") or "[]")]
    log = record.pop("log") or ""
    return {
        **record,
        "steps": steps,
        "step": sum(1 for step in steps if step["status"] == "done"),
        "step_count": len(steps),
        "log_lines": log.count("\n") + 1 if log else 0,
    }


def as_step(entry):
    """Шаг для фронта. Строка — формат до стадий и дорожек: такие джобы лежат
    в истории, и показать их надо, а не уронить страницу на первой же."""
    if isinstance(entry, str):
        return {"name": entry, "command": entry, "stage": 0, "lane": 0,
                "status": "done", "progress": None}
    return {**entry, "command": entry["name"]}


def _set_step(job_id, index, **fields):
    """Правит один шаг в json-колонке steps.

    Правит SQLite (json_set), а не Python: две дорожки пишут в одну колонку
    одновременно, и цикл «прочитать-склеить-записать» терял бы правку соседа.
    """
    assignments = ", ".join(f"'$[{index}].{name}', json(?)" for name in fields)
    values = [json.dumps(value, ensure_ascii=False) for value in fields.values()]
    with closing(connect()) as db:
        db.execute(
            f"UPDATE state.jobs SET steps = json_set(steps, {assignments}) WHERE id = ?",
            (*values, job_id),
        )
        db.commit()
    publish_job(job_id)


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
    activity.record("jobs", "started", subject=str(job_id))
    await _execute(job_id)
    activity.record("jobs", "finished", subject=str(job_id))
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


def make_context(job_id, step_index):
    """Контекст одного шага. Отмена читается и из своего состояния, и из
    множества отменённых джоб: дорожка, стартовавшая через миллисекунду после
    падения соседки, обязана увидеть отмену, которой при её создании ещё не
    было ни в одном контексте."""
    state = {"cancelled": False}

    def check_cancelled():
        if state["cancelled"] or job_id in _cancelled:
            raise _Cancelled()

    def progress(current, total, label):
        _set_step(job_id, step_index,
                  progress={"current": current, "total": total, "label": label})

    def log(message):
        _append_log(job_id, [message])
        events.publish({"type": "log", "job_id": job_id, "lines": [message]})

    return SimpleNamespace(
        check_cancelled=check_cancelled, progress=progress, log=log,
        cancel=lambda: state.update(cancelled=True),
        job_id=job_id,
    ), state


async def _execute(job_id):
    steps = json.loads(_raw_steps(job_id))
    with logctx.job(job_id):
        for stage in sorted({step["stage"] for step in steps}):
            lanes = {}
            for index, step in enumerate(steps):
                if step["stage"] == stage:
                    lanes.setdefault(step["lane"], []).append(index)

            tasks = [asyncio.ensure_future(_run_lane(job_id, steps, indexes))
                     for indexes in lanes.values()]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure = next((task.exception() for task in done if task.exception()), None)
            if pending:
                # to_thread не прерывается извне: соседкам ставится флаг, и мы
                # ждём, пока они выйдут сами. Пометить джобу раньше — значит
                # оставить поток, пишущий страницы и лог в завершённую джобу.
                _cancelled.add(job_id)
                await asyncio.wait(pending)
                failure = failure or next(
                    (task.exception() for task in pending if task.exception()), None)
            if failure is None:
                continue
            _cancelled.discard(job_id)
            if isinstance(failure, _Cancelled):
                _finish(job_id, "cancelled")
            else:
                _finish(job_id, "failed", error=f"{type(failure).__name__}: {failure}")
            return
    _finish(job_id, "done")


async def _run_lane(job_id, steps, indexes):
    """Шаги одной дорожки — строго по очереди. Первая же ошибка выходит наружу:
    её ловит стадия и решает судьбу соседних дорожек."""
    for index in indexes:
        name = steps[index]["name"]
        if job_id in _cancelled:
            raise _Cancelled()
        if name not in OPERATIONS:
            raise KeyError(f"нет операции {name}")
        ctx, _state = make_context(job_id, index)
        _current_ctxs.setdefault(job_id, []).append(ctx)
        _set_step(job_id, index, status="running")
        try:
            result = await asyncio.to_thread(OPERATIONS[name], ctx)
        except _Cancelled:
            _set_step(job_id, index, status="cancelled")
            raise
        except Exception:
            # Полный traceback пишется здесь, а не в стадии: имя упавшего шага
            # известно только тут, а state.jobs.error хранит короткую строку
            # для фронтенда. Без этой записи причину провала можно гадать
            # только по типу и сообщению.
            logger.exception(f"джоба {job_id}, шаг {name} упала")
            _set_step(job_id, index, status="failed")
            raise
        finally:
            _current_ctxs.get(job_id, []).remove(ctx)
        _set_step(job_id, index, status="done")
        _update(job_id, result=json.dumps(result, ensure_ascii=False))


def _append_log(job_id, lines):
    """Дописать строки в конец лога.

    Дописывает SQLite (log = log || ?), а не Python: читать весь лог, склеивать
    и писать обратно на каждой строке — квадрат по длине лога, и на длинном
    сборе это заметно. Потолок держит то же выражение: за ним UPDATE просто не
    находит строки, и один раз дописывается пометка.
    """
    chunk = "\n".join(lines)
    with closing(connect()) as db:
        written = db.execute(
            "UPDATE state.jobs SET log = CASE WHEN log = '' THEN ? ELSE log || ? END"
            " WHERE id = ? AND length(log) < ?",
            (chunk, "\n" + chunk, job_id, LOG_LIMIT_CHARS),
        ).rowcount
        if not written:
            db.execute(
                "UPDATE state.jobs SET log = log || ? WHERE id = ?"
                " AND length(log) >= ? AND log NOT LIKE ?",
                (OVERFLOW_NOTE, job_id, LOG_LIMIT_CHARS, f"%{OVERFLOW_NOTE}"),
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


def _finish(job_id, status, error=None):
    """Кода возврата у операции нет: она возвращает dict или бросает исключение.
    Колонка exit_code осталась от эпохи subprocess и больше не заполняется."""
    _cancelled.discard(job_id)
    _current_ctxs.pop(job_id, None)
    _update(job_id, status=status, error=error, finished_at=now())
    events.publish({"type": "refresh"})


def cancel(job_id):
    """Снимает queued мгновенно; running — кооперативно, между единицами работы."""
    record = job(job_id)
    if not record:
        raise KeyError(job_id)
    if record["status"] == "queued":
        _finish(job_id, "cancelled")
        return job(job_id)
    if record["status"] == "running":
        _cancelled.add(job_id)                       # увидят и те дорожки, что ещё не стартовали
        for ctx in list(_current_ctxs.get(job_id, ())):
            ctx.cancel()
    return job(job_id)


def cancel_current():
    """Кооперативно останавливает текущую running-джобу — зовётся при остановке
    бэкенда: без этого фоновый поток операции (asyncio.to_thread) продолжает
    молотить весь оставшийся список доменов, а Python не отпускает процесс,
    пока этот поток не завершится сам."""
    for job_id, contexts in list(_current_ctxs.items()):
        _cancelled.add(job_id)
        for ctx in list(contexts):
            ctx.cancel()


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
        for step in plan_steps(pipeline["steps"]):
            assert step["name"] in OPERATIONS, \
                f"{kind}: шаг {step['name']} не в реестре операций"


if __name__ == "__main__":
    check_pipelines()
    print("jobs ok — каталог пайплайнов цел; очередь проверяет pytest")