"""Единственная точка подключения Langfuse — общая для всех трёх систем, как
config.py для секретов. Без ключей трейсинг молча выключен: наблюдаемость не
должна быть обязательной зависимостью для отправки писем или анализа лидов.
"""

from functools import lru_cache

from config import settings


@lru_cache
def langfuse_handler():
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return CallbackHandler()
