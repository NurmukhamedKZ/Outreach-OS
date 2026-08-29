"""Гейты перед отправкой: от юридического к техническому.

Отмена и перенос проверяются раздельно. Их подмена друг другом и есть тот баг,
который либо спамит, либо тихо теряет follow-up.
"""

from datetime import datetime, timezone

import pytest

from sender.services import config, gates

CONFIG = config.load()
# Среда, 07:00 UTC = 12:00 в Алматы: середина рабочего окна.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def attempt(**overrides):
    base = {"thread_status": "queued", "suppressed": False, "number_status": "active",
            "capacity": 5, "last_sent_at": None, "jitter_minutes": 2.0}
    return gates.Attempt(**{**base, **overrides})


def test_everything_open_lets_the_message_through():
    assert gates.check(attempt(), INSIDE, CONFIG).action == "send"


def test_suppression_cancels_even_after_the_row_was_queued():
    """Лид мог отказаться за те три дня, что follow-up лежал в очереди (F21)."""
    decision = gates.check(attempt(suppressed=True), INSIDE, CONFIG)
    assert decision.action == "cancel"


@pytest.mark.parametrize("status", ["escalated", "closed_refused", "closed_junk",
                                    "unreachable", "exhausted"])
def test_a_thread_the_automaton_must_not_touch_cancels(status):
    """Человек уже взял тред — автомат в него не пишет."""
    assert gates.check(attempt(thread_status=status), INSIDE, CONFIG).action == "cancel"


def test_outside_the_window_postpones_and_never_cancels():
    """«Нужно, но не сейчас». Отмена здесь потеряла бы касание навсегда."""
    saturday = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    decision = gates.check(attempt(), saturday, CONFIG)
    assert decision.action == "reschedule"
    # Понедельник, 10:00 Алматы = 05:00 UTC.
    assert decision.send_after == datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)


def test_before_the_window_opens_waits_for_the_same_day():
    early = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)     # 08:00 в Алматы
    decision = gates.check(attempt(), early, CONFIG)
    assert decision.send_after == datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)


def test_after_the_window_closes_waits_for_the_next_day():
    late = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)     # 19:00 в Алматы
    decision = gates.check(attempt(), late, CONFIG)
    assert decision.send_after == datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)


def test_exhausted_daily_limit_postpones():
    decision = gates.check(attempt(capacity=0), INSIDE, CONFIG)
    assert decision.action == "reschedule" and decision.blame == gates.NUMBER


def test_a_number_that_is_not_active_postpones():
    """Карантин и прогрев — не повод отменять сообщение."""
    decision = gates.check(attempt(number_status="quarantined"), INSIDE, CONFIG)
    assert decision.action == "reschedule" and decision.blame == gates.NUMBER


def test_blame_separates_the_number_from_the_clock():
    """Воркер по этому полю решает, предлагать ли треду другой номер: ночью
    другой номер не поможет, а выбранный лимит — поможет."""
    saturday = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    assert gates.check(attempt(), saturday, CONFIG).blame == gates.WINDOW
    assert gates.check(attempt(last_sent_at="2026-09-02T06:59:00+00:00",
                               jitter_minutes=15.0), INSIDE, CONFIG).blame == gates.JITTER


def test_jitter_holds_the_number_after_the_previous_send():
    """Отправки подряд — машинный почерк, а почерк здесь и оценивают."""
    decision = gates.check(
        attempt(last_sent_at="2026-09-02T06:59:00+00:00", jitter_minutes=15.0),
        INSIDE, CONFIG)
    assert decision.action == "reschedule"
    assert decision.send_after == datetime(2026, 9, 2, 7, 14, tzinfo=timezone.utc)


def test_jitter_is_over_and_the_message_goes():
    assert gates.check(
        attempt(last_sent_at="2026-09-02T06:40:00+00:00", jitter_minutes=15.0),
        INSIDE, CONFIG).action == "send"


def test_the_legal_gate_wins_over_the_technical_one():
    """Порядок гейтов не косметика: отказ отменяет, даже если сейчас ночь и
    номер в карантине — иначе строка ушла бы в перенос и вернулась завтра."""
    decision = gates.check(
        attempt(suppressed=True, capacity=0, number_status="banned"),
        datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc), CONFIG)
    assert decision.action == "cancel"
