"""The seam with Node: httpx.MockTransport answers instead of Node, the contract is checked."""

import json

import httpx
import pytest

from core import whatsapp
from core.errors import DomainError


def node(handler) -> whatsapp.NodeWhatsApp:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return whatsapp.NodeWhatsApp("http://node.test/", client)


def test_send_passes_the_idempotency_key_and_returns_the_provider_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True, "sent": True, "provider_id": "3EB0"})

    result = node(handler).send("+77001112233", "+77010000001", "привет", key="outbox-42")

    assert seen["url"] == "http://node.test/send"
    assert (seen["key"], seen["type"], seen["to"]) == ("outbox-42", "text", "+77010000001")
    assert result == whatsapp.Sent(sent=True, provider_id="3EB0", error=None)


def test_a_frame_that_did_not_leave_is_an_answer_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "sent": False, "error": "loggedOut"})

    result = node(handler).send("+77001112233", "+77010000001", "привет", key="outbox-1")

    assert (result.sent, result.error) == (False, "loggedOut")


def test_a_non_2xx_answer_means_the_fate_of_the_send_is_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "timeout"})

    with pytest.raises(whatsapp.TransportError):
        node(handler).send("+77001112233", "+77010000001", "привет", key="outbox-1")


def test_a_dead_node_is_a_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(whatsapp.TransportError):
        node(handler).health()


def test_a_logged_out_session_comes_as_its_own_domain_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "sessionLoggedOut", "error": "loggedOut"})

    with pytest.raises(whatsapp.SessionLoggedOutError) as raised:
        node(handler).check("+77001112233", "+77010000001")

    assert isinstance(raised.value, whatsapp.TransportError)
    assert isinstance(raised.value, DomainError)
    assert (raised.value.code, raised.value.status_code) == ("errors.sessionLoggedOut", 409)


def test_check_pair_qr_and_health_read_their_fields():
    answers = {
        "/check": {"has_whatsapp": False},
        "/pair": {"code": "ABCD-1234"},
        "/qr": {"qr": "data:image/png;base64,AAAA"},
        "/health": {"+77001112233": {"state": "connected", "reconnects": 2}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers[request.url.path])

    transport = node(handler)

    assert transport.check("+77001112233", "+77010000001") is False
    assert transport.pair("+77001112233") == "ABCD-1234"
    assert transport.qr("+77001112233") == "data:image/png;base64,AAAA"
    assert transport.health() == {"+77001112233": {"state": "connected", "reconnects": 2}}


def test_the_fake_records_sends_and_answers_like_node():
    fake = whatsapp.FakeWhatsApp(sessions={"+77001112233": "loggedOut"})

    assert fake.send("+77001112233", "+77010000001", "привет", key="outbox-1").sent is True
    assert fake.sends == [
        {"number": "+77001112233", "to": "+77010000001", "text": "привет", "key": "outbox-1"}
    ]
    assert fake.health() == {"+77001112233": {"state": "loggedOut", "reconnects": 0}}
    with pytest.raises(whatsapp.SessionLoggedOutError):
        fake.check("+77001112233", "+77010000001")
    assert fake.qr("+77001112233").startswith("data:image/png")


def test_the_fake_refuses_and_breaks_on_demand():
    assert whatsapp.FakeWhatsApp(sent=False).send("+7700", "+7701", "x", key="k").sent is False
    assert whatsapp.FakeWhatsApp(has_whatsapp=False).check("+7700", "+7701") is False
    broken = whatsapp.FakeWhatsApp(error=whatsapp.TransportError("down"))
    with pytest.raises(whatsapp.TransportError):
        broken.send("+7700", "+7701", "x", key="k")
    with pytest.raises(whatsapp.TransportError):
        broken.health()
