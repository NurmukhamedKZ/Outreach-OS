"""Промпт хода: контекст лида, состоявшаяся переписка и задача — и ничего сверх."""

import httpx
import pytest

from writer.services import agent, config
from writer.schemas.outreach import Draft

CONFIG = config.load()

# Задача хода перестала быть свойством `agent`: у ответа в диалоге свой автор —
# seller, и его промпт живёт в seller.py.
TASK = "Задача: ответить на последнюю реплику лида."


def test_prompt_carries_context_history_and_task():
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": []},
        "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"},
                    {"type": "ads_platform", "quote": "Google Ads"}],
    }
    history = [
        {"role": "outgoing", "text": "Первое сообщение", "angle": "crm_widget"},
        {"role": "incoming", "text": "а сколько это стоит?", "angle": None},
    ]

    text = agent.prompt(seed, history, TASK)
    assert "Ромашка" in text and "бухгалтерия" in text, "контекст лида не попал в промпт"
    assert "виджет Bitrix24" in text, "цитата сигнала потеряна"
    assert "а сколько это стоит?" in text, "ответ лида не попал в промпт"
    assert TASK in text, "задача хода не попала в промпт"

    # Углы follow-up: использованный не предлагается второй раз, иначе «новый
    # повод» окажется тем же самым, только другими словами.
    assert agent.unused_angles(seed, ["crm_widget"]) == ["ads_platform"]
    assert agent.unused_angles(seed, ["crm_widget", "ads_platform"]) == []
    assert "3" in agent.followup_task(3, ["ads_platform"]), "в follow-up не видно, сколько молчат"


def test_system_role_carries_offer(monkeypatch):
    # Системная роль несёт оффер из конфига: без него модель напишет письмо про
    # услугу, которой у нас нет.
    import observability
    monkeypatch.setattr(observability.settings, "langfuse_public_key", None)
    monkeypatch.setattr(observability.settings, "langfuse_secret_key", None)
    observability.langfuse_handler.cache_clear()

    seed = {"name": "Ромашка", "city": "almaty",
            "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                        "approach": "заходить через рост", "sources": []},
            "signals": []}
    fake = FakeModel(Draft(text="Здравствуйте!", angle="ads_platform"))
    result = agent.draft(fake, seed, [], TASK,
                          session_id="thread-1", name="sender.followup",
                          offer=CONFIG["offer"]["text"],
                          pitchable=frozenset({"ads_platform"}))
    assert result.draft.angle == "ads_platform", result
    assert fake.seen[0][0] == "system", fake.seen[0]
    assert CONFIG["offer"]["text"].strip()[:40] in fake.seen[0][1], "оффер не дошёл до модели"
    assert fake.seen_config["run_name"] == "sender.followup"
    assert fake.seen_config["metadata"]["langfuse_session_id"] == "thread-1"
    assert fake.seen_config["callbacks"] == []


class FakeModel:
    """Заглушка вместо сети: проверяем, что уходит в модель, а не что она вернёт."""

    def __init__(self, answer):
        self.answer, self.seen, self.seen_config = answer, None, None

    def invoke(self, messages, config=None):
        self.seen, self.seen_config = messages, config
        return self.answer


class FlakyModel:
    """Падает N раз обрывом соединения, потом отвечает — как протухший keep-alive."""

    def __init__(self, fail_times, answer):
        self.fail_times, self.answer, self.calls = fail_times, answer, 0

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.RemoteProtocolError("peer closed connection")
        return self.answer


def test_draft_retries_transport_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    seed = {"name": "Ромашка", "city": "almaty",
            "dossier": {"summary": "", "hooks": [], "pains": [], "approach": "", "sources": []},
            "signals": []}
    flaky = FlakyModel(fail_times=agent.TRANSPORT_RETRIES - 1,
                        answer=Draft(text="Здравствуйте!", angle="ads_platform"))

    result = agent.draft(flaky, seed, [], agent.FIRST, session_id="thread-1", name="writer.first",
                         pitchable=frozenset({"ads_platform"}))

    assert result.draft.angle == "ads_platform"
    assert flaky.calls == agent.TRANSPORT_RETRIES


def test_draft_gives_up_after_max_transport_retries(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    seed = {"name": "Ромашка", "city": "almaty",
            "dossier": {"summary": "", "hooks": [], "pains": [], "approach": "", "sources": []},
            "signals": []}
    flaky = FlakyModel(fail_times=agent.TRANSPORT_RETRIES, answer=None)

    with pytest.raises(httpx.RemoteProtocolError):
        agent.draft(flaky, seed, [], agent.FIRST, session_id="thread-1", name="writer.first",
                    pitchable=frozenset({"ads_platform"}))

    assert flaky.calls == agent.TRANSPORT_RETRIES


def test_prompt_names_decision_maker_when_known():
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": [],
                    "decision_maker": "Айгуль, основатель"},
        "signals": [],
    }
    assert "Кто решает: Айгуль, основатель" in agent.prompt(seed, [], TASK)


def test_prompt_stays_silent_about_unknown_decision_maker():
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": [],
                    "decision_maker": None},
        "signals": [],
    }
    assert "Кто решает" not in agent.prompt(seed, [], TASK), \
        "пустая строка про ЛПР — приглашение модели выдумать имя"


def test_unknown_angle_collapses_into_other():
    """Модель вернула описание фразой вместо типа сигнала. Ронять из-за этого
    готовый черновик незачем, но и в разрез аналитики такой угол пускать
    нельзя: таблица by_angle наполнится вариациями одного и того же повода."""
    assert agent.normalize_angle("рассказал про отзывы",
                                 frozenset({"site_no_pricing"})) == agent.OTHER_ANGLE


def test_known_angle_and_answer_survive():
    pitchable = frozenset({"site_no_pricing"})

    assert agent.normalize_angle("site_no_pricing", pitchable) == "site_no_pricing"
    assert agent.normalize_angle("answer", pitchable) == "answer"


def test_system_prompt_carries_rules_of_current_stage_only():
    from writer.services import stages

    system = agent.system_prompt(offer="оплата за встречу", stage="contact")

    assert stages.rules_for("contact") in system
    assert stages.rules_for("closing") not in system, \
        "правила чужого этапа в промпте — приглашение перескочить"