"""Общая база тестов системы 3: state.db со всеми тремя слоями.

Таблицы переписки создаёт их владелец — thread_store системы 2, — а sender
только доливает свои колонки. Так же это происходит и в проде: collector
применяет schema.sql на старте, sender ALTER'ит уже существующее.
"""

from datetime import datetime, timezone

import pytest

import activity
from sender.db import migrate
from writer.db import thread_store

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)   # среда, 18:00 в Алматы


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    owner = thread_store.connect(path)          # threads и messages
    owner.execute("CREATE TABLE IF NOT EXISTS suppression ("
                  " handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, reason TEXT)")
    owner.commit()
    owner.close()
    activity.use(path)                          # журнал пишет в ту же state.db
    connection = migrate.connect(path)          # numbers, outbox, колонки состояния
    yield connection
    connection.close()
    activity.use(None)


class FakeTransport:
    """Транспорт без сети. `sent=False` — честное «фрейм в сокет не ушёл»."""

    def __init__(self, sent=True, has_whatsapp=True):
        self.sent_calls = []
        self.checked = []
        self._sent = sent
        self._has_whatsapp = has_whatsapp

    async def send(self, number, to, text, key, kind="text"):
        from sender.transport import Sent
        self.sent_calls.append({"number": number, "to": to, "text": text, "key": key})
        return Sent(sent=self._sent, provider_id="3EB0" if self._sent else None,
                    error=None if self._sent else "loggedOut")

    async def check(self, number, to):
        self.checked.append((number, to))
        return self._has_whatsapp


@pytest.fixture(autouse=True)
def isolated_autopilot(tmp_path, monkeypatch):
    """Kill switch — файл на машине оператора, и он не в git.

    Тест, который его читает, зелёный ровно тогда, когда тумблер стоит в
    нужном положении: полный прогон падал на test_status_lists_the_pool,
    пока в backend/sender/autopilot лежало «full». Отводим путь в tmp —
    режим берётся из config.toml, как на чистой установке, и ни один тест
    больше не зависит от того, чем оператор занимался перед прогоном.
    """
    from sender.services import config

    monkeypatch.setattr(config, "OVERRIDE", tmp_path / "autopilot")
