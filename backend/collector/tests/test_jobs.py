"""Слой джобов: очередь исполняет операции, провал и отмена не вешают воркера.

Как runs раньше, только состояние в базе: история и прогресс переживают
перезапуск, а «бегущая» джоба видна любому клиенту. Джоба теперь — список имён
операций (services.pipeline), и воркер зовёт их через asyncio.to_thread.
"""

import asyncio
import logging
import sys
from pathlib import Path

from collector.services import events, jobs, metrics


def test_pipelines_catalogue_consistent(stores):
    jobs.check_pipelines()


def test_context_carries_job_id(stores):
    job_id = jobs.enqueue_steps("custom", "Тест job_id", ["export"])
    ctx, _state = jobs.make_context(job_id)
    assert ctx.job_id == job_id


def test_job_lifecycle(stores, tmp_path, monkeypatch):
    """Джоба доходит до done; результат операции попадает в state.jobs."""
    from collector.services.pipeline import export as export_op
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
    from collector.services import jobs as jobs_module
    from collector.services.pipeline import OPERATIONS

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


def test_frontend_contract(stores):
    """Контракт фронтенда: снапшот счётчиков знает все три системы, у системы 3
    есть живой пул номеров на своём адресе."""
    snapshot = metrics.snapshot()
    assert set(snapshot) == {"sourcing", "writer", "sender", "jobs"}, sorted(snapshot)
    assert set(snapshot["writer"]) == {"threads", "drafts", "sent", "replies"}
    assert snapshot["sender"]["status"] == "live"

    assert metrics.threads_db_path().name == "state.db", \
        "путь state.db разошёлся с config.toml системы 2"

    from sender.routes import sender

    assert sender.router.prefix == "/api/sender"
    paths = {route.path for route in sender.router.routes}
    assert "/api/sender" in paths and "/api/sender/numbers" in paths, paths

def test_publish_from_worker_thread_reaches_subscriber():
    """Событие из рабочего потока доходит до ждущего подписчика сразу.

    Операции идут через asyncio.to_thread и шлют оттуда прогресс и лог.
    put_nowait из чужого потока цикл не будит: событие лежит в очереди, но
    ожидающий queue.get() просыпается только когда цикл проснётся сам по себе.
    Замер это и ловит — раньше событие приходило ровно на таймауте ожидания
    (5.005 с из 5), теперь за пятьдесят миллисекунд.
    """
    import threading
    import time

    async def scenario():
        async with events.subscribe() as queue:
            started = time.perf_counter()
            threading.Timer(0.05, events.publish,
                            args=({"type": "log", "lines": ["из потока"]},)).start()
            event = await asyncio.wait_for(queue.get(), timeout=5)
            waited = time.perf_counter() - started
            assert event["lines"] == ["из потока"], event
            assert waited < 1, f"событие из потока ждало {waited:.2f} с — цикл его не заметил"

    asyncio.run(scenario())


def test_log_append_does_not_rewrite_whole_log(stores):
    """Строки дописываются в конец, а не переписывают лог целиком."""
    job_id = jobs.enqueue_steps("custom", "Лог", ["export"])
    jobs._append_log(job_id, ["первая"])
    jobs._append_log(job_id, ["вторая", "третья"])
    assert jobs.log_of(job_id) == "первая\nвторая\nтретья"
    assert jobs.job(job_id)["log_lines"] == 3


def test_log_stops_at_limit_with_a_notice(stores, monkeypatch):
    """За потолком лог не растёт, но оператор видит, что его обрезали."""
    monkeypatch.setattr(jobs, "LOG_LIMIT_CHARS", 20)
    job_id = jobs.enqueue_steps("custom", "Лог", ["export"])
    for _ in range(5):
        jobs._append_log(job_id, ["строка подлиннее потолка"])
    log = jobs.log_of(job_id)
    assert log.count("строка подлиннее потолка") == 1, log
    assert log.endswith(jobs.OVERFLOW_NOTE), log


def test_operation_and_pipeline_names_do_not_collide(stores):
    """«rebuild» — и операция, и пайплайн: каждая точка входа ставит своё.

    Общий enqueue искал имя сначала среди пайплайнов, поэтому
    POST /api/operations/rebuild молча ставил пайплайн из двух шагов —
    операцию rebuild было не вызвать вовсе.
    """
    assert "rebuild" in jobs.OPERATIONS and "rebuild" in jobs.PIPELINES

    one = jobs.job(jobs.enqueue_operation("rebuild"))
    assert [s["name"] for s in one["steps"]] == ["rebuild"], one["steps"]

    whole = jobs.job(jobs.enqueue_pipeline("rebuild"))
    assert [s["name"] for s in whole["steps"]] == ["rebuild", "export"], whole["steps"]


def test_job_step_exception_logs_full_traceback(stores, caplog):
    """Необработанное исключение в шаге — не только запись в state.jobs.error,
    но и полный traceback в логе (backend.log в проде), иначе причину провала
    можно только гадать по типу+сообщению."""
    from collector.services.pipeline import OPERATIONS

    def boom(ctx):
        raise ValueError("детально сломалось на компании adelex.kz")

    OPERATIONS["_boom_test"] = boom
    try:
        job_id = jobs.enqueue_steps("custom", "Провал с трейсбэком", ["_boom_test"])
        with caplog.at_level(logging.ERROR, logger="collector.services.jobs"):
            asyncio.run(jobs.run_pending())

        record = jobs.job(job_id)
        assert record["status"] == "failed"
        assert "детально сломалось" in record["error"]

        tracebacks = [r for r in caplog.records if r.exc_info is not None]
        assert tracebacks, "исключение упало без exc_info — traceback потерян"
        assert "ValueError" in tracebacks[0].getMessage() or "ValueError" in str(tracebacks[0].exc_info)
    finally:
        OPERATIONS.pop("_boom_test", None)


def test_job_context_carries_job_id_during_step(stores):
    """logctx видит job_id ровно во время исполнения шага, не до и не после."""
    import logctx
    from collector.services.pipeline import OPERATIONS

    seen = []

    def probe(ctx):
        seen.append(logctx.current_job_id())
        return {}

    OPERATIONS["_probe_test"] = probe
    try:
        job_id = jobs.enqueue_steps("custom", "Проба контекста", ["_probe_test"])
        assert logctx.current_job_id() is None
        asyncio.run(jobs.run_pending())
        assert seen == [job_id]
        assert logctx.current_job_id() is None, "контекст не сброшен после джобы"
    finally:
        OPERATIONS.pop("_probe_test", None)
