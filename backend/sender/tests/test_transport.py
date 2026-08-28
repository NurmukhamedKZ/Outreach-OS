"""Шов с Node. Живого WhatsApp здесь нет: httpx.MockTransport отвечает вместо
Node, и проверяется ровно контракт — что уходит в ручку и что приезжает назад.
"""

import json

import httpx
import pytest

from sender import transport


def stub(handler) -> transport.Transport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return transport.Transport("http://node.test", client)


@pytest.mark.asyncio
async def test_send_passes_idempotency_key_and_returns_provider_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True, "sent": True, "provider_id": "3EB0"})

    result = await stub(handler).send("+7700", "+7701", "привет", key="42")

    assert seen["key"] == "42"
    assert seen["type"] == "text"
    assert result == transport.Sent(sent=True, provider_id="3EB0", error=None)


@pytest.mark.asyncio
async def test_send_reports_socket_failure_without_raising():
    """sent=False — сообщение точно не ушло; это не авария, а повод ретраить."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "sent": False, "error": "loggedOut"})

    result = await stub(handler).send("+7700", "+7701", "привет", key="42")

    assert result.sent is False
    assert result.error == "loggedOut"


@pytest.mark.asyncio
async def test_http_error_raises_transport_error():
    """Не-2xx от Node — неопределённость: ушло или нет, неизвестно."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(transport.TransportError):
        await stub(handler).send("+7700", "+7701", "привет", key="42")


@pytest.mark.asyncio
async def test_check_returns_bool():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_whatsapp": False})

    assert await stub(handler).check("+7700", "+7701") is False


@pytest.mark.asyncio
async def test_pair_returns_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "ABCD-1234"})

    assert await stub(handler).pair("+7700") == "ABCD-1234"


@pytest.mark.asyncio
async def test_health_returns_state_per_number():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"+7700": {"state": "connected", "reconnects": 2}})

    assert (await stub(handler).health())["+7700"]["state"] == "connected"
