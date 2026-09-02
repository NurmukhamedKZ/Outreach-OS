"""Единственное место, где читаются переменные окружения — для всех трёх
систем. Ключ нужен коду — он импортирует `settings` отсюда, а не лезет в
os.environ напрямую.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parent / ".env", extra="ignore")

    serper_api_key: str | None = None
    openrouter_api_key: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None
    sender_node_url: str = "http://127.0.0.1:8788"
    # `uv run python main.py` и `uv run uvicorn main:app --reload` обязаны
    # биндиться на один и тот же адрес: иначе случайный повторный запуск не
    # падает с "address already in use", а тихо слушает недостижимый wildcard
    # рядом с рабочим процессом — вебхуки Node лидят на первый, второй мёртв.
    # В Docker переопределяется на 0.0.0.0 (backend слушает контейнерный порт).
    uvicorn_host: str = "127.0.0.1"
    sender_webhook_secret: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


settings = Settings()
