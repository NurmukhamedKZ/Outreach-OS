FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Корневые модули (config.py и т.д.) — общая точка входа для всех трёх систем,
# collector/writer/sender импортируют их напрямую (`from config import settings`).
COPY config.py llm_schema.py logctx.py logging_setup.py observability.py \
     activity.py analytics.py paths.py main.py ./
COPY collector/ collector/
COPY writer/ writer/
COPY sender/ sender/

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага collect.* и probe.* падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "python", "main.py"]
