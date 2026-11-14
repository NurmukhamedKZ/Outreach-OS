"""Тумблеры аварий. Настройка стенда, а не факт истории.

Поэтому живут в памяти процесса и не переживают перезапуск — в отличие от
часов, которые принадлежат прогону и лежат в его базе. Авария, пережившая
перезапуск, объяснялась бы потом полдня.
"""

from dataclasses import dataclass, replace
from typing import Any

SEND = ("ok", "not_sent", "unknown")
DELIVERY = ("delivered", "read", "silent")
NUMBER = ("connected", "loggedOut", "stalled")


@dataclass(frozen=True)
class Faults:
    send: str = "ok"
    delivery: str = "delivered"
    number: str = "connected"
    has_whatsapp: bool = True


_current = Faults()

_ALLOWED = {"send": SEND, "delivery": DELIVERY, "number": NUMBER}


def current() -> Faults:
    return _current


def update(
    send: str | None = None,
    delivery: str | None = None,
    number: str | None = None,
    has_whatsapp: bool | None = None,
) -> Faults:
    """Частичное обновление: пульт шлёт только то, что переключили.

    Raises:
        ValueError: a switch is set to a state it does not have.
    """
    global _current
    switches = {"send": send, "delivery": delivery, "number": number}
    for field, given in switches.items():
        if given is not None and given not in _ALLOWED[field]:
            raise ValueError(f"{field}: {given!r}, бывают {_ALLOWED[field]}")
    changed: dict[str, Any] = {key: value for key, value in switches.items() if value is not None}
    if has_whatsapp is not None:
        changed["has_whatsapp"] = has_whatsapp
    _current = replace(_current, **changed)
    return _current


def reset() -> None:
    global _current
    _current = Faults()
