"""Конфиг системы 3. Пути резолвятся от каталога sender/, а не от cwd.

Тот же файл читается из двух рабочих каталогов: из backend/ (uvicorn) и из
sender/ (ручные проверки). Относительный путь означал бы в этих двух случаях
разные базы, и пул номеров тихо разъехался бы с перепиской.
"""

import logging
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

HOME = Path(__file__).resolve().parent.parent

# От «ничего не уходит» к «уходит всё». `manual` — модель только готовит
# черновики, уходит то, что оператор поставил в очередь кнопкой.
MODES = ("off", "manual", "replies", "full")

# Kill switch закрыт: стоит даже то, что уже в очереди.
OFF = "off"

# Kill switch отдельным файлом, а не строкой в config.toml: автопилот
# выключается кнопкой за секунду, а config.toml правит человек — робот, лезущий
# в тот же файл, стирает его комментарии и дерётся с ним за содержимое.
OVERRIDE = HOME / "autopilot"


def load() -> dict:
    config = tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
    return config


def autopilot() -> str:
    """Режим на сейчас. Читается каждым тиком: kill switch, который надо
    выключать перезапуском процесса, — не kill switch."""
    if OVERRIDE.exists():
        mode = OVERRIDE.read_text(encoding="utf-8").strip()
        if mode in MODES:
            return mode
        # Fail closed, а не откат на config.toml: умолчание там — manual, и
        # битый файл поверх выбранного «Стоп» отправил бы стоящую очередь.
        log.warning("в %s лежит неизвестный режим %r — держу отправку (off)",
                    OVERRIDE, mode)
        return OFF
    return load()["autopilot"]["mode"]


def set_autopilot(mode: str) -> None:
    """Запись атомарна: недописанный файл, прочитанный тиком, — это режим,
    которого никто не выбирал."""
    if mode not in MODES:
        raise ValueError(f"неизвестный режим автопилота: {mode}; бывают {MODES}")
    temporary = OVERRIDE.with_name(f"{OVERRIDE.name}.tmp")
    temporary.write_text(f"{mode}\n", encoding="utf-8")
    temporary.replace(OVERRIDE)
