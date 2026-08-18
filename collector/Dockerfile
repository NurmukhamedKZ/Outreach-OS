# Собирается из корня репозитория (docker-compose build: context: .), потому что
# api.py монтирует writer/ рядом собой через sys.path — так же, как на хосте
# collector/ и writer/ лежат друг рядом с другом.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Каждый проект — свой uv sync, как на хосте: collector/.venv и writer/.venv.
# writer/.venv нужен отдельно, потому что кнопка write на /runs запускает его
# отдельным `uv run` (routes/runs.py, cwd=writer/), а не через collector/.venv.
COPY collector/pyproject.toml collector/uv.lock collector/
RUN cd collector && uv sync --frozen --no-dev

COPY writer/pyproject.toml writer/uv.lock writer/
RUN cd writer && uv sync --frozen --no-dev

COPY collector/ collector/
COPY writer/ writer/

WORKDIR /app/collector

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага scripts.collect и services.probes.* падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8787"]
