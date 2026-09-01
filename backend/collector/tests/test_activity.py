"""Журнал фоновой работы: схлопывание, ретенция, отказ работать без пути.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_observability.py и test_logctx.py: своего каталога у backend/*.py нет.
"""

from datetime import datetime, timedelta, timezone

import pytest

import activity


@pytest.fixture
def journal(tmp_path):
    activity.use(tmp_path / "state.db")
    yield activity
    activity.use(None)


def test_record_keeps_what_happened(journal):
    journal.record("sender.tick", "sent", subject="+77001112233", detail="тред +77009998877")
    events = journal.recent()
    assert len(events) == 1
    assert events[0]["actor"] == "sender.tick"
    assert events[0]["outcome"] == "sent"
    assert events[0]["subject"] == "+77001112233"
    assert events[0]["detail"] == "тред +77009998877"
    assert events[0]["repeats"] == 1


def test_identical_events_in_a_row_collapse(journal):
    """Тик раз в 20 секунд дал бы 4300 строк в сутки и утопил бы в них
    единственное важное событие."""
    for _ in range(3):
        journal.record("sender.tick", "idle")
    events = journal.recent()
    assert len(events) == 1
    assert events[0]["repeats"] == 3
    assert events[0]["last_at"] >= events[0]["at"]


def test_alternating_outcomes_do_not_collapse(journal):
    """Схлопывается только повтор последней строки: иначе чередование
    sent/idle слилось бы в две вечные строки и порядок событий пропал бы."""
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("sender.tick", "idle")
    assert [event["outcome"] for event in journal.recent()] == ["idle", "sent", "idle"]


def test_different_subjects_do_not_collapse(journal):
    journal.record("sender.monitor", "quarantined", subject="+77001112233")
    journal.record("sender.monitor", "quarantined", subject="+77007776655")
    assert len(journal.recent()) == 2


def test_recent_filters_by_actor_and_limits(journal):
    journal.record("jobs", "started", subject="17")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("jobs", "finished", subject="17")
    assert [event["outcome"] for event in journal.recent(actor="jobs")] == ["finished", "started"]
    assert len(journal.recent(limit=1)) == 1


def test_workers_answer_whether_a_daemon_is_alive(journal):
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "idle")
    journal.record("sender.warmup", "sent", subject="+77001112233")
    workers = {row["actor"]: row for row in journal.workers()}
    assert workers["sender.tick"]["events"] == 1
    assert workers["sender.warmup"]["last_at"] is not None


def test_prune_drops_only_the_old(journal, tmp_path):
    import sqlite3

    journal.record("sender.tick", "idle")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("UPDATE activity SET at = ?, last_at = ?", (stale, stale))
    journal.record("sender.tick", "sent", subject="+77001112233")

    assert journal.prune(days=14) == 1
    assert [event["outcome"] for event in journal.recent()] == ["sent"]


def test_record_returns_the_event_it_wrote(journal):
    """Возврат нужен ленте: публиковать событие в SSE перечитыванием базы
    значило бы лишний запрос на каждый тик и падение на пустом журнале."""
    journal.record("sender.tick", "idle")
    event = journal.record("sender.tick", "idle")
    assert event["outcome"] == "idle"
    assert event["repeats"] == 2


def test_journal_without_a_path_refuses_loudly(tmp_path):
    """Молча проглоченный журнал — ровно та теневая работа, которую он убирает."""
    activity.use(None)
    with pytest.raises(activity.NotConfiguredError):
        activity.record("sender.tick", "idle")
