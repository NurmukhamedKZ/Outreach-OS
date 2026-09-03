"""Прогон — отдельный файл базы: чистота контекста обеспечена физически."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import clock
import paths
from sandbox import runs

MOMENT = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Прогоны уезжают в tmp_path: тест, создавший файл в data/sandbox/,
    остался бы в репозитории навсегда."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "sandbox")
    yield
    runs.deactivate()


def test_create_makes_a_database_with_all_three_schemas():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    assert run.path.exists()
    with sqlite3.connect(run.path) as db:
        tables = {row[0] for row in
                  db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"threads", "messages", "outbox", "numbers", "suppression",
            "sandbox_meta"} <= tables


def test_create_seeds_one_number_warmed_or_new():
    warm = runs.create("c_warm", warmed=True, moment=MOMENT)
    cold = runs.create("c_cold", warmed=False, moment=MOMENT)
    assert _number_status(warm) == "active"
    assert _number_status(cold) == "new"


def test_run_id_carries_the_company_and_the_moment():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    assert run.run_id == "20260903-090000-c_romashka"
    assert run.company_id == "c_romashka"
    assert run.warmed is True


def test_creating_over_an_existing_run_is_refused():
    """Создание поверх затёрло бы мету старого прогона (часы — в ноль), а
    таблицы оставило бы от прошлого сценария: ровно тот грязный контекст,
    ради которого прогон и сделан файлом."""
    runs.create("c_romashka", warmed=True, moment=MOMENT)
    with pytest.raises(runs.RunExistsError):
        runs.create("c_romashka", warmed=True, moment=MOMENT)


def test_activate_latest_takes_the_newest_run():
    runs.create("c_one", warmed=True, moment=MOMENT)
    new = runs.create("c_two", warmed=True, moment=MOMENT + timedelta(minutes=1))
    assert runs.activate_latest().run_id == new.run_id


def test_activate_latest_without_runs_lands_on_the_idle_base():
    """Песочница обязана смотреть на свою базу с первой секунды процесса:
    иначе фоновые задачи работают с боевой перепиской через подменный
    транспорт."""
    run = runs.activate_latest()
    assert run.run_id == runs.IDLE_RUN
    assert paths.state_db() == run.path
    assert paths.state_db() != paths.PRODUCTION_STATE


def test_idle_base_is_not_a_scenario():
    runs.activate_latest()
    assert runs.all() == []


def test_two_runs_of_one_company_do_not_share_history():
    """Ради этого прогон и сделан файлом: thread_id — это номер телефона и
    первичный ключ threads, второй прогон по тому же лиду упёрся бы в него."""
    first = runs.create("c_romashka", warmed=True, moment=MOMENT)
    second = runs.create("c_romashka", warmed=True,
                         moment=MOMENT + timedelta(minutes=1))
    assert first.path != second.path
    _open_thread(first, "+77010000001")
    assert _threads(second) == []


def test_activate_switches_every_seam():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    assert paths.state_db() == run.path
    assert runs.active().run_id == run.run_id


def test_deactivate_returns_the_production_database():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.deactivate()
    assert paths.state_db() == paths.PRODUCTION_STATE
    assert runs.active() is None
    assert clock.offset() == timedelta()


def test_shift_moves_the_clock_and_survives_reactivation():
    """Смещение принадлежит прогону, а не процессу: иначе прогон, начатый
    вчера с «+3 дня», после перезапуска бэкенда откатился бы назад."""
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.shift(3 * 24 * 3600)
    assert clock.offset() == timedelta(days=3)

    runs.deactivate()
    runs.activate(run.run_id)
    assert clock.offset() == timedelta(days=3)
    assert runs.get(run.run_id).offset == timedelta(days=3)


def test_shift_accumulates():
    run = runs.create("c_romashka", warmed=True, moment=MOMENT)
    runs.activate(run.run_id)
    runs.shift(3600)
    runs.shift(3600)
    assert clock.offset() == timedelta(hours=2)


def test_shift_without_an_active_run_is_an_error():
    with pytest.raises(runs.UnknownRunError):
        runs.shift(3600)


def test_all_lists_newest_first():
    old = runs.create("c_one", warmed=True, moment=MOMENT)
    new = runs.create("c_two", warmed=True, moment=MOMENT + timedelta(minutes=1))
    assert [run.run_id for run in runs.all()] == [new.run_id, old.run_id]


def test_get_of_a_missing_run_is_an_error():
    with pytest.raises(runs.UnknownRunError):
        runs.get("20260903-0900-c_nobody")


def _number_status(run):
    with sqlite3.connect(run.path) as db:
        return db.execute("SELECT status FROM numbers").fetchone()[0]


def _open_thread(run, thread_id):
    with sqlite3.connect(run.path) as db:
        db.execute("INSERT INTO threads (thread_id, company_id, seed, created_at)"
                   " VALUES (?, 'c_romashka', '{}', '2026-09-03T09:00:00+00:00')",
                   (thread_id,))


def _threads(run):
    with sqlite3.connect(run.path) as db:
        return [row[0] for row in db.execute("SELECT thread_id FROM threads")]
