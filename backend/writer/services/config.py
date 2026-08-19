"""Конфиг системы 2. Пути резолвятся от каталога writer/, а не от cwd.

Тот же файл читается из двух рабочих каталогов: из writer/ (scripts.write) и из
collector/ (uvicorn, который монтирует роутер writer'а). Относительный путь
означал бы в этих двух случаях разные базы, и переписка тихо ушла бы не туда.
"""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent


def load():
    config = tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
    config["leads_db"] = (HOME / config["leads_db"]).resolve()
    config["threads_db"] = (HOME / config["threads_db"]).resolve()
    return config
