"""Единственная точка настройки логирования бэкенда: и app-лог, и
network-лог, и маршрутизация scrapling-шума собраны здесь, а не размазаны по
main.py/fetch.py построчными logging.getLogger(...).setLevel(...).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import logctx

LOG_DIR = Path(__file__).parent / "logs"

_APP_FILE = "backend.log"
_NETWORK_FILE = "network.log"


class ContextFormatter(logging.Formatter):
    """Дописывает job_id/entity текущего contextvar-контекста в хвост строки
    — только непустые поля, чтобы служебные строки uvicorn (до старта первой
    джобы) не обрастали пустым 'job_id= entity='."""

    def format(self, record):
        base = super().format(record)
        tags = " ".join(
            f"{name}={value}"
            for name, value in (("job_id", logctx.current_job_id()), ("entity", logctx.current_entity()))
            if value is not None
        )
        return f"{base} {tags}" if tags else base


def _has_file_handler(logger, filename):
    return any(getattr(h, "baseFilename", "").endswith(filename) for h in logger.handlers)


def configure():
    """Вызывается один раз из main.py при старте процесса. Идемпотентна:
    повторный вызов (например, повторный импорт main в тестах) не плодит
    вторые копии хендлеров на тех же логгерах."""
    LOG_DIR.mkdir(exist_ok=True)
    fmt = ContextFormatter(fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    if not _has_file_handler(root, _APP_FILE):
        app_handler = RotatingFileHandler(LOG_DIR / _APP_FILE, maxBytes=10_000_000,
                                          backupCount=5, encoding="utf-8")
        app_handler.setFormatter(fmt)
        for logger_name in ("", "uvicorn", "uvicorn.access"):
            logging.getLogger(logger_name).addHandler(app_handler)
    root.setLevel(logging.INFO)

    scrapling_logger = logging.getLogger("scrapling")
    if not _has_file_handler(scrapling_logger, _NETWORK_FILE):
        network_handler = RotatingFileHandler(LOG_DIR / _NETWORK_FILE, maxBytes=20_000_000,
                                              backupCount=3, encoding="utf-8")
        network_handler.setFormatter(fmt)
        scrapling_logger.addHandler(network_handler)
        logging.getLogger("collector.fetch").addHandler(network_handler)

    # Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой
    # рубрики нет страницы подрубрик) — WARNING отсекает это, оставляя только
    # реальные ретраи/отказы. propagate=False держит этот шум вне backend.log:
    # без этого он утёк бы в root через обычное наследование логгеров.
    scrapling_logger.setLevel(logging.WARNING)
    scrapling_logger.propagate = False
    fetch_logger = logging.getLogger("collector.fetch")
    fetch_logger.setLevel(logging.INFO)
    fetch_logger.propagate = False
