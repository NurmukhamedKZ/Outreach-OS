"""Что требуется от человека — по образцу test_analytics.py: свой каталог у
backend/*.py тестов нет, а фикстура базы собрана так же, как в
sender/tests/conftest.py — таблицы переписки создаёт их владелец
(thread_store), sender только доливает свои колонки (migrate).
"""

from datetime import datetime, timezone

import pytest

import tasks
from sender.db import conversation, migrate
from writer.db import thread_store

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)   # threads и messages
    owner.close()
    connection = migrate.connect(path)   # numbers, outbox, колонки status/status_reason
    tasks.use(path)                      # без derived.db: имя компании = company_id
    yield connection
    connection.close()
    tasks.use(None)


def _thread(db, thread_id, company_id):
    thread_store.open_thread(db, thread_id, company_id, {"name": company_id})


def _stuck_row(db, thread_id, our_number="+77010000000"):
    db.execute(
        "INSERT INTO outbox (thread_id, our_number, send_after, status,"
        "                    created_at, updated_at)"
        " VALUES (?, ?, ?, 'stuck', ?, ?)",
        (thread_id, our_number, NOW, NOW, NOW),
    )
    db.commit()


def test_an_escalated_thread_becomes_a_task_with_its_reason(db):
    """Счётчик `escalated: 3` не говорит, кто эти трое, — за этим план и писался."""
    _thread(db, "+77010000001", "c1")
    conversation.set_status(db, "+77010000001", "escalated",
                            "заинтересован, назвал бюджет")
    db.commit()

    rows = tasks.tasks()

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "escalated"
    assert row["company_id"] == "c1"
    assert row["thread_id"] == "+77010000001"
    assert row["why"] == "заинтересован, назвал бюджет"


def test_a_thread_without_a_saved_reason_says_so_instead_of_guessing(db):
    """Треды старше плана 001 причины не имеют, и придуманная фраза врала бы."""
    _thread(db, "+77010000002", "c2")
    # Треды до плана 001 эскалировались без status_reason — как migrate.py
    # размечает старые треды с перепиской, не заполняя причину.
    db.execute("UPDATE threads SET status = 'escalated' WHERE thread_id = ?",
               ("+77010000002",))
    db.commit()

    rows = tasks.tasks()

    assert len(rows) == 1
    assert rows[0]["why"] == tasks.NO_REASON


def test_tasks_are_ordered_by_urgency(db):
    """Порядок — правило предметной области, и он принадлежит бэкенду."""
    _thread(db, "+77010000003", "c-draft")
    thread_store.add_draft(db, "+77010000003", "черновик первого письма", "site_no_pricing")

    _thread(db, "+77010000004", "c-escalated")
    conversation.set_status(db, "+77010000004", "escalated", "интересно")
    db.commit()

    _stuck_row(db, "+77010000004")

    kinds = [row["kind"] for row in tasks.tasks()]

    assert kinds.index("escalated") < kinds.index("stuck") < kinds.index("draft")


def test_no_state_database_means_no_tasks_and_no_exception(tmp_path):
    """На чистой установке страница обязана показать «задач нет», а не 500."""
    tasks.use(tmp_path / "missing.db")

    assert tasks.tasks() == []

    tasks.use(None)


def test_a_database_without_the_sender_columns_yields_no_tasks_and_no_exception(tmp_path):
    """Таблицу threads создаёт writer, колонки состояния — sender, и порядок
    старта не гарантирован. Жёсткий SELECT отдал бы 500 на главной."""
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)   # threads и messages, БЕЗ migrate.connect
    thread_store.open_thread(owner, "+77010000006", "c-presender", {"name": "c-presender"})
    owner.close()
    tasks.use(path)

    assert tasks.tasks() == []

    tasks.use(None)


def test_a_draft_that_was_regenerated_appears_once(db):
    """Копия SQL из cold_drafts обязана сохранить оговорку про последний
    черновик треда: «Перегенерировать» оставляет предыдущий вариант в таблице."""
    _thread(db, "+77010000005", "c-regen")
    thread_store.add_draft(db, "+77010000005", "первый вариант", "site_no_pricing")
    thread_store.add_draft(db, "+77010000005", "второй вариант", "ig_dormant")

    drafts = [row for row in tasks.tasks() if row["kind"] == "draft"]

    assert len(drafts) == 1
    # Без дедупа по max(message_id) строк было бы две — прямое доказательство,
    # что копия запроса всё ещё воспроизводит именно это условие cold_drafts.
    assert drafts[0]["thread_id"] == "+77010000005"
