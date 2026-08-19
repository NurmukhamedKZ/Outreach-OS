"""Общие LLM-хелперы анализа: структурированный вывод, кэш ответов.

Сеть здесь есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента.
"""

import json
import os

from langchain_openrouter import ChatOpenRouter

from collector.services import storage

MAX_RETRIES = 2
REASONING = {"enabled": False}


def structured_model(model, schema):
    """Модель с валидацией схемы: повторы при невалидной схеме — на стороне LangChain."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY пуст. Поднять бэкенд: "
            "uv run --env-file .env uvicorn api:app --port 8787"
        )
    return ChatOpenRouter(
        model=model, temperature=0, max_retries=MAX_RETRIES, reasoning=REASONING,
    ).with_structured_output(schema, method="json_schema")


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
