"""Что требуется от человека — по образцу test_analytics.py: свой каталог у
backend/*.py тестов нет, а базу целиком создаёт Alembic: переписку и очередь.
"""

from datetime import datetime, timezone

import pytest

import tasks
from conversations import public as conversation
from conversations.repository import ConversationRepository
from core import db as core_db

MOMENT = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
NOW = MOMENT.isoformat(timespec="seconds")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    core_db.migrate(path)                # threads, messages, outbox, numbers
    connection = core_db.connect(path)
    tasks.use(path)                      # без базы компаний: имя компании = company_id
    yield connection
    connection.close()
    tasks.use(None)


def _thread(db, channel, company_id):
    with db:
        ConversationRepository(db).open_thread(company_id, channel, {"name": company_id}, MOMENT)


def _draft(db, company_id, text, angle):
    with db:
        conversation.add_draft(db, company_id, text, angle, MOMENT)


def _stuck_row(db, thread_id, our_number="+77010000000"):
    db.execute(
        "INSERT INTO outbox (thread_id, our_number, send_after, status, kind, author,"
        "                    created_at, updated_at)"
        " VALUES (?, ?, ?, 'stuck', 'cold', 'operator', ?, ?)",
        (thread_id, our_number, NOW, NOW, NOW),
    )
    db.commit()


def test_an_escalated_thread_becomes_a_task_with_its_reason(db):
    """Счётчик `escalated: 3` не говорит, кто эти трое, — за этим план и писался."""
    _thread(db, "+77010000001", "c1")
    conversation.set_status(db, "c1", "escalated",
                            "заинтересован, назвал бюджет")
    db.commit()

    rows = tasks.tasks()

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "escalated"
    assert row["company_id"] == "c1"
    assert row["channel"] == "+77010000001"
    assert row["why"] == "заинтересован, назвал бюджет"


def test_a_thread_without_a_saved_reason_says_so_instead_of_guessing(db):
    """Треды старше плана 001 причины не имеют, и придуманная фраза врала бы."""
    _thread(db, "+77010000002", "c2")
    # Треды до плана 001 эскалировались без status_reason — как migrate.py
    # размечает старые треды с перепиской, не заполняя причину.
    db.execute("UPDATE threads SET status = 'escalated' WHERE company_id = ?", ("c2",))
    db.commit()

    rows = tasks.tasks()

    assert len(rows) == 1
    assert rows[0]["why"] == tasks.NO_REASON


def test_tasks_are_ordered_by_urgency(db):
    """Порядок — правило предметной области, и он принадлежит бэкенду."""
    _thread(db, "+77010000003", "c-draft")
    _draft(db, "c-draft", "черновик первого письма", "site_no_pricing")

    _thread(db, "+77010000004", "c-escalated")
    conversation.set_status(db, "c-escalated", "escalated", "интересно")
    db.commit()

    _stuck_row(db, "c-escalated")

    kinds = [row["kind"] for row in tasks.tasks()]

    assert kinds.index("escalated") < kinds.index("stuck") < kinds.index("draft")


def test_no_state_database_means_no_tasks_and_no_exception(tmp_path):
    """На чистой установке страница обязана показать «задач нет», а не 500."""
    tasks.use(tmp_path / "missing.db")

    assert tasks.tasks() == []

    tasks.use(None)


def test_a_database_without_queue_rows_still_lists_threads(tmp_path):
    """Пустая очередь — это нет застрявших отправок, а не страницы."""
    path = tmp_path / "state.db"
    core_db.migrate(path)
    with core_db.connect(path) as owner:
        _thread(owner, "+77010000006", "c-presender")
        _draft(owner, "c-presender", "черновик", "site_no_pricing")
    tasks.use(path)

    assert [row["kind"] for row in tasks.tasks()] == ["draft"]

    tasks.use(None)


def test_a_draft_that_was_regenerated_appears_once(db):
    """«Перегенерировать» оставляет предыдущий вариант в таблице, а задача на
    тред — одна: «черновик первого касания» берётся у conversations."""
    _thread(db, "+77010000005", "c-regen")
    _draft(db, "c-regen", "первый вариант", "site_no_pricing")
    _draft(db, "c-regen", "второй вариант", "site_hiring_sales")

    drafts = [row for row in tasks.tasks() if row["kind"] == "draft"]

    assert len(drafts) == 1
    assert drafts[0]["channel"] == "+77010000005"


def test_an_overdue_backup_is_a_task_after_threads_and_before_drafts(db):
    """Потеря базы — беда, но лид, который ждёт ответа, ждёт сейчас."""
    _thread(db, "+77010000003", "c-draft")
    _draft(db, "c-draft", "черновик первого письма", "site_no_pricing")
    _thread(db, "+77010000004", "c-escalated")
    conversation.set_status(db, "c-escalated", "escalated", "интересно")
    db.commit()

    rows = tasks.tasks(backup_why="бэкапа ещё не было")

    assert [row["kind"] for row in rows] == ["escalated", "backup", "draft"]
    assert rows[1]["why"] == "бэкапа ещё не было"
    assert (rows[1]["company_name"], rows[1]["channel"]) == ("Бэкап", "")
    assert rows[0]["company_name"] == "c-escalated"


def test_an_overdue_backup_shows_even_on_a_clean_install(tmp_path):
    tasks.use(tmp_path / "missing.db")

    assert [row["kind"] for row in tasks.tasks(backup_why="бэкап выключен")] == ["backup"]

    tasks.use(None)


def test_a_task_names_the_company_the_thread_was_opened_for(tmp_path):
    """#17: the thread points at a company of companies, and its name comes from there."""
    from contextlib import closing

    from conversations.tests.leads import add_lead

    path = tmp_path / "state.db"
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        add_lead(db, "2gis:1", [("whatsapp", "+77010000001")])
        _thread(db, "+77010000001", "2gis:1")
        _thread(db, "+77010000002", "2gis:gone")
        for company_id in ("2gis:1", "2gis:gone"):
            conversation.set_status(db, company_id, "escalated", "назвал бюджет")
        db.commit()
    tasks.use(path, path)

    names = {row["company_id"]: row["company_name"] for row in tasks.tasks()}

    assert names == {"2gis:1": "Компания 2gis:1", "2gis:gone": "2gis:gone"}
    tasks.use(None)


@pytest.mark.parametrize("companies_db", ["нет.db", "state.db"])
def test_without_a_companies_table_the_company_is_named_by_its_id(db, tmp_path, companies_db):
    """No file, or a file the companies migration has not reached yet."""
    _thread(db, "+77010000001", "2gis:1")
    conversation.set_status(db, "2gis:1", "escalated", "назвал бюджет")
    db.commit()
    tasks.use(tmp_path / "state.db", tmp_path / companies_db)

    assert tasks.tasks()[0]["company_name"] == "2gis:1"
