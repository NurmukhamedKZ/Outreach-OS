"""«Сейчас» продукта. В бою — системное время, в песочнице — сдвинутое.

Шов, а не настройка: смещение принадлежит прогону песочницы, и какой прогон
активен, знает она — clock хранит только сдвиг, тем же приёмом, каким
activity.use() хранит путь к журналу.

В бою use() не зовёт никто, смещение нулевое, и clock.now() — это буквально
datetime.now(timezone.utc). Поэтому переводить на него можно всё, включая
код, который в песочнице не работает.
"""

from datetime import datetime, timedelta, timezone

_offset = timedelta()


def now() -> datetime:
    """Момент, который продукт считает настоящим. Всегда с зоной: наивное
    время в очереди означало бы окно отправки не в том часовом поясе."""
    return datetime.now(timezone.utc) + _offset


def use(offset: timedelta | None) -> None:
    """Шов: сдвиг приходит от того, кто знает про прогоны. None — бой."""
    global _offset
    _offset = offset if offset is not None else timedelta()


def offset() -> timedelta:
    return _offset
