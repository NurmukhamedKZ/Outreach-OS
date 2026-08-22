"""Базовая схема ответа модели — общая для collector и writer (как config.py и
observability.py, единственная копия для обеих систем).

Даже под strict json_schema модель иногда шлёт null для поля, у которого в
схеме есть дефолт, вместо самого дефолта: "жалоб не найдено" превращается в
unanswered_complaints: null, а не в 0. Без коррекции это валит весь ответ
OutputParserException'ом на ровном месте — данные-то валидные, просто null
там, где мог бы быть дефолт. Optional-поля (X | None) это не трогает: там
null — легитимный ответ, а не заглушка вместо дефолта.
"""

from typing import Union, get_args, get_origin

from pydantic import BaseModel, model_validator


class LLMSchema(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, data):
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            if data.get(name) is None and name in data and not _accepts_none(field.annotation):
                # is_required() — единственно верная проверка: field.default
                # у required-поля равен сентинелу PydanticUndefined, а не
                # None, так что "field.default is not None" ловит его как
                # будто дефолт есть и подставляет несериализуемый сентинел
                # вместо значения (падало TypeError глубоко в error-пути
                # langchain на ЛЮБОМ последующем провале валидации).
                if not field.is_required():
                    data[name] = field.get_default(call_default_factory=True)
        return data


def _accepts_none(annotation):
    return get_origin(annotation) is Union and type(None) in get_args(annotation)


def demo():
    class Reviews(LLMSchema):
        complaints: list[str] = []
        unanswered: int = 0
        note: str | None = None
        summary: str  # required, без дефолта — как Dossier.summary

    got = Reviews.model_validate(
        {"complaints": [], "unanswered": None, "note": None, "summary": "ok"}
    )
    assert got.unanswered == 0, "null не заменился дефолтом поля"
    assert got.note is None, "Optional-поле не должно трогаться"

    try:
        Reviews.model_validate(
            {"complaints": [], "unanswered": None, "note": None, "summary": None}
        )
        raise AssertionError("required-поле с null обязано падать валидацией, а не сентинелом")
    except ValueError as e:
        assert "PydanticUndefined" not in str(e), f"сентинел утёк в ошибку: {e}"

    print("llm_schema demo ok")


if __name__ == "__main__":
    demo()
