"""Пульт: прогоны, часы, тумблеры и входящее от лида.

Входящее уезжает в ту же ручку /api/sender/webhook, которую дёргает настоящий
Node: стоп-слова, дедуп и гашение расписания обязаны быть боевыми.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


async def test_creating_over_an_existing_run_answers_409(monkeypatch):
    """Секунда в id разводит прогоны одной компании, но два запроса в одну
    секунду всё равно возможны. Оператор обязан увидеть «уже есть», а не 500
    из-за PRIMARY KEY на номере пула."""
    def occupied(*_args, **_kwargs):
        raise runs.RunExistsError("20260903-090000-c_romashka")

    monkeypatch.setattr(runs, "create", occupied)
    with pytest.raises(HTTPException) as failure:
        await routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    assert failure.value.status_code == 409


async def test_clock_without_a_run_blames_the_run_not_the_preset():
    """Без прогона «непонятный сдвиг» — не та причина отказа, которую надо
    показать оператору."""
    with pytest.raises(HTTPException) as failure:
        await routes.move_clock(routes.ClockMove(preset="чепуха"))
    assert failure.value.status_code == 409


def test_window_preset_is_zero_when_the_window_is_already_open(monkeypatch):
    """Среда, 14:00 в Алматы. Сутки вперёд сожгли бы день календаря прогрева и
    каденции за сдвиг, которого никто не просил."""
    monkeypatch.setattr(clock, "now",
                        lambda: datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))
    assert routes._to_window() == 0


def test_window_preset_lands_inside_the_window_from_outside(monkeypatch):
    saturday = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "now", lambda: saturday)
    landed = (saturday + timedelta(seconds=routes._to_window())).astimezone(
        ZoneInfo("Asia/Almaty"))
    assert landed.isoweekday() == 1
    assert 10 <= landed.hour < 18


def test_window_preset_does_not_hang_on_a_broken_config(monkeypatch):
    """Пустой weekdays вешал бы не запрос, а весь цикл событий: ручка
    асинхронная, а цикл поиска был безграничным."""
    from sender.services import config as sender_config

    broken = dict(sender_config.load())
    broken["window"] = {**broken["window"], "weekdays": []}
    monkeypatch.setattr(sender_config, "load", lambda: broken)
    with pytest.raises(HTTPException) as failure:
        routes._to_window()
    assert failure.value.status_code == 500


async def test_clock_refuses_to_move_the_idle_run():
    """`_idle` — не сценарий, а место, куда песочница смотрит, пока сценария
    нет. activate_latest() делает active() всегда непустым, и проверка «нет
    активного прогона» стала мёртвой: часы двигались на свежей установке,
    сдвигая окно отправки, календарь прогрева и таймеры follow-up всему
    процессу — без прогона, который бы за это отвечал. И переживали
    перезапуск: смещение ложится в мету `_idle`."""
    runs.activate_latest()
    assert runs.active().run_id == runs.IDLE_RUN

    with pytest.raises(HTTPException) as failure:
        await routes.move_clock(routes.ClockMove(preset="hour"))

    assert failure.value.status_code == 409
    assert clock.offset() == timedelta(), "часы процесса уехали на холостом прогоне"


async def test_chat_of_the_idle_run_blames_the_run(monkeypatch):
    """Та же мёртвая проверка в thread_of_active_run: на свежей установке
    оператор получал «треда ещё нет» вместо «прогона ещё нет»."""
    runs.activate_latest()
    with pytest.raises(HTTPException) as failure:
        routes.thread_of_active_run()
    assert failure.value.status_code == 409
    assert "прогон" in failure.value.detail


def test_window_preset_crosses_the_closing_edge(monkeypatch):
    """Среда, 17:50 в Алматы: окно формально открыто, и пресет возвращал ноль.

    Дальше джиттер в 2–15 минут выносит отправку за 18:00, гейт переносит её
    на завтра, и кнопка «к открытию окна» выглядит сломанной ровно на той
    границе, ради которой она есть.
    """
    edge = datetime(2026, 9, 2, 12, 50, tzinfo=timezone.utc)     # 17:50 в Алматы
    monkeypatch.setattr(clock, "now", lambda: edge)
    seconds = routes._to_window()
    assert seconds > 0, "у края окна пресет не сдвинул ничего"
    landed = (edge + timedelta(seconds=seconds)).astimezone(ZoneInfo("Asia/Almaty"))
    assert landed.isoweekday() == 4
    assert 10 <= landed.hour < 18
