from typing import Any

import pytest
from pydantic import ValidationError

from conversations import domain
from conversations.schemas import (
    ConversationSettings,
    Decision,
    Draft,
    OfferVariant,
    SellerAnswer,
    Stage,
)

SEED: dict[str, Any] = {
    "name": "Ромашка",
    "city": "almaty",
    "dossier": {
        "summary": "бухгалтерия",
        "hooks": [],
        "pains": [],
        "approach": "заходить через рост",
        "decision_maker": "Айгуль, основатель",
    },
    "signals": [
        {"type": "site_no_pricing", "quote": "цены по запросу"},
        {"type": "site_hiring_sales", "quote": "ищем менеджера"},
    ],
}
HISTORY: list[dict[str, Any]] = [
    {"role": "outgoing", "text": "Первое сообщение", "angle": "site_no_pricing"},
    {"role": "incoming", "text": "а сколько это стоит?", "angle": None},
]
VARIANTS = [
    OfferVariant(id="free_first_meetings", text="первые две встречи за наш счёт"),
    OfferVariant(id="pay_per_meeting", text="оплата за состоявшуюся встречу"),
]


def test_each_reply_moves_the_thread_one_step_and_the_last_stays():
    assert domain.advance(Stage.CONTACT) == Stage.PROBING
    assert domain.advance(Stage.PROBING) == Stage.OFFER
    assert domain.advance(Stage.OFFER) == Stage.CLOSING
    assert domain.advance(Stage.CLOSING) == Stage.CLOSING


def test_the_first_touch_may_not_offer_a_call():
    assert domain.RULES[Stage.CONTACT] != domain.RULES[Stage.CLOSING]
    assert "созвон" not in domain.RULES[Stage.CONTACT].lower()


def test_the_system_prompt_carries_the_offer_and_the_rules_of_the_current_stage_only():
    system = domain.writer_system("оплата за встречу", Stage.CONTACT)

    assert "оплата за встречу" in system
    assert domain.RULES[Stage.CONTACT] in system
    assert domain.RULES[Stage.CLOSING] not in system


def test_a_thread_keeps_its_offer_variant_and_the_variants_split_the_threads():
    settings = ConversationSettings(variants=VARIANTS)

    assert domain.offer_for("+77010000001", settings) == domain.offer_for("+77010000001", settings)
    ids = {domain.offer_for(f"+7701000{n:04d}", settings)[0] for n in range(50)}
    assert ids == {"free_first_meetings", "pay_per_meeting"}


def test_without_variants_the_base_offer_is_argued_under_no_id():
    settings = ConversationSettings(offer="один оффер", variants=[])

    assert domain.offer_for("+77010000001", settings) == (None, "один оффер")


def test_the_prompt_carries_the_lead_the_dialogue_and_the_task():
    text = domain.prompt(SEED, HISTORY, "Задача: ответить.")

    assert "Ромашка" in text and "бухгалтерия" in text
    assert "Кто решает: Айгуль, основатель" in text
    assert "цены по запросу" in text
    assert "они: а сколько это стоит?" in text
    assert text.endswith("Задача: ответить.")


def test_the_prompt_stays_silent_about_an_unknown_decision_maker():
    seed = {**SEED, "dossier": {**SEED["dossier"], "decision_maker": None}}

    assert "Кто решает" not in domain.prompt(seed, [], "Задача")


def test_a_followup_takes_an_unused_angle_and_without_one_asks_for_stop():
    unused = domain.unused_angles(SEED, ["site_no_pricing"])

    assert unused == ["site_hiring_sales"]
    assert "site_hiring_sales" in domain.followup_task(3, unused)
    assert "3 дней" in domain.followup_task(3, unused)
    assert domain.unused_angles(SEED, ["site_no_pricing", "site_hiring_sales"]) == []
    assert "stop=true" in domain.followup_task(5, [])


def test_an_angle_that_is_no_signal_type_collapses_into_other():
    pitchable = frozenset({"site_no_pricing"})

    assert domain.normalize_angle("рассказал про отзывы", pitchable) == domain.OTHER_ANGLE
    assert domain.normalize_angle("site_no_pricing", pitchable) == "site_no_pricing"
    assert domain.normalize_angle("answer", pitchable) == "answer"


def test_the_seller_sees_the_offer_the_dialogue_and_the_rules_of_its_stage():
    system, human = domain.seller_messages(SEED, HISTORY, "оплата за встречу", Stage.OFFER)

    assert system[0] == "system" and "оплата за встречу" in system[1]
    assert human[0] == "human"
    assert "а сколько это стоит?" in human[1]
    assert domain.RULES[Stage.OFFER] in human[1]


@pytest.mark.parametrize(
    "text",
    ["Это стоит 50 000 ₸ в месяц", "всего 300$", "от 20 000 тенге", "x" * 701],
)
def test_an_answer_with_a_price_or_too_long_goes_to_a_human(text):
    assert domain.needs_human(text, max_chars=700)


def test_a_plain_answer_goes_out():
    assert not domain.needs_human("Цену назовём после короткого разговора.", max_chars=700)


def test_every_verdict_ends_the_automaton_part():
    assert set(domain.VERDICT_STATUS) == set(Decision) - {Decision.ANSWER}
    assert set(domain.VERDICT_STATUS.values()) <= {*domain.AUTOMATON_STOPS}


def test_a_draft_without_a_new_reason_or_too_long_is_refused():
    assert Draft(text="Здравствуйте!", angle="site_no_pricing").stop is False
    with pytest.raises(ValidationError):
        Draft(text="Просто напоминаю о себе", angle="other")
    with pytest.raises(ValidationError):
        Draft(text="а" * 701, angle="other")


def test_an_answer_decision_needs_its_text():
    assert SellerAnswer(decision=Decision.JUNK, reason="спам").text == ""
    with pytest.raises(ValidationError):
        SellerAnswer(decision=Decision.ANSWER, reason="ответил", text="  ")


def test_the_defaults_are_the_writer_config_of_before():
    settings = ConversationSettings()

    assert (settings.model, settings.top_n, settings.max_age_days) == (
        "deepseek/deepseek-v4-flash",
        10,
        60,
    )
    assert [v.id for v in settings.variants] == ["free_first_meetings", "pay_per_meeting"]
    assert "reviews_missed_lead" in settings.pitchable


def test_two_variants_may_not_share_an_id():
    with pytest.raises(ValidationError):
        ConversationSettings(variants=[VARIANTS[0], VARIANTS[0]])
