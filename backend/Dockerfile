FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Корневые модули (config.py и т.д.) — общая точка входа для всех трёх систем:
# main.py и модули предметной области импортируют их напрямую (`from config import settings`).
COPY config.py llm_schema.py logging_setup.py observability.py \
     clock.py paths.py sqlite_tools.py main.py ./
COPY alembic.ini ./
COPY core/ core/
COPY refusals/ refusals/
COPY companies/ companies/
COPY research/ research/
COPY backup/ backup/
COPY migrations/ migrations/
COPY conversations/ conversations/
COPY delivery/ delivery/
COPY overview/ overview/
# Песочница едет в образ, потому что main.py импортирует её
# безусловно; ручек без SANDBOX=1 она всё равно не монтирует.
COPY sandbox/ sandbox/

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага сбор сегментов и исследование падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "python", "main.py"]
