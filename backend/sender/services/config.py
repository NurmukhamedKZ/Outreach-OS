"""Конфиг системы 3. Пути резолвятся от каталога sender/, а не от cwd.

Тот же файл читается из двух рабочих каталогов: из backend/ (uvicorn) и из
sender/ (ручные проверки). Относительный путь означал бы в этих двух случаях
разные базы, и пул номеров тихо разъехался бы с перепиской.
"""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent


def load() -> dict:
    config = tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
    config["state_db"] = (HOME / config["state_db"]).resolve()
    return config
