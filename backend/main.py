"""Точка входа. Три системы — один процесс, один пакетный корень (backend/).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
  uv run uvicorn main:app --port 8787 --reload --timeout-graceful-shutdown 5
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

# Файловый лог для всех систем: дублирует то, что уже видно в консоли, а не
# заменяет. uvicorn настраивает свои логгеры (uvicorn, uvicorn.access) с
# propagate=False, поэтому запросы и старт/стоп до root не доходят —
# хендлер вешается на них отдельно, а не только на root.
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
file_handler = RotatingFileHandler(LOG_DIR / "backend.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter(fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
for logger_name in ("", "uvicorn", "uvicorn.access"):
    logging.getLogger(logger_name).addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)

from collector.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
