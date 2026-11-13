"""Воронка считается тредами и не растёт вверх; диагноз молчит на малой выборке.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import sqlite3
from pathlib import Path

import pytest

import analytics
import clock
from conversations.repository import ConversationRepository
from core import db as core_db


def _now():
    return clock.now().isoformat(timespec="seconds")


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)
    db = core_db.connect(path)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS outbox ("
        "  id INTEGER PRIMARY KEY, message_id INTEGER, status TEXT NOT NULL,"
        "  delivered_at TEXT);"
    )
    yield db, path
    db.close()
    analytics.use(None)


def _thread(db, thread_id, *, stage="contact", outcome=None, sent=True, delivered=False,
            replied=False, angle="site_no_pricing", variant="pay_per_meeting"):
    """A thread of company c<thread_id> on the number thread_id."""
    repo, company_id = ConversationRepository(db), f"c{thread_id}"
    repo.open_thread(company_id, thread_id, {"name": "Ромашка"}, clock.now())
    repo.set_stage(company_id, stage)
    if outcome:
        repo.set_outcome(company_id, outcome, _now() if outcome.startswith("meeting_") else None)
    if sent:
        message_id = repo.add_draft(company_id, "текст", angle, clock.now(),
                                    offer_variant=variant)
        db.execute("UPDATE messages SET sent_text = 'текст', sent_at = ?"
                   " WHERE message_id = ?", (_now(), message_id))
        db.execute("INSERT INTO outbox (message_id, status, delivered_at)"
                   " VALUES (?, 'sent', ?)",
                   (message_id, _now() if delivered else None))
    if replied:
        repo.add_incoming(company_id, "интересно", None, clock.now())
    db.commit()


def test_funnel_counts_threads_and_never_grows(state):
    db, path = state
    _thread(db, "+77010000001", stage="closing", outcome="meeting_held",
            delivered=True, replied=True)
    _thread(db, "+77010000002", stage="probing", delivered=True, replied=True)
    _thread(db, "+77010000003", delivered=True)
    analytics.use(path)

    steps = {row["step"]: row["count"] for row in analytics.report()["funnel"]}

    assert steps["sent"] == 3
    assert steps["delivered"] == 3
    assert steps["replied"] == 2
    assert steps["dialog"] == 2
    assert steps["meeting_held"] == 1
    counts = [row["count"] for row in analytics.report()["funnel"]]
    assert counts == sorted(counts, reverse=True), f"воронка выросла вверх: {counts}"


def test_breakdowns_split_by_offer_and_angle(state):
    db, path = state
    _thread(db, "+77010000001", replied=True, angle="site_no_pricing",
            variant="pay_per_meeting")
    _thread(db, "+77010000002", angle="ig_dormant", variant="free_first_meetings")
    analytics.use(path)

    report = analytics.report()

    assert {row["key"] for row in report["by_offer"]} == {"pay_per_meeting", "free_first_meetings"}
    assert {row["key"] for row in report["by_angle"]} == {"site_no_pricing", "ig_dormant"}


def test_diagnosis_stays_silent_on_a_small_sample(state):
    db, path = state
    _thread(db, "+77010000001", replied=True)
    analytics.use(path)

    assert analytics.report()["diagnosis"] == "ok", \
        "диагноз по одному ответу — шум, из-за которого странице перестанут верить"


def test_missing_state_db_gives_an_empty_funnel(tmp_path):
    """Чистая установка: переписки ещё нет. Страница обязана показать нули, а
    не пятисотку от sqlite3, который в режиме ro не создаёт файл."""
    analytics.use(tmp_path / "нет-такой.db")

    report = analytics.report()

    assert [row["count"] for row in report["funnel"]] == [0] * len(analytics.STEPS)
    assert report["diagnosis"] == "ok"


def test_endpoint_answers_with_the_full_report(state):
    """Роутер поднимается отдельным приложением, а не collector.api: импорт
    боевого app зовёт analytics.use(store.STATE) на своих путях и тянет за
    собой lifespan трёх систем — тест роутера не должен от этого зависеть."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from collector.routes import analytics as analytics_routes

    db, path = state
    _thread(db, "+77010000001", replied=True)
    analytics.use(path)

    app = FastAPI()
    app.include_router(analytics_routes.router)
    response = TestClient(app).get("/api/analytics?days=7")

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    assert [row["step"] for row in body["funnel"]] == list(analytics.STEPS)
    assert "diagnosis" in body


def test_report_survives_a_database_without_foreign_layers(tmp_path):
    """state.db, где система 3 ещё не стартовала: outbox нет. Страница обязана
    показать нули — аналитика читает чужие слои и не создаёт ни одного."""
    path = tmp_path / "state.db"
    core_db.migrate(path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO threads (company_id, channel, seed, created_at)"
               " VALUES ('c_ok', '+77010000001', '{}', '2026-01-01T00:00:00+00:00')")
    db.commit()
    db.close()
    analytics.use(path)

    report = analytics.report()

    assert [row["count"] for row in report["funnel"]] == [0] * len(analytics.STEPS)
    assert report["by_offer"] == [] and report["diagnosis"] == "ok"
    analytics.use(None)


def test_a_thread_lands_in_one_row_per_breakdown(state):
    """Follow-up системы 3 пишет свой угол и не пишет offer_variant вовсе.
    Считая все письма, тред попадал бы и под свой вариант, и под «до A/B», а
    сумма по разрезу превышала бы sent из воронки."""
    db, path = state
    _thread(db, "+77010000001", angle="site_no_pricing", variant="pay_per_meeting")
    followup = ConversationRepository(db).add_draft("c+77010000001", "касание", "ig_dormant",
                                                    clock.now())
    db.execute("UPDATE messages SET sent_text = 'касание', sent_at = ?"
               " WHERE message_id = ?", (_now(), followup))
    db.commit()
    analytics.use(path)

    report = analytics.report()
    sent = next(row["count"] for row in report["funnel"] if row["step"] == "sent")

    assert sum(row["sent"] for row in report["by_offer"]) == sent
    assert sum(row["sent"] for row in report["by_angle"]) == sent
    assert [row["key"] for row in report["by_angle"]] == ["site_no_pricing"], \
        "разрез считает не первое письмо треда"


def _sent_thread(db, thread_id, company_id, *, replied=False, held=False):
    repo = ConversationRepository(db)
    repo.open_thread(company_id, thread_id, {"name": "Компания"}, clock.now())
    message_id = repo.add_draft(company_id, "текст", "reviews_missed_lead", clock.now())
    db.execute("UPDATE messages SET sent_text = 'текст', sent_at = ? WHERE message_id = ?",
               (_now(), message_id))
    if replied:
        repo.add_incoming(company_id, "интересно", None, clock.now())
    if held:
        repo.set_outcome(company_id, "meeting_held", _now())
    db.commit()


def test_the_segment_breakdown_names_the_rubric_and_city_of_the_company(tmp_path):
    """#17: threads point at companies (2gis:…), and the segment comes from them."""
    from contextlib import closing

    from companies.repository import CompanyRepository
    from companies.schemas import Segment
    from core import db as core_db
    from conversations.tests.leads import NOW, add_lead

    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        add_lead(db, "2gis:1", [("whatsapp", "+77010000001")])
        add_lead(db, "2gis:2", [("whatsapp", "+77010000002")])
        add_lead(db, "2gis:3", [("whatsapp", "+77010000003")])
        repo = CompanyRepository(db)
        repo.claim(Segment("astana", "339"), NOW)
        # 2gis:3 moves to a later segment only: drop its default one.
        db.execute("DELETE FROM branch_segments WHERE branch_id = '2gis:3-0'")
        repo.link_segment("2gis:3-0", Segment("astana", "339"))
        _sent_thread(db, "+77010000001", "2gis:1", replied=True, held=True)
        _sent_thread(db, "+77010000002", "2gis:2")
        _sent_thread(db, "+77010000003", "2gis:3", replied=True)
        _sent_thread(db, "+77010000009", "2gis:gone")
    analytics.use(path, path)

    assert analytics.report()["by_segment"] == [
        {"key": "Бухгалтерские услуги · Алматы", "sent": 2, "replied": 1, "meetings": 1},
        {"key": "Аудиторские услуги · Астана", "sent": 1, "replied": 1, "meetings": 0}]


@pytest.mark.parametrize("companies_db", ["нет.db", "state.db"])
def test_without_a_companies_table_there_is_no_segment_breakdown(state, tmp_path, companies_db):
    """No file, or a file the companies migration has not reached yet."""
    db, path = state
    _thread(db, "+77010000001")
    analytics.use(path, tmp_path / companies_db)

    assert analytics.report()["by_segment"] == []
