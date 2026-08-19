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


settings = Settings()
