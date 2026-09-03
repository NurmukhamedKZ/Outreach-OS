"""Подменный Node: тот же контракт, что у sender/node/index.js.

Транспорт системы 3 не переписывается ни строкой, поэтому проверяется ровно
то, на что он опирается: sent/провайдерский id, идемпотентность по ключу и
разница между «не ушло» и «неизвестно».
"""

import pytest
from fastapi import HTTPException

from sandbox import faults, node


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    node.forget_keys()
    faults.reset()
    sent = []
    monkeypatch.setattr(node, "deliver", _record(sent))
    # Статус уезжает фоновой задачей с паузой: в тесте её ждёт node.settle(),
    # а пауза обнуляется, чтобы сюита не спала секунду на каждой отправке.
    monkeypatch.setattr(node, "STATUS_PAUSE_SECONDS", 0)
    yield sent
    faults.reset()


def _record(sent):
    async def deliver(payload: dict) -> bool:
        sent.append(payload)
        return True
    return deliver


async def test_send_returns_a_provider_id():
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is True
    assert body["provider_id"]


async def test_repeat_by_key_does_not_send_twice(clean):
    first = await node.send(node.SendRequest(number="+77000000001",
                                             to="+77010000001", text="привет",
                                             key="outbox-1"))
    again = await node.send(node.SendRequest(number="+77000000001",
                                             to="+77010000001", text="привет",
                                             key="outbox-1"))
    await node.settle()
    assert again["provider_id"] == first["provider_id"]
    assert len(clean) == 1, "дубликат в холодном аутриче — прямой повод нажать Report"


async def test_not_sent_is_an_honest_no(clean):
    faults.update(send="not_sent")
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is False
    assert body["provider_id"] is None
    await node.settle()
    assert clean == [], "несостоявшаяся отправка не может иметь статуса доставки"


async def test_unknown_is_a_five_hundred():
    """`sent:false` — обещание «фрейм в сокет не ушёл», на нём строится
    безопасный ретрай. Неизвестность обязана приезжать как TransportError."""
    faults.update(send="unknown")
    with pytest.raises(HTTPException) as failure:
        await node.send(node.SendRequest(number="+77000000001",
                                         to="+77010000001", text="привет",
                                         key="outbox-1"))
    assert failure.value.status_code == 500


async def test_delivery_status_goes_to_our_own_webhook(clean):
    await node.send(node.SendRequest(number="+77000000001", to="+77010000001",
                                     text="привет", key="outbox-1"))
    await node.settle()
    assert clean[0]["kind"] == "status"
    assert clean[0]["status"] == node.DELIVERED
    assert clean[0]["provider_id"]


async def test_read_status_when_asked(clean):
    faults.update(delivery="read")
    await node.send(node.SendRequest(number="+77000000001", to="+77010000001",
                                     text="привет", key="outbox-1"))
    await node.settle()
    assert clean[0]["status"] == node.READ


async def test_silence_leaves_the_thread_in_queued(clean):
    faults.update(delivery="silent")
    body = await node.send(node.SendRequest(number="+77000000001",
                                            to="+77010000001", text="привет",
                                            key="outbox-1"))
    assert body["sent"] is True
    await node.settle()
    assert clean == []


async def test_check_follows_the_switch():
    assert await node.check(node.CheckRequest(number="+77000000001",
                                              to="+77010000001")) == {"has_whatsapp": True}
    faults.update(has_whatsapp=False)
    assert await node.check(node.CheckRequest(number="+77000000001",
                                              to="+77010000001")) == {"has_whatsapp": False}


async def test_health_reports_the_switched_state():
    faults.update(number="loggedOut")
    report = await node.health()
    assert report[node.SANDBOX_NUMBER]["state"] == "loggedOut"


async def test_pair_and_qr_answer_without_a_phone():
    """Страница пула обязана работать: она зовёт обе ручки на живом номере."""
    assert (await node.pair(node.NumberRequest(number="+77000000001")))["code"]
    assert (await node.qr(node.NumberRequest(number="+77000000001")))["qr"].startswith("data:image")
