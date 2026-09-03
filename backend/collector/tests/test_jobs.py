"""Слой джобов: очередь исполняет операции, провал и отмена не вешают воркера.

Как runs раньше, только состояние в базе: история и прогресс переживают
перезапуск, а «бегущая» джоба видна любому клиенту. Джоба теперь — список имён
операций (services.pipeline), и воркер зовёт их через asyncio.to_thread.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import paths

from collector.services import events, jobs, metrics


def test_pipelines_catalogue_consistent(stores):
    jobs.check_pipelines()


def test_context_carries_job_id(stores):
    job_id = jobs.enqueue_steps("custom", "Тест job_id", ["export"])
    ctx, _state = jobs.make_context(job_id, 0)
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
    assert set(snapshot) == {"sourcing", "writer", "sender", "jobs", "sandbox"}, \
        sorted(snapshot)
    assert set(snapshot["writer"]) == {"threads", "drafts", "sent", "replies"}
    assert snapshot["sender"]["status"] == "live"
    assert set(snapshot["sender"]) == {"status", "numbers", "queue", "threads", "heartbeat"}

    assert paths.state_db().name == "state.db", \
        "путь к невосстановимому слою разошёлся с paths.PRODUCTION_STATE"

    from sender.routes import sender

    assert sender.router.prefix == "/api/sender"
    mounted = {route.path for route in sender.router.routes}
    assert {"/api/sender", "/api/sender/numbers", "/api/sender/queue",
            "/api/sender/autopilot"} <= mounted, mounted

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


def test_plan_steps_lays_out_stages_and_lanes():
    """Строка — стадия из одной дорожки; кортеж кортежей — одна стадия, где
    дорожки идут рядом. Порядок списка — (стадия, дорожка), как в объявлении."""
    steps = jobs.plan_steps(["one", (("a", "b"), ("c",)), "last"])
    assert [(s["name"], s["stage"], s["lane"]) for s in steps] == [
        ("one", 0, 0),
        ("a", 1, 0), ("b", 1, 0),
        ("c", 1, 1),
        ("last", 2, 0),
    ]
    assert all(s["status"] == "pending" and s["progress"] is None for s in steps)


def test_lanes_of_one_stage_run_side_by_side(stores):
    """Две дорожки одной стадии идут одновременно: каждая ждёт события соседа.
    При последовательном исполнении обе не дождутся и тест упадёт."""
    import threading
    from collector.services.pipeline import OPERATIONS

    left_started, right_started = threading.Event(), threading.Event()

    def left(ctx):
        left_started.set()
        assert right_started.wait(timeout=5), "правая дорожка не стартовала"
        return {}

    def right(ctx):
        right_started.set()
        assert left_started.wait(timeout=5), "левая дорожка не стартовала"
        return {}

    OPERATIONS["_left_test"], OPERATIONS["_right_test"] = left, right
    try:
        job_id = jobs.enqueue_steps("custom", "Две дорожки",
                                    [(("_left_test",), ("_right_test",))])
        asyncio.run(jobs.run_pending())
        record = jobs.job(job_id)
        assert record["status"] == "done", record["error"]
        assert {s["status"] for s in record["steps"]} == {"done"}
    finally:
        OPERATIONS.pop("_left_test", None)
        OPERATIONS.pop("_right_test", None)


def test_steps_of_one_lane_run_in_order(stores):
    """Внутри дорожки — строго по очереди: ig_comments читает то, что положила
    в raw/ collect.instagram, и обогнать её не имеет права."""
    from collector.services.pipeline import OPERATIONS

    order = []
    OPERATIONS["_first_test"] = lambda ctx: order.append("first") or {}
    OPERATIONS["_second_test"] = lambda ctx: order.append("second") or {}
    try:
        job_id = jobs.enqueue_steps("custom", "Одна дорожка",
                                    [(("_first_test", "_second_test"),)])
        asyncio.run(jobs.run_pending())
        assert jobs.job(job_id)["status"] == "done"
        assert order == ["first", "second"]
    finally:
        OPERATIONS.pop("_first_test", None)
        OPERATIONS.pop("_second_test", None)


def test_failed_lane_stops_its_neighbour_before_the_job_finishes(stores):
    """asyncio.to_thread не прерывается извне, поэтому упавшая дорожка не имеет
    права уронить джобу, пока соседка ещё в сети: осиротевший поток продолжал
    бы качать страницы и писать лог в уже завершённую джобу."""
    import time
    from collector.services.pipeline import OPERATIONS

    def boom(ctx):
        time.sleep(0.1)          # дать соседке начать
        raise ValueError("ветка упала")

    def patient(ctx):
        while True:
            ctx.check_cancelled()
            time.sleep(0.02)

    OPERATIONS["_boom_lane_test"], OPERATIONS["_patient_lane_test"] = boom, patient
    try:
        job_id = jobs.enqueue_steps("custom", "Падение ветки",
                                    [(("_boom_lane_test",), ("_patient_lane_test",))])
        asyncio.run(asyncio.wait_for(jobs.run_pending(), timeout=10))
        record = jobs.job(job_id)
        assert record["status"] == "failed"
        assert "ветка упала" in record["error"], record["error"]
        statuses = {s["name"]: s["status"] for s in record["steps"]}
        assert statuses["_boom_lane_test"] == "failed", statuses
        assert statuses["_patient_lane_test"] == "cancelled", \
            "соседняя ветка осталась бегущей после того, как джоба помечена упавшей"
    finally:
        OPERATIONS.pop("_boom_lane_test", None)
        OPERATIONS.pop("_patient_lane_test", None)


def test_next_step_of_a_lane_does_not_start_after_a_sibling_failed(stores):
    """Отмена — флаг на джобе, а не только на уже созданных контекстах.

    Сторожить надо именно СЛЕДУЮЩИЙ шаг дорожки: первые шаги обеих дорожек
    стартуют одновременно, это замысел, и помешать соседке начать нельзя.
    А вот collect.ig_comments не имеет права уйти в сеть после того, как
    ветка сайтов уже упала, — иначе падение стоило бы лишних минут запросов.
    """
    import threading
    import time
    from collector.services.pipeline import OPERATIONS

    ran = []
    started = threading.Event()

    def slow_then_ok(ctx):
        started.set()
        time.sleep(0.2)
        return {}

    def boom(ctx):
        assert started.wait(timeout=5)
        raise ValueError("сосед упал")

    OPERATIONS["_slow_ok_test"] = slow_then_ok
    OPERATIONS["_must_not_run_test"] = lambda ctx: ran.append("second") or {}
    OPERATIONS["_boom_neighbour_test"] = boom
    try:
        job_id = jobs.enqueue_steps(
            "custom", "Поздний шаг",
            [(("_slow_ok_test", "_must_not_run_test"), ("_boom_neighbour_test",))])
        asyncio.run(asyncio.wait_for(jobs.run_pending(), timeout=10))

        assert jobs.job(job_id)["status"] == "failed"
        assert ran == [], "второй шаг дорожки стартовал уже после падения соседки"
        statuses = {s["name"]: s["status"] for s in jobs.job(job_id)["steps"]}
        assert statuses["_slow_ok_test"] == "done", statuses
        assert statuses["_must_not_run_test"] == "pending", statuses
    finally:
        for name in ("_slow_ok_test", "_must_not_run_test", "_boom_neighbour_test"):
            OPERATIONS.pop(name, None)


def test_old_jobs_with_plain_string_steps_still_render(stores):
    """В истории лежат джобы, чьи steps — массив строк. Показать их надо, а не
    уронить страницу «Процессы» на первой же старой записи."""
    job_id = jobs.enqueue_steps("custom", "Старая", ["export"])
    jobs._update(job_id, steps=json.dumps(["export", "rebuild"], ensure_ascii=False))
    record = jobs.job(job_id)
    assert [s["name"] for s in record["steps"]] == ["export", "rebuild"]
    assert {s["status"] for s in record["steps"]} == {"done"}
    assert record["step_count"] == 2


def test_step_progress_lands_in_its_own_step(stores):
    """Прогресс пишется в свой шаг: две ветки пишут в одну json-колонку
    одновременно, и цикл «прочитать-склеить-записать» терял бы правку соседа."""
    from collector.services.pipeline import OPERATIONS

    def reporting(ctx):
        ctx.progress(3, 7, "рубрики 2GIS")
        return {}

    OPERATIONS["_progress_test"] = reporting
    try:
        job_id = jobs.enqueue_steps("custom", "Прогресс", ["_progress_test"])
        asyncio.run(jobs.run_pending())
        step = jobs.job(job_id)["steps"][0]
        assert step["progress"] == {"current": 3, "total": 7, "label": "рубрики 2GIS"}
    finally:
        OPERATIONS.pop("_progress_test", None)


def test_discover_runs_instagram_beside_the_rest():
    """Объявление discover: Instagram идёт своей дорожкой рядом с 2GIS и
    сайтами, а rebuild — отдельной стадией после всего сбора."""
    steps = jobs.plan_steps(jobs.PIPELINES["discover"]["steps"])
    by_name = {s["name"]: s for s in steps}

    assert by_name["collect.instagram"]["stage"] == by_name["collect.sites"]["stage"]
    assert by_name["collect.instagram"]["lane"] != by_name["collect.sites"]["lane"]
    assert by_name["collect.ig_comments"]["lane"] == by_name["collect.instagram"]["lane"]
    assert by_name["collect.site_pages"]["lane"] == by_name["collect.sites"]["lane"]

    collecting = max(s["stage"] for s in steps if s["name"].startswith("collect."))
    assert by_name["rebuild"]["stage"] > collecting, \
        "rebuild обязан идти один и после всего сбора — он пересобирает derived прогоном"
