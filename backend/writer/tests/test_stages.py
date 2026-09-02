"""Этап переписки двигается на один шаг и только вперёд.

Правило «нельзя перескакивать этапы» живёт в коде, а не в промпте, по той же
причине, что запрет пустых follow-up живёт в схеме: инструкцию модель нарушает
ровно там, где это дороже всего — на вопросе «сколько стоит», где она прыгает
в closing и называет цену.
"""

import pytest

from writer.db import thread_store
from writer.services import stages


def test_stage_moves_one_step_forward():
    assert stages.stage_after("contact", "probing") == "probing"
    assert stages.stage_after("contact", "contact") == "contact"


def test_stage_never_skips_and_never_goes_back():
    assert stages.stage_after("contact", "closing") == "contact", "перескок через два этапа"
    assert stages.stage_after("offer", "contact") == "offer", "откат назад"


def test_unknown_stage_leaves_thread_where_it_was():
    assert stages.stage_after("probing", "переговоры") == "probing"
    assert stages.stage_after("probing", "") == "probing"


def test_each_reply_moves_the_thread_one_step():
    assert stages.advance("contact") == "probing"
    assert stages.advance("probing") == "offer"
    assert stages.advance("offer") == "closing"


def test_last_stage_is_a_dead_end_not_an_error():
    assert stages.advance("closing") == "closing"
    assert stages.advance("что-то своё") == stages.FIRST


def test_rules_differ_by_stage():
    assert stages.rules_for("contact") != stages.rules_for("closing")
    assert "созвон" not in stages.rules_for("contact").lower(), \
        "в первом касании созвон предлагать нельзя"


def test_new_thread_starts_in_contact():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    assert thread_store.thread(db, "+77010000001")["stage"] == stages.FIRST


def test_stage_survives_write():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    thread_store.set_stage(db, "+77010000001", "probing")

    assert thread_store.thread(db, "+77010000001")["stage"] == "probing"
