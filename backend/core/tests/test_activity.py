import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from core import db as core_db
from core.activity import Journal


@pytest.fixture
def path(tmp_path):
    found = tmp_path / "state.db"
    core_db.migrate(found)
    return found


@pytest.fixture
def journal(path):
    return Journal(lambda: path)


def test_record_keeps_what_happened(journal):
    journal.record("sender.tick", "sent", subject="+77001112233", detail="тред c1")

    assert journal.recent() == [
        {
            "at": journal.recent()[0]["at"],
            "last_at": journal.recent()[0]["at"],
            "repeats": 1,
            "actor": "sender.tick",
            "outcome": "sent",
            "subject": "+77001112233",
            "detail": "тред c1",
        }
    ]


def test_identical_events_in_a_row_collapse_and_move_the_pulse(journal, monkeypatch):
    """A tick every 20 seconds would drown the one event that matters in 4300 rows a day."""
    from core import activity

    for stamp in ("2026-11-14T10:00:00+00:00", "2026-11-14T10:00:20+00:00"):
        monkeypatch.setattr(activity, "_now", lambda stamp=stamp: stamp)
        journal.record("sender.tick", "idle")
    journal.record("sender.warmup", "idle")  # another actor's same outcome is a row of its own

    assert [(e["actor"], e["repeats"], e["at"], e["last_at"]) for e in journal.recent()] == [
        ("sender.warmup", 1, "2026-11-14T10:00:20+00:00", "2026-11-14T10:00:20+00:00"),
        ("sender.tick", 2, "2026-11-14T10:00:00+00:00", "2026-11-14T10:00:20+00:00"),
    ]
    assert journal.last_at("sender.tick") == "2026-11-14T10:00:20+00:00"


def test_alternating_outcomes_and_subjects_do_not_collapse(journal):
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("sender.tick", "idle")
    journal.record("sender.monitor", "quarantined", subject="+77001112233")
    journal.record("sender.monitor", "quarantined", subject="+77007776655")

    assert [event["outcome"] for event in journal.recent()] == [
        "quarantined",
        "quarantined",
        "idle",
        "sent",
        "idle",
    ]


def test_recent_filters_by_actor_and_limits(journal):
    journal.record("backup", "done")
    journal.record("sender.tick", "sent", subject="+77001112233")
    journal.record("backup", "failed")

    assert [event["outcome"] for event in journal.recent(actor="backup")] == ["failed", "done"]
    assert len(journal.recent(limit=1)) == 1


def test_workers_and_last_at_answer_whether_a_daemon_is_alive(journal):
    journal.record("sender.tick", "idle")
    journal.record("sender.tick", "idle")
    journal.record("sender.warmup", "sent", subject="+77001112233")

    workers = {row["actor"]: row for row in journal.workers()}
    assert workers["sender.tick"]["events"] == 1
    assert journal.last_at("sender.warmup") == workers["sender.warmup"]["last_at"]
    assert journal.last_at("backup") is None


def test_prune_drops_what_was_last_seen_long_ago(journal, path):
    """A row that began a month ago and still repeats is alive: the pulse reads it."""
    journal.record("sender.tick", "idle")
    journal.record("sender.monitor", "healthy")
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE activity SET at = ?, last_at = ? WHERE actor = 'sender.tick'", (stale, stale)
        )
        db.execute("UPDATE activity SET at = ? WHERE actor = 'sender.monitor'", (stale,))

    assert journal.prune() == 1
    assert [event["outcome"] for event in journal.recent()] == ["healthy"]


def test_a_record_survives_the_rollback_of_the_callers_transaction(journal, path):
    """The trace "we tried" must outlive a tick that failed and rolled back."""
    with closing(core_db.connect(path)) as db:
        try:
            with db:
                db.execute("INSERT INTO operator_settings (module, data) VALUES ('x', '{}')")
                raise RuntimeError("tick failed")
        except RuntimeError:
            journal.record_crash("sender.tick")
        assert db.execute("SELECT count(*) FROM operator_settings").fetchone()[0] == 0

    assert [event["outcome"] for event in journal.recent()] == ["crashed"]


def test_the_journal_follows_the_database_of_the_moment(tmp_path):
    """A sandbox run switches the database of a live process."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    core_db.migrate(first)
    core_db.migrate(second)
    current = [first]
    journal = Journal(lambda: current[0])

    journal.record("sender.tick", "idle")
    current[0] = second
    journal.record("sender.tick", "sent")

    assert [event["outcome"] for event in journal.recent()] == ["sent"]
    assert [event["outcome"] for event in Journal(lambda: first).recent()] == ["idle"]


def test_record_crash_never_throws(tmp_path):
    """From the `except` of a daemon's loop an exception would kill the daemon."""
    broken = Journal(lambda: tmp_path / "no-such-dir" / "state.db")

    broken.record_crash("sender.tick")


def test_record_crash_writes_when_it_can(journal):
    journal.record_crash("sender.warmup", detail="конфиг не читается")

    [event] = journal.recent(actor="sender.warmup")
    assert (event["outcome"], event["detail"]) == ("crashed", "конфиг не читается")


def test_revision_0007_keeps_the_journal_and_drops_the_old_queue(tmp_path):
    """Up from 0006 on a working database, down and up again: the journal keeps its rows."""
    path = tmp_path / "state.db"
    core_db.migrate(path)
    core_db.downgrade(path, "0006")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO activity (at, last_at, repeats, actor, outcome, subject)"
            " VALUES ('2026-11-01T10:00:00+00:00', '2026-11-01T10:05:00+00:00', 4,"
            " 'sender.tick', 'idle', NULL)"
        )
        db.execute(
            "INSERT INTO jobs (kind, title, status, steps, created_at)"
            " VALUES ('write', 'Черновики топ-N', 'done', '[]', '2026-10-01')"
        )
        db.execute(
            "INSERT INTO llm_answers (kind, subject, model, prompt, answer)"
            " VALUES ('profile', 'x', 'm', 'p', '{}')"
        )
        db.execute("UPDATE activity SET id = 41")
        db.execute(
            "INSERT INTO activity (at, last_at, actor, outcome)"
            " VALUES ('2026-10-01', '2026-10-01', 'jobs', 'finished')"
        )
    journal = Journal(lambda: path)

    for _ in range(2):
        core_db.migrate(path)
        [event] = journal.recent()
        assert (event["actor"], event["repeats"]) == ("sender.tick", 4)
        with closing(sqlite3.connect(path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            assert db.execute("SELECT id FROM activity").fetchall() == [(41,)]
        assert {"jobs", "llm_answers"}.isdisjoint(tables)
        assert "activity_recent" in tables
        core_db.downgrade(path, "0006")
        assert [event["repeats"] for event in journal.recent()] == [4]
