"""Промпт хода: контекст лида, состоявшаяся переписка и задача — и ничего сверх."""

from writer.services import agent, config
from writer.schemas.outreach import Draft

CONFIG = config.load()


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

    text = agent.prompt(seed, history, agent.REPLY)
    assert "Ромашка" in text and "бухгалтерия" in text, "контекст лида не попал в промпт"
    assert "виджет Bitrix24" in text, "цитата сигнала потеряна"
    assert "а сколько это стоит?" in text, "ответ лида не попал в промпт"
    assert agent.REPLY in text, "задача хода не попала в промпт"

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
    result = agent.draft(fake, seed, [], agent.REPLY,
                          session_id="thread-1", name="writer.reply",
                          offer=CONFIG["offer"]["text"])
    assert result.angle == "ads_platform", result
    assert fake.seen[0][0] == "system", fake.seen[0]
    assert CONFIG["offer"]["text"].strip()[:40] in fake.seen[0][1], "оффер не дошёл до модели"
    assert fake.seen_config["run_name"] == "writer.reply"
    assert fake.seen_config["metadata"]["langfuse_session_id"] == "thread-1"
    assert fake.seen_config["callbacks"] == []


class FakeModel:
    """Заглушка вместо сети: проверяем, что уходит в модель, а не что она вернёт."""

    def __init__(self, answer):
        self.answer, self.seen, self.seen_config = answer, None, None

    def invoke(self, messages, config=None):
        self.seen, self.seen_config = messages, config
        return self.answer