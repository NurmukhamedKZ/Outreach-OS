"""Pure rules of delivery: gates, the window, the autopilot, warmup, health, stopwords."""

from datetime import UTC, datetime, timedelta

import pytest

from delivery import domain
from delivery.domain import Action, Attempt, Blame, Rates
from delivery.exceptions import NoNumberAvailableError
from delivery.schemas import Author, DeliverySettings, Kind, Mode, NumberStatus, Phase, Window

SETTINGS = DeliverySettings()
# Wednesday, 07:00 UTC = 12:00 in Almaty: the middle of the window.
INSIDE = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
NIGHT = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)  # 02:00 in Almaty
SATURDAY = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


def attempt(
    *,
    thread_status="active",
    refused=False,
    number_status="active",
    capacity=5,
    last_sent_at=None,
    jitter_minutes=0.0,
    kind=Kind.COLD,
    lead_spoke_last=False,
):
    return Attempt(
        thread_status=thread_status,
        refused=refused,
        number_status=number_status,
        capacity=capacity,
        last_sent_at=last_sent_at,
        jitter_minutes=jitter_minutes,
        kind=kind,
        lead_spoke_last=lead_spoke_last,
    )


# --- gates: from the legal to the technical ------------------------------------------


def test_everything_open_lets_the_message_through():
    assert domain.check(attempt(), INSIDE, SETTINGS).action is Action.SEND


def test_a_refusal_cancels_even_after_the_row_was_queued():
    """The lead may have refused in the three days a follow-up waited (F21)."""
    decision = domain.check(attempt(refused=True), INSIDE, SETTINGS)
    assert (decision.action, decision.blame) == (Action.CANCEL, Blame.REFUSAL)


@pytest.mark.parametrize(
    "status", ["escalated", "closed_refused", "closed_junk", "unreachable", "exhausted"]
)
def test_a_thread_the_automaton_must_not_touch_cancels(status):
    assert domain.check(attempt(thread_status=status), INSIDE, SETTINGS).action is Action.CANCEL


def test_outside_the_window_postpones_and_never_cancels():
    decision = domain.check(attempt(), SATURDAY, SETTINGS)
    assert (decision.action, decision.blame) == (Action.RESCHEDULE, Blame.WINDOW)
    # Monday, 10:00 in Almaty = 05:00 UTC.
    assert decision.send_after == datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


def test_before_the_window_opens_waits_for_the_same_day():
    early = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)  # 08:00 in Almaty
    assert domain.check(attempt(), early, SETTINGS).send_after == datetime(
        2026, 9, 2, 5, 0, tzinfo=UTC
    )


def test_after_the_window_closes_waits_for_the_next_day():
    late = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)  # 19:00 in Almaty
    assert domain.check(attempt(), late, SETTINGS).send_after == datetime(
        2026, 9, 3, 5, 0, tzinfo=UTC
    )


def test_the_closing_hour_belongs_to_the_next_window():
    closing = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)  # 18:00 in Almaty
    just_before = closing - timedelta(seconds=1)
    assert domain.check(attempt(), just_before, SETTINGS).action is Action.SEND
    assert domain.check(attempt(), closing, SETTINGS).action is Action.RESCHEDULE


def test_the_window_follows_the_operator_settings():
    weekend = DeliverySettings(window=Window(hours=(9, 13), weekdays=[6, 7]))
    assert domain.check(attempt(), SATURDAY, weekend).action is Action.SEND
    decision = domain.check(attempt(), INSIDE, weekend)
    # Saturday, 09:00 in Almaty = 04:00 UTC.
    assert decision.send_after == datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


def test_an_exhausted_daily_limit_postpones_and_blames_the_number():
    decision = domain.check(attempt(capacity=0), INSIDE, SETTINGS)
    assert (decision.action, decision.blame) == (Action.RESCHEDULE, Blame.NUMBER)
    # The same hour tomorrow, in UTC: stamps are compared as text.
    assert decision.send_after == INSIDE + timedelta(days=1)
    assert decision.send_after.utcoffset() == timedelta(0)


def test_a_number_that_is_not_active_postpones():
    decision = domain.check(attempt(number_status="quarantined"), INSIDE, SETTINGS)
    assert (decision.action, decision.blame) == (Action.RESCHEDULE, Blame.NUMBER)


def test_jitter_holds_the_number_after_the_previous_send():
    decision = domain.check(
        attempt(last_sent_at=INSIDE - timedelta(minutes=1), jitter_minutes=15.0),
        INSIDE,
        SETTINGS,
    )
    assert (decision.action, decision.blame) == (Action.RESCHEDULE, Blame.JITTER)
    assert decision.send_after == INSIDE + timedelta(minutes=14)


def test_the_jitter_is_over_and_the_message_goes():
    late_enough = attempt(last_sent_at=INSIDE - timedelta(minutes=20), jitter_minutes=15.0)
    assert domain.check(late_enough, INSIDE, SETTINGS).action is Action.SEND


def test_the_legal_gate_wins_over_the_technical_one():
    decision = domain.check(
        attempt(refused=True, capacity=0, number_status="banned"), NIGHT, SETTINGS
    )
    assert decision.action is Action.CANCEL


def test_a_reply_goes_out_at_night_and_a_cold_touch_waits():
    assert domain.check(attempt(kind=Kind.REPLY), NIGHT, SETTINGS).action is Action.SEND
    assert domain.check(attempt(kind=Kind.COLD), NIGHT, SETTINGS).blame is Blame.WINDOW
    assert domain.check(attempt(kind=Kind.FOLLOWUP), NIGHT, SETTINGS).blame is Blame.WINDOW


def test_a_reply_still_obeys_the_daily_limit_of_the_number():
    decision = domain.check(attempt(kind=Kind.REPLY, capacity=0), NIGHT, SETTINGS)
    assert (decision.action, decision.blame) == (Action.RESCHEDULE, Blame.NUMBER)


def test_a_reply_from_the_lead_cancels_a_waiting_touch_but_not_an_answer():
    for kind in (Kind.COLD, Kind.FOLLOWUP):
        decision = domain.check(attempt(kind=kind, lead_spoke_last=True), INSIDE, SETTINGS)
        assert (decision.action, decision.blame) == (Action.CANCEL, Blame.THREAD)
    answer = attempt(kind=Kind.REPLY, lead_spoke_last=True)
    assert domain.check(answer, INSIDE, SETTINGS).action is Action.SEND


# --- the autopilot ----------------------------------------------------------------


@pytest.mark.parametrize("saved", ["full", "manual", "replies", "off"])
def test_a_known_mode_is_read_as_saved(saved):
    assert domain.mode(saved) is Mode(saved)


@pytest.mark.parametrize("saved", ["fulll", "", None, 3, "FULL"])
def test_an_unknown_mode_reads_as_off_not_as_the_default(saved):
    """Fail closed: the default is manual, and a broken value over a chosen stop sends the queue."""
    assert domain.mode(saved) is Mode.OFF


def test_off_releases_nothing_but_warmup():
    for author in Author:
        for kind in (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY):
            assert not domain.releases(Mode.OFF, author, kind)
    assert domain.releases(Mode.OFF, Author.AUTOMAT, Kind.WARMUP)


@pytest.mark.parametrize("mode", [Mode.MANUAL, Mode.REPLIES, Mode.FULL])
def test_a_row_of_the_operator_goes_in_every_mode_but_off(mode):
    for kind in (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY):
        assert domain.releases(mode, Author.OPERATOR, kind)


def test_manual_holds_every_row_of_the_automaton():
    for kind in (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY):
        assert not domain.releases(Mode.MANUAL, Author.AUTOMAT, kind)


def test_a_row_of_the_automaton_goes_in_a_mode_that_lets_it_queue_that_kind():
    assert domain.releases(Mode.REPLIES, Author.AUTOMAT, Kind.REPLY)
    assert not domain.releases(Mode.REPLIES, Author.AUTOMAT, Kind.COLD)
    assert not domain.releases(Mode.REPLIES, Author.AUTOMAT, Kind.FOLLOWUP)
    for kind in (Kind.COLD, Kind.FOLLOWUP, Kind.REPLY):
        assert domain.releases(Mode.FULL, Author.AUTOMAT, kind)


# --- warmup calendar ------------------------------------------------------------------

STARTED = "2026-08-01T09:00:00+00:00"


def day(n: int) -> datetime:
    """Noon of the n-th warmup day: day 1 is the day of registration."""
    return datetime(2026, 8, 1, 12, 0, tzinfo=UTC) + timedelta(days=n - 1)


@pytest.mark.parametrize(
    "n, phase",
    [
        (1, Phase.SOCKET_DELAY),
        (2, Phase.PASSIVE),
        (4, Phase.PASSIVE),
        (5, Phase.INTERNAL),
        (10, Phase.INTERNAL),
        (11, Phase.COLD),
        (40, Phase.COLD),
    ],
)
def test_phase_boundaries(n, phase):
    assert domain.plan(STARTED, day(n), 30).phase is phase


def test_the_first_days_send_nothing():
    assert domain.plan(STARTED, day(1), 30).daily_limit == 0
    assert domain.plan(STARTED, day(3), 30).daily_limit == 0


def test_the_internal_ramp_is_not_clipped_by_the_ceiling():
    assert [domain.plan(STARTED, day(n), 30).daily_limit for n in (5, 6, 9, 10)] == [6, 12, 45, 60]


def test_the_socket_delay_counts_hours_not_calendar_days():
    late = "2026-08-01T23:50:00+00:00"
    assert domain.plan(late, datetime(2026, 8, 2, 0, 0, tzinfo=UTC), 30).phase is Phase.SOCKET_DELAY
    assert domain.plan(late, datetime(2026, 8, 2, 23, 50, tzinfo=UTC), 30).phase is Phase.PASSIVE


def test_the_cold_ramp_starts_on_day_eleven_and_stops_at_the_ceiling():
    assert domain.plan(STARTED, day(11), 30).daily_limit == 5
    assert domain.plan(STARTED, day(40), 30).daily_limit == 30
    assert domain.plan(STARTED, day(40), 12).daily_limit == 12
    assert domain.plan(STARTED, day(10), 30).cold_allowed is False
    assert domain.plan(STARTED, day(11), 30).cold_allowed is True


def test_a_number_warmed_elsewhere_skips_the_calendar():
    plan = domain.plan_for(STARTED, True, day(1), 20)
    assert (plan.phase, plan.daily_limit, plan.cold_allowed) == (Phase.COLD, 20, True)
    assert domain.plan_for(STARTED, False, day(5), 30) == domain.plan(STARTED, day(5), 30)


def test_the_calendar_shows_the_phases_in_order_with_their_days_and_limits():
    rows = domain.calendar(30)
    assert [(row.phase, row.days, row.daily_limit) for row in rows] == [
        (Phase.SOCKET_DELAY, "1", "0"),
        (Phase.PASSIVE, "2–4", "0 (входящие раз в 2 ч)"),
        (Phase.INTERNAL, "5–10", "6 → 60 (рампа)"),
        (Phase.COLD, "11+", "5 → 30, потолок 30"),
    ]
    assert domain.calendar(10)[-1].daily_limit == "5 → 10, потолок 10"


# --- the pool -------------------------------------------------------------------------


def test_take_picks_the_most_room_and_spends_it():
    budget = {"+77001": 2, "+77002": 3}
    assert [domain.take(budget) for _ in range(4)] == ["+77002", "+77002", "+77001", "+77002"]
    assert budget == {"+77001": 1, "+77002": 0}


def test_take_with_no_room_raises():
    with pytest.raises(NoNumberAvailableError):
        domain.take({"+77001": 0})


# --- health -----------------------------------------------------------------------------

HEALTHY = Rates(sent=0, delivered=0, threads=0, replies=0)


def verdict(status, state, rates=HEALTHY, feed_alive=True):
    return domain.verdict(NumberStatus(status), state, rates, feed_alive)


def test_a_logged_out_session_quarantines_and_asks_for_a_new_link():
    found = verdict("active", {"state": "loggedOut", "reconnects": 0})
    assert found is not None
    assert found.status is NumberStatus.QUARANTINED
    assert "привяжите заново" in found.reason


def test_a_reconnect_spike_quarantines():
    assert (
        verdict("active", {"state": "connected", "reconnects": 6}).status
        is NumberStatus.QUARANTINED
    )


def test_a_healthy_or_unknown_number_is_left_alone():
    assert verdict("active", {"state": "connected", "reconnects": 5}) is None
    assert verdict("active", None) is None


def test_a_verdict_the_number_already_has_is_not_repeated():
    assert verdict("quarantined", {"state": "connected", "reconnects": 9}) is None


def test_banned_is_terminal():
    assert verdict("banned", {"state": "loggedOut", "reconnects": 99}) is None


def test_poor_delivery_quarantines_only_with_a_sample_and_a_live_feed():
    poor = Rates(sent=20, delivered=10, threads=20, replies=5)
    assert verdict("active", {"state": "connected", "reconnects": 0}, poor).reason == (
        "доставлено 10 из 20"
    )
    assert verdict("active", {"state": "connected", "reconnects": 0}, poor, False) is None
    small = Rates(sent=19, delivered=0, threads=19, replies=5)
    assert verdict("active", {"state": "connected", "reconnects": 0}, small) is None


def test_silence_of_the_leads_counts_threads_not_touches():
    silent = Rates(sent=60, delivered=60, threads=20, replies=0)
    found = verdict("active", {"state": "connected", "reconnects": 0}, silent)
    assert found is not None and found.reason == "ответили 0 лидов из 20"


# --- stopwords --------------------------------------------------------------------------


def test_a_stopword_is_found_as_a_fragment_in_any_case():
    assert domain.stopword("Пожалуйста, ОТПИШИТЕ меня", ["отпиш", "спам"]) == "отпиш"
    assert domain.stopword("добрый день, интересно", ["отпиш", "спам"]) is None


def test_a_stopword_is_text_not_a_pattern():
    assert domain.stopword("ну и цены", [".*"]) is None


@pytest.mark.parametrize(
    "fragment, message",
    [
        ("не пиши", "Не пишите мне больше"),
        ("не писать", "просил же не писать сюда"),
        ("отпиш", "Отпишитесь от меня"),
        ("удалите мой номер", "удалите мой номер из базы"),
        ("удалите номер", "Удалите номер, пожалуйста"),
        ("спам", "это СПАМ"),
        ("жалоб", "напишу жалобу"),
        ("отстань", "отстаньте"),
    ],
)
def test_every_default_fragment_catches_its_refusal(fragment, message):
    assert fragment in SETTINGS.stopwords
    assert domain.stopword(message, SETTINGS.stopwords) == fragment


def test_an_empty_or_one_letter_fragment_is_refused_by_the_schema():
    for fragments in (["спам", ""], ["спам", " "], ["а"]):
        with pytest.raises(ValueError):
            DeliverySettings(stopwords=fragments)


def test_the_touches_are_one_more_than_the_pauses():
    assert DeliverySettings().touches == 3
    assert DeliverySettings(follow_up_days=[2, 4, 8]).touches == 4
    assert "touches" in DeliverySettings().model_dump()


# --- where the calendar moves a number --------------------------------------------------


def test_the_calendar_moves_a_new_number_to_warming_and_a_warming_one_to_active():
    assert domain.promoted(NumberStatus.NEW, Phase.SOCKET_DELAY) is None
    assert domain.promoted(NumberStatus.NEW, Phase.PASSIVE) is NumberStatus.WARMING
    assert domain.promoted(NumberStatus.WARMING, Phase.INTERNAL) is None
    assert domain.promoted(NumberStatus.WARMING, Phase.COLD) is NumberStatus.ACTIVE
    for status in (NumberStatus.ACTIVE, NumberStatus.QUARANTINED, NumberStatus.BANNED):
        assert domain.promoted(status, Phase.COLD) is None


def test_a_number_leaves_quarantine_where_its_calendar_puts_it():
    assert domain.released(domain.plan_for(STARTED, True, day(1), 30)) is NumberStatus.ACTIVE
    assert domain.released(domain.plan_for(STARTED, False, day(11), 30)) is NumberStatus.ACTIVE
    assert domain.released(domain.plan_for(STARTED, False, day(5), 30)) is NumberStatus.WARMING


# --- found by the review of #37 ------------------------------------------------------------


def test_a_spent_limit_on_friday_waits_for_monday_morning():
    friday = datetime(2026, 9, 4, 7, 0, tzinfo=UTC)  # 12:00 in Almaty

    decision = domain.check(attempt(capacity=0), friday, SETTINGS)

    # Monday 10:00 in Almaty = 05:00 UTC, not Saturday at the same hour.
    assert decision.send_after == datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


def test_the_jitter_ends_exactly_at_its_moment():
    held = attempt(last_sent_at=INSIDE - timedelta(minutes=15), jitter_minutes=15.0)
    assert domain.check(held, INSIDE, SETTINGS).action is Action.SEND


def test_the_health_thresholds_are_strict_at_the_edge():
    steady = {"state": "connected", "reconnects": 0}
    edge = Rates(sent=20, delivered=16, threads=20, replies=1)  # 80 % delivered, 5 % answered
    assert verdict("active", steady, edge) is None
    assert (
        verdict("active", steady, Rates(sent=20, delivered=15, threads=20, replies=1)) is not None
    )
    assert (
        verdict("active", steady, Rates(sent=20, delivered=20, threads=20, replies=0)) is not None
    )
