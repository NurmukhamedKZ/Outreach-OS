"""Воронка считается тредами и не растёт вверх; диагноз молчит на малой выборке.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import sqlite3
from pathlib import Path

import pytest

import analytics
from writer.db import thread_store


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state.db"
    db = thread_store.connect(path)
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
    thread_store.open_thread(db, thread_id, f"c{thread_id}", {"name": "Ромашка"})
    thread_store.set_stage(db, thread_id, stage)
    if outcome:
        thread_store.set_outcome(db, thread_id, outcome)
    if sent:
        message_id = thread_store.add_draft(db, thread_id, "текст", angle,
                                            offer_variant=variant)
        db.execute("UPDATE messages SET sent_text = 'текст', sent_at = ?"
                   " WHERE message_id = ?", (thread_store.now(), message_id))
        db.execute("INSERT INTO outbox (message_id, status, delivered_at)"
                   " VALUES (?, 'sent', ?)",
                   (message_id, thread_store.now() if delivered else None))
    if replied:
        thread_store.add_incoming(db, thread_id, "интересно")
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
