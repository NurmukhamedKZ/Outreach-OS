"""Конфиг системы 2. Файл ищется от каталога writer/, а не от cwd: процесс
запускают из разных каталогов."""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent


def load():
    return tomllib.loads((HOME / "config.toml").read_text(encoding="utf-8"))
