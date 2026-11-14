"""Подменный Node: тот же контракт, что у sender/node/index.js.

Адаптер core/whatsapp.py не переписывается ни строкой, поэтому проверяется ровно
то, на что он опирается: sent/провайдерский id, идемпотентность по ключу и
разница между «не ушло» и «неизвестно».
"""

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from sandbox import faults, node, runs


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    node.forget_keys()
    faults.reset()
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(node, "deliver", _record(sent))
    # Статус уезжает потоком с паузой: в тесте её ждёт node.settle(),
    # а пауза обнуляется, чтобы сюита не спала секунду на каждой отправке.
    monkeypatch.setattr(node, "STATUS_PAUSE_SECONDS", 0)
    yield sent
    faults.reset()


def _record(sent):
    def deliver(payload: dict[str, Any]) -> bool:
        sent.append(payload)
        return True

    return deliver


def test_send_returns_a_provider_id():
    body = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    assert body["sent"] is True
    assert body["provider_id"]


def test_repeat_by_key_does_not_send_twice(clean):
    first = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    again = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    node.settle()
    assert again["provider_id"] == first["provider_id"]
    assert len(clean) == 1, "дубликат в холодном аутриче — прямой повод нажать Report"


def test_not_sent_is_an_honest_no(clean):
    faults.update(send="not_sent")
    body = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    assert body["sent"] is False
    assert body["provider_id"] is None
    node.settle()
    assert clean == [], "несостоявшаяся отправка не может иметь статуса доставки"


def test_unknown_is_a_five_hundred():
    """`sent:false` — обещание «фрейм в сокет не ушёл», на нём строится
    безопасный ретрай. Неизвестность обязана приезжать как TransportError."""
    faults.update(send="unknown")
    with pytest.raises(HTTPException) as failure:
        node.send(
            node.SendRequest(
                number="+77000000001", to="+77010000001", text="привет", key="outbox-1"
            )
        )
    assert failure.value.status_code == 500


def test_delivery_status_goes_to_our_own_webhook(clean):
    node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    node.settle()
    assert clean[0]["kind"] == "status"
    assert clean[0]["status"] == node.DELIVERED
    assert clean[0]["provider_id"]


def test_read_status_when_asked(clean):
    faults.update(delivery="read")
    node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    node.settle()
    assert clean[0]["status"] == node.READ


def test_silence_leaves_the_thread_in_queued(clean):
    faults.update(delivery="silent")
    body = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    assert body["sent"] is True
    node.settle()
    assert clean == []


def test_check_follows_the_switch():
    assert node.check(node.CheckRequest(number="+77000000001", to="+77010000001")) == {
        "has_whatsapp": True
    }
    faults.update(has_whatsapp=False)
    assert node.check(node.CheckRequest(number="+77000000001", to="+77010000001")) == {
        "has_whatsapp": False
    }


def test_health_reports_the_switched_state():
    faults.update(number="loggedOut")
    report = node.health()
    assert report[runs.SANDBOX_NUMBER]["state"] == "loggedOut"


def test_pair_and_qr_answer_without_a_phone():
    """Страница пула обязана работать: она зовёт обе ручки на живом номере."""
    assert (node.pair(node.NumberRequest(number="+77000000001")))["code"]
    assert (node.qr(node.NumberRequest(number="+77000000001")))["qr"].startswith("data:image")


def test_a_logged_out_session_refuses_a_check_with_the_code_and_sends_nothing(clean):
    faults.update(number="loggedOut")
    refused = node.check(node.CheckRequest(number="+77000000001", to="+77010000001"))
    assert isinstance(refused, JSONResponse)
    assert refused.status_code == 409 and b"sessionLoggedOut" in bytes(refused.body)
    body = node.send(
        node.SendRequest(number="+77000000001", to="+77010000001", text="привет", key="outbox-1")
    )
    assert body["sent"] is False


def test_a_qr_for_a_logged_out_session_opens_a_fresh_one():
    faults.update(number="loggedOut")
    node.qr(node.NumberRequest(number="+77000000001"))
    assert faults.current().number == "connected"
