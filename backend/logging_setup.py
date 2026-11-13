"""Единственная точка настройки логирования бэкенда: и app-лог, и
network-лог, и маршрутизация scrapling-шума собраны здесь, а не размазаны по
main.py/fetch.py построчными logging.getLogger(...).setLevel(...).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"

_APP_FILE = "backend.log"
_NETWORK_FILE = "network.log"


def _has_file_handler(logger: logging.Logger, filename: str) -> bool:
    return any(getattr(h, "baseFilename", "").endswith(filename) for h in logger.handlers)


def configure() -> None:
    """Вызывается один раз из main.py при старте процесса. Идемпотентна:
    повторный вызов (например, повторный импорт main в тестах) не плодит
    вторые копии хендлеров на тех же логгерах."""
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter(fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    if not _has_file_handler(root, _APP_FILE):
        app_handler = RotatingFileHandler(LOG_DIR / _APP_FILE, maxBytes=10_000_000,
                                          backupCount=5, encoding="utf-8")
        app_handler.setFormatter(fmt)
        for logger_name in ("", "uvicorn", "uvicorn.access"):
            logging.getLogger(logger_name).addHandler(app_handler)
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and Telegram's carries the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    scrapling_logger = logging.getLogger("scrapling")
    if not _has_file_handler(scrapling_logger, _NETWORK_FILE):
        network_handler = RotatingFileHandler(LOG_DIR / _NETWORK_FILE, maxBytes=20_000_000,
                                              backupCount=3, encoding="utf-8")
        network_handler.setFormatter(fmt)
        scrapling_logger.addHandler(network_handler)

    # Scrapling пишет INFO на каждый запрос, включая штатные 404 (у листовой
    # рубрики нет страницы подрубрик) — WARNING отсекает это, оставляя только
    # реальные ретраи/отказы. propagate=False держит этот шум вне backend.log:
    # без этого он утёк бы в root через обычное наследование логгеров.
    scrapling_logger.setLevel(logging.WARNING)
    scrapling_logger.propagate = False
