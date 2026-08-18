"""Слой джобов: очередь исполняет операции, провал и отмена не вешают воркера.

Как runs раньше, только состояние в базе: история и прогресс переживают
перезапуск, а «бегущая» джоба видна любому клиенту. Джоба теперь — список имён
операций (services.pipeline), и воркер зовёт их через asyncio.to_thread.
"""

import asyncio
import sys
from pathlib import Path

from services import events, jobs, metrics


def test_pipelines_catalogue_consistent(stores):
    jobs.check_pipelines()


def test_job_lifecycle(stores, tmp_path, monkeypatch):
    """Джоба доходит до done; результат операции попадает в state.jobs."""
    from services.pipeline import export as export_op
    monkeypatch.setattr(export_op, "OUT", tmp_path / "leads.csv")   # не трогать боевой CSV
    job_id = jobs.enqueue_steps("custom", "Тест", ["export"])
    asyncio.run(jobs.run_pending())
    record = jobs.job(job_id)
    assert record["status"] == "done"
    assert record["result"] is not None      # json результата export.run


def test_job_failure_is_typed(stores):
    job_id = jobs.enqueue_steps("custom", "Провал", ["нет_такой_операции"])
    asyncio.run(jobs.run_pending())
    record = jobs.job(job_id)
    assert record["status"] == "failed"
    assert "KeyError" in record["error"] or "нет_такой" in record["error"]


def test_job_cancel(stores):
    """Отмена running кооперативна: флаг в контексте, операция выходит сама."""
    from services import jobs as jobs_module
    from services.pipeline import OPERATIONS

    def slow(ctx):
        import time
        while True:
            ctx.check_cancelled()
            time.sleep(0.05)

    OPERATIONS["_slow_test"] = slow
    try:
        long = jobs_module.enqueue_steps("custom", "Долгая", ["_slow_test"])
        queued = jobs_module.enqueue_steps("custom", "Вслед", ["_slow_test"])

        async def cancel_when_running():
            while jobs_module.job(long)["status"] != "running":
                await asyncio.sleep(0.05)
            jobs_module.cancel(long)

        async def scenario():
            running = asyncio.ensure_future(jobs_module.run_pending())
            await asyncio.gather(running, cancel_when_running())

        asyncio.run(scenario())
        assert jobs_module.job(long)["status"] == "cancelled", jobs_module.job(long)
        assert jobs_module.job(queued)["status"] == "queued", "отмена задела чужую джобу"

        assert jobs_module.cancel(queued)["status"] == "cancelled"
        assert asyncio.run(jobs_module.run_pending()) is None, "отменённая джоба исполнилась"
    finally:
        OPERATIONS.pop("_slow_test", None)


def test_job_orphans_fail_on_restart(stores):
    job_id = jobs.enqueue_steps("custom", "Сирота", ["export"])
    jobs._update(job_id, status="running")
    jobs.fail_orphans()
    assert jobs.job(job_id)["status"] == "failed", "перезапуск оставил бы джобу «бегущей»"


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