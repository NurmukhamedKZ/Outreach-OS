FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY collector/ collector/
COPY writer/ writer/
COPY sender/ sender/
COPY main.py ./

# scrapling[fetchers] качает свой Chromium отдельно от pip-пакета — без этого
# шага collect.* и probe.* падают при первом обращении к сети.
RUN uv run scrapling install

EXPOSE 8787

CMD ["uv", "run", "python", "main.py"]
