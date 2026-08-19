"""Точка входа. Три системы — один процесс, один пакетный корень (backend/).

Запуск:
  uv run python main.py                       # из backend/
  uv run uvicorn main:app --port 8787 --reload
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from collector.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
