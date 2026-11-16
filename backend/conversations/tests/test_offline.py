"""The offline writer and seller: replies built from the prompt that pass the code's own rules."""

from typing import Any

import pytest

from conversations import domain, offline
from conversations.schemas import ConversationSettings, Decision, Draft, SellerAnswer, Stage
from conversations.tests.test_domain import SEED
from core.model import OFFLINE_MARK
from delivery.schemas import DeliverySettings

PITCHABLE = frozenset(ConversationSettings().pitchable)
HOOKED: dict[str, Any] = {
    **SEED,
    "dossier": {
        **SEED["dossier"],
        "hooks": [{"source": "reviews", "angle": "отзыв", "quote": "не дозвонились до офиса"}],
    },
}


def write(seed: dict[str, Any], task: str = domain.FIRST_TASK, history=()) -> Draft:
    messages = domain.writer_messages(seed, list(history), task, "оффер", Stage.CONTACT)
    draft = offline.answer(Draft, messages)
    assert isinstance(draft, Draft)
    return draft


def sell(*incoming: str) -> SellerAnswer:
    history = [{"role": "outgoing", "text": "Сколько звонков вы теряете?"}]
    history += [{"role": "incoming", "text": text} for text in incoming]
    answer = offline.answer(
        SellerAnswer, domain.seller_messages(SEED, history, "оффер", Stage.PROBING)
    )
    assert isinstance(answer, SellerAnswer)
    return answer


def test_a_first_draft_takes_the_first_signal_and_names_its_fact():
    draft = write(SEED)

    assert draft.text.startswith(f"{OFFLINE_MARK} Ромашка, ")
    assert "«цены по запросу»" in draft.text
    assert domain.normalize_angle(draft.angle, PITCHABLE) == "site_no_pricing"
    assert not draft.stop


def test_a_followup_takes_the_angle_its_task_names():
    draft = write(SEED, domain.followup_task(4, ["site_hiring_sales"]))

    assert (draft.angle, draft.stop) == ("site_hiring_sales", False)
    assert "«ищем менеджера»" in draft.text


def test_without_signals_a_hook_is_the_fact_and_the_angle_is_other():
    draft = write({**HOOKED, "signals": []})

    assert "«не дозвонились до офиса»" in draft.text
    assert domain.normalize_angle(draft.angle, PITCHABLE) == domain.OTHER_ANGLE


@pytest.mark.parametrize(
    "seed, task",
    [
        ({**SEED, "signals": []}, domain.FIRST_TASK),
        (SEED, domain.followup_task(9, [])),
    ],
    ids=["nothing to name", "no unused angle"],
)
def test_nothing_to_name_is_a_stop(seed, task):
    assert write(seed, task).stop


@pytest.mark.parametrize(
    "word, decision",
    [
        ("отказываемся", Decision.REFUSAL),
        ("созвонимся", Decision.INTERESTED),
        ("ошиблись", Decision.WRONG_NUMBER),
        ("абракадабра", Decision.JUNK),
    ],
)
def test_a_word_of_the_table_in_the_last_reply_gives_its_verdict(word, decision):
    answer = sell("Добрый день", f"Мы {word.upper()}, спасибо")

    assert (answer.decision, answer.text) == (decision, "")
    assert answer.reason.startswith(OFFLINE_MARK)


def test_any_other_reply_is_answered_and_goes_out():
    answer = sell("отказываемся", "А что вы предлагаете?")

    assert answer.decision == Decision.ANSWER, "only the last reply counts"
    assert answer.text.startswith(OFFLINE_MARK)
    assert answer.reason.startswith(OFFLINE_MARK)
    assert not domain.needs_human(answer.text, 600)


def test_a_question_about_the_price_is_answered_with_one_and_goes_to_a_human():
    answer = sell("Сколько это стоит?")

    assert answer.decision == Decision.ANSWER
    assert domain.needs_human(answer.text, 600)


def test_no_word_of_the_table_is_a_stop_word_closing_the_thread_before_the_seller():
    words = [*offline.VERDICTS, offline.PRICE_QUESTION]
    assert not [
        (word, stop) for word in words for stop in DeliverySettings().stopwords if stop in word
    ]
