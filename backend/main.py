"""Точка входа. Три системы — один процесс, один пакетный корень (backend/).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
  uv run uvicorn main:app --port 8787 --reload --timeout-graceful-shutdown 5
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

# backend.log (приложение) и network.log (сетевой шум scrapling) — оба
# настраиваются здесь, единой точкой; см. logging_setup.py.
import logging_setup  # noqa: E402
logging_setup.configure()

from collector.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
