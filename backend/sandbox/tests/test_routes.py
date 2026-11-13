"""Пульт: прогоны, часы, тумблеры и входящее от лида.

Входящее уезжает в ту же ручку /api/sender/webhook, которую дёргает настоящий
Node: стоп-слова, дедуп и гашение расписания обязаны быть боевыми.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

import clock
import paths
from core import db as core_db
from sandbox import faults, node, routes, runs
from conversations.tests.leads import add_lead

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
LEADS = ("c_romashka", "c_one", "c_two")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Прогоны и боевая база — в tmp_path; в боевой три лида research."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")
    core_db.migrate(paths.PRODUCTION_STATE)
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        for n, company_id in enumerate(LEADS, 1):
            add_lead(db, company_id, [("whatsapp", f"+7701000000{n}")])
    sent = []

    def deliver(payload: dict) -> bool:
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
        db.execute("INSERT INTO threads (company_id, channel, seed, created_at)"
                   " VALUES ('c_romashka', ?, '{}', '2026-09-03T09:00:00+00:00')",
                   (thread_id,))
    return run


def test_create_run_returns_it_active():
    body = routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    assert body["run"]["company_id"] == "c_romashka"
    assert runs.active().run_id == body["run"]["run_id"]


def test_list_runs_marks_the_active_one():
    first = routes.create_run(routes.NewRun(company_id="c_one", warmed=True))
    routes.create_run(routes.NewRun(company_id="c_two", warmed=True))
    routes.activate_run(first["run"]["run_id"])
    listed = (routes.list_runs())["runs"]
    assert [run["active"] for run in listed].count(True) == 1
    assert next(run for run in listed if run["active"])["company_id"] == "c_one"


def test_clock_preset_moves_the_clock():
    routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    body = routes.move_clock(routes.ClockMove(preset="day"))
    assert clock.offset() == timedelta(days=1)
    assert body["offset_seconds"] == 86400


def test_clock_accepts_raw_seconds():
    routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    routes.move_clock(routes.ClockMove(seconds=900))
    assert clock.offset() == timedelta(minutes=15)


def test_clock_to_the_window_lands_inside_it():
    """«К открытию окна» обязано попадать внутрь окна отправки, иначе кнопка
    врёт: гейт снова скажет «не время»."""
    routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    routes.move_clock(routes.ClockMove(preset="window"))
    almaty = clock.now() + timedelta(hours=5)      # Asia/Almaty = UTC+5
    assert 10 <= almaty.hour < 18
    assert almaty.isoweekday() <= 5


def test_faults_round_trip():
    routes.set_faults(routes.NewFaults(send="not_sent"))
    assert (routes.get_faults())["send"] == "not_sent"
    assert (routes.get_faults())["delivery"] == "delivered", "частичное обновление"


def test_unknown_fault_value_is_a_422():
    with pytest.raises(HTTPException) as failure:
        routes.set_faults(routes.NewFaults(send="куда-то"))
    assert failure.value.status_code == 422


def test_incoming_goes_through_the_real_webhook(sandbox):
    _run_with_thread()
    routes.incoming(routes.NewIncoming(text="сколько стоит?"))
    assert sandbox[-1]["kind"] == "incoming"
    assert sandbox[-1]["from"] == "77010000001@s.whatsapp.net"
    assert sandbox[-1]["text"] == "сколько стоит?"
    assert sandbox[-1]["provider_id"]


def test_incoming_without_a_thread_is_a_409():
    runs.activate(runs.create("c_romashka", warmed=True, moment=MOMENT).run_id)
    with pytest.raises(HTTPException) as failure:
        routes.incoming(routes.NewIncoming(text="привет"))
    assert failure.value.status_code == 409


def test_everything_needs_an_active_run():
    for call in (lambda: routes.move_clock(routes.ClockMove(preset="day")),
                 lambda: routes.incoming(routes.NewIncoming(text="привет"))):
        with pytest.raises(HTTPException) as failure:
            call()
        assert failure.value.status_code == 409


def test_creating_over_an_existing_run_answers_409(monkeypatch):
    """Секунда в id разводит прогоны одной компании, но два запроса в одну
    секунду всё равно возможны. Оператор обязан увидеть «уже есть», а не 500
    из-за PRIMARY KEY на номере пула."""
    def occupied(*_args, **_kwargs):
        raise runs.RunExistsError("20260903-090000-c_romashka")

    monkeypatch.setattr(runs, "create", occupied)
    with pytest.raises(HTTPException) as failure:
        routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    assert failure.value.status_code == 409


def test_clock_without_a_run_blames_the_run_not_the_preset():
    """Без прогона «непонятный сдвиг» — не та причина отказа, которую надо
    показать оператору."""
    with pytest.raises(HTTPException) as failure:
        routes.move_clock(routes.ClockMove(preset="чепуха"))
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


def test_window_preset_does_not_hang_on_a_window_that_never_opens(monkeypatch):
    """Схема не пропускает пустой weekdays, но поиск окна всё равно ограничен."""
    from delivery.schemas import DeliverySettings, Window

    never = DeliverySettings.model_construct(
        window=Window.model_construct(hours=(10, 18), weekdays=[]))
    monkeypatch.setattr(routes.delivery, "settings", lambda db: never)
    with pytest.raises(HTTPException) as failure:
        routes._to_window()
    assert failure.value.status_code == 500


def test_clock_refuses_to_move_the_idle_run():
    """`_idle` — не сценарий, а место, куда песочница смотрит, пока сценария
    нет. activate_latest() делает active() всегда непустым, и проверка «нет
    активного прогона» стала мёртвой: часы двигались на свежей установке,
    сдвигая окно отправки, календарь прогрева и таймеры follow-up всему
    процессу — без прогона, который бы за это отвечал. И переживали
    перезапуск: смещение ложится в мету `_idle`."""
    runs.activate_latest()
    assert runs.active().run_id == runs.IDLE_RUN

    with pytest.raises(HTTPException) as failure:
        routes.move_clock(routes.ClockMove(preset="hour"))

    assert failure.value.status_code == 409
    assert clock.offset() == timedelta(), "часы процесса уехали на холостом прогоне"


def test_chat_of_the_idle_run_blames_the_run(monkeypatch):
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


def test_runs_listing_carries_the_stand_clock():
    """Время стенда приезжает с бэкендом, а не хранится во фронте.

    Фронт держал его в состоянии, писал только кнопками сдвига и не сбрасывал
    ни при перезагрузке, ни при смене прогона: подвинув часы прогона A и
    переключившись на B, оператор видел время A рядом с «сдвиг нет» — два
    противоречащих числа на одной карточке."""
    run = routes.create_run(routes.NewRun(company_id="c_romashka", warmed=True))
    routes.move_clock(routes.ClockMove(preset="day"))

    listing = routes.list_runs()

    assert "now" in listing, "во фронте нечем показать время стенда, кроме памяти"
    moved = datetime.fromisoformat(listing["now"])
    assert moved - datetime.now(timezone.utc) > timedelta(hours=23)
    assert listing["runs"][0]["run_id"] == run["run"]["run_id"]


def test_a_run_is_made_only_for_a_research_lead():
    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        add_lead(db, "c_no_hook", [("whatsapp", "+77010000009")], hooks=[])

    for company_id, status in (("c_nobody", 404), ("c_no_hook", 409), ("../../evil", 404)):
        with pytest.raises(HTTPException) as failure:
            routes.create_run(routes.NewRun(company_id=company_id, warmed=True))
        assert failure.value.status_code == status, company_id
    assert runs.all() == []


def test_the_run_drafts_its_lead_from_research_into_its_own_database(monkeypatch):
    """Лид прогона — настоящая компания: досье читается из боевой базы, а тред
    ложится в базу прогона. Двоеточие в id компании — как у 2GIS."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from conversations import api, dependencies
    from core.model import FakeModel

    with closing(core_db.connect(paths.PRODUCTION_STATE)) as db:
        add_lead(db, "2gis:7000", [("whatsapp", "+77010000005")])
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[dependencies.build_model] = lambda: FakeModel(
        {"text": "Здравствуйте", "angle": "other"})

    run = (routes.create_run(routes.NewRun(company_id="2gis:7000", warmed=True)))["run"]
    response = TestClient(app).post("/api/threads/2gis:7000/draft", json={"kind": "first"})

    assert response.status_code == 200
    with closing(sqlite3.connect(runs.get(run["run_id"]).path)) as db:
        seeds = db.execute("SELECT channel, json_extract(seed, '$.name') FROM threads").fetchall()
    assert seeds == [("+77010000005", "Компания 2gis:7000")]
    with closing(sqlite3.connect(paths.PRODUCTION_STATE)) as db:
        assert db.execute("SELECT count(*) FROM threads").fetchone() == (0,)


def test_the_picker_lists_research_leads_of_the_production_database():
    """База прогона пуста: research живёт в боевой, и выбор лида читается оттуда."""
    runs.activate(runs.create("c_romashka", warmed=True, moment=MOMENT).run_id)

    leads = routes.list_leads()

    assert [(lead["company_id"], lead["channel"]["handle"]) for lead in leads] == [
        ("c_one", "+77010000002"), ("c_romashka", "+77010000001"), ("c_two", "+77010000003")]
