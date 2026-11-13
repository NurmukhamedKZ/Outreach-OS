"""«Черновики топ-N» берут лидов из research: кто лид, решает research, о чём писать — writer."""

import pytest

from refusals import public as refusals
from writer.db import thread_store
from writer.services import agent, operations
from writer.tests.research_leads import HOOK, NOW, add_lead, days_ago


class Ctx:
    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    def progress(self, current, total, label):
        pass

    def check_cancelled(self):
        pass


@pytest.fixture
def drafting(monkeypatch):
    """The model stand-in: every first message is drafted, each seed is kept."""
    seeds = []

    def draft(llm, seed, history, task, **kwargs):
        seeds.append(seed)
        return agent.Attempt(draft=agent.Draft(text="Здравствуйте", angle="reviews_missed_lead",
                                               stop=False), prompt=[], model="")

    monkeypatch.setattr(operations.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(operations.agent, "model", lambda config: object())
    monkeypatch.setattr(operations.agent, "draft", draft)
    return seeds


def test_drafts_go_to_leads_by_score_past_threads_and_refusals(db, drafting):
    add_lead(db, "low", [("phone", "+77010000001")],
             signals=[("site_no_pricing", NOW, "цены по запросу")])
    add_lead(db, "high", [("whatsapp", "+77010000002")],
             signals=[("reviews_missed_lead", NOW, HOOK)])
    add_lead(db, "refused", [("whatsapp", "+77010000003")],
             signals=[("reviews_missed_lead", NOW, HOOK)])
    add_lead(db, "threaded", [("whatsapp", "+77010000004")],
             signals=[("reviews_missed_lead", NOW, HOOK)])
    refusals.record(db, "+77010000003", "просил не писать", NOW)
    thread_store.open_thread(db, "+77010000004", "threaded", {})

    result = operations.open_new_threads(Ctx())

    assert result == {"drafted": 2}
    assert [seed["name"] for seed in drafting] == ["Компания high", "Компания low"]
    opened = {row["thread_id"]: row["company_id"] for row in thread_store.inbox(db)}
    assert opened == {"+77010000002": "high", "+77010000001": "low", "+77010000004": "threaded"}


def test_the_seed_carries_the_dossier_and_only_fresh_pitchable_signals(db, drafting):
    add_lead(db, "a", [("whatsapp", "+77010000001")],
             hooks=[(HOOK, days_ago(2)), ("Отзыв двухлетней давности", days_ago(700))],
             signals=[("reviews_missed_lead", days_ago(2), HOOK),
                      ("crm_widget", NOW, "Bitrix24"),
                      ("site_hiring_sales", days_ago(90), "Ищем менеджера по продажам")])

    operations.open_new_threads(Ctx())

    [seed] = drafting
    assert seed["city"] == "almaty"
    assert seed["dossier"]["summary"] == "Бухгалтерская фирма на Абая"
    assert seed["dossier"]["decision_maker"] == "Айгуль, основатель"
    assert seed["dossier"]["approach"] == "Начать с отзывов о недозвоне"
    assert [(h["source"], h["quote"]) for h in seed["dossier"]["hooks"]] == [("reviews", HOOK)]
    assert [(s["type"], s["quote"]) for s in seed["signals"]] == [("reviews_missed_lead", HOOK)]
    stored = thread_store.thread(db, "+77010000001")["seed"]
    assert stored == seed


def test_a_fresh_lead_behind_a_threaded_top_is_still_found(db, drafting):
    """Регрессия: окно отбора резалось до фильтра «тред уже есть», и когда весь
    топ уже в работе — обычное состояние после нескольких прогонов, — свежих
    лидов не находилось, хотя дальше по списку они были."""
    top_n = operations.CONFIG["llm"]["top_n"]
    for n in range(top_n):
        add_lead(db, f"busy{n}", [("whatsapp", f"+7701000{n:04d}")],
                 signals=[("reviews_missed_lead", NOW, HOOK)])
        thread_store.open_thread(db, f"+7701000{n:04d}", f"busy{n}", {})
    add_lead(db, "fresh", [("whatsapp", "+77019999999")])

    assert operations.open_new_threads(Ctx()) == {"drafted": 1}
    assert [seed["name"] for seed in drafting] == ["Компания fresh"]


def test_a_followup_takes_the_next_research_signal_the_thread_has_not_used(db, drafting):
    from sender.db import conversation
    from writer.services import followup

    add_lead(db, "a", [("whatsapp", "+77010000001")],
             signals=[("reviews_missed_lead", NOW, HOOK),
                      ("site_no_pricing", NOW, "Цены на сайте не указаны")])
    operations.open_new_threads(Ctx())
    [(message_id,)] = db.execute("SELECT message_id FROM messages").fetchall()
    with db:
        conversation.confirm_sent(db, message_id, "P1", NOW)

    task = followup.task(db, thread_store.thread(db, "+77010000001"))

    assert "возьми угол site_no_pricing" in task
