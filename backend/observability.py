"""Единственная точка подключения Langfuse — общая для всех трёх систем, как
config.py для секретов. Без ключей трейсинг молча выключен: наблюдаемость не
должна быть обязательной зависимостью для отправки писем или анализа лидов.
"""

import logging
from functools import lru_cache

from config import settings

_trace_logger = logging.getLogger("observability")


@lru_cache
def langfuse_handler():
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    # Без timeout здесь httpx.Client(timeout=None) — то есть НИКАКОГО таймаута:
    # если self-host Langfuse подвисает, get_trace_url() (см. log_trace ниже)
    # виснет на invoke() навсегда, вообще без исключения. Именно так один
    # LLM-вызов на живом прогоне вставал на 15+ минут — не из-за OpenRouter,
    # а из-за этого клиента. 10с достаточно для REST-вызова к своему серверу.
    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        timeout=10,
    )
    return CallbackHandler()


def log_trace(handler):
    """Строка в backend.log со ссылкой на Langfuse-трейс рядом с LLM-вызовом
    — чтобы прыгать из лога прямо в промпт/ответ, не открывая Langfuse UI
    руками и не гадая, какой из трейсов сессии это был.

    get_trace_url() при первом вызове ходит в сеть за project_id (см. SDK) —
    self-host недоступен точно так же, как любой другой сервис, и эта
    ссылка — необязательное удобство, а не часть протокола LLM-вызова.
    """
    if not handler or not handler.last_trace_id:
        return
    import langfuse

    try:
        url = langfuse.get_client().get_trace_url(trace_id=handler.last_trace_id)
    except Exception:
        _trace_logger.warning("не удалось получить ссылку на Langfuse-трейс", exc_info=True)
        return
    if url:
        _trace_logger.info(f"langfuse trace {url}")
