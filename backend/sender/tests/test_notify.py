"""Telegram: только события, требующие человека.

Отсутствие ключей — не ошибка, а «канал не настроен»: на машине разработчика
монитор здоровья обязан работать без бота.
"""

import httpx
import pytest

from sender import notify


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", "T0KEN")
    monkeypatch.setattr(notify.settings, "telegram_chat_id", "42")


async def test_returns_false_without_token(monkeypatch):
    monkeypatch.setattr(notify.settings, "telegram_bot_token", None)
    assert await notify.send("номер +7700 в карантине") is False


async def test_posts_text_to_configured_chat(configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await notify.send("номер +7700 забанен", client) is True
    assert "T0KEN" in seen["url"]
    assert "+7700" in seen["body"]


async def test_swallows_transport_failure(configured):
    """Упавшее уведомление не должно ронять монитор здоровья: авария номера
    важнее аварии телеграма."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await notify.send("что угодно", client) is False
