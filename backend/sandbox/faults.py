"""Тумблеры аварий. Настройка стенда, а не факт истории.

Поэтому живут в памяти процесса и не переживают перезапуск — в отличие от
часов, которые принадлежат прогону и лежат в его базе. Авария, пережившая
перезапуск, объяснялась бы потом полдня.
"""

from dataclasses import dataclass, replace

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


def update(**values: object) -> Faults:
    """Частичное обновление: пульт шлёт только то, что переключили."""
    global _current
    for field, allowed in _ALLOWED.items():
        given = values.get(field)
        if given is not None and given not in allowed:
            raise ValueError(f"{field}: {given!r}, бывают {allowed}")
    _current = replace(_current, **{key: value for key, value in values.items()
                                    if value is not None})
    return _current


def reset() -> None:
    global _current
    _current = Faults()
