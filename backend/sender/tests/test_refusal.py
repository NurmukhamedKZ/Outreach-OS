"""Шов к юридическому контуру системы 1.

Отказ — единственное, что возвращается из системы 3 в систему 1, и он не имеет
права зависеть от импорта: collector системе 3 недоступен (тест графа импортов),
а вебхук — ручка, которой параметр не передать.
"""

from sender.services import refusal


def test_a_registered_hook_gets_the_refusal(monkeypatch):
    written = []
    monkeypatch.setattr(refusal, "_hook", lambda handle, reason: written.append((handle, reason)) or True)

    assert refusal.refuse("+77010000001", "стоп-слово: отпиш") is True
    assert written == [("+77010000001", "стоп-слово: отпиш")]


def test_an_unregistered_hook_is_loud_and_does_not_crash(monkeypatch, caplog):
    """Молчащий шов — это потерянный отказ, а отказ юридический контур (F21)."""
    monkeypatch.setattr(refusal, "_hook", None)

    assert refusal.refuse("+77010000001", "стоп-слово") is False
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_use_registers_the_hook():
    calls = []
    refusal.use(lambda handle, reason: calls.append(handle) or True)
    try:
        assert refusal.refuse("+77010000002", "почему") is True
        assert calls == ["+77010000002"]
    finally:
        refusal.use(None)
