"""Общие LLM-хелперы анализа: структурированный вывод, кэш ответов.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_openrouter import ChatOpenRouter
from openrouter.errors import ResponseValidationError
from openrouter.utils import BackoffStrategy, RetryConfig

import logctx
from collector.services import storage
from config import settings
from observability import langfuse_handler, log_trace

# max_retries=0 не отключает ретраи SDK — оно лишь пропускает явную настройку
# retry_config у langchain_openrouter, и openrouter-клиент падает на СВОЙ
# дефолт (RetryConfig с max_elapsed_time=3 600 000мс, то есть час бэкоффа на
# одну попытку), который стекается поверх TRANSPORT_RETRIES ниже — на живом
# прогоне один вызов вставал на 9+ минут без единой строки в логе. Единственный
# надёжный способ выключить SDK-ретраи — передать strategy="none" явно per-call
# через model_kwargs (см. structured_model): тогда ретраи остаются ровно в
# одном месте — здесь, где виден их бэкофф и класс ошибки.
NO_SDK_RETRY = RetryConfig("none", BackoffStrategy(0, 0, 1, 0), False)
REASONING = {"enabled": False}
# Без явного timeout клиент не ограничен ничем: зависшее соединение блокирует
# invoke() навсегда, а вместе с ним и весь последовательный цикл analyze —
# ни ошибки, ни прогресса, ни возможности отменить джобу (cancel кооперативный,
# проверяется между итерациями). Засечено на живом прогоне: один вызов встал
# без движения дольше 10 минут. 60с достаточно для structured-ответа в пару КБ.
REQUEST_TIMEOUT_MS = 60_000
# retry_config провайдера ловит отказ соединения, но не обрыв тела ответа
# посреди чтения (RemoteProtocolError на протухшем keep-alive) — эта ошибка
# долетает до вызывающего целым исключением и валит весь прогон analyze на
# сотнях уже оплаченных вызовов. Три попытки здесь — тот же уровень, что и у
# retry_config провайдера, но на исключение, которое он не ловит. Тот же цикл
# ловит и OutputParserException: даже с require_parameters+strict провайдер
# изредка отвечает JSON мимо схемы (например "[]") — это не системная, а
# стохастическая порча одного вызова, повтор тем же промптом обычно проходит.
# И ResponseValidationError: обрыв тела ответа посреди чтения — тот же класс
# проблемы, что и RemoteProtocolError выше, но SDK openrouter заворачивает
# его в свой класс исключения вместо httpx.TransportError, и без явного
# перехвата это валило прогон на одном оборванном соединении.
TRANSPORT_RETRIES = 3

# Провайдеров со structured_outputs для модели из config.toml — около десятка;
# больше потоков чаще ловит 429, не даёт кэшу состязаться быстрее.
MAX_WORKERS = 8


def run_concurrent(ctx, targets, worker, label):
    """targets -> worker(target) в пуле потоков; check_cancelled/progress — из
    главного потока, по мере завершения задач.

    job_id пробрасывается в воркер-потоки явно (ThreadPoolExecutor не
    наследует contextvars вызывающего потока) — тот же приём, что в
    collect.py::in_parallel. Первая же ошибка worker() всплывает из
    future.result() без перехвата: analyze.* должен падать на первой
    ошибке LLM-вызова так же, как падал последовательный цикл, а не
    проглатывать её в сводке.
    """
    job_id = logctx.current_job_id()

    def run(target):
        logctx.set_job_id(job_id)
        return worker(target)

    results = []
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = [pool.submit(run, target) for target in targets]
        for number, future in enumerate(as_completed(futures), 1):
            ctx.check_cancelled()
            results.append(future.result())
            ctx.progress(number, len(targets), label)
    return results
# httpx timeout=REQUEST_TIMEOUT_MS доверять нельзя целиком: если провайдер
# стримит редкие keep-alive байты, каждый такой байт сбрасывает read-timeout,
# и запрос технически "не простаивает", просто генерирует ответ очень долго
# — на живом прогоне одно и то же TCP-соединение (тот же локальный порт)
# держалось открытым 30+ минут. HARD_TIMEOUT_S — независимый от HTTP-семантики
# потолок по настенным часам: invoke() уходит в отдельный поток, и мы просто
# перестаём его ждать по истечении срока, что бы внутри него ни происходило.
HARD_TIMEOUT_S = REQUEST_TIMEOUT_MS / 1000 + 15


def structured_model(model, schema):
    """Модель с валидацией схемы: повторы при невалидной схеме — на стороне LangChain.

    require_parameters ограничивает роутинг OpenRouter провайдерами, реально
    поддерживающими strict json_schema — без него запрос уходит и провайдерам,
    молча игнорирующим response_format, и модель отвечает произвольным JSON
    мимо схемы (например, "[]" вместо объекта — так падал analyze.reviews).
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY пуст. Задать в backend/.env — см. backend/.env.example")
    return ChatOpenRouter(
        model=model, api_key=settings.openrouter_api_key,
        temperature=0, reasoning=REASONING,
        timeout=REQUEST_TIMEOUT_MS,
        model_kwargs={"retries": NO_SDK_RETRY},
        openrouter_provider={"require_parameters": True},
    ).with_structured_output(schema, method="json_schema", strict=True)


def invoke(llm_model, messages, *, session_id, name, subject):
    """Реальный вызов модели — обёрнут langfuse-callback'ом. Кэш-хиты сюда не
    попадают: вызывающая сторона решает invoke() только на ветке без кэша."""
    handler = langfuse_handler()
    config = {
        "run_name": name,
        "metadata": {"langfuse_session_id": session_id, "langfuse_tags": [name]},
        "callbacks": [handler] if handler else [],
    }
    for attempt in range(1, TRANSPORT_RETRIES + 1):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(llm_model.invoke, messages, config=config)
            result = future.result(timeout=HARD_TIMEOUT_S)
            if handler:
                _log_trace_background(handler)
            return result
        except (httpx.TransportError, OutputParserException, ResponseValidationError, FutureTimeoutError):
            if attempt == TRANSPORT_RETRIES:
                raise
            time.sleep(attempt)
        finally:
            # wait=False: если invoke() всё ещё висит, не ждём его здесь —
            # поток-задание доработает в фоне сам и будет отброшен вместе
            # с пулом; блокировать на shutdown() значило бы свести на нет
            # весь смысл HARD_TIMEOUT_S выше.
            pool.shutdown(wait=False)


def _log_trace_background(handler):
    """log_trace() — необязательная ссылка для логов (см. её докстринг), а не
    часть протокола LLM-вызова, но её собственный HTTP-клиент (Langfuse REST
    API) на живом прогоне вставал на 10-25+ минут глубоко внутри SDK — не
    в OpenRouter-клиенте, который TRANSPORT_RETRIES/REQUEST_TIMEOUT_MS выше
    честно ограничивают. Вместо повторной охоты за очередным необёрнутым
    таймаутом в чужом SDK — наблюдаемость вынесена в фоновый поток: она
    физически не может задержать основной цикл analyze, что бы внутри неё
    ни зависло. job_id/entity снимаются здесь и переносятся вручную: поток
    не наследует contextvars вызывающего (тот же приём, что в
    collect.py::in_parallel)."""
    job_id, entity = logctx.current_job_id(), logctx.current_entity()

    def run():
        logctx.set_job_id(job_id)
        with logctx.entity(entity):
            log_trace(handler)

    threading.Thread(target=run, daemon=True).start()


def store_answer(db, kind, subject, model, prompt, answer):
    """Ответ кладётся в state.llm_answers вместе с запросом: через месяц промпт
    будет другим, и без запроса нельзя понять, на что модель отвечала."""
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, subject, model, prompt, json.dumps(answer, ensure_ascii=False)),
    )
    db.commit()


def answered(db, kind, subject, model, prompt):
    """Есть ли уже оплаченный ответ на этот запрос — в базе или файлом в raw/.

    Спрашиваются оба хранилища, потому что оба читает пересборка
    (rebuild.load_llm_answers). Проверять только базу значило бы платить второй
    раз за ответы, оставшиеся файлами; проверять только файлы — не видеть
    ничего, что записал analyze после переезда.
    """
    hit = db.execute(
        "SELECT 1 FROM state.llm_answers WHERE kind = ? AND subject = ?"
        " AND model = ? AND prompt = ? LIMIT 1",
        (kind, subject, model, prompt),
    ).fetchone()
    return bool(hit) or storage.has_llm_answer(model, prompt)
