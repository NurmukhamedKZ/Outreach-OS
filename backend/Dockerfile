FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Корневые модули (config.py и т.д.) — общая точка входа для всех трёх систем,
# collector и модули предметной области импортируют их напрямую (`from config import settings`).
COPY config.py llm_schema.py logctx.py logging_setup.py observability.py \
     activity.py analytics.py clock.py paths.py tasks.py sqlite_tools.py main.py ./
COPY alembic.ini ./
COPY core/ core/
COPY refusals/ refusals/
COPY companies/ companies/
COPY research/ research/
COPY backup/ backup/
COPY migrations/ migrations/
COPY collector/ collector/
COPY conversations/ conversations/
COPY delivery/ delivery/
# Песочница едет в образ, потому что collector/api.py импортирует её
# безусловно; ручек без SANDBOX=1 она всё равно не монтирует.
COPY sandbox/ sandbox/

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага сбор сегментов и исследование падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "python", "main.py"]
