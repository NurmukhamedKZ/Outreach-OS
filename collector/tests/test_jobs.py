"""Слой джобов: очередь исполняет, провал и отмена не вешают воркера.

Как runs раньше, только состояние в базе: история и прогресс переживают
перезапуск, а «бегущая» джоба видна любому клиенту.
"""

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from services import events, jobs, metrics


@contextmanager
def temp_ops_db():
    """Подменяет базу джобов на временную: проверки не пачкают историю запусков."""
    original = jobs.OPS_DB
    tmp = TemporaryDirectory()
    jobs.OPS_DB = Path(tmp.name) / "ops.db"
    try:
        yield
    finally:
        jobs.OPS_DB = original
        tmp.cleanup()


def step(script):
    return {"name": script[:30], "argv": ["python", "-c", script], "cwd": Path.cwd()}


def test_pipelines_catalogue_consistent():
    jobs.check_pipelines()


def test_job_lifecycle():
    """Успех, провал и #progress — три исхода шага, каждый виден в базе."""
    with temp_ops_db():
        ok = jobs.enqueue_steps("custom", "Успешная", [step("print('привет')")])
        assert asyncio.run(jobs.run_pending()) == ok, "воркер взял не свою джобу"
        assert jobs.job(ok)["status"] == "done", jobs.job(ok)
        assert "привет" in jobs.tail(ok)["lines"], "вывод шага не дошёл до лога"

        failed = jobs.enqueue_steps("custom", "Провальная", [step("raise SystemExit(3)")])
        asyncio.run(jobs.run_pending())
        record = jobs.job(failed)
        assert record["status"] == "failed" and record["exit_code"] == 3, record
        assert "не прошёл" in record["error"], record["error"]

        progress = "print('#progress " + '{"current": 3, "total": 12}' + "')"
        with_progress = jobs.enqueue_steps("custom", "С прогрессом", [step(progress)])
        asyncio.run(jobs.run_pending())
        assert jobs.job(with_progress)["progress"] == {"current": 3, "total": 12}, \
            jobs.job(with_progress)["progress"]

        missing = jobs.enqueue_steps("custom", "Без команды", [
            {"name": "нет такой", "argv": ["нет-такой-команды"], "cwd": Path.cwd()}
        ])
        asyncio.run(jobs.run_pending())
        assert jobs.job(missing)["status"] == "failed", "провал запуска повесил бы очередь"


def test_job_cancel():
    """Отмена running убивает процесс, queued снимается без запуска."""
    with temp_ops_db():
        long = jobs.enqueue_steps("custom", "Долгая", [step("import time; time.sleep(30)")])
        queued = jobs.enqueue_steps("custom", "Вслед", [step("print('не должен был')")])

        async def cancel_when_running():
            while jobs._current is None:
                await asyncio.sleep(0.05)
            jobs.cancel(long)

        async def scenario():
            running = asyncio.ensure_future(jobs.run_pending())
            await asyncio.gather(running, cancel_when_running())

        asyncio.run(scenario())
        assert jobs.job(long)["status"] == "cancelled", jobs.job(long)
        assert jobs.job(queued)["status"] == "queued", "отмена задела чужую джобу"

        assert jobs.cancel(queued)["status"] == "cancelled"
        assert asyncio.run(jobs.run_pending()) is None, "отменённая джоба исполнилась"


def test_job_orphans_fail_on_restart():
    with temp_ops_db():
        orphan = jobs.enqueue_steps("custom", "Сирота", [step("pass")])
        jobs._update(orphan, status="running")
        jobs.fail_orphans()
        assert jobs.job(orphan)["status"] == "failed", "перезапуск оставил бы джобу «бегущей»"


def test_events_broker():
    """Событие доходит подписчику; шина не теряет издателя при пустой подписке."""
    async def scenario():
        async with events.subscribe() as queue:
            events.publish({"type": "ping"})
            assert await asyncio.wait_for(queue.get(), timeout=1) == {"type": "ping"}, \
                "событие не дошло до подписчика"
        events.publish({"type": "ping"})  # подписчиков нет — и это не ошибка

    asyncio.run(scenario())


def test_frontend_contract():
    """Контракт фронтенда: снапшот счётчиков знает все три системы, у стаба
    системы 3 есть адрес и честный 501."""
    snapshot = metrics.snapshot()
    assert set(snapshot) == {"sourcing", "writer", "sender", "jobs"}, sorted(snapshot)
    assert set(snapshot["writer"]) == {"threads", "drafts", "sent", "replies"}
    assert snapshot["sender"]["status"] == "coming_soon"

    assert metrics.threads_db_path().name == "state.db", \
        "путь state.db разошёлся с config.toml системы 2"

    sys.path.insert(0, str(Path("sender").resolve().parent.parent / "sender"))
    try:
        import stub as sender

        assert sender.status()["status"] == "coming_soon"
        paths = {route.path for route in sender.router.routes}
        assert "/api/sender" in paths and "/api/sender/{rest_of_path:path}" in paths, paths
    finally:
        sys.path.pop(0)