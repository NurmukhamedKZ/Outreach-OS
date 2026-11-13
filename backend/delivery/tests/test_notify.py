"""Telegram: only events a human must act on; a channel not set up is no error."""

import logging

import httpx
import pytest

from config import settings
from delivery import notify


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "T0KEN")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")


def test_without_a_token_nothing_is_sent_and_the_log_keeps_no_text(monkeypatch, caplog):
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    with caplog.at_level(logging.INFO):
        assert notify.send("+77010000001 — разбирай руками") is False
    assert "+7701" not in caplog.text


def test_the_text_goes_to_the_configured_chat(configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.send("номер +7700 в карантине", client) is True
    assert "T0KEN" in seen["url"] and "+7700" in seen["body"]


def test_a_failure_of_telegram_is_swallowed(configured):
    """A number's trouble matters more than the trouble of the message about it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.send("что угодно", client) is False
