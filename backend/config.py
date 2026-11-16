"""Единственное место, где читаются переменные окружения — для всех трёх
систем. Ключ нужен коду — он импортирует `settings` отсюда, а не лезет в
os.environ напрямую.
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parent / ".env", extra="ignore")

    openrouter_api_key: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None
    sender_node_url: str = "http://127.0.0.1:8788"
    # Куда песочница шлёт события самой себе. Отдельной переменной, а не
    # склейкой из uvicorn_host: адрес вебхука должен переживать смену хоста
    # бэкенда (докер слушает 0.0.0.0, а ходить туда по 0.0.0.0 нельзя).
    sandbox_self_url: str = "http://127.0.0.1:8787"
    # Песочница: подменный транспорт и отдельная база вместо боевых. Флагом, а
    # не наличием файла: включать её должно быть так же явно, как выключать.
    sandbox: bool = False
    # Offline mode of a stand (#52): a model and pages that never leave the machine,
    # so an agent can run a batch and the seller for free. Invented text, so it
    # needs the sandbox and a stand's data directory (sandbox.mount refuses otherwise).
    offline: bool = False
    # `uv run python main.py` и `uv run uvicorn main:app --reload` обязаны
    # биндиться на один и тот же адрес: иначе случайный повторный запуск не
    # падает с "address already in use", а тихо слушает недостижимый wildcard
    # рядом с рабочим процессом — вебхуки Node лидят на первый, второй мёртв.
    # В Docker переопределяется на 0.0.0.0 (backend слушает контейнерный порт).
    uvicorn_host: str = "127.0.0.1"
    # Not PORT: `next dev` reads that one, and a shell exporting it for the
    # dashboard would move the backend too. A stand (scripts/stand) takes a free one.
    uvicorn_port: int = 8787
    # The irreplaceable layer's directory (paths.py derives the rest from it).
    # Moved only by a stand: an empty directory in production is an empty base,
    # refusals included.
    data_dir: Path = Path(__file__).resolve().parent / "data"
    sender_webhook_secret: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Где лежат снимки невосстановимого слоя (#8): папка в iCloud Drive. Нет её
    # родителя (Docker, не Mac) — бэкап выключен, и это видно на «Процессах».
    backup_dir: Path = (Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
                        / "Outreach-OS-backup")

    @field_validator("data_dir")
    @classmethod
    def _data_dir_is_absolute(cls, value: Path) -> Path:
        # An empty DATA_DIR is the cwd, a relative one depends on it: either is
        # a database nobody meant, without the refusals. Refused, not guessed.
        if not value.is_absolute():
            raise ValueError(f"DATA_DIR must be an absolute path, got {str(value)!r}")
        return value


settings = Settings()
